create table live.driver_status (
  driver_id integer primary key references ground_truth.drivers,
  updated_at timestamptz default now(),
  position geography(Point, 4326),
  last_location_id integer references reference.locations,   -- set when currently within a geofence
  speed_mph numeric,
  hos_remaining_hours numeric,
  jurisdiction text,                  -- 'U' / 'C'
  duty_status text,
  current_trip_id uuid,
  truck_number text references ground_truth.trucks,
  trailer_type text,                  -- denormalized from driver_equipment for scoring speed
  trailer_capacity_lbs integer,
  trailer_capacity_pallets integer
);

create table live.trips (
  trip_id uuid primary key default gen_random_uuid(),
  driver_id integer references ground_truth.drivers,
  status text,
  last_event text,
  eta timestamptz
);

create table live.geofence_events (
  event_id bigserial primary key,
  trip_id uuid references live.trips,
  location_id integer references reference.locations,
  event_type text check (event_type in ('arrival','departure')),
  occurred_at timestamptz default now()
);

-- State machine for the buffered geofence function (research/roadstar_platform_plan.md Section 1.2)
create table live.geofence_dwell_state (
  trip_id uuid references live.trips,
  location_id integer references reference.locations,
  driver_id integer references ground_truth.drivers,
  inside_since timestamptz,
  outside_since timestamptz,
  arrived_at timestamptz,
  departed_at timestamptz,
  primary key (trip_id, location_id)
);

create table live.detention_billing (
  trip_id uuid primary key references live.trips,
  arrival_at timestamptz,
  departure_at timestamptz,
  free_hours numeric default 2,
  billable_hours numeric generated always as
    (greatest(0, extract(epoch from (departure_at - arrival_at)) / 3600.0 - 2)) stored,
  amount numeric
);

create table live.quote_requests (
  quote_id uuid primary key default gen_random_uuid(),
  origin_geog geography(Point, 4326),
  dest_geog geography(Point, 4326),
  requested_at timestamptz default now(),
  weight_lbs integer,
  pallets integer,
  load_type text
);

create table live.quote_recommendations (
  quote_id uuid references live.quote_requests,
  rank integer,
  driver_id integer references ground_truth.drivers,
  expected_revenue numeric,
  expected_margin numeric,
  deadhead_miles numeric,
  hos_feasible boolean,
  eta_pickup timestamptz,
  model_version text,
  primary key (quote_id, rank)
);

-- Driver pre-trip inspection. overall_pass=false should block that driver from the candidate
-- filter in the quote-scoring query (dashboard/api/score-quote.py) until a passing inspection
-- is on file within the last 24h.
create table live.vehicle_inspections (
  inspection_id uuid primary key default gen_random_uuid(),
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  submitted_at timestamptz default now(),
  odometer_km numeric,
  brakes_ok boolean, tires_ok boolean, lights_ok boolean,
  fluid_levels_ok boolean, coupling_ok boolean, trailer_ok boolean,
  defects_noted text,
  overall_pass boolean generated always as
    (brakes_ok and tires_ok and lights_ok and fluid_levels_ok and coupling_ok and trailer_ok) stored
);

-- Maintenance warning: no real odometer/service history exists in the source data, so this is
-- a synthesized rule, not a backtested one -- state that plainly in the demo.
create table live.truck_maintenance_state (
  truck_number text primary key references ground_truth.trucks,
  cumulative_km_since_service numeric default 0,
  last_service_at timestamptz,
  service_interval_km numeric default 25000,
  service_interval_days integer default 90
);
