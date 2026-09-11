"""Regression coverage for live inference's dynamic, future-state-dependent feature computation
(documents/logs/23-24, documents/feature_reference_and_inference_guide.md Section 4, and the real
gap found while building it: /api/assign used to silently overwrite live.driver_status.
current_trip_id on every new booking, orphaning an earlier already-assigned trip -- a real bug,
not just a missing feature, since a real dispatcher books trips days in advance).

Three layers, matching this project's established testing split:
- `TestProjectDriverState`: pure unit tests against project_driver_state() (sim/live/
  score_quote.py) -- no DB, hand-built LiveDriverRow/QueuedTrip inputs, mirrors
  test_home_progress.py's hand-built-state style. Covers both the chain-walk projection AND the
  real availability window (a driver already committed to another job at/around the requested
  pickup time must not be projectable at all -- the "irrespective of the time I select it's
  always the same driver" bug: driver 119 always looked reachable because the whole queue was
  walked unconditionally, regardless of whether pickup_at actually fell before a commitment).
- `TestBuildCandidatesRejectsSchedulingConflicts`: real-DB integration test proving
  build_candidates() itself -- not a reimplementation -- excludes a driver whose ALREADY
  committed next trip would collide with the new order's real occupation window, and includes
  them again once there's real slack.
- `TestAssignSchedulesAdditionalTrips`: real-DB integration test (the `db` fixture, rolled back)
  proving /api/assign itself -- the shipped endpoint, not a reimplementation -- queues a SECOND
  booking as 'scheduled' without touching current_trip_id, the exact real bug this closes.
"""
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
load_dotenv()

from sim.live.score_quote import LiveDriverRow, QueuedTrip, build_candidates, project_driver_state  # noqa: E402
from sim.live.telemetry_simulator import _complete_trip  # noqa: E402
from sim.engine.run_sim import Order, load_sim_data  # noqa: E402

TEST_DRIVER_ID = 16  # matches test_live_pipeline.py's convention -- real driver_id, not in the 30-demo fleet
LONDON_HUB_LOCATION_ID = 1
MILTON_HUB_LOCATION_ID = 2


def _driver(**overrides) -> LiveDriverRow:
    base = dict(
        driver_id=TEST_DRIVER_ID, truck_number="TEST1", duty_status="off_duty",
        hos_remaining_hours=10.0, hos_driving_hours_remaining=10.0, hos_duty_hours_remaining=11.0,
        hos_cycle1_hours_remaining=50.0, hos_cycle2_hours_remaining=90.0,
        # Default: no real idle gap (updated_at == the test's own NOW) -- a test that wants to
        # exercise the qualifying-rest reset overrides this explicitly.
        updated_at=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        last_location_id=LONDON_HUB_LOCATION_ID, trailer_type="Dry Van",
        trailer_capacity_lbs=44500, trailer_capacity_pallets=26, home_terminal_zone="ONLDN",
        truck_pct_km_interval=0.1, truck_pct_days_interval=0.1, truck_maintenance_until=None,
    )
    base.update(overrides)
    return LiveDriverRow(**base)


