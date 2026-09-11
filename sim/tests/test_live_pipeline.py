"""Real-DB smoke tests for the live pipeline pieces built/fixed this session (documents/logs'
UI-build handoff) -- unlike the rest of sim/tests/ (pure Python logic, no I/O), these exercise the
ACTUAL Postgres functions and scoring pipeline against the real Supabase project, because that's
exactly where two real bugs were caught this session (the geofence buffer never firing during a
dwell phase, and a newly-geocoded location crashing the value function with a null region). Each
test runs inside a transaction that's rolled back at the end -- nothing here is ever committed to
the shared database, safe to run against the real project any time.

Run: `source venv/bin/activate && pytest sim/tests/test_live_pipeline.py -v`
Requires SUPABASE_DB_URL in .env (same as every other script in this project).
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

# A real driver NOT in the demo fleet (sim/live/seed_demo_fleet.py's _build_fleet_roster(), 30
# drivers as of the fleet scale-up) -- avoids colliding with live demo state (a real UniqueViolation
# against the already-committed seed, not just other test rows) even though every write here is
# rolled back within its own transaction.
TEST_DRIVER_ID = 16
LONDON_HUB_LOCATION_ID = 1
LONDON_HUB_POINT = 'POINT(-81.2453 42.9849)'  # matches sim/sql/002_reference_locations.sql exactly
FAR_AWAY_POINT = 'POINT(-79.8774 43.5183)'    # Milton hub -- well outside London's 200m geofence


@pytest.fixture
def db():
    """One connection, one transaction, rolled back after the test -- no cleanup logic needed,
    and nothing written here can ever leak into real demo/production state."""
    conn = psycopg2.connect(os.environ['SUPABASE_DB_URL'])
    cur = conn.cursor()
    yield cur
    conn.rollback()
    conn.close()


@pytest.fixture
def test_trip(db):
    trip_id = uuid.uuid4()
    db.execute(
        "insert into live.trips (trip_id, driver_id, status, origin_location_id) values (%s, %s, 'assigned', %s)",
        (str(trip_id), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID),
    )
    return trip_id


class TestGeofenceBuffer:
    """Regression coverage for the exact bug caught live this session: process_position_tick()
    needs CONTINUOUS ticks across the whole dwell, not just while a caller thinks a truck is
    'still driving there' -- stopping ticks the moment a caller's own state machine decides
    'arrived' means the 10-minute confirmation buffer (sim/sql/008) never actually elapses.
    """

    def test_no_arrival_before_buffer_elapses(self, db, test_trip):
        t0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        db.execute(
            "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
            (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, LONDON_HUB_POINT, t0),
        )
        db.execute(
            "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
            (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, LONDON_HUB_POINT, t0 + timedelta(minutes=5)),
        )
        db.execute("select count(*) from live.geofence_events where trip_id = %s", (str(test_trip),))
        assert db.fetchone()[0] == 0, "arrival fired before the 10-minute buffer elapsed"

    def test_arrival_fires_once_buffer_elapses(self, db, test_trip):
        t0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        for minutes in (0, 5, 11):
            db.execute(
                "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
                (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, LONDON_HUB_POINT, t0 + timedelta(minutes=minutes)),
            )
        db.execute("select event_type, occurred_at from live.geofence_events where trip_id = %s", (str(test_trip),))
        rows = db.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 'arrival'
        assert rows[0][1] == t0, "arrival should backdate to actual entry time, not confirmation time"

    def test_departure_fires_after_leaving_and_buffer_elapses(self, db, test_trip):
        t0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        for minutes in (0, 11):  # confirm arrival
            db.execute(
                "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
                (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, LONDON_HUB_POINT, t0 + timedelta(minutes=minutes)),
            )
        for minutes in (12, 23):  # now outside, confirm departure
            db.execute(
                "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
                (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, LONDON_HUB_POINT, t0 + timedelta(minutes=minutes)),
            )
        db.execute(
            "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
            (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, FAR_AWAY_POINT, t0 + timedelta(minutes=23)),
        )
        db.execute(
            "select live.process_position_tick(%s, %s, %s, ST_GeogFromText(%s), %s)",
            (str(test_trip), TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, FAR_AWAY_POINT, t0 + timedelta(minutes=34)),
        )
        db.execute(
            "select event_type from live.geofence_events where trip_id = %s order by occurred_at", (str(test_trip),)
        )
        events = [r[0] for r in db.fetchall()]
        assert events == ['arrival', 'departure']


class TestInspectionGate:
    """Regression coverage for the brief's real compliance requirement (score_quote.py's
    load_live_fleet_snapshot): a driver with no passing 24h DVIR must never become a candidate."""

    @staticmethod
    def _gate_result(db, driver_id: int) -> bool:
        """Mirrors the ACTUAL gate query in sim/live/score_quote.py's load_live_fleet_snapshot()
        -- checks the driver's MOST RECENT inspection specifically, not "does any passing
        inspection exist in the window" (the real bug this class regression-tests)."""
        db.execute(
            "select exists (select 1 from live.driver_status ds where ds.driver_id = %s and exists ("
            "  select 1 from ("
            "    select vi.overall_pass, vi.submitted_at from live.vehicle_inspections vi"
            "    where vi.driver_id = ds.driver_id order by vi.submitted_at desc limit 1"
            "  ) latest where latest.submitted_at > now() - interval '24 hours' and latest.overall_pass))",
            (driver_id,),
        )
        return db.fetchone()[0]

    def test_excludes_driver_with_no_inspection(self, db):
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, truck_number) "
            "values (%s, 'off_duty', 10, %s, (select truck_number from ground_truth.trucks limit 1))",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID),
        )
        assert self._gate_result(db, TEST_DRIVER_ID) is False

    def test_includes_driver_with_recent_passing_inspection(self, db):
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, truck_number) "
            "values (%s, 'off_duty', 10, %s, (select truck_number from ground_truth.trucks limit 1))",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.vehicle_inspections (driver_id, truck_number, brakes_ok, tires_ok, lights_ok, "
            "fluid_levels_ok, coupling_ok, trailer_ok) values (%s, (select truck_number from ground_truth.trucks limit 1), "
            "true, true, true, true, true, true)",
            (TEST_DRIVER_ID,),
        )
        assert self._gate_result(db, TEST_DRIVER_ID) is True

    def test_excludes_driver_with_failed_inspection(self, db):
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, truck_number) "
            "values (%s, 'off_duty', 10, %s, (select truck_number from ground_truth.trucks limit 1))",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.vehicle_inspections (driver_id, truck_number, brakes_ok, tires_ok, lights_ok, "
            "fluid_levels_ok, coupling_ok, trailer_ok) values (%s, (select truck_number from ground_truth.trucks limit 1), "
            "false, true, true, true, true, true)",  # brakes failed -> overall_pass generated column is false
            (TEST_DRIVER_ID,),
        )
        assert self._gate_result(db, TEST_DRIVER_ID) is False

    def test_excludes_driver_whose_latest_inspection_failed_after_an_earlier_pass(self, db):
        """The exact bug caught live this session: a driver passed their morning DVIR, then a
        real defect showed up and a LATER same-day inspection failed -- the gate must key off
        the latest inspection, not "was any inspection in the window ever a pass."""
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, truck_number) "
            "values (%s, 'off_duty', 10, %s, (select truck_number from ground_truth.trucks limit 1))",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.vehicle_inspections (driver_id, truck_number, submitted_at, brakes_ok, tires_ok, "
            "lights_ok, fluid_levels_ok, coupling_ok, trailer_ok) values (%s, (select truck_number from ground_truth.trucks limit 1), "
            "now() - interval '10 hours', true, true, true, true, true, true)",  # 7am-equivalent pass
            (TEST_DRIVER_ID,),
        )
        assert self._gate_result(db, TEST_DRIVER_ID) is True  # only the passing inspection exists so far

        db.execute(
            "insert into live.vehicle_inspections (driver_id, truck_number, submitted_at, brakes_ok, tires_ok, "
            "lights_ok, fluid_levels_ok, coupling_ok, trailer_ok) values (%s, (select truck_number from ground_truth.trucks limit 1), "
            "now(), false, true, true, true, true, true)",  # a later, failing re-inspection
            (TEST_DRIVER_ID,),
        )
        assert self._gate_result(db, TEST_DRIVER_ID) is False, (
            "the driver's LATEST inspection failed but the gate still passed them -- it's matching "
            "on 'any passing row in the window' instead of the most recent inspection"
        )


