# Live Scoring Theory-Proof: `sim/live/score_quote.py`

**What:** Direct follow-up to "make sure we have a live simulation possibility first in theory" --
proves the data pipeline for real-time quote scoring works end to end (quote in, ranked top-N
candidates out, using BOTH trained models and every real fix from tonight), without building the
dashboard/Vercel layer yet. No new scoring logic was written -- this wires `live.*` data into the
EXACT SAME `sim/engine/policy.py` functions the simulator already validated, per
`research/roadstar_platform_plan.md` Section 7's original "Request -> candidate filter -> feature
computation -> score -> rank -> top-N" design.

## A real schema gap found and closed first

`live.trips` (from `sim/sql/006_live.sql`, built earlier this session) had no destination or
projected-landing-state columns at all -- only `trip_id`, `driver_id`, `status`, `last_event`,
`eta`. Without that, a live in-transit driver could only be scored on "when free," never "where" --
a real regression against what tonight's mid-route-candidate mechanism (documents/logs/16-17)
already proved works in the simulator. User's own call, asked directly rather than assumed: extend
the schema now, not defer it. `sim/sql/029_add_live_projected_state.sql` adds
`dest_location_id`/`projected_hos_remaining_hours`/`projected_truck_pct_km_interval`/
`projected_truck_pct_days_interval` to `live.trips` (the same next-state fields
`sim.assignments`/`training_transitions` already carry, sim/sql/019/027) and `maintenance_until`
to `live.truck_maintenance_state` (mirroring `TruckMaintenanceState.maintenance_until` from
documents/logs/18's proactive-maintenance fix -- a truck in the shop is unavailable live, exactly
like in the sim).

## Design: reuse, not reimplement

Same principle stated in `reward.py`'s own module docstring ("one function, sim and live"):
`score_quote()` builds real `Candidate`/`Order` objects from `live.driver_status`/`live.trips`/
`live.truck_maintenance_state` rows, then calls the SAME `rank_candidates()` from
`sim/engine/policy.py` that scored millions of candidates tonight. Any drift between "what the sim
proved" and "what live actually does" would come from a second, parallel scoring implementation --
deliberately avoided. Concretely:

- **Idle drivers**: real current position/HOS from `live.driver_status`, `effective_start = now`.
- **Mid-route drivers**: PROJECTED landing state from the new `live.trips` columns --
  `effective_start = eta`, exactly matching `effective_driver_state()`'s logic in `run_sim.py`.
  Skipped (not guessed) if a real trip record hasn't been updated with projection data yet.
- **Trucks in the shop**: excluded via `maintenance_until`, matching `pick_pool_truck()`'s check.
- Both trained models load and score identically to every batch run tonight -- `make_value_fn()`
  with its shrinkage/clip fix, the same `GAMMA=0.9`, the same reward formula.

## One real, deliberate simplification -- stated plainly, not hidden

`live.driver_status.hos_remaining_hours` is ONE number (the platform plan's own schema design),
not the 5 separate HOS sub-clocks (`HOSState`) the sim models internally. Reconstructed as an
`HOSState` where every dimension equals that one number -- CONSERVATIVE, not a hidden
approximation: since the real number is already `min()` of all 5 clocks, this makes
`can_perform()` check the true binding constraint correctly, it just can't tell a live caller
*which* of the 5 clocks is binding (relevant for driver-facing messaging, not for feasibility).

## Verified working, not just written

- `sim/live/seed_test_fleet.py` seeds a small (25-driver) realistic mixed idle/mid-route fleet
  snapshot into `live.*` for local testing -- explicitly a test harness, not a real live-data feed.
- Ran `score_quote()` against a real quote request: **25 candidates built (15 idle + 10
  mid-route), both code paths confirmed exercised** -- checked directly that mid-route candidates
  carry real future `effective_start` timestamps and real projected deadhead distances from their
  landing spots, not just idle ones winning by default.
- Real, sensible ranked output (5 candidates, distinct scores, all HOS-feasible).
- All 42 existing tests still pass -- this addition didn't touch any core engine code.

## What's still NOT built (explicitly out of scope for this pass)

- The Vercel `api/score-quote.py` HTTP endpoint itself (this is the underlying pipeline it would
  call).
- A real live-data feed populating `live.driver_status`/`live.trips` from actual GPS/dispatch
  events (only a synthetic test-seed script exists).
- The dashboard UI (Section 8 of the platform plan) that would submit quotes and render results.

This pass answers the specific question asked: does the data pipeline exist and work, in theory,
with everything tonight validated wired in correctly? Yes, confirmed directly, not assumed.
