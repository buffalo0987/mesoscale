"""FastAPI application factory serving both /api and the compiled Mesoscale React PWA."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mesoscale.api.routes import router as api_router
from mesoscale.config import Settings, get_settings
from mesoscale.db.app_store import AppStore
from mesoscale.db.telemetry_store import TelemetryStore
from mesoscale.scheduler import MesoscaleScheduler
from mesoscale.services.forecast_engine import ForecastEngineService
from mesoscale.services.push_service import WebPushService


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg.ensure_dirs()
        app_store = AppStore(cfg.app_db_path)
        await app_store.initialize()

        telemetry_store = TelemetryStore(cfg.telemetry_db_path)
        await telemetry_store.initialize()

        push_service = WebPushService(
            app_store=app_store,
            vapid_keys_path=cfg.vapid_keys_path,
            vapid_subject=cfg.vapid_subject,
        )
        push_service.ensure_vapid_keys()

        engine = ForecastEngineService(
            settings=cfg,
            app_store=app_store,
            telemetry_store=telemetry_store,
            push_service=push_service,
        )
        app.state.settings = cfg
        app.state.engine = engine

        scheduler: MesoscaleScheduler | None = None
        if cfg.enable_scheduler:
            scheduler = MesoscaleScheduler(engine)
            scheduler.start()
            app.state.scheduler = scheduler

        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()
            telemetry_store.close()

    application = FastAPI(
        title="Mesoscale Weather Engine",
        version="0.1.0",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(api_router)

    # Mount compiled Vite PWA assets when built (either backend/mesoscale/static_dist or frontend/dist)
    pkg_dir = Path(__file__).resolve().parent
    candidate_dists = (
        pkg_dir / "static_dist",
        pkg_dir.parent.parent / "frontend" / "dist",
    )
    dist_dir = next((d for d in candidate_dists if (d / "index.html").exists()), None)

    if dist_dir is not None:
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            application.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @application.get("/{full_path:path}", include_in_schema=False)
        async def serve_pwa_spa(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            target = (dist_dir / full_path).resolve()
            if full_path and target.is_file() and str(target).startswith(str(dist_dir.resolve())):
                headers = {}
                if target.name in ("sw.js", "manifest.webmanifest"):
                    headers["Cache-Control"] = "no-cache"
                return FileResponse(target, headers=headers)
            return FileResponse(dist_dir / "index.html")

    return application


app = create_app()
