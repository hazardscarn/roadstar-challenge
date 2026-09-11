# 25 — Home-time / domicile-return research and methodology references

Research done this session in response to a direct user request ("search online on the papers
written by uber freight and all that do same... was this inspired by some DiDi model and Uber
Freight local also") — logged here as source material for a later demo-deck session, not yet
acted on in code. The actual home-time feature build (hours_since_home, chain-walk-projected
"last at home," combined legal+business urgency, and the real simulated/live "drive home empty"
event) is a separate, planned next step — see the session this log's own conversation continues
into for that plan.

## Context this extends

- [15_research_grounding_and_evaluation_bug.md](15_research_grounding_and_evaluation_bug.md)
  already grounded the base dispatch architecture (immediate reward + γ·V(s'), one-step lookahead
  via a trained value function) against Xu et al. (KDD 2018) and Powell's fleet-management ADP
  generically. This log is narrower and specific to the HOME-TIME/domicile-return problem
  specifically, which log 15 didn't cover.
- [23_home_base_return_gap_found.md](23_home_base_return_gap_found.md) /
  [24_home_progress_retarget_built_and_trained.md](24_home_progress_retarget_built_and_trained.md)
  built `home_progress_bonus`/`cycle_end_stranding_penalty`, and log 24 directly reported the
  mechanism never fired once (0/8,990 trips) in any tested run length, because it's gated on the
  legal 7-day/14-day HOS cycle clock, which a normal week never gets close to exhausting. This
  session's follow-up conversation confirmed the same finding independently (driver 5's real cycle
  snapshots: 56 of 70 cycle-hours still in reserve at week's end) and is the direct motivation for
  the research below — the fix isn't a bigger penalty, it's a different, business-driven trigger.

## Is "always start and end at home hub" a fair assumption here?

Confirmed as a legitimate, standard model for a **regional/dedicated** carrier (short intercity
hauls, small coverage area, driver home weekly or more often) as distinct from long-haul OTR
(drivers out for weeks at a time) — RoadStar's actual operating pattern (1-2 day trips, Southern
Ontario only) matches regional, not OTR. Not flagged as a simplification that distorts the
business; a real, common carrier structure.

## The core training method — is it legitimate for "dynamic programming with a future-state reward"?

Confirmed yes. What's built here (`sim/training/train_state_value_function.py` +
`sim/engine/policy.py`'s `score = immediate_reward + gamma * V(landing_state)`) is **Fitted Value
Iteration**, a standard approximate dynamic programming (ADP) technique — the right tool
specifically because the true state space is too large for exact tabular DP, a working simulator
exists to generate experience, offline/batch training is acceptable, and a fast one-step lookahead
is still needed at each real dispatch decision.

- Ernst, Geurts & Wehenkel, ["Tree-Based Batch Mode Reinforcement
  Learning"](https://www.jmlr.org/papers/v6/ernst05a.html), *JMLR* 6 (2005): 503-556. The closest
  methodological match to what's actually built: fitting a value/Q-function via TREE-based
  regression (this project: XGBoost) from a FIXED BATCH of pre-collected trajectories (this
  project: `sim/run_batch.py`'s ~1,000 pre-simulated weeks), not online updates.
- Sutton & Barto, *Reinforcement Learning: An Introduction*, 2nd ed. (MIT Press, 2018) — standard
  textbook grounding for value iteration, TD bootstrapping, and epsilon-greedy exploration, all
  used here.
- Ng, Harada & Russell, ["Policy Invariance Under Reward Transformations: Theory and Application
  to Reward Shaping"](https://dl.acm.org/doi/10.5555/645528.657613), *ICML* 1999: 278-287 — the
  proof that a potential-based shaping term (`gamma*Phi(s') - Phi(s)`) provably preserves the
  optimal policy of the underlying unshaped reward. Already cited correctly in `reward.py`'s own
  docstring for `home_progress_bonus`; confirmed here as the right, real citation.

## Domain-specific precedent — the SAME method, in truckload dispatch, with domicile/home-time built in, at real production scale

- Godfrey & Powell, "An Adaptive Dynamic Programming Algorithm for Dynamic Fleet Management, I:
  Single Period Travel Times" and "...II: Multiperiod Travel Times," *Transportation Science*
  36(1), 2002: 21-39 and 40-54. The original formulation of this ADP approach for stochastic fleet
  dispatch — the more precise citation than log 15's generic "Powell et al." mention.
- Simão, Day, George, Gifford, Nienow & Powell, ["An Approximate Dynamic Programming Algorithm for
  Large-Scale Fleet Management: A Case
  Application"](https://pubsonline.informs.org/doi/10.1287/trsc.1080.0238), *Transportation
  Science* 43(2), 2009: 178-197. **The single most directly relevant reference for the home-time
  work specifically.** Real deployment at Schneider National (6,000+ drivers). Key facts, worth
  quoting directly in a deck:
  - Each driver is represented with attributes including **domicile** and **days from home**,
    alongside the real 70-hour/8-day HOS rule.
  - For valuing a driver's future position, the value function needed only **three** attributes
    beyond time: **location, domicile, and driver type** — confirms this project's existing
    geographic features (distance/hours to home) are the right *shape*; what's missing is the time
    companion (`hours_since_home`, this session's proposed addition).
  - The resulting policy "**gets drivers home, on weekends, on a regular basis**" — a business
    cadence result, not "only when legally forced to" — direct validation for combining a
    business-driven urgency with the legal-cycle one (`max()` of the two), rather than relying on
    the legal clock alone.

## Ride-hailing value-function dispatch (DiDi) — validates the value-function/lookahead method itself

- Tang, Qin, Zhang, Wang, Xu, Ma, Zhu & Ye, ["A Deep Value-network Based Approach for Multi-Driver
  Order Dispatching"](https://dl.acm.org/doi/10.1145/3292500.3330724), KDD 2019. Models dispatch as
  a Semi-Markov Decision Process (trips take variable real time, same reason this project tracks
  real `loaded_hours`/dwell rather than a fixed step), fits a value network via value iteration
  with function approximation — structurally the same "value of landing here" idea as this
  project's V(s), just with a neural net instead of XGBoost.
- Qin, Zhu & Ye, ["Ride-Hailing Order Dispatching at DiDi via Reinforcement
  Learning"](https://dl.acm.org/doi/10.1287/inte.2020.1047), *Interfaces* (INFORMS), 2021 —
  practitioner writeup, validated via large-scale online A/B tests on DiDi's real platform
  (improved both driver income and rider experience). Same paper log 15 already cited.
- Also still relevant from log 15: Xu et al., ["Large-Scale Order Dispatch in On-Demand
  Ride-Hailing Platforms"](https://dl.acm.org/doi/10.1145/3219819.3219824), KDD 2018 — an earlier,
  arguably more foundational DiDi dispatch-RL paper.

## Uber Freight — validates BOTH the model architecture and the home-progress feature specifically, in freight

Uber Freight's own engineering blog, ["Achieving Better Load Matching with
AI"](https://www.uberfreight.com/en-US/blog/better-load-matching-with-ai) — their real production
architecture:

- **Two-stage**: candidate generation (multi-signal filtering), then **XGBoost ranking** — the
  exact same shape and the exact same model family as this project's own
  `build_candidates() -> feasible_candidates() -> score_candidate()/rank_candidates()` pipeline.
- One of their real, named ranking signals: **"loads taking freight drivers to their billing
  address as a proxy for home."** Direct, real-world confirmation that "progress toward home" is a
  genuine, valuable production dispatch-ranking signal in freight specifically — the strongest
  available validation for `home_progress_bonus`/`distance_to_home_miles`, from a source
  recognizable in a demo deck.
- Separately (search snippet, page itself 403'd to automated fetch): Uber Freight's algorithmic
  load bundling (launched Sept 2019) is reported to reduce deadhead miles by ~22.6%, and their
  empty-miles measurement convention (miles between dispatch and pickup = empty, pickup to
  drop-off = full) matches this project's own `pre_pickup_deadhead_miles`/`loaded_miles` split.

## One-line framing for the deck

*"Fitted Value Iteration (approximate dynamic programming), trained on Monte Carlo-simulated
dispatch rollouts, with potential-based reward shaping for the home-return objective — the same
class of method used in Schneider National's production fleet-management system (Powell et al.),
with a candidate-generation + XGBoost-ranking architecture matching Uber Freight's own published
load-matching system, and a value-function/one-step-lookahead pattern matching DiDi's production
ride-dispatch system."* Three real, independent, citable precedents, each validating a different
piece of what's actually built here — not overclaimed, not fabricated.

## Still to document for the deck (not done here)

The Monte Carlo priors/calibrated distributions the simulator actually draws from at each decision
(order arrival rates, dwell-time distributions, lead-time samples, lane frequencies, the
post-completion-deadhead probabilities, breakdown rates) — a separate compilation task, deferred
per the user's own explicit sequencing ("once logged, let's start building... probably better to
create a plan first").
