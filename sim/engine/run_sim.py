"""The discrete-event simulation loop -- research/roadstar_platform_plan.md Section 4.1.

Ties together everything already built: TripState (sim/engine/state.py) for the status sequence,
HOSLog/HOSState (hos.py) for legal feasibility, TruckMaintenanceState (maintenance.py) for
breakdown risk, compute_reward (reward.py) for scoring, and choose_assignment (policy.py) for
the epsilon-greedy decision -- driven by REAL calibrated distributions (calibration.*, seeded by
sim/build_calibration.py) rather than arbitrary guesses.

Two databases, per sim/db.py: fleet/calibration data is READ from remote Supabase once at
startup, held in memory for the run, and only sim EXHAUST (orders/assignments/events) is written,
in bulk, to local Postgres at the end of the run -- no per-event network round-trips.

## Real-vs-synthesized in this file, stated plainly

- **Real / calibrated**: order arrival timing (order_arrival_rate), which lane an order is on
  (lane_frequency), route distance/duration (lane_routes, OSRM), the run-type transition macro
  priors and dwell-time shape (TRANSITION_PRIORS, dwell_time_dist), the actual weight/pallets/
  load_type of each generated order (bootstrap-resampled from real historical_orders rows, not
  an assumed distribution shape), HOS limits (real Canadian regulation), the fleet size (131
  drivers, 131 trucks, from ground_truth).
- **Synthesized, labeled inline**: $ rates (config.py, unchanged), truck maintenance state
  (maintenance.py's existing docstring), each driver's small POOL of regular trucks (see below),
  each driver's synthesized prior-week cycle usage at sim start (seeded from the aggregated
  hos_remaining_at_completion figure, jittered -- a realistic *starting shape*, not a claim about
  any specific real driver's actual history), and the haversine+detour-factor fallback used only
  when a location pair isn't in the OSRM cache (calibration.lane_routes is hub-anchored + real
  lanes only -- see documents/logs/08_calibration_and_routing.md for that scope decision).

## Driver <-> truck: neither a fixed 1:1 pairing nor full 131x131 flexibility

Real data supports neither extreme: only 18/131 drivers have a known DEFAULT_PUNIT (a real,
specific truck), so most drivers have no fixed truck on record at all -- but that doesn't mean
every driver can use any of the 131 trucks either. Drivers and trucks are independent resources
here: each driver gets a small POOL of `TRUCK_POOL_SIZE` trucks (their real DEFAULT_PUNIT first,
if known, plus a synthesized fill -- pools overlap across drivers, matching how a real yard's
trucks get shared across shifts). At an order arrival, a driver is only a candidate if one of
their OWN pool trucks is BOTH currently available AND physically at the driver's current location
(a driver can't instantaneously teleport to a truck sitting somewhere else) -- among those, the
healthiest one (lowest breakdown risk) is picked. A driver can end up in a different one of their
pool trucks trip to trip; the pairing is never fixed for the whole run, and never a full cross of
every driver against every truck either.
"""
import argparse
import heapq
import itertools
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from psycopg2.extras import Json, execute_values

from sim.config import (
    ASSUMED_CAPACITY_VALUE_RATE_PER_LB, ASSUMED_CYCLE_STRANDING_PENALTY_CAD_MAX, ASSUMED_LTL_RATE_MULTIPLIER,
    ASSUMED_OPERATING_COST_PER_MILE, ASSUMED_PROMISE_BUFFER_HOURS, CAPACITY_BY_LOAD_TYPE,
    CAPACITY_PALLETS_BY_LOAD_TYPE, DISPATCH_DECISION_CUTOFF_HOURS, EPSILON_END, EPSILON_START,
    HOME_URGENCY_REPOSITION_THRESHOLD, HOS_MIN_DAILY_OFF_DUTY_HOURS, MAINTENANCE_DOWNTIME_DAYS,
    OSRM_BASE_URL, P_LTL_ORDER, linehaul_rate_per_mile,
)
from sim.db import cursor
from sim.engine.hos import DRIVING, OFF_DUTY, ON_DUTY_NOT_DRIVING, HOSLog
from sim.engine.maintenance import initialize_fleet as initialize_fleet_maintenance
from sim.engine.maintenance import sample_repair_hours
from sim.engine.policy import Candidate, choose_assignment, rank_candidates, zero_value_fn
from sim.engine.reward import (
    combined_home_urgency, compute_reward, late_delivery_penalty, load_fill_ratio, post_delivery_deadhead_cost,
    realized_breakdown_penalty,
)
from sim.engine.state import TRANSITION_PRIORS, TripState, TripStatus

AVG_FALLBACK_SPEED_KMH = 80.0   # SYNTHESIZED fallback only -- regional highway average
ROAD_DETOUR_FACTOR = 1.3        # SYNTHESIZED fallback only -- real roads aren't straight lines
GAMMA = 0.9                     # discount factor for the (currently-stub) value function
TOP_N_CANDIDATES_TO_LOG = 10    # candidate group size for learning-to-rank training, see sim/sql/023

# Pool sizes -- see module docstring's driver<->truck section. Checked directly against the raw
# Driver sheet (after cleaning the '<null>' string sentinel, Known Issue #8, which otherwise
# masks these as non-null): DEFAULT_PUNIT is real but only set for 18/131 drivers; the sibling
# column ASSIGNED_PUNIT is 100% empty (0/131) -- a dead column, not a per-trip truck record.
# Dispatch itself has NO truck/tractor identifier column at all, on any leg. There is no real
# per-trip truck-switching history anywhere in this export to compute a variance/rate feature
# from -- the ONLY real, derivable signal is this binary one (has a known default truck, or
# doesn't), so that's what's used: a known-default driver gets a REAL single-truck-user's pool
# (their actual truck + one synthesized backup for when it's down for service); everyone else
# gets the wider synthesized pool, since dispatch has no fixed record for them either.
KNOWN_DEFAULT_POOL_SIZE = 2
SYNTHESIZED_POOL_SIZE = 4


# --------------------------------------------------------------------------------------------
# Startup: load fleet + calibration data from remote Supabase, once, into memory
# --------------------------------------------------------------------------------------------

@dataclass
class SimData:
    locations: dict[int, tuple[float, float]]          # location_id -> (lat, lon)
    location_region: dict[int, int]                     # location_id -> region_id, see sim/cluster_locations.py
    region_centroids: dict[int, tuple[float, float]]    # region_id -> (centroid_lat, centroid_lon), for classifying arbitrary points (live)
    hub_ids: dict[str, int]                             # 'London' / 'Milton' -> location_id
    lane_weights: list[tuple[int, int, float]]          # (origin_id, dest_id, weight) for sampling
    lane_routes: dict[tuple[int, int], tuple[float, float]]  # (o,d) -> (distance_m, duration_s)
    order_arrival_rate: dict[tuple[int, int], float]    # (hour, dow) -> lambda
    dwell_minutes: dict[str, tuple[float, float, float]]  # 'pickup'/'delivery' -> (p25,median,p75)
    hos_median_remaining_hours: float
    order_pool: list[tuple[float, float, str]]          # bootstrap pool: (weight_lbs, pallets, load_type)
    driver_ids: list[int]
    driver_terminal_zone: dict[int, str | None]
    driver_home_hub: dict[int, int]  # driver_id -> location_id, calibration.driver_home_hub (sim/sql/045, sim/calibrate_driver_home_hub.py)
    truck_numbers: list[str]
    driver_default_truck: dict[int, str]  # real DEFAULT_PUNIT, 18/131 drivers -- ground_truth.driver_equipment
    origin_density: dict[int, float]      # location_id -> total real lane_frequency weight originating there
    lead_time_samples: list[float]        # real CREATED_TIME->ACTUAL_PICKUP hours, calibration.order_lead_time_hours


def load_sim_data() -> SimData:
    with cursor() as cur:
        # Every numeric value below comes back from psycopg2 as decimal.Decimal (the `numeric`
        # column type) -- cast to float immediately on load, since random.expovariate/triangular
        # and plain arithmetic elsewhere in this module don't mix float and Decimal.
        cur.execute("select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations")
        locations = {loc_id: (float(lat), float(lon)) for loc_id, lat, lon in cur.fetchall()}

        # region_id (sim/cluster_locations.py's k-means, k=30 chosen by silhouette score) --
        # replaces raw lat/lon as a model feature; see sim/sql/021 for why.
        cur.execute("select location_id, region_id from reference.locations")
        location_region = {loc_id: region_id for loc_id, region_id in cur.fetchall()}
        cur.execute("select region_id, centroid_lat, centroid_lon from calibration.region_clusters")
        region_centroids = {r: (float(lat), float(lon)) for r, lat, lon in cur.fetchall()}

        cur.execute("select location_id, label from reference.locations where label like 'RoadStar Terminal%'")
        hub_ids = {}
        for loc_id, label in cur.fetchall():
            if 'London' in label:
                hub_ids['London'] = loc_id
            elif 'Milton' in label:
                hub_ids['Milton'] = loc_id
            elif 'Barrie' in label:
                # Real 3rd hub (sim/sql/048_dispatch_board.sql) -- was only an ASSUMED
                # driver-home-hub anchor via a proxy customer location before this.
                hub_ids['Barrie'] = loc_id

        cur.execute("select origin_location_id, dest_location_id, weight from calibration.lane_frequency")
        lane_weights = [(o, d, float(w)) for o, d, w in cur.fetchall()]

        cur.execute("select origin_location_id, dest_location_id, distance_m, duration_s from calibration.lane_routes")
        lane_routes = {(o, d): (float(dist), float(dur)) for o, d, dist, dur in cur.fetchall()}

        cur.execute("select hour_of_day, day_of_week, lambda from calibration.order_arrival_rate")
        order_arrival_rate = {(h, dow): float(lam) for h, dow, lam in cur.fetchall()}

        # Aggregated (unweighted mean across run types) -- a live/simulated event doesn't know
        # its eventual retrospective run_type label ahead of time, so per-run_type dwell figures
        # (as stored) can't be conditioned on here; this collapses them to one shape per phase.
        cur.execute("select phase, avg(p25_minutes), avg(median_minutes), avg(p75_minutes) from calibration.dwell_time_dist group by phase")
        dwell_minutes = {phase: (float(p25), float(med), float(p75)) for phase, p25, med, p75 in cur.fetchall()}

        cur.execute("select avg(median_hours) from calibration.hos_remaining_at_completion")
        hos_median_remaining_hours = float(cur.fetchone()[0])

        cur.execute("select weight_lbs, pallets, load_type from ground_truth.historical_orders where weight_lbs is not null and pallets is not null and load_type is not null")
        order_pool = [(float(w), float(p), lt) for w, p, lt in cur.fetchall()]

        cur.execute("select driver_id, terminal_zone from ground_truth.drivers")
        driver_rows = cur.fetchall()
        driver_ids = [r[0] for r in driver_rows]
        driver_terminal_zone = dict(driver_rows)

        # Real user feedback: terminal_zone above is nearly blank (130/131 drivers report the
        # same generic company code) -- driver_home_hub_id() below reads THIS table instead, a
        # real historical-leg-derived anchor for the drivers who have one, a real-data-informed
        # ASSUMED one for the rest (sim/calibrate_driver_home_hub.py, sim/sql/045).
        cur.execute("select driver_id, hub_location_id from calibration.driver_home_hub")
        driver_home_hub = dict(cur.fetchall())

        cur.execute("select truck_number from ground_truth.trucks")
        truck_numbers = [r[0] for r in cur.fetchall()]

        cur.execute("select driver_id, truck_number from ground_truth.driver_equipment")
        driver_default_truck = dict(cur.fetchall())

        cur.execute("select lead_hours from calibration.order_lead_time_hours")
        lead_time_samples = [float(r[0]) for r in cur.fetchall()]

    # location_id -> total real lane_frequency weight ORIGINATING there -- "how much historical
    # demand typically starts from here." Feeds dest_local_order_density: a delivery ending up
    # somewhere with real reload demand nearby is a materially different situation than one that
    # doesn't, a sharper signal than raw hub-distance alone.
    origin_density: dict[int, float] = {}
    for origin_id, _dest_id, weight in lane_weights:
        origin_density[origin_id] = origin_density.get(origin_id, 0.0) + weight

    return SimData(
        locations=locations, location_region=location_region, region_centroids=region_centroids,
        hub_ids=hub_ids, lane_weights=lane_weights, lane_routes=lane_routes,
        order_arrival_rate=order_arrival_rate, dwell_minutes=dwell_minutes,
        hos_median_remaining_hours=hos_median_remaining_hours, order_pool=order_pool,
        driver_ids=driver_ids, driver_terminal_zone=driver_terminal_zone, driver_home_hub=driver_home_hub, truck_numbers=truck_numbers,
        driver_default_truck=driver_default_truck, origin_density=origin_density,
        lead_time_samples=lead_time_samples,
    )


