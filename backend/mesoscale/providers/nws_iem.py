"""Nationwide US & Global geocoding, NWS/AWC live METAR & alerts, and IEM ASOS archive."""

from __future__ import annotations

import asyncio
import csv
import io
import re
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

_COORD_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_US_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_STREET_HINT_RE = re.compile(
    r"(^\d+\s+[A-Za-z0-9])|"
    r"\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|ct|court|"
    r"cir|circle|way|pkwy|parkway|pl|place|ter|terrace|trl|trail|hwy|highway|fwy|apt|unit)\b",
    re.IGNORECASE,
)

# Named NWS/SPC hail-stone diameter table (inches)
HAIL_NAMED_SIZES_IN: tuple[tuple[str, float], ...] = (
    ("softball", 4.00),
    ("grapefruit", 4.00),
    ("baseball", 2.75),
    ("tennis ball", 2.50),
    ("hen egg", 2.00),
    ("lime", 2.00),
    ("golf ball", 1.75),
    ("golfball", 1.75),
    ("ping pong", 1.50),
    ("walnut", 1.50),
    ("half dollar", 1.25),
    ("quarter", 1.00),
    ("nickel", 0.88),
    ("dime", 0.75),
    ("penny", 0.75),
    ("marble", 0.50),
    ("hazel", 0.50),
    ("pea", 0.25),
)

_HAIL_NUMERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"MAX\s+HAIL\s+SIZE\s*\.{2,}\s*(\d+(?:\.\d+)?)\s*IN", re.IGNORECASE),
    re.compile(r"HAIL\s*\.{2,}\s*(?:UP\s+TO\s+|<\s*)?(\d+(?:\.\d+)?)\s*IN", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?INCH\s+HAIL", re.IGNORECASE),
    re.compile(r"HAIL\s+UP\s+TO\s+(\d+(?:\.\d+)?)\s*INCH", re.IGNORECASE),
)

_WIND_NUMERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"MAX\s+WIND\s+GUST\s*\.{2,}\s*(\d+(?:\.\d+)?)\s*MPH", re.IGNORECASE),
    re.compile(r"WIND(?:\s+GUSTS?)?\s*\.{2,}\s*(?:UP\s+TO\s+)?(\d+(?:\.\d+)?)\s*MPH", re.IGNORECASE),
    re.compile(r"WIND\s+GUSTS?\s+(?:UP\s+TO\s+|OF\s+)?(\d+(?:\.\d+)?)\s*MPH", re.IGNORECASE),
    re.compile(r"(\d{2,3})\s*MPH\s+WIND\s+GUST", re.IGNORECASE),
)

US_STATE_TIMEZONES: dict[str, str] = {
    "OK": "America/Chicago",
    "TX": "America/Chicago",
    "KS": "America/Chicago",
    "NE": "America/Chicago",
    "SD": "America/Chicago",
    "ND": "America/Chicago",
    "MN": "America/Chicago",
    "IA": "America/Chicago",
    "MO": "America/Chicago",
    "AR": "America/Chicago",
    "LA": "America/Chicago",
    "WI": "America/Chicago",
    "IL": "America/Chicago",
    "MS": "America/Chicago",
    "AL": "America/Chicago",
    "TN": "America/Chicago",
    "CO": "America/Denver",
    "NM": "America/Denver",
    "WY": "America/Denver",
    "MT": "America/Denver",
    "UT": "America/Denver",
    "ID": "America/Boise",
    "AZ": "America/Phoenix",
    "CA": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
    "PR": "America/Puerto_Rico",
}


def hail_inches_to_label(inches: float | None) -> str:
    """Human-readable hail stone size comparison (e.g. '1.00\" (Quarter)')."""
    if inches is None or inches <= 0:
        return "None"
    if inches >= 2.75:
        name = "Baseball+"
    elif inches >= 2.00:
        name = "Hen Egg / Lime"
    elif inches >= 1.75:
        name = "Golf Ball"
    elif inches >= 1.50:
        name = "Ping Pong Ball"
    elif inches >= 1.25:
        name = "Half Dollar"
    elif inches >= 1.00:
        name = "Quarter"
    elif inches >= 0.88:
        name = "Nickel"
    elif inches >= 0.75:
        name = "Penny / Dime"
    elif inches >= 0.50:
        name = "Marble"
    else:
        name = "Pea"
    return f'{inches:.2f}" ({name})'


