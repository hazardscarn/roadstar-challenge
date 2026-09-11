"""Wires the trained state-value model (sim/training/train_state_value_function.py) into a
`value_fn(candidate, order, now)` callable matching policy.py's expected signature -- the piece
that makes choose_assignment()'s `score = immediate_reward + gamma * value_fn(...)` a real
fitted-value score instead of the zero_value_fn stub.

Kept separate from policy.py (which stays free of any GPU/model-loading dependency) and separate
from run_sim.py (this module imports FROM run_sim -- dynamic_post_completion_probs, SimData,
_haversine_km -- not the other way around, so no import cycle).

Uses region_id (sim/cluster_locations.py's H3 hexagonal cells, not k-means -- see documents/logs/16
for why that switched), matching the retrained model -- NOT raw lat/lon (see sim/sql/021 and
documents/logs/14 for why raw coordinates were dropped in the first place).
"""
import pickle

import numpy as np
import xgboost as xgb

from sim.engine.policy import Candidate
from sim.engine.run_sim import (
    Order, SimData, _haversine_km, driver_home_hub_id, dynamic_post_completion_probs, get_route, nearest_region,
)

N_REGIONS = 25  # sim/cluster_locations.py's H3 res=4 cell count over the real 2,110 locations

# SHRINKAGE + CLIPPING -- documents/logs/18's regression finding: adding truck_pct_km/days_interval
# to V(s) produced a real, useful signal (Round-0 R^2 0.0032 -> 0.1445 on RAW target_value -- a
# genuine effect, see that log) but ALSO an extreme, near-binary cliff right at the ~85%
# breakdown-risk ramp threshold (V(s) swings from +234 to -4,543 crossing that one boundary -- a
# tree splitting sharply on a feature with relatively few training examples past that point, plus
# the flat $7,500 realized-breakdown-cost outliers concentrated there).
#
# Two real numbers make the domination problem concrete, not assumed: immediate_reward's real
# spread (training_transitions, greedy rows) is p10=-278.8 to p90=+221.5, std=203.3 -- V(s)'s OWN
# p10-p90 range is -1858.6 to +251 -- 5-7x WIDER even before gamma. Clipping the extreme tail
# alone (the earlier attempt in this fix) still left gamma*V(s') 15-50x larger than
# immediate_reward at the tail -- not enough. A real SHRINKAGE factor is needed first, scaling
# V(s')'s typical magnitude down to comparable size, THEN clip the residual tail as a backstop.
# This was CONFIRMED, not guessed, to cause a real, statistically significant regression (t=-4.68,
# trained losing by -$29.31/trip vs. greedy, paired eps=0 test) before this fix.
#
# V_SHRINKAGE_FACTOR: chosen so the shrunk p10-p90 spread of V(s) roughly matches
# immediate_reward's own p10-p90 spread (221.5 - (-278.8) = 500.3) rather than dwarfing it --
# (251 - (-1858.6)) / 500.3 ~= 4.2, so 1/4.2 ~= 0.24, rounded to a clean 0.25 (still leaves real
# headroom rather than shrinking to the point V(s') becomes a rounding error against
# immediate_reward -- the goal is comparable influence, not zero influence).
V_SHRINKAGE_FACTOR = 0.25
# Backstop clip on the SHRUNK value, at the shrunk p1/p99 (not the raw ones) -- a safety bound
# against any future retrain producing an even sharper cliff than this one.
V_CLIP_MIN = -2711.3 * V_SHRINKAGE_FACTOR  # raw p5 x shrinkage
V_CLIP_MAX = 351.3 * V_SHRINKAGE_FACTOR    # raw p99.5 x shrinkage


def load_state_value_model(path: str) -> tuple[xgb.Booster, list[str]]:
    with open(path, 'rb') as f:
        saved = pickle.load(f)
    booster = saved['booster']
    # Single-row predictions gain nothing from internal multithreading -- and left at its
    # trained default, each of run_batch.py's worker PROCESSES would also spawn multiple
    # internal XGBoost threads, oversubscribing the machine's cores many times over once
    # several workers run concurrently. One thread per call is the right setting here.
    booster.set_param({'nthread': 1, 'device': 'cpu'})
    return booster, saved['feature_columns']


