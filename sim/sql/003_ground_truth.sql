create table ground_truth.drivers (
  driver_id integer primary key,
  home_zone text,
  driver_type text check (driver_type in ('C','O')),        -- Company / Owner-operator
  pay_type text,
  driver_cycle numeric,                                       -- 7 or 8 (US FMCSA)
  driver_cycle_zone text check (driver_cycle_zone in ('U','C')), -- US / Canada jurisdiction
  default_punit text,
  terminal_zone text,
  other_code text
);
-- filter out the 38 blank placeholder rows per data_dictionary Known Issue #12 before insert

create table ground_truth.trucks (
  truck_number text primary key
);

create table ground_truth.trailers (
  trailer_number text primary key,
  trailer_type text check (trailer_type in ('Dry Van','Reefer','Flatbed')),
  capacity_lbs integer,
  capacity_pallets integer default 26   -- max pallets observed in real data (PALLETS range 8-28);
                                         -- Flatbed has no roster row per Known Issue -- synthesized,
                                         -- see sim/config.py CAPACITY_BY_LOAD_TYPE
);

-- Driver's currently-coupled equipment (see research/roadstar_platform_plan.md Section 5 --
-- driver, not truck, is the decision entity; truck/trailer ride along as attributes).
create table ground_truth.driver_equipment (
  driver_id integer primary key references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  trailer_type text,
  trailer_capacity_lbs integer,
  trailer_capacity_pallets integer
);

create table ground_truth.historical_orders (
  bill_number text primary key,
  trip_number numeric,
  callname text,
  origin_location_id integer references reference.locations,
  dest_location_id integer references reference.locations,
  actual_pickup timestamptz,
  actual_delivery timestamptz,
  distance_miles numeric,          -- store abs(DISTANCE) per Known Issue #3
  service_level text,
  temp_controlled boolean,
  load_type text,
  load_description text,
  weight_lbs integer,
  pallets integer,
  temperature text,
  was_dispatched boolean           -- false for the 316 undispatched orders
);

create table ground_truth.historical_legs (
  leg_id integer primary key,
  trip_number integer,
  leg_seq integer,
  driver_id integer references ground_truth.drivers,
  origin_location_id integer references reference.locations,
  dest_location_id integer references reference.locations,
  mt_loaded text check (mt_loaded in ('L','E')),
  leg_dist numeric,
  leg_weight numeric,
  det_pick_arrive timestamptz,
  det_delv_arrive timestamptz,
  last_fb_status text,
  run_type text                    -- from sim/classify.py's classify_run_type (canonical 8-category)
);
