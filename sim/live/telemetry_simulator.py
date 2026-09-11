"""Fleet Telematics Simulator -- the brief's "System Component 2: Separated Simulation Engine"
(backend service simulating live truck movement, GPS/speed/odometer/HOS, route delays, dock wait
times), wired to `live.*` so the dashboard's Live Ops map, geofence/detention billing, and Trip
History are all real once this is running -- not canned data.

Moves every driver with an active live.trips row along REAL OSRM route geometry
(sim/engine/route_interpolation.py -- the same function /api/route uses, not a second
implementation), through a real pickup -> dwell -> delivery -> dwell -> complete lifecycle,
calling the already-built live.process_position_tick() (sim/sql/008) at each tick so geofence
arrival/departure and the detention-billing cron job (sim/sql/010) work completely unchanged.

## Demo time acceleration -- flagged explicitly, not hidden

TIME_SCALE compresses a real multi-hour trip into a few real minutes so a demo doesn't require
literally waiting hours. Truck SPEED stays a realistic mph value (this only accelerates the
CLOCK the trip's progress is measured against, matching how e.g. flight-tracker demo replays
work) -- HOS/dwell/detention math is internally consistent because every duration (leg time,
dwell time, the geofence function's buffer) is compared against the same accelerated clock.

## What's real vs. simplified here (same honesty standard as score_quote.py's own docstring)

- Real: route geometry/timing (OSRM), dwell-time distribution shape (calibration.dwell_time_dist),
  the geofence buffer state machine (unchanged, sim/sql/008), detention billing (unchanged cron).
- Synthesized, flagged: odometer_km/fuel_pct (no real telemetry source exists anywhere in this
  project -- same as live.truck_maintenance_state's own header comment), the 401-slowdown event
  (a random duration-extension, not routed against real traffic data), on_time is left NULL at
  trip completion (live.trips doesn't yet track a promised-delivery deadline to compare against --
  a real gap, not silently defaulted to true), post_delivery_deadhead_miles is left 0 (this
  simplified simulator doesn't model a driver picking up a NEW order immediately after delivery --
  every completed trip returns the driver to idle), breakdown_occurred is left false (the sim
  engine's breakdown model, sim/engine/maintenance.py, isn't wired into live telemetry yet).
"""
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sim.db import cursor
from sim.engine.route_interpolation import get_route_geometry, interpolate_position, route_distance_km
from sim.engine.run_sim import driver_home_hub_id, get_route, load_sim_data, sample_dwell_hours

TICK_SECONDS = 4
TIME_SCALE = 30  # 1 real second = 30 simulated seconds -- see module docstring
SNAPSHOT_INTERVAL = timedelta(minutes=15)  # live.driver_state_snapshots cadence -- see plan decision 6
CRUISE_SPEED_MPH = (58, 66)
SLOWDOWN_SPEED_MPH = (15, 28)
SLOWDOWN_PROBABILITY_PER_TICK = 0.015  # ~ once every ~30 ticks of active driving, on average
SLOWDOWN_EXTRA_HOURS = (0.25, 0.6)  # sim-hours added to the leg when a slowdown fires
FUEL_PCT_PER_KM = 100 / 700  # ~700 km synthesized tank range -- flagged, no real fuel data exists
KM_PER_MILE = 1.60934


@dataclass
class LegState:
    phase: str  # 'to_pickup' | 'dwell_pickup' | 'to_delivery' | 'dwell_delivery'
    coords: list[tuple[float, float]]
    duration_hours: float
    started_sim: datetime
    extra_delay_hours: float = 0.0
    dwell_until_sim: datetime | None = None
    total_km: float = field(default=0.0)
    dist_miles: float = 0.0
    pickup_dwell_hours: float = 0.0  # carried from the pickup dwell so trip completion can log both dwell figures


def _load_active_trips():
    with cursor() as cur:
        cur.execute("""
            select t.trip_id, t.driver_id, t.status, t.origin_location_id, t.dest_location_id,
                   ds.truck_number
            from live.trips t
            join live.driver_status ds on ds.driver_id = t.driver_id
            where t.status in ('assigned', 'at_pickup', 'in_transit', 'at_delivery')
        """)
        return cur.fetchall()


