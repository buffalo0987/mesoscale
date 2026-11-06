"""Horizon-split CatBoost MultiQuantile + LightGBM Residual MOS & Champion/Challenger Gate."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import polars as pl
from catboost import CatBoostRegressor
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.isotonic import IsotonicRegression

from mesoscale.ml.bust_detector import evaluate_hourly_diagnostics
from mesoscale.ml.conformal_aci import apply_aci_scaling
from mesoscale.ml.features import FEATURE_COLUMNS, build_feature_matrix
from mesoscale.ml.kalman_nowcast import project_kalman_offsets
from mesoscale.ml.thermodynamics import (
    apparent_temperature_c,
    classify_winter_precip_type,
    wet_bulb_celsius,
)

QUANTILES: tuple[float, ...] = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
QUANTILE_ALPHA_STR = ",".join(f"{q:.2f}" for q in QUANTILES)


def pinball_loss_multi(y_true: np.ndarray, q_preds: np.ndarray, quantiles: tuple[float, ...] = QUANTILES) -> float:
    """Mean pinball loss (discrete CRPS proxy) across all predicted quantiles."""
    mask = ~np.isnan(y_true)
    if not np.any(mask):
        return 0.0
    yt = y_true[mask][:, None]
    qp = q_preds[mask]
    qs = np.array(quantiles, dtype=np.float64)[None, :]
    diff = yt - qp
    loss = np.maximum(qs * diff, (qs - 1.0) * diff)
    return float(np.mean(loss))


def compute_horizon_prior_temp(
    aux: dict[str, np.ndarray], hedge_weights: dict[str, float] | None = None
) -> np.ndarray:
    """
    Compute physics-informed baseline prior temperature blending mesoscale models (HRRR, RRFS, NBM)
    at short range (0-36h) and global/blended models (NBM, ECMWF IFS, ECMWF AIFS, GFS) at medium range.
    Predicting residuals relative to this physical prior prevents tree leaf clipping in extreme weather!
    """
    lead_h = aux["lead_h"]
    n = len(lead_h)
    models = ("hrrr", "rrfs", "nbm", "ecmwf_ifs", "ecmwf_aifs", "gfs")
    arrs = {m: aux[f"{m}_temp_c"] for m in models}

    out = np.zeros(n, dtype=np.float64)
    hw = hedge_weights or {}

    for i in range(n):
        lh = float(lead_h[i])
        if lh <= 18.0:
            base_w = {"hrrr": 0.34, "rrfs": 0.22, "nbm": 0.24, "ecmwf_ifs": 0.10, "ecmwf_aifs": 0.06, "gfs": 0.04}
        elif lh <= 48.0:
            base_w = {"hrrr": 0.24, "rrfs": 0.20, "nbm": 0.28, "ecmwf_ifs": 0.14, "ecmwf_aifs": 0.09, "gfs": 0.05}
        else:
            base_w = {"hrrr": 0.0, "rrfs": 0.10, "nbm": 0.34, "ecmwf_ifs": 0.28, "ecmwf_aifs": 0.20, "gfs": 0.08}

        num = den = 0.0
        for m in models:
            val = arrs[m][i]
            if not np.isnan(val):
                # Blend horizon prior weight with online Hedge performance weight when <= 24h
                w = base_w[m]
                if lh <= 24.0 and m in hw:
                    w = 0.7 * w + 0.3 * float(hw[m])
                num += w * float(val)
                den += w
        out[i] = num / den if den > 0 else float(aux["ens_mean_temp_c"][i])
    return out


def compute_consensus_predictions(
    wide_df: pl.DataFrame,
    *,
    kalman_state: dict[str, Any] | None = None,
    aci_state: dict[str, Any] | None = None,
    now_utc: Any = None,
) -> list[dict[str, Any]]:
    """
    Fast analytical multi-model consensus + Kalman nowcast engine.
    Used in <2s Ephemeral/GPS lookups and while Day-1 backfill is still running.
    """
    if wide_df.is_empty():
        return []
    _, aux = build_feature_matrix(wide_df)
    n = wide_df.height
    hw = (kalman_state or {}).get("hedge_weights")
    prior_t = compute_horizon_prior_temp(aux, hedge_weights=hw)

    # Calibrated ensemble spread + lead-growth uncertainty envelope
    spread = np.maximum(0.9, aux["ens_std_temp_c"] * 1.35 + 0.55 * np.sqrt(np.maximum(aux["lead_h"], 1.0) / 12.0))
    z_scores = np.array([-1.645, -1.282, -0.674, 0.0, 0.674, 1.282, 1.645], dtype=np.float64)
    q_mat = prior_t[:, None] + spread[:, None] * z_scores[None, :]
    q_mat = apply_aci_scaling(q_mat, aux["lead_h"], aci_state)

    # Precipitation probability consensus incorporating 8 km convective ring proximity
    nbm_pop = np.nan_to_num(
        wide_df["nbm_precip_prob"].cast(pl.Float64).to_numpy() / 100.0
        if "nbm_precip_prob" in wide_df.columns
        else np.zeros(n),
        nan=0.0,
    )
    hrrr_p = np.nan_to_num(
        wide_df["hrrr_precip_mm"].cast(pl.Float64).to_numpy()
        if "hrrr_precip_mm" in wide_df.columns
        else np.zeros(n),
        nan=0.0,
    )
    ifs_p = np.nan_to_num(
        wide_df["ecmwf_ifs_precip_mm"].cast(pl.Float64).to_numpy()
        if "ecmwf_ifs_precip_mm" in wide_df.columns
        else np.zeros(n),
        nan=0.0,
    )
    nbm_p = np.nan_to_num(
        wide_df["nbm_precip_mm"].cast(pl.Float64).to_numpy()
        if "nbm_precip_mm" in wide_df.columns
        else np.zeros(n),
        nan=0.0,
    )
    point_qpf = 0.4 * hrrr_p + 0.35 * nbm_p + 0.25 * ifs_p
    ring_boost = np.clip(aux["ring_precip_max_mm"] / 12.0, 0.0, 0.35)
    pop_cal = np.clip(np.maximum(nbm_pop, np.where(point_qpf >= 0.25, 0.55, 0.05) + ring_boost), 0.0, 0.98)

    valid_dts = wide_df["valid_time_utc"].to_list()
    if now_utc is not None and kalman_state:
        t_nudge, td_nudge, w_nudge = project_kalman_offsets(kalman_state, now_utc, valid_dts)
    else:
        t_nudge = np.zeros(n)
        td_nudge = np.zeros(n)
        w_nudge = np.zeros(n)

    q_mat = q_mat + t_nudge[:, None]
    td_p50 = np.minimum(q_mat[:, 3], aux["best_dewpoint_c"] + td_nudge)
    wind_p50 = np.maximum(0.0, aux["best_wind_ms"] + w_nudge)
    gust_p50 = np.maximum(wind_p50, aux["best_gust_ms"] + w_nudge)
    gust_p90 = np.maximum(gust_p50 * 1.2, aux["ring_precip_max_mm"] * 0.1 + gust_p50 + 2.5)

    return _assemble_hourly_forecast_dicts(
        valid_dts=valid_dts,
        aux=aux,
        q_mat=q_mat,
        td_p50=td_p50,
        wind_p50=wind_p50,
        gust_p50=gust_p50,
        gust_p90=gust_p90,
        pop_cal=pop_cal,
        qpf_p50=point_qpf,
        qpf_p90=np.maximum(point_qpf * 1.8, aux["ring_precip_max_mm"] * 0.6),
    )


@dataclass
class HorizonExpertBundle:
    cat_temp_quantiles: CatBoostRegressor
    lgb_temp_median: LGBMRegressor
    lgb_dewpoint: LGBMRegressor
    lgb_wind: LGBMRegressor
    lgb_gust_p50: LGBMRegressor
    lgb_gust_p90: LGBMRegressor
    lgb_pop_clf: LGBMClassifier | None
    iso_pop: IsotonicRegression | None
    lgb_qpf_p50: LGBMRegressor | None


@dataclass
class TrainedMosArtifact:
    location_id: int
    version: int
    mesoscale_expert: HorizonExpertBundle
    synoptic_expert: HorizonExpertBundle
    feature_importances: list[dict[str, Any]]
    holdout_metrics: dict[str, Any]


def _fit_horizon_expert(
    X: np.ndarray,
    prior_temp: np.ndarray,
    aux_dewpoint: np.ndarray,
    aux_wind: np.ndarray,
    aux_gust: np.ndarray,
    y_temp: np.ndarray,
    y_td: np.ndarray,
    y_wind: np.ndarray,
    y_gust: np.ndarray,
    y_precip_mm: np.ndarray,
    sample_weight: np.ndarray,
    precip_sample_weight: np.ndarray,
) -> HorizonExpertBundle:
    """Fit CatBoost MultiQuantile + LightGBM stacked residual models on a horizon subset."""
    res_temp = y_temp - prior_temp
    X_cb = np.where(np.isnan(X), np.nan, X)

    cat_q = CatBoostRegressor(
        loss_function=f"MultiQuantile:alpha={QUANTILE_ALPHA_STR}",
        iterations=240,
        depth=6,
        learning_rate=0.055,
        l2_leaf_reg=4.0,
        random_seed=42,
        verbose=False,
        allow_writing_files=False,
        nan_mode="Min",
    )
    cat_q.fit(X_cb, res_temp, sample_weight=sample_weight)

    lgb_med = LGBMRegressor(
        objective="regression_l1",
        n_estimators=200,
        num_leaves=31,
        learning_rate=0.05,
        min_child_samples=max(8, min(25, len(X) // 15)),
        subsample=0.85,
        colsample_bytree=0.85,
        random_state=42,
        n_jobs=2,
        verbose=-1,
    )
    lgb_med.fit(X, res_temp, sample_weight=sample_weight)

    def fit_aux_lgb(target_obs: np.ndarray, base_arr: np.ndarray, obj: str = "regression_l1", alpha: float = 0.5) -> LGBMRegressor:
        valid = ~np.isnan(target_obs)
        if np.sum(valid) < 30:
            y_res = np.zeros(len(X), dtype=np.float64)
            w_sub = np.ones(len(X), dtype=np.float64)
            X_sub = X
        else:
            X_sub = X[valid]
            y_res = target_obs[valid] - base_arr[valid]
            w_sub = sample_weight[valid]
        kwargs: dict[str, Any] = {
            "objective": obj,
            "n_estimators": 140,
            "num_leaves": 23,
            "learning_rate": 0.05,
            "min_child_samples": max(6, min(20, len(X_sub) // 15)),
            "random_state": 42,
            "n_jobs=2": 2,
            "verbose": -1,
        }
        kwargs.pop("n_jobs=2", None)
        kwargs["n_jobs"] = 2
        if obj == "quantile":
            kwargs["alpha"] = alpha
        reg = LGBMRegressor(**kwargs)
        reg.fit(X_sub, y_res, sample_weight=w_sub)
        return reg

    lgb_td = fit_aux_lgb(y_td, aux_dewpoint)
    lgb_ws = fit_aux_lgb(y_wind, aux_wind)
    lgb_wg50 = fit_aux_lgb(y_gust, aux_gust)
    lgb_wg90 = fit_aux_lgb(y_gust, aux_gust, obj="quantile", alpha=0.90)

    # Precipitation probability (PoP >= 0.25 mm) + Isotonic calibration
    p_clean = np.nan_to_num(y_precip_mm, nan=0.0)
    y_wet = (p_clean >= 0.25).astype(np.int32)
    wet_cnt = int(np.sum(y_wet == 1))
    dry_cnt = int(np.sum(y_wet == 0))

    lgb_pop: LGBMClassifier | None = None
    iso_pop: IsotonicRegression | None = None
    lgb_qpf: LGBMRegressor | None = None

    if wet_cnt >= 8 and dry_cnt >= 8:
        lgb_pop = LGBMClassifier(
            n_estimators=150,
            num_leaves=23,
            learning_rate=0.05,
            min_child_samples=max(6, min(20, len(X) // 20)),
            random_state=42,
            n_jobs=2,
            verbose=-1,
        )
        # Out-of-time calibration tail (last 25%) for IsotonicRegression so PoP doesn't overfit
        cal_split = int(len(X) * 0.75)
        if (
            cal_split >= 40
            and len(np.unique(y_wet[:cal_split])) == 2
            and len(np.unique(y_wet[cal_split:])) == 2
        ):
            lgb_pop.fit(X[:cal_split], y_wet[:cal_split], sample_weight=precip_sample_weight[:cal_split])
            raw_cal_probs = lgb_pop.predict_proba(X[cal_split:])[:, 1]
            iso_pop = IsotonicRegression(y_min=0.01, y_max=0.99, out_of_bounds="clip")
            iso_pop.fit(raw_cal_probs, y_wet[cal_split:], sample_weight=precip_sample_weight[cal_split:])
            # Refit classifier on full slice while retaining out-of-sample isotonic map
            lgb_pop.fit(X, y_wet, sample_weight=precip_sample_weight)
        else:
            lgb_pop.fit(X, y_wet, sample_weight=precip_sample_weight)

        wet_idx = np.nonzero(p_clean >= 0.1)[0]
        if len(wet_idx) >= 15:
            lgb_qpf = LGBMRegressor(
                objective="regression_l1",
                n_estimators=120,
                num_leaves=15,
                learning_rate=0.05,
                min_child_samples=5,
                random_state=42,
                n_jobs=2,
                verbose=-1,
            )
            lgb_qpf.fit(X[wet_idx], np.log1p(p_clean[wet_idx]))

    return HorizonExpertBundle(
        cat_temp_quantiles=cat_q,
        lgb_temp_median=lgb_med,
        lgb_dewpoint=lgb_td,
        lgb_wind=lgb_ws,
        lgb_gust_p50=lgb_wg50,
        lgb_gust_p90=lgb_wg90,
        lgb_pop_clf=lgb_pop,
        iso_pop=iso_pop,
        lgb_qpf_p50=lgb_qpf,
    )


def _predict_single_expert(
    expert: HorizonExpertBundle,
    X: np.ndarray,
    prior_temp: np.ndarray,
    aux: dict[str, np.ndarray],
    wide_df: pl.DataFrame,
) -> dict[str, np.ndarray]:
    cat_res = np.asarray(expert.cat_temp_quantiles.predict(X), dtype=np.float64)
    if cat_res.ndim == 1:
        cat_res = np.tile(cat_res[:, None], (1, len(QUANTILES)))
    lgb_res_med = np.asarray(expert.lgb_temp_median.predict(X), dtype=np.float64)

    # Stack median (60% CatBoost MultiQuantile + 40% LightGBM L1) while preserving CatBoost quantile shape
    stacked_med_res = 0.60 * cat_res[:, 3] + 0.40 * lgb_res_med
    shift = (stacked_med_res - cat_res[:, 3])[:, None]
    q_temp = prior_temp[:, None] + cat_res + shift
    q_temp = np.maximum.accumulate(q_temp, axis=1)

    td_p50 = aux["best_dewpoint_c"] + np.asarray(expert.lgb_dewpoint.predict(X), dtype=np.float64)
    td_p50 = np.minimum(td_p50, q_temp[:, 3])

    wind_p50 = np.maximum(0.0, aux["best_wind_ms"] + np.asarray(expert.lgb_wind.predict(X), dtype=np.float64))
    gust_p50 = np.maximum(wind_p50, aux["best_gust_ms"] + np.asarray(expert.lgb_gust_p50.predict(X), dtype=np.float64))
    gust_p90 = np.maximum(gust_p50, aux["best_gust_ms"] + np.asarray(expert.lgb_gust_p90.predict(X), dtype=np.float64))

    n = len(X)
    if expert.lgb_pop_clf is not None:
        raw_pop = expert.lgb_pop_clf.predict_proba(X)[:, 1]
        pop = expert.iso_pop.predict(raw_pop) if expert.iso_pop is not None else raw_pop
        pop = np.clip(pop, 0.0, 0.99)
    else:
        nbm_pop = (
            np.nan_to_num(wide_df["nbm_precip_prob"].cast(pl.Float64).to_numpy() / 100.0, nan=0.05)
            if "nbm_precip_prob" in wide_df.columns
            else np.full(n, 0.05)
        )
        pop = np.clip(nbm_pop + np.clip(aux["ring_precip_max_mm"] / 15.0, 0.0, 0.30), 0.0, 0.98)

    if expert.lgb_qpf_p50 is not None:
        cond_mm = np.expm1(np.maximum(0.0, expert.lgb_qpf_p50.predict(X)))
        qpf_p50 = np.where(pop >= 0.25, cond_mm * pop, 0.0)
    else:
        hrrr_p = np.nan_to_num(
            wide_df["hrrr_precip_mm"].cast(pl.Float64).to_numpy()
            if "hrrr_precip_mm" in wide_df.columns
            else np.zeros(n),
            nan=0.0,
        )
        qpf_p50 = np.where(pop >= 0.20, hrrr_p, 0.0)

    qpf_p90 = np.maximum(qpf_p50 * 1.8, aux["ring_precip_max_mm"] * pop)
    return {
        "q_temp": q_temp,
        "td_p50": td_p50,
        "wind_p50": wind_p50,
        "gust_p50": gust_p50,
        "gust_p90": gust_p90,
        "pop": pop,
        "qpf_p50": qpf_p50,
        "qpf_p90": qpf_p90,
    }


def predict_with_artifact(
    artifact: TrainedMosArtifact,
    wide_df: pl.DataFrame,
    *,
    kalman_state: dict[str, Any] | None = None,
    aci_state: dict[str, Any] | None = None,
    now_utc: Any = None,
) -> list[dict[str, Any]]:
    """
    Generate full horizon-blended (cosine taper 36-48h) + ACI-calibrated + Kalman-nudged
    hourly forecast dictionaries from a TrainedMosArtifact.
    """
    if wide_df.is_empty():
        return []
    X, aux = build_feature_matrix(wide_df)
    hw = (kalman_state or {}).get("hedge_weights")
    prior_t = compute_horizon_prior_temp(aux, hedge_weights=hw)

    p_meso = _predict_single_expert(artifact.mesoscale_expert, X, prior_t, aux, wide_df)
    p_syn = _predict_single_expert(artifact.synoptic_expert, X, prior_t, aux, wide_df)

    # Smooth cosine horizon blend across 36h..48h (no step discontinuities!)
    lh = aux["lead_h"]
    w_meso = np.where(
        lh <= 36.0,
        1.0,
        np.where(lh >= 48.0, 0.0, 0.5 * (1.0 + np.cos(np.pi * (lh - 36.0) / 12.0))),
    )

    q_mat = w_meso[:, None] * p_meso["q_temp"] + (1.0 - w_meso[:, None]) * p_syn["q_temp"]
    q_mat = np.maximum.accumulate(q_mat, axis=1)
    q_mat = apply_aci_scaling(q_mat, lh, aci_state)

    td_p50 = w_meso * p_meso["td_p50"] + (1.0 - w_meso) * p_syn["td_p50"]
    wind_p50 = w_meso * p_meso["wind_p50"] + (1.0 - w_meso) * p_syn["wind_p50"]
    gust_p50 = w_meso * p_meso["gust_p50"] + (1.0 - w_meso) * p_syn["gust_p50"]
    gust_p90 = w_meso * p_meso["gust_p90"] + (1.0 - w_meso) * p_syn["gust_p90"]
    pop_cal = np.clip(w_meso * p_meso["pop"] + (1.0 - w_meso) * p_syn["pop"], 0.0, 0.99)
    qpf_p50 = w_meso * p_meso["qpf_p50"] + (1.0 - w_meso) * p_syn["qpf_p50"]
    qpf_p90 = w_meso * p_meso["qpf_p90"] + (1.0 - w_meso) * p_syn["qpf_p90"]

    valid_dts = wide_df["valid_time_utc"].to_list()
    if now_utc is not None and kalman_state:
        t_nudge, td_nudge, w_nudge = project_kalman_offsets(kalman_state, now_utc, valid_dts)
    else:
        t_nudge = np.zeros(len(lh))
        td_nudge = np.zeros(len(lh))
        w_nudge = np.zeros(len(lh))

    q_mat = q_mat + t_nudge[:, None]
    td_p50 = np.minimum(q_mat[:, 3], td_p50 + td_nudge)
    wind_p50 = np.maximum(0.0, wind_p50 + w_nudge)
    gust_p50 = np.maximum(wind_p50, gust_p50 + w_nudge)
    gust_p90 = np.maximum(gust_p50, gust_p90 + w_nudge)

    return _assemble_hourly_forecast_dicts(
        valid_dts=valid_dts,
        aux=aux,
        q_mat=q_mat,
        td_p50=td_p50,
        wind_p50=wind_p50,
        gust_p50=gust_p50,
        gust_p90=gust_p90,
        pop_cal=pop_cal,
        qpf_p50=qpf_p50,
        qpf_p90=qpf_p90,
    )


def _assemble_hourly_forecast_dicts(
    *,
    valid_dts: list[Any],
    aux: dict[str, np.ndarray],
    q_mat: np.ndarray,
    td_p50: np.ndarray,
    wind_p50: np.ndarray,
    gust_p50: np.ndarray,
    gust_p90: np.ndarray,
    pop_cal: np.ndarray,
    qpf_p50: np.ndarray,
    qpf_p90: np.ndarray,
) -> list[dict[str, Any]]:
    tw_arr = wet_bulb_celsius(q_mat[:, 3], dewpoint_c=td_p50)
    feels_arr = apparent_temperature_c(q_mat[:, 3], td_p50, wind_p50)

    def opt(arr: np.ndarray, idx: int) -> float | None:
        v = float(arr[idx])
        return None if math.isnan(v) else round(v, 2)

    out: list[dict[str, Any]] = []
    for i, vt in enumerate(valid_dts):
        t50 = float(q_mat[i, 3])
        tw = float(tw_arr[i])
        pop = float(pop_cal[i])
        qpf = float(max(0.0, qpf_p50[i]))
        snow_cm = float(aux["snowfall_cm"][i]) if not math.isnan(float(aux["snowfall_cm"][i])) else 0.0
        fzl = None if math.isnan(float(aux["freezing_level_m"][i])) else float(aux["freezing_level_m"][i])

        ptype = classify_winter_precip_type(
            temp_c=t50,
            wet_bulb_c=tw,
            precip_prob=pop,
            precip_mm=qpf,
            snowfall_cm=snow_cm,
            freezing_level_m=fzl,
        )
        diag = evaluate_hourly_diagnostics(
            lead_h=float(aux["lead_h"][i]),
            temp_p10_c=float(q_mat[i, 1]),
            temp_p50_c=t50,
            temp_p90_c=float(q_mat[i, 5]),
            wet_bulb_c=tw,
            precip_prob=pop,
            precip_type=ptype,
            hrrr_temp_c=opt(aux["hrrr_temp_c"], i),
            ecmwf_temp_c=opt(aux["ecmwf_ifs_temp_c"], i),
            ring_precip_max_mm=float(aux["ring_precip_max_mm"][i]),
            point_precip_mm=qpf,
            ring_theta_e_grad_k=float(aux["ring_theta_e_grad_k"][i]),
            cape_jkg=float(aux["cape_jkg"][i]),
            cin_jkg=float(aux["cin_jkg"][i]),
        )

        out.append(
            {
                "valid_time_utc": vt.isoformat(),
                "lead_hours": round(float(aux["lead_h"][i]), 2),
                "temp_p05_c": round(float(q_mat[i, 0]), 2),
                "temp_p10_c": round(float(q_mat[i, 1]), 2),
                "temp_p25_c": round(float(q_mat[i, 2]), 2),
                "temp_p50_c": round(t50, 2),
                "temp_p75_c": round(float(q_mat[i, 4]), 2),
                "temp_p90_c": round(float(q_mat[i, 5]), 2),
                "temp_p95_c": round(float(q_mat[i, 6]), 2),
                "feels_like_c": round(float(feels_arr[i]), 2),
                "dewpoint_p50_c": round(float(td_p50[i]), 2),
                "wet_bulb_c": round(tw, 2),
                "theta_e_k": round(float(aux["theta_e_k"][i]), 1),
                "wind_p50_ms": round(float(wind_p50[i]), 2),
                "gust_p50_ms": round(float(gust_p50[i]), 2),
                "gust_p90_ms": round(float(gust_p90[i]), 2),
                "wind_dir_deg": round(float(aux["wind_dir_deg"][i]), 0),
                "pressure_hpa": round(float(aux["pressure_hpa"][i]), 1),
                "cloud_pct": round(float(aux["cloud_pct"][i]), 0),
                "precip_prob": round(pop, 3),
                "precip_p50_mm": round(qpf, 2),
                "precip_p90_mm": round(float(max(qpf, qpf_p90[i])), 2),
                "ring_precip_max_mm": round(float(aux["ring_precip_max_mm"][i]), 2),
                "precip_type": ptype,
                "cape_jkg": round(float(aux["cape_jkg"][i]), 0),
                "cin_jkg": round(float(aux["cin_jkg"][i]), 0),
                "freezing_level_m": round(fzl, 0) if fzl is not None else None,
                "hrrr_3run_temp_trend_c": round(float(aux["hrrr_3run_temp_trend"][i]), 2),
                "ecmwf_2run_temp_trend_c": round(float(aux["ecmwf_2run_temp_trend"][i]), 2),
                "raw_models_temp_c": {
                    "hrrr": opt(aux["hrrr_temp_c"], i),
                    "rrfs": opt(aux["rrfs_temp_c"], i),
                    "nbm": opt(aux["nbm_temp_c"], i),
                    "ecmwf_ifs": opt(aux["ecmwf_ifs_temp_c"], i),
                    "ecmwf_aifs": opt(aux["ecmwf_aifs_temp_c"], i),
                    "gfs": opt(aux["gfs_temp_c"], i),
                },
                **diag,
            }
        )
    return out


def train_and_gate_mos_model(
    *,
    location_id: int,
    train_df: pl.DataFrame,
    models_dir: Path,
    existing_version: int = 0,
    existing_artifact_path: str | None = None,
) -> tuple[TrainedMosArtifact | None, dict[str, Any]]:
    """
    Train horizon-split CatBoost MultiQuantile + LightGBM Residual MOS, run chronological
    Champion/Challenger holdout evaluation against raw NBM & HRRR (and existing Champion),
    and persist artifact if approved.
    """
    if train_df.height < 180:
        return None, {
            "promoted": False,
            "reason": f"Insufficient training pairs ({train_df.height} < 180); using Live Consensus",
            "train_rows": train_df.height,
        }

    df = train_df.sort(["valid_time_utc", "target_lead_h"])
    n = df.height
    split_idx = int(n * 0.85)
    train_sub = df.slice(0, split_idx)
    val_sub = df.slice(split_idx, n - split_idx)

    def _fit_both_experts(sub_df: pl.DataFrame) -> tuple[HorizonExpertBundle, HorizonExpertBundle]:
        X_all, aux_all = build_feature_matrix(sub_df)
        prior_all = compute_horizon_prior_temp(aux_all)
        y_t = sub_df["obs_temp_c"].cast(pl.Float64).to_numpy()
        y_td = sub_df["obs_dewpoint_c"].cast(pl.Float64).to_numpy()
        y_ws = sub_df["obs_wind_ms"].cast(pl.Float64).to_numpy()
        y_wg = sub_df["obs_gust_ms"].cast(pl.Float64).to_numpy()
        y_pr = sub_df["obs_precip_1h_mm"].cast(pl.Float64).to_numpy()
        has_user = (
            sub_df["obs_user_condition"].is_not_null().to_numpy()
            if "obs_user_condition" in sub_df.columns
            else np.zeros(len(X_all), dtype=bool)
        )

        # Gentle exponential recency weighting (recent 6 months weighted 1.0 -> older 0.55)
        recency_w = np.linspace(0.55, 1.0, len(X_all), dtype=np.float64)
        precip_w = np.where(has_user, recency_w * 3.0, recency_w)

        meso_mask = aux_all["lead_h"] <= 48.0
        syn_mask = aux_all["lead_h"] >= 36.0
        if np.sum(meso_mask) < 60:
            meso_mask = np.ones(len(X_all), dtype=bool)
        if np.sum(syn_mask) < 60:
            syn_mask = np.ones(len(X_all), dtype=bool)

        meso_exp = _fit_horizon_expert(
            X_all[meso_mask],
            prior_all[meso_mask],
            aux_all["best_dewpoint_c"][meso_mask],
            aux_all["best_wind_ms"][meso_mask],
            aux_all["best_gust_ms"][meso_mask],
            y_t[meso_mask],
            y_td[meso_mask],
            y_ws[meso_mask],
            y_wg[meso_mask],
            y_pr[meso_mask],
            recency_w[meso_mask],
            precip_w[meso_mask],
        )
        syn_exp = _fit_horizon_expert(
            X_all[syn_mask],
            prior_all[syn_mask],
            aux_all["best_dewpoint_c"][syn_mask],
            aux_all["best_wind_ms"][syn_mask],
            aux_all["best_gust_ms"][syn_mask],
            y_t[syn_mask],
            y_td[syn_mask],
            y_ws[syn_mask],
            y_wg[syn_mask],
            y_pr[syn_mask],
            recency_w[syn_mask],
            precip_w[syn_mask],
        )
        return meso_exp, syn_exp

    cand_meso, cand_syn = _fit_both_experts(train_sub)
    cand_tmp = TrainedMosArtifact(
        location_id=location_id,
        version=existing_version + 1,
        mesoscale_expert=cand_meso,
        synoptic_expert=cand_syn,
        feature_importances=[],
        holdout_metrics={},
    )

    # Evaluate on strict chronological holdout window
    val_preds = predict_with_artifact(cand_tmp, val_sub)
    y_val_t = val_sub["obs_temp_c"].cast(pl.Float64).to_numpy()
    y_val_p = np.nan_to_num(val_sub["obs_precip_1h_mm"].cast(pl.Float64).to_numpy(), nan=0.0)
    y_val_wet = (y_val_p >= 0.25).astype(np.float64)

    q_val = np.array(
        [
            [
                r["temp_p05_c"],
                r["temp_p10_c"],
                r["temp_p25_c"],
                r["temp_p50_c"],
                r["temp_p75_c"],
                r["temp_p90_c"],
                r["temp_p95_c"],
            ]
            for r in val_preds
        ],
        dtype=np.float64,
    )
    pop_val = np.array([r["precip_prob"] for r in val_preds], dtype=np.float64)

    cand_mae_c = float(np.mean(np.abs(y_val_t - q_val[:, 3])))
    cand_pinball = pinball_loss_multi(y_val_t, q_val)
    cand_brier = float(np.mean((pop_val - y_val_wet) ** 2))
    cand_cov80 = float(np.mean((y_val_t >= q_val[:, 1]) & (y_val_t <= q_val[:, 5])))

    # Baseline holdout metrics (NBM & HRRR)
    _, aux_val = build_feature_matrix(val_sub)
    nbm_v = aux_val["nbm_temp_c"]
    nbm_mask = ~np.isnan(nbm_v)
    nbm_mae_c = float(np.mean(np.abs(y_val_t[nbm_mask] - nbm_v[nbm_mask]))) if np.any(nbm_mask) else cand_mae_c

    hrrr_v = aux_val["hrrr_temp_c"]
    hrrr_mask = ~np.isnan(hrrr_v)
    hrrr_mae_c = float(np.mean(np.abs(y_val_t[hrrr_mask] - hrrr_v[hrrr_mask]))) if np.any(hrrr_mask) else cand_mae_c

    nbm_pop_v = (
        np.nan_to_num(val_sub["nbm_precip_prob"].cast(pl.Float64).to_numpy() / 100.0, nan=0.1)
        if "nbm_precip_prob" in val_sub.columns
        else np.full(len(y_val_wet), 0.1)
    )
    nbm_brier = float(np.mean((nbm_pop_v - y_val_wet) ** 2))

    # Champion comparison if an active production artifact already exists
    champ_mae_c: float | None = None
    if existing_artifact_path and Path(existing_artifact_path).exists():
        try:
            champ_art: TrainedMosArtifact = joblib.load(existing_artifact_path)
            champ_preds = predict_with_artifact(champ_art, val_sub)
            champ_p50 = np.array([r["temp_p50_c"] for r in champ_preds], dtype=np.float64)
            champ_mae_c = float(np.mean(np.abs(y_val_t - champ_p50)))
        except Exception:
            champ_mae_c = None

    # Gate check: reject if candidate degrades noticeably (> 0.25°C worse than existing Champion)
    if champ_mae_c is not None and cand_mae_c > champ_mae_c + 0.25:
        return None, {
            "promoted": False,
            "reason": f"Challenger MAE ({cand_mae_c:.2f}°C) trailed Champion ({champ_mae_c:.2f}°C)",
            "candidate_mae_c": round(cand_mae_c, 3),
            "champion_mae_c": round(champ_mae_c, 3),
        }

    # Refit final production artifact on 100% of verified data
    full_meso, full_syn = _fit_both_experts(df)

    # Extract normalized top feature importances across CatBoost + LightGBM
    cb_imp = np.asarray(full_meso.cat_temp_quantiles.get_feature_importance(), dtype=np.float64)
    lgb_imp = np.asarray(full_meso.lgb_temp_median.feature_importances_, dtype=np.float64)
    cb_norm = cb_imp / max(np.sum(cb_imp), 1e-9)
    lgb_norm = lgb_imp / max(np.sum(lgb_imp), 1e-9)
    blended_imp = 0.6 * cb_norm + 0.4 * lgb_norm
    top_idx = np.argsort(blended_imp)[::-1][:12]
    importances = [
        {"feature": FEATURE_COLUMNS[int(idx)], "importance": round(float(blended_imp[int(idx)]), 4)}
        for idx in top_idx
    ]

    metrics = {
        "promoted": True,
        "train_rows": n,
        "holdout_rows": val_sub.height,
        "mesoscale_mae_c": round(cand_mae_c, 3),
        "mesoscale_mae_f": round(cand_mae_c * 1.8, 3),
        "nbm_mae_c": round(nbm_mae_c, 3),
        "nbm_mae_f": round(nbm_mae_c * 1.8, 3),
        "hrrr_mae_c": round(hrrr_mae_c, 3),
        "hrrr_mae_f": round(hrrr_mae_c * 1.8, 3),
        "skill_gain_vs_nbm_pct": round(
            100.0 * (nbm_mae_c - cand_mae_c) / max(nbm_mae_c, 0.1), 1
        ),
        "pinball_crps_c": round(cand_pinball, 4),
        "mesoscale_brier": round(cand_brier, 4),
        "nbm_brier": round(nbm_brier, 4),
        "brier_skill_vs_nbm": round(
            1.0 - (cand_brier / max(nbm_brier, 1e-4)), 3
        ),
        "empirical_80pct_coverage": round(cand_cov80, 3),
        "feature_importances": importances,
    }

    new_ver = existing_version + 1
    models_dir.mkdir(parents=True, exist_ok=True)
    out_path = models_dir / f"location_{location_id}_v{new_ver}.joblib"
    final_artifact = TrainedMosArtifact(
        location_id=location_id,
        version=new_ver,
        mesoscale_expert=full_meso,
        synoptic_expert=full_syn,
        feature_importances=importances,
        holdout_metrics=metrics,
    )
    joblib.dump(final_artifact, out_path)
    return final_artifact, {**metrics, "artifact_path": str(out_path)}
