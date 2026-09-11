"""Segment 4, the real positioning-value model: a pure state-value function
V(driver_region, hos_remaining, hour_of_day, day_of_week), fit via Fitted Value Iteration --
what train_value_function.py's single-pass Q(state, order, truck) genuinely cannot do (see
documents/logs/12_feature_richness_ftl_ltl_and_lateness.md's closing section, and the direct
question that prompted this: "would this assign a London->Kitchener order to a Milton->London
truck because Kitchener is one deadhead-mile back?").

## Why V(s) excludes truck condition -- a deliberate design choice, not an oversight

train_value_function.py's Q-model includes truck_breakdown_risk etc. because a SPECIFIC candidate
truck is part of that decision. But a pure state-value function needs to answer "how good is it
to simply BE at this position, with this many hours left, at this time" -- independent of which
order or truck eventually gets evaluated against it. Since trucks are reassigned per-decision from
a small pool (see documents/logs/10), truck condition isn't a stable property of "being
somewhere" the way position/HOS/time are; it's specific to whichever candidate action is being
scored. So:

    STATE = (driver_region_id, driver_hos_remaining, hour_of_day, day_of_week)

and truck-specific risk stays exactly where it already is, in compute_reward()'s
maintenance_risk_penalty and the Q-model's truck_breakdown_risk feature.

## Why region_id, not raw lat/lon (a real bug in the first version of this file)

Trees split axis-aligned -- a lat/lon threshold can't express a real 2D area without many
sequential splits, and with sparse per-location data (2,110 locations, some visited only a
handful of times) that overfits to coordinate noise rather than learned geography. Caught
directly: a smoke test of the first version had a specific remote location outscoring a real hub,
with lat/lon dominating feature importance at only R^2~0.06 -- a sign of overfitting to
coordinates, not a real geographic signal. `region_id` (sim/cluster_locations.py's k-means over
all 2,110 real locations, k=30 chosen by silhouette score, sim/sql/021) gives trees a natural
categorical split on an actual cluster boundary instead.

## Fitted Value Iteration

    V_0(s)     = fit directly on realized target_value  (a Monte-Carlo-style baseline: what
                 outcomes actually followed from states like this, across the whole batch,
                 whatever order happened to get matched to them by the epsilon-greedy policy)
    V_{k+1}(s) = fit on (target_value + GAMMA * V_k(next_state))   for a few rounds

`next_state` is the driver's REAL recorded region/HOS/time right after each trip (sim/sql/019-020,
extract_transitions.py) -- not a guess.

## How this plugs into scoring

For a candidate order, the expected landing state depends on the post-completion branch
(dynamic_post_completion_probs() in run_sim.py) -- reload/dromt leave the truck at the
destination's region, a real deadhead moves it to the nearest hub's region:

    E[V(s')] = (p_reload + p_dromt) * V(order's destination region)
               + p_deadhead * V(nearest hub's region)

computed with the SAME dynamic_post_completion_probs() function the simulator itself uses, so
scoring and simulation agree on what "landing here" means. See sim/engine/value_function.py.
"""
import argparse
import pickle

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

from sim.db import cursor

GAMMA = 0.9  # matches sim/engine/run_sim.py's GAMMA -- same discount factor, one model family
N_REGIONS = 25  # sim/cluster_locations.py's H3 cell count -- fixed so current/next one-hot columns always align
# truck_pct_km/days_interval added (documents/logs/18): unlike order-specific fields, truck
# maintenance condition genuinely travels forward with the driver into their NEXT state -- the
# same truck, not swapped mid-trip (see run_sim.py's module docstring) -- so it belongs in STATE,
# not just the Q-model's action-specific features. See train() below for the current/next mapping.
#
# hos_cycle1/2_remaining + distance_to_home_miles added (documents/logs/23, sim/sql/041): the
# blended hos_remaining alone can't distinguish "recoverable tomorrow" (a daily-clock limit) from
# "genuinely cycle-capped" (a 7-day/14-day limit) -- exactly the distinction the home-base-return
# retarget needs V(s) to be able to reason about. distance_to_home_miles is the driver's OWN
# home-terminal distance (NOT dest_distance_to_hub_km, which is nearest-ANY-hub and
# driver-agnostic) -- lets V(s) anticipate the home_progress_bonus/cycle_end_stranding_penalty
# dynamic prospectively from POSITION, not just have it baked into an opaque scalar target.
NON_REGION_FEATURES = [
    'hos_remaining', 'hour_of_day', 'day_of_week', 'truck_pct_km_interval', 'truck_pct_days_interval',
    'hos_cycle1_remaining', 'hos_cycle2_remaining', 'distance_to_home_miles',
]