def parse_alert_hail_and_wind(props: dict[str, Any]) -> tuple[float | None, str, float | None]:
    """
    Extract maximum hail diameter (inches), hail threat tag, and max wind gust (mph)
    from NWS CAP `parameters` and Severe / Special Weather Statement (SPS) text.
    """
    params = props.get("parameters") or {}
    max_hail: float | None = None
    hail_threat = ""
    max_wind: float | None = None

    raw_hail = params.get("maxHailSize")
    if isinstance(raw_hail, list) and raw_hail:
        try:
            val = float(str(raw_hail[0]).strip().lstrip("<>"))
            if val > 0:
                max_hail = val
        except ValueError:
            pass

    raw_ht = params.get("hailThreat")
    if isinstance(raw_ht, list) and raw_ht:
        hail_threat = str(raw_ht[0]).strip()

    raw_wind = params.get("maxWindGust")
    if isinstance(raw_wind, list) and raw_wind:
        m = re.search(r"(\d+(?:\.\d+)?)", str(raw_wind[0]))
        if m:
            val = float(m.group(1))
            if "kt" in str(raw_wind[0]).lower():
                val *= 1.15078
            max_wind = round(val, 0)

    text_blob = " ".join(
        str(props.get(k) or "") for k in ("headline", "description", "instruction")
    )
    for pat in _HAIL_NUMERIC_PATTERNS:
        for m in pat.finditer(text_blob):
            try:
                v = float(m.group(1))
                if 0.1 <= v <= 7.0:
                    max_hail = max(max_hail or 0.0, v)
            except ValueError:
                continue

    lower_blob = text_blob.lower()
    if "hail" in lower_blob:
        for word, size_in in HAIL_NAMED_SIZES_IN:
            if word in lower_blob:
                max_hail = max(max_hail or 0.0, size_in)

    if max_wind is None:
        for pat in _WIND_NUMERIC_PATTERNS:
            m = pat.search(text_blob)
            if m:
                try:
                    max_wind = round(float(m.group(1)), 0)
                    break
                except ValueError:
                    continue

    return max_hail, hail_threat, max_wind


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


def _tz_for_us_state(state: str, lon: float) -> str:
    st = state.strip().upper()
    if st in US_STATE_TIMEZONES:
        return US_STATE_TIMEZONES[st]
    if len(st) == 2 and st.isalpha():
        return "America/New_York" if lon > -85.5 else "America/Chicago"
    return "America/Chicago"


def _pretty_census_address(raw: str) -> str:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return raw
    out = [parts[0].title()]
    if len(parts) >= 2:
        out.append(parts[1].title())
    for p in parts[2:]:
        toks = p.split()
        norm_toks = [t.upper() if len(t) == 2 and t.isalpha() else t.title() for t in toks]
        out.append(" ".join(norm_toks))
    return ", ".join(out)


