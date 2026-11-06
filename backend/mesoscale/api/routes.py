"""Typed FastAPI routes for Mesoscale PWA, Ephemeral mode, Verification, Radar, and Web Push."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mesoscale.ml.verification import build_verification_report
from mesoscale.providers.spatial_ring import ring_geojson_feature_collection
from mesoscale.services.forecast_engine import ForecastEngineService

router = APIRouter(prefix="/api")


class PinLocationRequest(BaseModel):
    name: str | None = None
    admin1: str | None = None
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    elevation_m: float | None = None
    timezone: str | None = None
    trigger_backfill: bool = True


class PrecipCheckinRequest(BaseModel):
    condition: Literal["dry", "rain", "tstorm", "freezing_rain", "sleet", "snow"]
    notes: str = ""


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: dict[str, str]
    user_agent: str = ""


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


def _engine(request: Request) -> ForecastEngineService:
    return request.app.state.engine


@router.get("/health")
async def get_health(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    sched = getattr(request.app.state, "scheduler", None)
    stats = await eng.telemetry.get_stats()
    locs = await eng.app_store.list_locations()
    return {
        "status": "ok",
        "app": "Mesoscale",
        "utc_time": datetime.now(UTC).isoformat(),
        "scheduler_running": bool(sched and sched.scheduler.running),
        "pinned_locations_count": len(locs),
        "storage": stats,
    }


@router.get("/bootstrap")
async def get_bootstrap(request: Request) -> dict[str, Any]:
    """
    Initial boot state for Mesoscale PWA.
    Starts with zero pre-seeded cities (`pinned_locations: []`) so first launch
    cleanly presents the Onboarding Search / Live GPS experience.
    """
    eng = _engine(request)
    locations, notif_prefs, pws_cfg = await asyncio.gather(
        eng.app_store.list_locations(),
        eng.app_store.get_notification_prefs(),
        eng.app_store.get_pws_config(),
    )
    safe_pws = {
        "enabled": bool(pws_cfg.get("enabled")),
        "provider": pws_cfg.get("provider", "none"),
        "station_id": pws_cfg.get("station_id", ""),
        "has_api_key": bool(pws_cfg.get("api_key")),
    }
    return {
        "app_name": "Mesoscale",
        "pinned_locations": locations,
        "vapid_public_key": eng.push.public_key,
        "notification_prefs": notif_prefs,
        "pws_config": safe_pws,
    }


@router.get("/locations/search")
async def search_locations(
    request: Request, q: str = Query(..., min_length=2, max_length=100)
) -> dict[str, Any]:
    eng = _engine(request)
    results = await eng.nws.geocode_search(q)
    return {"results": [r.to_dict() for r in results]}


@router.get("/ephemeral/forecast")
async def get_ephemeral_forecast(
    request: Request,
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
    name: str | None = Query(default=None),
) -> dict[str, Any]:
    eng = _engine(request)
    return await eng.get_ephemeral_forecast_bundle(lat=lat, lon=lon, name=name)


@router.post("/locations/pin")
async def pin_location(request: Request, body: PinLocationRequest) -> dict[str, Any]:
    eng = _engine(request)
    loc = await eng.pin_new_location(
        name=body.name,
        admin1=body.admin1,
        lat=body.lat,
        lon=body.lon,
        elevation_m=body.elevation_m,
        timezone_name=body.timezone,
        trigger_backfill=body.trigger_backfill,
    )
    return {"location": loc}


@router.get("/locations")
async def list_pinned_locations(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    return {"locations": await eng.app_store.list_locations()}


@router.get("/locations/{location_id}/status")
async def get_location_status(request: Request, location_id: int) -> dict[str, Any]:
    eng = _engine(request)
    loc = await eng.app_store.get_location(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Pinned location not found")
    model_rec = await eng.app_store.get_model_record(location_id)
    return {
        "id": loc["id"],
        "backfill_status": loc["backfill_status"],
        "backfill_progress": loc["backfill_progress"],
        "backfill_message": loc["backfill_message"],
        "model_status": loc["model_status"],
        "model_metrics": (model_rec or {}).get("metrics", {}),
    }


@router.get("/locations/{location_id}/forecast")
async def get_pinned_forecast(request: Request, location_id: int) -> dict[str, Any]:
    eng = _engine(request)
    bundle = await eng.get_pinned_forecast_bundle(location_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Pinned location not found")
    return bundle


@router.post("/locations/{location_id}/refresh")
async def refresh_pinned_location(request: Request, location_id: int) -> dict[str, Any]:
    eng = _engine(request)
    loc = await eng.app_store.get_location(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Pinned location not found")
    await eng.refresh_location_live(location_id)
    return {"ok": True}


@router.post("/locations/{location_id}/retrain")
async def retrain_pinned_location(request: Request, location_id: int) -> dict[str, Any]:
    eng = _engine(request)
    loc = await eng.app_store.get_location(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Pinned location not found")
    report = await eng.retrain_location_model(location_id)
    return {"ok": True, "report": report}


@router.delete("/locations/{location_id}")
async def delete_pinned_location(request: Request, location_id: int) -> dict[str, Any]:
    eng = _engine(request)
    existed = await eng.app_store.delete_location(location_id)
    if not existed:
        raise HTTPException(status_code=404, detail="Pinned location not found")
    await eng.telemetry.delete_location_telemetry(location_id)
    return {"deleted": True}


@router.post("/locations/{location_id}/checkin")
async def submit_precip_checkin(
    request: Request, location_id: int, body: PrecipCheckinRequest
) -> dict[str, Any]:
    eng = _engine(request)
    loc = await eng.app_store.get_location(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Pinned location not found")

    intensity_map = {
        "dry": 0.0,
        "rain": 1.8,
        "tstorm": 8.5,
        "freezing_rain": 1.5,
        "sleet": 1.2,
        "snow": 1.0,
    }
    rec = await eng.app_store.add_precip_checkin(
        location_id=location_id,
        condition=body.condition,
        intensity_mm_hr=intensity_map[body.condition],
        notes=body.notes,
    )
    # Trigger immediate observation blend update so ground truth incorporates check-in
    try:
        await eng.refresh_observations_and_kalman(location_id)
    except Exception:
        pass
    return {"checkin": rec}


@router.get("/locations/{location_id}/verification")
async def get_location_verification(
    request: Request,
    location_id: int,
    days: int = Query(default=30, ge=1, le=365),
    lead_bucket: str = Query(default="all"),
) -> dict[str, Any]:
    eng = _engine(request)
    loc = await eng.app_store.get_location(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Pinned location not found")

    models_df, ai_df = await eng.telemetry.query_verification_data(location_id, days=days)
    model_rec = await eng.app_store.get_model_record(location_id)
    holdout = (model_rec or {}).get("metrics") or {}

    return build_verification_report(
        models_df=models_df,
        ai_df=ai_df,
        holdout_metrics=holdout,
        lead_bucket=lead_bucket,
    )


@router.get("/map-overlay")
async def get_map_overlay(
    request: Request,
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
) -> dict[str, Any]:
    """
    Supply GeoJSON overlays for the MapLibre WebGL radar map:
    - Live nearby 5-min Oklahoma Mesonet + ASOS station wind/temp pins
    - Active NWS Severe Warning / Watch polygons
    - NOAA SPC Day 1 & Day 2 Convective Outlook polygons
    - 8 km 5-point spatial ring
    - IEM NEXRAD 5-min Web Mercator frame descriptors (past 50m -> live)
    """
    eng = _engine(request)
    meso_vecs, asos_vecs, alerts, spc_d1, spc_d2 = await asyncio.gather(
        eng.mesonet.fetch_nearby_station_vectors(lat, lon, radius_km=95.0),
        eng.nws.fetch_nearby_asos_vectors(lat, lon, limit=4),
        eng.nws.fetch_active_alerts(lat, lon),
        eng.spc.get_day_geojson(1),
        eng.spc.get_day_geojson(2),
    )

    station_features: list[dict[str, Any]] = []
    for st in meso_vecs + asos_vecs:
        if st.temp_c is None:
            continue
        station_features.append(
            {
                "type": "Feature",
                "properties": {
                    "station_id": st.station_id,
                    "name": st.name,
                    "source": st.source,
                    "temp_c": round(st.temp_c, 1),
                    "temp_f": round(st.temp_c * 1.8 + 32.0, 0),
                    "dewpoint_c": round(st.dewpoint_c, 1) if st.dewpoint_c is not None else None,
                    "wind_ms": round(st.wind_ms, 1) if st.wind_ms is not None else None,
                    "wind_mph": round(st.wind_ms * 2.23694, 0) if st.wind_ms is not None else None,
                    "gust_mph": round(st.gust_ms * 2.23694, 0) if st.gust_ms is not None else None,
                    "wind_dir_deg": round(st.wind_dir_deg, 0) if st.wind_dir_deg is not None else 0,
                    "valid_time_utc": st.valid_time_utc.isoformat(),
                },
                "geometry": {"type": "Point", "coordinates": [st.lon, st.lat]},
            }
        )

    alert_features = [
        {
            "type": "Feature",
            "properties": {
                "id": a.id,
                "event": a.event,
                "severity": a.severity,
                "headline": a.headline,
                "is_life_threatening": a.is_life_threatening,
            },
            "geometry": a.geometry,
        }
        for a in alerts
        if a.geometry is not None
    ]

    # IEM Web Mercator NEXRAD N0Q 5-minute animation frames (-50m .. live)
    offsets_min = [50, 45, 40, 35, 30, 25, 20, 15, 10, 5, 0]
    radar_frames = []
    for m in offsets_min:
        layer = (
            "nexrad-n0q-900913"
            if m == 0
            else f"nexrad-n0q-900913-m{m:02d}m"
        )
        radar_frames.append(
            {
                "offset_minutes": -m,
                "label": "Live" if m == 0 else f"-{m}m",
                "tile_url": f"https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/{layer}/{{z}}/{{x}}/{{y}}.png",
            }
        )

    return {
        "stations_geojson": {"type": "FeatureCollection", "features": station_features},
        "alerts_geojson": {"type": "FeatureCollection", "features": alert_features},
        "spc_day1_geojson": spc_d1,
        "spc_day2_geojson": spc_d2,
        "ring_geojson": ring_geojson_feature_collection(lat, lon, eng.settings.ring_radius_km),
        "radar_frames": radar_frames,
        "mrms_tile_url": "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/q2-hsr-900913/{z}/{x}/{y}.png",
    }


# ── Web Push & Settings Routes ────────────────────────────────────────


@router.post("/push/subscribe")
async def subscribe_push(request: Request, body: PushSubscribeRequest) -> dict[str, Any]:
    eng = _engine(request)
    p256dh = body.keys.get("p256dh", "")
    auth = body.keys.get("auth", "")
    if not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Missing Web Push encryption keys")
    await eng.app_store.save_push_subscription(
        endpoint=body.endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=body.user_agent,
    )
    return {"subscribed": True}


@router.post("/push/unsubscribe")
async def unsubscribe_push(request: Request, body: PushUnsubscribeRequest) -> dict[str, Any]:
    eng = _engine(request)
    await eng.app_store.remove_push_subscription(body.endpoint)
    return {"unsubscribed": True}


@router.post("/push/test")
async def send_test_push(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    res = await eng.push.send_payload_to_all(
        title="⚡ Mesoscale Push Active",
        body="End-to-end VAPID Web Push verified. Severe NWS warnings & smart shift alerts are armed.",
        tag="mesoscale-test",
        severity="info",
        bypass_quiet_hours=True,
    )
    return {"ok": True, **res}


@router.put("/settings/notifications")
async def update_notification_settings(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    eng = _engine(request)
    saved = await eng.app_store.set_notification_prefs(body)
    return {"notification_prefs": saved}


@router.put("/settings/pws")
async def update_pws_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    eng = _engine(request)
    current = await eng.app_store.get_pws_config()
    if body.get("api_key") == "" and current.get("api_key") and body.get("keep_existing_key"):
        body.pop("api_key", None)
    saved = await eng.app_store.set_pws_config(body)
    return {
        "pws_config": {
            "enabled": bool(saved.get("enabled")),
            "provider": saved.get("provider", "none"),
            "station_id": saved.get("station_id", ""),
            "has_api_key": bool(saved.get("api_key")),
        }
    }
