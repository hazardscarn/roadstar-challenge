"""Real road geometry + cumulative-distance interpolation -- "where is the truck right now,
given it left at time T along this route." One function, two callers, per this codebase's own
convention (reward.py, live.process_position_tick()): the live telemetry simulator
(sim/live/telemetry_simulator.py, real ticks) and dashboard/server's /api/route endpoint (map
polylines, on demand). Neither reimplements route-fetching or interpolation independently.
"""
import requests

from sim.config import OSRM_BASE_URL
from sim.db import cursor
from sim.engine.run_sim import SimData, _haversine_km


def get_route_geometry(data: SimData, origin_id: int, dest_id: int) -> tuple[list[tuple[float, float]], bool]:
    """Returns (coordinates, is_real_road_geometry) -- coordinates in (lon, lat) GeoJSON order.
    Cached OSRM geometry if this lane is in calibration.lane_routes (8,486 pre-built pairs), else
    a live call to the same self-hosted OSRM server the sim/scoring pipeline already uses
    (ops/osrm_setup.sh), else a straight line as a last resort (flagged via the bool, same
    fallback tier as sim/engine/run_sim.py's get_route() haversine branch).
    """
    if origin_id == dest_id:
        return [], True

    with cursor() as cur:
        cur.execute(
            "select geometry from calibration.lane_routes where origin_location_id = %s and dest_location_id = %s",
            (origin_id, dest_id),
        )
        row = cur.fetchone()
    if row and row[0]:
        return [tuple(pt) for pt in row[0]["coordinates"]], True

    if origin_id not in data.locations or dest_id not in data.locations:
        return [], False
    o_lat, o_lon = data.locations[origin_id]
    d_lat, d_lon = data.locations[dest_id]
    try:
        resp = requests.get(
            f"{OSRM_BASE_URL}/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}",
            params={"overview": "full", "geometries": "geojson"}, timeout=3,
        )
        result = resp.json()
        if result.get("code") == "Ok":
            return [tuple(pt) for pt in result["routes"][0]["geometry"]["coordinates"]], True
    except Exception:
        pass
    return [(o_lon, o_lat), (d_lon, d_lat)], False


def interpolate_position(coordinates: list[tuple[float, float]], fraction: float) -> tuple[float, float] | None:
    """coordinates: (lon, lat) pairs. fraction: 0..1 of total route distance traveled. Returns
    (lat, lon) at that point, walking cumulative segment distances -- NOT naive point-index
    interpolation, which would move unrealistically fast across a route's few long straight
    segments and crawl across its many short curvy ones.
    """
    if not coordinates:
        return None
    if len(coordinates) == 1:
        lon, lat = coordinates[0]
        return lat, lon

    pts = [(lat, lon) for lon, lat in coordinates]
    seg_lens = [_haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(seg_lens)
    if total == 0:
        return pts[0]

    target = max(0.0, min(1.0, fraction)) * total
    covered = 0.0
    for i, seg_len in enumerate(seg_lens):
        if covered + seg_len >= target or i == len(seg_lens) - 1:
            t = (target - covered) / seg_len if seg_len > 0 else 0.0
            lat = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
            lon = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
            return lat, lon
        covered += seg_len
    return pts[-1]


def route_distance_km(coordinates: list[tuple[float, float]]) -> float:
    pts = [(lat, lon) for lon, lat in coordinates]
    return sum(_haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
