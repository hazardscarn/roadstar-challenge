-- Real user ask: let a fleet manager draw a CUSTOM geofence for one specific trip's pickup or
-- dropoff, overriding the location's own default radius-based circle (reference.locations.radius_m)
-- -- for both real dispatch-derived trips (live.trips) AND the Simulation Trip demo (simulation.trips).
-- One shared, FK-less-on-trip_id table: live.trips and simulation.trips are separate, independently
-- generated uuid spaces, and a manually-drawn geofence means the exact same thing in both -- a
-- single lookup table keyed by (trip_id, location_id) avoids duplicating this concept, its RLS, and
-- its trigger-function branch per schema.
create table live.trip_geofence_overrides (
  trip_id uuid not null,
  location_id integer not null references reference.locations(location_id),
  geom geography(Polygon, 4326) not null,
  created_at timestamptz not null default now(),
  primary key (trip_id, location_id)
);

alter table live.trip_geofence_overrides enable row level security;
-- Deliberately zero policies -- same "manager-only via the SUPABASE_DB_URL superuser connection,
-- never a direct browser policy" reasoning as every other live.* table (009_rls.sql).

create index on live.trip_geofence_overrides (trip_id);

-- Real geofence check, extended: an override polygon for this (trip, location) wins outright over
-- the location's own default radius circle -- ST_Covers, not ST_DWithin (no extra buffer needed,
-- a manager draws the exact shape they want checked). Falls back to the exact prior behavior when
-- no override exists, so every trip that's never had one drawn is bit-for-bit unchanged.
create or replace function live.process_position_tick(
  p_trip_id uuid, p_driver_id integer, p_location_id integer,
  p_position geography, p_tick_time timestamptz
) returns void as $$
declare
  v_radius_m numeric;
  v_buffer interval;
  v_is_inside boolean;
  v_override_geom geography;
  v_state live.geofence_dwell_state%rowtype;
begin
  select geom into v_override_geom from live.trip_geofence_overrides
    where trip_id = p_trip_id and location_id = p_location_id;

  select coalesce(radius_m, 120), (coalesce(buffer_minutes, 10) || ' minutes')::interval
    into v_radius_m, v_buffer
    from reference.locations where location_id = p_location_id;

  if v_override_geom is not null then
    v_is_inside := ST_Covers(v_override_geom, p_position);
  else
    select ST_DWithin(p_position, (select geog from reference.locations where location_id = p_location_id), v_radius_m)
      into v_is_inside;
  end if;

  select * into v_state from live.geofence_dwell_state
    where trip_id = p_trip_id and location_id = p_location_id;
  if not found then
    insert into live.geofence_dwell_state (trip_id, location_id, driver_id)
    values (p_trip_id, p_location_id, p_driver_id) returning * into v_state;
  end if;

  if v_is_inside then
    v_state.outside_since := null;
    if v_state.inside_since is null then
      v_state.inside_since := p_tick_time;
    elsif v_state.arrived_at is null and p_tick_time - v_state.inside_since >= v_buffer then
      v_state.arrived_at := v_state.inside_since;  -- backdate to actual entry, not confirmation time
      insert into live.geofence_events (trip_id, location_id, event_type, occurred_at)
        values (p_trip_id, p_location_id, 'arrival', v_state.arrived_at);
    end if;
  else
    v_state.inside_since := null;
    if v_state.arrived_at is not null and v_state.departed_at is null then
      if v_state.outside_since is null then
        v_state.outside_since := p_tick_time;
      elsif p_tick_time - v_state.outside_since >= v_buffer then
        v_state.departed_at := v_state.outside_since;
        insert into live.geofence_events (trip_id, location_id, event_type, occurred_at)
          values (p_trip_id, p_location_id, 'departure', v_state.departed_at);
      end if;
    end if;
  end if;

  update live.geofence_dwell_state set
    inside_since = v_state.inside_since, outside_since = v_state.outside_since,
    arrived_at = v_state.arrived_at, departed_at = v_state.departed_at
  where trip_id = p_trip_id and location_id = p_location_id;
end;
$$ language plpgsql;

-- Same override check, mirrored into the Simulation Trip demo's own copy of this function
-- (sim/sql/044) -- see that file's header for why it's a retargeted copy, not a shared call.
create or replace function simulation.process_position_tick(
  p_trip_id uuid, p_driver_id integer, p_location_id integer, p_run_id uuid,
  p_position geography, p_tick_time timestamptz
) returns void as $$
declare
  v_radius_m numeric;
  v_buffer interval;
  v_is_inside boolean;
  v_override_geom geography;
  v_state simulation.geofence_dwell_state%rowtype;
  v_free_hours numeric;
  v_rate numeric;
begin
  select geom into v_override_geom from live.trip_geofence_overrides
    where trip_id = p_trip_id and location_id = p_location_id;

  select coalesce(radius_m, 120), (coalesce(buffer_minutes, 10) || ' minutes')::interval
    into v_radius_m, v_buffer
    from reference.locations where location_id = p_location_id;

  if v_override_geom is not null then
    v_is_inside := ST_Covers(v_override_geom, p_position);
  else
    select ST_DWithin(p_position, (select geog from reference.locations where location_id = p_location_id), v_radius_m)
      into v_is_inside;
  end if;

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