def _start_leg(data, origin_id: int, dest_id: int, phase: str, sim_clock: datetime) -> LegState:
    coords, _is_real = get_route_geometry(data, origin_id, dest_id)
    dist_miles, duration_hours = get_route(data, origin_id, dest_id)
    return LegState(phase=phase, coords=coords, duration_hours=max(duration_hours, 0.05), started_sim=sim_clock,
                     total_km=route_distance_km(coords) if coords else 0.0, dist_miles=dist_miles)


def _complete_trip(cur, data, trip_id: uuid.UUID, driver_id: int, truck_number: str,
                    origin_id: int, dest_id: int, pickup_dwell_h: float, delivery_dwell_h: float, now):
    cur.execute("""select weight_lbs, pallets, load_type, loaded_miles, pre_pickup_deadhead_miles, quote_id
                    from live.trips where trip_id = %s""", (str(trip_id),))
    weight_lbs, pallets, load_type, loaded_miles, pre_deadhead, quote_id = cur.fetchone()
    from sim.config import CAPACITY_BY_LOAD_TYPE, CAPACITY_PALLETS_BY_LOAD_TYPE
    cap_lbs = CAPACITY_BY_LOAD_TYPE.get(load_type, 44500)
    cap_pallets = CAPACITY_PALLETS_BY_LOAD_TYPE.get(load_type, 26)
    fill = max((weight_lbs or 0) / cap_lbs, (pallets or 0) / cap_pallets)

    cur.execute("""
        insert into live.trip_log
          (trip_id, driver_id, truck_number, completed_at, loaded_miles, pre_pickup_deadhead_miles,
           post_delivery_deadhead_miles, pickup_dwell_hours, delivery_dwell_hours, on_time,
           load_fill_ratio, breakdown_occurred)
        values (%s, %s, %s, %s, %s, %s, 0, %s, %s, null, %s, false)
    """, (str(trip_id), driver_id, truck_number, now, loaded_miles, pre_deadhead, pickup_dwell_h, delivery_dwell_h, fill))

    cur.execute("select value from calibration.assumptions where key = 'operating_cost_per_mile'")
    op_cost_per_mile = float(cur.fetchone()[0])
    revenue = None
    if quote_id:
        cur.execute("select expected_revenue from live.quote_recommendations where quote_id = %s and driver_id = %s",
                     (quote_id, driver_id))
        row = cur.fetchone()
        revenue = float(row[0]) if row else None
    cur.execute("""
        insert into live.trip_costs (trip_id, loaded_miles, deadhead_miles, operating_cost_per_mile, revenue)
        values (%s, %s, %s, %s, %s)
    """, (str(trip_id), loaded_miles, pre_deadhead, op_cost_per_mile, revenue))

    cur.execute("""update live.trips set status = 'completed', last_event = 'COMPLETE' where trip_id = %s""", (str(trip_id),))
    # Home-time retarget (sim/sql/046, documents/logs/25): a real arrival at the driver's OWN home
    # hub, stamped as it actually happens -- `data` is None only in a test that doesn't care about
    # this (see test_live_multi_trip_scoring.py), never in the real running simulator.
    is_home_arrival = data is not None and dest_id == driver_home_hub_id(data, driver_id)
    cur.execute("""
        update live.driver_status set current_trip_id = null, duty_status = 'off_duty',
               last_location_id = %s, position = null, speed_mph = 0, fuel_pct = 100, updated_at = now(),
               last_home_arrival_at = case when %s then now() else last_home_arrival_at end
        where driver_id = %s
    """, (dest_id, is_home_arrival, driver_id))

    # Real multi-trip booking (sim/sql/042, documents/logs/23-24 / feature_reference_and_inference_
    # guide.md Section 4): a driver can already have FUTURE trips booked ('scheduled' -- see
    # /api/assign) queued behind whatever was just completed. Promote the earliest one (by eta) to
    # 'assigned' so the tick loop above picks it up next iteration and starts it from wherever this
    # driver actually is now -- _start_leg()'s own 'assigned' branch already resolves "from"
    # position via live.driver_status.last_location_id (just set above), so the queued trip
    # correctly departs from dest_id, not a stale earlier position.
    cur.execute(
        """select trip_id from live.trips where driver_id = %s and status = 'scheduled'
           order by eta asc limit 1""",
        (driver_id,),
    )
    next_row = cur.fetchone()
    if next_row:
        cur.execute("update live.trips set status = 'assigned' where trip_id = %s", (next_row[0],))


