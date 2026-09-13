-- Real user ask: "Driver Assist" -- a driver-only view of today's assigned trips (from the real
-- Dispatch Board, dispatch.day_orders/assignments -- not the AI-dispatch REPLAY, which is a
-- simulated execution used for the manager demo, not a real per-driver worklist), with load
-- acceptance and duty-status logging (the real 4-status HOS framework: Off Duty, Sleeper Berth,
-- Driving, On-Duty Not Driving -- 49 CFR Part 395 / Canada's ELD Technical Standard).
--
-- Built under a hard 1-hour time limit: accessed only through dashboard/server/main.py's existing
-- service-role DB connection (sim/db.py), same pattern as every other dispatch.* table -- no new
-- RLS policies needed (dispatch.* already has RLS enabled with zero policies, so the browser's
-- anon/authenticated role already can't touch it directly; the backend verifies the driver's
-- identity from their Supabase auth token before ever running a query).

alter table dispatch.day_orders add column if not exists accepted_at timestamptz;

create table if not exists dispatch.duty_status_log (
  id bigserial primary key,
  day_id uuid references dispatch.days on delete cascade,
  driver_id integer references ground_truth.drivers not null,
  status text check (status in ('off_duty', 'sleeper_berth', 'driving', 'on_duty_not_driving')) not null,
  logged_at timestamptz not null default now(),
  odometer_km numeric,
  note text
);
create index if not exists duty_status_log_driver_day_idx on dispatch.duty_status_log (driver_id, day_id, logged_at desc);
