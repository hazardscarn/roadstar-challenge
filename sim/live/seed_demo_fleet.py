"""Resets and seeds the WHOLE live.* dataset for the actual demo fleet (scripts/seed_demo_accounts.py
made logins for all of them), replacing sim/live/seed_test_fleet.py's earlier, unrelated 25-driver
theory-proof seed (driver_ids 1-25, no overlap with our real demo accounts beyond a few IDs). This
is what Live Ops/Dispatch/Orders/Trip History/Fleet Health/Drivers all run against -- every table
gets truncated together (not just the fleet-state tables), because quote_requests/geofence_events/
trip_log/etc. all key off trip_id and were found ORPHANED (a real bug: quote_requests.status=
'assigned' pointing at a trip_id that no longer existed) the first time this script truncated only
live.trips/driver_status and left everything else stale.

Scaled 8 -> 30 drivers/trucks (real user feedback: "8 is not the best option"). DEMO_DRIVER_IDS/
DRIVER_TRUCKS are now built dynamically by _build_fleet_roster() rather than hand-typed -- the 18
real driver_id -> truck_number pairs on file in ground_truth.driver_equipment come first, filled to
30 with other real driver_ids (sorted, deterministic) paired to real ground_truth.trucks numbers
not already claimed by those 18 -- same synthesized-pool-truck treatment the original 8-driver
seed already used for driver 26 (no real DEFAULT_PUNIT on file).

Includes one PASSING live.vehicle_inspections row per driver dated `now()` -- without this, the
manager-side inspection gate (score_quote.py's EXISTS(...overall_pass...) filter, the brief's real
compliance requirement) would exclude every demo driver simply because Phase 7's inspection UI
hasn't been used yet. This mirrors a realistic baseline ("everyone did their pre-trip check this
morning"), not a way around the gate -- the gate itself is unchanged and still enforced.

Idle-driver POSITIONS are drawn from calibration.lane_frequency's real weighted location pool
(the SAME source run_sim.py itself uses to place simulated demand), not just the 2 terminal hubs
-- a real regional fleet's idle trucks are scattered near their last drop-off, not all parked at
the yard. Caught directly from a live demo: seeding everyone at 1-of-2 hubs made most candidates
on a given quote score IDENTICALLY (same deadhead distance, same everything) and made map markers
visually stack on top of each other -- a demo-data realism gap, not a scoring bug (the model was
always differentiating correctly on whatever real inputs it was given).
"""
import random
import uuid
from datetime import datetime, timedelta, timezone

from sim.db import cursor
from sim.engine.run_sim import driver_home_hub_id, load_sim_data
from sim.hub_weights import BARRIE_SHARE, LONDON_SHARE, MILTON_SHARE, hub_quotas

FLEET_SIZE = 20  # real user ask: a smaller, more legible demo fleet -- orders/day (~40-57,
# calibration.order_arrival_rate's own real weekday integral) comfortably exceeds this now,
# a real >2:1 ratio, instead of the near-1:1 ratio a 30-truck fleet left the optimizer with.

# Real user ask: a small demo fleet drawn purely by driver_id order can easily end up with ZERO
# Reefer/Flatbed trucks by pure chance -- real company-wide share is only ~4.4% Reefer, ~10.4%
# Flatbed (ground_truth.historical_orders load_type counts, checked directly: 92/215/1763 of
# 2,070 total) -- expected count in a 20-truck sample is under 1 for Reefer. Floored, not left to
# chance, same "real proportion, but with a floor so small regions/types aren't literally zero"
# pattern already used for order geography (sim/live/generate_dispatch_day.py) and hub shares
# (sim/calibrate_truck_profile.py) elsewhere in this project.
MIN_REEFER_TRUCKS = 2
MIN_FLATBED_TRUCKS = 2


