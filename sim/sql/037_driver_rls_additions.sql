-- Real gap found building the driver-side app: sim/sql/009_rls.sql gave managers full read on
-- live.truck_maintenance_state but never added the matching "drivers read their OWN truck's
-- maintenance state" policy the My Truck screen needs -- a driver session currently gets zero
-- rows back from this table.
create policy "drivers read own truck maintenance" on live.truck_maintenance_state for select
  using (
    truck_number = (
      select truck_number from live.driver_status
      where driver_id = (select driver_id from public.profiles where user_id = auth.uid())
    )
  );
