"""Native VAPID Web Push notification service with Quiet Hours & Tornado override."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from mesoscale.db.app_store import AppStore
from mesoscale.providers.base import NwsAlert

logger = logging.getLogger("mesoscale.push")


def _normalize_vapid_subject(subject: str) -> str:
    """
    Ensure RFC 8292 VAPID `sub` claim uses a real IANA TLD (`mailto:` or `https:`).
    Apple's `web.push.apple.com` (iOS Safari PWA) rejects `.tailnet` / `.local` with 403 BadJwtToken.
    """
    raw = (subject or "").strip()
    if not raw or raw.endswith((".tailnet", ".local")) or not raw.startswith(("mailto:", "https://")):
        return "mailto:alerts@mesoscale.org"
    return raw


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def is_within_quiet_hours(
    now_utc: datetime, tz_name: str, quiet_start: str, quiet_end: str
) -> bool:
    """Evaluate whether `now_utc` falls inside [quiet_start, quiet_end] in local time."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Chicago")

    local_dt = now_utc.astimezone(tz)
    cur_mins = local_dt.hour * 60 + local_dt.minute

    def to_mins(hhmm: str) -> int:
        parts = hhmm.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])

    s = to_mins(quiet_start)
    e = to_mins(quiet_end)
    if s == e:
        return False
    if s < e:
        return s <= cur_mins < e
    # Overnight window (e.g., 22:30 -> 06:30)
    return cur_mins >= s or cur_mins < e


