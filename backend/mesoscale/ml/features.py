"""Leakage-free meteorological regime feature engineering in Polars & NumPy."""

from __future__ import annotations

import math

import numpy as np
import polars as pl

from mesoscale.ml.thermodynamics import (
    equivalent_potential_temperature_k,
    lcl_height_m,
    wet_bulb_celsius,
)

FEATURE_COLUMNS: tuple[str, ...] = (
    "lead_h",
    "sin_hour",
    "cos_hour",
    "sin_doy",
    "cos_doy",
    "hrrr_temp_c",
    "rrfs_temp_c",
    "nbm_temp_c",
    "ecmwf_ifs_temp_c",
    "ecmwf_aifs_temp_c",
    "gfs_temp_c",
    "ens_mean_temp_c",
    "ens_std_temp_c",
    "ens_spread_temp_c",
    "hrrr_minus_nbm",
    "hrrr_minus_ecmwf",
    "ifs_minus_aifs",
    "hrrr_3run_temp_trend",
    "ecmwf_2run_temp_trend",
    "best_dewpoint_c",
    "dewpoint_depression_c",
    "wet_bulb_c",
    "theta_e_k",
    "lcl_m",
    "ring_precip_max_mm",
    "ring_temp_grad_c",
    "ring_theta_e_grad_k",
    "ring_gust_max_ms",
    "best_wind_ms",
    "best_gust_ms",
    "u_wind",
    "v_wind",
    "cloud_pct",
    "pressure_hpa",
    "pressure_tendency_3h",
    "solar_wm2",
    "cape_jkg",
    "cin_jkg",
    "hrrr_precip_mm",
    "nbm_precip_mm",
    "ecmwf_ifs_precip_mm",
    "gfs_precip_mm",
    "nbm_precip_prob",
    "hrrr_precip_prob",
)


def pivot_inference_grid_to_wide(
    long_df: pl.DataFrame, issue_time_utc_epoch_s: float
) -> pl.DataFrame:
    """
    Pivot TelemetryStore.query_inference_grid() long-form rows (valid_time_utc, model_id)
    into the exact wide-form column layout produced by TelemetryStore.build_training_pairs().
    """
    if long_df.is_empty():
        return pl.DataFrame()

    rows_by_valid: dict[object, dict[str, object]] = {}
    for r in long_df.to_dicts():
        vt = r["valid_time_utc"]
        mid = str(r["model_id"])
        rec = rows_by_valid.setdefault(
            vt,
            {
                "valid_time_utc": vt,
                "target_lead_h": max(
                    0.25, (vt.timestamp() - issue_time_utc_epoch_s) / 3600.0
                ),
                "hrrr_3run_temp_trend": r.get("hrrr_3run_temp_trend"),
                "ecmwf_2run_temp_trend": r.get("ecmwf_2run_temp_trend"),
            },
        )
        fields = (
            "temp_c",
            "dewpoint_c",
            "wind_ms",
            "gust_ms",
            "wind_dir_deg",
            "precip_mm",
            "snowfall_cm",
            "precip_prob",
            "cloud_pct",
            "pressure_hpa",
            "solar_wm2",
            "cape_jkg",
            "cin_jkg",
            "freezing_level_m",
            "ring_precip_max_mm",
            "ring_temp_grad_c",
            "ring_theta_e_grad_k",
            "ring_gust_max_ms",
        )
        for f in fields:
            rec[f"{mid}_{f}"] = r.get(f)

    return pl.DataFrame(list(rows_by_valid.values())).sort("valid_time_utc")


def _col_np(df: pl.DataFrame, name: str, n: int) -> np.ndarray:
    if name not in df.columns:
        return np.full(n, np.nan, dtype=np.float64)
    return df[name].cast(pl.Float64).to_numpy()


def _coalesce_np(*arrays: np.ndarray, default: float = np.nan) -> np.ndarray:
    if not arrays:
        return np.array([], dtype=np.float64)
    out = np.full_like(arrays[0], np.nan, dtype=np.float64)
    for arr in arrays:
        mask = np.isnan(out) & ~np.isnan(arr)
        out[mask] = arr[mask]
    if not math.isnan(default):
        out = np.where(np.isnan(out), default, out)
    return out


