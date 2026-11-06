"""Nationwide US NWS live METAR/alerts, geocoding, and Iowa Environmental Mesonet ASOS archive."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx

from mesoscale.providers.base import (
    GeoSearchResult,
    LatestStationReading,
    NwsAlert,
    StationMeta,
    haversine_km,
)


def _convert_nws_quantity(prop: dict[str, Any] | None, target: str) -> float | None:
    if not prop or prop.get("value") is None:
        return None
    try:
        val = float(prop["value"])
    except (TypeError, ValueError):
        return None
    unit = str(prop.get("unitCode") or "").lower()

    if target == "c":
        if "degf" in unit or "fahrenheit" in unit:
            return (val - 32.0) * 5.0 / 9.0
        return val
    if target == "ms":
        if "km_h" in unit or "km/h" in unit:
            return val / 3.6
        if "kt" in unit or "kn" in unit:
            return val * 0.514444
        if "mph" in unit or "mi_h" in unit:
            return val * 0.44704
        return val
    if target == "hpa":
        if ":pa" in unit or unit.endswith("pa"):
            return val / 100.0
        return val
    if target == "mm":
        # wmoUnit:m vs wmoUnit:mm
        if unit.endswith((":m", "/m")):
            return val * 1000.0
        return max(0.0, val)
    return val


def parse_iem_asos_csv(text: str, station_id: str) -> list[dict[str, Any]]:
    """Parse Iowa Environmental Mesonet asos.py comma-delimited historical ASOS output."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 2:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    records: list[dict[str, Any]] = []

    def fval(row: dict[str, str], key: str) -> float | None:
        raw = (row.get(key) or "").strip()
        if not raw or raw.lower() in ("null", "m", "na", "trace"):
            if raw.lower() == "trace":
                return 0.05
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    stid = station_id.upper()
    for row in reader:
        valid_str = (row.get("valid") or "").strip()
        if not valid_str:
            continue
        try:
            # IEM UTC format: '2026-05-01 14:53' or ISO
            dt = datetime.fromisoformat(valid_str.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except ValueError:
            continue

        sknt = fval(row, "sknt")
        gust = fval(row, "gust")
        alti = fval(row, "alti")
        p01m = fval(row, "p01m")

        records.append(
            {
                "station_id": stid,
                "source": "nws_asos",
                "valid_time_utc": dt,
                "temp_c": fval(row, "tmpc"),
                "dewpoint_c": fval(row, "dwpc"),
                "rel_humidity_pct": fval(row, "relh"),
                "wind_ms": sknt * 0.514444 if sknt is not None else None,
                "gust_ms": gust * 0.514444 if gust is not None else None,
                "wind_dir_deg": fval(row, "drct"),
                "precip_accum_mm": None,
                "precip_1h_mm": max(0.0, p01m) if p01m is not None else None,
                "pressure_hpa": alti * 33.86389 if alti is not None else None,
                "solar_wm2": None,
            }
        )
    return records


class NwsIemClient:
    """Client for US geocoding, NWS live METAR & alerts, and IEM historical ASOS."""

    def __init__(self, user_agent: str, timeout_s: float = 20.0) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/geo+json, application/json",
        }

    async def geocode_search(self, query: str, limit: int = 8) -> list[GeoSearchResult]:
        q = query.strip()
        if not q:
            return []
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": q, "count": 15, "language": "en", "format": "json"}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        raw_results = data.get("results") or []
        # Prioritize US results first, then global if requested
        us_first = sorted(
            raw_results,
            key=lambda r: (0 if r.get("country_code") == "US" else 1, -(r.get("population") or 0)),
        )
        out: list[GeoSearchResult] = []
        for item in us_first[:limit]:
            out.append(
                GeoSearchResult(
                    name=item.get("name") or q,
                    admin1=item.get("admin1") or "",
                    country=item.get("country_code") or "US",
                    lat=float(item["latitude"]),
                    lon=float(item["longitude"]),
                    elevation_m=float(item.get("elevation") or 300.0),
                    timezone=item.get("timezone") or "America/Chicago",
                )
            )
        return out

    async def reverse_point_metadata(self, lat: float, lon: float) -> GeoSearchResult:
        """Resolve city name, elevation, and timezone for raw GPS coordinates via NWS + Open-Meteo."""
        name = f"{lat:.3f}, {lon:.3f}"
        admin1 = ""
        tz = "America/Chicago"
        elev_m = 300.0
        async with httpx.AsyncClient(timeout=12.0, headers=self._headers(), follow_redirects=True) as client:
            try:
                r = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
                if r.status_code == 200:
                    props = r.json().get("properties") or {}
                    rel = (props.get("relativeLocation") or {}).get("properties") or {}
                    if rel.get("city"):
                        name = str(rel["city"])
                    if rel.get("state"):
                        admin1 = str(rel["state"])
                    if props.get("timeZone"):
                        tz = str(props["timeZone"])
            except httpx.HTTPError:
                pass
            try:
                er = await client.get(
                    "https://api.open-meteo.com/v1/elevation",
                    params={"latitude": lat, "longitude": lon},
                )
                if er.status_code == 200:
                    el_list = er.json().get("elevation") or []
                    if el_list and el_list[0] is not None:
                        elev_m = float(el_list[0])
            except httpx.HTTPError:
                pass

        return GeoSearchResult(
            name=name,
            admin1=admin1,
            country="US",
            lat=lat,
            lon=lon,
            elevation_m=elev_m,
            timezone=tz,
        )

    async def find_nearest_asos_stations(
        self, lat: float, lon: float, limit: int = 3
    ) -> list[StationMeta]:
        async with httpx.AsyncClient(
            timeout=self.timeout_s, headers=self._headers(), follow_redirects=True
        ) as client:
            try:
                pt_resp = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
                if pt_resp.status_code != 200:
                    return []
                stations_url = (pt_resp.json().get("properties") or {}).get("observationStations")
                if not stations_url:
                    return []
                st_resp = await client.get(stations_url)
                if st_resp.status_code != 200:
                    return []
                features = st_resp.json().get("features") or []
            except httpx.HTTPError:
                return []

        candidates: list[StationMeta] = []
        for feat in features[:15]:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            stid = str(props.get("stationIdentifier") or "").upper()
            if len(coords) < 2 or not stid:
                continue
            # Prefer 4-letter ICAO airport ASOS/AWOS stations (Kxxx / Pxxx)
            if len(stid) != 4 or not stid.isalpha():
                continue
            slon, slat = float(coords[0]), float(coords[1])
            elev_prop = props.get("elevation") or {}
            elev_m = float(elev_prop.get("value") or 300.0)
            dist = haversine_km(lat, lon, slat, slon)
            if dist <= 90.0:
                candidates.append(
                    StationMeta(
                        station_id=stid,
                        name=str(props.get("name") or stid),
                        source="nws_asos",
                        lat=slat,
                        lon=slon,
                        elevation_m=elev_m,
                        distance_km=round(dist, 2),
                    )
                )
        candidates.sort(key=lambda s: s.distance_km)
        return candidates[:limit]

    async def fetch_latest_asos_observations(
        self, station_id: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        url = f"https://api.weather.gov/stations/{station_id.upper()}/observations"
        async with httpx.AsyncClient(
            timeout=self.timeout_s, headers=self._headers(), follow_redirects=True
        ) as client:
            try:
                resp = await client.get(url, params={"limit": limit})
                if resp.status_code != 200:
                    return []
                features = resp.json().get("features") or []
            except httpx.HTTPError:
                return []

        records: list[dict[str, Any]] = []
        for feat in features:
            props = feat.get("properties") or {}
            ts_str = props.get("timestamp")
            if not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            records.append(
                {
                    "station_id": station_id.upper(),
                    "source": "nws_asos",
                    "valid_time_utc": dt,
                    "temp_c": _convert_nws_quantity(props.get("temperature"), "c"),
                    "dewpoint_c": _convert_nws_quantity(props.get("dewpoint"), "c"),
                    "rel_humidity_pct": _convert_nws_quantity(props.get("relativeHumidity"), "pct"),
                    "wind_ms": _convert_nws_quantity(props.get("windSpeed"), "ms"),
                    "gust_ms": _convert_nws_quantity(props.get("windGust"), "ms"),
                    "wind_dir_deg": _convert_nws_quantity(props.get("windDirection"), "deg"),
                    "precip_accum_mm": None,
                    "precip_1h_mm": _convert_nws_quantity(props.get("precipitationLastHour"), "mm"),
                    "pressure_hpa": _convert_nws_quantity(props.get("barometricPressure"), "hpa"),
                    "solar_wm2": None,
                }
            )
        return records

    async def fetch_nearby_asos_vectors(
        self, lat: float, lon: float, limit: int = 5
    ) -> list[LatestStationReading]:
        stations = await self.find_nearest_asos_stations(lat, lon, limit=limit)
        readings: list[LatestStationReading] = []
        for st in stations:
            obs = await self.fetch_latest_asos_observations(st.station_id, limit=1)
            if not obs:
                continue
            r = obs[0]
            readings.append(
                LatestStationReading(
                    station_id=st.station_id,
                    name=st.name,
                    source="nws_asos",
                    lat=st.lat,
                    lon=st.lon,
                    elevation_m=st.elevation_m,
                    valid_time_utc=r["valid_time_utc"],
                    temp_c=r["temp_c"],
                    dewpoint_c=r["dewpoint_c"],
                    wind_ms=r["wind_ms"],
                    gust_ms=r["gust_ms"],
                    wind_dir_deg=r["wind_dir_deg"],
                    precip_1h_mm=r["precip_1h_mm"],
                    pressure_hpa=r["pressure_hpa"],
                    distance_km=st.distance_km,
                )
            )
        return readings

    async def fetch_active_alerts(self, lat: float, lon: float) -> list[NwsAlert]:
        url = "https://api.weather.gov/alerts/active"
        async with httpx.AsyncClient(
            timeout=15.0, headers=self._headers(), follow_redirects=True
        ) as client:
            try:
                resp = await client.get(url, params={"point": f"{lat:.4f},{lon:.4f}"})
                if resp.status_code != 200:
                    return []
                features = resp.json().get("features") or []
            except httpx.HTTPError:
                return []

        alerts: list[NwsAlert] = []
        for feat in features:
            props = feat.get("properties") or {}
            alerts.append(
                NwsAlert(
                    id=str(props.get("id") or feat.get("id") or ""),
                    event=str(props.get("event") or "Weather Alert"),
                    severity=str(props.get("severity") or "Unknown"),
                    certainty=str(props.get("certainty") or "Unknown"),
                    urgency=str(props.get("urgency") or "Unknown"),
                    headline=str(props.get("headline") or props.get("event") or ""),
                    description=str(props.get("description") or ""),
                    instruction=str(props.get("instruction") or ""),
                    onset_utc=props.get("onset"),
                    expires_utc=props.get("expires"),
                    geometry=feat.get("geometry"),
                )
            )
        return alerts

    async def fetch_iem_historical_asos(
        self, station_id: str, start_utc: datetime, end_utc: datetime
    ) -> list[dict[str, Any]]:
        """Download historical hourly ASOS observations from Iowa Environmental Mesonet."""
        # IEM strips leading 'K' for CONUS 3-letter FAA IDs, or accepts both—try 3-letter first for Kxxx
        stid = station_id.upper()
        candidates = [stid[1:], stid] if len(stid) == 4 and stid.startswith("K") else [stid]

        url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            for code in candidates:
                params: list[tuple[str, str | int]] = [
                    ("station", code),
                    ("data", "tmpc"),
                    ("data", "dwpc"),
                    ("data", "relh"),
                    ("data", "sknt"),
                    ("data", "gust"),
                    ("data", "drct"),
                    ("data", "p01m"),
                    ("data", "alti"),
                    ("sts", start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
                    ("ets", end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
                    ("tz", "Etc/UTC"),
                    ("format", "onlycomma"),
                    ("latlon", "no"),
                    ("elev", "no"),
                    ("missing", "null"),
                    ("trace", "0.05"),
                    ("direct", "no"),
                    ("report_type", "3"),
                ]
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 200 and "valid" in resp.text[:200]:
                        parsed = parse_iem_asos_csv(resp.text, station_id=stid)
                        if parsed:
                            return parsed
                except httpx.HTTPError:
                    continue
        return []
