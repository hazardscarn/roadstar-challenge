-- Real gap found while wiring the frontend (not caught by 009_rls.sql, which only defined
-- POLICIES): PostgREST only exposes schemas listed in the `authenticator` role's
-- `pgrst.db_schemas` setting, and RLS policies are moot without a GRANT giving the role table
-- access in the first place -- checked directly, `live` had ZERO grants to anon/authenticated and
-- wasn't in the exposed-schema list at all, so supabase-js couldn't reach live.* regardless of
-- how correct the policies were. `public` worked without this because Supabase provisions those
-- grants automatically for the default schema; `live` is this project's own schema and never got
-- the equivalent.

alter role authenticator set pgrst.db_schemas = 'public, live, graphql_public';
notify pgrst, 'reload config';

grant usage on schema live to authenticated;

-- Broad SELECT -- RLS (009_rls.sql, 031_add_ui_support_columns.sql) is what actually restricts
-- rows per role/driver, this GRANT just lets the authenticated role reach the table at all.
grant select on all tables in schema live to authenticated;
alter default privileges in schema live grant select on tables to authenticated;

-- The one table a client genuinely writes to directly (driver's own DVIR submission) -- everything
-- else (assign, invoicing, telemetry) goes through dashboard/server's direct SUPABASE_DB_URL
-- connection, matching sim/db.py's existing pattern, not through PostgREST.
grant insert on live.vehicle_inspections to authenticated;

-- Realtime: Supabase only streams postgres changes for tables added to this publication.
-- Driver/trip movement, new recommendations, and geofence/detention events are what the map and
-- quote panel subscribe to (research/roadstar_platform_plan.md Section 8.1).
alter publication supabase_realtime add table live.driver_status;
alter publication supabase_realtime add table live.trips;
alter publication supabase_realtime add table live.quote_recommendations;
alter publication supabase_realtime add table live.geofence_events;
alter publication supabase_realtime add table live.detention_billing;
