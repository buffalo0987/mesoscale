"""Multi-station elevation-adjusted IDW ground-truth blender + 5-min Mesonet hourly rollup."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from mesoscale.ml.thermodynamics import wet_bulb_celsius
from mesoscale.providers.base import StationMeta
from mesoscale.providers.nws_iem import NwsIemClient
from mesoscale.providers.ok_mesonet import OklahomaMesonetClient

# Standard environmental lapse rate (-6.5 °C per 1,000m elevation gain)
LAPSE_RATE_C_PER_M = -0.0065

BLENDED_HOURLY_SCHEMA = {
    "location_id": pl.Int64,
    "valid_time_utc": pl.Datetime("us", "UTC"),
    "temp_c": pl.Float64,
    "dewpoint_c": pl.Float64,
    "wet_bulb_c": pl.Float64,
    "wind_ms": pl.Float64,
    "gust_ms": pl.Float64,
    "wind_dir_deg": pl.Float64,
    "precip_1h_mm": pl.Float64,
    "pressure_hpa": pl.Float64,
    "solar_wm2": pl.Float64,
    "user_condition": pl.String,
    "station_count": pl.Int64,
}


def compute_station_weights(stations: list[StationMeta]) -> list[StationMeta]:
    """
    Assign inverse-distance squared weights 1 / (d_km + 2.0)^2 with a 1.5x research-grade
    priority multiplier for 5-minute Oklahoma Mesonet towers.
    """
    if not stations:
        return []
    raw_weights: list[float] = []
    for st in stations:
        mult = 1.5 if st.source == "ok_mesonet" else (1.2 if st.source == "pws" else 1.0)
        w = mult / ((max(st.distance_km, 0.0) + 2.0) ** 2)
        raw_weights.append(w)
    total = sum(raw_weights) or 1.0
    out: list[StationMeta] = []
    for st, w in zip(stations, raw_weights, strict=False):
        st.blend_weight = round(w / total, 4)
        out.append(st)
    return out


async def discover_ground_truth_stations(
    lat: float,
    lon: float,
    mesonet_client: OklahomaMesonetClient,
    nws_client: NwsIemClient,
    max_total: int = 3,
) -> list[StationMeta]:
    """Pair the best 2-3 research-grade Oklahoma Mesonet and/or NWS ASOS towers for (lat, lon)."""
    meso = await mesonet_client.find_nearest_stations(lat, lon, max_distance_km=55.0, limit=2)
    asos = await nws_client.find_nearest_asos_stations(lat, lon, limit=3)
    combined = sorted(meso + asos, key=lambda s: (s.distance_km - (4.0 if s.source == "ok_mesonet" else 0.0)))
    picked = combined[:max_total]
    return compute_station_weights(picked)


def rollup_station_observations_to_hourly(raw_df: pl.DataFrame) -> pl.DataFrame:
    """
    Meteorologically exact hourly alignment:
    - Instantaneous scalars (temp_c, dewpoint_c, wind_ms, wind_dir_deg, pressure_hpa, solar_wm2)
      are taken from the reading closest to the top of the hour (:00 ± 15 min).
    - Gusts (gust_ms) are the trailing 60-minute MAXIMUM ending at valid_hour (:01..:00).
    - Oklahoma Mesonet daily cumulative RAIN (precip_accum_mm) is differenced step-by-step
      with midnight-UTC reset protection (delta < 0 -> max(new_accum, 0)) and summed over trailing 60m.
    """
    if raw_df.is_empty():
        return pl.DataFrame(
            schema={
                "station_id": pl.String,
                "source": pl.String,
                "valid_hour_utc": pl.Datetime("us", "UTC"),
                "temp_c": pl.Float64,
                "dewpoint_c": pl.Float64,
                "wind_ms": pl.Float64,
                "gust_ms": pl.Float64,
                "wind_dir_deg": pl.Float64,
                "precip_1h_mm": pl.Float64,
                "pressure_hpa": pl.Float64,
                "solar_wm2": pl.Float64,
            }
        )

    df = raw_df.sort(["station_id", "source", "valid_time_utc"])

    # Compute incremental rainfall from daily cumulative precip_accum_mm (Oklahoma Mesonet RAIN)
    df = df.with_columns(
        pl.col("precip_accum_mm").shift(1).over(["station_id", "source"]).alias("_prev_accum"),
        pl.col("valid_time_utc").shift(1).over(["station_id", "source"]).alias("_prev_time"),
    )
    df = df.with_columns(
        pl.when(pl.col("precip_1h_mm").is_not_null())
        .then(pl.col("precip_1h_mm"))
        .when(pl.col("precip_accum_mm").is_not_null() & pl.col("_prev_accum").is_not_null())
        .then(
            pl.when(
                (pl.col("precip_accum_mm") >= pl.col("_prev_accum") - 1e-4)
                & ((pl.col("valid_time_utc") - pl.col("_prev_time")) <= timedelta(minutes=90))
            )
            .then(pl.max_horizontal(pl.lit(0.0), pl.col("precip_accum_mm") - pl.col("_prev_accum")))
            .otherwise(pl.max_horizontal(pl.lit(0.0), pl.col("precip_accum_mm")))
        )
        .otherwise(pl.lit(0.0))
        .alias("_step_precip_mm"),
        # Trailing hour label: e.g. 14:05..15:00 belongs to trailing hour ending at 15:00 UTC;
        # ASOS :53/:54 METAR obs also belong to the upcoming :00 hour!
        (pl.col("valid_time_utc") + pl.duration(minutes=12))
        .dt.truncate("1h")
        .alias("valid_hour_utc"),
    )

    df = df.with_columns(
        (pl.col("valid_time_utc") - pl.col("valid_hour_utc"))
        .dt.total_seconds()
        .abs()
        .alias("_dist_to_top_s")
    )

    # Top-of-hour instantaneous scalars (closest tick within ±20 min)
    inst = (
        df.filter(pl.col("_dist_to_top_s") <= 1200)
        .sort("_dist_to_top_s")
        .group_by(["station_id", "source", "valid_hour_utc"], maintain_order=True)
        .first()
        .select(
            "station_id",
            "source",
            "valid_hour_utc",
            "temp_c",
            "dewpoint_c",
            "wind_ms",
            "wind_dir_deg",
            "pressure_hpa",
            "solar_wm2",
        )
    )

    # Trailing-hour maxima / accumulations
    trail_exprs: list[pl.Expr] = [
        pl.max_horizontal(pl.col("gust_ms").max(), pl.col("wind_ms").max()).alias("gust_ms"),
        pl.when(pl.col("source").first() == "ok_mesonet")
        .then(pl.col("_step_precip_mm").sum())
        .otherwise(pl.col("precip_1h_mm").max().fill_null(0.0))
        .alias("precip_1h_mm"),
    ]
    trail = df.group_by(["station_id", "source", "valid_hour_utc"]).agg(trail_exprs)

    return inst.join(trail, on=["station_id", "source", "valid_hour_utc"], how="inner").sort(
        ["station_id", "valid_hour_utc"]
    )


def blend_hourly_observations_for_location(
    *,
    location_id: int,
    target_elevation_m: float,
    stations: list[StationMeta],
    hourly_station_df: pl.DataFrame,
    user_checkins: list[dict[str, Any]] | None = None,
) -> pl.DataFrame:
    """
    Blend 2-3 nearby stations to the exact target coordinate & elevation using:
    - Standard lapse-rate adjustment: T_adj = T_station + (z_target - z_station) * (-0.0065 °C/m)
    - Inverse-distance + Mesonet-priority weights
    - Vector (u, v) wind direction blending
    - Direct fusion of 1-tap backyard precipitation check-ins
    """
    if hourly_station_df.is_empty():
        return pl.DataFrame(schema=BLENDED_HOURLY_SCHEMA)

    st_map: dict[str, StationMeta] = {s.station_id.upper(): s for s in stations}
    # Support ERA5L fallback when stations list doesn't contain ERA5L
    if "ERA5L" not in st_map:
        st_map["ERA5L"] = StationMeta(
            station_id="ERA5L",
            name="ERA5-Land",
            source="era5_land",
            lat=0.0,
            lon=0.0,
            elevation_m=target_elevation_m,
            distance_km=5.0,
            blend_weight=0.25,
        )

    checkin_by_hour: dict[datetime, tuple[str, float]] = {}
    for chk in user_checkins or []:
        try:
            dt = datetime.fromisoformat(str(chk["reported_at_utc"]).replace("Z", "+00:00"))
            hr = (dt + timedelta(minutes=20)).replace(minute=0, second=0, microsecond=0)
            checkin_by_hour[hr] = (str(chk["condition"]), float(chk.get("intensity_mm_hr") or 0.0))
        except (KeyError, ValueError):
            continue

    rows_by_hour: dict[datetime, list[dict[str, Any]]] = {}
    for row in hourly_station_df.to_dicts():
        hr = row["valid_hour_utc"]
        rows_by_hour.setdefault(hr, []).append(row)

    out_rows: list[dict[str, Any]] = []
    for hr in sorted(rows_by_hour.keys()):
        obs_list = rows_by_hour[hr]
        t_num = t_den = 0.0
        td_num = td_den = 0.0
        ws_num = ws_den = 0.0
        wg_num = wg_den = 0.0
        u_num = v_num = uv_den = 0.0
        pr_num = pr_den = 0.0
        ps_num = ps_den = 0.0
        sr_num = sr_den = 0.0

        for r in obs_list:
            meta = st_map.get(str(r["station_id"]).upper())
            w = meta.blend_weight if (meta and meta.blend_weight > 0) else 0.33
            dz = target_elevation_m - (meta.elevation_m if meta else target_elevation_m)
            elev_corr_c = dz * LAPSE_RATE_C_PER_M

            if r["temp_c"] is not None:
                t_num += w * (float(r["temp_c"]) + elev_corr_c)
                t_den += w
            if r["dewpoint_c"] is not None:
                # Dewpoint lapse rate ~ -0.002 °C/m
                td_num += w * (float(r["dewpoint_c"]) + dz * -0.002)
                td_den += w
            if r["wind_ms"] is not None:
                ws = float(r["wind_ms"])
                ws_num += w * ws
                ws_den += w
                if r["wind_dir_deg"] is not None:
                    rad = math.radians(float(r["wind_dir_deg"]))
                    u_num += w * (-ws * math.sin(rad))
                    v_num += w * (-ws * math.cos(rad))
                    uv_den += w
            if r["gust_ms"] is not None:
                wg_num += w * float(r["gust_ms"])
                wg_den += w
            if r["precip_1h_mm"] is not None:
                pr_num += w * float(r["precip_1h_mm"])
                pr_den += w
            if r["pressure_hpa"] is not None:
                ps_num += w * float(r["pressure_hpa"])
                ps_den += w
            if r["solar_wm2"] is not None:
                sr_num += w * float(r["solar_wm2"])
                sr_den += w

        if t_den == 0.0:
            continue

        temp_c = t_num / t_den
        dewpoint_c = min(temp_c, td_num / td_den) if td_den > 0 else temp_c - 4.0
        tw_c = float(wet_bulb_celsius(temp_c, dewpoint_c=dewpoint_c))
        wind_ms = ws_num / ws_den if ws_den > 0 else None
        gust_ms = wg_num / wg_den if wg_den > 0 else wind_ms

        wind_dir: float | None = None
        if uv_den > 0:
            u = u_num / uv_den
            v = v_num / uv_den
            if abs(u) > 1e-4 or abs(v) > 1e-4:
                wind_dir = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0

        precip_mm = pr_num / pr_den if pr_den > 0 else 0.0
        user_cond: str | None = None
        if hr in checkin_by_hour:
            cond, user_mm = checkin_by_hour[hr]
            user_cond = cond
            if cond == "dry":
                # Backyard check-in overrides distant shower false-positive
                precip_mm = min(precip_mm, 0.05)
            else:
                precip_mm = max(precip_mm, max(user_mm, 0.5))

        out_rows.append(
            {
                "location_id": location_id,
                "valid_time_utc": hr,
                "temp_c": round(temp_c, 3),
                "dewpoint_c": round(dewpoint_c, 3),
                "wet_bulb_c": round(tw_c, 3),
                "wind_ms": round(wind_ms, 3) if wind_ms is not None else None,
                "gust_ms": round(gust_ms, 3) if gust_ms is not None else None,
                "wind_dir_deg": round(wind_dir, 1) if wind_dir is not None else None,
                "precip_1h_mm": round(max(0.0, precip_mm), 3),
                "pressure_hpa": round(ps_num / ps_den, 2) if ps_den > 0 else None,
                "solar_wm2": round(sr_num / sr_den, 1) if sr_den > 0 else None,
                "user_condition": user_cond,
                "station_count": len(obs_list),
            }
        )

    if not out_rows:
        return pl.DataFrame(schema=BLENDED_HOURLY_SCHEMA)
    return pl.DataFrame(out_rows, schema=BLENDED_HOURLY_SCHEMA)
