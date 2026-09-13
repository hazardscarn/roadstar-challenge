"""Generates the day-ahead roster + order book for ONE service_date the first time the Dispatch
Board is opened for it (idempotent -- checks dispatch.days first, does nothing if that date
already exists). Same shape as sim/live/seed_demo_fleet.py's fleet roster + sim/engine/run_sim.py's
generate_order() bootstrap sampling, but scoped to a single calendar day of PLANNED work instead
of a continuous always-on fleet or one order at a time.

## Fleet scope

Reuses the SAME 30-driver/30-truck demo roster sim/live/seed_demo_fleet.py and
scripts/seed_demo_accounts.py already use (`_build_fleet_roster()`) -- the Dispatch Board is
operated by the same demo manager account against the same demo fleet the rest of the live app
already shows, not a second, differently-sized population.

## Orders -- real-data-sampled, region-stratified, with an OSRM-derived delivery ETA

Reuses sim/engine/run_sim.py's real bootstrap machinery (order_pool for weight/pallets/load_type,
get_route for real OSRM-cached-or-live distance/duration, quote_price for rate) and its Poisson
arrival process shape (calibration.order_arrival_rate, per-hour expected count -- see `_poisson()`
below) for realistic count AND real hour-of-day clustering -- the same shape data_analysis.ipynb's
own pickup-hour histogram shows.

**Geographic spread, take 2** (real user feedback, twice): a first version bucketed every lane to
its NEAREST of the 3 dispatch hubs, floored per hub -- still landed almost entirely on Milton/GTA
(real lane_frequency is GTA-heavy even within a hub's own catchment) and showed literally nothing
near Peterborough/Niagara Falls (checked directly: 0-2 real `calibration.lane_frequency` rows
touch either city at all -- there's genuinely almost no real historical freight data for those two
edge-of-coverage cities specifically). Fix: stratify by this project's EXISTING 30-region k-means
clustering (`sim/cluster_locations.py`, already the real-distance feature every other part of this
project uses) instead of 3 fixed hub points -- spans the whole coverage bounding box, so a region
containing Peterborough/Niagara's real geographic neighbors gets its own quota rather than being
swallowed by "whichever of 3 points is closest." Each region's quota is its real total
`lane_frequency` weight, SQRT-dampened (a standard skew-reduction transform) so the single most
common region (Milton/GTA core) doesn't crowd out every other real region entirely, while regions
with zero real lane data still correctly get zero (never a fabricated lane).

**Pickup + ETA**: `pickup_at` is a single specific time (not a window -- real user feedback that a
2h window was unnecessary), drawn from the same real per-hour Poisson shape. `delivery_eta` is
`pickup_at + get_route()`'s real OSRM-derived transit time -- the exact same distance/duration
this project's simulator and live scoring use, not a separate guessed figure.

Run: `python -m sim.live.generate_dispatch_day 2026-09-12` (defaults to tomorrow if omitted).
"""
import math
import random
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone

from psycopg2.extras import execute_values

from sim.config import LOAD_TYPE_SHARES, quote_price
from sim.db import cursor
from sim.engine.run_sim import get_route, load_sim_data
from sim.hub_weights import BARRIE_SHARE, LONDON_SHARE, MILTON_SHARE
from sim.live.seed_demo_fleet import _build_fleet_roster, _build_fleet_roster_custom

TRUCK_UNAVAILABLE_RATE = 0.08  # SYNTHESIZED stand-in for maintenance/breakdown -- see the plan's
# "out of scope" note: real tie-in to sim/engine/maintenance.py's service-interval model is a follow-up.
DRIVER_UNAVAILABLE_RATE = 0.08  # SYNTHESIZED stand-in for PTO/sick/day-off.


