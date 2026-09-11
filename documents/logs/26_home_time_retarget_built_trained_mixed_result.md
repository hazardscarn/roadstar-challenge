# 26 — Home-time (domicile return) retarget built, trained, and validated — a real, mixed result, NOT swapped into production

Built the full home-time feature/reward/simulated-event chain from the approved plan (`sim/calibrate_driver_home_hub.py`'s diverse home-hub work + `documents/logs/25`'s research). Retrained, ran the full validation suite twice (two threshold settings), and found a genuine, honest trade-off — **the new model is NOT swapped into `dashboard/server/main.py`/`score_quote.py`'s default; `state_value_function_v2_home_progress.pkl` stays the deployed model** pending the user's own read of this mixed result.

## What was built (matches the approved plan exactly)

1. **`hours_since_home`** — new driver state, chain-walk-projected in live (`score_quote.py`'s `project_driver_state()`, per the user's own explicit correction: a queued future trip landing at home updates it before the trip being scored, not read as a static value), synchronously tracked in sim (`DriverState.last_home_at`, updated in the real `TRIP_COMPLETE` handler — no chain-walk needed sim-side, the event loop is already strictly causal).
2. **Combined urgency** — `reward.py`'s `combined_home_urgency()` = `max(legal HOS-cycle urgency, business "days since home" urgency)`. The legal one alone was CONFIRMED dormant (`documents/logs/24`: 0/8,990 trips, and this session's own driver-5 real-snapshot check). `ASSUMED_TARGET_HOURS_BETWEEN_HOME = 168h` (one work week) is the new business cadence, cited against Powell/Schneider National's "days from home" driver attribute (`documents/logs/25`).
3. **A real, costed simulated event** — `run_sim.py`'s existing `dynamic_post_completion_probs()` post-delivery mechanic now targets the driver's OWN home hub (not generic nearest-hub) and scales `p_deadhead` up, once combined urgency crosses `HOME_URGENCY_REPOSITION_THRESHOLD`. This is the piece that makes "driving home empty" a real, priced training experience instead of only a post-hoc analysis number.
4. **Live**: deliberately NOT given an autonomous phantom-trip generator — `live.trips` always ties to a real `quote_id`; a dispatcher decides to send a truck home empty, the system only makes sure the features/ranking reflect real urgency.
5. Migration `sim/sql/046` (`live.driver_status.last_home_arrival_at`), seeding (`seed_demo_fleet.py`, flagged SYNTHESIZED), telemetry write-back (`telemetry_simulator.py`'s `_complete_trip()`).
6. Tests: `sim/tests/test_home_progress.py` (+7, `_business_home_urgency`/`combined_home_urgency` unit coverage, plus a direct demonstration that `compute_reward()` now fires under the fleet's REAL comfortable-legal-margin regime where it previously never did), `sim/tests/test_live_multi_trip_scoring.py` (+4, chain-walked `last_home_at`), `sim/tests/test_home_time_retarget.py` (new, a real 240h engine run confirming the mechanism isn't dormant and `last_home_at` tracks real arrivals). **136/136 sim tests passing.**

## The retrain + validation cycle — run TWICE, with a real, reasoned tuning pass in between

### First pass: `HOME_URGENCY_REPOSITION_THRESHOLD = 0.3`

Fresh 500-run/168h batch (45,374 completed trips, `sim.*` local tables truncated first — a genuinely clean generation, not mixed with pre-retarget history). Real, measured incidence: **660/45,374 assignments (1.5%) now show nonzero home-time shaping** (was 0/43,078 before this feature — `documents/logs/24`'s own reported gap, now closed). 37.7% of all assignments land the driver at their own home hub (both the pre-existing "order happens to end there" path and the new repositioning path).

Trained `state_value_function_v3_home_time.pkl` (30,621 greedy transitions, 4 FVI rounds, final R²=0.0865 — comparable convergence to the v2 retrain).

**Paired comparison** (60 matched seeds, v2 vs v3, `sim/training/run_paired_comparison.py`):
- total_reward: v2=8,099.85, v3=7,621.65 — t=-1.989, **p=0.0513** (right at the edge; not conventionally significant, but suggestive of a real small cost)
- avg distance-to-home-at-landing: v2=29.62mi, v3=28.27mi — t=-3.874, **p=0.0003** (real, significant improvement)

**Real-data backtest cycle analysis** (`--cycles`, same 42-driver matched pool): v3 produces MORE, SHORTER cycles (999 vs REAL's 668; avg duration 46.8h vs REAL's 74.9h) but at a real empty-mile cost per cycle (17.7mi avg vs REAL's own historical 1.7mi) — a genuinely higher empty-mile rate than real dispatchers achieve, even though the underlying distance-to-home metric elsewhere improved.

### Second pass: `HOME_URGENCY_REPOSITION_THRESHOLD = 0.6` (raised, re-validated, not assumed better)

The 0.3 threshold was clearly triggering the repositioning mechanic more eagerly than real dispatch patterns warrant — raised to 0.6 (a genuinely tighter bar) and re-ran the FULL sequence (truncate → fresh 500-run batch → retrain → paired comparison → backtest) rather than assuming the change would help.

**Paired comparison, re-run**: total_reward: v2=8,099.85, v3=7,983.14 — t=-0.907, **p=0.3681** (no longer even suggestive — a healthy, non-significant result). avg distance-to-home-at-landing: v2=29.62mi, v3=27.84mi — t=-4.890, **p=0.0000** (the improvement got STRONGER, not weaker). home_progress_bonus earned rose too (5,573 CAD vs 3,887 CAD baseline).

**Cycle analysis, re-run**: empty-miles/cycle improved modestly (17.7mi → 15.7mi) but is still well above REAL's 1.7mi/cycle — a real, structural property of this design (REAL dispatchers apparently find a real paying load heading home ~97% of the time; the calibrated simulated demand doesn't always offer an equally good option, so more of the real cost shows up as an actual priced empty leg instead).

**Main backtest (non-cycles, `--restrict-drivers`) — the genuine open concern, reported honestly, not smoothed over**: this run measures something DIFFERENT from the two above — the driver's position at the single END-OF-WINDOW snapshot (where the whole 1,667-real-order replay happens to stop), not an average across all decisions or all closed cycles. At threshold=0.6, this metric REVERSED: restricted to the 40 real drivers TRAINED actually used, TRAINED left them **296mi FARTHER** from home than REAL at that exact cutoff moment (912mi → 1,209mi), and the strict same-order sanity check (no bonus recovered orders) showed the same reversal (+181mi, WORSE than REAL). This did NOT happen at threshold=0.3 (that run showed a 16% GAP CLOSURE on the identical strict check).

**Why the three metrics disagree, reasoned through, not left unexplained**: a higher threshold makes the mechanism more selective — it only pulls a driver home once urgency is genuinely elevated, which reduces average distance-to-home ACROSS ALL decisions (favors 0.6) and reduces wasted empty miles PER CLOSED CYCLE (favors 0.6), but means a driver whose CURRENT, still-open cycle at the exact moment the backtest window ends simply hasn't crossed that higher bar yet is left wherever their last real paying load happened to leave them — a single point-in-time snapshot is much more sensitive to exactly where the window cuts off than an average is. All three numbers are real and correctly computed; they are just answering different questions ("on average, over every decision" vs "for cycles that actually closed" vs "right now, this instant").

Every other backtest property stayed strong and unchanged at 0.6: 100% missed-opportunity recovery (130/130 real historically-undispatched orders, ~$10.8K), work distributed more evenly across drivers than REAL's own history (std dev 22.1 vs REAL's 39.5), 40/42 real drivers actively used.

## Why this is NOT swapped into production

`dashboard/server/main.py`'s `_load_models_once()` and `sim/live/score_quote.py`'s default model path still point at `state_value_function_v2_home_progress.pkl`. The plan's own gate was "swap only after the paired test AND backtest both look good" — two of three real validation angles (the paired comparison and the cycle analysis) look genuinely good at threshold=0.6; the third (the end-of-window backtest snapshot) shows a real regression that wasn't there at threshold=0.3. Which of these three questions matters more for the actual business use case ("average behavior," "cost per completed home-cycle," or "where are my trucks right now") is a real judgment call, not something to resolve by picking whichever number is more flattering. Left for the user's own read before any swap.

`sim/training/state_value_function_v3_home_time.pkl` is saved and ready if/when a swap is decided. `HOME_URGENCY_REPOSITION_THRESHOLD` is currently 0.6 in `sim/config.py`; a further tuning pass (or a design change addressing the end-of-window sensitivity specifically) is the natural next step if the swap is wanted.