def nearest_region(data: SimData, lat: float, lon: float) -> int:
    """Classifies an ARBITRARY point (not necessarily one of the 2,110 fixed locations -- e.g. a
    real live GPS position) into the nearest region by centroid distance. The 2,110 known
    locations already have a region_id looked up directly (data.location_region); this is for
    anything else.
    """
    return min(data.region_centroids, key=lambda r: _haversine_km((lat, lon), data.region_centroids[r]))


# --------------------------------------------------------------------------------------------
# Routing: OSRM cache first, haversine fallback second (labeled)
# --------------------------------------------------------------------------------------------

def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


_live_osrm_cache: dict[tuple[int, int], tuple[float, float] | None] = {}  # per-process, avoids re-querying the same pair


def _query_live_osrm(data: SimData, origin_id: int, dest_id: int) -> tuple[float, float] | None:
    """One live call to the self-hosted OSRM server (ops/osrm_setup.sh, OSRM_BASE_URL) for a pair
    outside calibration.lane_routes' pre-built cache. Needed now that candidates include
    in-transit drivers' PROJECTED landing spots (documents/logs/17) -- those are often a real
    delivery destination, not a hub, so the old hub-anchored cache (98.9% hit rate when every
    driver bounced back to a hub after any real deadhead) would otherwise miss much more often.
    Returns None on any failure (server down, bad response) -- caller falls back to haversine.
    """
    import requests
    o_lat, o_lon = data.locations[origin_id]
    d_lat, d_lon = data.locations[dest_id]
    url = f"{OSRM_BASE_URL}/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}?overview=false"
    try:
        resp = requests.get(url, timeout=2)
        result = resp.json()
        if result.get('code') != 'Ok':
            return None
        route = result['routes'][0]
        return route['distance'], route['duration']  # meters, seconds -- same units as lane_routes
    except Exception:
        return None


def get_route(data: SimData, origin_id: int, dest_id: int) -> tuple[float, float]:
    """Returns (distance_miles, duration_hours). Real OSRM figure if cached OR reachable live;
    haversine x detour-factor only as a last-resort SYNTHESIZED fallback if both miss (server
    unreachable, or a location outside the road network) -- see the module docstring.
    """
    if origin_id == dest_id:
        return 0.0, 0.0
    cached = data.lane_routes.get((origin_id, dest_id))
    if cached:
        distance_m, duration_s = cached
        return distance_m / 1609.34, duration_s / 3600

    pair = (origin_id, dest_id)
    if pair not in _live_osrm_cache:
        _live_osrm_cache[pair] = _query_live_osrm(data, origin_id, dest_id)
    live = _live_osrm_cache[pair]
    if live:
        distance_m, duration_s = live
        return distance_m / 1609.34, duration_s / 3600

    km = _haversine_km(data.locations[origin_id], data.locations[dest_id]) * ROAD_DETOUR_FACTOR
    return km / 1.60934, km / AVG_FALLBACK_SPEED_KMH


def driver_home_hub_id(data: SimData, driver_id: int) -> int:
    """This driver's OWN home-base location_id -- NOT the nearest-any-hub used elsewhere
    (Order.dest_distance_to_hub_km, dynamic_post_completion_probs) -- see
    documents/logs/23_home_base_return_gap_found.md: a Milton driver landing near London hub is
    not "home." Factored out of initialize_fleet()'s own placement logic so the home-base-return
    reward-shaping features (candidate-building loop) use the EXACT same driver->hub mapping the
    sim used to place this driver at simulation start, not a second, possibly-drifting copy.

    Real user feedback + real gap found directly: ground_truth.drivers.terminal_zone is nearly
    blank (130/131 drivers report the same generic company code, not a real per-driver location)
    -- the OLD version of this function fell back on it, collapsing the entire fleet onto ONE home
    hub (London). Reads calibration.driver_home_hub instead (sim/calibrate_driver_home_hub.py,
    sim/sql/045): a real historical-leg-derived anchor for the drivers who have enough real trip
    history to infer one from, a real-data-informed ASSUMED one (from 4 real anchor points --
    London/Milton's real terminals plus Barrie/Niagara Falls, the brief's other 2 coverage cities,
    via a real customer location in each) for the rest. Falls back to the London hub only if a
    driver is somehow missing from that table entirely (should not happen -- every real driver_id
    got a row -- but never silently KeyErrors a live scoring call over it).
    """
    hub_id = data.driver_home_hub.get(driver_id)
    return hub_id if hub_id is not None else data.hub_ids['London']


EXTENDED_DWELL_PROBABILITY = 0.015  # SYNTHESIZED -- see sample_dwell_hours(). ~5-8 real cases in
# a typical week-long showcase run (a few hundred dwell draws) -- high enough that detention
# billing genuinely gets exercised, low enough not to cascade into system-wide lateness (checked
# directly: 4% caused ~1/4 of all deliveries to run late through knock-on driver-schedule delay,
# which swamped the "well and on-time" story this event was meant to add color to, not dominate).
EXTENDED_DWELL_HOURS_RANGE = (2.5, 3.5)  # SYNTHESIZED -- see sample_dwell_hours()


def sample_dwell_hours(data: SimData, phase: str, rng: random.Random) -> float:
    """Triangular distribution over the real p25/median/p75 -- a lightweight empirical-CDF proxy,
    not a claim that dwell times are exactly triangular; better than a bare median with no spread.

    A triangular(low=p25, high=p75, mode=median) draw is mathematically CAPPED at p75 -- and this
    network's real calibrated p75 (~45 min pickup, ~36 min delivery, checked directly) never
    reaches the 2-hour detention-free threshold, so detention billing was structurally
    unreachable, live or simulated, regardless of how many trips run. Real user feedback: show it
    actually happening. Same treatment as the existing SLOWDOWN mechanic (telemetry_simulator.py)
    and truck breakdown sampling -- a rare, clearly SYNTHESIZED "dock ran long" edge case (lumper
    backup, paperwork delay, congestion), not a claim this network's real dwell times are usually
    this long, and not picking which specific trip it happens to -- still a random draw.
    """
    if rng.random() < EXTENDED_DWELL_PROBABILITY:
        return rng.uniform(*EXTENDED_DWELL_HOURS_RANGE)
    p25, median, p75 = data.dwell_minutes[phase]
    return rng.triangular(p25, p75, median) / 60


HUB_PROXIMITY_KM = 5.0  # a delivery this close to a hub is treated as landing AT the hub
HUB_DEADHEAD_SHARE = 0.379  # real, weighted (see docstring below) -- not a guess


def dynamic_post_completion_probs(dist_to_hub_km: float) -> tuple[float, float, float]:
    """(p_reload_immediately, p_real_deadhead, p_dromt), summing to 1.

    REVISED (documents/logs/16_reward_realism_pass.md) after directly testing the original
    premise against real data. That premise -- deadhead risk ramps UP with distance from the
    nearest hub -- was checked against real per-city outcomes (17 real Ontario cities, matched to
    actual geocoded distance-to-hub, using the same `Empty run to pickup`/`Empty run after
    delivery` classification the rest of this project uses) and found EMPIRICALLY BACKWARDS at
    the one point that matters most: the two real hub cities themselves (distance ~0km) have the
    HIGHEST deadhead share in the whole dataset -- Milton 37.0% (2,397 real legs), London 50.6%
    (164 legs), both far above the 27.3% network-wide average -- not the lowest, because a hub is
    a company terminal, not a real shipper location: landing there means driving back OUT to find
    real freight, the opposite of what the old linear ramp assumed.

    Beyond the hub effect, neither distance-to-hub NOR local order density (also tested directly
    against the same 17 real cities) showed a clean, confident relationship with deadhead share --
    correlations were weak and inconsistent in sign (GTA suburbs close to Milton like Vaughan/
    Woodbridge/Mississauga ran HIGHER than the much farther, much higher-volume Oshawa/Whitby
    corridor). Fitting a smooth curve to that would be fitting noise, not signal, and dressing it
    up as "grounded in real data" would be worse than the flat prior it replaced. So: one real,
    well-replicated effect (hub adjacency), encoded directly; everywhere else, the honest answer
    is the real network-wide base rate (TRANSITION_PRIORS['p_real_deadhead']), not a fabricated
    curve.
    """
    p_deadhead = HUB_DEADHEAD_SHARE if dist_to_hub_km <= HUB_PROXIMITY_KM else TRANSITION_PRIORS['p_real_deadhead']

    orig_reload = TRANSITION_PRIORS['p_reload_immediately']
    orig_dromt = 1 - orig_reload - TRANSITION_PRIORS['p_real_deadhead']
    reload_share = orig_reload / (orig_reload + orig_dromt)

    remaining = 1 - p_deadhead
    return remaining * reload_share, p_deadhead, remaining * (1 - reload_share)


def repositioning_outcome(
    current_location_id: int, home_hub_id: int, nearest_hub_id: int, dist_to_hub_km: float,
    remaining_cycle_hours: float, hours_since_home: float | None, hours_to_home: float, rng: random.Random,
) -> tuple[str, int]:
    """The real "what does this driver/truck do next" decision, shared by TWO real call sites
    (documents/logs/25-26) -- ONE function, not a second implementation for each:

    1. `run_assignment()`'s immediate post-delivery moment (right after every trip).
    2. The later `IDLE_TIMEOUT` re-check (`run_simulation()`'s event loop) -- re-run with FRESH
       real current inputs if a driver is STILL uncommitted a real wait later, not a separate
       hardcoded "give up and go home" branch. Real user correction: a real dispatcher's default
       (absent an actual reason) is to reposition toward FREIGHT OPPORTUNITY (the nearest hub),
       not straight to a driver's personal home base -- only once real urgency (legal cycle margin,
       or a genuine business "been too long since home" cadence) actually justifies it does the
       target become home specifically, and even then it's a smoothly rising bias, not a hard
       switch -- see `combined_home_urgency()`.

    Returns (outcome, target_location_id): outcome is 'reload' (stays exactly where they are,
    ready), 'reposition' (drives to target_location_id -- the nearest hub, or home once urgent),
    or 'dromt' (drops trailer, stays local). Caller applies whatever real bookkeeping (TripStatus
    transitions for a live trip, or direct HOS-log/location mutation for an idle re-check) its own
    context needs -- this function only ever decides WHICH of the three, never executes it.
    """
    p_reload, p_deadhead, p_dromt = dynamic_post_completion_probs(dist_to_hub_km)
    target_hub_id = nearest_hub_id
    if current_location_id != home_hub_id and hours_since_home is not None:
        urgency = combined_home_urgency(remaining_cycle_hours, hours_since_home, hours_to_home)
        if urgency >= HOME_URGENCY_REPOSITION_THRESHOLD:
            target_hub_id = home_hub_id
            # Blend p_deadhead up toward 1 as urgency approaches 1, shrinking p_reload/p_dromt
            # proportionally -- always sums to 1, and low-urgency behavior (the vast majority of
            # cases) is completely unchanged since this whole block only runs above threshold.
            p_deadhead = p_deadhead + urgency * (1 - p_deadhead)
            remaining_prob = 1 - p_deadhead
            other_total = p_reload + p_dromt
            if other_total > 0:
                p_reload = p_reload / other_total * remaining_prob
                p_dromt = p_dromt / other_total * remaining_prob
            else:
                p_reload, p_dromt = remaining_prob, 0.0

    roll = rng.random()
    if roll < p_reload:
        return 'reload', current_location_id
    elif roll < p_reload + p_deadhead:
        return 'reposition', target_hub_id
    else:
        return 'dromt', current_location_id


