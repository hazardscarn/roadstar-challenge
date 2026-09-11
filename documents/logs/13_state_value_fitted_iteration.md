# A Real State-Value Function via Fitted Value Iteration

**What:** `sim/training/train_state_value_function.py` + `sim/engine/value_function.py` — the
piece that actually answers the Kitchener question: "would this assign a London→Kitchener order
to a Milton→London truck because Kitchener happens to be one deadhead-mile back?" The prior
single-pass model (file 12) structurally could not do this — it scores `Q(state, this-order)`,
conditioned on the specific order, not a pure `V(position, hos, time)` evaluable at a
*hypothetical* landing spot independent of which order got a driver there.

## Why V(s) deliberately excludes truck condition

`train_value_function.py`'s Q-model includes `truck_breakdown_risk` etc. because a specific
candidate truck is part of that decision. A pure state-value function needs "how good is it to
simply BE here" — independent of which truck eventually gets paired at the next decision. Since
trucks are reassigned per-decision from a small pool (file 10), truck condition isn't a stable
property of a position the way geography/HOS/time are; projecting the *next* truck's wear forward
would mean re-deriving `TruckMaintenanceState`'s progression outside the sim engine. State:
`(driver_lat, driver_lon, driver_hos_remaining, hour_of_day, day_of_week)` — nothing else.
Truck-specific risk stays exactly where it already was, in the Q-model.

## Capturing the real next-state (previously always null)

`sim/sql/019`/`020`: `sim.assignments.next_location_id`/`next_hos_remaining`/`next_available_at`,
computed in `run_sim.py` right after each trip's HOS interval is logged (needs the *updated*
`hos_log`, so it can't happen inside `run_assignment()` itself). `extract_transitions.py` now
actually populates `training_transitions.next_driver_position`/`next_driver_hos_remaining` —
columns that existed in the schema since file 07 but were always written as `null`.

## Fitted Value Iteration

```
V_0(s)     = fit directly on realized target_value        (Monte-Carlo-style baseline)
V_{k+1}(s) = fit on (target_value + gamma * V_k(next_state))   for a few rounds
```

`next_state` is each transition's REAL recorded landing position/HOS/time, not a guess. Each
round bootstraps a bit more of the future's value backward into today's estimate — a decision
that lands a driver somewhere genuinely good gets credit now, not just for its own trip's direct
aftermath.

**Result on the full 10,000-run batch (1,077,806 transitions), 4 rounds, gamma=0.9:**

| Round | Test MAE | Test R² |
|---|---|---|
| 0 (baseline) | 156.07 | 0.0464 |
| 1 | 155.60 | 0.0574 |
| 2 | 155.96 | 0.0606 |
| 3 | 155.72 | **0.0623** |

R² improves monotonically round over round (not oscillating or diverging) — the bootstrap is
converging, not blowing up. Feature importance: `lat`/`lon` dominate overwhelmingly (696M / 171M
gain vs. `day_of_week` at 52M and `hos_remaining` at 19M) — geography is, as expected, the
primary driver of pure positioning value.

## Wiring (`sim/engine/value_function.py`) — built, smoke-tested, not yet embedded in a sim run

`make_value_fn()` returns a `value_fn(candidate, order)` closure matching `policy.py`'s expected
signature, computing:

```
E[V(s')] = (p_reload + p_dromt) * V(order's destination) + p_deadhead * V(nearest hub)
```

using the SAME `dynamic_post_completion_probs()` the simulator itself uses to decide where a
truck actually ends up — scoring and simulation agree on what "landing here" means. Landed HOS is
approximated as `remaining_hours - planned_duty_hours` (a light estimate; the real figure depends
on dwell/deadhead specifics only known once a trip actually runs).

**Honest caveat, not smoothed over**: a quick smoke test comparing a real order's destination vs.
a deliberately remote location vs. landing exactly at a hub produced a real difference (confirming
the model *does* discriminate by geography) — but the ranking wasn't the naive "closer to hub is
always better" pattern one might expect (a specific remote location scored higher than a hub in
one comparison). With raw lat/lon dominating feature importance this strongly at R²≈0.06, some of
that could be genuine local structure in the data (a specific real customer location correlating
with good consolidation opportunities) or could be noise from sparse per-location sampling (2,110
possible locations, some visited only a handful of times) — this needs validation against more
runs or a coarser geographic feature (binned region, distance-based) before being trusted for
real dispatch decisions. Flagged here rather than presented as solved.

**Not yet done**: replacing `policy.py`'s `zero_value_fn` default with this model inside an actual
simulation run (which would mean re-simulating with the trained value function influencing
decisions — genuine policy iteration, the stretch goal in `documents/diagrams/pipeline_training.jpg`,
still explicitly out of scope for this pass). What's built and validated is the model itself and a
working, tested scoring function ready to be plugged in.

## Files

- `sim/sql/019_add_next_state.sql`, `020_add_next_decision_time.sql`
- `sim/training/train_state_value_function.py` — Fitted Value Iteration
- `sim/engine/value_function.py` — `load_state_value_model()`, `make_value_fn()`
- `sim/training/state_value_function.pkl` — the trained model (gitignored data artifact, regenerate anytime)
