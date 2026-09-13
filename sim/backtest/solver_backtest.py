"""Day-by-day backtest: REAL historical dispatch vs. the CP-SAT AI-assign solver
(sim/dispatch_solver.py), on the SAME real order book, one real calendar day at a time.

Real user ask, in order: before building any "AI Assign" UI, prove it out against real history
first -- apples-to-apples, not a leap of faith. Different from sim/backtest/real_data_replay.py
(REAL vs. the OLD trained value-function model, one continuous multi-month replay window): this
compares REAL vs. the NEW CP-SAT solver, one real CALENDAR DAY at a time -- the natural unit for a
day-ahead dispatch tool. Reuses that script's own real-data loaders (`load_real_dispatched_orders()`,
`load_undispatched_orders()`, `score_real_leg()`) rather than re-deriving them.

## Real fleet-scope bug, found directly and fixed (real user correction, not a nitpick)

A first version of this script gave the AI solver access to all 131 `calibration.truck_profile`
rows every day. Checked directly against `sim/load_ground_truth.py`: `ground_truth.drivers`/
`trucks` (131 each) are the RAW company-wide roster sheets, loaded with **no** Ontario filtering
at all (`load_drivers()`/`load_trucks()`, sim/load_ground_truth.py:55-101) -- unlike
`historical_orders`/`historical_legs`, which ARE explicitly ON-ON filtered at load time (that
file's own printed "(ON-ON only, of N total rows in the sheet)" lines). So most of those 131
drivers/trucks may belong to the company's OTHER (non-Ontario) routes and were never actually
available to Southern-Ontario dispatch at all -- 131 overstates real capacity, not a fair
comparison. The real Southern-Ontario-relevant roster is the same 42 drivers
`real_data_replay.py`'s own `load_real_dispatched_orders()` already established (distinct
`driver_id` values on ON-ON `historical_legs`, joined to a usable ON-ON `historical_orders` row) --
reused here as the SAME authoritative set, not a second, slightly-different count.

## Two backtest modes (real user ask, both required)

- **Mode A ("as it was")**: for day D, only the drivers who REAL actually dispatched on day D
  specifically are available to the AI solver too -- the strictest, most literal "given the exact
  same people the real dispatcher had that day, could AI have done better with them" comparison.
- **Mode B ("period pool")**: all 42 South-Ontario drivers are available every backtest day,
  regardless of whether that specific driver happened to run a real order on that specific day --
  representing the realistic, known South-Ontario-dedicated crew as a standing resource, not
  reading day-to-day real absence as unavailability (a driver REAL didn't happen to use that day
  isn't necessarily off -- reality just doesn't record who was idle vs. off-duty).

Both modes get a truck via the SAME roster-building rule sim/live/seed_demo_fleet.py's own
`_build_fleet_roster()` already established (real `driver_equipment` pairs first, then fill from
real, unclaimed truck numbers, deterministic) -- built ONCE for these 42 drivers specifically, not
re-derived.

## What's compared, and what isn't (apples-to-apples, stated plainly)

REAL's own revenue/deadhead numbers are real facts (real driver, real route, real cargo) --
`score_real_leg()`'s exact same "apples-to-apples" restraint from real_data_replay.py applies here
too: lateness/HOS-risk/breakdown-risk are excluded from REAL's own reward (no reconstructable real
state to charge them against fairly). The AI side's `total_net_revenue` comes from
`sim.dispatch_solver.build_model()`'s own post-solve `compute_reward()` call, which DOES include
those terms -- so the AI number is a fuller accounting, not a smaller one. Both sides' MISSED-ORDER
count is a directly comparable real fact either way (REAL's `was_dispatched=false` vs. AI's
`num_unassigned_orders`).

**Driver daily HOS -- revised per real user feedback**: an earlier version sampled a random 2-13h
HOS snapshot per driver per day. Real problem, found from the results themselves: REAL served
92.8% of orders with these same driver counts, proving these drivers' REAL that-day capacity was
routinely higher than a random sample often landed on -- Mode A was sometimes scoring WORSE than
REAL purely because of an unlucky low HOS draw, not a worse dispatch decision. Fixed (see
`HosTracker`): every driver starts at the full legal limit on their first day in this backtest,
then the daily/cycle clocks are SIMULATED forward from what THIS solver's own prior assignments
actually used -- a real, consistent trajectory, not a fresh random guess every day.

**Deadhead -- revised per real user feedback**: "deadhead should be the day's runs without a load
to charge for... includes pickup deadhead and drive back home hub at EOD." An earlier version only
priced the FIRST order's hub-to-pickup leg per truck per day and silently zeroed everything else
(including the drive home) -- both a real undercount (no end-of-day return charged at all) and
inconsistent with the objective's own per-order estimate. `dispatch_solver.py`'s `build_model()`
now recomputes the full, real chosen sequence after solving: hub -> first pickup, each
subsequent pickup from the PREVIOUS order's own dropoff (not the hub again), and the empty run
back to the home hub at end of day -- applied to REAL's own numbers too (`_score_real_day()`'s new
end-of-day leg), so both arms are charged the identical definition, not two different ones.

Run: `python -m sim.backtest.solver_backtest` (writes daily_results.csv with BOTH modes' columns +
prints a summary; use `sim/backtest/plot_backtest.py` afterward for the deck-ready facet plots).
"""
import os
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date as date_type, timedelta