# --------------------------------------------------------------------------------------------
# Order generation
# --------------------------------------------------------------------------------------------

@dataclass
class Order:
    order_id: uuid.UUID
    origin_location_id: int
    dest_location_id: int
    created_at: datetime
    weight_lbs: float
    pallets: float
    load_type: str
    loaded_miles: float
    loaded_hours: float
    service_type: str              # 'FTL' or 'LTL' -- P_LTL_ORDER, real (see sim/config.py)
    dest_distance_to_hub_km: float  # drives both the post-completion deadhead probability and a training feature
    dest_local_order_density: float  # real lane_frequency weight originating near the destination -- reload-likelihood signal
    requested_pickup_at: datetime    # REAL lead time from created_at -- calibration.order_lead_time_hours
    decision_time: datetime          # when dispatch actually has to decide -- see DISPATCH_DECISION_CUTOFF_HOURS
    promised_delivery_at: datetime   # SYNTHESIZED quoted delivery time -- see ASSUMED_PROMISE_BUFFER_HOURS, now anchored to requested_pickup_at


def generate_order(data: SimData, at: datetime, rng: random.Random) -> Order:
    origins, dests, weights = zip(*data.lane_weights)
    idx = rng.choices(range(len(weights)), weights=weights, k=1)[0]
    origin_id, dest_id = origins[idx], dests[idx]
    weight_lbs, pallets, load_type = rng.choice(data.order_pool)  # bootstrap a real order's cargo profile
    loaded_miles, loaded_hours = get_route(data, origin_id, dest_id)
    service_type = 'LTL' if rng.random() < P_LTL_ORDER else 'FTL'
    dest_distance_to_hub_km = min(
        _haversine_km(data.locations[dest_id], data.locations[hub_id]) for hub_id in data.hub_ids.values()
    )
    dest_local_order_density = data.origin_density.get(dest_id, 0.0)

    # REAL lead time (documents/logs/17) -- bootstrap-sampled from calibration.order_lead_time_hours,
    # same treatment as order_pool's weight/pallets/load_type. Falls back to 0 (dispatch-on-arrival)
    # only if the sample table is somehow empty, so a missing calibration table degrades gracefully
    # rather than crashing.
    lead_hours = rng.choice(data.lead_time_samples) if data.lead_time_samples else 0.0
    requested_pickup_at = at + timedelta(hours=lead_hours)
    # The dispatch decision fires at the LATER of "right now" and "24h before pickup" -- an order
    # booked far ahead just sits on the books until its cutoff; one booked with under 24h notice
    # (a real ~25% of them) gets decided immediately, same as before this was added. See
    # DISPATCH_DECISION_CUTOFF_HOURS's sourcing in sim/config.py.
    decision_time = max(at, requested_pickup_at - timedelta(hours=DISPATCH_DECISION_CUTOFF_HOURS))

    # Promise = typical drive + typical dwell (calibrated medians, driver-independent) + a
    # synthesized buffer covering time-to-dispatch and normal slack -- anchored to the REQUESTED
    # pickup time now, not booking time (a real delivery promise is "we'll pick up around X,
    # deliver by X+transit+buffer", not counted from when the customer first called).
    median_pickup_dwell_h = data.dwell_minutes['pickup'][1] / 60
    median_delivery_dwell_h = data.dwell_minutes['delivery'][1] / 60
    promised_delivery_at = requested_pickup_at + timedelta(
        hours=loaded_hours + median_pickup_dwell_h + median_delivery_dwell_h + ASSUMED_PROMISE_BUFFER_HOURS
    )
    return Order(
        order_id=uuid.uuid4(), origin_location_id=origin_id, dest_location_id=dest_id,
        created_at=at, weight_lbs=float(weight_lbs), pallets=float(pallets), load_type=load_type,
        loaded_miles=loaded_miles, loaded_hours=loaded_hours, service_type=service_type,
        dest_distance_to_hub_km=dest_distance_to_hub_km, dest_local_order_density=dest_local_order_density,
        requested_pickup_at=requested_pickup_at, decision_time=decision_time,
        promised_delivery_at=promised_delivery_at,
    )


def next_order_arrival(data: SimData, now: datetime, rng: random.Random) -> datetime:
    """Poisson process with an hour x day-of-week-varying rate: draw an exponential inter-arrival
    time using THIS hour's lambda (a standard, defensible approximation for a slowly-varying
    rate -- re-evaluated every draw, so the rate still tracks the calibrated hourly/daily shape).
    """
    lam = data.order_arrival_rate.get((now.hour, now.weekday()), 0.1)
    lam = max(lam, 0.01)  # avoid a divide-by-zero / infinite wait on a genuinely silent cell
    return now + timedelta(hours=rng.expovariate(lam))


# --------------------------------------------------------------------------------------------
# Fleet state
# --------------------------------------------------------------------------------------------

@dataclass
class DriverState:
    driver_id: int
    hos_log: HOSLog
    location_id: int
    # NOTE: no `available` flag -- a driver is never excluded from candidate-building outright.
    # effective_driver_state() (below) is the single source of truth for whether/how a driver can
    # be considered: idle drivers use their real current location at `now`; in-transit drivers use
    # their PROJECTED landing state via the commitment fields below. This is the documents/logs/17
    # change -- mid-route drivers used to be filtered out entirely (`if not drv.available: continue`)
    # before candidate-scoring ever ran; now they're real, scoreable candidates.
    #
    # In-transit commitment (documents/logs/17) -- set when this driver is dispatched, so a NEW
    # order's candidate list can consider them via their PROJECTED landing state instead of
    # excluding them outright. `committed_until` is the real, already-fully-determined completion
    # time of their current trip (the whole trip's timeline is walked synchronously at dispatch,
    # so this is known exactly, not guessed); `committed_location_id`/`committed_truck_number`
    # are where they land and which truck comes with them. None means no active commitment.
    committed_until: datetime | None = None
    committed_location_id: int | None = None
    committed_truck_number: str | None = None
    # Home-time retarget (documents/logs/25): the real last moment this driver was AT their home
    # hub -- updated synchronously in the TRIP_COMPLETE handler below, so it's always correct as
    # of `now` by the time any DISPATCH_DECISION reads it (the sim's strictly causal event loop
    # means no chain-walk/projection is needed here, unlike live's project_driver_state() -- see
    # that function's own docstring for why live genuinely needs one and this doesn't).
    last_home_at: datetime | None = None


@dataclass
class TruckState:
    truck_number: str
    location_id: int
    available: bool = True
    # documents/logs/19: a truck sent for PROACTIVE maintenance is out of the pool for
    # MAINTENANCE_DOWNTIME_DAYS -- tracked separately from `available`/driver commitment because
    # the DRIVER isn't stuck at the shop (they're free to take their next trip with a different
    # pool truck); only this specific truck is unavailable until `maintenance_until`.
    maintenance_until: datetime | None = None


@dataclass
class Fleet:
    drivers: dict[int, DriverState]
    trucks: dict[str, TruckState]
    truck_maint: dict[str, 'TruckMaintenanceState']
    truck_pool: dict[int, list[str]]  # driver_id -> their small set of regular trucks


def apply_idle_reset(hos_log: HOSLog, now: datetime) -> None:
    """A driver sitting AVAILABLE with no assignment is off-duty, not idling on some invisible
    on-duty clock -- this is what lets a driver's daily/weekly limits actually reset over the
    course of a run, instead of the sim only ever logging ONE reset at the very start (which
    would silently make every driver permanently HOS-infeasible after the first ~16 hours of
    simulated time, since nothing else in this design would ever grant them a fresh one). Only
    logged when the idle gap reaches the real qualifying threshold (10h) -- a shorter gap isn't a
    legal reset and is correctly left unlogged (it doesn't count as on-duty time either way).
    Must run for every candidate BEFORE hos_log.snapshot(now), not only for the one eventually
    chosen -- otherwise the feasibility check itself would score off a stale, artificially-worse
    HOS state.
    """
    if not hos_log.intervals:
        return
    last_end = hos_log.intervals[-1].end
    gap_hours = (now - last_end).total_seconds() / 3600
    if gap_hours >= HOS_MIN_DAILY_OFF_DUTY_HOURS:
        hos_log.add(last_end, now, OFF_DUTY)


def epsilon_schedule(now: datetime, sim_start: datetime, sim_end: datetime, eps_start: float, eps_end: float) -> float:
    """Linear decay across SIMULATED elapsed time (not per-decision-count): high exploration
    early (nothing trusted yet), decaying toward mostly-exploit by the end of the run -- standard
    Ξ΅-greedy/UCB practice. Applied along calendar time because one run is now a single continuous
    multi-month history (documents/logs/17), not repeated short episodes, so "early" and "late"
    mean points in that one history, not separate runs.
    """
    total = (sim_end - sim_start).total_seconds()
    if total <= 0:
        return eps_end
    frac = min(max((now - sim_start).total_seconds() / total, 0.0), 1.0)
    return eps_start + frac * (eps_end - eps_start)


def effective_driver_state(fleet: 'Fleet', driver_id: int, now: datetime) -> tuple[int, str | None, datetime]:
    """Where this driver effectively IS, which truck comes with them, and the earliest a NEW
    trip could actually start for them -- as of `now`. Uniformly correct for both cases:

    - No active commitment (idle, or their prior commitment already ended by `now`) -> their real
      current location, real current truck-pool choice (resolved by the caller via
      pick_pool_truck, not here), and a start time of `now` itself.
    - Still committed past `now` (mid-route) -> their PROJECTED landing spot and the truck already
      on that trip with them (a driver doesn't swap trucks mid-route -- see module docstring), and
      a start time of `committed_until` (they genuinely can't begin a new trip before that,
      however early `now` is) -- this is what makes a bad "won't even finish in time" pick
      naturally show up as a late pickup later, not a hidden inconsistency.

    Does NOT mutate anything -- pure projection for scoring. The caller applies apply_idle_reset
    and takes the HOS snapshot at the returned `effective_start`, exactly the same call whether
    the driver was idle or in-transit.
    """
    if fleet.drivers[driver_id].committed_until is not None and fleet.drivers[driver_id].committed_until > now:
        d = fleet.drivers[driver_id]
        return d.committed_location_id, d.committed_truck_number, d.committed_until
    return fleet.drivers[driver_id].location_id, None, now


def build_truck_pools(data: SimData, rng: random.Random) -> dict[int, list[str]]:
    """Each driver's small set of regular trucks -- see the KNOWN_DEFAULT_POOL_SIZE /
    SYNTHESIZED_POOL_SIZE constants above for why the two groups get different (real-vs-
    synthesized) treatment. The real DEFAULT_PUNIT is always included first when known; the rest
    of the pool is a synthesized random fill from the full fleet, so pools naturally overlap
    across drivers (multiple drivers can share a truck across different shifts, matching a real
    yard) -- that overlap is itself synthesized, since no data says whether or how trucks get
    shared among the 113 drivers with no fixed record.
    """
    pools = {}
    for driver_id in data.driver_ids:
        pool = []
        default = data.driver_default_truck.get(driver_id)
        target_size = KNOWN_DEFAULT_POOL_SIZE if default else SYNTHESIZED_POOL_SIZE
        if default:
            pool.append(default)
        fill_candidates = [t for t in data.truck_numbers if t not in pool]
        pool += rng.sample(fill_candidates, k=min(target_size - len(pool), len(fill_candidates)))
        pools[driver_id] = pool
    return pools


