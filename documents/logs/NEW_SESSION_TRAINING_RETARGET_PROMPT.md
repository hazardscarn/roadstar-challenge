Paste this as the opening message of the new session.

---

Retarget the trained dispatch model to price a real, currently-unmodeled risk: a driver ending up
stranded far from any terminal with too little HOS cycle time left to get back, and the flip
side -- credit for a chain of real (revenue-paying) trips that progressively works a driver back
toward base instead of one full empty return leg. Read
`documents/logs/23_home_base_return_gap_found.md` first -- it traces the exact gap through the
real code (reward.py, hos.py, the trained model's actual state features) so you don't have to
re-derive it. Also skim `documents/logs/13_state_value_fitted_iteration.md`,
`documents/logs/16_reward_realism_and_temporal_realism.md`, and
`documents/logs/17_truck_maintenance_feature_and_regression.md`/`18_realistic_breakdown_rate_and_proactive_maintenance.md`
for how the last two real feature/reward additions were done and validated -- match that rigor,
don't improvise a different process.

## The problem, precisely

Confirmed by reading the actual trained model (`sim/training/state_value_function.pkl`, loaded by
`sim/engine/value_function.py` and driving BOTH live scoring and the Simulation Showcase):

- `compute_reward()` (`sim/engine/reward.py`) has no term for a candidate's distance to home/a
  hub, at all.
- The trained `V(s)`'s state is `(region_id, hos_remaining, hour_of_day, day_of_week,
  truck_pct_km_interval, truck_pct_days_interval)` -- `region_id` teaches the model about real
  order DEMAND density, which is empirically NOT correlated with hub proximity (hubs are company
  terminals, not shippers -- `documents/logs/16`'s finding: Milton 37.0% real deadhead share,
  London 50.6%, both above the 27.3% network average, i.e. landing AT a hub is one of the WORST
  outcomes for needing another real deadhead, not the best).
- `hos_remaining` is a single blended MIN across all 5 real HOS limits
  (`HOSState.remaining_hours`) -- it cannot distinguish "near today's 14h duty limit, plenty of
  weekly cycle left, can rest and drive back tomorrow" from "near the 70h/7-day cycle limit, far
  from any hub, genuinely no real path back before a long forced reset." These are very different
  real situations the model currently can't tell apart.
- `ground_truth.drivers.home_zone`/`terminal_zone` is loaded once, to place each driver at their
  starting hub at simulation start, and never used again.

So: no reward signal, no state feature rich enough to even represent the situation, at any layer.
Not a bug in existing code -- a genuinely missing dimension.

## What "good" looks like (the user's own framing -- design to this, don't reinterpret it)

Not a strict "always return to exact start point" rule -- that would throw away real revenue on
legitimate longer-haul runs. The target behavior:

1. **Never let HOS cycle time run out with the driver stranded far from any hub with no realistic
   path back.** HOS legality (`feasible_candidates()`'s hard filter) is untouched and stays exactly
   as strict -- this is a NEW risk to price among otherwise-LEGAL choices, not a new hard
   constraint, with one possible exception worth a design decision: should ending a 7-day/14-day
   CYCLE far from a hub get an explicit, strong soft penalty distinct from the existing
   within-trip `hos_stranding_risk_penalty`? Probably yes -- propose it, but confirm the exact
   shape/weight empirically (same standard as `documents/logs/17`/`18`), don't just guess a
   constant.
2. **Reward a CHAIN of real trips that progressively closes distance back toward base**, not just
   a binary "did this exact trip end at a hub." Concretely (the user's own example): driver starts
   Milton, runs Milton->London (normal, no special credit needed for the outbound leg itself),
   then instead of one full empty Milton return leg (100% deadhead loss), gets assigned
   London->Kitchener, then Kitchener->Mississauga (materially closer to Milton than Kitchener) --
   each of those legs is REAL, REVENUE-PAYING freight, and the fact that they also happen to work
   the driver back toward home should be credited PROPORTIONALLY (however much closer to base that
   leg left them, as a fraction of what a direct empty return would have cost), not only if the
   driver eventually lands exactly back at a hub. Partial credit is expected and fine -- "that
   percentage of the deadhead leg covered and revenued IS the value" (direct user framing).
3. Do this as **reward shaping**, not as a hacked-in bonus that could change what the "optimal"
   policy even means -- use **potential-based shaping** (Ng, Harada & Russell 1999: `F(s,a,s') =
   γΦ(s') − Φ(s)` for any potential function `Φ`, provably preserves the optimal policy under the
   unshaped reward). A natural `Φ(s) = -distance_to_nearest_hub(s)` (or distance to the driver's
   OWN home terminal specifically -- make and document that design choice) gives exactly the
   "credit for closing the gap toward home, whichever real trip does it" property described above,
   for free, with a real citable justification -- this project already grounds its designs in real
   literature (`documents/logs/15`'s DiDi KDD 2018 / Powell ADP grounding), keep that standard.

## Concrete engineering plan

**A. New state/feature: distance to hub.** Add a real `distance_to_nearest_hub_km` (or
specifically distance to the driver's own `home_zone` hub -- decide and document which, they can
differ) computed the same way `dynamic_post_completion_probs()` already does
(`_haversine_km`/`get_route` against `data.hub_ids`), both at decision time (current state) and
at the candidate's landing state. Thread it through `Candidate`/`Order` (`sim/engine/policy.py`,
`sim/engine/run_sim.py`) the same way `dest_distance_to_hub_km` already exists on `Order` -- reuse
that field where possible instead of adding a duplicate.

**B. Split HOS state, don't keep the blended min for this.** The Q-model
(`sim/training/train_value_function.py`) and V(s) (`sim/training/train_state_value_function.py`)
both need `remaining_cycle1_hours`/`remaining_cycle2_hours` as EXPLICIT separate features
alongside (not instead of) the existing blended `hos_remaining` -- that's what actually
distinguishes "recoverable tomorrow" from "genuinely stranded." `HOSState` already has these
(`sim/engine/hos.py`); the gap is in what gets PERSISTED, not computed.

**C. Persistence gap -- this needs NEW sim runs, not just a re-extract.** Checked directly:
`sim.assignments`/`sim.orders` (`sim/sql/005_sim.sql`) only ever stored the blended
`driver_hos_remaining`/`next_hos_remaining`, never the 5 individual HOS components, and never any
distance-to-hub figure. `sim/training/extract_transitions.py` can only extract what's actually in
`sim.assignments` -- so the existing 558,184 rows across 100 stored sim runs (check
`select count(*) from training_transitions` / `select count(distinct sim_id) from
training_transitions` locally to confirm current state) **cannot** retroactively gain these
features. You will need to: (1) add the new columns to `sim.assignments`/`sim.orders` (a new
migration, next available number as of this writing is `sim/sql/041_...sql` -- verify against
`ls sim/sql/` before using it, more may have landed since), (2) make `run_sim.py` populate them
for both current AND next state (same "next-state capture" pattern `documents/logs/13` already
established for `next_hos_remaining`), (3) re-run `sim/run_batch.py` to generate fresh sim data
with the new columns actually populated, (4) re-run `extract_transitions.py` with the new SELECT
columns, (5) retrain.

**D. Reward shaping term.** Add `home_progress_bonus` (name it better if you find one) to
`RewardBreakdown` (`sim/engine/reward.py`), computed via the potential-based formula above at
assignment/next-state time (mirrors how `hos_stranding_risk_penalty`/`expected_lateness_penalty`
are already computed from state, not fabricated post hoc). Value it in real $/mile terms
consistent with `ASSUMED_OPERATING_COST_PER_MILE` (this is a SYNTHESIZED weighting choice like
every other assumed rate in `sim/config.py` -- label it the same way, don't present it as derived
from real data it isn't). If you add the cycle-end stranding penalty from point 1, add it as its
own separate, clearly-named term -- don't fold it into the existing trip-level
`hos_stranding_risk_penalty`, they're answering different questions.

**E. Data volume check before assuming 100 runs is enough.** "Near cycle-limit AND far from a
hub" is inherently a MINORITY event within any single week-scale run (cycles are 7/14 days;
`run_batch.py` already simulates over a full past year per run per its own docstring, so this
should occur repeatedly per driver per run -- but CONFIRM this empirically, e.g. count how many
of the new training rows actually have `remaining_cycle1_hours` below some low threshold
combined with a large `distance_to_nearest_hub_km`, once the new columns exist) -- if coverage of
that specific regime is thin, more runs (or oversampling those transitions during training) may
be needed. Don't assume either way; measure it, the same way `documents/logs/14`'s ranker attempt
measured before concluding, and reported the negative result honestly rather than shipping
something undertested.

**F. Validation -- match `documents/logs/17`/`18`'s standard exactly.** A real paired
significance test (old trained model vs. new trained model, same batch of held-out sim runs,
t-test on the outcome that matters -- likely something like "average distance from a hub at the
moment a driver's weekly cycle actually binds" or "real deadhead $ incurred specifically on
post-cycle-reset trips", not just overall reward, since overall reward could improve or worsen for
reasons unrelated to this fix). Also build a small, direct hand-built test scenario matching the
user's own example (Milton->London->Kitchener->Mississauga chain vs. a single Milton empty return)
and confirm the new reward actually scores the chain better -- add it to `sim/tests/` as a real
regression test, this project's established convention for anything regression-prone. Confirm HOS
hard-feasibility is completely unchanged by this work (a real regression check, not an assumption)
-- this is a preference change among legal candidates, never a new source of illegal ones.

## Diagram deliverables (explicitly asked for)

1. **Update `documents/diagrams/pipeline_training.jpg`** (source: `documents/diagrams/
   model_pipeline.drawio`, page 1 "Training Pipeline" -- confirmed; page 2 "Live Pipeline" is the
   separate `pipeline_live.jpg`, leave that one alone unless the training-side changes ripple into
   it) -- it
   currently marks `extract_transitions.py`/`train_value_function.py`/the trained model artifact
   as "(BUILT NEXT)"/planned, but they've been built and are live in production (`main.py` loads
   `sim/training/state_value_function.pkl` at startup and it drives real scoring) since well
   before this session -- the diagram is stale regardless of this task. Update it to reflect
   what's actually built, and add the new reward-shaping/HOS-detail/hub-distance work as its own
   stage in the pipeline.
