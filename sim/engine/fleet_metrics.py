"""Fleet-wide DISTRIBUTION metrics for the Simulation Showcase -- the same cycle-based and daily-
HOS-utilization definitions sim/backtest/real_data_replay.py's run_cycle_analysis() established
for the TRAINED arm of the validated backtest (documents/results/real_data_backtest), reused here
rather than reinvented, so a sim-demo figure means the same thing a validated backtest figure
means. Real user feedback: the showcase only ever showed a handful of averages next to the map;
these are the direct sim-demo equivalents of what backtesting already showed, with the distribution
(histogram), not just the mean, for the metrics where the mean alone hides the real spread.

A CYCLE = one full loop, home base -> home base. Every cycle is force-closed, never excluded:
either by a real completed trip whose destination IS the driver's home hub (extra_empty_miles=0,
or a real tracked distance if the LANDING spot after post-completion repositioning is home but the
order's own destination wasn't -- closed_via='sim_deadhead'), or by an ASSUMED empty return leg,
priced via get_route(), once the driver's trip sequence for the week simply runs out while still
away (closed_via='assumed') -- see cycles_for_driver()'s docstring in real_data_replay.py's
trained_cycles() for why "still away when the data ends" is PRICED, not dropped from the average.
"""
from dataclasses import dataclass
from datetime import datetime

from sim.config import HOS_MAX_DRIVING_HOURS
from sim.engine.run_sim import CompletedTrip, SimData, driver_home_hub_id, get_route


@dataclass
class Cycle:
    driver_id: int
    trips: int
    revenue: float
    extra_empty_miles: float
    closed_via: str  # 'trip' | 'sim_deadhead' | 'assumed'
    duration_hours: float
    hos_remaining_at_return: float | None


def cycles_for_driver(data: SimData, driver_id: int, driver_trips: list[CompletedTrip], sim_start: datetime) -> list[Cycle]:
    """SAME closing rule as real_data_replay.py's trained_cycles() -- ported, not reinvented, so
    this stays the real sim-side counterpart of that validated backtest figure. `next_location_id`/
    `next_hos_remaining`/`next_available_at` are the SAME next-state fields Fitted Value Iteration
    already relies on (run_sim.py's own CompletedTrip docstring) -- reading them here doesn't add
    any new computation to the sim, only aggregates numbers the trained run already produced.
    """
    home_hub = driver_home_hub_id(data, driver_id)
    driver_trips = sorted(driver_trips, key=lambda c: c.assigned_at)
    cycles: list[Cycle] = []
    cur_trips, cur_revenue, cur_start = 0, 0.0, sim_start
    last_loc, last_hos, last_end = home_hub, None, sim_start
    for c in driver_trips:
        final_loc = c.next_location_id or c.order.dest_location_id
        cur_trips += 1
        cur_revenue += c.order_revenue
        last_loc = final_loc
        last_hos = c.next_hos_remaining
        last_end = c.next_available_at or last_end
        if final_loc == home_hub:
            if final_loc == c.order.dest_location_id:
                closed_via, extra_empty_miles = 'trip', 0.0
            else:
                closed_via, extra_empty_miles = 'sim_deadhead', get_route(data, c.order.dest_location_id, final_loc)[0]
            duration_hours = (last_end - cur_start).total_seconds() / 3600
            cycles.append(Cycle(driver_id, cur_trips, cur_revenue, extra_empty_miles, closed_via, duration_hours, last_hos))
            cur_trips, cur_revenue, cur_start = 0, 0.0, last_end
    if last_loc != home_hub:
        extra_empty_miles = get_route(data, last_loc, home_hub)[0]
        duration_hours = (last_end - cur_start).total_seconds() / 3600
        cycles.append(Cycle(driver_id, cur_trips, cur_revenue, extra_empty_miles, 'assumed', duration_hours, last_hos))
    return cycles