def initialize_fleet(sim_data: SimData, rng: random.Random, sim_start: datetime) -> Fleet:
    truck_maint = initialize_fleet_maintenance(sim_data.truck_numbers, rng)
    truck_pool = build_truck_pools(sim_data, rng)

    drivers = {}
    for driver_id in sim_data.driver_ids:
        location_id = driver_home_hub_id(sim_data, driver_id)

        hos_log = HOSLog(driver_id=driver_id)
        # Synthesized starting cycle usage, shaped by the calibrated median -- see module
        # docstring. A qualifying reset ends right at sim start (every driver begins fully rested
        # on the DAILY clock); the WEEKLY cycle carries some already-used hours so cycle limits
        # can plausibly bind from early in the run, not just after days of simulated driving.
        used_cycle_hours = max(0.0, rng.gauss(70 - sim_data.hos_median_remaining_hours, 5))
        prior_start = sim_start - timedelta(days=6, hours=12)
        hos_log.add(prior_start, prior_start + timedelta(hours=used_cycle_hours), ON_DUTY_NOT_DRIVING)
        hos_log.add(sim_start - timedelta(hours=10), sim_start, OFF_DUTY)  # the qualifying daily reset

        # Every driver starts AT their home hub (location_id above) -- last_home_at = sim_start is
        # simply the truth, not an assumption (documents/logs/25).
        drivers[driver_id] = DriverState(driver_id=driver_id, hos_log=hos_log, location_id=location_id, last_home_at=sim_start)

    # Trucks need a starting location too. Placed at whichever driver's hub first claims them
    # from their pool -- guarantees every driver starts with at least one co-located, available
    # pool truck (their own hub), while a truck shared across multiple drivers' pools still only
    # gets placed once. Any truck nobody's pool reached (pools are a small sample, not a
    # partition) starts at a random hub instead.
    trucks: dict[str, TruckState] = {}
    for driver_id in sim_data.driver_ids:
        driver_hub = drivers[driver_id].location_id
        for truck_number in truck_pool[driver_id]:
            if truck_number not in trucks:
                trucks[truck_number] = TruckState(truck_number=truck_number, location_id=driver_hub)
    for truck_number in sim_data.truck_numbers:
        if truck_number not in trucks:
            trucks[truck_number] = TruckState(truck_number=truck_number, location_id=rng.choice(list(sim_data.hub_ids.values())))

    return Fleet(drivers=drivers, trucks=trucks, truck_maint=truck_maint, truck_pool=truck_pool)


# --------------------------------------------------------------------------------------------
# One assignment's full status walk -- mirrors the exact sequences validated in test_state.py
# --------------------------------------------------------------------------------------------

@dataclass
class CompletedTrip:
    trip_id: uuid.UUID
    order: Order
    driver_id: int
    truck_number: str
    assigned_at: datetime
    completed_at: datetime
    trip_state: TripState
    order_revenue: float
    immediate_reward: float  # reward.total at the moment of assignment, before post-trip resolution
    reward_total: float      # fully resolved: immediate_reward minus realized post-delivery-deadhead/breakdown costs
    deadhead_miles: float    # pre-pickup deadhead distance -- miles, NOT the dollar cost
    deadhead_cost: float     # the $ figure (deadhead_miles x ASSUMED_OPERATING_COST_PER_MILE), separate on purpose
    load_fill_ratio: float
    opportunity_cost_penalty: float
    was_exploration: bool
    driver_location_id: int  # decision-time state, for extract_transitions.py -- see sim/sql/014
    driver_hos_remaining: float       # the BINDING (smallest) limit -- see HOSState's own docstring
    # Real user feedback: "we are using both daily continuous limit and weekly limit also right?
    # Can we show this as another layer" -- HOSState already tracks all four distinct real limits
    # (daily driving/duty windows + 7-day/14-day cycles); driver_hos_remaining above is only ever
    # their MIN. These carry the individual figures through for display (e.g. the Simulation
    # Showcase's Active Drivers panel), not fabricated from the aggregate.
    driver_hos_driving_remaining: float          # daily driving-hours window
    driver_hos_duty_remaining: float             # daily on-duty window
    driver_hos_cycle1_remaining: float           # 7-day cycle
    driver_hos_cycle2_remaining: float           # 14-day cycle
    truck_breakdown_risk: float       # decision-time state, see sim/sql/015
    planned_driving_hours: float      # decision-time ESTIMATE, see sim/sql/016 -- known at scoring time, live-compatible
    planned_duty_hours: float
    lateness_penalty: float           # realized -- see sim/sql/017
    total_committed_distance_miles: float  # deadhead + loaded -- how far this decision ties up the driver/truck
    driver_pool_size: int             # 2 (real default truck) or 4 (synthesized) -- see module docstring
    truck_pct_km_interval: float      # raw odometer %, not just the derived breakdown_risk
    truck_pct_days_interval: float    # raw calendar %, the new time-based maintenance dimension
    # Home-base-return retarget (documents/logs/23) -- decision-time distance/reward-shaping state.
    # distance_to_home_miles_landing doubles as this transition's NEXT-state distance-to-home
    # feature for V(s) (it's the position after THIS trip, same role next_location_id/next_hos_*
    # below already play) -- no separate "next_distance_to_home" field needed.
    distance_to_home_miles: float = 0.0
    distance_to_home_miles_landing: float = 0.0
    home_progress_bonus: float = 0.0
    cycle_end_stranding_penalty: float = 0.0
    # Home-time retarget (documents/logs/25-26): the business-cadence companion to
    # distance_to_home_miles above, persisted so V(s) can see the EXACT signal
    # combined_home_urgency()'s business half is shaped around, not just its indirect effect on
    # realized rewards (a real gap this closes -- see documents/logs/26's own "model can't see the
    # signal" finding). hours_since_home_landing mirrors reward.py's compute_reward() internal
    # estimate exactly (0.0 once landing AT home, else hours_since_home + planned_duty_hours) --
    # same reasoning distance_to_home_miles_landing already establishes for its own next-state role.
    hours_since_home: float | None = None
    hours_since_home_landing: float | None = None
    # Home-time retarget, the real "don't leave a driver idle" event (documents/logs/25-26): set
    # by the IDLE_TIMEOUT handler below, retroactively, on whichever CompletedTrip left this
    # driver in the idle state that led to it -- a REAL, priced consequence of THIS decision, not
    # a soft shaping term, so Fitted Value Iteration sees it as a real cost in the training data.
    idle_then_drove_home_miles: float = 0.0        # real empty miles actually driven home after sitting idle too long
    idle_then_stranded_penalty: float = 0.0         # charged instead, when they legally couldn't make the full drive (see IDLE_TIMEOUT handler)
    # Next-state, for Fitted Value Iteration -- set by the caller (run_simulation()) after this
    # trip's HOS interval is logged, not here (this function doesn't have the updated hos_log).
    next_location_id: int | None = None
    next_hos_remaining: float | None = None
    next_hos_driving_remaining: float | None = None
    next_hos_duty_remaining: float | None = None
    next_hos_cycle1_remaining: float | None = None
    next_hos_cycle2_remaining: float | None = None
    next_available_at: datetime | None = None
    # Post-trip truck-maintenance state -- unlike order-specific fields (distance, destination),
    # truck condition DOES travel forward with the driver into their next state: the truck isn't
    # swapped mid-trip (see module docstring's driver<->truck section), so "how overdue is the
    # truck this driver will show up with" is a real property of where they'll be next, not
    # something tied to a not-yet-known future order. Missing from V(s) until now -- a real gap,
    # not a design necessity (documents/logs/18).
    next_truck_pct_km_interval: float | None = None
    next_truck_pct_days_interval: float | None = None


