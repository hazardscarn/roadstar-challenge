-- Extends live.* so a live in-transit driver can be scored as a REAL candidate the same way
-- documents/logs/16-19's sim work already does (effective_driver_state()'s projected next-state)
-- -- without these, live quote scoring could only ever consider currently-idle drivers, a real
-- regression versus what the simulator itself proved out tonight.
--
-- Why these specific columns: a trip's own known route/plan already determines exactly where
-- and when a driver lands (same reasoning as sim.assignments.next_location_id/next_hos_remaining,
-- sim/sql/019/027) -- these are PROJECTIONS from the trip's own plan, not live GPS telemetry, so
-- they're computed once when a trip is assigned/updated, not on every position tick.
alter table live.trips add column dest_location_id integer references reference.locations;
alter table live.trips add column projected_hos_remaining_hours numeric;
alter table live.trips add column projected_truck_pct_km_interval numeric;
alter table live.trips add column projected_truck_pct_days_interval numeric;

-- Mirrors sim/engine/maintenance.py's TruckMaintenanceState.maintenance_until (documents/logs/18's
-- proactive-maintenance fix) -- a truck currently in the shop is unavailable as a candidate,
-- exactly like the sim's own TruckState.maintenance_until check inside pick_pool_truck().
alter table live.truck_maintenance_state add column maintenance_until timestamptz;
