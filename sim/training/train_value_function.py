"""Segment 4: fit V(s) on the GB10's GPU from training_transitions -- the single-pass fitted-value
approach research/roadstar_platform_plan.md commits to (policy iteration is a stretch goal, not
this script's job).

## What the model actually predicts, and why it's NOT target_value directly

policy.py's choose_assignment() scores every candidate as:

    score = immediate_reward + gamma * value_fn(candidate, order)

`immediate_reward` (compute_reward().total) is already an exact, known-at-scoring-time function
of the order/driver/truck state -- there's nothing for a model to learn there, and fitting V(s) to
predict target_value directly would DOUBLE-COUNT immediate_reward once it's added into that score
formula (immediate_reward is already inside target_value, since target_value = immediate_reward -
realized_post_delivery_deadhead_cost - realized_breakdown_penalty). What the model actually needs
to predict is the part compute_reward() CAN'T see at decision time: the realized cost that shows
up only after the trip plays out. So the training target here is:

    continuation_value = target_value - immediate_reward   (<= 0 in every row: it's exactly
                                                              -(post_delivery_deadhead_cost + realized_breakdown_penalty))

and `value_fn` in policy.py should be wired to return this model's prediction directly (see
predict_value() below) -- score = immediate_reward + gamma * predicted_continuation_value then
matches the textbook fitted-value decomposition instead of double-counting.

## Features: state only, not the reward components already inside immediate_reward

order_revenue / deadhead_cost / opportunity_cost_penalty / lateness_risk_penalty are excluded on
purpose -- they're already fully accounted for in immediate_reward's own formula, so they carry no
information about the RESIDUAL this model is fitting. What's kept is genuine state context that
plausibly correlates with what happens *next* (does this decision leave the driver stranded,
overdue-truck-heavy, or well-positioned):

- driver position (lat/lon), driver_hos_remaining -- who's being sent
- order weight/load_type/service_type (FTL vs LTL), order's own loaded_miles -- what's being hauled
- dest_distance_to_hub_km -- the SAME distance that drives run_sim.py's post-completion deadhead
  probability (see dynamic_post_completion_probs); the whole point of adding this is to give the
  model something structurally connected to the outcome it's predicting, not just correlated by
  coincidence
- deadhead_m_to_pickup, load_fill_ratio -- decision quality signals
- planned_driving_hours, planned_duty_hours -- the ESTIMATE available at decision time (OSRM
  duration + calibrated dwell shape), not the realized actual_duration_hours (an outcome, excluded
  -- see extract_transitions.py's docstring for why feeding that back would leak the future)
- truck_breakdown_risk -- was this truck already overdue when picked
- hour_of_day/day_of_week -- order density varies a lot by both, per calibration.order_arrival_rate
"""
import argparse
import pickle

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

from sim.db import cursor

FEATURE_COLUMNS = [
    'driver_hos_remaining', 'order_weight', 'order_loaded_miles',
    'dest_distance_to_hub_km', 'dest_local_order_density', 'deadhead_m_to_pickup', 'load_fill_ratio',
    'planned_driving_hours', 'planned_duty_hours', 'total_committed_distance_miles', 'driver_pool_size',
    'truck_breakdown_risk', 'truck_pct_km_interval', 'truck_pct_days_interval',
    'hour_of_day', 'day_of_week',
    # Home-base-return retarget (documents/logs/23, sim/sql/041): genuine state context that
    # plausibly correlates with the RESIDUAL this model fits (does this decision leave the driver
    # stranded far from home with a tight cycle margin), same category as dest_distance_to_hub_km
    # above -- NOT the home_progress_bonus/cycle_end_stranding_penalty $ terms themselves, which
    # (like order_revenue/deadhead_cost) are already fully inside immediate_reward and excluded on
    # purpose (see module docstring).
    'driver_hos_cycle1_remaining', 'driver_hos_cycle2_remaining', 'distance_to_home_miles',
]
# region_id (k-means cluster, sim/cluster_locations.py) replaces raw lat/lon -- see sim/sql/021's
# comment for why (trees split axis-aligned; raw coordinates can't express a real 2D area without
# many sequential thresholds, and overfit to coordinate noise on sparse per-location data).
CATEGORICAL_COLUMNS = ['order_load_type', 'order_service_type', 'driver_region_id', 'dest_region_id']


