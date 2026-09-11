-- Threads next_truck_pct_km_interval/next_truck_pct_days_interval (sim/sql/027) through into
-- training_transitions -- V(s)'s new 5th/6th state feature, see train_state_value_function.py.
alter table training_transitions add column next_truck_pct_km_interval numeric;
alter table training_transitions add column next_truck_pct_days_interval numeric;