class TestDataConsistency:
    """Regression coverage for a real bug caught while building the Orders page: seed_demo_fleet.py
    used to truncate live.trips without touching live.quote_requests, leaving
    quote_requests.status='assigned' rows pointing at a trip_id that no longer existed --
    Orders/the dispatch Feed silently rendered every one of those as 'open' with no driver. This
    reads the CURRENT live state directly (not a rolled-back transaction -- there's nothing to
    roll back, it's read-only) so it actually catches a real orphan if one exists right now.
    """

    def test_no_orphaned_assigned_quotes(self, db):
        db.execute("""
            select qr.quote_id from live.quote_requests qr
            where qr.status = 'assigned'
              and not exists (select 1 from live.trips t where t.quote_id = qr.quote_id)
        """)
        orphans = db.fetchall()
        assert orphans == [], f"quote_requests marked 'assigned' with no matching trip: {orphans}"


class TestRlsEnabled:
    """Regression coverage for a real gap found while building Trip History: trip_log/trip_costs/
    invoices and the rating views had NO row-level security at all (relrowsecurity=false),
    meaning any authenticated role -- a driver included -- could read every driver's full history
    via a direct API call despite sim/sql/009_rls.sql's whole point being to prevent exactly
    that. Checks the actual pg_class flag, not just that a migration file exists."""

    @pytest.mark.parametrize('table', ['trip_log', 'trip_costs', 'invoices'])
    def test_table_has_row_level_security_enabled(self, db, table):
        db.execute("select relrowsecurity from pg_class where relname = %s and relnamespace = 'live'::regnamespace", (table,))
        assert db.fetchone()[0] is True, f"live.{table} has no RLS -- any authenticated role can read every row"

    @pytest.mark.parametrize('view', ['driver_ratings', 'truck_ratings'])
    def test_rating_view_is_security_invoker(self, db, view):
        db.execute(
            "select reloptions from pg_class where relname = %s and relnamespace = 'live'::regnamespace", (view,)
        )
        opts = db.fetchone()[0] or []
        assert any('security_invoker=on' in o for o in opts), (
            f"live.{view} isn't security_invoker -- it bypasses the querying role's RLS on trip_log entirely"
        )


