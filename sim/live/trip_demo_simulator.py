"""Single-trip, live-ticking demo engine behind the dashboard's "Simulation Trip" page -- separate
from both `sim/live/telemetry_simulator.py` (ticks the WHOLE live fleet, writes to `live.*`) and
the batch week-long Simulation Showcase (`sim/engine/run_sim.py` + `simulation.*`, computed
instantly then replayed). This runs exactly ONE trip forward in real time, specifically to
showcase the brief's "critical requirement" end to end: a truck crosses a geofence, the exact
arrival/departure timestamp is recorded automatically, and detention past the 2h free window gets
billed -- narratable live, not after the fact.

Real design choices, flagged not hidden (same standard as telemetry_simulator.py's own docstring):
- Writes ONLY to `simulation.*`, via `simulation.process_position_tick()` (sim/sql/044) -- the
  SAME buffered arrival/departure state-machine logic as the real `live.process_position_tick()`
  (sim/sql/008), copied line-for-line and retargeted, not a new mechanism invented for the demo.
  Never touches `live.*` -- a demo trip has no business appearing on the real Live Ops map.
- The driver starts AT the pickup location (zero pre-pickup deadhead) -- this demo's story is the
  DELIVERY leg's dock dwell/detention, not deadhead economics (that's what the Simulation Showcase
  already covers). The pickup-side dwell still runs an ARRIVAL through the same geofence trigger
  (short, fixed duration) but, matching the existing live simulator's own behavior, never confirms
  a pickup DEPARTURE -- the truck's next movement is only checked against the delivery geofence,
  same simplification telemetry_simulator.py already carries. Only the delivery leg gets a real,
  confirmed arrival-AND-departure cycle (see Phase 4 below) -- that's the leg this demo is about.
- `delivery_dwell_hours` is a DELIBERATE, forced value per scenario (not sampled from
  calibration.dwell_time_dist) -- that's the entire point of running the same lane twice: one run
  finishes inside the 2h free window, the other doesn't, so the SAME trigger produces two
  different, real, database-recorded outcomes to compare.
- odometer_km/fuel_pct/speed_mph are SYNTHESIZED, same honest flag as telemetry_simulator.py and
  live.driver_status's own columns (sim/sql/033) -- no real telemetry source exists in this
  project's data for any of these.
- No real 401-traffic dataset exists (confirmed -- the trained model itself was built against a
  no-traffic sim); the occasional slowdown below is the same cosmetic-but-ETA-affecting synthetic
  event telemetry_simulator.py already uses for the brief's "Event Generator" ask, not a claim of
  real traffic simulation.
"""
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json

from sim.config import quote_price
from sim.db import cursor, get_connection
from sim.engine.route_interpolation import get_route_geometry, interpolate_position, route_distance_km
from sim.engine.run_sim import SimData, get_route, load_sim_data

TICK_SECONDS = 0.5
TIME_SCALE = 120  # 1 real second = 120 simulated seconds -- one tick = 1 sim-minute at the
                   # TICK_SECONDS above. A multi-hour trip + dwell finishes in roughly 4-6 real
                   # minutes -- both scenarios now run concurrently (real user ask: one button,
                   # not two sequential waits), so the page's real wall-clock cost is one run's
                   # duration, not the sum of both.
# Every tick below gets written to simulation.trip_telemetry_log -- real user ask: the MAP should
# move on real GPS-density positions (one every sim-minute), not a throttled subset. The readable
# "Live log" text list still only DISPLAYS entries ~2 sim-minutes apart -- thinned client-side in
# SimulationTrip.tsx, not by writing less data here.
CRUISE_SPEED_MPH = (58, 66)
SLOWDOWN_SPEED_MPH = (15, 28)
SLOWDOWN_PROBABILITY_PER_TICK = 0.02
SLOWDOWN_EXTRA_HOURS = (0.1, 0.25)
FUEL_PCT_PER_KM = 100 / 700  # ~700 km synthesized tank range, same figure telemetry_simulator.py uses
KM_PER_MILE = 1.60934
PICKUP_DWELL_HOURS = 0.35  # ~21 min -- short and fixed; not this demo's focus

# The two scenarios the page runs side by side, same lane, same mechanism -- only the delivery
# dock dwell differs. detention_free_hours (2h, calibration.assumptions) is the dividing line.
SCENARIOS = {
    "baseline": {"label": "Normal dock time — inside the free 2h window", "delivery_dwell_hours": 1.25},
    "detention": {"label": "Extended dock time — past the free 2h window", "delivery_dwell_hours": 3.5},
}


