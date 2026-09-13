-- Real bug found immediately on first live test: sim/sql/050's RETURNS TABLE(truck_number,
-- driver_id, order_ids) implicitly creates PL/pgSQL variables with those exact names -- which
-- then collide with dispatch.assignments' own columns of the same name in every UNALIASED
-- reference inside the function body (the UPDATE statements' WHERE/SET clauses), throwing
-- "column reference is ambiguous" instead of doing the update. Fix: rename the OUT columns so
-- nothing in the function body can ever collide with a real table column again -- simpler and
-- more robust than auditing every reference for a table-alias qualifier.
drop function if exists dispatch.assign_order(date, text, uuid);
drop function if exists dispatch.unassign_order(date, text, uuid);
drop function if exists dispatch.assign_driver(date, text, integer);
drop function if exists dispatch.unassign_driver(date, text);

create or replace function dispatch.assign_order(p_service_date date, p_truck_number text, p_order_id uuid)
returns table(out_truck_number text, out_driver_id integer, out_order_ids uuid[]) as $$
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

  select a.truck_number into v_source_truck from dispatch.assignments a
    where a.day_id = v_day_id and p_order_id = any(a.order_ids) and a.truck_number != p_truck_number;

  update dispatch.assignments a set order_ids = array_remove(a.order_ids, p_order_id) where a.day_id = v_day_id;
  update dispatch.assignments a set order_ids = a.order_ids || array[p_order_id]::uuid[]
    where a.day_id = v_day_id and a.truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number in (p_truck_number, coalesce(v_source_truck, p_truck_number));
end;
$$ language plpgsql;


create or replace function dispatch.unassign_order(p_service_date date, p_truck_number text, p_order_id uuid)
returns table(out_truck_number text, out_driver_id integer, out_order_ids uuid[]) as $$
declare
  v_day_id uuid;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date;
  if v_day_id is null then
    raise exception 'No dispatch day for %', p_service_date;
  end if;

  update dispatch.assignments a set order_ids = array_remove(a.order_ids, p_order_id)
    where a.day_id = v_day_id and a.truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number = p_truck_number;
end;
$$ language plpgsql;


create or replace function dispatch.assign_driver(p_service_date date, p_truck_number text, p_driver_id integer)
returns table(out_truck_number text, out_driver_id integer, out_order_ids uuid[]) as $$
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

  select a.truck_number into v_source_truck from dispatch.assignments a
    where a.day_id = v_day_id and a.driver_id = p_driver_id and a.truck_number != p_truck_number;

  update dispatch.assignments a set driver_id = null where a.day_id = v_day_id and a.driver_id = p_driver_id;
  update dispatch.assignments a set driver_id = p_driver_id where a.day_id = v_day_id and a.truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number in (p_truck_number, coalesce(v_source_truck, p_truck_number));
end;
$$ language plpgsql;


create or replace function dispatch.unassign_driver(p_service_date date, p_truck_number text)
returns table(out_truck_number text, out_driver_id integer, out_order_ids uuid[]) as $$
declare
  v_day_id uuid;
begin
  select id into v_day_id from dispatch.days where service_date = p_service_date;
  if v_day_id is null then
    raise exception 'No dispatch day for %', p_service_date;
  end if;

  update dispatch.assignments a set driver_id = null where a.day_id = v_day_id and a.truck_number = p_truck_number;

  return query select a.truck_number, a.driver_id, a.order_ids from dispatch.assignments a
    where a.day_id = v_day_id and a.truck_number = p_truck_number;
end;
$$ language plpgsql;