def _predict_state_value(
    booster: xgb.Booster, feature_columns: list[str], region_id: int, hos_remaining: float,
    hour: int, dow: int, truck_pct_km_interval: float, truck_pct_days_interval: float,
    hos_cycle1_remaining: float | None = None, hos_cycle2_remaining: float | None = None,
    distance_to_home_miles: float | None = None, hours_since_home: float | None = None,
) -> float:
    """Raw numpy construction, not pandas get_dummies/concat -- measured at ~3.2ms/call with the
    pandas version (a real cost once candidates start numbering in the dozens per order arrival,
    turning a single simulated run from ~0.01s into 30-90s) vs. negligible with a plain array.
    Matters here and for live quote-scoring latency, not just this validation run.

    truck_pct_km/days_interval (documents/logs/18): the SAME truck rides forward with a driver
    into their next commitment (not swapped mid-trip), so post-trip maintenance % is a real
    property of the landing STATE, not an action-specific feature -- see
    train_state_value_function.py's NON_REGION_FEATURES.

    Uses booster.inplace_predict(), NOT booster.predict(xgb.DMatrix(...)) (documents/logs/19's
    performance fix): profiled directly on a real full-year run and found DMatrix construction
    ALONE cost ~18.4s of a 28.1s sample (65%) -- called twice per candidate (v_at_dest + v_at_hub)
    across every candidate scored at every decision, this was the dominant cost of running the
    trained policy at all, not the model's own prediction work. inplace_predict() skips
    constructing a DMatrix wrapper entirely, reading the raw numpy array directly -- same model,
    same output, ~measured well over an order of magnitude faster per call.

    Returns the SHRUNK-AND-CLIPPED prediction (V_SHRINKAGE_FACTOR, V_CLIP_MIN/MAX above), not the
    raw booster output -- see this module's header comment for the concrete regression this fixes.

    `hos_cycle1/2_remaining`/`distance_to_home_miles`/`hours_since_home` (documents/logs/23,25-26,
    sim/sql/041,047): optional, default None -- only set into the row if BOTH a real value was
    passed AND that column name actually exists in `feature_columns`. This is what lets the SAME
    function score either an OLD model shape (missing the newest home-progress dimensions) or a
    NEW retrained one without a separate code path -- a model trained on the old feature set
    simply never has these column names in its `feature_columns` list, so they're silently
    skipped rather than raising.
    """
    row = np.zeros((1, len(feature_columns)), dtype=np.float32)
    values = {
        'hos_remaining': hos_remaining, 'hour_of_day': hour, 'day_of_week': dow,
        'truck_pct_km_interval': truck_pct_km_interval, 'truck_pct_days_interval': truck_pct_days_interval,
        f'region_{region_id}': 1.0,
    }
    if hos_cycle1_remaining is not None:
        values['hos_cycle1_remaining'] = hos_cycle1_remaining
    if hos_cycle2_remaining is not None:
        values['hos_cycle2_remaining'] = hos_cycle2_remaining
    if distance_to_home_miles is not None:
        values['distance_to_home_miles'] = distance_to_home_miles
    if hours_since_home is not None:
        values['hours_since_home'] = hours_since_home
    for col, val in values.items():
        if col not in feature_columns:
            continue  # backward compat -- see docstring above
        idx = feature_columns.index(col)
        row[0, idx] = val
    raw = float(booster.inplace_predict(row)[0])
    shrunk = raw * V_SHRINKAGE_FACTOR
    return max(V_CLIP_MIN, min(V_CLIP_MAX, shrunk))


