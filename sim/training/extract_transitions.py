"""Segment 4: turn sim.* exhaust into training_transitions -- one row per assignment DECISION
across every sim run in local Postgres: state (driver + order, at decision time) -> action (who
got picked) -> realized reward -> target_value (what train_value_function.py fits V(s) against).

Shape matches sim/sql/007_training_transitions.sql (extended by 013-018), which was deliberately
designed to hold only what live.* can actually supply at real quote time (see that file's own
header comment) -- this script is the sim-side half of that contract.

Real, honest gaps in what's produced, not smoothed over:
- `hos_maintenance_risk_penalty` (renamed from the misleading `lateness_risk_penalty` in
  sim/sql/017 -- a REAL lateness_penalty column exists now) isn't separately computed anywhere in
  reward.py (only a COMBINED hos_stranding_risk_penalty + maintenance_risk_penalty exists).
  Recovered here algebraically -- order_revenue - deadhead_cost - opportunity_cost_penalty -
  immediate_reward -- rather than left null or invented from scratch.
- `action_taken` is always 'accepted': the simulator only ever records the candidate that WON an
  assignment (no row exists for a candidate considered and passed over) -- a genuine scope
  limitation, not a bug.
- `next_driver_position`/`next_driver_hos_remaining` are left null -- stretch-goal (TD/policy-
  iteration) only, not needed for the committed single-pass fitted value.
- `actual_duration_hours` is an OUTCOME (assigned_at -> the trip's real COMPLETE timestamp,
  joined via the trip_id now stored on sim.assignments -- sim/sql/016), useful for backtesting
  and sanity-checking planned_driving_hours/planned_duty_hours against what really happened, but
  deliberately NOT fed to train_value_function.py as an input feature -- the model may only see
  what's knowable AT DECISION TIME, and how long the trip actually took is knowable only after.
- `lateness_penalty` is REALIZED (known only once the trip actually finishes, like the deadhead/
  breakdown penalties) -- already netted into sim.assignments.reward_total by run_sim.py, so it
  flows through into target_value automatically; also exposed as its own column for analysis.

Home-base-return retarget (documents/logs/23, sim/sql/041): the HOS sub-clock columns
(driving/duty/cycle1/cycle2, current + next) and distance_to_home_miles (current + landing) are
new real state features, pulled straight from sim.assignments -- no recovery needed, they're
already what run_sim.py persisted directly. `hos_maintenance_risk_penalty`'s algebraic recovery
formula (below) DOES need updating though: immediate_margin now also nets in
home_progress_bonus/cycle_end_stranding_penalty (RewardBreakdown.total, sim/engine/reward.py), so
the old 4-term recovery would silently absorb those two new terms into the same bucket -- fixed to
subtract them back out explicitly, since both are now persisted separately and don't need
recovering.
"""
import argparse

from psycopg2.extras import execute_values

from sim.config import ASSUMED_OPERATING_COST_PER_MILE, CAPACITY_BY_LOAD_TYPE, CAPACITY_PALLETS_BY_LOAD_TYPE
from sim.db import cursor


def load_location_lookup() -> dict[int, tuple[float, float, int]]:
    """location_id -> (lat, lon, region_id), from the remote reference.locations table --
    resolved here because sim.assignments/sim.orders only store the integer id (no cross-database
    FK is possible, see sim/sql/005_sim.sql's header), not a geography point or region. region_id
    is the sim/cluster_locations.py k-means cluster (sim/sql/021) -- the categorical geographic
    feature that replaces raw lat/lon in the trained models (see sim/sql/021's comment for why).
    """
    with cursor() as cur:  # remote
        cur.execute("select location_id, ST_Y(geog::geometry), ST_X(geog::geometry), region_id from reference.locations")
        return {loc_id: (float(lat), float(lon), region_id) for loc_id, lat, lon, region_id in cur.fetchall()}


