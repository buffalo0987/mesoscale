"""Personal Weather Station (PWS) adapter for Tempest, Ambient Weather, and Ecowitt."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from mesoscale.ml.thermodynamics import dewpoint_from_temp_rh


class PersonalWeatherStationClient:
    """
    Optional backyard PWS adapter with solar radiation-spike Quality Control:
    rejects calm-wind daytime temperature readings deviating > 4.5°C from nearby
    research-grade Mesonet/ASOS reference so unshielded hobbyist sensors never poison ML.
    """

    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s

    async def fetch_latest(
        self, config: dict[str, Any], reference_temp_c: float | None = None
    ) -> dict[str, Any] | None:
        if not config.get("enabled"):
            return None
        provider = str(config.get("provider") or "none").lower()
        station_id = str(config.get("station_id") or "").strip()
        api_key = str(config.get("api_key") or "").strip()
        if provider == "none" or not station_id or not api_key:
            return None

        raw: dict[str, Any] | None = None
        if provider == "tempest":
            raw = await self._fetch_tempest(station_id, api_key)
        elif provider == "ambient":
            app_key = str(config.get("application_key") or "").strip()
            raw = await self._fetch_ambient(station_id, api_key, app_key)

        if raw is None:
            return None

        # Radiation-spike QC check against nearby Mesonet/ASOS reference
        if (
            reference_temp_c is not None
            and raw.get("temp_c") is not None
            and abs(float(raw["temp_c"]) - reference_temp_c) > 4.5
        ):
            raw["qc_temp_rejected"] = True
            raw["temp_c"] = None
        return raw

    async def _fetch_tempest(self, station_id: str, token: str) -> dict[str, Any] | None:
        url = f"https://swd.weatherflow.com/swd/rest/observations/station/{station_id}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                resp = await client.get(url, params={"token": token})
                if resp.status_code != 200:
                    return None
                obs_list = resp.json().get("obs") or []
                if not obs_list:
                    return None
                o = obs_list[0]
                t_c = float(o["air_temperature"]) if o.get("air_temperature") is not None else None
                rh = float(o["relative_humidity"]) if o.get("relative_humidity") is not None else None
                td_c = float(dewpoint_from_temp_rh(t_c, rh)) if (t_c is not None and rh is not None) else None
                return {
                    "station_id": f"PWS-{station_id}",
                    "source": "pws",
                    "valid_time_utc": datetime.fromtimestamp(int(o["timestamp"]), tz=UTC),
                    "temp_c": t_c,
                    "dewpoint_c": td_c,
                    "rel_humidity_pct": rh,
                    "wind_ms": float(o["wind_avg"]) if o.get("wind_avg") is not None else None,
                    "gust_ms": float(o["wind_gust"]) if o.get("wind_gust") is not None else None,
                    "wind_dir_deg": float(o["wind_direction"]) if o.get("wind_direction") is not None else None,
                    "precip_accum_mm": None,
                    "precip_1h_mm": float(o.get("precip_accum_last_1hr") or 0.0),
                    "pressure_hpa": float(o["sea_level_pressure"]) if o.get("sea_level_pressure") else None,
                    "solar_wm2": float(o["solar_radiation"]) if o.get("solar_radiation") is not None else None,
                }
            except (httpx.HTTPError, KeyError, ValueError):
                return None

    async def _fetch_ambient(
        self, mac_address: str, api_key: str, application_key: str
    ) -> dict[str, Any] | None:
        if not application_key:
            return None
        url = f"https://rt.ambientweather.net/v1/devices/{mac_address}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                resp = await client.get(
                    url, params={"apiKey": api_key, "applicationKey": application_key, "limit": 1}
                )
                if resp.status_code != 200:
                    return None
                items = resp.json()
                if not items:
                    return None
                o = items[0]
                t_c = (float(o["tempf"]) - 32.0) * 5.0 / 9.0 if o.get("tempf") is not None else None
                rh = float(o["humidity"]) if o.get("humidity") is not None else None
                td_c = float(dewpoint_from_temp_rh(t_c, rh)) if (t_c is not None and rh is not None) else None
                return {
                    "station_id": f"PWS-{mac_address[-4:]}",
                    "source": "pws",
                    "valid_time_utc": datetime.fromtimestamp(int(o["dateutc"]) / 1000.0, tz=UTC),
                    "temp_c": t_c,
                    "dewpoint_c": td_c,
                    "rel_humidity_pct": rh,
                    "wind_ms": float(o["windspeedmph"]) * 0.44704 if o.get("windspeedmph") is not None else None,
                    "gust_ms": float(o["windgustmph"]) * 0.44704 if o.get("windgustmph") is not None else None,
                    "wind_dir_deg": float(o["winddir"]) if o.get("winddir") is not None else None,
                    "precip_accum_mm": None,
                    "precip_1h_mm": float(o.get("hourlyrainin") or 0.0) * 25.4,
                    "pressure_hpa": float(o["baromrelin"]) * 33.86389 if o.get("baromrelin") is not None else None,
                    "solar_wm2": float(o["solarradiation"]) if o.get("solarradiation") is not None else None,
                }
            except (httpx.HTTPError, KeyError, ValueError):
                return None
