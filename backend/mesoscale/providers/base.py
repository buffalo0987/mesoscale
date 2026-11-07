"""Core data structures and geodesic helpers for weather providers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two WGS84 coordinates."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(slots=True)
class StationMeta:
    station_id: str
    name: str
    source: str  # 'ok_mesonet' | 'nws_asos' | 'pws'
    lat: float
    lon: float
    elevation_m: float
    distance_km: float = 0.0
    blend_weight: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GeoSearchResult:
    name: str
    admin1: str
    country: str
    lat: float
    lon: float
    elevation_m: float
    timezone: str
    display_address: str = ""
    result_type: str = "city"  # 'address' | 'coordinates' | 'postal' | 'city'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NwsAlert:
    id: str
    event: str
    severity: str
    certainty: str
    urgency: str
    headline: str
    description: str
    instruction: str
    onset_utc: str | None
    expires_utc: str | None
    geometry: dict[str, Any] | None
    max_hail_in: float | None = None
    hail_threat: str = ""
    max_wind_mph: float | None = None

    @property
    def is_life_threatening(self) -> bool:
        ev = self.event.lower()
        sev = self.severity.lower()
        return (
            "tornado warning" in ev
            or "flash flood emergency" in ev
            or ("severe thunderstorm warning" in ev and sev == "extreme")
            or sev == "extreme"
        )

    @property
    def has_hail_threat(self) -> bool:
        if self.max_hail_in is not None and self.max_hail_in > 0.0:
            return True
        combined = f"{self.headline} {self.description} {self.instruction}".lower()
        return "hail" in combined

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_life_threatening"] = self.is_life_threatening
        d["has_hail_threat"] = self.has_hail_threat
        return d


@dataclass(slots=True)
class LatestStationReading:
    station_id: str
    name: str
    source: str
    lat: float
    lon: float
    elevation_m: float
    valid_time_utc: datetime
    temp_c: float | None
    dewpoint_c: float | None
    wind_ms: float | None
    gust_ms: float | None
    wind_dir_deg: float | None
    precip_1h_mm: float | None
    pressure_hpa: float | None
    distance_km: float = 0.0
