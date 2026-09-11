-- sim.assignments captured the OUTCOME of a decision (deadhead_miles, reward_total) but never
-- the driver's STATE at the moment the decision was made -- position and remaining HOS hours,
-- exactly the features training_transitions.driver_position/driver_hos_remaining expect
-- (sim/sql/007_training_transitions.sql), and arguably the most decision-relevant signal a
-- dispatch value function needs (a driver who's far away and nearly out of hours is a much
-- riskier pick than one nearby with a full day left, even at identical immediate reward).
-- Found while designing extract_transitions.py -- same class of gap as reward_total (013).
alter table sim.assignments add column driver_location_id integer;  -- no FK: reference.locations lives in the remote DB
alter table sim.assignments add column driver_hos_remaining numeric;
