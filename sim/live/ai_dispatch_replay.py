"""Generates a full-day, multi-truck REPLAY of an AI-dispatched day (whatever sim/dispatch_solver.py
decided, via dispatch.assignments/dispatch.day_orders for one service_date) for the Simulation
Showcase page.

Real user ask: replace the old ML/RL-trained-policy week-long batch simulation on that page with a
direct visualization of what the CP-SAT solver actually decided for one real day, so judges can see
hub-localized truck activity (Milton trucks running near Milton, London trucks near London) and a
realistic multi-truck day play out on a real map, backed by the same Supabase `simulation` schema
every other simulation feature already uses (sim/sql/038).

Reuses the EXACT SimulationRunResult/SimTrip JSON shape dashboard/server/main.py's existing
/api/simulation/run (the old week-long batch endpoint) already returns -- the frontend's playback
engine (positionAt, buildTimeline, the whole SimulationShowcase.tsx map/timeline/speed-control
machinery) needs no changes to play this back; only the data SOURCE and per-truck coloring
(by hub, not by driver) differ.

## Realistic speed (real user ask)

Evenly-time-spaced trajectory samples (the old week-sim's own sample_leg()) imply constant average
speed across a whole leg -- real trucks don't drive that way. Each leg here instead gets samples
spaced EVENLY IN DISTANCE but UNEVENLY IN TIME, per a synthesized highway/city speed profile: city
pace near the pickup/dropoff ends (dock approach, local roads), highway pace in the middle, with
independent per-segment jitter simulating real traffic variance. positionAt()'s existing linear
interpolation over [t, lat, lon] samples renders this as visibly faster/slower motion with zero
frontend change. Each sample also carries a synthesized [speed_mph, fuel_pct] pair (same convention
sim/live/trip_demo_simulator.py's telemetry already uses) so a clicked trip can show a live
speed/fuel readout at the current playback time straight from already-loaded data.

## One deliberate detention event (real user ask)

"please also add a geofencing event... show this like a red light warning" -- left to chance, a
generated day might not happen to produce one. The truck with the most orders gets its FINAL
delivery dwell deliberately stretched past the real 2h free window (calibration.assumptions'
detention_free_hours), so every generated day guarantees at least one visible, billed detention
case -- flagged in this module, not hidden.

Run: `python -m sim.live.ai_dispatch_replay 2026-09-13` (defaults to tomorrow's dispatch day).
"""
from __future__ import annotations

import random
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from psycopg2.extras import Json, execute_values

from sim.config import ASSUMED_OPERATING_COST_PER_MILE, DETENTION_FREE_HOURS, quote_price
from sim.db import cursor
from sim.dispatch_solver import dwell_hours_per_order
from sim.engine.route_interpolation import get_route_geometry, interpolate_position, route_distance_km
from sim.engine.run_sim import load_sim_data

DAY_START_HOUR = 3  # matches sim/dispatch_solver.py's own real-data-grounded assumption (real user
# correction: checked directly, 7.6% of real historical pickups happen before 06:00, almost all of
# it in the 03:00-06:00 window -- a fixed 06:00 floor was structurally blocking early pickups)
DETENTION_RATE_PER_HR_CAD = 75.0  # matches sim/sql/044's own coalesce() fallback
AVG_HIGHWAY_SPEED_KMH = 95.0
AVG_CITY_SPEED_KMH = 40.0
CITY_FRACTION = 0.15  # first/last 15% of a leg's distance is "local roads" pace
TRAFFIC_JITTER = 0.18  # +/- 18% per-segment speed jitter -- simulated traffic, not a guess at real data
SAMPLES_PER_LEG = 14


def _realistic_time_fracs(rng: random.Random, n: int = SAMPLES_PER_LEG) -> list[float]:
    """n+1 fractional TIME points (0..1) for n+1 evenly-DISTANCE-spaced samples along a leg, given
    a synthesized city/highway speed profile -- see module docstring's "Realistic speed" section."""
    seg_times = []
    for i in range(n):
        mid = (i + 0.5) / n
        speed = AVG_CITY_SPEED_KMH if mid < CITY_FRACTION or mid > 1 - CITY_FRACTION else AVG_HIGHWAY_SPEED_KMH
        speed *= rng.uniform(1 - TRAFFIC_JITTER, 1 + TRAFFIC_JITTER)
        seg_times.append(1.0 / speed)
    total = sum(seg_times)
    fracs, acc = [0.0], 0.0
    for st in seg_times:
        acc += st
        fracs.append(acc / total)
    return fracs, seg_times, total


