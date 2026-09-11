-- Captures the real next-state for every assignment decision: where the driver/truck physically
-- end up, their HOS remaining, and when they're next available -- needed for Fitted Value
-- Iteration (V_{k+1}(s) = realized_reward + gamma * V_k(next_state)). Previously left null
-- (training_transitions.next_driver_position/next_driver_hos_remaining, sim/sql/007) because the
-- committed single-pass approach didn't need them; now building the real state-value function.
alter table sim.assignments add column next_location_id integer;  -- no FK: reference.locations lives in the remote DB
alter table sim.assignments add column next_hos_remaining numeric;
alter table sim.assignments add column next_available_at timestamptz;
