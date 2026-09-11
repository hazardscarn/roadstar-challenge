-- Shape must match what live.* can actually supply at real quote time (see the ordering note
-- in research/roadstar_platform_plan.md Section 3 -- 006_live.sql is applied before this file
-- is finalized, deliberately, so the model never trains on a convenience column live data can't
-- produce).
create table training_transitions (
  sim_id uuid,
  decision_time timestamptz,
  driver_id integer,
  driver_position geography(Point, 4326),
  driver_hos_remaining numeric,
  driver_jurisdiction text,
  driver_duty_status text,
  truck_number text,
  trailer_type text,
  trailer_capacity_lbs integer,
  trailer_capacity_pallets integer,
  order_id uuid,
  order_weight numeric,
  order_load_type text,
  order_revenue numeric,
  deadhead_m_to_pickup numeric,
  load_fill_ratio numeric,
  opportunity_cost_penalty numeric,
  deadhead_cost numeric,
  lateness_risk_penalty numeric,
  hour_of_day integer,
  day_of_week integer,
  action_taken text check (action_taken in ('accepted','declined','assigned_to_other')),
  was_exploration boolean,
  immediate_reward numeric,
  next_driver_position geography(Point, 4326),
  next_driver_hos_remaining numeric,
  target_value numeric   -- filled post-hoc, sim/training/extract_transitions.py
);
create index on training_transitions (sim_id, driver_id, decision_time);