def _queued_trip(**overrides) -> QueuedTrip:
    base = dict(
        trip_id=uuid.uuid4(), status="scheduled", dest_location_id=MILTON_HUB_LOCATION_ID,
        eta=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc),
        projected_hos_remaining_hours=None, projected_truck_pct_km_interval=None,
        projected_truck_pct_days_interval=None, planned_driving_hours=2.0, planned_duty_hours=3.5,
        planned_completion_at=datetime(2026, 9, 12, 17, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return QueuedTrip(**base)


class TestProjectDriverState:
    NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

    def test_idle_driver_no_queue_returns_real_current_state(self):
        drv = _driver()
        result = project_driver_state(drv, [], self.NOW, self.NOW)
        assert result is not None
        proj, next_trip = result
        assert proj.location_id == LONDON_HUB_LOCATION_ID
        assert proj.effective_start == self.NOW
        assert proj.hos_cycle1_hours_remaining == 50.0
        assert proj.hos_cycle2_hours_remaining == 90.0
        assert next_trip is None

    def test_idle_driver_with_no_known_position_is_not_a_candidate(self):
        drv = _driver(last_location_id=None)
        assert project_driver_state(drv, [], self.NOW, self.NOW) is None

    def test_single_in_progress_trip_uses_real_telemetry_projection(self):
        """An active trip's projected_* fields (telemetry-set, sim/sql/029) are the real source
        of truth for the landing state -- not the assignment-time planned_* estimate. pickup_at
        is set to the trip's real completion so it's SETTLED (this driver is free right at
        pickup_at, the boundary case, not still busy)."""
        drv = _driver()
        trip = _queued_trip(
            status="in_transit", dest_location_id=MILTON_HUB_LOCATION_ID,
            eta=datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc),
            projected_hos_remaining_hours=6.5, projected_truck_pct_km_interval=0.3,
            projected_truck_pct_days_interval=0.2,
        )
        proj, next_trip = project_driver_state(drv, [trip], self.NOW, trip.eta)
        assert proj.location_id == MILTON_HUB_LOCATION_ID
        assert proj.effective_start == trip.eta
        assert proj.hos_duty_hours_remaining == 6.5
        assert proj.truck_pct_km_interval == 0.3
        # cycle clocks reduced by the SAME delta the duty clock dropped (11.0 -> 6.5 = 4.5h)
        assert proj.hos_cycle1_hours_remaining == pytest.approx(50.0 - 4.5)
        assert proj.hos_cycle2_hours_remaining == pytest.approx(90.0 - 4.5)
        assert next_trip is None

    def test_single_scheduled_trip_uses_planned_estimate_not_stale_current_position(self):
        """THE bug this whole feature closes: a driver with a future booked-but-not-started trip
        must be scored from their PROJECTED landing state, not their live-right-now position --
        the worked example from feature_reference_and_inference_guide.md Section 4."""
        drv = _driver()
        trip = _queued_trip(status="scheduled", planned_duty_hours=3.5)
        proj, next_trip = project_driver_state(drv, [trip], self.NOW, trip.planned_completion_at)
        assert proj.location_id == MILTON_HUB_LOCATION_ID  # NOT London -- would be wrong (the bug)
        assert proj.effective_start == trip.planned_completion_at
        assert proj.hos_duty_hours_remaining == pytest.approx(11.0 - 3.5)
        assert proj.hos_cycle1_hours_remaining == pytest.approx(50.0 - 3.5)
        assert next_trip is None

    def test_assigned_not_yet_ticked_behaves_like_scheduled(self):
        """'assigned' (status set at booking time) but telemetry hasn't ticked it yet -- same
        estimate-based projection as 'scheduled', not treated as already in-progress."""
        drv = _driver()
        trip = _queued_trip(status="assigned", planned_duty_hours=2.0)
        proj, _next_trip = project_driver_state(drv, [trip], self.NOW, trip.planned_completion_at)
        assert proj.effective_start == trip.planned_completion_at
        assert proj.hos_duty_hours_remaining == pytest.approx(11.0 - 2.0)

    def test_multi_trip_chain_compounds_across_real_committed_trips(self):
        """The actual chain-walk: 2 already-booked trips, both still 'scheduled', BOTH settled
        before pickup_at -- the driver's projected state must reflect BOTH, landing wherever
        trip 2 ends, with HOS reduced by BOTH trips' planned duty hours, not just the first."""
        drv = _driver()
        trip1 = _queued_trip(
            status="scheduled", dest_location_id=MILTON_HUB_LOCATION_ID,
            eta=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc), planned_duty_hours=3.0,
            planned_completion_at=datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc),
        )
        trip2 = _queued_trip(
            status="scheduled", dest_location_id=LONDON_HUB_LOCATION_ID,
            eta=datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc), planned_duty_hours=4.0,
            planned_completion_at=datetime(2026, 9, 12, 21, 30, tzinfo=timezone.utc),
        )
        proj, next_trip = project_driver_state(drv, [trip1, trip2], self.NOW, trip2.planned_completion_at)
        assert proj.location_id == LONDON_HUB_LOCATION_ID  # trip 2's destination, not trip 1's
        assert proj.effective_start == trip2.planned_completion_at
        # both trips' planned_duty_hours subtracted in sequence: 11.0 - 3.0 - 4.0 = 4.0
        assert proj.hos_duty_hours_remaining == pytest.approx(11.0 - 3.0 - 4.0)
        assert proj.hos_cycle1_hours_remaining == pytest.approx(50.0 - 3.0 - 4.0)
        assert next_trip is None

    def test_idle_driver_with_a_real_qualifying_gap_gets_daily_clocks_back(self):
        """THE gap this test closes: decision_time can be up to a day in the future (real quote
        lead time) -- an idle driver with nothing booked between now and then isn't frozen at
        today's live HOS reading; if the real gap is a qualifying 10h+ rest, their DAILY clocks
        (driving/duty) are back to full by decision_time. Cycle clocks are untouched -- a daily
        rest doesn't restore cycle margin, only a genuine cycle reset does (not modeled here)."""
        drv = _driver(
            hos_driving_hours_remaining=2.0, hos_duty_hours_remaining=2.5,
            hos_cycle1_hours_remaining=20.0, hos_cycle2_hours_remaining=40.0,
            updated_at=datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc),  # 14h before self.NOW
        )
        proj, _next_trip = project_driver_state(drv, [], self.NOW, self.NOW)
        assert proj.hos_driving_hours_remaining == 13.0  # HOS_MAX_DRIVING_HOURS
        assert proj.hos_duty_hours_remaining == 14.0  # HOS_MAX_ON_DUTY_HOURS
        assert proj.hos_cycle1_hours_remaining == 20.0  # untouched
        assert proj.hos_cycle2_hours_remaining == 40.0  # untouched
        assert proj.effective_start == self.NOW

    def test_idle_driver_with_a_short_gap_does_not_get_reset(self):
        """A real gap under the 10h qualifying-rest threshold must NOT reset the daily clocks --
        this is the case that would silently over-credit a driver who's only been idle a couple
        hours if the reset weren't gated on the real threshold."""
        drv = _driver(
            hos_driving_hours_remaining=2.0, hos_duty_hours_remaining=2.5,
            updated_at=datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc),  # 5h before self.NOW
        )
        proj, _next_trip = project_driver_state(drv, [], self.NOW, self.NOW)
        assert proj.hos_driving_hours_remaining == 2.0
        assert proj.hos_duty_hours_remaining == 2.5

    def test_gap_after_a_completed_queue_also_triggers_the_reset(self):
        """The SAME reset applies after a driver's queue clears, not just the pure-idle case --
        a driver whose only committed trip lands well before decision_time, with nothing else
        booked, gets the same real-rest assumption for the remaining gap."""
        drv = _driver(hos_driving_hours_remaining=1.0, hos_duty_hours_remaining=1.0)
        trip = _queued_trip(
            status="scheduled", planned_duty_hours=3.0,
            planned_completion_at=datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc),  # 12h before self.NOW
        )
        proj, _next_trip = project_driver_state(drv, [trip], self.NOW, self.NOW)
        assert proj.hos_driving_hours_remaining == 13.0
        assert proj.hos_duty_hours_remaining == 14.0
        assert proj.effective_start == self.NOW  # now > landing_time -- they'd depart at `now`, not earlier

    def test_multi_trip_chain_never_goes_negative(self):
        """A driver with more committed duty hours than they actually have left is a real
        infeasibility (feasible_candidates()'s HOS hard filter catches it downstream) -- but
        the projection itself must never produce a nonsensical negative remaining-hours figure."""
        drv = _driver(hos_duty_hours_remaining=2.0, hos_cycle1_hours_remaining=5.0)
        trip = _queued_trip(status="scheduled", planned_duty_hours=8.0)
        proj, _next_trip = project_driver_state(drv, [trip], self.NOW, trip.planned_completion_at)
        assert proj.hos_duty_hours_remaining == 0.0
        assert proj.hos_cycle1_hours_remaining == 0.0

    # --- The real availability window (documents/logs -- "irrespective of the time I select
    # it's always the same driver" bug fix): a driver isn't a candidate just because they'll
    # EVENTUALLY be free -- they must be free AT the requested pickup moment specifically. ---

    def test_driver_still_mid_trip_past_pickup_time_is_not_a_candidate(self):
        """An in-progress trip whose real telemetry-tracked completion (trip.eta, kept live by
        telemetry_simulator.py's _write_trip_projection) falls AFTER the requested pickup time --
        this driver is genuinely still on the road at that moment, no matter how far in the
        future decision_time itself is. Must return None, not a projected state."""
        drv = _driver()
        trip = _queued_trip(status="in_transit", eta=datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc))
        pickup_at = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)  # 1h before the trip actually ends
        assert project_driver_state(drv, [trip], self.NOW, pickup_at) is None

    def test_driver_whose_next_trip_already_started_by_pickup_time_is_not_a_candidate(self):
        """A 'scheduled' trip whose own real departure (eta) falls AT OR BEFORE the requested
        pickup time -- this driver would already be out on that other job by then, even though
        it hasn't finished. This is the exact case from the handwritten design note: "does driver
        already have a trip planned to start at Pt... if so NOT qualified."."""
        drv = _driver()
        trip = _queued_trip(
            status="scheduled",
            eta=datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc),  # starts at 11:00
            planned_completion_at=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc),  # ends at 14:00
        )
        pickup_at = datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)  # exactly when it starts
        assert project_driver_state(drv, [trip], self.NOW, pickup_at) is None

    def test_driver_free_in_a_real_gap_projects_only_through_settled_trips(self):
        """THE core fix: a driver with one trip completing well before pickup_at, and a SEPARATE
        future trip starting well AFTER pickup_at, is available in that real gap. The projected
        state must reflect ONLY the settled trip (not the future one, which hasn't happened yet
        as of pickup_at) -- the old behaviour walked the ENTIRE queue unconditionally, which
        would have landed this driver at the future trip's destination with its HOS cost already
        applied, wrongly, for a pickup that happens BEFORE that trip even starts. The future trip
        comes back as `next_trip`, for build_candidates()'s own duration-overlap check."""
        drv = _driver(hos_duty_hours_remaining=11.0, hos_cycle1_hours_remaining=50.0)
        settled_trip = _queued_trip(
            status="scheduled", dest_location_id=MILTON_HUB_LOCATION_ID,
            eta=datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc), planned_duty_hours=2.0,
            planned_completion_at=datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc),
        )
        future_trip = _queued_trip(
            status="scheduled", dest_location_id=LONDON_HUB_LOCATION_ID,
            eta=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc), planned_duty_hours=5.0,
            planned_completion_at=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
        )
        pickup_at = datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)  # in the gap between the two
        result = project_driver_state(drv, [settled_trip, future_trip], self.NOW, pickup_at)
        assert result is not None
        proj, next_trip = result
        assert proj.location_id == MILTON_HUB_LOCATION_ID  # settled trip's destination, NOT London
        assert proj.hos_duty_hours_remaining == pytest.approx(11.0 - 2.0)  # only the settled trip's cost
        assert proj.hos_cycle1_hours_remaining == pytest.approx(50.0 - 2.0)
        assert next_trip is future_trip


