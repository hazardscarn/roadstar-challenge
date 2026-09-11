"""Truck maintenance risk -- entirely SYNTHESIZED, not derived from data. Checked first:
Trucks (data/1788655393951_Hackathon_Data.xlsx) has exactly one column, TRUCK_NUMBER -- no
odometer, no service history, no make/model/year anywhere in the source export. There is nothing
real to calibrate a breakdown model against, so this is a labeled assumption end to end, matching
the same rigor as sim/config.py's synthesized $-rates.

Why build it anyway: without SOME cost tied to overusing a single truck, the ADP value function
has no reason to ever prefer rotating trucks over always picking the same nearest/best-scoring
one -- a real dispatch consideration the model should learn to weigh, even on a synthesized
signal. Same mechanic feeds both the simulator's reward penalty AND the live dashboard's
maintenance-warning panel (live.truck_maintenance_state, sim/sql/006_live.sql) -- one hazard
function, two consumers.
"""
import random
from dataclasses import dataclass

from sim.config import (
    ASSUMED_BREAKDOWN_COST_CAD, BREAKDOWN_RISK_AT_DUE, BREAKDOWN_RISK_MAX, MAINTENANCE_SERVICE_INTERVAL_DAYS,
    MAINTENANCE_SERVICE_INTERVAL_KM, PROACTIVE_MAINTENANCE_PROB_PER_TRIP, PROACTIVE_MAINTENANCE_THRESHOLD_PCT,
)


@dataclass
class TruckMaintenanceState:
    """Two independent dimensions of overdue-ness, matching how real fleet maintenance is
    actually scheduled (every N km OR every M days, whichever comes first) -- a truck driven
    hard but recently could be km-overdue while calendar-fresh, and a truck driven lightly but
    sitting since a long-ago service could be calendar-overdue while km-fresh. breakdown_risk
    uses whichever dimension is worse.
    """
    truck_number: str
    cumulative_km_since_service: float = 0.0
    days_since_service: float = 0.0
    service_interval_km: float = MAINTENANCE_SERVICE_INTERVAL_KM
    service_interval_days: float = MAINTENANCE_SERVICE_INTERVAL_DAYS

    @property
    def pct_of_km_interval(self) -> float:
        return self.cumulative_km_since_service / self.service_interval_km

    @property
    def pct_of_days_interval(self) -> float:
        return self.days_since_service / self.service_interval_days

    @property
    def pct_of_interval(self) -> float:
        """Whichever dimension is more overdue drives risk -- 'whichever comes first', not an average."""
        return max(self.pct_of_km_interval, self.pct_of_days_interval)

    @property
    def needs_warning(self) -> bool:
        """Matches the dashboard's "service due soon" badge threshold (85% of interval)."""
        return self.pct_of_interval >= 0.85

    @property
    def breakdown_risk(self) -> float:
        """SOURCED, not the original hand-picked curve (documents/logs/19): real truckload
        dry-van fleets average 14,991 miles between roadside breakdowns (TMC/FleetNet America
        benchmarking data), and this fleet's own real average trip length is ~53.5 miles -- so a
        truck AT its due point should break down on roughly 1 trip in 280 (53.5/14,991 ~= 0.36%),
        not the original curve's 15% at-due / 30%-capped-far-overdue figures (~40-80x too high --
        that inflated rate was what made V(s) learn an extreme, score-dominating cliff around the
        85% overdue threshold, see sim/engine/value_function.py's fix docstring).

        0 below 70% of the service interval; ramps linearly to BREAKDOWN_RISK_AT_DUE (0.0036,
        the real dry-van rate) at 100%; creeps up past that if driven beyond the interval without
        service, capped at BREAKDOWN_RISK_MAX (0.01, roughly 3x the at-due rate -- still a real,
        felt cost for neglect, not 30%-per-trip implosion). This is what sample_breakdown() draws
        from -- a proper Bernoulli roll at this probability every trip, so breakdowns show up as
        genuinely RARE events (matching the real ~0.36% rate), weighted toward overdue trucks
        without dominating every other consideration in the reward.
        """
        pct = self.pct_of_interval
        if pct < 0.7:
            return 0.0
        if pct <= 1.0:
            return (pct - 0.7) / 0.3 * BREAKDOWN_RISK_AT_DUE
        return min(BREAKDOWN_RISK_MAX, BREAKDOWN_RISK_AT_DUE + (pct - 1.0) * (BREAKDOWN_RISK_MAX - BREAKDOWN_RISK_AT_DUE))

    def expected_breakdown_cost(self) -> float:
        """The reward-penalty term: probability x the SAME realistic, sourced breakdown cost
        (ASSUMED_BREAKDOWN_COST_CAD) that gets charged in full if a breakdown is actually realized
        -- see that constant's docstring for why using two different bases here was a real bug,
        not just an approximation.
        """
        return self.breakdown_risk * ASSUMED_BREAKDOWN_COST_CAD

    def after_trip(self, distance_miles: float, hours_elapsed: float = 0.0) -> 'TruckMaintenanceState':
        """A new state reflecting this trip's distance AND wall-clock time added. Immutable-style
        (returns a new instance) so a simulation step doesn't need to worry about aliasing.
        """
        km = distance_miles * 1.60934
        return TruckMaintenanceState(
            truck_number=self.truck_number,
            cumulative_km_since_service=self.cumulative_km_since_service + km,
            days_since_service=self.days_since_service + hours_elapsed / 24,
            service_interval_km=self.service_interval_km,
            service_interval_days=self.service_interval_days,
        )

    def serviced(self) -> 'TruckMaintenanceState':
        """A new state after a maintenance event resets BOTH counters."""
        return TruckMaintenanceState(
            truck_number=self.truck_number, cumulative_km_since_service=0.0, days_since_service=0.0,
            service_interval_km=self.service_interval_km, service_interval_days=self.service_interval_days,
        )

    def sample_breakdown(self, rng: random.Random) -> bool:
        """One Bernoulli draw for THIS trip: does the truck actually break down mid-route?
        Distinct from expected_breakdown_cost() (a probability-weighted average used when
        SCORING which truck to pick) -- this is the realized outcome for one specific trip.
        """
        return rng.random() < self.breakdown_risk

    def sample_proactive_maintenance(self, rng: random.Random) -> bool:
        """documents/logs/19 -- real fleets service trucks proactively, not only after a
        breakdown; without this a truck in a long simulated year had NO path back to a healthy
        state at all (serviced() existed but was never called from the sim loop -- a real gap).
        Only rolled once a truck crosses PROACTIVE_MAINTENANCE_THRESHOLD_PCT (the same 85%
        "due soon" line the dashboard warning uses) -- a fresh truck is never randomly pulled in
        for service, matching how real scheduling only kicks in once due.
        """
        if self.pct_of_interval < PROACTIVE_MAINTENANCE_THRESHOLD_PCT:
            return False
        return rng.random() < PROACTIVE_MAINTENANCE_PROB_PER_TRIP


