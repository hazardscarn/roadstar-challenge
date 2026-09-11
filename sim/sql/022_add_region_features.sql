-- region_id is derived at extraction time from location_ids already stored (driver_location_id,
-- sim.orders.dest_location_id, next_location_id -- all resolved against reference.locations.region_id,
-- sim/sql/021) -- no new sim.assignments/sim.orders columns needed, just these three on the
-- flattened training table.
alter table training_transitions add column driver_region_id integer;
alter table training_transitions add column dest_region_id integer;
alter table training_transitions add column next_region_id integer;
