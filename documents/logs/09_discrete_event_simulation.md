# Epsilon-Greedy Policy and the Discrete-Event Simulation Loop

**What:** `sim/engine/policy.py` (the epsilon-greedy assignment decision) and
`sim/engine/run_sim.py` (the actual discrete-event loop) — the piece that finally *runs* every
component built so far (`state.py`, `hos.py`, `maintenance.py`, `reward.py`, and now
`calibration.*`) against real calibrated distributions instead of stubs. First hand-validated run:
**101 completed trips, 0 unassigned, over a 1-week (168h) simulated horizon**, 131 real drivers ×
131 real trucks.

## `policy.py` — self-contained, ready for a real value function later

`choose_assignment()` filters to HOS-feasible candidates first (never scores an illegal
assignment), then either explores (uniform-random feasible candidate) or exploits (highest
`immediate_reward + gamma * V(s')`). No trained value function exists yet (Segment 4, not built):
`value_fn` defaults to a stub returning `0.0`, so exploitation is pure immediate-reward-greedy for
now. Swapping in a real `V(s')` later needs no change to this file — only a different `value_fn`
passed in by the caller. 5 tests (`test_policy.py`), including one that confirms an overdue truck
measurably loses to an otherwise-identical fresh one purely from the maintenance penalty.

## `run_sim.py` — what's real, what's synthesized, stated the same way as everywhere else

**Real / calibrated**: order arrival timing (`calibration.order_arrival_rate`, a genuine
hour×day-of-week Poisson process), which lane an order lands on (`lane_frequency`), route
distance/duration (`lane_routes`, real OSRM), the macro branch priors and dwell-time shape
(`TRANSITION_PRIORS`, `dwell_time_dist`), each generated order's actual weight/pallets/load_type
(**bootstrap-resampled from real `ground_truth.historical_orders` rows** — not an assumed
distribution shape), HOS limits (real regulation), fleet size (131/131, from `ground_truth`).

**Synthesized, labeled inline**: a "rig" is a FIXED (driver, truck) pairing for the whole run
(only 18/131 real drivers have a known `DEFAULT_PUNIT` — 1:1 pairing is a simplification, not an
enforced constraint the way HOS is); each driver's starting weekly-cycle usage (jittered around
the calibrated `hos_remaining_at_completion` median — a realistic *starting shape*, not a claim
about any specific driver's real history); the haversine + 1.3x-detour-factor + 80km/h fallback
used only when a location pair isn't in the OSRM cache (the cache is hub-anchored + real lanes
only, per file 08 — most non-hub-to-non-hub pairs fall back to this).

## Two real bugs found running the first validation pass

1. **`Decimal` vs `float`**: every numeric value read back from Postgres via psycopg2 comes back
   as `decimal.Decimal`, not `float`. `random.expovariate()`/`triangular()` and plain arithmetic
   elsewhere in the module don't mix `float` and `Decimal` — raised `TypeError` the first time an
   hour×day-of-week cell with a real (non-default) arrival rate got drawn. Fixed by casting every
   value to `float` immediately in `load_sim_data()`.
2. **Drivers never got a second HOS reset — the real bug worth remembering**: the sim only ever
   logged ONE reset, at simulation start. Every driver became permanently HOS-infeasible ~16
   hours into simulated time and stayed that way for the rest of the run (confirmed directly: a
   168-hour run produced 95 unassigned orders and **zero** completed trips before this fix). Real
   HOS resets require a genuine 10+ hour off-duty break — which a rig sitting idle between
   assignments legitimately *is*, but nothing in the design was logging that idle time as
   off-duty. Fixed with `apply_idle_reset()`: whenever a rig is evaluated as a candidate, the gap
   since its last logged interval is checked, and a real ≥10h idle gap is logged as an `OFF_DUTY`
   interval — a genuine reset, not a synthetic shortcut, using the exact same
   `HOSLog._last_qualifying_reset_end()` logic every other reset already goes through. Must run
   for *every* candidate before scoring, not only the one eventually chosen, or the feasibility
   check itself would score off a stale HOS state. After the fix: **101/101 orders assigned, 0
   unassigned**, over the same 168-hour run.

Also fixed for correctness (not bugs, but real gaps caught building this): a `uuid.UUID` sim/order/
trip ID couldn't be inserted until `psycopg2.extras.register_uuid()` was called (now in
`sim/db.py`, once, for every caller); `sim.trip_events`' `event_type` CHECK constraint predated
`BREAKDOWN` (added to `state.py` in the reward/risk-factors segment) — `sim/sql/012_add_breakdown_status.sql`
fixes it; and the rig-availability event was originally scheduled at the trip's `COMPLETE`
timestamp rather than after any post-completion deadhead leg finished, which would have let a rig
get double-booked while still empty-driving back toward a hub — fixed by scheduling `TRIP_COMPLETE`
at the true end of all of the rig's motion for that trip.

## The reward finding: average net reward is negative, and that's a real result

**Avg over 101 trips: order_revenue ≈ deadhead_cost ≈ opportunity_cost_penalty ≈ 92-94 CAD each —
revenue barely covers ONE of the two cost terms.** Not a miscalibrated constant: average
pre-pickup deadhead came out to ~52 miles against an average loaded leg of only ~29 miles, because
~80% of the time a rig's resting position after a delivery is wherever that real historical drop
location happened to be (only ~20% of trips trigger the "real deadhead back toward a hub" branch),
scattering the fleet across a ~150km-diameter region with no mechanism yet to prefer being well
positioned for *whatever comes next*. A purely-greedy, no-value-function policy (`value_fn` stub =
0) has no way to see that consequence — it only ever minimizes THIS order's own deadhead. This is
exactly the gap Segment 4's trained value function (not yet built) exists to close, and a good
sign the simulation is measuring something real rather than trivially always-positive. Hand-traced
full event histories (`--trace N`) confirm every mechanic individually: legal status sequences, no
double-booking, deadhead/dwell/post-delivery-deadhead computing correctly, `loaded` flag now set
correctly on the freight-carrying leg (PICKD through DOCKED-at-delivery) for a readable trace —
purely cosmetic, doesn't change any reward number (confirmed: identical totals before/after, same
seed).

## Where the exhaust goes

One full run (`--save`) persisted to LOCAL Postgres (`sim.runs`/`orders`/`assignments`/
`trip_events`) in three bulk `execute_values` calls, matching the plan's "in-memory accumulation +
bulk insert per run, not per-event network round-trips" performance note. `sim.position_ticks`
(GPS breadcrumb interpolation for the live map) is deliberately not populated yet — a
dashboard-facing concern, not needed for training-signal correctness; a natural next addition once
the dashboard's map is being built.

All 36 tests pass (`pytest sim/tests/`): the prior 31 plus 5 new for `policy.py`.