import pandas as pd

from sim.backtest.real_data_replay import load_real_dispatched_orders, load_undispatched_orders, score_real_leg
from sim.config import (
    ASSUMED_OPERATING_COST_PER_MILE, HOS_CYCLE_1_MAX_HOURS, HOS_CYCLE_2_MAX_HOURS, HOS_MAX_DRIVING_HOURS,
    HOS_MAX_ON_DUTY_HOURS, linehaul_rate_per_mile,
)
from sim.db import cursor
from sim.dispatch_solver import Driver, Order, SolverResult, Truck, build_model, dwell_hours_per_order
from sim.engine.run_sim import get_route, load_sim_data

MIN_ORDERS_PER_DAY = 5  # skip near-empty real days -- not enough signal to be a meaningful comparison
UNDISPATCHED_LEAD_HOURS = 44.3  # SAME real median lead time real_data_replay.py's own
# build_undispatched_orders_for_replay() already uses (calibration.order_lead_time_hours median).
OUT_DIR = 'documents/results/dispatch_solver_backtest'
CYCLE1_WINDOW_DAYS = 7
CYCLE2_WINDOW_DAYS = 14
NUM_SEARCH_WORKERS = 8  # per solve; two solves (mode A, mode B) run concurrently on threads --
# CP-SAT's actual C++ search releases the GIL, so this is real parallelism, not GPU (see the real
# user question this answers: no mainstream CP/MIP solver -- CP-SAT, Gurobi, CPLEX, SCIP -- has a
# GPU backend; branch-and-bound/SAT search doesn't parallelize onto GPU hardware the way dense
# linear algebra does). 8 x 2 concurrent = 16 of this machine's 20 cores, leaving headroom for the
# main thread/DB I/O.


@dataclass
class DayResult:
    day: str
    n_orders: int
    real_dispatched: int
    real_missed: int
    real_revenue: float
    real_deadhead_cost: float
    real_deadhead_miles: float
    real_net: float
    # Mode A -- only the drivers REAL actually used THIS day
    a_assigned: int
    a_unassigned: int
    a_net_revenue: float
    a_deadhead_miles: float
    # Mode B -- the full Milton/London South-Ontario pool, every day
    b_assigned: int
    b_unassigned: int
    b_net_revenue: float
    b_deadhead_miles: float


