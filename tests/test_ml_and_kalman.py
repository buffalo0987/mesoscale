"""Tests for CatBoost MultiQuantile + LightGBM MOS skill vs NBM, ACI coverage, and Kalman filter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from mesoscale.ml.conformal_aci import (
    default_aci_state,
    update_aci_state_with_residuals,
)
from mesoscale.ml.kalman_nowcast import project_kalman_offsets, update_kalman_state
from mesoscale.ml.mos_trainer import predict_with_artifact, train_and_gate_mos_model


def _generate_regime_biased_dataset(n_hours: int = 520) -> pl.DataFrame:
    """
    Create synthetic multi-model dataset mimicking real Southern Plains weather regimes:
    - Regime 1 (Northerly shallow cold dome + overcast): ECMWF is +5.5°C too warm, NBM is +3.2°C
      too warm, HRRR is +1.2°C too warm.
    - Regime 2 (Clear calm night valley drainage): All NWP models are +2.8°C too warm.
    - Convective Rain: Driven strongly by `hrrr_ring_precip_max_mm` when point `nbm_precip_prob` under-forecasts.
    """
    rng = np.random.default_rng(12345)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    rows: list[dict] = []

    for i in range(n_hours):
        vt = start + timedelta(hours=i)
        hr = vt.hour
        diurnal = -5.0 * np.cos(2.0 * np.pi * (hr - 3) / 24.0)
        synoptic = 10.0 + 9.0 * np.sin(2.0 * np.pi * i / 120.0)
        north_front = synoptic < 7.5
        clear_calm_night = (not north_front) and (hr in (3, 4, 5, 6, 7, 8, 9, 10))

        true_temp = synoptic + diurnal + rng.normal(0, 0.45)
        if clear_calm_night:
            true_temp -= 2.8  # Radiational drainage microclimate

        if north_front:
            wdir = 10.0
            cloud = 95.0
            hrrr_t = true_temp + 1.2 + rng.normal(0, 0.5)
            rrfs_t = true_temp + 1.0 + rng.normal(0, 0.5)
            nbm_t = true_temp + 3.4 + rng.normal(0, 0.6)
            ifs_t = true_temp + 5.6 + rng.normal(0, 0.7)
            aifs_t = true_temp + 4.8 + rng.normal(0, 0.7)
            gfs_t = true_temp + 4.5 + rng.normal(0, 0.8)
        elif clear_calm_night:
            wdir = 190.0
            cloud = 5.0
            hrrr_t = true_temp + 2.8 + rng.normal(0, 0.5)
            rrfs_t = true_temp + 2.6 + rng.normal(0, 0.5)
            nbm_t = true_temp + 2.9 + rng.normal(0, 0.5)
            ifs_t = true_temp + 2.7 + rng.normal(0, 0.5)
            aifs_t = true_temp + 2.8 + rng.normal(0, 0.5)
            gfs_t = true_temp + 3.0 + rng.normal(0, 0.6)
        else:
            wdir = 175.0
            cloud = 35.0
            hrrr_t = true_temp + rng.normal(0, 0.8)
            rrfs_t = true_temp + rng.normal(0, 0.8)
            nbm_t = true_temp + rng.normal(0, 0.8)
            ifs_t = true_temp + rng.normal(0, 0.7)
            aifs_t = true_temp + rng.normal(0, 0.7)
            gfs_t = true_temp + rng.normal(0, 0.9)

        lead_h = float([6, 12, 24, 36, 42, 60, 96][i % 7])
        nearby_cell = (i % 9 == 0)
        ring_max = 8.5 if nearby_cell else 0.0
        obs_precip = 2.4 if (nearby_cell and rng.random() < 0.75) else 0.0

        rows.append(
            {
                "location_id": 1,
                "valid_time_utc": vt,
                "target_lead_h": lead_h,
                "obs_temp_c": true_temp,
                "obs_dewpoint_c": true_temp - 5.0,
                "obs_wet_bulb_c": true_temp - 2.0,
                "obs_wind_ms": 6.0 if north_front else 2.0,
                "obs_gust_ms": 10.0 if north_front else 3.5,
                "obs_wind_dir_deg": wdir,
                "obs_precip_1h_mm": obs_precip,
                "obs_pressure_hpa": 982.0 if north_front else 974.0,
                "obs_user_condition": None,
                "hrrr_temp_c": None if lead_h > 48 else hrrr_t,
                "rrfs_temp_c": None if lead_h > 60 else rrfs_t,
                "nbm_temp_c": nbm_t,
                "ecmwf_ifs_temp_c": ifs_t,
                "ecmwf_aifs_temp_c": aifs_t,
                "gfs_temp_c": gfs_t,
                "nbm_dewpoint_c": true_temp - 4.5,
                "hrrr_dewpoint_c": None if lead_h > 48 else true_temp - 4.8,
                "hrrr_wind_ms": None if lead_h > 48 else (6.5 if north_front else 2.2),
                "nbm_wind_ms": 6.0 if north_front else 2.2,
                "hrrr_gust_ms": None if lead_h > 48 else (10.5 if north_front else 3.8),
                "nbm_gust_ms": 9.8 if north_front else 3.8,
                "hrrr_wind_dir_deg": None if lead_h > 48 else wdir,
                "nbm_wind_dir_deg": wdir,
                "hrrr_cloud_pct": None if lead_h > 48 else cloud,
                "nbm_cloud_pct": cloud,
                "hrrr_pressure_hpa": 982.0 if north_front else 974.0,
                "hrrr_solar_wm2": 0.0 if clear_calm_night else 350.0,
                "hrrr_cape_jkg": 1800.0 if nearby_cell else 50.0,
                "hrrr_cin_jkg": -20.0 if nearby_cell else -120.0,
                "hrrr_freezing_level_m": 2800.0,
                "hrrr_snowfall_cm": 0.0,
                "hrrr_precip_mm": 0.1 if nearby_cell else 0.0,
                "nbm_precip_mm": 0.1 if nearby_cell else 0.0,
                "ecmwf_ifs_precip_mm": 0.0,
                "gfs_precip_mm": 0.0,
                "nbm_precip_prob": 20.0 if nearby_cell else 5.0,
                "hrrr_precip_prob": 25.0 if nearby_cell else 5.0,
                "hrrr_ring_precip_max_mm": ring_max,
                "hrrr_ring_temp_grad_c": 4.5 if north_front else 0.5,
                "hrrr_ring_theta_e_grad_k": 14.0 if north_front else 1.5,
                "hrrr_ring_gust_max_ms": 12.0 if north_front else 4.0,
            }
        )
    return pl.DataFrame(rows)


def test_stacked_catboost_lightgbm_mos_beats_nbm_and_hrrr(tmp_path: Path) -> None:
    df = _generate_regime_biased_dataset(520)
    artifact, report = train_and_gate_mos_model(
        location_id=1,
        train_df=df,
        models_dir=tmp_path / "models",
    )
    assert artifact is not None
    assert report["promoted"] is True
    # Mesoscale AI MUST substantially beat both raw NBM and raw HRRR on chronological holdout!
    assert report["mesoscale_mae_c"] < report["nbm_mae_c"] * 0.65
    assert report["mesoscale_mae_c"] < report["hrrr_mae_c"] * 0.80
    assert report["mesoscale_brier"] < report["nbm_brier"]

    # Check non-crossing quantile monotonicity across all lead hours (including 36-48h cosine taper)
    preds = predict_with_artifact(artifact, df.tail(35))
    assert len(preds) == 35
    for r in preds:
        qs = [
            r["temp_p05_c"],
            r["temp_p10_c"],
            r["temp_p25_c"],
            r["temp_p50_c"],
            r["temp_p75_c"],
            r["temp_p90_c"],
            r["temp_p95_c"],
        ]
        assert qs == sorted(qs), f"Quantiles crossed: {qs}"


def test_conformal_aci_widens_under_volatility_and_kalman_tracks_cold_front() -> None:
    # 1. ACI: feed repeated out-of-band busts -> interval scale must automatically widen > 1.0!
    state = default_aci_state()
    state = update_aci_state_with_residuals(
        state,
        lead_hours=np.full(20, 12.0),
        p10=np.full(20, 10.0),
        p90=np.full(20, 14.0),
        obs=np.full(20, 3.0),  # Severe cold busts outside [10, 14]
    )
    assert state["7_24h"]["scale"] > 1.15

    # 2. 2-State Kalman Filter: simulate a cold front plunging from -2°C error to -6°C error in 30 min
    t0 = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=30)
    ks = update_kalman_state(None, obs_time_utc=t0, obs_temp_c=16.0, pred_temp_c=18.0)
    ks = update_kalman_state(ks, obs_time_utc=t1, obs_temp_c=11.0, pred_temp_c=17.5)
    # Both level bias and negative cooling trend rate must be active
    assert ks["x"][0] < -3.0
    assert ks["x"][1] < -1.0

    future = [t1 + timedelta(hours=h) for h in (1, 3, 9)]
    t_nudge, _, _ = project_kalman_offsets(ks, t1, future)
    assert t_nudge[0] < -2.5  # Strong immediate cold-front downward correction at +1h
    assert abs(t_nudge[2]) < abs(t_nudge[0])  # Decays smoothly toward 0 at +9h
