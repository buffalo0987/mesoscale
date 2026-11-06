"""Tests for Oklahoma Mesonet (.mdf/.mts/geoinfo) parsing, midnight rollover, and IDW blending."""

from __future__ import annotations

from datetime import UTC, datetime

from mesoscale.providers.base import StationMeta
from mesoscale.providers.ok_mesonet import (
    parse_mdf_or_mts_text,
    parse_mesonet_geoinfo_csv,
    records_to_station_polars,
)
from mesoscale.providers.station_blender import (
    blend_hourly_observations_for_location,
    compute_station_weights,
    rollup_station_observations_to_hourly,
)

SAMPLE_GEOINFO = """stid,name,city,nlat,elon,elev,datd
NRMN,Norman,Norman,35.2361,-97.4649,357,20991231
OKCE,Oklahoma City East,Oklahoma City,35.4720,-97.4649,368,20991231
OLD1,Old Decommissioned,Nowhere,35.0000,-97.0000,300,20150101
"""

SAMPLE_MTS_ROLLOVER = """  101 00000000 Oklahoma Mesonet Sample
  12 2026 11 05 23 50 00
 STID  STNM  TIME   RELH   TAIR   WSPD   WDIR   WMAX    RAIN     PRES   SRAD
 NRMN   121     0   82.0   12.4    4.2    180    7.1   15.00   974.20      0
 NRMN   121     5   84.0   12.0    4.5    185    8.9   16.20   974.10      0
 NRMN   121    10   85.0   11.8    5.0    190   11.4    0.80   974.00      0
 NRMN   121    15   -999   -998    4.8    195    6.2    1.30   973.90      0
"""


def test_parse_mesonet_geoinfo_filters_decommissioned() -> None:
    stations = parse_mesonet_geoinfo_csv(SAMPLE_GEOINFO)
    ids = [s.station_id for s in stations]
    assert ids == ["NRMN", "OKCE"]
    assert stations[0].elevation_m == 357.0


def test_mesonet_mts_qc_flags_and_midnight_rainfall_rollover() -> None:
    rows = parse_mdf_or_mts_text(SAMPLE_MTS_ROLLOVER)
    assert len(rows) == 4
    # Row 3 (offset +15m) has -999/-998 QC error flags -> must parse as None, not -998°C!
    assert rows[3]["temp_c"] is None
    assert rows[3]["rel_humidity_pct"] is None

    raw_df = records_to_station_polars(rows)
    hourly = rollup_station_observations_to_hourly(raw_df)
    assert hourly.height == 1
    rec = hourly.to_dicts()[0]
    assert rec["valid_hour_utc"] == datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
    # Closest reading to 00:00 UTC with valid temp is 00:00 UTC (offset +10m -> 11.8°C)
    assert abs(rec["temp_c"] - 11.8) < 1e-6
    # Trailing hour peak gust must be max(7.1, 8.9, 11.4, 6.2) = 11.4 m/s
    assert abs(rec["gust_ms"] - 11.4) < 1e-6
    # Midnight rollover: (16.20 - 15.00) + 0.80 (reset!) + (1.30 - 0.80) = 1.20 + 0.80 + 0.50 = 2.50 mm!
    assert abs(rec["precip_1h_mm"] - 2.50) < 1e-4


def test_elevation_adjusted_idw_station_blending_and_user_checkin() -> None:
    s1 = StationMeta("NRMN", "Norman", "ok_mesonet", 35.23, -97.46, elevation_m=300.0, distance_km=4.0)
    s2 = StationMeta("KOKC", "Will Rogers", "nws_asos", 35.39, -97.60, elevation_m=400.0, distance_km=4.0)
    weighted = compute_station_weights([s1, s2])
    # Oklahoma Mesonet receives 1.5x quality multiplier at equal distance
    assert weighted[0].blend_weight > weighted[1].blend_weight

    hr = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    raw = records_to_station_polars(
        [
            {
                "station_id": "NRMN",
                "source": "ok_mesonet",
                "valid_time_utc": hr,
                "temp_c": 20.0,
                "dewpoint_c": 10.0,
                "rel_humidity_pct": 52.0,
                "wind_ms": 5.0,
                "gust_ms": 8.0,
                "wind_dir_deg": 350.0,
                "precip_accum_mm": 0.0,
                "precip_1h_mm": 0.0,
                "pressure_hpa": 980.0,
                "solar_wm2": 400.0,
            },
            {
                "station_id": "KOKC",
                "source": "nws_asos",
                "valid_time_utc": hr,
                "temp_c": 19.35,  # 100m higher -> 0.65°C cooler in standard atmosphere!
                "dewpoint_c": 9.8,
                "rel_humidity_pct": 54.0,
                "wind_ms": 5.0,
                "gust_ms": 8.0,
                "wind_dir_deg": 10.0,  # Across 360° north wrap-around!
                "precip_accum_mm": None,
                "precip_1h_mm": 0.0,
                "pressure_hpa": 970.0,
                "solar_wm2": None,
            },
        ]
    )
    hourly_st = rollup_station_observations_to_hourly(raw)
    # Target elevation = 500m (+200m above NRMN -> -1.30°C lapse adjustment => ~18.7°C!)
    blended = blend_hourly_observations_for_location(
        location_id=1,
        target_elevation_m=500.0,
        stations=weighted,
        hourly_station_df=hourly_st,
        user_checkins=[
            {
                "reported_at_utc": "2026-11-06T11:55:00+00:00",
                "condition": "tstorm",
                "intensity_mm_hr": 8.5,
            }
        ],
    )
    row = blended.to_dicts()[0]
    assert abs(row["temp_c"] - 18.7) < 0.05
    # Vector wind blend of 350° and 10° must point near Due North (0°/360°), NOT 180° South!
    assert row["wind_dir_deg"] <= 8.0 or row["wind_dir_deg"] >= 352.0
    # Backyard thunderstorm check-in overrides 0.0 mm airport reading
    assert row["precip_1h_mm"] >= 8.0
    assert row["user_condition"] == "tstorm"
