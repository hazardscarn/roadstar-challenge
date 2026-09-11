-- More feature-richness additions, all requested directly: a second (calendar-time) maintenance
-- dimension alongside the existing odometer one (a truck driven lightly but sitting since a
-- long-ago service is a real risk case the km-only model completely missed), the driver's total
-- committed distance for a decision (not just the deadhead leg), how many trucks a driver
-- regularly has to choose from, and local historical demand near a delivery point (a sharper
-- reload-likelihood signal than raw hub-distance alone).
alter table sim.orders add column dest_local_order_density numeric;

alter table sim.assignments add column total_committed_distance_miles numeric;
alter table sim.assignments add column driver_pool_size integer;
alter table sim.assignments add column truck_pct_km_interval numeric;
alter table sim.assignments add column truck_pct_days_interval numeric;

alter table training_transitions add column dest_local_order_density numeric;
alter table training_transitions add column total_committed_distance_miles numeric;
alter table training_transitions add column driver_pool_size integer;
alter table training_transitions add column truck_pct_km_interval numeric;
alter table training_transitions add column truck_pct_days_interval numeric;
