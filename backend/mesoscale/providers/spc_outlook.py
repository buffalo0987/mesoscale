"""NOAA Storm Prediction Center (SPC) Day 1-3 Categorical Convective Outlooks GeoJSON adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

SPC_RISK_RANK: dict[str, int] = {
    "NONE": 0,
    "TSTM": 1,
    "MRGL": 2,
    "SLGT": 3,
    "ENH": 4,
    "MDT": 5,
    "HIGH": 6,
}

SPC_RISK_META: dict[str, dict[str, str]] = {
    "NONE": {"label": "No Severe Risk", "color": "#475569"},
    "TSTM": {"label": "General Thunderstorms", "color": "#86efac"},
    "MRGL": {"label": "Marginal Risk (1/5)", "color": "#22c55e"},
    "SLGT": {"label": "Slight Risk (2/5)", "color": "#eab308"},
    "ENH": {"label": "Enhanced Risk (3/5)", "color": "#f97316"},
    "MDT": {"label": "Moderate Risk (4/5)", "color": "#ef4444"},
    "HIGH": {"label": "High Risk (5/5)", "color": "#ec4899"},
}

# NOAA CloudGIS MapServer layer IDs for Day 1 (layer 1), Day 2 (layer 9), Day 3 (layer 17) Categorical
SPC_URLS_BY_DAY: dict[int, tuple[str, ...]] = {
    1: (
        "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson",
        "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer/1/query?where=1%3D1&outFields=DN,LABEL,LABEL2,ISSUE,EXPIRE,FILL,STROKE&f=geojson",
    ),
    2: (
        "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.nolyr.geojson",
        "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer/9/query?where=1%3D1&outFields=DN,LABEL,LABEL2,ISSUE,EXPIRE,FILL,STROKE&f=geojson",
    ),
    3: (
        "https://www.spc.noaa.gov/products/outlook/day3otlk_cat.nolyr.geojson",
        "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer/17/query?where=1%3D1&outFields=DN,LABEL,LABEL2,ISSUE,EXPIRE,FILL,STROKE&f=geojson",
    ),
}


SPC_DAY1_HAIL_URLS: tuple[str, ...] = (
    "https://www.spc.noaa.gov/products/outlook/day1otlk_hail.nolyr.geojson",
    "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer/5/query?where=1%3D1&outFields=DN,LABEL,LABEL2,ISSUE,EXPIRE,FILL,STROKE&f=geojson",
)


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        intersect = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersect:
            inside = not inside
        j = i
    return inside


def _point_in_geometry(lon: float, lat: float, geom: dict[str, Any] | None) -> bool:
    if not geom:
        return False
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        if not coords:
            return False
        if not _point_in_ring(lon, lat, coords[0]):
            return False
        for hole in coords[1:]:
            if _point_in_ring(lon, lat, hole):
                return False
        return True
    if gtype == "MultiPolygon":
        for poly in coords:
            if poly and _point_in_ring(lon, lat, poly[0]):
                in_hole = any(_point_in_ring(lon, lat, hole) for hole in poly[1:])
                if not in_hole:
                    return True
    return False


def evaluate_point_spc_risk(geojson: dict[str, Any], lat: float, lon: float) -> dict[str, Any]:
    """Find highest categorical SPC risk polygon containing (lat, lon)."""
    best_code = "NONE"
    for feat in geojson.get("features") or []:
        props = feat.get("properties") or {}
        code = str(props.get("LABEL") or "").strip().upper()
        if code not in SPC_RISK_RANK:
            continue
        if (
            _point_in_geometry(lon, lat, feat.get("geometry"))
            and SPC_RISK_RANK[code] > SPC_RISK_RANK[best_code]
        ):
            best_code = code
    meta = SPC_RISK_META[best_code]
    return {
        "code": best_code,
        "rank": SPC_RISK_RANK[best_code],
        "label": meta["label"],
        "color": meta["color"],
    }


def evaluate_point_spc_hail_risk(geojson: dict[str, Any], lat: float, lon: float) -> dict[str, Any]:
    """Evaluate SPC Day 1 Probabilistic Hail % (5..60%) and Significant Hail (SIGN >= 2 in) at (lat, lon)."""
    best_prob = 0
    sig_hail = False
    for feat in geojson.get("features") or []:
        if not _point_in_geometry(lon, lat, feat.get("geometry")):
            continue
        props = feat.get("properties") or {}
        label = str(props.get("LABEL") or "").strip().upper()
        dn_raw = props.get("DN")
        if label in ("SIGN", "CIG1", "CIG2", "CIG3"):
            sig_hail = True
            continue
        for candidate in (dn_raw, label.rstrip("%")):
            if candidate is None:
                continue
            try:
                pct = int(float(str(candidate).strip()))
                if 0 < pct <= 100:
                    best_prob = max(best_prob, pct)
            except ValueError:
                continue

    if best_prob == 0 and not sig_hail:
        desc = "Low (<5%)"
    elif sig_hail:
        desc = f"{best_prob}% Prob • Hatched Significant Hail (≥2\")"
    else:
        desc = f"{best_prob}% Day 1 Hail Risk"
    return {
        "prob_pct": best_prob,
        "sig_hail": sig_hail,
        "label": desc,
    }


class SpcOutlookClient:
    """Cached fetcher for NOAA SPC Day 1-3 Convective & Day 1 Hail Outlook GeoJSON layers."""

    def __init__(self, ttl_seconds: float = 900.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[int | str, tuple[float, dict[str, Any]]] = {}

    async def get_day1_hail_geojson(self) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get("day1_hail")
        if cached and (now - cached[0]) < self.ttl_seconds:
            return cached[1]

        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            for url in SPC_DAY1_HAIL_URLS:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("type") == "FeatureCollection":
                            self._cache["day1_hail"] = (now, data)
                            return data
                except (httpx.HTTPError, ValueError):
                    continue

        empty = {"type": "FeatureCollection", "features": []}
        self._cache["day1_hail"] = (now, empty)
        return empty

    async def get_day_geojson(self, day: int = 1) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(day)
        if cached and (now - cached[0]) < self.ttl_seconds:
            return cached[1]

        urls = SPC_URLS_BY_DAY.get(day, SPC_URLS_BY_DAY[1])
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("type") == "FeatureCollection":
                            # Enrich features with canonical color for MapLibre rendering
                            for feat in data.get("features") or []:
                                props = feat.setdefault("properties", {})
                                code = str(props.get("LABEL") or "NONE").strip().upper()
                                meta = SPC_RISK_META.get(code, SPC_RISK_META["NONE"])
                                props["mesoscale_color"] = meta["color"]
                                props["mesoscale_label"] = meta["label"]
                            self._cache[day] = (now, data)
                            return data
                except (httpx.HTTPError, ValueError):
                    continue

        empty = {"type": "FeatureCollection", "features": []}
        self._cache[day] = (now, empty)
        return empty

    async def get_outlook_summary_for_point(self, lat: float, lon: float) -> dict[str, Any]:
        d1, d2, d3, d1_hail = await asyncio.gather(
            self.get_day_geojson(1),
            self.get_day_geojson(2),
            self.get_day_geojson(3),
            self.get_day1_hail_geojson(),
        )
        r1 = evaluate_point_spc_risk(d1, lat, lon)
        r2 = evaluate_point_spc_risk(d2, lat, lon)
        r3 = evaluate_point_spc_risk(d3, lat, lon)
        h1 = evaluate_point_spc_hail_risk(d1_hail, lat, lon)
        max_rank = max(r1["rank"], r2["rank"], r3["rank"])
        return {
            "day1": r1,
            "day2": r2,
            "day3": r3,
            "day1_hail": h1,
            "has_severe_risk": max_rank >= SPC_RISK_RANK["MRGL"] or h1["prob_pct"] >= 5,
            "has_thunder_risk": max_rank >= SPC_RISK_RANK["TSTM"] or h1["prob_pct"] >= 5,
        }
