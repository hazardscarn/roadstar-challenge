-- REAL empirical order lead-time samples (CREATED_TIME -> ACTUAL_PICKUP, real Tlorder ON-ON
-- orders with both timestamps present and non-negative gap -- 1,643 of 2,109 orders qualify).
-- Bootstrap-sampled from directly at sim runtime (sim/engine/run_sim.py's generate_order()),
-- same pattern as order_pool's weight/pallets/load_type bootstrap -- real observed values, not a
-- fitted parametric shape. See documents/logs/17 for the full distribution (median 44h/~1.8
-- days, p90 145h/~6 days, p99 256h/~11 days) that replaced the old "order arrives = must dispatch
-- now" assumption.
create table calibration.order_lead_time_hours (
  id serial primary key,
  lead_hours numeric not null
);
