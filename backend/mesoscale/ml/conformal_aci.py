"""Online Adaptive Conformal Inference (Gibbs & Candes) for calibrated 80% intervals."""

from __future__ import annotations

from typing import Any

import numpy as np

LEAD_BUCKETS = ("1_6h", "7_24h", "25_48h", "49_240h")
TARGET_ALPHA = 0.20  # 80% empirical coverage for [P10, P90]


def lead_to_bucket(lead_h: float) -> str:
    if lead_h <= 6.0:
        return "1_6h"
    if lead_h <= 24.0:
        return "7_24h"
    if lead_h <= 48.0:
        return "25_48h"
    return "49_240h"


def default_aci_state() -> dict[str, Any]:
    return {
        b: {
            "alpha_t": TARGET_ALPHA,
            "scale": 1.0,
            "empirical_coverage": 0.80,
            "n_updates": 0,
        }
        for b in LEAD_BUCKETS
    }


def update_aci_state_with_residuals(
    aci_state: dict[str, Any] | None,
    *,
    lead_hours: np.ndarray,
    p10: np.ndarray,
    p90: np.ndarray,
    obs: np.ndarray,
    gamma: float = 0.025,
) -> dict[str, Any]:
    """
    Online Gibbs & Candes ACI step:
      err_t = 1 if obs_t not in [p10_t, p90_t] else 0
      alpha_{t+1} = alpha_t + gamma * (TARGET_ALPHA - err_t)
    When miscoverage exceeds 20% (err_t = 1), alpha_t decreases -> interval scale widens!
    When coverage is too wide (err_t = 0), alpha_t increases -> interval scale sharpens!
    """
    state = default_aci_state()
    if aci_state:
        for b in LEAD_BUCKETS:
            if b in aci_state:
                state[b].update(aci_state[b])

    for lh, lo, hi, y in zip(lead_hours, p10, p90, obs, strict=False):
        if np.isnan(y) or np.isnan(lo) or np.isnan(hi):
            continue
        b = lead_to_bucket(float(lh))
        rec = state[b]
        err = 0.0 if (lo <= y <= hi) else 1.0
        alpha_next = float(np.clip(rec["alpha_t"] + gamma * (TARGET_ALPHA - err), 0.05, 0.40))
        # Map effective alpha_t relative to nominal 0.20 into multiplicative half-width factor
        scale = float(np.clip((TARGET_ALPHA / alpha_next) ** 0.65, 0.70, 2.25))
        cov_prev = float(rec.get("empirical_coverage", 0.80))
        cov_next = 0.96 * cov_prev + 0.04 * (1.0 - err)
        rec["alpha_t"] = round(alpha_next, 5)
        rec["scale"] = round(scale, 4)
        rec["empirical_coverage"] = round(cov_next, 4)
        rec["n_updates"] = int(rec.get("n_updates", 0)) + 1

    return state


def apply_aci_scaling(
    quantiles_matrix: np.ndarray,
    lead_hours: np.ndarray,
    aci_state: dict[str, Any] | None,
) -> np.ndarray:
    """
    Scale quantile spreads around P50 (column index 3 of [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95])
    using the active Adaptive Conformal scale factor per lead-time bucket.
    """
    if not aci_state or quantiles_matrix.size == 0:
        return quantiles_matrix
    out = quantiles_matrix.copy()
    p50 = out[:, 3:4]
    scales = np.ones(( len(lead_hours), 1 ), dtype=np.float64)
    for i, lh in enumerate(lead_hours):
        b = lead_to_bucket(float(lh))
        s = float((aci_state.get(b) or {}).get("scale", 1.0))
        scales[i, 0] = np.clip(s, 0.70, 2.25)
    out = p50 + (out - p50) * scales
    return np.maximum.accumulate(out, axis=1)
