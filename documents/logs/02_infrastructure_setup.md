# Infrastructure Setup: Supabase, OSRM, Local Postgres

**What:** Provisioned the databases and the road-routing service everything else depends on.

## Two databases, on purpose

- **Remote (Supabase)**: `reference`, `ground_truth`, `calibration`, `live` schemas. Connection
  string in `.env` as `SUPABASE_DB_URL`.
- **Local (Docker container, `imresamu/postgis:17-3.5`, port 5433)**: `sim` schema +
  `training_transitions`. Connection string in `.env` as `LOCAL_DB_URL`.

**Why split**: `sim.*`/`training_transitions` is pure simulation exhaust — with 1,000+ planned
simulation runs generating potentially millions of position-tick/event rows, writing all of that
over the network to Supabase would be slow and would consume the project's free-tier storage on
data nobody outside this machine needs to see. The simulation engine and model training both run
locally on this machine anyway, so that data never needs to leave it. `reference`/`ground_truth`/
`calibration`/`live` stay on Supabase because the dashboard, Realtime, and Auth genuinely need
them to be reachable remotely.

**Consequence**: `sim.sql`'s tables have no foreign-key constraints on `driver_id`/
`truck_number`/`location_id` — those reference rows that live in the *other* database, and
Postgres can't enforce a foreign key across two separate instances. Referential integrity there is
guaranteed by the application (the simulation engine loads `ground_truth`/`reference` from remote
into memory once at startup and only ever writes back IDs it got from that source), not the
database. See `sim/db.py`'s module docstring for the exact reasoning.

**Later, for the demo**: a small number of representative simulation runs (not all 1,000+) will
be copied into a `sim.*` schema recreated in Supabase, purely so the dashboard/demo can show a
real sample run — the "we ran 1,000+ of these locally for training" claim stays backed by the
real local data; only a demo-sized sample is duplicated remotely. Not yet built.

## OSRM (road routing)

**What it's for**: real driving distance/duration/route geometry between two known points —
Section 2 of `research/roadstar_platform_plan.md`. Used to (a) cache lane distances for the
simulator instead of recomputing per tick, (b) draw real route breadcrumbs on the dashboard map,
(c) interpolate a truck's position along an actual road path during simulation.

**Setup**: `ops/osrm_setup.sh` — downloads the Ontario OpenStreetMap extract
(`download.geofabrik.de`), then runs `osrm-extract` → `osrm-partition` → `osrm-customize` via
Docker, then can serve on port 5000 (`osrm-routed --algorithm mld`).

**A real blocker hit and fixed**: this machine is **arm64** (NVIDIA GB10, Grace-Blackwell), and
the official `osrm/osrm-backend` image on Docker Hub is amd64-only — it failed with
`exec format error`. Fixed by switching to `ghcr.io/project-osrm/osrm-backend:latest`, which
publishes a native arm64 build. Verified working: queried a real route from the London terminal to
the Milton terminal and got back 143.4km / ~114min, a genuine driving-network result, not a
straight-line estimate.

**Why self-hosted instead of the public OSRM demo server**: no external dependency at demo time,
no shared-server rate-limit risk, and it lets the system route arbitrary uncached lanes live if a
judge asks for a quote outside the pre-cached set.

## Database schema (10 migration files, `sim/sql/001`-`010`)

Applied in order, each to the database it belongs to (per the split above):

| File | Creates | Where |
|---|---|---|
| `001_schemas_extensions.sql` | 5 schemas, `postgis` + `pg_cron` extensions | remote |
| `002_reference_locations.sql` | `reference.locations` + the 2 terminal hub rows | remote |
| `003_ground_truth.sql` | cleaned historical data tables (drivers, trucks, trailers, orders, legs) | remote |
| `004_calibration.sql` | distributions the simulator draws from + `calibration.assumptions` | remote |
| `005_sim.sql` | simulation-run tables (orders, assignments, HOS transitions, GPS ticks) | **local** |
| `006_live.sql` | the real-time production schema (driver/truck status, trips, geofencing, detention, quotes, inspections, maintenance) | remote |
| `007_training_transitions.sql` | the flat table the ML model trains on | **local** |
| `008_geofence_function.sql` | the buffered arrival/departure detection function (see below) | remote |
| `009_rls.sql` | `profiles` table + row-level security (manager vs. driver) | remote |
| `010_cron_detention.sql` | the 5-minute detention-billing cron job | remote |

**Why `006_live.sql` is applied before `007_training_transitions.sql` is finalized**: the model
should only ever be trained on features the live system can actually supply at real quote time.
Locking the live schema's shape first, then designing the training table to match it, avoids
training on a convenience column production can't produce.

**`pg_cron` confirmed available** on this Supabase project's tier (version 1.6.4) — the detention
job runs as a real scheduled job, not the view-based fallback that was prepared in case it wasn't
available.

## The geofence function (`live.process_position_tick`)

A Postgres function, called once per new position tick per (trip, driver): fires an **arrival**
event only after a truck's position has been continuously *inside* a location's radius for 10
minutes (not on first crossing — avoids false triggers from a truck looping near a gate or idling
in a queue just outside the line), and a **departure** event symmetrically, after 10 minutes
continuously *outside*. Radius and buffer are per-location config
(`reference.locations.radius_m`/`buffer_minutes`), not hardcoded. The same function will be called
by both the simulator and, later, any real GPS ingestion — one implementation, two callers.
