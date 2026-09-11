-- Real gap found while wiring the Trip History page: live.trip_log, live.trip_costs,
-- live.invoices, and the live.driver_ratings/truck_ratings VIEWS have RLS disabled entirely --
-- checked directly (pg_class.relrowsecurity = false on all five). Combined with sim/sql/032's
-- broad `grant select on all tables in schema live to authenticated`, this means ANY logged-in
-- user (a future driver account included) can currently read every driver's full trip history,
-- internal margin data, and invoices via a direct PostgREST call -- exactly the cross-driver
-- leak sim/sql/009_rls.sql was written to prevent, just on tables added after it.

alter table live.trip_log enable row level security;
create policy "managers read all trip_log" on live.trip_log for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "drivers read own trip_log" on live.trip_log for select
  using (driver_id = (select driver_id from public.profiles where user_id = auth.uid()));

-- Internal margin/billing data -- manager-only, no driver policy at all (live.trip_costs' own
-- header comment: "NOT shown to the shipper" -- doubly not shown to a driver).
alter table live.trip_costs enable row level security;
create policy "managers read all trip_costs" on live.trip_costs for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));

alter table live.invoices enable row level security;
create policy "managers read all invoices" on live.invoices for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));

-- driver_ratings/truck_ratings are VIEWS over trip_log -- by default a view runs with the
-- DEFINER's privileges, bypassing the querying role's RLS entirely (Postgres views are
-- security-definer-like unless told otherwise). security_invoker makes them respect the
-- CALLER's RLS instead, so they inherit trip_log's new policies automatically rather than
-- silently re-opening the same leak one layer up.
alter view live.driver_ratings set (security_invoker = on);
alter view live.truck_ratings set (security_invoker = on);