class NwsIemClient:
    """Client for US/Global rooftop geocoding, NWS + AWC live METAR & alerts, and IEM ASOS."""

    def __init__(self, user_agent: str, timeout_s: float = 20.0) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/geo+json, application/json",
        }

    async def _search_us_census(
        self, client: httpx.AsyncClient, query: str
    ) -> list[GeoSearchResult]:
        url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        params = {
            "address": query,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
        try:
            resp = await client.get(url, params=params, timeout=8.0)
            if resp.status_code != 200:
                return []
            matches = ((resp.json().get("result") or {}).get("addressMatches")) or []
        except (httpx.HTTPError, ValueError):
            return []

        results: list[GeoSearchResult] = []
        for m in matches[:5]:
            coords = m.get("coordinates") or {}
            if coords.get("y") is None or coords.get("x") is None:
                continue
            lat = float(coords["y"])
            lon = float(coords["x"])
            comp = m.get("addressComponents") or {}
            matched_raw = str(m.get("matchedAddress") or query)
            pretty = _pretty_census_address(matched_raw)
            street_part = pretty.split(",")[0].strip()
            city = str(comp.get("city") or "").title()
            state = str(comp.get("state") or "").upper()
            short_name = f"{street_part}, {city}" if city else street_part
            results.append(
                GeoSearchResult(
                    name=short_name,
                    admin1=state,
                    country="US",
                    lat=lat,
                    lon=lon,
                    elevation_m=-9999.0,
                    timezone=_tz_for_us_state(state, lon),
                    display_address=pretty,
                    result_type="address",
                )
            )
        return results

    async def _search_nominatim(
        self, client: httpx.AsyncClient, query: str, *, is_us_zip: bool = False
    ) -> list[GeoSearchResult]:
        url = "https://nominatim.openstreetmap.org/search"
        params: dict[str, Any] = {
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 6,
        }
        if is_us_zip:
            params["postalcode"] = query
            params["countrycodes"] = "us"
        else:
            params["q"] = query

        try:
            resp = await client.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent, "Accept-Language": "en"},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            items = resp.json()
            if not isinstance(items, list):
                return []
        except (httpx.HTTPError, ValueError):
            return []

        out: list[GeoSearchResult] = []
        for item in items:
            try:
                lat = float(item["lat"])
                lon = float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            addr = item.get("address") or {}
            cc = str(addr.get("country_code") or "us").upper()
            house = str(addr.get("house_number") or "").strip()
            road = str(addr.get("road") or addr.get("pedestrian") or "").strip()
            city = str(
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("municipality")
                or addr.get("hamlet")
                or addr.get("suburb")
                or item.get("name")
                or ""
            ).strip()
            state = str(addr.get("state") or addr.get("ISO3166-2-lvl4") or "").strip()
            if state.startswith("US-"):
                state = state[3:]
            postcode = str(addr.get("postcode") or "").strip()
            disp = str(item.get("display_name") or "").strip()

            if is_us_zip:
                rtype = "postal"
                label_name = f"{city} ({postcode or query})" if city else query
            elif house and road:
                rtype = "address"
                label_name = f"{house} {road}" + (f", {city}" if city else "")
            elif road or item.get("category") in ("building", "tourism", "amenity", "place"):
                rtype = "address" if road else "city"
                base_nm = str(item.get("name") or road or city or query).strip()
                label_name = (
                    f"{base_nm}, {city}"
                    if city and city.lower() not in base_nm.lower()
                    else base_nm
                )
            else:
                rtype = "city"
                label_name = city or str(item.get("name") or query)

            tz = _tz_for_us_state(state, lon) if cc == "US" else ""
            out.append(
                GeoSearchResult(
                    name=label_name,
                    admin1=state,
                    country=cc,
                    lat=lat,
                    lon=lon,
                    elevation_m=-9999.0,
                    timezone=tz,
                    display_address=disp,
                    result_type=rtype,
                )
            )
        return out

    async def _search_open_meteo_gazetteer(
        self, client: httpx.AsyncClient, query: str
    ) -> list[GeoSearchResult]:
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": query, "count": 12, "language": "en", "format": "json"}
        try:
            resp = await client.get(url, params=params, timeout=8.0)
            if resp.status_code != 200:
                return []
            raw_results = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            return []

        us_first = sorted(
            raw_results,
            key=lambda r: (0 if r.get("country_code") == "US" else 1, -(r.get("population") or 0)),
        )
        out: list[GeoSearchResult] = []
        for item in us_first[:8]:
            name = str(item.get("name") or query)
            admin1 = str(item.get("admin1") or "")
            country = str(item.get("country_code") or "US").upper()
            country_full = str(item.get("country") or country)
            disp_parts = [p for p in (name, admin1, country_full) if p]
            out.append(
                GeoSearchResult(
                    name=name,
                    admin1=admin1,
                    country=country,
                    lat=float(item["latitude"]),
                    lon=float(item["longitude"]),
                    elevation_m=float(item["elevation"])
                    if item.get("elevation") is not None
                    else -9999.0,
                    timezone=str(item.get("timezone") or "America/Chicago"),
                    display_address=", ".join(disp_parts),
                    result_type="city",
                )
            )
        return out

    async def _hydrate_elevations_and_timezones(
        self, client: httpx.AsyncClient, items: list[GeoSearchResult]
    ) -> None:
        if not items:
            return
        need_elev = [it for it in items if it.elevation_m <= -9000.0]
        if need_elev:
            try:
                resp = await client.get(
                    "https://api.open-meteo.com/v1/elevation",
                    params={
                        "latitude": ",".join(f"{it.lat:.5f}" for it in need_elev),
                        "longitude": ",".join(f"{it.lon:.5f}" for it in need_elev),
                    },
                    timeout=6.0,
                )
                if resp.status_code == 200:
                    elevs = resp.json().get("elevation") or []
                    for it, ev in zip(need_elev, elevs, strict=False):
                        if ev is not None:
                            it.elevation_m = round(float(ev), 1)
            except (httpx.HTTPError, ValueError):
                pass
        for it in items:
            if it.elevation_m <= -9000.0:
                it.elevation_m = 250.0

        # Hydrate timezone for any international Nominatim hit lacking IANA tz (e.g. Aruba -> America/Aruba)
        need_tz = [it for it in items if not it.timezone]
        for it in need_tz[:3]:
            try:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": round(it.lat, 4),
                        "longitude": round(it.lon, 4),
                        "timezone": "auto",
                        "forecast_days": 1,
                    },
                    timeout=5.0,
                )
                if r.status_code == 200:
                    tz = r.json().get("timezone")
                    if tz:
                        it.timezone = str(tz)
            except (httpx.HTTPError, ValueError):
                pass
            if not it.timezone:
                it.timezone = "UTC"

    async def geocode_search(self, query: str, limit: int = 8) -> list[GeoSearchResult]:
        """
        4-tier zero-API-key US & global rooftop geocoder:
        1) Direct `lat, lon` coordinates
        2) U.S. Census Bureau TIGER Street Geocoder (exact US street addresses)
        3) OpenStreetMap Nominatim (US ZIPs, newer subdivisions, resorts, global addresses)
        4) Open-Meteo Global Gazetteer (cities, towns, islands such as Aruba)
        """
        q = query.strip()
        if not q:
            return []

        coord_match = _COORD_RE.match(q)
        if coord_match:
            lat = float(coord_match.group(1))
            lon = float(coord_match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                meta = await self.reverse_point_metadata(lat, lon)
                meta.result_type = "coordinates"
                if not meta.display_address:
                    meta.display_address = f"{lat:.4f}, {lon:.4f}"
                return [meta]

        is_zip = bool(_US_ZIP_RE.match(q))
        looks_like_street = bool(_STREET_HINT_RE.search(q)) or ("," in q and any(ch.isdigit() for ch in q))

        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            tasks: list[Any] = []
            if is_zip:
                tasks.append(self._search_nominatim(client, q, is_us_zip=True))
                tasks.append(self._search_open_meteo_gazetteer(client, q))
            elif looks_like_street:
                tasks.append(self._search_us_census(client, q))
                tasks.append(self._search_nominatim(client, q, is_us_zip=False))
            else:
                tasks.append(self._search_open_meteo_gazetteer(client, q))
                if "," in q or len(q.split()) >= 2:
                    tasks.append(self._search_nominatim(client, q, is_us_zip=False))

            batches = await asyncio.gather(*tasks)
            combined: list[GeoSearchResult] = [item for batch in batches for item in batch]

            # If Open-Meteo gazetteer returned nothing (e.g. specific landmark/resort name), try Nominatim
            if not combined and not is_zip and not looks_like_street:
                combined = await self._search_nominatim(client, q, is_us_zip=False)

            type_rank = {"address": 0, "coordinates": 1, "postal": 2, "city": 3}
            combined.sort(
                key=lambda r: (
                    type_rank.get(r.result_type, 4),
                    0 if r.country == "US" else 1,
                )
            )

            deduped: list[GeoSearchResult] = []
            for cand in combined:
                if any(
                    haversine_km(cand.lat, cand.lon, ex.lat, ex.lon) < 0.20
                    for ex in deduped
                ):
                    continue
                deduped.append(cand)
                if len(deduped) >= limit:
                    break

            await self._hydrate_elevations_and_timezones(client, deduped)
            return deduped

    async def reverse_point_metadata(self, lat: float, lon: float) -> GeoSearchResult:
        """Resolve street/city name, country, 90m elevation, and IANA timezone for raw GPS/map-tap points."""
        name = f"{lat:.4f}, {lon:.4f}"
        admin1 = ""
        country = "US"
        tz = ""
        elev_m = 250.0
        display_address = ""
        result_type = "coordinates"

        async with httpx.AsyncClient(
            timeout=10.0, headers=self._headers(), follow_redirects=True
        ) as client:
            # 1. NWS Point Metadata (authoritative US city/state/timezone)
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
            except (httpx.HTTPError, ValueError):
                pass

            # 2. Nominatim Reverse Rooftop Lookup (street address or international island/city like Aruba)
            try:
                nr = await client.get(
                    "https://nominatim.openstreetmap.org/reverse",
                    params={"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "zoom": 18},
                    headers={"User-Agent": self.user_agent, "Accept-Language": "en"},
                    timeout=6.0,
                )
                if nr.status_code == 200:
                    ndata = nr.json()
                    addr = ndata.get("address") or {}
                    cc = str(addr.get("country_code") or "").upper()
                    if cc:
                        country = cc
                    house = str(addr.get("house_number") or "").strip()
                    road = str(addr.get("road") or "").strip()
                    city = str(
                        addr.get("city")
                        or addr.get("town")
                        or addr.get("village")
                        or addr.get("municipality")
                        or ""
                    ).strip()
                    st_val = str(addr.get("state") or "").strip()
                    if not admin1 and st_val:
                        admin1 = st_val
                    if ndata.get("display_name"):
                        display_address = str(ndata["display_name"])
                    if house and road:
                        name = f"{house} {road}" + (f", {city}" if city else "")
                        result_type = "address"
                    elif road and city:
                        name = f"{road}, {city}"
                        result_type = "address"
                    elif city and name.startswith(f"{lat:.4f}"):
                        name = city
                        result_type = "city"
            except (httpx.HTTPError, ValueError):
                pass

            # 3. 90-meter Copernicus DEM Elevation + Auto Timezone fallback (for non-US coordinates)
            try:
                er = await client.get(
                    "https://api.open-meteo.com/v1/elevation",
                    params={"latitude": lat, "longitude": lon},
                    timeout=6.0,
                )
                if er.status_code == 200:
                    el_list = er.json().get("elevation") or []
                    if el_list and el_list[0] is not None:
                        elev_m = round(float(el_list[0]), 1)
            except (httpx.HTTPError, ValueError):
                pass

            if not tz:
                try:
                    tr = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params={
                            "latitude": round(lat, 4),
                            "longitude": round(lon, 4),
                            "timezone": "auto",
                            "forecast_days": 1,
                        },
                        timeout=5.0,
                    )
                    if tr.status_code == 200 and tr.json().get("timezone"):
                        tz = str(tr.json()["timezone"])
                except (httpx.HTTPError, ValueError):
                    pass

        return GeoSearchResult(
            name=name,
            admin1=admin1,
            country=country or "US",
            lat=lat,
            lon=lon,
            elevation_m=elev_m,
            timezone=tz or "America/Chicago",
            display_address=display_address or f"{lat:.4f}, {lon:.4f}",
            result_type=result_type,
        )

    async def _find_global_awc_metar_stations(
        self, client: httpx.AsyncClient, lat: float, lon: float, limit: int = 3
    ) -> list[StationMeta]:
        """
        Zero-API-key international ICAO airport METAR discovery via NOAA Aviation Weather Center
        (e.g., discovers `TNCA` Queen Beatrix Int'l Airport when pinning Aruba!).
        """
        lat_min = max(-89.9, lat - 1.25)
        lat_max = min(89.9, lat + 1.25)
        lon_min = max(-179.9, lon - 1.25)
        lon_max = min(179.9, lon + 1.25)
        url = "https://aviationweather.gov/api/data/metar"
        params = {
            "bbox": f"{lat_min:.3f},{lon_min:.3f},{lat_max:.3f},{lon_max:.3f}",
            "format": "json",
            "hours": 3,
        }
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            if resp.status_code != 200:
                return []
            rows = resp.json()
            if not isinstance(rows, list):
                return []
        except (httpx.HTTPError, ValueError):
            return []

        seen: set[str] = set()
        candidates: list[StationMeta] = []
        for item in rows:
            stid = str(item.get("icaoId") or "").strip().upper()
            if len(stid) != 4 or stid in seen:
                continue
            try:
                slat = float(item["lat"])
                slon = float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            seen.add(stid)
            elev_m = float(item.get("elev") or 20.0)
            dist = haversine_km(lat, lon, slat, slon)
            if dist <= 130.0:
                candidates.append(
                    StationMeta(
                        station_id=stid,
                        name=str(item.get("name") or stid),
                        source="nws_asos",
                        lat=slat,
                        lon=slon,
                        elevation_m=elev_m,
                        distance_km=round(dist, 2),
                    )
                )
        candidates.sort(key=lambda s: s.distance_km)
        return candidates[:limit]

    async def find_nearest_asos_stations(
        self, lat: float, lon: float, limit: int = 3
    ) -> list[StationMeta]:
        candidates: list[StationMeta] = []
        async with httpx.AsyncClient(
            timeout=self.timeout_s, headers=self._headers(), follow_redirects=True
        ) as client:
            features: list[dict[str, Any]] = []
            try:
                pt_resp = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
                if pt_resp.status_code == 200:
                    stations_url = (pt_resp.json().get("properties") or {}).get(
                        "observationStations"
                    )
                    if stations_url:
                        st_resp = await client.get(stations_url)
                        if st_resp.status_code == 200:
                            features = st_resp.json().get("features") or []
            except (httpx.HTTPError, ValueError):
                features = []

            for feat in features[:18]:
                props = feat.get("properties") or {}
                geom = feat.get("geometry") or {}
                coords = geom.get("coordinates") or []
                stid = str(props.get("stationIdentifier") or "").upper()
                if len(coords) < 2 or not stid:
                    continue
                # Support 4-char US ICAO & alphanumeric FAA AWOS (Kxxx, K1V6, PAxx, PHxx, TJxx)
                if len(stid) != 4 or stid[0] not in ("K", "P", "T") or not stid.isalnum():
                    continue
                slon, slat = float(coords[0]), float(coords[1])
                elev_prop = props.get("elevation") or {}
                elev_m = float(elev_prop.get("value") or 300.0)
                dist = haversine_km(lat, lon, slat, slon)
                if dist <= 95.0:
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

            if not candidates:
                candidates = await self._find_global_awc_metar_stations(
                    client, lat, lon, limit=limit
                )

        candidates.sort(key=lambda s: s.distance_km)
        return candidates[:limit]

    async def _fetch_awc_metar_observations(
        self, client: httpx.AsyncClient, station_id: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        url = "https://aviationweather.gov/api/data/metar"
        params = {"ids": station_id.upper(), "format": "json", "hours": 6}
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            if resp.status_code != 200:
                return []
            rows = resp.json()
            if not isinstance(rows, list):
                return []
        except (httpx.HTTPError, ValueError):
            return []

        def _row_num(row_obj: dict[str, Any], k: str) -> float | None:
            v = row_obj.get(k)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        out: list[dict[str, Any]] = []
        for r in rows[:limit]:
            obs_time = r.get("obsTime")
            report_iso = r.get("reportTime")
            dt: datetime | None = None
            if obs_time is not None:
                try:
                    dt = datetime.fromtimestamp(int(obs_time), tz=UTC)
                except (TypeError, ValueError):
                    dt = None
            if dt is None and report_iso:
                try:
                    dt = datetime.fromisoformat(str(report_iso).replace("Z", "+00:00"))
                except ValueError:
                    continue
            if dt is None:
                continue

            wspd_kt = _row_num(r, "wspd")
            wgst_kt = _row_num(r, "wgst")
            precip_in = _row_num(r, "precip")
            out.append(
                {
                    "station_id": station_id.upper(),
                    "source": "nws_asos",
                    "valid_time_utc": dt,
                    "temp_c": _row_num(r, "temp"),
                    "dewpoint_c": _row_num(r, "dewp"),
                    "rel_humidity_pct": None,
                    "wind_ms": wspd_kt * 0.514444 if wspd_kt is not None else None,
                    "gust_ms": wgst_kt * 0.514444 if wgst_kt is not None else None,
                    "wind_dir_deg": _row_num(r, "wdir"),
                    "precip_accum_mm": None,
                    "precip_1h_mm": max(0.0, precip_in * 25.4) if precip_in is not None else None,
                    "pressure_hpa": _row_num(r, "altim"),
                    "solar_wm2": None,
                }
            )
        return out

    async def fetch_latest_asos_observations(
        self, station_id: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        stid = station_id.upper()
        url = f"https://api.weather.gov/stations/{stid}/observations"
        async with httpx.AsyncClient(
            timeout=self.timeout_s, headers=self._headers(), follow_redirects=True
        ) as client:
            features: list[dict[str, Any]] = []
            if stid.startswith(("K", "P", "T")):
                try:
                    resp = await client.get(url, params={"limit": limit})
                    if resp.status_code == 200:
                        features = resp.json().get("features") or []
                except (httpx.HTTPError, ValueError):
                    features = []

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
                        "station_id": stid,
                        "source": "nws_asos",
                        "valid_time_utc": dt,
                        "temp_c": _convert_nws_quantity(props.get("temperature"), "c"),
                        "dewpoint_c": _convert_nws_quantity(props.get("dewpoint"), "c"),
                        "rel_humidity_pct": _convert_nws_quantity(
                            props.get("relativeHumidity"), "pct"
                        ),
                        "wind_ms": _convert_nws_quantity(props.get("windSpeed"), "ms"),
                        "gust_ms": _convert_nws_quantity(props.get("windGust"), "ms"),
                        "wind_dir_deg": _convert_nws_quantity(props.get("windDirection"), "deg"),
                        "precip_accum_mm": None,
                        "precip_1h_mm": _convert_nws_quantity(
                            props.get("precipitationLastHour"), "mm"
                        ),
                        "pressure_hpa": _convert_nws_quantity(
                            props.get("barometricPressure"), "hpa"
                        ),
                        "solar_wm2": None,
                    }
                )

            if not records:
                records = await self._fetch_awc_metar_observations(client, stid, limit=limit)
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
            except (httpx.HTTPError, ValueError):
                return []

        alerts: list[NwsAlert] = []
        for feat in features:
            props = feat.get("properties") or {}
            max_hail_in, hail_threat, max_wind_mph = parse_alert_hail_and_wind(props)
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
                    max_hail_in=max_hail_in,
                    hail_threat=hail_threat,
                    max_wind_mph=max_wind_mph,
                )
            )
        return alerts

    async def fetch_iem_historical_asos(
        self, station_id: str, start_utc: datetime, end_utc: datetime
    ) -> list[dict[str, Any]]:
        """Download historical hourly ASOS/METAR observations from Iowa Environmental Mesonet."""
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