def _compute_two_hub_homes(cur, driver_ids: list[int]) -> tuple[dict[int, str], dict[int, int]]:
    """Real user correction: Barrie is a hub WE synthesized for the forward-looking Dispatch
    Board (`sim.hub_weights`/`calibrate_driver_home_hub.py`'s 3-anchor draw) -- it's fine to keep
    for the live system, but it has no place in a backtest scored against real history, which only
    ever had 2 real hubs. Recomputes every one of these 42 real drivers' home hub using ONLY
    London/Milton as anchors -- the SAME real historical-leg-derived nearest-anchor methodology
    `sim/calibrate_driver_home_hub.py` already established, just without the 3rd anchor that
    doesn't apply here -- rather than reading the live `calibration.driver_home_hub` table (which
    is computed WITH Barrie in mind and would incorrectly drop real drivers instead of correctly
    re-homing them to one of the 2 hubs that actually existed).
    """
    from sim.engine.run_sim import _haversine_km

    cur.execute("select location_id, label from reference.locations where label like 'RoadStar Terminal%'")
    anchor_loc: dict[str, int] = {}
    for loc_id, label in cur.fetchall():
        if 'London' in label:
            anchor_loc['London'] = loc_id
        elif 'Milton' in label:
            anchor_loc['Milton'] = loc_id
    cur.execute(
        "select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations where location_id = any(%s)",
        (list(anchor_loc.values()),),
    )
    anchor_coords = {loc_id: (lat, lon) for loc_id, lat, lon in cur.fetchall()}

    cur.execute("""
        select hl.driver_id, ho.origin_location_id, count(*) as n
        from ground_truth.historical_legs hl
        join ground_truth.historical_orders ho on ho.trip_number = hl.trip_number
        where hl.driver_id = any(%s) and ho.origin_location_id is not null
        group by hl.driver_id, ho.origin_location_id
    """, (driver_ids,))
    by_driver: dict[int, list[tuple[int, int]]] = {}
    for driver_id, loc, n in cur.fetchall():
        by_driver.setdefault(driver_id, []).append((loc, n))

    loc_ids_needed = {loc for rows in by_driver.values() for loc, _n in rows}
    cur.execute(
        "select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations where location_id = any(%s)",
        (list(loc_ids_needed),),
    )
    loc_coords = {loc_id: (lat, lon) for loc_id, lat, lon in cur.fetchall()}

    home_hub_loc: dict[int, int] = {}
    for driver_id, rows in by_driver.items():
        rows.sort(key=lambda x: -x[1])  # most-frequent real origin first
        top_loc = rows[0][0]
        if top_loc not in loc_coords:
            continue
        lat, lon = loc_coords[top_loc]
        home_hub_loc[driver_id] = min(anchor_coords, key=lambda aid: _haversine_km((lat, lon), anchor_coords[aid]))

    # Any of the 42 with no usable real leg-origin signal (should be rare -- they're all in the
    # real DISPATCHED-orders subset by definition) get the REAL empirical London:Milton ratio
    # from the drivers who DO have signal -- same real-data-informed-not-uniform principle
    # calibrate_driver_home_hub.py already established, just without a 3rd share to fold in.
    london_id, milton_id = anchor_loc['London'], anchor_loc['Milton']
    l_count = sum(1 for v in home_hub_loc.values() if v == london_id)
    m_count = sum(1 for v in home_hub_loc.values() if v == milton_id)
    london_share = l_count / (l_count + m_count) if (l_count + m_count) else 0.5
    rng = random.Random(43)
    missing = [d for d in driver_ids if d not in home_hub_loc]
    for d in missing:
        home_hub_loc[d] = london_id if rng.random() < london_share else milton_id
    if missing:
        print(f"  ({len(missing)} of {len(driver_ids)} drivers had no real leg-origin signal -- "
              f"assigned by the real {london_share:.0%}/{1 - london_share:.0%} London/Milton split observed in the rest)")

    name_by_loc = {v: k for k, v in anchor_loc.items()}
    driver_hubs = {d: name_by_loc[home_hub_loc[d]] for d in driver_ids}
    return driver_hubs, home_hub_loc