def build_feature_matrix(wide_df: pl.DataFrame) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """
    Transform a wide multi-model Polars DataFrame into:
    1) `X`: float64 matrix of shape (N, len(FEATURE_COLUMNS)) preserving NaNs for tree splits
    2) `aux`: dictionary of derived physical arrays (wet_bulb_c, theta_e_k, freezing_level_m, etc.)
    """
    n = wide_df.height
    if n == 0:
        return np.empty((0, len(FEATURE_COLUMNS)), dtype=np.float64), {}

    lead_h = _col_np(wide_df, "target_lead_h", n)
    vt_series = wide_df["valid_time_utc"]
    hours = vt_series.dt.hour().to_numpy().astype(np.float64)
    doys = vt_series.dt.ordinal_day().to_numpy().astype(np.float64)

    sin_hour = np.sin(2.0 * np.pi * hours / 24.0)
    cos_hour = np.cos(2.0 * np.pi * hours / 24.0)
    sin_doy = np.sin(2.0 * np.pi * doys / 365.25)
    cos_doy = np.cos(2.0 * np.pi * doys / 365.25)

    hrrr_t = _col_np(wide_df, "hrrr_temp_c", n)
    rrfs_t = _col_np(wide_df, "rrfs_temp_c", n)
    nbm_t = _col_np(wide_df, "nbm_temp_c", n)
    ifs_t = _col_np(wide_df, "ecmwf_ifs_temp_c", n)
    aifs_t = _col_np(wide_df, "ecmwf_aifs_temp_c", n)
    gfs_t = _col_np(wide_df, "gfs_temp_c", n)

    temp_stack = np.vstack([hrrr_t, rrfs_t, nbm_t, ifs_t, aifs_t, gfs_t])
    with np.errstate(all="ignore"):
        ens_mean_t = np.nanmean(temp_stack, axis=0)
        ens_std_t = np.nanstd(temp_stack, axis=0)
        ens_spread_t = np.nanmax(temp_stack, axis=0) - np.nanmin(temp_stack, axis=0)
    ens_std_t = np.nan_to_num(ens_std_t, nan=0.8)
    ens_spread_t = np.nan_to_num(ens_spread_t, nan=1.5)

    hrrr_minus_nbm = hrrr_t - nbm_t
    hrrr_minus_ecmwf = hrrr_t - ifs_t
    ifs_minus_aifs = ifs_t - aifs_t

    hrrr_3run_trend = np.nan_to_num(_col_np(wide_df, "hrrr_3run_temp_trend", n), nan=0.0)
    ecmwf_2run_trend = np.nan_to_num(_col_np(wide_df, "ecmwf_2run_temp_trend", n), nan=0.0)

    best_td = _coalesce_np(
        _col_np(wide_df, "nbm_dewpoint_c", n),
        _col_np(wide_df, "hrrr_dewpoint_c", n),
        _col_np(wide_df, "rrfs_dewpoint_c", n),
        _col_np(wide_df, "ecmwf_ifs_dewpoint_c", n),
        _col_np(wide_df, "gfs_dewpoint_c", n),
        ens_mean_t - 5.0,
    )
    best_td = np.minimum(best_td, ens_mean_t)
    dp_dep = np.maximum(0.0, ens_mean_t - best_td)

    pressure = _coalesce_np(
        _col_np(wide_df, "hrrr_pressure_hpa", n),
        _col_np(wide_df, "ecmwf_ifs_pressure_hpa", n),
        default=975.0,
    )
    pressure_tend_3h = np.zeros(n, dtype=np.float64)
    if n > 3:
        pressure_tend_3h[3:] = pressure[3:] - pressure[:-3]

    tw_c = wet_bulb_celsius(ens_mean_t, dewpoint_c=best_td)
    theta_e = equivalent_potential_temperature_k(ens_mean_t, best_td, pressure)
    lcl = lcl_height_m(ens_mean_t, best_td)

    ring_p_max = np.nanmax(
        np.vstack(
            [
                np.nan_to_num(_col_np(wide_df, "hrrr_ring_precip_max_mm", n), nan=0.0),
                np.nan_to_num(_col_np(wide_df, "rrfs_ring_precip_max_mm", n), nan=0.0),
                np.nan_to_num(_col_np(wide_df, "ecmwf_ifs_ring_precip_max_mm", n), nan=0.0),
                np.nan_to_num(_col_np(wide_df, "gfs_ring_precip_max_mm", n), nan=0.0),
            ]
        ),
        axis=0,
    )
    ring_t_grad = _coalesce_np(
        _col_np(wide_df, "hrrr_ring_temp_grad_c", n),
        _col_np(wide_df, "ecmwf_ifs_ring_temp_grad_c", n),
        default=0.0,
    )
    ring_the_grad = _coalesce_np(
        _col_np(wide_df, "hrrr_ring_theta_e_grad_k", n),
        _col_np(wide_df, "ecmwf_ifs_ring_theta_e_grad_k", n),
        default=0.0,
    )
    ring_gust_max = _coalesce_np(
        _col_np(wide_df, "hrrr_ring_gust_max_ms", n),
        _col_np(wide_df, "ecmwf_ifs_gust_ms", n),
        default=0.0,
    )

    wind_stack = np.vstack(
        [
            _col_np(wide_df, "hrrr_wind_ms", n),
            _col_np(wide_df, "rrfs_wind_ms", n),
            _col_np(wide_df, "nbm_wind_ms", n),
            _col_np(wide_df, "ecmwf_ifs_wind_ms", n),
            _col_np(wide_df, "gfs_wind_ms", n),
        ]
    )
    gust_stack = np.vstack(
        [
            _col_np(wide_df, "hrrr_gust_ms", n),
            _col_np(wide_df, "rrfs_gust_ms", n),
            _col_np(wide_df, "nbm_gust_ms", n),
            _col_np(wide_df, "ecmwf_ifs_gust_ms", n),
            _col_np(wide_df, "gfs_gust_ms", n),
        ]
    )
    with np.errstate(all="ignore"):
        best_wind = np.nan_to_num(np.nanmean(wind_stack, axis=0), nan=3.0)
        best_gust = np.nan_to_num(np.nanmean(gust_stack, axis=0), nan=5.0)

    wdir = _coalesce_np(
        _col_np(wide_df, "hrrr_wind_dir_deg", n),
        _col_np(wide_df, "nbm_wind_dir_deg", n),
        _col_np(wide_df, "ecmwf_ifs_wind_dir_deg", n),
        default=180.0,
    )
    wrad = np.radians(wdir)
    u_wind = -best_wind * np.sin(wrad)
    v_wind = -best_wind * np.cos(wrad)

    cloud_pct = _coalesce_np(
        _col_np(wide_df, "hrrr_cloud_pct", n),
        _col_np(wide_df, "nbm_cloud_pct", n),
        _col_np(wide_df, "ecmwf_ifs_cloud_pct", n),
        default=30.0,
    )
    solar_wm2 = _coalesce_np(
        _col_np(wide_df, "hrrr_solar_wm2", n),
        _col_np(wide_df, "ecmwf_ifs_solar_wm2", n),
        default=0.0,
    )
    cape = _coalesce_np(
        _col_np(wide_df, "hrrr_cape_jkg", n),
        _col_np(wide_df, "gfs_cape_jkg", n),
        default=0.0,
    )
    cin = _coalesce_np(
        _col_np(wide_df, "hrrr_cin_jkg", n),
        _col_np(wide_df, "gfs_cin_jkg", n),
        default=0.0,
    )
    fzl = _coalesce_np(
        _col_np(wide_df, "hrrr_freezing_level_m", n),
        _col_np(wide_df, "gfs_freezing_level_m", n),
        default=np.nan,
    )
    snow_cm = _coalesce_np(
        _col_np(wide_df, "hrrr_snowfall_cm", n),
        default=0.0,
    )

    hrrr_p = _col_np(wide_df, "hrrr_precip_mm", n)
    nbm_p = _col_np(wide_df, "nbm_precip_mm", n)
    ifs_p = _col_np(wide_df, "ecmwf_ifs_precip_mm", n)
    gfs_p = _col_np(wide_df, "gfs_precip_mm", n)
    nbm_pop = _col_np(wide_df, "nbm_precip_prob", n)
    hrrr_pop = _col_np(wide_df, "hrrr_precip_prob", n)

    cols = [
        lead_h,
        sin_hour,
        cos_hour,
        sin_doy,
        cos_doy,
        hrrr_t,
        rrfs_t,
        nbm_t,
        ifs_t,
        aifs_t,
        gfs_t,
        ens_mean_t,
        ens_std_t,
        ens_spread_t,
        hrrr_minus_nbm,
        hrrr_minus_ecmwf,
        ifs_minus_aifs,
        hrrr_3run_trend,
        ecmwf_2run_trend,
        best_td,
        dp_dep,
        tw_c,
        theta_e,
        lcl,
        ring_p_max,
        ring_t_grad,
        ring_the_grad,
        ring_gust_max,
        best_wind,
        best_gust,
        u_wind,
        v_wind,
        cloud_pct,
        pressure,
        pressure_tend_3h,
        solar_wm2,
        cape,
        cin,
        hrrr_p,
        nbm_p,
        ifs_p,
        gfs_p,
        nbm_pop,
        hrrr_pop,
    ]
    X = np.column_stack(cols).astype(np.float64)
    aux = {
        "lead_h": lead_h,
        "ens_mean_temp_c": ens_mean_t,
        "ens_std_temp_c": ens_std_t,
        "best_dewpoint_c": best_td,
        "wet_bulb_c": tw_c,
        "theta_e_k": theta_e,
        "best_wind_ms": best_wind,
        "best_gust_ms": best_gust,
        "wind_dir_deg": wdir,
        "pressure_hpa": pressure,
        "cloud_pct": cloud_pct,
        "cape_jkg": cape,
        "cin_jkg": cin,
        "freezing_level_m": fzl,
        "snowfall_cm": snow_cm,
        "ring_precip_max_mm": ring_p_max,
        "ring_temp_grad_c": ring_t_grad,
        "ring_theta_e_grad_k": ring_the_grad,
        "hrrr_temp_c": hrrr_t,
        "rrfs_temp_c": rrfs_t,
        "nbm_temp_c": nbm_t,
        "ecmwf_ifs_temp_c": ifs_t,
        "ecmwf_aifs_temp_c": aifs_t,
        "gfs_temp_c": gfs_t,
        "hrrr_3run_temp_trend": hrrr_3run_trend,
        "ecmwf_2run_temp_trend": ecmwf_2run_trend,
    }
    return X, aux
