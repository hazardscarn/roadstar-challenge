import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.config import MAINTENANCE_SERVICE_INTERVAL_KM
from sim.engine.maintenance import TruckMaintenanceState, initialize_fleet, sample_repair_hours


def test_fresh_truck_has_no_risk():
    t = TruckMaintenanceState(truck_number='B3339')
    assert t.breakdown_risk == 0.0
    assert not t.needs_warning


def test_risk_ramps_between_70_and_100_percent():
    t = TruckMaintenanceState(truck_number='B3339', cumulative_km_since_service=25000 * 0.85)
    assert 0.0 < t.breakdown_risk < 0.15
    assert t.needs_warning  # >= 85% threshold, matches the dashboard badge


def test_overdue_truck_capped_risk():
    t = TruckMaintenanceState(truck_number='B3339', cumulative_km_since_service=25000 * 1.5)
    assert t.breakdown_risk <= 0.30


def test_after_trip_accumulates_and_service_resets():
    t = TruckMaintenanceState(truck_number='B3339')
    t2 = t.after_trip(distance_miles=100, hours_elapsed=48)  # ~160.934 km, 2 days
    assert t2.cumulative_km_since_service == pytest.approx(160.934, abs=0.01)
    assert t2.days_since_service == pytest.approx(2.0, abs=0.01)
    t3 = t2.serviced()
    assert t3.cumulative_km_since_service == 0.0
    assert t3.days_since_service == 0.0


def test_calendar_overdue_truck_carries_risk_even_when_km_fresh():
    # A truck driven lightly but sitting since a long-ago service -- whichever dimension is
    # worse should drive risk, not an average that would dilute it away.
    t = TruckMaintenanceState(truck_number='B3339', cumulative_km_since_service=0, days_since_service=180 * 1.5)
    assert t.breakdown_risk > 0.0
    assert t.pct_of_km_interval == 0.0
    assert t.pct_of_days_interval > 1.0


def test_sample_breakdown_respects_risk():
    rng = random.Random(42)
    fresh = TruckMaintenanceState(truck_number='B1')  # risk = 0
    assert fresh.sample_breakdown(rng) is False
    overdue = TruckMaintenanceState(truck_number='B2', cumulative_km_since_service=MAINTENANCE_SERVICE_INTERVAL_KM * 2)
    draws = [overdue.sample_breakdown(rng) for _ in range(200)]
    observed_rate = sum(draws) / len(draws)
    assert observed_rate == pytest.approx(overdue.breakdown_risk, abs=0.08)  # 200 draws, generous tolerance


def test_repair_hours_in_expected_range():
    rng = random.Random(1)
    hours = [sample_repair_hours(rng) for _ in range(50)]
    assert all(2 <= h <= 8 for h in hours)


def test_fleet_initialization_is_spread_not_uniformly_fresh():
    rng = random.Random(7)
    fleet = initialize_fleet(['A', 'B', 'C', 'D', 'E'], rng)
    assert len(fleet) == 5
    values = [t.cumulative_km_since_service for t in fleet.values()]
    assert len(set(values)) == 5  # all different, not all defaulted to 0
    assert any(v > MAINTENANCE_SERVICE_INTERVAL_KM for v in values) or max(values) > 0
    # at least some spread across the range, not clustered at zero
    assert max(values) - min(values) > MAINTENANCE_SERVICE_INTERVAL_KM * 0.3
