-- Dedicated `simulation` schema -- see plan encapsulated-swinging-naur.md. A purpose-built mirror
-- of the live.* tables the week-long Simulation Showcase needs, NOT a tagged/shared copy of
-- live.* rows: live.driver_status and every other live table are never written to by a showcase
-- run, and the Showcase gets its own dedicated view instead of reusing Billing/Trip History.
-- Where live's own shape exists only to handle real-world messiness the simulation doesn't have
-- (noisy GPS needing debounce, arbitrary manager-typed addresses), it's simplified rather than
-- blindly copied -- noted inline below.

create schema if not exists simulation;

-- One header row per showcase run -- lets multiple runs be kept and compared side by side, and is
-- what the frontend lists/re-reads to know a run exists.
create table simulation.runs (
  run_id uuid primary key default gen_random_uuid(),
  seed integer,
  created_at timestamptz default now(),
  week_start timestamptz,
  week_end timestamptz,
  driver_ids integer[],
  n_orders_generated integer,
  n_completed integer,
  n_unassigned integer,
  total_reward numeric,
  total_revenue numeric,
  total_detention_billed numeric,
  total_invoiced numeric,
  deadhead_avoided_miles numeric,   -- naive-nearest-truck-dispatch baseline comparison (see plan decision 7)
  deadhead_avoided_value numeric,
  status text default 'complete'
);

-- No origin_geog/dest_geog: unlike live (a manager can type an arbitrary new address), every
-- simulated order is drawn from a known reference.locations row.
create table simulation.quote_requests (
  quote_id uuid primary key default gen_random_uuid(),
  run_id uuid references simulation.runs on delete cascade,
  origin_location_id integer references reference.locations,
  dest_location_id integer references reference.locations,
  requested_at timestamptz,      -- the quote time
  requested_pickup_at timestamptz,
  weight_lbs numeric,
  pallets numeric,
  load_type text,
  service_type text,
  status text check (status in ('open','assigned','expired','cancelled'))
);

