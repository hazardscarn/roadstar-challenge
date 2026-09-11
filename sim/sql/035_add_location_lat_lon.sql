-- PostGIS geography columns come back from PostgREST as WKB hex, not plain numbers -- every
-- frontend screen that needs a location's coordinates directly (this session's geofence panel,
-- and Orders/Trip History/Billing next) would otherwise need a WKB-parsing library client-side
-- or a backend round-trip just to read a point. Generated columns solve this once, for every
-- future screen, the same way live.detention_billing.billable_hours is already a generated
-- column rather than computed ad hoc per caller.
alter table reference.locations add column if not exists lat double precision generated always as (ST_Y(geog::geometry)) stored;
alter table reference.locations add column if not exists lon double precision generated always as (ST_X(geog::geometry)) stored;