def run_assignment(
    data: SimData, order: Order, candidate: Candidate, driver_id: int, truck_number: str,
    driver_location_id: int, driver_pool_size: int, truck_state, reward, was_exploration: bool,
    now: datetime, rng: random.Random,
) -> tuple[CompletedTrip, 'TruckMaintenanceState', int, datetime]:
    """Walks one TripState through its full real sequence, sampling breakdown and secondary-
    pickup events from the calibrated priors, and returns the completed trip, the truck's
    post-trip maintenance state, where the driver+truck physically end up, and when they're both
    free again (driving_end -- may be later than the trip's own COMPLETE timestamp if a
    post-completion deadhead leg follows).
    """
    trip_id = uuid.uuid4()
    trip = TripState(trip_id=str(trip_id), driver_id=driver_id)
    t = now

    trip.transition(TripStatus.ASSGN, t)

    # Pre-pickup deadhead (empty), with a chance of a real mid-route breakdown -- BREAKDOWN is
    # only reachable from DISP (sim/engine/state.py's VALID_TRANSITIONS), matching this leg.
    deadhead_miles, deadhead_hours = candidate.pre_pickup_deadhead_miles, candidate.pre_pickup_deadhead_hours
    # Depart just in time to arrive at the REQUESTED pickup time (with a small early buffer, not
    # exact-second precision -- a real driver doesn't cut it that fine), not the instant the
    # decision fires (documents/logs/17) -- a real dispatcher doesn't send a truck out a day early
    # just because an order was booked with lead time. If there's more lead time than the deadhead
    # itself takes, the driver waits (assigned/committed, but not yet moving) until it's time to
    # leave. If there's LESS lead time than the deadhead needs (order decided late, or driver far
    # away), departure can't happen before `now` either way -- they leave immediately and may
    # genuinely arrive late, which is exactly what the lateness penalty is there to catch.
    #
    # SYNTHESIZED buffer -- no real "how early do drivers actually leave" data exists to calibrate
    # against (same caveat as the rest of this module's synthesized timing assumptions). 15-45 min
    # early, uniform: covers ordinary real-world slack (a few minutes to hook up, normal traffic
    # margin) without being a second dispatch-buffer stacked on top of ASSUMED_PROMISE_BUFFER_HOURS
    # (that one pads the PROMISE itself; this one is about actual departure-time variance).
    target_eta = order.requested_pickup_at - timedelta(minutes=rng.uniform(15, 45))
    departure_time = max(t, target_eta - timedelta(hours=deadhead_hours))
    t = departure_time
    trip.transition(TripStatus.DISP, t, loaded=False, distance_miles=deadhead_miles)
    t += timedelta(hours=deadhead_hours)
    if truck_state.sample_breakdown(rng):
        trip.transition(TripStatus.BREAKDOWN, t)
        t += timedelta(hours=sample_repair_hours(rng))
        trip.transition(TripStatus.DISP, t, loaded=False, distance_miles=0.0)  # resume, same leg
    trip.transition(TripStatus.ARRSHIP, t)

    if rng.random() < TRANSITION_PRIORS['p_spotted_not_docked']:
        trip.transition(TripStatus.SPTLD, t)
    else:
        trip.transition(TripStatus.DOCKED, t)
    t += timedelta(hours=sample_dwell_hours(data, 'pickup', rng))
    trip.transition(TripStatus.PICKD, t, loaded=True, weight_lbs=order.weight_lbs)
    trip.transition(TripStatus.DEPSHIP, t, loaded=True)

    # Secondary pickup: gated by order.service_type=='LTL', not a flat coin flip -- real data
    # shows LTL/multi-stop trips ARE consolidated trips by definition (334/334 in
    # ground_truth.historical_legs), so an LTL order deterministically gets a second pickup here,
    # modeled as a short nearby detour rather than a full second OSRM lookup (calibration doesn't
    # track a distinct real lane for "wherever the 2nd pickup was"). This is where the "secondary
    # pickup for more revenue" mechanic actually pays off: the second shipment's own LTL revenue
    # is added to the trip's total below, on top of the primary shipment's.
    secondary_pickup_revenue = 0.0
    if order.service_type == 'LTL':
        trip.transition(TripStatus.STOPOFF, t, loaded=True)
        t += timedelta(minutes=rng.uniform(15, 30))
        trip.transition(TripStatus.ARRSHIP, t, loaded=True)
        trip.transition(TripStatus.SPTLD, t, loaded=True)
        second_weight, second_pallets, second_load_type = rng.choice(data.order_pool)
        t += timedelta(hours=sample_dwell_hours(data, 'pickup', rng))
        trip.transition(TripStatus.PICKD, t, loaded=True, weight_lbs=float(second_weight))
        trip.transition(TripStatus.DEPSHIP, t, loaded=True)
        second_fill = load_fill_ratio(
            float(second_weight), float(second_pallets),
            CAPACITY_BY_LOAD_TYPE.get(second_load_type, 44500), CAPACITY_PALLETS_BY_LOAD_TYPE.get(second_load_type, 26),
        )
        secondary_pickup_revenue = order.loaded_miles * linehaul_rate_per_mile(order.loaded_miles) * ASSUMED_LTL_RATE_MULTIPLIER * second_fill

    t += timedelta(hours=order.loaded_hours)
    trip.transition(TripStatus.ARRCONS, t, loaded=True)
    trip.transition(TripStatus.DOCKED, t, loaded=True)
    t += timedelta(hours=sample_dwell_hours(data, 'delivery', rng))
    trip.transition(TripStatus.COMPLETE, t)
    completed_at = t

    # Post-completion branch: reload immediately (no deadhead -- rig just stays put, ready for
    # the next order at this location), a real empty repositioning back toward the nearest hub
    # (the actual revenue-loss signal), or a short/no-op empty leg. p_real_deadhead now SCALES
    # with distance from the nearest hub (see dynamic_post_completion_probs) instead of being a
    # flat, state-independent coin flip -- a delivery far from any hub is genuinely more likely
    # to leave a truck stranded with nothing nearby to reload, and a flat probability gave a
    # value function nothing learnable to anticipate here (see documents/logs/12 for the finding
    # that motivated this: training on the fixed-probability version produced R^2 ~ 0).
    nearest_hub_id = min(
        data.hub_ids.values(),
        key=lambda hid: _haversine_km(data.locations[order.dest_location_id], data.locations[hid]),
    )

    # Home-time retarget (documents/logs/25-26): the real, costed simulated event this whole
    # mechanism exists to add, decided by the SAME `repositioning_outcome()` used by the
    # IDLE_TIMEOUT re-check below (one real decision function, not two parallel implementations).
    # When this driver's combined home urgency (legal cycle margin OR business days-since-home,
    # see reward.py's combined_home_urgency()) is high, the reposition target becomes their OWN
    # home hub, not the generic nearest one, and they become MORE likely to reposition at all --
    # blended in smoothly via urgency, not a hard switch, so training data shows the real range
    # from "no pull" to "strongly pulled home" rather than a step function.
    # candidate.hos_state/hours_since_home are DECISION-TIME figures (this function has no access
    # to the live-updated hos_log/fleet -- only run_simulation()'s caller does); the SAME light
    # estimate reward.py's own home_progress_bonus already uses for a landing state (current
    # figure minus this trip's own planned duty hours) is reused here for consistency, not a
    # separate, second approximation.
    home_hub_id = driver_home_hub_id(data, driver_id)
    remaining_cycle_at_completion = max(
        0.0, min(candidate.hos_state.remaining_cycle1_hours, candidate.hos_state.remaining_cycle2_hours)
        - candidate.planned_duty_hours,
    )
    # Mirrors reward.py's compute_reward() internal hours_since_home_landing estimate exactly:
    # 0.0 once landing AT home (order.dest_location_id == home_hub_id), else the light
    # current-plus-this-trip's-duty estimate -- same reasoning distance_to_home_miles_landing
    # already establishes for its own next-state role.
    hours_since_home_at_completion = None
    if candidate.hours_since_home is not None:
        hours_since_home_at_completion = (
            0.0 if order.dest_location_id == home_hub_id
            else candidate.hours_since_home + candidate.planned_duty_hours
        )
    outcome, target_location_id = repositioning_outcome(
        order.dest_location_id, home_hub_id, nearest_hub_id, order.dest_distance_to_hub_km,
        remaining_cycle_at_completion, hours_since_home_at_completion, candidate.hours_to_home_landing or 0.0, rng,
    )

    final_location_id = order.dest_location_id
    if outcome == 'reload':
        pass  # no transition needed -- rig is simply available here, at t
    elif outcome == 'reposition':
        dh_miles, dh_hours = get_route(data, order.dest_location_id, target_location_id)
        trip.transition(TripStatus.DISP, t, loaded=False, distance_miles=dh_miles)
        t += timedelta(hours=dh_hours)
        trip.transition(TripStatus.ARRSHIP, t)  # arrives back at the hub, ready for the next pickup
        final_location_id = target_location_id
    else:  # 'dromt'
        trip.transition(TripStatus.DROMT, t)  # empty trailer dropped, staying local

    # Captured BEFORE after_trip() advances the odometer/calendar -- decision-time state.
    truck_breakdown_risk = truck_state.breakdown_risk
    truck_pct_km_interval = truck_state.pct_of_km_interval
    truck_pct_days_interval = truck_state.pct_of_days_interval
    truck_state = truck_state.after_trip(deadhead_miles + order.loaded_miles, hours_elapsed=(t - now).total_seconds() / 3600)

    # Proactive maintenance (documents/logs/19): rolled AFTER after_trip(), against the truck's
    # real post-trip overdue-ness -- without this a truck in a long simulated year never had a
    # path back to a healthy state (serviced() existed but nothing ever called it). Deliberately
    # does NOT extend `t`/completed_at here -- this is the TRUCK going into the shop, not the
    # driver's own trip running long (unlike the BREAKDOWN branch above, which happens MID-ROUTE
    # and genuinely strands driver+truck together). A driver whose truck needs service just picks
    # a different truck from their pool for their next trip in real operations; baking downtime
    # into `t` would wrongly log it as DRIVING time on the driver's HOS clock -- the exact bug
    # class already caught and fixed once tonight for the just-in-time-departure logic. The
    # caller (run_simulation()) applies the real truck-only unavailability window instead.
    went_to_maintenance = truck_state.sample_proactive_maintenance(rng)
    if went_to_maintenance:
        truck_state = truck_state.serviced()

    # secondary_pickup_revenue is realized here (the trip actually happened), same treatment as
    # post_delivery_deadhead_cost/realized_breakdown_penalty -- known only after the fact, netted
    # into both the total reward AND the trip's reported order_revenue (a consolidated LTL trip
    # really did earn two shipments' worth, not one).
    lateness_penalty = late_delivery_penalty(completed_at, order.promised_delivery_at)
    total_reward = (
        reward.total - post_delivery_deadhead_cost(trip) - realized_breakdown_penalty(trip)
        + secondary_pickup_revenue - lateness_penalty
    )
    order_revenue = reward.order_revenue + secondary_pickup_revenue
    fill_ratio = load_fill_ratio(
        order.weight_lbs, order.pallets,
        CAPACITY_BY_LOAD_TYPE.get(order.load_type, 44500), CAPACITY_PALLETS_BY_LOAD_TYPE.get(order.load_type, 26),
    )

    completed = CompletedTrip(
        trip_id=trip_id, order=order, driver_id=driver_id, truck_number=truck_number,
        assigned_at=now, completed_at=completed_at, trip_state=trip, order_revenue=order_revenue,
        immediate_reward=reward.total, reward_total=total_reward, deadhead_miles=deadhead_miles,
        deadhead_cost=reward.deadhead_cost, load_fill_ratio=fill_ratio,
        opportunity_cost_penalty=reward.opportunity_cost_penalty,
        was_exploration=was_exploration, driver_location_id=driver_location_id,
        driver_hos_remaining=candidate.hos_state.remaining_hours,
        driver_hos_driving_remaining=candidate.hos_state.remaining_driving_hours,
        driver_hos_duty_remaining=candidate.hos_state.remaining_duty_hours,
        driver_hos_cycle1_remaining=candidate.hos_state.remaining_cycle1_hours,
        driver_hos_cycle2_remaining=candidate.hos_state.remaining_cycle2_hours,
        lateness_penalty=lateness_penalty,
        distance_to_home_miles=candidate.distance_to_home_miles or 0.0,
        distance_to_home_miles_landing=candidate.distance_to_home_miles_landing or 0.0,
        hours_since_home=candidate.hours_since_home, hours_since_home_landing=hours_since_home_at_completion,
        home_progress_bonus=reward.home_progress_bonus,
        cycle_end_stranding_penalty=reward.cycle_end_stranding_penalty,
        total_committed_distance_miles=deadhead_miles + order.loaded_miles,
        driver_pool_size=driver_pool_size,
        truck_pct_km_interval=truck_pct_km_interval,
        truck_pct_days_interval=truck_pct_days_interval,
        truck_breakdown_risk=truck_breakdown_risk,
        planned_driving_hours=candidate.planned_driving_hours,
        planned_duty_hours=candidate.planned_duty_hours,
    )
    # `departure_time` (may be later than `now` -- see the just-in-time-departure note above) is
    # returned separately so the CALLER logs HOS correctly: any wait between `now` (assignment/
    # commitment) and `departure_time` (actually pulling out) is real off-duty time, not phantom
    # driving -- it must never be logged as a DRIVING interval, or a driver who waits for a future
    # pickup would incorrectly appear to have burned HOS hours just sitting still.
    return completed, truck_state, final_location_id, t, departure_time, went_to_maintenance


# --------------------------------------------------------------------------------------------
# Main event loop
# --------------------------------------------------------------------------------------------

def pick_pool_truck(fleet: Fleet, driver_id: int, location_id: int, now: datetime) -> str | None:
    """Among this driver's regular pool (build_truck_pools), the healthiest truck that's both
    available and physically at the driver's current location right now -- a driver can't pair
    with a truck sitting somewhere else without first traveling to it, which isn't modeled as its
    own leg (see the module docstring's driver<->truck section). A truck currently in for
    PROACTIVE maintenance (documents/logs/19 -- `maintenance_until` in the future) is excluded the
    same way an in-use truck is -- it's genuinely not at the yard right now. Returns None if none
    of the driver's pool trucks are currently free and in the same place -- that driver simply
    isn't a candidate for this order.
    """
    options = [
        t for t in fleet.truck_pool[driver_id]
        if fleet.trucks[t].available and fleet.trucks[t].location_id == location_id
        and (fleet.trucks[t].maintenance_until is None or fleet.trucks[t].maintenance_until <= now)
    ]
    if not options:
        return None
    return min(options, key=lambda t: fleet.truck_maint[t].breakdown_risk)  # dispatcher picks their healthiest option