def _write_driver_snapshot(cur, driver_id: int, truck_number: str, trip_id: uuid.UUID,
                            duty_status: str, lat: float | None, lon: float | None, sim_clock):
    """The 15-min periodic feature-state snapshot (sim/sql/038) -- what Trip History's expand
    view reads instead of the bare position-only live.position_history. Home-base-return retarget
    (sim/sql/042, documents/logs/23-24): reads the 4 REAL, independently-tracked HOS sub-clocks
    now maintained on live.driver_status (see this file's tick-update SQL above) -- no longer a
    single blended figure duplicated 4 times.
    """
    cur.execute(
        """select hos_remaining_hours, hos_driving_hours_remaining, hos_duty_hours_remaining,
                  hos_cycle1_hours_remaining, hos_cycle2_hours_remaining
           from live.driver_status where driver_id = %s""",
        (driver_id,),
    )
    row = cur.fetchone()
    hos_h, hos_driving, hos_duty, hos_cycle1, hos_cycle2 = (
        (float(v) if v is not None else None) for v in row
    ) if row else (None, None, None, None, None)
    # Fall back to the blended figure only if a real sub-clock is somehow still null (a driver
    # seeded before sim/sql/042, or a genuine gap) -- never silently write None into a NOT-nullable
    # display column.
    hos_driving = hos_driving if hos_driving is not None else hos_h
    hos_duty = hos_duty if hos_duty is not None else hos_h
    hos_cycle1 = hos_cycle1 if hos_cycle1 is not None else hos_h
    hos_cycle2 = hos_cycle2 if hos_cycle2 is not None else hos_h
    # breakdown_risk/pct_of_km_interval/pct_of_days_interval are TruckMaintenanceState PROPERTIES
    # (sim/engine/maintenance.py), not stored columns -- reconstructed from the raw stored figures.
    cur.execute(
        "select cumulative_km_since_service, service_interval_km, extract(epoch from (now() - last_service_at))/86400, service_interval_days "
        "from live.truck_maintenance_state where truck_number = %s",
        (truck_number,),
    )
    tm_row = cur.fetchone()
    pct_km = pct_days = breakdown_risk = None
    if tm_row:
        cum_km, interval_km, days_since, interval_days = tm_row
        pct_km = float(cum_km or 0) / float(interval_km or 1)
        pct_days = float(days_since or 0) / float(interval_days or 1)
        breakdown_risk = max(pct_km, pct_days)  # matches TruckMaintenanceState.breakdown_risk's own "whichever is worse"

    cur.execute(
        "select overall_pass from live.vehicle_inspections where driver_id = %s order by submitted_at desc limit 1",
        (driver_id,),
    )
    insp_row = cur.fetchone()
    inspection_ok = bool(insp_row[0]) if insp_row else None

    cur.execute(
        """insert into live.driver_state_snapshots
             (driver_id, truck_number, trip_id, snapshot_at, lat, lon, duty_status,
              hos_driving_hours_remaining, hos_duty_hours_remaining, hos_cycle1_hours_remaining,
              hos_cycle2_hours_remaining, truck_breakdown_risk, truck_pct_km_interval,
              truck_pct_days_interval, inspection_ok)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (driver_id, truck_number, str(trip_id), sim_clock, lat, lon, duty_status,
         hos_driving, hos_duty, hos_cycle1, hos_cycle2, breakdown_risk, pct_km, pct_days, inspection_ok),
    )


def _write_trip_projection(cur, trip_id: uuid.UUID, driver_id: int, truck_number: str, phase: str,
                            leg: LegState, origin_id: int, dest_id: int, sim_clock: datetime, data):
    """Keeps live.trips.eta / projected_hos_remaining_hours / projected_truck_pct_*_interval
    genuinely LIVE as a trip actually progresses (sim/sql/029, sim/sql/042). Bug found live
    (documents/logs -- driver 119 always top-ranked regardless of the pickup date selected):
    these columns were previously written ONCE, at seed time (seed_demo_fleet.py), and never
    refreshed here -- so score_quote.py's project_driver_state() (the in-progress-trip branch)
    was reading a landing_time frozen at seed time. Real wall-clock time always advances past that
    frozen instant, so the idle-gap qualifying-rest check always saw a large gap and unconditionally
    reset that driver's daily HOS clocks to full on EVERY quote -- independent of the actual
    requested pickup date. Fix: recompute the landing estimate from the trip's ACTUAL current
    leg/phase (real remaining time in this leg + calibrated median dwell/loaded-leg time for
    whatever phases remain), at the same periodic cadence _write_driver_snapshot already uses.
    """
    remaining_in_leg = max(0.0, (leg.duration_hours + leg.extra_delay_hours)
                            - (sim_clock - leg.started_sim).total_seconds() / 3600)
    median_pickup_dwell_h = data.dwell_minutes['pickup'][1] / 60
    median_delivery_dwell_h = data.dwell_minutes['delivery'][1] / 60

    if phase == 'to_pickup':
        _, loaded_hours = get_route(data, origin_id, dest_id)
        remaining_duty_hours = remaining_in_leg + median_pickup_dwell_h + loaded_hours + median_delivery_dwell_h
    elif phase == 'dwell_pickup':
        dwell_remaining = max(0.0, (leg.dwell_until_sim - sim_clock).total_seconds() / 3600) if leg.dwell_until_sim else 0.0
        _, loaded_hours = get_route(data, origin_id, dest_id)
        remaining_duty_hours = dwell_remaining + loaded_hours + median_delivery_dwell_h
    elif phase == 'to_delivery':
        remaining_duty_hours = remaining_in_leg + median_delivery_dwell_h
    else:  # dwell_delivery
        remaining_duty_hours = max(0.0, (leg.dwell_until_sim - sim_clock).total_seconds() / 3600) if leg.dwell_until_sim else 0.0

    eta = sim_clock + timedelta(hours=remaining_duty_hours)

    cur.execute("select hos_duty_hours_remaining from live.driver_status where driver_id = %s", (driver_id,))
    row = cur.fetchone()
    hos_duty = float(row[0]) if row and row[0] is not None else 0.0
    projected_hos_remaining = max(0.0, hos_duty - remaining_duty_hours)

    cur.execute(
        "select cumulative_km_since_service, service_interval_km, "
        "extract(epoch from (now() - last_service_at))/86400, service_interval_days "
        "from live.truck_maintenance_state where truck_number = %s",
        (truck_number,),
    )
    tm_row = cur.fetchone()
    pct_km = pct_days = None
    if tm_row:
        cum_km, interval_km, days_since, interval_days = tm_row
        pct_km = float(cum_km or 0) / float(interval_km or 1)
        pct_days = float(days_since or 0) / float(interval_days or 1)

    cur.execute(
        """update live.trips set eta = %s, projected_hos_remaining_hours = %s,
               projected_truck_pct_km_interval = %s, projected_truck_pct_days_interval = %s
           where trip_id = %s""",
        (eta, projected_hos_remaining, pct_km, pct_days, str(trip_id)),
    )


def run():
    data = load_sim_data()
    rng = random.Random()
    legs: dict[uuid.UUID, LegState] = {}
    last_snapshot: dict[uuid.UUID, datetime] = {}
    sim_clock = datetime.now(timezone.utc)
    print(f'Telemetry simulator running -- TICK_SECONDS={TICK_SECONDS}, TIME_SCALE={TIME_SCALE}x, Ctrl+C to stop')

    while True:
        tick_start = time.time()
        sim_clock = sim_clock + timedelta(seconds=TICK_SECONDS * TIME_SCALE)
        tick_hours = (TICK_SECONDS * TIME_SCALE) / 3600

        rows = _load_active_trips()
        with cursor() as cur:
            for trip_id, driver_id, status, origin_id, dest_id, truck_number in rows:
                trip_id = uuid.UUID(str(trip_id))
                leg = legs.get(trip_id)

                if leg is None:
                    if status == 'assigned':
                        cur.execute("select last_location_id from live.driver_status where driver_id = %s", (driver_id,))
                        (from_id,) = cur.fetchone()
                        from_id = from_id or origin_id  # driver already at pickup (e.g. reassigned) -- start there
                        leg = _start_leg(data, from_id, origin_id, 'to_pickup', sim_clock)
                    elif status == 'in_transit':
                        leg = _start_leg(data, origin_id, dest_id, 'to_delivery', sim_clock)
                        cur.execute("update live.trips set loaded_miles = %s where trip_id = %s", (leg.dist_miles, str(trip_id)))
                    else:
                        # A dwell phase with no in-memory state (process restarted mid-dwell) --
                        # give it a short grace dwell rather than stalling the trip forever.
                        leg = LegState(phase='dwell_pickup' if status == 'at_pickup' else 'dwell_delivery',
                                        coords=[], duration_hours=0.0, started_sim=sim_clock,
                                        dwell_until_sim=sim_clock + timedelta(minutes=1))
                    legs[trip_id] = leg

                if leg.phase in ('dwell_pickup', 'dwell_delivery'):
                    # Keep feeding the SAME (stationary) position every tick while dwelling --
                    # live.process_position_tick()'s 10-minute continuous-inside buffer (sim/sql/
                    # 008) only confirms an arrival/departure from a CONTINUOUS series of ticks;
                    # stopping ticks the moment our own state machine calls a driver "arrived"
                    # meant the buffer's 10-minute window never actually elapsed, and no
                    # geofence_events row (the brief's actual requirement) ever got written. This
                    # was caught directly: a live test run showed 'at_pickup' status but zero
                    # geofence_events rows until this fix.
                    dwell_location_id = origin_id if leg.phase == 'dwell_pickup' else dest_id
                    dwell_pos = data.locations.get(dwell_location_id)
                    if dwell_pos:
                        d_lat, d_lon = dwell_pos
                        cur.execute("select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
                                    (str(trip_id), driver_id, dwell_location_id, f'POINT({d_lon} {d_lat})', sim_clock))
                        if sim_clock - last_snapshot.get(trip_id, datetime.min.replace(tzinfo=timezone.utc)) >= SNAPSHOT_INTERVAL:
                            _write_driver_snapshot(cur, driver_id, truck_number, trip_id, 'on_duty_not_driving', d_lat, d_lon, sim_clock)
                            _write_trip_projection(cur, trip_id, driver_id, truck_number, leg.phase, leg,
                                                    origin_id, dest_id, sim_clock, data)
                            last_snapshot[trip_id] = sim_clock
                    if sim_clock >= leg.dwell_until_sim:
                        if leg.phase == 'dwell_pickup':
                            cur.execute("update live.trips set status = 'in_transit', last_event = 'DEPSHIP' where trip_id = %s", (str(trip_id),))
                            cur.execute("update live.driver_status set duty_status = 'driving' where driver_id = %s", (driver_id,))
                            new_leg = _start_leg(data, origin_id, dest_id, 'to_delivery', sim_clock)
                            new_leg.pickup_dwell_hours = leg.pickup_dwell_hours  # preserve across the leg-state swap
                            cur.execute("update live.trips set loaded_miles = %s where trip_id = %s", (new_leg.dist_miles, str(trip_id)))
                            legs[trip_id] = new_leg
                        else:
                            delivery_dwell_h = (sim_clock - leg.started_sim).total_seconds() / 3600
                            _complete_trip(cur, data, trip_id, driver_id, truck_number, origin_id, dest_id,
                                            leg.pickup_dwell_hours, delivery_dwell_h, sim_clock)
                            del legs[trip_id]
                            last_snapshot.pop(trip_id, None)
                    continue

                # Occasional 401-style slowdown -- brief's "Event Generator" requirement, made real
                # enough to actually affect ETA, not just cosmetic.
                if rng.random() < SLOWDOWN_PROBABILITY_PER_TICK:
                    extra = rng.uniform(*SLOWDOWN_EXTRA_HOURS)
                    leg.extra_delay_hours += extra
                    speed = rng.uniform(*SLOWDOWN_SPEED_MPH)
                    cur.execute("update live.trips set last_event = 'SLOWDOWN' where trip_id = %s", (str(trip_id),))
                    print(f'  [slowdown] trip {trip_id} driver {driver_id}: +{extra*60:.0f} min (401 congestion)')
                else:
                    speed = rng.uniform(*CRUISE_SPEED_MPH)

                elapsed_h = (sim_clock - leg.started_sim).total_seconds() / 3600
                effective_duration = leg.duration_hours + leg.extra_delay_hours
                fraction = min(1.0, elapsed_h / effective_duration) if effective_duration > 0 else 1.0
                dest_location_for_geofence = origin_id if leg.phase == 'to_pickup' else dest_id
                # A driver already sitting exactly at the pickup point (0-distance leg) has no
                # route geometry to interpolate along -- without this fallback the geofence
                # arrival would never fire for that real, common case (an idle driver whose
                # current position IS the requested pickup facility).
                pos = interpolate_position(leg.coords, fraction) if leg.coords else data.locations.get(dest_location_for_geofence)
                if pos:
                    lat, lon = pos
                    # Home-base-return retarget (sim/sql/042): decrement all 4 REAL HOS sub-clocks
                    # by the same real elapsed on-duty delta hos_remaining_hours has always used,
                    # not just the one blended figure -- this is what makes hos_cycle1/2_remaining
                    # genuinely distinct from the daily clocks (see this file's module docstring
                    # for the honest scope note: no qualifying-reset detection yet, same
                    # simplification the blended figure already carried). hos_remaining_hours
                    # itself stays the min of the two daily clocks post-update, preserving its
                    # existing "the one already-binding number" meaning for every other reader.
                    cur.execute("""
                        update live.driver_status set
                          position = ST_GeogFromText(%s), speed_mph = %s,
                          hos_driving_hours_remaining = greatest(0, hos_driving_hours_remaining - %s),
                          hos_duty_hours_remaining = greatest(0, hos_duty_hours_remaining - %s),
                          hos_cycle1_hours_remaining = greatest(0, hos_cycle1_hours_remaining - %s),
                          hos_cycle2_hours_remaining = greatest(0, hos_cycle2_hours_remaining - %s),
                          hos_remaining_hours = least(
                            greatest(0, hos_driving_hours_remaining - %s), greatest(0, hos_duty_hours_remaining - %s),
                            greatest(0, hos_cycle1_hours_remaining - %s), greatest(0, hos_cycle2_hours_remaining - %s)
                          ),
                          odometer_km = coalesce(odometer_km, 0) + %s,
                          fuel_pct = greatest(5, fuel_pct - %s),
                          last_location_id = null, updated_at = now()
                        where driver_id = %s
                    """, (f'POINT({lon} {lat})', speed, tick_hours, tick_hours, tick_hours, tick_hours,
                          tick_hours, tick_hours, tick_hours, tick_hours,
                          leg.total_km * (tick_hours / max(effective_duration, 0.01)),
                          leg.total_km * (tick_hours / max(effective_duration, 0.01)) * FUEL_PCT_PER_KM, driver_id))
                    cur.execute("insert into live.position_history (trip_id, driver_id, position, speed_mph, recorded_at) "
                                "values (%s, %s, ST_GeogFromText(%s), %s, now())",
                                (str(trip_id), driver_id, f'POINT({lon} {lat})', speed))
                    cur.execute("select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
                                (str(trip_id), driver_id, dest_location_for_geofence, f'POINT({lon} {lat})', sim_clock))
                    if sim_clock - last_snapshot.get(trip_id, datetime.min.replace(tzinfo=timezone.utc)) >= SNAPSHOT_INTERVAL:
                        _write_driver_snapshot(cur, driver_id, truck_number, trip_id, 'driving', lat, lon, sim_clock)
                        _write_trip_projection(cur, trip_id, driver_id, truck_number, leg.phase, leg,
                                                origin_id, dest_id, sim_clock, data)
                        last_snapshot[trip_id] = sim_clock

                if fraction >= 1.0:
                    if leg.phase == 'to_pickup':
                        dwell_h = sample_dwell_hours(data, 'pickup', rng)
                        leg.phase, leg.dwell_until_sim = 'dwell_pickup', sim_clock + timedelta(hours=dwell_h)
                        leg.pickup_dwell_hours = dwell_h
                        cur.execute("update live.trips set status = 'at_pickup', last_event = 'DOCKED' where trip_id = %s", (str(trip_id),))
                        cur.execute("update live.driver_status set duty_status = 'on_duty_not_driving', last_location_id = %s where driver_id = %s",
                                    (origin_id, driver_id))
                    else:
                        dwell_h = sample_dwell_hours(data, 'delivery', rng)
                        leg.phase, leg.dwell_until_sim = 'dwell_delivery', sim_clock + timedelta(hours=dwell_h)
                        cur.execute("update live.trips set status = 'at_delivery', last_event = 'ARRCONS' where trip_id = %s", (str(trip_id),))
                        cur.execute("update live.driver_status set duty_status = 'on_duty_not_driving', last_location_id = %s where driver_id = %s",
                                    (dest_id, driver_id))

        elapsed = time.time() - tick_start
        time.sleep(max(0.0, TICK_SECONDS - elapsed))


if __name__ == '__main__':
    run()
