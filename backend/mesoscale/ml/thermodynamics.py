"""Vectorized meteorological thermodynamics (Wet-Bulb, Theta-e, LCL, Feels-Like, Precip-Type)."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArr = npt.NDArray[np.float64]


def relative_humidity_from_temp_dewpoint(
    temp_c: FloatArr | float, dewpoint_c: FloatArr | float
) -> FloatArr:
    """Compute relative humidity (%) in [1, 100] from air temp and dewpoint (°C)."""
    t = np.asarray(temp_c, dtype=np.float64)
    td = np.minimum(np.asarray(dewpoint_c, dtype=np.float64), t)
    es = 6.112 * np.exp((17.67 * t) / (t + 243.5))
    e = 6.112 * np.exp((17.67 * td) / (td + 243.5))
    rh = 100.0 * (e / np.maximum(es, 1e-6))
    return np.clip(rh, 1.0, 100.0)


def dewpoint_from_temp_rh(
    temp_c: FloatArr | float, rel_humidity_pct: FloatArr | float
) -> FloatArr:
    """Compute dewpoint (°C) from temperature (°C) and relative humidity (%)."""
    t = np.asarray(temp_c, dtype=np.float64)
    rh = np.clip(np.asarray(rel_humidity_pct, dtype=np.float64), 1.0, 100.0)
    es = 6.112 * np.exp((17.67 * t) / (t + 243.5))
    e = np.maximum(es * (rh / 100.0), 1e-4)
    ln_ratio = np.log(e / 6.112)
    td = (243.5 * ln_ratio) / (17.67 - ln_ratio)
    return np.minimum(td, t)


def wet_bulb_celsius(
    temp_c: FloatArr | float,
    dewpoint_c: FloatArr | float | None = None,
    rel_humidity_pct: FloatArr | float | None = None,
) -> FloatArr:
    """
    Stull (2011) psychrometric wet-bulb temperature (°C), clamped to [Td, T].
    Crucial for distinguishing cold rain from Oklahoma freezing rain / glazing.
    """
    t = np.asarray(temp_c, dtype=np.float64)
    if rel_humidity_pct is None:
        if dewpoint_c is None:
            return t.copy()
        rh = relative_humidity_from_temp_dewpoint(t, dewpoint_c)
        td = np.minimum(np.asarray(dewpoint_c, dtype=np.float64), t)
    else:
        rh = np.clip(np.asarray(rel_humidity_pct, dtype=np.float64), 1.0, 100.0)
        td = (
            np.minimum(np.asarray(dewpoint_c, dtype=np.float64), t)
            if dewpoint_c is not None
            else dewpoint_from_temp_rh(t, rh)
        )

    tw = (
        t * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
        + np.arctan(t + rh)
        - np.arctan(rh - 1.676331)
        + 0.00391838 * np.power(rh, 1.5) * np.arctan(0.023101 * rh)
        - 4.686035
    )
    return np.clip(tw, td, t)


def equivalent_potential_temperature_k(
    temp_c: FloatArr | float,
    dewpoint_c: FloatArr | float,
    pressure_hpa: FloatArr | float = 975.0,
) -> FloatArr:
    """
    Bolton (1980) eq. (43) Equivalent Potential Temperature theta_e (Kelvin).
    Strongly highlights drylines, outflow boundaries, and shallow Arctic fronts.
    """
    t = np.asarray(temp_c, dtype=np.float64)
    td = np.minimum(np.asarray(dewpoint_c, dtype=np.float64), t)
    p = np.where(
        np.isnan(np.asarray(pressure_hpa, dtype=np.float64)),
        975.0,
        np.asarray(pressure_hpa, dtype=np.float64),
    )
    p = np.clip(p, 500.0, 1060.0)

    tk = t + 273.15
    tdk = np.maximum(td + 273.15, 180.0)

    e = 6.112 * np.exp((17.67 * td) / (td + 243.5))
    e = np.minimum(e, 0.5 * p)
    r = 0.622 * e / np.maximum(p - e, 10.0)  # mixing ratio (kg/kg)

    denom = (1.0 / np.maximum(tdk - 56.0, 1.0)) + (np.log(np.maximum(tk / tdk, 1.0)) / 800.0)
    t_lcl = (1.0 / denom) + 56.0

    theta_dl = tk * np.power(1000.0 / np.maximum(p - e, 10.0), 0.2854 * (1.0 - 0.28 * r))
    exponent = ((3.376 / t_lcl) - 0.00254) * (1000.0 * r) * (1.0 + 0.81 * r)
    return theta_dl * np.exp(np.clip(exponent, -5.0, 5.0))


def lcl_height_m(temp_c: FloatArr | float, dewpoint_c: FloatArr | float) -> FloatArr:
    """Approximate Lifted Condensation Level (cloud base AGL, meters) via Espy's equation."""
    t = np.asarray(temp_c, dtype=np.float64)
    td = np.minimum(np.asarray(dewpoint_c, dtype=np.float64), t)
    return np.maximum(0.0, 125.0 * (t - td))