def make_value_fn(booster: xgb.Booster, feature_columns: list[str], data: SimData):
    """Returns a value_fn(candidate, order, now) closure for policy.choose_assignment(). `now`
    (the sim clock at decision time) supplies hour_of_day/day_of_week -- a reasonable stand-in
    for when the trip lands, since the trip itself is short relative to a full day/week cycle for
    this regional fleet.

    E[V(s')] = (p_reload + p_dromt) * V(order's destination region) + p_deadhead * V(nearest
    hub's region) -- the SAME dynamic_post_completion_probs() the simulator itself uses to decide
    where a truck actually ends up, so scoring and simulation agree on what "landing here" means.
    A driver's HOS remaining at the landing spot is approximated as their current remaining hours
    minus this trip's planned duty hours -- the real post-trip HOS depends on dwell/deadhead
    specifics only known once the trip actually runs, so this is a light estimate, not the exact
    figure the training data itself used.
    """
    def value_fn(candidate: Candidate, order: Order, now=None) -> float:
        hour = now.hour if now is not None else 12
        dow = now.weekday() if now is not None else 0

        # .get() + nearest_region() fallback, not a bare dict index: a location can reach here
        # with a null/missing region_id (e.g. a newly-geocoded address inserted between one
        # process's startup load and this call -- caught directly as a live 500 error, 3 rows
        # found with region_id NULL). nearest_region() is the SAME classification run_sim.py uses
        # for any arbitrary point, so this degrades to the correct region, not a guess.
        dest_region = data.location_region.get(order.dest_location_id)
        if dest_region is None:
            dest_region = nearest_region(data, *data.locations[order.dest_location_id])
        p_reload, p_deadhead, p_dromt = dynamic_post_completion_probs(order.dest_distance_to_hub_km)
        landed_hos = max(0.0, candidate.hos_state.remaining_hours - candidate.planned_duty_hours)
        # Home-base-return retarget (documents/logs/23, sim/sql/041) -- same light decision-time
        # estimate landed_hos above already uses, applied to the two cycle clocks specifically
        # (the ones hos_urgency actually cares about, see reward.py's _hos_urgency).
        landed_cycle1 = max(0.0, candidate.hos_state.remaining_cycle1_hours - candidate.planned_duty_hours)
        landed_cycle2 = max(0.0, candidate.hos_state.remaining_cycle2_hours - candidate.planned_duty_hours)
        # candidate.distance_to_home_miles_landing was already computed by the caller (the SAME
        # figure driving home_progress_bonus at decision time, see run_sim.py's candidate-building
        # loop / sim/live/score_quote.py's equivalent) -- reused here rather than a second
        # get_route() call, so V(s')'s notion of "distance to home at the destination" and the
        # reward's own notion never silently diverge.
        dest_distance_to_home = candidate.distance_to_home_miles_landing

        # Home-time retarget (documents/logs/25-26) -- the business-cadence companion to
        # dest_distance_to_home above, using the SAME "0.0 once landing AT home, else current plus
        # this trip's duty hours" estimate reward.py's compute_reward()/run_sim.py's
        # run_assignment() both already use, so V(s') and the realized reward never silently
        # disagree about what "hours since home at the landing state" means.
        home_hub_id = driver_home_hub_id(data, candidate.driver_id)
        landed_hours_since_home = None
        if candidate.hours_since_home is not None:
            landed_hours_since_home = (
                0.0 if order.dest_location_id == home_hub_id
                else candidate.hours_since_home + candidate.planned_duty_hours
            )

        # Projected post-trip truck-maintenance state -- SAME after_trip() math run_sim.py itself
        # uses once a trip actually completes (documents/logs/18), so scoring and simulation agree
        # on what "the truck's condition after this trip" means, same principle as
        # dynamic_post_completion_probs() below already being shared between scoring and sim.
        committed_miles = candidate.pre_pickup_deadhead_miles + order.loaded_miles
        # planned_duty_hours (drive + dwell estimate) approximates the wall-clock elapsed, matching
        # landed_hos's own use of it as the best decision-time estimate available.
        landed_truck_state = candidate.truck_state.after_trip(committed_miles, hours_elapsed=candidate.planned_duty_hours)
        truck_pct_km = landed_truck_state.pct_of_km_interval
        truck_pct_days = landed_truck_state.pct_of_days_interval

        v_at_dest = _predict_state_value(
            booster, feature_columns, dest_region, landed_hos, hour, dow, truck_pct_km, truck_pct_days,
            hos_cycle1_remaining=landed_cycle1, hos_cycle2_remaining=landed_cycle2,
            distance_to_home_miles=dest_distance_to_home, hours_since_home=landed_hours_since_home,
        )
        if p_deadhead > 0:
            dest_lat, dest_lon = data.locations[order.dest_location_id]
            nearest_hub_id = min(
                data.hub_ids.values(),
                key=lambda hid: _haversine_km((dest_lat, dest_lon), data.locations[hid]),
            )
            # The deadhead branch lands at nearest_hub_id, NOT order.dest_location_id -- its
            # distance-to-home (and hours-since-home) is a genuinely different figure from the
            # dest-anchored ones above (this driver's own home hub may or may not BE
            # nearest_hub_id), so it needs its own get_route() call/zero-at-home check rather than
            # reusing the candidate's dest-anchored figures.
            hub_distance_to_home, _ = get_route(data, nearest_hub_id, home_hub_id)
            landed_hours_since_home_at_hub = None
            if candidate.hours_since_home is not None:
                landed_hours_since_home_at_hub = (
                    0.0 if nearest_hub_id == home_hub_id
                    else candidate.hours_since_home + candidate.planned_duty_hours
                )
            v_at_hub = _predict_state_value(
                booster, feature_columns, data.location_region[nearest_hub_id], landed_hos, hour, dow,
                truck_pct_km, truck_pct_days,
                hos_cycle1_remaining=landed_cycle1, hos_cycle2_remaining=landed_cycle2,
                distance_to_home_miles=hub_distance_to_home, hours_since_home=landed_hours_since_home_at_hub,
            )
        else:
            v_at_hub = v_at_dest

        return (p_reload + p_dromt) * v_at_dest + p_deadhead * v_at_hub

    return value_fn
