-- UI-build gaps found by direct audit of the applied schema (documents/logs/22's handoff,
-- session "NEW_SESSION_UI_BUILD_PROMPT" follow-up): score_quote.py's real pipeline and the
-- dashboard's Orders/breadcrumb views need a few columns/tables that don't exist yet. Purely
-- additive -- nothing here renames or removes anything already applied.

-- live.quote_requests: score_quote.py's QuoteRequest resolves routing/features through
-- reference.locations (location_id), not raw geography points -- origin_geog/dest_geog stay
-- (useful for a future manual-pin fallback) but the real pipeline needs location_id columns.
-- requested_pickup_at is the exact column the pickup-date fix (documents/logs/21) depends on --
-- without it every quote collapses to "pickup right now," which is what caused that bug.
alter table live.quote_requests add column if not exists origin_location_id integer references reference.locations;
alter table live.quote_requests add column if not exists dest_location_id integer references reference.locations;
alter table live.quote_requests add column if not exists requested_pickup_at timestamptz;
alter table live.quote_requests add column if not exists service_type text default 'FTL';
alter table live.quote_requests add column if not exists status text default 'open'
  check (status in ('open','assigned','expired'));

-- live.trips: needs a back-reference to the quote it fulfills, plus enough denormalized order
-- detail (weight/pallets/load_type/loaded_miles/deadhead) to render an Orders table and populate
-- live.trip_log at completion without re-deriving everything from scratch.
alter table live.trips add column if not exists quote_id uuid references live.quote_requests;
alter table live.trips add column if not exists origin_location_id integer references reference.locations;
alter table live.trips add column if not exists created_at timestamptz default now();
alter table live.trips add column if not exists weight_lbs numeric;
alter table live.trips add column if not exists pallets numeric;
alter table live.trips add column if not exists load_type text;
alter table live.trips add column if not exists loaded_miles numeric;
alter table live.trips add column if not exists pre_pickup_deadhead_miles numeric;

-- Breadcrumb history -- live.driver_status.position is current-only, the brief's "Historical
-- Breadcrumbs" requirement needs a time series. Written by both the live telemetry simulator
-- (real ticks) and the Simulation Showcase's playback-sample generation (research/
-- roadstar_platform_plan.md's "one function, two callers" convention, sim/engine/
-- route_interpolation.py feeds both).
create table if not exists live.position_history (
  id bigserial primary key,
  trip_id uuid references live.trips,
  driver_id integer references ground_truth.drivers,
  position geography(Point, 4326),
  speed_mph numeric,
  recorded_at timestamptz not null
);
create index if not exists position_history_trip_time on live.position_history (trip_id, recorded_at);
create index if not exists position_history_driver_time on live.position_history (driver_id, recorded_at);

alter table live.position_history enable row level security;
create policy "managers read all position history" on live.position_history for select
  using (exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager'));
create policy "drivers read own position history" on live.position_history for select
  using (driver_id = (select driver_id from public.profiles where user_id = auth.uid()));

-- New status value: RLS on live.quote_requests wasn't originally enabled (managers insert/read
-- only, per 009_rls.sql) -- unaffected by these additive columns, no policy change needed.