def _build_south_ontario_roster() -> tuple[list[int], dict[int, str], list[Truck], dict[int, int]]:
    """All 42 real South-Ontario drivers (real_data_replay.py's own established set -- none
    dropped, see `_compute_two_hub_homes()` for why), each given a truck via the SAME real-pairs-
    first-then-fill rule sim/live/seed_demo_fleet.py's `_build_fleet_roster()` already uses -- not
    the full 131-truck/driver company-wide roster (see module docstring).

    Returns (driver_ids, driver_id -> hub city, list of their Truck objects, driver_id -> hub location_id).
    """
    driver_ids = sorted(load_real_dispatched_orders()['driver_id'].unique().tolist())

    with cursor() as cur:
        driver_hubs, driver_hub_loc = _compute_two_hub_homes(cur, driver_ids)
        print(f"South-Ontario roster: {len(driver_ids)} real drivers, all homed to real "
              f"London/Milton hubs only (no Barrie -- see module docstring).")

        cur.execute(
            "select driver_id, truck_number from ground_truth.driver_equipment where truck_number is not null "
            "and driver_id = any(%s) order by driver_id",
            (driver_ids,),
        )
        driver_trucks: dict[int, str] = dict(cur.fetchall())
        n_real_pairs = len(driver_trucks)
        used_trucks = set(driver_trucks.values())

        # Truck NUMBER choice doesn't need hub-matching here -- every truck's hub gets overridden
        # to its own driver's (2-hub-only) home below regardless of what calibration.truck_profile
        # originally said, so any unclaimed real truck number will do.
        cur.execute("select truck_number from ground_truth.trucks order by truck_number")
        all_truck_numbers = [r[0] for r in cur.fetchall()]
        remaining_trucks = iter(t for t in all_truck_numbers if t not in used_trucks)
        for driver_id in (d for d in driver_ids if d not in driver_trucks):
            driver_trucks[driver_id] = next(remaining_trucks)

        print(f"  {n_real_pairs} with a real known truck (driver_equipment), "
              f"{len(driver_ids) - n_real_pairs} filled from an unclaimed real truck number.")

        truck_numbers = list(driver_trucks.values())
        cur.execute("""
            select truck_number, truck_type, capacity_lbs, capacity_pallets
            from calibration.truck_profile where truck_number = any(%s)
        """, (truck_numbers,))
        truck_by_number = {r[0]: (r[1], float(r[2]), int(r[3])) for r in cur.fetchall()}

    # Every truck's hub is its OWN driver's (real, 2-hub-only) home hub -- type/capacity still
    # come from calibration.truck_profile (that part's synthesis is unrelated to which hub a
    # truck calls home).
    trucks = [
        Truck(driver_trucks[d], *truck_by_number[driver_trucks[d]], driver_hubs[d], driver_hub_loc[d])
        for d in driver_ids
    ]
    return driver_ids, driver_hubs, trucks, driver_hub_loc


class HosTracker:
    """Real user ask: "assume for AI HOS is clean from the beginning and then start from there...
    simulate the HOS states for each next day." Replaces the earlier random-per-day HOS sample
    (which could land BELOW what a driver's real demonstrated capacity that day actually was,
    artificially capping AI below REAL for reasons that had nothing to do with dispatch quality).

    Every driver starts at the full legal limit (sim/config.py's real regulatory constants) on
    their first day in this backtest. Daily driving/duty clocks reset fresh each morning (this
    operation's own day-ahead framing: a qualifying rest happens every night, matching how
    dispatch.day_drivers/generate_dispatch_day.py already treat "the day" as the planning unit).
    Cycle1/cycle2 are the REAL rolling 7-day/14-day sum of on-duty hours actually logged by
    THIS solver's own prior assignments -- not sampled, simulated forward from what actually got
    dispatched, one mode's own tracker per mode (Mode A and Mode B make different choices, so
    they carry different HOS state forward independently).
    """
    def __init__(self):
        self.hours_by_day: dict[int, dict[date_type, float]] = {}

    def record(self, driver_id: int, day: date_type, hours_used: float) -> None:
        self.hours_by_day.setdefault(driver_id, {})[day] = hours_used

    def remaining_for(self, driver_id: int, day: date_type) -> tuple[float, float, float, float]:
        log = self.hours_by_day.get(driver_id, {})
        cycle1_used = sum(h for d, h in log.items() if 0 <= (day - d).days < CYCLE1_WINDOW_DAYS)
        cycle2_used = sum(h for d, h in log.items() if 0 <= (day - d).days < CYCLE2_WINDOW_DAYS)
        return (
            HOS_MAX_DRIVING_HOURS, HOS_MAX_ON_DUTY_HOURS,
            max(0.0, HOS_CYCLE_1_MAX_HOURS - cycle1_used), max(0.0, HOS_CYCLE_2_MAX_HOURS - cycle2_used),
        )