def run_simulation(
    hours: float, seed: int, epsilon_start: float = EPSILON_START, epsilon_end: float = EPSILON_END,
    data: SimData | None = None, value_fn=None, sim_start: datetime | None = None,
    orders: list['Order'] | None = None,
) -> dict:
    """`data` lets a caller running many runs (sim/run_batch.py) load calibration/fleet data from
    Supabase ONCE per worker process instead of once per run -- the network round-trip otherwise
    dominates wall-clock time at any real batch size. Single-run CLI usage is unaffected: omit
    `data` and it loads exactly as before.

    `value_fn` (default None -> policy.zero_value_fn) lets a caller drive REAL dispatch decisions
    with the trained state-value model (sim/engine/value_function.py's make_value_fn()) instead of
    the always-0.0 stub -- this is what actually tests whether the value-augmented score produces
    better real outcomes, as opposed to just scoring decisions after the fact with a stub-driven
    policy (see documents/logs/15's off-policy-evaluation finding for why that isn't a valid test).

    `sim_start` defaults to a year before this project's reference "today" (documents/logs/17):
    training happens over a PAST period, so a live system starting today is unambiguously a fresh
    period the model never saw, not something already baked into training data.

    `epsilon_start`/`epsilon_end` replace a single flat `epsilon` -- see epsilon_schedule().

    `orders` (documents/logs/19's REAL-DATA REPLAY experiment): when given, REPLACES the
    synthetic Poisson-arrival/generate_order() process with this exact pre-built sequence of REAL
    historical orders (real weight/pallets/load_type/origin/dest/timing) -- everything else (fleet
    init, HOS, maintenance, candidate scoring, dispatch decisions) runs identically either way.
    This is a "replay session," not an attempt to reconstruct real historical driver positions
    (which the source data can't support -- no real fleet-state snapshots exist): our OWN
    internally-consistent simulated fleet state processes the REAL order sequence, so this tests
    "can our system serve this realistic order pattern well," not "did the real dispatcher
    provably make a mistake." `hours`/`sim_start` are ignored when `orders` is given -- the run
    spans exactly the given orders' own time range.
    """
    if value_fn is None:
        value_fn = zero_value_fn
    rng = random.Random(seed)
    if data is None:
        data = load_sim_data()

    if orders is not None:
        sim_start = min(o.decision_time for o in orders)
        sim_end = max(o.decision_time for o in orders) + timedelta(hours=1)  # 1h slack so the last decision's own event still fires
    else:
        if sim_start is None:
            sim_start = datetime(2025, 9, 8, 6, 0)  # one year before this project's reference "today"
        sim_end = sim_start + timedelta(hours=hours)

    fleet = initialize_fleet(data, rng, sim_start)
    sim_id = uuid.uuid4()  # generated up front (not at return time) -- needed while logging candidate_score_rows during the loop
    candidate_score_rows: list[tuple] = []
    # SAME shape as candidate_score_rows, but ranked by the value-augmented score (immediate +
    # gamma*V(s')) -- the score actually driving choose_assignment()'s real pick, not the
    # immediate-reward-only score sim.candidate_scores' own training-data contract deliberately
    # uses. Real user feedback: a live/showcase candidate panel showing the immediate-only score
    # made the actually-chosen candidate look like it wasn't the "top" one, when really the model
    # picked it for its higher TOTAL (including future-positioning) value -- confusing without
    # this. Kept as a SEPARATE list (not a change to candidate_score_rows itself) so the offline
    # training-data table's own documented contract is untouched.
    value_augmented_candidate_score_rows: list[tuple] = []

    counter = itertools.count()
    events: list[tuple[datetime, int, str, object]] = []
    if orders is not None:
        for order in orders:
            heapq.heappush(events, (order.decision_time, next(counter), 'DISPATCH_DECISION', order))
    else:
        heapq.heappush(events, (next_order_arrival(data, sim_start, rng), next(counter), 'ORDER_ARRIVAL', None))

    completed_trips: list[CompletedTrip] = []
    unassigned_orders = 0
    unassigned_order_ids: list[uuid.UUID] = []  # documents/logs/19 -- lets a caller identify WHICH orders had no feasible candidate, not just the count
    # Real IDLE_TIMEOUT/IDLE_REPOSITION_COMPLETE outcomes (documents/logs/25-27) -- this mechanism
    # mutates fleet.drivers[...].location_id directly, OUTSIDE of any CompletedTrip's own
    # next_location_id, so a caller walking completed_trips alone (e.g. real_data_replay.py's
    # cycle analysis) would never see these real relocations at all -- confirmed directly: a real
    # backtest replay showed 32 real IDLE_TIMEOUT repositions/2,260mi that the cycle-analysis
    # script's own bookkeeping silently missed. Logged explicitly here so any caller that needs
    # the real driver-position timeline (not just the aggregate reward effect already folded into
    # the preceding trip's reward_total) has a real, structured record to walk.
    idle_repositions: list[dict] = []

    while events:
        now, _, kind, payload = heapq.heappop(events)
        # TRIP_COMPLETE events are always let through even past sim_end -- an in-progress trip's
        # own real outcome must never be silently dropped just because it finishes slightly after
        # the horizon (documents/logs/19: matters for the real-data replay especially, where every
        # real order should be accounted for, not truncated by an arbitrary cutoff). NEW
        # decision-generating events (ORDER_ARRIVAL/DISPATCH_DECISION) past sim_end are dropped
        # instead -- `continue`, not `break`: the heap is a min-heap by time, so a later-time
        # ORDER_ARRIVAL/DISPATCH_DECISION can be popped BEFORE an earlier-time TRIP_COMPLETE that
        # was already scheduled from an in-progress trip; `break`ing here would wrongly discard
        # that still-pending real completion instead of draining down to it.
        if now > sim_end and kind not in ('TRIP_COMPLETE', 'IDLE_REPOSITION_COMPLETE'):
            continue

        if kind == 'ORDER_ARRIVAL':
            heapq.heappush(events, (next_order_arrival(data, now, rng), next(counter), 'ORDER_ARRIVAL', None))
            order = generate_order(data, now, rng)
            # Booking the order and DECIDING who takes it are now two separate events
            # (documents/logs/17) -- an order with a real lead time just sits on the books until
            # its decision_time (24h before requested pickup, or immediately if booked with less
            # notice than that). This is what makes "hold this order for someone who'll be free
            # and nearby soon" possible at all, instead of every decision being forced instantly.
            heapq.heappush(events, (order.decision_time, next(counter), 'DISPATCH_DECISION', order))

        elif kind == 'DISPATCH_DECISION':
            order = payload

            candidates, truck_by_driver, driver_positions = [], {}, {}
            # Home-base-return retarget (documents/logs/23): the LANDING distance/hours-to-home
            # only depends on (order.dest_location_id, driver's home hub) -- not on the driver's
            # own current position -- and there are only ever 2 real home hubs (Milton/London), so
            # this caches to at most 2 real get_route() calls per order arrival instead of one per
            # candidate driver.
            home_hub_landing_cache: dict[int, tuple[float, float]] = {}
            for driver_id in data.driver_ids:
                drv = fleet.drivers[driver_id]
                eff_location_id, eff_truck_number, eff_start = effective_driver_state(fleet, driver_id, now)
                if eff_truck_number is None:
                    # No active commitment past `now` -- resolve a pool truck fresh, exactly as
                    # before this change (a driver can't be paired with a truck sitting elsewhere).
                    eff_truck_number = pick_pool_truck(fleet, driver_id, eff_location_id, now)
                    if eff_truck_number is None:
                        continue  # this driver has no available, co-located pool truck right now
                # Mid-route drivers ARE candidates now (documents/logs/17) -- scored from their
                # PROJECTED landing spot/time, not excluded outright. The only hard filter stays
                # HOS legality (below, via can_perform inside feasible_candidates); a legal but
                # badly-timed or wrong-direction pick is left in and taught via the reward instead
                # (real deadhead cost, and a late pickup cascades into the real lateness penalty
                # once run_assignment() below starts the clock at `eff_start`, not `now`).
                dh_miles, dh_hours = get_route(data, eff_location_id, order.origin_location_id)
                apply_idle_reset(drv.hos_log, eff_start)
                hos_state = drv.hos_log.snapshot(eff_start)
                planned_driving = dh_hours + order.loaded_hours
                # Real user correction: this used to be a flat "+1.5" guess -- now the real
                # calibrated median dwell (pickup + delivery, ~1.0h combined), same figure
                # sim/dispatch_solver.py's dwell_hours_per_order() and every other real-dwell call
                # site in this project already uses, not a second, independent number.
                planned_duty = planned_driving + data.dwell_minutes['pickup'][1] / 60 + data.dwell_minutes['delivery'][1] / 60

                home_hub_id = driver_home_hub_id(data, driver_id)
                home_miles_now, home_hours_now = get_route(data, eff_location_id, home_hub_id)
                if home_hub_id not in home_hub_landing_cache:
                    home_hub_landing_cache[home_hub_id] = get_route(data, order.dest_location_id, home_hub_id)
                home_miles_landing, home_hours_landing = home_hub_landing_cache[home_hub_id]
                # Home-time retarget (documents/logs/25): real hours since this driver was last AT
                # home, as of THIS candidate's own effective_start -- last_home_at is always
                # correct as of `now` (see DriverState's own comment), and eff_start >= now, so this
                # is a real, not-yet-elapsed-at-decision-time projection, the same treatment
                # eff_start already gets for every other feature here.
                hours_since_home = max(0.0, (eff_start - drv.last_home_at).total_seconds() / 3600)

                cand = Candidate(
                    driver_id=driver_id, truck_number=eff_truck_number, hos_state=hos_state,
                    truck_state=fleet.truck_maint[eff_truck_number], pre_pickup_deadhead_miles=dh_miles,
                    planned_driving_hours=planned_driving, planned_duty_hours=planned_duty,
                    pre_pickup_deadhead_hours=dh_hours, effective_start=eff_start,
                    distance_to_home_miles=home_miles_now, distance_to_home_miles_landing=home_miles_landing,
                    hours_to_home_current=home_hours_now, hours_to_home_landing=home_hours_landing,
                    hours_since_home=hours_since_home,
                )
                candidates.append(cand)
                truck_by_driver[driver_id] = eff_truck_number
                driver_positions[driver_id] = (eff_location_id, eff_start)

            capacity_lbs = CAPACITY_BY_LOAD_TYPE.get(order.load_type, 44500)
            capacity_pallets = CAPACITY_PALLETS_BY_LOAD_TYPE.get(order.load_type, 26)

            # Full candidate ranking, logged regardless of what's actually dispatched -- learning-
            # to-rank needs a real GROUP of considered candidates per arrival, not just the winner
            # (sim.candidate_scores, sim/sql/023). Scored once here (immediate_reward only, no
            # embedded GPU model -- keeps the hot loop fast) and again inside choose_assignment()
            # below; a small duplicated cost for keeping policy.py's interface unchanged.
            ranked = rank_candidates(
                candidates, order, capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
                capacity_value_rate_per_lb=ASSUMED_CAPACITY_VALUE_RATE_PER_LB, gamma=GAMMA,
            )
            top_ranked = ranked[:TOP_N_CANDIDATES_TO_LOG]

            # The value-augmented ranking -- same rank_candidates() call, just WITH value_fn/now,
            # so it reflects the score choose_assignment() actually picks its winner from (see
            # value_augmented_candidate_score_rows above). UNLIKE top_ranked above, this is NOT
            # truncated to TOP_N_CANDIDATES_TO_LOG -- that constant's whole reason for existing is
            # sim.candidate_scores' learning-to-rank training contract (a fixed-size candidate
            # group per row), which value_augmented_candidate_score_rows has nothing to do with
            # (confirmed: it's read only by the live/showcase audit trail, never by training).
            # Real user feedback: the Order Story showed only 10 of the real ~30 candidates the
            # model actually scored -- logging the full feasible set here is what lets it show all
            # of them with their real scores and features, not an arbitrary top slice.
            value_ranked = rank_candidates(
                candidates, order, capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
                capacity_value_rate_per_lb=ASSUMED_CAPACITY_VALUE_RATE_PER_LB, gamma=GAMMA,
                value_fn=value_fn, now=now,
            )

            epsilon = epsilon_schedule(now, sim_start, sim_end, epsilon_start, epsilon_end)
            # value_fn drives the ACTUAL decision here -- rank_candidates() above stays
            # immediate-reward-only on purpose (sim.candidate_scores' documented contract, no GPU
            # model in that hot-loop logging path); this is the one call that needs to be
            # value-augmented to genuinely test whether it changes real outcomes.
            result = choose_assignment(
                candidates, order, capacity_lbs=capacity_lbs, capacity_pallets=capacity_pallets,
                capacity_value_rate_per_lb=ASSUMED_CAPACITY_VALUE_RATE_PER_LB,
                epsilon=epsilon, gamma=GAMMA, rng=rng, value_fn=value_fn, now=now,
            )
            if result is None:
                unassigned_orders += 1
                unassigned_order_ids.append(order.order_id)
                continue

            candidate, reward, was_exploration = result
            driver_id, truck_number = candidate.driver_id, truck_by_driver[candidate.driver_id]

            for rank_position, (c, score) in enumerate(top_ranked, start=1):
                candidate_score_rows.append((
                    sim_id, order.order_id, c.driver_id, c.truck_number,
                    driver_positions[c.driver_id][0], c.hos_state.remaining_hours,
                    c.truck_state.breakdown_risk, c.truck_state.pct_of_km_interval, c.truck_state.pct_of_days_interval,
                    c.pre_pickup_deadhead_miles, c.planned_driving_hours, c.planned_duty_hours,
                    score, rank_position, c.driver_id == driver_id,
                ))
            for rank_position, (c, score) in enumerate(value_ranked, start=1):
                value_augmented_candidate_score_rows.append((
                    sim_id, order.order_id, c.driver_id, c.truck_number,
                    driver_positions[c.driver_id][0], c.hos_state.remaining_hours,
                    c.truck_state.breakdown_risk, c.truck_state.pct_of_km_interval, c.truck_state.pct_of_days_interval,
                    c.pre_pickup_deadhead_miles, c.planned_driving_hours, c.planned_duty_hours,
                    score, rank_position, c.driver_id == driver_id,
                ))
            driver_location_id, effective_start = driver_positions[driver_id]  # decision-time PROJECTED position/start
            driver_pool_size = len(fleet.truck_pool[driver_id])
            completed, new_truck_state, final_location_id, driving_end, departure_time, went_to_maintenance = run_assignment(
                data, order, candidate, driver_id, truck_number, driver_location_id, driver_pool_size,
                fleet.truck_maint[truck_number], reward, was_exploration, effective_start, rng,
            )
            fleet.truck_maint[truck_number] = new_truck_state
            # documents/logs/19 -- the DRIVER isn't stuck at the shop (they're free for their next
            # trip with a different pool truck), only this specific truck is held out of service.
            # driving_end + MAINTENANCE_DOWNTIME_DAYS, not just MAINTENANCE_DOWNTIME_DAYS from
            # `now` -- the truck can't go in for service until this trip is actually finished.
            if went_to_maintenance:
                fleet.trucks[truck_number].maintenance_until = driving_end + timedelta(days=MAINTENANCE_DOWNTIME_DAYS)
            # A just-in-time departure (documents/logs/17) can leave a real gap between
            # `effective_start` (assignment/commitment) and `departure_time` (actually pulling
            # out) -- that gap is genuine off-duty waiting, not driving, and long enough gaps
            # should count as a real qualifying HOS reset (same threshold apply_idle_reset uses
            # for genuinely idle drivers) so a driver who waited 10+ hours for a future pickup
            # isn't scored as still tired when they finally leave.
            if departure_time > effective_start:
                gap_hours = (departure_time - effective_start).total_seconds() / 3600
                if gap_hours >= HOS_MIN_DAILY_OFF_DUTY_HOURS:
                    fleet.drivers[driver_id].hos_log.add(effective_start, departure_time, OFF_DUTY)
                # A short (<10h) wait doesn't qualify as a real reset -- left unlogged, matching
                # apply_idle_reset's existing treatment of sub-threshold gaps elsewhere (HOSLog's
                # hour totals only sum LOGGED intervals, so unlogged time is simply neither driving
                # nor on-duty, not silently double-counted either way).
            # Starts at departure_time (not effective_start) -- can't overlap the OFF_DUTY wait
            # interval just added above; HOSLog.add() requires strictly non-overlapping intervals.
            fleet.drivers[driver_id].hos_log.add(departure_time, driving_end, DRIVING)  # coarse: whole trip as one driving block
            # New commitment recorded immediately (documents/logs/17) -- this is what lets a
            # driver be picked again as a PROJECTED candidate for a future order even before their
            # current trip's own TRIP_COMPLETE event has fired.
            fleet.drivers[driver_id].committed_until = driving_end
            fleet.drivers[driver_id].committed_location_id = final_location_id
            fleet.drivers[driver_id].committed_truck_number = truck_number
            fleet.trucks[truck_number].available = False
            # Next-state for Fitted Value Iteration -- the driver's real position/legal hours
            # right after this trip, computed here (not inside run_assignment()) because it needs
            # the hos_log AFTER the interval above was just added.
            completed.next_location_id = final_location_id
            next_hos_state = fleet.drivers[driver_id].hos_log.snapshot(driving_end)
            completed.next_hos_remaining = next_hos_state.remaining_hours
            completed.next_hos_driving_remaining = next_hos_state.remaining_driving_hours
            completed.next_hos_duty_remaining = next_hos_state.remaining_duty_hours
            completed.next_hos_cycle1_remaining = next_hos_state.remaining_cycle1_hours
            completed.next_hos_cycle2_remaining = next_hos_state.remaining_cycle2_hours
            completed.next_available_at = driving_end
            # new_truck_state IS the post-trip truck state (after_trip() already applied inside
            # run_assignment()) -- the same truck rides forward with this driver into their next
            # commitment, so this is genuinely "the state you'll be in," not a future-order guess.
            completed.next_truck_pct_km_interval = new_truck_state.pct_of_km_interval
            completed.next_truck_pct_days_interval = new_truck_state.pct_of_days_interval
            # Scheduled at driving_end, not completed.completed_at: the driver+truck aren't
            # actually free to take a new order until any post-completion deadhead leg (driving
            # back toward a hub) has also finished -- using the earlier COMPLETE timestamp here
            # would let the same pair get double-booked while still empty-driving, and would also
            # violate HOSLog.add's non-overlapping invariant on the very next trip.
            heapq.heappush(
                events, (driving_end, next(counter), 'TRIP_COMPLETE',
                         (driver_id, truck_number, final_location_id, completed)),
            )

        elif kind == 'TRIP_COMPLETE':
            driver_id, truck_number, final_location_id, completed = payload
            completed_trips.append(completed)
            # Guard against a driver who's ALREADY been re-committed to a newer trip by the time
            # this (older) TRIP_COMPLETE fires -- a real, expected case now that in-transit drivers
            # are candidates: they can be picked for a NEW order before this event even runs. Only
            # update location_id/truck availability from THIS trip if nothing newer has since
            # superseded it -- otherwise this stale landing spot would overwrite state that a
            # still-active newer commitment (committed_location_id/committed_until) already
            # supersedes; the newer commitment's own TRIP_COMPLETE will set the real final state.
            if fleet.drivers[driver_id].committed_until == now:
                fleet.drivers[driver_id].location_id = final_location_id
                fleet.trucks[truck_number].available = True
                fleet.trucks[truck_number].location_id = final_location_id
                # Home-time retarget (documents/logs/25): a real cycle closing at home, tracked as
                # it actually happens -- not inferred after the fact the way fleet_metrics.py's
                # analysis-only cycle detection has to for persisted/reloaded data.
                if final_location_id == driver_home_hub_id(data, driver_id):
                    fleet.drivers[driver_id].last_home_at = now
                # Home-time retarget (documents/logs/25-26): "don't stay idle" -- a driver isn't
                # left free forever with zero consequence if nothing ever claims them. A real
                # follow-up check, at the same 24h horizon real dispatch decisions already use (not
                # a new number, and not a dynamically-computed "safe deadline" -- propose a
                # reasonable, grounded default, then validate it with real data, same as every
                # other synthesized constant in this project). `completed` is the SAME object
                # already appended to completed_trips above -- IDLE_TIMEOUT mutates it directly so
                # whatever real cost this driver's idle stretch turns out to cost lands back on
                # the decision that actually caused it. This check RESCHEDULES itself (below) as
                # long as the driver is still genuinely uncommitted -- it's not a one-shot deadline.
                heapq.heappush(
                    events,
                    (now + timedelta(hours=DISPATCH_DECISION_CUTOFF_HOURS), next(counter), 'IDLE_TIMEOUT',
                     (driver_id, truck_number, completed)),
                )

        elif kind == 'IDLE_TIMEOUT':
            driver_id, truck_number, preceding_trip = payload
            drv = fleet.drivers[driver_id]
            home_hub_id = driver_home_hub_id(data, driver_id)
            still_idle = drv.committed_until is None or drv.committed_until <= now
            if still_idle and drv.location_id != home_hub_id:
                # Real idle-gap qualifying rest FIRST (apply_idle_reset -- the same real mechanism
                # every other idle gap in this sim gets credited with), so both the urgency figure
                # and the feasibility check below reflect a genuinely rested driver, not a stale,
                # artificially-tired one.
                apply_idle_reset(drv.hos_log, now)
                hos_state = drv.hos_log.snapshot(now)
                hours_since_home = max(0.0, (now - drv.last_home_at).total_seconds() / 3600)
                nearest_hub_id = min(
                    data.hub_ids.values(),
                    key=lambda hid: _haversine_km(data.locations[drv.location_id], data.locations[hid]),
                )
                dist_to_hub_km = _haversine_km(data.locations[drv.location_id], data.locations[nearest_hub_id])
                _, hours_to_home = get_route(data, drv.location_id, home_hub_id)
                # SAME decision function `run_assignment()`'s post-delivery moment uses, re-run with
                # FRESH current inputs -- not a separate, bespoke "give up and go home" branch (the
                # design this replaces). A genuinely idle driver defaults to repositioning toward
                # freight OPPORTUNITY (the nearest hub) exactly like a real dispatcher would, absent
                # an actual reason (elevated legal or business urgency) to send them home instead --
                # `repositioning_outcome()` is what decides which, using the SAME urgency ramp.
                outcome, target_location_id = repositioning_outcome(
                    drv.location_id, home_hub_id, nearest_hub_id, dist_to_hub_km,
                    min(hos_state.remaining_cycle1_hours, hos_state.remaining_cycle2_hours),
                    hours_since_home, hours_to_home, rng,
                )
                if outcome == 'reposition':
                    dh_miles, dh_hours = get_route(data, drv.location_id, target_location_id)
                    if hos_state.can_perform(dh_hours, dh_hours):
                        # Legally can make the full drive -- do it for real, same bookkeeping any
                        # other real driving leg gets. Logged NOW (same pattern run_assignment()
                        # uses) but location_id/last_home_at are only finalized when the drive
                        # actually FINISHES (IDLE_REPOSITION_COMPLETE below) -- committed_until is
                        # what makes this driver correctly unavailable/projectable-from-target in
                        # the meantime, exactly like a real trip commitment. Updating location_id
                        # immediately here (the bug this replaces) let a DISPATCH_DECISION firing
                        # mid-drive treat the driver as already free NOW at the target, while
                        # hos_log already had the drive logged through a LATER time -- a genuine
                        # causality violation (HOSLog.add() correctly rejects the overlap).
                        drv.hos_log.add(now, now + timedelta(hours=dh_hours), DRIVING)
                        driving_end = now + timedelta(hours=dh_hours)
                        drv.committed_until = driving_end
                        drv.committed_location_id = target_location_id
                        drv.committed_truck_number = truck_number
                        fleet.trucks[truck_number].available = False
                        idle_cost = dh_miles * ASSUMED_OPERATING_COST_PER_MILE
                        preceding_trip.idle_then_drove_home_miles = dh_miles
                        preceding_trip.reward_total -= idle_cost
                        heapq.heappush(
                            events,
                            (driving_end, next(counter), 'IDLE_REPOSITION_COMPLETE',
                             (driver_id, truck_number, drv.location_id, target_location_id, dh_miles, driving_end)),
                        )
                    else:
                        # Genuinely can't legally make even this drive after a real rest -- the
                        # "Automated HOS Compliance" rule this whole project holds to means this
                        # never gets forced through as an illegal drive. FLAGGED SIMPLIFICATION:
                        # doesn't attempt to model exactly how far they DID get (would need
                        # route-interpolation machinery this event doesn't have) -- they stay put,
                        # genuinely stuck, and this is priced as the real, larger stranding outcome
                        # it is (same cap cycle_end_stranding_penalty already uses elsewhere), not
                        # silently ignored.
                        preceding_trip.idle_then_stranded_penalty = ASSUMED_CYCLE_STRANDING_PENALTY_CAD_MAX
                        preceding_trip.reward_total -= ASSUMED_CYCLE_STRANDING_PENALTY_CAD_MAX
                else:
                    # Still genuinely idle (the roll came up 'reload'/'dromt' -- no real reason yet
                    # to move) -- check again in another 24h rather than silently giving up after
                    # one look, same real follow-up cadence, looped for as long as nothing claims
                    # this driver. Bounded by sim_end same as every other event (the top-of-loop
                    # `now > sim_end` guard drops it once the run is over).
                    heapq.heappush(
                        events,
                        (now + timedelta(hours=DISPATCH_DECISION_CUTOFF_HOURS), next(counter), 'IDLE_TIMEOUT',
                         (driver_id, truck_number, preceding_trip)),
                    )

        elif kind == 'IDLE_REPOSITION_COMPLETE':
            # Finalizes an idle-timeout repositioning drive (scheduled above) at the real time it
            # actually finishes -- same guard pattern TRIP_COMPLETE uses: only apply if nothing
            # newer (a real order this driver got assigned to mid-drive) has since superseded this
            # commitment. If superseded, the hos_log entry/cost already charged stays valid (the
            # drive really happened); only the final location/availability bookkeeping is skipped
            # here, since the newer commitment's own completion event owns that now.
            driver_id, truck_number, source_location_id, target_location_id, dh_miles, driving_end = payload
            if fleet.drivers[driver_id].committed_until == driving_end:
                fleet.drivers[driver_id].location_id = target_location_id
                fleet.drivers[driver_id].committed_until = None
                fleet.drivers[driver_id].committed_location_id = None
                fleet.drivers[driver_id].committed_truck_number = None
                reached_home = target_location_id == driver_home_hub_id(data, driver_id)
                if reached_home:
                    fleet.drivers[driver_id].last_home_at = driving_end
                fleet.trucks[truck_number].available = True
                fleet.trucks[truck_number].location_id = target_location_id
                # Real, structured record of this relocation (see idle_repositions' own comment
                # above) -- only logged once it actually completes un-superseded, matching every
                # other real outcome this loop records.
                idle_repositions.append({
                    'driver_id': driver_id, 'time': driving_end, 'from_location_id': source_location_id,
                    'to_location_id': target_location_id, 'miles': dh_miles, 'reached_home': reached_home,
                })

    return {
        'sim_id': sim_id, 'seed': seed, 'epsilon_start': epsilon_start, 'epsilon_end': epsilon_end, 'hours': hours,
        'sim_start': sim_start, 'completed_trips': completed_trips, 'unassigned_orders': unassigned_orders,
        'unassigned_order_ids': unassigned_order_ids, 'candidate_score_rows': candidate_score_rows,
        'value_augmented_candidate_score_rows': value_augmented_candidate_score_rows,
        # Real IDLE_TIMEOUT/IDLE_REPOSITION_COMPLETE relocations -- see idle_repositions' own
        # comment above. Empty list, not missing, when the mechanism never fired this run.
        'idle_repositions': idle_repositions,
        # Final fleet state (every driver/truck, not just ones with a completed trip this run) --
        # e.g. the Simulation Showcase's end-of-week truck_maintenance_state table needs EVERY
        # demo truck's final health, including ones that never got dispatched.
        'fleet': fleet,
    }


