# Feature & Reward Reference — Training and Live Inference

**Audience:** a session building/retraining the model (`sim/training/`) and a session wiring live
quote inference (`sim/live/score_quote.py` and whatever calls it from the dashboard). This is the
single source of truth for what each feature means, how it's calculated, where it comes from at
TRAINING time (sim) vs. INFERENCE time (live), and — the part that needs the most care — how a
candidate's state must be *projected forward* at inference time, not just read live, because a
quote can be for a future pickup date and a truck can already be booked on a future trip before
that date.

Read `documents/logs/23_home_base_return_gap_found.md` and
`documents/logs/NEW_SESSION_TRAINING_RETARGET_PROMPT.md` first for the gap this feature set closes
and the engineering plan (persistence, retraining, validation) around it. This document is the
feature-level reference that plan depends on — it doesn't repeat the "why," it defines the "what"
and "how to compute it," precisely enough that training and inference never silently diverge.

**One rule that matters more than any single feature below**: training and inference must compute
every feature the SAME way, from state that means the same thing. If the sim computes
`distance_to_own_home_hub_km` from a driver's simulated position and inference computes it from a
driver's *live-reported* position without applying the same future-projection logic the sim
implicitly gets for free (because the sim is a real forward-time event loop), the model sees
different distributions at inference than it was trained on and its scores stop being meaningful.
Section 4 exists because of exactly this risk.

---

## 1. Hard constraints (not learned, not features of `V(s)`)

These gate which candidates are even scored. Unchanged by this feature set.