def _score_real_day(data, day_dispatched: pd.DataFrame, driver_hub_loc: dict[int, int]) -> tuple[float, float, float]:
    """REAL side for one day -- per-driver sequential deadhead WITHIN THIS DAY only (a driver's
    prior real destination resets each new calendar day, unlike real_data_replay.py's own
    continuous multi-month walk -- the whole point here is per-day comparison), PLUS the same
    start-of-day (hub -> first pickup) and end-of-day (last dropoff -> hub) empty legs the AI side
    is charged too (real user correction: the raw historical data has no record of a driver's
    empty run TO their first real pickup of the day -- it only starts timing at the pickup itself
    -- but a real driver has to start somewhere, and this operation's own standing assumption is
    the home hub every morning. Applied identically to both arms, or AI's hub-anchored deadhead
    was being compared against an artificially deadhead-free REAL first leg -- not apples-to-apples)."""
    revenue = dh_cost = dh_miles = 0.0
    for driver_id, grp in day_dispatched.groupby('driver_id'):
        prev_dest = driver_hub_loc.get(driver_id)  # assume the day starts at the driver's home hub -- see docstring
        for _, row in grp.sort_values('actual_pickup').iterrows():
            ev = score_real_leg(data, row, prev_dest)
            revenue += ev['order_revenue']
            dh_cost += ev['deadhead_cost']
            dh_miles += ev['deadhead_miles']
            prev_dest = row.dest_location_id
        home_hub = driver_hub_loc.get(driver_id)
        if prev_dest is not None and home_hub is not None:
            eod_miles, _eod_hours = get_route(data, prev_dest, home_hub)
            dh_miles += eod_miles
            dh_cost += eod_miles * ASSUMED_OPERATING_COST_PER_MILE
    return revenue, dh_cost, dh_miles


def _build_ai_orders(data, day_dispatched: pd.DataFrame, day_undispatched: pd.DataFrame) -> list[Order]:
    """The solver's input is the FULL real order book for the day -- both what REAL actually
    served and what REAL left on the books -- since the solver has to decide that itself, same as
    the real dispatcher did."""
    orders = []
    for _, row in day_dispatched.iterrows():
        loaded_miles, loaded_hours = get_route(data, row.origin_location_id, row.dest_location_id)
        orders.append(Order(
            order_id=str(row.bill_number), pickup_city='', dest_city='',
            pickup_location_id=row.origin_location_id, dest_location_id=row.dest_location_id,
            weight_lbs=float(row.weight_lbs), pallets=int(row.pallets), load_type=row.load_type,
            pickup_at=row.actual_pickup, rate=loaded_miles * linehaul_rate_per_mile(loaded_miles),
            loaded_miles=loaded_miles, loaded_hours=loaded_hours,
        ))
    for _, row in day_undispatched.iterrows():
        loaded_miles, loaded_hours = get_route(data, row.origin_location_id, row.dest_location_id)
        pickup_at = row.created_time + timedelta(hours=UNDISPATCHED_LEAD_HOURS)
        orders.append(Order(
            order_id=str(row.bill_number), pickup_city='', dest_city='',
            pickup_location_id=row.origin_location_id, dest_location_id=row.dest_location_id,
            weight_lbs=float(row.weight_lbs), pallets=int(row.pallets), load_type=row.load_type,
            pickup_at=pickup_at, rate=loaded_miles * linehaul_rate_per_mile(loaded_miles),
            loaded_miles=loaded_miles, loaded_hours=loaded_hours,
        ))
    return orders


def _solve(all_trucks_by_driver: dict[int, Truck], driver_ids: list[int], driver_hubs: dict[int, str],
           orders: list[Order], day: date_type, sim_data, tracker: HosTracker) -> SolverResult:
    trucks = [all_trucks_by_driver[d] for d in driver_ids]
    drivers = []
    for d in driver_ids:
        driving, duty, cycle1, cycle2 = tracker.remaining_for(d, day)
        drivers.append(Driver(d, driver_hubs[d], driving, duty, cycle1, cycle2))
    return build_model(trucks, orders, drivers, day, sim_data=sim_data, num_search_workers=NUM_SEARCH_WORKERS)


