"""Populates calibration.truck_profile (sim/sql/048_dispatch_board.sql) -- the type/capacity/
home-hub every truck needs for the manual Dispatch Board (drag an order onto a matching-type
truck under capacity; drag a driver from the same home hub) but has NEVER had anywhere in this
project's schema.

## Why this exists -- a real gap found directly, not assumed

ground_truth.trucks is a bare truck-number roster (`truck_number` is its ONLY column) -- the
source Excel has nothing else on file for a truck: no type, capacity, make, model, or location.
Every other part of this project resolves "load_type"/capacity from the ORDER, not the truck
(sim/config.py's CAPACITY_BY_LOAD_TYPE, applied per-order in sim/engine/run_sim.py) -- a
deliberate choice, documented at sim/load_ground_truth.py's driver_equipment load step, because
no real data supports a fixed trailer-per-truck relationship. That's still true and unchanged
for the simulator/scoring pipeline; the Dispatch Board is a DIFFERENT product surface that needs
trucks to be real, differentiated entities a fleet manager can look at and match by hand, so this
is new SYNTHESIZED data, same honesty standard as calibration.assumptions/CAPACITY_BY_LOAD_TYPE.

## What's real vs. synthesized here

- **Type mix**: SYNTHESIZED, a fixed real user decision (sim/config.py's LOAD_TYPE_SHARES: 70%
  Dry Van/25% Reefer/5% Flatbed) -- this used to be drawn proportional to the real historical
  load_type frequency (~85%/4%/10%), but truck_type has never been a real per-truck attribute
  (ground_truth.trucks has no type column at all -- see below), so there was no real signal being
  preserved either way, just a choice of which mix to synthesize.
- **Capacity/dimensions**: SYNTHESIZED, centered on the existing real-data-grounded
  CAPACITY_BY_LOAD_TYPE/CAPACITY_PALLETS_BY_LOAD_TYPE constants (sim/config.py) with small
  real-looking per-truck variance -- a real fleet's trucks of the same nominal type aren't all
  bit-for-bit identical, but there's no real per-truck spec sheet to calibrate the variance
  against either.
- **Home hub**: real London/Milton ratio + Barrie at a fixed ~20% share -- see
  sim/hub_weights.py's own docstring. Same weighting sim/calibrate_driver_home_hub.py now uses
  for driver home hubs (via that same real historical-driver signal -- there's no independent
  real truck-location signal to derive a truck-specific ratio from), so trucks and drivers end up
  with consistent hub proportions.

Run once (idempotent -- truncates and rebuilds): `python -m sim.calibrate_truck_profile`
"""
import random
from collections import Counter

from psycopg2.extras import execute_values

from sim.config import CAPACITY_BY_LOAD_TYPE, CAPACITY_PALLETS_BY_LOAD_TYPE, LOAD_TYPE_SHARES
from sim.db import cursor
from sim.hub_weights import compute_hub_weights

SEED = 43  # deliberately different from calibrate_driver_home_hub.py's 42 -- independent draw, same reproducibility convention

# SYNTHESIZED -- real 53ft dry van/reefer vs. 48ft flatbed trailers are the standard North
# American lengths for these equipment types; no real per-truck spec exists to calibrate against
# (see module docstring). Matches the Claude Design mock's own reference values exactly.
DIMENSIONS_BY_TYPE = {
    'Dry Van': {'length_ft': 53, 'inside_height_ft': 8.5, 'width_in': 98},
    'Reefer': {'length_ft': 53, 'inside_height_ft': 8.5, 'width_in': 98},
    'Flatbed': {'length_ft': 48, 'inside_height_ft': 0, 'width_in': 102},  # open deck -- no inside height
}
CAPACITY_VARIANCE = 0.05  # +/-5% around CAPACITY_BY_LOAD_TYPE -- see module docstring


def build() -> None:
    with cursor() as cur:
        cur.execute("select truck_number from ground_truth.trucks order by truck_number")
        truck_numbers = [r[0] for r in cur.fetchall()]

        # Real user decision (sim/config.py's LOAD_TYPE_SHARES docstring): a fixed 70% Dry Van/25%
        # Reefer/5% Flatbed mix, not the real historical load_type frequency this used to derive
        # from -- truck_type has always been a synthesized label (no real per-truck type exists to
        # calibrate against, see this module's own docstring), so there's no real signal being
        # overridden, just a plain choice of a more Reefer-heavy demo fleet.
        type_names = list(CAPACITY_BY_LOAD_TYPE)  # canonical order/vocabulary
        type_weights = [LOAD_TYPE_SHARES[t] for t in type_names]

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

        # Reuse the SAME real historical-driver home-hub signal sim/calibrate_driver_home_hub.py
        # derives its London:Milton ratio from -- there's no independent real truck-location
        # signal, and consistency between the two populations' hub proportions is the point.
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
            (list(loc_ids_needed | set(anchor_locations)),),
        )
        loc_coords = {loc_id: (lat, lon) for loc_id, lat, lon in cur.fetchall()}

    from sim.engine.run_sim import _haversine_km
    anchor_coords = {loc_id: loc_coords[loc_id] for loc_id in anchor_locations}
    historical_hub: dict[int, int] = {}
    for driver_id, rows in by_driver.items():
        rows.sort(key=lambda x: -x[1])
        top_loc = rows[0][0]
        if top_loc not in loc_coords:
            continue
        lat, lon = loc_coords[top_loc]
        historical_hub[driver_id] = min(anchor_coords, key=lambda aid: _haversine_km((lat, lon), anchor_coords[aid]))

    historical_counts = Counter(historical_hub.values())
    hub_weights = compute_hub_weights(historical_counts, london_id, milton_id, barrie_id)
    hub_ids = list(hub_weights)
    hub_weight_values = [hub_weights[h] for h in hub_ids]

    rng = random.Random(SEED)
    rows_to_write = []
    for truck_number in truck_numbers:
        truck_type = rng.choices(type_names, weights=type_weights, k=1)[0]
        base_capacity_lbs = CAPACITY_BY_LOAD_TYPE[truck_type]
        base_capacity_pallets = CAPACITY_PALLETS_BY_LOAD_TYPE[truck_type]
        capacity_lbs = round(base_capacity_lbs * rng.uniform(1 - CAPACITY_VARIANCE, 1 + CAPACITY_VARIANCE))
        capacity_pallets = base_capacity_pallets  # pallet slot count doesn't meaningfully vary per-truck for a fixed trailer length
        dims = DIMENSIONS_BY_TYPE[truck_type]
        home_hub_id = rng.choices(hub_ids, weights=hub_weight_values, k=1)[0]
        rows_to_write.append((
            truck_number, truck_type, capacity_lbs, capacity_pallets,
            dims['length_ft'], dims['inside_height_ft'], dims['width_in'], home_hub_id,
        ))

    with cursor() as cur:
        cur.execute("truncate calibration.truck_profile")
        execute_values(
            cur,
            """insert into calibration.truck_profile
               (truck_number, truck_type, capacity_lbs, capacity_pallets, length_ft, inside_height_ft, width_in, home_hub_location_id)
               values %s""",
            rows_to_write,
        )

    type_dist = Counter(r[1] for r in rows_to_write)
    hub_dist = Counter(r[7] for r in rows_to_write)
    print(f"Wrote {len(rows_to_write)} truck profiles")
    print(f"  type mix: {dict(type_dist)}")
    for loc_id, name in anchor_locations.items():
        print(f"  {name} (loc {loc_id}): {hub_dist.get(loc_id, 0)} trucks")


if __name__ == "__main__":
    build()
