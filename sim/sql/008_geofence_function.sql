-- Buffered geofence arrival/departure detection. Called once per new position tick per
-- (trip, driver) pair -- from the simulator (Python, via psycopg2) and, later, from any live-GPS
-- ingestion path. Same function, both callers -- research/roadstar_platform_plan.md Section 1.
create or replace function live.process_position_tick(
  p_trip_id uuid, p_driver_id integer, p_location_id integer,
  p_position geography, p_tick_time timestamptz
) returns void as $$
declare
  v_radius_m numeric;
  v_buffer interval;
  v_is_inside boolean;
  v_state live.geofence_dwell_state%rowtype;
begin
  select coalesce(radius_m, 120), (coalesce(buffer_minutes, 10) || ' minutes')::interval
    into v_radius_m, v_buffer
    from reference.locations where location_id = p_location_id;

  select ST_DWithin(p_position, (select geog from reference.locations where location_id = p_location_id), v_radius_m)
    into v_is_inside;

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