@pytest.fixture
def db():
    """Same pattern as test_live_pipeline.py's own db fixture -- one rolled-back transaction."""
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    yield cur
    conn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def sim_data():
    return load_sim_data()


class TestBuildCandidatesRejectsSchedulingConflicts:
    """Real-DB integration coverage of build_candidates()'s duration-overlap check -- the piece
    project_driver_state() itself can't do alone, since it doesn't know the NEW order's own
    deadhead/duration. A driver with a real, already-committed future trip must be REJECTED as a
    candidate when taking the new order would run past that trip's own start time, and ACCEPTED
    once there's real slack -- exercises the shipped build_candidates(), not a reimplementation.
    """

    NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

    def _seed_committed_trip(self, db, next_trip_start: datetime, next_trip_end: datetime):
        trip_id = uuid.uuid4()
        db.execute(
            """insert into live.trips
                 (trip_id, driver_id, status, origin_location_id, dest_location_id, eta,
                  planned_completion_at, weight_lbs, pallets, load_type, created_at)
               values (%s, %s, 'scheduled', %s, %s, %s, %s, 20000, 10, 'Dry Van', now())""",
            (str(trip_id), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, MILTON_HUB_LOCATION_ID,
             next_trip_start, next_trip_end),
        )
        db.connection.commit()  # build_candidates() -> load_driver_trip_queues() opens its OWN cursor()
        return trip_id

    def _order(self):
        return Order(
            order_id=uuid.uuid4(), origin_location_id=LONDON_HUB_LOCATION_ID,
            dest_location_id=MILTON_HUB_LOCATION_ID, created_at=self.NOW,
            weight_lbs=20000.0, pallets=10.0, load_type="Dry Van",
            loaded_miles=30.0, loaded_hours=6.0,  # deliberately long, so occupying the driver runs late
            service_type="FTL", dest_distance_to_hub_km=5.0, dest_local_order_density=0.0,
            requested_pickup_at=self.NOW + timedelta(hours=1),
            decision_time=self.NOW, promised_delivery_at=self.NOW + timedelta(hours=8),
        )

    def test_driver_excluded_when_the_new_order_would_collide_with_their_next_committed_trip(self, sim_data, db):
        """The committed trip starts just 6h out -- taking this ~7h-long new order (deadhead +
        dwell + haul + dwell) from a pickup only 1h out would still have this driver on the road
        when their already-promised trip is due to depart. Must not be offered as a candidate."""
        trip_id = self._seed_committed_trip(
            db, next_trip_start=self.NOW + timedelta(hours=6), next_trip_end=self.NOW + timedelta(hours=9),
        )
        try:
            fleet = [_driver()]
            candidates = build_candidates(sim_data, fleet, self._order(), self.NOW)
            assert candidates == [], "a driver whose next committed trip would collide must be excluded entirely"
        finally:
            db.execute("delete from live.trips where trip_id = %s", (str(trip_id),))
            db.connection.commit()

    def test_driver_included_when_there_is_real_slack_before_their_next_committed_trip(self, sim_data, db):
        """Same driver, same new order -- but their committed trip doesn't start until the next
        day, leaving real slack. Must be offered as a candidate again."""
        trip_id = self._seed_committed_trip(
            db, next_trip_start=self.NOW + timedelta(hours=20), next_trip_end=self.NOW + timedelta(hours=23),
        )
        try:
            fleet = [_driver()]
            candidates = build_candidates(sim_data, fleet, self._order(), self.NOW)
            assert len(candidates) == 1
            assert candidates[0].driver_id == TEST_DRIVER_ID
        finally:
            db.execute("delete from live.trips where trip_id = %s", (str(trip_id),))
            db.connection.commit()


