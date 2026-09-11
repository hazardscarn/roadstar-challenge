"""Validates the literature-grounded claim directly, using real data: does ranking candidates by
immediate_reward + gamma * V(expected landing state) -- the DiDi/Powell-style value-augmented
score, already computable from what's built (sim/engine/value_function.py) -- beat the plain
greedy immediate-reward ranking on real outcomes? Reuses the SAME relevance-labeling machinery
as train_ranker.py (real empirical bucket outcomes for unchosen candidates, real target_value for
the chosen one -- see that file's docstring for why) so this comparison is apples-to-apples with
the 42.7% baseline already measured there. No new model is trained here -- just a different way
of RANKING the same logged candidates (sim.candidate_scores), using the already-trained
state_value_function.pkl.
"""
import pickle

import numpy as np
import pandas as pd
import xgboost as xgb

from sim.config import HOS_MAX_DRIVING_HOURS
from sim.db import cursor
from sim.engine.run_sim import GAMMA, dynamic_post_completion_probs
from sim.training.train_ranker import build_empirical_bucket_means, build_relevance_labels, load_candidate_groups, load_real_outcomes

N_REGIONS = 30


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlambda / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def load_state_value_model(path: str):
    with open(path, 'rb') as f:
        saved = pickle.load(f)
    return saved['booster'], saved['feature_columns']


def predict_v_batch(booster, feature_columns, region_ids: np.ndarray, hos: np.ndarray, hour: np.ndarray, dow: np.ndarray) -> np.ndarray:
    dummies = pd.get_dummies(pd.Categorical(region_ids, categories=range(N_REGIONS)), prefix='region')
    X = pd.concat([dummies, pd.DataFrame({'hos_remaining': hos, 'hour_of_day': hour, 'day_of_week': dow})], axis=1)
    X = X[feature_columns]
    return booster.predict(xgb.DMatrix(X, feature_names=feature_columns))


def main():
    booster, feature_columns = load_state_value_model('sim/training/state_value_function.pkl')

    with cursor() as cur:  # remote
        cur.execute("select location_id, region_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations")
        loc_rows = cur.fetchall()
    region_of = {r[0]: r[1] for r in loc_rows}
    latlon_of = {r[0]: (float(r[2]), float(r[3])) for r in loc_rows}
    with cursor() as cur:
        cur.execute("select location_id from reference.locations where label like 'RoadStar Terminal%'")
        hub_locations = [r[0] for r in cur.fetchall()]

    # Reuse train_ranker.py's exact relevance construction -- real empirical bucket outcomes for
    # unchosen candidates, real target_value for the chosen one. Same labels, same 216,929-group
    # dataset already used for the 42.7% greedy baseline.
    bucket_means, global_mean = build_empirical_bucket_means()
    candidates = load_candidate_groups()
    real_outcomes = load_real_outcomes()
    df = build_relevance_labels(candidates, bucket_means, global_mean, real_outcomes)

    with cursor(local=True) as cur:
        cur.execute("select sim_id, order_id, decision_time from training_transitions where action_taken = 'accepted'")
        times = pd.DataFrame(cur.fetchall(), columns=['sim_id', 'order_id', 'decision_time'])
    df = df.merge(times, on=['sim_id', 'order_id'], how='inner')

    with cursor(local=True) as cur:
        cur.execute("select sim_id, order_id, dest_location_id, dest_distance_to_hub_km from sim.orders")
        dest_info = pd.DataFrame(cur.fetchall(), columns=['sim_id', 'order_id', 'dest_location_id', 'dest_distance_to_hub_km'])
    df = df.merge(dest_info, on=['sim_id', 'order_id'], how='inner')

    print(f'Loaded {len(df):,} candidate rows across {df["order_id"].nunique():,} groups')

    df['dest_region_id'] = df['dest_location_id'].map(region_of)
    df['hour_of_day'] = df['decision_time'].dt.hour
    df['day_of_week'] = df['decision_time'].dt.dayofweek
    df['dest_distance_to_hub_km'] = df['dest_distance_to_hub_km'].astype(float)
    p_reload, p_deadhead, p_dromt = zip(*df['dest_distance_to_hub_km'].apply(dynamic_post_completion_probs))
    df['p_reload_or_dromt'] = np.array(p_reload) + np.array(p_dromt)
    df['p_deadhead'] = np.array(p_deadhead)

    landed_hos = np.clip(df['driver_hos_remaining'].astype(float) - 3.5, 0, HOS_MAX_DRIVING_HOURS)
    v_at_dest = predict_v_batch(booster, feature_columns, df['dest_region_id'].values, landed_hos.values, df['hour_of_day'].values, df['day_of_week'].values)

    nearest_hub = {}
    for loc_id in df['dest_location_id'].unique():
        lat, lon = latlon_of[loc_id]
        nearest_hub[loc_id] = min(hub_locations, key=lambda h: _haversine_km(lat, lon, *latlon_of[h]))
    hub_region = df['dest_location_id'].map(nearest_hub).map(region_of)
    v_at_hub = predict_v_batch(booster, feature_columns, hub_region.values, landed_hos.values, df['hour_of_day'].values, df['day_of_week'].values)

    df['v_expected'] = df['p_reload_or_dromt'] * v_at_dest + df['p_deadhead'] * v_at_hub
    df['combined_score'] = df['predicted_score'].astype(float) + GAMMA * df['v_expected']

    def top1_match_rate(is_top: pd.Series) -> tuple[int, int]:
        matches, total = 0, 0
        for _, grp in df.groupby('order_id'):
            if not grp['was_chosen'].any() or len(grp) < 2:
                continue
            chosen_idx = grp['was_chosen'].values.argmax()
            if grp['relevance'].values[chosen_idx] < grp['relevance'].median():
                continue  # only count groups where the real dispatch actually did well -- same filter as train_ranker.py
            total += 1
            if is_top.loc[grp.index].values[chosen_idx]:
                matches += 1
        return matches, total

    greedy_is_top = df.groupby('order_id')['rank_position'].transform('min') == df['rank_position']
    value_augmented_is_top = df.groupby('order_id')['combined_score'].transform('max') == df['combined_score']

    g_matches, g_total = top1_match_rate(greedy_is_top)
    v_matches, v_total = top1_match_rate(value_augmented_is_top)
    print(f'\nGreedy (immediate-reward-only) #1 matches a good real outcome: {g_matches}/{g_total} ({g_matches/g_total:.1%})')
    print(f'Value-augmented (immediate + gamma*V(s\')) #1 matches: {v_matches}/{v_total} ({v_matches/v_total:.1%})')


if __name__ == '__main__':
    main()
