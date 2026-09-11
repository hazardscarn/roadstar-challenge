"""The trip status state machine -- shared by the simulator and the live system.

Uses the real TMS status vocabulary already in the source data (Dispatch.LAST_FB_STATUS /
sim.trip_events.event_type's CHECK constraint), treated as a proper sequential state machine
instead of the retrospective, adjacency-reconstructed `run_type` leg classifier
(sim/classify.py). `run_type` was built to label messy historical records after the fact and
carries real fragility (duplicate rows, trip-boundary ambiguity -- see
documents/logs/03_run_type_classifier.md and 05_ground_truth_loading.md); reusing that logic as
a live state machine would import the same fragility into something built clean. Here, a trip is
in exactly one status at a time and every transition is an explicit, validated event -- no
double-counting is possible by construction. `run_type`-equivalent labels can still be derived,
once, from a COMPLETED TripState's history (see `summarize()` below) for comparing simulated
output against the real historical distributions -- a one-time summary, not a live counter.

Same module, two callers: the simulator drives `TripState.transition()` from its discrete-event
loop (sim/engine/run_sim.py, not yet built); the live system drives the identical method from
real geofence/status-update events. Neither caller-specific assumption belongs in this file.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TripStatus(str, Enum):
    ASSGN = 'ASSGN'      # order assigned to a driver/truck
    DISP = 'DISP'        # dispatched, moving toward a pickup (loaded=True/False, see TripState)
    ARRSHIP = 'ARRSHIP'  # arrived at shipper (pickup) -- dock dwell begins
    SPTLD = 'SPTLD'      # trailer spotted/dropped at the door (drop-and-hook)
    DOCKED = 'DOCKED'    # backed into a dock door (live load/unload) -- reused at both ends
    PICKD = 'PICKD'      # freight loaded onto the trailer
    DEPSHIP = 'DEPSHIP'  # departed shipper, on route (loaded)
    STOPOFF = 'STOPOFF'  # intermediate stop: branches to a 2nd pickup (LTL) or a partial delivery
    ARRCONS = 'ARRCONS'  # arrived at consignee (delivery) -- dock dwell begins
    COMPLETE = 'COMPLETE'  # delivery finished, bill closed
    DROMT = 'DROMT'      # empty trailer dropped -- truck now empty, awaiting next assignment
    AVAIL = 'AVAIL'      # idle / parked, no active assignment
    BREAKDOWN = 'BREAKDOWN'  # mid-route mechanical failure -- not in the real LAST_FB_STATUS
                             # vocabulary; added because a realized breakdown needs to be a real,
                             # timed state (adding genuine repair delay), not just a quiet
                             # probability baked into the reward number. See
                             # sim/engine/maintenance.py for why this risk is entirely synthesized.


# Which transitions are physically/logically valid. This graph is domain knowledge (standard
# TMS dispatch process), not fit from data -- LAST_FB_STATUS is a single snapshot per leg in the
# source export, so the real historical data cannot support fitting a status-to-status sequence
# at this granularity. Stated explicitly rather than implied.
VALID_TRANSITIONS: dict[TripStatus, set[TripStatus]] = {
    TripStatus.ASSGN: {TripStatus.DISP},
    TripStatus.DISP: {TripStatus.ARRSHIP, TripStatus.BREAKDOWN},
    TripStatus.BREAKDOWN: {TripStatus.DISP},  # repair complete, resume toward the same destination
    TripStatus.ARRSHIP: {TripStatus.SPTLD, TripStatus.DOCKED},
    TripStatus.SPTLD: {TripStatus.PICKD},
    TripStatus.DOCKED: {TripStatus.PICKD, TripStatus.COMPLETE},  # PICKD at pickup, COMPLETE at delivery
    TripStatus.PICKD: {TripStatus.DEPSHIP},
    TripStatus.DEPSHIP: {TripStatus.STOPOFF, TripStatus.ARRCONS},
    TripStatus.STOPOFF: {TripStatus.ARRSHIP, TripStatus.ARRCONS},  # 2nd pickup, or continue to delivery
    TripStatus.ARRCONS: {TripStatus.DOCKED},
    TripStatus.COMPLETE: {TripStatus.ASSGN, TripStatus.DISP, TripStatus.DROMT},
    TripStatus.DROMT: {TripStatus.ASSGN, TripStatus.DISP, TripStatus.AVAIL},
    TripStatus.AVAIL: {TripStatus.ASSGN},
}

# Macro-level branch probabilities the SIMULATOR samples from at each decision point -- these ARE
# calibrated from real historical findings (analysis/data_analysis.ipynb), unlike the transition
# graph above. The live system never samples: it receives a real event and validates it against
# VALID_TRANSITIONS instead. Fill in exact values from calibration.assumptions at load time
# (sim/build_calibration.py); these are the documented starting priors.
TRANSITION_PRIORS = {
    # P(a loaded leg picks up a 2nd shipment before delivering) -- matches the historical
    # LTL/multi-stop share among loaded legs (data_analysis.ipynb Section 1).
    'p_secondary_pickup': 0.07,
    # Post-delivery branch, from data_analysis.ipynb's post-trip-transition analysis (Section 5):
    'p_reload_immediately': 0.38,   # COMPLETE -> ASSGN, no deadhead at all
    'p_real_deadhead': 0.20,        # COMPLETE -> DISP, genuine empty repositioning
    # remainder (~42%) -> COMPLETE -> DROMT -> short/no-op empty leg, matching "same city" share

    # ARRSHIP -> SPTLD vs DOCKED: originally assumed 50/50 (a guess, not a calibrated figure).
    # Checked against the real data (Section 11 of data_analysis.ipynb): DOCKED never appears
    # at all among ON-ON loaded legs (0 occurrences vs 114 SPTLD) -- the assumption was wrong,
    # not just imprecise. Drop-and-hook is effectively this fleet's only real dock pattern.
    'p_spotted_not_docked': 0.99,

    # Multi-leg trips only: an intermediate (non-final) leg's captured status, empirically
    # (Section 11). NOT about a 2nd pickup for revenue -- STOPOFF has zero overlap with the
    # LTL/multi-stop flag; it's a separate, real signal about multi-leg trip structure, used
    # here only to generate a realistic status-event log for a multi-leg trip's middle legs.
    'intermediate_leg_status': {
        'STOPOFF': 0.567, 'COMPLETE': 0.302, 'DEPSHIP': 0.062, 'SPTLD': 0.050,
        'PICKD': 0.003, 'DISP': 0.003, 'ARRCONS': 0.003,
    },
    'p_final_leg_is_complete': 0.992,  # the trip's actual last leg, almost always COMPLETE
}


@dataclass
class StatusEvent:
    status: TripStatus
    at: datetime
    position: tuple[float, float] | None = None  # (lat, lon), None if not geofence-relevant
    loaded: bool = False
    weight_lbs: float = 0.0
    distance_miles: float = 0.0  # distance covered getting TO this event, if a DISP leg


@dataclass
class TripState:
    """One truck's progress through one trip. Accumulates exactly what the reward function
    (sim/engine/reward.py) needs, as it goes -- not recomputed from a separate log afterward.
    """
    trip_id: str
    driver_id: int
    history: list[StatusEvent] = field(default_factory=list)

    @property
    def current(self) -> TripStatus | None:
        return self.history[-1].status if self.history else None

    def transition(self, status: TripStatus, at: datetime, **kwargs) -> None:
        """Validate and record a transition. Raises on an illegal transition -- this is the
        one thing that guarantees no double-counting or impossible sequence, in sim or live.
        """
        current = self.current
        if current is not None and status not in VALID_TRANSITIONS[current]:
            raise ValueError(f'Illegal transition for trip {self.trip_id}: {current} -> {status}')
        self.history.append(StatusEvent(status=status, at=at, **kwargs))

    # --- reward-relevant accumulators -------------------------------------------------------

    def _span_hours(self, start_status: TripStatus, end_status: TripStatus) -> float:
        """Sum, over every occurrence of start_status, of the time to its NEXT subsequent
        end_status -- not just directly-adjacent pairs. A real dwell (e.g. ARRSHIP -> DEPSHIP)
        usually has DOCKED/PICKD/SPTLD in between, so adjacent-pair matching alone finds
        nothing; this instead pairs each start with the first matching end that follows it.
        Handles repeats correctly too (each ARRSHIP, e.g. a 2nd pickup, pairs with its own
        following DEPSHIP).
        """
        total = 0.0
        pending_start = None
        for event in self.history:
            if event.status == start_status:
                pending_start = event.at
            elif event.status == end_status and pending_start is not None:
                total += (event.at - pending_start).total_seconds() / 3600
                pending_start = None
        return total

    @property
    def deadhead_hours(self) -> float:
        """Time spent in DISP while empty, anywhere in this trip's history."""
        total = 0.0
        for prev, nxt in zip(self.history, self.history[1:]):
            if prev.status == TripStatus.DISP and not prev.loaded:
                total += (nxt.at - prev.at).total_seconds() / 3600
        return total

    def _first_index(self, status: TripStatus) -> int | None:
        for i, e in enumerate(self.history):
            if e.status == status:
                return i
        return None

    def _last_index(self, status: TripStatus) -> int | None:
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].status == status:
                return i
        return None

    @property
    def pre_pickup_deadhead_miles(self) -> float:
        """Empty repositioning TO the first pickup -- normal/expected, not a cost to penalize
        (confirmed in analysis/data_analysis.ipynb: this is how a truck gets loaded in the first
        place). Tracked for completeness, not fed into the reward penalty.
        """
        first_arrship = self._first_index(TripStatus.ARRSHIP)
        if first_arrship is None:
            return 0.0
        return sum(
            e.distance_miles for e in self.history[:first_arrship]
            if e.status == TripStatus.DISP and not e.loaded
        )

    @property
    def post_delivery_deadhead_miles(self) -> float:
        """Empty run AFTER the last delivery in this trip -- the real revenue-loss signal (per
        this session's earlier analysis: 327 legs / 6,255 real no-revenue miles, corrected from
        an initially-double-counted 10,679 -- see analysis/data_analysis.ipynb Section 6). This
        is the distance the reward function's deadhead_cost should actually be charged against.
        """
        last_complete = self._last_index(TripStatus.COMPLETE)
        if last_complete is None:
            return 0.0
        return sum(
            e.distance_miles for e in self.history[last_complete:]
            if e.status == TripStatus.DISP and not e.loaded
        )

    @property
    def had_post_delivery_deadhead(self) -> bool:
        return self.post_delivery_deadhead_miles > 0

    @property
    def had_breakdown(self) -> bool:
        return any(e.status == TripStatus.BREAKDOWN for e in self.history)

    @property
    def breakdown_repair_hours(self) -> float:
        """Real elapsed delay from every BREAKDOWN->DISP (repair complete) span -- this is what
        makes a realized breakdown a genuine cost (a missed appointment window, a stranded
        driver) rather than an invisible probability.
        """
        return self._span_hours(TripStatus.BREAKDOWN, TripStatus.DISP)

    @property
    def pickup_dwell_hours(self) -> float:
        """Dock time at the shipper -- ARRSHIP through DEPSHIP (any number of intermediate
        SPTLD/DOCKED/PICKD steps). Feeds detention/lateness risk.
        """
        return self._span_hours(TripStatus.ARRSHIP, TripStatus.DEPSHIP)

    @property
    def delivery_dwell_hours(self) -> float:
        """Dock time at the consignee -- ARRCONS through COMPLETE."""
        return self._span_hours(TripStatus.ARRCONS, TripStatus.COMPLETE)

    @property
    def had_secondary_pickup(self) -> bool:
        """STOPOFF followed by another ARRSHIP -- a real LTL consolidation event, raises
        load_fill_ratio (and therefore revenue) rather than just adding empty-mile cost.
        """
        for prev, nxt in zip(self.history, self.history[1:]):
            if prev.status == TripStatus.STOPOFF and nxt.status == TripStatus.ARRSHIP:
                return True
        return False

    @property
    def total_weight_picked_up(self) -> float:
        """Sum of weight added at every PICKD event -- captures consolidated (multi-pickup)
        loads correctly, where a single 'order_weight' would only capture the first pickup.
        """
        return sum(e.weight_lbs for e in self.history if e.status == TripStatus.PICKD)

    def summarize_run_type(self) -> str:
        """A run_type-equivalent label derived ONCE from a completed trip's real status
        sequence -- for comparing simulated output against analysis/data_analysis.ipynb's real
        historical distributions. Never a live counter; never fed back into the state machine.
        """
        if self.had_secondary_pickup:
            return 'Multi-stop consolidated freight (LTL)'
        if self.deadhead_hours > 0:
            return 'Point-to-point single load (FTL) + deadhead'
        return 'Point-to-point single load (FTL)'
