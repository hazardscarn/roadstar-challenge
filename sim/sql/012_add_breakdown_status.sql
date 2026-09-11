-- sim.trip_events' event_type CHECK constraint predates BREAKDOWN (added to
-- sim/engine/state.py's TripStatus enum later, in the reward/risk-factors segment) -- the sim
-- engine's own event log can't record a breakdown it just simulated without this.
alter table sim.trip_events drop constraint trip_events_event_type_check;
alter table sim.trip_events add constraint trip_events_event_type_check
  check (event_type in
    ('ASSGN','DISP','ARRSHIP','SPTLD','DOCKED','PICKD','DEPSHIP','STOPOFF','ARRCONS','DROMT','COMPLETE','BREAKDOWN'));
