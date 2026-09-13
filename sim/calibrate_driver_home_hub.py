"""Populates calibration.driver_home_hub (sim/sql/045) -- the ASSUMED home-base anchor
driver_home_hub_id() (sim/engine/run_sim.py) reads for every home-base-return reward-shaping
feature (home_progress_bonus, cycle_end_stranding_penalty, distance_to_home_*).

## Why this exists -- a real gap found directly, not assumed

ground_truth.drivers' own terminal_zone/home_zone columns are nearly blank: confirmed directly
(not assumed) that 130 of 131 real drivers report the same generic company code, 'RSTAR', in
BOTH columns -- not a real per-driver location. driver_home_hub_id() used to fall back on that
field (Milton if terminal_zone == 'ONMIL', else London), which meant the ENTIRE 30-driver demo
fleet collapsed onto ONE home hub (London) -- real user report, and a genuine mismatch: it isn't
that the real company only operates from one depot, it's that this one column just never got
populated with real per-driver granularity.

## REVISED for the 3-hub day-ahead dispatch pivot (sim/sql/048_dispatch_board.sql)

Barrie is now a REAL reference.locations terminal_hub row (was an assumed anchor via a proxy
customer location, RONA INC. (BARRIE)) and Niagara Falls is dropped entirely -- the dispatch
board only operates 3 real hubs (London/Milton/Barrie), so every driver's home hub needs to
resolve to one of exactly those 3, not a 4th city with no real dispatch presence.

## The real signal that DOES exist

ground_truth.historical_legs (driver_id) joined to ground_truth.historical_orders (the real
geocoded origin) shows each driver's actual habitual real pickup pattern. Checked directly: only
53 of 131 drivers have enough of this real history to infer a pattern from -- for those, THIS
script buckets their most-frequent real origin to the NEAREST of the 3 real hubs (real haversine
distance, sim.engine.run_sim._haversine_km, the same function every other real-distance feature
in this project uses).

## The other 78 drivers -- no real signal at all

Assigned via a DETERMINISTIC seeded weighted draw -- but NOT an even/empirically-uniform split
across anchors anymore. Real user feedback: derive the real London vs. Milton ratio from the 53
historical drivers' own signal (sim/hub_weights.py), then fold Barrie in at a fixed ~20% share
(it's a brand-new hub with zero real history to derive a share from), scaling London/Milton's
real ratio down to fill the remaining 80% -- real signal preserved where it exists, one clearly-
labeled assumption where it doesn't, and the SAME weighting `sim/calibrate_truck_profile.py` uses
for truck home hubs, so trucks and drivers end up with consistent hub proportions.

Run once (idempotent -- truncates and rebuilds): `python -m sim.calibrate_driver_home_hub`
"""
import random
from collections import Counter

from psycopg2.extras import execute_values

from sim.db import cursor
from sim.engine.run_sim import _haversine_km
from sim.hub_weights import compute_hub_weights, hub_quotas

SEED = 42  # deterministic/reproducible -- this project's own convention (real_data_replay.py's seed=1, etc.), just this script's own independent draw


def nearest_anchor(lat: float, lon: float, anchor_coords: dict[int, tuple[float, float]]) -> int:
    return min(anchor_coords, key=lambda aid: _haversine_km((lat, lon), anchor_coords[aid]))


