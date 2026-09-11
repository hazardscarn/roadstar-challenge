"""Resets and seeds the WHOLE live.* dataset for the actual demo fleet (scripts/seed_demo_accounts.py
made logins for all of them), replacing sim/live/seed_test_fleet.py's earlier, unrelated 25-driver
theory-proof seed (driver_ids 1-25, no overlap with our real demo accounts beyond a few IDs). This
is what Live Ops/Dispatch/Orders/Trip History/Fleet Health/Drivers all run against -- every table
gets truncated together (not just the fleet-state tables), because quote_requests/geofence_events/
trip_log/etc. all key off trip_id and were found ORPHANED (a real bug: quote_requests.status=
'assigned' pointing at a trip_id that no longer existed) the first time this script truncated only
live.trips/driver_status and left everything else stale.

Scaled 8 -> 30 drivers/trucks (real user feedback: "8 is not the best option"). DEMO_DRIVER_IDS/
DRIVER_TRUCKS are now built dynamically by _build_fleet_roster() rather than hand-typed -- the 18
real driver_id -> truck_number pairs on file in ground_truth.driver_equipment come first, filled to
30 with other real driver_ids (sorted, deterministic) paired to real ground_truth.trucks numbers
not already claimed by those 18 -- same synthesized-pool-truck treatment the original 8-driver
seed already used for driver 26 (no real DEFAULT_PUNIT on file).

Includes one PASSING live.vehicle_inspections row per driver dated `now()` -- without this, the
manager-side inspection gate (score_quote.py's EXISTS(...overall_pass...) filter, the brief's real
compliance requirement) would exclude every demo driver simply because Phase 7's inspection UI
hasn't been used yet. This mirrors a realistic baseline ("everyone did their pre-trip check this
morning"), not a way around the gate -- the gate itself is unchanged and still enforced.

Idle-driver POSITIONS are drawn from calibration.lane_frequency's real weighted location pool
(the SAME source run_sim.py itself uses to place simulated demand), not just the 2 terminal hubs
-- a real regional fleet's idle trucks are scattered near their last drop-off, not all parked at
the yard. Caught directly from a live demo: seeding everyone at 1-of-2 hubs made most candidates
on a given quote score IDENTICALLY (same deadhead distance, same everything) and made map markers
visually stack on top of each other -- a demo-data realism gap, not a scoring bug (the model was
always differentiating correctly on whatever real inputs it was given).
"""
import random
import uuid
from datetime import datetime, timedelta, timezone

from sim.db import cursor
from sim.engine.run_sim import driver_home_hub_id, load_sim_data

FLEET_SIZE = 30


def _build_fleet_roster(cur) -> tuple[list[int], dict[int, str]]:
    """Real driver_id -> truck_number pairs (ground_truth.driver_equipment) first, filled to
    FLEET_SIZE with other real driver_ids matched to real, not-already-claimed truck_numbers --
    see module docstring. Deterministic (sorted), not random, so re-running this script without
    changing FLEET_SIZE always seeds the same roster.
    """
    cur.execute('select driver_id, truck_number from ground_truth.driver_equipment where truck_number is not null order by driver_id')
    real_pairs = cur.fetchall()
    driver_trucks = dict(real_pairs)
    used_trucks = set(driver_trucks.values())

    cur.execute('select driver_id from ground_truth.drivers order by driver_id')
    all_driver_ids = [r[0] for r in cur.fetchall()]
    remaining_drivers = [d for d in all_driver_ids if d not in driver_trucks]

    cur.execute('select truck_number from ground_truth.trucks order by truck_number')
    all_truck_numbers = [r[0] for r in cur.fetchall()]
    remaining_trucks = [t for t in all_truck_numbers if t not in used_trucks]

    n_extra = FLEET_SIZE - len(driver_trucks)
    for driver_id, truck_number in zip(remaining_drivers[:n_extra], remaining_trucks[:n_extra]):
        driver_trucks[driver_id] = truck_number

    driver_ids = sorted(driver_trucks)[:FLEET_SIZE]
    return driver_ids, {d: driver_trucks[d] for d in driver_ids}


# MID_ROUTE/HUB shares kept proportional to the original 8-driver seed (2/8 mid-route, 1/8 hub).
MID_ROUTE_SHARE = 2 / 8
HUB_SHARE = 1 / 8


def _weighted_location_pool(data) -> list[int]:
    """Real lane endpoints, weighted by real historical frequency (calibration.lane_frequency,
    already loaded into data.lane_weights) -- the same realism source run_sim.py draws simulated
    demand from, reused here for realistic idle-driver positions instead of picking arbitrary or
    hub-only spots."""
    pool = []
    for origin_id, dest_id, weight in data.lane_weights:
        n = max(1, round(weight))
        pool.extend([origin_id, dest_id] * n)
    return pool


