# Three Schema/Code Fixes Found Building the Training Pipeline, and the Re-runs

**What:** Three real bugs, found in sequence while building toward `extract_transitions.py` and
verifying its output (`sim/export_run_preview.py`) — each fixed, each required re-running the
batch (cheap, ~99s each time), each now verified. Also: scaled from one validated run to a real
batch (`sim/run_batch.py`), built `sim/training/extract_transitions.py` for real, and directly
verified the engine produces a genuine time-ordered discrete-event simulation with correct reward
arithmetic, not randomly generated rows.

## Fix 1: `reward_total` wasn't persisted anywhere

`sim.assignments.immediate_margin` only ever held the reward *at the moment of assignment*
(`compute_reward().total`) — it never included the realized post-delivery-deadhead cost or
breakdown penalty a trip picks up once it actually finishes, even though
`run_sim.py`'s `CompletedTrip.reward_total` computes that correctly in memory. Training a value
function on `immediate_margin` alone would teach it to ignore exactly the outcomes (a stranded
empty truck, a breakdown) it's supposed to learn to avoid. Fixed: `sim/sql/013_add_reward_total.sql`
adds the column; `run_sim.py`'s `save_run()` now persists the real `reward_total`.

## Fix 2: decision-time driver state wasn't captured

`training_transitions` (`sim/sql/007_training_transitions.sql`) was designed from the start to
need `driver_position`/`driver_hos_remaining` — arguably the most decision-relevant features a
dispatch value function has (a driver far away and nearly out of hours is a much riskier pick
than one nearby with a full day left, even at identical immediate reward) — but `sim.assignments`
never captured them. Fixed: `sim/sql/014_add_decision_state.sql` adds
`driver_location_id`/`driver_hos_remaining`; `run_sim.py` now reads the driver's location and
`candidate.hos_state.remaining_hours` at the moment of assignment (before anything moves) and
persists both.

## Fix 3: `deadhead_miles` was actually storing a dollar figure

Caught by hand-verifying `extract_transitions.py`'s output, not by inspection: the script
algebraically recovers a combined risk-penalty column (`lateness_risk_penalty` = `order_revenue -
deadhead_cost - opportunity_cost_penalty - immediate_reward`) that must always be non-negative
(both risk components it represents are non-negative by construction in `reward.py`) — and it
came out **negative** for some rows. Root cause: `run_sim.py`'s `save_run()` was writing
`c.deadhead_cost` (a dollar figure) into the `deadhead_miles` column, not an actual mileage —
`CompletedTrip` never had a real `deadhead_miles` field at all. Every downstream mile-based
calculation reading that column (this recovery formula included) was silently working with
dollars instead of miles. Fixed: added a real `deadhead_miles` field to `CompletedTrip`, populated
from the actual pre-pickup deadhead distance already computed in `run_assignment()`. Re-verified:
`lateness_risk_penalty` is non-negative across all 1,073,622 extracted rows after the fix
(min = 0.0 exactly).

## `sim/training/extract_transitions.py` — the real version, not a preview

Reads `sim.assignments` joined to `sim.orders` across every run in local Postgres, resolves each
driver's decision-time `location_id` to a real lat/lon via `reference.locations` (remote,
loaded once), and writes one row per assignment decision into `training_transitions`
(`sim/sql/007_training_transitions.sql`) — state (driver position/HOS + order) → action (who was
picked) → `target_value` (the fully-resolved realized `reward_total`, the single-pass fitted-value
training label). **1,073,622 rows extracted from the full 10,000-run batch in 45 seconds.**

Real, stated scope limits, not smoothed over: `action_taken` is always `'accepted'` (the simulator
only records the candidate that won — no data exists for candidates considered and passed over);
`lateness_risk_penalty` is the algebraically-recovered SUM of `hos_stranding_risk_penalty` +
`maintenance_risk_penalty` (reward.py has no separate lateness computation, so the column holds
the closest real derivable quantity, not an invented one); `next_driver_position`/
`next_driver_hos_remaining` are left null (only needed for the stretch-goal TD/policy-iteration
approach, not the committed single-pass fitted value).

## Scaling to a real batch, and proving it's not just random

`sim/run_batch.py` runs many simulations in parallel — each worker process loads
calibration/fleet data from Supabase exactly ONCE via a `multiprocessing.Pool` initializer, not
once per run, since the network round-trip would otherwise dominate at any real batch size.
**10,000 runs completed in 99 seconds** (20 workers, this machine's GB10) — 1,073,622 completed
trips, 1.83 GB on disk. Scaling held cleanly linear across 50 → 1,000 → 10,000 runs (~183 KB/run
throughout), so the projections for 100K (~16 min, ~18 GB) and 1M (~2.7 hrs, ~183 GB) are
grounded, not guessed.

**Decided not to scale further right now**: a GBM/XGBoost value function (Segment 4's committed
approach, not a deep net) saturates well under a million training rows for a state space this
bounded (131 drivers × 131 trucks, one region). 10,000 runs already gives ~1.07M rows — past
where more data meaningfully helps. Scaling to 100K/1M would cost real wall-clock time and disk
that Segment 5 (dashboard) and the live pipeline still need, for negligible expected model
improvement — kept as an option (numbers above) if a bigger "simulated N runs" figure is wanted
for the demo narrative specifically, not for training quality.

**Verified the engine is a genuine time-ordered simulation** (`sim/export_run_preview.py`'s
`_verify_time_ordering()`): every trip's own status sequence (ASSGN→DISP→ARRSHIP→...→COMPLETE)
has monotonically non-decreasing timestamps, checked directly against the database, not assumed —
passed for all 80 trips in the exported sample run. The export's `trip_events TIME-ORDERED` sheet
makes this visible directly: every event across every trip/driver in one run, sorted by the
simulated clock rather than grouped by trip — different drivers' events interleave in real
chronological order, the way an actual dispatch floor would, not as isolated sequences a random
generator would have no reason to keep consistent with each other.

## Files

- `sim/sql/013_add_reward_total.sql`, `sim/sql/014_add_decision_state.sql` — the two migrations
- `sim/run_batch.py` — parallel batch runner
- `sim/training/extract_transitions.py` — the real training-data extraction, writes `training_transitions`
- `sim/export_run_preview.py` — one-run Excel export, both correctness checks, `data/sim_run_preview.xlsx`
  is a live sample (not committed data, regenerate anytime with `python3 -m sim.export_run_preview`)

All 36 tests still pass. The 10,000-run batch was regenerated after all three fixes landed (same
throughput, ~99s) and re-extracted (~45s, 1,073,622 rows) so every row in local Postgres today —
both `sim.*` and `training_transitions` — has the corrected columns.