# --------------------------------------------------------------------------------------------
# Persist one run's exhaust to LOCAL Postgres, in bulk
# --------------------------------------------------------------------------------------------

def save_run(result: dict) -> None:
    sim_id = result['sim_id']
    with cursor(local=True) as cur:
        cur.execute(
            "insert into sim.runs (sim_id, seed, epsilon_start, epsilon_end, config_json) values (%s, %s, %s, %s, %s)",
            (sim_id, result['seed'], result['epsilon_start'], result['epsilon_end'], Json({'hours': result['hours'], 'start': result['epsilon_start'], 'end': result['epsilon_end']})),
        )

        order_rows, assignment_rows, event_rows = [], [], []
        for c in result['completed_trips']:
            order_rows.append((
                sim_id, c.order.order_id, c.order.origin_location_id, c.order.dest_location_id,
                c.order.created_at, c.order.decision_time,
                c.order.requested_pickup_at,  # pickup_window_start -- repurposed, see sim/sql/026
                int(round(c.order.weight_lbs)), int(round(c.order.pallets)),
                c.order.load_type, c.trip_state.summarize_run_type(), c.order_revenue, 'completed',
                c.order.service_type, c.order.loaded_miles, c.order.dest_distance_to_hub_km,
                c.order.dest_local_order_density, c.order.promised_delivery_at,
            ))
            assignment_rows.append((
                sim_id, c.order.order_id, c.driver_id, c.truck_number, c.assigned_at,
                c.was_exploration, c.immediate_reward, c.deadhead_miles,  # miles, not deadhead_cost -- see CompletedTrip
                c.load_fill_ratio, c.opportunity_cost_penalty,
                c.reward_total,  # fully resolved -- includes realized post-delivery-deadhead/breakdown costs
                c.driver_location_id, c.driver_hos_remaining,  # decision-time state, see sim/sql/014
                c.truck_breakdown_risk, c.trip_id, c.planned_driving_hours, c.planned_duty_hours,
                c.lateness_penalty, c.total_committed_distance_miles, c.driver_pool_size,
                c.truck_pct_km_interval, c.truck_pct_days_interval,
                c.next_location_id, c.next_hos_remaining, c.next_available_at,
                c.next_truck_pct_km_interval, c.next_truck_pct_days_interval,
                # HOS sub-clocks (documents/logs/23) -- computed in memory since sim/sql/013-014,
                # never persisted until now (sim/sql/041).
                c.driver_hos_driving_remaining, c.driver_hos_duty_remaining,
                c.driver_hos_cycle1_remaining, c.driver_hos_cycle2_remaining,
                c.next_hos_driving_remaining, c.next_hos_duty_remaining,
                c.next_hos_cycle1_remaining, c.next_hos_cycle2_remaining,
                # Home-base-return retarget (documents/logs/23, sim/sql/041):
                c.distance_to_home_miles, c.distance_to_home_miles_landing,
                c.home_progress_bonus, c.cycle_end_stranding_penalty,
                # Home-time retarget (documents/logs/25-26, sim/sql/047):
                c.hours_since_home, c.hours_since_home_landing,
            ))
            for i, ev in enumerate(c.trip_state.history):
                event_rows.append((sim_id, c.trip_id, i, c.driver_id, ev.status.value, ev.at))

        execute_values(
            cur,
            """insert into sim.orders (sim_id, order_id, origin_location_id, dest_location_id,
               created_at, decision_time, pickup_window_start,
               weight_lbs, pallets, load_type, run_type_hint, revenue, status,
               service_type, loaded_miles, dest_distance_to_hub_km, dest_local_order_density,
               promised_delivery_at)
               values %s""",
            order_rows,
        )
        execute_values(
            cur,
            """insert into sim.assignments (sim_id, order_id, driver_id, truck_number, assigned_at,
               was_exploration, immediate_margin, deadhead_miles, load_fill_ratio, opportunity_cost_penalty,
               reward_total, driver_location_id, driver_hos_remaining, truck_breakdown_risk, trip_id,
               planned_driving_hours, planned_duty_hours, lateness_penalty, total_committed_distance_miles,
               driver_pool_size, truck_pct_km_interval, truck_pct_days_interval,
               next_location_id, next_hos_remaining, next_available_at,
               next_truck_pct_km_interval, next_truck_pct_days_interval,
               driver_hos_driving_remaining, driver_hos_duty_remaining,
               driver_hos_cycle1_remaining, driver_hos_cycle2_remaining,
               next_hos_driving_remaining, next_hos_duty_remaining,
               next_hos_cycle1_remaining, next_hos_cycle2_remaining,
               distance_to_home_miles, distance_to_home_miles_landing,
               home_progress_bonus, cycle_end_stranding_penalty,
               hours_since_home, hours_since_home_landing)
               values %s""",
            assignment_rows,
        )
        execute_values(
            cur,
            "insert into sim.trip_events (sim_id, trip_id, leg_seq, driver_id, event_type, event_time) values %s",
            event_rows,
        )
        if result['candidate_score_rows']:
            execute_values(
                cur,
                """insert into sim.candidate_scores (sim_id, order_id, driver_id, truck_number,
                   driver_location_id, driver_hos_remaining, truck_breakdown_risk, truck_pct_km_interval,
                   truck_pct_days_interval, pre_pickup_deadhead_miles, planned_driving_hours,
                   planned_duty_hours, predicted_score, rank_position, was_chosen)
                   values %s""",
                result['candidate_score_rows'],
            )
    print(f'  saved run {sim_id}: {len(order_rows)} completed trips, {len(event_rows)} events, '
          f'{len(result["candidate_score_rows"])} candidate-score rows -> local sim.*')


