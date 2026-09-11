# The Simulation's Status State Machine

**What:** `sim/engine/state.py` — the trip status model the simulator (and, later, the live
system) actually runs on. Replaces the idea of reusing `run_type` as a live simulated state.

**Why not reuse `run_type`**: `run_type` (`sim/classify.py`) was built to retrospectively label
messy historical leg records — reconstructed adjacency on data that had duplicate rows and
trip-boundary artifacts (see files 03 and 05). It's a good *analysis* tool, but importing that
same reconstruction logic into a live simulator risks importing its fragility too. The fix: a
proper sequential state machine using the real TMS status vocabulary already in the data
(`LAST_FB_STATUS` / already the `sim.trip_events.event_type` CHECK constraint) — a trip is in
exactly one status at a time, every transition is an explicit validated event, and `run_type` is
now something *derived once* from a completed trip's real status history
(`TripState.summarize_run_type()`), never a live accumulator. No double-counting is possible by
construction.

**Same module, two callers**: the simulator drives `TripState.transition()` from its
discrete-event loop; the live system will drive the identical method from real geofence/status
events. Neither caller-specific assumption lives in this file.

**Built to directly serve the reward function** (not just track state): `TripState` accumulates
exactly what `sim/engine/reward.py` (not yet built) will need — `deadhead_hours`,
`pickup_dwell_hours`, `delivery_dwell_hours`, `had_secondary_pickup`, `total_weight_picked_up` —
computed once per trip from the real event history, not recomputed some other way later.

## A real bug caught by its own test

`pickup_dwell_hours`/`delivery_dwell_hours` were first implemented as "sum of directly-adjacent
status pairs" — which returns 0 whenever there's a real intermediate status in between (e.g.
`ARRSHIP -> DOCKED -> PICKD -> DEPSHIP`, three hops, not one). Caught by
`sim/tests/test_state.py::test_simple_ftl_trip_deadhead_and_dwell`. Fixed with `_span_hours()`:
pairs each occurrence of a start status with its *next subsequent* occurrence of the end status,
regardless of how many other statuses fall in between.

## Two transition priors checked against real data — one assumption was wrong, not just imprecise

Added as Section 11 of `analysis/data_analysis.ipynb` for the full numbers; summary:

1. **`ARRSHIP -> SPTLD` vs `ARRSHIP -> DOCKED`**: assumed 50/50 going in (flagged honestly as an
   assumption, since `LAST_FB_STATUS` is a single snapshot and can't support fitting fine-grained
   sequencing). Checked anyway at the level it *can* support (relative frequency each status is
   ever recorded): **`DOCKED` never appears at all** among ON-ON loaded legs (0 vs 114 `SPTLD`).
   The assumption wasn't just imprecise, it was wrong — corrected to `p_spotted_not_docked =
   0.99`.
2. **`STOPOFF`**: originally modeled as "picked up a 2nd shipment" (the LTL/multi-stop
   mechanism). Checked directly — **zero overlap** with the real multi-stop/LTL flag
   (`LS_NUM_TOTAL>1`/`LS_FREIGHT2-4`). What it actually correlates with: **whether a leg is the
   final leg of a multi-leg trip**. 99.2% of a multi-leg trip's true final leg reads `COMPLETE`;
   among non-final legs, 56.7% read `STOPOFF` (rest: `COMPLETE` 30.2% — matches the known
   multi-driver-per-trip pattern, a driver's own last leg reads `COMPLETE` even mid-trip —
   `DEPSHIP` 6.2%, `SPTLD` 5.0%, slivers of `PICKD`/`DISP`/`ARRCONS`). `p_secondary_pickup`
   (≈0.07, still backed by the real `LS_NUM_TOTAL` multi-stop flag) is unchanged and separate —
   `STOPOFF` is used only to generate a realistic status-event log texture for a multi-leg trip's
   middle legs, not for the revenue-bearing consolidation mechanic.

Both corrections are in `sim/engine/state.py`'s `TRANSITION_PRIORS`, with the reasoning inline as
comments, not just in this log.
