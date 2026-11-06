"""Open-Meteo multi-model NWP live forecast, 15m HRRR nowcast, and 365-day historical backfill."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import polars as pl

from mesoscale.db.telemetry_store import MODEL_AVAILABILITY_LAG_MINUTES
from mesoscale.providers.spatial_ring import compute_ring_features, compute_spatial_ring

# Candidate Open-Meteo model slugs in priority order per internal model_id
MODEL_SLUG_CANDIDATES: dict[str, tuple[str, ...]] = {
    "hrrr": ("ncep_hrrr_conus", "gfs_hrrr"),
    "rrfs": ("ncep_rrfs_conus", "ncep_nam_conus", "gfs_graphcast025"),
    "nbm": ("ncep_nbm_conus",),
    "ecmwf_ifs": ("ecmwf_ifs", "ecmwf_ifs025"),
    "ecmwf_aifs": ("ecmwf_aifs025_single", "ecmwf_aifs025"),
    "gfs": ("gfs_seamless", "gfs_global"),
}

# Models queried across all 5 points of the 8 km spatial ring (convective & boundary resolving)
RING_ENABLED_MODELS = {"hrrr", "rrfs", "nbm", "ecmwf_ifs"}

HOURLY_VARS = (
    "temperature_2m,"
    "dew_point_2m,"
    "wind_speed_10m,"
    "wind_gusts_10m,"
    "wind_direction_10m,"
    "precipitation,"
    "snowfall,"
    "precipitation_probability,"
    "cloud_cover,"
    "surface_pressure,"
    "shortwave_radiation,"
    "cape,"
    "convective_inhibition,"
    "freezing_level_height"
)

MINUTELY_15_VARS = (
    "temperature_2m,"
    "dew_point_2m,"
    "wind_speed_10m,"
    "wind_gusts_10m,"
    "wind_direction_10m,"
    "precipitation,"
    "snowfall,"
    "shortwave_radiation,"
    "cape"
)

SNAPSHOT_SCHEMA = {
    "location_id": pl.Int64,
    "model_id": pl.String,
    "init_time_utc": pl.Datetime("us", "UTC"),
    "available_at_utc": pl.Datetime("us", "UTC"),
    "valid_time_utc": pl.Datetime("us", "UTC"),
    "lead_hours": pl.Float64,
    "is_subhourly_15m": pl.Boolean,
    "temp_c": pl.Float64,
    "dewpoint_c": pl.Float64,
    "wind_ms": pl.Float64,
    "gust_ms": pl.Float64,
    "wind_dir_deg": pl.Float64,
    "precip_mm": pl.Float64,
    "snowfall_cm": pl.Float64,
    "precip_prob": pl.Float64,
    "cloud_pct": pl.Float64,
    "pressure_hpa": pl.Float64,
    "solar_wm2": pl.Float64,
    "cape_jkg": pl.Float64,
    "cin_jkg": pl.Float64,
    "freezing_level_m": pl.Float64,
    "ring_precip_max_mm": pl.Float64,
    "ring_precip_mean_mm": pl.Float64,
    "ring_temp_grad_c": pl.Float64,
    "ring_theta_e_grad_k": pl.Float64,
    "ring_gust_max_ms": pl.Float64,
}


def _arr(section: dict[str, Any], key: str, n: int) -> np.ndarray:
    raw = section.get(key)
    if not raw or len(raw) != n:
        return np.full(n, np.nan, dtype=np.float64)
    return np.array([np.nan if v is None else float(v) for v in raw], dtype=np.float64)


def _infer_cycle_init_utc(model_id: str, fetched_at_utc: datetime) -> datetime:
    """Estimate the active NWP cycle initialization timestamp accounting for dissemination lag."""
    lag_min = MODEL_AVAILABILITY_LAG_MINUTES.get(model_id, 60)
    avail_ref = fetched_at_utc - timedelta(minutes=lag_min)
    if model_id in ("hrrr", "rrfs", "nbm"):
        # Hourly cycles
        return avail_ref.replace(minute=0, second=0, microsecond=0)
    # 6-hourly global cycles (00Z, 06Z, 12Z, 18Z)
    cycle_hour = (avail_ref.hour // 6) * 6
    return avail_ref.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def parse_open_meteo_payload_to_df(
    payload: dict[str, Any] | list[dict[str, Any]],
    *,
    location_id: int,
    model_id: str,
    init_time_utc: datetime,
    is_subhourly_15m: bool = False,
) -> pl.DataFrame:
    """Convert single-point or 5-point Open-Meteo JSON response into forecast_snapshots DataFrame."""
    items = payload if isinstance(payload, list) else [payload]
    if not items:
        return pl.DataFrame(schema=SNAPSHOT_SCHEMA)

    sec_key = "minutely_15" if is_subhourly_15m else "hourly"
    center_sec = items[0].get(sec_key) or {}
    times_unix = center_sec.get("time") or []
    n = len(times_unix)
    if n == 0:
        return pl.DataFrame(schema=SNAPSHOT_SCHEMA)

    point_arrays: list[dict[str, np.ndarray]] = []
    for item in items:
        sec = item.get(sec_key) or {}
        point_arrays.append(
            {
                "temp_c": _arr(sec, "temperature_2m", n),
                "dewpoint_c": _arr(sec, "dew_point_2m", n),
                "wind_ms": _arr(sec, "wind_speed_10m", n),
                "gust_ms": _arr(sec, "wind_gusts_10m", n),
                "wind_dir_deg": _arr(sec, "wind_direction_10m", n),
                "precip_mm": _arr(sec, "precipitation", n),
                "snowfall_cm": _arr(sec, "snowfall", n),
                "precip_prob": _arr(sec, "precipitation_probability", n),
                "cloud_pct": _arr(sec, "cloud_cover", n),
                "pressure_hpa": _arr(sec, "surface_pressure", n),
                "solar_wm2": _arr(sec, "shortwave_radiation", n),
                "cape_jkg": _arr(sec, "cape", n),
                "cin_jkg": _arr(sec, "convective_inhibition", n),
                "freezing_level_m": _arr(sec, "freezing_level_height", n),
            }
        )

    c = point_arrays[0]
    ring = compute_ring_features(point_arrays)
    lag_min = MODEL_AVAILABILITY_LAG_MINUTES.get(model_id, 60)
    available_at = init_time_utc + timedelta(minutes=lag_min)

    valid_dts = [datetime.fromtimestamp(int(t), tz=UTC) for t in times_unix]
    lead_h = np.array(
        [(v - init_time_utc).total_seconds() / 3600.0 for v in valid_dts], dtype=np.float64
    )

    # Filter out rows where core temperature is completely NaN (past model forecast horizon)
    valid_mask = ~np.isnan(c["temp_c"])
    if not np.any(valid_mask):
        return pl.DataFrame(schema=SNAPSHOT_SCHEMA)

    idx = np.nonzero(valid_mask)[0]

    def pick(arr: np.ndarray) -> list[float | None]:
        return [None if np.isnan(arr[i]) else float(arr[i]) for i in idx]

    df = pl.DataFrame(
        {
            "location_id": [location_id] * len(idx),
            "model_id": [model_id] * len(idx),
            "init_time_utc": [init_time_utc] * len(idx),
            "available_at_utc": [available_at] * len(idx),
            "valid_time_utc": [valid_dts[i] for i in idx],
            "lead_hours": [float(lead_h[i]) for i in idx],
            "is_subhourly_15m": [is_subhourly_15m] * len(idx),
            "temp_c": pick(c["temp_c"]),
            "dewpoint_c": pick(c["dewpoint_c"]),
            "wind_ms": pick(c["wind_ms"]),
            "gust_ms": pick(c["gust_ms"]),
            "wind_dir_deg": pick(c["wind_dir_deg"]),
            "precip_mm": pick(c["precip_mm"]),
            "snowfall_cm": pick(c["snowfall_cm"]),
            "precip_prob": pick(c["precip_prob"]),
            "cloud_pct": pick(c["cloud_pct"]),
            "pressure_hpa": pick(c["pressure_hpa"]),
            "solar_wm2": pick(c["solar_wm2"]),
            "cape_jkg": pick(c["cape_jkg"]),
            "cin_jkg": pick(c["cin_jkg"]),
            "freezing_level_m": pick(c["freezing_level_m"]),
            "ring_precip_max_mm": pick(ring["ring_precip_max_mm"]),
            "ring_precip_mean_mm": pick(ring["ring_precip_mean_mm"]),
            "ring_temp_grad_c": pick(ring["ring_temp_grad_c"]),
            "ring_theta_e_grad_k": pick(ring["ring_theta_e_grad_k"]),
            "ring_gust_max_ms": pick(ring["ring_gust_max_ms"]),
        },
        schema=SNAPSHOT_SCHEMA,
    )
    return df


class OpenMeteoClient:
    """Async client for live multi-model NWP forecasts + 365-day historical archive backfill."""

    def __init__(self, ring_radius_km: float = 8.0, timeout_s: float = 25.0) -> None:
        self.ring_radius_km = ring_radius_km
        self.timeout_s = timeout_s
        self._resolved_slugs: dict[str, str] = {}

    async def _fetch_single_model(
        self,
        client: httpx.AsyncClient,
        *,
        location_id: int,
        lat: float,
        lon: float,
        model_id: str,
        now_utc: datetime,
    ) -> list[pl.DataFrame]:
        pts = (
            compute_spatial_ring(lat, lon, self.ring_radius_km)
            if model_id in RING_ENABLED_MODELS
            else [("center", round(lat, 5), round(lon, 5))]
        )
        lat_param = ",".join(str(p[1]) for p in pts)
        lon_param = ",".join(str(p[2]) for p in pts)

        forecast_days = 3 if model_id in ("hrrr", "rrfs") else 10
        slugs = (
            (self._resolved_slugs[model_id],)
            if model_id in self._resolved_slugs
            else MODEL_SLUG_CANDIDATES[model_id]
        )

        init_utc = _infer_cycle_init_utc(model_id, now_utc)
        dfs: list[pl.DataFrame] = []

        for slug in slugs:
            params: dict[str, Any] = {
                "latitude": lat_param,
                "longitude": lon_param,
                "hourly": HOURLY_VARS,
                "models": slug,
                "temperature_unit": "celsius",
                "wind_speed_unit": "ms",
                "precipitation_unit": "mm",
                "timeformat": "unixtime",
                "forecast_days": forecast_days,
            }
            if model_id == "hrrr":
                params["minutely_15"] = MINUTELY_15_VARS
                params["forecast_minutely_15"] = 24  # 6 hours of 15-min nowcast

            try:
                resp = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                hourly_df = parse_open_meteo_payload_to_df(
                    payload,
                    location_id=location_id,
                    model_id=model_id,
                    init_time_utc=init_utc,
                    is_subhourly_15m=False,
                )
                if hourly_df.is_empty():
                    continue
                self._resolved_slugs[model_id] = slug
                dfs.append(hourly_df)
                if model_id == "hrrr":
                    sub_df = parse_open_meteo_payload_to_df(
                        payload,
                        location_id=location_id,
                        model_id="hrrr",
                        init_time_utc=init_utc,
                        is_subhourly_15m=True,
                    )
                    if not sub_df.is_empty():
                        dfs.append(sub_df)
                break
            except httpx.HTTPError:
                continue

        return dfs

    async def fetch_all_live_models(
        self, location_id: int, lat: float, lon: float
    ) -> pl.DataFrame:
        """Concurrently pull HRRR (1h + 15m), RRFS, NBM, ECMWF IFS, ECMWF AIFS, and GFS."""
        now_utc = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            tasks = [
                self._fetch_single_model(
                    client,
                    location_id=location_id,
                    lat=lat,
                    lon=lon,
                    model_id=mid,
                    now_utc=now_utc,
                )
                for mid in MODEL_SLUG_CANDIDATES
            ]
            nested = await asyncio.gather(*tasks)

        frames = [df for group in nested for df in group if not df.is_empty()]
        if not frames:
            return pl.DataFrame(schema=SNAPSHOT_SCHEMA)
        return pl.concat(frames, how="vertical")

    async def fetch_historical_model_window(
        self,
        *,
        location_id: int,
        lat: float,
        lon: float,
        model_id: str,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """
        Fetch archived forecasts from Open-Meteo Historical Forecast API for Day-1 Cold-Start Backfill.
        Synthesizes realistic daily NWP cycle timestamps so ASOF JOIN lead-time training works seamlessly.
        """
        pts = (
            compute_spatial_ring(lat, lon, self.ring_radius_km)
            if model_id in ("hrrr", "ecmwf_ifs")
            else [("center", round(lat, 5), round(lon, 5))]
        )
        lat_param = ",".join(str(p[1]) for p in pts)
        lon_param = ",".join(str(p[2]) for p in pts)

        slugs = MODEL_SLUG_CANDIDATES[model_id]
        async with httpx.AsyncClient(timeout=45.0) as client:
            for slug in slugs:
                params = {
                    "latitude": lat_param,
                    "longitude": lon_param,
                    "start_date": start_date,
                    "end_date": end_date,
                    "hourly": HOURLY_VARS,
                    "models": slug,
                    "temperature_unit": "celsius",
                    "wind_speed_unit": "ms",
                    "precipitation_unit": "mm",
                    "timeformat": "unixtime",
                }
                try:
                    resp = await client.get(
                        "https://historical-forecast-api.open-meteo.com/v1/forecast",
                        params=params,
                    )
                    if resp.status_code != 200:
                        continue
                    payload = resp.json()
                    items = payload if isinstance(payload, list) else [payload]
                    sec = items[0].get("hourly") or {}
                    times = sec.get("time") or []
                    if not times:
                        continue

                    # Parse with nominal init_time = valid_time - 12h so archived runs are
                    # strictly available prior to valid_time across backfill windows.
                    base_df = parse_open_meteo_payload_to_df(
                        payload,
                        location_id=location_id,
                        model_id=model_id,
                        init_time_utc=datetime(2020, 1, 1, tzinfo=UTC),
                        is_subhourly_15m=False,
                    )
                    if base_df.is_empty():
                        continue

                    lag_min = MODEL_AVAILABILITY_LAG_MINUTES.get(model_id, 60)
                    # Stamp each archived day's 00Z cycle so 2h..168h ASOF JOINs find realistic past forecasts
                    df = base_df.with_columns(
                        (pl.col("valid_time_utc").dt.truncate("1d") - pl.duration(days=1)).alias(
                            "init_time_utc"
                        )
                    ).with_columns(
                        (pl.col("init_time_utc") + pl.duration(minutes=lag_min)).alias(
                            "available_at_utc"
                        ),
                        (
                            (pl.col("valid_time_utc") - pl.col("init_time_utc")).dt.total_seconds()
                            / 3600.0
                        ).alias("lead_hours"),
                    )
                    return df.select(list(SNAPSHOT_SCHEMA.keys()))
                except httpx.HTTPError:
                    continue
        return pl.DataFrame(schema=SNAPSHOT_SCHEMA)

    async def fetch_era5_reanalysis_fallback(
        self, lat: float, lon: float, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Fallback historical hourly observations via ERA5-Land if no nearby ASOS/Mesonet archive exists."""
        params = {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "start_date": start_date,
            "end_date": end_date,
            "hourly": "temperature_2m,dew_point_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation,surface_pressure,shortwave_radiation",
            "temperature_unit": "celsius",
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
            "timeformat": "unixtime",
        }
        async with httpx.AsyncClient(timeout=40.0) as client:
            try:
                resp = await client.get(
                    "https://archive-api.open-meteo.com/v1/archive", params=params
                )
                if resp.status_code != 200:
                    return []
                h = resp.json().get("hourly") or {}
                times = h.get("time") or []
                n = len(times)
                if n == 0:
                    return []
                t = _arr(h, "temperature_2m", n)
                td = _arr(h, "dew_point_2m", n)
                rh = _arr(h, "relative_humidity_2m", n)
                ws = _arr(h, "wind_speed_10m", n)
                wg = _arr(h, "wind_gusts_10m", n)
                wd = _arr(h, "wind_direction_10m", n)
                pr = _arr(h, "precipitation", n)
                ps = _arr(h, "surface_pressure", n)
                sr = _arr(h, "shortwave_radiation", n)
                out: list[dict[str, Any]] = []
                for i in range(n):
                    if np.isnan(t[i]):
                        continue
                    out.append(
                        {
                            "station_id": "ERA5L",
                            "source": "era5_land",
                            "valid_time_utc": datetime.fromtimestamp(int(times[i]), tz=UTC),
                            "temp_c": float(t[i]),
                            "dewpoint_c": None if np.isnan(td[i]) else float(td[i]),
                            "rel_humidity_pct": None if np.isnan(rh[i]) else float(rh[i]),
                            "wind_ms": None if np.isnan(ws[i]) else float(ws[i]),
                            "gust_ms": None if np.isnan(wg[i]) else float(wg[i]),
                            "wind_dir_deg": None if np.isnan(wd[i]) else float(wd[i]),
                            "precip_accum_mm": None,
                            "precip_1h_mm": 0.0 if np.isnan(pr[i]) else max(0.0, float(pr[i])),
                            "pressure_hpa": None if np.isnan(ps[i]) else float(ps[i]),
                            "solar_wm2": None if np.isnan(sr[i]) else float(sr[i]),
                        }
                    )
                return out
            except httpx.HTTPError:
                return []
