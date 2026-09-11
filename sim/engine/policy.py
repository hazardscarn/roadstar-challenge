"""Epsilon-greedy assignment policy -- research/roadstar_platform_plan.md Section 6.

At each order arrival, scores every currently-feasible (driver, truck) candidate and either
EXPLORES (uniform-random feasible candidate, probability epsilon) or EXPLOITS (highest-scored
candidate). No trained value function exists yet (Segment 4 -- sim/training/train_value_function.py,
not yet built): `value_fn` defaults to a stub returning 0.0 for every candidate, so exploitation
is pure immediate-reward greedy for now. Swapping in a real V(s') later needs no change here --
only a different `value_fn` passed in by the caller.
"""
import random
from dataclasses import dataclass
from datetime import datetime

from sim.engine.hos import HOSState
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.reward import RewardBreakdown, compute_reward


@dataclass
class Candidate:
    """One feasible (driver, truck) pairing available to take an order, at the moment of
    scoring. `hos_state`/`truck_state` are snapshots taken at decision time, not live references
    -- scoring must not mutate them (sim/engine/run_sim.py applies the real state change only
    after a candidate is actually chosen).
    """
    driver_id: int
    truck_number: str
    hos_state: HOSState
    truck_state: TruckMaintenanceState
    pre_pickup_deadhead_miles: float
    planned_driving_hours: float   # this order's own drive time, used for the HOS feasibility check
    planned_duty_hours: float      # drive time + expected dwell, the fuller on-duty commitment
    pre_pickup_deadhead_hours: float = 0.0  # real OSRM duration when known, for the timeline walk
    location_id: int | None = None  # this candidate's effective (current or projected-landing) location -- optional, for callers that persist a candidate-scoring audit trail (live.quote_candidate_snapshots/simulation.quote_candidate_snapshots)
    effective_start: datetime | None = None  # documents/logs/17: THIS candidate's own earliest-free
    # time -- `now` for an idle driver, but a mid-route driver's projected landing time for one
    # who's still in transit. The expected-lateness ETA math (reward.py) must use THIS, not the
    # shared decision-time `now`, or a mid-route candidate's real departure delay goes uncounted.
    # Home-base-return retarget (documents/logs/23, NEW_SESSION_TRAINING_RETARGET_PROMPT.md):
    # real OSRM distance/duration from THIS candidate's current effective position, and from the
    # order's destination (this candidate's landing spot if chosen), to the driver's OWN home
    # terminal -- NOT the existing Order.dest_distance_to_hub_km, which is nearest-ANY-hub and
    # driver-agnostic. Optional/None -- a caller that doesn't yet compute these (e.g. a unit test
    # building a Candidate directly) gets home_progress_bonus/cycle_end_stranding_penalty=0.0 from
    # compute_reward(), the same backward-compatible pattern effective_start/expected_lateness_penalty
    # already established.
    distance_to_home_miles: float | None = None
    distance_to_home_miles_landing: float | None = None
    hours_to_home_current: float | None = None
    hours_to_home_landing: float | None = None


def zero_value_fn(candidate: Candidate, order, now=None) -> float:
    """Stub V(s') -- see module docstring. Always 0.0. `now` accepted (and ignored) so this has
    the same signature as sim/engine/value_function.py's trained value_fn -- a real one needs
    the current sim clock (order density/positioning value varies by hour/day-of-week), this one
    doesn't care.
    """
    return 0.0