class TestTelemetryPromotesNextScheduledTrip:
    """_complete_trip() (sim/live/telemetry_simulator.py) must promote the driver's earliest
    'scheduled' trip to 'assigned' when the current one finishes -- otherwise a queued future
    booking would sit forever, never picked up by _load_active_trips()'s status filter. Calls the
    real function directly with the test's own rolled-back cursor -- no cross-connection commit
    needed here (unlike the /api/assign test above), since _complete_trip() only ever uses the
    cursor it's given.
    """

    def test_completing_a_trip_promotes_the_next_scheduled_one(self, db):
        current_trip_id = uuid.uuid4()
        scheduled_trip_id = uuid.uuid4()
        later_scheduled_trip_id = uuid.uuid4()

        db.execute("select truck_number from ground_truth.trucks limit 1")
        (truck_number,) = db.fetchone()
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, current_trip_id, truck_number) "
            "values (%s, 'driving', 10, %s, %s)",
            (TEST_DRIVER_ID, str(current_trip_id), truck_number),
        )
        for trip_id, status, eta_offset in (
            (current_trip_id, "in_transit", 0),
            (scheduled_trip_id, "scheduled", 3),
            (later_scheduled_trip_id, "scheduled", 6),
        ):
            db.execute(
                "insert into live.trips (trip_id, driver_id, status, origin_location_id, dest_location_id, eta, weight_lbs, pallets, load_type, created_at) "
                "values (%s, %s, %s, %s, %s, now() + (%s || ' hours')::interval, 20000, 10, 'Dry Van', now())",
                (str(trip_id), TEST_DRIVER_ID, status, LONDON_HUB_LOCATION_ID, MILTON_HUB_LOCATION_ID, eta_offset),
            )
        db.execute(
            "insert into calibration.assumptions (key, value) values ('operating_cost_per_mile', 1.75) on conflict (key) do nothing"
        )

        _complete_trip(
            db, None, current_trip_id, TEST_DRIVER_ID, truck_number,
            LONDON_HUB_LOCATION_ID, MILTON_HUB_LOCATION_ID, 0.5, 0.5, datetime.now(timezone.utc),
        )

        db.execute("select trip_id, status from live.trips where driver_id = %s order by eta", (TEST_DRIVER_ID,))
        rows = {str(tid): status for tid, status in db.fetchall()}
        assert rows[str(current_trip_id)] == "completed"
        assert rows[str(scheduled_trip_id)] == "assigned", "the EARLIEST scheduled trip must be promoted"
        assert rows[str(later_scheduled_trip_id)] == "scheduled", "a LATER scheduled trip must stay queued"


