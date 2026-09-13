-- Real user ask: Finalize Dispatch should actually create real live.trips rows (so Live Ops
-- shows tomorrow's plan), and Edit Dispatch (reopen) should cleanly undo exactly those trips --
-- not just flip dispatch.days.status with no real effect, which is all it did before this.
--
-- Store the real route (loaded_miles/loaded_hours) on dispatch.day_orders at GENERATION time
-- (sim/live/generate_dispatch_day.py already computes this via get_route() for pricing) instead
-- of recomputing it at finalize time -- keeps finalize/reopen pure SQL (no Python/OSRM round
-- trip needed), and this project's "one function, one real computation" convention (see
-- sim/config.py's quote_price() docstring) rather than a second, possibly-drifting lookup.
--
-- dispatch.day_orders only ever holds disposable, regeneratable demo data -- any existing day is
-- cleared before this runs, so the new NOT NULL columns need no backfill.
alter table dispatch.day_orders add column loaded_miles numeric not null;
alter table dispatch.day_orders add column loaded_hours numeric not null;

-- Traces which live.trips row a dispatch-board order became, so reopen_day() can cancel EXACTLY
-- the trips finalize_day() created for this service_date -- unambiguous, no timestamp/heuristic
-- matching needed.
alter table live.trips add column dispatch_order_id uuid references dispatch.day_orders(id);

-- One truck's assigned orders become one live.trips row each, sequenced by pickup_at -- the
-- FIRST one for a driver with no other pending trip becomes their real current trip (status
-- 'assigned', live.driver_status updated immediately: truck_number/trailer_type/capacity for
-- TOMORROW's assigned equipment, current_trip_id, duty_status='driving'); every other one queues
-- as 'scheduled', the SAME status/promotion vocabulary dashboard/server/main.py's own /api/assign
-- already uses (sim/live/telemetry_simulator.py's _complete_trip() promotes the next scheduled
-- trip once the current one finishes) -- one shared trip lifecycle, not a second parallel one for
-- dispatch-board-originated trips.
--
-- Real simplification, stated plainly: this project's live system has no day-boundary clock (no
-- notion of "tomorrow arrives, now start the plan") -- finalizing makes the plan live IMMEDIATELY
-- rather than waiting for a real midnight rollover that doesn't exist here. Idempotent against
-- double-calling: only acts if dispatch.days is currently 'draft' for this date.
create or replace function dispatch.finalize_day(p_service_date date) returns void as $$
declare
  v_day_id uuid;
  r record;
  v_order record;
  v_trip_id uuid;
  v_n_pending integer;
  v_status text;
  v_truck_type text;
  v_capacity_lbs numeric;
  v_capacity_pallets integer;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date and status = 'draft';
  if v_day_id is null then
    return;  -- doesn't exist, or already finalized -- idempotent no-op
  end if;

  for r in
    select a.truck_number, a.driver_id, a.order_ids
    from dispatch.assignments a
    where a.day_id = v_day_id and a.driver_id is not null and array_length(a.order_ids, 1) > 0
  loop
    select tp.truck_type, tp.capacity_lbs, tp.capacity_pallets into v_truck_type, v_capacity_lbs, v_capacity_pallets
      from calibration.truck_profile tp where tp.truck_number = r.truck_number;

    for v_order in
      select o.* from dispatch.day_orders o where o.id = any(r.order_ids) order by o.pickup_at
    loop
      select count(*) into v_n_pending from live.trips
        where driver_id = r.driver_id and status not in ('completed', 'cancelled');
      v_status := case when v_n_pending = 0 then 'assigned' else 'scheduled' end;

      v_trip_id := gen_random_uuid();
      insert into live.trips
        (trip_id, driver_id, status, last_event, eta, origin_location_id, dest_location_id,
         created_at, weight_lbs, pallets, load_type, loaded_miles,
         planned_driving_hours, planned_duty_hours, planned_completion_at, dispatch_order_id)
      values
        (v_trip_id, r.driver_id, v_status, 'ASSGN', v_order.pickup_at, v_order.pickup_location_id, v_order.dest_location_id,
         now(), v_order.weight_lbs, v_order.pallets, v_order.load_type, v_order.loaded_miles,
         v_order.loaded_hours, v_order.loaded_hours + 1.5, v_order.delivery_eta, v_order.id);

      if v_status = 'assigned' then
        update live.driver_status set
          truck_number = r.truck_number, trailer_type = v_truck_type,
          trailer_capacity_lbs = v_capacity_lbs, trailer_capacity_pallets = v_capacity_pallets,
          current_trip_id = v_trip_id, duty_status = 'driving', updated_at = now()
        where driver_id = r.driver_id;
      end if;
    end loop;
  end loop;

  update dispatch.days set status = 'finalized', finalized_at = now() where id = v_day_id;
end;
$$ language plpgsql;


-- Undoes exactly what finalize_day() created for this service_date -- same "only clear
-- driver_status if it STILL points at this exact trip" race-guard dashboard/server/main.py's own
-- _release_assignment() already uses (the driver could've been dispatched onto something newer
-- since finalize ran). Idempotent: only acts if dispatch.days is currently 'finalized'.
create or replace function dispatch.reopen_day(p_service_date date) returns void as $$
declare
  v_day_id uuid;
  r record;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date and status = 'finalized';
  if v_day_id is null then
    return;  -- doesn't exist, or already draft -- idempotent no-op
  end if;

  for r in
    select t.trip_id, t.driver_id from live.trips t
    join dispatch.day_orders o on o.id = t.dispatch_order_id
    where o.day_id = v_day_id and t.status not in ('completed', 'cancelled')
  loop
    update live.trips set status = 'cancelled' where trip_id = r.trip_id;
    update live.driver_status set current_trip_id = null, duty_status = 'off_duty', updated_at = now()
      where driver_id = r.driver_id and current_trip_id = r.trip_id;
  end loop;

  update dispatch.days set status = 'draft', finalized_at = null where id = v_day_id;
end;
$$ language plpgsql;
