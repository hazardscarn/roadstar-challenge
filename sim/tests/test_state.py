"""Validates the trip status state machine: illegal transitions are rejected, and the
reward-relevant accumulators (deadhead hours, dock dwell, secondary pickup detection) compute
correctly against a realistic, hand-built sequence.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.engine.state import TripState, TripStatus


def test_illegal_transition_rejected():
    t = TripState(trip_id='t1', driver_id=1)
    t.transition(TripStatus.ASSGN, datetime(2026, 7, 1, 8, 0))
    with pytest.raises(ValueError):
        t.transition(TripStatus.COMPLETE, datetime(2026, 7, 1, 8, 5))  # can't skip straight to done


def test_simple_ftl_trip_deadhead_and_dwell():
    t = TripState(trip_id='t1', driver_id=1)
    t0 = datetime(2026, 7, 1, 8, 0)
    t.transition(TripStatus.ASSGN, t0)
    t.transition(TripStatus.DISP, t0, loaded=False)                       # empty repositioning starts
    t.transition(TripStatus.ARRSHIP, t0 + timedelta(hours=1))             # 1h deadhead to here
    t.transition(TripStatus.DOCKED, t0 + timedelta(hours=1, minutes=10))
    t.transition(TripStatus.PICKD, t0 + timedelta(hours=1, minutes=10), weight_lbs=20000)
    t.transition(TripStatus.DEPSHIP, t0 + timedelta(hours=2))             # ARRSHIP->DEPSHIP = 1h dwell
    t.transition(TripStatus.ARRCONS, t0 + timedelta(hours=5))
    t.transition(TripStatus.DOCKED, t0 + timedelta(hours=5, minutes=5))
    t.transition(TripStatus.COMPLETE, t0 + timedelta(hours=6))            # ARRCONS->COMPLETE = 1h dwell

    assert t.deadhead_hours == pytest.approx(1.0)
    assert t.pickup_dwell_hours == pytest.approx(1.0)
    assert t.delivery_dwell_hours == pytest.approx(1.0)
    assert t.had_secondary_pickup is False
    assert t.total_weight_picked_up == 20000
    assert t.summarize_run_type() == 'Point-to-point single load (FTL) + deadhead'


def test_secondary_pickup_detected_as_ltl():
    t = TripState(trip_id='t2', driver_id=2)
    t0 = datetime(2026, 7, 1, 8, 0)
    t.transition(TripStatus.ASSGN, t0)
    t.transition(TripStatus.DISP, t0, loaded=False)
    t.transition(TripStatus.ARRSHIP, t0 + timedelta(minutes=30))
    t.transition(TripStatus.SPTLD, t0 + timedelta(minutes=35))
    t.transition(TripStatus.PICKD, t0 + timedelta(minutes=40), weight_lbs=10000)
    t.transition(TripStatus.DEPSHIP, t0 + timedelta(minutes=50))
    t.transition(TripStatus.STOPOFF, t0 + timedelta(hours=1))
    t.transition(TripStatus.ARRSHIP, t0 + timedelta(hours=1, minutes=10))   # 2nd pickup
    t.transition(TripStatus.DOCKED, t0 + timedelta(hours=1, minutes=15))
    t.transition(TripStatus.PICKD, t0 + timedelta(hours=1, minutes=20), weight_lbs=8000)
    t.transition(TripStatus.DEPSHIP, t0 + timedelta(hours=1, minutes=30))
    t.transition(TripStatus.ARRCONS, t0 + timedelta(hours=4))
    t.transition(TripStatus.DOCKED, t0 + timedelta(hours=4, minutes=5))
    t.transition(TripStatus.COMPLETE, t0 + timedelta(hours=4, minutes=30))

    assert t.had_secondary_pickup is True
    assert t.total_weight_picked_up == 18000  # both pickups counted, not just the first
    assert t.summarize_run_type() == 'Multi-stop consolidated freight (LTL)'


def test_post_delivery_branches_all_valid():
    for next_status in (TripStatus.ASSGN, TripStatus.DISP, TripStatus.DROMT):
        t = TripState(trip_id='t3', driver_id=3)
        t.transition(TripStatus.COMPLETE, datetime(2026, 7, 1, 8, 0))
        t.transition(next_status, datetime(2026, 7, 1, 8, 30))  # should not raise


def test_breakdown_is_a_real_timed_state():
    t = TripState(trip_id='t4', driver_id=4)
    t0 = datetime(2026, 7, 1, 8, 0)
    t.transition(TripStatus.ASSGN, t0)
    t.transition(TripStatus.DISP, t0, loaded=True)
    t.transition(TripStatus.BREAKDOWN, t0)  # mid-route failure instead of reaching ARRSHIP
    t.transition(TripStatus.DISP, t0 + timedelta(hours=5), loaded=True)  # repaired, resuming
    t.transition(TripStatus.ARRSHIP, t0 + timedelta(hours=8))  # eventually arrives

    assert t.had_breakdown is True
    assert t.breakdown_repair_hours == pytest.approx(5.0)


def test_breakdown_only_valid_from_disp():
    t = TripState(trip_id='t5', driver_id=5)
    t.transition(TripStatus.ASSGN, datetime(2026, 7, 1, 8, 0))
    with pytest.raises(ValueError):
        t.transition(TripStatus.BREAKDOWN, datetime(2026, 7, 1, 8, 5))  # can't break down while still ASSGN
