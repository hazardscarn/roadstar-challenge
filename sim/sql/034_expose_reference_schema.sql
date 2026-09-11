-- reference.locations is non-sensitive (facility names/coordinates, the same data already
-- served by dashboard/server's /api/locations) -- exposing it directly lets the frontend resolve
-- origin/dest labels for quote_requests/trips rows itself (the Dispatch Feed, Orders, Trip
-- History views all need this) instead of a bespoke join view or a backend round-trip per row.
alter role authenticator set pgrst.db_schemas = 'public, live, reference, graphql_public';
notify pgrst, 'reload config';

grant usage on schema reference to authenticated;
grant select on all tables in schema reference to authenticated;
alter default privileges in schema reference grant select on tables to authenticated;

notify pgrst, 'reload schema';