def _fleet_candidates(cur) -> tuple[dict[str, list[tuple[int, str, str, bool]]], dict[str, str], list[tuple[int, str, str, bool]]]:
    """Shared step behind both _build_fleet_roster() (the fixed default 55/30/15 fleet used by
    live-ops seeding) and _build_fleet_roster_custom() (the Dispatch Board's Setup-panel-driven
    fleet, real user ask -- pick hub/type counts directly instead of the app deciding them):
    builds the full pool of (driver_id, truck_number, hub, is_real_pair) candidates -- real
    driver_equipment pairs preferred, hub-mismatched real pairs dropped and same-hub synthesized
    instead (see the real bug this fixed, below) -- bucketed by hub, plus a truck_number ->
    truck_type lookup. Both callers select FROM this same pool so "which real pairs/hubs exist"
    is computed once, one real way, no matter which selection policy runs on top of it.
    """
    cur.execute('select driver_id, truck_number from ground_truth.driver_equipment where truck_number is not null order by driver_id')
    real_pairs = dict(cur.fetchall())

    cur.execute('select driver_id from ground_truth.drivers order by driver_id')
    all_driver_ids = [r[0] for r in cur.fetchall()]

    cur.execute('select truck_number from ground_truth.trucks order by truck_number')
    all_truck_numbers = [r[0] for r in cur.fetchall()]

    cur.execute("""
        select dhh.driver_id, hub.city from calibration.driver_home_hub dhh
        join reference.locations hub on hub.location_id = dhh.hub_location_id
    """)
    driver_hub = dict(cur.fetchall())

    cur.execute("""
        select tp.truck_number, tp.truck_type, hub.city from calibration.truck_profile tp
        join reference.locations hub on hub.location_id = tp.home_hub_location_id
    """)
    truck_type: dict[str, str] = {}
    truck_hub: dict[str, str] = {}
    for truck_number, ttype, hub in cur.fetchall():
        truck_type[truck_number] = ttype
        truck_hub[truck_number] = hub

    used_trucks = set(real_pairs.values())
    unclaimed_trucks = [t for t in all_truck_numbers if t not in used_trucks]
    drivers_without_pair = [d for d in all_driver_ids if d not in real_pairs]

    if not driver_hub or not truck_hub:
        # calibration tables not seeded yet -- degrade to a single unhubbed bucket rather than
        # crash (matches the old plain-fill fallback); callers' quota logic just won't find any
        # named hub and will fall through to their own leftover-fill path.
        synthesized_pairs = dict(zip(drivers_without_pair, unclaimed_trucks))
        all_pairs = {**real_pairs, **synthesized_pairs}
        candidates = [(d, t, None, d in real_pairs) for d, t in all_pairs.items()]
        return {}, truck_type, candidates

    # Real bug found directly (checked, not assumed): a truck's calibrated home hub
    # (calibration.truck_profile) is drawn INDEPENDENTLY of its real driver's own calibrated hub
    # (calibration.driver_home_hub) -- two separate random draws with no relationship to each
    # other, so a driver correctly picked for a hub-quota slot could still end up paired with a
    # truck the dispatch board displays as a DIFFERENT hub. Real driver_equipment pairs that
    # disagree are dropped here (not overridden -- a real pair with a fabricated hub relabeled on
    # top of it isn't more real than a synthesized one); those drivers get a same-hub SYNTHESIZED
    # truck instead, chosen to match, not just the next truck_number in line.
    real_pairs = {d: t for d, t in real_pairs.items() if driver_hub.get(d) == truck_hub.get(t)}
    used_trucks = set(real_pairs.values())
    unclaimed_trucks = [t for t in all_truck_numbers if t not in used_trucks]
    drivers_without_pair = [d for d in all_driver_ids if d not in real_pairs]

    trucks_by_hub: dict[str, list[str]] = {}
    for t in unclaimed_trucks:
        trucks_by_hub.setdefault(truck_hub.get(t), []).append(t)
    synthesized_pairs: dict[int, str] = {}
    for d in drivers_without_pair:
        pool = trucks_by_hub.get(driver_hub.get(d)) or unclaimed_trucks  # same-hub if any exist, else any leftover
        if not pool:
            continue
        t = pool.pop(0)
        if t in unclaimed_trucks:
            unclaimed_trucks.remove(t)
        synthesized_pairs[d] = t

    # (driver_id, truck_number, hub, is_real_pair) for every one of the 131 real drivers.
    all_pairs = {**real_pairs, **synthesized_pairs}
    candidates = [
        (d, t, driver_hub.get(d), d in real_pairs)
        for d, t in all_pairs.items()
        if driver_hub.get(d) is not None
    ]
    by_hub: dict[str, list[tuple[int, str, str, bool]]] = {}
    for c in candidates:
        by_hub.setdefault(c[2], []).append(c)
    for hub_bucket in by_hub.values():
        hub_bucket.sort(key=lambda c: (not c[3], c[0]))  # real pairs first, then by driver_id
    return by_hub, truck_type, candidates