| Check | Definition | Source |
|---|---|---|
| HOS legality | `HOSState.can_perform(planned_driving_hours, planned_duty_hours)` — true only if the candidate's planned trip fits under ALL 5 real HOS limits | [`sim/engine/hos.py:154-164`](sim/engine/hos.py#L154-L164) |
| Capacity | `weight_lbs`/`pallets` vs. the truck's `capacity_lbs`/`capacity_pallets` (by `load_type`) | `sim/config.py`'s `CAPACITY_BY_LOAD_TYPE`/`CAPACITY_PALLETS_BY_LOAD_TYPE` |
| DVIR (live only) | A driver without a PASSING inspection in the last 24h is excluded before scoring | [`sim/live/score_quote.py:136-144`](sim/live/score_quote.py#L136-L144) |
| Truck in shop (live only) | `truck_maintenance_until > now` excludes the truck | [`sim/live/score_quote.py:197-198`](sim/live/score_quote.py#L197-L198) |

---

## 2. HOS features — the 5 real clocks, and what each is used for

Canada's HOS has two different kinds of limit. Get this distinction right — it's the basis for
`hos_urgency` in Section 3.

| Clock | Resets via | Used in hard filter | Used in `hos_stranding_risk_penalty` (existing) | Used in `hos_urgency` (NEW) |
|---|---|---|---|---|
| `remaining_driving_hours` (13h) | 10h+ off-duty block | Yes | Yes (via blended `remaining_hours`) | **No** |
| `remaining_duty_hours` (14h) | 10h+ off-duty block | Yes | Yes | **No** |
| `remaining_elapsed_window_hours` (16h) | 10h+ off-duty block | Yes | Yes | **No** |
| `remaining_cycle1_hours` (70h/7-day) | Only a qualifying long reset | Yes | Yes (blended) | **Yes** |
| `remaining_cycle2_hours` (120h/14-day) | Only a qualifying long reset | Yes | Yes (blended) | **Yes** |

**Why the daily clocks (13h/14h/16h) are deliberately excluded from `hos_urgency`**: hitting a
daily limit far from home is not real stranding — the driver takes the mandatory rest and
continues tomorrow, as long as the *cycle* budget still allows eventually getting home. It's
already fully priced by two existing mechanisms (the hard filter, and the within-trip
`hos_stranding_risk_penalty`, both of which already use the blended `remaining_hours` = min across
all 5 clocks, daily and cycle alike). Only the cycle clocks can produce genuine multi-day
stranding, so only they drive `hos_urgency`. Do not fold the daily clocks into `hos_urgency` too —
that would double-count a risk already priced elsewhere under a different name.

**Calculation, both contexts:**

```python
# Training (sim): HOSLog.snapshot(now) already computes all 5 explicitly.
hos_state = driver_hos_log.snapshot(now)
remaining_cycle1_hours = hos_state.remaining_cycle1_hours
remaining_cycle2_hours = hos_state.remaining_cycle2_hours

# Inference (live): NOT currently available. live.driver_status.hos_remaining_hours is ONE
# blended number today (score_quote.py's _make_hos_state() reconstructs all 5 dimensions as
# equal to it — conservative for feasibility, but CANNOT support hos_urgency, which specifically
# needs the cycle clocks separated from the daily ones). This is a real schema gap:
# live.driver_status needs remaining_cycle1_hours/remaining_cycle2_hours as their own columns,
# computed from the driver's real duty-status history the same way HOSLog.snapshot() does, not
# derived from the single blended figure (you cannot recover 2 numbers from their min).
```

**Action needed before this is inference-ready**: add `remaining_cycle1_hours`,
`remaining_cycle2_hours` to `live.driver_status` (or compute them on read from a real duty-status
log table, if one exists/gets built) — the single blended `hos_remaining_hours` is structurally
insufficient for this feature, not just currently unpopulated.

---

## 3. Position / home-progress features (NEW this pass)

| Feature | Definition | Calculation | Cardinality |
|---|---|---|---|
| `distance_to_own_home_hub_km` (current) | Distance from the driver's position **right now** (decision time) to **their own** home terminal (not nearest-any-hub) | `_haversine_km(current_position, data.locations[driver_terminal_zone[driver_id]])` — same helper `dest_distance_to_hub_km` already uses, but keyed to the driver's OWN terminal, not `min()` over all hubs | One value per driver per decision (shared by every candidate being compared) |
| `distance_to_own_home_hub_km` (candidate landing) | Same distance, but from where THIS candidate would leave the driver after completing the trip being scored | `_haversine_km(order.dest_location_id's position, driver's home hub)` | One value **per candidate** (each candidate's destination differs) |
| `closing_distance_km` | Signed delta: positive = this candidate moves the driver closer to home; negative = further away | `distance_to_own_home_hub_km(current) − distance_to_own_home_hub_km(candidate landing)` | Per candidate |
| `hos_urgency` | 0–1: how urgently the driver needs to head home before the cycle binds | See formula below | Per driver per decision (same cardinality as `distance...current`) |
| `truck_type` | Dry Van / Reefer / Flatbed | Real column, already exists on the truck/trailer record | Per driver |

**`hos_urgency` formula** (needs empirical validation of `avg_speed_kmh`/`safety_buffer_hours`
before trusting the exact ramp shape — same standard as `documents/logs/17`/`18`, propose then
measure, don't hardcode blind):

```python
remaining_cycle_hours = min(remaining_cycle1_hours, remaining_cycle2_hours)
hours_needed_home = distance_to_own_home_hub_km / avg_speed_kmh   # avg_speed_kmh: use a real
    # network-derived average (e.g. from calibration.lane_routes), not a guessed constant
margin_ratio = remaining_cycle_hours / (hours_needed_home + safety_buffer_hours)
hos_urgency = max(0.0, min(1.0, 1 - margin_ratio))
```

**Training-time source**: computed directly from `SimData`/`HOSLog`/driver position, same module
as everything else in `run_sim.py`.

**Inference-time source**: `distance_to_own_home_hub_km (current)` needs the driver's real current
(or *effectively* current — see Section 4) position and `driver_terminal_zone`-equivalent live
column (does `live.driver_status` or `ground_truth.drivers` carry a home-terminal field reachable
from a live driver row? **Verify this before building** — if not, it needs adding, mirroring how
`ground_truth.drivers.home_zone`/`terminal_zone` already exists for the sim side).

---

## 4. THE CRITICAL PART: computing features at inference time when a truck is already booked on a future trip

### The problem, precisely

`sim/live/score_quote.py`'s `build_candidates()` ([score_quote.py:189-236](sim/live/score_quote.py#L189-L236))
today only knows about **one** trip per driver: `LiveDriverRow.current_trip_id`, joined against
`live.trips` for exactly that one row ([score_quote.py:124-145](sim/live/score_quote.py#L124-L145)).
It already handles the "mid-route" case correctly — a driver currently IN PROGRESS on a trip is
scored from their PROJECTED landing state (`trip_dest_location_id`, `trip_eta`,
`trip_projected_hos_remaining`, etc.), not their stale current position. That's the right pattern.

**What it does NOT handle**: a driver who is idle *right now*, or mid-route on trip A, but who
ALSO already has a future trip B booked (quoted, assigned, not yet started) that will occur
between now and the new quote's relevant decision time. If trip B exists and would still be
in-progress (or would have just landed the driver somewhere specific) at the moment this NEW quote
needs to be evaluated, the correct candidate state is the driver's position/HOS/truck-condition
**after trip B**, not their live-right-now state and not even just "after their current trip."
Scoring from the wrong state here isn't a rounding error — it can offer a truck that's actually
unavailable, or price a deadhead/ETA against the wrong starting position entirely.

This matters specifically BECAUSE quotes have real lead time (`requested_pickup_at` can be days
out, per `documents/logs/21`'s whole fix) — the longer the lead time on a new quote, the more
likely it is that OTHER trips get booked onto a truck in the interim, and the model needs to
reason about the driver's state *at the time this new trip would actually happen*, which may be
after a whole chain of already-committed trips, not just whatever's live right now.

### The fix: a trip-chain projection function, not a single current-trip join

Generalize the existing single-hop "mid-route -> projected landing state" pattern
(`effective_driver_state()` in `run_sim.py`, mirrored in `build_candidates()` above) into a
**multi-hop chain walk**:

```python
def project_driver_state(driver_id: str, as_of: datetime) -> ProjectedState | None:
    """Walks a driver's REAL trip queue (current in-progress trip, if any, plus every already-
    booked FUTURE trip not yet started, ordered by planned start time) forward from `now` to
    `as_of`, applying each trip's known/projected effect on position, HOS, and truck condition in
    sequence. Returns the driver's state AT `as_of`, or None if the driver is still committed to a
    trip that would still be in progress AT `as_of` (genuinely not available for a NEW assignment
    at that time -- a real infeasibility, not a bug to work around).

    This is the SAME principle as run_sim.py's effective_driver_state()/build_candidates()'s
    mid-route branch, just chained across N trips instead of stopping after one.
    """
    trips = load_driver_trip_queue(driver_id)  # ALL trips with status in
        # ('IN_PROGRESS', 'SCHEDULED'/'ASSIGNED'), ordered by planned_pickup_at -- see schema
        # note below, this query does not exist yet in the current live schema/code.
    state = current_live_state(driver_id)      # position, hos_remaining, truck condition, right now
    t = now()
    for trip in trips:
        if trip.planned_start_at > as_of:
            break  # this trip and everything after it hasn't started by as_of -- stop here
        if trip.projected_landing_at > as_of:
            return None  # driver would still be mid-trip AT as_of -- not a valid candidate for as_of
        state = apply_trip_projection(state, trip)  # advance position/HOS/truck-condition through
                                                       # this one committed trip, same math
                                                       # effective_driver_state() already uses for one hop
        t = trip.projected_landing_at
    return state
```

`as_of` here should be the NEW quote's `order.decision_time` (or, for candidate-availability
purposes, effectively the point at which the driver needs to be free to start the new pickup —
these may not be identical; confirm against how `decision_time`/`effective_start` are already used
in `score_candidate()`, [policy.py:67-72](sim/engine/policy.py#L67-L72), before assuming which one
applies here).

### Schema gap this exposes — verify and likely extend before building

`live.trips` and `LiveDriverRow` as they exist today model **one** current trip per driver
(`live.driver_status.current_trip_id`). To support the chain above, the live schema needs a real
way to answer "give me every trip already committed to this driver that hasn't started yet,
ordered by when it will." Check first whether this already exists in some form (a `status` column
on `live.trips` with a `SCHEDULED`/`ASSIGNED` value distinct from `IN_PROGRESS`/`COMPLETE`, and
whether multiple rows per driver are already permitted) before assuming new schema is needed — but
go in expecting it probably needs at least: (a) confirming `live.trips` allows N future rows per
driver, not just the one `current_trip_id` pointer, (b) a query/view that returns a driver's full
forward queue ordered by planned start, (c) each future trip row carrying enough to project
through it (planned pickup/dest locations, planned duration, planned HOS consumption) even before
it starts — the same shape of projection data `sim/sql/029_add_live_projected_state.sql` already
added for the ONE current-trip case, generalized to every queued trip, not just the first one.

### Worked example

Driver 12, based at Milton. Right now (`t0`) they're idle at Milton. They already have trip B
booked: Milton → London, planned pickup `t0+6h`, projected landing `t0+14h`. A NEW quote comes in
at `t0` with `requested_pickup_at = t0+20h` (a real ~1-day-out booking).

- Naive (current) behavior: `build_candidates()` sees `current_trip_id IS NULL` (driver 12 isn't
  IN PROGRESS on anything right now) and scores them as **idle at Milton, right now** — wrong, the
  driver will actually be busy on trip B for the first 14 hours and won't be at Milton at
  `t0+20h`, they'll be at London (or wherever trip B's post-completion branch lands them).
- Correct behavior: `project_driver_state(12, as_of=t0+20h... )` walks through trip B (it starts
  at `t0+6h`, lands at `t0+14h`, both before the new quote's relevant decision point), returns the
  driver's state as of `t0+14h` (position = London or wherever, HOS reduced by trip B's duty
  hours, truck condition advanced by trip B's miles) — THAT state is what should feed
  `distance_to_own_home_hub_km`, `hos_urgency`, deadhead-to-new-pickup, and every other feature for
  scoring this driver against the new quote.

Getting this wrong doesn't just mis-price one candidate — it can make an already-booked-out driver
look falsely available, or falsely idle-at-the-wrong-place, both of which would produce a
real, wrong dispatch recommendation shown to a dispatcher.

---

## 5. Capacity/load features (NEW, LTL-conditional)

| Feature | Definition | FTL relevance | LTL relevance |
|---|---|---|---|
| `current_capacity_used_pct` | `load_fill_ratio()` of whatever the truck is ALREADY carrying, for a driver mid-route with room left | None — FTL is paid in full regardless of fill, so this should carry ~zero learned weight there | Real — drives whether taking on a compatible nearby order is worth it |
| `service_type` | FTL/LTL, already exists per order | Gates whether the row above matters at all | — |

These only become actionable once the mid-route insertion candidate type exists (flagged as a
separate, larger engineering item — see the exchange in this session's transcript on why it's a
candidate-*generation* change, not just a feature, and why a marginal insertion needs to be priced
against the ORIGINAL trip's lateness risk, not just gated by feasibility). Listed here for
completeness of the feature inventory, not as something `score_quote.py` needs today.

---

## 6. Full reward formula (target state, `sim/engine/reward.py`)

```
immediate_reward =
      order_revenue
    − deadhead_cost
    − opportunity_cost_penalty          (LTL only, existing)
    − hos_stranding_risk_penalty        (existing — within-trip margin risk, all 5 HOS clocks blended)
    − maintenance_risk_penalty          (existing)
    − expected_lateness_penalty         (existing)
    − cycle_end_stranding_penalty       (NEW — separate from hos_stranding_risk_penalty; fires on
                                          genuine multi-day cycle exhaustion far from home, not
                                          within-trip margin)
    + home_progress_bonus               (NEW — potential-based shaping)

home_progress_bonus:
    Φ(s)  = -hos_urgency(s) × distance_to_own_home_hub_km(s)
    home_progress_bonus = γ·Φ(s') − Φ(s)
```

`Φ` is a function of state only (never of the candidate/action directly) — required for the
Ng/Harada/Russell (1999) potential-based-shaping policy-invariance guarantee to actually hold.
`hos_urgency` is computed from state (HOS clocks + position), so this is satisfied.

---

## 7. Open items to verify before building inference — don't assume, check the real schema/code

1. Does `live.driver_status` (or any live table) carry a driver's home-terminal identifier reachable
   the way `ground_truth.drivers.home_zone`/`terminal_zone` does for the sim? If not, add it.
2. Does `live.trips` support multiple future (`SCHEDULED`/not-yet-started) rows per driver today,
   or only the single `current_trip_id` pointer `LiveDriverRow` currently reads? Check the actual
   table/constraints before assuming either way.
3. Do future-booked trips (quoted and assigned in advance) actually get written to `live.trips` at
   assignment time with enough projected-state data to walk through BEFORE they start (not just
   once they're `IN_PROGRESS`)? If assignment currently only creates a row once a trip begins,
   that's a real gap this feature set depends on closing.
4. `avg_speed_kmh` for `hos_urgency`'s `hours_needed_home` — pull a real network-derived average
   (e.g. from `calibration.lane_routes`) rather than a guessed constant.
5. `remaining_cycle1_hours`/`remaining_cycle2_hours` — confirm whether any live duty-status log
   exists to compute these from, or whether they need to be added as directly-maintained columns
   updated as a driver's duty status changes.

None of these are hard blockers to writing code against — they're the concrete list of "what to
check first" so inference isn't built against an assumed schema that turns out not to match reality.
