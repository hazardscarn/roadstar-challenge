"""Canadian Hours of Service (South of 60°N) -- Project Brief Section 4, constants in
sim/config.py. Tracks BOTH the daily/continuous clocks (13h driving / 14h on-duty / 16h elapsed
window, reset by a 10h off-duty break) AND the rolling weekly cycles (70h/7-day Cycle 1, 120h/
14-day Cycle 2) -- either one can be the binding constraint, and a real driver can be well
within their daily clock while still cycle-capped, or vice versa. Used two ways:

1. Hard feasibility filter (`can_perform`) -- a driver who WOULD violate any of these limits is
   removed from the candidate list before scoring ever happens (Project Brief's "Automated HOS
   Compliance" requirement). This is what keeps every simulated trajectory legally valid, not
   just legally *checked*.
2. Soft risk signal (`stranding_risk`) for the reward function -- a driver can be legally
   cleared to START a trip and still get stranded mid-route if a delay (dock wait, traffic) eats
   their margin. `can_perform` alone can't capture that; `stranding_risk` scores how thin the
   margin is even for a technically-legal assignment.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sim.config import (
    HOS_MAX_DRIVING_HOURS, HOS_MAX_ON_DUTY_HOURS, HOS_MAX_ELAPSED_WINDOW_HOURS,
    HOS_MIN_DAILY_OFF_DUTY_HOURS, HOS_CYCLE_1_MAX_HOURS, HOS_CYCLE_2_MAX_HOURS,
)

DRIVING = 'DRIVING'
ON_DUTY_NOT_DRIVING = 'ON_DUTY_NOT_DRIVING'
OFF_DUTY = 'OFF_DUTY'


@dataclass
class DutyInterval:
    start: datetime
    end: datetime
    status: str  # DRIVING / ON_DUTY_NOT_DRIVING / OFF_DUTY

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass
class HOSLog:
    """One driver's duty-status history. Append-only, in chronological order."""
    driver_id: int
    intervals: list[DutyInterval] = field(default_factory=list)

    def add(self, start: datetime, end: datetime, status: str) -> None:
        if self.intervals and start < self.intervals[-1].end:
            raise ValueError(f'Interval starts before the previous one ends for driver {self.driver_id}')
        self.intervals.append(DutyInterval(start, end, status))

    def _last_qualifying_reset_end(self, before: datetime) -> datetime | None:
        """End time of the most recent OFF_DUTY interval of >=10 consecutive hours before
        `before` -- the daily reset point for the 13h/14h/16h clocks. A 10h off-duty block
        inherently contains the required 8h core rest, so a single threshold covers both.

        Iterates NEWEST-FIRST, not from the start of the log (documents/logs/19's performance
        fix): intervals are strictly chronological (HOSLog.add() enforces non-overlapping,
        increasing order), so the first qualifying match found scanning backward from the end IS
        the most recent one -- returning on the first hit is exactly equivalent to the old
        oldest-first scan-and-keep-overwriting, just without paying O(this driver's entire
        simulated-year history) on every single candidate-scoring call. That full-history scan was
        a real, previously-undiscovered cost: profiled at ~61% of total run time on a full-year
        run (HOSLog.snapshot() alone: 8.2s of 13.35s over a 2000h/1282-trip sample), growing worse
        the further into a run a decision falls, since intervals only ever accumulate.
        """
        for iv in reversed(self.intervals):
            if iv.end > before:
                continue
            if iv.status == OFF_DUTY and iv.hours >= HOS_MIN_DAILY_OFF_DUTY_HOURS:
                return iv.end
        return None

    def _hours_since(self, since: datetime | None, until: datetime, statuses: set[str]) -> float:
        """Iterates NEWEST-FIRST and stops once intervals fall entirely before `since` (same
        chronological-order guarantee as _last_qualifying_reset_end above) -- once one interval's
        END is at or before `since`, every earlier interval is too (strictly increasing order), so
        nothing further can contribute. Bounds the real cost to roughly how many intervals fall
        inside [since, until] (at most ~14 days of a driver's history, the widest window any
        caller ever asks for), not the driver's entire simulated-year log.
        """
        total = 0.0
        for iv in reversed(self.intervals):
            if since is not None and iv.end <= since:
                break
            if iv.status not in statuses:
                continue
            start = max(iv.start, since) if since else iv.start
            end = min(iv.end, until)
            if end > start:
                total += (end - start).total_seconds() / 3600
        return total

    def cycle_hours(self, now: datetime, days: int) -> float:
        """Total on-duty (driving + on-duty-not-driving) hours in the trailing `days`x24h
        window ending at `now` -- the rolling-window definition Cycle 1/2 actually use.
        """
        window_start = now - timedelta(days=days)
        return self._hours_since(window_start, now, {DRIVING, ON_DUTY_NOT_DRIVING})

    def snapshot(self, now: datetime) -> 'HOSState':
        reset = self._last_qualifying_reset_end(now)
        driving_since_reset = self._hours_since(reset, now, {DRIVING})
        on_duty_since_reset = self._hours_since(reset, now, {DRIVING, ON_DUTY_NOT_DRIVING})
        # No qualifying reset found yet means either a brand-new driver (log is empty) or a log
        # that starts mid-shift with no prior rest recorded -- either way, elapsed time since a
        # reset we don't know about should default to 0 (full window available), not infinity.
        # Infinity here was a real bug: it made a fresh driver's 16h window compute as already
        # exhausted (16 - inf -> clamped to 0), the opposite of correct.
        elapsed_since_reset = (now - reset).total_seconds() / 3600 if reset else 0.0
        return HOSState(
            remaining_driving_hours=max(0.0, HOS_MAX_DRIVING_HOURS - driving_since_reset),
            remaining_duty_hours=max(0.0, HOS_MAX_ON_DUTY_HOURS - on_duty_since_reset),
            remaining_elapsed_window_hours=max(0.0, HOS_MAX_ELAPSED_WINDOW_HOURS - elapsed_since_reset),
            remaining_cycle1_hours=max(0.0, HOS_CYCLE_1_MAX_HOURS - self.cycle_hours(now, 7)),
            remaining_cycle2_hours=max(0.0, HOS_CYCLE_2_MAX_HOURS - self.cycle_hours(now, 14)),
        )


