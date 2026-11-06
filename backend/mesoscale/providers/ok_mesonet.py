"""Native Oklahoma Mesonet 5-minute (.mdf), daily (.mts), and geoinfo.csv adapter."""

from __future__ import annotations

import csv
import io
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import polars as pl

from mesoscale.ml.thermodynamics import dewpoint_from_temp_rh
from mesoscale.providers.base import LatestStationReading, StationMeta, haversine_km

# Curated fallback catalog covering key Oklahoma Mesonet research stations across the state
# (used immediately if mesonet.org's geoinfo CSV endpoint is temporarily unreachable).
CURATED_OK_MESONET_STATIONS: tuple[tuple[str, str, float, float, float], ...] = (
    ("OKCE", "Oklahoma City East", 35.4720, -97.4649, 368.0),
    ("OKCN", "Oklahoma City North", 35.5562, -97.5142, 355.0),
    ("OKCW", "Oklahoma City West", 35.4730, -97.5847, 377.0),
    ("NRMN", "Norman", 35.2361, -97.4649, 357.0),
    ("SPEN", "Spencer", 35.5421, -97.3415, 373.0),
    ("ELRE", "El Reno", 35.5485, -98.0365, 419.0),
    ("GUTH", "Guthrie", 35.8489, -97.4798, 330.0),
    ("SHAW", "Shawnee", 35.3649, -96.9482, 328.0),
    ("WASH", "Washington", 34.9822, -97.5211, 345.0),
    ("MINC", "Minco", 35.2723, -97.9555, 430.0),
    ("KING", "Kingfisher", 35.8805, -97.9114, 319.0),
    ("STIL", "Stillwater", 36.1209, -97.0953, 272.0),
    ("TULS", "Tulsa", 36.2025, -95.8861, 198.0),
    ("BIXB", "Bixby", 35.9631, -95.8662, 184.0),
    ("CLAR", "Claremore", 36.3212, -95.6462, 208.0),
    ("LAWT", "Lawton", 34.6089, -98.4576, 355.0),
    ("APAC", "Apache", 34.9142, -98.2922, 440.0),
    ("ENID", "Enid", 36.4126, -97.8732, 396.0),
    ("WOOD", "Woodward", 36.4233, -99.4169, 625.0),
    ("GOOD", "Goodwell", 36.5988, -101.5955, 996.0),
    ("ALTU", "Altus", 34.5872, -99.3381, 416.0),
    ("ARD2", "Ardmore", 34.1927, -97.0858, 268.0),
    ("MCAL", "McAlester", 34.8823, -95.7810, 226.0),
    ("IDAB", "Idabel", 33.8301, -94.8803, 110.0),
    ("TAHL", "Tahlequah", 35.9724, -94.9866, 290.0),
    ("POTE", "Poteau", 35.0605, -94.6295, 145.0),
    ("PONC", "Ponca City", 36.7365, -97.1072, 315.0),
    ("WEAT", "Weatherford", 35.5083, -98.7753, 538.0),
    ("CHIC", "Chickasha", 35.0324, -97.9145, 328.0),
    ("PAUL", "Pauls Valley", 34.7155, -97.2292, 294.0),
)

GEOINFO_URLS = (
    "https://www.mesonet.org/index.php/api/stationinfo/from_all_active_with_geo_fields/format/csv/",
    "https://data.mesonet.org/data/public/mesonet/mdf/geoinfo.csv",
)