2. **A new, detailed draw.io diagram of the reward composition / training objective itself** --
   not a data-flow diagram like the one above, but a breakdown of every term in `RewardBreakdown`
   (`order_revenue`, `deadhead_cost`, `opportunity_cost_penalty`, `hos_stranding_risk_penalty`,
   `maintenance_risk_penalty`, `expected_lateness_penalty`, plus the new
   `home_progress_bonus`/cycle-stranding-penalty terms), showing what real-world question each
   term answers and roughly how it's computed -- something a judge could read to understand "what
   is this model actually trying to maximize" at a glance. Use the `drawio-skill` (already
   confirmed working in this environment this session -- CLI is installed, `drawio --version`
   works, no Graphviz available so use the ELK `--layout` pass or hand-placed XML, not
   `autolayout.py`). Export as JPEG to `documents/diagrams/`, matching this session's existing
   naming (`0N_description.jpg` + same-name `.drawio` source).

## Where everything lives (quick index)

- Reward: `sim/engine/reward.py`
- HOS: `sim/engine/hos.py`
- Candidate/policy: `sim/engine/policy.py`
- Discrete-event engine (post-completion branch, assignment persistence):
  `sim/engine/run_sim.py`
- Trained Q-model: `sim/training/train_value_function.py`
- Trained state-value V(s), Fitted Value Iteration: `sim/training/train_state_value_function.py`
- Transition extraction (`sim.assignments`/`sim.orders` -> `training_transitions`):
  `sim/training/extract_transitions.py`
- Batch sim runner: `sim/run_batch.py`
- Local sim schema: `sim/sql/005_sim.sql`, `007_training_transitions.sql`, `019`/`020` (next-state
  additions -- the precedent for this exact kind of change)
- Live/showcase scoring closure that actually calls the trained model:
  `sim/engine/value_function.py`
- Config/assumed rates: `sim/config.py`
- Existing tests: `sim/tests/`

## What NOT to touch

Everything already validated this session and the UI work built on top of it (dashboard/,
`simulation.*` schema, the Simulation Showcase) -- this is a training/reward-only session. Don't
re-run or modify anything under `dashboard/` or the `simulation.*` schema; a later session can
wire a retrained model in once it's validated, that's a separate, much smaller step (just swapping
which `.pkl` `main.py` loads, after confirming the new model's feature set is backward-compatible
with `make_value_fn()`'s call signature or updating that too).
