-- Learning-to-rank needs GROUPS of candidates per order arrival, not just the one that won --
-- the simulator previously only ever recorded the dispatched candidate. Logs the top-N scored
-- candidates (by immediate_reward at decision time -- no GPU model in the hot loop) per arrival,
-- win-or-lose. Real outcomes only exist for the chosen one (sim.assignments); unchosen
-- candidates' training relevance is grounded in REAL empirical outcomes from elsewhere in the
-- dataset (feature-bucket averages over already-dispatched decisions with similar
-- region/hos/truck-risk profiles) -- not a model scoring itself. See
-- sim/training/train_ranker.py.
create table sim.candidate_scores (
  sim_id uuid,
  order_id uuid,
  driver_id integer,
  truck_number text,
  driver_location_id integer,   -- no FK: reference.locations lives in the remote DB
  driver_hos_remaining numeric,
  truck_breakdown_risk numeric,
  truck_pct_km_interval numeric,
  truck_pct_days_interval numeric,
  pre_pickup_deadhead_miles numeric,
  planned_driving_hours numeric,
  planned_duty_hours numeric,
  predicted_score numeric,      -- immediate_reward at decision time (compute_reward().total)
  rank_position integer,        -- 1 = best-scored candidate in this arrival's top-N
  was_chosen boolean
);
create index on sim.candidate_scores (sim_id, order_id);