def _build_fleet_roster(cur) -> tuple[list[int], dict[int, str]]:
    """Real user correction: taking ALL real ground_truth.driver_equipment pairs unconditionally
    (the old behavior) meant the demo fleet's HUB mix was whatever those ~18 real pairs' hubs
    happened to be, regardless of the discussed Milton/London/Barrie target -- real driver_equipment
    is itself heavily Milton-concentrated (matches the real historical fleet's own operating
    pattern), so a demo fleet built that way could show near-zero London or Barrie trucks even
    after calibration.driver_home_hub's OWN population-level proportions were fixed. This now
    selects the FLEET_SIZE roster by HUB QUOTA first (sim/hub_weights.py's fixed Milton 55/
    London 30/Barrie 15 shares, applied to the SMALL demo fleet directly, not just the full
    131-driver population), preferring real driver_equipment pairs within each hub bucket where
    available, then floors truck TYPE (Reefer/Flatbed) via a same-hub swap so the fleet has real
    equipment diversity too -- see MIN_REEFER_TRUCKS/MIN_FLATBED_TRUCKS above. This is the fixed,
    no-controls fleet every OTHER caller (live-ops seeding, seed_demo_accounts.py) still uses; the
    Dispatch Board's own Setup panel calls _build_fleet_roster_custom() below instead.
    """
    by_hub, truck_type, candidates = _fleet_candidates(cur)
    if not by_hub:
        pair_map = {d: t for d, t, _h, _r in candidates}
        driver_ids = sorted(pair_map)[:FLEET_SIZE]
        return driver_ids, {d: pair_map[d] for d in driver_ids}

    quotas = hub_quotas(FLEET_SIZE, {'Milton': MILTON_SHARE, 'London': LONDON_SHARE, 'Barrie': BARRIE_SHARE})
    selected = [c for hub, n in quotas.items() for c in by_hub.get(hub, [])[:n]]
    shortfall = FLEET_SIZE - len(selected)
    if shortfall > 0:  # a hub's real candidate pool ran dry -- fill from whoever's left, any hub
        chosen_ids = {c[0] for c in selected}
        leftover = sorted((c for c in candidates if c[0] not in chosen_ids), key=lambda c: (not c[3], c[0]))
        selected.extend(leftover[:shortfall])

    selected = _type_floored_swap(selected, by_hub, truck_type)

    driver_ids = sorted(c[0] for c in selected)
    return driver_ids, {c[0]: c[1] for c in selected}


