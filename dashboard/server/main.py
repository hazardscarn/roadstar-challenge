"""Local FastAPI backend for the RoadStar dashboard (local-first per the build plan -- no Vercel).

Wraps the already-proven `sim/live/score_quote.py` pipeline directly -- one scoring codepath,
sim and live alike, no reimplementation (see that module's own docstring). Writes go through
`sim/db.py`'s SUPABASE_DB_URL connection (a direct superuser Postgres connection), the same
pattern the simulator itself already uses -- this bypasses RLS/PostgREST for backend-only writes,
which is fine: RLS's job is to gate what the BROWSER can read/write directly, not this trusted
local process. The one client-side write path (driver DVIR submission) goes straight from the
frontend via supabase-js + RLS instead (sim/sql/032's INSERT grant on vehicle_inspections).

Run: `source venv/bin/activate && uvicorn dashboard.server.main:app --port 8787 --reload`
(from the repo root, so `sim.*` imports resolve).
"""
import os
import json
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# Repo root on sys.path so `sim.*` imports work regardless of cwd uvicorn was launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sim.db import cursor  # noqa: E402
from sim.engine.route_interpolation import get_route_geometry, interpolate_position  # noqa: E402
from sim.engine.run_sim import get_route, load_sim_data, run_simulation  # noqa: E402
from sim.engine.value_function import load_state_value_model, make_value_fn  # noqa: E402
from sim.live import dispatch_board, telemetry_simulator  # noqa: E402
from sim.live.score_quote import QuoteRequest, score_quote  # noqa: E402
from sim.live.trip_demo_simulator import SCENARIOS, run_trip_demo, start_trip_demo  # noqa: E402

app = FastAPI(title="RoadStar Dispatch API")
# Local dev origins always allowed; the deployed frontend's origin (e.g. the Vercel domain) comes
# from ALLOWED_ORIGINS (comma-separated) so going live doesn't need another code change -- just a
# Railway env var. Note the deployed frontend normally reaches this API through Vercel's own
# rewrite proxy (dashboard/vercel.json), which is same-origin from the browser's POV and never
# triggers CORS at all -- this only matters for hitting the backend directly (previews, testing).
_extra_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5183", "http://localhost:5173", *_extra_origins],
    allow_methods=["*"],
    allow_headers=["*"],
)

_state: dict = {}


@app.on_event("startup")
def _load_models_once() -> None:
    # SimData (location/route/calibration caches) and the trained V(s) model are expensive to
    # load (remote DB queries + a pickle) -- load once at process start, reuse across requests,
    # matching how sim/live/score_quote.py's own CLI usage already does this.
    data = load_sim_data()
    # v4 hours-since-home model (documents/logs/27) -- swapped in after the home-time retarget's
    # idle-timeout redesign + hours_since_home feature were built, retrained, and validated. A
    # genuine, disclosed mixed result, not a clean win: the paired-comparison test shows a real,
    # statistically significant total-reward regression (t=-2.416, p=0.0188, 60 matched seeds)
    # against v2 -- NOT an artifact, confirmed unaffected by the two real backtest bugs found and
    # fixed the same day (documents/logs/27's follow-up section). Weighed against that: real,
    # strong improvements on every home-positioning metric once those bugs were fixed -- 36-62%
    # closure of the real-data distance-to-home gap, 8.8% less deadhead, 100% missed-opportunity
    # recovery, more even work distribution -- and `hours_since_home` earning real, nonzero
    # feature importance (V(s) can now see the business-cadence signal directly, not just its
    # indirect effect on past rewards). Swapped in on the user's explicit call (real revenue cost
    # accepted for real driver-positioning gains), not because every metric came back clean.
    # Live inference already computes the features this model needs (sim/live/score_quote.py's
    # project_driver_state()/build_candidates() -- hours_since_home threaded through since
    # documents/logs/25-26), and make_value_fn()'s _predict_state_value() is backward-compatible
    # either way (only sets a feature if the loaded model's own feature_columns actually has that
    # column name), so this swap alone -- no other code change -- activates the new model in both
    # live scoring and the Simulation Showcase.
    booster, cols = load_state_value_model(str(REPO_ROOT / "sim/training/state_value_function_v4_hours_since_home.pkl"))
    _state["data"] = data
    _state["value_fn"] = make_value_fn(booster, cols, data)

    # Fleet Telematics Simulator (sim/live/telemetry_simulator.py) -- the process that actually
    # moves an ASSIGNED trip's truck, ticks geofence arrival/departure, and writes position
    # history, so Live Ops shows real movement instead of parked markers. Gated behind an env var
    # (off by default) rather than always-on: this codebase's own local-dev pattern is running
    # that script as ITS OWN separate process alongside `uvicorn --reload` (see its module
    # docstring); auto-starting it here too would double-tick every trip locally. Only the
    # deployed backend (Railway) sets RUN_TELEMETRY_SIMULATOR=true -- one persistent process
    # there covers both the API and the live fleet, no second service needed. This is exactly the
    # kind of long-lived background work Vercel serverless functions can't do, which is the whole
    # reason this backend lives on Railway and not Vercel in the first place.
    if os.environ.get("RUN_TELEMETRY_SIMULATOR", "").lower() == "true":
        threading.Thread(target=telemetry_simulator.run, daemon=True).start()


@app.get("/api/health")
def health():
    return {"ok": True, "models_loaded": "data" in _state}


@app.get("/api/fleet")
def get_fleet():
    """Resolved-position fleet snapshot for the map -- polled on a short interval rather than a
    Supabase Realtime subscription (deliberate simplification: PostGIS geography columns come
    back from PostgREST as WKB hex, not plain lat/lon, so resolving them client-side would need a
    WKB-parsing library; this endpoint resolves them server-side with plain SQL instead, which is
    simpler and just as responsive for a local-first demo). live.driver_status/trips RLS still
    governs what a DRIVER's own client can see if they query those tables directly (e.g. the
    driver's own current-trip view) -- this endpoint is manager-only fleet-wide data, called only
    from the manager Live Ops screen.
    """
    with cursor() as cur:
        cur.execute("""
            select
              ds.driver_id, ds.truck_number, ds.duty_status, ds.hos_remaining_hours, ds.current_trip_id,
              ds.last_location_id, ds.speed_mph, ds.odometer_km, ds.fuel_pct, ds.updated_at,
              coalesce(ST_Y(ds.position::geometry), ST_Y(rl.geog::geometry)) as lat,
              coalesce(ST_X(ds.position::geometry), ST_X(rl.geog::geometry)) as lon,
              t.status as trip_status, t.eta, t.origin_location_id, t.dest_location_id,
              ST_Y(rl_origin.geog::geometry) as origin_lat, ST_X(rl_origin.geog::geometry) as origin_lon,
              ST_Y(rl_dest.geog::geometry) as dest_lat, ST_X(rl_dest.geog::geometry) as dest_lon,
              (exists (select 1 from live.vehicle_inspections vi where vi.driver_id = ds.driver_id
                       and vi.submitted_at > now() - interval '24 hours' and vi.overall_pass)) as inspection_ok
            from live.driver_status ds
            left join reference.locations rl on rl.location_id = ds.last_location_id
            left join live.trips t on t.trip_id = ds.current_trip_id
            left join reference.locations rl_origin on rl_origin.location_id = t.origin_location_id
            left join reference.locations rl_dest on rl_dest.location_id = t.dest_location_id
        """)
        rows = cur.fetchall()
    cols = ["driver_id", "truck_number", "duty_status", "hos_remaining_hours", "current_trip_id",
            "last_location_id", "speed_mph", "odometer_km", "fuel_pct", "updated_at", "lat", "lon",
            "trip_status", "eta", "origin_location_id", "dest_location_id",
            "origin_lat", "origin_lon", "dest_lat", "dest_lon", "inspection_ok"]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/api/route")
def api_get_route(from_location_id: int, to_location_id: int):
    """Real road geometry for drawing a truck's route on the map -- delegates to
    sim/engine/route_interpolation.py's get_route_geometry(), the SAME function the telemetry
    simulator uses to move trucks (one function, two callers, not a duplicate implementation)."""
    coords, is_real = get_route_geometry(_state["data"], from_location_id, to_location_id)
    return {"coordinates": [list(c) for c in coords], "fallback": not is_real}


@app.get("/api/locations")
def search_locations(q: str = "", limit: int = 15):
    """Autocomplete for the quote panel's origin/destination pickers."""
    with cursor() as cur:
        cur.execute(
            """select location_id, label, city, tier from reference.locations
               where label ilike %s or city ilike %s
               order by (tier = 'terminal_hub') desc, label limit %s""",
            (f"%{q}%", f"%{q}%", limit),
        )
        rows = cur.fetchall()
    return [{"location_id": r[0], "label": r[1], "city": r[2], "tier": r[3]} for r in rows]


class GeocodeBody(BaseModel):
    query: str


@app.post("/api/geocode")
def geocode_new_address(body: GeocodeBody):
    """Real user feedback: the quote panel could only pick from the ~2,110 locations already in
    reference.locations (historical customers + sourced facilities) -- no way to quote a genuinely
    new customer address. Fixes that by geocoding via Nominatim (same free, keyless service and
    usage-policy compliance -- real User-Agent, no batch hammering -- sim/geocode_locations.py
    already used to source the table in the first place) and inserting a real new row, so it's a
    real location_id from then on -- routing/scoring never special-cases "new" vs "old" locations,
    they're the exact same table.
    """
    import requests

    from sim.config import COVERAGE_BBOX
    from sim.geocode_locations import USER_AGENT, in_coverage

    q = body.query.strip()
    if len(q) < 4:
        raise HTTPException(400, "Type a fuller address")

    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={
            "q": q, "format": "json", "limit": 1, "countrycodes": "ca", "addressdetails": 1,
            # Bias results to the coverage box (bounded=1 restricts to it outright) -- same
            # region the Nominatim policy note in sim/geocode_locations.py already targets.
            "viewbox": f"{COVERAGE_BBOX['min_lon']},{COVERAGE_BBOX['max_lat']},{COVERAGE_BBOX['max_lon']},{COVERAGE_BBOX['min_lat']}",
            "bounded": 1,
        },
        headers={"User-Agent": USER_AGENT}, timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise HTTPException(404, "Couldn't find that address in the Southern Ontario coverage area")

    result = results[0]
    lat, lon = float(result["lat"]), float(result["lon"])
    if not in_coverage(lat, lon):
        raise HTTPException(422, "That address is outside the Southern Ontario coverage area (Barrie / Peterborough / Pickering / London / Niagara Falls)")

    address = result.get("address", {})
    city = address.get("city") or address.get("town") or address.get("village") or address.get("municipality") or ""
    # A short, human label ("123 Main St, Kitchener") instead of Nominatim's full display_name
    # (neighbourhood/region/postal code/country) -- matches the concise style of every other
    # label already in the table (sim/geocode_locations.py's real_customer rows).
    street = " ".join(x for x in [address.get("house_number"), address.get("road")] if x)
    label = f"{street}, {city}" if street and city else result.get("display_name", q)[:250]

    # region_id is a required model feature (sim/sql/021 -- trees split on region, not raw
    # lat/lon), and it's how a brand-new location gets full feature parity with the 2,110 that
    # were bulk-sourced. nearest_region() is the exact function run_sim.py already uses for
    # classifying an arbitrary point, reused here rather than reimplemented.
    from sim.engine.run_sim import nearest_region
    region_id = nearest_region(_state["data"], lat, lon)

    with cursor() as cur:
        cur.execute(
            """insert into reference.locations (label, city, tier, geog, source, radius_m, region_id)
               values (%s, %s, 'real_customer', ST_GeogFromText(%s), 'nominatim', 120, %s)
               on conflict (label) do update set region_id = excluded.region_id
               where reference.locations.region_id is null""",
            (label, city, f"POINT({lon} {lat})", region_id),
        )
        cur.execute("select location_id, label, city, tier from reference.locations where label = %s", (label,))
        row = cur.fetchone()

    # Keep the in-process SimData cache in sync -- it's loaded ONCE at startup (main.py's
    # lifespan hook) for performance, so a location inserted after that moment would otherwise
    # be invisible to get_route()/scoring for the rest of this process's life (caught directly:
    # a live test hit `KeyError` in get_route() -> _query_live_osrm() for a just-geocoded id).
    _state["data"].locations[row[0]] = (lat, lon)
    _state["data"].location_region[row[0]] = region_id

    return {"location_id": row[0], "label": row[1], "city": row[2], "tier": row[3]}


class ScoreQuoteBody(BaseModel):
    origin_location_id: int
    dest_location_id: int
    weight_lbs: float
    pallets: float
    load_type: str
    requested_pickup_at: datetime
    service_type: str = "FTL"


@app.post("/api/score-quote")
def api_score_quote(body: ScoreQuoteBody):
    quote_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    quote = QuoteRequest(
        quote_id=quote_id, origin_location_id=body.origin_location_id, dest_location_id=body.dest_location_id,
        weight_lbs=body.weight_lbs, pallets=body.pallets, load_type=body.load_type,
        requested_at=now, requested_pickup_at=body.requested_pickup_at, service_type=body.service_type,
    )
    result = score_quote(quote, data=_state["data"], value_fn=_state["value_fn"])

    # Persist for audit trail / Realtime subscribers (research/roadstar_platform_plan.md Section
    # 8.1's quote panel design: submit -> subscribe to quote_recommendations for that quote_id).
    with cursor() as cur:
        cur.execute(
            """insert into live.quote_requests
                 (quote_id, origin_location_id, dest_location_id, requested_at, requested_pickup_at,
                  weight_lbs, pallets, load_type, service_type, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')""",
            (quote_id, body.origin_location_id, body.dest_location_id, now, body.requested_pickup_at,
             body.weight_lbs, body.pallets, body.load_type, body.service_type),
        )
        for row in result["top_n"]:
            cur.execute(
                """insert into live.quote_recommendations
                     (quote_id, rank, driver_id, expected_revenue, expected_margin, deadhead_miles,
                      hos_feasible, eta_pickup, model_version)
                   values (%s, %s, %s, %s, %s, %s, true, %s, %s)""",
                (quote_id, row["rank"], row["driver_id"], row["order_revenue"], row["immediate_reward"],
                 row["deadhead_miles"], row["eta_pickup"], "state_value_v1"),
            )

        # The real candidate-scoring audit trail -- every feasible candidate's feature state and
        # score, not just the top-N recommendations (score_quote.py's all_scored, previously
        # computed then discarded). was_assigned is backfilled by /api/assign once a pick is made.
        for row in result["all_scored"]:
            cur.execute(
                """insert into live.quote_candidate_snapshots
                     (quote_id, driver_id, truck_number, location_id, hos_remaining_hours,
                      truck_breakdown_risk, truck_pct_km_interval, truck_pct_days_interval,
                      deadhead_miles, planned_driving_hours, planned_duty_hours, score, rank,
                      was_assigned, scored_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s)""",
                (quote_id, row["driver_id"], row["truck_number"], row["location_id"],
                 row["hos_remaining_hours"], row["truck_breakdown_risk"], row["truck_pct_km_interval"],
                 row["truck_pct_days_interval"], row["deadhead_miles"], row["planned_driving_hours"],
                 row["planned_duty_hours"], row["score"], row["rank"], now),
            )

    return {"quote_id": str(quote_id), **result}


class AssignBody(BaseModel):
    quote_id: uuid.UUID
    driver_id: int


