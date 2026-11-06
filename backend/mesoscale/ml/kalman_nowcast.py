"""2-State [Bias, Trend] Linear Kalman Filter + Online Pinball Hedge for 0-6h Nowcasting."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

# Damping timescales (hours) so bias & rate-of-change decay smoothly back to NWP/MOS beyond +6h
TAU_BIAS_HOURS = 3.5
TAU_TREND_HOURS = 1.4
MAX_INNOVATION_C = 12.0


def _transition_matrix(dt_hours: float) -> np.ndarray:
    dt = max(0.01, min(dt_hours, 24.0))
    eb = math.exp(-dt / TAU_BIAS_HOURS)
    er = math.exp(-dt / TAU_TREND_HOURS)
    coupling = TAU_TREND_HOURS * (1.0 - er)
    return np.array([[eb, coupling], [0.0, er]], dtype=np.float64)


def default_kalman_state() -> dict[str, Any]:
    return {
        "x": [0.0, 0.0],  # [bias_c, trend_c_per_hour]
        "P": [[2.0, 0.0], [0.0, 1.5]],
        "wind_bias_ms": 0.0,
        "dewpoint_bias_c": 0.0,
        "updated_at_utc": None,
        "hedge_weights": {
            "hrrr": 0.28,
            "rrfs": 0.20,
            "nbm": 0.24,
            "ecmwf_ifs": 0.14,
            "ecmwf_aifs": 0.08,
            "gfs": 0.06,
        },
    }


def update_kalman_state(
    prev_state: dict[str, Any] | None,
    *,
    obs_time_utc: datetime,
    obs_temp_c: float,
    pred_temp_c: float,
    obs_dewpoint_c: float | None = None,
    pred_dewpoint_c: float | None = None,
    obs_wind_ms: float | None = None,
    pred_wind_ms: float | None = None,
    raw_model_preds_c: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """
    Assimilate a fresh 5-min Oklahoma Mesonet / ASOS observation into the 2-state
    [level_bias, trend_rate] Kalman filter and update Online Hedge model weights.
    """
    state = default_kalman_state()
    if prev_state:
        state.update(prev_state)

    raw_err = float(obs_temp_c - pred_temp_c)
    err = float(np.clip(raw_err, -MAX_INNOVATION_C, MAX_INNOVATION_C))

    x = np.array(state.get("x") or [0.0, 0.0], dtype=np.float64)
    P = np.array(state.get("P") or [[2.0, 0.0], [0.0, 1.5]], dtype=np.float64)

    dt_h = 0.25
    prev_ts_str = state.get("updated_at_utc")
    if prev_ts_str:
        try:
            prev_dt = datetime.fromisoformat(str(prev_ts_str).replace("Z", "+00:00"))
            elapsed = (obs_time_utc - prev_dt).total_seconds() / 3600.0
            if elapsed <= 0.01:
                return state
            dt_h = min(elapsed, 12.0)
        except ValueError:
            dt_h = 0.25

    F = _transition_matrix(dt_h)
    # Process noise scales with elapsed time (allows rapid adaptation during frontal passages)
    Q = np.array([[0.35 * dt_h, 0.0], [0.0, 0.65 * dt_h]], dtype=np.float64)
    R = np.array([[0.30]], dtype=np.float64)  # Research-grade Mesonet/ASOS measurement variance
    H = np.array([[1.0, 0.0]], dtype=np.float64)

    # Predict step
    x_pred = F @ x
    P_pred = F @ P @ F.T + Q

    # Update step
    y_innov = np.array([err]) - (H @ x_pred)
    S = H @ P_pred @ H.T + R
    K = (P_pred @ H.T) / float(S[0, 0])
    x_new = x_pred + (K.flatten() * float(y_innov[0]))
    x_new[0] = float(np.clip(x_new[0], -MAX_INNOVATION_C, MAX_INNOVATION_C))
    x_new[1] = float(np.clip(x_new[1], -6.0, 6.0))
    P_new = (np.eye(2) - K @ H) @ P_pred

    # EWMA nudges for dewpoint & wind speed
    td_bias = float(state.get("dewpoint_bias_c", 0.0))
    if obs_dewpoint_c is not None and pred_dewpoint_c is not None:
        td_err = float(np.clip(obs_dewpoint_c - pred_dewpoint_c, -10.0, 10.0))
        td_bias = 0.65 * td_bias + 0.35 * td_err

    w_bias = float(state.get("wind_bias_ms", 0.0))
    if obs_wind_ms is not None and pred_wind_ms is not None:
        w_err = float(np.clip(obs_wind_ms - pred_wind_ms, -8.0, 8.0))
        w_bias = 0.65 * w_bias + 0.35 * w_err

    # Online Multiplicative Weights (Hedge) update across raw NWP models
    weights = dict(state.get("hedge_weights") or default_kalman_state()["hedge_weights"])
    if raw_model_preds_c:
        eta = 0.06
        for mid, mval in raw_model_preds_c.items():
            if mval is not None and not math.isnan(mval) and mid in weights:
                mae_i = min(abs(obs_temp_c - float(mval)), 10.0)
                weights[mid] = max(0.02, float(weights[mid]) * math.exp(-eta * mae_i))
        w_sum = sum(weights.values()) or 1.0
        weights = {k: round(v / w_sum, 4) for k, v in weights.items()}

    return {
        "x": [round(float(x_new[0]), 4), round(float(x_new[1]), 4)],
        "P": [[round(float(P_new[0, 0]), 4), round(float(P_new[0, 1]), 4)],
              [round(float(P_new[1, 0]), 4), round(float(P_new[1, 1]), 4)]],
        "dewpoint_bias_c": round(td_bias, 4),
        "wind_bias_ms": round(w_bias, 4),
        "updated_at_utc": obs_time_utc.astimezone(UTC).isoformat(),
        "hedge_weights": weights,
    }


def project_kalman_offsets(
    kalman_state: dict[str, Any] | None,
    now_utc: datetime,
    valid_times_utc: list[datetime],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Project damped [bias, trend] temperature corrections + dewpoint & wind nudges
    across future valid_times_utc (smoothly decaying to 0 beyond ~6-8 hours).
    """
    n = len(valid_times_utc)
    zeros = np.zeros(n, dtype=np.float64)
    if not kalman_state or not kalman_state.get("updated_at_utc"):
        return zeros, zeros.copy(), zeros.copy()

    try:
        updated_dt = datetime.fromisoformat(
            str(kalman_state["updated_at_utc"]).replace("Z", "+00:00")
        )
    except ValueError:
        return zeros, zeros.copy(), zeros.copy()

    age_h = (now_utc - updated_dt).total_seconds() / 3600.0
    if age_h > 6.0:
        return zeros, zeros.copy(), zeros.copy()

    x = np.array(kalman_state.get("x") or [0.0, 0.0], dtype=np.float64)
    td_b0 = float(kalman_state.get("dewpoint_bias_c", 0.0))
    w_b0 = float(kalman_state.get("wind_bias_ms", 0.0))

    temp_nudge = np.zeros(n, dtype=np.float64)
    td_nudge = np.zeros(n, dtype=np.float64)
    wind_nudge = np.zeros(n, dtype=np.float64)

    for i, vt in enumerate(valid_times_utc):
        dt_from_obs = (vt - updated_dt).total_seconds() / 3600.0
        if dt_from_obs < -0.5 or dt_from_obs > 10.0:
            continue
        dt_pos = max(0.05, dt_from_obs)
        F = _transition_matrix(dt_pos)
        proj = F @ x
        temp_nudge[i] = float(np.clip(proj[0], -MAX_INNOVATION_C, MAX_INNOVATION_C))
        decay = math.exp(-dt_pos / 3.0)
        td_nudge[i] = td_b0 * decay
        wind_nudge[i] = w_b0 * math.exp(-dt_pos / 2.5)

    return temp_nudge, td_nudge, wind_nudge