def day_exists(cur, service_date: date) -> str | None:
    cur.execute("select status from dispatch.days where service_date = %s", (service_date,))
    row = cur.fetchone()
    return row[0] if row else None


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm -- pure stdlib, no numpy dependency in this project (sim/engine/run_sim.py
    uses plain `random` throughout). Used INSTEAD of run_sim.py's next_order_arrival() sequential
    arrival-time walk: that function re-evaluates lambda only at each draw, so a single low-lambda
    hour can produce a large jump that skips right over an adjacent high-lambda hour -- fine for a
    long continuous multi-month simulation (any one day's variance washes out), but checked
    directly here and found to swing a single day's order count wildly (0 to 29 orders across 5
    seeds on the exact same real weekday) -- unusable for a one-day-at-a-time generator a fleet
    manager needs to see a sane board on every time they open it. Sampling each real hour's
    EXPECTED count directly (lambda IS the expected count for a 1-hour bucket) and scattering
    arrivals uniformly within that hour keeps the same real calibration.order_arrival_rate shape
    with only genuine Poisson variance, not compounded random-walk variance.
    """
    if lam <= 0:
        return 0
    l_exp = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= l_exp:
            return k - 1


def _load_business_pool(cur) -> dict[str, list[int]]:
    """The curated, REAL, named business addresses (sim/geocode_business_locations.py -- real
    factories/DCs found via web search, plus chain retail confirmed by successful geocoding),
    tagged with which of the 3 real dispatch hubs they represent local freight for. Grouped here so
    _generate_orders() can draw a hub's own catchment businesses directly."""
    cur.execute("""
        select c.hub_catchment, l.location_id from reference.business_hub_catchment c
        join reference.locations l on l.location_id = c.location_id
    """)
    pool: dict[str, list[int]] = {}
    for hub, loc_id in cur.fetchall():
        pool.setdefault(hub, []).append(loc_id)
    return pool


# Real user description of the shape wanted: "London could have some of its area covered trips and
# some for Barrie milton etc as distributions and then some for complete[ing the network] etc" --
# most of a hub's freight stays local to its own catchment (a London-hub truck picks up/drops off
# in the London/KWC/Guelph area), a real minority connects hubs (the actual inter-hub chaining the
# CP-SAT solver is built to exploit). SYNTHESIZED split -- no real data says exactly 75/25, but it's
# the shape described, not an even 3-way mix.
LOCAL_ORDER_SHARE = 0.75