def build() -> None:
    with cursor() as cur:
        # Resolve the 3 real hub location_ids by label rather than hardcoding IDs -- Barrie's
        # exact id depends on insert order, London/Milton's don't change but there's no reason to
        # treat them differently.
        cur.execute("select location_id, label from reference.locations where label like 'RoadStar Terminal%'")
        anchor_locations: dict[int, str] = {}
        for loc_id, label in cur.fetchall():
            for name in ("London", "Milton", "Barrie"):
                if name in label:
                    anchor_locations[loc_id] = name
        if len(anchor_locations) != 3:
            raise RuntimeError(f"Expected exactly 3 real terminal hubs, found {anchor_locations} -- run sim/sql/048_dispatch_board.sql first.")
        london_id = next(i for i, n in anchor_locations.items() if n == "London")
        milton_id = next(i for i, n in anchor_locations.items() if n == "Milton")
        barrie_id = next(i for i, n in anchor_locations.items() if n == "Barrie")

        cur.execute(
            "select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations where location_id = any(%s)",
            (list(anchor_locations),),
        )
        anchor_coords = {loc_id: (lat, lon) for loc_id, lat, lon in cur.fetchall()}

        cur.execute("select driver_id from ground_truth.drivers order by driver_id")
        all_driver_ids = [r[0] for r in cur.fetchall()]

        # Every driver's real historical origin frequency -- the actual signal driver_home_hub_id()
        # never had access to before this table.
        cur.execute("""
            select hl.driver_id, ho.origin_location_id, count(*) as n
            from ground_truth.historical_legs hl
            join ground_truth.historical_orders ho on ho.trip_number = hl.trip_number
            where hl.driver_id is not null and ho.origin_location_id is not null
            group by hl.driver_id, ho.origin_location_id
        """)
        by_driver: dict[int, list[tuple[int, int]]] = {}
        for driver_id, loc, n in cur.fetchall():
            by_driver.setdefault(driver_id, []).append((loc, n))

        loc_ids_needed = {loc for rows in by_driver.values() for loc, _n in rows}
        cur.execute(
            "select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations where location_id = any(%s)",
            (list(loc_ids_needed),),
        )
        loc_coords = {loc_id: (lat, lon) for loc_id, lat, lon in cur.fetchall()}

    historical_hub: dict[int, int] = {}
    for driver_id, rows in by_driver.items():
        rows.sort(key=lambda x: -x[1])  # most-frequent real origin first
        top_loc = rows[0][0]
        if top_loc not in loc_coords:
            continue
        lat, lon = loc_coords[top_loc]
        historical_hub[driver_id] = nearest_anchor(lat, lon, anchor_coords)

    # Fixed, discussed population-level shares (sim/hub_weights.py -- Milton 55/London 30/
    # Barrie 15) -- real user correction: the pure real-ratio-derived split (this used to feed
    # `compute_hub_weights` the REAL London:Milton counts above) turned out so Milton-heavy that
    # the FULL 131-driver population landed at ~76% Milton / ~8% London / ~17% Barrie, checked
    # directly -- not the discussed target, and it collapsed London (a REAL, currently-operating
    # hub) to almost nothing. Real signal is still used -- as a per-driver PREFERENCE for which
    # hub they land in -- but the POPULATION PROPORTION is now a hard quota, not an emergent
    # property of however skewed the raw historical signal happens to be.
    hub_weights = compute_hub_weights({}, london_id, milton_id, barrie_id)
    anchor_ids = list(hub_weights)
    quotas = hub_quotas(len(all_driver_ids), {a: hub_weights[a] for a in anchor_ids})

    rng = random.Random(SEED)
    assigned_hub: dict[int, int] = {}
    remaining_quota = dict(quotas)
    deferred: list[int] = []

    # Real signal first, honored as a preference up to quota -- a driver whose real activity
    # clusters near London gets London if a London slot is still open.
    signal_drivers = sorted(historical_hub, key=lambda d: d)
    rng.shuffle(signal_drivers)  # so which drivers get deferred when a quota fills isn't just "highest driver_id loses"
    for driver_id in signal_drivers:
        preferred = historical_hub[driver_id]
        if remaining_quota.get(preferred, 0) > 0:
            assigned_hub[driver_id] = preferred
            remaining_quota[preferred] -= 1
        else:
            deferred.append(driver_id)

    # Everyone else (no real signal, or their preferred hub's quota was already full) fills
    # whatever's left, in a deterministic seeded order -- exactly consumes remaining_quota to 0.
    no_signal_drivers = [d for d in all_driver_ids if d not in historical_hub]
    fill_order = deferred + no_signal_drivers
    rng.shuffle(fill_order)
    hub_pool = [hub for hub, n in remaining_quota.items() for _ in range(n)]
    rng.shuffle(hub_pool)
    for driver_id, hub in zip(fill_order, hub_pool):
        assigned_hub[driver_id] = hub

    rows_to_write = []
    for driver_id in all_driver_ids:
        if driver_id in historical_hub and assigned_hub.get(driver_id) == historical_hub[driver_id]:
            rows_to_write.append((driver_id, assigned_hub[driver_id], "historical"))
        else:
            hub = assigned_hub[driver_id]
            rows_to_write.append((driver_id, hub, "assumed"))

    with cursor() as cur:
        cur.execute("truncate calibration.driver_home_hub")
        execute_values(
            cur,
            "insert into calibration.driver_home_hub (driver_id, hub_location_id, source) values %s",
            rows_to_write,
        )

    n_historical = sum(1 for _did, _hub, src in rows_to_write if src == "historical")
    print(f"Wrote {len(rows_to_write)} driver home-hub rows ({n_historical} historical, {len(rows_to_write) - n_historical} assumed)")
    hub_counts = Counter(hub for _did, hub, _src in rows_to_write)
    for loc_id, name in anchor_locations.items():
        print(f"  {name} (loc {loc_id}): {hub_counts.get(loc_id, 0)} drivers")


if __name__ == "__main__":
    build()