def load_training_data() -> pd.DataFrame:
    with cursor(local=True) as cur:
        cur.execute(
            """select driver_region_id, dest_region_id,
                      driver_hos_remaining, order_weight, order_load_type, order_service_type,
                      order_loaded_miles, dest_distance_to_hub_km, dest_local_order_density,
                      deadhead_m_to_pickup, load_fill_ratio, planned_driving_hours, planned_duty_hours,
                      total_committed_distance_miles, driver_pool_size,
                      truck_breakdown_risk, truck_pct_km_interval, truck_pct_days_interval,
                      hour_of_day, day_of_week, immediate_reward, target_value,
                      driver_hos_cycle1_remaining, driver_hos_cycle2_remaining, distance_to_home_miles
               from training_transitions"""
        )
        df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for col in df.columns:
        if df[col].dtype.name == 'object' and col not in CATEGORICAL_COLUMNS:
            df[col] = df[col].astype(float)
    df['continuation_value'] = df['target_value'].astype(float) - df['immediate_reward'].astype(float)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    X = df[FEATURE_COLUMNS].copy()
    dummies = pd.concat(
        [pd.get_dummies(df[col], prefix=col) for col in CATEGORICAL_COLUMNS], axis=1,
    )
    return pd.concat([X, dummies], axis=1)


def train(test_size: float = 0.2, seed: int = 42) -> dict:
    df = load_training_data()
    print(f'Loaded {len(df):,} training_transitions rows')
    print(f'continuation_value: min={df["continuation_value"].min():.2f}, '
          f'mean={df["continuation_value"].mean():.2f}, max={df["continuation_value"].max():.2f} '
          '(mean should be negative: mostly -(realized post-delivery-deadhead cost + breakdown '
          'penalty), but CAN go positive on a trip where a secondary LTL pickup materialized -- '
          'that revenue was not yet known at immediate_reward time either, so it is a legitimate '
          'part of the same residual, not a bug)')

    X = build_features(df)
    y = df['continuation_value']
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=test_size, random_state=seed)

    dtrain = xgb.DMatrix(X_train, label=y_train, enable_categorical=False)
    dtest = xgb.DMatrix(X_test, label=y_test, enable_categorical=False)

    params = {
        'tree_method': 'hist', 'device': 'cuda',  # GB10 GPU
        'objective': 'reg:squarederror', 'max_depth': 6, 'eta': 0.1,
        'subsample': 0.8, 'colsample_bytree': 0.8, 'eval_metric': 'mae',
    }
    evals_result = {}
    booster = xgb.train(
        params, dtrain, num_boost_round=500,
        evals=[(dtrain, 'train'), (dtest, 'test')], early_stopping_rounds=20,
        evals_result=evals_result, verbose_eval=False,
    )

    preds = booster.predict(dtest)
    mae = mean_absolute_error(y_test, preds)
    r2 = r2_score(y_test, preds)
    baseline_mae = mean_absolute_error(y_test, np.full_like(y_test, y_train.mean()))
    print(f'\nTest MAE: {mae:.2f} CAD (naive mean-baseline MAE: {baseline_mae:.2f} CAD)')
    print(f'Test R^2: {r2:.4f}')
    print(f'Best iteration: {booster.best_iteration}')

    importance = booster.get_score(importance_type='gain')
    print('\nFeature importance (gain):')
    for feat, score in sorted(importance.items(), key=lambda kv: -kv[1]):
        print(f'  {feat}: {score:.1f}')

    return {'booster': booster, 'feature_columns': list(X.columns), 'mae': mae, 'r2': r2, 'baseline_mae': baseline_mae}


def save_model(result: dict, out_path: str) -> None:
    with open(out_path, 'wb') as f:
        pickle.dump({'booster': result['booster'], 'feature_columns': result['feature_columns']}, f)
    print(f'\nSaved model -> {out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='sim/training/value_function.pkl')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    result = train(seed=args.seed)
    save_model(result, args.out)
