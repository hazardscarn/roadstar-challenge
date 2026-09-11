-- NOTE: driver_id/truck_number/location_id columns here intentionally have NO foreign key
-- constraint -- reference.locations and ground_truth.drivers/trucks live in the REMOTE
-- Supabase database (see sim/db.py's module docstring), and Postgres cannot enforce a foreign
-- key across two separate database instances. The sim engine loads ground_truth/reference data
-- from remote into memory once at startup (sim/engine/run_sim.py) and only ever writes IDs it
-- got from that source, so referential integrity is guaranteed by the application, not the DB.

create table sim.runs (
  sim_id uuid primary key default gen_random_uuid(),
  seed integer,
  epsilon_start numeric,            -- decaying epsilon-greedy exploration, see sim/engine/policy.py
  epsilon_end numeric,
  started_at timestamptz default now(),
  config_json jsonb
);

create table sim.orders (
  sim_id uuid references sim.runs,
  order_id uuid default gen_random_uuid(),
  origin_geog geography(Point, 4326),
  dest_geog geography(Point, 4326),
  origin_location_id integer,       -- reference.locations.location_id (remote), nullable
  dest_location_id integer,
  created_at timestamptz,
  pickup_window_start timestamptz,
  pickup_window_end timestamptz,
  delivery_window_start timestamptz,
  delivery_window_end timestamptz,
  weight_lbs integer,
  pallets integer,
  load_type text,
  run_type_hint text,
  revenue numeric,                  -- from calibration.assumptions rate, see sim/engine/reward.py
  status text default 'open',
  primary key (sim_id, order_id)
);

create table sim.assignments (
  sim_id uuid references sim.runs,
  order_id uuid,
  driver_id integer,                -- ground_truth.drivers.driver_id (remote)
  truck_number text,                -- ground_truth.trucks.truck_number (remote)
  assigned_at timestamptz,
  was_exploration boolean,          -- true if the epsilon-greedy draw picked this over the greedy choice
  immediate_margin numeric,
  deadhead_miles numeric,
  load_fill_ratio numeric,          -- max(weight_ratio, pallet_ratio)
  opportunity_cost_penalty numeric,
  primary key (sim_id, order_id)
);

create table sim.hos_transitions (
  sim_id uuid references sim.runs,
  driver_id integer,
  event_time timestamptz,
  duty_status text check (duty_status in ('DRIVING','ON_DUTY_NOT_DRIVING','OFF_DUTY')),
  hos_remaining_hours numeric
);
create index on sim.hos_transitions (sim_id, driver_id, event_time);

create table sim.trip_events (
  sim_id uuid references sim.runs,
  trip_id uuid,
  leg_seq integer,
  driver_id integer,
  event_type text check (event_type in
    ('ASSGN','DISP','ARRSHIP','SPTLD','DOCKED','PICKD','DEPSHIP','STOPOFF','ARRCONS','DROMT','COMPLETE')),
  event_time timestamptz,
  location_geog geography(Point, 4326)
);
create index on sim.trip_events (sim_id, driver_id, event_time);

-- Synthetic GPS breadcrumb trail -- interpolated along cached OSRM route geometry between
-- scheduled events, not its own event type. Bulk-inserted per completed run (see
-- sim/engine/run_sim.py note on avoiding per-tick network round-trips).
create table sim.position_ticks (
  sim_id uuid references sim.runs,
  tick_time timestamptz,
  driver_id integer,
  truck_number text,
  position geography(Point, 4326),
  speed_mph numeric,
  hos_remaining_hours numeric,
  duty_status text,
  current_trip_id uuid,
  current_leg_id integer,
  distance_covered_this_leg_m numeric,
  primary key (sim_id, driver_id, tick_time)
);
create index on sim.position_ticks (sim_id, tick_time);
