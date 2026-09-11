"""Segment 2: populate calibration.lane_routes from the self-hosted OSRM server.

Scope (per research/roadstar_platform_plan.md's Segment 2 line: "populated for every historical
lane + reference.locations pairs"): every location routed to/from BOTH terminal hubs (both
directions -- a truck's deadhead-to-hub and dispatch-from-hub distance/ETA/geometry), plus every
real historical origin-destination lane already in calibration.lane_frequency. Not full pairwise
over all ~2,110 locations (that's ~4.45M pairs -- no sim mechanic needs a route between two
arbitrary non-hub locations that was never actually a lane and isn't a hub leg; order generation
draws from real historical lanes via lane_frequency, and empty-repositioning legs are hub-anchored).

geometries=geojson + overview=simplified: full-resolution geometry isn't needed for map-marker
animation and would make ~8,500 rows heavy for Supabase's free tier; simplified keeps the shape
close enough while keeping storage small.
"""
import concurrent.futures as cf

import requests
from psycopg2.extras import execute_values

from sim.config import OSRM_BASE_URL
from sim.db import cursor

WORKERS = 16  # local self-hosted server, low latency -- safe to parallelize heavily


def fetch_locations() -> list[tuple[int, float, float]]:
    with cursor() as cur:
        cur.execute(
            "select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations"
        )
        return cur.fetchall()


def fetch_hub_ids() -> list[int]:
    with cursor() as cur:
        cur.execute("select location_id from reference.locations where label like 'RoadStar Terminal%'")
        return [row[0] for row in cur.fetchall()]


def fetch_lane_pairs() -> list[tuple[int, int]]:
    with cursor() as cur:
        cur.execute("select origin_location_id, dest_location_id from calibration.lane_frequency")
        return cur.fetchall()


def build_pair_list(locations, hub_ids, lane_pairs) -> set[tuple[int, int]]:
    by_id = {loc_id: (lat, lon) for loc_id, lat, lon in locations}
    pairs = set()
    for hub_id in hub_ids:
        for loc_id in by_id:
            if loc_id == hub_id:
                continue
            pairs.add((hub_id, loc_id))
            pairs.add((loc_id, hub_id))
    for origin_id, dest_id in lane_pairs:
        if origin_id in by_id and dest_id in by_id and origin_id != dest_id:
            pairs.add((origin_id, dest_id))
    return pairs, by_id


def route_one(args):
    origin_id, dest_id, by_id = args
    o_lat, o_lon = by_id[origin_id]
    d_lat, d_lon = by_id[dest_id]
    url = (
        f"{OSRM_BASE_URL}/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}"
        f"?overview=simplified&geometries=geojson"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('code') != 'Ok':
            return None
        route = data['routes'][0]
        return (origin_id, dest_id, route['distance'], route['duration'], route['geometry'])
    except requests.RequestException:
        return None


def run():
    print('Loading reference.locations, hub ids, and historical lanes...')
    locations = fetch_locations()
    hub_ids = fetch_hub_ids()
    lane_pairs = fetch_lane_pairs()
    print(f'  {len(locations)} locations, {len(hub_ids)} hubs, {len(lane_pairs)} historical lanes')

    pairs, by_id = build_pair_list(locations, hub_ids, lane_pairs)
    print(f'  {len(pairs)} distinct routes to compute')

    results, failed = [], 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(route_one, (o, d, by_id)) for o, d in pairs]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            r = fut.result()
            if r is None:
                failed += 1
            else:
                results.append(r)
            if i % 1000 == 0:
                print(f'  ...{i}/{len(pairs)} routed ({failed} failed so far)')

    print(f'  {len(results)} routed, {failed} failed (unreachable in the OSM road graph)')

    import json
    rows = [(o, d, dist, dur, json.dumps(geom)) for o, d, dist, dur, geom in results]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.lane_routes
               (origin_location_id, dest_location_id, distance_m, duration_s, geometry)
               values %s on conflict (origin_location_id, dest_location_id) do update set
               distance_m = excluded.distance_m, duration_s = excluded.duration_s,
               geometry = excluded.geometry, computed_at = now()""",
            rows,
            template="(%s, %s, %s, %s, %s::jsonb)",
        )
    print(f'  calibration.lane_routes: {len(rows)} rows upserted')


if __name__ == '__main__':
    run()