def apparent_temperature_c(
    temp_c: FloatArr | float,
    dewpoint_c: FloatArr | float,
    wind_ms: FloatArr | float,
) -> FloatArr:
    """NWS Wind Chill + Rothfusz Heat Index apparent temperature (°C)."""
    t = np.asarray(temp_c, dtype=np.float64)
    rh = relative_humidity_from_temp_dewpoint(t, dewpoint_c)
    w_kmh = np.maximum(np.asarray(wind_ms, dtype=np.float64), 0.0) * 3.6
    t_f = t * 9.0 / 5.0 + 32.0

    # Wind chill (T <= 10°C and wind >= 4.8 km/h)
    wc_c = (
        13.12
        + 0.6215 * t
        - 11.37 * np.power(np.maximum(w_kmh, 4.8), 0.16)
        + 0.3965 * t * np.power(np.maximum(w_kmh, 4.8), 0.16)
    )

    # Heat index (T >= 26.7°C and RH >= 40%)
    hi_f = (
        -42.379
        + 2.04901523 * t_f
        + 10.14333127 * rh
        - 0.22475541 * t_f * rh
        - 0.00683783 * t_f * t_f
        - 0.05481717 * rh * rh
        + 0.00122874 * t_f * t_f * rh
        + 0.00085282 * t_f * rh * rh
        - 0.00000199 * t_f * t_f * rh * rh
    )
    hi_c = (hi_f - 32.0) * 5.0 / 9.0

    out = t.copy()
    cold_mask = (t <= 10.0) & (w_kmh >= 4.8)
    hot_mask = (t >= 26.7) & (rh >= 40.0)
    out = np.where(cold_mask, wc_c, out)
    out = np.where(hot_mask, hi_c, out)
    return out


def classify_winter_precip_type(
    temp_c: float,
    wet_bulb_c: float,
    precip_prob: float,
    precip_mm: float = 0.0,
    snowfall_cm: float = 0.0,
    freezing_level_m: float | None = None,
) -> str:
    """
    Classify hourly hydrometeor phase into:
    'none' | 'rain' | 'freezing_rain' | 'sleet' | 'snow'
    """
    if precip_prob < 0.12 and precip_mm < 0.08 and snowfall_cm < 0.03:
        return "none"

    fzl = freezing_level_m if (freezing_level_m is not None and not np.isnan(freezing_level_m)) else None
    snow_water_eq_mm = snowfall_cm * 1.0  # 10:1 nominal water equivalent in mm
    total_liq = max(precip_mm, snow_water_eq_mm, 0.01)
    snow_frac = snow_water_eq_mm / total_liq if snowfall_cm > 0 else 0.0

    # Warm surface wet-bulb -> liquid rain
    if wet_bulb_c > 0.5 and (snowfall_cm < 0.05 or temp_c > 2.8):
        return "rain"

    # Freezing rain (glazing): sub-freezing/marginal wet-bulb at surface, warm melting layer aloft
    has_warm_nose = (fzl is not None and fzl >= 500.0) or (precip_mm >= 0.15 and snow_frac < 0.18)
    if wet_bulb_c <= 0.15 and temp_c >= -4.0 and has_warm_nose:
        if temp_c <= -2.2 and fzl is not None and fzl < 650.0:
            return "sleet"
        return "freezing_rain"

    # Sleet / wintry mix transition zone
    if -1.8 <= wet_bulb_c <= 0.4 and (
        (0.18 <= snow_frac <= 0.65) or (fzl is not None and 280.0 <= fzl < 500.0)
    ):
        return "sleet"

    # Snow: cold column or snow-dominated output
    if wet_bulb_c <= 0.4 and (snowfall_cm >= 0.05 or (fzl is not None and fzl < 350.0) or wet_bulb_c <= -0.6):
        return "snow"

    # Marginal cold rain vs freezing drizzle fallback
    if wet_bulb_c <= -0.1:
        return "freezing_rain"
    return "rain"
