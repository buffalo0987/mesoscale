"""Meteorological Bust-Risk, Convective Cap vs. Initiation, and Ice-Storm diagnostics."""

from __future__ import annotations

import math
from typing import Any


def evaluate_hourly_diagnostics(
    *,
    lead_h: float,
    temp_p10_c: float,
    temp_p50_c: float,
    temp_p90_c: float,
    wet_bulb_c: float,
    precip_prob: float,
    precip_type: str,
    hrrr_temp_c: float | None,
    ecmwf_temp_c: float | None,
    ring_precip_max_mm: float,
    point_precip_mm: float,
    ring_theta_e_grad_k: float,
    cape_jkg: float,
    cin_jkg: float,
) -> dict[str, Any]:
    """
    Evaluate actionable meteorological regime & bust risks for a single forecast hour.
    Returns bust_risk_level ('none' | 'moderate' | 'high'), bust_summary, cap_status, and flags.
    """
    spread_c = temp_p90_c - temp_p10_c
    meso_syn_diff_c = 0.0
    if (
        hrrr_temp_c is not None
        and ecmwf_temp_c is not None
        and not math.isnan(hrrr_temp_c)
        and not math.isnan(ecmwf_temp_c)
    ):
        meso_syn_diff_c = abs(hrrr_temp_c - ecmwf_temp_c)

    reasons: list[str] = []
    risk_level = "none"

    # 1. Bimodal Frontal / Dryline Timing Bust Risk
    if lead_h <= 72.0 and (spread_c >= 6.5 or meso_syn_diff_c >= 5.5):
        risk_level = "high"
        if meso_syn_diff_c >= 5.0 and hrrr_temp_c is not None and ecmwf_temp_c is not None:
            hf = round(hrrr_temp_c * 9.0 / 5.0 + 32.0)
            ef = round(ecmwf_temp_c * 9.0 / 5.0 + 32.0)
            reasons.append(f"Bimodal frontal timing spread (HRRR {hf}°F vs ECMWF {ef}°F)")
        else:
            sf = round(spread_c * 9.0 / 5.0)
            reasons.append(f"Wide {sf}°F P10–P90 temperature envelope")
    elif lead_h <= 72.0 and (spread_c >= 4.8 or meso_syn_diff_c >= 4.0 or ring_theta_e_grad_k >= 12.0):
        risk_level = "moderate"
        reasons.append("Sharp airmass boundary nearby (frontal/dryline gradient)")

    # 2. 8 km Spatial Ring Convective Proximity Alert
    convective_proximity = ring_precip_max_mm >= 4.0 and point_precip_mm < 0.8
    if convective_proximity:
        if risk_level == "none":
            risk_level = "moderate"
        in_ring = round(ring_precip_max_mm / 25.4, 2)
        reasons.append(f"Heavy cell modeled within 8 km ({in_ring}\" max nearby)")

    # 3. Freezing Rain / Ice Glaze Alert
    freezing_rain_risk = precip_type == "freezing_rain" and precip_prob >= 0.25
    if freezing_rain_risk:
        risk_level = "high"
        tw_f = round(wet_bulb_c * 9.0 / 5.0 + 32.0, 1)
        reasons.append(f"Freezing rain glaze risk (Wet-Bulb {tw_f}°F)")

    # 4. Cap Strength vs. Convective Initiation (Oklahoma / Plains specialty)
    cap_status = "none"
    if cape_jkg >= 500.0:
        # Note: Open-Meteo CIN is typically negative (e.g. -120 J/kg) or magnitude
        cin_mag = abs(cin_jkg)
        if cin_mag >= 100.0:
            cap_status = "strong_cap"
        elif cin_mag >= 35.0:
            cap_status = "breakable_cap"
        else:
            cap_status = "uncapped_initiation"
            if cape_jkg >= 1500.0:
                reasons.append(f"Weak cap over {round(cape_jkg)} J/kg CAPE — storm initiation favored")

    return {
        "bust_risk_level": risk_level,
        "bust_summary": " • ".join(reasons),
        "convective_proximity": convective_proximity,
        "freezing_rain_risk": freezing_rain_risk,
        "cap_status": cap_status,
    }


def summarize_storm_winter_mode(
    hourly_rows: list[dict[str, Any]], spc_summary: dict[str, Any], active_alerts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Determine whether context-aware Storm & Winter Mode should auto-expand and summarize peak signals."""
    next_48 = hourly_rows[:48]
    max_cape = max((float(r.get("cape_jkg") or 0.0) for r in next_48), default=0.0)
    min_cin_at_peak = 0.0
    for r in next_48:
        if float(r.get("cape_jkg") or 0.0) == max_cape and max_cape > 0:
            min_cin_at_peak = float(r.get("cin_jkg") or 0.0)
            break

    winter_hours = [
        r for r in next_48 if r.get("precip_type") in ("freezing_rain", "sleet", "snow") and float(r.get("precip_prob") or 0.0) >= 0.20
    ]
    freezing_rain_hours = [r for r in winter_hours if r.get("precip_type") == "freezing_rain"]
    bust_hours = [r for r in next_48 if r.get("bust_risk_level") == "high"]

    auto_active = bool(
        spc_summary.get("has_severe_risk")
        or max_cape >= 600.0
        or len(winter_hours) > 0
        or len(bust_hours) > 0
        or len(active_alerts) > 0
    )

    return {
        "auto_active": auto_active,
        "spc": spc_summary,
        "peak_cape_jkg": round(max_cape, 0),
        "cin_at_peak_cape_jkg": round(min_cin_at_peak, 0),
        "winter_precip_hours_48h": len(winter_hours),
        "freezing_rain_hours_48h": len(freezing_rain_hours),
        "high_bust_hours_48h": len(bust_hours),
    }
