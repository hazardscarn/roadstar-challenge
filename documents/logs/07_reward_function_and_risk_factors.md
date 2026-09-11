# Reward Function, HOS Engine, and Risk Factors

**What:** `sim/engine/{hos,maintenance,reward}.py` — the pieces that turn a `TripState` into an
actual reward number, plus two risk factors beyond deadhead: HOS stranding risk (legal-but-risky
assignments) and truck maintenance (entirely synthesized). `sim/sql/011_trip_log_and_ratings.sql`
— the post-trip audit trail feeding driver/truck ratings on the dashboard.

## Deadhead, refined: direction matters

`TripState` now distinguishes **pre-pickup** deadhead (normal — how a truck gets loaded, not
charged in the reward) from **post-delivery** deadhead (the real revenue-loss signal, per this
session's earlier corrected finding: 327 legs / 6,255 real no-revenue miles). Charged separately:
`compute_reward()` charges pre-pickup deadhead against the assignment decision that caused it;
`post_delivery_deadhead_cost()` charges post-delivery deadhead against the completed trip that
left the driver stranded — realized after the fact, not at decision time, since it belongs to
whatever happens *next*, not to the trip that just finished.

## HOS engine (`sim/engine/hos.py`) — both clocks, not just one

Tracks the daily/continuous limits (13h driving / 14h on-duty / 16h elapsed window, reset by a
qualifying 10h off-duty break) **and** the rolling 7-day (70h) / 14-day (120h) cycles
simultaneously — `HOSState.remaining_hours` is the min across all five, and
`binding_constraint` names whichever one is actually tightest. Validated directly: a driver with
a fully fresh daily reset but a busy prior week (65 of 70 cycle-hours already used) is correctly
blocked by the cycle limit even though their daily clock has full room
(`test_weekly_cycle_binds_even_with_a_fresh_daily_reset`) — the specific case this needed to get
right.

**A real bug found by its own test**: a brand-new driver with no history computed
`remaining_elapsed_window_hours = 0` (window already exhausted) instead of the full 16 hours —
`inf - 16` clamped to 0 when no prior reset was on record. Fixed: no reset on record means the
window hasn't started counting down, not that infinite time has already elapsed.

Two uses: `can_perform()` is the hard feasibility filter (removes an infeasible driver from the
candidate list before scoring, never scored at all); `stranding_risk()` is a soft 0-1 signal for
the reward — a driver can be legally cleared to start and still get stranded if a delay eats
their margin, which the hard filter alone can't capture.

## Truck maintenance (`sim/engine/maintenance.py`) — synthesized end to end, said plainly

Checked first: `Trucks` (the source Excel) has exactly one column, `TRUCK_NUMBER` — no odometer,
no service history, nothing to calibrate a breakdown model against. Built anyway, because without
some cost tied to overusing one truck, the value function has no reason to ever prefer rotating
trucks — a real dispatch consideration worth teaching it, even on an assumed signal.

- `breakdown_risk`: 0 below 70% of the service interval, ramps to 0.15 at 100%, capped at 0.30
  well past due — bounded, not catastrophic by default.
- **Fleet initialization is NOT uniformly fresh**: `initialize_fleet()` spreads every truck's
  starting odometer uniformly across `[0, 1.15x the service interval]`, so some trucks start
  already overdue from day one of the simulation — a real fleet has trucks at every point in
  their service cycle, not all freshly serviced.
- **Two different breakdown costs, two different jobs**: `expected_breakdown_cost()` (probability
  x cost) is a soft signal used when *choosing* which truck to assign. A realized breakdown is a
  separate, genuine event: `TripStatus.BREAKDOWN` is a real timed state in the trip's history
  (added specifically for this — not part of the real `LAST_FB_STATUS` vocabulary), and
  `realized_breakdown_penalty()` applies a large, one-time cost (`ASSUMED_BREAKDOWN_EVENT_PENALTY_CAD
  = 8000`, deliberately much bigger than the ~2000 expected-cost figure) only when a breakdown is
  actually sampled to occur for that specific trip — a genuine bad outcome in the training data,
  not an averaged risk estimate.

## The reward function (`sim/engine/reward.py`)

```
immediate_reward = order_revenue
                    - deadhead_cost              (pre-pickup only)
                    - opportunity_cost_penalty    (load_fill_ratio-based)
                    - hos_stranding_risk_penalty
                    - maintenance_risk_penalty     (expected-cost form)
```

Plus, realized after a trip completes and only if it actually happened:
`post_delivery_deadhead_cost()` and `realized_breakdown_penalty()`. `order_revenue` and the
operating-cost rate behind `deadhead_cost` are synthesized (`sim/config.py` —
`ASSUMED_LINEHAUL_RATE_PER_MILE`, `ASSUMED_OPERATING_COST_PER_MILE`); everything else is a real,
structural cost computed from the trip's actual tracked state. Same function, sim and live: it
takes state snapshots, not a sim-specific object, so the live scoring path calls it identically.

## `live.trip_log` + rating views (`sim/sql/011_trip_log_and_ratings.sql`)

One row per completed trip (both driver and truck side), applied to remote Supabase and
verified. `live.driver_ratings`/`live.truck_ratings` are views computed on read from `trip_log` —
one source of truth, not a separately-maintained score that can drift from the underlying log.
Feeds the dashboard's driver-stats and truck-maintenance-warning panels from real logged outcomes
(simulated or live), not ad-hoc derivation.

All 31 tests pass (`pytest sim/tests/`), including two real bugs caught by their own tests along
the way (the elapsed-window bug above, and an earlier dwell-time span bug in `state.py` — see
file 06).
