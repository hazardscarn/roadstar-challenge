import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.engine.hos import HOSLog
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.policy import Candidate, choose_assignment, feasible_candidates


@dataclass
class FakeOrder:
    loaded_miles: float
    weight_lbs: float
    pallets: float


def _candidate(driver_id, truck_number, *, deadhead=5, planned_driving=3, planned_duty=4, truck_state=None):
    hos_state = HOSLog(driver_id=driver_id).snapshot(datetime(2026, 7, 1, 8, 0))  # fresh, full margin
    return Candidate(
        driver_id=driver_id, truck_number=truck_number, hos_state=hos_state,
        truck_state=truck_state or TruckMaintenanceState(truck_number=truck_number),
        pre_pickup_deadhead_miles=deadhead, planned_driving_hours=planned_driving,
        planned_duty_hours=planned_duty,
    )


SCORE_KWARGS = dict(capacity_lbs=44500, capacity_pallets=26, capacity_value_rate_per_lb=0.01, gamma=0.9)


def test_infeasible_candidates_are_never_chosen():
    feasible = _candidate(1, 'T1', planned_driving=3, planned_duty=4)
    infeasible = _candidate(2, 'T2', planned_driving=20, planned_duty=20)  # exceeds every daily limit
    result = feasible_candidates([feasible, infeasible])
    assert result == [feasible]


def test_returns_none_when_no_feasible_candidate():
    order = FakeOrder(loaded_miles=100, weight_lbs=30000, pallets=15)
    infeasible = _candidate(1, 'T1', planned_driving=20, planned_duty=20)
    result = choose_assignment(
        [infeasible], order, epsilon=0.5, rng=random.Random(0), **SCORE_KWARGS,
    )
    assert result is None


def test_greedy_picks_the_lower_deadhead_candidate():
    order = FakeOrder(loaded_miles=100, weight_lbs=30000, pallets=15)
    near = _candidate(1, 'T1', deadhead=5)
    far = _candidate(2, 'T2', deadhead=150)
    result = choose_assignment([near, far], order, epsilon=0.0, rng=random.Random(0), **SCORE_KWARGS)
    assert result is not None
    chosen, reward, was_exploration = result
    assert chosen.driver_id == 1
    assert was_exploration is False


def test_epsilon_one_always_explores():
    order = FakeOrder(loaded_miles=100, weight_lbs=30000, pallets=15)
    near = _candidate(1, 'T1', deadhead=5)
    far = _candidate(2, 'T2', deadhead=150)
    chosen, reward, was_exploration = choose_assignment(
        [near, far], order, epsilon=1.0, rng=random.Random(0), **SCORE_KWARGS,
    )
    assert was_exploration is True


def test_overdue_truck_lowers_its_candidate_score_below_a_fresh_one():
    order = FakeOrder(loaded_miles=100, weight_lbs=30000, pallets=15)
    fresh = _candidate(1, 'T1', deadhead=5, truck_state=TruckMaintenanceState(truck_number='T1'))
    overdue = _candidate(
        2, 'T2', deadhead=5,
        truck_state=TruckMaintenanceState(truck_number='T2', cumulative_km_since_service=25000 * 1.1),
    )
    chosen, reward, was_exploration = choose_assignment(
        [fresh, overdue], order, epsilon=0.0, rng=random.Random(0), **SCORE_KWARGS,
    )
    assert chosen.driver_id == 1  # same deadhead either way -- the overdue truck must be why it loses
