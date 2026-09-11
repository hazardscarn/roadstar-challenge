"""Regression coverage for _compute_cycle() (dashboard/server/main.py) -- the Order Story's real
CYCLE view (home base -> home base), added after real user feedback: the order book's "+$X saved"
badge was showing on trips whose own story never explained where the $ came from (it belongs to
the NEXT trip's candidate comparison, not this trip's own), and the story only ever showed the one
trip immediately before/after instead of the driver's WHOLE cycle, labeled P1/D1/P2/D2/.../H.

Real-DB test (the `db` fixture, rolled back) -- seeds a real, minimal simulation.* trip history for
TEST_DRIVER_ID (real driver_id=16, not in the 30-demo fleet, matching this project's established
convention) and calls _compute_cycle() directly with that SAME cursor, no cross-connection commit
needed since it takes `cur` as a param.

Driver 16's real home hub comes from calibration.driver_home_hub (sim/sql/045, sim/
calibrate_driver_home_hub.py) -- a real historical-leg-derived anchor, or a real-data-informed
ASSUMED one from 4 real anchor points (London/Milton's real terminals, Barrie/Niagara Falls via a
real customer location in each), NOT hardcoded here as a magic constant -- queried once at module
load via driver_home_hub_id() itself, so this test stays correct if the calibration is ever rerun
and lands driver 16 on a different one of the 4 anchors.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
load_dotenv()

from dashboard.server.main import _compute_cycle, _state  # noqa: E402
from sim.config import ASSUMED_OPERATING_COST_PER_MILE  # noqa: E402
from sim.engine.run_sim import driver_home_hub_id, load_sim_data  # noqa: E402

TEST_DRIVER_ID = 16  # real driver_id, not in the 30-demo fleet
LONDON_HUB = 1
MILTON_HUB = 2
CUSTOMER_KITCHENER = 5659  # a real reference.locations row, used as a mid-cycle stop


@pytest.fixture(scope="module", autouse=True)
def _ensure_sim_data_loaded():
    if _state.get("data") is None:
        _state["data"] = load_sim_data()


@pytest.fixture
def home_hub(_ensure_sim_data_loaded) -> int:
    """TEST_DRIVER_ID's REAL home-hub location_id, from calibration.driver_home_hub -- queried,
    not hardcoded, so this suite stays correct across a recalibration (sim/calibrate_driver_
    home_hub.py) even if it lands driver 16 on a different one of the 4 real anchor points."""
    return driver_home_hub_id(_state["data"], TEST_DRIVER_ID)


@pytest.fixture
def db():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    yield cur
    conn.rollback()
    conn.close()


def _seed_run(db, run_id):
    db.execute(
        "insert into simulation.runs (run_id, seed, created_at, week_start, week_end) values (%s, 1, now(), now(), now())",
        (str(run_id),),
    )


def _seed_trip(db, run_id, trip_id, quote_id, driver_id, truck_number, origin_id, dest_id, eta,
                completed_at, loaded_miles, pre_pickup_deadhead, post_delivery_deadhead, revenue):
    db.execute(
        """insert into simulation.quote_requests (quote_id, run_id, origin_location_id, dest_location_id, requested_at, requested_pickup_at, status)
           values (%s, %s, %s, %s, %s, %s, 'assigned')""",
        (str(quote_id), str(run_id), origin_id, dest_id, eta, eta),
    )
    db.execute(
        """insert into simulation.trips
             (trip_id, run_id, driver_id, status, eta, origin_location_id, dest_location_id, created_at,
              weight_lbs, pallets, load_type, loaded_miles, pre_pickup_deadhead_miles, quote_id)
           values (%s, %s, %s, 'completed', %s, %s, %s, now(), 20000, 10, 'Dry Van', %s, %s, %s)""",
        (str(trip_id), str(run_id), driver_id, eta, origin_id, dest_id, loaded_miles, pre_pickup_deadhead, str(quote_id)),
    )
    db.execute(
        """insert into simulation.trip_log
             (trip_id, run_id, driver_id, truck_number, completed_at, loaded_miles,
              pre_pickup_deadhead_miles, post_delivery_deadhead_miles, on_time, load_fill_ratio,
              order_revenue, deadhead_cost, post_delivery_deadhead_cost, lateness_penalty_amount)
           values (%s, %s, %s, %s, %s, %s, %s, %s, true, 0.5, %s, 0, 0, 0)""",
        (str(trip_id), str(run_id), driver_id, truck_number, completed_at, loaded_miles,
         pre_pickup_deadhead, post_delivery_deadhead, revenue),
    )


def _seed_candidate(db, run_id, quote_id, driver_id, truck_number, deadhead_miles, was_assigned):
    db.execute(
        """insert into simulation.quote_candidate_snapshots
             (run_id, quote_id, driver_id, truck_number, deadhead_miles, score, rank, was_assigned, scored_at)
           values (%s, %s, %s, %s, %s, 0, 1, %s, now())""",
        (str(run_id), str(quote_id), driver_id, truck_number, deadhead_miles, was_assigned),
    )


class TestComputeCycle:
    def test_single_trip_closing_at_home_hub_is_closed_via_trip_with_zero_empty_return(self, db, home_hub):
        """The simplest real case: one trip whose own destination IS the driver's home hub --
        closed_via must be 'trip', empty_return_miles exactly 0 (never estimated when a real trip
        already lands there)."""
        run_id, trip_id, quote_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        now = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)
        _seed_run(db, run_id)
        other_hub = LONDON_HUB if home_hub == MILTON_HUB else MILTON_HUB
        _seed_trip(db, run_id, trip_id, quote_id, TEST_DRIVER_ID, "B3339", other_hub, home_hub,
                   now, now + timedelta(hours=2), 30.0, 5.0, 0.0, 200.0)

        cycle = _compute_cycle(db, run_id, TEST_DRIVER_ID, trip_id)
        assert cycle is not None
        assert cycle["closed_via"] == "trip"
        assert cycle["empty_return_miles"] == 0.0
        assert cycle["n_trips"] == 1
        assert cycle["trips"][0]["pickup_label"] == "P1"
        assert cycle["trips"][0]["dropoff_label"] == "D1"
        assert cycle["trips"][0]["is_current"] is True

    def test_cycle_still_away_when_data_runs_out_is_closed_via_assumed(self, db):
        """A driver whose trip sequence for this run simply ends away from home -- closed_via must
        be 'assumed', with a real, non-zero, ROUTED empty_return_miles/value -- never silently 0."""
        run_id, trip_id, quote_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        now = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)
        _seed_run(db, run_id)
        _seed_trip(db, run_id, trip_id, quote_id, TEST_DRIVER_ID, "B3339", LONDON_HUB, CUSTOMER_KITCHENER,
                   now, now + timedelta(hours=2), 40.0, 0.0, 0.0, 150.0)

        cycle = _compute_cycle(db, run_id, TEST_DRIVER_ID, trip_id)
        assert cycle["closed_via"] == "assumed"
        assert cycle["empty_return_miles"] > 0
        # Both figures are independently rounded from the same unrounded real distance -- compare
        # with tolerance for that rounding, not bit-exact reconstruction from the already-rounded miles.
        assert cycle["empty_return_value"] == pytest.approx(cycle["empty_return_miles"] * ASSUMED_OPERATING_COST_PER_MILE, abs=0.1)

    def test_trajectory_is_the_clean_pickup_to_dropoff_leg_not_the_full_deadhead_path(self, db):
        """THE real bug this closes: the stored simulation.trips.trajectory column is the truck's
        FULL real path (pre-pickup deadhead + dwell + the loaded leg, concatenated) -- reusing it
        for the cycle view put the P1 pin at wherever the deadhead STARTED (a real, unrelated
        prior location), not the real pickup, and bundled every trip's own deadhead leg into the
        same map, reading as the road itself forking near a shared corridor. _compute_cycle() must
        fetch a clean, deadhead-free origin->dest geometry instead -- first point at (approximately)
        the real pickup coordinates, last point at the real drop-off coordinates."""
        run_id, trip_id, quote_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        now = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)
        _seed_run(db, run_id)
        # A large real pre_pickup_deadhead_miles -- if this leaked into the trajectory (the old
        # bug), the first point would be far from Milton; a clean fetch starts right at Milton.
        _seed_trip(db, run_id, trip_id, quote_id, TEST_DRIVER_ID, "B3339", MILTON_HUB, LONDON_HUB,
                   now, now + timedelta(hours=2), 30.0, 89.1, 0.0, 200.0)

        cycle = _compute_cycle(db, run_id, TEST_DRIVER_ID, trip_id)
        traj = cycle["trips"][0]["trajectory"]
        assert len(traj) >= 2
        first_lat, first_lon = traj[0][1], traj[0][2]
        last_lat, last_lon = traj[-1][1], traj[-1][2]
        # Milton hub's real coordinates (reference.locations) -- not London's, which is where a
        # deadhead-polluted trajectory would have wrongly started instead.
        assert first_lat == pytest.approx(43.5183, abs=0.01)
        assert first_lon == pytest.approx(-79.8774, abs=0.01)
        assert last_lat == pytest.approx(42.9849, abs=0.01)
        assert last_lon == pytest.approx(-81.2453, abs=0.01)

    def test_reload_savings_value_nets_out_the_chosen_candidates_own_deadhead(self, db, home_hub):
        """THE real gap this closes: the order-book's own reload_immediate badge used to average
        just the OTHER candidates' deadhead, without netting out the chosen candidate's own
        (usually near-zero, but not always exactly zero) deadhead for that next job -- this
        function must, matching Section 3's deadhead_vs_avg_alternative formula exactly."""
        run_id = uuid.uuid4()
        trip1_id, quote1_id = uuid.uuid4(), uuid.uuid4()
        trip2_id, quote2_id = uuid.uuid4(), uuid.uuid4()
        now = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)
        _seed_run(db, run_id)
        other_hub = LONDON_HUB if home_hub == MILTON_HUB else MILTON_HUB
        # Trip 1: the OTHER hub -> Kitchener, reloads immediately (post_delivery_deadhead=0), a
        # real freight move -- deliberately NOT home yet, so this cycle stays open for trip 2.
        _seed_trip(db, run_id, trip1_id, quote1_id, TEST_DRIVER_ID, "B3339", other_hub, CUSTOMER_KITCHENER,
                   now, now + timedelta(hours=2), 30.0, 0.0, 0.0, 100.0)
        # Trip 2 (the NEXT job): Kitchener -> home hub (closes the cycle), driver already there = 0 deadhead.
        _seed_trip(db, run_id, trip2_id, quote2_id, TEST_DRIVER_ID, "B3339", CUSTOMER_KITCHENER, home_hub,
                   now + timedelta(hours=2), now + timedelta(hours=4), 30.0, 0.0, 0.0, 100.0)
        # Real OTHER candidates that WOULD have needed real deadhead to reach quote 2 -- the actual
        # counterfactual this $ figure is meant to price.
        _seed_candidate(db, run_id, quote2_id, TEST_DRIVER_ID, "B3339", 0.0, True)
        _seed_candidate(db, run_id, quote2_id, 17, "B8269", 20.0, False)
        _seed_candidate(db, run_id, quote2_id, 18, "B5794", 30.0, False)

        cycle = _compute_cycle(db, run_id, TEST_DRIVER_ID, trip1_id)
        assert cycle["n_trips"] == 2
        trip1_out = next(t for t in cycle["trips"] if t["trip_id"] == str(trip1_id))
        assert trip1_out["reload_immediate"] is True
        # avg(20, 30) - chosen(0) = 25.0 miles -> 25.0 * 1.75 = 43.75
        assert trip1_out["reload_savings_value"] == pytest.approx(25.0 * ASSUMED_OPERATING_COST_PER_MILE)