class TestAssignSchedulesAdditionalTrips:
    """Real-DB coverage of the actual bug found and fixed: /api/assign used to unconditionally
    overwrite live.driver_status.current_trip_id on every new booking. Calls the SHIPPED endpoint
    function directly (dashboard/server/main.py's api_assign), not a reimplementation -- the same
    principle test_live_pipeline.py's _release_assignment tests already established.
    """

    def _seed_driver(self, db):
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, truck_number, "
            "hos_driving_hours_remaining, hos_duty_hours_remaining, hos_cycle1_hours_remaining, hos_cycle2_hours_remaining) "
            "values (%s, 'off_duty', 10, %s, (select truck_number from ground_truth.trucks limit 1), 10, 11, 50, 90)",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID),
        )

    def _seed_quote(self, db, quote_id):
        db.execute(
            "insert into live.quote_requests (quote_id, origin_location_id, dest_location_id, requested_at, requested_pickup_at, weight_lbs, pallets, load_type, status) "
            "values (%s, %s, %s, now(), now() + interval '2 hours', 20000, 10, 'Dry Van', 'open')",
            (quote_id, LONDON_HUB_LOCATION_ID, MILTON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.quote_recommendations (quote_id, rank, driver_id, expected_revenue, deadhead_miles, eta_pickup, model_version) "
            "values (%s, 1, %s, 500, 5, now() + interval '2 hours', 'test')",
            (quote_id, TEST_DRIVER_ID),
        )

    def test_second_booking_is_scheduled_not_assigned_and_preserves_current_trip_id(self, db):
        from dashboard.server.main import AssignBody, _state, api_assign
        from sim.engine.run_sim import load_sim_data

        if _state.get("data") is None:
            _state["data"] = load_sim_data()

        quote1, quote2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed_driver(db)
        self._seed_quote(db, quote1)
        self._seed_quote(db, quote2)
        db.connection.commit()  # api_assign opens its OWN cursor() -- needs these rows visible to it

        try:
            result1 = api_assign(AssignBody(quote_id=quote1, driver_id=TEST_DRIVER_ID))
            assert result1["status"] == "assigned"
            first_trip_id = result1["trip_id"]

            result2 = api_assign(AssignBody(quote_id=quote2, driver_id=TEST_DRIVER_ID))
            assert result2["status"] == "scheduled", "a driver already booked must get the new trip queued, not started immediately"

            db.execute("select current_trip_id from live.driver_status where driver_id = %s", (TEST_DRIVER_ID,))
            (current_trip_id,) = db.fetchone()
            assert str(current_trip_id) == first_trip_id, (
                "current_trip_id must still point at the FIRST trip -- the real bug this closes: "
                "a second booking used to silently overwrite it"
            )

            db.execute(
                "select trip_id, status from live.trips where driver_id = %s order by eta", (TEST_DRIVER_ID,)
            )
            rows = db.fetchall()
            assert [str(r[0]) for r in rows] == [first_trip_id, result2["trip_id"]]
            assert [r[1] for r in rows] == ["assigned", "scheduled"]
        finally:
            # This test commits mid-way (api_assign uses its own connection/cursor) -- clean up
            # explicitly rather than relying on the db fixture's rollback, which only covers ITS
            # own connection's writes. The first trip goes 'assigned', so if a real telemetry
            # simulator process happens to be running against this same DB (a real live-demo
            # scenario, not just this test) -- _load_active_trips() filters by TRIP STATUS only,
            # not by driver pool membership, so it does NOT ignore a non-demo-fleet TEST_DRIVER_ID
            # -- it can pick this trip up and write real geofence/position/snapshot rows
            # referencing it. Flipping to a terminal status stops it being selected on telemetry's
            # NEXT poll, but doesn't retroactively stop a tick already in flight -- caught directly
            # TWICE (a stale driver 16 row, mid-cleanup, found sitting in the DB from an earlier
            # run while telemetry was live). A short, deterministic wait longer than one real
            # tick's own polling interval (TICK_SECONDS=4) guarantees telemetry has already made
            # its next _load_active_trips() call and skipped this now-cancelled trip before any
            # dependent-table cleanup below runs -- more reliable than a bare retry-on-FK-error
            # loop, since a caught error leaves this cursor's transaction aborted.
            db.execute("update live.trips set status = 'cancelled' where driver_id = %s", (TEST_DRIVER_ID,))
            db.connection.commit()
            time.sleep(5)
            for tbl in (
                "geofence_dwell_state", "geofence_events", "position_history",
                "trip_costs", "trip_log", "driver_state_snapshots",
            ):
                db.execute(
                    f"delete from live.{tbl} where trip_id in (select trip_id from live.trips where driver_id = %s)",
                    (TEST_DRIVER_ID,),
                )
            db.execute("delete from live.quote_candidate_snapshots where quote_id in (%s, %s)", (quote1, quote2))
            db.execute("delete from live.quote_recommendations where quote_id in (%s, %s)", (quote1, quote2))
            db.execute("delete from live.trips where driver_id = %s", (TEST_DRIVER_ID,))
            db.execute("delete from live.quote_requests where quote_id in (%s, %s)", (quote1, quote2))
            db.execute("delete from live.driver_status where driver_id = %s", (TEST_DRIVER_ID,))
            db.connection.commit()