def _generate_orders(
    cur, data, service_date: date, rng: random.Random,
    num_orders: int | None = None, hub_shares: dict[str, float] | None = None,
) -> list[dict]:
    # Real user report, twice over: order pickup/dropoff kept clustering almost entirely around
    # Milton/GTA even for London- and Barrie-hub trucks -- "it's very bad to show them that London
    # hub trucks driving into Milton for picking and Mississauga." Checked directly: BOTH this
    # project's real historical lane data (calibration.lane_frequency) AND its existing 2,000-row
    # OSM-sourced synthetic_facility pool are themselves 90%+ nearest-Milton -- a real artifact of
    # the GTA being far more densely mapped/travelled than London/Barrie/Niagara/Peterborough, not
    # fixable by re-weighting either pool. Fix: draw origin/destination from the curated real-
    # business pool (_load_business_pool()), stratified by hub catchment using the SAME shares the
    # fleet itself was built from (`hub_shares`, threaded down from generate()'s own hub_counts) --
    # so a day's order geography always matches its truck/driver geography, by construction.
    business_pool = _load_business_pool(cur)
    default_shares = {'Milton': MILTON_SHARE, 'London': LONDON_SHARE, 'Barrie': BARRIE_SHARE}
    hub_shares = {h: s for h, s in (hub_shares or default_shares).items() if business_pool.get(h)}
    hub_names = list(hub_shares)
    hub_weights = [hub_shares[h] for h in hub_names]

    day_start = datetime.combine(service_date, time(0, 0), tzinfo=timezone.utc)
    dow = service_date.weekday()  # Monday=0 .. Sunday=6
    # Real user correction: a fleet manager plans TOMORROW's board, and "tomorrow" is whatever
    # calendar day it happens to be -- which, run often enough, lands on a real weekend. The real
    # calibrated arrival rate genuinely IS much lower on weekends (checked directly: integrated
    # daily lambda is ~50/weekday on average vs. ~3 Saturday / ~11 Sunday) -- honest, but wrong for
    # this tool's actual job: a day-ahead PLANNING board with a real fleet of ~28-30 trucks needs a
    # representative BUSINESS-DAY order volume to actually exercise the optimizer (more orders than
    # trucks, real multi-order chaining) -- not to literally replay whatever a historical Saturday
    # looked like. Weekends borrow the average of the 5 real weekday profiles instead of their own
    # real (genuinely thin) one; a real weekday keeps its own real profile unchanged.
    if dow >= 5:
        daily_total = sum(sum(data.order_arrival_rate.get((h, d), 0.1) for d in range(5)) / 5 for h in range(24))
    else:
        daily_total = sum(data.order_arrival_rate.get((h, dow), 0.1) for h in range(24))

    # Real user report: generated pickup times kept clustering almost entirely in the morning,
    # bad enough to look like an allocation bug on its own ("jobs don't all happen in the
    # morning"). Root cause found directly: the HOUR-OF-DAY shape above (calibration.
    # order_arrival_rate) is built from CREATED_TIME -- when an order was BOOKED -- not from when a
    # pickup actually happens, a different real event (a shipper can book Tuesday's pickup Monday
    # morning). The real signal for "what time does a pickup actually happen" is ground_truth.
    # historical_orders.actual_pickup -- checked directly: 91.8% populated (ground_truth.
    # historical_legs.det_pick_arrive, the notebook's own "dock arrival" column, is 100% EMPTY in
    # this loaded dataset) and a genuinely smooth real business-day curve (peaks ~6-9am, tapers
    # through the afternoon/evening), not the notebook's separate appointment-stamp artifact. Real
    # user clarification: this doesn't need to replay that historical sample verbatim -- just use
    # its real SHAPE to spread today's total volume across the day, instead of the wrong-event
    # shape above. Total daily volume still comes from the real booking-time-based estimate
    # (`daily_total`, unchanged) -- only which HOUR each of those orders lands in changes.
    cur.execute("""
        select extract(hour from actual_pickup)::int as h, count(*)
        from ground_truth.historical_orders where actual_pickup is not null group by h
    """)
    pickup_hour_counts = dict(cur.fetchall())
    pickup_hour_total = sum(pickup_hour_counts.values()) or 1
    hourly_lambda = {h: daily_total * (pickup_hour_counts.get(h, 0) / pickup_hour_total) for h in range(24)}

    # Real user ask (Setup panel): let a manager type an exact order COUNT and get exactly that
    # many, instead of the calibrated arrival-rate integral's own (Poisson) expected value -- a
    # per-hour Poisson draw is the right model for "how many orders showed up today" in the
    # default/no-count-given mode, but checked directly: it has enough day-to-day variance
    # (~sqrt(N)) that asking for 35 could plausibly hand back 25, which defeats a control whose
    # whole point is "I typed a number, I want that many." So: draw exactly `num_orders` hours,
    # ONE CATEGORICAL PICK PER ORDER weighted by the same real hourly_lambda shape -- still
    # realistically clustered around the real busy hours, just an exact total instead of a
    # Poisson-distributed one.
    if num_orders is not None:
        hours = list(range(24))
        weights = [hourly_lambda[h] for h in hours]
        if sum(weights) <= 0:
            weights = [1.0] * 24
        arrivals = sorted(
            day_start + timedelta(hours=h, minutes=rng.uniform(0, 60))
            for h in rng.choices(hours, weights=weights, k=num_orders)
        )
    else:
        arrivals = []
        for hour in range(24):
            for _ in range(_poisson(rng, hourly_lambda[hour])):
                arrivals.append(day_start + timedelta(hours=hour, minutes=rng.uniform(0, 60)))
        arrivals.sort()

    # Real user decision (sim/config.py's LOAD_TYPE_SHARES): draw load_type from the fixed 70%
    # Dry Van/25% Reefer/5% Flatbed split, not order_pool's own real historical mix (~85%/4%/10%)
    # -- "orders should be kinda on the same lines" as the truck fleet's own type mix. weight_lbs/
    # pallets still come from a REAL historical order of that same type (bucketed once here), so
    # the cargo profile per type stays realistic -- only which type gets picked is overridden.
    pool_by_type: dict[str, list[tuple[float, float]]] = {}
    for w, p, lt in data.order_pool:
        pool_by_type.setdefault(lt, []).append((w, p))
    type_names = [t for t in LOAD_TYPE_SHARES if pool_by_type.get(t)]
    type_weights = [LOAD_TYPE_SHARES[t] for t in type_names]

    orders = []
    for pickup_at in arrivals:
        hub = rng.choices(hub_names, weights=hub_weights, k=1)[0]
        local_pool = business_pool[hub]
        other_hubs = [h for h in hub_names if h != hub]
        # Most orders stay local to their hub's own catchment (both ends drawn from the SAME real
        # business pool); the rest connect two different hubs' real freight -- see LOCAL_ORDER_SHARE.
        if other_hubs and rng.random() >= LOCAL_ORDER_SHARE:
            origin_id = rng.choice(local_pool)
            dest_id = rng.choice(business_pool[rng.choice(other_hubs)])
        elif len(local_pool) >= 2:
            origin_id, dest_id = rng.sample(local_pool, 2)
        else:  # a hub with only one real business on file -- can't happen with the curated list
            # today, but degrade to a same-point non-order rather than crash if it ever does.
            origin_id = dest_id = local_pool[0]
        load_type = rng.choices(type_names, weights=type_weights, k=1)[0]
        weight_lbs, pallets = rng.choice(pool_by_type[load_type])
        # Real OSRM-cached-or-live distance/duration (sim/engine/run_sim.py's get_route(), the
        # SAME function the simulator and live scoring use) -- delivery_eta is pickup_at plus
        # this real transit time, not a separate guessed figure.
        loaded_miles, loaded_hours = get_route(data, origin_id, dest_id)
        price = quote_price(loaded_miles, load_type)
        orders.append({
            'pickup_location_id': origin_id,
            'dest_location_id': dest_id,
            'weight_lbs': weight_lbs,
            'pallets': int(round(pallets)),
            'load_type': load_type,
            'pickup_at': pickup_at,
            'delivery_eta': pickup_at + timedelta(hours=loaded_hours),
            'loaded_miles': loaded_miles,
            'loaded_hours': loaded_hours,
            'rate': price['estimated_total_charge'],
        })
    return orders