def _sample_leg(coords, t0_s: float, t1_s: float, rng: random.Random, fuel_start: float) -> tuple[list[list[float]], float]:
    """Returns (samples, fuel_end) -- samples are [t_offset_s, lat, lon, speed_mph, fuel_pct].
    fuel drains a small realistic amount per leg (never modeled as a hard constraint anywhere in
    this project -- purely a cosmetic telemetry readout, same honest flag sim/live/telemetry_
    simulator.py's own fuel_pct already carries)."""
    if not coords or t1_s <= t0_s:
        return [], fuel_start
    n = SAMPLES_PER_LEG
    fracs, seg_times, total_time_units = _realistic_time_fracs(rng, n)
    dist_km = route_distance_km(coords)
    duration_s = t1_s - t0_s
    samples = []
    fuel = fuel_start
    for i in range(n + 1):
        dist_frac = i / n
        pos = interpolate_position(coords, dist_frac)
        if not pos:
            continue
        lat, lon = pos
        t = t0_s + duration_s * fracs[i]
        if i == 0:
            speed_mph = 0.0
        else:
            seg_km = dist_km / n
            seg_hours = (seg_times[i - 1] / total_time_units) * (duration_s / 3600) if total_time_units else 0
            speed_mph = (seg_km / seg_hours) * 0.621371 if seg_hours > 0 else 0.0
            fuel = max(4.0, fuel - (seg_km / dist_km if dist_km else 0) * rng.uniform(1.2, 2.4))
        samples.append([round(t, 1), lat, lon, round(speed_mph, 1), round(fuel, 1)])
    return samples, fuel