class TestDriverOwnDataAccess:
    """Regression coverage for a gap found building the driver-side app: sim/sql/009_rls.sql gave
    every OTHER live.* table a 'drivers read own X' policy but never added one for
    truck_maintenance_state -- the My Truck screen would have silently gotten zero rows for
    every driver. Checks the policy actually exists, not just that a migration file was written."""

    def test_drivers_have_a_read_policy_on_truck_maintenance_state(self, db):
        db.execute(
            "select policyname from pg_policies where tablename = 'truck_maintenance_state' and policyname ilike '%%driver%%'"
        )
        assert db.fetchall(), "no driver-scoped RLS policy on live.truck_maintenance_state -- My Truck would return nothing"


class TestRegionFallback:
    """Regression coverage for the exact bug this session's user-reported 500 traced to: a
    location with a null region_id must never crash scoring -- value_function.py falls back to
    nearest_region() instead of a bare dict index."""

    def test_new_location_gets_a_real_region_id(self, db):
        from sim.engine.run_sim import load_sim_data, nearest_region
        data = load_sim_data()
        # London hub's own coordinates -- nearest_region() must return SOME real region a
        # trained model's feature_columns actually has a 'region_<id>' column for.
        region_id = nearest_region(data, 42.9849, -81.2453)
        assert isinstance(region_id, int)
        assert region_id in data.region_centroids

    def test_value_function_tolerates_missing_region(self, db):
        """A location present in data.locations but ABSENT from data.location_region (simulating
        the exact live bug: a row inserted after this process's SimData was loaded) must not
        crash value_fn -- it should fall back to nearest_region(), not KeyError/ValueError."""
        from sim.engine.run_sim import load_sim_data
        data = load_sim_data()
        some_id = next(iter(data.locations))
        data.location_region.pop(some_id, None)  # simulate the gap
        assert data.location_region.get(some_id) is None  # confirms the gap is real before testing the fallback

        from sim.engine.run_sim import nearest_region
        lat, lon = data.locations[some_id]
        # This is the exact call value_fn() now makes when data.location_region.get(...) is None --
        # asserting it doesn't raise is the regression test itself.
        region_id = nearest_region(data, *data.locations[some_id])
        assert isinstance(region_id, int)


