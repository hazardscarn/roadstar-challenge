"""Postgres connection helpers -- TWO databases, deliberately:

- SUPABASE_DB_URL (remote): reference.*, ground_truth.*, calibration.*, live.* -- shared
  reference data and the actual live production schema the dashboard/Realtime/Auth depend on.
- LOCAL_DB_URL (local Docker Postgres+PostGIS, port 5433): sim.* and training_transitions --
  pure simulation exhaust. With 1,000+ sim runs generating millions of position-tick/event rows,
  writing that over the network to Supabase would be slow and would burn the project's storage
  on data nobody outside this machine needs. The sim engine and model training both run locally
  anyway (GB10), so this data never needs to leave the machine. See the chat history around
  2026-09-08 for the reasoning; a couple of representative runs get copied to a small `sim.*`
  schema recreated in Supabase later, for demo purposes only -- see sim/copy_sample_runs.py
  (not yet built).

Not the supabase-py client on purpose: everything in sim/ runs bulk pandas-to-SQL jobs and the
event-loop simulator, which are far more naturally expressed as psycopg2/SQLAlchemy against
Postgres directly than through the REST-shaped supabase-py client.
"""
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()
psycopg2.extras.register_uuid()  # lets Python uuid.UUID objects (sim/engine/run_sim.py's IDs) adapt directly


def get_connection():
    """Remote Supabase connection -- reference/ground_truth/calibration/live. A fresh direct
    connection every call (NOT pooled -- see `cursor()`'s own docstring for why the two need to
    behave differently): the one direct caller of this function
    (sim/live/trip_demo_simulator.py) holds the connection open for a whole run and calls
    `conn.close()` itself, which would silently leak a slot from a shared pool forever."""
    db_url = os.environ.get('SUPABASE_DB_URL')
    if not db_url:
        raise RuntimeError(
            'SUPABASE_DB_URL not set in .env. Get it from the Supabase dashboard: '
            'Settings > Database > Connection string (use the "Session pooler" or direct URI, '
            'URL-encode the password if it has special characters).'
        )
    return psycopg2.connect(db_url)


def get_local_connection():
    """Local Docker Postgres+PostGIS connection -- sim.* and training_transitions."""
    db_url = os.environ.get('LOCAL_DB_URL')
    if not db_url:
        raise RuntimeError(
            'LOCAL_DB_URL not set in .env. Expected the local Docker Postgres container '
            '(docker run ... imresamu/postgis:17-3.5, port 5433) -- '
            'postgresql://postgres:localdev@localhost:5433/postgres'
        )
    return psycopg2.connect(db_url)


# Real user feedback + a bug this project already found once before, independently, in
# sim/live/trip_demo_simulator.py's own comment ("an earlier draft that opened a fresh connection
# per tick... added real per-tick network round-trip latency... found directly in a live test
# run"): opening a brand-new connection to the REMOTE Supabase instance for every `cursor()` call
# pays a full TCP+TLS handshake every time -- checked directly on the Dispatch Board's drag-and-
# drop endpoints, this was the dominant cost (~650-750ms for a SINGLE round trip after cutting
# the query count from 9 to 1 -- cutting queries alone wasn't enough). Pooled here, once, for
# every `cursor()` caller across the whole backend, instead of each hot path inventing its own
# hold-the-connection-open workaround (trip_demo_simulator.py's own manual pattern, predating
# this fix, is left as-is -- see get_connection()'s docstring for why it can't share this pool).
# Lazily created (not at import time) so a one-off script that only touches one of local/remote
# never pays for or requires the other's env var. Sized for this project's actual scale (a small
# demo backend, not a high-concurrency production service) -- not tuned further without a real
# load number to justify it.
_pools: dict[str, psycopg2.pool.ThreadedConnectionPool] = {}


def _pool(local: bool) -> psycopg2.pool.ThreadedConnectionPool:
    key = 'local' if local else 'remote'
    if key not in _pools:
        # get_connection()/get_local_connection() raise the real "env var not set" error with
        # setup instructions; opened once here just to validate before handing the same URL to
        # the pool (which opens its own connections internally), then discarded immediately.
        validated = get_local_connection() if local else get_connection()
        validated.close()
        db_url = os.environ['LOCAL_DB_URL'] if local else os.environ['SUPABASE_DB_URL']
        _pools[key] = psycopg2.pool.ThreadedConnectionPool(1, 10, db_url)
    return _pools[key]


@contextmanager
def cursor(commit=True, local=False):
    pool = _pool(local)
    conn = pool.getconn()
    broken = False
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        # A pooled connection MUST go back in a clean (non-aborted-transaction) state, or the
        # next borrower inherits a broken transaction. If even rollback fails, the connection
        # itself is dead (e.g. a dropped network link) -- close it instead of pooling it.
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        pool.putconn(conn, close=broken)


def run_sql_file(path, local=False):
    with open(path) as f:
        sql = f.read()
    with cursor(local=local) as cur:
        cur.execute(sql)


# 005 (sim.*) and 007 (training_transitions) go to the local Postgres container; everything
# else goes to remote Supabase. See the module docstring for why.
LOCAL_FILES = {
    '005_sim.sql', '007_training_transitions.sql', '012_add_breakdown_status.sql',
    '013_add_reward_total.sql', '014_add_decision_state.sql', '015_add_truck_breakdown_risk.sql',
    '016_add_ftl_ltl_and_lane_features.sql', '017_add_lateness_penalty.sql',
    '018_add_more_features.sql', '019_add_next_state.sql', '020_add_next_decision_time.sql',
    '022_add_region_features.sql', '023_add_candidate_scores.sql', '026_add_decision_time_to_orders.sql',
    '027_add_next_truck_maintenance.sql', '028_add_next_truck_maintenance_to_transitions.sql',
    '041_add_home_progress_features.sql',
}


def apply_all_migrations(sql_dir='sim/sql'):
    """Apply every 0xx_*.sql file in sim/sql, in filename order, routed to local or remote per
    LOCAL_FILES above. Idempotent where the files themselves are (create table if not exists /
    unique-index-guarded inserts) -- most `create table` statements here are NOT `if not
    exists`, so re-running from a non-empty schema will error; that's intentional (fail loud on
    a state you didn't expect) rather than silently skipping a schema change.
    """
    import glob
    for path in sorted(glob.glob(f'{sql_dir}/*.sql')):
        is_local = os.path.basename(path) in LOCAL_FILES
        print(f'applying {path} ({"local" if is_local else "remote"})...')
        run_sql_file(path, local=is_local)
    print('all migrations applied.')


if __name__ == '__main__':
    apply_all_migrations()
