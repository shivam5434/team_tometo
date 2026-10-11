"""0.1-degree grid helpers.

MEASURED (Phase 1b probe): Open-Meteo snaps every request to a 0.1-degree
grid point (~11 km), e.g. (28.61,77.21) -> (28.6,77.2). The underlying CAMS
model is coarser (~45 km), so neighbouring 0.1-degree cells differ slightly
but are smooth interpolations, not independent measurements.

Two coordinates in the same 0.1-degree cell return identical values, so the
cell id is the cache key for the on-demand lookup in Phase 3.
"""
from __future__ import annotations

STEP = 0.1
# Generous bounding box around India (incl. Andaman & Nicobar, Lakshadweep).
INDIA_BBOX = {"lat": (6.0, 37.5), "lon": (68.0, 98.0)}


def in_india_bbox(lat: float, lon: float) -> bool:
    """Cheap sanity check for user-supplied coordinates (not a border test)."""
    return (INDIA_BBOX["lat"][0] <= lat <= INDIA_BBOX["lat"][1]
            and INDIA_BBOX["lon"][0] <= lon <= INDIA_BBOX["lon"][1])


def cell_index(lat: float, lon: float):
    return (round(lat / STEP), round(lon / STEP))


def cell_id(lat: float, lon: float) -> str:
    i, j = cell_index(lat, lon)
    return f"{i}_{j}"


def cell_center(lat: float, lon: float):
    i, j = cell_index(lat, lon)
    return (round(i * STEP, 1), round(j * STEP, 1))


def dedupe_by_cell(points):
    """points: iterable of dicts with lat/lon. Keep the first point per cell."""
    seen, out = set(), []
    for p in points:
        cid = cell_id(p["lat"], p["lon"])
        if cid in seen:
            continue
        seen.add(cid)
        out.append({**p, "cell": cid})
    return out
