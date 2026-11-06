"""5-point geodesic spatial neighborhood ring for convective proximity & boundary gradients."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt

from mesoscale.ml.thermodynamics import equivalent_potential_temperature_k
from mesoscale.providers.base import EARTH_RADIUS_KM


def compute_spatial_ring(
    lat: float, lon: float, radius_km: float = 8.0
) -> list[tuple[str, float, float]]:
    """
    Return 5 points: [('center', lat, lon), ('N', ...), ('E', ...), ('S', ...), ('W', ...)].
    Sampling an 8 km ring captures nearby simulated convective cells (HRRR/RRFS) and
    horizontal airmass gradients (fronts, drylines, outflow boundaries).
    """
    d_lat = math.degrees(radius_km / EARTH_RADIUS_KM)
    cos_lat = max(math.cos(math.radians(lat)), 0.05)
    d_lon = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    return [
        ("center", round(lat, 5), round(lon, 5)),
        ("north", round(lat + d_lat, 5), round(lon, 5)),
        ("east", round(lat, 5), round(lon + d_lon, 5)),
        ("south", round(lat - d_lat, 5), round(lon, 5)),
        ("west", round(lat, 5), round(lon - d_lon, 5)),
    ]


def compute_ring_features(
    point_series: list[dict[str, npt.NDArray[np.float64]]],
) -> dict[str, npt.NDArray[np.float64]]:
    """
    Given aligned hourly arrays for [center, north, east, south, west], compute:
    - ring_precip_max_mm: max hourly precip across the 5-point 8 km neighborhood
    - ring_precip_mean_mm: spatial mean hourly precip
    - ring_gust_max_ms: max 10m gust across the 5-point neighborhood
    - ring_temp_grad_c: horizontal temperature difference magnitude across 16 km (°C)
    - ring_theta_e_grad_k: horizontal theta_e boundary gradient magnitude across 16 km (K)
    """
    if not point_series:
        return {}

    n = len(point_series[0]["temp_c"])
    if len(point_series) < 5:
        c = point_series[0]
        zeros = np.zeros(n, dtype=np.float64)
        return {
            "ring_precip_max_mm": np.nan_to_num(c.get("precip_mm", zeros), nan=0.0),
            "ring_precip_mean_mm": np.nan_to_num(c.get("precip_mm", zeros), nan=0.0),
            "ring_gust_max_ms": c.get("gust_ms", np.full(n, np.nan)),
            "ring_temp_grad_c": zeros,
            "ring_theta_e_grad_k": zeros,
        }

    precips = np.vstack([np.nan_to_num(p["precip_mm"], nan=0.0) for p in point_series])
    gusts = np.vstack([p["gust_ms"] for p in point_series])
    temps = np.vstack([p["temp_c"] for p in point_series])

    theta_es: list[npt.NDArray[np.float64]] = []
    for p in point_series:
        td = np.where(np.isnan(p["dewpoint_c"]), p["temp_c"] - 5.0, p["dewpoint_c"])
        pres = p.get("pressure_hpa", np.full(n, 975.0))
        theta_es.append(equivalent_potential_temperature_k(p["temp_c"], td, pres))
    thetas = np.vstack(theta_es)

    # Indices: 0=center, 1=north, 2=east, 3=south, 4=west
    dt_ns = temps[1] - temps[3]
    dt_ew = temps[2] - temps[4]
    temp_grad = np.hypot(np.nan_to_num(dt_ns, nan=0.0), np.nan_to_num(dt_ew, nan=0.0))

    dth_ns = thetas[1] - thetas[3]
    dth_ew = thetas[2] - thetas[4]
    theta_grad = np.hypot(np.nan_to_num(dth_ns, nan=0.0), np.nan_to_num(dth_ew, nan=0.0))

    all_nan = np.all(np.isnan(gusts), axis=0)
    safe_gusts = np.where(all_nan, 0.0, gusts)
    gust_max = np.where(all_nan, np.nan, np.nanmax(safe_gusts, axis=0))

    return {
        "ring_precip_max_mm": np.max(precips, axis=0),
        "ring_precip_mean_mm": np.mean(precips, axis=0),
        "ring_gust_max_ms": gust_max,
        "ring_temp_grad_c": temp_grad,
        "ring_theta_e_grad_k": theta_grad,
    }


def ring_geojson_feature_collection(lat: float, lon: float, radius_km: float = 8.0) -> dict[str, Any]:
    """Build GeoJSON circle + 5 sample points for the MapLibre radar overlay."""
    pts = compute_spatial_ring(lat, lon, radius_km)
    circle_coords: list[list[float]] = []
    d_lat = math.degrees(radius_km / EARTH_RADIUS_KM)
    cos_lat = max(math.cos(math.radians(lat)), 0.05)
    d_lon = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    for deg in range(0, 365, 10):
        rad = math.radians(deg)
        circle_coords.append([round(lon + d_lon * math.cos(rad), 5), round(lat + d_lat * math.sin(rad), 5)])

    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "properties": {"kind": "ring_boundary", "radius_km": radius_km},
            "geometry": {"type": "Polygon", "coordinates": [circle_coords]},
        }
    ]
    for label, plat, plon in pts:
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": "ring_point", "label": label},
                "geometry": {"type": "Point", "coordinates": [plon, plat]},
            }
        )
    return {"type": "FeatureCollection", "features": features}