@app.post("/api/assign")
def api_assign(body: AssignBody):
    """One-click assign: writes the real trip, marks the quote fulfilled. Uses the recommendation
    row already computed by /api/score-quote (rank->driver) for the trip's projected fields rather
    than re-scoring.

    Real multi-trip booking (sim/sql/042, documents/logs/23-24 /
    feature_reference_and_inference_guide.md Section 4): a real dispatcher books trips days in
    advance, so a driver can already have other FUTURE trips queued before this one. Checked
    directly: the OLD version of this endpoint unconditionally overwrote
    live.driver_status.current_trip_id on every assignment -- a real bug, silently orphaning an
    earlier trip's reference from the single pointer every scoring/read path joined through, not
    just a missing feature. Fixed: a driver who already has a pending (non-terminal) trip gets
    this new one queued as status='scheduled' instead of 'assigned' -- it does NOT touch
    driver_status at all (the driver keeps whatever they're currently doing); the telemetry
    simulator promotes the next scheduled trip to 'assigned' when the current one completes
    (sim/live/telemetry_simulator.py's _complete_trip()). Only a driver with NO pending trip gets
    the original immediate-start behavior, unchanged.
    """
    data = _state["data"]
    with cursor() as cur:
        cur.execute(
            """select truck_number, last_location_id from live.driver_status where driver_id = %s""",
            (body.driver_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"driver {body.driver_id} not found in live.driver_status")
        truck_number, _origin_loc = row

        cur.execute(
            """select origin_location_id, dest_location_id, weight_lbs, pallets, load_type, requested_pickup_at
               from live.quote_requests where quote_id = %s""",
            (str(body.quote_id),),
        )
        q = cur.fetchone()
        if q is None:
            raise HTTPException(404, "quote not found")
        origin_location_id, dest_location_id, weight_lbs, pallets, load_type, requested_pickup_at = q

        cur.execute(
            """select rank, expected_revenue, deadhead_miles, eta_pickup from live.quote_recommendations
               where quote_id = %s and driver_id = %s""",
            (str(body.quote_id), body.driver_id),
        )
        rec = cur.fetchone()
        if rec is None:
            raise HTTPException(404, "that driver was not a recommended candidate for this quote")
        _rank, _revenue, deadhead_miles, eta_pickup = rec

        # Does this driver already have a pending (non-terminal) trip? Queried directly against
        # live.trips, not the single current_trip_id pointer -- the real source of truth for "is
        # this driver already booked," per the fix above.
        cur.execute(
            "select count(*) from live.trips where driver_id = %s and status not in ('completed', 'cancelled')",
            (body.driver_id,),
        )
        (n_pending,) = cur.fetchone()
        status = "scheduled" if n_pending > 0 else "assigned"

        # planned_driving_hours/planned_duty_hours/planned_completion_at -- real feasibility-only
        # estimates (same shape sim/live/score_quote.py's build_candidates() and sim/engine/
        # run_sim.py's own DISPATCH_DECISION loop already use: dh_hours + loaded_hours for driving,
        # +1.5h dwell for duty), so a 'scheduled' trip can be projected through BEFORE telemetry
        # ever computes its real landing state (sim/live/score_quote.py's project_driver_state()).
        # dh_hours here is estimated from the already-known deadhead_miles via the real measured
        # AVG_NETWORK_SPEED_MPH (sim/config.py) rather than a second get_route() call requiring
        # the exact effective origin this driver will actually depart from -- a light estimate for
        # this projection purpose only, not a re-price (the real routed dh_hours already drove
        # this candidate's actual score/deadhead_cost at /api/score-quote time).
        from sim.config import AVG_NETWORK_SPEED_MPH
        loaded_miles, loaded_hours = get_route(data, origin_location_id, dest_location_id)
        dh_hours_est = float(deadhead_miles or 0) / AVG_NETWORK_SPEED_MPH
        planned_driving_hours = dh_hours_est + loaded_hours
        median_pickup_dwell_h = data.dwell_minutes["pickup"][1] / 60
        median_delivery_dwell_h = data.dwell_minutes["delivery"][1] / 60
        # Real user correction: this used to be a flat "+1.5" guess found nowhere in the data --
        # now the real calibrated median (pickup + delivery dwell, ~1.0h combined), same figure
        # sim/dispatch_solver.py's dwell_hours_per_order() and every other real-dwell call site in
        # this project already uses, not a second, independent number for the same real quantity.
        planned_duty_hours = planned_driving_hours + median_pickup_dwell_h + median_delivery_dwell_h
        planned_completion_at = eta_pickup + timedelta(
            hours=dh_hours_est + median_pickup_dwell_h + loaded_hours + median_delivery_dwell_h
        )

        # projected_hos_remaining_hours/truck_pct_* are left null here -- the telemetry simulator
        # (Phase 5) is the real source of truth for a trip's projected landing state once it's
        # actually moving; build_candidates() already falls back to the driver's current values
        # when they're null, so this doesn't break future scoring, just defers the precision.
        trip_id = uuid.uuid4()
        cur.execute(
            """insert into live.trips
                 (trip_id, driver_id, status, last_event, eta, quote_id, origin_location_id, dest_location_id,
                  created_at, weight_lbs, pallets, load_type, pre_pickup_deadhead_miles,
                  planned_driving_hours, planned_duty_hours, planned_completion_at)
               values (%s, %s, %s, 'ASSGN', %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s)""",
            (trip_id, body.driver_id, status, eta_pickup, str(body.quote_id), origin_location_id, dest_location_id,
             weight_lbs, pallets, load_type, deadhead_miles,
             planned_driving_hours, planned_duty_hours, planned_completion_at),
        )
        if status == "assigned":
            cur.execute(
                """update live.driver_status set current_trip_id = %s, duty_status = 'driving', updated_at = now()
                   where driver_id = %s""",
                (trip_id, body.driver_id),
            )
        cur.execute("update live.quote_requests set status = 'assigned' where quote_id = %s", (str(body.quote_id),))
        cur.execute(
            "update live.quote_candidate_snapshots set was_assigned = true where quote_id = %s and driver_id = %s",
            (str(body.quote_id), body.driver_id),
        )

    return {"trip_id": str(trip_id), "driver_id": body.driver_id, "status": status}


# ---------------------------------------------------------------------------------------------
# Order management (Dispatch page's "Manage Order" tab) -- look up, edit + re-score + reassign,
# or cancel an existing order. Operates purely on live.* -- unrelated to the simulation schema.
# ---------------------------------------------------------------------------------------------

def _release_assignment(cur, quote_id: str) -> None:
    """Cancels the active trip (if any) tied to this quote and frees the driver -- guards the
    same 'don't clobber a newer commitment' race run_sim.py's own event loop guards
    (run_sim.py:1009-1018): only clears driver_status if it STILL points at this exact trip, since
    the driver could've already been dispatched on something newer since this trip was created.
    """
    cur.execute(
        "select trip_id, driver_id from live.trips where quote_id = %s and status not in ('completed', 'cancelled')",
        (quote_id,),
    )
    row = cur.fetchone()
    if row is None:
        return
    trip_id, driver_id = row
    cur.execute("update live.trips set status = 'cancelled' where trip_id = %s", (trip_id,))
    cur.execute(
        """update live.driver_status set current_trip_id = null, duty_status = 'off_duty', updated_at = now()
           where driver_id = %s and current_trip_id = %s""",
        (driver_id, trip_id),
    )


@app.get("/api/orders/{quote_id}")
def get_order(quote_id: str):
    """Accepts EITHER a quote_id or a trip_id -- the manager may only know one or the other
    (the plan's own "enter the order id or trip id" ask). A trip_id resolves to its quote_id
    first, then the lookup proceeds identically either way."""
    with cursor() as cur:
        cur.execute("select quote_id from live.trips where trip_id = %s", (quote_id,))
        trip_match = cur.fetchone()
        if trip_match and trip_match[0]:
            quote_id = str(trip_match[0])

        cur.execute(
            """select quote_id, origin_location_id, dest_location_id, requested_at, requested_pickup_at,
                      weight_lbs, pallets, load_type, service_type, status
               from live.quote_requests where quote_id = %s""",
            (quote_id,),
        )
        q = cur.fetchone()
        if q is None:
            raise HTTPException(404, "quote not found")
        cols = ["quote_id", "origin_location_id", "dest_location_id", "requested_at", "requested_pickup_at",
                "weight_lbs", "pallets", "load_type", "service_type", "status"]
        quote = dict(zip(cols, q))
        quote["quote_id"] = str(quote["quote_id"])

        for key in ("origin_location_id", "dest_location_id"):
            cur.execute("select label from reference.locations where location_id = %s", (quote[key],))
            r = cur.fetchone()
            quote[f"{key}_label"] = r[0] if r else None

        cur.execute(
            """select t.trip_id, t.status, t.driver_id, ds.truck_number, ds.duty_status
               from live.trips t left join live.driver_status ds on ds.driver_id = t.driver_id
               where t.quote_id = %s order by t.created_at desc limit 1""",
            (quote_id,),
        )
        t = cur.fetchone()
        trip = None
        if t:
            trip = {"trip_id": str(t[0]), "status": t[1], "driver_id": t[2], "truck_number": t[3], "duty_status": t[4]}

    return {"quote": quote, "trip": trip}


class RescoreBody(BaseModel):
    requested_pickup_at: datetime | None = None
    weight_lbs: float | None = None
    pallets: float | None = None
    load_type: str | None = None


@app.post("/api/orders/{quote_id}/rescore")
def rescore_order(quote_id: str, body: RescoreBody):
    """Edits an existing order (pickup time/weight/pallets/load_type), releases whatever it's
    currently assigned to, and re-runs the real scoring pipeline fresh -- the manager picks a new
    candidate the same way they would for a brand-new quote (via the existing /api/assign)."""
    with cursor() as cur:
        cur.execute(
            "select origin_location_id, dest_location_id, weight_lbs, pallets, load_type, requested_pickup_at, service_type from live.quote_requests where quote_id = %s",
            (quote_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "quote not found")
        origin_location_id, dest_location_id, weight_lbs, pallets, load_type, requested_pickup_at, service_type = row

        weight_lbs = body.weight_lbs if body.weight_lbs is not None else weight_lbs
        pallets = body.pallets if body.pallets is not None else pallets
        load_type = body.load_type if body.load_type is not None else load_type
        requested_pickup_at = body.requested_pickup_at if body.requested_pickup_at is not None else requested_pickup_at

        _release_assignment(cur, quote_id)
        cur.execute(
            """update live.quote_requests set weight_lbs = %s, pallets = %s, load_type = %s,
                 requested_pickup_at = %s, status = 'open' where quote_id = %s""",
            (weight_lbs, pallets, load_type, requested_pickup_at, quote_id),
        )

    quote = QuoteRequest(
        quote_id=uuid.UUID(quote_id), origin_location_id=origin_location_id, dest_location_id=dest_location_id,
        weight_lbs=weight_lbs, pallets=pallets, load_type=load_type,
        requested_at=datetime.now(timezone.utc), requested_pickup_at=requested_pickup_at, service_type=service_type,
    )
    result = score_quote(quote, data=_state["data"], value_fn=_state["value_fn"])

    with cursor() as cur:
        cur.execute("delete from live.quote_recommendations where quote_id = %s", (quote_id,))
        cur.execute("delete from live.quote_candidate_snapshots where quote_id = %s", (quote_id,))
        for r in result["top_n"]:
            cur.execute(
                """insert into live.quote_recommendations
                     (quote_id, rank, driver_id, expected_revenue, expected_margin, deadhead_miles,
                      hos_feasible, eta_pickup, model_version)
                   values (%s, %s, %s, %s, %s, %s, true, %s, %s)""",
                (quote_id, r["rank"], r["driver_id"], r["order_revenue"], r["immediate_reward"],
                 r["deadhead_miles"], r["eta_pickup"], "state_value_v1"),
            )
        for r in result["all_scored"]:
            cur.execute(
                """insert into live.quote_candidate_snapshots
                     (quote_id, driver_id, truck_number, location_id, hos_remaining_hours,
                      truck_breakdown_risk, truck_pct_km_interval, truck_pct_days_interval,
                      deadhead_miles, planned_driving_hours, planned_duty_hours, score, rank,
                      was_assigned, scored_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, now())""",
                (quote_id, r["driver_id"], r["truck_number"], r["location_id"], r["hos_remaining_hours"],
                 r["truck_breakdown_risk"], r["truck_pct_km_interval"], r["truck_pct_days_interval"],
                 r["deadhead_miles"], r["planned_driving_hours"], r["planned_duty_hours"], r["score"], r["rank"]),
            )

    return {"quote_id": quote_id, **result}


@app.post("/api/orders/{quote_id}/cancel")
def cancel_order(quote_id: str):
    with cursor() as cur:
        cur.execute("select quote_id from live.quote_requests where quote_id = %s", (quote_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "quote not found")
        _release_assignment(cur, quote_id)
        cur.execute("update live.quote_requests set status = 'cancelled' where quote_id = %s", (quote_id,))
    return {"quote_id": quote_id, "status": "cancelled"}


class SimulationRunBody(BaseModel):
    seed: int | None = None


@app.post("/api/simulation/run")
def run_week_simulation(body: SimulationRunBody):
    """The Simulation Showcase: a full simulated WEEK of realistic fleet operation against the
    SAME 30-driver demo fleet Live Ops shows -- a real order book generated up front (quote time +
    pickup time, same real lead-time/lane/cargo distributions run_sim.py always uses), each order
    assigned by the real trained V(s) model AT its quote time (pure exploit -- no training-time
    exploration; this demonstrates the trained model's real decisions, not its training process).

    Persisted into the dedicated `simulation` schema (sim/sql/038) -- a real, queryable,
    always-repeatable replay; live.* is never touched by a showcase run.

    Also runs a SECOND pass over the identical order book/fleet with `zero_value_fn` (immediate-
    reward-only, no trained foresight) as a real, computed baseline -- NOT persisted, used only
    for the run's "value created by the trained model" comparison. (An earlier design considered
    comparing against a naive "always the closest truck" baseline using only the winning
    candidate's deadhead miles -- checked directly and rejected: by definition a closest-truck
    baseline already minimizes deadhead, so the trained model can only ever tie or lose on that
    narrow metric, never beat it. Comparing full realized reward against a real second run isolates
    the model's actual foresight value -- future positioning, HOS/maintenance-risk avoidance --
    instead of a comparison that could never show anything but a negative number.)
    """
    import random as _random
    from copy import copy

    from psycopg2.extras import Json, execute_values

    from sim.config import (
        ASSUMED_MANUAL_DISPATCH_MINUTES_PER_DECISION, ASSUMED_OPERATING_COST_PER_MILE,
        ASSUMED_PROMISE_BUFFER_HOURS, leg_detention, quote_price,
    )
    from sim.engine.policy import zero_value_fn
    from sim.engine.reward import post_delivery_deadhead_cost
    from sim.engine.run_sim import generate_order, next_order_arrival
    from sim.live.seed_demo_fleet import _build_fleet_roster

    def business_margin(trips) -> dict:
        """REAL, business-explainable $ only -- revenue earned, deadhead cost paid (pre-pickup +
        post-delivery), realized lateness penalty. Deliberately excludes reward_total's other
        terms (HOS-stranding-risk/maintenance-risk/opportunity-cost -- forward-looking penalties
        that exist to help the MODEL rank candidates, not dollars anyone spent) and realized
        breakdown-repair cost (truck health/maintenance is out of this sim's dispatch-decision
        scope) -- real user feedback: mixing those into one "reward" number produced a confusing,
        occasionally negative figure that didn't map to "did dispatch do a good job."
        """
        revenue = sum(c.order_revenue for c in trips)
        deadhead = sum(c.deadhead_cost + post_delivery_deadhead_cost(c.trip_state) for c in trips)
        lateness = sum(c.lateness_penalty for c in trips)
        return {"revenue": revenue, "deadhead_cost": deadhead, "lateness_penalty": lateness,
                "net_margin": revenue - deadhead - lateness}

    seed = body.seed if body.seed is not None else _random.randint(1, 1_000_000)
    rng = _random.Random(seed)
    base_data = _state["data"]

    with cursor() as cur:
        demo_driver_ids, _driver_trucks = _build_fleet_roster(cur)

    # Trim to the 30-driver demo fleet. Order density: real user feedback -- 30 trucks getting
    # ~7/day (proportional to the real 131-driver network) left most trucks doing one trip all
    # week, nowhere near enough volume to show deadhead/multi-trip/reuse dynamics. Deliberately
    # targeting ~28 orders/day for this 30-truck demo fleet instead -- a demo-density choice, NOT
    # a calibrated figure (flagged the same way every other showcase-only simplification in this
    # function is) -- busy enough that trucks take multiple loads and some orders genuinely go
    # unassigned (shown as "lost opportunity" below), which is the point: a real test of fleet
    # capacity, not proportional demand.
    data = copy(base_data)
    data.driver_ids = demo_driver_ids
    TARGET_ORDERS_PER_DAY = 28.0
    # NOT a naive "target / sum(lambda dict)" scale -- checked directly, that analytical shortcut
    # undercounts badly here: next_order_arrival()'s hour-varying-Poisson approximation (re-uses
    # ONE hour's own lambda for the whole gap until the next draw, its own documented
    # simplification) samples measurably fewer orders/week than the raw calibration sum implies,
    # because the calibrated rate is lumpy (concentrated in a handful of real peak hours) --
    # empirically ~110/week sampled at the real unscaled rate, not the ~255/week naive sum. A
    # single calibration pass (real draws, not a formula) measures the ACTUAL relationship and
    # scales from that -- still real Poisson variance run to run (a real property of this
    # calibrated arrival process, not something to eliminate), just centered on the real target.
    def _measure_weekly_orders(trial_scale: float, n_trials: int = 40) -> float:
        trial_rates = {k: v * trial_scale for k, v in base_data.order_arrival_rate.items()}
        calibration_start = datetime(2026, 1, 1)  # arbitrary fixed anchor -- only hour-of-day/day-of-week matter
        calibration_end = calibration_start + timedelta(days=7)
        total = 0
        for trial_seed in range(n_trials):
            trial_rng = _random.Random(trial_seed)
            trial_t = calibration_start
            while True:
                lam = trial_rates.get((trial_t.hour, trial_t.weekday()), 0.1 * trial_scale)
                trial_t = trial_t + timedelta(hours=trial_rng.expovariate(max(lam, 0.001)))
                if trial_t > calibration_end:
                    break
                total += 1
        return total / n_trials

    _measured_at_1x = _measure_weekly_orders(1.0)
    scale = (TARGET_ORDERS_PER_DAY * 7) / _measured_at_1x if _measured_at_1x else 1.0
    data.order_arrival_rate = {k: v * scale for k, v in base_data.order_arrival_rate.items()}

    # Real user feedback: too many generated orders were same-location "yard shuttle" moves
    # (origin_location_id == dest_location_id) -- real, but disproportionately represented here:
    # they're ~17% of calibration.lane_frequency's total weight mass (concentrated at the 2
    # terminal hubs + one customer yard), and that table only has real order history for 46/2119
    # real facility locations in reference.locations. Neither is touched at the source
    # (load_sim_data()/calibration.lane_frequency stays exactly as calibrated -- training/backtest
    # fidelity depends on it) -- this reshapes only THIS showcase run's own lane pool:
    #   1. down-weight yard-shuttle lanes so they still occur (they're real) but don't dominate.
    #   2. add real, never-before-sampled facility locations (tier='real_customer' -- genuine
    #      geocoded businesses, not the 2,000 synthetic OSM-derived filler locations) as new
    #      pickup/delivery lanes to and from the 2 real terminal hubs, mirroring the same
    #      hub-and-spoke shape most of the real historical lanes already have.
    # Lane EXISTENCE and WEIGHT for the new pairs are SYNTHESIZED (there's no real order history
    # for them); the location coordinates themselves are 100% real, and routes are still priced/
    # driven via the same get_route() (real cached/live OSRM, haversine only as last resort) as
    # every other lane.
    YARD_SHUTTLE_WEIGHT_FACTOR = 0.2
    with cursor() as cur:
        cur.execute(
            """select location_id from reference.locations
               where tier = 'real_customer'
                 and location_id not in (select origin_location_id from calibration.lane_frequency
                                          union select dest_location_id from calibration.lane_frequency)"""
        )
        new_locations = [r[0] for r in cur.fetchall() if r[0] in data.locations]

        # Real user feedback: the stated coverage boundaries (Barrie north, Peterborough &
        # Pickering east, London west, Niagara Falls south) barely show up in the showcase -- most
        # simulated trips stayed in the Milton<->London corridor. Checked directly against the
        # raw source data (data/*.xlsx, 4031 rows): NOT a pipeline bug -- the real historical
        # order volume itself is genuinely concentrated in Milton/GTA (Barrie: 10/2789 ON rows,
        # Niagara Falls: 5, Pickering: 2, Peterborough: 0), so calibration.lane_frequency
        # faithfully reflects that. Per explicit direction: this stays live/demo-only (no touching
        # calibration.lane_frequency, no retraining, no re-clustering calibration.region_clusters)
        # -- Peterborough is dropped entirely (confirmed zero reference.locations of ANY tier
        # anywhere near it, real or synthesized -- there's nothing to route through).
        #
        # The fix: the real_customer-only new_locations query above already MISSED the real fix --
        # `synthetic_facility` (2,000 real OSM-sourced buildings spanning the WHOLE coverage bbox,
        # sim/geocode_locations.py's Tier B) has genuine geographic reach into these cities that
        # `real_customer` (the 59 named historical shippers, themselves GTA-concentrated) doesn't.
        # Confirmed directly: 19 real synthetic_facility locations near Barrie, 50 near Hamilton,
        # 13 near Pickering, 9 near Niagara Falls -- real coordinates, just never given lane
        # volume because the historical shippers never happened to be there. Pulling THOSE in,
        # boosted above the plain median weight so they're genuinely visible (not just technically
        # present among ~2,000 candidates), is what actually broadens the showcase's geographic
        # spread -- still real locations/routes, the lane existence and volume are what's
        # synthesized, same disclosure as new_locations above.
        COVERAGE_BOOST_REGIONS = {
            'Barrie': (44.25, 44.45, -79.75, -79.60),
            'Hamilton': (43.15, 43.30, -80.00, -79.75),
            'Niagara Falls': (43.00, 43.15, -79.15, -78.95),
            'Pickering': (43.78, 43.92, -79.15, -78.90),
        }
        boost_locations: dict[int, str] = {}
        for region_name, (min_lat, max_lat, min_lon, max_lon) in COVERAGE_BOOST_REGIONS.items():
            cur.execute(
                """select location_id from reference.locations
                   where tier in ('real_customer', 'synthetic_facility')
                     and ST_Y(geog::geometry) between %s and %s
                     and ST_X(geog::geometry) between %s and %s
                     and location_id not in (select origin_location_id from calibration.lane_frequency
                                              union select dest_location_id from calibration.lane_frequency)""",
                (min_lat, max_lat, min_lon, max_lon),
            )
            for (loc_id,) in cur.fetchall():
                if loc_id in data.locations:
                    boost_locations[loc_id] = region_name
        # A real_customer location that happens to fall in one of these boxes would otherwise be
        # added twice (once at median weight via new_locations, once boosted here) -- boosted
        # wins, so each location gets exactly one weight.
        new_locations = [loc_id for loc_id in new_locations if loc_id not in boost_locations]

    real_lane_weights = sorted(w for o, d, w in base_data.lane_weights if o != d)
    median_lane_weight = real_lane_weights[len(real_lane_weights) // 2] if real_lane_weights else 5.0

    # Region TOTAL weight, not per-location -- Hamilton has ~5x as many real synthetic_facility
    # buildings as Niagara Falls in this coverage box, so splitting a per-location weight evenly
    # would just make Hamilton numerically dominate the whole showcase instead of Milton/GTA,
    # trading one lopsided concentration for another. REGION_TOTAL_WEIGHT instead targets roughly
    # Guelph's real total lane mass (calibration.lane_frequency: ~122 across 9 lanes) for EACH of
    # the 4 boosted regions -- "a real, solid secondary market," comparable to each other, not
    # scaled by how many OSM buildings happened to be sampled there.
    REGION_TOTAL_WEIGHT = 120.0
    region_location_counts: dict[str, int] = {}
    for region_name in boost_locations.values():
        region_location_counts[region_name] = region_location_counts.get(region_name, 0) + 1
    # Each location contributes 4 lane_weights entries (2 hubs x 2 directions) -- divide the
    # region's total budget across all of them so the region's OWN total comes out to
    # REGION_TOTAL_WEIGHT regardless of how many individual locations it has.
    boost_weight_by_location = {
        loc_id: REGION_TOTAL_WEIGHT / (4 * region_location_counts[region_name])
        for loc_id, region_name in boost_locations.items()
    }

    adjusted_lane_weights = [
        (o, d, w * YARD_SHUTTLE_WEIGHT_FACTOR if o == d else w) for o, d, w in base_data.lane_weights
    ]
    for hub_id in data.hub_ids.values():
        for loc_id in new_locations:
            adjusted_lane_weights.append((hub_id, loc_id, median_lane_weight))
            adjusted_lane_weights.append((loc_id, hub_id, median_lane_weight))
        for loc_id, w in boost_weight_by_location.items():
            adjusted_lane_weights.append((hub_id, loc_id, w))
            adjusted_lane_weights.append((loc_id, hub_id, w))
    data.lane_weights = adjusted_lane_weights
    data.origin_density = {}
    for o, _d, w in adjusted_lane_weights:
        data.origin_density[o] = data.origin_density.get(o, 0.0) + w

    week_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    # Generous allowance for loaded transit + pickup/delivery dwell + a possible LTL secondary
    # stop, so a clipped order still has real room to actually complete within the week, not just
    # be picked up right at the boundary.
    TRANSIT_DWELL_ALLOWANCE_HOURS = 30.0

    orders = []
    t = week_start
    while True:
        t = next_order_arrival(data, t, rng)
        if t > week_end:
            break
        order = generate_order(data, t, rng)
        # Showcase-only simplification (real dispatch/other sim runs keep the real 24h-before-
        # pickup cutoff): assign at quote time, immediately -- "order comes in -> model picks a
        # driver," not deferred to a later decision point.
        order.decision_time = order.created_at

        # Real user feedback: "if it's one week we should have this in one week" -- real lead
        # times (calibration.order_lead_time_hours) have a genuine long tail (up to ~38 days,
        # 99th pct ~11 days), which could otherwise push a trip's pickup/delivery well past the
        # nominal week even though it was BOOKED within it. Clipped to whatever room is actually
        # left in the week for this specific order (not a flat cap) -- most real samples (median
        # ~44h) are well under this anyway, so only the genuine tail is ever touched.
        remaining_hours = (week_end - order.created_at).total_seconds() / 3600
        max_lead_hours = max(1.0, remaining_hours - TRANSIT_DWELL_ALLOWANCE_HOURS)
        if (order.requested_pickup_at - order.created_at).total_seconds() / 3600 > max_lead_hours:
            order.requested_pickup_at = order.created_at + timedelta(hours=max_lead_hours)
            median_pickup_dwell_h = data.dwell_minutes['pickup'][1] / 60
            median_delivery_dwell_h = data.dwell_minutes['delivery'][1] / 60
            order.promised_delivery_at = order.requested_pickup_at + timedelta(
                hours=order.loaded_hours + median_pickup_dwell_h + median_delivery_dwell_h + ASSUMED_PROMISE_BUFFER_HOURS
            )
        orders.append(order)

    result = run_simulation(
        hours=0, seed=seed, epsilon_start=0.0, epsilon_end=0.0,
        data=data, value_fn=_state["value_fn"], orders=orders,
    )
    baseline = run_simulation(
        hours=0, seed=seed, epsilon_start=0.0, epsilon_end=0.0,
        data=data, value_fn=zero_value_fn, orders=orders,
    )
    baseline_margin = business_margin(baseline["completed_trips"])
    baseline_n_completed = len(baseline["completed_trips"])

    sim_start = result["sim_start"]

    # Real user feedback: the showcase only ever showed a handful of averages next to the map --
    # these are the direct sim-demo equivalents of what real backtesting already showed (cycle-
    # based metrics, daily HOS utilization, trips-per-driver spread), as DISTRIBUTIONS not just
    # means, computed once here (sim/engine/fleet_metrics.py, reusing real_data_replay.py's own
    # cycle-closing definitions) and stored on the run row so a reload shows the identical numbers.
    from sim.engine.fleet_metrics import compute_fleet_metrics
    fleet_metrics = compute_fleet_metrics(data, result["completed_trips"], data.driver_ids, sim_start)
    run_id = uuid.uuid4()

    def sample_leg(coords, t0, t1, n=10):
        if not coords or t1 is None or t1 <= t0:
            return []
        pts = []
        for i in range(n + 1):
            frac = i / n
            pos = interpolate_position(coords, frac)
            if not pos:
                continue
            lat, lon = pos
            t2 = t0 + (t1 - t0) * frac
            pts.append([(t2 - sim_start).total_seconds(), lat, lon])
        return pts

    orders_by_id = {o.order_id: o for o in orders}
    completed_ids = {c.order.order_id for c in result["completed_trips"]}
    unassigned_ids = set(result["unassigned_order_ids"])
    revenue_by_order = {c.order.order_id: c.order_revenue for c in result["completed_trips"]}

    quote_rows, rec_rows, snapshot_rows = [], [], []
    trip_rows, trip_log_rows, geofence_rows, detention_rows, invoice_rows = [], [], [], [], []
    driver_snapshot_rows, inspection_rows = [], []
    loc_ids: set[int] = set()
    trips_out = []
    total_revenue = total_deadhead_cost = total_lateness_penalty = total_detention = total_invoiced = 0.0
    n_breakdown = n_on_time = n_late = 0
    n_deadhead_avoided = 0
    deadhead_avoided_value = 0.0

    for order in orders:
        status = "assigned" if order.order_id in completed_ids else ("expired" if order.order_id in unassigned_ids else "open")
        quote_rows.append((
            order.order_id, run_id, order.origin_location_id, order.dest_location_id,
            order.created_at, order.requested_pickup_at, order.weight_lbs, order.pallets,
            order.load_type, order.service_type, status,
        ))
        loc_ids.add(order.origin_location_id)
        loc_ids.add(order.dest_location_id)

    # Candidate-scoring audit trail (decision 2) -- every feasible candidate, not just the winner.
    # Uses the VALUE-AUGMENTED ranking (value_augmented_candidate_score_rows), not the immediate-
    # reward-only one sim.candidate_scores' own training-data contract uses -- real user feedback:
    # showing the immediate-only score made the actually-assigned candidate look like it wasn't
    # even the top-ranked one, when the model really picked it for its higher TOTAL (including
    # future-positioning) value. This ranking is guaranteed consistent with what was actually
    # assigned (rank 1 == chosen, under epsilon=0 -- no exploration in this showcase).
    # order_id -> [(driver_id, deadhead_miles, was_assigned), ...] for EVERY candidate the model
    # actually scored on that order -- the same real data the Order Story's "vs the other real
    # candidates" comparison already uses (decision below: per-trip deadhead-avoided $ value).
    candidates_by_order: dict[uuid.UUID, list[tuple[int, float, bool]]] = {}
    for row in result["value_augmented_candidate_score_rows"]:
        (_sim_id, order_id, driver_id, truck_number, location_id, hos_remaining,
         breakdown_risk, pct_km, pct_days, deadhead_miles, planned_driving, planned_duty,
         score, rank_pos, was_assigned) = row
        order = orders_by_id.get(order_id)
        scored_at = order.decision_time if order else sim_start
        snapshot_rows.append((
            run_id, order_id, driver_id, truck_number, location_id, hos_remaining,
            breakdown_risk, pct_km, pct_days, deadhead_miles, planned_driving, planned_duty,
            score, rank_pos, was_assigned, scored_at,
        ))
        candidates_by_order.setdefault(order_id, []).append((driver_id, deadhead_miles, was_assigned))
        if was_assigned and rank_pos is not None:
            rec_rows.append((
                run_id, order_id, rank_pos, driver_id, revenue_by_order.get(order_id), None,
                deadhead_miles, True, order.requested_pickup_at if order else scored_at, "state_value_v1",
            ))

    # Real user feedback: showing ONE flat run-average $ figure on every "deadhead avoided" trip
    # ("+$44 saved" on every qualifying row) reads as a fabricated constant, not a per-trip real
    # number. Each driver's NEXT real trip this run is a real, known event -- so for a trip that
    # reloaded immediately (no empty leg to reach that next job), the per-trip counterfactual is
    # "what would the OTHER real candidates the model considered for that SAME next order have
    # had to deadhead" -- the identical real-alternative-candidates comparison the Order Story
    # page already uses (deadhead_vs_avg_alternative), just applied to the NEXT order instead of
    # this one. Grounded in this run's own real scored candidates, not a theoretical baseline.
    trips_by_driver: dict[int, list] = {}
    for c in result["completed_trips"]:
        trips_by_driver.setdefault(c.driver_id, []).append(c)
    next_order_by_trip_id: dict[uuid.UUID, uuid.UUID] = {}
    for driver_trips in trips_by_driver.values():
        driver_trips.sort(key=lambda c: c.assigned_at)
        for i in range(len(driver_trips) - 1):
            next_order_by_trip_id[driver_trips[i].trip_id] = driver_trips[i + 1].order.order_id

    def deadhead_avoided_value_for(trip_id, next_order_id) -> float:
        """The real per-trip $ value of THIS trip's deadhead-avoided outcome -- 0.0 (not a
        fallback average) if there's no next job to compare against, or that next job had no
        other real candidates scored to compare against."""
        if next_order_id is None:
            return 0.0
        others = [dh for did, dh, was_assigned in candidates_by_order.get(next_order_id, []) if not was_assigned]
        if not others:
            return 0.0
        return (sum(others) / len(others)) * ASSUMED_OPERATING_COST_PER_MILE

    for c in result["completed_trips"]:
        first_at: dict[str, datetime] = {}
        for ev in c.trip_state.history:
            first_at.setdefault(ev.status.value, ev.at)
        arr_pickup, dep_pickup, arr_delivery = first_at.get("ARRSHIP"), first_at.get("DEPSHIP"), first_at.get("ARRCONS")

        deadhead_coords, _ = get_route_geometry(data, c.driver_location_id, c.order.origin_location_id)
        loaded_coords, _ = get_route_geometry(data, c.order.origin_location_id, c.order.dest_location_id)
        trajectory = sample_leg(deadhead_coords, c.assigned_at, arr_pickup or c.assigned_at) + \
            sample_leg(loaded_coords, dep_pickup or arr_pickup or c.assigned_at, arr_delivery or c.completed_at)

        on_time = c.completed_at <= c.order.promised_delivery_at
        n_on_time += int(on_time)
        n_late += int(not on_time)
        if c.trip_state.had_breakdown:
            n_breakdown += 1

        pickup_dwell_h = (dep_pickup - arr_pickup).total_seconds() / 3600 if arr_pickup and dep_pickup else None
        delivery_dwell_h = (c.completed_at - arr_delivery).total_seconds() / 3600 if arr_delivery else None

        trip_rows.append((
            c.trip_id, run_id, c.driver_id, "assigned", "ASSGN", c.assigned_at,
            c.order.origin_location_id, c.order.dest_location_id, c.order.created_at,
            c.order.weight_lbs, c.order.pallets, c.order.load_type, c.order.loaded_miles,
            c.deadhead_miles, c.order.order_id, c.driver_location_id, Json(trajectory),
        ))
        # Real, business-explainable $ only (see business_margin() above) -- revenue, deadhead
        # cost (pre-pickup + post-delivery), realized lateness. No risk-penalty or breakdown-cost
        # terms folded in.
        post_dh_cost = post_delivery_deadhead_cost(c.trip_state)
        trip_log_rows.append((
            c.trip_id, run_id, c.driver_id, c.truck_number, c.completed_at, c.order.loaded_miles,
            c.deadhead_miles, c.trip_state.post_delivery_deadhead_miles, pickup_dwell_h, delivery_dwell_h,
            on_time, c.load_fill_ratio, None,
            c.trip_state.had_breakdown, c.trip_state.breakdown_repair_hours, c.reward_total,
            c.order_revenue, c.deadhead_cost, post_dh_cost, c.lateness_penalty,
        ))
        total_revenue += c.order_revenue
        total_deadhead_cost += c.deadhead_cost + post_dh_cost
        total_lateness_penalty += c.lateness_penalty

        # Per-leg detention (an improvement over live's trip-only grouping -- see plan) -- pickup
        # and delivery each billed against their own free-hours allowance.
        trip_detention = 0.0
        for loc_id, arr, dep in ((c.order.origin_location_id, arr_pickup, dep_pickup),
                                  (c.order.dest_location_id, arr_delivery, c.completed_at)):
            if arr:
                geofence_rows.append((run_id, c.trip_id, loc_id, "arrival", arr))
            if dep:
                geofence_rows.append((run_id, c.trip_id, loc_id, "departure", dep))
            det = leg_detention(arr, dep)
            if det["amount"] > 0:
                trip_detention += det["amount"]
                detention_rows.append((c.trip_id, loc_id, run_id, arr, dep, 2, det["amount"]))
        total_detention += trip_detention

        pricing = quote_price(c.order.loaded_miles, c.order.load_type, c.order.service_type, c.load_fill_ratio)
        invoice_id = uuid.uuid4()
        invoice_rows.append((
            invoice_id, run_id, f"SIM-{seed}-{len(invoice_rows) + 1:04d}", c.trip_id, c.order.order_id,
            c.completed_at, c.completed_at + timedelta(days=30), None, None, "ON",
            pricing["linehaul_amount"], round(trip_detention, 2), pricing["fuel_surcharge_amount"], 0, "draft",
        ))
        total_invoiced += (pricing["estimated_total_charge"] + trip_detention) * 1.13  # + 13% HST, matching live.invoices' own generated total_amount

        # 5 lifecycle-event snapshots (decision 6) -- HOS/truck figures anchored to the two REAL
        # points the engine actually computed (decision-time and post-trip), not a fabricated
        # per-event recomputation.
        o_lat, o_lon = data.locations[c.order.origin_location_id]
        d_lat, d_lon = data.locations[c.order.dest_location_id]
        # Real, DISTINCT HOS layers at each point -- decision-time (driving/duty daily windows +
        # 7-day/14-day cycles, all real HOSState components, not the single MIN aggregate
        # repeated four times) and post-trip (the engine's own next_hos_* snapshot).
        decision_hos = (c.driver_hos_driving_remaining, c.driver_hos_duty_remaining, c.driver_hos_cycle1_remaining, c.driver_hos_cycle2_remaining)
        next_hos = (c.next_hos_driving_remaining, c.next_hos_duty_remaining, c.next_hos_cycle1_remaining, c.next_hos_cycle2_remaining)
        for snap_at, lat, lon, duty, hos_tuple, pct_km_s, pct_days_s in (
            (c.assigned_at, None, None, "driving", decision_hos, c.truck_pct_km_interval, c.truck_pct_days_interval),
            (arr_pickup, o_lat, o_lon, "on_duty_not_driving", decision_hos, c.truck_pct_km_interval, c.truck_pct_days_interval),
            (dep_pickup, o_lat, o_lon, "driving", decision_hos, c.truck_pct_km_interval, c.truck_pct_days_interval),
            (arr_delivery, d_lat, d_lon, "on_duty_not_driving", next_hos, c.next_truck_pct_km_interval, c.next_truck_pct_days_interval),
            (c.completed_at, d_lat, d_lon, "off_duty", next_hos, c.next_truck_pct_km_interval, c.next_truck_pct_days_interval),
        ):
            if snap_at is None:
                continue
            hos_driving, hos_duty, hos_cycle1, hos_cycle2 = hos_tuple
            driver_snapshot_rows.append((
                run_id, c.driver_id, c.truck_number, c.trip_id, snap_at, lat, lon, duty,
                hos_driving, hos_duty, hos_cycle1, hos_cycle2, c.truck_breakdown_risk, pct_km_s, pct_days_s, True,
            ))

        loc_ids.add(c.order.origin_location_id)
        loc_ids.add(c.order.dest_location_id)

        # Real user feedback + established precedent (sim/classify.py's "Yard shuttle" category,
        # sim/backtest/real_data_replay.py's explicit exclusion of origin==dest zero-distance
        # moves from real-freight $ recovered figures): a trip whose OWN hauled leg is ~0 miles
        # never actually left the yard, so a "reloaded immediately, no deadhead" outcome right
        # after it is a trivial byproduct of the driver never having moved -- not a real
        # deadhead-avoidance decision the model gets credit for. Gate the credit on this being a
        # genuine freight move.
        # Same yard-shuttle-style trap the loaded_miles gate above already fixed: if this is the
        # driver's LAST trip of the run, post_delivery_deadhead_miles reads 0 simply because no
        # further DISP leg was ever logged -- not because dispatch actually avoided one. Only
        # count/price it when there's a REAL next trip to have deadheaded to (or not).
        next_order_id = next_order_by_trip_id.get(c.trip_id)
        is_real_freight_move = c.order.loaded_miles >= 0.1
        reload_immediate = is_real_freight_move and c.trip_state.post_delivery_deadhead_miles <= 0 and next_order_id is not None
        deadhead_saved = deadhead_avoided_value_for(c.trip_id, next_order_id) if reload_immediate else 0.0
        if reload_immediate:
            n_deadhead_avoided += 1
            deadhead_avoided_value += deadhead_saved
        trips_out.append({
            "trip_id": str(c.trip_id), "quote_id": str(c.order.order_id),
            "driver_id": c.driver_id, "truck_number": c.truck_number,
            "reload_immediate": reload_immediate,
            "deadhead_saved": round(deadhead_saved, 2),
            "assigned_at_s": (c.assigned_at - sim_start).total_seconds(),
            "completed_at_s": (c.completed_at - sim_start).total_seconds(),
            "arr_pickup_at_s": (arr_pickup - sim_start).total_seconds() if arr_pickup else None,
            "dep_pickup_at_s": (dep_pickup - sim_start).total_seconds() if dep_pickup else None,
            "arr_delivery_at_s": (arr_delivery - sim_start).total_seconds() if arr_delivery else None,
            "origin_location_id": c.order.origin_location_id, "dest_location_id": c.order.dest_location_id,
            "weight_lbs": c.order.weight_lbs, "pallets": c.order.pallets, "load_type": c.order.load_type,
            "order_revenue": round(c.order_revenue, 2),
            "deadhead_cost": round(c.deadhead_cost + post_dh_cost, 2),
            "lateness_penalty": round(c.lateness_penalty, 2),
            "net_margin": round(c.order_revenue - c.deadhead_cost - post_dh_cost - c.lateness_penalty, 2),
            "deadhead_miles": round(c.deadhead_miles, 1), "on_time": on_time,
            "had_breakdown": c.trip_state.had_breakdown, "trajectory": trajectory,
            "detention_amount": round(trip_detention, 2), "invoice_total": round((pricing["estimated_total_charge"] + trip_detention) * 1.13, 2),
        })

    # Final state for EVERY demo truck (not just ones with a completed trip) -- run_simulation()
    # now returns `fleet` (decision: small additive change) so idle-all-week trucks still get a
    # real end-of-run maintenance row instead of being silently missing.
    fleet = result["fleet"]
    truck_rows = []
    # fleet.truck_maint always covers the FULL 131-truck universe (initialize_fleet() tracks
    # maintenance state fleet-wide, independent of driver_ids trimming) -- restrict to trucks
    # that actually belong to this demo fleet, so the showcase shows "our fleet," not all 131
    # real trucks. NOT just each driver's one default truck (`_driver_trucks.values()`) -- a
    # driver's actual truck pool has 2-4 candidates (build_truck_pools()), so a completed trip
    # can real-world use a non-default pool truck; found directly by checking the Fleet Dashboard
    # against its own data -- trucks that had real trip_log rows but no maintenance row at all.
    # Union in every truck_number that actually appears in this run's own completed trips.
    demo_truck_numbers = set(_driver_trucks.values()) | {c.truck_number for c in result["completed_trips"]}
    for truck_number, tm in fleet.truck_maint.items():
        if truck_number not in demo_truck_numbers:
            continue
        truck_rows.append((
            truck_number, run_id, tm.cumulative_km_since_service,
            week_end - timedelta(days=tm.days_since_service), tm.service_interval_km, tm.service_interval_days,
        ))

    # One passing inspection per driver per simulated day -- decision 8.
    for driver_id in demo_driver_ids:
        for day in range(7):
            inspection_rows.append((uuid.uuid4(), run_id, driver_id, _driver_trucks[driver_id],
                                     week_start + timedelta(days=day, hours=5), None, True))

    # "Lost opportunity" -- real user feedback: orders nobody could take are a real, positive-
    # framed business number (revenue this fleet size left on the table), not just a bare count.
    # Priced with the SAME quote_price() every other $ figure in this run uses -- no fill-ratio
    # signal exists for an order that was never assigned a truck, so FTL/no-fill assumed (a
    # slight overestimate for any LTL orders among them, not a fabricated one).
    lost_opportunity_revenue = 0.0
    for order_id in result["unassigned_order_ids"]:
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        pricing = quote_price(order.loaded_miles, order.load_type, order.service_type, 1.0)
        lost_opportunity_revenue += pricing["estimated_total_charge"]

    with cursor() as cur:
        if loc_ids:
            cur.execute("select location_id, label from reference.locations where location_id = any(%s)", (list(loc_ids),))
            labels = dict(cur.fetchall())
        else:
            labels = {}

        net_margin = total_revenue - total_deadhead_cost - total_lateness_penalty
        cur.execute(
            """insert into simulation.runs
                 (run_id, seed, created_at, week_start, week_end, driver_ids, n_orders_generated,
                  n_completed, n_unassigned, total_revenue, total_detention_billed,
                  total_invoiced, total_deadhead_cost, total_lateness_penalty, net_margin,
                  baseline_net_margin, n_deadhead_avoided, deadhead_avoided_value, status, fleet_metrics)
               values (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'complete', %s)""",
            (run_id, seed, week_start, week_end, demo_driver_ids, len(orders), len(result["completed_trips"]),
             result["unassigned_orders"], round(total_revenue, 2),
             round(total_detention, 2), round(total_invoiced, 2), round(total_deadhead_cost, 2),
             round(total_lateness_penalty, 2), round(net_margin, 2), round(baseline_margin["net_margin"], 2),
             n_deadhead_avoided, round(deadhead_avoided_value, 2), Json(fleet_metrics)),
        )
        if quote_rows:
            execute_values(cur, "insert into simulation.quote_requests (quote_id, run_id, origin_location_id, dest_location_id, requested_at, requested_pickup_at, weight_lbs, pallets, load_type, service_type, status) values %s", quote_rows)
        if rec_rows:
            execute_values(cur, "insert into simulation.quote_recommendations (run_id, quote_id, rank, driver_id, expected_revenue, expected_margin, deadhead_miles, hos_feasible, eta_pickup, model_version) values %s", rec_rows)
        if snapshot_rows:
            execute_values(cur, "insert into simulation.quote_candidate_snapshots (run_id, quote_id, driver_id, truck_number, location_id, hos_remaining_hours, truck_breakdown_risk, truck_pct_km_interval, truck_pct_days_interval, deadhead_miles, planned_driving_hours, planned_duty_hours, score, rank, was_assigned, scored_at) values %s", snapshot_rows)
        if trip_rows:
            execute_values(cur, "insert into simulation.trips (trip_id, run_id, driver_id, status, last_event, eta, origin_location_id, dest_location_id, created_at, weight_lbs, pallets, load_type, loaded_miles, pre_pickup_deadhead_miles, quote_id, driver_location_id, trajectory) values %s", trip_rows)
        if trip_log_rows:
            execute_values(cur, "insert into simulation.trip_log (trip_id, run_id, driver_id, truck_number, completed_at, loaded_miles, pre_pickup_deadhead_miles, post_delivery_deadhead_miles, pickup_dwell_hours, delivery_dwell_hours, on_time, load_fill_ratio, hos_stranding_risk, breakdown_occurred, breakdown_repair_hours, reward_total, order_revenue, deadhead_cost, post_delivery_deadhead_cost, lateness_penalty_amount) values %s", trip_log_rows)
        if geofence_rows:
            execute_values(cur, "insert into simulation.geofence_events (run_id, trip_id, location_id, event_type, occurred_at) values %s", geofence_rows)
        if detention_rows:
            execute_values(cur, "insert into simulation.detention_billing (trip_id, location_id, run_id, arrival_at, departure_at, free_hours, amount) values %s", detention_rows)
        if invoice_rows:
            execute_values(cur, "insert into simulation.invoices (invoice_id, run_id, invoice_number, trip_id, quote_id, issued_at, due_at, bill_to_name, bill_to_address, delivery_province, linehaul_amount, detention_amount, fuel_surcharge_amount, accessorial_amount, status) values %s", invoice_rows)
        if truck_rows:
            execute_values(cur, "insert into simulation.truck_maintenance_state (truck_number, run_id, cumulative_km_since_service, last_service_at, service_interval_km, service_interval_days) values %s", truck_rows)
        if inspection_rows:
            execute_values(cur, "insert into simulation.vehicle_inspections (inspection_id, run_id, driver_id, truck_number, submitted_at, odometer_km, overall_pass) values %s", inspection_rows)
        if driver_snapshot_rows:
            execute_values(cur, "insert into simulation.driver_state_snapshots (run_id, driver_id, truck_number, trip_id, snapshot_at, lat, lon, duty_status, hos_driving_hours_remaining, hos_duty_hours_remaining, hos_cycle1_hours_remaining, hos_cycle2_hours_remaining, truck_breakdown_risk, truck_pct_km_interval, truck_pct_days_interval, inspection_ok) values %s", driver_snapshot_rows)

    for t in trips_out:
        t["origin_label"] = labels.get(t["origin_location_id"])
        t["dest_label"] = labels.get(t["dest_location_id"])
    trips_out.sort(key=lambda t: t["assigned_at_s"])

    n_completed = len(trips_out)
    # Real user feedback: "if it's one week we should have this in one week" -- capped flat at
    # the nominal 7 days, no longer stretched to cover a late-tail trip's own completion (the
    # lead-time clipping above already keeps that from being a large effect; any trip still
    # wrapping up exactly at the boundary just shows as still in progress at week's end, a real
    # and realistic snapshot, not a truncated/wrong outcome -- its real completion is still fully
    # reflected in the summary totals below regardless of the playback window).
    duration_seconds = (week_end - week_start).total_seconds()

    return {
        "run_id": str(run_id), "seed": seed,
        "sim_start": sim_start.isoformat(), "duration_seconds": duration_seconds,
        "summary": {
            "n_orders_generated": len(orders), "n_completed": n_completed,
            "n_unassigned": result["unassigned_orders"],
            "n_decisions": len(orders),
            "dispatcher_hours_saved": round(len(orders) * ASSUMED_MANUAL_DISPATCH_MINUTES_PER_DECISION / 60, 1),
            "lost_opportunity_revenue": round(lost_opportunity_revenue, 2),
            "total_revenue": round(total_revenue, 2),
            "n_deadhead_avoided": n_deadhead_avoided,
            "deadhead_avoided_value": round(deadhead_avoided_value, 2),
            # Kept for the Data Tables tab / drill-down, not the headline KPIs (real user
            # feedback: a net-of-structural-cost figure that can swing negative reads as "the
            # model did something wrong" when deadhead-to-pickup is a real, largely unavoidable
            # geography cost every dispatcher pays regardless -- the positive, real wins
            # (deadhead avoided via immediate reload, revenue captured, dispatcher time saved)
            # tell the actual story better).
            "total_deadhead_cost": round(total_deadhead_cost, 2),
            "total_lateness_penalty": round(total_lateness_penalty, 2),
            "net_margin": round(net_margin, 2),
            "total_detention_billed": round(total_detention, 2), "total_invoiced": round(total_invoiced, 2),
            "n_breakdowns": n_breakdown, "n_on_time": n_on_time, "n_late": n_late,
            "value_vs_baseline": round(net_margin - baseline_margin["net_margin"], 2),
            "baseline_n_completed": baseline_n_completed,
            "baseline_net_margin": round(baseline_margin["net_margin"], 2),
        },
        "fleet_metrics": fleet_metrics,
        "trips": trips_out,
    }


@app.get("/api/simulation/runs")
def list_simulation_runs(limit: int = 20):
    """Real user feedback: keep past runs as real records, loadable again -- not just
    "always trigger a fresh one." Lists what's already in simulation.runs (sim/sql/038).
    `run_kind = 'batch'` only (sim/sql/044) -- the single-trip "Simulation Trip" demo runs get
    their own run_kind and their own page, not mixed into this week-run picker."""
    with cursor() as cur:
        cur.execute(
            """select run_id, seed, created_at, week_start, week_end, n_orders_generated,
                      n_completed, n_unassigned, total_revenue, net_margin, n_deadhead_avoided,
                      deadhead_avoided_value
               from simulation.runs where run_kind = 'batch' order by created_at desc limit %s""",
            (limit,),
        )
        cols = ["run_id", "seed", "created_at", "week_start", "week_end", "n_orders_generated",
                "n_completed", "n_unassigned", "total_revenue", "net_margin", "n_deadhead_avoided",
                "deadhead_avoided_value"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["run_id"] = str(r["run_id"])
    return rows


@app.get("/api/simulation/runs/{run_id}")
def reload_simulation_run(run_id: str):
    """Reconstructs the SAME payload shape /api/simulation/run returns, entirely from the
    persisted simulation.* rows (sim/sql/038/039/040) -- trajectories recomputed from real route
    geometry + the run's own stored timestamps (cheap: geometry lookups + interpolation, no HOS/
    scoring recompute needed), everything else read directly. Runs saved before the
    driver_location_id column (sim/sql/040) show no deadhead leg on reload -- flagged, not
    silently guessed.
    """
    from sim.config import ASSUMED_MANUAL_DISPATCH_MINUTES_PER_DECISION, ASSUMED_OPERATING_COST_PER_MILE

    with cursor() as cur:
        cur.execute("select run_id, seed, week_start, week_end, fleet_metrics from simulation.runs where run_id = %s", (run_id,))
        run_row = cur.fetchone()
        if run_row is None:
            raise HTTPException(404, "simulation run not found")
        _run_id, seed, week_start, week_end, fleet_metrics = run_row
        sim_start = week_start

        cur.execute(
            """select t.trip_id, t.driver_id, tl.truck_number, t.origin_location_id, t.dest_location_id,
                      t.eta, tl.completed_at, t.weight_lbs, t.pallets, t.load_type, qr.service_type,
                      tl.loaded_miles, tl.pre_pickup_deadhead_miles, tl.post_delivery_deadhead_miles,
                      tl.on_time, tl.breakdown_occurred, tl.order_revenue, tl.deadhead_cost,
                      tl.post_delivery_deadhead_cost, tl.lateness_penalty_amount, tl.net_margin,
                      tl.load_fill_ratio, t.driver_location_id, t.quote_id, t.trajectory
               from simulation.trips t
               join simulation.trip_log tl on tl.trip_id = t.trip_id
               left join simulation.quote_requests qr on qr.quote_id = t.quote_id
               where t.run_id = %s""",
            (run_id,),
        )
        trip_rows = cur.fetchall()
        # The stored `trajectory` column's [t_offset_seconds, lat, lon] points are relative to
        # the ORIGINAL run's real sim_start (= its earliest order's decision time, not
        # necessarily week_start -- the first order arrival is some real time AFTER week_start).
        # Re-deriving the SAME zero-point from the loaded trips themselves (not week_start) keeps
        # every offset -- stored trajectories AND the assigned_at_s/completed_at_s computed below
        # -- on one consistent clock; using week_start here would silently misalign the two.
        etas = [row[5] for row in trip_rows if row[5] is not None]
        if etas:
            sim_start = min(etas)

        cur.execute(
            "select trip_id, location_id, event_type, occurred_at from simulation.geofence_events where run_id = %s",
            (run_id,),
        )
        geofence_by_trip: dict[str, list] = {}
        for trip_id, location_id, event_type, occurred_at in cur.fetchall():
            geofence_by_trip.setdefault(str(trip_id), []).append((location_id, event_type, occurred_at))

        cur.execute(
            "select trip_id, sum(amount) from simulation.detention_billing where run_id = %s group by trip_id",
            (run_id,),
        )
        detention_by_trip = {str(tid): float(amt or 0) for tid, amt in cur.fetchall()}

        cur.execute(
            "select trip_id, linehaul_amount, detention_amount, fuel_surcharge_amount from simulation.invoices where run_id = %s",
            (run_id,),
        )
        invoice_by_trip = {
            str(tid): float(lh or 0) + float(det or 0) + float(fs or 0) for tid, lh, det, fs in cur.fetchall()
        }

        cur.execute("select count(*) from simulation.quote_requests where run_id = %s and status = 'expired'", (run_id,))
        (n_unassigned,) = cur.fetchone()
        cur.execute("select count(*) from simulation.quote_requests where run_id = %s", (run_id,))
        (n_orders_generated,) = cur.fetchone()

        # Same real per-trip deadhead-avoided $ computation the run-time path uses (see its own
        # comment) -- the persisted candidate-scoring audit trail (sim/sql/038) makes this exactly
        # reproducible on reload, not just at run time.
        cur.execute(
            "select quote_id, driver_id, deadhead_miles, was_assigned from simulation.quote_candidate_snapshots where run_id = %s",
            (run_id,),
        )
        candidates_by_order: dict[str, list[tuple[int, float, bool]]] = {}
        for c_quote_id, c_driver_id, c_deadhead, c_was_assigned in cur.fetchall():
            candidates_by_order.setdefault(str(c_quote_id), []).append((c_driver_id, float(c_deadhead or 0), bool(c_was_assigned)))

        loc_ids: set[int] = set()
        for row in trip_rows:
            loc_ids.add(row[3])
            loc_ids.add(row[4])
        labels: dict[int, str] = {}
        if loc_ids:
            cur.execute("select location_id, label from reference.locations where location_id = any(%s)", (list(loc_ids),))
            labels = dict(cur.fetchall())

    # driver_id -> that driver's trips this run, sorted chronologically -- same "each driver's
    # real next trip" pairing the run-time path builds, from the same real assigned_at (eta) and
    # quote_id columns already selected above (row[1]=driver_id, row[5]=eta, row[23]=quote_id).
    trips_by_driver: dict[int, list] = {}
    for row in trip_rows:
        trips_by_driver.setdefault(row[1], []).append(row)
    next_order_by_trip_id: dict[str, str] = {}
    for driver_rows in trips_by_driver.values():
        driver_rows.sort(key=lambda r: r[5])
        for i in range(len(driver_rows) - 1):
            next_order_by_trip_id[str(driver_rows[i][0])] = str(driver_rows[i + 1][23])

    def deadhead_avoided_value_for(trip_id_str: str) -> float:
        next_order_id = next_order_by_trip_id.get(trip_id_str)
        if next_order_id is None:
            return 0.0
        others = [dh for did, dh, was_assigned in candidates_by_order.get(next_order_id, []) if not was_assigned]
        if not others:
            return 0.0
        return (sum(others) / len(others)) * ASSUMED_OPERATING_COST_PER_MILE

    trips_out = []
    total_revenue = total_deadhead_cost = total_lateness_penalty = net_margin_sum = 0.0
    n_on_time = n_late = n_breakdown = n_deadhead_avoided = 0
    deadhead_avoided_value = 0.0
    for row in trip_rows:
        (trip_id, driver_id, truck_number, origin_id, dest_id, assigned_at, completed_at,
         weight_lbs, pallets, load_type, service_type, loaded_miles, deadhead_miles,
         post_dh_miles, on_time, had_breakdown, order_revenue, deadhead_cost, post_dh_cost,
         lateness_penalty, net_margin, fill_ratio, driver_location_id, quote_id, stored_trajectory) = row

        events = geofence_by_trip.get(str(trip_id), [])
        arr_pickup = min((occ for loc, typ, occ in events if loc == origin_id and typ == "arrival"), default=None)
        dep_pickup = min((occ for loc, typ, occ in events if loc == origin_id and typ == "departure"), default=None)
        arr_delivery = min((occ for loc, typ, occ in events if loc == dest_id and typ == "arrival"), default=None)

        # Real user feedback: reload was slow re-fetching route geometry (incl. live OSRM calls)
        # per trip -- the trajectory was already computed once at run time and is read straight
        # back here, a pure Supabase read, no geometry recompute. Older runs saved before this
        # column existed fall back to an empty trajectory (flagged, not silently wrong) rather
        # than paying the old slow path again.
        trajectory = stored_trajectory or []

        # Same yard-shuttle exclusion as the run-time computation above (see its comment) --
        # applied identically on reload so the KPI doesn't drift between a fresh run and a
        # reloaded one. Also requires a REAL next trip (same trap as the yard-shuttle gate: with
        # no next trip, post_delivery_deadhead_miles reads 0 simply because nothing was logged
        # after it, not because a deadhead was genuinely avoided).
        is_real_freight_move = float(loaded_miles or 0) >= 0.1
        has_next_trip = str(trip_id) in next_order_by_trip_id
        reload_immediate = is_real_freight_move and float(post_dh_miles or 0) <= 0 and has_next_trip
        deadhead_saved = deadhead_avoided_value_for(str(trip_id)) if reload_immediate else 0.0
        if reload_immediate:
            n_deadhead_avoided += 1
            deadhead_avoided_value += deadhead_saved
        n_on_time += int(bool(on_time))
        n_late += int(not on_time)
        n_breakdown += int(bool(had_breakdown))
        total_revenue += float(order_revenue or 0)
        total_deadhead_cost += float(deadhead_cost or 0) + float(post_dh_cost or 0)
        total_lateness_penalty += float(lateness_penalty or 0)
        net_margin_sum += float(net_margin or 0)

        trips_out.append({
            "trip_id": str(trip_id), "quote_id": str(quote_id) if quote_id else None,
            "driver_id": driver_id, "truck_number": truck_number,
            "reload_immediate": reload_immediate,
            "deadhead_saved": round(deadhead_saved, 2),
            "assigned_at_s": (assigned_at - sim_start).total_seconds(),
            "completed_at_s": (completed_at - sim_start).total_seconds(),
            "arr_pickup_at_s": (arr_pickup - sim_start).total_seconds() if arr_pickup else None,
            "dep_pickup_at_s": (dep_pickup - sim_start).total_seconds() if dep_pickup else None,
            "arr_delivery_at_s": (arr_delivery - sim_start).total_seconds() if arr_delivery else None,
            "origin_location_id": origin_id, "dest_location_id": dest_id,
            "origin_label": labels.get(origin_id), "dest_label": labels.get(dest_id),
            "weight_lbs": float(weight_lbs or 0), "pallets": float(pallets or 0), "load_type": load_type,
            "order_revenue": round(float(order_revenue or 0), 2),
            "deadhead_cost": round(float(deadhead_cost or 0) + float(post_dh_cost or 0), 2),
            "lateness_penalty": round(float(lateness_penalty or 0), 2),
            "net_margin": round(float(net_margin or 0), 2),
            "deadhead_miles": round(float(deadhead_miles or 0), 1), "on_time": bool(on_time),
            "had_breakdown": bool(had_breakdown), "trajectory": trajectory,
            "detention_amount": round(detention_by_trip.get(str(trip_id), 0.0), 2),
            "invoice_total": round(invoice_by_trip.get(str(trip_id), 0.0) * 1.13, 2),  # invoice_by_trip already includes detention -- just add HST
        })

    trips_out.sort(key=lambda t: t["assigned_at_s"])
    duration_seconds = (week_end - week_start).total_seconds()

    return {
        "run_id": run_id, "seed": seed,
        "sim_start": sim_start.isoformat(), "duration_seconds": duration_seconds,
        "summary": {
            "n_orders_generated": n_orders_generated, "n_completed": len(trips_out),
            "n_unassigned": n_unassigned, "n_decisions": n_orders_generated,
            "dispatcher_hours_saved": round(n_orders_generated * ASSUMED_MANUAL_DISPATCH_MINUTES_PER_DECISION / 60, 1),
            "lost_opportunity_revenue": 0.0,  # not recomputed on reload -- see runs list for the figure saved at run time
            "total_revenue": round(total_revenue, 2),
            "n_deadhead_avoided": n_deadhead_avoided, "deadhead_avoided_value": round(deadhead_avoided_value, 2),
            "total_deadhead_cost": round(total_deadhead_cost, 2), "total_lateness_penalty": round(total_lateness_penalty, 2),
            "net_margin": round(net_margin_sum, 2),
            "total_detention_billed": round(sum(detention_by_trip.values()), 2),
            "total_invoiced": round(sum(t["invoice_total"] for t in trips_out), 2),
            "n_breakdowns": n_breakdown, "n_on_time": n_on_time, "n_late": n_late,
            "value_vs_baseline": 0.0,  # baseline pass isn't re-run on reload -- see the run's own recorded figure via /api/simulation/runs
            "baseline_n_completed": len(trips_out), "baseline_net_margin": round(net_margin_sum, 2),
        },
        # Read straight back from the stored column (computed once at run time) -- a run saved
        # before sim/sql/043 has none on file, flagged as null rather than silently recomputed
        # differently (cycles_for_driver() needs the original CompletedTrip objects' next_hos_*/
        # next_available_at, which aren't reconstructable from the flattened persisted rows alone).
        "fleet_metrics": fleet_metrics,
        "trips": trips_out,
    }


def _compute_cycle(cur, run_id: str, driver_id: int, this_trip_id) -> dict | None:
    """This trip's real CYCLE (home base -> home base) -- the persisted-data counterpart of
    sim/engine/fleet_metrics.py's cycles_for_driver(): SAME closing rule (a cycle closes when a
    trip's own destination IS the driver's home hub; runs to an ASSUMED close, priced via
    get_route(), if the driver's real trip sequence for this run simply ends first), applied here
    to find and return the ONE cycle a specific order's trip belongs to, with every trip in it --
    not just the immediately-adjacent ones -- labeled P1/D1/P2/D2/... for the Order Story's cycle
    view. Returns None only if this trip can't be found in the driver's own persisted trip list at
    all (should not happen for a real trip_id from this same run).
    """
    from sim.config import ASSUMED_OPERATING_COST_PER_MILE
    from sim.engine.run_sim import driver_home_hub_id

    data = _state["data"]
    home_hub_id = driver_home_hub_id(data, driver_id)

    cur.execute(
        """select t.trip_id, t.quote_id, t.origin_location_id, t.dest_location_id, t.eta,
                  tl.completed_at, tl.order_revenue, t.pre_pickup_deadhead_miles,
                  tl.post_delivery_deadhead_miles, t.loaded_miles
           from simulation.trips t join simulation.trip_log tl on tl.trip_id = t.trip_id
           where t.run_id = %s and t.driver_id = %s order by t.eta""",
        (run_id, driver_id),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    cycles: list[list] = []
    cur_cycle: list = []
    for row in rows:
        cur_cycle.append(row)
        if row[3] == home_hub_id:  # dest_location_id -- closes right here, real paid trip
            cycles.append(cur_cycle)
            cur_cycle = []
    if cur_cycle:
        cycles.append(cur_cycle)  # never made it back home within this run's data -- ASSUMED close

    this_cycle = next((c for c in cycles if any(r[0] == this_trip_id for r in c)), None)
    if this_cycle is None:
        return None

    last_row = this_cycle[-1]
    closed_via = 'trip' if last_row[3] == home_hub_id else 'assumed'
    empty_return_miles = 0.0 if closed_via == 'trip' else get_route(data, last_row[3], home_hub_id)[0]

    loc_ids = {home_hub_id}
    for r in this_cycle:
        loc_ids.add(r[2])
        loc_ids.add(r[3])
    cur.execute("select location_id, label from reference.locations where location_id = any(%s)", (list(loc_ids),))
    labels = dict(cur.fetchall())

    # Mid-cycle reload savings -- the SAME real per-trip $ value the order-book's "+$X saved"
    # badge uses (api_simulation_run's deadhead_avoided_value_for()), scoped to consecutive trips
    # WITHIN this one cycle, netting out the chosen candidate's own (usually near-zero, but not
    # always exactly zero) deadhead against the average of the real OTHER candidates that next
    # order actually had on the table -- the same formula Section 3's deadhead_vs_avg_alternative
    # already uses, applied here to the NEXT trip in the cycle instead of this trip's own pickup.
    quote_ids = [r[1] for r in this_cycle]  # native uuid.UUID objects -- quote_id is a uuid column, `= any(text[])` has no operator
    cur.execute(
        "select quote_id, driver_id, deadhead_miles, was_assigned from simulation.quote_candidate_snapshots where quote_id = any(%s)",
        (quote_ids,),
    )
    candidates_by_quote: dict[str, list[tuple]] = {}
    for c_quote_id, c_driver_id, c_deadhead, c_was_assigned in cur.fetchall():
        candidates_by_quote.setdefault(str(c_quote_id), []).append((c_driver_id, float(c_deadhead or 0), bool(c_was_assigned)))

    trips_out = []
    for i, row in enumerate(this_cycle):
        (r_trip_id, r_quote_id, r_origin, r_dest, r_eta, r_completed, r_revenue,
         r_pre_dh, r_post_dh, r_loaded_miles) = row
        # Real user feedback: "from Pn to Dn the path should be on but at 401 and all it's
        # splitting into multipaths" -- the STORED trajectory (`r_traj`, used elsewhere for
        # playback) is the truck's FULL real path -- pre-pickup DEADHEAD + pickup dwell + the
        # loaded leg, concatenated (confirmed directly: trip_id b1060626...'s own trajectory[0] is
        # London, not this trip's Milton origin, because that trip had 89mi of real deadhead
        # first). Using it here meant a) the P/D pins landed at the wrong coordinates (trajectory
        # endpoints are the deadhead START and the loaded-leg END, not the real pickup/drop-off),
        # and b) drawing several cycle trips' FULL paths together bundled every trip's own deadhead
        # leg back through the same shared corridor, reading as the road itself forking. Fetching
        # the clean origin->dest geometry directly (the SAME real cached/live OSRM lookup every
        # other route line in this app already uses, not a second implementation) gives exactly
        # the P-to-D leg the labels promise, with no deadhead/dwell segment mixed in.
        loaded_coords, _is_real_geometry = get_route_geometry(data, r_origin, r_dest)
        clean_trajectory = [[float(idx), lat, lon] for idx, (lon, lat) in enumerate(loaded_coords)]
        is_real_freight_move = float(r_loaded_miles or 0) >= 0.1
        has_next_in_cycle = i < len(this_cycle) - 1
        reload_immediate = is_real_freight_move and float(r_post_dh or 0) <= 0 and has_next_in_cycle
        reload_savings_value = 0.0
        if reload_immediate:
            next_quote_id = str(this_cycle[i + 1][1])
            next_candidates = candidates_by_quote.get(next_quote_id, [])
            others = [dh for did, dh, was_assigned in next_candidates if not was_assigned]
            chosen = next((dh for did, dh, was_assigned in next_candidates if was_assigned), 0.0)
            if others:
                avg_other = sum(others) / len(others)
                reload_savings_value = max(0.0, avg_other - chosen) * ASSUMED_OPERATING_COST_PER_MILE
        trips_out.append({
            "trip_id": str(r_trip_id), "quote_id": str(r_quote_id), "is_current": r_trip_id == this_trip_id,
            "pickup_label": f"P{i + 1}", "dropoff_label": f"D{i + 1}",
            "origin_location_id": r_origin, "dest_location_id": r_dest,
            "origin_label": labels.get(r_origin), "dest_label": labels.get(r_dest),
            "assigned_at": r_eta.isoformat() if r_eta else None,
            "completed_at": r_completed.isoformat() if r_completed else None,
            "order_revenue": round(float(r_revenue or 0), 2),
            "deadhead_miles": round(float(r_pre_dh or 0), 1),
            "reload_immediate": reload_immediate,
            "reload_savings_value": round(reload_savings_value, 2),
            "trajectory": clean_trajectory,
        })

    return {
        "home_hub_label": labels.get(home_hub_id),
        "closed_via": closed_via,  # 'trip' (real paid delivery landed at home) | 'assumed' (still away when this run's data ends)
        "empty_return_miles": round(empty_return_miles, 1),
        "empty_return_value": round(empty_return_miles * ASSUMED_OPERATING_COST_PER_MILE, 2),
        "n_trips": len(this_cycle),
        "total_revenue": round(sum(float(r[6] or 0) for r in this_cycle), 2),
        "trips": trips_out,
    }


def _ai_dispatch_trip_row_to_sim_trip(r: tuple) -> dict:
    (trip_id, driver_id, truck_number, hub_city, assigned_at_s, completed_at_s, arr_pickup_at_s,
     dep_pickup_at_s, arr_delivery_at_s, origin_location_id, dest_location_id, origin_label, dest_label,
     weight_lbs, pallets, load_type, order_revenue, deadhead_cost, deadhead_miles, net_margin,
     detention_amount, is_detention_demo, trajectory) = r
    return {
        "trip_id": str(trip_id), "quote_id": "", "driver_id": driver_id, "truck_number": truck_number,
        "hub_city": hub_city, "reload_immediate": False, "deadhead_saved": 0,
        "assigned_at_s": float(assigned_at_s), "completed_at_s": float(completed_at_s),
        "arr_pickup_at_s": float(arr_pickup_at_s) if arr_pickup_at_s is not None else None,
        "dep_pickup_at_s": float(dep_pickup_at_s) if dep_pickup_at_s is not None else None,
        "arr_delivery_at_s": float(arr_delivery_at_s) if arr_delivery_at_s is not None else None,
        "origin_location_id": origin_location_id, "dest_location_id": dest_location_id,
        "origin_label": origin_label, "dest_label": dest_label,
        "weight_lbs": float(weight_lbs or 0), "pallets": pallets or 0, "load_type": load_type,
        "order_revenue": float(order_revenue or 0), "deadhead_cost": float(deadhead_cost or 0),
        "lateness_penalty": 0.0, "net_margin": float(net_margin or 0),
        "deadhead_miles": float(deadhead_miles or 0), "on_time": True, "had_breakdown": False,
        "detention_amount": float(detention_amount or 0), "is_detention_demo": bool(is_detention_demo),
        "invoice_total": float(order_revenue or 0) + float(detention_amount or 0),
        "trajectory": trajectory,
    }


@app.post("/api/simulation/ai-dispatch-run")
def run_ai_dispatch_simulation(body: dict):
    """Real user ask: replace the Simulation Showcase's old ML/RL-trained-policy week-long batch
    sim with a full-day, multi-truck replay of what the AI (CP-SAT) dispatcher actually decided for
    one real day -- sim/live/ai_dispatch_replay.py. `service_date` (YYYY-MM-DD) must already have
    an AI-assigned (or manually dispatched) plan on the Dispatch Board."""
    from sim.live.ai_dispatch_replay import DAY_START_HOUR as ai_dispatch_day_start_hour
    from sim.live.ai_dispatch_replay import generate as generate_ai_dispatch_replay
    service_date = date.fromisoformat(body["service_date"])
    try:
        result = generate_ai_dispatch_replay(service_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Real bug found directly: this hardcoded "T06:00:00" independently of ai_dispatch_replay.py's
    # own DAY_START_HOUR constant -- the two silently drifted apart the moment that constant was
    # corrected to 03:00 (real-data-grounded, see its own comment), which would have made every
    # trip's displayed playback time 3 hours off from what the trips were actually generated
    # against. Reading the same constant instead of a second hardcoded literal.
    return {
        "run_id": result["run_id"], "seed": 0,
        "sim_start": f"{service_date.isoformat()}T{ai_dispatch_day_start_hour:02d}:00:00+00:00",
        "duration_seconds": max((t["completed_at_s"] for t in result["trips"]), default=0),
        "summary": result["summary"], "fleet_metrics": None,
        "trips": [_ai_dispatch_trip_row_to_sim_trip((
            uuid.UUID(t["trip_id"]), t["driver_id"], t["truck_number"], t["hub_city"], t["assigned_at_s"],
            t["completed_at_s"], t["arr_pickup_at_s"], t["dep_pickup_at_s"], t["arr_delivery_at_s"],
            t["origin_location_id"], t["dest_location_id"], t["origin_label"], t["dest_label"],
            t["weight_lbs"], t["pallets"], t["load_type"], t["order_revenue"], t["deadhead_cost"],
            t["deadhead_miles"], t["net_margin"], t["detention_amount"], t["is_detention_demo"], t["trajectory"],
        )) for t in result["trips"]],
    }


@app.get("/api/simulation/ai-dispatch-runs")
def list_ai_dispatch_runs(limit: int = 20):
    with cursor() as cur:
        cur.execute(
            """select run_id, scenario_label, created_at, n_orders_generated, n_completed, n_unassigned,
                      total_revenue, net_margin, total_detention_billed
               from simulation.runs where run_kind = 'ai_dispatch_day' order by created_at desc limit %s""",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {"run_id": str(r[0]), "service_date": r[1], "created_at": r[2].isoformat(), "n_orders_generated": r[3],
         "n_completed": r[4], "n_unassigned": r[5], "total_revenue": float(r[6] or 0), "net_margin": float(r[7] or 0),
         "total_detention_billed": float(r[8] or 0)}
        for r in rows
    ]


@app.get("/api/simulation/ai-dispatch-runs/{run_id}")
def get_ai_dispatch_run(run_id: str):
    with cursor() as cur:
        cur.execute(
            "select scenario_label, week_start, fleet_metrics from simulation.runs where run_id = %s and run_kind = 'ai_dispatch_day'",
            (run_id,),
        )
        run_row = cur.fetchone()
        if run_row is None:
            raise HTTPException(404, "AI dispatch run not found")
        service_date, sim_start, stored_summary = run_row

        cur.execute(
            """select trip_id, driver_id, truck_number, hub_city, assigned_at_s, completed_at_s,
                      arr_pickup_at_s, dep_pickup_at_s, arr_delivery_at_s, origin_location_id, dest_location_id,
                      origin_label, dest_label, weight_lbs, pallets, load_type, order_revenue, deadhead_cost,
                      deadhead_miles, net_margin, detention_amount, is_detention_demo, trajectory
               from simulation.ai_dispatch_trips where run_id = %s order by assigned_at_s""",
            (run_id,),
        )
        trips = [_ai_dispatch_trip_row_to_sim_trip(r) for r in cur.fetchall()]

    return {
        "run_id": run_id, "seed": 0, "sim_start": sim_start.isoformat() if sim_start else None,
        "duration_seconds": max((t["completed_at_s"] for t in trips), default=0),
        "summary": stored_summary or {},
        "fleet_metrics": None, "trips": trips,
    }


@app.get("/api/simulation/orders/{quote_id}/story")
def simulation_order_story(quote_id: str):
    """The "explain everything" drill-in real user feedback asked for: one order's whole real
    story -- every candidate the model actually considered, which one it picked and why (vs. the
    average of the OTHER real candidates it had on the table, not a fabricated theoretical
    baseline), the trip's real lifecycle (geofence-confirmed arrivals/departures), detention/
    invoice if any, and what this same driver did right after -- did THEY have to deadhead to
    their next job, or reload immediately. Everything here is read directly from simulation.*
    (sim/sql/038-040) -- nothing recomputed or guessed.
    """
    from sim.config import ASSUMED_OPERATING_COST_PER_MILE

    with cursor() as cur:
        cur.execute(
            """select quote_id, run_id, origin_location_id, dest_location_id, requested_at,
                      requested_pickup_at, weight_lbs, pallets, load_type, service_type, status
               from simulation.quote_requests where quote_id = %s""",
            (quote_id,),
        )
        q = cur.fetchone()
        if q is None:
            raise HTTPException(404, "order not found")
        (_quote_id, run_id, origin_id, dest_id, requested_at, requested_pickup_at,
         weight_lbs, pallets, load_type, service_type, status) = q

        cur.execute("select label from reference.locations where location_id = %s", (origin_id,))
        origin_label = (cur.fetchone() or [None])[0]
        cur.execute("select label from reference.locations where location_id = %s", (dest_id,))
        dest_label = (cur.fetchone() or [None])[0]

        # Real user feedback: "are we only evaluating 10 candidates or scored for all 30 drivers?
        # can we show the score all 30 here along with the features we use for getting the
        # score?" -- run_sim.py's engine always scored every feasible driver (up to the full 30-
        # truck demo fleet); only the PERSISTED audit trail was capped at 10 (TOP_N_CANDIDATES_TO_LOG,
        # a constant that exists for a completely different contract -- sim.candidate_scores'
        # fixed-size learning-to-rank training rows, confirmed unused here). Fixed at the source
        # (run_sim.py logs the FULL feasible ranking now) -- this query already had no cap of its
        # own, so it now returns everything that got persisted. Added the remaining real feature
        # columns (truck_pct_km_interval/truck_pct_days_interval/planned_duty_hours) that were
        # already being scored on and stored, just not surfaced here.
        cur.execute(
            """select driver_id, truck_number, location_id, hos_remaining_hours, truck_breakdown_risk,
                      truck_pct_km_interval, truck_pct_days_interval, deadhead_miles,
                      planned_driving_hours, planned_duty_hours, score, rank, was_assigned, scored_at
               from simulation.quote_candidate_snapshots where quote_id = %s order by rank""",
            (quote_id,),
        )
        cand_cols = ["driver_id", "truck_number", "location_id", "hos_remaining_hours", "truck_breakdown_risk",
                     "truck_pct_km_interval", "truck_pct_days_interval", "deadhead_miles",
                     "planned_driving_hours", "planned_duty_hours", "score", "rank", "was_assigned", "scored_at"]
        candidates = [dict(zip(cand_cols, r)) for r in cur.fetchall()]

        chosen = next((c for c in candidates if c["was_assigned"]), None)
        others = [c for c in candidates if not c["was_assigned"]]
        deadhead_vs_avg_alternative = None
        if chosen is not None and others:
            # psycopg2 returns `numeric` columns as Decimal -- cast to float before mixing with
            # the plain-float ASSUMED_OPERATING_COST_PER_MILE constant.
            chosen_deadhead = float(chosen["deadhead_miles"] or 0)
            avg_other_deadhead = float(sum(float(c["deadhead_miles"] or 0) for c in others) / len(others))
            deadhead_vs_avg_alternative = {
                "chosen_deadhead_miles": chosen_deadhead,
                "avg_alternative_deadhead_miles": round(avg_other_deadhead, 1),
                "miles_saved": round(avg_other_deadhead - chosen_deadhead, 1),
                "value_saved": round((avg_other_deadhead - chosen_deadhead) * ASSUMED_OPERATING_COST_PER_MILE, 2),
                "n_other_candidates": len(others),
            }

        trip = None
        if chosen is not None or status == "assigned":
            cur.execute(
                """select t.trip_id, t.driver_id, tl.truck_number, t.eta, tl.completed_at, tl.on_time,
                          tl.load_fill_ratio, tl.order_revenue, tl.deadhead_cost, tl.post_delivery_deadhead_cost,
                          tl.post_delivery_deadhead_miles, tl.lateness_penalty_amount, tl.net_margin,
                          tl.breakdown_occurred, t.trajectory, t.pre_pickup_deadhead_miles, t.loaded_miles
                   from simulation.trips t join simulation.trip_log tl on tl.trip_id = t.trip_id
                   where t.quote_id = %s""",
                (quote_id,),
            )
            trow = cur.fetchone()
            if trow:
                (trip_id, driver_id, truck_number, assigned_at, completed_at, on_time, fill_ratio,
                 order_revenue, deadhead_cost, post_dh_cost, post_dh_miles, lateness_penalty, net_margin,
                 had_breakdown, this_trajectory, pre_pickup_deadhead_miles, loaded_miles) = trow
                # Same yard-shuttle exclusion as the run-time/reload KPI computations (see
                # api_simulation_run's comment) -- a ~0-mile hauled leg never actually left the
                # yard, so "reloaded immediately" right after it isn't a real deadhead-avoidance
                # outcome to credit the model for.
                is_real_freight_move = float(loaded_miles or 0) >= 0.1

                cur.execute(
                    "select location_id, event_type, occurred_at from simulation.geofence_events where trip_id = %s order by occurred_at",
                    (trip_id,),
                )
                geofence_events = [
                    {"location_id": loc, "location_label": origin_label if loc == origin_id else dest_label,
                     "event_type": et, "occurred_at": occ.isoformat()}
                    for loc, et, occ in cur.fetchall()
                ]

                cur.execute(
                    "select location_id, arrival_at, departure_at, billable_hours, amount from simulation.detention_billing where trip_id = %s",
                    (trip_id,),
                )
                detention = [
                    {"location_id": loc, "location_label": origin_label if loc == origin_id else dest_label,
                     "arrival_at": arr.isoformat() if arr else None, "departure_at": dep.isoformat() if dep else None,
                     "billable_hours": float(bh or 0), "amount": float(amt or 0)}
                    for loc, arr, dep, bh, amt in cur.fetchall()
                ]

                cur.execute(
                    """select invoice_number, linehaul_amount, detention_amount, fuel_surcharge_amount, subtotal, tax_amount, total_amount, status
                       from simulation.invoices where trip_id = %s""",
                    (trip_id,),
                )
                irow = cur.fetchone()
                invoice = None
                if irow:
                    inv_number, linehaul, det_amt, fuel, subtotal, tax, total, inv_status = irow
                    invoice = {"invoice_number": inv_number, "linehaul_amount": float(linehaul or 0),
                               "detention_amount": float(det_amt or 0), "fuel_surcharge_amount": float(fuel or 0),
                               "subtotal": float(subtotal or 0), "tax_amount": float(tax or 0),
                               "total_amount": float(total or 0), "status": inv_status}

                # This SAME driver's PREVIOUS and NEXT trip in this run -- real user feedback:
                # "show the previous trip ... how this saved deadhead miles ... the whole route of
                # them going back to home zone." The low (often zero) deadhead on THIS trip is
                # usually explained by where the PREVIOUS trip physically ended -- showing both,
                # with real trajectories, lets the map draw the driver's actual continuous path.
                def _adjacent_trip(cur, comparator: str, order_dir: str):
                    cur.execute(
                        f"""select t2.trip_id, t2.pre_pickup_deadhead_miles, t2.origin_location_id, t2.dest_location_id,
                                   t2.eta, t2.trajectory, tl2.completed_at
                            from simulation.trips t2 left join simulation.trip_log tl2 on tl2.trip_id = t2.trip_id
                            where t2.run_id = %s and t2.driver_id = %s and t2.eta {comparator} %s
                            order by t2.eta {order_dir} limit 1""",
                        (run_id, driver_id, assigned_at),
                    )
                    row2 = cur.fetchone()
                    if not row2:
                        return None
                    t2_id, t2_deadhead, t2_origin, t2_dest, t2_eta, t2_traj, t2_completed = row2
                    cur.execute("select label from reference.locations where location_id = %s", (t2_origin,))
                    t2_origin_label = (cur.fetchone() or [None])[0]
                    cur.execute("select label from reference.locations where location_id = %s", (t2_dest,))
                    t2_dest_label = (cur.fetchone() or [None])[0]
                    return {
                        "trip_id": str(t2_id), "assigned_at": t2_eta.isoformat() if t2_eta else None,
                        "completed_at": t2_completed.isoformat() if t2_completed else None,
                        "origin_label": t2_origin_label, "dest_label": t2_dest_label,
                        "deadhead_miles": float(t2_deadhead or 0), "had_deadhead": float(t2_deadhead or 0) > 0,
                        "trajectory": t2_traj or [],
                    }

                previous_trip = _adjacent_trip(cur, "<", "desc")
                next_trip = _adjacent_trip(cur, ">", "asc")

                # This trip's real CYCLE (home base -> home base) -- every trip this SAME driver
                # took, in order, from the last time they were at home base through the next time
                # they get back (or through the end of this run's data if they don't yet). The
                # direct real-data counterpart of sim/engine/fleet_metrics.py's cycles_for_driver()
                # (reused conceptually, not copy-pasted -- that one walks in-memory CompletedTrip
                # objects at run time; this one walks the SAME driver's persisted rows on reload,
                # since Order Story is always a reload-shaped read). Real user feedback: "order
                # story should show all the trips driver took in that cycle instead of just before
                # this and after... P1 P2... D1 D2... H" -- and: the order book's own "+$X saved"
                # badge (reload_immediate) was showing on trips whose story never explained where
                # the $ came from -- a real gap, not a display choice: Section 7's "Return leg"
                # stat used to hardcode "$0" for a reloaded-immediately trip instead of the actual
                # value avoided. Fixed by computing that SAME real figure here, once, and reusing
                # it both on the cycle's own per-trip list and on `trip` itself below.
                cycle = _compute_cycle(cur, run_id, driver_id, trip_id)
                current_cycle_trip = next((t for t in cycle["trips"] if t["is_current"]), None) if cycle else None
                reload_savings_value = current_cycle_trip["reload_savings_value"] if current_cycle_trip else 0.0

                trip = {
                    "trip_id": str(trip_id), "driver_id": driver_id, "truck_number": truck_number,
                    "assigned_at": assigned_at.isoformat() if assigned_at else None,
                    "completed_at": completed_at.isoformat() if completed_at else None,
                    "on_time": bool(on_time), "load_fill_ratio": float(fill_ratio or 0),
                    "order_revenue": round(float(order_revenue or 0), 2),
                    "deadhead_cost": round(float(deadhead_cost or 0), 2),
                    "deadhead_miles": round(float(pre_pickup_deadhead_miles or 0), 1),
                    "post_delivery_deadhead_cost": round(float(post_dh_cost or 0), 2),
                    "post_delivery_deadhead_miles": round(float(post_dh_miles or 0), 1),
                    "reloaded_immediately": is_real_freight_move and float(post_dh_miles or 0) <= 0 and next_trip is not None,
                    "reload_savings_value": round(reload_savings_value, 2),
                    "lateness_penalty": round(float(lateness_penalty or 0), 2),
                    "net_margin": round(float(net_margin or 0), 2),
                    "had_breakdown": bool(had_breakdown),
                    "geofence_events": geofence_events, "detention": detention, "invoice": invoice,
                    "trajectory": this_trajectory or [],
                    "previous_trip": previous_trip, "next_trip": next_trip,
                    "cycle": cycle,
                }

    return {
        "quote": {
            "quote_id": quote_id, "origin_label": origin_label, "dest_label": dest_label,
            "requested_at": requested_at.isoformat(), "requested_pickup_at": requested_pickup_at.isoformat(),
            "weight_lbs": float(weight_lbs or 0), "pallets": float(pallets or 0), "load_type": load_type,
            "service_type": service_type, "status": status,
        },
        "candidates": candidates,
        "chosen_driver_id": chosen["driver_id"] if chosen else None,
        "deadhead_vs_avg_alternative": deadhead_vs_avg_alternative,
        "trip": trip,
    }


class TripDemoStartBody(BaseModel):
    origin_location_id: int
    dest_location_id: int
    scenario: str  # 'baseline' | 'detention' -- see sim/live/trip_demo_simulator.SCENARIOS
    driver_id: int | None = None
    weight_lbs: float = 22000
    pallets: float = 12
    load_type: str = "Dry Van"


@app.get("/api/trip-demo/scenarios")
def trip_demo_scenarios():
    """Scenario labels/dwell hours the frontend picker shows -- single source of truth, sim/live/
    trip_demo_simulator.SCENARIOS, so the UI copy never drifts from what actually gets simulated."""
    return SCENARIOS


# Real user ask: geofence editing needs to happen BEFORE the truck starts moving, not layered on
# top of an already-ticking simulation -- "first I should have the option to edit geofence, then
# after I set it it should start." /start now only CREATES the trip (real trip_id, needed since
# geofence overrides are keyed by trip_id) and parks its tick loop on this event instead of
# launching it immediately; /begin below releases it. One in-memory registry is enough here (a
# single-process demo app, not a distributed system) -- no DB schema change needed since the
# already-built DemoTripHandle is just held, not reconstructed from scratch later.
_pending_trip_demo_starts: dict[str, threading.Event] = {}


def _run_trip_demo_when_signaled(handle, ready_event: threading.Event) -> None:
    ready_event.wait()
    run_trip_demo(handle)


@app.post("/api/trip-demo/start")
def trip_demo_start(body: TripDemoStartBody):
    """Creates the single-trip live geofence/detention demo's trip (sim/live/trip_demo_simulator.py)
    -- 'Simulation Trip' page -- and returns immediately with its real ids, but does NOT start the
    truck moving yet. The tick loop (real ticks over several real seconds/minutes, TIME_SCALE-
    accelerated, writing to `simulation.*` as it goes, polled via GET /api/trip-demo/{trip_id}/log)
    only begins once POST /api/trip-demo/{trip_id}/begin is called -- real user ask, so a manager
    can set up a custom pickup/dropoff geofence against the real trip_id first, THEN start the run,
    instead of the two happening at the same moment.
    """
    if body.scenario not in SCENARIOS:
        raise HTTPException(400, f"scenario must be one of {list(SCENARIOS)}")
    try:
        handle = start_trip_demo(
            body.origin_location_id, body.dest_location_id, body.scenario,
            weight_lbs=body.weight_lbs, pallets=body.pallets, load_type=body.load_type,
            driver_id=body.driver_id,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    ready_event = threading.Event()
    _pending_trip_demo_starts[str(handle.trip_id)] = ready_event
    threading.Thread(target=_run_trip_demo_when_signaled, args=(handle, ready_event), daemon=True).start()
    return {
        "run_id": str(handle.run_id), "trip_id": str(handle.trip_id), "quote_id": str(handle.quote_id),
        "driver_id": handle.driver_id, "truck_number": handle.truck_number, "scenario": handle.scenario,
        # The SAME geometry run_trip_demo ticks against (DemoTripHandle.route_coords) -- the
        # frontend draws this directly instead of a second, independent /api/route fetch.
        "route_coords": [list(c) for c in handle.route_coords],
    }


@app.post("/api/trip-demo/{trip_id}/begin")
def trip_demo_begin(trip_id: str):
    """Releases a trip created by /start to actually begin ticking. 404s if this trip_id was
    never parked here (already begun, or an unknown id) -- idempotent-safe: calling it twice just
    404s the second time rather than double-starting anything."""
    event = _pending_trip_demo_starts.pop(trip_id, None)
    if event is None:
        raise HTTPException(404, "No pending trip demo waiting to start for this trip_id")
    event.set()
    return {"status": "started"}


@app.get("/api/trip-demo/{trip_id}/log")
def trip_demo_log(trip_id: str, since_id: int = 0):
    """Polled by the frontend every couple of real seconds while a demo trip is running -- new
    telemetry rows since `since_id`, plus the trip's current status and any geofence/detention
    rows recorded SO FAR, so the UI can raise a live alert the instant either fires (it doesn't
    have to wait for trip completion to know an arrival/departure/detention charge just happened).
    """
    with cursor() as cur:
        cur.execute(
            """select id, recorded_at, phase, lat, lon, speed_mph, fuel_pct, odometer_km, note
               from simulation.trip_telemetry_log where trip_id = %s and id > %s order by id""",
            (trip_id, since_id),
        )
        cols = ["id", "recorded_at", "phase", "lat", "lon", "speed_mph", "fuel_pct", "odometer_km", "note"]
        telemetry = []
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            r["recorded_at"] = r["recorded_at"].isoformat()
            for k in ("lat", "lon", "speed_mph", "fuel_pct", "odometer_km"):
                r[k] = float(r[k]) if r[k] is not None else None
            telemetry.append(r)

        cur.execute("select status, last_event, quote_id from simulation.trips where trip_id = %s", (trip_id,))
        trow = cur.fetchone()
        if trow is None:
            raise HTTPException(404, "demo trip not found")
        status, last_event, quote_id = trow

        cur.execute(
            "select location_id, event_type, occurred_at from simulation.geofence_events where trip_id = %s order by occurred_at",
            (trip_id,),
        )
        geofence_events = [
            {"location_id": loc, "event_type": et, "occurred_at": occ.isoformat()} for loc, et, occ in cur.fetchall()
        ]

        cur.execute(
            "select location_id, arrival_at, departure_at, billable_hours, amount from simulation.detention_billing where trip_id = %s",
            (trip_id,),
        )
        detention = [
            {"location_id": loc, "arrival_at": arr.isoformat() if arr else None,
             "departure_at": dep.isoformat() if dep else None, "billable_hours": float(bh or 0), "amount": float(amt or 0)}
            for loc, arr, dep, bh, amt in cur.fetchall()
        ]

    return {
        "status": status, "last_event": last_event, "quote_id": str(quote_id) if quote_id else None,
        "telemetry": telemetry, "geofence_events": geofence_events, "detention": detention,
    }


class GenerateInvoiceBody(BaseModel):
    trip_id: str


@app.post("/api/invoices/generate")
def generate_invoice(body: GenerateInvoiceBody):
    """CRA-itemized invoice against simulation.invoices (sim/sql/038) -- linehaul, detention, fuel
    surcharge, and accessorial kept SEPARATE (not pre-summed), subtotal/tax/total are the schema's
    own generated columns. Real user pivot: the simulation is now the app's only data source (no
    more live.* telemetry) -- sim/live/ai_dispatch_replay.py's generate() already auto-creates a
    'draft' invoice per trip right after a replay, so this endpoint is now mainly an idempotent
    fallback (a trip somehow missing one), not the primary path. delivery_province is hardcoded
    'ON' -- every real order in this dataset is Ontario-to-Ontario (documents/schema_reference.md),
    not a guess for THIS fleet, though the column exists for a future non-ON delivery. bill_to_
    name/address are SYNTHESIZED from the delivery location's label -- no real shipper-identity
    table exists anywhere in the source data, not presented as real customer contact info.
    """
    with cursor() as cur:
        cur.execute(
            "select driver_id, truck_number, loaded_miles, completed_at, load_fill_ratio from simulation.trip_log where trip_id = %s",
            (body.trip_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "No completed trip_log row for this trip_id")
        _driver_id, _truck_number, loaded_miles, completed_at, load_fill_ratio = row

        cur.execute(
            "select dest_location_id, quote_id, weight_lbs, pallets, load_type from simulation.trips where trip_id = %s",
            (body.trip_id,),
        )
        trip_row = cur.fetchone()
        dest_location_id, quote_id, weight_lbs, pallets, load_type = trip_row if trip_row else (None, None, None, None, None)

        service_type = "FTL"
        if quote_id:
            cur.execute("select service_type from simulation.quote_requests where quote_id = %s", (quote_id,))
            st_row = cur.fetchone()
            if st_row and st_row[0]:
                service_type = st_row[0]

        dest_label = "Unknown destination"
        if dest_location_id:
            cur.execute("select label from reference.locations where location_id = %s", (dest_location_id,))
            r = cur.fetchone()
            if r:
                dest_label = r[0]

        cur.execute("select amount from simulation.detention_billing where trip_id = %s", (body.trip_id,))
        det_row = cur.fetchone()
        detention_amount = float(det_row[0]) if det_row and det_row[0] is not None else 0.0

        # Real 2026-researched pricing (sim/config.py's quote_price()) -- distance-tiered base
        # rate x truck-type premium (dry van/reefer/flatbed) x LTL/fill treatment, plus fuel
        # surcharge -- the SAME function the live quote summary shows the dispatcher up front
        # (sim/live/score_quote.py), so what a customer was quoted and what they're actually
        # invoiced are computed identically, not two independent guesses. A flat
        # 'linehaul_rate_per_mile' key doesn't exist in calibration.assumptions (only the
        # per-tier keys do) -- caught directly as a live 500 before this was fixed.
        from sim.config import quote_price
        fill = float(load_fill_ratio) if load_fill_ratio is not None else 1.0
        pricing = quote_price(float(loaded_miles or 0), load_type or "Dry Van", service_type, fill)
        linehaul_amount = pricing["linehaul_amount"]
        fuel_surcharge_amount = pricing["fuel_surcharge_amount"]

        cur.execute("select count(*) from simulation.invoices")
        (seq,) = cur.fetchone()
        invoice_number = f"RS-{datetime.now(timezone.utc).year}-{seq + 1:04d}"

        invoice_id = uuid.uuid4()
        issued_at = completed_at or datetime.now(timezone.utc)
        due_at = issued_at + timedelta(days=30)
        cur.execute(
            """insert into simulation.invoices
                 (invoice_id, invoice_number, trip_id, quote_id, issued_at, due_at, bill_to_name, bill_to_address,
                  delivery_province, linehaul_amount, detention_amount, fuel_surcharge_amount)
               values (%s, %s, %s, %s, %s, %s, %s, %s, 'ON', %s, %s, %s)""",
            (invoice_id, invoice_number, body.trip_id, quote_id, issued_at, due_at, dest_label, dest_label,
             linehaul_amount, detention_amount, fuel_surcharge_amount),
        )

    return {"invoice_id": str(invoice_id), "invoice_number": invoice_number}


def _build_invoice_pdf(inv: dict) -> bytes:
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("<b>RoadStar Dispatch</b> — Southern Ontario Regional Freight", styles["Title"]),
        Spacer(1, 4),
        Paragraph(f"Invoice {inv['invoice_number']}", styles["Heading2"]),
        Paragraph(f"Issued: {inv['issued_at'].strftime('%B %d, %Y')} &nbsp;&nbsp; Due: {inv['due_at'].strftime('%B %d, %Y') if inv['due_at'] else '—'}", styles["Normal"]),
        Spacer(1, 12),
        Paragraph(f"<b>Bill To:</b> {inv['bill_to_name'] or '—'}", styles["Normal"]),
        Paragraph(f"{inv['bill_to_address'] or ''}", styles["Normal"]),
        Paragraph(f"Delivery province: {inv['delivery_province']} (HST/GST follows delivery province, not carrier's home province)", styles["Normal"]),
        Spacer(1, 16),
    ]

    rows = [["Description", "Amount (CAD)"]]
    rows.append(["Linehaul", f"${inv['linehaul_amount'] or 0:.2f}"])
    if inv["detention_amount"]:
        rows.append(["Detention (beyond 2h free)", f"${inv['detention_amount']:.2f}"])
    if inv["fuel_surcharge_amount"]:
        rows.append(["Fuel surcharge", f"${inv['fuel_surcharge_amount']:.2f}"])
    if inv["accessorial_amount"]:
        rows.append(["Accessorial", f"${inv['accessorial_amount']:.2f}"])
    rows.append(["Subtotal", f"${inv['subtotal']:.2f}"])
    rows.append([f"HST ({float(inv['tax_rate']) * 100:.0f}%)", f"${inv['tax_amount']:.2f}"])
    rows.append(["Total Due", f"${inv['total_amount']:.2f}"])

    table = Table(rows, colWidths=[4 * inch, 2 * inch])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e46b3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("LINEBELOW", (0, -3), (-1, -3), 1, colors.grey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "Trucking-specific line items itemized separately per CRA invoicing requirements (an invoice over "
        "CAD 150 must show recipient, date, description of supply, and GST/HST amount separately from the subtotal).",
        styles["Normal"],
    ))
    doc.build(story)
    return buf.getvalue()


@app.get("/api/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: str):
    with cursor() as cur:
        cur.execute(
            """select invoice_number, issued_at, due_at, bill_to_name, bill_to_address, delivery_province,
                      linehaul_amount, detention_amount, fuel_surcharge_amount, accessorial_amount,
                      subtotal, tax_rate, tax_amount, total_amount
               from simulation.invoices where invoice_id = %s""",
            (invoice_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Invoice not found")
        cols = ["invoice_number", "issued_at", "due_at", "bill_to_name", "bill_to_address", "delivery_province",
                "linehaul_amount", "detention_amount", "fuel_surcharge_amount", "accessorial_amount",
                "subtotal", "tax_rate", "tax_amount", "total_amount"]
        inv = dict(zip(cols, row))

    pdf_bytes = _build_invoice_pdf(inv)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{inv["invoice_number"]}.pdf"'},
    )


@app.post("/api/invoices/{invoice_id}/mark-sent")
def mark_invoice_sent(invoice_id: str):
    """No real email delivery yet (confirmed with the user -- PDF generation only, for now) --
    this just records the invoice as sent in the database, stated plainly in the UI, not
    presented as an actual email having gone out. simulation.invoices has no sent_at column
    (unlike the old live.invoices) -- status alone is enough for a demo invoice."""
    with cursor() as cur:
        cur.execute("update simulation.invoices set status = 'sent' where invoice_id = %s", (invoice_id,))
    return {"status": "sent"}


# --------------------------------------------------------------------------------------------
# Dispatch Board -- manual day-ahead dispatch (real hackathon-lead clarification: a fleet manager
# assigns tomorrow's work today by hand, trucks/orders/drivers matched by type/capacity/hub, not a
# single live-scored quote at a time). Thin wrappers only -- all DB logic lives in
# sim/live/dispatch_board.py, matching this file's existing score_quote.py/seed_demo_fleet.py split.
# --------------------------------------------------------------------------------------------

def _dispatch_date(date_str: str) -> date:
    """`date_str == "tomorrow"` resolves server-side -- "always the day before" is the product's
    own stated assumption (a fleet manager always plans tomorrow's work today), not a UI nicety,
    so the frontend doesn't need to compute or care about the server's notion of "today"."""
    if date_str == "tomorrow":
        return (datetime.now(timezone.utc) + timedelta(days=1)).date()
    return date.fromisoformat(date_str)


class AssignOrderBody(BaseModel):
    truck_number: str
    order_id: uuid.UUID


class AssignDriverBody(BaseModel):
    truck_number: str
    driver_id: int


class UnassignOrderBody(BaseModel):
    truck_number: str
    order_id: uuid.UUID


class UnassignDriverBody(BaseModel):
    truck_number: str


class SimulateSetupBody(BaseModel):
    hub_counts: dict[str, int]
    type_shares: dict[str, float]
    num_orders: int
    seed: int | None = None


@app.get("/api/dispatch/{date_str}")
def get_dispatch_board(date_str: str):
    """Real user ask: no more silent auto-generation of a default-parameter day the first time a
    date is opened -- the frontend checks this first; a 404 here means "no day yet, show the Setup
    panel" (POST .../simulate creates one), not an error state."""
    try:
        service_date = _dispatch_date(date_str)
        if dispatch_board.day_status(service_date) is None:
            raise HTTPException(404, "no dispatch day generated yet for this date -- run Simulate first")
        return dispatch_board.load_board(service_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/dispatch/{date_str}/simulate")
def dispatch_simulate_setup(date_str: str, body: SimulateSetupBody):
    """The Setup panel's "Simulate" button -- see sim/live/dispatch_board.py's simulate_setup()."""
    try:
        return dispatch_board.simulate_setup(
            _dispatch_date(date_str), body.hub_counts, body.type_shares, body.num_orders, body.seed,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/dispatch/{date_str}/assign-order")
def dispatch_assign_order(date_str: str, body: AssignOrderBody):
    try:
        rows = dispatch_board.assign_order(_dispatch_date(date_str), body.truck_number, body.order_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"assignments": rows}


@app.post("/api/dispatch/{date_str}/unassign-order")
def dispatch_unassign_order(date_str: str, body: UnassignOrderBody):
    try:
        rows = dispatch_board.unassign_order(_dispatch_date(date_str), body.truck_number, body.order_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"assignments": rows}


@app.post("/api/dispatch/{date_str}/assign-driver")
def dispatch_assign_driver(date_str: str, body: AssignDriverBody):
    try:
        rows = dispatch_board.assign_driver(_dispatch_date(date_str), body.truck_number, body.driver_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"assignments": rows}


@app.post("/api/dispatch/{date_str}/unassign-driver")
def dispatch_unassign_driver(date_str: str, body: UnassignDriverBody):
    try:
        rows = dispatch_board.unassign_driver(_dispatch_date(date_str), body.truck_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"assignments": rows}


@app.post("/api/dispatch/{date_str}/ai-assign")
def dispatch_ai_assign(date_str: str):
    try:
        return dispatch_board.ai_assign(_dispatch_date(date_str))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/dispatch/{date_str}/reset")
def dispatch_reset(date_str: str):
    try:
        return dispatch_board.reset_assignments(_dispatch_date(date_str))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/dispatch/{date_str}/regenerate-orders")
def dispatch_regenerate_orders(date_str: str):
    """DEMO-ONLY -- see sim/live/dispatch_board.py's regenerate_order_book() docstring."""
    try:
        return dispatch_board.regenerate_order_book(_dispatch_date(date_str))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/dispatch/{date_str}/finalize")
def dispatch_finalize(date_str: str):
    try:
        dispatch_board.finalize(_dispatch_date(date_str))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "finalized"}


@app.post("/api/dispatch/{date_str}/reopen")
def dispatch_reopen(date_str: str):
    try:
        dispatch_board.reopen(_dispatch_date(date_str))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "draft"}


class TripGeofenceBody(BaseModel):
    location_id: int
    points: list[tuple[float, float]]  # [(lat, lon), ...], polygon vertices in map-drawn order


@app.get("/api/trips/{trip_id}")
def trip_detail(trip_id: uuid.UUID):
    """Real user ask: every trip under each truck/driver on the Final Dispatch Plan should be a
    clickable page -- full detail (pickup/dropoff, times, truck/load) plus the map + geofence
    editor (see /api/trips/{trip_id}/geofence below)."""
    with cursor() as cur:
        cur.execute("""
            select t.trip_id, t.driver_id, t.status, t.eta, t.created_at, t.planned_completion_at,
                   t.origin_location_id, ol.label, ol.city, ol.lat, ol.lon, ol.radius_m,
                   t.dest_location_id, dl.label, dl.city, dl.lat, dl.lon, dl.radius_m,
                   t.weight_lbs, t.pallets, t.load_type,
                   ds.truck_number, tp.truck_type, tp.capacity_lbs, tp.capacity_pallets
            from live.trips t
            join reference.locations ol on ol.location_id = t.origin_location_id
            join reference.locations dl on dl.location_id = t.dest_location_id
            left join live.driver_status ds on ds.driver_id = t.driver_id
            left join calibration.truck_profile tp on tp.truck_number = ds.truck_number
            where t.trip_id = %s
        """, (str(trip_id),))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Trip not found")
        (tid, driver_id, status, eta, created_at, planned_completion_at,
         origin_id, origin_label, origin_city, origin_lat, origin_lon, origin_radius,
         dest_id, dest_label, dest_city, dest_lat, dest_lon, dest_radius,
         weight_lbs, pallets, load_type,
         truck_number, truck_type, capacity_lbs, capacity_pallets) = row

        cur.execute(
            "select location_id, ST_AsGeoJSON(geom::geometry) from live.trip_geofence_overrides where trip_id = %s",
            (str(trip_id),),
        )
        overrides = {r[0]: json.loads(r[1]) for r in cur.fetchall()}

    def stop(location_id, label, city, lat, lon, radius_m):
        override = overrides.get(location_id)
        return {
            'location_id': location_id, 'label': label, 'city': f"{city}, ON",
            'lat': float(lat), 'lon': float(lon),
            'default_radius_m': float(radius_m) if radius_m is not None else 120.0,
            'geofence_source': 'manual' if override else 'default',
            'manual_geometry': override,  # GeoJSON polygon ([lon,lat] rings) or null
        }

    return {
        'trip_id': str(tid), 'driver_id': driver_id, 'status': status,
        'eta': eta.isoformat() if eta else None,
        'created_at': created_at.isoformat() if created_at else None,
        'planned_completion_at': planned_completion_at.isoformat() if planned_completion_at else None,
        'truck_number': truck_number, 'truck_type': truck_type,
        'capacity_lbs': float(capacity_lbs) if capacity_lbs is not None else None,
        'capacity_pallets': capacity_pallets,
        'weight_lbs': float(weight_lbs) if weight_lbs is not None else None,
        'pallets': pallets, 'load_type': load_type,
        'pickup': stop(origin_id, origin_label, origin_city, origin_lat, origin_lon, origin_radius),
        'dropoff': stop(dest_id, dest_label, dest_city, dest_lat, dest_lon, dest_radius),
    }


@app.get("/api/trips/{trip_id}/geofence")
def get_trip_geofence(trip_id: uuid.UUID, location_id: int):
    """Schema-agnostic (no dependency on live.trips OR simulation.trips existing) -- just the
    override status + shape for one (trip_id, location_id) pair, plus the location's own default
    radius as a fallback. Used by the Simulation Trip demo page, which has its own trip/location
    IDs already in hand and doesn't need the fuller /api/trips/{trip_id} (real-dispatch-only) join."""
    with cursor() as cur:
        cur.execute("select radius_m from reference.locations where location_id = %s", (location_id,))
        row = cur.fetchone()
        default_radius = float(row[0]) if row and row[0] is not None else 120.0
        cur.execute(
            "select ST_AsGeoJSON(geom::geometry) from live.trip_geofence_overrides where trip_id = %s and location_id = %s",
            (str(trip_id), location_id),
        )
        geom_row = cur.fetchone()
    manual_geometry = json.loads(geom_row[0]) if geom_row else None
    return {
        'default_radius_m': default_radius,
        'geofence_source': 'manual' if manual_geometry else 'default',
        'manual_geometry': manual_geometry,
    }


@app.post("/api/trips/{trip_id}/geofence")
def save_trip_geofence(trip_id: uuid.UUID, body: TripGeofenceBody):
    """Manager-drawn custom geofence for ONE stop (pickup or dropoff) of ONE trip -- overrides the
    location's own default radius circle for every future position tick on this trip (sim/sql/053
    -- live.process_position_tick() AND simulation.process_position_tick() both check this table
    first). Works identically for a real dispatch trip (live.trips) or a Simulation Trip demo run
    (simulation.trips) -- the override table is keyed by trip_id + location_id only, no schema tie."""
    if len(body.points) < 3:
        raise HTTPException(400, "A geofence needs at least 3 points")
    ring = list(body.points) + [body.points[0]]
    wkt = "POLYGON((" + ", ".join(f"{lon} {lat}" for lat, lon in ring) + "))"
    with cursor() as cur:
        cur.execute(
            """insert into live.trip_geofence_overrides (trip_id, location_id, geom)
               values (%s, %s, ST_GeogFromText(%s))
               on conflict (trip_id, location_id) do update set geom = excluded.geom, created_at = now()""",
            (str(trip_id), body.location_id, wkt),
        )
    return {"status": "saved"}


@app.delete("/api/trips/{trip_id}/geofence")
def clear_trip_geofence(trip_id: uuid.UUID, location_id: int):
    """Reverts one stop back to the location's default radius circle."""
    with cursor() as cur:
        cur.execute(
            "delete from live.trip_geofence_overrides where trip_id = %s and location_id = %s",
            (str(trip_id), location_id),
        )
    return {"status": "reverted"}


@app.get("/api/drivers/{driver_id}/trips")
def driver_trip_history(driver_id: int):
    """Past (completed, live.trip_log) + future (queued 'scheduled', live.trips) trips for ONE
    driver -- real user ask: clicking a truck on Live Ops should show its full picture (past
    trips, current trip, upcoming trips, HOS), not just whatever's currently active. Current trip
    is already covered by /api/fleet (joined via driver_status.current_trip_id) -- not repeated
    here."""
    with cursor() as cur:
        cur.execute("""
            select tl.trip_id, tl.completed_at, tl.on_time, t.origin_location_id, t.dest_location_id,
                   t.weight_lbs, t.pallets, t.load_type, ol.city, dl.city
            from live.trip_log tl
            join live.trips t on t.trip_id = tl.trip_id
            left join reference.locations ol on ol.location_id = t.origin_location_id
            left join reference.locations dl on dl.location_id = t.dest_location_id
            where tl.driver_id = %s
            order by tl.completed_at desc
            limit 10
        """, (driver_id,))
        past = [
            {
                "trip_id": str(r[0]), "completed_at": r[1].isoformat() if r[1] else None, "on_time": r[2],
                "origin_city": r[8], "dest_city": r[9], "weight_lbs": float(r[5]) if r[5] is not None else None,
                "pallets": r[6], "load_type": r[7],
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            select t.trip_id, t.status, t.eta, t.planned_completion_at, t.weight_lbs, t.pallets, t.load_type,
                   ol.city, dl.city
            from live.trips t
            left join reference.locations ol on ol.location_id = t.origin_location_id
            left join reference.locations dl on dl.location_id = t.dest_location_id
            where t.driver_id = %s and t.status = 'scheduled'
            order by t.eta
        """, (driver_id,))
        future = [
            {
                "trip_id": str(r[0]), "status": r[1], "eta": r[2].isoformat() if r[2] else None,
                "planned_completion_at": r[3].isoformat() if r[3] else None,
                "weight_lbs": float(r[4]) if r[4] is not None else None, "pallets": r[5], "load_type": r[6],
                "origin_city": r[7], "dest_city": r[8],
            }
            for r in cur.fetchall()
        ]

    return {"past": past, "future": future}


# --------------------------------------------------------------------------------------------
# Data page -- real user ask: "a page that will show this data tables from simulation showcase
# in detail... like a sql data view page where on top we have tabs for tables and this page shows
# those tables with description on top what this table is". Real user pivot: the simulation is
# now the app's only data source (no more live.* telemetry), so this exposes exactly the
# simulation.* tables sim/live/ai_dispatch_replay.py's generate() populates -- a real, whitelisted
# set (never arbitrary SQL/table names from the client), each with a plain-language description.
# --------------------------------------------------------------------------------------------

DATA_TABLES: dict[str, dict[str, str]] = {
    "runs": {
        "label": "Simulation Runs",
        "description": "One row per AI-dispatch replay run -- a full simulated day. Summarizes orders generated/completed, total revenue, deadhead cost, and detention billed for that run.",
        "order_by": "created_at desc",
    },
    "trips": {
        "label": "Trips",
        "description": "One row per trip the AI dispatcher actually ran that day -- driver, route, load, and status. The core trip record everything else (trip_log, geofence_events, invoices) hangs off of.",
        "order_by": "created_at desc",
    },
    "trip_log": {
        "label": "Trip Log",
        "description": "The completed-trip audit trail: dwell times at pickup/delivery, on-time performance, load fill ratio, and a real dollar reward figure (revenue minus deadhead cost minus detention). This is what the Trip History page displays.",
        "order_by": "completed_at desc",
    },
    "geofence_events": {
        "label": "Geofence Events",
        "description": "Real arrival/departure events recorded at each delivery dock during a replay -- the trigger that starts and stops the detention clock, not a guessed timestamp.",
        "order_by": "occurred_at desc",
    },
    "detention_billing": {
        "label": "Detention Billing",
        "description": "Per-trip-stop detention charges: the real free-hours allowance, arrival/departure timestamps, and the dollar amount owed once a dock stop runs past the free window.",
        "order_by": "arrival_at desc",
    },
    "invoices": {
        "label": "Invoices",
        "description": "The CRA-itemized invoice generated for each completed trip -- linehaul, detention, fuel surcharge, and tax kept as separate line items, with subtotal/tax/total computed by the database itself. This is what the Billing page displays, generates, and sends.",
        "order_by": "issued_at desc",
    },
    "driver_state_snapshots": {
        "label": "Driver State Snapshots",
        "description": "Point-in-time driver/truck state captured at each real milestone of a trip (assigned, arrived at pickup, departed pickup, arrived at delivery, completed) -- position, duty status, and remaining Hours-of-Service at that moment.",
        "order_by": "snapshot_at desc",
    },
}


@app.get("/api/data/tables")
def data_tables():
    """The tab list for the Data page -- table name, display label, and description, in one
    place so the frontend never hardcodes table copy that could drift from what's actually here."""
    return [{"table": name, **meta} for name, meta in DATA_TABLES.items()]


@app.get("/api/data/{table}")
def data_table_rows(table: str, limit: int = 200, filters: str | None = None, sort_col: str | None = None, sort_dir: str = "asc"):
    """Rows for one whitelisted simulation.* table -- never arbitrary SQL or a client-supplied
    table name beyond this fixed set, and columns are read back from the cursor itself (no need to
    hand-maintain a column list per table here that could drift from the real schema).

    Real user ask: Excel-style per-column filters and sort on the Data page (e.g. "filter driver
    state snapshots for a trip and see it", "options to sort in descending or ascending order").
    `filters` is a JSON object of {column: substring} from the frontend's own filter row; `sort_col`
    (with `sort_dir`) overrides the table's default recency order when the user clicks a column
    header. Both happen here, not just on the already-fetched page, since the default view is only
    the most recent 200 rows across every run -- an older trip's snapshots could be well past that
    page, and sorting only the visible page would be misleading rather than a real sort. Column
    NAMES (both filter keys and sort_col) are validated against the real schema (via cur.description
    on the table itself) before ever reaching the query string; values are always parameterized,
    never interpolated -- no SQL injection surface from either side."""
    meta = DATA_TABLES.get(table)
    if meta is None:
        raise HTTPException(404, f"Unknown data table {table!r}")
    filter_dict: dict[str, str] = {}
    if filters:
        try:
            filter_dict = json.loads(filters)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "filters must be a JSON object of {column: substring}") from exc
    with cursor() as cur:
        cur.execute(f"select * from simulation.{table} limit 0")
        all_cols = {d[0] for d in cur.description}
        where_clauses: list[str] = []
        params: list[object] = []
        for col, value in filter_dict.items():
            if col not in all_cols or not isinstance(value, str) or not value.strip():
                continue
            where_clauses.append(f'"{col}"::text ilike %s')
            params.append(f"%{value.strip()}%")
        where_sql = f"where {' and '.join(where_clauses)}" if where_clauses else ""
        if sort_col is not None and sort_col in all_cols:
            direction = "desc" if sort_dir == "desc" else "asc"
            order_sql = f'"{sort_col}" {direction} nulls last'
        else:
            order_sql = meta["order_by"]
        params.append(min(limit, 1000))
        cur.execute(f"select * from simulation.{table} {where_sql} order by {order_sql} limit %s", params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

    def _serialize(v):
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, uuid.UUID):
            return str(v)
        return v

    return {
        "table": table, "columns": cols,
        "rows": [[_serialize(v) for v in row] for row in rows],
    }


# ============================================================================================
# Driver Assist -- real user ask, built under a 1-hour time limit. A driver-only view: today's
# trips as assigned by the Dispatch Board (dispatch.day_orders/assignments -- the real day-ahead
# plan, not the AI-dispatch REPLAY used for the manager's Live Ops Simulation demo), load
# acceptance, duty-status logging (the real 4-status HOS framework), and the pre-trip inspection
# already built (Inspection.tsx) repointed at a working data source. All access goes through this
# service-role connection -- dispatch.* has RLS enabled with zero policies (sim/sql/048), so the
# browser's own Supabase client already can't touch it directly; identity is verified here from
# the driver's real Supabase auth token before any query runs, never trusted from a client param.
# ============================================================================================

def _resolve_driver_id(authorization: str | None) -> int:
    """Verifies the caller's Supabase session token for real (calls Supabase's own /auth/v1/user,
    not a locally-decoded guess) and resolves it to a driver_id via public.profiles -- the same
    real role source auth-context.tsx's own comment establishes ("role is checked server-side via
    this table, never trusted from client state alone")."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    supabase_url = os.environ["VITE_SUPABASE_URL"].rstrip("/")
    resp = requests.get(
        f"{supabase_url}/auth/v1/user",
        headers={"Authorization": f"Bearer {token}", "apikey": os.environ["VITE_SUPABASE_ANON_KEY"]},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(401, "Invalid or expired session")
    user_id = resp.json()["id"]
    with cursor() as cur:
        cur.execute("select driver_id, role from public.profiles where user_id = %s", (user_id,))
        row = cur.fetchone()
    if row is None or row[1] != "driver" or row[0] is None:
        raise HTTPException(403, "Not a driver account")
    return row[0]


@app.get("/api/driver/today")
def driver_today(day: str | None = None, authorization: str | None = Header(default=None)):
    """Assigned trips for the logged-in driver on a given calendar day (defaults to today), from
    the real finalized Dispatch Board plan (whichever driver_id/truck_number the manager actually
    assigned them to). Real user ask: "does this get created automatically for any day the driver
    have trips assigned... can we have a calendar." There's no separate "create Driver Assist"
    step -- it's a live read of whatever dispatch.day_orders/assignments already has for that
    date, same as the manager's own Dispatch Board; `day` just picks which date to read."""
    driver_id = _resolve_driver_id(authorization)
    target_date = date.fromisoformat(day) if day else date.today()
    with cursor() as cur:
        cur.execute("select id, status from dispatch.days where service_date = %s", (target_date,))
        day_row = cur.fetchone()
        if day_row is None:
            return {"service_date": target_date.isoformat(), "day_status": None, "truck_number": None, "trips": []}
        day_id, day_status = day_row

        cur.execute(
            "select truck_number, order_ids from dispatch.assignments where day_id = %s and driver_id = %s",
            (day_id, driver_id),
        )
        assignment = cur.fetchone()
        if assignment is None or not assignment[1]:
            return {"service_date": target_date.isoformat(), "day_status": day_status, "truck_number": None, "trips": []}
        truck_number, order_ids = assignment

        cur.execute(
            """select o.id, o.pickup_location_id, o.dest_location_id, po.label, do_.label,
                      o.weight_lbs, o.pallets, o.load_type, o.rate, o.pickup_at, o.delivery_eta, o.accepted_at
               from dispatch.day_orders o
               join reference.locations po on po.location_id = o.pickup_location_id
               join reference.locations do_ on do_.location_id = o.dest_location_id
               where o.id = any(%s)
               order by o.pickup_at""",
            (order_ids,),
        )
        trips = [
            {
                "order_id": str(r[0]), "pickup_location_id": r[1], "dest_location_id": r[2],
                "pickup_label": r[3], "dest_label": r[4], "weight_lbs": float(r[5]), "pallets": r[6],
                "load_type": r[7], "rate": float(r[8]), "pickup_at": r[9].isoformat(), "delivery_eta": r[10].isoformat() if r[10] else None,
                "accepted_at": r[11].isoformat() if r[11] else None,
            }
            for r in cur.fetchall()
        ]
    return {"service_date": target_date.isoformat(), "day_status": day_status, "truck_number": truck_number, "trips": trips}


class AcceptOrderBody(BaseModel):
    order_id: str


@app.post("/api/driver/accept-order")
def driver_accept_order(body: AcceptOrderBody, authorization: str | None = Header(default=None)):
    """Not scoped to today's date -- a driver can view a future day's trips (see the calendar in
    driver_today) and accept one ahead of time, so ownership is checked directly against the
    order's own day, whichever day that is."""
    driver_id = _resolve_driver_id(authorization)
    with cursor() as cur:
        cur.execute(
            """update dispatch.day_orders o set accepted_at = now()
               where o.id = %s and o.id = any(
                 select unnest(a.order_ids) from dispatch.assignments a
                 where a.driver_id = %s and a.day_id = o.day_id
               )
               returning o.accepted_at""",
            (uuid.UUID(body.order_id), driver_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Order not found in your assignment")
    return {"order_id": body.order_id, "accepted_at": row[0].isoformat()}


class DutyStatusBody(BaseModel):
    status: str
    odometer_km: float | None = None
    note: str | None = None


DUTY_STATUSES = {"off_duty", "sleeper_berth", "driving", "on_duty_not_driving"}


@app.post("/api/driver/duty-status")
def driver_duty_status(body: DutyStatusBody, authorization: str | None = Header(default=None)):
    """Real 4-status HOS duty log (49 CFR Part 395 / Canada's ELD Technical Standard) -- a
    timestamped entry every time the driver's activity changes, same real-world convention an
    ELD follows."""
    if body.status not in DUTY_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(DUTY_STATUSES)}")
    driver_id = _resolve_driver_id(authorization)
    today = date.today()
    with cursor() as cur:
        cur.execute("select id from dispatch.days where service_date = %s", (today,))
        day_row = cur.fetchone()
        day_id = day_row[0] if day_row else None
        cur.execute(
            "insert into dispatch.duty_status_log (day_id, driver_id, status, odometer_km, note) "
            "values (%s, %s, %s, %s, %s) returning id, logged_at",
            (day_id, driver_id, body.status, body.odometer_km, body.note),
        )
        new_id, logged_at = cur.fetchone()
    return {"id": new_id, "status": body.status, "logged_at": logged_at.isoformat()}


@app.get("/api/driver/duty-log")
def driver_duty_log(authorization: str | None = Header(default=None)):
    driver_id = _resolve_driver_id(authorization)
    today = date.today()
    with cursor() as cur:
        cur.execute(
            """select l.status, l.logged_at, l.odometer_km, l.note from dispatch.duty_status_log l
               join dispatch.days d on d.id = l.day_id
               where l.driver_id = %s and d.service_date = %s
               order by l.logged_at desc""",
            (driver_id, today),
        )
        rows = [{"status": r[0], "logged_at": r[1].isoformat(), "odometer_km": float(r[2]) if r[2] is not None else None, "note": r[3]} for r in cur.fetchall()]
    return {"entries": rows}


class InspectionBody(BaseModel):
    brakes_ok: bool
    tires_ok: bool
    lights_ok: bool
    fluid_levels_ok: bool
    coupling_ok: bool
    trailer_ok: bool
    odometer_km: float | None = None
    defects_noted: str | None = None


@app.post("/api/driver/inspection")
def driver_inspection(body: InspectionBody, authorization: str | None = Header(default=None)):
    """Pre-trip DVIR (49 CFR Sec.396.11/.13 -- see Inspection.tsx's own note on the real 11
    federal categories vs. this condensed 6). Repointed here from the dead live.driver_status
    lookup: truck_number now comes from today's real Dispatch Board assignment."""
    driver_id = _resolve_driver_id(authorization)
    today = date.today()
    overall_pass = all([body.brakes_ok, body.tires_ok, body.lights_ok, body.fluid_levels_ok, body.coupling_ok, body.trailer_ok])
    with cursor() as cur:
        cur.execute(
            """select a.truck_number from dispatch.assignments a join dispatch.days d on d.id = a.day_id
               where a.driver_id = %s and d.service_date = %s""",
            (driver_id, today),
        )
        row = cur.fetchone()
        truck_number = row[0] if row else None
        cur.execute(
            """insert into live.vehicle_inspections
               (driver_id, truck_number, odometer_km, brakes_ok, tires_ok, lights_ok, fluid_levels_ok, coupling_ok, trailer_ok, defects_noted)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               returning submitted_at, overall_pass""",
            (driver_id, truck_number, body.odometer_km, body.brakes_ok, body.tires_ok, body.lights_ok,
             body.fluid_levels_ok, body.coupling_ok, body.trailer_ok, body.defects_noted),
        )
        submitted_at, overall_pass = cur.fetchone()
    return {"truck_number": truck_number, "overall_pass": overall_pass, "submitted_at": submitted_at.isoformat()}


@app.get("/api/driver/last-inspection")
def driver_last_inspection(authorization: str | None = Header(default=None)):
    driver_id = _resolve_driver_id(authorization)
    with cursor() as cur:
        cur.execute(
            "select submitted_at, overall_pass from live.vehicle_inspections where driver_id = %s order by submitted_at desc limit 1",
            (driver_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {"submitted_at": None, "overall_pass": None}
    return {"submitted_at": row[0].isoformat(), "overall_pass": row[1]}


@app.get("/api/manager/driver-live-status")
def manager_driver_live_status():
    """Real user ask: the Drivers page's Status column always reads 'off_duty' -- true but
    misleading, since it's a completed simulation's TERMINAL snapshot (every driver ends their
    simulated day back at hub), not a live status. Returns, per driver, TODAY's real
    dispatch.duty_status_log entry (actually logged via Driver Assist) plus real load-acceptance
    counts (dispatch.day_orders.accepted_at) -- both genuinely live, unlike the simulation
    exhaust. Drivers with no Driver Assist account simply have no row here (honest gap, not a
    guessed status)."""
    today = date.today()
    with cursor() as cur:
        cur.execute("select id from dispatch.days where service_date = %s", (today,))
        day_row = cur.fetchone()
        if day_row is None:
            return {"drivers": {}}
        day_id = day_row[0]

        cur.execute(
            """select distinct on (driver_id) driver_id, status, logged_at
               from dispatch.duty_status_log where day_id = %s order by driver_id, logged_at desc""",
            (day_id,),
        )
        live_status = {r[0]: {"status": r[1], "logged_at": r[2].isoformat()} for r in cur.fetchall()}

        cur.execute(
            """select a.driver_id,
                      count(*) filter (where o.id is not null) as total,
                      count(*) filter (where o.accepted_at is not null) as accepted
               from dispatch.assignments a
               join lateral unnest(a.order_ids) as oid on true
               join dispatch.day_orders o on o.id = oid
               where a.day_id = %s
               group by a.driver_id""",
            (day_id,),
        )
        acceptance = {r[0]: {"total": r[1], "accepted": r[2]} for r in cur.fetchall()}

    driver_ids = set(live_status) | set(acceptance)
    return {
        "drivers": {
            str(d): {
                "live_status": live_status.get(d, {}).get("status"),
                "live_status_at": live_status.get(d, {}).get("logged_at"),
                "orders_accepted": acceptance.get(d, {}).get("accepted", 0),
                "orders_total": acceptance.get(d, {}).get("total", 0),
            }
            for d in driver_ids
        }
    }