create table simulation.quote_recommendations (
  run_id uuid references simulation.runs on delete cascade,
  quote_id uuid references simulation.quote_requests,
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

-- The persisted form of run_sim.py's candidate_score_rows -- THE "feature state of this
-- driver/truck for the best candidate score" audit trail: every feasible candidate considered per
-- order, not just the winner.
create table simulation.quote_candidate_snapshots (
  id bigserial primary key,
  run_id uuid references simulation.runs on delete cascade,
  quote_id uuid references simulation.quote_requests,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  location_id integer references reference.locations,
  hos_remaining_hours numeric,
  truck_breakdown_risk numeric,
  truck_pct_km_interval numeric,
  truck_pct_days_interval numeric,
  deadhead_miles numeric,
  planned_driving_hours numeric,
  planned_duty_hours numeric,
  score numeric,
  rank integer,
  was_assigned boolean,
  scored_at timestamptz
);
create index on simulation.quote_candidate_snapshots (quote_id);
create index on simulation.quote_candidate_snapshots (run_id);

create table simulation.trips (
  trip_id uuid primary key,
  run_id uuid references simulation.runs on delete cascade,
  driver_id integer references ground_truth.drivers,
  status text,
  last_event text,
  eta timestamptz,
  origin_location_id integer references reference.locations,
  dest_location_id integer references reference.locations,
  created_at timestamptz,
  weight_lbs numeric,
  pallets numeric,
  load_type text,
  loaded_miles numeric,
  pre_pickup_deadhead_miles numeric,
  quote_id uuid references simulation.quote_requests
);

create table simulation.trip_log (
  trip_id uuid primary key references simulation.trips,
  run_id uuid references simulation.runs on delete cascade,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  completed_at timestamptz,
  loaded_miles numeric,
  pre_pickup_deadhead_miles numeric,
  post_delivery_deadhead_miles numeric,
  pickup_dwell_hours numeric,
  delivery_dwell_hours numeric,
  on_time boolean,
  load_fill_ratio numeric,
  hos_stranding_risk numeric,
  breakdown_occurred boolean default false,
  breakdown_repair_hours numeric default 0,
  reward_total numeric
);

-- No geofence_dwell_state mirror: that table exists live only to debounce noisy real GPS ticks.
-- The discrete-event sim already knows each trip's exact ARRSHIP/DEPSHIP/ARRCONS instant, so
-- events are written directly.
create table simulation.geofence_events (
  event_id bigserial primary key,
  run_id uuid references simulation.runs on delete cascade,
  trip_id uuid references simulation.trips,
  location_id integer references reference.locations,
  event_type text check (event_type in ('arrival','departure')),
  occurred_at timestamptz
);

-- PER LEG, (trip_id, location_id) pk -- an actual improvement over live.detention_billing's
-- trip-only pk, which conflates pickup-leg and delivery-leg dwell into one figure (a real,
-- pre-existing gap in the live cron job, sim/sql/010, not propagated here).
create table simulation.detention_billing (
  trip_id uuid references simulation.trips,
  location_id integer references reference.locations,
  run_id uuid references simulation.runs on delete cascade,
  arrival_at timestamptz,
  departure_at timestamptz,
  free_hours numeric default 2,
  billable_hours numeric generated always as
    (greatest(0, extract(epoch from (departure_at - arrival_at)) / 3600.0 - 2)) stored,
  amount numeric,
  primary key (trip_id, location_id)
);

create table simulation.invoices (
  invoice_id uuid primary key default gen_random_uuid(),
  run_id uuid references simulation.runs on delete cascade,
  invoice_number text,
  trip_id uuid references simulation.trips,
  quote_id uuid references simulation.quote_requests,
  issued_at timestamptz,
  due_at timestamptz,
  bill_to_name text,
  bill_to_address text,
  delivery_province text,
  linehaul_amount numeric,
  detention_amount numeric,
  fuel_surcharge_amount numeric default 0,
  accessorial_amount numeric default 0,
  subtotal numeric generated always as
    (coalesce(linehaul_amount,0) + coalesce(detention_amount,0) + coalesce(fuel_surcharge_amount,0) + coalesce(accessorial_amount,0)) stored,
  tax_rate numeric default 0.13,
  tax_amount numeric generated always as
    ((coalesce(linehaul_amount,0) + coalesce(detention_amount,0) + coalesce(fuel_surcharge_amount,0) + coalesce(accessorial_amount,0)) * tax_rate) stored,
  total_amount numeric generated always as
    ((coalesce(linehaul_amount,0) + coalesce(detention_amount,0) + coalesce(fuel_surcharge_amount,0) + coalesce(accessorial_amount,0)) * (1 + tax_rate)) stored,
  status text default 'draft'
);

-- Final post-week state per truck per run (not a timeseries -- driver_state_snapshots below
-- already carries pct_km_interval/pct_days_interval at every lifecycle event).
create table simulation.truck_maintenance_state (
  truck_number text references ground_truth.trucks,
  run_id uuid references simulation.runs on delete cascade,
  cumulative_km_since_service numeric,
  last_service_at timestamptz,
  service_interval_km numeric default 25000,
  service_interval_days numeric default 90,
  primary key (truck_number, run_id)
);

-- One passing row per driver per simulated day -- same "realistic baseline" treatment
-- seed_demo_fleet.py already uses for the live fleet (a real inspection gate exists; this is a
-- believable pass record, not a bypass).
create table simulation.vehicle_inspections (
  inspection_id uuid primary key default gen_random_uuid(),
  run_id uuid references simulation.runs on delete cascade,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  submitted_at timestamptz,
  odometer_km numeric,
  overall_pass boolean default true
);

-- Event-driven (assigned/arrived-pickup/departed-pickup/arrived-delivery/completed), not literal
-- 15-min ticks -- see plan decision 6. No position_history mirror: the trajectory the frontend
-- animates is already returned directly in the run's JSON payload.
create table simulation.driver_state_snapshots (
  id bigserial primary key,
  run_id uuid references simulation.runs on delete cascade,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  trip_id uuid references simulation.trips,
  snapshot_at timestamptz,
  lat double precision,
  lon double precision,
  duty_status text,
  hos_driving_hours_remaining numeric,
  hos_duty_hours_remaining numeric,
  hos_cycle1_hours_remaining numeric,
  hos_cycle2_hours_remaining numeric,
  truck_breakdown_risk numeric,
  truck_pct_km_interval numeric,
  truck_pct_days_interval numeric,
  inspection_ok boolean
);
create index on simulation.driver_state_snapshots (run_id, driver_id, snapshot_at);

-- Ratings: aggregated straight from trip_log, mirroring live.driver_ratings/truck_ratings'
-- definitions (sim/sql/011), grouped additionally by run_id so a run's own driver spotlight card
-- reads a clean per-run figure.
create view simulation.driver_ratings as
select
  run_id, driver_id,
  count(*) as trips_completed,
  avg((on_time)::int)::numeric(4,3) as on_time_rate,
  avg(post_delivery_deadhead_miles) as avg_post_delivery_deadhead_miles,
  avg(hos_stranding_risk) as avg_hos_stranding_risk,
  avg(load_fill_ratio) as avg_load_fill_ratio,
  sum(reward_total) as total_reward
from simulation.trip_log
group by run_id, driver_id;

create view simulation.truck_ratings as
select
  run_id, truck_number,
  count(*) as trips_completed,
  sum((breakdown_occurred)::int) as breakdown_count,
  avg(breakdown_repair_hours) filter (where breakdown_occurred) as avg_repair_hours_when_broken,
  avg(load_fill_ratio) as avg_load_fill_ratio
from simulation.trip_log
group by run_id, truck_number;

-- RLS: everything in simulation.* is manager-read-all -- no driver ever needs to see simulated
-- data about themselves, so none of live's driver-read-own complexity applies here. The backend
-- (direct superuser Postgres connection via sim/db.py) writes; PostgREST/the dashboard only reads.
do $$
declare
  t text;
begin
  for t in select unnest(array[
    'runs','quote_requests','quote_recommendations','quote_candidate_snapshots',
    'trips','trip_log','geofence_events','detention_billing','invoices',
    'truck_maintenance_state','vehicle_inspections','driver_state_snapshots'
  ])
  loop
    execute format('alter table simulation.%I enable row level security', t);
    execute format(
      'create policy "managers read all" on simulation.%I for select using (
         exists (select 1 from public.profiles where user_id = auth.uid() and role = ''manager'')
       )', t
    );
  end loop;