def _record_hours_used(tracker: HosTracker, result: SolverResult, orders: list[Order], driver_ids: list[int],
                        day: date_type, sim_data) -> None:
    """Every driver in the pool gets a record for THIS day -- 0 if they got no orders (a real
    rest day, full cycle recovery), or the real on-duty hours (loaded + dwell, same convention
    dispatch_solver.py's own HOS constraint uses) for whatever they were actually assigned.
    Recording 0 explicitly (not just leaving it unset) matters -- `HosTracker.remaining_for()`'s
    rolling window needs to know this driver existed-and-rested that day, not merely that no data
    is available."""
    dwell_hours = dwell_hours_per_order(sim_data)  # real calibrated figure, same one build_model() used
    order_hours = {o.order_id: o.loaded_hours for o in orders}
    used_by_driver = {d: 0.0 for d in driver_ids}
    for a in result.assignments if result.feasible else []:
        if a.driver_id is None:
            continue
        used_by_driver[a.driver_id] = sum(order_hours[oid] + dwell_hours for oid in a.order_ids)
    for d, hours in used_by_driver.items():
        tracker.record(d, day, hours)


def run_backtest() -> pd.DataFrame:
    dispatched_df = load_real_dispatched_orders()
    undispatched_df = load_undispatched_orders()
    dispatched_df['day'] = pd.to_datetime(dispatched_df['actual_pickup']).dt.date
    undispatched_df['day'] = pd.to_datetime(undispatched_df['created_time']).dt.date

    all_driver_ids, driver_hubs, trucks_list, driver_hub_loc = _build_south_ontario_roster()
    truck_by_driver = dict(zip(all_driver_ids, trucks_list))
    data = load_sim_data()

    days = sorted(set(dispatched_df['day']) | set(undispatched_df['day']))
    print(f"{len(days)} real calendar days found ({days[0]} to {days[-1]})")
    print("HOS: every driver starts at the full legal limit on their first day here, then "
          "carries forward whatever THIS solver actually assigned them, day to day (real user "
          "ask) -- Mode A and Mode B track separately since they make different choices.")

    # Sequential over days -- HOS now genuinely depends on what got assigned the day before, so
    # (unlike the roster/data loading) this can't be split across independent parallel days
    # anymore. The two MODES within one day are still independent of each other, so they run
    # concurrently on threads (CP-SAT's C++ solve releases the GIL -- see NUM_SEARCH_WORKERS'
    # own comment for why this, not GPU, is the real lever).
    tracker_a, tracker_b = HosTracker(), HosTracker()
    results: list[DayResult] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for day in days:
            day_dispatched = dispatched_df[dispatched_df['day'] == day]
            day_undispatched = undispatched_df[undispatched_df['day'] == day]
            n_orders = len(day_dispatched) + len(day_undispatched)
            if n_orders < MIN_ORDERS_PER_DAY:
                continue  # real day skipped for volume -- both trackers implicitly treat it as a full rest day (no record() call, so it drops out of the rolling window)

            # REAL's own reported numbers must come from the exact same driver pool AI is
            # restricted to ("can't give 35 for AI and 42 in real and call it fair" -- the real
            # fix for that was re-homing all 42 to the 2 real hubs, not dropping any of them, so
            # this filter is now a no-op in practice -- kept as a defensive guarantee, not a
            # dead check, since it's cheap and the invariant matters more than the line count).
            day_dispatched_pool = day_dispatched[day_dispatched['driver_id'].isin(all_driver_ids)]
            n_excluded_from_real = len(day_dispatched) - len(day_dispatched_pool)

            real_revenue, real_dh_cost, real_dh_miles = _score_real_day(data, day_dispatched_pool, driver_hub_loc)
            real_net = real_revenue - real_dh_cost
            real_served = len(day_dispatched_pool)
            real_missed = len(day_undispatched) + n_excluded_from_real
            orders = _build_ai_orders(data, day_dispatched, day_undispatched)

            mode_a_drivers = sorted(day_dispatched_pool['driver_id'].unique().tolist())
            fut_a = pool.submit(_solve, truck_by_driver, mode_a_drivers, driver_hubs, orders, day, data, tracker_a)
            fut_b = pool.submit(_solve, truck_by_driver, all_driver_ids, driver_hubs, orders, day, data, tracker_b)
            result_a, result_b = fut_a.result(), fut_b.result()

            _record_hours_used(tracker_a, result_a, orders, mode_a_drivers, day, data)
            _record_hours_used(tracker_b, result_b, orders, all_driver_ids, day, data)

            results.append(DayResult(
                day=str(day), n_orders=n_orders,
                real_dispatched=real_served, real_missed=real_missed,
                real_revenue=round(real_revenue, 2), real_deadhead_cost=round(real_dh_cost, 2),
                real_deadhead_miles=round(real_dh_miles, 1), real_net=round(real_net, 2),
                a_assigned=result_a.num_assigned_orders, a_unassigned=result_a.num_unassigned_orders,
                a_net_revenue=result_a.total_net_revenue, a_deadhead_miles=result_a.deadhead_miles_total,
                b_assigned=result_b.num_assigned_orders, b_unassigned=result_b.num_unassigned_orders,
                b_net_revenue=result_b.total_net_revenue, b_deadhead_miles=result_b.deadhead_miles_total,
            ))
            print(f"{day}: REAL {real_served:>2}/{n_orders:<3} (${real_net:>7,.0f}, {real_dh_miles:>5,.0f}mi) | "
                  f"A[{len(mode_a_drivers):>2} drv] {result_a.num_assigned_orders:>2}/{n_orders:<3} "
                  f"(${result_a.total_net_revenue:>7,.0f}, {result_a.deadhead_miles_total:>5,.0f}mi)"
                  f"{' [timeout]' if result_a.has_timeout else ''} | "
                  f"B[{len(all_driver_ids)} drv] {result_b.num_assigned_orders:>2}/{n_orders:<3} "
                  f"(${result_b.total_net_revenue:>7,.0f}, {result_b.deadhead_miles_total:>5,.0f}mi)"
                  f"{' [timeout]' if result_b.has_timeout else ''}", flush=True)

    df = pd.DataFrame([asdict(r) for r in results])
    os.makedirs(OUT_DIR, exist_ok=True)
    out_csv = f'{OUT_DIR}/daily_results.csv'
    df.to_csv(out_csv, index=False)
    print(f"\nWrote {len(df)} days to {out_csv}")
    _print_summary(df)
    return df