def _build_fleet_roster_custom(
    cur, hub_counts: dict[str, int], type_shares: dict[str, float],
) -> tuple[list[int], dict[int, str], dict[str, int], dict[str, int]]:
    """The Dispatch Board's Setup-panel fleet: an exact per-hub team count (`hub_counts`, e.g.
    {'Milton': 11, 'London': 6, 'Barrie': 3}) and a target truck-type mix (`type_shares`, e.g.
    {'Dry Van': 0.70, 'Reefer': 0.25, 'Flatbed': 0.05}) picked by the user, not a fixed 20/55/30/15
    the app decides -- real user ask, to simplify and put the demo's scale/mix directly in their
    hands for this POC instead of buried in calibration scripts. Selects real driver/truck pairs
    from the SAME pool _build_fleet_roster() uses (_fleet_candidates()).

    Type quota is computed ONCE, GLOBALLY (hub_quotas(total, type_shares)), not per hub -- real bug
    found directly: rounding a small share (5% Flatbed) independently within each small hub bucket
    (11/6/3 for a 20-truck fleet) rounds DOWN to zero in every single hub, so a 5% target could
    never actually appear no matter how many times Simulate ran, even though the population clearly
    has real Flatbed trucks to give it. Computing the quota over the whole fleet first, then placing
    those slots into whichever hub still has room and a real candidate of that type (scarcest type
    first, so a thin type isn't crowded out by a bigger one claiming every hub first), fixes that
    while still hitting each hub's own count exactly. Real equipment inventory is still finite and
    hub-skewed (checked directly: Barrie currently has zero real Flatbed trucks) -- if a requested
    mix asks for more of a type than real inventory can supply anywhere, this backfills same-hub-
    any-type, then any-hub-any-type, and returns the ACTUAL hub/type counts achieved so the caller
    can tell the user plainly when a request couldn't be fully honored, not claim it silently was.
    """
    by_hub, truck_type, candidates = _fleet_candidates(cur)
    total_requested = sum(hub_counts.values())

    if not by_hub:  # calibration not seeded -- no hub/type signal to quota against at all
        pair_map = {d: t for d, t, _h, _r in candidates}
        driver_ids = sorted(pair_map)[:total_requested]
        driver_trucks = {d: pair_map[d] for d in driver_ids}
        return driver_ids, driver_trucks, {}, {}

    remaining_hub_slots = {hub: n for hub, n in hub_counts.items() if n > 0}
    selected: list[tuple[int, str, str, bool]] = []
    selected_ids: set[int] = set()

    global_type_quota = hub_quotas(total_requested, type_shares) if type_shares else {}
    for t in sorted(global_type_quota, key=lambda t: global_type_quota[t]):  # scarcest type first
        want = global_type_quota[t]
        got = 0
        for hub in sorted(remaining_hub_slots, key=lambda h: -remaining_hub_slots[h]):
            if got >= want or remaining_hub_slots[hub] <= 0:
                continue
            cands = sorted(
                (c for c in by_hub.get(hub, []) if truck_type.get(c[1]) == t and c[0] not in selected_ids),
                key=lambda c: (not c[3], c[0]),
            )
            take = min(len(cands), remaining_hub_slots[hub], want - got)
            for c in cands[:take]:
                selected.append(c)
                selected_ids.add(c[0])
                remaining_hub_slots[hub] -= 1
                got += 1

    # Any hub still short (its type-preferred candidates ran out, or type_shares was empty) --
    # fill with whatever's left in that SAME hub, any type, before falling back further.
    for hub, n_left in list(remaining_hub_slots.items()):
        if n_left <= 0:
            continue
        leftover = sorted(
            (c for c in by_hub.get(hub, []) if c[0] not in selected_ids),
            key=lambda c: (not c[3], c[0]),
        )
        for c in leftover[:n_left]:
            selected.append(c)
            selected_ids.add(c[0])
            remaining_hub_slots[hub] -= 1

    if len(selected) < total_requested:  # a whole hub ran short of real candidates -- fill from any hub
        leftover = sorted((c for c in candidates if c[0] not in selected_ids), key=lambda c: (not c[3], c[0]))
        selected.extend(leftover[:total_requested - len(selected)])

    driver_ids = sorted(c[0] for c in selected)
    driver_trucks = {c[0]: c[1] for c in selected}
    actual_hub_counts: dict[str, int] = {}
    actual_type_counts: dict[str, int] = {}
    for c in selected:
        actual_hub_counts[c[2]] = actual_hub_counts.get(c[2], 0) + 1
        t = truck_type.get(c[1], 'Dry Van')
        actual_type_counts[t] = actual_type_counts.get(t, 0) + 1
    return driver_ids, driver_trucks, actual_hub_counts, actual_type_counts


