-- Home-time retarget (documents/logs/25-26): closes the real gap documents/logs/26 reported --
-- the business "hours since home" signal was shaping rewards, but the trained V(s) model never
-- had DIRECT visibility into it, only its indirect effect via realized reward. Mirrors
-- distance_to_home_miles/distance_to_home_miles_landing (sim/sql/041) exactly: hours_since_home
-- is the decision-time state, hours_since_home_landing doubles as this transition's NEXT-state
-- value (the driver's business-cadence clock right after this trip -- 0.0 once landing AT home),
-- same reasoning distance_to_home_miles_landing already established -- no separate
-- next_hours_since_home column needed.

alter table sim.assignments add column hours_since_home numeric;
alter table sim.assignments add column hours_since_home_landing numeric;

-- Mirror onto training_transitions -- the flattened table the models actually train on.
alter table training_transitions add column hours_since_home numeric;
alter table training_transitions add column hours_since_home_landing numeric;
