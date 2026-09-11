-- Real user feedback on the Live Ops build: the dispatcher table view needs speed/mileage/fuel,
-- and none of odometer/fuel exist anywhere in live.driver_status yet. speed_mph already existed
-- (sim/sql/006_live.sql) but nothing populated it. Odometer and fuel are NECESSARILY synthesized
-- -- no real telemetry source exists anywhere in this project's data (same honesty standard as
-- live.truck_maintenance_state's own header comment) -- sim/live/telemetry_simulator.py is what
-- actually drives these going forward: fuel depletes with distance driven and refills at a hub
-- dwell, odometer accumulates real driven distance from the OSRM route geometry.
alter table live.driver_status add column if not exists odometer_km numeric;
alter table live.driver_status add column if not exists fuel_pct numeric default 100 check (fuel_pct between 0 and 100);
