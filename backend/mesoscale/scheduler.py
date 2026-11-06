"""APScheduler background orchestrator with startup outage gap-recovery."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from mesoscale.services.forecast_engine import ForecastEngineService

logger = logging.getLogger("mesoscale.scheduler")


class MesoscaleScheduler:
    """Manages 5-min Mesonet/Kalman ticks, hourly 6-model logging, and nightly MOS retraining."""

    def __init__(self, engine: ForecastEngineService) -> None:
        self.engine = engine
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.started_at_utc: datetime | None = None

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.started_at_utc = datetime.now(UTC)

        # Every 5 minutes: live 5-min Oklahoma Mesonet / ASOS observations + 2-State Kalman update
        self.scheduler.add_job(
            self.job_fast_observation_loop,
            "interval",
            minutes=5,
            id="fast_obs_kalman",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Hourly at :08 past the hour: full 6-model spatial-ring ingest + ACI + out-of-sample log
        self.scheduler.add_job(
            self.job_hourly_forecast_loop,
            "cron",
            minute=8,
            id="hourly_nwp_ingest",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Nightly at 09:15 UTC (03:15 CST): CatBoost + LightGBM MOS retrain & subhourly compaction
        self.scheduler.add_job(
            self.job_nightly_retrain,
            "cron",
            hour=9,
            minute=15,
            id="nightly_mos_retrain",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Startup catch-up check 10 seconds after boot (resumes interrupted backfills or stale locations)
        self.scheduler.add_job(
            self.job_startup_recovery,
            "date",
            run_date=datetime.now(UTC) + timedelta(seconds=8),
            id="startup_recovery",
            replace_existing=True,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def job_startup_recovery(self) -> None:
        locations = await self.engine.app_store.list_locations()
        now_utc = datetime.now(UTC)
        for loc in locations:
            lid = int(loc["id"])
            if loc.get("backfill_status") in ("pending", "running"):
                self.engine.launch_background_backfill(lid)
                continue
            wm = await self.engine.app_store.get_watermark("forecast_refresh", str(lid))
            needs_refresh = True
            if wm:
                try:
                    last_dt = datetime.fromisoformat(wm.replace("Z", "+00:00"))
                    needs_refresh = (now_utc - last_dt) > timedelta(minutes=55)
                except ValueError:
                    needs_refresh = True
            if needs_refresh:
                try:
                    await self.engine.refresh_location_live(lid)
                except Exception as exc:
                    logger.warning("Startup refresh failed for location %s: %s", lid, exc)

    async def job_fast_observation_loop(self) -> None:
        locations = await self.engine.app_store.list_locations()
        for loc in locations:
            try:
                await self.engine.refresh_observations_and_kalman(int(loc["id"]))
            except Exception as exc:
                logger.warning("Fast obs loop warning for %s: %s", loc["id"], exc)

    async def job_hourly_forecast_loop(self) -> None:
        locations = await self.engine.app_store.list_locations()
        for loc in locations:
            try:
                await self.engine.refresh_location_live(int(loc["id"]))
            except Exception as exc:
                logger.warning("Hourly forecast loop warning for %s: %s", loc["id"], exc)

    async def job_nightly_retrain(self) -> None:
        await self.engine.telemetry.compact_subhourly(retention_days=30)
        locations = await self.engine.app_store.list_locations()
        for loc in locations:
            try:
                await self.engine.retrain_location_model(int(loc["id"]))
            except Exception as exc:
                logger.warning("Nightly retrain warning for %s: %s", loc["id"], exc)
