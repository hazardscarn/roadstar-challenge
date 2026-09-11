# A Real Feature That Caused a Real Regression, Found and Fixed

**What:** In response to being asked why V(s) only uses 4 features, found and added one genuinely
missing one (truck maintenance condition), which then caused a confirmed, statistically
significant regression -- traced to a concrete mechanism, fixed, and re-verified.

## Why V(s) stays narrower than the Q-model, and the one real gap in that design

The Q-model already carries ~16 features including distance/destination-specific ones -- V(s)
deliberately excludes those because they don't exist yet at the moment V(s') is evaluated (the
next order isn't known). But truck condition is different: the SAME truck rides forward with a
driver into their next trip (not swapped mid-route), so "how overdue is the truck they'll show up
with" genuinely is part of the landing STATE, not a future-order guess. This was missing --
`next_truck_pct_km_interval`/`next_truck_pct_days_interval` threaded through `sim.assignments` ->
`training_transitions` -> V(s)'s feature set -> `value_function.py`'s live-scoring closure
(projected via the SAME `after_trip()` math the simulator itself uses).

**Effect confirmed real, not leakage**: isolated at Round-0 (direct fit on raw `target_value`,
before any FVI bootstrap could contaminate it) -- RΒ² 0.0032 without the feature vs. 0.1445 with
it. Real mechanism: `truck_breakdown_risk` is a deterministic function of these same percentages,
and the large realized breakdown-cost penalty is baked directly into `target_value`.

## The regression, found via the paired significance test itself

Retrained and re-ran the paired Ξ΅=0 test (same methodology as file 15): **t=-4.68, trained
significantly WORSE than greedy** -- not just "no better," a real regression. Investigated rather
than accepted: probed `_predict_state_value()` directly across a truck_pct sweep and found an
extreme, near-binary cliff at the ~85% overdue threshold (V(s) jumps from +234 to -4,543 crossing
that one boundary) -- `gamma * V(s')` alone at that cliff (~-4,089) utterly dwarfed
`immediate_reward`'s real spread (measured directly: p10=-278.8, p90=+221.5, std=203.3), so the
policy had effectively collapsed to "avoid any overdue-truck candidate, ignore everything else."

**Root cause of the cliff itself, found while fixing it (see file 18)**: the underlying breakdown
rate feeding this cliff's training data was itself unrealistically high, a second, deeper bug --
fixing that (file 18) is what actually made the cliff go away in the next retrain, not the
shrinkage fix alone.

## The fix

Two layers, both real, both kept:
1. **Shrinkage + clip** in `value_function.py` (`V_SHRINKAGE_FACTOR=0.25`, bounds derived from the
   trained model's own real p5/p99.5 prediction percentiles, not arbitrary round numbers) -- a
   defensive backstop against any future retrain producing another sharp cliff, regardless of
   cause.
2. **The real root-cause fix** (file 18): the breakdown-rate curve itself was ~80x too aggressive
   vs. real fleet data, which is what let a small number of extreme training examples dominate a
   tree split in the first place.

Re-verified after both fixes (file 19's final batch): V(s) range tightened from `[-5416, 378]` to
`[-1573, 491]`, converges smoothly across FVI rounds (0.15->0.27, not diverging), and
`gamma*V(s')` now swings a sane Β±20-30 across the full truck-condition range instead of ~4,300.