def generate(service_date: date, seed: int | None = None) -> dict:
    rng = random.Random(seed if seed is not None else hash(service_date.isoformat()) & 0xFFFFFFFF)
    data = load_sim_data()
    sim_start = datetime.combine(service_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=DAY_START_HOUR)

    with cursor() as cur:
        cur.execute("select id from dispatch.days where service_date = %s", (service_date,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No dispatch day for {service_date} -- open the Dispatch page for that date first")
        day_id = row[0]

        cur.execute("""
            select dt.truck_number, tp.home_hub_location_id, hub.city
            from dispatch.day_trucks dt join calibration.truck_profile tp on tp.truck_number = dt.truck_number
            join reference.locations hub on hub.location_id = tp.home_hub_location_id
            where dt.day_id = %s
        """, (day_id,))
        truck_hub = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        cur.execute("""
            select dd.driver_id, dd.hos_driving_hours_remaining, dd.hos_duty_hours_remaining
            from dispatch.day_drivers dd where dd.day_id = %s
        """, (day_id,))
        driver_hos = {r[0]: (float(r[1]), float(r[2])) for r in cur.fetchall()}

        cur.execute("""
            select o.id, o.pickup_location_id, o.dest_location_id, ol.label, dl.label,
                   o.weight_lbs, o.pallets, o.load_type, o.pickup_at, o.loaded_miles, o.loaded_hours, o.rate
            from dispatch.day_orders o
            join reference.locations ol on ol.location_id = o.pickup_location_id
            join reference.locations dl on dl.location_id = o.dest_location_id
            where o.day_id = %s
        """, (day_id,))
        orders_by_id = {}
        for r in cur.fetchall():
            orders_by_id[str(r[0])] = {
                'order_id': str(r[0]), 'pickup_location_id': r[1], 'dest_location_id': r[2],
                'pickup_label': r[3], 'dest_label': r[4], 'weight_lbs': float(r[5]), 'pallets': r[6],
                'load_type': r[7], 'pickup_at': r[8], 'loaded_miles': float(r[9]), 'loaded_hours': float(r[10]),
                'rate': float(r[11]),
            }

        cur.execute("select truck_number, driver_id, order_ids from dispatch.assignments where day_id = %s", (day_id,))
        assignments = [(r[0], r[1], [str(x) for x in r[2]]) for r in cur.fetchall() if r[1] is not None and r[2]]

    if not assignments:
        raise ValueError(f"{service_date} has no finalized truck/driver/order assignments to replay -- run AI Assign (or dispatch manually) first")

    dwell_hours = dwell_hours_per_order(data)
    pickup_dwell_h = data.dwell_minutes['pickup'][1] / 60
    delivery_dwell_h = data.dwell_minutes['delivery'][1] / 60

    # Deliberate detention target: the truck with the most orders (real user ask -- guarantee at
    # least one visible over-2h-detention case every generated day, not left to chance).
    detention_truck = max(assignments, key=lambda a: len(a[2]))[0] if assignments else None

    run_id = uuid.uuid4()
    trip_rows, telemetry_summary_rows = [], []
    total_revenue = total_deadhead_miles = total_deadhead_cost = total_loaded_miles = 0.0
    n_completed = 0
    driver_hours_used: dict[int, float] = {}

    # Real user ask: "the simulation is now our data source" -- everything downstream (Trip
    # History, the new Data page, Billing) reads from these tables, and they were sitting
    # completely empty after a replay because this function only ever wrote its own dedicated
    # simulation.ai_dispatch_trips row, never the fuller simulation.* schema (sim/sql/038) that
    # already exists for exactly this purpose. Built from the SAME per-order data already computed
    # below -- no separate pass, no separate assumptions.
    sim_trip_rows: list[tuple] = []
    trip_log_rows: list[tuple] = []
    geofence_event_rows: list[tuple] = []
    detention_billing_rows: list[tuple] = []
    invoice_rows: list[tuple] = []
    driver_snapshot_rows: list[tuple] = []

    for truck_number, driver_id, order_ids in assignments:
        hub_loc_id, hub_city = truck_hub.get(truck_number, (None, None))
        legs = sorted((orders_by_id[oid] for oid in order_ids if oid in orders_by_id), key=lambda o: o['pickup_at'])
        if not legs or hub_loc_id is None:
            continue

        prev_loc = hub_loc_id
        t_cursor_s = 0.0
        fuel = rng.uniform(70, 100)
        driver_hours_used.setdefault(driver_id, 0.0)

        for idx, order in enumerate(legs):
            is_last = idx == len(legs) - 1
            dh_coords, _ = get_route_geometry(data, prev_loc, order['pickup_location_id'])
            dh_km = route_distance_km(dh_coords)
            dh_hours = dh_km / AVG_HIGHWAY_SPEED_KMH if dh_km else 0.0
            # Real bug found directly: this used to chain PURE travel/dwell durations from
            # t_cursor_s=0 with zero reference to order['pickup_at'] (the real scheduled time from
            # dispatch.day_orders) -- every truck's simulated day started at sim_start regardless
            # of whether its first real pickup was at 6am or 8pm, so the whole replay ran as a
            # compressed physics-only chain, finishing hours before a real fleet's actual day would
            # and totally disconnected from the real times shown in the Order Book/Dispatch Board.
            real_pickup_offset_s = max(0.0, (order['pickup_at'] - sim_start).total_seconds())
            assigned_at_s = t_cursor_s
            if idx == 0:
                # First leg only: the truck has a free choice of hub-departure time (bounded below
                # by sim_start itself) -- delay departure to arrive just-in-time for the REAL
                # scheduled pickup, instead of always leaving immediately at sim_start.
                assigned_at_s = max(0.0, real_pickup_offset_s - dh_hours * 3600)
            physical_arr_s = assigned_at_s + dh_hours * 3600
            # Chained legs can't leave earlier than they actually become free (idx>0's
            # assigned_at_s above) -- if that means arriving before the NEXT order's real scheduled
            # time, the truck waits (arr_pickup_s reflects the real time, not an early grab); if it
            # can't make the real time at all, this is genuine lateness, same as the solver's own
            # feasibility check already prices in.
            arr_pickup_s = max(physical_arr_s, real_pickup_offset_s)
            dh_samples, fuel = _sample_leg(dh_coords, assigned_at_s, physical_arr_s, rng, fuel)

            dep_pickup_s = arr_pickup_s + pickup_dwell_h * 3600

            load_coords, _ = get_route_geometry(data, order['pickup_location_id'], order['dest_location_id'])
            arr_delivery_s = dep_pickup_s + order['loaded_hours'] * 3600
            load_samples, fuel = _sample_leg(load_coords, dep_pickup_s, arr_delivery_s, rng, fuel)

            is_detention = is_last and truck_number == detention_truck
            this_delivery_dwell_h = (DETENTION_FREE_HOURS + rng.uniform(0.6, 1.3)) if is_detention else delivery_dwell_h
            completed_s = arr_delivery_s + this_delivery_dwell_h * 3600
            dep_delivery_s = completed_s  # departure from the delivery dock -- captured BEFORE the
            # is_last end-of-day return leg below extends completed_s further; geofence departure
            # and detention billing both mean "left the customer's dock," not "back at the hub."
            detention_amount = round(max(0.0, this_delivery_dwell_h - DETENTION_FREE_HOURS) * DETENTION_RATE_PER_HR_CAD, 2)

            trailing_samples = []
            eod_miles = 0.0
            if is_last:
                eod_coords, _ = get_route_geometry(data, order['dest_location_id'], hub_loc_id)
                eod_km = route_distance_km(eod_coords)
                eod_hours = eod_km / AVG_HIGHWAY_SPEED_KMH if eod_km else 0.0
                eod_start_s = completed_s
                eod_end_s = eod_start_s + eod_hours * 3600
                trailing_samples, fuel = _sample_leg(eod_coords, eod_start_s, eod_end_s, rng, fuel)
                completed_s = eod_end_s
                eod_miles = eod_km * 0.621371

            trajectory = dh_samples + load_samples + trailing_samples
            deadhead_miles = dh_km * 0.621371 + eod_miles
            deadhead_cost = deadhead_miles * ASSUMED_OPERATING_COST_PER_MILE

            trip_id = uuid.uuid4()
            trip_rows.append({
                'trip_id': str(trip_id), 'order_id': order['order_id'], 'driver_id': driver_id,
                'truck_number': truck_number, 'hub_city': hub_city,
                'assigned_at_s': round(assigned_at_s, 1), 'arr_pickup_at_s': round(arr_pickup_s, 1),
                'dep_pickup_at_s': round(dep_pickup_s, 1), 'arr_delivery_at_s': round(arr_delivery_s, 1),
                'completed_at_s': round(completed_s, 1),
                'origin_location_id': order['pickup_location_id'], 'dest_location_id': order['dest_location_id'],
                'origin_label': order['pickup_label'], 'dest_label': order['dest_label'],
                'weight_lbs': order['weight_lbs'], 'pallets': order['pallets'], 'load_type': order['load_type'],
                'order_revenue': order['rate'], 'deadhead_cost': round(deadhead_cost, 2),
                'deadhead_miles': round(deadhead_miles, 1), 'net_margin': round(order['rate'] - deadhead_cost - detention_amount * 0, 2),
                'detention_amount': detention_amount, 'is_detention_demo': is_detention,
                'trajectory': trajectory,
            })

            # Real absolute timestamps for every table below -- the trip_rows dict above only
            # keeps second-offsets (what the frontend's playback timeline wants); everything that
            # goes to the DB wants real timestamptz values.
            abs_assigned = sim_start + timedelta(seconds=assigned_at_s)
            abs_arr_pickup = sim_start + timedelta(seconds=arr_pickup_s)
            abs_dep_pickup = sim_start + timedelta(seconds=dep_pickup_s)
            abs_arr_delivery = sim_start + timedelta(seconds=arr_delivery_s)
            abs_dep_delivery = sim_start + timedelta(seconds=dep_delivery_s)
            abs_completed = sim_start + timedelta(seconds=completed_s)

            sim_trip_rows.append((
                str(trip_id), str(run_id), driver_id, 'completed', 'DEPCONS', abs_arr_delivery,
                order['pickup_location_id'], order['dest_location_id'], abs_assigned,
                order['weight_lbs'], order['pallets'], order['load_type'],
                order['loaded_miles'], round(dh_km * 0.621371, 1),
            ))
            trip_log_rows.append((
                str(trip_id), str(run_id), driver_id, truck_number, abs_completed,
                order['loaded_miles'], round(dh_km * 0.621371, 1), round(eod_miles, 1),
                round(pickup_dwell_h, 2), round(this_delivery_dwell_h, 2),
                not is_detention, round(min(1.0, order['pallets'] / 28), 2), 0.0, False, 0.0,
                round(order['rate'] - deadhead_cost - detention_amount, 2),
            ))
            # Real user ask: "can we also have a couple of geofence events created" -- every trip
            # already has a real, computed arrival/departure instant at its delivery dock (that's
            # exactly what dwell time IS), so this is free given data already on hand, not a
            # separate invented pass.
            geofence_event_rows.append((str(run_id), str(trip_id), order['dest_location_id'], 'arrival', abs_arr_delivery))
            geofence_event_rows.append((str(run_id), str(trip_id), order['dest_location_id'], 'departure', abs_dep_delivery))
            detention_billing_rows.append((
                str(trip_id), order['dest_location_id'], str(run_id),
                abs_arr_delivery, abs_dep_delivery, DETENTION_FREE_HOURS, detention_amount,
            ))
            # Real user ask: "billing should be from the invoice we have created from the
            # simulation" -- the SAME quote_price() the dispatch board's own order rate and the
            # customer-facing quote page use, not a second, independently-invented invoice formula.
            price = quote_price(order['loaded_miles'], order['load_type'])
            invoice_rows.append((
                str(uuid.uuid4()), str(run_id), f"INV-{str(trip_id)[:8].upper()}", str(trip_id), None,
                abs_completed, abs_completed + timedelta(days=30),
                order['dest_label'], order['dest_label'], 'ON',
                price['linehaul_amount'], detention_amount, price['fuel_surcharge_amount'], 0.0, 'draft',
            ))
            # Event-driven snapshots (assigned/arrived-pickup/departed-pickup/arrived-delivery/
            # completed), per simulation.driver_state_snapshots' own schema comment.
            #
            # Real bug found directly (exposed by the real-pickup-time anchoring fix above): HOS
            # remaining used to be deducted by RAW WALL-CLOCK OFFSET (t_s/3600) from day start, which
            # was only ever correct by coincidence -- before that fix, t_s always equalled real
            # worked hours exactly, since every truck's timeline was one unbroken chain of work with
            # no gaps. Now that a truck can legitimately wait for a later real scheduled pickup (or
            # simply not start until well after sim_start), wall-clock offset can hugely exceed
            # actual hours WORKED, so every driver's HOS floored at 0 almost immediately -- a real
            # ELD clock only depletes during real on-duty time, never while idle/waiting. Fixed by
            # deducting the driver's own accumulated on-duty hours (driving + dwell, exactly what
            # driver_hours_used already tracks below) at each checkpoint instead of elapsed time.
            hos_drive_start, hos_duty_start = driver_hos.get(driver_id, (0.0, 0.0))
            worked_before = driver_hours_used[driver_id]
            for t_s, loc_id, duty, worked_h in (
                (assigned_at_s, prev_loc, 'on_duty_not_driving', worked_before),
                (arr_pickup_s, order['pickup_location_id'], 'on_duty_not_driving', worked_before + dh_hours),
                (dep_pickup_s, order['pickup_location_id'], 'driving', worked_before + dh_hours + pickup_dwell_h),
                (arr_delivery_s, order['dest_location_id'], 'on_duty_not_driving', worked_before + dh_hours + pickup_dwell_h + order['loaded_hours']),
                (completed_s, order['dest_location_id'] if not is_last else hub_loc_id, 'off_duty',
                 worked_before + dh_hours + pickup_dwell_h + order['loaded_hours'] + this_delivery_dwell_h),
            ):
                lat, lon = data.locations.get(loc_id, (None, None))
                driver_snapshot_rows.append((
                    str(run_id), driver_id, truck_number, str(trip_id), sim_start + timedelta(seconds=t_s),
                    lat, lon, duty,
                    round(max(0.0, hos_drive_start - worked_h), 1), round(max(0.0, hos_duty_start - worked_h), 1),
                    None, None, None, None, None, True,
                ))

            total_revenue += order['rate']
            total_deadhead_miles += deadhead_miles
            total_deadhead_cost += deadhead_cost
            total_loaded_miles += order['loaded_miles']
            n_completed += 1
            driver_hours_used[driver_id] += dh_hours + order['loaded_hours'] + pickup_dwell_h + this_delivery_dwell_h

            prev_loc = order['dest_location_id']
            t_cursor_s = completed_s

    # Real user ask: Fleet Health and Drivers should show "what happened at the end of the
    # simulation for the day" instead of live.* telemetry. One row per truck/driver actually used
    # today -- a real passing pre-trip inspection (same "everyone did their check this morning"
    # baseline sim/live/seed_demo_fleet.py already uses for the live fleet) and a plausible
    # maintenance-interval snapshot (SYNTHESIZED odometer figure -- no real per-truck service
    # history exists anywhere in the source data, same honest flag FleetHealth.tsx's own
    # description already carries).
    truck_by_driver = {driver_id: truck_number for truck_number, driver_id, _order_ids in assignments}
    truck_maintenance_rows = [
        (truck_number, str(run_id), round(rng.uniform(2000, 24000), 0), sim_start - timedelta(days=round(rng.uniform(5, 80))))
        for truck_number, _driver_id, _order_ids in assignments
    ]
    vehicle_inspection_rows = [
        (str(run_id), driver_id, truck_by_driver.get(driver_id), sim_start - timedelta(hours=rng.uniform(0.5, 3)),
         round(rng.uniform(50000, 300000), 0), True)
        for driver_id in driver_hours_used
    ]

    avg_hos_remaining_start = sum(h[0] for h in driver_hos.values()) / len(driver_hos) if driver_hos else 0.0
    avg_hours_used = sum(driver_hours_used.values()) / len(driver_hours_used) if driver_hours_used else 0.0
    n_drivers_used = len(driver_hours_used)

    summary = {
        'n_orders_generated': len(orders_by_id), 'n_completed': n_completed,
        'n_unassigned': len(orders_by_id) - n_completed, 'n_decisions': n_completed,
        'total_revenue': round(total_revenue, 2), 'total_deadhead_cost': round(total_deadhead_cost, 2),
        'total_deadhead_miles': round(total_deadhead_miles, 1), 'net_margin': round(total_revenue - total_deadhead_cost, 2),
        'n_drivers_used': n_drivers_used, 'avg_hos_remaining_at_start': round(avg_hos_remaining_start, 1),
        'avg_driver_hours_used': round(avg_hours_used, 1),
        'total_detention_billed': round(sum(t['detention_amount'] for t in trip_rows), 2),
    }

    day_end = sim_start + timedelta(hours=24)
    # n_drivers_used/avg_hos_*/avg_driver_hours_used/total_deadhead_miles aren't their own columns
    # on simulation.runs -- stashed in the existing fleet_metrics jsonb column instead (already
    # null-by-default for every other run_kind) so a later GET (reload) returns the identical
    # summary a fresh run does, not a thinner one.
    with cursor() as cur:
        cur.execute(
            """insert into simulation.runs
               (run_id, seed, created_at, run_kind, scenario_label, week_start, week_end,
                driver_ids, n_orders_generated, n_completed, n_unassigned, total_revenue,
                total_detention_billed, total_deadhead_cost, net_margin, status, fleet_metrics)
               values (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(run_id), seed or 0, 'ai_dispatch_day', service_date.isoformat(), sim_start, day_end,
             list(driver_hours_used.keys()), summary['n_orders_generated'], summary['n_completed'],
             summary['n_unassigned'], summary['total_revenue'], summary['total_detention_billed'],
             summary['total_deadhead_cost'], summary['net_margin'], 'complete', Json(summary)),
        )
        rows = [
            (str(uuid.uuid4()), str(run_id), t['driver_id'], t['truck_number'], t['hub_city'],
             t['assigned_at_s'], t['completed_at_s'], t['arr_pickup_at_s'], t['dep_pickup_at_s'], t['arr_delivery_at_s'],
             t['origin_location_id'], t['dest_location_id'], t['origin_label'], t['dest_label'],
             t['weight_lbs'], t['pallets'], t['load_type'], t['order_revenue'], t['deadhead_cost'],
             t['deadhead_miles'], t['net_margin'], t['detention_amount'], t['is_detention_demo'], Json(t['trajectory']))
            for t in trip_rows
        ]
        execute_values(cur, """
            insert into simulation.ai_dispatch_trips
            (trip_id, run_id, driver_id, truck_number, hub_city, assigned_at_s, completed_at_s,
             arr_pickup_at_s, dep_pickup_at_s, arr_delivery_at_s, origin_location_id, dest_location_id,
             origin_label, dest_label, weight_lbs, pallets, load_type, order_revenue, deadhead_cost,
             deadhead_miles, net_margin, detention_amount, is_detention_demo, trajectory)
            values %s
        """, rows)

        # Real user ask: "the simulation is now our data source" -- Trip History, the new Data
        # page, and Billing all read the fuller simulation.* schema (sim/sql/038) directly, not
        # simulation.ai_dispatch_trips (that table only ever existed for this page's own map/
        # timeline playback). Populated from the exact same per-order computation above.
        execute_values(cur, """
            insert into simulation.trips
            (trip_id, run_id, driver_id, status, last_event, eta, origin_location_id,
             dest_location_id, created_at, weight_lbs, pallets, load_type, loaded_miles,
             pre_pickup_deadhead_miles)
            values %s
        """, sim_trip_rows)
        execute_values(cur, """
            insert into simulation.trip_log
            (trip_id, run_id, driver_id, truck_number, completed_at, loaded_miles,
             pre_pickup_deadhead_miles, post_delivery_deadhead_miles, pickup_dwell_hours,
             delivery_dwell_hours, on_time, load_fill_ratio, hos_stranding_risk,
             breakdown_occurred, breakdown_repair_hours, reward_total)
            values %s
        """, trip_log_rows)
        execute_values(cur, """
            insert into simulation.geofence_events (run_id, trip_id, location_id, event_type, occurred_at)
            values %s
        """, geofence_event_rows)
        execute_values(cur, """
            insert into simulation.detention_billing
            (trip_id, location_id, run_id, arrival_at, departure_at, free_hours, amount)
            values %s
        """, detention_billing_rows)
        execute_values(cur, """
            insert into simulation.invoices
            (invoice_id, run_id, invoice_number, trip_id, quote_id, issued_at, due_at,
             bill_to_name, bill_to_address, delivery_province, linehaul_amount, detention_amount,
             fuel_surcharge_amount, accessorial_amount, status)
            values %s
        """, invoice_rows)
        execute_values(cur, """
            insert into simulation.driver_state_snapshots
            (run_id, driver_id, truck_number, trip_id, snapshot_at, lat, lon, duty_status,
             hos_driving_hours_remaining, hos_duty_hours_remaining, hos_cycle1_hours_remaining,
             hos_cycle2_hours_remaining, truck_breakdown_risk, truck_pct_km_interval,
             truck_pct_days_interval, inspection_ok)
            values %s
        """, driver_snapshot_rows)
        execute_values(cur, """
            insert into simulation.truck_maintenance_state
            (truck_number, run_id, cumulative_km_since_service, last_service_at)
            values %s
        """, truck_maintenance_rows)
        execute_values(cur, """
            insert into simulation.vehicle_inspections
            (run_id, driver_id, truck_number, submitted_at, odometer_km, overall_pass)
            values %s
        """, vehicle_inspection_rows)

    return {'run_id': str(run_id), 'service_date': service_date.isoformat(), 'summary': summary, 'trips': trip_rows}


if __name__ == "__main__":
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else (datetime.now(timezone.utc).date() + timedelta(days=1))
    result = generate(target)
    print(f"Generated AI-dispatch-day replay {result['run_id']} for {target}: {result['summary']}")
