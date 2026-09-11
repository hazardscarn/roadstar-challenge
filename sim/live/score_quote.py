"""Live quote scoring -- the theory-proof requested directly: can a real quote request be scored,
end to end, using the SAME candidate/scoring pipeline the simulator validated tonight (documents/
logs/16-19)? No dashboard/Vercel wiring yet -- this is the data pipeline itself, testable locally
against real or seeded live.* rows, matching research/roadstar_platform_plan.md Section 7's
"Request -> candidate filter -> feature computation -> score -> rank -> top-N" flow.

## Why this reuses sim/engine/policy.py and sim/engine/value_function.py directly, not a rewrite

Same principle as the module docstring in reward.py: "one function, sim and live." A live quote
candidate becomes a `Candidate` (sim/engine/policy.py) built from `live.driver_status`/
`live.trips`/`live.truck_maintenance_state` instead of from simulated fleet state, then scored by
the EXACT SAME `score_candidate()`/`rank_candidates()` that already ran millions of times tonight.
Any drift between "what the sim validated" and "what live actually does" would come from building
a second, parallel scoring implementation -- deliberately not done here.

## What's real vs. simplified in this pass

- Real: candidate filtering (idle vs. mid-route/multi-trip-queued via projected next-state), the
  reward formula (distance-tiered rate, deadhead cost, HOS-margin risk, expected-lateness pricing,
  maintenance risk, the home-base-return terms), the trained Q-model AND V(s) model, H3 region
  features, truck-condition-in-V(s), real distinct HOS sub-clocks (driving/duty/cycle1/cycle2 --
  see `_make_hos_state()`), a driver's OWN home-terminal distance (not nearest-any-hub).
- Simplified, flagged explicitly: the 16h elapsed-window clock has no independently-tracked live
  column (unlike driving/duty/cycle1/cycle2, which do -- sim/sql/042) -- conservatively set equal
  to the driver's real `hos_driving_hours_remaining`/`hos_duty_hours_remaining` min, same
  CONSERVATIVE-not-approximate treatment this module always used for the whole HOSState before
  this pass (setting it to the tightest real clock available means can_perform() never
  under-counts a real violation, it just can't show which of several clocks is binding for
  driver-facing messaging). Truck-condition projection through a not-yet-started queued trip
  (`project_driver_state()`) stays at the pre-trip figure -- no live after_trip() math without the
  sim's own TruckMaintenanceState machinery running live-side; a real gap, flagged, not silently
  guessed at.

## Home-base-return retarget -- dynamic, future-state-dependent scoring (documents/logs/23-24)

`build_candidates()` used to look at exactly ONE trip per driver (`current_trip_id`, joined to a
single `live.trips` row) -- correct for "mid-route right now," wrong for "already booked on
several FUTURE trips" (a real dispatcher books days ahead; verified directly against `live.trips`
-- no constraint stops multiple non-terminal rows per driver, and /api/assign used to silently
orphan earlier bookings from the single `current_trip_id` pointer on every new assignment, a real
bug, not just a missing feature -- fixed alongside this). `project_driver_state()` below walks a
driver's REAL trip queue (every non-terminal `live.trips` row, ordered by `eta`) forward to
compute where they'll ACTUALLY be, with what real HOS/truck state, by the time a NEW quote's
decision matters -- the same principle `sim/engine/run_sim.py`'s single-hop
`effective_driver_state()` already established for the simulator, generalized to N real committed
trips (the simulator itself never needs N-hop: its strictly causal event loop only ever has one
rolling commitment per driver -- see documents/feature_reference_and_inference_guide.md Section 4).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sim.config import (
    ASSUMED_CAPACITY_VALUE_RATE_PER_LB, CAPACITY_BY_LOAD_TYPE, CAPACITY_PALLETS_BY_LOAD_TYPE,
    HOS_MAX_DRIVING_HOURS, HOS_MAX_ON_DUTY_HOURS, HOS_MIN_DAILY_OFF_DUTY_HOURS,
)
from sim.db import cursor
from sim.engine.hos import HOSState
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.policy import Candidate, feasible_candidates, score_candidate
from sim.engine.run_sim import Order, SimData, _haversine_km, driver_home_hub_id, get_route, load_sim_data
from sim.engine.value_function import load_state_value_model, make_value_fn

GAMMA = 0.9  # matches sim/engine/run_sim.py's GAMMA -- one model family, live and sim alike
TOP_N = 5    # matches the platform plan's "top-5 as cards" dashboard design
# Trips that still occupy a driver's future schedule -- everything project_driver_state() needs to
# walk through. Terminal states ('completed'/'cancelled') are excluded; that's the whole set.
NON_TERMINAL_TRIP_STATUSES = ('assigned', 'scheduled', 'at_pickup', 'in_transit', 'at_delivery')
IN_PROGRESS_TRIP_STATUSES = ('at_pickup', 'in_transit', 'at_delivery')


@dataclass
class QuoteRequest:
    """Mirrors live.quote_requests -- what a real dispatcher/customer submits.

    `requested_at` (when the quote was SUBMITTED) and `requested_pickup_at` (when the customer
    WANTS pickup) are deliberately separate fields -- the first version of this module collapsed
    them into one, which broke the exact real lead-time mechanic the whole sim was built around
    tonight (documents/logs/16-19): the expected-lateness penalty compares a candidate's real
    projected ETA against `requested_pickup_at`, and a mid-route driver's real future ETA can only
    ever look "late" if it's compared against a promise anchored to right-now instead of the
    customer's real requested date. Caught directly: every mid-route candidate was scoring far
    worse than idle ones on a seeded test fleet purely from this construction bug, not from any
    real property of the trained model (documents/logs/21).

    `requested_pickup_at` defaults to `requested_at` (a same-instant request, matching the ~25% of
    real orders in calibration.order_lead_time_hours booked with under DISPATCH_DECISION_CUTOFF_HOURS
    notice) -- but a real quote UI should let the customer pick a future date, exactly like the
    live.quote_requests schema anticipates the dashboard doing.
    """
    quote_id: uuid.UUID
    origin_location_id: int
    dest_location_id: int
    weight_lbs: float
    pallets: float
    load_type: str
    requested_at: datetime
    requested_pickup_at: datetime | None = None  # None -> defaults to requested_at (same-instant request)
    service_type: str = 'FTL'


@dataclass
class LiveDriverRow:
    """One live.driver_status row, joined with ground_truth.drivers (home terminal) and
    live.truck_maintenance_state -- the live-data equivalent of what sim/engine/run_sim.py's
    Fleet holds in memory during a sim run. Does NOT carry any single trip's data anymore (that
    used to live here as current_trip_id-joined fields) -- a driver's real trip QUEUE is loaded
    separately (`load_driver_trip_queues()`) and walked by `project_driver_state()`, since a
    driver can have several already-committed future trips, not just one.
    """
    driver_id: int
    truck_number: str
    duty_status: str
    hos_remaining_hours: float
    # Home-base-return retarget (sim/sql/042): the 4 REAL, independently-tracked HOS sub-clocks --
    # no longer reconstructed as the one blended figure duplicated 5 times (see this module's
    # header comment for the one clock -- the 16h elapsed window -- still conservatively
    # collapsed, and why).
    hos_driving_hours_remaining: float
    hos_duty_hours_remaining: float
    hos_cycle1_hours_remaining: float
    hos_cycle2_hours_remaining: float
    # When THIS row was last really true -- for an idle driver, roughly when they went off-duty
    # (nothing since has touched their row). project_driver_state() uses this to tell "idle for a
    # real qualifying rest by the time the new pickup would happen" from "idle for 20 minutes" --
    # see that function's own docstring.
    updated_at: datetime
    last_location_id: int | None
    trailer_type: str
    trailer_capacity_lbs: int
    trailer_capacity_pallets: int
    home_terminal_zone: str | None
    # Home-time retarget (sim/sql/046, documents/logs/25): the real last moment this driver was AT
    # their own home hub -- written by telemetry_simulator.py's _complete_trip(). Like updated_at,
    # this is only ever a STARTING point -- project_driver_state() projects it FORWARD through the
    # driver's real trip queue, since a queued future trip landing at home changes it before the
    # quote being scored ever gets decided. None for a driver seeded before sim/sql/046 -- falls
    # back to updated_at (their last known state) rather than crashing, same tolerant pattern the
    # HOS sub-clocks above use for a driver seeded before sim/sql/042.
    last_home_arrival_at: datetime | None = None
    # From live.truck_maintenance_state:
    truck_pct_km_interval: float = 0.0
    truck_pct_days_interval: float = 0.0
    truck_maintenance_until: datetime | None = None


@dataclass
class QueuedTrip:
    """One row from a driver's real, ordered trip queue -- everything project_driver_state()
    needs to walk through it, whether it's already IN PROGRESS (telemetry-tracked real projected
    state) or merely ASSIGNED/SCHEDULED (assignment-time planned-* estimate, sim/sql/042).
    """
    trip_id: uuid.UUID
    status: str
    dest_location_id: int
    eta: datetime
    projected_hos_remaining_hours: float | None
    projected_truck_pct_km_interval: float | None
    projected_truck_pct_days_interval: float | None
    planned_driving_hours: float | None
    planned_duty_hours: float | None
    planned_completion_at: datetime | None


@dataclass
class ProjectedDriverState:
    """Where a driver will ACTUALLY be, with what real HOS/truck state, once their whole current
    trip queue clears -- what `build_candidates()` scores every driver from, whether that's `now`
    (empty queue, genuinely idle) or some real future moment (one or several committed trips deep).
    """
    location_id: int
    effective_start: datetime
    hos_driving_hours_remaining: float
    hos_duty_hours_remaining: float
    hos_cycle1_hours_remaining: float
    hos_cycle2_hours_remaining: float
    truck_pct_km_interval: float
    truck_pct_days_interval: float
    # Home-time retarget (documents/logs/25): the real last-at-home moment, CHAIN-WALKED forward
    # through this driver's real trip queue exactly like landing_time/hos_* above -- if a queued
    # trip lands at home before the trip being scored, this reflects that projected arrival, not
    # whatever the driver's live.driver_status row happened to say at query time.
    last_home_at: datetime


def load_live_fleet_snapshot() -> tuple[list[LiveDriverRow], int]:
    """Pulls the current live fleet state -- the live-data equivalent of sim/engine/run_sim.py's
    in-memory Fleet. Excludes trucks currently in the shop (documents/logs/18's proactive-
    maintenance mechanism) the same way pick_pool_truck() does in the sim.

    IMPORTANT -- this is a STARTING POINT, never the final feature values scoring uses. Every row
    here is "what's true about this driver right now" (or, for the DVIR gate, "in the last 24h").
    A quote's real decision_time can be up to a day in the future (documents/logs/21's real lead-
    time mechanic), so build_candidates() ALWAYS runs this through project_driver_state() before
    a single number reaches Candidate/compute_reward() -- that's what turns "today's HOS reading"
    into "this driver's HOS as of when the new pickup would actually happen" (already-committed
    trips walked through, AND a real idle-gap qualifying-rest assumption if they'd be free with
    real time to spare -- see project_driver_state()'s own docstring). Nothing downstream of
    build_candidates() ever reads a LiveDriverRow field directly.

    Manager-side inspection gate (the brief's real compliance requirement, documents/logs/22's
    UI-build handoff): a driver without a PASSING DVIR on file in the last 24h never becomes a
    candidate at all -- enforced here, in the query itself, not as a UI warning bolted on after
    scoring. Returns (fleet, n_excluded_for_inspection) so the caller can surface the exclusion
    count to the dispatcher for transparency (score_quote()'s summary).

    Checks the driver's MOST RECENT inspection specifically (via DISTINCT ON), not "does ANY
    passing inspection exist in the last 24h" -- the handoff brief's own literal SQL had exactly
    that bug, caught live: a driver whose 7am inspection passed but whose 7pm re-inspection
    failed (a real brake defect found later the same day) was still coming back as a valid
    candidate, because SOME passing row in the window still existed. Real DVIR compliance means
    "is the LATEST inspection a pass," not "was any inspection ever a pass today."

    Home-base-return retarget (sim/sql/042, documents/feature_reference_and_inference_guide.md
    Section 7 item 1): joins ground_truth.drivers for the driver's real home terminal_zone --
    verified this needed no new schema, just a join on the same driver_id key used everywhere
    else in this project.
    """
    with cursor() as cur:
        cur.execute("select count(*) from live.driver_status")
        (n_total,) = cur.fetchone()

        cur.execute("""
            select
              ds.driver_id, ds.truck_number, ds.duty_status, ds.hos_remaining_hours,
              ds.hos_driving_hours_remaining, ds.hos_duty_hours_remaining,
              ds.hos_cycle1_hours_remaining, ds.hos_cycle2_hours_remaining, ds.updated_at,
              ds.last_location_id, ds.trailer_type, ds.trailer_capacity_lbs, ds.trailer_capacity_pallets,
              gd.terminal_zone, ds.last_home_arrival_at,
              tm.cumulative_km_since_service, tm.service_interval_km,
              tm.last_service_at, tm.service_interval_days, tm.maintenance_until
            from live.driver_status ds
            left join ground_truth.drivers gd on gd.driver_id = ds.driver_id
            left join live.truck_maintenance_state tm on tm.truck_number = ds.truck_number
            where exists (
              select 1 from (
                select vi.overall_pass, vi.submitted_at from live.vehicle_inspections vi
                where vi.driver_id = ds.driver_id
                order by vi.submitted_at desc limit 1
              ) latest
              where latest.submitted_at > now() - interval '24 hours'
                and latest.overall_pass
            )
        """)
        rows = cur.fetchall()
        n_excluded_for_inspection = n_total - len(rows)

    fleet = []
    for r in rows:
        (driver_id, truck_number, duty_status, hos_remaining, hos_driving, hos_duty, hos_cycle1, hos_cycle2, updated_at,
         last_location_id, trailer_type, cap_lbs, cap_pallets, terminal_zone, last_home_arrival_at,
         cum_km, interval_km, last_service_at, interval_days, maint_until) = r

        pct_km = float(cum_km) / float(interval_km) if cum_km is not None and interval_km else 0.0
        if last_service_at is not None and interval_days:
            days_since = (datetime.now(last_service_at.tzinfo) - last_service_at).total_seconds() / 86400
            pct_days = days_since / interval_days
        else:
            pct_days = 0.0

        hos_remaining_f = float(hos_remaining) if hos_remaining is not None else 0.0
        fleet.append(LiveDriverRow(
            driver_id=driver_id, truck_number=truck_number, duty_status=duty_status,
            hos_remaining_hours=hos_remaining_f,
            # Fall back to the blended figure for a driver seeded before sim/sql/042 -- never a
            # silent None reaching HOSState's arithmetic.
            hos_driving_hours_remaining=float(hos_driving) if hos_driving is not None else hos_remaining_f,
            hos_duty_hours_remaining=float(hos_duty) if hos_duty is not None else hos_remaining_f,
            hos_cycle1_hours_remaining=float(hos_cycle1) if hos_cycle1 is not None else hos_remaining_f,
            hos_cycle2_hours_remaining=float(hos_cycle2) if hos_cycle2 is not None else hos_remaining_f,
            updated_at=updated_at,
            last_location_id=last_location_id, trailer_type=trailer_type or 'Dry Van',
            trailer_capacity_lbs=cap_lbs or CAPACITY_BY_LOAD_TYPE.get('Dry Van', 44500),
            trailer_capacity_pallets=cap_pallets or CAPACITY_PALLETS_BY_LOAD_TYPE.get('Dry Van', 26),
            home_terminal_zone=terminal_zone,
            truck_pct_km_interval=pct_km, truck_pct_days_interval=pct_days,
            truck_maintenance_until=maint_until,
            last_home_arrival_at=last_home_arrival_at,
        ))
    return fleet, n_excluded_for_inspection


def load_driver_trip_queues(driver_ids: list[int]) -> dict[int, list[QueuedTrip]]:
    """Every driver's REAL, ordered future-trip queue -- the direct replacement for the old
    single current_trip_id join. Queried straight against live.trips (the real source of truth),
    not the single-pointer driver_status column that /api/assign used to (and no longer does)
    treat as authoritative -- see this module's header comment and documents/logs/23-24.
    """
    if not driver_ids:
        return {}
    with cursor() as cur:
        cur.execute(
            f"""select driver_id, trip_id, status, dest_location_id, eta,
                       projected_hos_remaining_hours, projected_truck_pct_km_interval,
                       projected_truck_pct_days_interval, planned_driving_hours, planned_duty_hours,
                       planned_completion_at
                from live.trips
                where driver_id = any(%s) and status in %s
                order by driver_id, eta asc""",
            (driver_ids, NON_TERMINAL_TRIP_STATUSES),
        )
        rows = cur.fetchall()

    queues: dict[int, list[QueuedTrip]] = {}
    for (driver_id, trip_id, status, dest_location_id, eta, proj_hos, proj_km, proj_days,
         planned_driving, planned_duty, planned_completion_at) in rows:
        queues.setdefault(driver_id, []).append(QueuedTrip(
            trip_id=trip_id, status=status, dest_location_id=dest_location_id, eta=eta,
            projected_hos_remaining_hours=float(proj_hos) if proj_hos is not None else None,
            projected_truck_pct_km_interval=float(proj_km) if proj_km is not None else None,
            projected_truck_pct_days_interval=float(proj_days) if proj_days is not None else None,
            planned_driving_hours=float(planned_driving) if planned_driving is not None else None,
            planned_duty_hours=float(planned_duty) if planned_duty is not None else None,
            planned_completion_at=planned_completion_at,
        ))
    return queues


def _trip_end_time(trip: QueuedTrip) -> datetime:
    """When this queued trip will actually be DONE -- real telemetry completion estimate for an
    in-progress trip (kept live by telemetry_simulator.py's _write_trip_projection, see that
    function's docstring for the bug this closes), or the assignment-time planned estimate for
    one not yet started.
    """
    if trip.status in IN_PROGRESS_TRIP_STATUSES:
        return trip.eta
    return trip.planned_completion_at or trip.eta


def _trip_start_time(trip: QueuedTrip) -> datetime:
    """When this queued trip will actually BEGIN. For 'assigned'/'scheduled' (not yet started),
    `eta` IS the real departure/readiness estimate written by /api/assign. An IN-PROGRESS trip has
    no FUTURE start at all -- it already began (in real wall-clock terms, always before any
    pickup_at this function is asked about) -- so it counts as unconditionally "already started"
    for the availability check below, using `datetime.min` as a start that's always <= pickup_at
    rather than trip.eta, which for an in-progress trip means its real projected COMPLETION, not
    its start (the overloaded-eta semantics telemetry_simulator.py's own header comment flags).
    """
    if trip.status in IN_PROGRESS_TRIP_STATUSES:
        return datetime.min.replace(tzinfo=timezone.utc)
    return trip.eta


def project_driver_state(
    drv: LiveDriverRow, trip_queue: list[QueuedTrip], now: datetime, pickup_at: datetime, home_hub_id: int,
) -> tuple[ProjectedDriverState, QueuedTrip | None] | None:
    """Walks a driver's REAL trip queue forward from their real current state, applying each
    ALREADY-SETTLED trip's known-or-projected effect on position/HOS/truck condition in sequence
    -- generalizes the single-hop mid-route pattern `effective_driver_state()` (sim/engine/
    run_sim.py) already uses in the sim to N real committed trips (documents/
    feature_reference_and_inference_guide.md Section 4's worked example).

    ## The real availability check (this function's hard filter)

    A driver isn't a candidate for a new pickup just because they'll EVENTUALLY be free -- a real
    dispatcher can't offer a truck that's still out on a different, already-promised job at the
    moment this new one needs to be picked up. `pickup_at` (`order.requested_pickup_at`) is the
    real moment that matters for this check -- NOT `now`/`decision_time`, which can be up to
    DISPATCH_DECISION_CUTOFF_HOURS earlier than the pickup itself.

    The queue is split at `pickup_at`: trips whose real end time (`_trip_end_time()`) falls AT OR
    BEFORE `pickup_at` are "settled" -- they will genuinely be done, and are walked through below
    exactly like before, to compute this driver's real state AS OF `pickup_at`. Anything left over
    ("remaining", still ordered by eta/start) is NOT walked through -- projecting state past
    `pickup_at` isn't what a pickup happening AT `pickup_at` needs, and (the actual bug this
    closes) doing so unconditionally used to make an already-multi-booked driver look reachable
    for a pickup that would actually collide with a job they're already committed to.

    If the FIRST remaining trip has already STARTED by `pickup_at` (`_trip_start_time() <=
    pickup_at` -- always true for an in-progress trip, since it started in the past) then this
    driver is genuinely busy AT `pickup_at` and returns None: not a candidate at all, matching the
    real-world constraint (documents/logs -- the sketch this implements: "does driver already have
    a trip planned to start at Pt... if so NOT qualified").

    Otherwise this driver IS free at `pickup_at`, and the first remaining trip (if any) is
    returned alongside the projected state as `next_committed_trip` -- build_candidates() uses it
    to additionally reject this driver if accepting the NEW order would run past that already-
    committed trip's own start time (a check this function can't make itself, since it doesn't
    know the new order's own deadhead/duration).

    A driver with nothing queued (or whose queue is entirely settled before `pickup_at`, with a
    real idle gap before `now`) isn't frozen at today's live reading either -- if there's real
    idle time before `now`, their DAILY HOS clocks (driving/duty, which reset via any 10h+
    off-duty block) are assumed recovered by then, the same real mechanism run_sim.py's own
    `apply_idle_reset()` applies for an idle simulated driver. The CYCLE clocks (cycle1/cycle2)
    are NOT touched by this -- they only reset via a genuine qualifying cycle reset, which needs
    real interval history this pass doesn't maintain live (a flagged, deliberate simplification:
    this can UNDERSTATE a long-idle driver's real cycle margin, never overstate it -- conservative,
    not silently optimistic).

    Returns None when there's truly nothing to route from (an idle driver, empty queue, no known
    live position) OR when the driver is genuinely busy at `pickup_at` (see above).
    """
    if drv.last_location_id is None and not trip_queue:
        return None  # unroutable -- no known position and nothing queued to route from

    settled = [t for t in trip_queue if _trip_end_time(t) <= pickup_at]
    remaining = [t for t in trip_queue if _trip_end_time(t) > pickup_at]

    next_trip = remaining[0] if remaining else None
    if next_trip is not None and _trip_start_time(next_trip) <= pickup_at:
        return None  # genuinely busy at pickup_at -- not a candidate for this order at all

    location_id = drv.last_location_id
    hos_driving = drv.hos_driving_hours_remaining
    hos_duty = drv.hos_duty_hours_remaining
    hos_cycle1 = drv.hos_cycle1_hours_remaining
    hos_cycle2 = drv.hos_cycle2_hours_remaining
    truck_pct_km = drv.truck_pct_km_interval
    truck_pct_days = drv.truck_pct_days_interval
    # When this driver actually becomes free to start a NEW trip -- starts at the last real
    # moment their state is known (roughly when they went off-duty, for an idle driver), walked
    # forward through every SETTLED already-committed trip below (anything still open past
    # pickup_at is deliberately excluded from this walk -- see docstring).
    landing_time = drv.updated_at
    # Home-time retarget (documents/logs/25): CHAIN-WALKED forward through the queue exactly like
    # landing_time/hos_* above -- a queued trip that lands at home BEFORE the trip being scored
    # must update this, not the driver's live.driver_status row (which only reflects reality up
    # to NOW, not a future already-committed booking) -- the user's own explicit correction: "this
    # last at home feature have to be dynamically added... in future trip assignments this also
    # updates so next calc will know this."
    last_home_at = drv.last_home_arrival_at or drv.updated_at

    for trip in settled:
        location_id = trip.dest_location_id
        if trip.status in IN_PROGRESS_TRIP_STATUSES:
            # Already moving -- telemetry is the real source of truth for the landing state
            # (sim/sql/029's projected_* columns), same fields the old single-hop branch used.
            landing_time = trip.eta
            if trip.projected_hos_remaining_hours is not None:
                # Telemetry only ever projects the ONE blended figure forward for an active trip
                # (it has no real per-clock ledger -- see this module's header comment) -- apply
                # the SAME delta to all 4 real clocks rather than silently freezing 3 of them,
                # so a long active trip still visibly consumes cycle margin.
                delta = max(0.0, hos_duty - float(trip.projected_hos_remaining_hours))
                hos_driving = max(0.0, hos_driving - delta)
                hos_duty = float(trip.projected_hos_remaining_hours)
                hos_cycle1 = max(0.0, hos_cycle1 - delta)
                hos_cycle2 = max(0.0, hos_cycle2 - delta)
            if trip.projected_truck_pct_km_interval is not None:
                truck_pct_km = trip.projected_truck_pct_km_interval
                truck_pct_days = trip.projected_truck_pct_days_interval
        else:
            # 'assigned' (not yet ticked by telemetry) or 'scheduled' (queued, not due to start) --
            # no real projected state exists yet; walk through the assignment-time ESTIMATE
            # (planned_driving_hours/planned_duty_hours/planned_completion_at, sim/sql/042) the
            # same light way sim/engine/value_function.py's make_value_fn() already estimates a
            # not-yet-run candidate's landing HOS (current minus this trip's planned duty hours).
            landing_time = trip.planned_completion_at or trip.eta
            duty_est = trip.planned_duty_hours or 0.0
            hos_driving = max(0.0, hos_driving - duty_est)
            hos_duty = max(0.0, hos_duty - duty_est)
            hos_cycle1 = max(0.0, hos_cycle1 - duty_est)
            hos_cycle2 = max(0.0, hos_cycle2 - duty_est)
            # truck_pct_km/days: left at the pre-trip figure -- no live after_trip() projection
            # for a not-yet-started trip without the sim's TruckMaintenanceState machinery running
            # live-side. Flagged, not silently guessed (see this module's header comment).

        if trip.dest_location_id == home_hub_id:
            last_home_at = landing_time  # this settled trip's own landing time IS a real home arrival

    # Real idle-gap qualifying-rest assumption (see docstring) -- this driver can't actually
    # depart for the NEW pickup before `now` (the decision hasn't happened yet) even if they were
    # free earlier, so the earliest real departure is max(landing_time, now); if that gap is a
    # real 10h+ block, the daily clocks are back to full by then.
    gap_hours = max(0.0, (now - landing_time).total_seconds() / 3600) if now > landing_time else 0.0
    if gap_hours >= HOS_MIN_DAILY_OFF_DUTY_HOURS:
        hos_driving = HOS_MAX_DRIVING_HOURS
        hos_duty = HOS_MAX_ON_DUTY_HOURS
    effective_start = max(landing_time, now)

    return ProjectedDriverState(
        location_id=location_id, effective_start=effective_start,
        hos_driving_hours_remaining=hos_driving, hos_duty_hours_remaining=hos_duty,
        hos_cycle1_hours_remaining=hos_cycle1, hos_cycle2_hours_remaining=hos_cycle2,
        truck_pct_km_interval=truck_pct_km, truck_pct_days_interval=truck_pct_days,
        last_home_at=last_home_at,
    ), next_trip


def _make_hos_state(driving: float, duty: float, cycle1: float, cycle2: float) -> HOSState:
    """Home-base-return retarget (sim/sql/042): 4 of the 5 real HOS clocks now come from real,
    independently-tracked live data (driving/duty/cycle1/cycle2) -- only the 16h elapsed-window
    clock still has no live column of its own. Conservatively set to min(driving, duty), the
    tightest of the two real clocks available -- same CONSERVATIVE-not-approximate principle this
    function always used (can_perform() can only ever be as-or-more strict than reality, never
    silently permit a real violation), just no longer covering all 5 dimensions the way it used to
    before real driving/duty/cycle1/cycle2 columns existed.
    """
    return HOSState(
        remaining_driving_hours=driving, remaining_duty_hours=duty,
        remaining_elapsed_window_hours=min(driving, duty),
        remaining_cycle1_hours=cycle1, remaining_cycle2_hours=cycle2,
    )


def build_candidates(data: SimData, fleet: list[LiveDriverRow], order: Order, now: datetime) -> list[Candidate]:
    """The live-data equivalent of run_sim.py's DISPATCH_DECISION candidate-building loop --
    SAME effective-state logic (idle drivers scored from their real current position; drivers with
    one or more already-committed trips scored from their PROJECTED state after the whole queue
    clears, documents/logs/16-17/23-24), just reading from live.* rows instead of an in-memory
    simulated Fleet.

    Home-base-return features (distance/hours to the driver's OWN home terminal, current position
    AND this candidate's landing spot if chosen) computed identically to sim/engine/run_sim.py's
    own DISPATCH_DECISION loop -- same driver_home_hub_id()/get_route() calls, cached per home hub
    (up to 4 real anchor points -- calibration.driver_home_hub, documents/logs/25) so this costs
    at most a handful of extra real route lookups per order arrival regardless of fleet size, not
    one per candidate.
    """
    trip_queues = load_driver_trip_queues([drv.driver_id for drv in fleet])
    home_hub_landing_cache: dict[int, tuple[float, float]] = {}

    candidates = []
    for drv in fleet:
        if drv.truck_maintenance_until is not None and drv.truck_maintenance_until > now:
            continue  # truck in the shop -- matches pick_pool_truck()'s check in the sim

        # home_hub_id computed BEFORE project_driver_state() -- the chain-walk needs it to detect
        # a queued trip that lands at home before the trip being scored (documents/logs/25).
        home_hub_id = driver_home_hub_id(data, drv.driver_id)

        # project_driver_state() is the real availability check (documents/logs -- the dynamic-
        # feature-space sketch this implements): None means this driver is genuinely unroutable
        # OR already busy AT the requested pickup moment (still out on a trip that hasn't ended by
        # then, or one that will already have STARTED by then) -- not a candidate at all, matching
        # a real dispatcher's own constraint. order.requested_pickup_at, not decision_time/now, is
        # the moment that must be free -- decision_time can be up to a day earlier.
        result = project_driver_state(drv, trip_queues.get(drv.driver_id, []), now, order.requested_pickup_at, home_hub_id)
        if result is None:
            continue
        proj, next_trip = result

        dh_miles, dh_hours = get_route(data, proj.location_id, order.origin_location_id)

        if next_trip is not None:
            # Free AT pickup_at, but this driver already has a LATER trip committed -- reject if
            # taking this new order would run past that trip's own real departure time (would
            # make the driver late for, or literally still be on this order during, a job already
            # promised to someone else). new_order_duration is a real estimate of how long this
            # driver would be occupied by the new order, end to end: drive to pickup, load dwell,
            # the loaded haul itself, unload dwell -- the same real median-dwell figures used
            # everywhere else in this project (build_order_from_quote(), run_sim.py's own
            # promised_delivery_at construction), not a second, inconsistent guess.
            median_pickup_dwell_h = data.dwell_minutes['pickup'][1] / 60
            median_delivery_dwell_h = data.dwell_minutes['delivery'][1] / 60
            new_order_duration_hours = dh_hours + median_pickup_dwell_h + order.loaded_hours + median_delivery_dwell_h
            if order.requested_pickup_at + timedelta(hours=new_order_duration_hours) > next_trip.eta:
                continue  # would collide with / delay this driver's next already-committed trip

        hos_state = _make_hos_state(
            proj.hos_driving_hours_remaining, proj.hos_duty_hours_remaining,
            proj.hos_cycle1_hours_remaining, proj.hos_cycle2_hours_remaining,
        )
        truck_state = TruckMaintenanceState(
            truck_number=drv.truck_number,
            cumulative_km_since_service=proj.truck_pct_km_interval * 25000,  # reconstructed back from pct -- see maintenance.py's real interval constant
            days_since_service=proj.truck_pct_days_interval * 180,
        )
        planned_driving = dh_hours + order.loaded_hours
        planned_duty = planned_driving + 1.5  # matches run_sim.py's own feasibility-only dwell estimate

        home_miles_now, home_hours_now = get_route(data, proj.location_id, home_hub_id)
        if home_hub_id not in home_hub_landing_cache:
            home_hub_landing_cache[home_hub_id] = get_route(data, order.dest_location_id, home_hub_id)
        home_miles_landing, home_hours_landing = home_hub_landing_cache[home_hub_id]
        # Home-time retarget (documents/logs/25): real hours since this driver was last AT home,
        # as of this candidate's own effective_start -- proj.last_home_at is already the CHAIN-
        # WALKED projection (project_driver_state()), not a static real-time-only value.
        hours_since_home = max(0.0, (proj.effective_start - proj.last_home_at).total_seconds() / 3600)

        candidates.append(Candidate(
            driver_id=drv.driver_id, truck_number=drv.truck_number, hos_state=hos_state,
            truck_state=truck_state, pre_pickup_deadhead_miles=dh_miles,
            planned_driving_hours=planned_driving, planned_duty_hours=planned_duty,
            pre_pickup_deadhead_hours=dh_hours, location_id=proj.location_id, effective_start=proj.effective_start,
            distance_to_home_miles=home_miles_now, distance_to_home_miles_landing=home_miles_landing,
            hours_to_home_current=home_hours_now, hours_to_home_landing=home_hours_landing,
            hours_since_home=hours_since_home,
        ))
    return candidates


def build_order_from_quote(data: SimData, quote: QuoteRequest) -> Order:
    """QuoteRequest -> the SAME Order shape score_candidate()/rank_candidates() expect --
    reuses real feature derivations (dest_distance_to_hub_km, dest_local_order_density) the
    exact same way generate_order() does in the sim, so live scoring sees the same feature
    distributions the trained model was actually trained on.

    requested_pickup_at/decision_time: a REAL date, not collapsed to requested_at (documents/
    logs/21 -- see QuoteRequest's docstring for the bug this fixes). decision_time follows the
    SAME 24h-before-pickup cutoff every other order in this project uses
    (DISPATCH_DECISION_CUTOFF_HOURS) -- for a quote with real lead time, `now` (the actual moment
    score_quote() runs) is naturally later than that cutoff, so decision_time collapses to `now`
    in practice, which is correct: the decision genuinely IS happening now, live, regardless of
    how far the pickup date sits in the future. promised_delivery_at is anchored to the REAL
    requested pickup time, not the booking instant -- a real delivery promise is "we'll pick up
    around X, deliver by X+transit+buffer," not counted from when the quote was submitted.
    """
    requested_pickup_at = quote.requested_pickup_at or quote.requested_at
    loaded_miles, loaded_hours = get_route(data, quote.origin_location_id, quote.dest_location_id)
    dest_distance_to_hub_km = min(
        _haversine_km(data.locations[quote.dest_location_id], data.locations[hub_id])
        for hub_id in data.hub_ids.values()
    )
    from sim.config import ASSUMED_PROMISE_BUFFER_HOURS, DISPATCH_DECISION_CUTOFF_HOURS
    from datetime import timedelta
    median_pickup_dwell_h = data.dwell_minutes['pickup'][1] / 60
    median_delivery_dwell_h = data.dwell_minutes['delivery'][1] / 60
    promised_delivery_at = requested_pickup_at + timedelta(
        hours=loaded_hours + median_pickup_dwell_h + median_delivery_dwell_h + ASSUMED_PROMISE_BUFFER_HOURS
    )
    decision_time = max(quote.requested_at, requested_pickup_at - timedelta(hours=DISPATCH_DECISION_CUTOFF_HOURS))
    return Order(
        order_id=quote.quote_id, origin_location_id=quote.origin_location_id,
        dest_location_id=quote.dest_location_id, created_at=quote.requested_at,
        weight_lbs=quote.weight_lbs, pallets=quote.pallets, load_type=quote.load_type,
        loaded_miles=loaded_miles, loaded_hours=loaded_hours, service_type=quote.service_type,
        dest_distance_to_hub_km=dest_distance_to_hub_km,
        dest_local_order_density=data.origin_density.get(quote.dest_location_id, 0.0),
        requested_pickup_at=requested_pickup_at, decision_time=decision_time,
        promised_delivery_at=promised_delivery_at,
    )


def score_quote(quote: QuoteRequest, data: SimData | None = None, value_fn=None, top_n: int = TOP_N) -> dict:
    """THE theory-proof entry point: quote in, ranked top-N candidates out, PLUS the full reward
    distribution across every feasible candidate considered (documents/logs/21 -- the user asked
    specifically to see the distribution of reward separated, not just a winner, so the best
    options and how they compare are both visible, matching how sim.candidate_scores already logs
    a full group per decision in the simulator, not just the one chosen candidate).

    Returns {'top_n': [...], 'all_scored': [...], 'summary': {...}} -- 'top_n' is what would
    actually go into live.quote_recommendations; 'all_scored' is every feasible candidate's full
    reward breakdown (mid-route vs. idle, deadhead, revenue, each penalty term, final score) for
    inspecting WHY the ranking came out the way it did; 'summary' gives the reward-distribution
    stats (best/worst/median/spread) plus counts of idle vs. mid-route candidates actually
    competing -- confirms mid-route/return-deadhead drivers are genuinely in the running, not
    just idle ones winning by default (see this module's docstring on the decision_time fix that
    made this possible).
    """
    if data is None:
        data = load_sim_data()
    if value_fn is None:
        # v4 hours-since-home model (documents/logs/27) -- same one dashboard/server/main.py loads
        # at startup; kept in sync so a standalone/test call of score_quote() without an explicit
        # value_fn scores identically to the live app.
        booster, cols = load_state_value_model('sim/training/state_value_function_v4_hours_since_home.pkl')
        value_fn = make_value_fn(booster, cols, data)

    fleet, n_excluded_for_inspection = load_live_fleet_snapshot()
    order = build_order_from_quote(data, quote)
    # order.decision_time, NOT quote.requested_at directly (documents/logs/21) -- the real
    # decision moment (24h before a real future pickup date, or `now` if the quote has little/no
    # lead time) is what both candidate-building and scoring need to agree with the Order object
    # itself on, or the ETA math they share (expected-lateness pricing) goes internally inconsistent.
    now = order.decision_time
    candidates = build_candidates(data, fleet, order, now)

    if not candidates:
        # Still price the quote even with zero feasible candidates -- a dispatcher needs to see
        # what this lane WOULD have cost to explain to the customer why nothing could be offered.
        from sim.engine.reward import load_fill_ratio as _load_fill_ratio0
        from sim.config import quote_price as _quote_price0
        cap_lbs0 = CAPACITY_BY_LOAD_TYPE.get(order.load_type, 44500)
        cap_pallets0 = CAPACITY_PALLETS_BY_LOAD_TYPE.get(order.load_type, 26)
        fill0 = _load_fill_ratio0(order.weight_lbs, order.pallets, cap_lbs0, cap_pallets0)
        pricing0 = _quote_price0(order.loaded_miles, order.load_type, order.service_type, fill0)
        return {'top_n': [], 'all_scored': [], 'summary': {
            'n_candidates': 0, 'n_feasible': 0, 'n_excluded_for_inspection': n_excluded_for_inspection,
            'loaded_miles': round(order.loaded_miles, 1), 'load_fill_ratio': round(fill0, 2),
            'rate_per_mile': pricing0['rate_per_mile'], 'linehaul_amount': pricing0['linehaul_amount'],
            'fuel_surcharge_amount': pricing0['fuel_surcharge_amount'],
            'estimated_total_charge': pricing0['estimated_total_charge'],
        }}

    capacity_lbs = CAPACITY_BY_LOAD_TYPE.get(order.load_type, 44500)
    capacity_pallets = CAPACITY_PALLETS_BY_LOAD_TYPE.get(order.load_type, 26)

    # feasible_candidates() -- the SAME hard HOS filter rank_candidates()/choose_assignment() use
    # internally -- applied here explicitly so we keep the RewardBreakdown per candidate (rank_
    # candidates() only returns the scalar score, discarding the breakdown the distribution view
    # needs).
    feasible = feasible_candidates(candidates)
    scored = []
    for c in feasible:
        score, reward = score_candidate(
            c, order, capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
            capacity_value_rate_per_lb=ASSUMED_CAPACITY_VALUE_RATE_PER_LB, gamma=GAMMA,
            value_fn=value_fn, now=now,
        )
        scored.append((c, score, reward))
    scored.sort(key=lambda t: -t[1])

    def _row(candidate: Candidate, score: float, reward, rank: int | None) -> dict:
        is_mid_route = candidate.effective_start is not None and candidate.effective_start > now
        return {
            'rank': rank, 'driver_id': candidate.driver_id, 'truck_number': candidate.truck_number,
            'score': round(score, 2), 'is_mid_route_candidate': is_mid_route,
            'deadhead_miles': round(candidate.pre_pickup_deadhead_miles, 1),
            'eta_pickup': candidate.effective_start,
            'order_revenue': round(reward.order_revenue, 2),
            'deadhead_cost': round(reward.deadhead_cost, 2),
            'opportunity_cost_penalty': round(reward.opportunity_cost_penalty, 2),
            'hos_stranding_risk_penalty': round(reward.hos_stranding_risk_penalty, 2),
            'maintenance_risk_penalty': round(reward.maintenance_risk_penalty, 2),
            'expected_lateness_penalty': round(reward.expected_lateness_penalty, 2),
            'cycle_end_stranding_penalty': round(reward.cycle_end_stranding_penalty, 2),
            'home_progress_bonus': round(reward.home_progress_bonus, 2),
            'immediate_reward': round(reward.total, 2),
            # Raw feature state at decision time -- for live.quote_candidate_snapshots, the "what
            # feature state did this driver/truck have for the best candidate score" audit trail.
            'location_id': candidate.location_id,
            'hos_remaining_hours': round(candidate.hos_state.remaining_hours, 2),
            'distance_to_home_miles': round(candidate.distance_to_home_miles, 1) if candidate.distance_to_home_miles is not None else None,
            'truck_breakdown_risk': round(candidate.truck_state.breakdown_risk, 4),
            'truck_pct_km_interval': round(candidate.truck_state.pct_of_km_interval, 4),
            'truck_pct_days_interval': round(candidate.truck_state.pct_of_days_interval, 4),
            'planned_driving_hours': round(candidate.planned_driving_hours, 2),
            'planned_duty_hours': round(candidate.planned_duty_hours, 2),
        }

    top_n_rows = [_row(c, s, r, rank) for rank, (c, s, r) in enumerate(scored[:top_n], start=1)]
    all_scored_rows = [_row(c, s, r, None) for c, s, r in scored]

    scores = [s for _, s, _ in scored]
    n_mid_route = sum(1 for c, _, _ in scored if c.effective_start is not None and c.effective_start > now)

    # Customer-facing quoted price -- real 2026-researched distance-tiered + truck-type + fuel
    # surcharge pricing (sim/config.py's quote_price(), the SAME function invoice generation uses
    # at completion) -- same for every candidate (it's a property of the ORDER, not who drives it),
    # so it belongs on the quote summary once, not repeated per candidate row.
    from sim.engine.reward import load_fill_ratio as _load_fill_ratio
    from sim.config import quote_price as _quote_price
    fill = _load_fill_ratio(order.weight_lbs, order.pallets, capacity_lbs, capacity_pallets)
    pricing = _quote_price(order.loaded_miles, order.load_type, order.service_type, fill)

    summary = {
        'quote_id': quote.quote_id, 'n_candidates': len(candidates), 'n_feasible': len(feasible),
        'n_idle_candidates': len(feasible) - n_mid_route, 'n_mid_route_candidates': n_mid_route,
        'best_score': round(max(scores), 2) if scores else None,
        'worst_score': round(min(scores), 2) if scores else None,
        'median_score': round(sorted(scores)[len(scores) // 2], 2) if scores else None,
        'score_spread': round(max(scores) - min(scores), 2) if scores else None,
        'top_pick_is_mid_route': top_n_rows[0]['is_mid_route_candidate'] if top_n_rows else None,
        'decision_time': now, 'requested_pickup_at': order.requested_pickup_at,
        # Brief's real compliance requirement, made visible to the dispatcher rather than silent:
        # drivers dropped from consideration entirely for lacking a passing 24h DVIR.
        'n_excluded_for_inspection': n_excluded_for_inspection,
        'loaded_miles': round(order.loaded_miles, 1), 'load_fill_ratio': round(fill, 2),
        'rate_per_mile': pricing['rate_per_mile'], 'linehaul_amount': pricing['linehaul_amount'],
        'fuel_surcharge_amount': pricing['fuel_surcharge_amount'],
        'estimated_total_charge': pricing['estimated_total_charge'],
    }

    return {'top_n': top_n_rows, 'all_scored': all_scored_rows, 'summary': summary}
