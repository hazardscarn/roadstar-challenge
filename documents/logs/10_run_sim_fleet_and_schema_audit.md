# Driver↔Truck Pool Redesign, and a Full Schema Audit

**What:** Two things that happened together after the first `run_sim.py` validation pass —
(1) a real design fix to how drivers and trucks pair up, prompted by a direct question about
whether the sim was doing a full 131×131 cross; (2) a full audit of every table in every schema,
prompted by direct questions about `historical_orders.trip_number`, `driver_equipment`'s row
count, and what the `calibration.*` numbers actually mean. Full schema-by-schema detail lives in
[`../schema_reference.md`](../schema_reference.md) — this file is the "what changed and why."

## The driver↔truck question, and why the original design was wrong in the OTHER direction

The first `run_sim.py` (file 09) paired each of the 131 drivers to exactly ONE truck, fixed for
the entire simulation run. Asked directly whether the sim instead does a full cross of all 131
drivers against all 131 trucks: no — but the fixed-1:1 model was just as unrealistic in the
opposite direction. Checked the real data before deciding how to fix it (per the explicit ask to
verify against real data rather than guess): `Driver.ASSIGNED_PUNIT` is **100% empty** (0/131,
a dead column, not a per-trip truck record) and `Dispatch` has **zero** truck/tractor identifier
column at all, on any leg — so there's no real per-trip truck-switching history anywhere in this
export to compute a variance/rate feature from, as was asked. The only real, derivable signal is
binary: `DEFAULT_PUNIT` is set for 18/131 drivers (verified only after cleaning the `<null>`
string sentinel — Known Issue #8 — which otherwise makes this column look like it has 98 non-null
values instead of 18).

**Fix**: neither extreme. Each driver now gets a small POOL of regular trucks —
`KNOWN_DEFAULT_POOL_SIZE=2` (their real truck + one synthesized backup) for the 18 drivers with a
known default, `SYNTHESIZED_POOL_SIZE=4` (fully synthesized fill) for the other 113. At an order
arrival, a driver is only a candidate if one of their OWN pool trucks is both available AND
physically co-located with them right now (`pick_pool_truck()`) — the healthiest one (lowest
breakdown risk) is picked. A driver can end up in a different pool truck trip to trip; the pairing
is never fixed for the whole run, and never a full cross either. Re-validated: 69/69 orders
assigned over the same 168h horizon (down from 101 with the old fixed-pairing model — expected,
since the pool+co-location constraint is a real restriction, not a bug).

## The schema audit

Prompted by direct questions, not a routine check. Ran targeted queries against every schema
(see `../schema_reference.md`'s "Verified checks" table for the full list) — every check passed:
no orphaned foreign keys, no type mismatches, `reference.locations` coordinates all inside the
coverage box, `ground_truth.trailers`' 425 rows match the raw Excel exactly.

Two specific findings, both "working as intended" rather than bugs:

- **`historical_orders.trip_number` null for 141/2,070 rows (6.8%)** — every one of those 141 is
  an order with `was_dispatched=false`; zero dispatched orders have a null trip number. Exactly
  Known Issue #1's expected shape.
- **`driver_equipment` (18 rows) smaller than `drivers`/`trucks` (131 each)** — not old/stale
  data, and not a smaller fleet than assumed (both real counts are 131, not ~20). `driver_equipment`
  is deliberately small: it only holds the 18 drivers with a real fixed truck on file.

## New documents

- [`documents/schema_reference.md`](../schema_reference.md) — full schema-by-schema reference:
  every table, every column's meaning, and a plain explanation of the calibration numbers that
  prompted the "no idea what median_hours/p25 is for" question (they're the shape of real
  pickup/delivery dwell times and driver HOS-at-completion figures, sampled from — not a single
  fixed average).
- [`documents/diagrams/sim_run_walkthrough.drawio`](../diagrams/sim_run_walkthrough.drawio) (+ PNG
  exports) — a 2-page diagram: the full `run_sim.py` data flow (Supabase → SimData → Fleet init →
  event loop → local Postgres), and one real completed trip walked step by step with actual
  timestamps and its reward breakdown.
- [`documents/diagrams/model_pipeline.drawio`](../diagrams/model_pipeline.drawio) (+ JPEG
  exports `pipeline_training.jpg`/`pipeline_live.jpg`) — the planned training pipeline
  (sim runs → transition extraction → GBM/XGBoost value function) and live pipeline (a real quote
  → the same `compute_reward()` module scored against live state → recommendation → trip →
  geofencing → trip_log → rating views), color-coded by what's built vs. planned vs. shared code.
