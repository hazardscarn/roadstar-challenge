"""Validates the HOS engine enforces BOTH the daily/continuous clocks and the rolling weekly
cycles -- the critical case is a driver who's fine on one and capped by the other.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.engine.hos import HOSLog, DRIVING, ON_DUTY_NOT_DRIVING, OFF_DUTY


def test_fresh_driver_has_full_hours():
    log = HOSLog(driver_id=1)
    now = datetime(2026, 7, 1, 8, 0)
    state = log.snapshot(now)
    assert state.remaining_driving_hours == 13
    assert state.remaining_duty_hours == 14
    assert state.remaining_cycle1_hours == 70
    assert state.remaining_cycle2_hours == 120
    assert state.can_perform(planned_driving_hours=5, planned_duty_hours=6)


def test_daily_driving_limit_binds_even_with_fresh_cycle():
    log = HOSLog(driver_id=2)
    t0 = datetime(2026, 7, 1, 6, 0)
    # 10h off-duty reset, then drive 12 of the 13 allowed hours today
    log.add(t0, t0 + timedelta(hours=10), OFF_DUTY)
    log.add(t0 + timedelta(hours=10), t0 + timedelta(hours=22), DRIVING)
    now = t0 + timedelta(hours=22)
    state = log.snapshot(now)
    assert state.remaining_driving_hours == 1  # 13 - 12
    assert state.remaining_cycle1_hours == 58  # 70 - 12 (the 12h driven counts as on-duty time)
    assert state.binding_constraint == 'driving_13h'
    assert not state.can_perform(planned_driving_hours=2, planned_duty_hours=2)  # would exceed 13h
    assert state.can_perform(planned_driving_hours=0.5, planned_duty_hours=0.5)


def test_weekly_cycle_binds_even_with_a_fresh_daily_reset():
    log = HOSLog(driver_id=3)
    day0 = datetime(2026, 6, 25, 6, 0)
    # A busy week: 5 prior days of 13h on-duty each, with a qualifying 10h off-duty reset each night
    for d in range(5):
        start = day0 + timedelta(days=d)
        log.add(start, start + timedelta(hours=13), DRIVING)
        log.add(start + timedelta(hours=13), start + timedelta(hours=23), OFF_DUTY)
    # Day 6: fresh daily reset already happened (23h since last on-duty period), full 13h/14h available
    now = day0 + timedelta(days=5, hours=1)
    state = log.snapshot(now)
    assert state.remaining_driving_hours == 13  # daily clock is fully fresh
    assert state.remaining_duty_hours == 14
    assert state.remaining_cycle1_hours == 5  # 70 - (5 * 13) = 5 -- the REAL binding constraint
    assert state.binding_constraint == 'cycle1_70h_7day'
    # legal on the daily clock, but only 5h of cycle room left -- must be rejected
    assert not state.can_perform(planned_driving_hours=8, planned_duty_hours=8)
    assert state.can_perform(planned_driving_hours=4, planned_duty_hours=4)


def test_stranding_risk_ramps_up_near_the_margin():
    log = HOSLog(driver_id=4)
    now = datetime(2026, 7, 1, 8, 0)
    state = log.snapshot(now)  # fresh driver, remaining_hours = 13 (driving is the tightest)
    assert state.stranding_risk(planned_duty_hours=3) == 0.0     # well under half the margin
    assert 0.0 < state.stranding_risk(planned_duty_hours=10) < 1.0  # comfortably legal, some risk
    assert state.stranding_risk(planned_duty_hours=13) == 1.0    # exactly maxes out the margin