class WebPushService:
    """Manages VAPID keys, Quiet Hours policy, alert deduplication, and Web Push delivery."""

    def __init__(
        self,
        app_store: AppStore,
        vapid_keys_path: Path,
        vapid_subject: str = "mailto:alerts@mesoscale.org",
    ) -> None:
        self.app_store = app_store
        self.vapid_keys_path = vapid_keys_path
        self.vapid_subject = _normalize_vapid_subject(vapid_subject)
        self._public_key_b64: str = ""
        self._private_pem: str = ""
        self._vapid: Vapid | None = None

    def ensure_vapid_keys(self) -> str:
        if self._public_key_b64 and self._private_pem and self._vapid is not None:
            return self._public_key_b64

        if self.vapid_keys_path.exists():
            data = json.loads(self.vapid_keys_path.read_text())
            self._public_key_b64 = data["public_key"]
            self._private_pem = data["private_pem"]
            self._vapid = Vapid.from_pem(self._private_pem.encode("utf-8"))
            return self._public_key_b64

        self.vapid_keys_path.parent.mkdir(parents=True, exist_ok=True)
        vapid = Vapid()
        vapid.generate_keys()
        raw_pub = vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self._public_key_b64 = _b64url_no_pad(raw_pub)
        self._private_pem = vapid.private_pem().decode("utf-8")
        self._vapid = vapid
        self.vapid_keys_path.write_text(
            json.dumps(
                {"public_key": self._public_key_b64, "private_pem": self._private_pem},
                indent=2,
            )
        )
        return self._public_key_b64

    @property
    def public_key(self) -> str:
        return self.ensure_vapid_keys()

    async def send_payload_to_all(
        self,
        *,
        title: str,
        body: str,
        tag: str,
        severity: str = "info",
        url: str = "/",
        bypass_quiet_hours: bool = False,
        tz_name: str = "America/Chicago",
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        prefs = await self.app_store.get_notification_prefs()
        if not prefs.get("enabled", True):
            return {"sent": 0, "subscriber_count": 0, "suppressed": "disabled"}

        ref_now = now_utc or datetime.now(UTC)
        if (
            not bypass_quiet_hours
            and prefs.get("quiet_hours_enabled", True)
            and is_within_quiet_hours(
                ref_now,
                tz_name,
                str(prefs.get("quiet_start", "22:30")),
                str(prefs.get("quiet_end", "06:30")),
            )
        ):
            return {"sent": 0, "subscriber_count": 0, "suppressed": "quiet_hours"}

        subs = await self.app_store.list_push_subscriptions()
        if not subs:
            return {"sent": 0, "subscriber_count": 0, "suppressed": "no_subscribers"}

        self.ensure_vapid_keys()
        assert self._vapid is not None
        payload_str = json.dumps(
            {
                "title": title,
                "body": body,
                "tag": tag,
                "severity": severity,
                "url": url,
                "timestamp": ref_now.isoformat(),
            }
        )

        sent = 0
        dead_endpoints: list[str] = []
        errors: list[str] = []
        for sub in subs:
            sub_info = {
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            }
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=sub_info,
                    data=payload_str,
                    vapid_private_key=self._vapid,
                    vapid_claims={"sub": self.vapid_subject},
                    ttl=3600,
                    timeout=12.0,
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                resp_text = getattr(exc.response, "text", "") or ""
                if status in (404, 410):
                    dead_endpoints.append(sub["endpoint"])
                    errors.append(f"Expired subscription removed (HTTP {status})")
                else:
                    msg = f"Push gateway HTTP {status}: {resp_text[:120] or str(exc)[:120]}"
                    logger.warning("WebPushException: %s", msg)
                    errors.append(msg)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.warning("WebPush transport error: %s", msg)
                errors.append(msg)

        for ep in dead_endpoints:
            await self.app_store.remove_push_subscription(ep)

        return {
            "sent": sent,
            "subscriber_count": len(subs),
            "failed": len(errors),
            "pruned": len(dead_endpoints),
            "errors": errors[:3],
        }

    async def evaluate_and_notify_location(
        self,
        *,
        location: dict[str, Any],
        alerts: list[NwsAlert],
        hourly_forecast: list[dict[str, Any]] | None = None,
        hail_risk: dict[str, Any] | None = None,
        prev_day_p50_map: dict[str, float] | None = None,
        now_utc: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Evaluate Garage Hail Shield + NWS Severe Warnings + Smart Forecast Shift / Bust / Ice triggers
        with deduplication and size-escalation awareness so each distinct threat notifies cleanly.
        """
        if not location.get("alerts_enabled", True):
            return []

        ref_now = now_utc or datetime.now(UTC)
        prefs = await self.app_store.get_notification_prefs()
        if not prefs.get("enabled", True):
            return []

        sent_cache = await self.app_store.get_setting("sent_alerts_cache", {})
        # Prune dedup entries older than 48h
        cutoff_iso = (ref_now - timedelta(hours=48)).isoformat()
        sent_cache = {k: v for k, v in sent_cache.items() if str(v) >= cutoff_iso}

        loc_id = location["id"]
        loc_name = location["name"]
        tz_name = location.get("timezone") or "America/Chicago"
        dispatched: list[dict[str, Any]] = []

        async def maybe_fire(
            dedup_key: str,
            title: str,
            body: str,
            severity: str,
            bypass_quiet: bool = False,
        ) -> None:
            if dedup_key in sent_cache:
                return
            res = await self.send_payload_to_all(
                title=title,
                body=body,
                tag=dedup_key,
                severity=severity,
                url=f"/?loc={loc_id}",
                bypass_quiet_hours=bypass_quiet,
                tz_name=tz_name,
                now_utc=ref_now,
            )
            if res.get("sent", 0) > 0:
                sent_cache[dedup_key] = ref_now.isoformat()
            dispatched.append({"key": dedup_key, **res})

        # 0. Garage Hail Shield Alert (NEXRAD approaching hail core OR NWS CAP/SPS hail tag)
        if prefs.get("hail_alerts", True) and hail_risk:
            h_status = str(hail_risk.get("status") or "none")
            h_size = float(hail_risk.get("max_hail_in") or 0.0)
            min_sz = float(prefs.get("hail_min_size_in", 0.50))
            bypass_hail_quiet = bool(prefs.get("hail_bypass_quiet_hours", True))
            if h_status in ("approaching", "imminent") and h_size >= min_sz:
                cells = hail_risk.get("approaching_cells") or []
                eta_min = hail_risk.get("eta_minutes")
                size_bucket = int(math.floor(h_size * 2.0 + 1e-6))  # 0.50" steps trigger re-escalation!
                if cells:
                    top_c = cells[0]
                    cell_tag = str(top_c.get("cell_key") or "CELL")
                    eta_txt = f"~{eta_min} min ETA" if eta_min is not None else "Approaching"
                    title = f"🧊 Hail Alert ({eta_txt}) — {loc_name}"
                    body = (
                        f"Move vehicles under cover: {hail_risk.get('hail_label', '')} hail core "
                        f"{top_c.get('distance_mi', '?')} mi {top_c.get('bearing_from_target', '')} "
                        f"tracking toward {loc_name} at {top_c.get('speed_mph', '?')} mph."
                    )
                else:
                    cell_tag = str(hail_risk.get("nws_alert_event") or "NWS_HAIL").replace(" ", "_")
                    title = f"🧊 Hail Threat Indicated — {loc_name}"
                    body = (
                        f"Move vehicles under cover: {hail_risk.get('headline') or 'Hail indicated at your location.'}"
                    )
                dedup_hail = f"hail:{loc_id}:{ref_now.strftime('%Y%m%d')}:{cell_tag}:b{size_bucket}"
                await maybe_fire(
                    dedup_hail,
                    title,
                    body,
                    severity="critical" if (h_status == "imminent" or h_size >= 1.0) else "warning",
                    bypass_quiet=bypass_hail_quiet or h_size >= 1.0,
                )

        # 1. NWS Warnings & Watches (Tornado / Extreme ALWAYS bypass Quiet Hours!)
        for alert in alerts:
            is_watch = "watch" in alert.event.lower()
            if is_watch and not prefs.get("nws_watches", True):
                continue
            if not is_watch and not prefs.get("nws_warnings", True):
                continue
            key = f"nws:{loc_id}:{alert.id or alert.event}"
            await maybe_fire(
                key,
                f"⚠️ {alert.event} — {loc_name}",
                alert.headline or alert.description[:160],
                severity="critical" if alert.is_life_threatening else "warning",
                bypass_quiet=alert.is_life_threatening,
            )

        hrs_list = hourly_forecast or []
        next_36 = hrs_list[:36]

        # 2. Freezing Rain / Ice Glaze Onset Alert
        if prefs.get("freezing_rain_risk", True):
            ice_hrs = [r for r in next_36 if r.get("freezing_rain_risk")]
            if ice_hrs:
                first_hr = ice_hrs[0]["valid_time_utc"][:13]
                tw_f = round(float(ice_hrs[0]["wet_bulb_c"]) * 1.8 + 32.0, 1)
                await maybe_fire(
                    f"ice:{loc_id}:{first_hr}",
                    f"🧊 Freezing Rain Glaze Risk — {loc_name}",
                    f"Sub-freezing wet-bulb ({tw_f}°F) modeled across {len(ice_hrs)} upcoming hours.",
                    severity="warning",
                )

        # 3. Bimodal Frontal Bust Risk Alert
        if prefs.get("frontal_bust_risk", True):
            bust_hrs = [r for r in next_36 if r.get("bust_risk_level") == "high" and not r.get("freezing_rain_risk")]
            if len(bust_hrs) >= 2:
                day_tag = bust_hrs[0]["valid_time_utc"][:10]
                await maybe_fire(
                    f"bust:{loc_id}:{day_tag}",
                    f"🌡️ High Frontal Bust Risk — {loc_name}",
                    bust_hrs[0].get("bust_summary") or "Large mesoscale vs synoptic temperature divergence.",
                    severity="info",
                )

        # 4. Smart Forecast Temperature Shift (>= threshold °F vs earlier run)
        shift_thresh_f = float(prefs.get("model_temp_shift_f", 8.0))
        if prev_day_p50_map and shift_thresh_f > 0 and len(hrs_list) > 6:
            for r in hrs_list[6:36]:
                vt = r["valid_time_utc"]
                if vt in prev_day_p50_map:
                    delta_f = (float(r["temp_p50_c"]) - float(prev_day_p50_map[vt])) * 1.8
                    if abs(delta_f) >= shift_thresh_f:
                        dir_word = "warmer" if delta_f > 0 else "colder"
                        await maybe_fire(
                            f"shift:{loc_id}:{vt[:13]}",
                            f"📉 Forecast Shifted {abs(delta_f):.0f}°F {dir_word.title()} — {loc_name}",
                            f"Mesoscale AI adjusted {vt[11:16]} UTC forecast by {delta_f:+.1f}°F as new high-res runs arrived.",
                            severity="info",
                        )
                        break

        await self.app_store.set_setting("sent_alerts_cache", sent_cache)
        return dispatched
