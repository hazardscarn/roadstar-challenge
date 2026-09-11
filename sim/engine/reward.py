"""immediate_reward(order, driver) -- research/roadstar_platform_plan.md Section 6/8, extended
with the cost factors surfaced this session: HOS stranding risk (a driver can be legally cleared
to start a trip and still get stranded mid-route if a delay eats their margin), truck maintenance
risk (entirely synthesized -- see sim/engine/maintenance.py's docstring for why there's no real
data to derive it from), expected lateness risk (documents/logs/17), and the home-base-return
retarget (documents/logs/23_home_base_return_gap_found.md,
documents/logs/NEW_SESSION_TRAINING_RETARGET_PROMPT.md,
documents/feature_reference_and_inference_guide.md -- see below).

    immediate_reward = order_revenue
                        - deadhead_cost
                        - opportunity_cost_penalty
                        - hos_stranding_risk_penalty
                        - maintenance_risk_penalty
                        - expected_lateness_penalty
                        - cycle_end_stranding_penalty
                        + home_progress_bonus

`order_revenue` and the operating-cost rate behind `deadhead_cost` are SYNTHESIZED (sim/config.py
-- no revenue field exists anywhere in the source data). Everything else here is a real,
structural cost derived from the trip's actual state.

Same function, sim and live: it takes a TripState/HOSState/TruckMaintenanceState snapshot, not a
sim-specific object -- the live scoring path (sim/live/score_quote.py) calls this identically for
a real candidate driver's real current state.

## home_progress_bonus / cycle_end_stranding_penalty -- what problem these solve

Nothing above (before this pass) priced a driver ending up stranded far from base with too little
HOS cycle time left to get back, nor credited a chain of real, revenue-paying trips that
progressively works a driver back toward base instead of one full empty return leg -- confirmed a
real, structural gap by reading every place it could plausibly already live (documents/logs/23).
Two deliberately SEPARATE new terms, answering different questions:

- `home_progress_bonus`: POTENTIAL-BASED reward shaping (Ng, Harada & Russell 1999: F(s,a,s') =
  gamma*Phi(s') - Phi(s), for any state-only potential function Phi, providably preserves the
  optimal policy under the unshaped reward -- this reshapes PREFERENCE among otherwise-legal
  choices, it can never make an illegal choice look legal). Phi(s) = -hos_urgency(s) x
  distance_to_home(s): a driver comfortably within their weekly/bi-weekly cycle gets near-zero
  pull toward home (take real revenue trips on their own merits, home-directed or not); as the
  cycle genuinely tightens, the pull sharpens fast. This is what gives partial, incremental credit
  for a REAL trip that happens to close some of the gap back toward base, proportional to how much
  it closes -- not only when a driver eventually lands exactly back at a hub.
- `cycle_end_stranding_penalty`: a separate, real backstop for the outcome the bonus above is
  trying to prevent from ever actually happening -- landing, after this trip, in a state where the
  driver's cycle margin is genuinely tight AND they're still far from home. Distinct from the
  existing `hos_stranding_risk_penalty` (a WITHIN-TRIP margin check against ANY of the 5 real HOS
  clocks, daily or cycle) -- this one only ever looks at the 7-day/14-day CYCLE clocks
  specifically, because hitting a DAILY limit far from home is recoverable (rest, continue
  tomorrow, as long as cycle budget still allows the eventual trip home); only a cycle-level
  exhaustion is a genuine, non-recoverable-without-a-long-reset stranding. Folding these two into
  one term would conflate two different real risks under one name -- kept apart on purpose.

Both are `None`/0.0 by default (like `expected_lateness_penalty` before it) so every existing
caller that doesn't yet pass the new home/cycle inputs keeps working unchanged.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sim.config import (
    ASSUMED_BREAKDOWN_COST_CAD, ASSUMED_CYCLE_STRANDING_PENALTY_CAD_MAX, ASSUMED_LATE_PENALTY_PER_HOUR_CAD,
    ASSUMED_LTL_RATE_MULTIPLIER, ASSUMED_OPERATING_COST_PER_MILE, HOS_URGENCY_SAFETY_BUFFER_HOURS,
    LATE_GRACE_MINUTES, LATE_PENALTY_EXPONENT, linehaul_rate_per_mile,
)
from sim.engine.hos import HOSState
from sim.engine.maintenance import TruckMaintenanceState
from sim.engine.state import TripState


@dataclass
class RewardBreakdown:
    order_revenue: float
    deadhead_cost: float
    opportunity_cost_penalty: float
    hos_stranding_risk_penalty: float
    maintenance_risk_penalty: float
    expected_lateness_penalty: float = 0.0
    cycle_end_stranding_penalty: float = 0.0
    home_progress_bonus: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.order_revenue - self.deadhead_cost - self.opportunity_cost_penalty
            - self.hos_stranding_risk_penalty - self.maintenance_risk_penalty
            - self.expected_lateness_penalty - self.cycle_end_stranding_penalty
            + self.home_progress_bonus
        )


def load_fill_ratio(weight_lbs: float, pallets: float, capacity_lbs: float, capacity_pallets: float) -> float:
    """max(), not average -- freight typically cubes out (runs out of floor/pallet space)
    before it weighs out (data_analysis.ipynb Section 1c: FTL averages only ~42.5% weight fill),
    so pallet fill is often the true binding constraint even when weight fill looks low.
    """
    weight_ratio = weight_lbs / capacity_lbs if capacity_lbs else 0.0
    pallet_ratio = pallets / capacity_pallets if capacity_pallets else 0.0
    return max(weight_ratio, pallet_ratio)


def _lateness_penalty_from_hours_late(hours_late: float) -> float:
    """Shared shape between the EXPECTED (decision-time) and REALIZED (post-trip) lateness
    penalties -- same convex curve (LATE_PENALTY_EXPONENT > 1), same grace handling, so a
    candidate scored as risky at decision time and one scored as having actually run late are
    measured on the same scale, not two different formulas that happen to look similar.
    """
    return ASSUMED_LATE_PENALTY_PER_HOUR_CAD * (max(0.0, hours_late) ** LATE_PENALTY_EXPONENT)


def _hos_urgency(remaining_cycle_hours: float, hours_to_home: float) -> float:
    """0-1: how urgently a driver needs to head home before their HOS CYCLE (7-day/14-day, NOT
    the daily clocks -- see this module's header comment for why) genuinely runs out. 0 while
    there's comfortable cycle margin relative to the real drive-time needed to reach home
    (`hours_to_home`, from get_route() -- real OSRM duration, not a guessed average-speed
    conversion), ramping toward 1 as that margin tightens -- same ramp shape as
    HOSState.stranding_risk() for the same reason (a comfortable margin needs zero pull; a tight
    one needs it sharply). Already home (hours_to_home <= 0) is always 0 urgency, not a
    division edge case.
    """
    if hours_to_home <= 0:
        return 0.0
    margin_ratio = remaining_cycle_hours / (hours_to_home + HOS_URGENCY_SAFETY_BUFFER_HOURS)
    return max(0.0, min(1.0, 1 - margin_ratio))


def compute_reward(
    *,
    loaded_miles: float,
    weight_lbs: float,
    pallets: float,
    capacity_lbs: float,
    capacity_pallets: float,
    pre_pickup_deadhead_miles: float,
    hos_state: HOSState,
    planned_duty_hours: float,
    truck_state: TruckMaintenanceState,
    capacity_value_rate_per_lb: float,
    service_type: str = 'FTL',
    pre_pickup_deadhead_hours: float = 0.0,
    decision_time: datetime | None = None,
    requested_pickup_at: datetime | None = None,
    distance_to_home_miles: float | None = None,
    distance_to_home_miles_landing: float | None = None,
    hours_to_home_current: float | None = None,
    hours_to_home_landing: float | None = None,
    gamma: float = 0.9,
) -> RewardBreakdown:
    """One assignment decision's reward. `pre_pickup_deadhead_miles` is charged here (it's the
    real cost of THIS assignment reaching the load) -- post-delivery deadhead is a DIFFERENT
    trip's cost (it belongs to whatever comes next), tracked on the completed TripState instead,
    not double-charged into this decision. See TripState.post_delivery_deadhead_miles.

    FTL vs LTL revenue, real-world distinction (see sim/config.py's ASSUMED_LTL_RATE_MULTIPLIER):
    an FTL shipper pays the full linehaul rate for the WHOLE truck regardless of how full it
    actually is -- there's no real "wasted capacity" cost to the shipper, so no opportunity-cost
    penalty applies. An LTL shipper pays a premium per-unit rate but only for the fraction of the
    truck they actually use -- unfilled capacity IS real forgone revenue here (the whole point of
    a secondary pickup: filling more of the truck earns more). A secondary LTL pickup's own
    revenue is added separately, after the fact, once it's known to have happened -- see
    sim/engine/run_sim.py's run_assignment().

    `expected_lateness_penalty` (documents/logs/17): whether picking a mid-route driver leaves
    them running late is NOT a soft, learned correlation here -- it's computed directly from the
    real OSRM deadhead ETA (`decision_time + pre_pickup_deadhead_hours`, the same duration that
    already drives the sim's own timeline) vs. `requested_pickup_at`. If that projected arrival is
    already past the requested pickup, the trip starts late and (since delivery timing flows
    linearly from pickup timing) finishes exactly that much late too -- a deterministic
    calculation, not a guess a value function has to infer from outcomes alone. Optional args
    (default None/0.0, meaning "no lateness signal available") so existing callers that don't yet
    pass timing info keep working -- see policy.py's score_candidate() for the real call site.

    `home_progress_bonus`/`cycle_end_stranding_penalty` (see module header comment): also optional
    -- `distance_to_home_miles*`/`hours_to_home_*` default to None, meaning "no home-position
    signal available," same treatment as the lateness args. `gamma` defaults to 0.9, matching
    sim/engine/run_sim.py's GAMMA -- needed here (not just by the caller) because the shaping term
    has to be part of RewardBreakdown.total, the one number persisted/logged everywhere else.
    """
    fill = load_fill_ratio(weight_lbs, pallets, capacity_lbs, capacity_pallets)
    rate_per_mile = linehaul_rate_per_mile(loaded_miles)  # distance-tiered, not flat -- see sim/config.py
    if service_type == 'LTL':
        order_revenue = loaded_miles * rate_per_mile * ASSUMED_LTL_RATE_MULTIPLIER * fill
        opportunity_cost_penalty = (1 - fill) * capacity_value_rate_per_lb * capacity_lbs
    else:  # FTL
        order_revenue = loaded_miles * rate_per_mile
        opportunity_cost_penalty = 0.0  # already paid in full regardless of fill -- no capacity to "waste"

    deadhead_cost = pre_pickup_deadhead_miles * ASSUMED_OPERATING_COST_PER_MILE

    hos_stranding_risk_penalty = hos_state.stranding_risk(planned_duty_hours) * ASSUMED_OPERATING_COST_PER_MILE * loaded_miles
    maintenance_risk_penalty = truck_state.expected_breakdown_cost()

    expected_lateness_penalty = 0.0
    if decision_time is not None and requested_pickup_at is not None:
        projected_arrival = decision_time + timedelta(hours=pre_pickup_deadhead_hours)
        hours_late = (projected_arrival - requested_pickup_at).total_seconds() / 3600 - LATE_GRACE_MINUTES / 60
        expected_lateness_penalty = _lateness_penalty_from_hours_late(hours_late)

    home_progress_bonus = 0.0
    cycle_end_stranding_penalty = 0.0
    if (
        distance_to_home_miles is not None and distance_to_home_miles_landing is not None
        and hours_to_home_current is not None and hours_to_home_landing is not None
    ):
        remaining_cycle_now = min(hos_state.remaining_cycle1_hours, hos_state.remaining_cycle2_hours)
        # Landing cycle margin is an ESTIMATE (current margin minus this trip's planned duty
        # hours, clamped) -- the same light approximation sim/engine/value_function.py already
        # uses for landed_hos, not the exact post-trip figure only known once the trip actually
        # runs (dwell/deadhead specifics). Good enough for a shaping signal computed at decision
        # time from decision-time-only inputs.
        remaining_cycle_landing = max(0.0, remaining_cycle_now - planned_duty_hours)
        urgency_now = _hos_urgency(remaining_cycle_now, hours_to_home_current)
        urgency_landing = _hos_urgency(remaining_cycle_landing, hours_to_home_landing)
        phi_now = -urgency_now * distance_to_home_miles
        phi_landing = -urgency_landing * distance_to_home_miles_landing
        cad_per_mile = ASSUMED_OPERATING_COST_PER_MILE  # avoided-deadhead framing -- same rate deadhead_cost itself uses
        home_progress_bonus = (gamma * phi_landing - phi_now) * cad_per_mile
        cycle_end_stranding_penalty = urgency_landing * ASSUMED_CYCLE_STRANDING_PENALTY_CAD_MAX

    return RewardBreakdown(
        order_revenue=order_revenue,
        deadhead_cost=deadhead_cost,
        opportunity_cost_penalty=opportunity_cost_penalty,
        hos_stranding_risk_penalty=hos_stranding_risk_penalty,
        maintenance_risk_penalty=maintenance_risk_penalty,
        expected_lateness_penalty=expected_lateness_penalty,
        cycle_end_stranding_penalty=cycle_end_stranding_penalty,
        home_progress_bonus=home_progress_bonus,
    )


def post_delivery_deadhead_cost(trip: TripState) -> float:
    """The OTHER deadhead cost -- charged against the completed trip that left the driver
    stranded empty, not against whatever trip comes next. Kept separate from compute_reward()
    above because it's realized after the fact (once a trip's full history is known), not at
    the assignment decision point.
    """
    return trip.post_delivery_deadhead_miles * ASSUMED_OPERATING_COST_PER_MILE


def realized_breakdown_penalty(trip: TripState) -> float:
    """A large, one-time, realized cost -- applied only if this specific trip's history
    actually contains a BREAKDOWN event (sampled via TruckMaintenanceState.sample_breakdown()
    during the sim loop). Deliberately separate from, and much larger than,
    maintenance_risk_penalty in compute_reward() (an averaged EXPECTATION used at the moment of
    choosing a truck) -- this is the real outcome once it's happened, not a risk estimate.
    """
    return ASSUMED_BREAKDOWN_COST_CAD if trip.had_breakdown else 0.0


def late_delivery_penalty(actual_completed_at: datetime, promised_delivery_at: datetime) -> float:
    """Realized-only, same treatment as post_delivery_deadhead_cost/realized_breakdown_penalty:
    how late a trip actually finishes isn't knowable at decision time (dwell times are sampled
    stochastically), only once it completes. A small grace buffer (LATE_GRACE_MINUTES) absorbs
    minor real-world slack before any penalty applies at all. CONVEX in hours late
    (LATE_PENALTY_EXPONENT > 1), not linear -- a customer's satisfaction degrades faster than
    proportionally the longer they wait past what they were promised, not just steadily. The
    cascading "a late trip delays the driver's next trip too" effect needs no extra modeling here
    -- it's already an emergent property of the discrete-event clock (a driver isn't available
    for their next assignment until this trip's real driving_end, whatever that turns out to be).
    """
    minutes_late = (actual_completed_at - promised_delivery_at).total_seconds() / 60
    hours_late = minutes_late / 60 - LATE_GRACE_MINUTES / 60
    return _lateness_penalty_from_hours_late(hours_late)
