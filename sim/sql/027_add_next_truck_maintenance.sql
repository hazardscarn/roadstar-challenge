-- V(s)'s feature set gains a truck-condition dimension -- see sim/training/train_state_value_function.py's
-- docstring on why "truck condition" was originally excluded (it's specific to whichever candidate
-- action gets scored, not a stable property of position) and why THIS is different: the truck that
-- rides forward with a driver into their NEXT trip is the SAME truck (drivers/trucks aren't swapped
-- mid-trip -- see run_sim.py's module docstring), so post-trip truck-maintenance % genuinely IS part
-- of "the state you'll be in," not a future-order guess the way order-specific distance would be.
alter table sim.assignments add column next_truck_pct_km_interval numeric;
alter table sim.assignments add column next_truck_pct_days_interval numeric;