@dataclass
class DemoTripHandle:
    run_id: uuid.UUID
    trip_id: uuid.UUID
    quote_id: uuid.UUID
    driver_id: int
    truck_number: str
    origin_id: int
    dest_id: int
    scenario: str
    # Computed ONCE here and reused by run_trip_demo below AND returned to the frontend by the
    # /api/trip-demo/start response, instead of the frontend independently re-fetching the same
    # lane via /api/route -- a second, separate call to the same underlying function that isn't
    # guaranteed deterministic on every path (the live-OSRM fallback branch isn't a stable cached
    # lookup). Checked directly: calibration.lane_routes has no duplicate rows for this lane, so
    # that wasn't actually the cause of the "path splitting" report below -- but there's no reason
    # to keep two independent fetches of the one thing when a single source of truth is just as
    # easy and removes the whole class of risk.
    route_coords: list[tuple[float, float]]


def pick_demo_driver(data: SimData, rng: random.Random) -> tuple[int, str]:
    """Any driver with a real (not synthesized-pool) truck on record, so the demo fleet always
    matches a genuine ground_truth.driver_equipment pairing."""
    driver_id, truck_number = rng.choice(list(data.driver_default_truck.items()))
    return driver_id, truck_number


def start_trip_demo(
    origin_id: int, dest_id: int, scenario: str, *,
    weight_lbs: float = 22000, pallets: float = 12, load_type: str = "Dry Van",
    driver_id: int | None = None,
) -> DemoTripHandle:
    """Creates the run/quote/trip rows and returns immediately -- the actual ticking happens in
    `run_trip_demo`, meant to be launched in a background thread by the caller (dashboard/server/
    main.py's POST /api/trip-demo/start) so the HTTP request doesn't block for the trip's whole
    (accelerated) duration.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}, expected one of {list(SCENARIOS)}")
    data = load_sim_data()
    rng = random.Random()
    if driver_id is not None:
        truck_number = data.driver_default_truck.get(driver_id) or rng.choice(data.truck_numbers)
    else:
        driver_id, truck_number = pick_demo_driver(data, rng)

    run_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    quote_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    route_coords, _is_real = get_route_geometry(data, origin_id, dest_id)

    with cursor() as cur:
        cur.execute(
            """insert into simulation.runs (run_id, seed, created_at, week_start, week_end,
                                             driver_ids, status, run_kind, scenario_label)
               values (%s, %s, %s, %s, %s, %s, 'running', 'trip_demo', %s)""",
            (run_id, rng.randint(0, 2**31), now, now, now, [driver_id], SCENARIOS[scenario]["label"]),
        )
        cur.execute(
            """insert into simulation.quote_requests
                 (quote_id, run_id, origin_location_id, dest_location_id, requested_at,
                  requested_pickup_at, weight_lbs, pallets, load_type, service_type, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'FTL', 'assigned')""",
            (quote_id, run_id, origin_id, dest_id, now, now, weight_lbs, pallets, load_type),
        )
        cur.execute(
            """insert into simulation.trips
                 (trip_id, run_id, driver_id, status, last_event, eta, origin_location_id,
                  dest_location_id, created_at, weight_lbs, pallets, load_type,
                  pre_pickup_deadhead_miles, quote_id, driver_location_id)
               values (%s, %s, %s, 'assigned', 'ASSIGNED', %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)""",
            (trip_id, run_id, driver_id, now, origin_id, dest_id, now, weight_lbs, pallets, load_type,
             quote_id, origin_id),
        )

    return DemoTripHandle(run_id=run_id, trip_id=trip_id, quote_id=quote_id, driver_id=driver_id,
                           truck_number=truck_number, origin_id=origin_id, dest_id=dest_id, scenario=scenario,
                           route_coords=route_coords)


def _tick_geofence(cur, handle: DemoTripHandle, location_id: int, lat: float, lon: float, sim_clock: datetime) -> None:
    cur.execute(
        "select simulation.process_position_tick(%s, %s, %s, %s, ST_GeogFromText(%s), %s)",
        (handle.trip_id, handle.driver_id, location_id, handle.run_id, f"POINT({lon} {lat})", sim_clock),
    )


def _write_telemetry(cur, handle: DemoTripHandle, sim_clock: datetime, phase: str,
                      lat: float | None, lon: float | None, speed_mph: float, fuel_pct: float,
                      odometer_km: float, note: str | None = None) -> None:
    cur.execute(
        """insert into simulation.trip_telemetry_log
             (run_id, trip_id, recorded_at, phase, lat, lon, speed_mph, fuel_pct, odometer_km, note)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (handle.run_id, handle.trip_id, sim_clock, phase, lat, lon, speed_mph, fuel_pct, odometer_km, note),
    )