def load_transitions() -> pd.DataFrame:
    """GREEDY-ONLY (was_exploration=false) -- documents/logs/17's diverging-FVI finding: fitting
    V(s) on ALL transitions (incl. the ~33% Epsilon-greedy exploration draws, which pick a
    uniformly RANDOM legal candidate) trains a value function that estimates "what happens under a
    policy that's still 1/3 flailing," not "what's this position worth if dispatch plays it well."
    Exploration outcomes averaged -$783/trip vs. greedy's -$63.5/trip in the 100-run/554K-row
    batch -- mixing them in made V(s) diverge further negative every FVI round (never converging
    within 4 rounds) because the recursion kept propagating that pessimism backward. V(s) is
    supposed to answer the value-of-good-behavior question -- exploration data is for comparing
    policies (the paired batch-comparison scripts), not for training what "good" looks like.
    Some residual optimism-drag can still occur EARLY in a simulated year (Epsilon starts at 0.6,
    so early next_states are more likely to have been reached via an earlier random decision even
    on a currently-greedy row) -- expected and honest, not something this filter alone erases.
    """
    with cursor(local=True) as cur:
        cur.execute(
            """select driver_region_id as region_id, driver_hos_remaining as hos_remaining,
                      hour_of_day, day_of_week, truck_pct_km_interval, truck_pct_days_interval,
                      next_region_id, next_driver_hos_remaining as next_hos_remaining,
                      next_decision_time, next_truck_pct_km_interval, next_truck_pct_days_interval,
                      driver_hos_cycle1_remaining as hos_cycle1_remaining,
                      driver_hos_cycle2_remaining as hos_cycle2_remaining,
                      next_hos_cycle1_remaining, next_hos_cycle2_remaining,
                      distance_to_home_miles, distance_to_home_miles_landing as next_distance_to_home_miles,
                      target_value
               from training_transitions
               where was_exploration = false"""
        )
        df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    float_cols = [
        'hos_remaining', 'next_hos_remaining', 'target_value',
        'truck_pct_km_interval', 'truck_pct_days_interval',
        'next_truck_pct_km_interval', 'next_truck_pct_days_interval',
        'hos_cycle1_remaining', 'hos_cycle2_remaining',
        'next_hos_cycle1_remaining', 'next_hos_cycle2_remaining',
        'distance_to_home_miles', 'next_distance_to_home_miles',
    ]
    for col in float_cols:
        df[col] = df[col].astype(float)
    df['next_hour_of_day'] = df['next_decision_time'].dt.hour
    df['next_day_of_week'] = df['next_decision_time'].dt.dayofweek
    return df


def _region_dummies(region_series: pd.Series, prefix: str = 'region') -> pd.DataFrame:
    """One-hot over a FIXED category range (0..N_REGIONS-1), not just whatever regions happen to
    appear in this particular series -- current-state and next-state dummies must line up in the
    exact same columns for one booster to score both.
    """
    cat = pd.Categorical(region_series, categories=range(N_REGIONS))
    return pd.get_dummies(cat, prefix=prefix)


def build_state_features(df: pd.DataFrame) -> pd.DataFrame:
    dummies = _region_dummies(df['region_id'])
    return pd.concat([dummies, df[NON_REGION_FEATURES]], axis=1)


def build_next_state_features(df: pd.DataFrame) -> pd.DataFrame:
    """next_* -> current-feature-name mapping, EXPLICIT (not positional zip) so adding/reordering
    NON_REGION_FEATURES can't silently mismatch a next_* column to the wrong current-feature name.
    """
    dummies = _region_dummies(df['next_region_id'])
    rename_map = {
        'next_hos_remaining': 'hos_remaining',
        'next_hour_of_day': 'hour_of_day',
        'next_day_of_week': 'day_of_week',
        'next_truck_pct_km_interval': 'truck_pct_km_interval',
        'next_truck_pct_days_interval': 'truck_pct_days_interval',
        'next_hos_cycle1_remaining': 'hos_cycle1_remaining',
        'next_hos_cycle2_remaining': 'hos_cycle2_remaining',
        'next_distance_to_home_miles': 'distance_to_home_miles',
    }
    next_cols = df[list(rename_map.keys())].rename(columns=rename_map)
    return pd.concat([dummies, next_cols], axis=1)


