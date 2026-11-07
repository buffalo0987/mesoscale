"""Tests for rooftop geocoding, Aruba/AWOS stations, NEXRAD hail shield, subseasonal EMOS, and 730d retention."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
import polars as pl
import pytest
import respx
from mesoscale.config import Settings
from mesoscale.db.telemetry_store import TelemetryStore
from mesoscale.main import create_app
from mesoscale.ml.features import pivot_inference_grid_to_wide
from mesoscale.ml.mos_trainer import (
    compute_consensus_predictions,
    compute_exponential_recency_weights,
)
from mesoscale.providers.hail_tracker import (
    build_hail_cells_geojson,
    evaluate_hail_risk_for_point,
    parse_nexrad_attr_geojson,
)
from mesoscale.providers.nws_iem import NwsIemClient, parse_alert_hail_and_wind
from mesoscale.providers.spc_outlook import evaluate_point_spc_hail_risk
from mesoscale.providers.subseasonal import (
    compute_subseasonal_weeks_emos,
    extract_daily_member_matrix,
)


@pytest.mark.asyncio
async def test_street_geocoding_awos_and_aruba_global_metar() -> None:
    nws = NwsIemClient(user_agent="MesoscaleTest/1.0")

    with respx.mock(assert_all_called=False) as router:
        # 1. US Census rooftop street address + 90m Open-Meteo DEM elevation
        router.get("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "addressMatches": [
                            {
                                "matchedAddress": "1200 ASP AVE, NORMAN, OK, 73019",
                                "coordinates": {"x": -97.4452, "y": 35.2058},
                                "addressComponents": {"city": "NORMAN", "state": "OK"},
                            }
                        ]
                    }
                },
            )
        )
        router.get("https://nominatim.openstreetmap.org/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        router.get("https://api.open-meteo.com/v1/elevation").mock(
            return_value=httpx.Response(200, json={"elevation": [359.4]})
        )

        addr_hits = await nws.geocode_search("1200 Asp Ave, Norman, OK")
        assert len(addr_hits) == 1
        assert addr_hits[0].result_type == "address"
        assert addr_hits[0].name == "1200 Asp Ave, Norman"
        assert addr_hits[0].admin1 == "OK"
        assert addr_hits[0].timezone == "America/Chicago"
        assert addr_hits[0].elevation_m == pytest.approx(359.4)

        # 2. Alphanumeric US FAA AWOS station discovery (e.g. K1V6 Canon City, CO)
        router.get("https://api.weather.gov/points/38.4310,-105.1080").mock(
            return_value=httpx.Response(
                200,
                json={
                    "properties": {
                        "observationStations": "https://api.weather.gov/gridpoints/PUB/50,50/stations"
                    }
                },
            )
        )
        router.get("https://api.weather.gov/gridpoints/PUB/50,50/stations").mock(
            return_value=httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "stationIdentifier": "K1V6",
                                "name": "Fremont County Airport",
                                "elevation": {"value": 1658.0},
                            },
                            "geometry": {"type": "Point", "coordinates": [-105.1058, 38.4280]},
                        }
                    ]
                },
            )
        )
        co_stations = await nws.find_nearest_asos_stations(38.4310, -105.1080)
        assert len(co_stations) == 1
        assert co_stations[0].station_id == "K1V6"

        # 3. International destination: Aruba (12.51°N, -70.01°W) -> discovers TNCA via NOAA AWC METAR
        router.get("https://api.weather.gov/points/12.5100,-70.0100").mock(
            return_value=httpx.Response(404, json={"status": 404})
        )
        router.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "icaoId": "TNCA",
                        "name": "Queen Beatrix Intl, Aruba",
                        "lat": 12.5014,
                        "lon": -70.0152,
                        "elev": 18.0,
                        "obsTime": int(datetime(2026, 11, 7, 18, 0, tzinfo=UTC).timestamp()),
                        "temp": 30.2,
                        "dewp": 24.5,
                        "wdir": 85,
                        "wspd": 19,
                        "wgst": 26,
                        "altim": 1012.5,
                    }
                ],
            )
        )
        aruba_stations = await nws.find_nearest_asos_stations(12.5100, -70.0100)
        assert len(aruba_stations) == 1
        assert aruba_stations[0].station_id == "TNCA"

        # Verify live METAR observation fallback for non-US ICAO station TNCA
        router.get("https://api.weather.gov/stations/TNCA/observations").mock(
            return_value=httpx.Response(404, json={})
        )
        aruba_obs = await nws.fetch_latest_asos_observations("TNCA", limit=2)
        assert len(aruba_obs) == 1
        assert aruba_obs[0]["temp_c"] == pytest.approx(30.2)
        assert aruba_obs[0]["wind_ms"] == pytest.approx(19 * 0.514444, rel=1e-3)


def test_nexrad_hail_cone_eta_and_receding_cell_rejection() -> None:
    # Target driveway in Norman, OK
    home_lat, home_lon = 35.2200, -97.4400

    # Cell A: ~15 km WSW of Home, moving FROM 240° (WSW) -> heading 60° (ENE) straight toward Home at 32 kt
    # Cell B: ~15 km ENE of Home, also moving FROM 240° -> heading 60° (ENE) AWAY from Home!
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "nexrad": "TLX",
                    "storm_id": "A1",
                    "poh": 100,
                    "posh": 70,
                    "max_size": 1.25,
                    "drct": 240,
                    "sknt": 32,
                    "vil": 58,
                },
                "geometry": {"type": "Point", "coordinates": [-97.5830, 35.1520]},
            },
            {
                "type": "Feature",
                "properties": {
                    "nexrad": "TLX",
                    "storm_id": "B9",
                    "poh": 100,
                    "posh": 80,
                    "max_size": 1.75,
                    "drct": 240,
                    "sknt": 35,
                    "vil": 64,
                },
                "geometry": {"type": "Point", "coordinates": [-97.2950, 35.2880]},
            },
        ],
    }

    cells = parse_nexrad_attr_geojson(geojson)
    assert len(cells) == 2

    # Evaluate with both cells active -> Cell A1 approaches in ~15 min; receding Cell B9 is rejected!
    risk = evaluate_hail_risk_for_point(
        lat=home_lat,
        lon=home_lon,
        cells=cells,
        min_hail_in=0.50,
        lead_window_minutes=45,
    )
    assert risk["status"] in ("approaching", "imminent")
    assert len(risk["approaching_cells"]) == 1
    assert risk["approaching_cells"][0]["cell_key"] == "TLX:A1"
    assert 12 <= risk["eta_minutes"] <= 20
    assert risk["max_hail_in"] == pytest.approx(1.25)

    # Evaluate with ONLY receding Cell B9 -> must NOT enter arrival cone or trigger approaching/imminent!
    receding_only = evaluate_hail_risk_for_point(
        lat=home_lat,
        lon=home_lon,
        cells=[c for c in cells if c.storm_id == "B9"],
        min_hail_in=0.50,
        lead_window_minutes=45,
    )
    assert receding_only["approaching_cells"] == []
    assert receding_only["status"] == "watch"  # Nearby within 90 mi, but zero false driveway alarm

    overlay = build_hail_cells_geojson(cells, home_lat, home_lon)
    kinds = [f["properties"]["feature_kind"] for f in overlay["features"]]
    assert kinds.count("hail_core") == 2
    assert kinds.count("hail_track") == 2


def test_nws_cap_and_sps_hail_parsing_and_spc_hail_polygon() -> None:
    # 1. Special Weather Statement (SPS) prose parsing ("marble size hail")
    sps_props = {
        "event": "Special Weather Statement",
        "headline": "Strong thunderstorm approaching Norman",
        "description": "HAZARD...Wind gusts up to 50 mph and marble size hail.",
        "parameters": {},
    }
    hail_in, threat, wind_mph = parse_alert_hail_and_wind(sps_props)
    assert hail_in == pytest.approx(0.50)
    assert threat == ""
    assert wind_mph == pytest.approx(50.0)

    # 2. Severe Thunderstorm Warning CAP `maxHailSize`
    svr_props = {
        "event": "Severe Thunderstorm Warning",
        "headline": "Severe Thunderstorm Warning",
        "description": "Golf ball hail reported.",
        "parameters": {"maxHailSize": ["1.75"], "hailThreat": ["RADAR INDICATED"], "maxWindGust": ["65 MPH"]},
    }
    hail_svr, threat_svr, wind_svr = parse_alert_hail_and_wind(svr_props)
    assert hail_svr == pytest.approx(1.75)
    assert threat_svr == "RADAR INDICATED"
    assert wind_svr == pytest.approx(65.0)

    # 3. SPC Day 1 Probabilistic Hail + Hatched SIGN (>=2") polygon evaluation
    spc_hail_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"DN": 30, "LABEL": "0.30"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-98.0, 35.0], [-97.0, 35.0], [-97.0, 36.0], [-98.0, 36.0], [-98.0, 35.0]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"DN": 10, "LABEL": "SIGN"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-98.0, 35.0], [-97.0, 35.0], [-97.0, 36.0], [-98.0, 36.0], [-98.0, 35.0]]],
                },
            },
        ],
    }
    res = evaluate_point_spc_hail_risk(spc_hail_fc, 35.22, -97.44)
    assert res["prob_pct"] == 30
    assert res["sig_hail"] is True


@pytest.mark.asyncio
async def test_rooftop_separation_mute_toggle_and_hail_push_escalation(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "data", enable_scheduler=False)
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        eng = app.state.engine
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Two rooftops ~450m apart in the same neighborhood MUST remain separate pins (<85m threshold)
            loc_a = await eng.app_store.create_location(
                name="Home",
                address_label="100 Main St, Norman, OK",
                admin1="OK",
                country="US",
                lat=35.2200,
                lon=-97.4400,
                elevation_m=357.0,
                timezone_name="America/Chicago",
                stations=[],
            )
            loc_b = await eng.app_store.create_location(
                name="Parents House",
                address_label="500 Main St, Norman, OK",
                admin1="OK",
                country="US",
                lat=35.2240,
                lon=-97.4400,
                elevation_m=358.0,
                timezone_name="America/Chicago",
                stations=[],
            )
            assert loc_a["id"] != loc_b["id"]

            # Re-pinning the exact same driveway (~20m jitter) deduplicates cleanly
            loc_a_dup = await eng.app_store.create_location(
                name="Home",
                address_label="100 Main St, Norman, OK",
                admin1="OK",
                country="US",
                lat=35.2201,
                lon=-97.4401,
                elevation_m=357.0,
                timezone_name="America/Chicago",
                stations=[],
            )
            assert loc_a_dup["id"] == loc_a["id"]

            # Mute alerts on Location B via PATCH /api/locations/{id}
            patch_res = await client.patch(
                f"/api/locations/{loc_b['id']}",
                json={"alerts_enabled": False, "name": "Aruba Honeymoon"},
            )
            assert patch_res.status_code == 200
            updated_b = patch_res.json()["location"]
            assert updated_b["alerts_enabled"] is False
            assert updated_b["name"] == "Aruba Honeymoon"

            # Subscribe push endpoint and test Garage Hail Shield + size-escalation during Quiet Hours!
            await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://fcm.googleapis.com/fcm/send/truck-shield-1",
                    "keys": {"p256dh": "k1", "auth": "a1"},
                },
            )
            night_utc = datetime(2026, 11, 7, 8, 15, tzinfo=UTC)  # 02:15 AM CST (Quiet Hours)
            marble_hail = {
                "status": "approaching",
                "headline": "Hail Core ~18 min Away",
                "eta_minutes": 18,
                "max_hail_in": 0.50,
                "hail_label": '0.50" (Marble)',
                "approaching_cells": [
                    {
                        "cell_key": "TLX:C4",
                        "distance_mi": 11.0,
                        "bearing_from_target": "WSW",
                        "speed_mph": 36.0,
                    }
                ],
            }

            with patch("mesoscale.services.push_service.webpush") as mock_wp:
                # Muted Location B must NEVER fire push alerts
                muted_out = await eng.push.evaluate_and_notify_location(
                    location=updated_b,
                    alerts=[],
                    hail_risk=marble_hail,
                    now_utc=night_utc,
                )
                assert muted_out == []
                assert mock_wp.call_count == 0

                # Active Home Location A fires immediately at 2:15 AM (bypasses Quiet Hours!)
                first_fire = await eng.push.evaluate_and_notify_location(
                    location=loc_a,
                    alerts=[],
                    hail_risk=marble_hail,
                    now_utc=night_utc,
                )
                assert len(first_fire) == 1
                assert first_fire[0]["sent"] == 1
                assert mock_wp.call_count == 1

                # Duplicate 2-min poll at same 0.50" marble size is deduplicated
                dup_fire = await eng.push.evaluate_and_notify_location(
                    location=loc_a,
                    alerts=[],
                    hail_risk=marble_hail,
                    now_utc=night_utc + timedelta(minutes=2),
                )
                assert dup_fire == []
                assert mock_wp.call_count == 1

                # Storm intensifies to 1.75" Golf Ball hail -> escalates size bucket & fires urgent update!
                golf_hail = {
                    **marble_hail,
                    "status": "imminent",
                    "eta_minutes": 9,
                    "max_hail_in": 1.75,
                    "hail_label": '1.75" (Golf Ball)',
                }
                esc_fire = await eng.push.evaluate_and_notify_location(
                    location=loc_a,
                    alerts=[],
                    hail_risk=golf_hail,
                    now_utc=night_utc + timedelta(minutes=6),
                )
                assert len(esc_fire) == 1
                assert esc_fire[0]["sent"] == 1
                assert mock_wp.call_count == 2

            # Verify `.joblib` model file pruning & unpin cleanup
            eng.settings.models_dir.mkdir(parents=True, exist_ok=True)
            lid_b = int(updated_b["id"])
            for v in (1, 2, 3, 4):
                (eng.settings.models_dir / f"location_{lid_b}_v{v}.joblib").write_bytes(b"stub")
            removed = eng.prune_old_model_files(lid_b, keep_min_version=3)
            assert removed == 2
            assert not (eng.settings.models_dir / f"location_{lid_b}_v1.joblib").exists()
            assert (eng.settings.models_dir / f"location_{lid_b}_v3.joblib").exists()
            assert (eng.settings.models_dir / f"location_{lid_b}_v4.joblib").exists()

            del_res = await client.delete(f"/api/locations/{lid_b}")
            assert del_res.status_code == 200
            assert not (eng.settings.models_dir / f"location_{lid_b}_v4.joblib").exists()


def test_subseasonal_weeks_2_to_4_emos_calibration() -> None:
    today = date(2026, 11, 7)
    gefs_dates = [today + timedelta(days=i) for i in range(35)]
    seas_dates = [today + timedelta(days=i) for i in range(35)]

    # 8 years of daily local climatology (~16°C highs, ~5°C lows, 2mm/day precip)
    hist_start = date(2018, 1, 1)
    hist_dates = [hist_start + timedelta(days=i) for i in range(365 * 8)]
    rng = np.random.default_rng(42)
    hist_tmax = 16.0 + rng.normal(0.0, 2.5, size=len(hist_dates))
    hist_tmin = 5.0 + rng.normal(0.0, 2.2, size=len(hist_dates))
    hist_prcp = np.maximum(0.0, rng.exponential(1.8, size=len(hist_dates)))

    # 31 GEFS + 51 SEAS5 members signaling a +3.5°C warmer-than-normal regime across Weeks 2-4
    gefs_daily = {
        f"temperature_2m_max_member{m:02d}": [19.5 + 0.1 * (m % 5)] * 35 for m in range(1, 32)
    }
    gefs_tmax = extract_daily_member_matrix(gefs_daily, "temperature_2m_max", 35)
    assert gefs_tmax.shape == (31, 35)
    gefs_tmin = np.full((31, 35), 8.5, dtype=np.float64)
    gefs_prcp = np.full((31, 35), 3.0, dtype=np.float64)

    seas_tmax = np.full((51, 35), 19.2, dtype=np.float64)
    seas_tmin = np.full((51, 35), 8.2, dtype=np.float64)
    seas_prcp = np.full((51, 35), 2.8, dtype=np.float64)

    out = compute_subseasonal_weeks_emos(
        today_local=today,
        gefs_dates=gefs_dates,
        gefs_tmax=gefs_tmax,
        gefs_tmin=gefs_tmin,
        gefs_prcp=gefs_prcp,
        seas_dates=seas_dates,
        seas_tmax=seas_tmax,
        seas_tmin=seas_tmin,
        seas_prcp=seas_prcp,
        hist_dates=hist_dates,
        hist_tmax=hist_tmax,
        hist_tmin=hist_tmin,
        hist_prcp=hist_prcp,
        cpc_summary={"8_14_temp": {"category": "Above", "prob_pct": 60}, "8_14_precip": None},
        local_microclimate_offset_c=0.4,
        soil_dryness_index=0.5,
    )

    assert out["available"] is True
    assert out["ensemble_members"] == 82
    assert len(out["weeks"]) == 3

    w2, w3, w4 = out["weeks"]
    assert w2["temp_signal"] == "warmer"
    assert w2["cpc_temp_badge"] == "NOAA CPC: 60% Leaning Warmer"
    assert w2["high_p10_c"] < w2["high_p50_c"] < w2["high_p90_c"]
    # EMOS shrinkage damps Week 4 anomaly closer to climatology than Week 2!
    assert w2["emos_shrinkage_alpha"] > w3["emos_shrinkage_alpha"] > w4["emos_shrinkage_alpha"]
    assert w2["temp_anomaly_c"] > w4["temp_anomaly_c"] > 0.0


def test_730d_compaction_synoptic_thinning_recency_weights_and_global_qpf(tmp_path: Path) -> None:
    # 1. DuckDB 730d rolling retention + >90d 6-hourly synoptic cycle thinning
    store = TelemetryStore(tmp_path / "telemetry.duckdb")
    now_utc = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    def _mk_snap(valid_dt: datetime, init_hour: int, sub15: bool = False) -> dict[str, object]:
        init_dt = valid_dt.replace(hour=init_hour) - timedelta(days=1)
        return {
            "location_id": 1,
            "model_id": "gfs",
            "init_time_utc": init_dt,
            "available_at_utc": init_dt + timedelta(hours=2),
            "valid_time_utc": valid_dt,
            "lead_hours": 24.0,
            "is_subhourly_15m": sub15,
            "temp_c": 22.0,
            "dewpoint_c": 14.0,
            "wind_ms": 5.0,
            "gust_ms": 8.0,
            "wind_dir_deg": 180.0,
            "precip_mm": 0.0,
            "snowfall_cm": 0.0,
            "precip_prob": 10.0,
            "cloud_pct": 25.0,
            "pressure_hpa": 1013.0,
            "solar_wm2": 300.0,
            "cape_jkg": 0.0,
            "cin_jkg": 0.0,
            "freezing_level_m": 3500.0,
            "ring_precip_max_mm": 0.0,
            "ring_precip_mean_mm": 0.0,
            "ring_temp_grad_c": 0.1,
            "ring_theta_e_grad_k": 0.2,
            "ring_gust_max_ms": 8.0,
        }

    rows = [
        _mk_snap(now_utc - timedelta(days=10), 13, sub15=False),   # Recent non-synoptic -> KEEP
        _mk_snap(now_utc - timedelta(days=45), 12, sub15=True),    # >30d 15m subhourly -> DELETE
        _mk_snap(now_utc - timedelta(days=150), 12, sub15=False),  # >90d 12Z synoptic -> KEEP
        _mk_snap(now_utc - timedelta(days=150), 15, sub15=False),  # >90d 15Z intermediate -> THIN!
        _mk_snap(now_utc - timedelta(days=800), 0, sub15=False),   # >730d expired -> DELETE
    ]
    store._upsert_polars_sync("forecast_snapshots", pl.DataFrame(rows))
    stats = store.compact_telemetry_sync(
        subhourly_days=30, retention_days=730, thin_runs_after_days=90
    )
    assert stats["subhourly_deleted"] == 1
    assert stats["snapshots_expired"] == 1
    assert stats["intermediate_runs_thinned"] == 1

    rem = store._query_pl_sync("SELECT * FROM forecast_snapshots")
    assert rem.height == 2
    store.close()

    # 2. Exponential recency weights across a 2-year (730-day) training span
    w_df = pl.DataFrame(
        {
            "valid_time_utc": [
                now_utc - timedelta(days=730),
                now_utc - timedelta(days=420),
                now_utc,
            ]
        }
    )
    weights = compute_exponential_recency_weights(w_df, half_life_days=420.0, min_weight=0.35)
    assert weights[2] == pytest.approx(1.0)
    assert weights[1] == pytest.approx(0.50, abs=0.01)
    assert 0.35 <= weights[0] < 0.50

    # 3. Non-CONUS (e.g. Aruba) consensus QPF renormalization when HRRR/RRFS/NBM are absent
    aruba_grid = pl.DataFrame(
        [
            {
                "location_id": 0,
                "model_id": "ecmwf_ifs",
                "init_time_utc": now_utc - timedelta(hours=6),
                "available_at_utc": now_utc - timedelta(hours=1),
                "valid_time_utc": now_utc + timedelta(hours=3),
                "lead_hours": 9.0,
                "is_subhourly_15m": False,
                "temp_c": 29.0,
                "dewpoint_c": 24.0,
                "wind_ms": 8.0,
                "gust_ms": 11.0,
                "wind_dir_deg": 90.0,
                "precip_mm": 4.0,
                "snowfall_cm": 0.0,
                "precip_prob": None,
                "cloud_pct": 65.0,
                "pressure_hpa": 1012.0,
                "solar_wm2": 500.0,
                "cape_jkg": 900.0,
                "cin_jkg": -20.0,
                "freezing_level_m": 4800.0,
                "ring_precip_max_mm": 5.0,
                "ring_precip_mean_mm": 4.2,
                "ring_temp_grad_c": 0.3,
                "ring_theta_e_grad_k": 1.0,
                "ring_gust_max_ms": 12.0,
                "model_run_rank": 1,
            },
            {
                "location_id": 0,
                "model_id": "gfs",
                "init_time_utc": now_utc - timedelta(hours=6),
                "available_at_utc": now_utc - timedelta(hours=1),
                "valid_time_utc": now_utc + timedelta(hours=3),
                "lead_hours": 9.0,
                "is_subhourly_15m": False,
                "temp_c": 28.8,
                "dewpoint_c": 23.8,
                "wind_ms": 7.8,
                "gust_ms": 10.8,
                "wind_dir_deg": 90.0,
                "precip_mm": 4.0,
                "snowfall_cm": 0.0,
                "precip_prob": None,
                "cloud_pct": 60.0,
                "pressure_hpa": 1012.0,
                "solar_wm2": 510.0,
                "cape_jkg": 850.0,
                "cin_jkg": -25.0,
                "freezing_level_m": 4800.0,
                "ring_precip_max_mm": 4.5,
                "ring_precip_mean_mm": 4.0,
                "ring_temp_grad_c": 0.2,
                "ring_theta_e_grad_k": 0.8,
                "ring_gust_max_ms": 11.5,
                "model_run_rank": 1,
            },
        ]
    )
    wide = pivot_inference_grid_to_wide(aruba_grid, now_utc.timestamp())
    preds = compute_consensus_predictions(wide, now_utc=now_utc)
    assert len(preds) == 1
    # Both available global models predict 4.0 mm -> renormalized QPF must be 4.0 mm, NOT diluted to 0.8 mm!
    assert preds[0]["precip_p50_mm"] == pytest.approx(4.0)
    assert preds[0]["precip_prob"] >= 0.85