def print_summary(result: dict) -> None:
    trips = result['completed_trips']
    n = len(trips)
    eps_start = result.get('epsilon_start', 0.6)
    eps_end = result.get('epsilon_end', 0.05)
    print(f"\n=== Run summary (seed={result['seed']}, eps {eps_start}->{eps_end}, {result['hours']}h, sim_start={result['sim_start']}) ===")
    print(f'  completed trips: {n}, unassigned orders: {result["unassigned_orders"]}')
    if n == 0:
        return
    total_reward = sum(c.reward_total for c in trips)
    breakdowns = sum(1 for c in trips if c.trip_state.had_breakdown)
    post_dh = sum(1 for c in trips if c.trip_state.had_post_delivery_deadhead)
    print(f'  total reward: {total_reward:,.2f} CAD, avg/trip: {total_reward / n:,.2f} CAD')
    print(f'  breakdowns: {breakdowns} ({breakdowns / n:.1%}), post-delivery-deadhead trips: {post_dh} ({post_dh / n:.1%})')
    print(f'  exploration draws: {sum(1 for c in trips if c.was_exploration)} / {n}')


def print_trace(result: dict, n: int) -> None:
    """Full status-event history for the first `n` completed trips -- the hand-validation
    checkpoint from the implementation plan (HOS decrements/resets correctly? no
    double-booking? deadhead/dwell/breakdown showing up where expected?).
    """
    for c in result['completed_trips'][:n]:
        print(f'\n--- trip {c.trip_id} | driver {c.driver_id} | truck {c.truck_number} ---')
        print(f'  order: {c.order.origin_location_id} -> {c.order.dest_location_id}, '
              f'{c.order.loaded_miles:.1f} mi, {c.order.weight_lbs:.0f} lbs, {c.order.load_type}')
        for ev in c.trip_state.history:
            print(f'  {ev.at}  {ev.status.value:10s}  loaded={ev.loaded}  dist={ev.distance_miles:.1f}mi  wt={ev.weight_lbs:.0f}lbs')
        print(f'  deadhead_hours={c.trip_state.deadhead_hours:.2f}  pickup_dwell={c.trip_state.pickup_dwell_hours:.2f}h  '
              f'delivery_dwell={c.trip_state.delivery_dwell_hours:.2f}h  post_delivery_deadhead_mi={c.trip_state.post_delivery_deadhead_miles:.1f}')
        print(f'  reward: immediate={c.immediate_reward:,.2f}  final={c.reward_total:,.2f}  '
              f'(order_revenue={c.order_revenue:,.2f}, deadhead_cost={c.deadhead_cost:,.2f}, '
              f'opportunity_cost={c.opportunity_cost_penalty:,.2f})')


if __name__ == '__main__':
    from sim.config import EPSILON_START, EPSILON_END
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float, default=4320.0)  # ~180 days / 6 months for a smoke test run
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--epsilon-start', type=float, default=EPSILON_START)
    parser.add_argument('--epsilon-end', type=float, default=EPSILON_END)
    parser.add_argument('--save', action='store_true', help='persist this run to local sim.* tables')
    parser.add_argument('--trace', type=int, default=0, help='print the full event history for the first N completed trips')
    args = parser.parse_args()

    result = run_simulation(
        hours=args.hours, seed=args.seed, epsilon_start=args.epsilon_start,
        epsilon_end=args.epsilon_end, data=None, value_fn=None, sim_start=None,
    )
    print_summary(result)
    if args.trace:
        print_trace(result, args.trace)
    if args.save:
        save_run(result)