def _type_floored_swap(
    selected: list[tuple[int, str, str, bool]], by_hub: dict[str, list], truck_type: dict[str, str],
) -> list[tuple[int, str, str, bool]]:
    """Guarantees at least MIN_REEFER_TRUCKS/MIN_FLATBED_TRUCKS of each real type in the final
    fleet -- real company-wide share is only ~4.4% Reefer, ~10.4% Flatbed (ground_truth.
    historical_orders load_type counts: 92/215/1763 of 2,070), so a small demo fleet chosen by hub
    quota alone can still land on zero of either by chance. Swaps a same-hub Dry Van pick for an
    available Reefer/Flatbed candidate that DIDN'T make the hub-quota cut, preserving both the hub
    quota (swap stays within the same hub) and, where possible, real driver_equipment pairs
    (prefers swapping out a synthesized pick before a real one)."""
    selected = list(selected)
    for want_type, floor in (('Reefer', MIN_REEFER_TRUCKS), ('Flatbed', MIN_FLATBED_TRUCKS)):
        have = sum(1 for c in selected if truck_type.get(c[1]) == want_type)
        selected_ids = {c[0] for c in selected}
        for hub_bucket in by_hub.values():
            if have >= floor:
                break
            candidates_of_type = [c for c in hub_bucket if truck_type.get(c[1]) == want_type and c[0] not in selected_ids]
            for candidate in candidates_of_type:
                if have >= floor:
                    break
                # Swap out this hub's worst current pick (synthesized over real, Dry Van already
                # counted) that isn't itself a floor-protected Reefer/Flatbed pick.
                same_hub_swappable = [
                    c for c in selected if c[2] == candidate[2] and truck_type.get(c[1]) not in ('Reefer', 'Flatbed')
                ]
                if not same_hub_swappable:
                    continue
                same_hub_swappable.sort(key=lambda c: c[3])  # synthesized (False) before real (True)
                out = same_hub_swappable[0]
                selected.remove(out)
                selected.append(candidate)
                selected_ids.discard(out[0])
                selected_ids.add(candidate[0])
                have += 1
    return selected


# MID_ROUTE/HUB shares kept proportional to the original 8-driver seed (2/8 mid-route, 1/8 hub).
MID_ROUTE_SHARE = 2 / 8
HUB_SHARE = 1 / 8


def _weighted_location_pool(data) -> list[int]:
    """Real lane endpoints, weighted by real historical frequency (calibration.lane_frequency,
    already loaded into data.lane_weights) -- the same realism source run_sim.py draws simulated
    demand from, reused here for realistic idle-driver positions instead of picking arbitrary or
    hub-only spots."""
    pool = []
    for origin_id, dest_id, weight in data.lane_weights:
        n = max(1, round(weight))
        pool.extend([origin_id, dest_id] * n)
    return pool


