import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.engine.hos import HOSLog
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.reward import (
    compute_reward, late_delivery_penalty, load_fill_ratio, post_delivery_deadhead_cost, realized_breakdown_penalty,
)
from sim.engine.state import TripState, TripStatus


def _base_kwargs(**overrides):
    fresh_hos = HOSLog(driver_id=1).snapshot(datetime(2026, 7, 1, 8, 0))
    fresh_truck = TruckMaintenanceState(truck_number='B3339')
    kwargs = dict(
        loaded_miles=100, weight_lbs=40000, pallets=24, capacity_lbs=44500, capacity_pallets=26,
        pre_pickup_deadhead_miles=5, hos_state=fresh_hos, planned_duty_hours=3,
        truck_state=fresh_truck, capacity_value_rate_per_lb=0.01,
    )
    kwargs.update(overrides)
    return kwargs


def test_well_utilized_short_deadhead_trip_is_strongly_positive():
    r = compute_reward(**_base_kwargs())
    assert r.total > 0
    assert r.order_revenue > r.deadhead_cost + r.opportunity_cost_penalty


def test_more_pre_pickup_deadhead_lowers_reward():
    r_short = compute_reward(**_base_kwargs(pre_pickup_deadhead_miles=5))
    r_long = compute_reward(**_base_kwargs(pre_pickup_deadhead_miles=150))
    assert r_long.deadhead_cost > r_short.deadhead_cost
    assert r_long.total < r_short.total


def test_low_load_fill_increases_opportunity_cost_for_ltl():
    # Only meaningful for LTL: an LTL shipper pays roughly proportional to space used, so an
    # under-filled LTL truck is real forgone revenue -- see the FTL test right below for the contrast.
    full = compute_reward(**_base_kwargs(weight_lbs=40000, pallets=24, service_type='LTL'))
    empty_ish = compute_reward(**_base_kwargs(weight_lbs=5000, pallets=3, service_type='LTL'))
    assert empty_ish.opportunity_cost_penalty > full.opportunity_cost_penalty
    assert empty_ish.total < full.total


def test_ftl_has_no_opportunity_cost_regardless_of_fill():
    # An FTL shipper pays for the whole truck flat -- there's no "wasted capacity" cost to them,
    # so a half-empty FTL truck earns exactly the same revenue as a full one.
    full = compute_reward(**_base_kwargs(weight_lbs=40000, pallets=24, service_type='FTL'))
    empty_ish = compute_reward(**_base_kwargs(weight_lbs=5000, pallets=3, service_type='FTL'))
    assert full.opportunity_cost_penalty == 0.0
    assert empty_ish.opportunity_cost_penalty == 0.0
    assert full.order_revenue == empty_ish.order_revenue


def test_ltl_revenue_scales_with_fill_and_is_less_than_ftl_at_the_same_distance():
    ftl = compute_reward(**_base_kwargs(weight_lbs=40000, pallets=24, service_type='FTL'))
    ltl_half_full = compute_reward(**_base_kwargs(weight_lbs=20000, pallets=12, service_type='LTL'))
    ltl_full = compute_reward(**_base_kwargs(weight_lbs=40000, pallets=24, service_type='LTL'))
    assert ltl_half_full.order_revenue < ltl_full.order_revenue  # more filled -> more LTL revenue
    # a single LTL shipment earns less than a full FTL truck over the same distance -- the
    # premium multiplier doesn't fully offset only using a fraction of the truck
    assert ltl_half_full.order_revenue < ftl.order_revenue


def test_hos_stranding_risk_increases_penalty_near_the_margin():
    fresh_hos = HOSLog(driver_id=1).snapshot(datetime(2026, 7, 1, 8, 0))  # 13h margin
    r_comfortable = compute_reward(**_base_kwargs(hos_state=fresh_hos, planned_duty_hours=2))
    r_tight = compute_reward(**_base_kwargs(hos_state=fresh_hos, planned_duty_hours=12))
    assert r_tight.hos_stranding_risk_penalty > r_comfortable.hos_stranding_risk_penalty
    assert r_tight.total < r_comfortable.total


def test_overdue_truck_increases_maintenance_penalty():
    fresh_truck = TruckMaintenanceState(truck_number='B1')
    overdue_truck = TruckMaintenanceState(truck_number='B2', cumulative_km_since_service=25000 * 1.1)
    r_fresh = compute_reward(**_base_kwargs(truck_state=fresh_truck))
    r_overdue = compute_reward(**_base_kwargs(truck_state=overdue_truck))
    assert r_overdue.maintenance_risk_penalty > r_fresh.maintenance_risk_penalty
    assert r_overdue.total < r_fresh.total


def test_post_delivery_deadhead_cost_scales_with_tracked_miles():
    t = TripState(trip_id='t1', driver_id=1)
    t0 = datetime(2026, 7, 1, 8, 0)
    t.transition(TripStatus.ASSGN, t0)
    t.transition(TripStatus.DISP, t0, loaded=False)
    t.transition(TripStatus.ARRSHIP, t0)
    t.transition(TripStatus.DOCKED, t0)
    t.transition(TripStatus.PICKD, t0, weight_lbs=20000)
    t.transition(TripStatus.DEPSHIP, t0)
    t.transition(TripStatus.ARRCONS, t0)
    t.transition(TripStatus.DOCKED, t0)
    t.transition(TripStatus.COMPLETE, t0)
    t.transition(TripStatus.DISP, t0, loaded=False, distance_miles=40)  # post-delivery deadhead
    t.transition(TripStatus.ARRSHIP, t0)  # arrives at the next pickup

    assert t.post_delivery_deadhead_miles == 40
    assert post_delivery_deadhead_cost(t) > 0


def test_realized_breakdown_penalty_only_when_it_actually_happened():
    t0 = datetime(2026, 7, 1, 8, 0)

    clean_trip = TripState(trip_id='clean', driver_id=1)
    clean_trip.transition(TripStatus.ASSGN, t0)
    clean_trip.transition(TripStatus.DISP, t0, loaded=True)
    assert realized_breakdown_penalty(clean_trip) == 0.0

    broken_trip = TripState(trip_id='broken', driver_id=2)
    broken_trip.transition(TripStatus.ASSGN, t0)
    broken_trip.transition(TripStatus.DISP, t0, loaded=True)
    broken_trip.transition(TripStatus.BREAKDOWN, t0)
    penalty = realized_breakdown_penalty(broken_trip)
    assert penalty > 0
    # must be much larger than a single trip's typical expected-cost penalty (a few hundred CAD)
    assert penalty > 1000


def test_late_delivery_within_grace_buffer_has_no_penalty():
    promised = datetime(2026, 7, 1, 14, 0)
    actual = promised + timedelta(minutes=10)  # under LATE_GRACE_MINUTES=15
    assert late_delivery_penalty(actual, promised) == 0.0


def test_late_delivery_penalty_grows_convexly_with_hours_late():
    promised = datetime(2026, 7, 1, 14, 0)
    penalty_1h = late_delivery_penalty(promised + timedelta(hours=1, minutes=15), promised)
    penalty_2h = late_delivery_penalty(promised + timedelta(hours=2, minutes=15), promised)
    assert penalty_1h > 0
    # convex (exponent > 1): doubling hours late more than doubles the penalty
    assert penalty_2h > 2 * penalty_1h


def test_on_time_or_early_delivery_has_no_penalty():
    promised = datetime(2026, 7, 1, 14, 0)
    early = promised - timedelta(hours=2)
    assert late_delivery_penalty(early, promised) == 0.0
