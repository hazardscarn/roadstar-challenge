"""Unit coverage for sim/engine/fleet_metrics.py -- the Simulation Showcase's cycle-based and
daily-HOS-utilization DISTRIBUTION metrics (real user feedback: show the same things backtesting
already showed -- deadhead-back-to-hub distance, HOS utilization, trips-per-driver spread -- as
distributions, not just averages). No DB -- a hand-built SimData (mirrors test_home_progress.py's
style) with just the fields cycles_for_driver()/get_route()/driver_home_hub_id() actually touch,
and lane_routes pre-populated for every pair a test exercises so get_route() never reaches out to
a live OSRM server.
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sim.engine.fleet_metrics import compute_fleet_metrics, cycles_for_driver  # noqa: E402
from sim.engine.run_sim import CompletedTrip, Order, SimData  # noqa: E402
from sim.engine.state import TripState  # noqa: E402

LONDON_HUB = 1
MILTON_HUB = 2
CUSTOMER_A = 100
CUSTOMER_B = 200


def _sim_data(**lane_route_overrides) -> SimData:
    lane_routes = {
        (CUSTOMER_A, LONDON_HUB): (50_000.0, 3600.0),   # 31.1 mi, 1h
        (CUSTOMER_B, LONDON_HUB): (80_000.0, 5400.0),   # 49.7 mi, 1.5h
        (CUSTOMER_A, MILTON_HUB): (60_000.0, 4500.0),
    }
    lane_routes.update(lane_route_overrides)
    return SimData(
        locations={}, location_region={}, region_centroids={}, hub_ids={'London': LONDON_HUB, 'Milton': MILTON_HUB},
        lane_weights=[], lane_routes=lane_routes, order_arrival_rate={}, dwell_minutes={},
        hos_median_remaining_hours=8.0, order_pool=[], driver_ids=[], driver_terminal_zone={},
        truck_numbers=[], driver_default_truck={}, origin_density={}, lead_time_samples=[],
    )


def _order(**overrides) -> Order:
    base = dict(
        order_id=uuid.uuid4(), origin_location_id=LONDON_HUB, dest_location_id=CUSTOMER_A,
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc), weight_lbs=20000.0, pallets=10.0,
        load_type='Dry Van', loaded_miles=30.0, loaded_hours=1.0, service_type='FTL',
        dest_distance_to_hub_km=5.0, dest_local_order_density=0.0,
        requested_pickup_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        decision_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        promised_delivery_at=datetime(2026, 9, 1, 6, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Order(**base)


def _trip(**overrides) -> CompletedTrip:
    order = overrides.pop('order', None) or _order()
    base = dict(
        trip_id=uuid.uuid4(), order=order, driver_id=16, truck_number='TEST1',
        assigned_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        completed_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
        trip_state=TripState(trip_id='t', driver_id=16),
        order_revenue=300.0, immediate_reward=300.0, reward_total=300.0,
        deadhead_miles=0.0, deadhead_cost=0.0, load_fill_ratio=0.5, opportunity_cost_penalty=0.0,
        was_exploration=False, driver_location_id=LONDON_HUB, driver_hos_remaining=10.0,
        driver_hos_driving_remaining=10.0, driver_hos_duty_remaining=11.0,
        driver_hos_cycle1_remaining=50.0, driver_hos_cycle2_remaining=90.0,
        truck_breakdown_risk=0.0, planned_driving_hours=2.0, planned_duty_hours=3.5,
        lateness_penalty=0.0, total_committed_distance_miles=30.0, driver_pool_size=2,
        truck_pct_km_interval=0.1, truck_pct_days_interval=0.1,
    )
    base.update(overrides)
    return CompletedTrip(**base)


class TestCyclesForDriver:
    def test_cycle_closed_by_a_real_trip_landing_at_home_has_zero_empty_miles(self):
        """The 'found a paid trip on the way back' case -- extra_empty_miles must be exactly 0,
        never estimated, when the order's own destination IS the driver's home hub."""
        data = _sim_data()
        trip = _trip(
            driver_id=16, order=_order(origin_location_id=LONDON_HUB, dest_location_id=MILTON_HUB),
            next_location_id=MILTON_HUB, next_hos_remaining=9.0,
            next_available_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
        )
        data.driver_terminal_zone[16] = 'ONMIL'  # home hub = Milton
        cycles = cycles_for_driver(data, 16, [trip], sim_start=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert len(cycles) == 1
        c = cycles[0]
        assert c.trips == 1
        assert c.closed_via == 'trip'
        assert c.extra_empty_miles == 0.0
        assert c.revenue == 300.0
        assert c.hos_remaining_at_return == 9.0

    def test_cycle_closed_by_an_assumed_empty_return_when_the_data_runs_out(self):
        """THE real gap this whole metric exists to price: a driver still away from home when
        their trip sequence for the week simply ends -- must be priced via get_route(), not
        dropped from the average."""
        data = _sim_data()
        data.driver_terminal_zone[16] = None  # home hub = London (default)
        trip = _trip(
            driver_id=16, order=_order(origin_location_id=LONDON_HUB, dest_location_id=CUSTOMER_A),
            next_location_id=CUSTOMER_A, next_hos_remaining=7.0,
            next_available_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
        )
        cycles = cycles_for_driver(data, 16, [trip], sim_start=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert len(cycles) == 1
        c = cycles[0]
        assert c.closed_via == 'assumed'
        # get_route(data, CUSTOMER_A, LONDON_HUB) -- 50_000m / 1609.34 = 31.07mi (cached above)
        assert c.extra_empty_miles == pytest.approx(50_000.0 / 1609.34)
        assert c.hos_remaining_at_return == 7.0

    def test_multiple_cycles_for_one_driver_in_one_week(self):
        """A driver who makes it home, then goes out and doesn't make it back again -- must
        produce TWO separate cycles, not one merged one."""
        data = _sim_data()
        data.driver_terminal_zone[16] = None  # home = London
        trip1 = _trip(
            driver_id=16, order=_order(origin_location_id=LONDON_HUB, dest_location_id=LONDON_HUB),
            next_location_id=LONDON_HUB, next_hos_remaining=9.0,
            assigned_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
            next_available_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
        )
        trip2 = _trip(
            driver_id=16, order=_order(origin_location_id=LONDON_HUB, dest_location_id=CUSTOMER_B),
            next_location_id=CUSTOMER_B, next_hos_remaining=5.0,
            assigned_at=datetime(2026, 9, 2, 8, tzinfo=timezone.utc),
            next_available_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        )
        cycles = cycles_for_driver(data, 16, [trip1, trip2], sim_start=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert len(cycles) == 2
        assert cycles[0].closed_via == 'trip'
        assert cycles[1].closed_via == 'assumed'
        assert cycles[1].extra_empty_miles == pytest.approx(80_000.0 / 1609.34)


class TestComputeFleetMetrics:
    def test_drivers_used_and_trips_per_driver_distribution(self):
        data = _sim_data()
        data.driver_terminal_zone[16] = None
        data.driver_terminal_zone[17] = None
        driver16_trips = [
            _trip(driver_id=16, order=_order(dest_location_id=LONDON_HUB), next_location_id=LONDON_HUB,
                  next_hos_remaining=9.0, next_available_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc))
            for _ in range(3)
        ]
        driver17_trips = [
            _trip(driver_id=17, order=_order(dest_location_id=LONDON_HUB), next_location_id=LONDON_HUB,
                  next_hos_remaining=9.0, next_available_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc))
        ]
        metrics = compute_fleet_metrics(
            data, driver16_trips + driver17_trips, driver_pool_ids=[16, 17, 18],
            sim_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        assert metrics['driver_pool_size'] == 3
        assert metrics['drivers_used'] == 2  # driver 18 never got a trip
        assert metrics['trips_per_driver']['histogram'] == [1, 3]
        assert metrics['trips_per_driver']['mean'] == 2.0
        assert metrics['trips_per_driver']['max'] == 3

    def test_daily_hos_utilization_sums_driving_hours_per_driver_per_day(self):
        """Two trips the SAME driver, SAME calendar day -- utilization must sum both trips'
        planned_driving_hours for that one work-day, not average or overwrite them."""
        data = _sim_data()
        data.driver_terminal_zone[16] = None
        same_day = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
        t1 = _trip(driver_id=16, planned_driving_hours=6.0, assigned_at=same_day,
                   order=_order(dest_location_id=LONDON_HUB), next_location_id=LONDON_HUB,
                   next_available_at=same_day)
        t2 = _trip(driver_id=16, planned_driving_hours=8.0, assigned_at=same_day.replace(hour=14),
                   order=_order(dest_location_id=LONDON_HUB), next_location_id=LONDON_HUB,
                   next_available_at=same_day)
        metrics = compute_fleet_metrics(data, [t1, t2], driver_pool_ids=[16], sim_start=same_day)
        util = metrics['daily_hos_utilization']
        assert util['n_work_days_observed'] == 1
        # (6.0 + 8.0) / 13.0 * 100 = 107.7% -- over the legal cap, flagged via n_over_13h_limit
        assert util['avg_utilization_pct'] == pytest.approx(107.7, abs=0.1)
        assert util['n_over_13h_limit'] == 1

    def test_empty_pool_returns_zeroed_not_crashing_metrics(self):
        data = _sim_data()
        metrics = compute_fleet_metrics(data, [], driver_pool_ids=[], sim_start=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert metrics['drivers_used'] == 0
        assert metrics['cycles']['n_cycles'] == 0
        assert metrics['cycles']['avg_hos_remaining_at_return'] is None
        assert metrics['daily_hos_utilization']['avg_utilization_pct'] == 0.0