def generate(
    service_date: date,
    seed: int | None = None,
    hub_counts: dict[str, int] | None = None,
    type_shares: dict[str, float] | None = None,
    num_orders: int | None = None,
) -> dict:
    """Generates the day-ahead roster + order book. Two modes:

    - Default (`hub_counts=None`): the fixed 55/30/15-hub, real-type-mix, calibrated-volume fleet
      _build_fleet_roster() always built -- unchanged from before this function took parameters.
    - Custom (`hub_counts` given -- the Dispatch Board's Setup-panel "Simulate" button, real user
      ask to control fleet size/hub mix/type mix/order count directly instead of the app deciding
      them): builds via _build_fleet_roster_custom() and, if `num_orders` is given, scales the
      order-arrival volume to that target (see _generate_orders()'s own comment on how).
    """
    rng = random.Random(seed if seed is not None else hash(service_date.isoformat()) & 0xFFFFFFFF)
    data = load_sim_data()

    with cursor() as cur:
        existing_status = day_exists(cur, service_date)
        if existing_status is not None:
            print(f"dispatch.days already has {service_date} (status={existing_status}) -- nothing to do")
            return {'generated': False, 'status': existing_status}

        actual_hub_counts: dict[str, int] | None = None
        actual_type_counts: dict[str, int] | None = None
        if hub_counts is not None:
            driver_ids, driver_trucks, actual_hub_counts, actual_type_counts = _build_fleet_roster_custom(
                cur, hub_counts, type_shares or {},
            )
        else:
            driver_ids, driver_trucks = _build_fleet_roster(cur)
        truck_numbers = list(driver_trucks.values())

        cur.execute("insert into dispatch.days (service_date, status) values (%s, 'draft') returning id", (service_date,))
        day_id = cur.fetchone()[0]

        truck_rows = []
        for tn in truck_numbers:
            available = rng.random() >= TRUCK_UNAVAILABLE_RATE
            truck_rows.append((day_id, tn, available, None if available else 'Scheduled maintenance'))
        execute_values(cur, "insert into dispatch.day_trucks (day_id, truck_number, available, unavailable_reason) values %s", truck_rows)

        driver_rows = []
        for driver_id in driver_ids:
            available = rng.random() >= DRIVER_UNAVAILABLE_RATE
            # Same wide, realistic HOS-snapshot sampling sim/live/seed_demo_fleet.py already uses
            # (2-13h real spread on the binding daily clock, duty/cycle1/cycle2 layered on top) --
            # reused as-is rather than re-derived, see that script's own comment for the sourcing.
            hos_driving = rng.uniform(2.0, 13.0)
            hos_duty = min(14.0, hos_driving + rng.uniform(0.0, 1.0))
            hos_cycle1 = min(70.0, hos_driving + rng.uniform(10.0, 55.0))
            hos_cycle2 = min(120.0, hos_cycle1 + rng.uniform(10.0, 45.0))
            driver_rows.append((
                day_id, driver_id, available,
                None if available else 'Day off',
                round(hos_driving, 1), round(hos_duty, 1), round(hos_cycle1, 1), round(hos_cycle2, 1),
            ))
        execute_values(
            cur,
            """insert into dispatch.day_drivers
               (day_id, driver_id, available, unavailable_reason,
                hos_driving_hours_remaining, hos_duty_hours_remaining, hos_cycle1_hours_remaining, hos_cycle2_hours_remaining)
               values %s""",
            driver_rows,
        )

        # Real user ask: order GEOGRAPHY should match the fleet's own hub mix, by construction --
        # a day set up as mostly-Milton trucks should also be mostly-Milton freight, and a London-
        # or Barrie-heavy fleet should see its own area's real businesses show up proportionally,
        # not just whatever the app's fixed default happened to be.
        hub_order_shares = (
            {h: n / sum(hub_counts.values()) for h, n in hub_counts.items() if n > 0}
            if hub_counts else None
        )
        orders = _generate_orders(cur, data, service_date, rng, num_orders=num_orders, hub_shares=hub_order_shares)
        order_rows = [
            (day_id, o['pickup_location_id'], o['dest_location_id'], o['weight_lbs'], o['pallets'],
             o['load_type'], o['pickup_at'], o['delivery_eta'], o['loaded_miles'], o['loaded_hours'], o['rate'])
            for o in orders
        ]
        if order_rows:
            execute_values(
                cur,
                """insert into dispatch.day_orders
                   (day_id, pickup_location_id, dest_location_id, weight_lbs, pallets, load_type,
                    pickup_at, delivery_eta, loaded_miles, loaded_hours, rate)
                   values %s""",
                order_rows,
            )

        # Empty assignment row per truck up front -- matches the board's own
        # `assignments[truckId] = {driverId: null, orderIds: []}` shape from the first load.
        assignment_rows = [(day_id, tn, None, []) for tn in truck_numbers]
        execute_values(cur, "insert into dispatch.assignments (day_id, truck_number, driver_id, order_ids) values %s", assignment_rows)

    print(f"Generated dispatch day {service_date}: {len(truck_numbers)} trucks, {len(driver_ids)} drivers, {len(orders)} orders")
    return {
        'generated': True,
        'num_trucks': len(truck_numbers),
        'num_drivers': len(driver_ids),
        'num_orders': len(orders),
        'actual_hub_counts': actual_hub_counts,
        'actual_type_counts': actual_type_counts,
    }


