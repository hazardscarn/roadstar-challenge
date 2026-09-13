"""Backend read/write logic for the manual day-ahead Dispatch Board -- dashboard/server/main.py's
thin /api/dispatch/* endpoints call these. Same split as sim/live/score_quote.py's own module
docstring convention: heavy DB logic lives here, main.py stays a thin FastAPI wrapper.

Every write does its own server-side capacity/type/hub validation (defense in depth behind the
client's own checks, ported from the Claude Design mock -- see dashboard/src/pages/manager/
DispatchBoard.tsx) -- raises plain ValueError on a rule violation, which main.py turns into a 400.
"""
from datetime import date as date_type
from uuid import UUID

import psycopg2

from sim.db import cursor
from sim.dispatch_solver import _load_day_data, build_model
from sim.live.generate_dispatch_day import day_exists, generate
from sim.live.generate_dispatch_day import regenerate_full as _regenerate_full
from sim.live.generate_dispatch_day import regenerate_orders as _regenerate_orders


def day_status(service_date: date_type) -> str | None:
    """Cheap existence check -- no generation, unlike ensure_day()/load_board(). The Setup-panel
    GET endpoint uses this to decide whether to show the Setup panel (no day yet) or the real
    board (one already exists), instead of silently auto-generating a default-parameter day the
    first time a date is opened -- real user ask: fleet size/hub mix/order count should be a
    choice made once, up front, via Simulate, not an app default the manager discovers after the
    fact and has to undo."""
    with cursor() as cur:
        return day_exists(cur, service_date)