def extract(sim_id: str | None = None) -> int:
    locations = load_location_lookup()

    where = "where a.sim_id = %s" if sim_id else ""
    params = (sim_id,) if sim_id else ()
    with cursor(local=True) as cur:
        cur.execute(
            f"""
            select
              a.sim_id, a.assigned_at, a.driver_id, a.driver_location_id, a.driver_hos_remaining,
              a.truck_number, a.was_exploration, a.immediate_margin, a.deadhead_miles,
              a.load_fill_ratio, a.opportunity_cost_penalty, a.reward_total,
              a.truck_breakdown_risk, a.trip_id, a.planned_driving_hours, a.planned_duty_hours,
              a.lateness_penalty, a.total_committed_distance_miles, a.driver_pool_size,
              a.truck_pct_km_interval, a.truck_pct_days_interval,
              a.next_location_id, a.next_hos_remaining, a.next_available_at,
              a.next_truck_pct_km_interval, a.next_truck_pct_days_interval,
              a.driver_hos_driving_remaining, a.driver_hos_duty_remaining,
              a.driver_hos_cycle1_remaining, a.driver_hos_cycle2_remaining,
              a.next_hos_driving_remaining, a.next_hos_duty_remaining,
              a.next_hos_cycle1_remaining, a.next_hos_cycle2_remaining,
              a.distance_to_home_miles, a.distance_to_home_miles_landing,
              a.home_progress_bonus, a.cycle_end_stranding_penalty,
              a.hours_since_home, a.hours_since_home_landing,
              o.order_id, o.weight_lbs, o.load_type, o.revenue, o.service_type, o.loaded_miles,
              o.dest_location_id, o.dest_distance_to_hub_km, o.dest_local_order_density, o.promised_delivery_at,
              te.completed_at
            from sim.assignments a
            join sim.orders o on o.sim_id = a.sim_id and o.order_id = a.order_id
            left join (
                select sim_id, trip_id, max(event_time) as completed_at
                from sim.trip_events where event_type = 'COMPLETE' group by sim_id, trip_id
            ) te on te.sim_id = a.sim_id and te.trip_id = a.trip_id
            {where}
            """,
            params,
        )
        rows = cur.fetchall()

    transitions = []
    for (
        run_sim_id, assigned_at, driver_id, driver_location_id, driver_hos_remaining,
        truck_number, was_exploration, immediate_margin, deadhead_miles,
        load_fill_ratio, opportunity_cost_penalty, reward_total,
        truck_breakdown_risk, trip_id, planned_driving_hours, planned_duty_hours,
        lateness_penalty, total_committed_distance_miles, driver_pool_size,
        truck_pct_km_interval, truck_pct_days_interval,
        next_location_id, next_hos_remaining, next_available_at,
        next_truck_pct_km_interval, next_truck_pct_days_interval,
        driver_hos_driving_remaining, driver_hos_duty_remaining,
        driver_hos_cycle1_remaining, driver_hos_cycle2_remaining,
        next_hos_driving_remaining, next_hos_duty_remaining,
        next_hos_cycle1_remaining, next_hos_cycle2_remaining,
        distance_to_home_miles, distance_to_home_miles_landing,
        home_progress_bonus, cycle_end_stranding_penalty,
        hours_since_home, hours_since_home_landing,
        order_id, weight_lbs, load_type, order_revenue, service_type, order_loaded_miles,
        dest_location_id, dest_distance_to_hub_km, dest_local_order_density, promised_delivery_at, completed_at,
    ) in rows:
        lat, lon, driver_region_id = locations.get(driver_location_id, (None, None, None))
        driver_position_wkt = f'POINT({lon} {lat})' if lat is not None else None
        next_lat, next_lon, next_region_id = locations.get(next_location_id, (None, None, None))
        next_position_wkt = f'POINT({next_lon} {next_lat})' if next_lat is not None else None
        _, _, dest_region_id = locations.get(dest_location_id, (None, None, None))

        # Algebraic recovery, not invention -- see module docstring. immediate_margin already
        # nets out order_revenue - deadhead_cost - opportunity_cost_penalty - (hos_stranding +
        # maintenance) risk; deadhead_cost itself isn't separately stored on this row, but
        # ASSUMED_OPERATING_COST_PER_MILE x deadhead_miles reconstructs it exactly (same formula
        # compute_reward() used originally, real config constant not a guess). order_revenue here
        # already includes any secondary-pickup LTL revenue (realized after the assignment
        # decision), so for a consolidated LTL trip this recovery absorbs that too, alongside the
        # real risk penalties -- still a valid non-negative figure, just not perfectly pure.
        #
        # home_progress_bonus/cycle_end_stranding_penalty (sim/sql/041) are now ALSO inside
        # immediate_margin (RewardBreakdown.total, sim/engine/reward.py) -- both are already
        # persisted as their own columns above, so subtract them back out here rather than let
        # this recovery silently fold them into the SAME bucket as hos_stranding/maintenance risk.
        deadhead_cost = float(deadhead_miles) * ASSUMED_OPERATING_COST_PER_MILE
        home_progress_bonus_f = float(home_progress_bonus) if home_progress_bonus is not None else 0.0
        cycle_end_stranding_penalty_f = float(cycle_end_stranding_penalty) if cycle_end_stranding_penalty is not None else 0.0
        hos_maintenance_risk_penalty = (
            float(order_revenue) - deadhead_cost - float(opportunity_cost_penalty) - float(immediate_margin)
            - cycle_end_stranding_penalty_f + home_progress_bonus_f
        )

        capacity_lbs = CAPACITY_BY_LOAD_TYPE.get(load_type, 44500)
        capacity_pallets = CAPACITY_PALLETS_BY_LOAD_TYPE.get(load_type, 26)
        actual_duration_hours = (completed_at - assigned_at).total_seconds() / 3600 if completed_at else None

        transitions.append((
            run_sim_id, assigned_at, driver_id, driver_position_wkt, driver_hos_remaining,
            'ON', None,  # driver_jurisdiction (single-region project), driver_duty_status (not distinguishable at decision time)
            truck_number, load_type, capacity_lbs, capacity_pallets,  # trailer_type proxied by the order's load_type -- sim doesn't track a trailer-intrinsic type separately
            order_id, weight_lbs, load_type, order_revenue,
            float(deadhead_miles) * 1609.34, load_fill_ratio, opportunity_cost_penalty, deadhead_cost,
            hos_maintenance_risk_penalty,
            assigned_at.hour, assigned_at.weekday(),
            'accepted', was_exploration, immediate_margin,
            next_position_wkt, next_hos_remaining,  # now populated -- feeds Fitted Value Iteration (train_state_value_function.py)
            reward_total,  # target_value: the single-pass fitted-value training label
            truck_breakdown_risk, service_type, order_loaded_miles, dest_distance_to_hub_km,
            planned_driving_hours, planned_duty_hours, actual_duration_hours,
            lateness_penalty, promised_delivery_at, dest_local_order_density,
            total_committed_distance_miles, driver_pool_size, truck_pct_km_interval, truck_pct_days_interval,
            next_available_at, driver_region_id, dest_region_id, next_region_id,
            next_truck_pct_km_interval, next_truck_pct_days_interval,
            driver_hos_driving_remaining, driver_hos_duty_remaining,
            driver_hos_cycle1_remaining, driver_hos_cycle2_remaining,
            next_hos_driving_remaining, next_hos_duty_remaining,
            next_hos_cycle1_remaining, next_hos_cycle2_remaining,
            distance_to_home_miles, distance_to_home_miles_landing,
            home_progress_bonus_f, cycle_end_stranding_penalty_f,
            hours_since_home, hours_since_home_landing,
        ))

    with cursor(local=True) as cur:
        if sim_id:
            cur.execute('delete from training_transitions where sim_id = %s', (sim_id,))
        else:
            cur.execute('truncate training_transitions')
        execute_values(
            cur,
            """insert into training_transitions (
                 sim_id, decision_time, driver_id, driver_position, driver_hos_remaining,
                 driver_jurisdiction, driver_duty_status, truck_number, trailer_type,
                 trailer_capacity_lbs, trailer_capacity_pallets, order_id, order_weight,
                 order_load_type, order_revenue, deadhead_m_to_pickup, load_fill_ratio,
                 opportunity_cost_penalty, deadhead_cost, hos_maintenance_risk_penalty, hour_of_day,
                 day_of_week, action_taken, was_exploration, immediate_reward,
                 next_driver_position, next_driver_hos_remaining, target_value,
                 truck_breakdown_risk, order_service_type, order_loaded_miles,
                 dest_distance_to_hub_km, planned_driving_hours, planned_duty_hours,
                 actual_duration_hours, lateness_penalty, promised_delivery_at,
                 dest_local_order_density, total_committed_distance_miles, driver_pool_size,
                 truck_pct_km_interval, truck_pct_days_interval, next_decision_time,
                 driver_region_id, dest_region_id, next_region_id,
                 next_truck_pct_km_interval, next_truck_pct_days_interval,
                 driver_hos_driving_remaining, driver_hos_duty_remaining,
                 driver_hos_cycle1_remaining, driver_hos_cycle2_remaining,
                 next_hos_driving_remaining, next_hos_duty_remaining,
                 next_hos_cycle1_remaining, next_hos_cycle2_remaining,
                 distance_to_home_miles, distance_to_home_miles_landing,
                 home_progress_bonus, cycle_end_stranding_penalty,
                 hours_since_home, hours_since_home_landing
               ) values %s""",
            transitions,
            template="""(
                 %s, %s, %s, ST_GeogFromText(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_GeogFromText(%s), %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
               )""",
            page_size=5000,
        )

    print(f'extracted {len(transitions):,} training_transitions rows'
          f'{f" for sim_id={sim_id}" if sim_id else " (all runs)"}')
    return len(transitions)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sim-id', default=None, help='limit to one run; default is ALL runs (truncates first)')
    args = parser.parse_args()
    extract(args.sim_id)
