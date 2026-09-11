-- One row per completed trip, both truck and driver side -- the audit trail behind the
-- dashboard's driver/truck stats and rating panels (research/roadstar_platform_plan.md Section
-- 8), populated identically whether the trip was simulated or a real live one. Sourced directly
-- from a completed TripState + TruckMaintenanceState (sim/engine/state.py, .../maintenance.py).
create table live.trip_log (
  trip_id uuid primary key references live.trips,
  driver_id integer references ground_truth.drivers,
  truck_number text references ground_truth.trucks,
  completed_at timestamptz default now(),
  loaded_miles numeric,
  pre_pickup_deadhead_miles numeric,
  post_delivery_deadhead_miles numeric,   -- the real revenue-loss signal, kept separate on purpose
  pickup_dwell_hours numeric,
  delivery_dwell_hours numeric,
  on_time boolean,                        -- delivered within the appointment window
  load_fill_ratio numeric,
  hos_stranding_risk numeric,             -- 0-1, how thin the driver's margin was for this trip
  breakdown_occurred boolean default false,
  breakdown_repair_hours numeric default 0,
  reward_total numeric                    -- the realized immediate_reward, for backtesting the model
);

-- Driver ratings: aggregated straight from trip_log, not a separately-maintained score --
-- one source of truth, recomputed on read.
create view live.driver_ratings as
select
  driver_id,
  count(*) as trips_completed,
  avg((on_time)::int)::numeric(4,3) as on_time_rate,
  avg(post_delivery_deadhead_miles) as avg_post_delivery_deadhead_miles,
  avg(hos_stranding_risk) as avg_hos_stranding_risk,
  avg(load_fill_ratio) as avg_load_fill_ratio
from live.trip_log
group by driver_id;

-- Truck ratings: reliability-focused (breakdown history), feeds the same maintenance-warning
-- panel as live.truck_maintenance_state, from the realized-outcome side rather than the
-- odometer-projection side.
create view live.truck_ratings as
select
  truck_number,
  count(*) as trips_completed,
  sum((breakdown_occurred)::int) as breakdown_count,
  avg(breakdown_repair_hours) filter (where breakdown_occurred) as avg_repair_hours_when_broken,
  avg(load_fill_ratio) as avg_load_fill_ratio
from live.trip_log
group by truck_number;
