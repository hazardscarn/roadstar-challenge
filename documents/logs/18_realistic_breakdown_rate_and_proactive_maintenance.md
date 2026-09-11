# Realistic Breakdown Rate, Real Proactive Maintenance, and Two Performance Bugs

**What:** The user's own instinct -- "a truck breaking down should be much rarer than this, and
we're never modeling trucks actually getting serviced" -- checked directly against sourced real
data and confirmed exactly right on both counts, then fixed, then two unrelated but real
performance bugs found and fixed while re-validating.

## The breakdown rate was ~80x too high, sourced and confirmed

Real, sourced benchmark (TMC/FleetNet America fleet-maintenance data): truckload dry-van fleets --
this fleet's own real load-type mix -- average **14,991 miles between roadside breakdowns**. This
fleet's own real average trip length (measured from `sim.assignments`) is ~53.5 miles, so a truck
AT its due point should break down on roughly 1 trip in 280 (0.36%), not the original curve's 15%
at-due / 30%-capped-far-overdue figures. `BREAKDOWN_RISK_AT_DUE`/`BREAKDOWN_RISK_MAX` replaced
with sourced figures (0.36% / 1%).

## Trucks had no path back to a healthy state

`TruckMaintenanceState.serviced()` existed but was never called anywhere in the sim loop -- a real
gap, confirmed by grep (only referenced in a test file). Over a full simulated year every truck
monotonically climbed toward permanently overdue, which is also what fed the extreme cliff in file
17. Added real proactive maintenance: once a truck crosses 85% of its interval (the same threshold
the dashboard's own warning uses), each trip has a 5% chance it goes in for service and resets.
Modeled as TRUCK-only downtime (`TruckState.maintenance_until`, checked in `pick_pool_truck()`),
deliberately NOT baked into the driver's own trip timeline -- the driver isn't stuck at the shop,
they take their next trip with a different pool truck; this avoided repeating the exact "phantom
driving hours during a wait" bug class already caught once in file 16.

**Result, verified against the user's explicit target (1-3 breakdowns per 5,000 trips)**: 198
breakdowns / 558,184 trips in the full retrained batch = 0.0355%, ~1.77 per 5,000 -- squarely in
range. Average reward flipped from -$298.81/trip to +$74.96/trip in the same batch, confirming the
inflated breakdown cost had been the dominant term dragging the whole simulation's economics
negative all session, not a real reflection of dispatch quality.

## Two real performance bugs, found while re-validating (not correctness bugs)

1. **`HOSLog._hours_since`/`_last_qualifying_reset_end` scanned a driver's ENTIRE interval history
   from day one on every single call** -- O(n), growing worse the further into a run a decision
   falls. Profiled directly: 61% of total run time on a full-year sample. Fixed to iterate
   newest-first and stop early (intervals are strictly chronological, so this is mathematically
   identical, not an approximation) -- verified against all 42 existing tests passing unchanged.
   Measured 7.6x speedup (85s -> 11.2s for a full-year zero-policy run).
2. **`_predict_state_value()` constructed a fresh `xgb.DMatrix` on every single call** -- profiled
   directly: DMatrix construction alone was 65% of total time once the trained value function
   drives real decisions (called twice per candidate, across every candidate, every decision).
   Switched to `booster.inplace_predict()` (skips the DMatrix wrapper entirely, reads the raw
   numpy array directly) -- verified identical output to floating-point precision first, then
   measured an 11x speedup on the profiled sample (28.1s -> 2.5s for 200h).

## Final validation: the definitive paired significance test

Regenerated the full 100-run/558K-row training batch with all fixes in place, retrained both
models, then ran a proper parallel paired Ξ΅=0 test (60 seeds, zero-policy vs. trained-policy, full
year each, via `run_batch.py`'s own worker pool rather than a slow serial loop):

**t = -1.51 -- not statistically significant.** Trained value function is statistically
indistinguishable from greedy (48% win rate, -$0.80/trip mean difference, well within noise).

This is a materially different, healthier result than the immediately preceding regression
(t=-4.68): fixing the real bugs (cliff, breakdown rate, both perf issues) removed active harm --
what's left is an honest "not yet proven to help," consistent with everything found earlier this
session (RΒ²β‰ˆ0.27 is real signal, confirmed via the London-hub finding surviving cleanly, but modest
relative to per-trip outcome variance this feature set structurally can't see). The live dispatch
scoring infrastructure itself (`rank_candidates`/`choose_assignment`, real reward economics,
deterministic lateness-risk pricing) is solid, working code independent of this result -- V(s) is
one input to it, not the whole system.
