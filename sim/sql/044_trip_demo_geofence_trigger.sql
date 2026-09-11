-- "Simulation Trip" demo (new page, parallel to the week-long Simulation Showcase): a SINGLE trip
-- ticked live in real time -- not a batch week replay -- specifically to showcase the brief's
-- "critical requirement" (Geofences & Detention Automated Billing) end to end: a truck crosses a
-- geofence, the exact arrival/departure timestamp is recorded automatically, and detention past
-- the 2h free window is billed -- run twice on the same lane (normal dock time vs. extended dock
-- time) so the with/without-detention outcomes can be shown side by side on the SAME mechanism.
--
-- Real design choice, flagged not hidden: this does NOT call live.process_position_tick() against
-- a real live.trips row -- that would insert a fake demo trip into the actual Live Ops map / Trip
-- History / Billing screens, which is exactly the contamination sim/sql/038's own header comment
-- says the `simulation` schema exists to avoid. Instead this ships `simulation.process_position_
-- tick()` -- the IDENTICAL buffered arrival/departure state-machine logic as live's (sim/sql/008),
-- copied line-for-line and only retargeted to simulation.* tables -- so the demo narration ("the
-- real platform's own trigger detects this dynamically") is honest: it's the same mechanism,
-- proven against the same buffered-debounce design, just writing into the isolated demo schema.
-- The one deliberate behavioral difference from live's version: simulation.detention_billing is
-- already keyed per (trip_id, location_id) (038's own improvement over live's trip-only pk), so
-- this function upserts arrival/amount directly at event time instead of relying on a separate
-- 5-minute cron job (sim/sql/010) -- there's no reason to wait for a periodic job when the event
-- that should trigger the write already just happened.

alter table simulation.runs add column if not exists run_kind text not null default 'batch'
  check (run_kind in ('batch','trip_demo'));
alter table simulation.runs add column if not exists scenario_label text; -- 'baseline' | 'detention', trip_demo runs only

-- State machine for the buffered geofence function -- exact structural mirror of
-- live.geofence_dwell_state (sim/sql/006), FK'd to simulation.trips instead of live.trips.
create table simulation.geofence_dwell_state (
  trip_id uuid references simulation.trips,
  location_id integer references reference.locations,
  driver_id integer references ground_truth.drivers,
  inside_since timestamptz,
  outside_since timestamptz,
  arrived_at timestamptz,
  departed_at timestamptz,
  primary key (trip_id, location_id)
);

-- The live-polled "logs every 5 (simulated) minutes" table the new page reads -- geo coordinates,
-- speed, fuel -- the demo-specific analogue of live.driver_state_snapshots, but per-trip and with
-- the raw telemetry fields (speed_mph/fuel_pct) that snapshot table doesn't carry.
create table simulation.trip_telemetry_log (
  id bigserial primary key,
  run_id uuid references simulation.runs on delete cascade,
  trip_id uuid references simulation.trips,
  recorded_at timestamptz not null,
  phase text,  -- 'to_pickup' | 'dwell_pickup' | 'to_delivery' | 'dwell_delivery' | 'completed'
  lat double precision,
  lon double precision,
  speed_mph numeric,
  fuel_pct numeric,      -- synthesized, same honest flag as live.driver_status.fuel_pct (sim/sql/033)
  odometer_km numeric,   -- synthesized, same as above
  note text              -- e.g. 'geofence arrival confirmed', '401 slowdown', 'detention threshold crossed'
);
create index on simulation.trip_telemetry_log (trip_id, id);

-- Line-for-line the same buffered debounce logic as live.process_position_tick (sim/sql/008) --
-- see this file's header comment for why it's a retargeted copy, not a call to the live function.
create or replace function simulation.process_position_tick(
  p_trip_id uuid, p_driver_id integer, p_location_id integer, p_run_id uuid,
  p_position geography, p_tick_time timestamptz
) returns void as $$
declare
  v_radius_m numeric;
  v_buffer interval;
  v_is_inside boolean;
  v_state simulation.geofence_dwell_state%rowtype;
  v_free_hours numeric;
  v_rate numeric;
begin
  select coalesce(radius_m, 120), (coalesce(buffer_minutes, 10) || ' minutes')::interval
    into v_radius_m, v_buffer
    from reference.locations where location_id = p_location_id;

  select ST_DWithin(p_position, (select geog from reference.locations where location_id = p_location_id), v_radius_m)
    into v_is_inside;

  select * into v_state from simulation.geofence_dwell_state
    where trip_id = p_trip_id and location_id = p_location_id;
  if not found then
    insert into simulation.geofence_dwell_state (trip_id, location_id, driver_id)
    values (p_trip_id, p_location_id, p_driver_id) returning * into v_state;
  end if;

  if v_is_inside then
    v_state.outside_since := null;
    if v_state.inside_since is null then
      v_state.inside_since := p_tick_time;
    elsif v_state.arrived_at is null and p_tick_time - v_state.inside_since >= v_buffer then
      v_state.arrived_at := v_state.inside_since;  -- backdate to actual entry, not confirmation time
      insert into simulation.geofence_events (run_id, trip_id, location_id, event_type, occurred_at)
        values (p_run_id, p_trip_id, p_location_id, 'arrival', v_state.arrived_at);
      -- Open the per-leg detention row immediately (038's per-(trip,location) pk) instead of
      -- waiting on a periodic cron job -- see header comment.
      insert into simulation.detention_billing (trip_id, location_id, run_id, arrival_at, free_hours)
        values (p_trip_id, p_location_id, p_run_id, v_state.arrived_at, 2)
        on conflict (trip_id, location_id) do update set arrival_at = excluded.arrival_at;
    end if;
  else
    v_state.inside_since := null;
    if v_state.arrived_at is not null and v_state.departed_at is null then
      if v_state.outside_since is null then
        v_state.outside_since := p_tick_time;
      elsif p_tick_time - v_state.outside_since >= v_buffer then
        v_state.departed_at := v_state.outside_since;
        insert into simulation.geofence_events (run_id, trip_id, location_id, event_type, occurred_at)
          values (p_run_id, p_trip_id, p_location_id, 'departure', v_state.departed_at);

        select value into v_free_hours from calibration.assumptions where key = 'detention_free_hours';
        select value into v_rate from calibration.assumptions where key = 'detention_rate_per_hr_cad';
        update simulation.detention_billing set
          departure_at = v_state.departed_at,
          free_hours = coalesce(v_free_hours, 2),
          amount = greatest(0, extract(epoch from (v_state.departed_at - arrival_at)) / 3600.0 - coalesce(v_free_hours, 2))
                   * coalesce(v_rate, 75)
        where trip_id = p_trip_id and location_id = p_location_id;
      end if;
    end if;
  end if;

  update simulation.geofence_dwell_state set
    inside_since = v_state.inside_since, outside_since = v_state.outside_since,
    arrived_at = v_state.arrived_at, departed_at = v_state.departed_at
  where trip_id = p_trip_id and location_id = p_location_id;
end;
$$ language plpgsql;

-- RLS + grants, same "managers read all, backend writes via the direct superuser connection"
-- pattern as every other simulation.* table (sim/sql/038's own do-block).
alter table simulation.geofence_dwell_state enable row level security;
create policy "managers read all" on simulation.geofence_dwell_state for select using (
  exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager')
);
alter table simulation.trip_telemetry_log enable row level security;
create policy "managers read all" on simulation.trip_telemetry_log for select using (
  exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager')
);
grant select on simulation.geofence_dwell_state, simulation.trip_telemetry_log to authenticated;