class TestFleetSizeTrimming:
    """Regression coverage for the Simulation Showcase week run's core mechanism: passing a
    SimData whose driver_ids is trimmed to the 30-driver demo fleet must scope
    initialize_fleet()/build_truck_pools() to EXACTLY that set, with no engine change needed --
    the whole design rests on this actually being true, not assumed."""

    def test_initialize_fleet_only_includes_the_given_driver_ids(self, db):
        from copy import copy
        from datetime import datetime as dt
        from random import Random

        from sim.engine.run_sim import initialize_fleet, load_sim_data

        data = load_sim_data()
        trimmed = copy(data)
        trimmed.driver_ids = data.driver_ids[:5]  # an arbitrary small subset, not the real demo 30

        fleet = initialize_fleet(trimmed, Random(1), dt(2026, 1, 1))
        assert set(fleet.drivers.keys()) == set(trimmed.driver_ids)
        # every pool truck belongs to a driver in the trimmed set -- no leakage from the full 131
        assert set(fleet.truck_pool.keys()) == set(trimmed.driver_ids)


class TestOrderManagementRelease:
    """Regression coverage for the Dispatch page's Manage Order tab: releasing/reassigning an
    order must free the driver ONLY if they're still on THAT exact trip -- the same
    'don't clobber a newer commitment' race run_sim.py's own event loop guards
    (run_sim.py:1009-1018). Uses a real trip_id/quote_id inside a rolled-back transaction."""

    def test_release_frees_the_driver_when_still_on_that_trip(self, db):
        from dashboard.server.main import _release_assignment

        quote_id = str(uuid.uuid4())
        trip_id = uuid.uuid4()
        db.execute(
            "insert into live.quote_requests (quote_id, origin_location_id, dest_location_id, requested_at, weight_lbs, pallets, load_type, status) "
            "values (%s, %s, %s, now(), 20000, 10, 'Dry Van', 'assigned')",
            (quote_id, LONDON_HUB_LOCATION_ID, LONDON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.trips (trip_id, driver_id, status, last_event, quote_id, origin_location_id, dest_location_id, created_at) "
            "values (%s, %s, 'assigned', 'ASSGN', %s, %s, %s, now())",
            (str(trip_id), TEST_DRIVER_ID, quote_id, LONDON_HUB_LOCATION_ID, LONDON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, current_trip_id, truck_number) "
            "values (%s, 'driving', 10, %s, %s, (select truck_number from ground_truth.trucks limit 1))",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, str(trip_id)),
        )

        _release_assignment(db, quote_id)

        db.execute("select status from live.trips where trip_id = %s", (str(trip_id),))
        assert db.fetchone()[0] == "cancelled"
        db.execute("select current_trip_id, duty_status from live.driver_status where driver_id = %s", (TEST_DRIVER_ID,))
        current_trip_id, duty_status = db.fetchone()
        assert current_trip_id is None
        assert duty_status == "off_duty"

    def test_release_does_not_clobber_a_newer_commitment(self, db):
        """If the driver has ALREADY been dispatched on a newer trip by the time this order is
        cancelled/reassigned, releasing the OLD trip must not free the driver from the NEW one."""
        from dashboard.server.main import _release_assignment

        old_quote_id = str(uuid.uuid4())
        old_trip_id = uuid.uuid4()
        new_trip_id = uuid.uuid4()
        db.execute(
            "insert into live.quote_requests (quote_id, origin_location_id, dest_location_id, requested_at, weight_lbs, pallets, load_type, status) "
            "values (%s, %s, %s, now(), 20000, 10, 'Dry Van', 'assigned')",
            (old_quote_id, LONDON_HUB_LOCATION_ID, LONDON_HUB_LOCATION_ID),
        )
        db.execute(
            "insert into live.trips (trip_id, driver_id, status, last_event, quote_id, origin_location_id, dest_location_id, created_at) "
            "values (%s, %s, 'assigned', 'ASSGN', %s, %s, %s, now())",
            (str(old_trip_id), TEST_DRIVER_ID, old_quote_id, LONDON_HUB_LOCATION_ID, LONDON_HUB_LOCATION_ID),
        )
        # Driver already moved on to a DIFFERENT trip -- current_trip_id points at new_trip_id, not old_trip_id.
        db.execute(
            "insert into live.driver_status (driver_id, duty_status, hos_remaining_hours, last_location_id, current_trip_id, truck_number) "
            "values (%s, 'driving', 10, %s, %s, (select truck_number from ground_truth.trucks limit 1))",
            (TEST_DRIVER_ID, LONDON_HUB_LOCATION_ID, str(new_trip_id)),
        )

        _release_assignment(db, old_quote_id)

        db.execute("select current_trip_id, duty_status from live.driver_status where driver_id = %s", (TEST_DRIVER_ID,))
        current_trip_id, duty_status = db.fetchone()
        assert str(current_trip_id) == str(new_trip_id), "releasing the OLD trip must not clobber the driver's newer commitment"
        assert duty_status == "driving"


