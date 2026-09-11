# Feature Richness, FTL/LTL Revenue, and Appointment Lateness

**What:** A large, connected pass triggered by direct questions about training-data quality — "I
don't see anything useful as a feature," "FTL orders get paid for the full truck right?", "where's
the timing feature," "add time-since-maintenance as a feature too," and "coming late than
promised should be penalized." All five landed together since they touch the same reward/feature
pipeline. Full before/after: R² went from **0.0045 → 0.1222** (feature richness alone) **→
0.0975** (adding lateness, which introduces its own hard-to-predict variance — still far above
the original baseline).

## FTL vs LTL revenue — a real gap in what was being optimized

Every order was charged the same flat per-mile rate regardless of type. Real distinction: an FTL
shipper pays for the WHOLE truck flat, no matter how full it is — no real "wasted capacity" cost
to them. An LTL shipper pays roughly proportional to space used, at a premium rate, and a SECOND
LTL pickup should add its OWN revenue (the "secondary pickup for more revenue" mechanic asked for
early in this build, never actually wired into the numbers before now).

- `order.service_type` drawn per order (`P_LTL_ORDER` = 334/(334+2677) = 11.1%, the REAL
  LTL/multi-stop share from `ground_truth.historical_legs` — not a guess).
- `compute_reward()`: FTL → flat revenue, zero opportunity-cost (nothing wasted, nothing to
  waste); LTL → revenue scales with `load_fill_ratio`, opportunity-cost penalty applies (real
  forgone consolidation revenue).
- Secondary pickup: now gated by `service_type == 'LTL'` (deterministic — real data shows
  LTL-classified trips ARE consolidated trips, 334/334), not a flat 7% coin flip. When it fires,
  the second shipment's own LTL revenue is added to the trip's realized total.
- Effect: average reward/trip went from ~-107 CAD to ~-12 CAD in the first re-run — LTL trips
  (11.4% of the batch) now average **$206.72** vs FTL's **$94.71**, correctly reflecting two
  shipments' worth of revenue on a consolidated trip.

## The value function had no signal because the simulator hid it, not because of bad hyperparameters

Trained the first model on the fixed reward and got **R² ≈ 0.0045** — essentially useless.
Diagnosed two structural causes, both simulator-design issues, not training bugs:

1. **Truck breakdown risk was invisible.** `sample_breakdown()` depends on the truck's
   maintenance state, but neither `sim.assignments` nor `training_transitions` captured it —
   up to $8,000 of outcome variance had no corresponding feature at all.
2. **Post-delivery deadhead was a flat 20% coin flip**, independent of any state. If the
   simulator itself doesn't make an outcome depend on the decision, there's nothing for a value
   function to learn to prevent — directly undermining the original point of tracking deadhead at
   all ("to prevent this from happening").