def sample_repair_hours(rng: random.Random) -> float:
    """SYNTHESIZED ASSUMPTION -- no real repair-time data exists. A roadside mechanical
    breakdown plausibly takes 2-8 hours to resolve (tow, diagnose, repair or swap trucks);
    uniform across that range in the absence of anything better to calibrate against.
    """
    return rng.uniform(2, 8)


def initialize_fleet(truck_numbers: list[str], rng: random.Random) -> dict[str, TruckMaintenanceState]:
    """Starting maintenance state for every truck in the fleet -- NOT all fresh (cumulative_km=0,
    days=0). A real fleet has trucks at every point in their service cycle, including some
    already overdue (maintenance scheduling in practice isn't perfectly proactive). Uniform
    across [0, 1.15x the service interval] on BOTH dimensions, sampled INDEPENDENTLY (a truck can
    be km-fresh but calendar-overdue, or vice versa -- they aren't the same clock), so some
    trucks start already past-due on one or both and carry real breakdown risk from day one.
    SYNTHESIZED -- no real fleet-age data exists to calibrate this distribution against either,
    same caveat as everything else in this module.
    """
    return {
        truck_number: TruckMaintenanceState(
            truck_number=truck_number,
            cumulative_km_since_service=rng.uniform(0, MAINTENANCE_SERVICE_INTERVAL_KM * 1.15),
            days_since_service=rng.uniform(0, MAINTENANCE_SERVICE_INTERVAL_DAYS * 1.15),
        )
        for truck_number in truck_numbers
    }
