-- Seeded by sim/build_calibration.py -- known-good numbers already computed in
-- analysis/data_analysis.ipynb / analysis/driver-analysis.ipynb are inserted with a citation
-- comment, not recomputed from scratch. See sim/build_calibration.py for which tables are
-- freshly computed (lane_frequency, order_arrival_rate -- these need reference.locations,
-- which didn't exist when the notebooks were written) vs seeded verbatim.

create table calibration.run_type_transition (
  from_run_type text,
  to_run_type text,
  probability numeric,
  primary key (from_run_type, to_run_type)
);

create table calibration.lane_frequency (
  origin_location_id integer references reference.locations,
  dest_location_id integer references reference.locations,
  weight numeric,
  primary key (origin_location_id, dest_location_id)
);

create table calibration.order_arrival_rate (
  hour_of_day integer,
  day_of_week integer,
  lambda numeric,                  -- Poisson rate for order-generation
  primary key (hour_of_day, day_of_week)
);

create table calibration.dwell_time_dist (
  run_type text,
  phase text check (phase in ('pickup','delivery')),
  p25_minutes numeric,
  median_minutes numeric,
  p75_minutes numeric,
  primary key (run_type, phase)
);

create table calibration.hos_remaining_at_completion (
  run_type text primary key,
  median_hours numeric
);

-- Route cache, populated from OSRM (see ops/osrm_setup.sh, sim/osrm_cache.py)
create table calibration.lane_routes (
  origin_location_id integer references reference.locations,
  dest_location_id integer references reference.locations,
  distance_m numeric,
  duration_s numeric,
  geometry jsonb,                  -- GeoJSON LineString from OSRM
  computed_at timestamptz default now(),
  primary key (origin_location_id, dest_location_id)
);

-- Every synthesized $-rate assumption lives here, ONE place, read at runtime by the reward
-- function, both backtest scripts, the Vercel inference function, and every dashboard $-figure
-- component -- never hardcoded a second time anywhere else. See sim/config.py for the values
-- and the "why" (no price/revenue field exists anywhere in the source data).
create table calibration.assumptions (
  key text primary key,
  value numeric,
  unit text,
  rationale text,
  is_assumption boolean default true
);
