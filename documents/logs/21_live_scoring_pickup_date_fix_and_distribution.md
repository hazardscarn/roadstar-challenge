# A Real Bug in `score_quote.py` Found From the User's Own Instinct, Plus Reward-Distribution Output

**What:** The user asked for two things directly: (1) make sure a live quote's real pickup date
feeds the feature space, so the model can find the optimal value/reward properly, and (2) confirm
mid-route/return-deadhead drivers actually get assigned when they're the better choice -- "not
always new idle guys" -- since that's what the model was trained to optimize for. Investigating
(2) surfaced a real, confirmed bug that was silently causing exactly the "idle guys always win"
behavior the user suspected.

## The bug, found by instrumenting real candidate scores, not guessed

`QuoteRequest` (file 20) had only one timestamp, `requested_at` -- the moment the quote was
SUBMITTED -- and `build_order_from_quote()` used it for BOTH `requested_pickup_at` and
`decision_time`. That collapses the real lead-time distinction the entire sim was built around
tonight (documents/logs/16-19) back to zero.

Consequence, confirmed by directly printing every candidate's `expected_lateness_penalty` on a
seeded test fleet: every mid-route candidate scored catastrophically worse than idle ones (penalty
range 53-544 CAD vs. idle drivers' 13-136 CAD) -- not because the model disfavors mid-route
drivers, but because the ETA math was comparing a driver's real FUTURE landing time against a
promise anchored to right-now, guaranteeing an apparent lateness violation for anyone not already
idle. This is the exact same "zero lead time" construction bug caught and fixed once already
tonight in the real-data backtest script (documents/logs/19) -- reintroduced here because
`score_quote.py` was a separate module built afterward, not touched by that earlier fix.

## The fix

- `QuoteRequest` gains a real `requested_pickup_at` field, separate from `requested_at` (defaults
  to `requested_at` for a same-instant request -- matching the real ~25% of orders in
  `calibration.order_lead_time_hours` booked with under 24h notice, not a special case).
- `build_order_from_quote()` computes `decision_time = max(requested_at, requested_pickup_at -
  DISPATCH_DECISION_CUTOFF_HOURS)` -- the SAME 24h-cutoff rule every other order in this project
  uses, sourced from real freight-brokerage practice (documents/logs/16).
- `promised_delivery_at` anchors to the REAL requested pickup time, not the booking instant.
- `score_quote()`'s internal `now` changed from `quote.requested_at` to `order.decision_time`,
  keeping candidate-building and scoring internally consistent with the Order object's own timing.

## Verified fixed, with real before/after numbers

Same-instant quote (no real lead time given): behavior essentially unchanged (as expected --
`decision_time` collapses to `now` when there's no real slack), confirming the fix doesn't break
the common case.

Quote with a REAL 3-hour future pickup date, same seeded fleet: **mid-route candidates now
genuinely compete** -- 6 of 20 feasible candidates were mid-route, and **4 of the top 10 ranked
results were mid-route drivers**, including the #2 overall pick (driver 25, beating 3 idle
drivers purely on real deadhead/timing economics -- a genuinely shorter real return-leg deadhead:
24.64 vs. 54.88 CAD for the idle drivers ranked around it). This is the exact "return-deadhead
driver gets assigned when it's the better choice" behavior the user asked to confirm was actually
happening, not just theoretically true.

## Reward-distribution output, per the user's second ask

`score_quote()`'s return shape changed from a bare list to `{'top_n': [...], 'all_scored': [...],
'summary': {...}}`:

- `all_scored`: EVERY feasible candidate's full reward breakdown (revenue, deadhead cost,
  opportunity cost, HOS-stranding risk, maintenance risk, expected-lateness penalty, final score,
  and whether it's an idle or mid-route candidate) -- not just the winner, so the full distribution
  the ranking came from is inspectable, matching how `sim.candidate_scores` already logs a real
  group per decision in the simulator rather than just the chosen candidate.
- `summary`: best/worst/median/score-spread across all feasible candidates, plus explicit
  idle-vs-mid-route candidate counts and whether the top pick itself was mid-route -- a direct,
  checkable confirmation (not an assumption) of whether return-deadhead drivers are genuinely in
  contention for a given quote, not just idle ones by default.

Required pulling the per-candidate `RewardBreakdown` out explicitly (`feasible_candidates()` +
`score_candidate()` called directly, mirroring what `rank_candidates()` already does internally)
since `rank_candidates()` itself only returns the scalar score, not the breakdown -- the sim's hot
loop wasn't touched to add this, kept as a live-side addition only.

All 42 existing tests still pass -- no core engine code changed, only `sim/live/score_quote.py`.
