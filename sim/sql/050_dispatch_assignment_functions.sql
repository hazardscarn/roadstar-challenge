-- Real user feedback: every drag-and-drop on the Dispatch Board felt like a ~2s delay. Root
-- cause, found directly: sim/live/dispatch_board.py's Python-side assign/unassign functions did
-- 6-9 SEQUENTIAL round trips each to the REMOTE Supabase Postgres instance (a validation select,
-- another select, an update, a re-fetch, ...) -- each one pays real network latency, not just
-- local DB time, so a "quick" drop was actually a chain of ~150ms hops. Same PL/pgSQL-function
-- pattern this project already uses for exactly this reason (live.process_position_tick(),
-- sim/sql/008_geofence_function.sql) -- one function call is ONE round trip no matter how many
-- validation/write steps happen inside it. Validation failures (type mismatch, capacity overflow,
-- hub mismatch) raise a real Postgres exception with the same message text the old Python code
-- used, caught and re-raised as a ValueError by sim/live/dispatch_board.py.

create or replace function dispatch.assign_order(p_service_date date, p_truck_number text, p_order_id uuid)
returns table(truck_number text, driver_id integer, order_ids uuid[]) as $$
declare
  v_day_id uuid;
  v_truck_type text;
  v_capacity_lbs numeric;
  v_load_type text;
  v_weight_lbs numeric;
  v_current_order_ids uuid[];
  v_used_weight numeric;
  v_source_truck text;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date;
  if v_day_id is null then
    raise exception 'No dispatch day for %', p_service_date;
  end if;

  select tp.truck_type, tp.capacity_lbs into v_truck_type, v_capacity_lbs
    from calibration.truck_profile tp where tp.truck_number = p_truck_number;
  if v_truck_type is null then
    raise exception 'Truck % not found', p_truck_number;
  end if;

  select o.load_type, o.weight_lbs into v_load_type, v_weight_lbs
    from dispatch.day_orders o where o.id = p_order_id and o.day_id = v_day_id;
  if v_load_type is null then
    raise exception 'Order not found for this day';
  end if;

  if v_load_type != v_truck_type then
    raise exception 'Truck % is %, this order is % -- equipment types must match.', p_truck_number, v_truck_type, v_load_type;
  end if;

  select a.order_ids into v_current_order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number = p_truck_number for update;

  if v_current_order_ids is not null and p_order_id = any(v_current_order_ids) then
    return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
      where a.day_id = v_day_id and a.truck_number = p_truck_number;
    return;  -- already assigned here -- idempotent no-op
  end if;

  select coalesce(sum(o.weight_lbs), 0) into v_used_weight from dispatch.day_orders o where o.id = any(v_current_order_ids);
  if v_used_weight + v_weight_lbs > v_capacity_lbs then
    raise exception 'Truck % can''t take this load -- % lbs would exceed its % lb capacity.',
      p_truck_number, to_char(round(v_used_weight + v_weight_lbs), 'FM999,999,999'), to_char(round(v_capacity_lbs), 'FM999,999,999');
  end if;

  -- An order can only ride on one truck -- find and drop it from wherever it currently is (a
  -- re-drag from one truck to another) before adding it to the target.
  select a.truck_number into v_source_truck from dispatch.assignments a
    where a.day_id = v_day_id and p_order_id = any(a.order_ids) and a.truck_number != p_truck_number;

  update dispatch.assignments set order_ids = array_remove(order_ids, p_order_id) where day_id = v_day_id;
  update dispatch.assignments set order_ids = order_ids || array[p_order_id]::uuid[]
    where day_id = v_day_id and truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number in (p_truck_number, coalesce(v_source_truck, p_truck_number));
end;
$$ language plpgsql;


create or replace function dispatch.unassign_order(p_service_date date, p_truck_number text, p_order_id uuid)
returns table(truck_number text, driver_id integer, order_ids uuid[]) as $$
declare
  v_day_id uuid;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date;
  if v_day_id is null then
    raise exception 'No dispatch day for %', p_service_date;
  end if;

  update dispatch.assignments set order_ids = array_remove(order_ids, p_order_id)
    where day_id = v_day_id and truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number = p_truck_number;
end;
$$ language plpgsql;


create or replace function dispatch.assign_driver(p_service_date date, p_truck_number text, p_driver_id integer)
returns table(truck_number text, driver_id integer, order_ids uuid[]) as $$
declare
  v_day_id uuid;
  v_truck_hub integer;
  v_driver_hub integer;
  v_source_truck text;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date;
  if v_day_id is null then
    raise exception 'No dispatch day for %', p_service_date;
  end if;

  select home_hub_location_id into v_truck_hub from calibration.truck_profile where truck_number = p_truck_number;
  if v_truck_hub is null then
    raise exception 'Truck % not found', p_truck_number;
  end if;

  select hub_location_id into v_driver_hub from calibration.driver_home_hub where driver_id = p_driver_id;
  if v_driver_hub is null then
    raise exception 'Driver % not found', p_driver_id;
  end if;

  if v_truck_hub != v_driver_hub then
    raise exception 'Hub mismatch -- driver % and truck % are based at different hubs.', p_driver_id, p_truck_number;
  end if;

  -- A driver can only run one truck for the day -- find and clear any other truck they were on
  -- (matches the Claude Design mock's own re-drag behavior) before assigning the new one.
  select a.truck_number into v_source_truck from dispatch.assignments a
    where a.day_id = v_day_id and a.driver_id = p_driver_id and a.truck_number != p_truck_number;

  update dispatch.assignments set driver_id = null where day_id = v_day_id and driver_id = p_driver_id;
  update dispatch.assignments set driver_id = p_driver_id where day_id = v_day_id and truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number in (p_truck_number, coalesce(v_source_truck, p_truck_number));
end;
$$ language plpgsql;


create or replace function dispatch.unassign_driver(p_service_date date, p_truck_number text)
returns table(truck_number text, driver_id integer, order_ids uuid[]) as $$
declare
  v_day_id uuid;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date;
  if v_day_id is null then
    raise exception 'No dispatch day for %', p_service_date;
  end if;

  update dispatch.assignments set driver_id = null where day_id = v_day_id and truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number = p_truck_number;
end;
$$ language plpgsql;