def regenerate_full(
    service_date: date,
    hub_counts: dict[str, int] | None = None,
    type_shares: dict[str, float] | None = None,
    num_orders: int | None = None,
    seed: int | None = None,
) -> dict:
    """The Setup panel's "Simulate" button: unlike generate() (idempotent -- a no-op if the date
    already has a day), this ALWAYS rebuilds -- a manager retrying a different fleet size/hub mix/
    order volume against the same date, not just the first-ever open of it. Wipes this date's
    entire prior generation (fleet roster, order book, assignments -- and, if it had already been
    finalized, its real live.trips rows too, cancelled via dispatch.reopen_day() first, the same
    "Edit Dispatch" path) before calling generate() fresh with the new parameters. dispatch.days'
    child tables all cascade-delete off day_id (sim/sql/048), so dropping that one row is enough.
    """
    with cursor() as cur:
        cur.execute("select id, status from dispatch.days where service_date = %s", (service_date,))
        row = cur.fetchone()
        if row is not None:
            day_id, status = row
            if status == 'finalized':
                cur.execute("select dispatch.reopen_day(%s)", (service_date,))
            cur.execute(
                "update live.trips set dispatch_order_id = null "
                "where dispatch_order_id in (select id from dispatch.day_orders where day_id = %s)",
                (day_id,),
            )
            cur.execute("delete from dispatch.days where id = %s", (day_id,))
    return generate(service_date, seed=seed, hub_counts=hub_counts, type_shares=type_shares, num_orders=num_orders)