def compute_fleet_metrics(
    data: SimData, all_trips: list[CompletedTrip], driver_pool_ids: list[int], sim_start: datetime,
) -> dict:
    """Everything a fresh (or persisted, via the stored JSONB this returns) Simulation Showcase
    run can show beyond its headline KPIs -- distributions, not just averages, per real user
    feedback: 'show more, without shrinking the map' -- this is data for a collapsible panel, the
    UI concern is the caller's, not this function's.
    """
    trips_by_driver: dict[int, list[CompletedTrip]] = {}
    for c in all_trips:
        trips_by_driver.setdefault(c.driver_id, []).append(c)

    # --- #9/#11: drivers used, and the trips-per-driver distribution ---
    trip_counts = [len(ts) for ts in trips_by_driver.values()]
    n_drivers_used = len(trips_by_driver)
    n_pool = len(set(driver_pool_ids))
    mean_trips = sum(trip_counts) / len(trip_counts) if trip_counts else 0.0
    variance = sum((x - mean_trips) ** 2 for x in trip_counts) / len(trip_counts) if trip_counts else 0.0

    # --- Cycles (#3 -- deadhead-back-to-hub distance, plus trips/revenue/duration/HOS-at-return) ---
    all_cycles: list[Cycle] = []
    for driver_id, driver_trips in trips_by_driver.items():
        all_cycles.extend(cycles_for_driver(data, driver_id, driver_trips, sim_start))
    n_cycles = len(all_cycles)
    hos_vals = [c.hos_remaining_at_return for c in all_cycles if c.hos_remaining_at_return is not None]

    # --- #6/#7: daily HOS utilization, per driver PER WORK-DAY (not a single average) ---
    # A "work-day" here is keyed off the trip's real assigned_at date -- matches the backtest's
    # own "for every real work-day in the window" framing; planned_driving_hours is the SAME
    # decision-time driving-hours figure the model itself scored/reasoned with for that trip
    # (run_sim.py's CompletedTrip), not a re-derived estimate.
    daily_driving: dict[tuple[int, str], float] = {}
    for c in all_trips:
        key = (c.driver_id, c.assigned_at.date().isoformat())
        daily_driving[key] = daily_driving.get(key, 0.0) + c.planned_driving_hours
    utilization_fracs = [h / HOS_MAX_DRIVING_HOURS for h in daily_driving.values()]
    n_over_limit = sum(1 for h in daily_driving.values() if h > HOS_MAX_DRIVING_HOURS)

    return {
        'driver_pool_size': n_pool,
        'drivers_used': n_drivers_used,
        'trips_per_driver': {
            'mean': round(mean_trips, 2),
            'std_dev': round(variance ** 0.5, 2),
            'min': min(trip_counts, default=0),
            'max': max(trip_counts, default=0),
            'histogram': sorted(trip_counts),
        },
        'cycles': {
            'n_cycles': n_cycles,
            'avg_trips_per_cycle': round(sum(c.trips for c in all_cycles) / n_cycles, 2) if n_cycles else 0.0,
            'avg_revenue_per_cycle': round(sum(c.revenue for c in all_cycles) / n_cycles, 2) if n_cycles else 0.0,
            'avg_empty_return_miles': round(sum(c.extra_empty_miles for c in all_cycles) / n_cycles, 1) if n_cycles else 0.0,
            'empty_return_miles_histogram': [round(c.extra_empty_miles, 1) for c in all_cycles],
            'avg_duration_hours': round(sum(c.duration_hours for c in all_cycles) / n_cycles, 1) if n_cycles else 0.0,
            'avg_hos_remaining_at_return': round(sum(hos_vals) / len(hos_vals), 1) if hos_vals else None,
            'n_closed_by_trip': sum(1 for c in all_cycles if c.closed_via in ('trip', 'sim_deadhead')),
            'n_closed_by_assumed_empty_return': sum(1 for c in all_cycles if c.closed_via == 'assumed'),
        },
        'daily_hos_utilization': {
            'n_work_days_observed': len(daily_driving),
            'avg_utilization_pct': round(sum(utilization_fracs) / len(utilization_fracs) * 100, 1) if utilization_fracs else 0.0,
            'utilization_pct_histogram': [round(u * 100, 1) for u in sorted(utilization_fracs)],
            'n_over_13h_limit': n_over_limit,  # sanity check -- should be ~0 in a clean run; >0 flags a logging bug, not a real violation
        },
    }
