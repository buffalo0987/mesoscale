"""Tests for DuckDB point-in-time ASOF non-leakage, 8km spatial ring gradients, and wet-bulb physics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from mesoscale.db.telemetry_store import TelemetryStore
from mesoscale.ml.thermodynamics import (
    classify_winter_precip_type,
    equivalent_potential_temperature_k,
    wet_bulb_celsius,
)
from mesoscale.providers.open_meteo import SNAPSHOT_SCHEMA
from mesoscale.providers.spatial_ring import compute_ring_features, compute_spatial_ring


def test_duckdb_point_in_time_non_leakage(tmp_path: Path) -> None:
    """
    Verify that a 12:00Z HRRR run (disseminated at 12:55Z) is NEVER visible to an
    inference or training query issued at 12:15Z (strictly sees 11:00Z run instead).
    """
    store = TelemetryStore(tmp_path / "test.duckdb")
    store.initialize_sync()

    valid_t = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    init_11z = datetime(2026, 11, 6, 11, 0, tzinfo=UTC)
    init_12z = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    def make_row(init_dt: datetime, temp_c: float) -> dict:
        base = {k: None for k in SNAPSHOT_SCHEMA}
        base.update(
            {
                "location_id": 1,
                "model_id": "hrrr",
                "init_time_utc": init_dt,
                "available_at_utc": init_dt + timedelta(minutes=55),
                "valid_time_utc": valid_t,
                "lead_hours": (valid_t - init_dt).total_seconds() / 3600.0,
                "is_subhourly_15m": False,
                "temp_c": temp_c,
                "dewpoint_c": 5.0,
            }
        )
        return base

    df = pl.DataFrame(
        [make_row(init_11z, 21.0), make_row(init_12z, 9.5)], schema=SNAPSHOT_SCHEMA
    )
    store._upsert_polars_sync("forecast_snapshots", df)

    # Query at 12:15Z (before 12Z HRRR finishes running at 12:55Z): MUST return 21.0°C!
    q_early = store.query_inference_grid_sync(
        1,
        datetime(2026, 11, 6, 12, 15, tzinfo=UTC),
        valid_t - timedelta(hours=1),
        valid_t + timedelta(hours=1),
    )
    assert q_early.height == 1
    assert abs(q_early["temp_c"][0] - 21.0) < 1e-6

    # Query at 13:05Z (after 12:55Z availability): MUST return 9.5°C and capture run-to-run drop!
    q_late = store.query_inference_grid_sync(
        1,
        datetime(2026, 11, 6, 13, 5, tzinfo=UTC),
        valid_t - timedelta(hours=1),
        valid_t + timedelta(hours=1),
    )
    assert q_late.height == 1
    assert abs(q_late["temp_c"][0] - 9.5) < 1e-6
    assert abs(q_late["hrrr_prev_temp_trend"][0] - (-11.5)) < 1e-6

    # Also verify ASOF JOIN in build_training_pairs_sync (6h lead = 12:00Z issue -> sees 11Z run = 21.0°C!)
    from mesoscale.providers.station_blender import BLENDED_HOURLY_SCHEMA

    obs_df = pl.DataFrame(
        [
            {
                "location_id": 1,
                "valid_time_utc": valid_t,
                "temp_c": 10.2,
                "dewpoint_c": 4.0,
                "wet_bulb_c": 7.5,
                "wind_ms": 5.0,
                "gust_ms": 8.0,
                "wind_dir_deg": 10.0,
                "precip_1h_mm": 0.0,
                "pressure_hpa": 980.0,
                "solar_wm2": 100.0,
                "user_condition": None,
                "station_count": 2,
            }
        ],
        schema=BLENDED_HOURLY_SCHEMA,
    )
    store._upsert_polars_sync("observations_blended_hourly", obs_df)
    pairs = store.build_training_pairs_sync(1, lead_offsets_hours=(5, 6))
    # target_lead_h=6 -> issue_time = 12:00Z (12Z HRRR available at 12:55Z -> sees 21.0°C)
    # target_lead_h=5 -> issue_time = 13:00Z (after 12:55Z -> sees 9.5°C)
    by_lead = {int(r["target_lead_h"]): r["hrrr_temp_c"] for r in pairs.to_dicts()}
    assert abs(by_lead[6] - 21.0) < 1e-6
    assert abs(by_lead[5] - 9.5) < 1e-6
    store.close()


def test_spatial_ring_dryline_theta_e_gradient_and_winter_freezing_rain() -> None:
    pts = compute_spatial_ring(35.4676, -97.5164, radius_km=8.0)
    assert len(pts) == 5

    # Simulate classic Oklahoma dryline: 32°C / Td 22°C East vs 33°C / Td -2°C West!
    # Notice raw temperature barely changes (1°C), while theta_e jumps by > 40 K!
    def mk_pt(t: float, td: float, p_mm: float) -> dict[str, np.ndarray]:
        return {
            "temp_c": np.array([t]),
            "dewpoint_c": np.array([td]),
            "precip_mm": np.array([p_mm]),
            "gust_ms": np.array([12.0]),
            "pressure_hpa": np.array([970.0]),
        }

    ring = compute_ring_features(
        [
            mk_pt(32.5, 12.0, 0.0),  # center (dry!)
            mk_pt(32.5, 12.0, 0.2),  # north
            mk_pt(32.0, 22.5, 9.4),  # east (moist supercell 8 km away!)
            mk_pt(32.5, 12.0, 0.0),  # south
            mk_pt(33.0, -2.0, 0.0),  # west (bone-dry caprock air)
        ]
    )
    assert ring["ring_precip_max_mm"][0] == 9.4
    assert ring["ring_temp_grad_c"][0] < 2.0
    assert ring["ring_theta_e_grad_k"][0] > 35.0

    # Verify Oklahoma Ice Storm freezing-rain detection:
    # Surface air temp +1.2°C (~34.2°F) in dry ENE wind (Td -4.5°C) -> Wet-Bulb is SUB-FREEZING!
    tw = float(wet_bulb_celsius(1.2, dewpoint_c=-4.5))
    assert tw < -0.5
    ptype = classify_winter_precip_type(
        temp_c=1.2,
        wet_bulb_c=tw,
        precip_prob=0.75,
        precip_mm=2.5,
        snowfall_cm=0.0,
        freezing_level_m=950.0,
    )
    assert ptype == "freezing_rain"

    # Deep sub-freezing snow case
    tw_snow = float(wet_bulb_celsius(-4.0, dewpoint_c=-5.0))
    assert (
        classify_winter_precip_type(
            temp_c=-4.0,
            wet_bulb_c=tw_snow,
            precip_prob=0.80,
            precip_mm=2.0,
            snowfall_cm=2.2,
            freezing_level_m=0.0,
        )
        == "snow"
    )
    assert float(equivalent_potential_temperature_k(32.0, 22.0, 975.0)) > float(
        equivalent_potential_temperature_k(32.0, 0.0, 975.0)
    )