def seed(seed_value: int = 11):
    rng = random.Random(seed_value)
    data = load_sim_data()
    now = datetime.now(timezone.utc)
    weighted_pool = _weighted_location_pool(data)
    hub_ids = list(data.hub_ids.values())

    with cursor() as cur:
        cur.execute("truncate table live.driver_status cascade")
        cur.execute("truncate table live.trips cascade")
        cur.execute("truncate table live.truck_maintenance_state cascade")
        cur.execute("truncate table live.vehicle_inspections cascade")
        cur.execute("truncate table live.position_history cascade")
        # Real bug this caused: quote_requests/quote_recommendations/geofence_events/trip_log/
        # trip_costs/invoices/detention_billing all reference trip_id or get their lifecycle
        # driven by live.trips -- truncating trips alone left every OTHER table's rows orphaned
        # (a quote_requests.status='assigned' row pointing at a trip_id that no longer existed),
        # which silently broke the Orders/Feed pages (their trip lookup just came back empty).
        # A reseed has to reset the WHOLE live.* dataset together, not just the fleet tables.
        cur.execute("truncate table live.quote_recommendations cascade")
        cur.execute("truncate table live.quote_requests cascade")
        cur.execute("truncate table live.geofence_events cascade")
        cur.execute("truncate table live.geofence_dwell_state cascade")
        cur.execute("truncate table live.detention_billing cascade")
        cur.execute("truncate table live.trip_costs cascade")
        cur.execute("truncate table live.invoices cascade")
        cur.execute("truncate table live.trip_log cascade")
        cur.execute("truncate table live.quote_candidate_snapshots cascade")
        cur.execute("truncate table live.driver_state_snapshots cascade")

        demo_driver_ids, driver_trucks = _build_fleet_roster(cur)
        n_mid_route = round(len(demo_driver_ids) * MID_ROUTE_SHARE)
        n_hub = round(len(demo_driver_ids) * HUB_SHARE)
        mid_route_driver_ids = set(rng.sample(demo_driver_ids, n_mid_route))
        hub_driver_ids = set(rng.sample([d for d in demo_driver_ids if d not in mid_route_driver_ids], n_hub))

        for driver_id in demo_driver_ids:
            truck_number = driver_trucks[driver_id]
            is_mid_route = driver_id in mid_route_driver_ids
            # Wider real spread (2-13h, not 6-13h) -- some drivers genuinely close to their HOS
            # limit, matching a real fleet snapshot at a random moment, not an artificially
            # comfortable one that never triggers the HOS-risk term.
            hos_remaining = rng.uniform(2.0, 13.0)

            # Home-base-return retarget (sim/sql/042, documents/logs/23-24): real, DISTINCT
            # starting values for all 4 HOS sub-clocks, not the single blended figure copy-pasted
            # 4 times (that was the exact gap this migration closes -- see the migration's own
            # comment). hos_remaining above is already the binding (smallest) constraint in this
            # 2-13h range, so it seeds the daily driving clock directly; duty gets a small real
            # buffer above it (duty >= driving is always true for a real driver); the two CYCLE
            # clocks (70h/7d, 120h/14d) are sampled independently and WIDER, at or above the daily
            # figure -- matching a real fleet where the weekly/bi-weekly cycle is only occasionally
            # the binding constraint (log 24's own measured finding on the real calibrated batch),
            # not tied to the daily draw. No qualifying-reset detection is modeled here (documents/
            # logs/23's own scoped-out item) -- these are independent starting snapshots, decremented
            # by real elapsed on-duty time going forward (telemetry_simulator.py), same honest
            # simplification the existing blended hos_remaining_hours already carried.
            hos_driving_remaining = hos_remaining
            hos_duty_remaining = min(14.0, hos_remaining + rng.uniform(0.0, 1.0))
            hos_cycle1_remaining = min(70.0, hos_remaining + rng.uniform(10.0, 55.0))
            hos_cycle2_remaining = min(120.0, hos_cycle1_remaining + rng.uniform(10.0, 45.0))

            # Healthy-biased maintenance state -- matches sim/engine/maintenance.py's
            # initialize_fleet() distribution (documents/logs/18), most trucks well under interval.
            pct = rng.uniform(0.05, 0.7)
            cum_km = pct * 25000
            days_since = rng.uniform(0.05, 0.7) * 90
            last_service_at = now - timedelta(days=days_since)
            cur.execute("""
                insert into live.truck_maintenance_state
                  (truck_number, cumulative_km_since_service, last_service_at, service_interval_km, service_interval_days)
                values (%s, %s, %s, 25000, 90)
                on conflict (truck_number) do update set
                  cumulative_km_since_service = excluded.cumulative_km_since_service,
                  last_service_at = excluded.last_service_at
            """, (truck_number, cum_km, last_service_at))

            # Passing DVIR on file as of this morning -- see module docstring. odometer_km baseline
            # reused for live.driver_status below too, so both reflect the same starting figure.
            odometer_km = rng.uniform(80000, 300000)
            cur.execute("""
                insert into live.vehicle_inspections
                  (driver_id, truck_number, submitted_at, odometer_km,
                   brakes_ok, tires_ok, lights_ok, fluid_levels_ok, coupling_ok, trailer_ok)
                values (%s, %s, %s, %s, true, true, true, true, true, true)
            """, (driver_id, truck_number, now - timedelta(hours=rng.uniform(1, 6)), odometer_km))

            # Home-time retarget (sim/sql/046, documents/logs/25): SYNTHESIZED -- no real "when did
            # this driver last leave home" data exists to seed from. A driver seeded exactly AT
            # their own home hub gets a recent, plausible arrival (they just got back); everyone
            # else gets a real spread of past departure times (up to 10 days), so a freshly-seeded
            # fleet shows genuine variation in home-time urgency from the first tick, not everyone
            # starting at 0.
            home_hub_id = driver_home_hub_id(data, driver_id)

            if not is_mid_route:
                loc_id = rng.choice(hub_ids) if driver_id in hub_driver_ids else rng.choice(weighted_pool)
                last_home_arrival_at = (
                    now - timedelta(hours=rng.uniform(0.5, 6)) if loc_id == home_hub_id
                    else now - timedelta(hours=rng.uniform(6, 24 * 10))
                )
                cur.execute("""
                    insert into live.driver_status
                      (driver_id, updated_at, last_location_id, hos_remaining_hours, duty_status,
                       current_trip_id, truck_number, trailer_type, trailer_capacity_lbs, trailer_capacity_pallets,
                       odometer_km, fuel_pct, hos_driving_hours_remaining, hos_duty_hours_remaining,
                       hos_cycle1_hours_remaining, hos_cycle2_hours_remaining, last_home_arrival_at)
                    values (%s, %s, %s, %s, 'off_duty', null, %s, 'Dry Van', 44500, 26, %s, %s, %s, %s, %s, %s, %s)
                """, (driver_id, now, loc_id, hos_remaining, truck_number, odometer_km, rng.uniform(55, 100),
                      hos_driving_remaining, hos_duty_remaining, hos_cycle1_remaining, hos_cycle2_remaining,
                      last_home_arrival_at))
            else:
                trip_id = uuid.uuid4()
                origin_loc = rng.choice(weighted_pool)
                dest_loc = rng.choice(weighted_pool)
                eta = now + timedelta(hours=rng.uniform(0.5, 4.0))
                projected_hos = max(0.0, hos_remaining - rng.uniform(1.0, 3.0))
                last_home_arrival_at = now - timedelta(hours=rng.uniform(6, 24 * 10))  # mid-route -- not home right now
                cur.execute("""
                    insert into live.driver_status
                      (driver_id, updated_at, last_location_id, hos_remaining_hours, duty_status,
                       current_trip_id, truck_number, trailer_type, trailer_capacity_lbs, trailer_capacity_pallets,
                       odometer_km, fuel_pct, hos_driving_hours_remaining, hos_duty_hours_remaining,
                       hos_cycle1_hours_remaining, hos_cycle2_hours_remaining, last_home_arrival_at)
                    values (%s, %s, null, %s, 'driving', %s, %s, 'Dry Van', 44500, 26, %s, %s, %s, %s, %s, %s, %s)
                """, (driver_id, now, hos_remaining, trip_id, truck_number, odometer_km, rng.uniform(40, 90),
                      hos_driving_remaining, hos_duty_remaining, hos_cycle1_remaining, hos_cycle2_remaining,
                      last_home_arrival_at))
                # status='in_transit' (not the sim schema's legacy 'in_progress') -- this is the
                # exact vocabulary sim/live/telemetry_simulator.py's lifecycle drives, so a
                # freshly-seeded mid-route driver is immediately picked up and moved for real.
                cur.execute("""
                    insert into live.trips
                      (trip_id, driver_id, status, last_event, eta, origin_location_id, dest_location_id,
                       created_at, weight_lbs, pallets, load_type, pre_pickup_deadhead_miles,
                       projected_hos_remaining_hours, projected_truck_pct_km_interval, projected_truck_pct_days_interval)
                    values (%s, %s, 'in_transit', 'DEPSHIP', %s, %s, %s, %s, %s, %s, 'Dry Van', 0, %s, %s, %s)
                """, (trip_id, driver_id, eta, origin_loc, dest_loc, now - timedelta(hours=1),
                      rng.uniform(8000, 30000), rng.randint(4, 20),
                      projected_hos, pct, days_since / 90))

    print(f'Seeded {len(demo_driver_ids)} demo drivers ({len(demo_driver_ids) - len(mid_route_driver_ids)} idle, '
          f'{len(mid_route_driver_ids)} mid-route, scattered across real weighted locations) into live.* -- '
          f'driver_ids {demo_driver_ids}')


if __name__ == '__main__':
    seed()
