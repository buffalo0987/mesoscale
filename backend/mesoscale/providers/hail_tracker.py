"""Real-time WSR-88D NEXRAD Level-III Storm Cell Hail Tracker & Driveway ETA Engine."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from mesoscale.providers.base import NwsAlert, haversine_km
from mesoscale.providers.nws_iem import hail_inches_to_label

IEM_NEXRAD_ATTR_URL = "https://mesonet.agron.iastate.edu/geojson/nexrad_attr.geojson"

COMPASS_16 = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def bearing_to_compass(deg: float) -> str:
    idx = int(((deg % 360.0) + 11.25) // 22.5) % 16
    return COMPASS_16[idx]


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing clockwise from North from point 1 -> point 2."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _project_latlon(lat: float, lon: float, heading_deg: float, dist_km: float) -> tuple[float, float]:
    """Project (lat, lon) forward along `heading_deg` by `dist_km`."""
    hr = math.radians(heading_deg)
    dlat = (dist_km * math.cos(hr)) / 110.574
    cos_lat = max(0.15, math.cos(math.radians(lat)))
    dlon = (dist_km * math.sin(hr)) / (111.320 * cos_lat)
    return round(lat + dlat, 5), round(lon + dlon, 5)


@dataclass(slots=True)
class NexradHailCell:
    cell_key: str
    nexrad_id: str
    storm_id: str
    lat: float
    lon: float
    poh_pct: int
    posh_pct: int
    max_size_in: float
    effective_hail_in: float
    motion_from_deg: float
    heading_deg: float
    speed_kt: float
    speed_mph: float
    vil_kg_m2: float
    valid_utc: str


@dataclass(slots=True)
class HailCellThreat:
    cell_key: str
    nexrad_id: str
    storm_id: str
    lat: float
    lon: float
    distance_km: float
    distance_mi: float
    bearing_from_target: str
    speed_mph: float
    heading_deg: float
    eta_minutes: int
    cpa_miss_km: float
    poh_pct: int
    posh_pct: int
    max_hail_in: float
    hail_label: str
    in_arrival_cone: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prop_float(props: dict[str, Any], key: str, default: float = 0.0) -> float:
    raw = props.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def parse_nexrad_attr_geojson(payload: dict[str, Any]) -> list[NexradHailCell]:
    """
    Parse IEM `nexrad_attr.geojson` Level-III storm cell attributes, keeping cells with
    non-trivial hail signatures (`max_size > 0`, `posh >= 10`, or `poh >= 40`).
    Note: WSR-88D `drct` is meteorological direction *from* which the cell moves, so
    forward storm motion heading = `(drct + 180) % 360`.
    """
    features = payload.get("features") or []
    cells: list[NexradHailCell] = []
    for feat in features:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue

        props = feat.get("properties") or {}
        poh = int(max(0.0, _prop_float(props, "poh", 0.0)))
        posh = int(max(0.0, _prop_float(props, "posh", 0.0)))
        max_sz = max(0.0, _prop_float(props, "max_size", 0.0))
        vil = max(0.0, _prop_float(props, "vil", 0.0))

        if max_sz <= 0.0 and posh < 10 and poh < 40:
            continue

        if max_sz > 0.0:
            eff_in = max_sz
        elif posh >= 50:
            eff_in = 1.00
        elif posh >= 30:
            eff_in = 0.75
        elif poh >= 70:
            eff_in = 0.50
        else:
            eff_in = 0.25

        drct = _prop_float(props, "drct", 240.0) % 360.0
        heading = (drct + 180.0) % 360.0
        sknt = max(0.0, _prop_float(props, "sknt", 0.0))
        nexrad = str(props.get("nexrad") or "RAD").strip().upper()
        storm_id = str(props.get("storm_id") or "CELL").strip().upper()
        valid_utc = str(props.get("valid") or "")

        cells.append(
            NexradHailCell(
                cell_key=f"{nexrad}:{storm_id}",
                nexrad_id=nexrad,
                storm_id=storm_id,
                lat=lat,
                lon=lon,
                poh_pct=poh,
                posh_pct=posh,
                max_size_in=round(max_sz, 2),
                effective_hail_in=round(eff_in, 2),
                motion_from_deg=round(drct, 0),
                heading_deg=round(heading, 0),
                speed_kt=round(sknt, 1),
                speed_mph=round(sknt * 1.15078, 1),
                vil_kg_m2=round(vil, 1),
                valid_utc=valid_utc,
            )
        )
    return cells


def evaluate_hail_risk_for_point(
    *,
    lat: float,
    lon: float,
    cells: list[NexradHailCell],
    alerts: list[NwsAlert] | None = None,
    spc_hail: dict[str, Any] | None = None,
    min_hail_in: float = 0.50,
    lead_window_minutes: int = 45,
) -> dict[str, Any]:
    """
    3-Stage "Truck-in-the-Garage" Hail Risk & Driveway ETA Evaluator:
    1) Vector CPA & `18°` storm-motion swath cone across live NEXRAD hail cells.
    2) Active NWS Severe Thunderstorm Warnings & Special Weather Statements (`maxHailSize`).
    3) SPC Day 1 Probabilistic Hail Outlook (`5%–60%` + `SIGN` >=2" zone).
    """
    approaching: list[HailCellThreat] = []
    nearby_count = 0
    nearest_any_mi: float | None = None

    for cell in cells:
        dist_km = haversine_km(lat, lon, cell.lat, cell.lon)
        if dist_km > 150.0:
            continue

        dist_mi = dist_km * 0.621371
        if cell.effective_hail_in >= min_hail_in:
            nearby_count += 1
            if nearest_any_mi is None or dist_mi < nearest_any_mi:
                nearest_any_mi = round(dist_mi, 1)

        # Tangent plane from storm cell -> target driveway (km, East=+x, North=+y)
        mean_lat_rad = math.radians((lat + cell.lat) * 0.5)
        dx_km = (lon - cell.lon) * 111.320 * max(0.15, math.cos(mean_lat_rad))
        dy_km = (lat - cell.lat) * 110.574

        speed_km_min = (cell.speed_kt * 1.852) / 60.0
        head_rad = math.radians(cell.heading_deg)
        ux = math.sin(head_rad)
        uy = math.cos(head_rad)

        in_cone = False
        eta_min = 999
        cpa_miss_km = dist_km

        if dist_km <= 5.0:
            # Hail core is already overhead / on top of the neighborhood!
            in_cone = True
            eta_min = 1
            cpa_miss_km = dist_km
        elif speed_km_min >= 0.10:
            along_km = dx_km * ux + dy_km * uy
            if along_km > 0.0:
                # Positive `along_km` strictly guarantees cell is moving TOWARD target, never away
                t_cpa = along_km / speed_km_min
                cross_km = abs(dx_km * uy - dy_km * ux)
                # 5.5 km core radius + 18-degree supercell right/left-mover deviation cone
                allowed_miss_km = 5.5 + along_km * math.tan(math.radians(18.0))
                cpa_miss_km = cross_km
                if t_cpa <= max(lead_window_minutes, 60) and cross_km <= allowed_miss_km:
                    in_cone = True
                    eta_min = max(1, int(round(t_cpa)))

        if in_cone and cell.effective_hail_in >= min_hail_in:
            # Bearing FROM user's house looking out toward the approaching storm (e.g. "WSW")
            brg_from_target = bearing_to_compass(_bearing_deg(lat, lon, cell.lat, cell.lon))
            approaching.append(
                HailCellThreat(
                    cell_key=cell.cell_key,
                    nexrad_id=cell.nexrad_id,
                    storm_id=cell.storm_id,
                    lat=round(cell.lat, 4),
                    lon=round(cell.lon, 4),
                    distance_km=round(dist_km, 1),
                    distance_mi=round(dist_mi, 1),
                    bearing_from_target=brg_from_target,
                    speed_mph=cell.speed_mph,
                    heading_deg=cell.heading_deg,
                    eta_minutes=eta_min,
                    cpa_miss_km=round(cpa_miss_km, 1),
                    poh_pct=cell.poh_pct,
                    posh_pct=cell.posh_pct,
                    max_hail_in=cell.effective_hail_in,
                    hail_label=hail_inches_to_label(cell.effective_hail_in),
                    in_arrival_cone=True,
                )
            )

    approaching.sort(key=lambda c: (c.eta_minutes, -c.max_hail_in))

    # Check active NWS Severe Thunderstorm Warnings / Special Weather Statements for hail tags
    alert_hail_in = 0.0
    alert_event = ""
    is_severe_warning = False
    for al in alerts or []:
        h_in = al.max_hail_in or (0.50 if al.has_hail_threat else 0.0)
        if h_in >= min_hail_in and h_in >= alert_hail_in:
            alert_hail_in = h_in
            alert_event = al.event
            if "warning" in al.event.lower():
                is_severe_warning = True

    spc_prob = int((spc_hail or {}).get("prob_pct") or 0)
    spc_sig = bool((spc_hail or {}).get("sig_hail"))

    lead_cells = [c for c in approaching if c.eta_minutes <= lead_window_minutes]
    top_cell = lead_cells[0] if lead_cells else (approaching[0] if approaching else None)

    max_hail_in = max(
        (max((c.max_hail_in for c in lead_cells), default=0.0)),
        alert_hail_in,
    )

    if (top_cell and top_cell.eta_minutes <= 15) or (is_severe_warning and alert_hail_in >= min_hail_in):
        status = "imminent"
    elif (top_cell and top_cell.eta_minutes <= lead_window_minutes) or alert_hail_in >= min_hail_in:
        status = "approaching"
    elif spc_prob >= 15 or spc_sig or nearby_count > 0:
        status = "watch"
    else:
        status = "none"

    eta_out = top_cell.eta_minutes if top_cell else None
    hail_lbl = hail_inches_to_label(max_hail_in) if max_hail_in > 0 else "None"

    if status in ("imminent", "approaching"):
        if top_cell:
            headline = (
                f"Hail Core ~{top_cell.eta_minutes} min Away ({top_cell.distance_mi:.0f} mi {top_cell.bearing_from_target}) "
                f"— Est. {hail_lbl} moving at {top_cell.speed_mph:.0f} mph"
            )
        else:
            headline = f"{alert_event}: {hail_lbl} Hail Indicated at Your Location"
    elif status == "watch":
        if nearby_count > 0 and nearest_any_mi is not None:
            headline = (
                f"{nearby_count} NEXRAD hail core(s) within 90 mi (closest {nearest_any_mi:.0f} mi, "
                f"currently tracking outside your driveway cone)"
            )
        else:
            headline = f"SPC Day 1 Hail Risk: {spc_prob}%{' (Hatched ≥2\" Sig Hail)' if spc_sig else ''}"
    else:
        headline = "No approaching hail cores detected on NEXRAD"

    return {
        "status": status,  # 'none' | 'watch' | 'approaching' | 'imminent'
        "headline": headline,
        "eta_minutes": eta_out,
        "max_hail_in": round(max_hail_in, 2),
        "hail_label": hail_lbl,
        "nws_alert_event": alert_event,
        "spc_day1_hail_prob_pct": spc_prob,
        "spc_day1_sig_hail": spc_sig,
        "nearby_cells_count": nearby_count,
        "approaching_cells": [c.to_dict() for c in approaching[:4]],
    }


def build_hail_cells_geojson(
    cells: list[NexradHailCell],
    center_lat: float,
    center_lon: float,
    radius_km: float = 180.0,
) -> dict[str, Any]:
    """Build GeoJSON FeatureCollection of live NEXRAD hail cores + 45-min projected storm tracks."""
    features: list[dict[str, Any]] = []
    for cell in cells:
        if haversine_km(center_lat, center_lon, cell.lat, cell.lon) > radius_km:
            continue
        lbl = hail_inches_to_label(cell.effective_hail_in)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_kind": "hail_core",
                    "cell_key": cell.cell_key,
                    "storm_id": cell.storm_id,
                    "nexrad_id": cell.nexrad_id,
                    "hail_in": cell.effective_hail_in,
                    "hail_label": lbl,
                    "poh_pct": cell.poh_pct,
                    "posh_pct": cell.posh_pct,
                    "speed_mph": cell.speed_mph,
                    "heading_deg": cell.heading_deg,
                },
                "geometry": {"type": "Point", "coordinates": [cell.lon, cell.lat]},
            }
        )

        # 45-minute projected forward storm track vector
        dist_45m_km = (cell.speed_kt * 1.852) * 0.75
        if dist_45m_km >= 3.0:
            end_lat, end_lon = _project_latlon(cell.lat, cell.lon, cell.heading_deg, dist_45m_km)
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "feature_kind": "hail_track",
                        "cell_key": cell.cell_key,
                        "hail_in": cell.effective_hail_in,
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[cell.lon, cell.lat], [end_lon, end_lat]],
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


class HailTrackerClient:
    """90-second cached client for nationwide WSR-88D NEXRAD Level-III storm cell hail attributes."""

    def __init__(self, ttl_seconds: float = 90.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cached_at: float = 0.0
        self._cached_cells: list[NexradHailCell] = []

    async def fetch_active_hail_cells(self) -> list[NexradHailCell]:
        now = time.monotonic()
        if self._cached_cells and (now - self._cached_at) < self.ttl_seconds:
            return self._cached_cells
        if self._cached_at > 0 and (now - self._cached_at) < self.ttl_seconds:
            return self._cached_cells

        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            try:
                resp = await client.get(IEM_NEXRAD_ATTR_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        self._cached_cells = parse_nexrad_attr_geojson(data)
                        self._cached_at = now
                        return self._cached_cells
            except (httpx.HTTPError, ValueError):
                pass
        return self._cached_cells

    async def get_hail_summary_for_point(
        self,
        *,
        lat: float,
        lon: float,
        alerts: list[NwsAlert] | None = None,
        spc_hail: dict[str, Any] | None = None,
        min_hail_in: float = 0.50,
        lead_window_minutes: int = 45,
    ) -> dict[str, Any]:
        cells = await self.fetch_active_hail_cells()
        return evaluate_hail_risk_for_point(
            lat=lat,
            lon=lon,
            cells=cells,
            alerts=alerts,
            spc_hail=spc_hail,
            min_hail_in=min_hail_in,
            lead_window_minutes=lead_window_minutes,
        )

    async def get_hail_overlay_geojson(
        self, lat: float, lon: float, radius_km: float = 180.0
    ) -> dict[str, Any]:
        cells = await self.fetch_active_hail_cells()
        return build_hail_cells_geojson(cells, lat, lon, radius_km=radius_km)
