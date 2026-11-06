"""Tests for clean first-boot onboarding, FastAPI endpoints, Quiet Hours, and Tornado bypass."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import polars as pl
import pytest
from mesoscale.config import Settings
from mesoscale.main import create_app
from mesoscale.providers.base import NwsAlert
from mesoscale.services.push_service import is_within_quiet_hours


@pytest.mark.asyncio
async def test_clean_first_boot_and_quiet_hours_tornado_override(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "data", enable_scheduler=False)
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. First boot MUST start with zero pre-pinned locations and a valid VAPID key
            boot = (await client.get("/api/bootstrap")).json()
            assert boot["app_name"] == "Mesoscale"
            assert boot["pinned_locations"] == []
            assert len(boot["vapid_public_key"]) >= 80

            # 2. Health endpoint
            health = (await client.get("/api/health")).json()
            assert health["status"] == "ok"
            assert health["pinned_locations_count"] == 0

            # 3. Subscribe a browser Web Push endpoint
            sub_res = await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://fcm.googleapis.com/fcm/send/fake-token-123",
                    "keys": {"p256dh": "fake_p256dh_key", "auth": "fake_auth_key"},
                    "user_agent": "TestBrowser",
                },
            )
            assert sub_res.status_code == 200

            # 4. Verify Quiet Hours suppression vs Tornado Warning life-safety bypass!
            # 08:30 UTC == 02:30 AM CST (inside 22:30 -> 06:30 quiet hours)
            night_utc = datetime(2026, 11, 6, 8, 30, tzinfo=UTC)
            assert is_within_quiet_hours(night_utc, "America/Chicago", "22:30", "06:30") is True

            eng = app.state.engine
            with patch("mesoscale.services.push_service.webpush") as mock_wp:
                # Routine forecast shift during Quiet Hours -> MUST be suppressed!
                routine = await eng.push.send_payload_to_all(
                    title="Forecast Shift",
                    body="Temp shifted 9°F",
                    tag="shift-1",
                    bypass_quiet_hours=False,
                    tz_name="America/Chicago",
                    now_utc=night_utc,
                )
                assert routine["suppressed"] == "quiet_hours"
                assert mock_wp.call_count == 0

                # Tornado Warning at 2:30 AM CST -> MUST bypass Quiet Hours and fire!
                tor_alert = NwsAlert(
                    id="urn:oid:tornado-1",
                    event="Tornado Warning",
                    severity="Extreme",
                    certainty="Observed",
                    urgency="Immediate",
                    headline="Tornado Warning for Cleveland and Oklahoma Counties",
                    description="Confirmed tornado near Moore moving NE.",
                    instruction="Take shelter now!",
                    onset_utc="2026-11-06T08:28:00Z",
                    expires_utc="2026-11-06T09:15:00Z",
                    geometry=None,
                )
                assert tor_alert.is_life_threatening is True
                dispatched = await eng.push.evaluate_and_notify_location(
                    location={"id": 1, "name": "Oklahoma City", "timezone": "America/Chicago"},
                    alerts=[tor_alert],
                    hourly_forecast=[],
                    now_utc=night_utc,
                )
                assert len(dispatched) == 1
                assert dispatched[0]["sent"] == 1
                assert mock_wp.call_count == 1

            # 5. Verify /api/locations/{id}/verification with DuckDB TIMESTAMPTZ -> Polars UTC
            loc = await eng.app_store.create_location(
                name="Norman",
                admin1="Oklahoma",
                country="US",
                lat=35.2226,
                lon=-97.4395,
                elevation_m=357.0,
                timezone_name="America/Chicago",
                stations=[],
            )
            lid = loc["id"]
            base_vt = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            snap_rows = []
            obs_rows = []
            for h_ago in range(4, 10):
                vt = base_vt - timedelta(hours=h_ago)
                it = vt - timedelta(hours=12)
                snap_rows.append(
                    {
                        "location_id": lid,
                        "model_id": "nbm",
                        "init_time_utc": it,
                        "available_at_utc": it + timedelta(hours=1),
                        "valid_time_utc": vt,
                        "lead_hours": 12.0,
                        "is_subhourly_15m": False,
                        "temp_c": 18.0,
                        "dewpoint_c": 10.0,
                        "wind_ms": 4.0,
                        "gust_ms": 7.0,
                        "wind_dir_deg": 180.0,
                        "precip_mm": 0.0,
                        "snowfall_cm": 0.0,
                        "precip_prob": 10.0,
                        "cloud_pct": 20.0,
                        "pressure_hpa": 1014.0,
                        "solar_wm2": 400.0,
                        "cape_jkg": 0.0,
                        "cin_jkg": 0.0,
                        "freezing_level_m": 3200.0,
                        "ring_precip_max_mm": 0.0,
                        "ring_precip_mean_mm": 0.0,
                        "ring_temp_grad_c": 0.2,
                        "ring_theta_e_grad_k": 0.5,
                        "ring_gust_max_ms": 7.5,
                    }
                )
                obs_rows.append(
                    {
                        "location_id": lid,
                        "valid_time_utc": vt,
                        "temp_c": 17.0,
                        "dewpoint_c": 9.5,
                        "wet_bulb_c": 13.0,
                        "wind_ms": 4.2,
                        "gust_ms": 7.2,
                        "wind_dir_deg": 175.0,
                        "precip_1h_mm": 0.0,
                        "pressure_hpa": 1014.2,
                        "solar_wm2": 390.0,
                        "user_condition": None,
                        "station_count": 2,
                    }
                )
            await eng.telemetry.upsert_forecast_snapshots(pl.DataFrame(snap_rows))
            await eng.telemetry.upsert_blended_hourly(pl.DataFrame(obs_rows))
            ver_res = await client.get(f"/api/locations/{lid}/verification?days=30&lead_bucket=all")
            assert ver_res.status_code == 200
            ver_json = ver_res.json()
            assert len(ver_json["leaderboard"]) == 1
            assert ver_json["leaderboard"][0]["model_id"] == "nbm"
            assert len(ver_json["replay_72h"]) == 6