def _get_day_id(cur, service_date: date_type) -> UUID:
    cur.execute("select id from dispatch.days where service_date = %s", (service_date,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No dispatch day generated for {service_date} yet")
    return row[0]


def _ensure_draft(cur, service_date: date_type) -> None:
    """Real bug found directly (not assumed): ai_assign()/reset_assignments() used to rewrite
    dispatch.assignments unconditionally, with no check on dispatch.days.status. If a day was
    already finalized -- live.trips rows already created by dispatch.finalize_day() -- and one of
    these ran again (a re-solve, a reset), the assignments table would silently drift out of sync
    with the live.trips rows finalize_day() had already created: some orders' truck/driver pairing
    changed in dispatch.assignments, but no new live.trips row exists for the new pairing (a second
    finalize_day() call is a no-op once status != 'draft', see that function's own idempotency
    guard) and the OLD trip -- now referencing an order truck_number/driver combo the board no
    longer shows -- is left dangling. Confirmed directly: exactly this happened to one real test
    day (2 of a 5-order truck's live.trips rows pointed at stale, no-longer-assigned order ids;
    the 2 orders the board currently showed as assigned had no trip_id at all -- which is also why
    clicking into those rows for geofence-editing stopped working, since that UI path needs a real
    trip_id). Fix: any assignment-mutating call first reopens a finalized day (dispatch.reopen_day()
    -- cancels the stale live.trips rows and frees the driver, the exact same "Edit Dispatch" path
    the UI's own reopen button uses) so assignments and live.trips can never drift apart. The
    frontend already hides the Assign/Reset buttons once a day is finalized (DispatchBoard.tsx
    only renders them in the draft view), so this is a defense-in-depth guard for direct/API calls,
    not a new user-facing behavior -- it just makes the invariant "assignments and live.trips agree
    while a day isn't in-progress" hold no matter how these functions get called."""
    cur.execute("select status from dispatch.days where service_date = %s", (service_date,))
    row = cur.fetchone()
    if row is not None and row[0] == 'finalized':
        cur.execute("select dispatch.reopen_day(%s)", (service_date,))


def _call_assignment_fn(fn_name: str, *args) -> list[dict]:
    """Calls one of dispatch.{assign,unassign}_{order,driver}() -- one round trip to the remote
    Supabase instance no matter how many validation/write steps happen inside the function (see
    sim/sql/050_dispatch_assignment_functions.sql's own docstring for why this replaced 6-9
    sequential Python-side queries per drag). A validation failure raises a real Postgres
    exception with the same message text the old Python code used; re-raised here as a plain
    ValueError, same contract every other function in this module already has.
    """
    placeholders = ', '.join(['%s'] * len(args))
    with cursor() as cur:
        try:
            cur.execute(f"select * from dispatch.{fn_name}({placeholders})", args)
        except psycopg2.Error as exc:
            message = (exc.diag.message_primary if exc.diag and exc.diag.message_primary else str(exc)).strip()
            raise ValueError(message) from exc
        return [{'truck_number': r[0], 'driver_id': r[1], 'order_ids': [str(x) for x in r[2]]} for r in cur.fetchall()]


def ensure_day(service_date: date_type) -> None:
    """Idempotent -- generates the day's roster/order book only if it doesn't exist yet
    (sim/live/generate_dispatch_day.py's own day_exists() check), cheap on every subsequent call."""
    with cursor() as cur:
        exists = day_exists(cur, service_date) is not None
    if not exists:
        generate(service_date)


def load_board(service_date: date_type) -> dict:
    ensure_day(service_date)
    with cursor() as cur:
        day_id = _get_day_id(cur, service_date)
        cur.execute("select status from dispatch.days where id = %s", (day_id,))
        status = cur.fetchone()[0]

        cur.execute("""
            select dt.truck_number, dt.available, dt.unavailable_reason,
                   tp.truck_type, tp.capacity_lbs, tp.capacity_pallets, tp.length_ft,
                   tp.inside_height_ft, tp.width_in, hub.city
            from dispatch.day_trucks dt
            join calibration.truck_profile tp on tp.truck_number = dt.truck_number
            join reference.locations hub on hub.location_id = tp.home_hub_location_id
            where dt.day_id = %s
            order by dt.truck_number
        """, (day_id,))
        trucks = [
            {
                'truck_number': r[0], 'available': r[1], 'unavailable_reason': r[2],
                'truck_type': r[3], 'capacity_lbs': float(r[4]), 'capacity_pallets': r[5],
                'length_ft': float(r[6]), 'inside_height_ft': float(r[7]), 'width_in': float(r[8]),
                'hub': r[9],
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            select dd.driver_id, dd.available, dd.unavailable_reason,
                   dd.hos_driving_hours_remaining, dd.hos_duty_hours_remaining,
                   dd.hos_cycle1_hours_remaining, dd.hos_cycle2_hours_remaining, hub.city
            from dispatch.day_drivers dd
            join calibration.driver_home_hub dhh on dhh.driver_id = dd.driver_id
            join reference.locations hub on hub.location_id = dhh.hub_location_id
            where dd.day_id = %s
            order by dd.driver_id
        """, (day_id,))
        drivers = [
            {
                'driver_id': r[0], 'available': r[1], 'unavailable_reason': r[2],
                'hos_driving_hours_remaining': float(r[3]), 'hos_duty_hours_remaining': float(r[4]),
                'hos_cycle1_hours_remaining': float(r[5]), 'hos_cycle2_hours_remaining': float(r[6]),
                'hub': r[7],
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            select o.id, ol.label, ol.city, dl.label, dl.city, o.weight_lbs, o.pallets,
                   o.load_type, o.pickup_at, o.delivery_eta, o.rate,
                   o.pickup_location_id, o.dest_location_id, o.accepted_at,
                   -- Real bug found directly: reopen_day() CANCELS a trip, it doesn't delete it
                   -- (sim/sql/052's own docstring), so a day that's been finalized/edited/
                   -- re-finalized more than once can have SEVERAL live.trips rows for the same
                   -- dispatch_order_id. A plain LEFT JOIN duplicated the order once per matching
                   -- trip (a real observed case: 45 assigned orders came back as 88 "order" rows).
                   -- A scalar subquery picking the single most-recent NON-cancelled trip (or null)
                   -- guarantees exactly one row per order, always.
                   (select t.trip_id from live.trips t where t.dispatch_order_id = o.id and t.status != 'cancelled'
                    order by t.created_at desc limit 1) as trip_id,
                   (select (gp.location_id is not null) from live.trips t
                    left join live.trip_geofence_overrides gp on gp.trip_id = t.trip_id and gp.location_id = o.pickup_location_id
                    where t.dispatch_order_id = o.id and t.status != 'cancelled' order by t.created_at desc limit 1) as pickup_manual,
                   (select (gd.location_id is not null) from live.trips t
                    left join live.trip_geofence_overrides gd on gd.trip_id = t.trip_id and gd.location_id = o.dest_location_id
                    where t.dispatch_order_id = o.id and t.status != 'cancelled' order by t.created_at desc limit 1) as dropoff_manual
            from dispatch.day_orders o
            join reference.locations ol on ol.location_id = o.pickup_location_id
            join reference.locations dl on dl.location_id = o.dest_location_id
            where o.day_id = %s
            order by o.pickup_at
        """, (day_id,))
        # Real user ask: show the actual pickup/dropoff BUSINESS, not just a bare city name --
        # reference.locations.label already carries it ("Walmart (Mississauga)" for the curated
        # real-business pool sim/geocode_business_locations.py sourced, or a real named customer/
        # facility for any older order predating that pool) -- falls back to "{city}, ON" only if
        # a location genuinely has no more specific label than its own city.
        def _place_label(label, city):
            return label if label and label != city else f"{city}, ON"

        orders = [
            {
                'order_id': str(r[0]), 'pickup_city': _place_label(r[1], r[2]), 'dest_city': _place_label(r[3], r[4]),
                'weight_lbs': float(r[5]), 'pallets': r[6], 'load_type': r[7],
                'pickup_at': r[8].isoformat(), 'delivery_eta': r[9].isoformat(),
                'rate': float(r[10]),
                'pickup_location_id': r[11], 'dest_location_id': r[12],
                # Real user ask: "how does the dispatch know the driver have accepted trips...
                # can the manager view see it?" -- set by the driver's own POST /api/driver/
                # accept-order (dashboard/server/main.py), read back here so the board can show it.
                'accepted_at': r[13].isoformat() if r[13] else None,
                # Only set once dispatch.finalize_day() has created the real live.trips row --
                # null while still in draft, which is exactly when a trip detail page/geofence
                # makes no sense yet (nothing is actually running).
                'trip_id': str(r[14]) if r[14] else None,
                'pickup_geofence_source': 'manual' if r[15] else 'default',
                'dropoff_geofence_source': 'manual' if r[16] else 'default',
            }
            for r in cur.fetchall()
        ]

        cur.execute("select truck_number, driver_id, order_ids from dispatch.assignments where day_id = %s", (day_id,))
        assignments = {row[0]: {'driver_id': row[1], 'order_ids': [str(x) for x in row[2]]} for row in cur.fetchall()}

    # Real user ask: when an order can't be matched (the Reefer/pallet-overflow cases that came up
    # directly), show WHY -- not just "unassigned." Computed here, not guessed: mirrors the exact
    # same hard truck-type/capacity checks sim/dispatch_solver.py applies before the CP-SAT solver
    # ever sees a (truck, order) pair, so an order flagged here really was structurally infeasible,
    # not a quality-of-optimization miss the solver could have done better on.
    #
    # Real bug found directly: this was showing on the very first page load, before Dispatch with
    # AI (or any manual drag) had even been tried once -- EVERY order is trivially "unassigned" on
    # a fresh board, so a message like "N trucks could have carried this but were already
    # committed" was actively wrong (nothing has been committed to anything yet). Fix: the HARD
    # tiers (no compatible equipment type; exceeds every truck's capacity) are permanent facts
    # about this order vs. the fleet, true whether or not dispatch has run -- always shown. The
    # SOFT tier (driver-hub reach / scheduling competition) only means anything once an actual
    # assignment attempt exists on this board -- suppressed until then.
    assigned_order_ids = {oid for a in assignments.values() for oid in a['order_ids']}
    any_assignment_attempted = bool(assigned_order_ids) or any(a['driver_id'] is not None for a in assignments.values())
    for order in orders:
        order['unassigned_reason'] = (
            None if order['order_id'] in assigned_order_ids
            else _unassigned_reason(order, trucks, drivers, assignments, any_assignment_attempted)
        )

    return {
        'day_id': str(day_id), 'service_date': service_date.isoformat(), 'status': status,
        'trucks': trucks, 'drivers': drivers, 'orders': orders, 'assignments': assignments,
    }


def _unassigned_reason(
    order: dict, trucks: list[dict], drivers: list[dict], assignments: dict, any_assignment_attempted: bool,
) -> str | None:
    """Tiered, real diagnosis -- equipment type, then capacity, then driver-hub reach (all three
    are permanent facts about this order vs. TODAY's fleet roster, true whether or not dispatch
    has run yet -- always returned). The last tier ("N trucks could have, but were already
    committed") is only meaningful once a real assignment attempt exists on this board -- returns
    None (no reason shown at all) before that, rather than a misleading "already committed" claim
    on a board where nothing has been committed to anything yet."""
    available_trucks = [t for t in trucks if t['available']]
    available_drivers = [d for d in drivers if d['available']]

    type_ok = [t for t in available_trucks if t['truck_type'] == order['load_type']]
    if not type_ok:
        other_types = sorted({t['truck_type'] for t in available_trucks})
        fleet_note = f"fleet on hand today: {', '.join(other_types)}" if other_types else "no trucks available at all today"
        return f"No available {order['load_type']} truck today ({fleet_note})."

    capacity_ok = [t for t in type_ok if order['weight_lbs'] <= t['capacity_lbs'] and order['pallets'] <= t['capacity_pallets']]
    if not capacity_ok:
        max_pallets = max(t['capacity_pallets'] for t in type_ok)
        max_lbs = max(t['capacity_lbs'] for t in type_ok)
        over_bits = []
        if order['pallets'] > max_pallets:
            over_bits.append(f"{order['pallets']} pallets (largest available {order['load_type']} truck holds {max_pallets})")
        if order['weight_lbs'] > max_lbs:
            over_bits.append(f"{order['weight_lbs']:.0f} lbs (largest available {order['load_type']} truck holds {max_lbs:.0f})")
        return f"Exceeds capacity of every available {order['load_type']} truck today -- needs {' and '.join(over_bits)}."

    hubs_with_drivers = {d['hub'] for d in available_drivers}
    driver_reachable = [t for t in capacity_ok if t['hub'] in hubs_with_drivers]
    if not driver_reachable:
        return f"{len(capacity_ok)} truck(s) could carry this load by size, but none has an available driver at its home hub today."

    if not any_assignment_attempted:
        return None  # genuinely compatible on paper -- whether it gets a truck is still an open question, not yet a fact

    # Real user report, checked directly and confirmed real: "committed to other orders" was wrong
    # for a case where a truck at a reachable hub was genuinely EMPTY (no driver, no orders) -- the
    # actual blocker was that the one remaining free driver at that hub didn't have enough real
    # Hours-of-Service left once the drive from their hub OUT to this pickup (deadhead) is added on
    # top of the loaded leg -- a real HOS math problem (verified: 3.51h of driving needed vs. 2.6h
    # remaining, in the case that surfaced this), not a scheduling/capacity conflict at all. This
    # doesn't recompute that exact deadhead math here (would need a live OSRM route lookup per
    # candidate truck, for every unassigned order, on every board load -- real cost for a hint);
    # instead it distinguishes the two honestly: a driver already on another truck today (genuinely
    # "committed") vs. a driver who's free but likely HOS-thin, which points straight at the real
    # cause instead of implying every truck at that hub is simply busy.
    used_driver_ids = {a['driver_id'] for a in assignments.values() if a['driver_id'] is not None}
    reachable_hubs = {t['hub'] for t in driver_reachable}
    free_drivers = [d for d in available_drivers if d['hub'] in reachable_hubs and d['driver_id'] not in used_driver_ids]
    if free_drivers:
        low_hos = min(d['hos_driving_hours_remaining'] for d in free_drivers)
        return (
            f"{len(driver_reachable)} truck(s) match by type/size/hub, and {len(free_drivers)} driver(s) there "
            f"aren't already driving another truck today -- but their remaining Hours-of-Service (as little as "
            f"{low_hos:.1f}h drive time left) is likely short once the real drive from their hub out to this "
            "pickup is added to the loaded leg. Worth checking directly; this is a real HOS constraint, not a "
            "vague scheduling guess."
        )

    return (
        f"{len(driver_reachable)} truck(s) could have carried this load (right type, size, and a "
        "hub-matched driver in principle) -- every driver at those hubs is already driving another "
        "truck today. A scheduling trade-off, not a hard equipment or capacity mismatch."
    )


def assign_order(service_date: date_type, truck_number: str, order_id: UUID) -> list[dict]:
    """Returns the updated assignment row(s) (target truck, plus the source truck if this order
    was re-dragged off another one) -- the frontend merges these directly into its local state
    instead of doing a full board refetch."""
    return _call_assignment_fn('assign_order', service_date, truck_number, order_id)


def unassign_order(service_date: date_type, truck_number: str, order_id: UUID) -> list[dict]:
    return _call_assignment_fn('unassign_order', service_date, truck_number, order_id)


def assign_driver(service_date: date_type, truck_number: str, driver_id: int) -> list[dict]:
    return _call_assignment_fn('assign_driver', service_date, truck_number, driver_id)


def unassign_driver(service_date: date_type, truck_number: str) -> list[dict]:
    return _call_assignment_fn('unassign_driver', service_date, truck_number)


def finalize(service_date: date_type) -> None:
    """Materializes the plan into real live.trips rows (dispatch.finalize_day(), sim/sql/052) --
    one truck's assigned orders become one trip each, sequenced by pickup time; the first one for
    an otherwise-free driver goes live immediately (live.driver_status updated: truck/trailer for
    tomorrow's assignment, current_trip_id, duty_status='driving'), the rest queue as 'scheduled'
    -- the same status vocabulary /api/assign already uses. See that function's own docstring for
    why "finalize -> live immediately" instead of waiting for a day-boundary this project's live
    system doesn't model.
    """
    with cursor() as cur:
        cur.execute("select id from dispatch.days where service_date = %s", (service_date,))
        if cur.fetchone() is None:
            raise ValueError(f"No dispatch day for {service_date}")
        cur.execute("select dispatch.finalize_day(%s)", (service_date,))


def ai_assign(service_date: date_type) -> dict:
    """The "Dispatch with AI" button: runs the real CP-SAT solver (sim/dispatch_solver.py) over
    this day's real trucks/orders/drivers and REPLACES today's manual board state with its optimal
    plan -- same effect as a fleet manager dragging every match by hand, just computed at once.

    Whole-day replace, not additive: every truck's row is cleared first, then set to exactly what
    the solver decided (driver_id + order_ids, in solved order) -- there's no meaningful way to
    "merge" an AI re-solve with whatever partial manual state existed before it (the solver already
    accounts for every order and every truck/driver in one shot). A manager who wants to keep some
    manual picks and only fill the rest is a real, separate feature -- not built here, see the
    plan doc's open question on this.
    """
    ensure_day(service_date)
    trucks, orders, drivers, _current = _load_day_data(service_date)
    result = build_model(trucks, orders, drivers, service_date)
    if not result.feasible:
        raise ValueError(
            "AI Assign could not find a feasible plan for this day"
            + (" (solver timed out before finding one)" if result.has_timeout else "")
        )

    with cursor() as cur:
        _ensure_draft(cur, service_date)
        day_id = _get_day_id(cur, service_date)
        cur.execute("update dispatch.assignments set driver_id = null, order_ids = '{}' where day_id = %s", (day_id,))
        for a in result.assignments:
            if a.driver_id is None and not a.order_ids:
                continue
            cur.execute(
                "update dispatch.assignments set driver_id = %s, order_ids = %s where day_id = %s and truck_number = %s",
                (a.driver_id, [UUID(oid) for oid in a.order_ids], day_id, a.truck_number),
            )
        cur.execute("select truck_number, driver_id, order_ids from dispatch.assignments where day_id = %s", (day_id,))
        assignments = {row[0]: {'driver_id': row[1], 'order_ids': [str(x) for x in row[2]]} for row in cur.fetchall()}

    return {
        'assignments': assignments,
        'num_assigned_orders': result.num_assigned_orders,
        'num_unassigned_orders': result.num_unassigned_orders,
        'total_net_revenue': result.total_net_revenue,
        'deadhead_miles_total': result.deadhead_miles_total,
        'has_timeout': result.has_timeout,
    }


def reset_assignments(service_date: date_type) -> dict:
    """The plain "Reset" button: clears every truck back to its initial empty state (no driver, no
    orders) -- same starting point the board shows the very first time a day is opened, so AI
    Assign (or manual dispatch) can be run again from scratch. Trucks/drivers/orders themselves are
    untouched -- only dispatch.assignments."""
    with cursor() as cur:
        _ensure_draft(cur, service_date)
        day_id = _get_day_id(cur, service_date)
        cur.execute("update dispatch.assignments set driver_id = null, order_ids = '{}' where day_id = %s", (day_id,))
        cur.execute("select truck_number, driver_id, order_ids from dispatch.assignments where day_id = %s", (day_id,))
        assignments = {row[0]: {'driver_id': row[1], 'order_ids': [str(x) for x in row[2]]} for row in cur.fetchall()}
    return {'assignments': assignments}


def simulate_setup(
    service_date: date_type,
    hub_counts: dict[str, int],
    type_shares: dict[str, float],
    num_orders: int,
    seed: int | None = None,
) -> dict:
    """The Setup panel's "Simulate" button: (re)builds this date's ENTIRE dispatch day -- fleet
    roster (by hub team count + truck-type mix) and order book (by exact count) -- from parameters
    the fleet manager picked, instead of a fixed 20-truck/55-30-15/calibrated-volume default the
    app used to decide on its own. Always rebuilds, even if this date already has a day (a manager
    trying a different mix against the same date) -- see generate_dispatch_day.regenerate_full()'s
    own docstring for how that's done safely against an already-finalized day.

    Returns a normal load_board() payload plus `setup`, the ACTUAL hub/type counts the real
    equipment pool could support -- real inventory is finite (checked directly: only 4 of 131 real
    trucks are Reefer fleet-wide, Barrie has none at all) -- so a request for more of a scarce type
    than exists gets the closest real substitute, reported honestly rather than silently claimed as
    exactly what was asked for.
    """
    result = _regenerate_full(service_date, hub_counts=hub_counts, type_shares=type_shares, num_orders=num_orders, seed=seed)
    board = load_board(service_date)
    board['setup'] = {
        'requested_hub_counts': hub_counts,
        'requested_type_shares': type_shares,
        'requested_num_orders': num_orders,
        'actual_hub_counts': result.get('actual_hub_counts'),
        'actual_type_counts': result.get('actual_type_counts'),
        'actual_num_orders': result.get('num_orders'),
    }
    return board


def regenerate_order_book(service_date: date_type) -> dict:
    """DEMO-ONLY: swaps in a genuinely different random order book for the same day (same
    trucks/drivers), for showing the board/AI Assign against more than one scenario in a live demo
    -- see sim/live/generate_dispatch_day.py's regenerate_orders() docstring. Returns a full
    load_board()-shaped payload since, unlike every other write here, the ORDER SET itself changed,
    not just who's assigned to what."""
    ensure_day(service_date)
    _regenerate_orders(service_date)
    return load_board(service_date)


def reopen(service_date: date_type) -> None:
    """Undoes exactly what finalize() created (dispatch.reopen_day()) -- cancels this day's
    live.trips rows and frees any driver still pointing at one of them, so Edit Dispatch can
    change the plan and re-finalize cleanly."""
    with cursor() as cur:
        cur.execute("select id from dispatch.days where service_date = %s", (service_date,))
        if cur.fetchone() is None:
            raise ValueError(f"No dispatch day for {service_date}")
        cur.execute("select dispatch.reopen_day(%s)", (service_date,))
