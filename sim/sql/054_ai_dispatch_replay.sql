-- Real user ask: replace the Simulation Showcase page's old ML/RL-trained-policy week-long batch
-- sim with a full-day, multi-truck replay of whatever the AI (CP-SAT) dispatcher actually decided
-- for one real day -- sim/live/ai_dispatch_replay.py. Separate table from simulation.trips (that
-- one's shape is tied to the old per-order-quote/reload-decision batch sim -- run_type, quote_id,
-- reload_immediate, etc. -- none of which apply here; this is a one-row-per-dispatched-order
-- replay of an already-decided plan, not a decision-by-decision simulation).

alter table simulation.runs drop constraint runs_run_kind_check;
alter table simulation.runs add constraint runs_run_kind_check
  check (run_kind in ('batch', 'trip_demo', 'ai_dispatch_day'));

create table simulation.ai_dispatch_trips (
  trip_id uuid primary key,
  run_id uuid not null references simulation.runs(run_id) on delete cascade,
  driver_id integer not null references ground_truth.drivers(driver_id),
  truck_number text not null references ground_truth.trucks(truck_number),
  hub_city text,
  assigned_at_s numeric not null,
  completed_at_s numeric not null,
  arr_pickup_at_s numeric,
  dep_pickup_at_s numeric,
  arr_delivery_at_s numeric,
  origin_location_id integer references reference.locations(location_id),
  dest_location_id integer references reference.locations(location_id),
  origin_label text,
  dest_label text,
  weight_lbs numeric,
  pallets integer,
  load_type text,
  order_revenue numeric,
  deadhead_cost numeric,
  deadhead_miles numeric,
  net_margin numeric,
  detention_amount numeric default 0,
  is_detention_demo boolean not null default false,
  trajectory jsonb not null default '[]'  -- [t_offset_s, lat, lon, speed_mph, fuel_pct][]
);

create index on simulation.ai_dispatch_trips (run_id);

alter table simulation.ai_dispatch_trips enable row level security;
-- Same "zero policies, manager-only via SUPABASE_DB_URL" reasoning as every other live/simulation
-- table (009_rls.sql) -- nothing needs direct browser access, everything goes through the API.