def seed(seed_value: int = 11):
    rng = random.Random(seed_value)
    data = load_sim_data()
    now = datetime.now(timezone.utc)
    weighted_pool = _weighted_location_pool(data)
    hub_ids = list(data.hub_ids.values())

    with cursor() as cur:
        cur.execute("truncate table live.driver_status cascade")
        cur.execute("truncate table live.trips cascade")
        cur.execute("truncate table live.truck_maintenance_state cascade")
        cur.execute("truncate table live.vehicle_inspections cascade")
        cur.execute("truncate table live.position_history cascade")
        # Real bug this caused: quote_requests/quote_recommendations/geofence_events/trip_log/
        # trip_costs/invoices/detention_billing all reference trip_id or get their lifecycle
        # driven by live.trips -- truncating trips alone left every OTHER table's rows orphaned
        # (a quote_requests.status='assigned' row pointing at a trip_id that no longer existed),
        # which silently broke the Orders/Feed pages (their trip lookup just came back empty).
        # A reseed has to reset the WHOLE live.* dataset together, not just the fleet tables.
        cur.execute("truncate table live.quote_recommendations cascade")
        cur.execute("truncate table live.quote_requests cascade")
        cur.execute("truncate table live.geofence_events cascade")
        cur.execute("truncate table live.geofence_dwell_state cascade")
        cur.execute("truncate table live.detention_billing cascade")
        cur.execute("truncate table live.trip_costs cascade")
        cur.execute("truncate table live.invoices cascade")
        cur.execute("truncate table live.trip_log cascade")
        cur.execute("truncate table live.quote_candidate_snapshots cascade")
        cur.execute("truncate table live.driver_state_snapshots cascade")

        demo_driver_ids, driver_trucks = _build_fleet_roster(cur)
        n_mid_route = round(len(demo_driver_ids) * MID_ROUTE_SHARE)
        n_hub = round(len(demo_driver_ids) * HUB_SHARE)
        mid_route_driver_ids = set(rng.sample(demo_driver_ids, n_mid_route))
        hub_driver_ids = set(rng.sample([d for d in demo_driver_ids if d not in mid_route_driver_ids], n_hub))

        for driver_id in demo_driver_ids:
            truck_number = driver_trucks[driver_id]
            is_mid_route = driver_id in mid_route_driver_ids
            # Wider real spread (2-13h, not 6-13h) -- some drivers genuinely close to their HOS
            # limit, matching a real fleet snapshot at a random moment, not an artificially
            # comfortable one that never triggers the HOS-risk term.
            hos_remaining = rng.uniform(2.0, 13.0)

            # Home-base-return retarget (sim/sql/042, documents/logs/23-24): real, DISTINCT
            # starting values for all 4 HOS sub-clocks, not the single blended figure copy-pasted
            # 4 times (that was the exact gap this migration closes -- see the migration's own
            # comment). hos_remaining above is already the binding (smallest) constraint in this
            # 2-13h range, so it seeds the daily driving clock directly; duty gets a small real
            # buffer above it (duty >= driving is always true for a real driver); the two CYCLE
            # clocks (70h/7d, 120h/14d) are sampled independently and WIDER, at or above the daily
            # figure -- matching a real fleet where the weekly/bi-weekly cycle is only occasionally
            # the binding constraint (log 24's own measured finding on the real calibrated batch),
            # not tied to the daily draw. No qualifying-reset detection is modeled here (documents/
            # logs/23's own scoped-out item) -- these are independent starting snapshots, decremented
            # by real elapsed on-duty time going forward (telemetry_simulator.py), same honest
            # simplification the existing blended hos_remaining_hours already carried.
            hos_driving_remaining = hos_remaining
            hos_duty_remaining = min(14.0, hos_remaining + rng.uniform(0.0, 1.0))
            hos_cycle1_remaining = min(70.0, hos_remaining + rng.uniform(10.0, 55.0))
            hos_cycle2_remaining = min(120.0, hos_cycle1_remaining + rng.uniform(10.0, 45.0))

            # Healthy-biased maintenance state -- matches sim/engine/maintenance.py's
            # initialize_fleet() distribution (documents/logs/18), most trucks well under interval.
            pct = rng.uniform(0.05, 0.7)
            cum_km = pct * 25000
            days_since = rng.uniform(0.05, 0.7) * 90
            last_service_at = now - timedelta(days=days_since)
            cur.execute("""
                insert into live.truck_maintenance_state
                  (truck_number, cumulative_km_since_service, last_service_at, service_interval_km, service_interval_days)
                values (%s, %s, %s, 25000, 90)
                on conflict (truck_number) do update set
                  cumulative_km_since_service = excluded.cumulative_km_since_service,
                  last_service_at = excluded.last_service_at
            """, (truck_number, cum_km, last_service_at))

            # Passing DVIR on file as of this morning -- see module docstring. odometer_km baseline
            # reused for live.driver_status below too, so both reflect the same starting figure.
            odometer_km = rng.uniform(80000, 300000)
            cur.execute("""
                insert into live.vehicle_inspections
                  (driver_id, truck_number, submitted_at, odometer_km,
                   brakes_ok, tires_ok, lights_ok, fluid_levels_ok, coupling_ok, trailer_ok)
                values (%s, %s, %s, %s, true, true, true, true, true, true)
            """, (driver_id, truck_number, now - timedelta(hours=rng.uniform(1, 6)), odometer_km))

            # Home-time retarget (sim/sql/046, documents/logs/25): SYNTHESIZED -- no real "when did
            # this driver last leave home" data exists to seed from. A driver seeded exactly AT
            # their own home hub gets a recent, plausible arrival (they just got back); everyone
            # else gets a real spread of past departure times (up to 10 days), so a freshly-seeded
            # fleet shows genuine variation in home-time urgency from the first tick, not everyone
            # starting at 0.
            home_hub_id = driver_home_hub_id(data, driver_id)

            if not is_mid_route:
                loc_id = rng.choice(hub_ids) if driver_id in hub_driver_ids else rng.choice(weighted_pool)
                last_home_arrival_at = (
                    now - timedelta(hours=rng.uniform(0.5, 6)) if loc_id == home_hub_id
                    else now - timedelta(hours=rng.uniform(6, 24 * 10))
                )
                cur.execute("""
                    insert into live.driver_status
                      (driver_id, updated_at, last_location_id, hos_remaining_hours, duty_status,
                       current_trip_id, truck_number, trailer_type, trailer_capacity_lbs, trailer_capacity_pallets,
                       odometer_km, fuel_pct, hos_driving_hours_remaining, hos_duty_hours_remaining,
                       hos_cycle1_hours_remaining, hos_cycle2_hours_remaining, last_home_arrival_at)
                    values (%s, %s, %s, %s, 'off_duty', null, %s, 'Dry Van', 44500, 26, %s, %s, %s, %s, %s, %s, %s)
                """, (driver_id, now, loc_id, hos_remaining, truck_number, odometer_km, rng.uniform(55, 100),
                      hos_driving_remaining, hos_duty_remaining, hos_cycle1_remaining, hos_cycle2_remaining,
                      last_home_arrival_at))
            else:
                trip_id = uuid.uuid4()
                origin_loc = rng.choice(weighted_pool)
                dest_loc = rng.choice(weighted_pool)
                eta = now + timedelta(hours=rng.uniform(0.5, 4.0))
                projected_hos = max(0.0, hos_remaining - rng.uniform(1.0, 3.0))
                last_home_arrival_at = now - timedelta(hours=rng.uniform(6, 24 * 10))  # mid-route -- not home right now
                cur.execute("""
                    insert into live.driver_status
                      (driver_id, updated_at, last_location_id, hos_remaining_hours, duty_status,
                       current_trip_id, truck_number, trailer_type, trailer_capacity_lbs, trailer_capacity_pallets,
                       odometer_km, fuel_pct, hos_driving_hours_remaining, hos_duty_hours_remaining,
                       hos_cycle1_hours_remaining, hos_cycle2_hours_remaining, last_home_arrival_at)
                    values (%s, %s, null, %s, 'driving', %s, %s, 'Dry Van', 44500, 26, %s, %s, %s, %s, %s, %s, %s)
                """, (driver_id, now, hos_remaining, trip_id, truck_number, odometer_km, rng.uniform(40, 90),
                      hos_driving_remaining, hos_duty_remaining, hos_cycle1_remaining, hos_cycle2_remaining,
                      last_home_arrival_at))
                # status='in_transit' (not the sim schema's legacy 'in_progress') -- this is the
                # exact vocabulary sim/live/telemetry_simulator.py's lifecycle drives, so a
                # freshly-seeded mid-route driver is immediately picked up and moved for real.
                cur.execute("""
                    insert into live.trips
                      (trip_id, driver_id, status, last_event, eta, origin_location_id, dest_location_id,
                       created_at, weight_lbs, pallets, load_type, pre_pickup_deadhead_miles,
                       projected_hos_remaining_hours, projected_truck_pct_km_interval, projected_truck_pct_days_interval)
                    values (%s, %s, 'in_transit', 'DEPSHIP', %s, %s, %s, %s, %s, %s, 'Dry Van', 0, %s, %s, %s)
                """, (trip_id, driver_id, eta, origin_loc, dest_loc, now - timedelta(hours=1),
                      rng.uniform(8000, 30000), rng.randint(4, 20),
                      projected_hos, pct, days_since / 90))

    print(f'Seeded {len(demo_driver_ids)} demo drivers ({len(demo_driver_ids) - len(mid_route_driver_ids)} idle, '
          f'{len(mid_route_driver_ids)} mid-route, scattered across real weighted locations) into live.* -- '
          f'driver_ids {demo_driver_ids}')


if __name__ == '__main__':
    seed()