@dataclass
class HOSState:
    """A driver's remaining legal hours under every applicable limit, at one point in time.
    The BINDING constraint is whichever is smallest -- a driver can be fine on their daily
    clock and still be cycle-capped (a busy week), or vice versa (a slow week, but pulled a
    long shift today).
    """
    remaining_driving_hours: float
    remaining_duty_hours: float
    remaining_elapsed_window_hours: float
    remaining_cycle1_hours: float
    remaining_cycle2_hours: float

    @property
    def remaining_hours(self) -> float:
        """The single binding number -- how much more on-duty time is actually available
        before SOME limit is hit, whichever one it is.
        """
        return min(
            self.remaining_driving_hours, self.remaining_duty_hours,
            self.remaining_elapsed_window_hours, self.remaining_cycle1_hours,
            self.remaining_cycle2_hours,
        )

    @property
    def binding_constraint(self) -> str:
        values = {
            'driving_13h': self.remaining_driving_hours,
            'on_duty_14h': self.remaining_duty_hours,
            'elapsed_16h': self.remaining_elapsed_window_hours,
            'cycle1_70h_7day': self.remaining_cycle1_hours,
            'cycle2_120h_14day': self.remaining_cycle2_hours,
        }
        return min(values, key=values.get)

    def can_perform(self, planned_driving_hours: float, planned_duty_hours: float) -> bool:
        """Hard feasibility check -- would this trip, taken at face value, fit under EVERY
        limit? Used to remove infeasible drivers from the candidate list before scoring.
        """
        return (
            planned_driving_hours <= self.remaining_driving_hours
            and planned_duty_hours <= self.remaining_duty_hours
            and planned_duty_hours <= self.remaining_elapsed_window_hours
            and planned_duty_hours <= self.remaining_cycle1_hours
            and planned_duty_hours <= self.remaining_cycle2_hours
        )

    def stranding_risk(self, planned_duty_hours: float) -> float:
        """0-1 soft risk score for the reward function: how much of the driver's remaining
        margin this trip is expected to consume. 0 below half the margin (comfortable), ramping
        linearly to 1.0 as planned duration approaches the full remaining budget (a trip that
        exactly maxes out the margin has zero room for a dock delay or traffic before a real
        HOS violation -- legal on paper, high real-world stranding risk).
        """
        margin = self.remaining_hours
        if margin <= 0:
            return 1.0
        ratio = planned_duty_hours / margin
        return max(0.0, min(1.0, (ratio - 0.5) / 0.5))
