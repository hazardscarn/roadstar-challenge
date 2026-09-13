-- Day-ahead manual Dispatch Board (real hackathon-lead clarification: this is a day-before batch
-- dispatch tool, not a per-quote live-scoring one -- see documents/logs' dispatch-board session
-- for the full context). Three pieces:
--   1. Barrie as a real third terminal hub (was only an ASSUMED driver-home-hub anchor via a
--      proxy customer location -- RONA INC. (BARRIE), location_id 5719 -- until now).
--   2. calibration.truck_profile -- trucks have never had a type/capacity/home-hub anywhere in
--      the schema (ground_truth.trucks is a bare truck-number roster; the source Excel has
--      nothing else). The dispatch board needs real, differentiated truck entities.
--   3. The `dispatch` schema -- the day-ahead roster + plan, deliberately separate from `live`
--      (continuous, real-time, committed fleet state) and `sim` (batch simulation exhaust).

-- 1. Barrie hub -- same real-geocoded point already on file for this city (RONA INC. (BARRIE),
-- location_id 5719, the honest real anchor coordinate this project already uses for Barrie
-- elsewhere), promoted to a real terminal_hub row rather than left as a customer-location proxy.
insert into reference.locations (label, city, tier, geog, source, radius_m) values
  ('RoadStar Terminal — Barrie', 'Barrie', 'terminal_hub', ST_GeogFromText('POINT(-79.6901302 44.3893208)'), 'manual', 200)
on conflict (label) do nothing;

-- 2. calibration.truck_profile -- SYNTHESIZED (no real per-truck type/capacity/location data
-- exists, same honesty standard as every other calibration.* table -- see sim/config.py's own
-- CAPACITY_BY_LOAD_TYPE header comment). Seeded by sim/calibrate_truck_profile.py: type mix
-- drawn proportional to real historical load_type frequency, capacity/dimensions centered on the
-- existing CAPACITY_BY_LOAD_TYPE/CAPACITY_PALLETS_BY_LOAD_TYPE constants with small real-looking
-- variance, home hub drawn from sim/hub_weights.py's real-London/Milton-ratio-plus-20%-Barrie
-- weighting (NOT an even 3-way split -- real user feedback).
create table if not exists calibration.truck_profile (
  truck_number text primary key references ground_truth.trucks(truck_number),
  truck_type text not null check (truck_type in ('Dry Van', 'Reefer', 'Flatbed')),
  capacity_lbs numeric not null,
  capacity_pallets integer not null,
  length_ft numeric not null,
  inside_height_ft numeric not null,
  width_in numeric not null,
  home_hub_location_id integer not null references reference.locations(location_id),
  computed_at timestamptz not null default now()
);

-- 3. `dispatch` schema -- the day-ahead roster + plan a fleet manager builds the day before.
create schema if not exists dispatch;

create table if not exists dispatch.days (
  id uuid primary key default gen_random_uuid(),
  service_date date not null unique,
  status text not null default 'draft' check (status in ('draft', 'finalized')),
  created_at timestamptz not null default now(),
  finalized_at timestamptz
);

create table if not exists dispatch.day_trucks (
  day_id uuid not null references dispatch.days(id) on delete cascade,
  truck_number text not null references ground_truth.trucks(truck_number),
  available boolean not null default true,
  unavailable_reason text,
  primary key (day_id, truck_number)
);

create table if not exists dispatch.day_drivers (
  day_id uuid not null references dispatch.days(id) on delete cascade,
  driver_id integer not null references ground_truth.drivers(driver_id),
  available boolean not null default true,
  unavailable_reason text,
  hos_driving_hours_remaining numeric not null,
  hos_duty_hours_remaining numeric not null,
  hos_cycle1_hours_remaining numeric not null,
  hos_cycle2_hours_remaining numeric not null,
  primary key (day_id, driver_id)
);

create table if not exists dispatch.day_orders (
  id uuid primary key default gen_random_uuid(),
  day_id uuid not null references dispatch.days(id) on delete cascade,
  pickup_location_id integer not null references reference.locations(location_id),
  dest_location_id integer not null references reference.locations(location_id),
  weight_lbs numeric not null,
  pallets integer not null,
  load_type text not null check (load_type in ('Dry Van', 'Reefer', 'Flatbed')),
  pickup_window_start timestamptz not null,
  pickup_window_end timestamptz not null,
  rate numeric not null
);

-- One row per (day, truck) -- mirrors the Claude Design mock's own in-memory state shape exactly
-- (`assignments[truckId] = {driverId, orderIds}`, Dispatch Board.dc.html's initAssignments()), so
-- the board's UI state maps directly onto a DB row instead of being reinvented. order_ids
-- references dispatch.day_orders.id, not enforced via FK (array column -- app-level integrity,
-- same cross-boundary tradeoff sim.* already accepts for its own array/id references).
create table if not exists dispatch.assignments (
  day_id uuid not null references dispatch.days(id) on delete cascade,
  truck_number text not null references ground_truth.trucks(truck_number),
  driver_id integer references ground_truth.drivers(driver_id),
  order_ids uuid[] not null default '{}',
  primary key (day_id, truck_number)
);

create index on dispatch.day_trucks (day_id);
create index on dispatch.day_drivers (day_id);
create index on dispatch.day_orders (day_id);
create index on dispatch.assignments (day_id);

-- RLS enabled, deliberately NO policies -- every dispatch.* read/write goes through
-- dashboard/server/main.py's new /api/dispatch/* endpoints (sim/db.py's direct SUPABASE_DB_URL
-- superuser connection, which bypasses RLS entirely), never straight from the browser via
-- supabase-js/PostgREST. Same "no insert/update policy needed for authenticated roles" reasoning
-- 009_rls.sql already documents for live.driver_status/trips -- RLS-enabled-with-zero-policies
-- means anon/authenticated get nothing, which is exactly right here: nothing needs it. Not
-- exposing `dispatch` via pgrst.db_schemas either (sim/sql/032/034's pattern), for the same
-- reason -- PostgREST access isn't part of this build.
alter table dispatch.days enable row level security;
alter table dispatch.day_trucks enable row level security;
alter table dispatch.day_drivers enable row level security;
alter table dispatch.day_orders enable row level security;
alter table dispatch.assignments enable row level security;