def regenerate_orders(service_date: date, seed: int | None = None) -> int:
    """DEMO-ONLY: throws away this day's current order book (and, since they'd now reference
    deleted orders, its current assignments too) and generates a genuinely different one. Unlike
    generate()'s default seed (a deterministic hash of the date, so a page refresh always shows the
    SAME board), this defaults to an unseeded RNG -- each call is meant to look different, matching
    the "let me see another order book" demo use case this exists for. Trucks/drivers/day_id are
    untouched; only dispatch.day_orders and dispatch.assignments are affected.
    """
    rng = random.Random(seed)
    data = load_sim_data()
    with cursor() as cur:
        cur.execute("select id, status from dispatch.days where service_date = %s", (service_date,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No dispatch day for {service_date} yet")
        day_id, status = row

        # If this day was already finalized (e.g. from an earlier demo run), cancel its real
        # live.trips rows first (dispatch.reopen_day() -- the same function "Edit Dispatch" calls)
        # -- otherwise the delete below hits live.trips' dispatch_order_id foreign key. reopen_day()
        # only CANCELS those rows, it doesn't clear the FK link itself, so that's done explicitly
        # next regardless of whether this day needed reopening: about to delete the order book
        # entirely, so the "which trip did this dispatch order become" trace is moot either way.
        if status == 'finalized':
            cur.execute("select dispatch.reopen_day(%s)", (service_date,))
        cur.execute(
            "update live.trips set dispatch_order_id = null "
            "where dispatch_order_id in (select id from dispatch.day_orders where day_id = %s)",
            (day_id,),
        )

        cur.execute("delete from dispatch.day_orders where day_id = %s", (day_id,))
        cur.execute("update dispatch.assignments set driver_id = null, order_ids = '{}' where day_id = %s", (day_id,))

        orders = _generate_orders(cur, data, service_date, rng)
        order_rows = [
            (day_id, o['pickup_location_id'], o['dest_location_id'], o['weight_lbs'], o['pallets'],
             o['load_type'], o['pickup_at'], o['delivery_eta'], o['loaded_miles'], o['loaded_hours'], o['rate'])
            for o in orders
        ]
        if order_rows:
            execute_values(
                cur,
                """insert into dispatch.day_orders
                   (day_id, pickup_location_id, dest_location_id, weight_lbs, pallets, load_type,
                    pickup_at, delivery_eta, loaded_miles, loaded_hours, rate)
                   values %s""",
                order_rows,
            )

    print(f"Regenerated order book for {service_date}: {len(orders)} orders")
    return len(orders)


if __name__ == "__main__":
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else (datetime.now(timezone.utc).date() + timedelta(days=1))
    generate(target)