def feasible_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Hard HOS filter -- an infeasible driver is removed BEFORE scoring, never scored then
    discarded (the Project Brief's Automated HOS Compliance requirement: illegal assignments
    must never be reachable, not just discouraged).
    """
    return [c for c in candidates if c.hos_state.can_perform(c.planned_driving_hours, c.planned_duty_hours)]


def score_candidate(
    candidate: Candidate, order, *, capacity_lbs: float, capacity_pallets: float,
    capacity_value_rate_per_lb: float, gamma: float, value_fn, now=None,
) -> tuple[float, RewardBreakdown]:
    """immediate_reward + gamma * V(s') for one candidate. Returns both the scalar score (for
    ranking) and the full RewardBreakdown (for logging what actually drove the score). `now` is
    the current sim clock, passed through to value_fn -- a real state-value model needs it
    (order density/positioning value varies by hour/day-of-week); zero_value_fn ignores it.
    """
    # The ETA basis for expected-lateness risk must be THIS candidate's own earliest-free time,
    # not the shared decision-time `now` -- an idle driver's earliest-free time IS `now`, but a
    # mid-route driver's is their projected landing time (candidate.effective_start), which can be
    # well after `now`. Falls back to `now` when a caller hasn't set effective_start (e.g. direct
    # unit-test construction of a Candidate) so this stays backward-compatible.
    eta_basis = candidate.effective_start if candidate.effective_start is not None else now
    reward = compute_reward(
        loaded_miles=order.loaded_miles, weight_lbs=order.weight_lbs, pallets=order.pallets,
        capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
        pre_pickup_deadhead_miles=candidate.pre_pickup_deadhead_miles,
        hos_state=candidate.hos_state, planned_duty_hours=candidate.planned_duty_hours,
        truck_state=candidate.truck_state, capacity_value_rate_per_lb=capacity_value_rate_per_lb,
        service_type=getattr(order, 'service_type', 'FTL'),
        pre_pickup_deadhead_hours=candidate.pre_pickup_deadhead_hours,
        decision_time=eta_basis, requested_pickup_at=getattr(order, 'requested_pickup_at', None),
        distance_to_home_miles=candidate.distance_to_home_miles,
        distance_to_home_miles_landing=candidate.distance_to_home_miles_landing,
        hours_to_home_current=candidate.hours_to_home_current,
        hours_to_home_landing=candidate.hours_to_home_landing,
        gamma=gamma,
    )
    score = reward.total + gamma * value_fn(candidate, order, now)
    return score, reward


def rank_candidates(
    candidates: list[Candidate], order, *, capacity_lbs: float, capacity_pallets: float,
    capacity_value_rate_per_lb: float, gamma: float, value_fn=zero_value_fn, now=None,
) -> list[tuple[Candidate, float]]:
    """All FEASIBLE candidates, scored and sorted best-first -- (candidate, score) pairs. Used
    for logging a real candidate GROUP per order arrival (sim.candidate_scores,
    sim/sql/023_add_candidate_scores.sql) so a learning-to-rank model has more than just the one
    winning candidate to train on. Separate from choose_assignment() (which additionally handles
    the epsilon-greedy explore/exploit draw) so logging the full ranking doesn't have to
    duplicate that logic.
    """
    feasible = feasible_candidates(candidates)
    scored = [
        (c, score_candidate(
            c, order, capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
            capacity_value_rate_per_lb=capacity_value_rate_per_lb, gamma=gamma, value_fn=value_fn, now=now,
        )[0])
        for c in feasible
    ]
    scored.sort(key=lambda t: -t[1])
    return scored


def choose_assignment(
    candidates: list[Candidate], order, *, capacity_lbs: float, capacity_pallets: float,
    capacity_value_rate_per_lb: float, epsilon: float, gamma: float, rng: random.Random,
    value_fn=zero_value_fn, now=None,
) -> tuple[Candidate, RewardBreakdown, bool] | None:
    """Returns (chosen candidate, its RewardBreakdown, was_exploration) or None if no driver is
    currently feasible (the order stays queued -- run_sim.py's event loop decides what happens
    to an order nobody can legally take right now).
    """
    feasible = feasible_candidates(candidates)
    if not feasible:
        return None

    scored = [
        (c, *score_candidate(
            c, order, capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
            capacity_value_rate_per_lb=capacity_value_rate_per_lb, gamma=gamma, value_fn=value_fn, now=now,
        ))
        for c in feasible
    ]

    if rng.random() < epsilon:
        chosen, _, reward = rng.choice(scored)               # EXPLORE
        was_exploration = True
    else:
        chosen, _, reward = max(scored, key=lambda t: t[1])  # EXPLOIT
        was_exploration = False

    return chosen, reward, was_exploration
