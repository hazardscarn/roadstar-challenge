-- sim.assignments only stored immediate_margin (the reward AT THE MOMENT OF ASSIGNMENT,
-- compute_reward().total) -- it never captured the REALIZED reward once a trip actually
-- finished (post_delivery_deadhead_cost() + realized_breakdown_penalty() subtracted in), even
-- though sim/engine/run_sim.py computes that number correctly in memory
-- (CompletedTrip.reward_total). Found while previewing what a flattened training-transitions
-- table would look like -- training a value function on immediate_margin alone would teach it
-- to ignore exactly the outcome (deadhead, breakdowns) it's supposed to learn to avoid.
alter table sim.assignments add column reward_total numeric;