def run_trip_demo(handle: DemoTripHandle) -> None:
    """Ticks the trip forward to completion. Blocking -- run this in a background thread."""
    data = load_sim_data()
    rng = random.Random()
    dwell_hours = SCENARIOS[handle.scenario]["delivery_dwell_hours"]

    sim_clock = datetime.now(timezone.utc)
    t0 = sim_clock
    odometer_km = 0.0
    fuel_pct = 100.0
    trajectory: list[list[float]] = []

    coords = handle.route_coords  # computed once in start_trip_demo -- see DemoTripHandle's own comment
    dist_miles, loaded_hours = get_route(data, handle.origin_id, handle.dest_id)
    loaded_hours = max(loaded_hours, 0.05)
    total_km = route_distance_km(coords) if coords else 0.0
    origin_lat, origin_lon = data.locations[handle.origin_id]
    dest_lat, dest_lon = data.locations[handle.dest_id]

    # ONE connection held open for the whole run, committed per tick -- ticking against remote
    # Supabase, an earlier draft that opened a fresh connection per tick (matching telemetry_
    # simulator.py's OWN per-outer-tick pattern, fine there since one tick covers the whole live
    # fleet) added real per-tick network round-trip latency here, where one tick is only one
    # trip's worth of work -- found directly in a live test run (a 240s run only reached the
    # ARRCONS status, nowhere near complete). Reusing the connection fixed it.
    conn = get_connection()
    try:
        cur = conn.cursor()

        def commit_tick() -> None:
            conn.commit()

        def log_tick(phase, lat, lon, speed, note=None):
            # EVERY call writes now (no throttling) -- see the module-level comment by
            # TICK_SECONDS/TIME_SCALE on why: the map wants real GPS-density positions, and
            # thinning the readable log list to ~2 sim-minutes is the frontend's job, not this
            # write path's.
            _write_telemetry(cur, handle, sim_clock, phase, lat, lon, speed, fuel_pct, odometer_km, note)

        cur.execute("update simulation.trips set status = 'at_pickup', last_event = 'ARRSHIP' where trip_id = %s",
                     (handle.trip_id,))
        commit_tick()

        # Phase 1: pickup dwell -- short and fixed, still runs through the real geofence trigger.
        dwell_until = sim_clock + timedelta(hours=PICKUP_DWELL_HOURS)
        while sim_clock < dwell_until:
            sim_clock = sim_clock + timedelta(seconds=TICK_SECONDS * TIME_SCALE)
            trajectory.append([(sim_clock - t0).total_seconds(), origin_lat, origin_lon])
            _tick_geofence(cur, handle, handle.origin_id, origin_lat, origin_lon, sim_clock)
            log_tick("dwell_pickup", origin_lat, origin_lon, 0)
            commit_tick()
            time.sleep(TICK_SECONDS)

        cur.execute("update simulation.trips set status = 'in_transit', last_event = 'DEPSHIP', loaded_miles = %s where trip_id = %s",
                     (dist_miles, handle.trip_id))
        commit_tick()

        # Phase 2: loaded leg to delivery -- real OSRM route geometry, real leg duration, occasional
        # synthetic slowdown (see module docstring on why this isn't real traffic simulation).
        #
        # `max_fraction` is a real bug fix, not defensive padding: `fraction = elapsed / (loaded_hours
        # + extra_delay_hours)` recomputes from the TOTAL leg duration every tick, and a slowdown
        # firing adds a full 0.1-0.25h to that denominator IN ONE TICK -- far more than the ~1
        # sim-minute `elapsed` gains in the same tick. The naive fraction therefore DROPS the
        # instant a slowdown fires, which walks `interpolate_position` backward along the route --
        # the truck's marker (and the drawn "already driven" line) visibly jumps back and re-treads
        # forward, looking exactly like the route "splitting into multiple paths" a live test run
        # showed. A slowdown should slow future progress, not undo progress already made -- clamping
        # to the highest fraction reached so far keeps position monotonically forward while still
        # genuinely slowing the RATE of further progress (each subsequent tick's raw fraction grows
        # against the larger denominator).
        leg_start = sim_clock
        extra_delay_hours = 0.0
        max_fraction = 0.0
        while True:
            sim_clock = sim_clock + timedelta(seconds=TICK_SECONDS * TIME_SCALE)
            tick_hours = (TICK_SECONDS * TIME_SCALE) / 3600
            note = None
            if rng.random() < SLOWDOWN_PROBABILITY_PER_TICK:
                extra = rng.uniform(*SLOWDOWN_EXTRA_HOURS)
                extra_delay_hours += extra
                note = "401 slowdown — synthetic, ETA-affecting (not real traffic data)"
            elapsed_h = (sim_clock - leg_start).total_seconds() / 3600
            effective_duration = loaded_hours + extra_delay_hours
            raw_fraction = min(1.0, elapsed_h / effective_duration) if effective_duration > 0 else 1.0
            fraction = max(raw_fraction, max_fraction)
            max_fraction = fraction
            pos = interpolate_position(coords, fraction) if coords else (dest_lat, dest_lon)
            if pos:
                lat, lon = pos
                speed = rng.uniform(*SLOWDOWN_SPEED_MPH) if note else rng.uniform(*CRUISE_SPEED_MPH)
                step_km = total_km * (tick_hours / max(effective_duration, 0.01))
                odometer_km += step_km
                fuel_pct = max(5.0, fuel_pct - step_km * FUEL_PCT_PER_KM)
                trajectory.append([(sim_clock - t0).total_seconds(), lat, lon])
                _tick_geofence(cur, handle, handle.dest_id, lat, lon, sim_clock)
                log_tick("to_delivery", lat, lon, round(speed, 1), note)
                commit_tick()
            if fraction >= 1.0:
                break
            time.sleep(TICK_SECONDS)

        cur.execute("update simulation.trips set status = 'at_delivery', last_event = 'ARRCONS' where trip_id = %s",
                     (handle.trip_id,))
        commit_tick()

        # Phase 3: delivery dock dwell -- THE scenario-defining phase. Continuous ticks against the
        # SAME buffered geofence function the whole demo hinges on: `dwell_hours` (forced, per
        # scenario) determines whether this run crosses the 2h free-detention line or not.
        dwell_until = sim_clock + timedelta(hours=dwell_hours)
        while sim_clock < dwell_until:
            sim_clock = sim_clock + timedelta(seconds=TICK_SECONDS * TIME_SCALE)
            trajectory.append([(sim_clock - t0).total_seconds(), dest_lat, dest_lon])
            _tick_geofence(cur, handle, handle.dest_id, dest_lat, dest_lon, sim_clock)
            log_tick("dwell_delivery", dest_lat, dest_lon, 0,
                            "stationary — dock dwell, geofence buffer tracking arrival/departure")
            commit_tick()
            time.sleep(TICK_SECONDS)

        cur.execute("update simulation.trips set last_event = 'DEPCONS' where trip_id = %s", (handle.trip_id,))
        commit_tick()

        # Phase 4: pull away from the dock -- confirms the DEPARTURE half of the geofence trigger.
        # (A real, pre-existing gap in the live telemetry simulator, sim/live/telemetry_simulator.py:
        # a completed delivery leg there never actually drives the truck away from the destination, so
        # a departure event -- and therefore any live.detention_billing amount -- never fires for a
        # delivery stop either. Deliberately fixed here so this demo's with/without-detention story is
        # a real, complete arrival-AND-departure cycle, not just half of one.)
        #
        # Looped until the trigger actually confirms departed_at (checked directly each tick), not
        # a guessed fixed duration -- an earlier version computed a fixed window from buffer_minutes
        # and undershot it (the buffer counts from the FIRST outside tick, not from loop start, so
        # a `buffer_minutes + small margin` window ran out just before the buffer elapsed; caught
        # directly in a live test run where departed_at stayed null after "completion"). Safety-
        # capped so a misconfigured buffer_minutes can't hang the demo forever.
        away_lat, away_lon = dest_lat + 0.01, dest_lon + 0.01  # ~1km away -- comfortably outside any configured radius_m (default 120m)
        departed_confirmed = False
        for _ in range(40):  # 40 ticks * 2 sim-min/tick = 80 sim-min of margin, comfortably above any real buffer_minutes
            sim_clock = sim_clock + timedelta(seconds=TICK_SECONDS * TIME_SCALE)
            trajectory.append([(sim_clock - t0).total_seconds(), away_lat, away_lon])
            _tick_geofence(cur, handle, handle.dest_id, away_lat, away_lon, sim_clock)
            log_tick("departed", away_lat, away_lon, round(rng.uniform(*CRUISE_SPEED_MPH), 1),
                            "pulling away from the dock")
            commit_tick()
            cur.execute("select departed_at from simulation.geofence_dwell_state where trip_id = %s and location_id = %s",
                         (handle.trip_id, handle.dest_id))
            row = cur.fetchone()
            if row and row[0] is not None:
                departed_confirmed = True
                break
            time.sleep(TICK_SECONDS)
        if not departed_confirmed:
            print(f"[trip_demo_simulator] WARNING: departure never confirmed for trip {handle.trip_id} "
                  f"within the safety margin -- detention amount will be understated for this run.")
    finally:
        conn.close()

    _complete_trip_demo(handle, dist_miles, trajectory=trajectory, completed_at=sim_clock)


