"""Hand-built regression tests for the home-base-return retarget (documents/logs/23,
NEW_SESSION_TRAINING_RETARGET_PROMPT.md, documents/feature_reference_and_inference_guide.md).

Built DIRECTLY against compute_reward() with hand-constructed HOSState/distance inputs, NOT
against a real batch run -- a real, measured finding from this session's own batch data (500
independent simulated weeks, 43,078 real assignment decisions) is that this fleet's real
calibrated order volume relative to its ~113-driver headcount never naturally produces a
genuinely cycle-tight driver (observed minimum driver_hos_cycle1_remaining across the WHOLE
batch: 27.3h, nowhere near the threshold where hos_urgency engages given the network's real
max observed distance-to-home of ~190mi) -- so home_progress_bonus/cycle_end_stranding_penalty
are identically 0.0 across every real row in that batch. That's an honest coverage gap in the
CALIBRATED data, not a flaw in the mechanism -- these tests force the tight-cycle regime by hand
to verify the mechanism itself is correct, independent of whether this fleet's real demand ever
naturally exercises it. See documents/logs/24_home_progress_retarget_built_and_trained.md for the
full write-up.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.engine.hos import HOSLog
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.reward import _hos_urgency, compute_reward


def _fresh_hos(remaining_cycle1=70.0, remaining_cycle2=120.0):
    """A driver with full daily-clock margin (13h/14h/16h all fresh) but a HAND-SET cycle
    margin -- isolates the cycle-only urgency mechanism from the daily clocks, which
    _hos_urgency() deliberately never looks at (see reward.py's module docstring for why).
    """
    fresh = HOSLog(driver_id=1).snapshot(datetime(2026, 7, 1, 8, 0))
    fresh.remaining_cycle1_hours = remaining_cycle1
    fresh.remaining_cycle2_hours = remaining_cycle2
    return fresh


def _base_kwargs(**overrides):
    kwargs = dict(
        loaded_miles=40, weight_lbs=40000, pallets=24, capacity_lbs=44500, capacity_pallets=26,
        pre_pickup_deadhead_miles=10, hos_state=_fresh_hos(), planned_duty_hours=3,
        truck_state=TruckMaintenanceState(truck_number='B3339'), capacity_value_rate_per_lb=0.01,
    )
    kwargs.update(overrides)
    return kwargs


# --- _hos_urgency() itself -------------------------------------------------------------------

def test_urgency_is_zero_with_comfortable_cycle_margin():
    # This IS the calibrated batch's real regime (min cycle1_remaining observed: 27.3h, max
    # distance_to_home observed: ~190mi) -- confirms the "no false positives" property that
    # batch's own home_progress_bonus=0.0-everywhere result depends on.
    assert _hos_urgency(remaining_cycle_hours=27.0, hours_to_home=4.0) == 0.0


def test_urgency_rises_as_cycle_margin_tightens():
    loose = _hos_urgency(remaining_cycle_hours=40.0, hours_to_home=4.0)
    tight = _hos_urgency(remaining_cycle_hours=6.0, hours_to_home=4.0)
    tighter = _hos_urgency(remaining_cycle_hours=2.0, hours_to_home=4.0)
    assert loose == 0.0
    assert 0.0 < tight < tighter <= 1.0


def test_urgency_rises_as_home_gets_further():
    near = _hos_urgency(remaining_cycle_hours=8.0, hours_to_home=1.0)
    far = _hos_urgency(remaining_cycle_hours=8.0, hours_to_home=10.0)
    assert far > near


def test_urgency_is_zero_when_already_home():
    assert _hos_urgency(remaining_cycle_hours=0.5, hours_to_home=0.0) == 0.0


# --- home_progress_bonus / cycle_end_stranding_penalty via compute_reward() -------------------

def test_no_home_inputs_means_zero_shaping_backward_compat():
    # Every existing caller that doesn't pass the new args (tests, the real-data backtest script,
    # sim/live/score_quote.py before it's updated) must keep working unchanged.
    r = compute_reward(**_base_kwargs())
    assert r.home_progress_bonus == 0.0
    assert r.cycle_end_stranding_penalty == 0.0


def test_closing_distance_toward_home_is_rewarded_when_urgent():
    # Driver deep into a busy week (tight cycle margin -- the regime the calibrated batch never
    # reaches, given this network's small real geography keeps hours_to_home low enough that
    # urgency only engages once remaining cycle hours drops to within a few hours of
    # HOS_URGENCY_SAFETY_BUFFER_HOURS -- see this file's header comment) picks a leg that moves
    # them CLOSER to home.
    tight_cycle_hos = _fresh_hos(remaining_cycle1=2.0, remaining_cycle2=40.0)
    r = compute_reward(**_base_kwargs(
        hos_state=tight_cycle_hos,
        distance_to_home_miles=70.0, distance_to_home_miles_landing=15.0,  # closes 55mi of the gap
        hours_to_home_current=1.0, hours_to_home_landing=0.3,
    ))
    assert r.home_progress_bonus > 0.0


def test_moving_away_from_home_costs_when_urgent():
    tight_cycle_hos = _fresh_hos(remaining_cycle1=2.0, remaining_cycle2=40.0)
    r = compute_reward(**_base_kwargs(
        hos_state=tight_cycle_hos,
        distance_to_home_miles=15.0, distance_to_home_miles_landing=70.0,  # opens up the gap instead
        hours_to_home_current=0.3, hours_to_home_landing=1.0,
    ))
    assert r.home_progress_bonus < 0.0


def test_home_progress_bonus_is_near_zero_when_not_urgent_even_if_closing_distance():
    # Same closing-distance move as the "rewarded" test above, but with a COMFORTABLE cycle
    # margin (matches this fleet's real calibrated demand) -- per the user's own framing ("not
    # always pulling toward home -- take real revenue trips on their own merits until it's
    # actually urgent"), this should NOT meaningfully reward the move.
    r = compute_reward(**_base_kwargs(
        hos_state=_fresh_hos(remaining_cycle1=60.0, remaining_cycle2=100.0),
        distance_to_home_miles=70.0, distance_to_home_miles_landing=15.0,
        hours_to_home_current=1.5, hours_to_home_landing=0.3,
    ))
    assert abs(r.home_progress_bonus) < 1.0  # negligible, not the ~dozens-of-CAD swing above


def test_cycle_end_stranding_penalty_fires_when_landing_tight_and_far():
    # A trip that would leave the driver with a near-exhausted cycle AND still far from home.
    tight_cycle_hos = _fresh_hos(remaining_cycle1=10.0, remaining_cycle2=15.0)
    r = compute_reward(**_base_kwargs(
        hos_state=tight_cycle_hos, planned_duty_hours=6.0,  # eats most of the remaining cycle margin
        distance_to_home_miles=80.0, distance_to_home_miles_landing=150.0,  # lands FURTHER from home
        hours_to_home_current=2.0, hours_to_home_landing=3.5,
    ))
    assert r.cycle_end_stranding_penalty > 0.0


def test_cycle_end_stranding_penalty_is_zero_when_landing_near_home_even_if_cycle_tight():
    tight_cycle_hos = _fresh_hos(remaining_cycle1=10.0, remaining_cycle2=15.0)
    r = compute_reward(**_base_kwargs(
        hos_state=tight_cycle_hos, planned_duty_hours=1.0,
        distance_to_home_miles=10.0, distance_to_home_miles_landing=0.5,  # basically arrives home
        hours_to_home_current=0.2, hours_to_home_landing=0.02,
    ))
    assert r.cycle_end_stranding_penalty == 0.0


# --- The user's own example: a chain of real legs progressively closing the gap ---------------

def test_chain_of_real_legs_beats_one_full_empty_return_on_shaping_alone():
    """Milton -> London (normal outbound, no credit expected/tested here) -> Kitchener ->
    Mississauga: each leg is REAL, REVENUE-PAYING freight that also happens to close some of the
    gap back toward Milton -- vs. the alternative of one full empty return leg from London
    straight back to Milton (100% deadhead loss, zero home_progress_bonus since an empty
    repositioning leg isn't a scored assignment decision at all). Confirms PARTIAL, INCREMENTAL
    credit accumulates across a real chain without requiring the driver to land exactly at Milton
    on any single leg -- the user's own framing: "that percentage of the deadhead leg covered and
    revenued IS the value."
    """
    # Leg 1: London -> Kitchener. Home (Milton) distance closes from 70mi to 40mi. Driver already
    # deep into a busy week (remaining_cycle1=2.0h) -- the tight regime the calibrated batch never
    # naturally reaches (see this file's header comment).
    leg1_hos = _fresh_hos(remaining_cycle1=2.0, remaining_cycle2=40.0)
    leg1 = compute_reward(**_base_kwargs(
        hos_state=leg1_hos, planned_duty_hours=1.0,
        distance_to_home_miles=70.0, distance_to_home_miles_landing=40.0,
        hours_to_home_current=1.0, hours_to_home_landing=0.6,
    ))
    # Leg 2: Kitchener -> Mississauga, picking up right where leg 1's LANDING state left off (same
    # remaining cycle margin/distance/hours-to-home leg 1 landed at) -- a genuine chain, not two
    # independent decisions. Home distance closes further, 40mi to 15mi.
    leg2_hos = _fresh_hos(remaining_cycle1=1.0, remaining_cycle2=39.0)  # 2.0 - leg1's 1.0 planned_duty_hours
    leg2 = compute_reward(**_base_kwargs(
        hos_state=leg2_hos, planned_duty_hours=1.0,
        distance_to_home_miles=40.0, distance_to_home_miles_landing=15.0,
        hours_to_home_current=0.6, hours_to_home_landing=0.35,
    ))
    chain_total_bonus = leg1.home_progress_bonus + leg2.home_progress_bonus

    # A single leg that instead moves AWAY from home by the same total distance the two real legs
    # above closed (70 -> 15 = 55mi gained) -- representative of "took a job in the opposite
    # direction instead," the scenario the user asked the model be able to weigh against the chain.
    # Same starting state as leg 1 (same driver, same decision point, different choice).
    opposite_direction = compute_reward(**_base_kwargs(
        hos_state=leg1_hos, planned_duty_hours=1.0,
        distance_to_home_miles=70.0, distance_to_home_miles_landing=125.0,
        hours_to_home_current=1.0, hours_to_home_landing=1.8,
    ))

    assert chain_total_bonus > 0.0
    assert opposite_direction.home_progress_bonus < 0.0
    assert chain_total_bonus > opposite_direction.home_progress_bonus


# --- HOS hard-feasibility must be completely unaffected ---------------------------------------

def test_hos_hard_feasibility_unchanged_by_home_progress_args():
    """This is a PREFERENCE change among already-legal candidates, never a new source of illegal
    ones -- can_perform() doesn't take any of the new distance/home args at all, so there's
    nothing for this feature set to have broken here; asserted directly rather than assumed.
    """
    hos_state = _fresh_hos(remaining_cycle1=5.0, remaining_cycle2=8.0)  # already tight
    legal = hos_state.can_perform(planned_driving_hours=3.0, planned_duty_hours=4.0)
    illegal = hos_state.can_perform(planned_driving_hours=20.0, planned_duty_hours=20.0)
    assert legal is True
    assert illegal is False
    # Scoring this candidate (regardless of how extreme the home-progress inputs are) must never
    # change what feasible_candidates()/can_perform() themselves decide -- compute_reward() has no
    # path back into HOS legality, it only ever consumes an already-computed HOSState.
    r = compute_reward(**_base_kwargs(
        hos_state=hos_state, distance_to_home_miles=200.0, distance_to_home_miles_landing=200.0,
        hours_to_home_current=5.0, hours_to_home_landing=5.0,
    ))
    assert isinstance(r.total, float)  # scores fine regardless -- feasibility is a separate, untouched gate
