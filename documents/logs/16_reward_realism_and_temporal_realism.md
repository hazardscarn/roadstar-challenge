# Reward Realism, Temporal Realism, and a Structural Candidate-Pool Redesign

**What:** A long pass fixing genuine correctness/realism gaps flagged directly by the user, not
guessed at: an inconsistent linehaul rate, an inconsistent breakdown-cost basis, an unfitted
post-completion-deadhead curve that turned out to run backwards against real data, then a much
larger structural change -- real order lead times, a 24h dispatch-decision cutoff, mid-route
drivers as real candidates via projected next-state, an exploration-decay schedule, and training
over a PAST calendar year (not the future) so a live system starting "today" is unambiguously
unseen.

## Reward realism (real research, not guesses)

- **Linehaul rate was flat ($3.25/mi regardless of distance)** -- checked against real 2026
  trucking market data: shorter hauls carry a well-documented per-mile premium (fixed per-stop
  costs amortized over fewer miles). Replaced with `LINEHAUL_RATE_TIERS_CAD_PER_MILE`, a
  4-tier distance-based schedule ($5.00/mi under 50mi down to $2.75/mi over 150mi), sourced from
  real short-haul-premium market research, not a single guessed number.
- **Breakdown cost used two DIFFERENT numbers** for the same event -- $2,000 "expected" at
  decision time vs. $8,000 "realized" once it happened, a real inconsistency that structurally
  under-weighted breakdown risk in every dispatch decision relative to what batch-level reporting
  then charged for it after the fact. Sourced real tow+repair+downtime cost research
  ($5,500-$14,800 CAD/incident) and unified both uses onto one number,
  `ASSUMED_BREAKDOWN_COST_CAD` (later revised again -- see file 19).
- **`dynamic_post_completion_probs()`'s distance-to-hub ramp was never checked against real
  data** -- when checked (17 real Ontario cities, matched to geocoded distance-to-hub), it was
  found EMPIRICALLY BACKWARDS: the two real hub cities (distance ~0km) have the HIGHEST real
  deadhead share in the dataset (Milton 37.0%, London 50.6%, vs. 27.3% overall), not the lowest --
  a hub is a company terminal, not a real shipper location. Neither distance-to-hub nor local
  order density showed any other clean relationship past that one real, well-replicated hub
  effect. Replaced the fabricated ramp with the one real effect (hub-adjacency) plus the honest
  network-wide base rate everywhere else, rather than dressing up noise as "grounded in data."

## H3 hexagons replace k-means region clustering

Research into how real matching systems handle this (Uber Freight's own literature; a 2026 paper,
"Ping2Hex," on FTL truck-load matching) found H3 hexagonal binning -- Uber's own open-source
library -- outperforms k-means region clustering for this exact problem: deterministic (no
silhouette-score search, no risk of cluster boundaries shifting when a location is added), real
multi-resolution geometry, uniform neighbor structure. `sim/cluster_locations.py` rewritten:
res=4 gives 25 distinct cells over the real 2,110 locations (was k=30 via k-means).
`N_REGIONS` updated everywhere it's referenced.

## Temporal realism -- the structural redesign

The user's core critique: one week of simulated time isn't enough depth, and the candidate pool
(idle drivers only) doesn't reflect how real dispatch works -- orders are booked with real lead
time, and a driver mid-route toward a good return leg should be a real, scoreable candidate, not
invisible until they're free.

- **Real order lead time**: `calibration.order_lead_time_hours` seeded from 1,643 real
  `Tlorder.CREATED_TIME` -> `ACTUAL_PICKUP` gaps (median 44.3h, p90 145h) -- bootstrap-sampled at
  runtime, same treatment as the existing order_pool weight/pallets sampling.
- **Booking and deciding are now two separate events.** `Order.decision_time =
  max(booked_at, requested_pickup_at - DISPATCH_DECISION_CUTOFF_HOURS)` -- 24h, sourced from real
  freight brokerage practice (tenders 24-72h before pickup; dispatchers finalize each day's route
  plan in the morning, not at the pickup moment). An order with real lead time sits on the books
  until its cutoff; one booked with <24h notice (a real ~25% of them) is decided immediately.
- **Mid-route drivers are real candidates**, scored from their PROJECTED landing state (position,
  HOS) via a new `effective_driver_state()` helper -- not excluded outright the way `if not
  drv.available: continue` used to hard-filter them. The only hard filter remaining is HOS
  legality against the projected state; a legal-but-badly-timed pick is left in and taught via the
  reward, not blocked.
- **Just-in-time departure, not instant departure**: a driver assigned with real lead time now
  departs late enough to arrive at `requested_pickup_at` (minus a small real 15-45min buffer), not
  the instant the decision fires -- a real dispatcher doesn't send a truck out a day early. The
  wait is logged as real OFF_DUTY time (eligible for a genuine HOS reset if long enough), never as
  phantom DRIVING time -- caught and fixed as a real bug during this same pass (the first version
  of this change baked the wait into the driving interval, which would have falsely burned HOS
  hours on genuinely idle time).
- **`EPSILON_START=0.6` decaying to `EPSILON_END=0.05`** across simulated elapsed time (not flat
  0.2 for the whole run) -- standard Ξ΅-greedy practice, verified in real output data to decay
  smoothly 60%->3% across a full simulated year.
- **Training now runs over a PAST calendar year** (`sim_start` defaults to one year before this
  project's reference "today"), not a fabricated future week -- so a live system starting today is
  unambiguously a fresh period the model never trained on.
- **Real OSRM, not just the hub-anchored cache**: `get_route()` gained a live-OSRM fallback for
  location pairs outside `calibration.lane_routes`' pre-built cache (now needed since mid-route
  candidates' projected landing spots are often a real delivery destination, not a hub). Verified
  directly: 98.9% of route lookups in a real simulated week still hit the pre-built cache; the live
  fallback covers the rest.

## Expected-lateness pricing, added at the user's direct prompt

The user asked: does picking a risky mid-route driver actually get penalized BEFORE the bad
outcome happens, or only after? Checked directly -- it wasn't. Added
`expected_lateness_penalty` to `compute_reward()`, computed deterministically from the real OSRM
deadhead ETA (`candidate.effective_start + pre_pickup_deadhead_hours`) vs. `requested_pickup_at`,
using the SAME convex penalty shape as the existing realized lateness penalty (factored into one
shared `_lateness_penalty_from_hours_late()` so the two can't drift apart). A real bug caught
during this same change: the ETA math must use each candidate's OWN `effective_start` (a mid-route
driver's projected landing time), not the shared decision-clock `now` -- added `effective_start`
to `Candidate` to fix it. Verified directly: mid-route realized-lateness rate dropped from 7.0% to
0.4-0.8% once this was wired in, on par with idle-driver picks.

## Verification

All of the above verified with real instrumented runs, not just code review: timing invariants (0
violations across `created_at <= decision_time <= requested_pickup_at`), HOS interval consistency
(no crashes across a full year), the real lead-time distribution matching the source data's median
exactly (44.3h in both), and the mid-route/lateness-penalty interaction directly measured before
and after the fix.
