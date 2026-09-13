"""OR-Tools CP-SAT solver for optimal truck x order x driver assignment on ONE dispatch day.

Real hackathon-lead pivot to a fully-known-in-advance day-ahead batch problem (see
research/dispatch_ai_optimization_plan.md for the full research + design writeup this file
implements) -- a Generalized Assignment Problem WITH SEQUENCING: trucks<->orders<->drivers is the
matching layer, and a same-truck order can now legitimately follow another same-truck order
(see "Sequencing" section below) rather than every order being priced as if it were that truck's
only stop of the day. Solved once per "AI Assign" click, not a live continuously-re-optimizing
system. Reuses sim/engine/reward.py's real, already-tuned economics (linehaul_rate_per_mile,
ASSUMED_OPERATING_COST_PER_MILE, the lateness-penalty curve, HOSState's hard-feasibility check) as
the objective's real dollar terms -- the CP-SAT model decides WHICH structure of assignments to
pick, it doesn't reinvent how good any one assignment is. Deliberately NOT reused: the RL/ADP-era
reward-shaping terms (`home_progress_bonus`, `cycle_end_stranding_penalty` -- potential-based
shaping only makes sense for a sequential policy learning from repeated trials; a one-shot
deterministic solve has no policy to shape) and `maintenance_risk_penalty` (an entirely
SYNTHESIZED breakdown-risk signal with no real data behind it -- real user call: it doesn't belong
diluting a clean, explainable revenue number). `compute_reward()` is still called for the parts
that DO apply (order_revenue, deadhead_cost, hos_stranding_risk_penalty), just with a fresh
default `TruckMaintenanceState` (0 risk) and no home/cycle args passed, rather than duplicating
that formula here.

## Sequencing -- why this exists, real bug it fixes

Backtesting (sim/backtest/solver_backtest.py) surfaced two symptoms that traced to the SAME root
cause: (1) Mode B (full 42-driver pool) racked up far MORE total deadhead miles than REAL despite
serving more orders (confirmed directly: on 2026-07-14, 38 of 42 available trucks got activated
and 25 of those carried only ONE order each -- the solver was spreading work across many
single-order trucks instead of chaining), and (2) Mode A (REAL's own same-day crew) served FEWER
orders than REAL managed with that identical crew (66.5% vs REAL's 92.8%). Root cause, found by
directly comparing what the old objective BELIEVED a 2-order truck-day cost vs. the true route: on
that same day, one truck's actual 2nd order was priced by the old model at 113.8mi of phantom
deadhead (hub -> its pickup, as if the truck's first order never happened) when the TRUE cost of
going there directly from the first order's dropoff was only 38.8mi -- a 75mi overcount, repeated
(to varying degrees) on every multi-order truck-day. Overpricing a 2nd order made stacking look
worse than reality, so with idle trucks available (Mode B) the solver spread thin instead of
chaining (genuinely inflating real total deadhead once realized), and with a FIXED small crew
(Mode A, no spare trucks to spread into) the same phantom cost made a legitimately profitable 2nd
order look unprofitable, so it just went unassigned.

The fix: `Next[t, i, j]` boolean variables let the model decide, for two orders both riding truck
t, whether j immediately follows i (i's dropoff -> j's pickup, not hub -> j's pickup). This is
NOT a full VRP/TSP formulation (no AddCircuit, no continuous time variables) -- `Next` pairs are
only ever created for two orders in real chronological order (`orders[i].pickup_at <
orders[j].pickup_at`, ties broken by order_id), which makes the successor graph a DAG by
construction: no subtour-elimination machinery is needed the way it would be for a general
routing problem, because time itself can't loop back. Each order gets `<= 1` predecessor and
`<= 1` successor (per truck), so a truck's assigned orders form one or more simple chains, never a
cycle. The per-(truck, order) deadhead/lateness cost is written so that CHOOSING a chain reduces
to picking the cheaper of "start from hub" vs. "continue from the previous dropoff" for every
order -- see `build_and_solve()`'s objective-construction loop for the exact substitution.

Chain feasibility is a HARD cutoff (`MAX_CHAIN_LATENESS_HOURS`), not a soft penalty like the
first-leg lateness: a `Next[t, i, j]` link is never even created if the truck couldn't physically
reach j's pickup within that grace window after dropping off i, so the "never lose an order"
guarantee can't force a nonsensical wildly-late chain into existence -- a genuinely late 2nd
order just has to ride a DIFFERENT truck (or go unassigned, if no truck fits it at all).

Per-order capacity is unchanged (still hard-filtered before the model ever sees an incompatible
(truck, order) pair); the OLD "aggregate weight/pallets across the truck's whole day" constraint
is REMOVED -- it was only ever a stand-in for "this truck can't carry more than it can carry,"
correct only by accident when trucks got a single order. Real trucks carry loads SEQUENTIALLY
(pick up, drop off, pick up again), so what actually matters is each individual order fitting the
truck, which the compatibility filter already guarantees.

HOS driving/duty hours now also count the REAL deadhead (hub-to-first-pickup or chained,
end-of-day return) using the identical substitution as the cost terms, not just each order's own
loaded hours -- closing a second real gap (a driver's clock was previously only charged for time
actually loaded, undercounting how much of their legal day a truck's real routing consumes).

Not yet wired to an API endpoint or a frontend button -- see that plan doc's "Integration plan"
section for the deliberately-held-for-review next step. Run standalone:
    python -m sim.dispatch_solver [YYYY-MM-DD]   # defaults to tomorrow, generating the day if needed
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from ortools.sat.python import cp_model

from sim.config import ASSUMED_OPERATING_COST_PER_MILE
from sim.db import cursor
from sim.engine.hos import HOSState
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.reward import RewardBreakdown, _lateness_penalty_from_hours_late, compute_reward
from sim.engine.run_sim import get_route, load_sim_data

# CP-SAT constraints/objective coefficients must be integers -- every dollar figure here is
# scaled to CENTS and rounded before being handed to the model, then divided back down for
# reporting. Not a workaround for a bug -- this is CP-SAT's documented native representation.
SCALE = 100

DAY_START_HOUR = 6  # SYNTHESIZED assumption: a truck realistically leaves its home hub around 06:00
# local on a normal operating day -- used only to estimate whether a truck can reach an order's
# pickup ON TIME from its home hub. Same role ASSUMED_PROMISE_BUFFER_HOURS plays elsewhere in this
# project: a labeled, reasonable business assumption, not something derived from data.
HOS_TIE_BREAK_WEIGHT = 0.10  # CAD-equivalent per hour of a driver's OWN remaining HOS margin --
# deliberately tiny relative to real order revenue (hundreds of CAD) so this only breaks ties
# between otherwise-equal placements (prefer the better-rested driver), never overrides economics.
SOLVER_TIME_LIMIT_SECONDS = 60.0  # Real user ask: 30s was an arbitrary starting choice, not a real
# ceiling -- checked directly, nothing else in the stack times out first (no client-side fetch
# timeout, no uvicorn request timeout), so this is a free lever. Doubled it -- checked directly on
# a real 40-order board that 30s -> FEASIBLE (not OPTIMAL, a real gap: repeated solves on the exact
# same input varied 28-31 assigned orders) and that 120s barely moved the needle on THAT board
# (same order count, +$33 revenue) -- the search plateaus rather than steadily improving, so there's
# no strong case for going much higher than this without real solver-tuning work (warm-starting,
# search strategy) to actually use the extra time well. 60s is a reasonable middle ground: still a
# live "AI Assign" click, not a multi-minute wait, while giving the search meaningfully more room
# than the original 30s on harder/larger boards than the one this was checked against.

MAX_CHAIN_GAP_HOURS = 6.0  # a same-truck "next order" candidate is only even considered within
# this many hours of the first order's pickup -- purely a model-size/realism bound (matches the
# real elapsed-window single-shift ceiling this project already references elsewhere), NOT the
# feasibility check itself (that's MAX_CHAIN_LATENESS_HOURS below); a big gap is fine to consider
# (the truck can legitimately idle), an implausibly cross-shift one is pruned before the solver
# ever sees it.
MAX_CHAIN_LATENESS_HOURS = 0.5  # HARD cutoff, not a soft penalty: if a truck genuinely could not
# reach order j's pickup within this many hours of its scheduled time after dropping off order i,
# that Next[t,i,j] link is never created at all. Real bug found directly, checked against a live
# board (truck B4800 chained two orders with pickup times 2 minutes apart at different locations):
# this was 2.0h, which let the "never lose an order" objective treat a truck arriving up to two
# HOURS late to a scheduled pickup as an acceptable, just-penalized outcome -- reads as flatly
# impossible on a real dispatch board, not a minor scheduling compromise. 0.5h is a real-world-
# sized appointment grace window (route-estimate slop, not "the truck can just be late"), and it's
# a genuinely HARD cutoff -- unlike the lateness DOLLAR terms in the objective (soft-penalized
# guesses that make a slightly-late chain cost more without forbidding it), a blown dock
# appointment by many hours isn't "late," it needs a DIFFERENT truck, and this cutoff is what
# keeps the "never lose an order" mega-penalty (see build_and_solve()) from being able to force a
# nonsensical, wildly-late chain into existence just to avoid leaving that order unassigned.
MAX_FIRST_LEG_LATENESS_HOURS = 0.5  # Same real bug, same fix, for a truck's FIRST order of the
# day: hub-departure (DAY_START_HOUR below) -> that order's pickup used to have NO hard cutoff at
# all, only the same soft dollar penalty -- an order the truck could genuinely never reach from its
# hub in time was still a valid (t, o) pair the solver could pick if the economics favored it. Less
# likely to surface visibly (DAY_START_HOUR=6 is early relative to most real pickup times), but the
# same class of bug, not a hypothetical -- closed on the same footing as the chain cutoff above.


# --------------------------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------------------------

@dataclass
class Truck:
    truck_number: str
    truck_type: str
    capacity_lbs: float
    capacity_pallets: int
    hub: str
    hub_location_id: int


@dataclass
class Order:
    order_id: str
    pickup_city: str
    dest_city: str
    pickup_location_id: int
    dest_location_id: int
    weight_lbs: float
    pallets: int
    load_type: str
    pickup_at: datetime
    rate: float
    loaded_miles: float
    loaded_hours: float


@dataclass
class Driver:
    driver_id: int
    hub: str
    hos_driving_remaining: float
    hos_duty_remaining: float
    hos_cycle1_remaining: float
    hos_cycle2_remaining: float


@dataclass
class Assignment:
    truck_number: str
    driver_id: int | None
    order_ids: list[str]


@dataclass
class SolverResult:
    feasible: bool
    assignments: list[Assignment]
    total_net_revenue: float
    num_assigned_orders: int
    num_unassigned_orders: int
    deadhead_miles_total: float
    lateness_penalty_total: float
    has_timeout: bool = False


# --------------------------------------------------------------------------------------------
# Data loading -- same tables the manual board's load_board() reads (sim/live/dispatch_board.py)
# --------------------------------------------------------------------------------------------

def _load_day_data(service_date: date) -> tuple[list[Truck], list[Order], list[Driver], dict]:
    with cursor() as cur:
        cur.execute("select id from dispatch.days where service_date = %s", (service_date,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No dispatch day for {service_date}")
        day_id = row[0]

        cur.execute("""
            select dt.truck_number, tp.truck_type, tp.capacity_lbs, tp.capacity_pallets,
                   hub.city, tp.home_hub_location_id
            from dispatch.day_trucks dt
            join calibration.truck_profile tp on tp.truck_number = dt.truck_number
            join reference.locations hub on hub.location_id = tp.home_hub_location_id
            where dt.day_id = %s and dt.available = true
        """, (day_id,))
        trucks = [Truck(r[0], r[1], float(r[2]), int(r[3]), r[4], r[5]) for r in cur.fetchall()]

        cur.execute("""
            select o.id, ol.city, dl.city, o.pickup_location_id, o.dest_location_id,
                   o.weight_lbs, o.pallets, o.load_type, o.pickup_at, o.rate, o.loaded_miles, o.loaded_hours
            from dispatch.day_orders o
            join reference.locations ol on ol.location_id = o.pickup_location_id
            join reference.locations dl on dl.location_id = o.dest_location_id
            where o.day_id = %s
            order by o.pickup_at
        """, (day_id,))
        orders = [
            Order(str(r[0]), r[1], r[2], r[3], r[4], float(r[5]), int(r[6]), r[7], r[8],
                  float(r[9]), float(r[10]), float(r[11]))
            for r in cur.fetchall()
        ]

        cur.execute("""
            select dd.driver_id, hub.city, dd.hos_driving_hours_remaining, dd.hos_duty_hours_remaining,
                   dd.hos_cycle1_hours_remaining, dd.hos_cycle2_hours_remaining
            from dispatch.day_drivers dd
            join calibration.driver_home_hub dhh on dhh.driver_id = dd.driver_id
            join reference.locations hub on hub.location_id = dhh.hub_location_id
            where dd.day_id = %s and dd.available = true
        """, (day_id,))
        drivers = [Driver(r[0], r[1], float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in cur.fetchall()]

        cur.execute("select truck_number, driver_id, order_ids from dispatch.assignments where day_id = %s", (day_id,))
        current_assignments = {r[0]: {'driver_id': r[1], 'order_ids': [str(x) for x in r[2]]} for r in cur.fetchall()}

    return trucks, orders, drivers, current_assignments


# --------------------------------------------------------------------------------------------
# Per-leg cost/lateness helpers -- driver-independent economics, computed once, feed the
# objective's per-(truck, order) and per-(order, order) coefficients. See module docstring's
# "Sequencing" section for how these combine into a chain-aware objective.
# --------------------------------------------------------------------------------------------

def dwell_hours_per_order(sim_data) -> float:
    """Real CALIBRATED dwell (calibration.dwell_time_dist, computed from actual historical
    timestamps -- same table sim/live/score_quote.py, sim/live/telemetry_simulator.py,
    sim/engine/run_sim.py and sim/backtest/real_data_replay.py all already read this from), not a
    guess. Median pickup dwell (~36 min) + median delivery dwell (~25.5 min) = the total real
    non-driving time one order adds to a truck's day -- real user correction: this used to be a
    flat 1.5h constant found nowhere in the data; checked what would be the right number to use
    instead (both real project data AND external industry benchmarks -- FreightWaves SONAR
    ~119min/stop, DOT ~2.5h/stop average wait, ATRI's own definition of "detention" as dwell
    BEYOND 2h -- all corroborate this project's own already-sourced DETENTION_FREE_HOURS=2h
    contractual assumption in sim/config.py, but this project's REAL calibrated per-fleet dwell
    (~1h combined median) is the more specific, more authoritative number and is used here instead
    of either the flat guess or the generic industry figure).
    """
    return (sim_data.dwell_minutes['pickup'][1] + sim_data.dwell_minutes['delivery'][1]) / 60


def _first_leg_hours_late(order: Order, deadhead_hours: float, service_date: date) -> float:
    """How late (or, if negative, how early) a truck would reach `order`'s pickup departing fresh
    from its home hub at DAY_START_HOUR -- accurate for whichever order actually ends up FIRST on a
    truck's day. Raw hours, not yet a penalty -- see MAX_FIRST_LEG_LATENESS_HOURS's own comment for
    why this needs a hard cutoff at the call site before it ever becomes a soft dollar term."""
    day_start = datetime.combine(service_date, time(DAY_START_HOUR, 0), tzinfo=timezone.utc)
    projected_arrival = day_start + timedelta(hours=deadhead_hours)
    return (projected_arrival - order.pickup_at).total_seconds() / 3600


def _chain_hours_late(prev_order: Order, chain_hours: float, order: Order, dwell_hours: float) -> float:
    """How late (or, if negative, how early) a truck would reach `order`'s pickup if it just came
    from `prev_order`'s dropoff -- prev_order's own pickup_at + its loaded drive time + real dwell
    (loading + unloading) + the real drive time between the two locations, compared to order's own
    scheduled pickup. Same clock basis `_first_leg_lateness` uses (real timestamps, not a
    synthesized guess), just anchored to the truck's actual previous stop instead of its hub.
    """
    prev_done_at = prev_order.pickup_at + timedelta(hours=prev_order.loaded_hours + dwell_hours)
    arrival = prev_done_at + timedelta(hours=chain_hours)
    return (arrival - order.pickup_at).total_seconds() / 3600


# --------------------------------------------------------------------------------------------
# CP-SAT model
# --------------------------------------------------------------------------------------------

def build_and_solve(trucks: list[Truck], orders: list[Order], drivers: list[Driver], service_date: date, sim_data=None, num_search_workers: int = 8):
    # for get_route()'s real OSRM-cached distance/duration -- accepts a pre-loaded SimData so a
    # caller solving MANY days in a loop (sim/backtest/solver_backtest.py) doesn't pay this
    # multi-query load (locations, lane_frequency, region clusters, ...) once per day.
    if sim_data is None:
        sim_data = load_sim_data()
    dwell_hours = dwell_hours_per_order(sim_data)

    # Precompute deadhead (truck hub -> order pickup), the end-of-day return leg (order dest ->
    # truck hub), and first-leg lateness once per compatible (t, o) pair -- "compatible" already
    # excludes equipment-type and per-order capacity mismatches, so the model never even sees
    # those pairs (smaller model, not a modeling weakness -- same "filter before the solver"
    # approach the manual board's own drag-and-drop check uses).
    #
    # Real perf bug found directly (backtesting a 131-truck real roster surfaced it -- a 15-order
    # day took 5.7s to BUILD, not solve): trucks only ever sit at one of a handful of hub
    # locations, so calling get_route(truck.hub_location_id, order.pickup_location_id) once per
    # truck recomputes the SAME route dozens of times over (every truck sharing a hub repeats an
    # identical lookup). Cached here by (hub_location_id, pickup_location_id) instead.
    route_by_hub_pickup: dict[tuple[int, int], tuple[float, float]] = {}
    route_by_dest_hub: dict[tuple[int, int], tuple[float, float]] = {}

    def _hub_to_pickup(hub_location_id: int, pickup_location_id: int) -> tuple[float, float]:
        key = (hub_location_id, pickup_location_id)
        if key not in route_by_hub_pickup:
            route_by_hub_pickup[key] = get_route(sim_data, hub_location_id, pickup_location_id)
        return route_by_hub_pickup[key]

    def _dest_to_hub(dest_location_id: int, hub_location_id: int) -> tuple[float, float]:
        key = (dest_location_id, hub_location_id)
        if key not in route_by_dest_hub:
            route_by_dest_hub[key] = get_route(sim_data, dest_location_id, hub_location_id)
        return route_by_dest_hub[key]

    deadhead: dict[tuple[int, int], tuple[float, float]] = {}       # (t,o) -> (hub->pickup miles, hours)
    eod_return: dict[tuple[int, int], tuple[float, float]] = {}     # (t,o) -> (dest->hub miles, hours)
    first_lateness: dict[tuple[int, int], float] = {}               # (t,o) -> CAD penalty if o is truck t's FIRST order
    for t_idx, truck in enumerate(trucks):
        for o_idx, order in enumerate(orders):
            if truck.truck_type != order.load_type:
                continue
            if order.weight_lbs > truck.capacity_lbs or order.pallets > truck.capacity_pallets:
                continue
            hub_miles, hub_hours = _hub_to_pickup(truck.hub_location_id, order.pickup_location_id)
            # Real bug found directly: this pair used to be admitted regardless of how late a
            # 6am-departing truck would actually be to order's pickup -- only a soft dollar penalty
            # applied, no hard cutoff, unlike the chain (dropoff->next-pickup) case. Excluded here
            # entirely when genuinely unreachable, same footing as the chain cutoff above.
            raw_hours_late = _first_leg_hours_late(order, hub_hours, service_date)
            if raw_hours_late > MAX_FIRST_LEG_LATENESS_HOURS:
                continue
            eod_miles, eod_hours = _dest_to_hub(order.dest_location_id, truck.hub_location_id)
            deadhead[(t_idx, o_idx)] = (hub_miles, hub_hours)
            eod_return[(t_idx, o_idx)] = (eod_miles, eod_hours)
            first_lateness[(t_idx, o_idx)] = _lateness_penalty_from_hours_late(max(0.0, raw_hours_late))

    # Chain candidates -- truck-INDEPENDENT (the real-world drive between order i's dropoff and
    # order j's pickup doesn't depend on which truck makes it), so computed once per (i, j) order
    # pair, not once per (truck, i, j). Orders sorted by pickup time so the MAX_CHAIN_GAP_HOURS
    # prune can `break` out of the inner loop instead of scanning every later order.
    chain_miles: dict[tuple[int, int], float] = {}
    chain_hours: dict[tuple[int, int], float] = {}
    chain_late_penalty: dict[tuple[int, int], float] = {}
    order_indices_by_time = sorted(range(len(orders)), key=lambda o: (orders[o].pickup_at, orders[o].order_id))
    for a, i in enumerate(order_indices_by_time):
        oi = orders[i]
        for j in order_indices_by_time[a + 1:]:
            oj = orders[j]
            gap_hours = (oj.pickup_at - oi.pickup_at).total_seconds() / 3600
            if gap_hours > MAX_CHAIN_GAP_HOURS:
                break  # sorted by pickup_at -- every later j only has a bigger gap
            c_miles, c_hours = get_route(sim_data, oi.dest_location_id, oj.pickup_location_id)
            hours_late = _chain_hours_late(oi, c_hours, oj, dwell_hours)
            if hours_late > MAX_CHAIN_LATENESS_HOURS:
                continue  # truck genuinely can't get there in time -- no link, not even a penalized one
            chain_miles[(i, j)] = c_miles
            chain_hours[(i, j)] = c_hours
            chain_late_penalty[(i, j)] = _lateness_penalty_from_hours_late(max(0.0, hours_late))

    model = cp_model.CpModel()

    X: dict[tuple[int, int], cp_model.IntVar] = {
        (t, o): model.new_bool_var(f"X_{t}_{o}") for (t, o) in deadhead
    }
    Y: dict[tuple[int, int], cp_model.IntVar] = {
        (t, d): model.new_bool_var(f"Y_{t}_{d}")
        for t, truck in enumerate(trucks)
        for d, driver in enumerate(drivers)
        if driver.hub == truck.hub
    }
    A = [model.new_bool_var(f"A_{o}") for o in range(len(orders))]

    # Next[t,i,j]: order j immediately follows order i on truck t's route (i's dropoff -> j's
    # pickup). Only created for (t, i, j) where both (t,i) and (t,j) are themselves valid --
    # equipment/capacity-compatible -- pairs, restricted to the pruned chain candidates above.
    # Because chain candidates only ever go forward in time, this graph is a DAG by construction:
    # no subtour-elimination constraint is needed for what would otherwise be a routing problem.
    Next: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for t in range(len(trucks)):
        for (i, j) in chain_miles:
            if (t, i) in X and (t, j) in X:
                Next[(t, i, j)] = model.new_bool_var(f"Next_{t}_{i}_{j}")

    next_by_ti: dict[tuple[int, int], list[int]] = defaultdict(list)  # (t,i) -> possible successors j
    next_by_tj: dict[tuple[int, int], list[int]] = defaultdict(list)  # (t,j) -> possible predecessors i
    for (t, i, j) in Next:
        next_by_ti[(t, i)].append(j)
        next_by_tj[(t, j)].append(i)

    # Each order rides at most one truck; A[o] mirrors that sum (still boolean since the sum is <= 1).
    for o in range(len(orders)):
        pairs = [X[(t, o)] for t in range(len(trucks)) if (t, o) in X]
        model.add(sum(pairs) <= 1)
        model.add(A[o] == sum(pairs))

    # A Next link can only be "on" if both orders it connects are actually assigned to this truck,
    # and each order has at most one predecessor / at most one successor -- together with the DAG
    # property above, this forces every truck's assigned orders into one or more simple chains.
    for (t, i, j), var in Next.items():
        model.add(var <= X[(t, i)])
        model.add(var <= X[(t, j)])
    for key, succs in next_by_ti.items():
        model.add(sum(Next[(key[0], key[1], j)] for j in succs) <= 1)
    for key, preds in next_by_tj.items():
        model.add(sum(Next[(key[0], i, key[1])] for i in preds) <= 1)

    # Real bug found directly, checked against a live board: truck B4800 was assigned two orders
    # with pickup times 2 minutes apart at DIFFERENT locations -- a physically impossible schedule
    # (the SAME truck can't be in Vaughan and Brampton at once, let alone finish a delivery that
    # doesn't complete until 15:26 before its own "next" pickup at 13:54). Root cause: Next[t,i,j]
    # only ever ADDS an optional chain relationship when the (i,j) pair is a feasible candidate
    # (above) -- nothing REQUIRED that two orders riding the same truck actually be connected by
    # one. The "<=1 predecessor/successor" constraints above only bound the Next graph once a Next
    # edge is chosen; they never stopped the solver from setting X[t,i]=1 and X[t,j]=1 with NO edge
    # between them at all when (i,j) was never even a valid candidate (pruned by MAX_CHAIN_GAP_HOURS
    # or MAX_CHAIN_LATENESS_HOURS). Since chain candidates are only ever built i-before-j by pickup
    # time, an (i,j) pair missing from chain_miles means there is NO valid order for one truck to
    # do both -- j can't precede i (sorted), and i-then-j doesn't fit either. A hard mutual-
    # exclusion constraint per truck closes exactly that gap: unlike the first-leg/chain lateness
    # terms (soft, revenue-scaled penalties), this is never something the "never lose an order"
    # objective can outbid -- it's a hard constraint, the same footing as truck capacity.
    for a, i in enumerate(order_indices_by_time):
        for j in order_indices_by_time[a + 1:]:
            if (i, j) in chain_miles:
                continue  # a real Next edge CAN connect them -- already governed by the constraints above
            for t in range(len(trucks)):
                if (t, i) in X and (t, j) in X:
                    model.add(X[(t, i)] + X[(t, j)] <= 1)

    # Per-order capacity is already enforced above (an order that alone exceeds a truck's
    # weight/pallets never even got an X[(t,o)] variable) -- no aggregate "sum across the whole
    # day" constraint needed anymore: sequencing means a truck's orders don't overlap in time, so
    # nothing beyond each individual order fitting the truck is a real physical requirement.

    # One driver per truck, one truck per driver.
    for t in range(len(trucks)):
        model.add(sum(Y[(t, d)] for d in range(len(drivers)) if (t, d) in Y) <= 1)
    for d in range(len(drivers)):
        model.add(sum(Y[(t, d)] for t in range(len(trucks)) if (t, d) in Y) <= 1)

    # A loaded truck needs a driver -- a driverless truck can't actually run any of its orders.
    for t in range(len(trucks)):
        truck_orders = [o for o in range(len(orders)) if (t, o) in X]
        truck_drivers = [d for d in range(len(drivers)) if (t, d) in Y]
        if truck_orders:
            model.add(sum(X[(t, o)] for o in truck_orders) <= len(truck_orders) * sum(Y[(t, d)] for d in truck_drivers))
        # Real bug found directly (a fleet manager looking at the finalized plan): an EMPTY truck
        # (no orders at all) could still get a driver assigned. Cause: the HOS tie-break term below
        # gives a small POSITIVE reward for every Y[t,d]=1 on its own, with nothing tying it to the
        # truck actually being used -- "free" objective value, so the solver happily paired up
        # otherwise-idle drivers with otherwise-idle trucks. A truck with zero orders should never
        # get a driver: sum(Y[t,:]) is already <= 1 (one driver per truck, above), so this only
        # ever binds when truck_orders is empty, forcing that sum to exactly 0 -- non-binding
        # whenever the truck actually has work.
        model.add(sum(Y[(t, d)] for d in truck_drivers) <= sum(X[(t, o)] for o in truck_orders))

    # Objective. UNASSIGNED_PENALTY must provably dominate -- strictly more than every order's
    # revenue combined, so the solver can never come out ahead by dropping even the single
    # highest-value order (see plan doc Section 5.3 -- "never lose an order" is a guarantee here,
    # not a hopeful weight). Chaining only ever makes serving an order CHEAPER than the hub-anchored
    # default it substitutes for, so this bound (derived against the old hub-anchored-only pricing)
    # still safely dominates under the new sequencing-aware costs too.
    unassigned_penalty = round(2 * sum(o.rate for o in orders) * SCALE) if orders else 0

    objective_terms = []
    for t, truck in enumerate(trucks):
        truck_orders = [o for o in range(len(orders)) if (t, o) in X]
        if not truck_orders:
            continue
        driving_hour_terms = []
        for o in truck_orders:
            order = orders[o]
            hub_miles, hub_hours = deadhead[(t, o)]
            eod_miles, eod_hours = eod_return[(t, o)]
            preds = next_by_tj.get((t, o), [])  # orders that could immediately precede o on truck t
            succs = next_by_ti.get((t, o), [])  # orders that could immediately follow o on truck t

            # Revenue.
            objective_terms.append(round(order.rate * SCALE) * X[(t, o)])

            # Pickup-leg deadhead cost: hub->pickup by default, replaced by the (usually much
            # cheaper) prior-dropoff->pickup leg whenever a predecessor is actually chosen.
            objective_terms.append(-round(hub_miles * ASSUMED_OPERATING_COST_PER_MILE * SCALE) * X[(t, o)])
            for i in preds:
                delta_miles = chain_miles[(i, o)] - hub_miles
                objective_terms.append(-round(delta_miles * ASSUMED_OPERATING_COST_PER_MILE * SCALE) * Next[(t, i, o)])

            # End-of-day return cost: dest->hub by default, waived (this order isn't actually last)
            # whenever a successor is chosen -- the return leg belongs to whichever order ends up last.
            objective_terms.append(-round(eod_miles * ASSUMED_OPERATING_COST_PER_MILE * SCALE) * X[(t, o)])
            for j in succs:
                objective_terms.append(round(eod_miles * ASSUMED_OPERATING_COST_PER_MILE * SCALE) * Next[(t, o, j)])

            # Lateness: first-leg estimate by default, replaced by the real chain-leg lateness
            # (already hard-filtered to MAX_CHAIN_LATENESS_HOURS at candidate-creation time) if
            # this order actually follows another one on the same truck.
            first_late = first_lateness[(t, o)]
            objective_terms.append(-round(first_late * SCALE) * X[(t, o)])
            for i in preds:
                objective_terms.append(round(first_late * SCALE) * Next[(t, i, o)])
                objective_terms.append(-round(chain_late_penalty[(i, o)] * SCALE) * Next[(t, i, o)])

            # HOS hours -- same substitution, in hours instead of CAD: the truck's real driving
            # time includes whichever pickup leg (hub or chained) and EOD leg actually apply, not
            # just the order's own loaded hours (previously undercounted).
            driving_hour_terms.append(int(round(order.loaded_hours * SCALE)) * X[(t, o)])
            driving_hour_terms.append(int(round(hub_hours * SCALE)) * X[(t, o)])
            for i in preds:
                delta_hours = chain_hours[(i, o)] - hub_hours
                driving_hour_terms.append(int(round(delta_hours * SCALE)) * Next[(t, i, o)])
            driving_hour_terms.append(int(round(eod_hours * SCALE)) * X[(t, o)])
            for j in succs:
                driving_hour_terms.append(-int(round(eod_hours * SCALE)) * Next[(t, o, j)])

        # HOS budget: IF driver d drives truck t, THEN the hours actually needed for this truck's
        # whole real route (every leg above, plus per-order dwell) must fit their real remaining
        # budget (every clock HOSState.can_perform() checks, minus the 16h elapsed-window clock --
        # neither dispatch.day_drivers nor live.driver_status track that 5th clock anywhere in this
        # codebase yet; not introduced here either, see the plan doc).
        driving_needed = sum(driving_hour_terms)
        n_assigned = sum(X[(t, o)] for o in truck_orders)
        duty_needed = driving_needed + int(round(dwell_hours * SCALE)) * n_assigned
        for d, driver in enumerate(drivers):
            if (t, d) not in Y:
                continue
            model.add(driving_needed <= int(round(driver.hos_driving_remaining * SCALE))).only_enforce_if(Y[(t, d)])
            model.add(duty_needed <= int(round(driver.hos_duty_remaining * SCALE))).only_enforce_if(Y[(t, d)])
            model.add(duty_needed <= int(round(driver.hos_cycle1_remaining * SCALE))).only_enforce_if(Y[(t, d)])
            model.add(duty_needed <= int(round(driver.hos_cycle2_remaining * SCALE))).only_enforce_if(Y[(t, d)])

    for (t, d), driver in ((k, drivers[k[1]]) for k in Y):
        margin = min(driver.hos_driving_remaining, driver.hos_duty_remaining, driver.hos_cycle1_remaining, driver.hos_cycle2_remaining)
        objective_terms.append(round(HOS_TIE_BREAK_WEIGHT * margin * SCALE) * Y[(t, d)])
    for o in range(len(orders)):
        objective_terms.append(-unassigned_penalty * (1 - A[o]))

    model.maximize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_SECONDS
    # CP-SAT parallelizes across CPU threads internally (portfolio search, not GPU-capable --
    # see sim/backtest/solver_backtest.py's own note on why a backtest driving many INDEPENDENT
    # per-day solves concurrently needs this turned DOWN per solve, so N parallel processes x K
    # threads each doesn't oversubscribe the machine's real core count).
    solver.parameters.num_search_workers = num_search_workers
    status = solver.solve(model)

    return status, solver, X, Y, Next, deadhead, eod_return, first_lateness, chain_late_penalty


def build_model(trucks: list[Truck], orders: list[Order], drivers: list[Driver], service_date: date, sim_data=None, num_search_workers: int = 8) -> SolverResult:
    if not trucks or not drivers:
        return SolverResult(False, [], 0.0, 0, len(orders), 0.0, 0.0)

    if sim_data is None:
        sim_data = load_sim_data()
    dwell_hours = dwell_hours_per_order(sim_data)  # same real calibrated figure build_and_solve() used

    status, solver, X, Y, Next, deadhead, eod_return, first_lateness, chain_late_penalty = build_and_solve(
        trucks, orders, drivers, service_date, sim_data, num_search_workers,
    )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolverResult(False, [], 0.0, 0, len(orders), 0.0, 0.0, has_timeout=(status == cp_model.UNKNOWN))

    assignments = []
    total_net_revenue = 0.0
    num_assigned = 0
    deadhead_total = 0.0
    lateness_total = 0.0

    for t, truck in enumerate(trucks):
        # INDICES are the ground truth of what the solver actually decided -- order_id strings
        # are not unique (a real bill_number can legitimately repeat across multiple rows, e.g. a
        # split/multi-leg shipment; confirmed directly: 2026-07-13 alone has 5 bill_numbers that
        # appear 2-3 times in that day's real order book). A prior version of this reporting
        # re-derived "which orders are assigned to this truck" via `order.order_id in order_ids`
        # (a STRING membership test against the whole order list) -- that matches EVERY index
        # sharing a repeated order_id, not just the one the solver actually assigned, so a single
        # real assignment could get phantom-duplicated 2-3x into num_assigned/total_net_revenue.
        # Real bug found directly from an impossible result: >100% service rate (more orders
        # "assigned" than existed in the day's order book). Indices, not strings, from here on.
        assigned_idxs = [o for o in range(len(orders)) if (t, o) in X and solver.value(X[(t, o)])]
        order_ids = [orders[o].order_id for o in assigned_idxs]
        driver_idx = next((d for d in range(len(drivers)) if (t, d) in Y and solver.value(Y[(t, d)])), None)
        driver_id = drivers[driver_idx].driver_id if driver_idx is not None else None
        assignments.append(Assignment(truck.truck_number, driver_id, order_ids))

        if not order_ids or driver_id is None:
            continue
        driver = drivers[driver_idx]
        hos_state = HOSState(
            remaining_driving_hours=driver.hos_driving_remaining, remaining_duty_hours=driver.hos_duty_remaining,
            remaining_elapsed_window_hours=driver.hos_duty_remaining,  # documented gap -- see module docstring
            remaining_cycle1_hours=driver.hos_cycle1_remaining, remaining_cycle2_hours=driver.hos_cycle2_remaining,
        )
        # Real user correction: maintenance risk is an entirely SYNTHESIZED signal built for the
        # old RL/ADP training objective (see sim/engine/maintenance.py's own docstring -- there's
        # no real breakdown-history data behind it at all), which has no place diluting a
        # deterministic, one-shot dispatch decision's reported revenue. A freshly-constructed
        # default (0 km/days since service) makes TruckMaintenanceState.expected_breakdown_cost()
        # evaluate to exactly 0.0 below, with no live DB round-trip needed to get there.
        truck_state = TruckMaintenanceState(truck_number=truck.truck_number)

        # Reconstruct the truck's ACTUAL chosen chain(s) from the solved Next[] values -- reads
        # the same decision the objective itself optimized over, so this reporting pass can never
        # again drift out of sync with what the solver was actually maximizing (the exact bug
        # class the module docstring's "Sequencing" section describes fixing).
        succ: dict[int, int] = {}
        pred: dict[int, int] = {}
        for i in assigned_idxs:
            for j in assigned_idxs:
                if i == j:
                    continue
                key = (t, i, j)
                if key in Next and solver.value(Next[key]):
                    succ[i] = j
                    pred[j] = i
        chain_starts = [o for o in assigned_idxs if o not in pred]

        for start in chain_starts:
            prev_location_id = truck.hub_location_id
            o_idx = start
            is_first = True
            while True:
                order = orders[o_idx]
                dh_miles, dh_hours = get_route(sim_data, prev_location_id, order.pickup_location_id)
                if is_first:
                    late_penalty = first_lateness.get((t, o_idx), 0.0)
                else:
                    late_penalty = chain_late_penalty.get((pred[o_idx], o_idx), 0.0)
                breakdown = compute_reward(
                    loaded_miles=order.loaded_miles, weight_lbs=order.weight_lbs, pallets=order.pallets,
                    capacity_lbs=truck.capacity_lbs, capacity_pallets=truck.capacity_pallets,
                    pre_pickup_deadhead_miles=dh_miles, pre_pickup_deadhead_hours=dh_hours,
                    hos_state=hos_state, planned_duty_hours=order.loaded_hours + dwell_hours,
                    truck_state=truck_state, capacity_value_rate_per_lb=0.0,
                )
                total_net_revenue += breakdown.total
                deadhead_total += dh_miles
                lateness_total += late_penalty
                num_assigned += 1
                prev_location_id = order.dest_location_id
                is_first = False
                if o_idx not in succ:
                    break
                o_idx = succ[o_idx]

            # End-of-day empty return from this chain's last dropoff back to the truck's home hub
            # -- a real cost (nobody parks a $150k asset wherever the last load happened to end),
            # priced at the same real ASSUMED_OPERATING_COST_PER_MILE rate every other deadhead
            # mile in this project uses, not a new figure.
            eod_miles, _eod_hours = get_route(sim_data, prev_location_id, truck.hub_location_id)
            deadhead_total += eod_miles
            total_net_revenue -= eod_miles * ASSUMED_OPERATING_COST_PER_MILE

    return SolverResult(
        feasible=True, assignments=assignments, total_net_revenue=round(total_net_revenue, 2),
        num_assigned_orders=num_assigned, num_unassigned_orders=len(orders) - num_assigned,
        deadhead_miles_total=round(deadhead_total, 1), lateness_penalty_total=round(lateness_total, 2),
    )


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------

def ai_assign(service_date: date) -> dict:
    trucks, orders, drivers, current = _load_day_data(service_date)
    result = build_model(trucks, orders, drivers, service_date)
    return {
        'feasible': result.feasible,
        'assignments': [{'truck_number': a.truck_number, 'driver_id': a.driver_id, 'order_ids': a.order_ids} for a in result.assignments],
        'total_net_revenue': result.total_net_revenue,
        'num_assigned_orders': result.num_assigned_orders,
        'num_unassigned_orders': result.num_unassigned_orders,
        'deadhead_miles_total': result.deadhead_miles_total,
        'lateness_penalty_total': result.lateness_penalty_total,
        'has_timeout': result.has_timeout,
        'current_manual_assignment': current,
    }


if __name__ == "__main__":
    import json

    target_date = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else (datetime.now(timezone.utc).date() + timedelta(days=1))

    with cursor() as cur:
        cur.execute("select status from dispatch.days where service_date = %s", (target_date,))
        if not cur.fetchone():
            from sim.live.generate_dispatch_day import generate
            generate(target_date)

    print(f"Solving for {target_date}...")
    result = ai_assign(target_date)
    print(json.dumps({k: v for k, v in result.items() if k != 'current_manual_assignment'}, indent=2, default=str))
    print(f"Total net revenue: ${result['total_net_revenue']:,.2f}")
    print(f"Assigned orders: {result['num_assigned_orders']}/{result['num_assigned_orders'] + result['num_unassigned_orders']}")
