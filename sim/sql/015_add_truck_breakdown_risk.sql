-- Found training the first value function: truck breakdown risk (TruckMaintenanceState at
-- decision time) was never captured anywhere -- up to $8,000 of a trip's realized outcome
-- (realized_breakdown_penalty) was completely invisible to the model as a result. Added to both
-- sim.assignments (what run_sim.py writes per decision) and training_transitions (what the model
-- trains on) so overdue-truck risk becomes a learnable feature instead of unexplained variance.
alter table sim.assignments add column truck_breakdown_risk numeric;
alter table training_transitions add column truck_breakdown_risk numeric;
