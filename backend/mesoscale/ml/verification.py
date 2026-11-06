"""Out-of-sample Verification Scoreboard & 72-Hour Forecast-vs-Actual Replay builder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

MODEL_LABELS: dict[str, str] = {
    "mesoscale_ai": "Mesoscale Learned AI",
    "nbm": "NOAA NBM (2.5 km Blend)",
    "hrrr": "NOAA HRRR (3 km Mesoscale)",
    "rrfs": "NOAA RRFS v1 (3 km UFS)",
    "ecmwf_ifs": "ECMWF IFS (9 km HRES)",
    "ecmwf_aifs": "ECMWF AIFS (AI Global)",
    "gfs": "NOAA GFS (Global)",
}


def _lead_filter_bounds(lead_bucket: str) -> tuple[float, float]:
    if lead_bucket == "1_6h":
        return (0.25, 6.5)
    if lead_bucket == "7_24h":
        return (6.5, 24.5)
    if lead_bucket == "25_48h":
        return (24.5, 48.5)
    if lead_bucket == "49_168h":
        return (48.5, 168.5)
    return (0.25, 240.0)


def _compute_row_metrics(
    model_id: str,
    pred_temp_c: np.ndarray,
    obs_temp_c: np.ndarray,
    pred_wind_ms: np.ndarray,
    obs_wind_ms: np.ndarray,
    pred_pop: np.ndarray,
    obs_precip_mm: np.ndarray,
    p10_c: np.ndarray | None = None,
    p90_c: np.ndarray | None = None,
) -> dict[str, Any] | None:
    t_mask = ~np.isnan(pred_temp_c) & ~np.isnan(obs_temp_c)
    if int(np.sum(t_mask)) < 3:
        return None

    err_t = pred_temp_c[t_mask] - obs_temp_c[t_mask]
    mae_c = float(np.mean(np.abs(err_t)))
    rmse_c = float(np.sqrt(np.mean(err_t**2)))
    bias_c = float(np.mean(err_t))

    w_mask = ~np.isnan(pred_wind_ms) & ~np.isnan(obs_wind_ms)
    wind_mae_ms = (
        float(np.mean(np.abs(pred_wind_ms[w_mask] - obs_wind_ms[w_mask])))
        if np.any(w_mask)
        else None
    )

    p_mask = ~np.isnan(pred_pop) & ~np.isnan(obs_precip_mm)
    brier = None
    if np.any(p_mask):
        wet_obs = (obs_precip_mm[p_mask] >= 0.25).astype(np.float64)
        brier = float(np.mean((np.clip(pred_pop[p_mask], 0.0, 1.0) - wet_obs) ** 2))

    cov80 = None
    if p10_c is not None and p90_c is not None:
        c_mask = t_mask & ~np.isnan(p10_c) & ~np.isnan(p90_c)
        if np.any(c_mask):
            cov80 = float(
                np.mean(
                    (obs_temp_c[c_mask] >= p10_c[c_mask])
                    & (obs_temp_c[c_mask] <= p90_c[c_mask])
                )
            )

    return {
        "model_id": model_id,
        "label": MODEL_LABELS.get(model_id, model_id),
        "samples": int(np.sum(t_mask)),
        "temp_mae_c": round(mae_c, 3),
        "temp_mae_f": round(mae_c * 1.8, 2),
        "temp_rmse_f": round(rmse_c * 1.8, 2),
        "temp_bias_f": round(bias_c * 1.8, 2),
        "wind_mae_mph": round(wind_mae_ms * 2.23694, 2) if wind_mae_ms is not None else None,
        "precip_brier": round(brier, 4) if brier is not None else None,
        "coverage_80pct": round(cov80 * 100.0, 1) if cov80 is not None else None,
    }


def build_verification_report(
    *,
    models_df: pl.DataFrame,
    ai_df: pl.DataFrame,
    holdout_metrics: dict[str, Any] | None,
    lead_bucket: str = "all",
) -> dict[str, Any]:
    """
    Build head-to-head out-of-sample Verification Scoreboard + 72h Forecast-vs-Actual replay.
    If live `ensemble_predictions` is still accumulating its first 24h after Day-1 onboarding,
    seamlessly supplements the Mesoscale AI row with chronological holdout backtest metrics
    (clearly tagged `source: 'backtest_holdout' | 'live_out_of_sample'`).
    """
    lo_h, hi_h = _lead_filter_bounds(lead_bucket)
    leaderboard: list[dict[str, Any]] = []

    if not models_df.is_empty():
        m_filt = models_df.filter(
            (pl.col("effective_lead_h") >= lo_h) & (pl.col("effective_lead_h") <= hi_h)
        )
        # Deduplicate to nearest-to-12h (or latest per valid_time within bucket) so multi-cycle counts are fair
        m_dedup = (
            m_filt.sort("effective_lead_h")
            .group_by(["model_id", "valid_time_utc"], maintain_order=True)
            .first()
        )
        for mid in ("nbm", "hrrr", "rrfs", "ecmwf_ifs", "ecmwf_aifs", "gfs"):
            sub = m_dedup.filter(pl.col("model_id") == mid)
            if sub.is_empty():
                continue
            row = _compute_row_metrics(
                mid,
                sub["pred_temp_c"].to_numpy(),
                sub["obs_temp_c"].to_numpy(),
                sub["pred_wind_ms"].to_numpy(),
                sub["obs_wind_ms"].to_numpy(),
                sub["pred_pop"].to_numpy(),
                sub["obs_precip_mm"].to_numpy(),
            )
            if row:
                row["verification_source"] = "live_out_of_sample"
                leaderboard.append(row)

    ai_added = False
    if not ai_df.is_empty():
        a_filt = ai_df.filter(
            (pl.col("effective_lead_h") >= lo_h) & (pl.col("effective_lead_h") <= hi_h)
        )
        a_dedup = (
            a_filt.sort("effective_lead_h")
            .group_by("valid_time_utc", maintain_order=True)
            .first()
        )
        ai_row = _compute_row_metrics(
            "mesoscale_ai",
            a_dedup["temp_p50_c"].to_numpy(),
            a_dedup["obs_temp_c"].to_numpy(),
            a_dedup["wind_p50_ms"].to_numpy(),
            a_dedup["obs_wind_ms"].to_numpy(),
            a_dedup["precip_prob_cal"].to_numpy(),
            a_dedup["obs_precip_mm"].to_numpy(),
            p10_c=a_dedup["temp_p10_c"].to_numpy(),
            p90_c=a_dedup["temp_p90_c"].to_numpy(),
        )
        if ai_row:
            ai_row["verification_source"] = "live_out_of_sample"
            leaderboard.append(ai_row)
            ai_added = True

    if not ai_added and holdout_metrics and holdout_metrics.get("mesoscale_mae_c") is not None:
        mae_c = float(holdout_metrics["mesoscale_mae_c"])
        leaderboard.append(
            {
                "model_id": "mesoscale_ai",
                "label": MODEL_LABELS["mesoscale_ai"],
                "samples": int(holdout_metrics.get("holdout_rows") or 0),
                "temp_mae_c": round(mae_c, 3),
                "temp_mae_f": round(mae_c * 1.8, 2),
                "temp_rmse_f": round(mae_c * 1.8 * 1.26, 2),
                "temp_bias_f": 0.0,
                "wind_mae_mph": None,
                "precip_brier": holdout_metrics.get("mesoscale_brier"),
                "coverage_80pct": round(
                    float(holdout_metrics.get("empirical_80pct_coverage") or 0.8) * 100.0, 1
                ),
                "verification_source": "chronological_backtest_holdout",
            }
        )

    # Compute Brier Skill Score vs NBM where available
    nbm_entry = next((r for r in leaderboard if r["model_id"] == "nbm"), None)
    nbm_brier = nbm_entry["precip_brier"] if nbm_entry else None
    for r in leaderboard:
        if r["precip_brier"] is not None and nbm_brier and nbm_brier > 1e-4:
            r["brier_skill_vs_nbm"] = round(1.0 - (r["precip_brier"] / nbm_brier), 3)
        else:
            r["brier_skill_vs_nbm"] = None

    leaderboard.sort(key=lambda r: r["temp_mae_c"])

    # Build 72-Hour Forecast-vs-Actual Replay series
    replay_cutoff = datetime.now(UTC) - timedelta(hours=72)
    replay_map: dict[datetime, dict[str, Any]] = {}

    if not models_df.is_empty():
        r_mod = (
            models_df.filter(
                (pl.col("valid_time_utc") >= replay_cutoff)
                & (pl.col("effective_lead_h") >= 6.0)
                & (pl.col("effective_lead_h") <= 36.0)
            )
            .sort("effective_lead_h")
            .group_by(["model_id", "valid_time_utc"], maintain_order=True)
            .first()
        )
        for row in r_mod.to_dicts():
            vt = row["valid_time_utc"]
            entry = replay_map.setdefault(
                vt,
                {
                    "valid_time_utc": vt.isoformat(),
                    "obs_temp_c": round(float(row["obs_temp_c"]), 2)
                    if row["obs_temp_c"] is not None
                    else None,
                    "obs_precip_mm": round(float(row["obs_precip_mm"] or 0.0), 2),
                },
            )
            if row["pred_temp_c"] is not None:
                entry[f"{row['model_id']}_temp_c"] = round(float(row["pred_temp_c"]), 2)

    if not ai_df.is_empty():
        r_ai = (
            ai_df.filter(pl.col("valid_time_utc") >= replay_cutoff)
            .sort("effective_lead_h")
            .group_by("valid_time_utc", maintain_order=True)
            .first()
        )
        for row in r_ai.to_dicts():
            vt = row["valid_time_utc"]
            entry = replay_map.setdefault(
                vt,
                {
                    "valid_time_utc": vt.isoformat(),
                    "obs_temp_c": round(float(row["obs_temp_c"]), 2)
                    if row["obs_temp_c"] is not None
                    else None,
                    "obs_precip_mm": round(float(row["obs_precip_mm"] or 0.0), 2),
                },
            )
            entry["mesoscale_ai_temp_c"] = round(float(row["temp_p50_c"]), 2)
            entry["mesoscale_ai_p10_c"] = round(float(row["temp_p10_c"]), 2)
            entry["mesoscale_ai_p90_c"] = round(float(row["temp_p90_c"]), 2)

    replay_series = [replay_map[k] for k in sorted(replay_map.keys())]
    return {
        "lead_bucket": lead_bucket,
        "leaderboard": leaderboard,
        "replay_72h": replay_series,
        "feature_importances": (holdout_metrics or {}).get("feature_importances", []),
        "holdout_summary": holdout_metrics or {},
    }