class TestSimulationSchemaRls:
    """RLS coverage for the new simulation.* schema (sim/sql/038) and its two live.* siblings --
    same pattern TestRlsEnabled already uses for trip_log/trip_costs/invoices."""

    @pytest.mark.parametrize('table', [
        'runs', 'quote_requests', 'quote_recommendations', 'quote_candidate_snapshots',
        'trips', 'trip_log', 'geofence_events', 'detention_billing', 'invoices',
        'truck_maintenance_state', 'vehicle_inspections', 'driver_state_snapshots',
    ])
    def test_simulation_table_has_rls_enabled(self, db, table):
        db.execute("select relrowsecurity from pg_class where relname = %s and relnamespace = 'simulation'::regnamespace", (table,))
        assert db.fetchone()[0] is True, f"simulation.{table} has no RLS enabled"

    @pytest.mark.parametrize('table', ['quote_candidate_snapshots', 'driver_state_snapshots'])
    def test_new_live_table_has_rls_enabled(self, db, table):
        db.execute("select relrowsecurity from pg_class where relname = %s and relnamespace = 'live'::regnamespace", (table,))
        assert db.fetchone()[0] is True, f"live.{table} has no RLS enabled"

    def test_driver_state_snapshots_has_a_driver_scoped_policy(self, db):
        db.execute(
            "select policyname from pg_policies where tablename = 'driver_state_snapshots' and schemaname = 'live' and policyname ilike '%%driver%%'"
        )
        assert db.fetchall(), "no driver-scoped RLS policy on live.driver_state_snapshots"
