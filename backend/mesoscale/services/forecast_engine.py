"""Central forecasting, Day-1 365-day historical backfill, and <2s Ephemeral lookup service."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import polars as pl

from mesoscale.config import Settings
from mesoscale.db.app_store import AppStore
from mesoscale.db.telemetry_store import TelemetryStore
from mesoscale.ml.bust_detector import summarize_storm_winter_mode
from mesoscale.ml.conformal_aci import update_aci_state_with_residuals
from mesoscale.ml.features import pivot_inference_grid_to_wide
from mesoscale.ml.kalman_nowcast import update_kalman_state
from mesoscale.ml.mos_trainer import (
    TrainedMosArtifact,
    compute_consensus_predictions,
    predict_with_artifact,
    train_and_gate_mos_model,
)
from mesoscale.ml.thermodynamics import apparent_temperature_c, wet_bulb_celsius
from mesoscale.providers.base import StationMeta
from mesoscale.providers.hail_tracker import HailTrackerClient
from mesoscale.providers.nws_iem import NwsIemClient
from mesoscale.providers.ok_mesonet import (
    OklahomaMesonetClient,
    records_to_station_polars,
)
from mesoscale.providers.open_meteo import OpenMeteoClient
from mesoscale.providers.pws_adapter import PersonalWeatherStationClient
from mesoscale.providers.spc_outlook import SpcOutlookClient
from mesoscale.providers.station_blender import (
    blend_hourly_observations_for_location,
    discover_ground_truth_stations,
    rollup_station_observations_to_hourly,
)
from mesoscale.providers.subseasonal import SubseasonalClient
from mesoscale.services.push_service import WebPushService


def _build_daily_outlook(hourly: list[dict[str, Any]], tz_name: str) -> list[dict[str, Any]]:
    """Aggregate hourly probabilistic forecasts into local-timezone 10-day daily cards."""
    if not hourly:
        return []
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Chicago")

    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in hourly:
        dt = datetime.fromisoformat(r["valid_time_utc"].replace("Z", "+00:00")).astimezone(tz)
        day_key = dt.strftime("%Y-%m-%d")
        by_day.setdefault(day_key, []).append(r)

    days: list[dict[str, Any]] = []
    for day_key in sorted(by_day.keys())[:10]:
        hrs = by_day[day_key]
        if len(hrs) < 4 and len(days) > 0:
            continue
        p50s = [float(x["temp_p50_c"]) for x in hrs]
        p90s = [float(x["temp_p90_c"]) for x in hrs]
        p10s = [float(x["temp_p10_c"]) for x in hrs]
        pops = [float(x["precip_prob"]) for x in hrs]
        qpfs = [float(x["precip_p50_mm"]) for x in hrs]
        gusts = [float(x["gust_p50_ms"]) for x in hrs]

        ptype_rank = {"none": 0, "rain": 1, "snow": 2, "sleet": 3, "freezing_rain": 4}
        dom_ptype = max((str(x["precip_type"]) for x in hrs), key=lambda p: ptype_rank.get(p, 0))
        has_bust = any(x.get("bust_risk_level") == "high" for x in hrs)

        days.append(
            {
                "date": day_key,
                "temp_max_c": round(max(p50s), 1),
                "temp_min_c": round(min(p50s), 1),
                "temp_max_p90_c": round(max(p90s), 1),
                "temp_min_p10_c": round(min(p10s), 1),
                "precip_prob_max": round(max(pops), 2),
                "precip_total_mm": round(sum(qpfs), 2),
                "dominant_precip_type": dom_ptype,
                "max_gust_ms": round(max(gusts), 1),
                "cloud_mean_pct": round(float(np.mean([float(x["cloud_pct"]) for x in hrs])), 0),
                "has_high_bust_risk": has_bust,
            }
        )
    return days


class ForecastEngineService:
    """Coordinates live ingestion, Kalman/ACI online updates, Day-1 backfill, and Ephemeral mode."""

    def __init__(
        self,
        *,
        settings: Settings,
        app_store: AppStore,
        telemetry_store: TelemetryStore,
        push_service: WebPushService,
    ) -> None:
        self.settings = settings
        self.app_store = app_store
        self.telemetry = telemetry_store
        self.push = push_service

        self.open_meteo = OpenMeteoClient(
            ring_radius_km=settings.ring_radius_km,
            timeout_s=settings.open_meteo_timeout_s,
        )
        self.mesonet = OklahomaMesonetClient()
        self.nws = NwsIemClient(user_agent=settings.nws_user_agent)
        self.spc = SpcOutlookClient()
        self.hail = HailTrackerClient()
        self.subseasonal = SubseasonalClient()
        self.pws = PersonalWeatherStationClient()

        self._loaded_artifacts: dict[int, tuple[int, TrainedMosArtifact]] = {}
        self._ephemeral_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._backfill_tasks: dict[int, asyncio.Task[None]] = {}

    def _get_artifact(self, location_id: int, model_rec: dict[str, Any] | None) -> TrainedMosArtifact | None:
        if not model_rec or not model_rec.get("artifact_path"):
            return None
        ver = int(model_rec.get("version") or 0)
        cached = self._loaded_artifacts.get(location_id)
        if cached and cached[0] == ver:
            return cached[1]
        p = Path(model_rec["artifact_path"])
        if not p.exists():
            return None
        try:
            art: TrainedMosArtifact = joblib.load(p)
            self._loaded_artifacts[location_id] = (ver, art)
            return art
        except Exception:
            return None

    # ── Pinning & Day-1 Cold-Start Backfill ───────────────────────────────

    async def pin_new_location(
        self,
        *,
        name: str | None,
        admin1: str | None,
        lat: float,
        lon: float,
        elevation_m: float | None = None,
        timezone_name: str | None = None,
        country: str | None = None,
        custom_name: str | None = None,
        address_label: str | None = None,
        alerts_enabled: bool = True,
        trigger_backfill: bool = True,
    ) -> dict[str, Any]:
        meta = None
        if not name or elevation_m is None or not timezone_name or not country:
            meta = await self.nws.reverse_point_metadata(lat, lon)
            name = name or meta.name
            admin1 = admin1 if admin1 is not None else meta.admin1
            elevation_m = elevation_m if elevation_m is not None else meta.elevation_m
            timezone_name = timezone_name or meta.timezone
            country = country or meta.country

        clean_custom = (custom_name or "").strip()
        final_name = clean_custom or (name or "").strip() or f"{lat:.4f}, {lon:.4f}"
        final_addr = (address_label or "").strip()
        if not final_addr:
            if clean_custom and name and name.strip() != clean_custom:
                final_addr = name.strip()
            elif meta and meta.display_address:
                final_addr = meta.display_address

        stations = await discover_ground_truth_stations(lat, lon, self.mesonet, self.nws)
        loc = await self.app_store.create_location(
            name=final_name,
            address_label=final_addr,
            admin1=admin1 or "",
            country=(country or "US").upper(),
            lat=lat,
            lon=lon,
            elevation_m=float(elevation_m),
            timezone_name=timezone_name,
            stations=[s.to_dict() for s in stations],
            alerts_enabled=alerts_enabled,
        )
        loc_id = int(loc["id"])

        # Perform immediate live refresh so user gets forecast in ~2 seconds while backfill runs
        try:
            await self.refresh_location_live(loc_id)
        except Exception:
            pass

        if trigger_backfill and loc.get("backfill_status") in ("pending", "error"):
            self.launch_background_backfill(loc_id)
        return await self.app_store.get_location(loc_id) or loc

    def launch_background_backfill(self, location_id: int) -> None:
        existing = self._backfill_tasks.get(location_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self.run_day1_backfill_and_train(location_id))
        self._backfill_tasks[location_id] = task

    async def run_day1_backfill_and_train(self, location_id: int) -> dict[str, Any]:
        """
        Day-1 Zero Cold-Start Pipeline:
        1) Pulls 365 days of historical multi-model forecasts (HRRR, NBM, ECMWF IFS, GFS).
        2) Pulls matching historical ground truth from IEM ASOS / Oklahoma Mesonet / ERA5 fallback.
        3) Blends multi-station observations to exact elevation & trains CatBoost + LightGBM MOS.
        """
        loc = await self.app_store.get_location(location_id)
        if not loc:
            return {"ok": False, "reason": "not_found"}

        try:
            await self.app_store.update_location_status(
                location_id,
                backfill_status="running",
                backfill_progress=0.08,
                backfill_message="Downloading 365 days of archived HRRR, NBM, ECMWF & GFS runs...",
                model_status="training",
            )

            now_utc = datetime.now(UTC)
            end_dt = now_utc - timedelta(days=2)
            start_dt = end_dt - timedelta(days=self.settings.backfill_days)
            s_str = start_dt.strftime("%Y-%m-%d")
            e_str = end_dt.strftime("%Y-%m-%d")

            lat, lon = float(loc["lat"]), float(loc["lon"])
            stations = [StationMeta(**s) for s in loc.get("stations", [])]

            # Fetch historical model archives concurrently (polite 4-model fanout)
            hist_models = ("hrrr", "nbm", "ecmwf_ifs", "gfs")
            model_dfs = await asyncio.gather(
                *[
                    self.open_meteo.fetch_historical_model_window(
                        location_id=location_id,
                        lat=lat,
                        lon=lon,
                        model_id=mid,
                        start_date=s_str,
                        end_date=e_str,
                    )
                    for mid in hist_models
                ]
            )
            non_empty_models = [d for d in model_dfs if not d.is_empty()]
            if non_empty_models:
                await self.telemetry.upsert_forecast_snapshots(
                    pl.concat(non_empty_models, how="vertical")
                )

            await self.app_store.update_location_status(
                location_id,
                backfill_progress=0.48,
                backfill_message="Fetching historical ASOS & Oklahoma Mesonet ground-truth observations...",
            )

            obs_records: list[dict[str, Any]] = []
            # 1. Pull historical ASOS from Iowa Environmental Mesonet for paired ASOS stations
            asos_stations = [s for s in stations if s.source == "nws_asos"]
            for ast in asos_stations[:2]:
                recs = await self.nws.fetch_iem_historical_asos(ast.station_id, start_dt, now_utc)
                obs_records.extend(recs)

            # 2. Supplement recent 14 days of 5-minute Oklahoma Mesonet .mts files when in Oklahoma
            meso_stations = [s for s in stations if s.source == "ok_mesonet"]
            if meso_stations:
                primary_meso = meso_stations[0].station_id
                recent_days = [now_utc - timedelta(days=d) for d in range(1, 15)]
                mts_batches = await asyncio.gather(
                    *[self.mesonet.fetch_station_mts_day(primary_meso, day) for day in recent_days]
                )
                for batch in mts_batches:
                    obs_records.extend(batch)

            # 3. Ensure complete 365-day coverage via ERA5-Land fallback if station archive had gaps
            if len(obs_records) < 1500:
                era5_recs = await self.open_meteo.fetch_era5_reanalysis_fallback(
                    lat, lon, s_str, e_str
                )
                obs_records.extend(era5_recs)

            if obs_records:
                raw_obs_df = records_to_station_polars(obs_records)
                await self.telemetry.upsert_station_observations(raw_obs_df)
                hourly_st = rollup_station_observations_to_hourly(raw_obs_df)
                checkins = await self.app_store.list_precip_checkins(location_id, limit=500)
                blended = blend_hourly_observations_for_location(
                    location_id=location_id,
                    target_elevation_m=float(loc["elevation_m"]),
                    stations=stations,
                    hourly_station_df=hourly_st,
                    user_checkins=checkins,
                )
                await self.telemetry.upsert_blended_hourly(blended)

            await self.app_store.update_location_status(
                location_id,
                backfill_progress=0.78,
                backfill_message="Training CatBoost MultiQuantile + LightGBM Regime MOS...",
            )

            train_res = await self.retrain_location_model(location_id)
            await self.refresh_location_live(location_id)

            model_state = "trained" if train_res.get("promoted") else "fallback"
            msg = (
                f"AI trained on {train_res.get('train_rows', 0):,} local hours "
                f"(Holdout MAE {train_res.get('mesoscale_mae_f', '—')}°F vs NBM {train_res.get('nbm_mae_f', '—')}°F)"
                if train_res.get("promoted")
                else str(train_res.get("reason") or "Live Multi-Model Consensus active")
            )
            await self.app_store.update_location_status(
                location_id,
                backfill_status="ready",
                backfill_progress=1.0,
                backfill_message=msg,
                model_status=model_state,
            )
            return {"ok": True, **train_res}
        except Exception as exc:
            await self.app_store.update_location_status(
                location_id,
                backfill_status="error",
                backfill_message=f"Backfill error: {exc}",
                model_status="fallback",
            )
            return {"ok": False, "error": str(exc)}

    def prune_old_model_files(self, location_id: int, keep_min_version: int) -> int:
        """Delete superseded `.joblib` artifacts so nightly retrains never bloat disk over 2+ years."""
        removed = 0
        if not self.settings.models_dir.exists():
            return 0
        prefix = f"location_{location_id}_v"
        for p in self.settings.models_dir.glob(f"{prefix}*.joblib"):
            ver_part = p.stem[len(prefix) :]
            if ver_part.isdigit() and int(ver_part) < keep_min_version:
                try:
                    p.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
        return removed

    def remove_all_location_models(self, location_id: int) -> None:
        self._loaded_artifacts.pop(location_id, None)
        self.prune_old_model_files(location_id, keep_min_version=10**9)

    async def retrain_location_model(self, location_id: int) -> dict[str, Any]:
        train_df = await self.telemetry.build_training_pairs(
            location_id, retention_days=self.settings.retention_days
        )
        existing_rec = await self.app_store.get_model_record(location_id)
        existing_ver = int(existing_rec["version"]) if existing_rec else 0
        existing_path = str(existing_rec["artifact_path"]) if existing_rec else None

        artifact, report = await asyncio.to_thread(
            train_and_gate_mos_model,
            location_id=location_id,
            train_df=train_df,
            models_dir=self.settings.models_dir,
            existing_version=existing_ver,
            existing_artifact_path=existing_path,
        )
        if artifact is not None and report.get("promoted"):
            self._loaded_artifacts[location_id] = (artifact.version, artifact)
            await self.app_store.upsert_model_record(
                location_id,
                version=artifact.version,
                train_rows=int(report["train_rows"]),
                artifact_path=str(report["artifact_path"]),
                metrics=report,
            )
            await self.app_store.update_location_status(location_id, model_status="trained")
            # Retain active Champion (`version`) + 1 prior rollback backup (`version - 1`)
            await asyncio.to_thread(
                self.prune_old_model_files, location_id, max(1, artifact.version - 1)
            )
        return report

    # ── Live Multi-Model + 5-Min Observation Refresh & Online Learning ────

    async def refresh_observations_and_kalman(self, location_id: int) -> dict[str, Any] | None:
        loc = await self.app_store.get_location(location_id)
        if not loc:
            return None
        stations = [StationMeta(**s) for s in loc.get("stations", [])]
        meso_ids = {s.station_id for s in stations if s.source == "ok_mesonet"}
        asos_ids = [s.station_id for s in stations if s.source == "nws_asos"]

        records: list[dict[str, Any]] = []
        if meso_ids:
            records.extend(await self.mesonet.fetch_latest_mdf(station_filter=meso_ids))
        for aid in asos_ids[:2]:
            records.extend(await self.nws.fetch_latest_asos_observations(aid, limit=4))

        # Optional backyard Personal Weather Station with radiation-spike QC
        pws_cfg = await self.app_store.get_pws_config()
        ref_t = next((float(r["temp_c"]) for r in records if r.get("temp_c") is not None), None)
        pws_rec = await self.pws.fetch_latest(pws_cfg, reference_temp_c=ref_t)
        if pws_rec:
            records.append(pws_rec)

        if not records:
            return None

        raw_df = records_to_station_polars(records)
        await self.telemetry.upsert_station_observations(raw_df)
        hourly_st = rollup_station_observations_to_hourly(raw_df)
        checkins = await self.app_store.list_precip_checkins(location_id, limit=30)
        blended = blend_hourly_observations_for_location(
            location_id=location_id,
            target_elevation_m=float(loc["elevation_m"]),
            stations=stations,
            hourly_station_df=hourly_st,
            user_checkins=checkins,
        )
        if not blended.is_empty():
            await self.telemetry.upsert_blended_hourly(blended)

        # Compute instantaneous live blended reading for 5-min Kalman Nowcast filter
        latest_by_st: dict[str, dict[str, Any]] = {}
        for r in sorted(records, key=lambda x: x["valid_time_utc"]):
            if r.get("temp_c") is not None:
                latest_by_st[str(r["station_id"]).upper()] = r

        if not latest_by_st:
            return None

        st_map = {s.station_id.upper(): s for s in stations}
        t_num = t_den = td_num = td_den = ws_num = ws_den = wg_max = 0.0
        latest_ts = max(r["valid_time_utc"] for r in latest_by_st.values())

        for stid, r in latest_by_st.items():
            meta = st_map.get(stid)
            w = meta.blend_weight if (meta and meta.blend_weight > 0) else 0.4
            dz = float(loc["elevation_m"]) - (meta.elevation_m if meta else float(loc["elevation_m"]))
            t_adj = float(r["temp_c"]) + dz * -0.0065
            t_num += w * t_adj
            t_den += w
            if r.get("dewpoint_c") is not None:
                td_num += w * (float(r["dewpoint_c"]) + dz * -0.002)
                td_den += w
            if r.get("wind_ms") is not None:
                ws_num += w * float(r["wind_ms"])
                ws_den += w
            if r.get("gust_ms") is not None:
                wg_max = max(wg_max, float(r["gust_ms"]))

        live_temp = t_num / max(t_den, 1e-6)
        live_td = min(live_temp, td_num / td_den) if td_den > 0 else live_temp - 4.0
        live_wind = ws_num / ws_den if ws_den > 0 else None

        # Compare against un-nudged base prediction at nearest hour to update Kalman [bias, trend]
        now_utc = datetime.now(UTC)
        grid = await self.telemetry.query_inference_grid(
            location_id,
            now_utc,
            now_utc - timedelta(hours=1),
            now_utc + timedelta(hours=2),
        )
        model_rec = await self.app_store.get_model_record(location_id)
        prev_kalman = (model_rec or {}).get("kalman_state")
        prev_aci = (model_rec or {}).get("aci_state")

        if not grid.is_empty():
            wide = pivot_inference_grid_to_wide(grid, now_utc.timestamp())
            art = self._get_artifact(location_id, model_rec)
            base_preds = (
                predict_with_artifact(art, wide, kalman_state=None, aci_state=prev_aci, now_utc=now_utc)
                if art is not None
                else compute_consensus_predictions(wide, kalman_state=None, aci_state=prev_aci, now_utc=now_utc)
            )
            if base_preds:
                cur_p = base_preds[0]
                new_kalman = update_kalman_state(
                    prev_kalman,
                    obs_time_utc=latest_ts,
                    obs_temp_c=live_temp,
                    pred_temp_c=float(cur_p["temp_p50_c"]),
                    obs_dewpoint_c=live_td,
                    pred_dewpoint_c=float(cur_p["dewpoint_p50_c"]),
                    obs_wind_ms=live_wind,
                    pred_wind_ms=float(cur_p["wind_p50_ms"]),
                    raw_model_preds_c=cur_p.get("raw_models_temp_c"),
                )
                await self.app_store.update_online_states(location_id, kalman_state=new_kalman)

        return {
            "valid_time_utc": latest_ts.isoformat(),
            "age_minutes": round(max(0.0, (now_utc - latest_ts).total_seconds() / 60.0), 1),
            "temp_c": round(live_temp, 2),
            "dewpoint_c": round(live_td, 2),
            "wet_bulb_c": round(float(wet_bulb_celsius(live_temp, dewpoint_c=live_td)), 2),
            "feels_like_c": round(
                float(apparent_temperature_c(live_temp, live_td, live_wind or 0.0)), 2
            ),
            "wind_ms": round(live_wind, 2) if live_wind is not None else None,
            "gust_ms": round(wg_max, 2) if wg_max > 0 else None,
            "contributing_stations": list(latest_by_st.keys()),
        }

    async def refresh_location_live(self, location_id: int) -> None:
        """Full hourly cycle: ingest 6 models, refresh obs + Kalman, update ACI, log predictions, check push."""
        loc = await self.app_store.get_location(location_id)
        if not loc:
            return

        # Snapshot previous P50 curve for Smart Forecast-Shift alert detection
        now_utc = datetime.now(UTC)
        _, prev_ai = await self.telemetry.query_verification_data(location_id, days=2)
        prev_p50_map: dict[str, float] = {}
        if not prev_ai.is_empty():
            for r in prev_ai.to_dicts():
                prev_p50_map[r["valid_time_utc"].isoformat()] = float(r["temp_p50_c"])

        snap_df = await self.open_meteo.fetch_all_live_models(
            location_id, float(loc["lat"]), float(loc["lon"])
        )
        if not snap_df.is_empty():
            await self.telemetry.upsert_forecast_snapshots(snap_df)

        await self.refresh_observations_and_kalman(location_id)

        # Online Adaptive Conformal Inference (ACI) update on newly verified past predictions
        _, recent_ai_vs_obs = await self.telemetry.query_verification_data(location_id, days=3)
        model_rec = await self.app_store.get_model_record(location_id)
        aci_state = (model_rec or {}).get("aci_state")
        if not recent_ai_vs_obs.is_empty():
            tail = recent_ai_vs_obs.tail(48)
            aci_state = update_aci_state_with_residuals(
                aci_state,
                lead_hours=tail["effective_lead_h"].to_numpy(),
                p10=tail["temp_p10_c"].to_numpy(),
                p90=tail["temp_p90_c"].to_numpy(),
                obs=tail["obs_temp_c"].to_numpy(),
            )
            await self.app_store.update_online_states(location_id, aci_state=aci_state)

        # Generate & permanently log out-of-sample ensemble_predictions
        hourly = await self._compute_pinned_hourly(location_id, now_utc)
        if hourly:
            issued = now_utc.replace(minute=0, second=0, microsecond=0)
            ens_df = pl.DataFrame(
                [
                    {
                        "location_id": location_id,
                        "issued_at_utc": issued,
                        "valid_time_utc": datetime.fromisoformat(
                            r["valid_time_utc"].replace("Z", "+00:00")
                        ),
                        "lead_hours": float(r["lead_hours"]),
                        "temp_p05_c": float(r["temp_p05_c"]),
                        "temp_p10_c": float(r["temp_p10_c"]),
                        "temp_p25_c": float(r["temp_p25_c"]),
                        "temp_p50_c": float(r["temp_p50_c"]),
                        "temp_p75_c": float(r["temp_p75_c"]),
                        "temp_p90_c": float(r["temp_p90_c"]),
                        "temp_p95_c": float(r["temp_p95_c"]),
                        "dewpoint_p50_c": float(r["dewpoint_p50_c"]),
                        "wet_bulb_c": float(r["wet_bulb_c"]),
                        "wind_p50_ms": float(r["wind_p50_ms"]),
                        "gust_p50_ms": float(r["gust_p50_ms"]),
                        "gust_p90_ms": float(r["gust_p90_ms"]),
                        "wind_dir_deg": float(r["wind_dir_deg"]),
                        "pressure_hpa": float(r["pressure_hpa"]),
                        "precip_prob_cal": float(r["precip_prob"]),
                        "precip_p50_mm": float(r["precip_p50_mm"]),
                        "precip_p90_mm": float(r["precip_p90_mm"]),
                        "precip_type": str(r["precip_type"]),
                        "bust_risk_level": str(r["bust_risk_level"]),
                        "bust_summary": str(r["bust_summary"]),
                    }
                    for r in hourly
                ],
                schema={
                    "location_id": pl.Int64,
                    "issued_at_utc": pl.Datetime("us", "UTC"),
                    "valid_time_utc": pl.Datetime("us", "UTC"),
                    "lead_hours": pl.Float64,
                    "temp_p05_c": pl.Float64,
                    "temp_p10_c": pl.Float64,
                    "temp_p25_c": pl.Float64,
                    "temp_p50_c": pl.Float64,
                    "temp_p75_c": pl.Float64,
                    "temp_p90_c": pl.Float64,
                    "temp_p95_c": pl.Float64,
                    "dewpoint_p50_c": pl.Float64,
                    "wet_bulb_c": pl.Float64,
                    "wind_p50_ms": pl.Float64,
                    "gust_p50_ms": pl.Float64,
                    "gust_p90_ms": pl.Float64,
                    "wind_dir_deg": pl.Float64,
                    "pressure_hpa": pl.Float64,
                    "precip_prob_cal": pl.Float64,
                    "precip_p50_mm": pl.Float64,
                    "precip_p90_mm": pl.Float64,
                    "precip_type": pl.String,
                    "bust_risk_level": pl.String,
                    "bust_summary": pl.String,
                },
            )
            await self.telemetry.upsert_ensemble_predictions(ens_df)

            lat_f, lon_f = float(loc["lat"]), float(loc["lon"])
            alerts, spc_sum, notif_prefs = await asyncio.gather(
                self.nws.fetch_active_alerts(lat_f, lon_f),
                self.spc.get_outlook_summary_for_point(lat_f, lon_f),
                self.app_store.get_notification_prefs(),
            )
            hail_risk = await self.hail.get_hail_summary_for_point(
                lat=lat_f,
                lon=lon_f,
                alerts=alerts,
                spc_hail=spc_sum.get("day1_hail"),
                min_hail_in=float(notif_prefs.get("hail_min_size_in", 0.50)),
                lead_window_minutes=int(notif_prefs.get("hail_lead_minutes", 45)),
            )
            await self.push.evaluate_and_notify_location(
                location=loc,
                alerts=alerts,
                hourly_forecast=hourly,
                hail_risk=hail_risk,
                prev_day_p50_map=prev_p50_map,
                now_utc=now_utc,
            )

        await self.app_store.set_watermark("forecast_refresh", str(location_id), now_utc.isoformat())

    async def check_rapid_alerts_for_location(self, location_id: int) -> dict[str, Any] | None:
        """
        Lightweight 2-minute Garage Hail & Tornado/Severe Warning watchdog.
        Shares the 90-second cached national NEXRAD storm-cell feed across all pinned locations.
        """
        loc = await self.app_store.get_location(location_id)
        if not loc or not loc.get("alerts_enabled", True):
            return None
        if str(loc.get("country") or "US").upper() != "US":
            return None

        prefs = await self.app_store.get_notification_prefs()
        if not prefs.get("enabled", True):
            return None

        lat, lon = float(loc["lat"]), float(loc["lon"])
        alerts, spc_sum = await asyncio.gather(
            self.nws.fetch_active_alerts(lat, lon),
            self.spc.get_outlook_summary_for_point(lat, lon),
        )
        hail_risk = await self.hail.get_hail_summary_for_point(
            lat=lat,
            lon=lon,
            alerts=alerts,
            spc_hail=spc_sum.get("day1_hail"),
            min_hail_in=float(prefs.get("hail_min_size_in", 0.50)),
            lead_window_minutes=int(prefs.get("hail_lead_minutes", 45)),
        )
        dispatched = await self.push.evaluate_and_notify_location(
            location=loc,
            alerts=alerts,
            hourly_forecast=None,
            hail_risk=hail_risk,
            now_utc=datetime.now(UTC),
        )
        return {"hail_risk": hail_risk, "dispatched": dispatched}

    async def _compute_pinned_hourly(
        self, location_id: int, now_utc: datetime
    ) -> list[dict[str, Any]]:
        grid = await self.telemetry.query_inference_grid(
            location_id,
            now_utc,
            now_utc - timedelta(minutes=30),
            now_utc + timedelta(days=10),
        )
        if grid.is_empty():
            return []
        wide = pivot_inference_grid_to_wide(grid, now_utc.timestamp())
        model_rec = await self.app_store.get_model_record(location_id)
        kalman_state = (model_rec or {}).get("kalman_state")
        aci_state = (model_rec or {}).get("aci_state")
        art = self._get_artifact(location_id, model_rec)
        if art is not None:
            return predict_with_artifact(
                art, wide, kalman_state=kalman_state, aci_state=aci_state, now_utc=now_utc
            )
        return compute_consensus_predictions(
            wide, kalman_state=kalman_state, aci_state=aci_state, now_utc=now_utc
        )

    async def get_pinned_forecast_bundle(self, location_id: int) -> dict[str, Any] | None:
        loc = await self.app_store.get_location(location_id)
        if not loc:
            return None

        now_utc = datetime.now(UTC)
        hourly = await self._compute_pinned_hourly(location_id, now_utc)
        if not hourly:
            await self.refresh_location_live(location_id)
            hourly = await self._compute_pinned_hourly(location_id, now_utc)

        sub15_df = await self.telemetry.query_subhourly_15m(
            location_id, now_utc, now_utc - timedelta(minutes=15), now_utc + timedelta(hours=4)
        )
        nowcast_15m = [
            {
                "valid_time_utc": r["valid_time_utc"].isoformat(),
                "temp_c": round(float(r["temp_c"]), 2) if r["temp_c"] is not None else None,
                "precip_mm": round(float(r["precip_mm"] or 0.0), 2),
                "ring_precip_max_mm": round(float(r["ring_precip_max_mm"] or 0.0), 2),
                "gust_ms": round(float(r["gust_ms"]), 2) if r["gust_ms"] is not None else None,
            }
            for r in sub15_df.to_dicts()
        ]

        recent_obs = await self.telemetry.query_recent_blended_obs(location_id, hours=6)
        current_obs: dict[str, Any] | None = None
        if not recent_obs.is_empty():
            last = recent_obs.tail(1).to_dicts()[0]
            obs_dt = last["valid_time_utc"]
            current_obs = {
                "valid_time_utc": obs_dt.isoformat(),
                "age_minutes": round(max(0.0, (now_utc - obs_dt).total_seconds() / 60.0), 1),
                "temp_c": last["temp_c"],
                "dewpoint_c": last["dewpoint_c"],
                "wet_bulb_c": last["wet_bulb_c"],
                "feels_like_c": round(
                    float(
                        apparent_temperature_c(
                            last["temp_c"],
                            last["dewpoint_c"] or (last["temp_c"] - 4.0),
                            last["wind_ms"] or 0.0,
                        )
                    ),
                    2,
                ),
                "wind_ms": last["wind_ms"],
                "gust_ms": last["gust_ms"],
                "wind_dir_deg": last["wind_dir_deg"],
                "precip_1h_mm": last["precip_1h_mm"],
                "pressure_hpa": last["pressure_hpa"],
            }

        lat, lon = float(loc["lat"]), float(loc["lon"])
        tz_str = str(loc.get("timezone") or "America/Chicago")
        country_str = str(loc.get("country") or "US")
        alerts, spc_summary, model_rec, checkins, notif_prefs, obs_14d = await asyncio.gather(
            self.nws.fetch_active_alerts(lat, lon),
            self.spc.get_outlook_summary_for_point(lat, lon),
            self.app_store.get_model_record(location_id),
            self.app_store.list_precip_checkins(location_id, limit=5),
            self.app_store.get_notification_prefs(),
            self.telemetry.query_recent_blended_obs(location_id, hours=24 * 14),
        )
        hail_risk, subseasonal = await asyncio.gather(
            self.hail.get_hail_summary_for_point(
                lat=lat,
                lon=lon,
                alerts=alerts,
                spc_hail=spc_summary.get("day1_hail"),
                min_hail_in=float(notif_prefs.get("hail_min_size_in", 0.50)),
                lead_window_minutes=int(notif_prefs.get("hail_lead_minutes", 45)),
            ),
            self.subseasonal.get_subseasonal_outlook(
                lat=lat,
                lon=lon,
                tz_name=tz_str,
                country=country_str,
                recent_obs_df=obs_14d,
            ),
        )
        alert_dicts = [a.to_dict() for a in alerts]
        storm_winter = summarize_storm_winter_mode(
            hourly, spc_summary, alert_dicts, hail_risk=hail_risk
        )
        kalman_state = (model_rec or {}).get("kalman_state") or {}

        return {
            "mode": "pinned_ai" if loc.get("model_status") == "trained" else "pinned_consensus",
            "location": loc,
            "engine_info": {
                "model_status": loc.get("model_status"),
                "backfill_status": loc.get("backfill_status"),
                "backfill_progress": loc.get("backfill_progress"),
                "backfill_message": loc.get("backfill_message"),
                "model_version": (model_rec or {}).get("version", 0),
                "train_rows": (model_rec or {}).get("train_rows", 0),
                "kalman_bias_c": (kalman_state.get("x") or [0.0, 0.0])[0],
                "kalman_trend_c_hr": (kalman_state.get("x") or [0.0, 0.0])[1],
                "hedge_weights": kalman_state.get("hedge_weights", {}),
            },
            "current_observation": current_obs,
            "nowcast_15m": nowcast_15m,
            "hourly": hourly,
            "daily": _build_daily_outlook(hourly, tz_str),
            "subseasonal": subseasonal,
            "hail_risk": hail_risk,
            "alerts": alert_dicts,
            "storm_winter": storm_winter,
            "recent_checkins": checkins,
        }

    # ── Instant <2s Ephemeral / Live GPS Consensus Mode ───────────────────

    async def get_ephemeral_forecast_bundle(
        self, lat: float, lon: float, name: str | None = None
    ) -> dict[str, Any]:
        cache_key = f"{lat:.3f}:{lon:.3f}"
        now_mono = time.monotonic()
        cached = self._ephemeral_cache.get(cache_key)
        if cached and (now_mono - cached[0]) < 300.0:
            return cached[1]

        now_utc = datetime.now(UTC)
        snap_df, meta, stations, alerts, spc_summary = await asyncio.gather(
            self.open_meteo.fetch_all_live_models(0, lat, lon),
            self.nws.reverse_point_metadata(lat, lon),
            discover_ground_truth_stations(lat, lon, self.mesonet, self.nws),
            self.nws.fetch_active_alerts(lat, lon),
            self.spc.get_outlook_summary_for_point(lat, lon),
        )
        display_name = name or meta.name

        # Fetch live observation from closest paired Mesonet / ASOS tower for instant Kalman nudge
        live_obs_rec: dict[str, Any] | None = None
        if stations:
            top = stations[0]
            if top.source == "ok_mesonet":
                rows = await self.mesonet.fetch_latest_mdf(station_filter={top.station_id})
                if rows:
                    live_obs_rec = rows[0]
            else:
                rows = await self.nws.fetch_latest_asos_observations(top.station_id, limit=1)
                if rows:
                    live_obs_rec = rows[0]

        hourly_df = snap_df.filter(pl.col("is_subhourly_15m").not_())
        sub15_df = snap_df.filter(pl.col("is_subhourly_15m"))

        kalman_state: dict[str, Any] | None = None
        current_obs: dict[str, Any] | None = None
        wide = pivot_inference_grid_to_wide(hourly_df, now_utc.timestamp()) if not hourly_df.is_empty() else pl.DataFrame()

        if live_obs_rec and live_obs_rec.get("temp_c") is not None and not wide.is_empty():
            dz = meta.elevation_m - stations[0].elevation_m
            obs_t = float(live_obs_rec["temp_c"]) + dz * -0.0065
            obs_td = (
                float(live_obs_rec["dewpoint_c"]) + dz * -0.002
                if live_obs_rec.get("dewpoint_c") is not None
                else obs_t - 4.0
            )
            obs_dt = live_obs_rec["valid_time_utc"]
            un_nudged = compute_consensus_predictions(wide, now_utc=now_utc)
            if un_nudged:
                kalman_state = update_kalman_state(
                    None,
                    obs_time_utc=obs_dt,
                    obs_temp_c=obs_t,
                    pred_temp_c=float(un_nudged[0]["temp_p50_c"]),
                    obs_dewpoint_c=obs_td,
                    pred_dewpoint_c=float(un_nudged[0]["dewpoint_p50_c"]),
                )
            current_obs = {
                "valid_time_utc": obs_dt.isoformat(),
                "age_minutes": round(max(0.0, (now_utc - obs_dt).total_seconds() / 60.0), 1),
                "temp_c": round(obs_t, 2),
                "dewpoint_c": round(obs_td, 2),
                "wet_bulb_c": round(float(wet_bulb_celsius(obs_t, dewpoint_c=obs_td)), 2),
                "feels_like_c": round(
                    float(apparent_temperature_c(obs_t, obs_td, live_obs_rec.get("wind_ms") or 0.0)),
                    2,
                ),
                "wind_ms": live_obs_rec.get("wind_ms"),
                "gust_ms": live_obs_rec.get("gust_ms"),
                "wind_dir_deg": live_obs_rec.get("wind_dir_deg"),
                "precip_1h_mm": live_obs_rec.get("precip_1h_mm"),
                "pressure_hpa": live_obs_rec.get("pressure_hpa"),
            }

        hourly = compute_consensus_predictions(wide, kalman_state=kalman_state, now_utc=now_utc)
        nowcast_15m = [
            {
                "valid_time_utc": r["valid_time_utc"].isoformat(),
                "temp_c": round(float(r["temp_c"]), 2) if r["temp_c"] is not None else None,
                "precip_mm": round(float(r["precip_mm"] or 0.0), 2),
                "ring_precip_max_mm": round(float(r["ring_precip_max_mm"] or 0.0), 2),
                "gust_ms": round(float(r["gust_ms"]), 2) if r["gust_ms"] is not None else None,
            }
            for r in sub15_df.sort("valid_time_utc").head(16).to_dicts()
        ]
        alert_dicts = [a.to_dict() for a in alerts]
        notif_prefs = await self.app_store.get_notification_prefs()
        hail_risk, subseasonal = await asyncio.gather(
            self.hail.get_hail_summary_for_point(
                lat=lat,
                lon=lon,
                alerts=alerts,
                spc_hail=spc_summary.get("day1_hail"),
                min_hail_in=float(notif_prefs.get("hail_min_size_in", 0.50)),
                lead_window_minutes=int(notif_prefs.get("hail_lead_minutes", 45)),
            ),
            self.subseasonal.get_subseasonal_outlook(
                lat=lat,
                lon=lon,
                tz_name=meta.timezone,
                country=meta.country,
                recent_obs_df=None,
            ),
        )

        bundle = {
            "mode": "ephemeral_consensus",
            "location": {
                "id": None,
                "name": display_name,
                "address_label": meta.display_address,
                "alerts_enabled": False,
                "admin1": meta.admin1,
                "country": meta.country,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "elevation_m": round(meta.elevation_m, 1),
                "timezone": meta.timezone,
                "stations": [s.to_dict() for s in stations],
            },
            "engine_info": {
                "model_status": "ephemeral_consensus",
                "backfill_status": "not_pinned",
                "backfill_progress": 0.0,
                "backfill_message": "Instant multi-model consensus + live Kalman nowcast. Pin to train local AI.",
                "kalman_bias_c": (kalman_state.get("x") or [0.0, 0.0])[0] if kalman_state else 0.0,
                "kalman_trend_c_hr": (kalman_state.get("x") or [0.0, 0.0])[1] if kalman_state else 0.0,
            },
            "current_observation": current_obs,
            "nowcast_15m": nowcast_15m,
            "hourly": hourly,
            "daily": _build_daily_outlook(hourly, meta.timezone),
            "subseasonal": subseasonal,
            "hail_risk": hail_risk,
            "alerts": alert_dicts,
            "storm_winter": summarize_storm_winter_mode(
                hourly, spc_summary, alert_dicts, hail_risk=hail_risk
            ),
            "recent_checkins": [],
        }
        self._ephemeral_cache[cache_key] = (now_mono, bundle)
        return bundle
