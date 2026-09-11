-- MVP RBAC: a profiles table + RLS join, not a custom-claims Auth Hook (materially less setup
-- time for equivalent security guarantees at MVP scope -- research/roadstar_platform_plan.md's
-- implementation plan calls this out as the accepted fallback if time is tight).
create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id),
  role text check (role in ('manager','driver')) not null,
  driver_id integer references ground_truth.drivers   -- null for managers
);

alter table live.driver_status enable row level security;
alter table live.trips enable row level security;
alter table live.quote_requests enable row level security;
alter table live.quote_recommendations enable row level security;
alter table live.detention_billing enable row level security;
alter table live.vehicle_inspections enable row level security;
alter table live.truck_maintenance_state enable row level security;

-- Managers read everything in live.*
create policy "managers read all driver_status" on live.driver_status for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers read all trips" on live.trips for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers read all detention" on live.detention_billing for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers read all inspections" on live.vehicle_inspections for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers read all maintenance" on live.truck_maintenance_state for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers read all quotes" on live.quote_requests for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers read all recommendations" on live.quote_recommendations for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "managers insert quotes" on live.quote_requests for insert
  with check (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));

-- Drivers see only their own rows
create policy "drivers read own status" on live.driver_status for select
  using (driver_id = (select driver_id from public.profiles where user_id = auth.uid()));
create policy "drivers read own trips" on live.trips for select
  using (driver_id = (select driver_id from public.profiles where user_id = auth.uid()));
create policy "drivers read own inspections" on live.vehicle_inspections for select
  using (driver_id = (select driver_id from public.profiles where user_id = auth.uid()));
create policy "drivers submit own inspections" on live.vehicle_inspections for insert
  with check (driver_id = (select driver_id from public.profiles where user_id = auth.uid()));

-- Writes to driver_status/trips/geofence_events/quote_recommendations come only from the
-- simulator or the Vercel scoring function, using the service_role key, which bypasses RLS by
-- design -- no insert/update policy needed for authenticated roles on those tables.