end $$;

alter view simulation.driver_ratings set (security_invoker = on);
alter view simulation.truck_ratings set (security_invoker = on);
grant select on simulation.driver_ratings, simulation.truck_ratings to authenticated;

grant usage on schema simulation to authenticated;
grant select on all tables in schema simulation to authenticated;

-- Live-schema additions, independent of the simulation schema work -----------------------------

-- The LIVE dispatch path's own candidate-scoring audit trail -- score_quote.py's all_scored rows,
-- currently computed and discarded, persisted here by /api/score-quote. No run_id: this is
-- live-only, never touched by a showcase run.
create table live.quote_candidate_snapshots (
  id bigserial primary key,
  quote_id uuid references live.quote_requests,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  location_id integer references reference.locations,
  hos_remaining_hours numeric,
  truck_breakdown_risk numeric,
  truck_pct_km_interval numeric,
  truck_pct_days_interval numeric,
  deadhead_miles numeric,
  planned_driving_hours numeric,
  planned_duty_hours numeric,
  score numeric,
  rank integer,
  was_assigned boolean,
  scored_at timestamptz default now()
);
create index on live.quote_candidate_snapshots (quote_id);
alter table live.quote_candidate_snapshots enable row level security;
create policy "managers read all" on live.quote_candidate_snapshots for select using (
  exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager')
);

-- 15-minute periodic feature-state snapshot -- what Trip History's expand view now reads instead
-- of the bare position-only live.position_history (see plan decision 6).
create table live.driver_state_snapshots (
  id bigserial primary key,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  trip_id uuid references live.trips,
  snapshot_at timestamptz default now(),
  lat double precision,
  lon double precision,
  duty_status text,
  hos_driving_hours_remaining numeric,
  hos_duty_hours_remaining numeric,
  hos_cycle1_hours_remaining numeric,
  hos_cycle2_hours_remaining numeric,
  truck_breakdown_risk numeric,
  truck_pct_km_interval numeric,
  truck_pct_days_interval numeric,
  inspection_ok boolean
);
create index on live.driver_state_snapshots (trip_id, snapshot_at);
create index on live.driver_state_snapshots (driver_id, snapshot_at);
alter table live.driver_state_snapshots enable row level security;
create policy "managers read all" on live.driver_state_snapshots for select using (
  exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager')
);
create policy "drivers read own" on live.driver_state_snapshots for select using (
  exists (select 1 from public.profiles where user_id = auth.uid() and driver_id = live.driver_state_snapshots.driver_id)
);

grant select on live.quote_candidate_snapshots, live.driver_state_snapshots to authenticated;

-- Order management: allow cancelling a quote_request, not just open/assigned/expired.
alter table live.quote_requests drop constraint quote_requests_status_check;
alter table live.quote_requests add constraint quote_requests_status_check
  check (status in ('open','assigned','expired','cancelled'));

-- Expose the new `simulation` schema via PostgREST (extends the authenticator role GUC set up in
-- sim/sql/032 -- same ALTER ROLE pattern, needs the same explicit approval at apply time).
-- Run separately/last: alter role authenticator set pgrst.db_schemas = 'public, live, graphql_public, reference, simulation';
-- then: select pg_reload_conf();
