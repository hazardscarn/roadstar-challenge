# A Real-Data Backtest: REAL Dispatch vs. TRAINED Model, on the Actual Historical Orders

**What:** After the paired synthetic significance test (file 18), the user asked for the
remaining validation from the original plan: run the actual real historical order data through
the trained model and compare against what the real dispatcher actually did -- not another
synthetic batch. Built as `sim/backtest/real_data_replay.py`, redesigned twice based on direct
user correction before landing on the final methodology below.

## Design, and why it changed twice

**First attempt** compared REAL vs. a GREEDY replay vs. TRAINED, using every reward component
(including lateness/HOS-risk penalties) for all three. Wrong on two counts, both caught by the
user directly: (1) GREEDY wasn't wanted -- the real comparison is REAL vs. TRAINED, not a
three-way policy bake-off; (2) charging TRAINED for lateness/HOS-risk penalties that REAL can't be
fairly charged (no reconstructable real HOS/fatigue state exists for a historical driver) isn't
apples-to-apples.

**Final design**, per the user's own framing: "look at the actual revenue and cases like this
triggered on both... number of opportunities missed... we can't add the lateness penalty and
breakdown etc which we can do apples to apples in our model based run vs actuals." Two arms only:

- **REAL**: the actual historical dispatch outcome, reconstructed from real data -- for each of 42
  real drivers with a resolvable identity (via `trip_number` -> `historical_legs.driver_id`, the
  only real per-order driver-identity signal available; `historical_legs`' own position/timing
  columns are checked and confirmed 100% null, unusable), their own real chronological order
  history gives a real pre-pickup deadhead distance (previous real order's destination -> this
  real order's origin), routed through the SAME real OSRM `get_route()` both arms use.
- **TRAINED**: the identical 1,667 real orders (real origin/dest/weight/pallets/load_type/timing),
  replayed through our own internally-consistent simulated fleet (the `orders=` parameter added to
  `run_simulation()` for this purpose -- REPLACES the synthetic Poisson-arrival process with a
  pre-built real sequence, everything else -- HOS, maintenance, candidate scoring -- runs
  identically). This is a REPLAY, not a reconstruction of real historical fleet state (the same
  honest caveat from file 17): it tests "how would OUR system have handled this real demand
  pattern," not "did the real dispatcher provably make a mistake."
- **$ figure limited to revenue - deadhead cost ONLY** -- the one subset both arms can be scored
  on fairly. Lateness/breakdown/HOS-risk are excluded from the $ total entirely, not estimated for
  REAL, per the user's explicit instruction.

## A real construction bug caught before trusting the first run

The first working version set `decision_time = requested_pickup_at = actual_pickup` (the real
historical pickup instant) for TRAINED's replay orders -- giving the model ZERO real lead time to
reposition, unlike every other run this whole session (which used the real 24h-median lead time).
This alone produced a spurious 45.7% "late" rate that was an artifact of the construction, not a
real finding -- caught by checking WHY the $ gap looked large before reporting it, not accepted at
face value. Fixed: `decision_time = actual_pickup - 24h` (matching `DISPATCH_DECISION_CUTOFF_HOURS`,
the same real-world convention used everywhere else).

## A real event-loop bug caught while building the replay

`run_simulation()`'s main loop originally did `if now > sim_end: break` -- for the replay's
tightly-bounded time window (unlike a full year, where a few cut-off trips near the end are
negligible), this risked silently discarding a still-in-progress trip's real completion. Fixed to
`continue` past sim_end for new decision-generating events but let already-scheduled
`TRIP_COMPLETE` events always run to completion (the heap is a min-heap by time, so a
later-time event popped before an earlier-time one already in the queue is a real, expected case
-- `break` would wrongly discard that pending completion instead of draining down to it). Verified
against all 42 existing tests passing unchanged before trusting the replay's results.

## Results (1,667 real dispatched orders, 42 real drivers)

| | REAL | TRAINED | Diff |
|---|---|---|---|
| Total deadhead miles | 30,766 | 30,364 | -402 |
| Total revenue (CAD) | 229,688 | 229,688 | +0 (same real orders) |
| Total deadhead cost (CAD) | 53,841 | 53,137 | -704 |
| **Net (revenue - deadhead)** | **175,847** | **176,551** | **+704** |
| Avg net/order | 105.49 | 105.91 | +0.42 |

A small, real, directionally-favorable edge -- consistent with file 18's finding (the trained
value function's signal is real but modest at this feature richness), not a dramatic win, and
reported as exactly that.

## Missed-opportunity recovery (the real point of this experiment)

Per the user's direct framing -- "in the actual case this was a lost opportunity... if we can
assign a candidate it's a win" -- the 130 usable REAL `was_dispatched=false` orders (real
origin+dest present; real `CREATED_TIME` pulled from the raw Excel by `bill_number` join, since
these orders never got far enough in real life to have an `actual_pickup`) were inserted into the
SAME merged replay timeline as the 1,667 dispatched orders, facing the identical evolving fleet
state:

- **TRAINED found a feasible candidate for 130/130 (100%)** of these real missed opportunities.
- Net $ recovered: **10,509 CAD** total across all 130; **10,618 CAD / 82 orders (129.48 CAD/order
  avg)** excluding 48 real YARD SHUTTLE moves (origin=dest resolving to a real terminal hub --
  checked directly, e.g. `location_id=2` = the Milton hub -- matching `classify_run_type()`'s own
  established real-data category, not a data error; these correctly net ~$0 under a linehaul
  formula since their real value is yard staging/handling, not the kind of $ this experiment
  measures).

**Honest interpretation, not oversold**: 100% recovery isn't proof the trained model is
exceptional -- checked directly, the real order volume in this window is genuinely light (~13.7
orders/driver across ~5.5 real months, vs. this same fleet handling 40+ trips/driver/year in the
full synthetic batches) -- so real fleet-wide CAPACITY almost certainly existed the whole time.
What this experiment actually demonstrates is narrower and still real: **the capacity was there,
and our system's candidate-scoring/matching logic reliably finds and uses it where the real
historical process, for whatever real-world reason, did not** -- a genuine, concretely
demonstrated capability, not a claim that the real dispatcher made 130 provable mistakes (many
real-world constraints -- contract terms, driver preference, information lag -- aren't visible in
this data and aren't claimed to be captured here).
