"""Extension plugin hook for direct NOAA AWS S3 GRIB2 ingestion via Herbie."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class RawGribConvectiveHook(Protocol):
    """
    Drop-in interface for optional raw GRIB2 extraction (e.g., via `herbie-data` + `cfgrib`
    against `s3://noaa-hrrr-bdp-pds` and `s3://noaa-rrfs-ops-pds`) when sub-grid severe
    fields such as 0-3 km Storm-Relative Helicity (`HLCY`), Updraft Helicity (`MXUPHL`),
    or Composite Simulated Reflectivity (`REFC`) are desired beyond Open-Meteo JSON fields.
    """

    async def fetch_convective_fields(
        self, lat: float, lon: float, cycle_utc: datetime, model: str = "hrrr"
    ) -> dict[int, dict[str, float]]: ...


class NoOpGribHook:
    """Default v1 no-op implementation (JSON CAPE/CIN/Freezing-Level active via OpenMeteoClient)."""

    async def fetch_convective_fields(
        self, lat: float, lon: float, cycle_utc: datetime, model: str = "hrrr"
    ) -> dict[int, dict[str, float]]:
        return {}