def _complete_trip_demo(handle: DemoTripHandle, loaded_miles: float,
                         trajectory: list[list[float]], completed_at: datetime) -> None:
    """Writes trip_log + invoice, same shape/columns the batch Simulation Showcase and the live
    invoice endpoint already use (sim/sql/030/038/039), so the existing `SimulationOrderStory`
    component and `/api/simulation/orders/{quote_id}/story` endpoint render this demo trip with NO
    frontend changes needed for that part.
    """
    with cursor() as cur:
        cur.execute("select load_type from simulation.trips where trip_id = %s", (handle.trip_id,))
        (load_type,) = cur.fetchone()
        deadhead_cost = 0.0  # driver started at pickup for this demo -- see module docstring

        cur.execute("select amount from simulation.detention_billing where trip_id = %s and location_id = %s",
                     (handle.trip_id, handle.dest_id))
        det_row = cur.fetchone()
        detention_amount = float(det_row[0]) if det_row and det_row[0] is not None else 0.0

        pricing = quote_price(float(loaded_miles or 0), load_type or "Dry Van", "FTL", 1.0)
        linehaul_amount = pricing["linehaul_amount"]
        fuel_surcharge_amount = pricing["fuel_surcharge_amount"]

        cur.execute(
            """insert into simulation.trip_log
                 (trip_id, run_id, driver_id, truck_number, completed_at, loaded_miles,
                  pre_pickup_deadhead_miles, post_delivery_deadhead_miles, pickup_dwell_hours,
                  delivery_dwell_hours, on_time, load_fill_ratio, breakdown_occurred,
                  order_revenue, deadhead_cost, post_delivery_deadhead_cost, lateness_penalty_amount)
               values (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, true, 1.0, false, %s, %s, 0, 0)""",
            (handle.trip_id, handle.run_id, handle.driver_id, handle.truck_number, completed_at, loaded_miles,
             PICKUP_DWELL_HOURS, SCENARIOS[handle.scenario]["delivery_dwell_hours"], linehaul_amount, deadhead_cost),
        )

        cur.execute("select count(*) from simulation.invoices")
        (seq,) = cur.fetchone()
        invoice_id = uuid.uuid4()
        invoice_number = f"RS-DEMO-{datetime.now(timezone.utc).year}-{seq + 1:04d}"
        cur.execute(
            """insert into simulation.invoices
                 (invoice_id, run_id, invoice_number, trip_id, quote_id, issued_at, due_at,
                  bill_to_name, bill_to_address, delivery_province, linehaul_amount, detention_amount,
                  fuel_surcharge_amount, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ON', %s, %s, %s, 'draft')""",
            (invoice_id, handle.run_id, invoice_number, handle.trip_id, handle.quote_id, completed_at,
             completed_at + timedelta(days=30), f"Demo shipper — location {handle.dest_id}",
             f"Demo shipper — location {handle.dest_id}", linehaul_amount, detention_amount, fuel_surcharge_amount),
        )

        cur.execute(
            """update simulation.trips set status = 'completed', last_event = 'COMPLETE', trajectory = %s
               where trip_id = %s""",
            (Json(trajectory), handle.trip_id),
        )
        cur.execute("update simulation.runs set status = 'complete', n_completed = 1 where run_id = %s", (handle.run_id,))