Fixed both: `truck_breakdown_risk` captured at decision time (`sim/sql/015`); `p_real_deadhead`
now scales with `dest_distance_to_hub_km` (5% at 0km ramping to 55% at 150km — the coverage
region's diagonal) instead of a flat prior (`dynamic_post_completion_probs()`). **Result: R² =
0.1222**, `truck_breakdown_risk` immediately the dominant feature by a wide margin.

## Two maintenance dimensions, not one

Real fleets schedule service by BOTH distance AND calendar time ("every N km OR M months,
whichever comes first"). The existing model only tracked km. Added `days_since_service` /
`MAINTENANCE_SERVICE_INTERVAL_DAYS` (180, synthesized) as an independent dimension —
`TruckMaintenanceState.breakdown_risk` now uses `max(pct_of_km_interval, pct_of_days_interval)`,
so a truck driven lightly but sitting since a long-ago service (calendar-overdue, km-fresh) is
correctly flagged, a real case the km-only model completely missed. Both dimensions initialized
independently (uniform, spread, not all-fresh — same principle as the original km-only design).
Both raw percentages exposed as features (`truck_pct_km_interval`, `truck_pct_days_interval`),
not just the derived risk score — after full re-training, these are the #2 and #3 most important
features, right behind `truck_breakdown_risk` itself.

## Appointment lateness — a real, separately-modeled penalty now

No promised delivery time existed anywhere, so there was no way to penalize running late, and no
model discovered LTL is what the "lateness_risk_penalty" column actually was (a misleading name —
it's the hos_stranding + maintenance risk combo, renamed to `hos_maintenance_risk_penalty` in
`sim/sql/017` now that a REAL lateness concept exists alongside it).

- `Order.promised_delivery_at` = order creation time + loaded transit + calibrated median dwell
  (pickup + delivery) + `ASSUMED_PROMISE_BUFFER_HOURS` (5h, synthesized — covers typical
  time-to-dispatch and normal slack, no real quote data exists to calibrate against).
- `late_delivery_penalty()`: realized-only (like post-delivery-deadhead/breakdown — not knowable
  until the trip finishes), a small grace buffer (`LATE_GRACE_MINUTES=15`) before anything is
  charged, then **convex** in hours late (`LATE_PENALTY_EXPONENT=1.5`) — satisfaction degrades
  faster than proportionally the longer a customer waits, not just steadily.
- The cascading "a late trip delays the driver's next trip too" effect needed no new code — it's
  already an emergent property of the discrete-event clock (a driver isn't available for their
  next assignment until this trip's real `driving_end`, whatever that turns out to be).
- Only ~0.4% of trips in a 20-run sample actually triggered a penalty (most finish comfortably
  inside the generous promise buffer) — a plausible rate, not over- or under-triggering.

## Other feature additions requested directly

- **`total_committed_distance_miles`** (deadhead + loaded) — the driver's full distance
  commitment for this decision, not reconstructable by a tree from two separate distance columns
  without several splits.
- **`dest_local_order_density`** — real `calibration.lane_frequency` weight originating near the
  delivery point; a sharper "will this driver likely find a reload nearby" signal than raw
  hub-distance alone.
- **`driver_pool_size`** (2 or 4 — see file 10's driver↔truck redesign) — exposed directly as a
  feature now, not just an internal assignment constraint.

## The honest limitation surfaced along the way: single-trip value vs. real sequential value

Asked directly whether the current approach can capture "assign this Milton→London truck to a
London→Kitchener order because it conveniently heads back the right way" — the honest answer is
**no, not yet**. What's trained is `Q(state, this-specific-order)` — how this trip's own direct
aftermath turned out — not a pure state-value `V(position, hos, time)` that could be evaluated at
a *hypothetical* landing spot regardless of which order got the driver there. That requires
genuine Fitted Value Iteration: capture the real next-state (position/HOS right after each trip —
schema columns already reserved, still null), then iteratively refit V using itself as the
bootstrap (`V_{k+1}(s) = realized_reward + γ·V_k(next_state)`, a few rounds), then rewire
`policy.py` to score a candidate by evaluating V at the order's own destination. Scoped as the
next real piece of work, not yet built.

## Files

- `sim/sql/015` (truck_breakdown_risk), `016` (FTL/LTL + lane features + trip_id/planned hours),
  `017` (lateness_penalty + the lateness_risk_penalty→hos_maintenance_risk_penalty rename), `018`
  (total_committed_distance, driver_pool_size, both maintenance-interval percentages,
  dest_local_order_density)
- `sim/engine/reward.py` — `service_type` branch in `compute_reward()`, new `late_delivery_penalty()`
- `sim/engine/maintenance.py` — the second (calendar-time) maintenance dimension
- `sim/engine/run_sim.py` — `dynamic_post_completion_probs()`, all new Order/CompletedTrip fields
- `sim/training/extract_transitions.py`, `train_value_function.py` — updated for every new column

All 42 tests pass (8 new: 2 maintenance, 3 reward/FTL-LTL, 3 lateness — see `sim/tests/`). Full
10,000-run batch regenerated end to end after every fix (~105s batch + ~65s extract + ~10s train).