def fit_round(X_train, y_train, X_test, y_test) -> xgb.Booster:
    dtrain = xgb.DMatrix(X_train, label=y_train)
    dtest = xgb.DMatrix(X_test, label=y_test)
    params = {
        'tree_method': 'hist', 'device': 'cuda',
        'objective': 'reg:squarederror', 'max_depth': 5, 'eta': 0.1,
        'subsample': 0.8, 'colsample_bytree': 0.8, 'eval_metric': 'mae',
    }
    return xgb.train(
        params, dtrain, num_boost_round=300,
        evals=[(dtrain, 'train'), (dtest, 'test')], early_stopping_rounds=15, verbose_eval=False,
    )


def train(n_iterations: int = 4, test_size: float = 0.2, seed: int = 42) -> dict:
    df = load_transitions()
    print(f'Loaded {len(df):,} transitions for Fitted Value Iteration ({n_iterations} rounds, gamma={GAMMA})')

    X = build_state_features(df)
    X_next = build_next_state_features(df)
    feature_columns = list(X.columns)
    y_realized = df['target_value']

    idx_train, idx_test = train_test_split(df.index, test_size=test_size, random_state=seed)
    booster = None
    for k in range(n_iterations):
        if booster is None:
            bootstrap_target = y_realized  # V_0: pure realized-outcome baseline, no bootstrap yet
        else:
            v_next = booster.predict(xgb.DMatrix(X_next, feature_names=feature_columns))
            bootstrap_target = y_realized + GAMMA * v_next

        booster = fit_round(
            X.loc[idx_train], bootstrap_target.loc[idx_train],
            X.loc[idx_test], bootstrap_target.loc[idx_test],
        )
        preds = booster.predict(xgb.DMatrix(X.loc[idx_test], feature_names=feature_columns))
        mae = mean_absolute_error(bootstrap_target.loc[idx_test], preds)
        r2 = r2_score(bootstrap_target.loc[idx_test], preds)
        print(f'  round {k}: test MAE={mae:.2f} CAD, test R^2={r2:.4f}, '
              f'V(s) range=[{preds.min():.1f}, {preds.max():.1f}], mean={preds.mean():.1f}')

    v_final = booster.predict(xgb.DMatrix(X.loc[idx_test], feature_names=feature_columns))
    print(f'\nFinal V(s) on held-out states: mean={v_final.mean():.2f}, std={v_final.std():.2f}')

    # Per-region average V(s), the direct sanity check a raw lat/lon model couldn't give cleanly:
    # does the ranking of "which region is best to be in" look like real geography (hub regions
    # near the top, not a sparse-data fluke)?
    region_cols = [c for c in feature_columns if c.startswith('region_')]
    region_avg_v = {}
    for col in region_cols:
        mask = X.loc[idx_test, col] == 1
        if mask.sum() > 0:
            region_avg_v[col] = (float(preds[mask.values].mean()), int(mask.sum()))
    print('\nAverage V(s) by region (region: avg_V, n_test_samples), sorted best to worst:')
    for col, (avg_v, n) in sorted(region_avg_v.items(), key=lambda kv: -kv[1][0]):
        print(f'  {col}: {avg_v:.1f} (n={n})')

    importance = booster.get_score(importance_type='gain')
    print('\nFeature importance (gain), top 10:')
    for feat, score in sorted(importance.items(), key=lambda kv: -kv[1])[:10]:
        print(f'  {feat}: {score:.1f}')

    return {'booster': booster, 'feature_columns': feature_columns}


def save_model(result: dict, out_path: str) -> None:
    with open(out_path, 'wb') as f:
        pickle.dump({'booster': result['booster'], 'feature_columns': result['feature_columns']}, f)
    print(f'\nSaved state-value model -> {out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--iterations', type=int, default=4)
    parser.add_argument('--out', default='sim/training/state_value_function.pkl')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    result = train(n_iterations=args.iterations, seed=args.seed)
    save_model(result, args.out)