def _clean_mesonet_val(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    # Oklahoma Mesonet flags missing/QC errors with <= -990 (-999, -998, -997, -996, -995)
    if v <= -990.0 or math.isnan(v):
        return None
    return v


def parse_mesonet_geoinfo_csv(text: str) -> list[StationMeta]:
    """Parse Oklahoma Mesonet geoinfo.csv into StationMeta records."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    stations: list[StationMeta] = []
    for row in reader:
        norm = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
        stid = norm.get("stid", "").upper()
        if len(stid) != 4:
            continue
        # Skip decommissioned stations if datd != 20991231 and datd is non-empty past date
        datd = norm.get("datd", "20991231")
        if datd and datd not in ("20991231", "0", "") and len(datd) == 8 and datd < "20260101":
            continue
        try:
            lat = float(norm.get("nlat") or norm.get("lat") or "")
            lon = float(norm.get("elon") or norm.get("lon") or "")
            elev = float(norm.get("elev") or "350.0")
        except ValueError:
            continue
        name = norm.get("name") or stid
        stations.append(
            StationMeta(
                station_id=stid,
                name=f"{name} Mesonet ({stid})",
                source="ok_mesonet",
                lat=lat,
                lon=lon,
                elevation_m=elev,
            )
        )
    return stations


def parse_mdf_or_mts_text(
    text: str, station_filter: set[str] | None = None
) -> list[dict[str, Any]]:
    """
    Parse Oklahoma Mesonet space-delimited .mdf (all-station snapshot) or .mts (24h station series).
    Header format:
      Line 1: Copyright notice (e.g. '  101 00000000 ...' or text)
      Line 2: '  24 2026 11 06 15 00 00' -> base UTC timestamp (num_cols YYYY MM DD HH MM SS)
      Line 3: ' STID  STNM  TIME   RELH   TAIR   WSPD   WVEC   WDIR   WDSD   WSSD   WMAX    RAIN     PRES   SRAD ...'
      Line 4+: space-delimited rows where TIME is offset in minutes from base timestamp!
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return []

    # Locate base timestamp line & column header line
    time_line_idx = -1
    for idx in range(min(5, len(lines) - 1)):
        toks = lines[idx].split()
        if len(toks) >= 7 and all(t.lstrip("-").isdigit() for t in toks[:7]):
            yr = int(toks[1])
            if 1990 <= yr <= 2100:
                time_line_idx = idx
                break
    if time_line_idx < 0 or time_line_idx + 1 >= len(lines):
        return []

    t_toks = [int(x) for x in lines[time_line_idx].split()[:7]]
    base_dt = datetime(
        t_toks[1], t_toks[2], t_toks[3], t_toks[4], t_toks[5], t_toks[6], tzinfo=UTC
    )
    headers = [h.upper() for h in lines[time_line_idx + 1].split()]
    col_idx = {h: i for i, h in enumerate(headers)}
    if "STID" not in col_idx or "TIME" not in col_idx:
        return []

    records: list[dict[str, Any]] = []
    for raw_line in lines[time_line_idx + 2 :]:
        parts = raw_line.split()
        if len(parts) < len(headers):
            continue
        stid = parts[col_idx["STID"]].upper()
        if station_filter is not None and stid not in station_filter:
            continue
        offset_min = _clean_mesonet_val(parts[col_idx["TIME"]])
        if offset_min is None:
            continue
        valid_dt = base_dt + timedelta(minutes=float(offset_min))

        def get_val(col: str, row_parts: list[str] = parts) -> float | None:
            i = col_idx.get(col)
            return _clean_mesonet_val(row_parts[i]) if i is not None and i < len(row_parts) else None

        tair = get_val("TAIR")
        relh = get_val("RELH")
        td: float | None = None
        if tair is not None and relh is not None:
            td = float(dewpoint_from_temp_rh(tair, relh))

        records.append(
            {
                "station_id": stid,
                "source": "ok_mesonet",
                "valid_time_utc": valid_dt,
                "temp_c": tair,
                "dewpoint_c": td,
                "rel_humidity_pct": relh,
                "wind_ms": get_val("WSPD"),
                "gust_ms": get_val("WMAX"),
                "wind_dir_deg": get_val("WDIR"),
                "precip_accum_mm": get_val("RAIN"),
                "precip_1h_mm": None,
                "pressure_hpa": get_val("PRES"),
                "solar_wm2": get_val("SRAD"),
            }
        )
    return records


class OklahomaMesonetClient:
    """Async adapter for Oklahoma Mesonet real-time 5-min (.mdf) and daily (.mts) feeds."""

    def __init__(self, timeout_s: float = 15.0) -> None:
        self.timeout_s = timeout_s
        self._catalog: list[StationMeta] | None = None

    async def get_station_catalog(self) -> list[StationMeta]:
        if self._catalog is not None:
            return self._catalog

        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            for url in GEOINFO_URLS:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and "stid" in resp.text.lower():
                        parsed = parse_mesonet_geoinfo_csv(resp.text)
                        if len(parsed) >= 20:
                            self._catalog = parsed
                            return parsed
                except httpx.HTTPError:
                    continue

        self._catalog = [
            StationMeta(
                station_id=stid,
                name=f"{name} Mesonet ({stid})",
                source="ok_mesonet",
                lat=lat,
                lon=lon,
                elevation_m=elev,
            )
            for stid, name, lat, lon, elev in CURATED_OK_MESONET_STATIONS
        ]
        return self._catalog

    async def find_nearest_stations(
        self, lat: float, lon: float, max_distance_km: float = 60.0, limit: int = 3
    ) -> list[StationMeta]:
        # Quick bounding check around Oklahoma + border counties
        if not (33.0 <= lat <= 37.6 and -103.6 <= lon <= -93.8):
            return []
        catalog = await self.get_station_catalog()
        ranked: list[StationMeta] = []
        for s in catalog:
            d = haversine_km(lat, lon, s.lat, s.lon)
            if d <= max_distance_km:
                ranked.append(
                    StationMeta(
                        station_id=s.station_id,
                        name=s.name,
                        source="ok_mesonet",
                        lat=s.lat,
                        lon=s.lon,
                        elevation_m=s.elevation_m,
                        distance_km=round(d, 2),
                    )
                )
        ranked.sort(key=lambda x: x.distance_km)
        return ranked[:limit]

    async def fetch_latest_mdf(
        self, station_filter: set[str] | None = None, now_utc: datetime | None = None
    ) -> list[dict[str, Any]]:
        """
        Fetch the latest published 5-minute .mdf network snapshot (walking back 5-25 min
        to account for ~3-6 min Mesonet quality-control & ingest lag).
        """
        ref = (now_utc or datetime.now(UTC)).replace(second=0, microsecond=0)
        ref = ref - timedelta(minutes=ref.minute % 5)

        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            for step in range(1, 7):
                ts = ref - timedelta(minutes=5 * step)
                stamp = ts.strftime("%Y%m%d%H%M")
                y, m, d = ts.strftime("%Y"), ts.strftime("%m"), ts.strftime("%d")
                urls = (
                    f"https://data.mesonet.org/data/public/mesonet/mdf/{y}/{m}/{d}/{stamp}.mdf",
                    f"https://www.mesonet.org/data/public/mesonet/mdf/{y}/{m}/{d}/{stamp}.mdf",
                )
                for url in urls:
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200 and "STID" in resp.text:
                            rows = parse_mdf_or_mts_text(resp.text, station_filter=station_filter)
                            if rows:
                                return rows
                    except httpx.HTTPError:
                        continue
        return []

    async def fetch_station_mts_day(self, station_id: str, day_utc: datetime) -> list[dict[str, Any]]:
        """Fetch a single UTC day's 5-minute .mts file for a specific Oklahoma Mesonet station."""
        stid = station_id.lower()
        y, m, d = day_utc.strftime("%Y"), day_utc.strftime("%m"), day_utc.strftime("%d")
        ymd = day_utc.strftime("%Y%m%d")
        urls = (
            f"https://data.mesonet.org/data/public/mesonet/mts/{y}/{m}/{d}/{ymd}{stid}.mts",
            f"https://www.mesonet.org/data/public/mesonet/mts/{y}/{m}/{d}/{ymd}{stid}.mts",
        )
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and "STID" in resp.text:
                        return parse_mdf_or_mts_text(
                            resp.text, station_filter={station_id.upper()}
                        )
                except httpx.HTTPError:
                    continue
        return []

    async def fetch_nearby_station_vectors(
        self, lat: float, lon: float, radius_km: float = 95.0
    ) -> list[LatestStationReading]:
        """Return live 5-min temperature & wind vectors across nearby Oklahoma Mesonet towers."""
        nearby = await self.find_nearest_stations(lat, lon, max_distance_km=radius_km, limit=25)
        if not nearby:
            return []
        by_id = {s.station_id: s for s in nearby}
        rows = await self.fetch_latest_mdf(station_filter=set(by_id.keys()))
        out: list[LatestStationReading] = []
        for r in rows:
            meta = by_id.get(r["station_id"])
            if not meta:
                continue
            out.append(
                LatestStationReading(
                    station_id=meta.station_id,
                    name=meta.name,
                    source="ok_mesonet",
                    lat=meta.lat,
                    lon=meta.lon,
                    elevation_m=meta.elevation_m,
                    valid_time_utc=r["valid_time_utc"],
                    temp_c=r["temp_c"],
                    dewpoint_c=r["dewpoint_c"],
                    wind_ms=r["wind_ms"],
                    gust_ms=r["gust_ms"],
                    wind_dir_deg=r["wind_dir_deg"],
                    precip_1h_mm=None,
                    pressure_hpa=r["pressure_hpa"],
                    distance_km=meta.distance_km,
                )
            )
        return out


def records_to_station_polars(records: list[dict[str, Any]]) -> pl.DataFrame:
    """Convert raw observation dicts into typed Polars DataFrame matching observations_station_raw."""
    schema = {
        "station_id": pl.String,
        "source": pl.String,
        "valid_time_utc": pl.Datetime("us", "UTC"),
        "temp_c": pl.Float64,
        "dewpoint_c": pl.Float64,
        "rel_humidity_pct": pl.Float64,
        "wind_ms": pl.Float64,
        "gust_ms": pl.Float64,
        "wind_dir_deg": pl.Float64,
        "precip_accum_mm": pl.Float64,
        "precip_1h_mm": pl.Float64,
        "pressure_hpa": pl.Float64,
        "solar_wm2": pl.Float64,
    }
    if not records:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(records, schema=schema)