def _print_summary(df: pd.DataFrame) -> None:
    if df.empty:
        print("No days met MIN_ORDERS_PER_DAY -- nothing to summarize.")
        return
    n_days = len(df)
    real_total = df['real_dispatched'].sum() + df['real_missed'].sum()
    a_total = df['a_assigned'].sum() + df['a_unassigned'].sum()
    b_total = df['b_assigned'].sum() + df['b_unassigned'].sum()
    print(f"\n=== {n_days}-day summary (REAL vs. AI/CP-SAT, same real order books) ===")
    print(f"{'':32s} {'REAL':>14s} {'A (as-it-was)':>14s} {'B (period pool)':>16s}")
    print(f"{'orders served':32s} {df['real_dispatched'].sum():>14,} {df['a_assigned'].sum():>14,} {df['b_assigned'].sum():>16,}")
    print(f"{'orders missed':32s} {df['real_missed'].sum():>14,} {df['a_unassigned'].sum():>14,} {df['b_unassigned'].sum():>16,}")
    print(f"{'service rate':32s} {df['real_dispatched'].sum()/real_total:>13.1%} "
          f"{df['a_assigned'].sum()/a_total:>13.1%} {df['b_assigned'].sum()/b_total:>15.1%}")
    print(f"{'total net revenue (CAD)':32s} {df['real_net'].sum():>14,.0f} {df['a_net_revenue'].sum():>14,.0f} {df['b_net_revenue'].sum():>16,.0f}")
    print(f"{'total deadhead miles':32s} {df['real_deadhead_miles'].sum():>14,.0f} {df['a_deadhead_miles'].sum():>14,.0f} {df['b_deadhead_miles'].sum():>16,.0f}")
    print(f"{'avg net revenue / day (CAD)':32s} {df['real_net'].mean():>14,.0f} {df['a_net_revenue'].mean():>14,.0f} {df['b_net_revenue'].mean():>16,.0f}")


if __name__ == "__main__":
    run_backtest()
