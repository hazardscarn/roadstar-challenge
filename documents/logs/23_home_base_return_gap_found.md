# 23. A real gap found: nothing rewards getting drivers back near base

Prompted by a direct user question while reviewing the Simulation Showcase's Order Story
("does the model learn to avoid stranding a driver, or to prefer trips that work back toward
base?"). Traced the actual reward function and trained state-value model's feature set end to end
to answer honestly rather than guess. Short answer: **no, this isn't modeled at all, not even
indirectly** -- confirmed by reading every place it could plausibly be, not by assumption.

## What was checked

**`sim/engine/reward.py`'s `compute_reward()`** -- the full `RewardBreakdown`:

```
immediate_reward = order_revenue - deadhead_cost - opportunity_cost_penalty
                    - hos_stranding_risk_penalty - maintenance_risk_penalty
                    - expected_lateness_penalty
```

`hos_stranding_risk_penalty` (via `HOSState.stranding_risk()`, `sim/engine/hos.py:166`) sounds
related but isn't -- it's a purely WITHIN-TRIP margin check ("does *this* trip's planned duty
hours eat past half this driver's remaining legal margin"), 0-1, linear ramp. It has no concept of
where the driver ends up, only whether this one trip itself is cutting it close. No term anywhere
prices "how far is this candidate's landing spot from home" or "how many real cycle-hours are left
to get back."

**`ground_truth.drivers.home_zone`/`terminal_zone`** -- loaded (`run_sim.py`'s
`driver_terminal_zone`) and used exactly ONCE: to place each driver at their starting hub
(Milton/London) at simulation start (`initialize_fleet()`). Never referenced again during
dispatch. A driver's actual "home base" plays zero role in any decision after minute 0.

**`sim/training/train_state_value_function.py`'s state representation** -- confirmed the trained
`V(s)` (the model actually loaded and driving both live scoring and the Simulation Showcase, via
`sim/engine/value_function.py`'s `make_value_fn()`) has this state:

```
STATE = (driver_region_id, driver_hos_remaining, hour_of_day, day_of_week,
         truck_pct_km_interval, truck_pct_days_interval)
```

Two real limitations for the question asked:
1. **No distance-to-hub / distance-to-home-terminal feature at all.** `region_id` (k-means over
   2,110 real locations) can only ever teach the model "this AREA tends to have good/bad real
   order demand" -- and the earlier hub-deadhead finding (`documents/logs/16`) already showed
   demand-dense areas are NOT the hubs themselves (a hub is a company terminal, not a shipper --
   landing there means driving back OUT to find real freight: Milton 37.0% real deadhead share,
   London 50.6%, both above the 27.3% network average). So even if the model perfectly learns
   `region_id`'s value, that value has nothing to do with proximity to base -- if anything the
   correlation runs backward.
2. **`hos_remaining` is a single blended MIN** across all 5 real HOS limits
   (`HOSState.remaining_hours`, `sim/engine/hos.py:133`) -- daily driving (13h), daily duty (14h),
   16h elapsed window, 70h/7-day cycle, 120h/14-day cycle. This collapses away exactly the
   distinction that matters for "stranded far from base": a driver near their DAILY limit but with
   plenty of WEEKLY cycle left can just rest and drive back tomorrow (not actually stranded); a
   driver near their WEEKLY CYCLE limit far from any hub genuinely has no real path back before a
   long forced reset. The model currently cannot tell these two situations apart.

**`sim/engine/value_function.py`'s `make_value_fn()`** -- the live/showcase scoring closure.
`E[V(s')] = (p_reload + p_dromt) * V(destination region) + p_deadhead * V(nearest hub region)`,
using `dynamic_post_completion_probs()`'s REAL, calibrated probabilities. This already knows,
probabilistically, whether a candidate is likely to end up needing a real deadhead leg back toward
a hub -- but it prices that outcome only via the LEARNED region value (itself demand-driven, per
above), never via an explicit "closer to home is worth more" term. Confirmed no such term exists.

## The concrete pattern described (worth building toward)

A real fleet expects drivers/trucks to end up reasonably near a terminal by the time HOS forces a
long reset -- not because every trip should go straight back to base (a strict "always return"
rule is a real efficiency loss vs. a smart multi-hop chain), but because ending a cycle stranded
far from any yard is a genuine operational cost this pipeline doesn't price at all today. The
user's own example: Milton -> London (normal outbound), then instead of one big empty return leg
Milton (100% loss), a chain like London -> Kitchener -> Mississauga -> ... progressively closes
the distance back toward Milton on REAL, REVENUE-PAYING legs -- the "return trip" gets partially
(not necessarily fully) paid for by real freight along the way. That's a genuinely different,
better-shaped objective than either extreme (ignore position entirely, which is today's behavior,
or force a literal return-to-base rule, which would throw away real revenue whenever a further-out
run is legitimately worth taking).

HOS hard-feasibility (`feasible_candidates()`'s legality filter) is untouched by any of this and
stays exactly as strict as it is today -- this is about the QUALITY of otherwise-legal choices,
not a new hard constraint (with one possible exception worth considering: a soft-but-strong
penalty specifically for ending a 7-day/14-day CYCLE far from any hub, which is a distinct,
currently-unpriced risk from the existing within-trip `hos_stranding_risk_penalty`).

## Handoff

Scoped out as its own session rather than done inline here -- real reward/feature-set surgery
across `reward.py`, both trained models, `extract_transitions.py`'s schema, and a retrain, with
its own real validation (paired significance test, same standard `documents/logs/17`/`18` already
set). See `documents/logs/NEW_SESSION_TRAINING_RETARGET_PROMPT.md`.
