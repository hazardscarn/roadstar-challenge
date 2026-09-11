"""Segment 4, the ranking model: "give the best option and rank the top-N" is a LEARNING-TO-RANK
problem, not a regression-then-sort problem -- a direct response to the explicit ask (the
pointwise Q/V models both still exist and still matter; this is a different, better-matched
objective for producing the quote panel's ranked top-N).

## The label problem, and how it's handled honestly

The simulator only ever records the REAL outcome for whichever candidate actually got dispatched
(sim.candidate_scores/sim/sql/023 logs the top-10 CONSIDERED candidates per arrival, but only one
of them ran). Two ways to label the other 9:

1. Use the pointwise models' own predictions -- fast, but circular: the ranker would just
   re-learn what the pointwise model already encodes, under a different loss.
2. Ground unchosen candidates' relevance in REAL empirical outcomes from elsewhere in the
   dataset -- bucket real dispatched decisions (driver_region_id, HOS bucket, truck-risk bucket,
   order service_type) and use the bucket's real mean target_value as the relevance estimate for
   any unchosen candidate with a similar profile. This is what's implemented here: no candidate's
   relevance ever comes from a model scoring itself -- only from REAL realized outcomes,
   aggregated over real decisions with similar characteristics. The CHOSEN candidate in each
   group always gets its own exact real target_value, not a bucket average.

This is still an approximation (a bucket average is a coarser estimate than a truly independent
per-candidate outcome would be -- getting that would require dispatching multiple trucks against
the same real order, which no real or simulated system can do), but it is grounded in real
dispatched results throughout, not a model's self-generated score.
"""
import argparse

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import ndcg_score

from sim.db import cursor

N_REGIONS = 30
FEATURE_COLUMNS = [
    'driver_hos_remaining', 'truck_breakdown_risk', 'truck_pct_km_interval', 'truck_pct_days_interval',
    'pre_pickup_deadhead_miles', 'planned_driving_hours', 'planned_duty_hours',
]
HOS_BINS = [-0.01, 4, 8, 12, 13.01]
RISK_BINS = [-0.01, 0.001, 0.05, 0.15, 1.01]


def _bucket(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['hos_bucket'] = pd.cut(df['driver_hos_remaining'], bins=HOS_BINS, labels=False)
    df['risk_bucket'] = pd.cut(df['truck_breakdown_risk'], bins=RISK_BINS, labels=False)
    return df


def build_empirical_bucket_means() -> tuple[pd.Series, float]:
    """Real dispatched decisions only (training_transitions), bucketed by the same scheme
    candidates get bucketed into below -- the source of ground truth for unchosen candidates'
    relevance labels.
    """
    with cursor(local=True) as cur:
        cur.execute(
            """select driver_region_id, driver_hos_remaining, truck_breakdown_risk,
                      order_service_type, target_value
               from training_transitions"""
        )
        df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for col in ['driver_hos_remaining', 'truck_breakdown_risk', 'target_value']:
        df[col] = df[col].astype(float)
    df = _bucket(df)
    global_mean = df['target_value'].mean()
    bucket_means = df.groupby(['driver_region_id', 'hos_bucket', 'risk_bucket', 'order_service_type'])['target_value'].mean()
    print(f'Built {len(bucket_means):,} empirical buckets from {len(df):,} real dispatched decisions '
          f'(global mean target_value={global_mean:.2f} as fallback for buckets with no coverage)')
    return bucket_means, global_mean


def load_candidate_groups() -> pd.DataFrame:
    with cursor() as cur:  # remote -- need region_id per location
        cur.execute("select location_id, region_id from reference.locations")
        region_lookup = dict(cur.fetchall())

    with cursor(local=True) as cur:
        cur.execute(
            """select cs.sim_id, cs.order_id, cs.driver_id, cs.driver_location_id,
                      cs.driver_hos_remaining, cs.truck_breakdown_risk, cs.truck_pct_km_interval,
                      cs.truck_pct_days_interval, cs.pre_pickup_deadhead_miles,
                      cs.planned_driving_hours, cs.planned_duty_hours, cs.was_chosen, cs.rank_position,
                      cs.predicted_score, o.service_type
               from sim.candidate_scores cs
               join sim.orders o on o.sim_id = cs.sim_id and o.order_id = cs.order_id"""
        )
        df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for col in ['driver_hos_remaining', 'truck_breakdown_risk', 'truck_pct_km_interval',
                'truck_pct_days_interval', 'pre_pickup_deadhead_miles', 'planned_driving_hours', 'planned_duty_hours']:
        df[col] = df[col].astype(float)
    df['driver_region_id'] = df['driver_location_id'].map(region_lookup)
    df = _bucket(df)
    return df


def load_real_outcomes() -> pd.DataFrame:
    """(sim_id, order_id, driver_id) -> real target_value, for the was_chosen=True row in each group."""
    with cursor(local=True) as cur:
        cur.execute("select sim_id, order_id, driver_id, target_value from training_transitions")
        df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    df['target_value'] = df['target_value'].astype(float)
    return df


def build_relevance_labels(candidates: pd.DataFrame, bucket_means: pd.Series, global_mean: float, real_outcomes: pd.DataFrame) -> pd.DataFrame:
    keys = list(zip(candidates['driver_region_id'], candidates['hos_bucket'], candidates['risk_bucket'], candidates['service_type']))
    candidates = candidates.copy()
    candidates['relevance'] = [bucket_means.get(k, global_mean) for k in keys]

    real = real_outcomes.rename(columns={'target_value': 'real_target_value'})
    merged = candidates.merge(real, on=['sim_id', 'order_id', 'driver_id'], how='left')
    is_chosen_with_real = merged['was_chosen'] & merged['real_target_value'].notna()
    merged.loc[is_chosen_with_real, 'relevance'] = merged.loc[is_chosen_with_real, 'real_target_value']
    return merged


def train(seed: int = 42) -> dict:
    bucket_means, global_mean = build_empirical_bucket_means()
    candidates = load_candidate_groups()
    real_outcomes = load_real_outcomes()
    labeled = build_relevance_labels(candidates, bucket_means, global_mean, real_outcomes)
    print(f'Loaded {len(labeled):,} candidate rows across {labeled["order_id"].nunique():,} order groups')

    # qid must be sorted/contiguous for XGBRanker's group inference from a plain array order --
    # sort by order_id (arbitrary but consistent), which also makes qid monotonically grouped.
    labeled = labeled.sort_values('order_id').reset_index(drop=True)
    region_dummies = pd.get_dummies(pd.Categorical(labeled['driver_region_id'], categories=range(N_REGIONS)), prefix='region')
    service_dummies = pd.get_dummies(labeled['service_type'], prefix='service_type')
    X = pd.concat([labeled[FEATURE_COLUMNS], region_dummies, service_dummies], axis=1)
    # rank:ndcg requires non-negative INTEGER relevance grades, not a raw continuous reward --
    # bucket the continuous relevance into 5 ordinal grades by global quantile (0=worst quintile
    # of outcomes ... 4=best), a standard graded-relevance transform for learning-to-rank.
    y = pd.qcut(labeled['relevance'], q=5, labels=False, duplicates='drop').astype(int)
    qid_codes = labeled['order_id'].astype('category').cat.codes.values

    # Split by GROUP (order_id), not by row -- a group must stay intact on one side of the split.
    unique_qids = np.unique(qid_codes)
    rng = np.random.RandomState(seed)
    test_qids = set(rng.choice(unique_qids, size=int(0.2 * len(unique_qids)), replace=False))
    is_test = np.isin(qid_codes, list(test_qids))

    ranker = xgb.XGBRanker(
        tree_method='hist', device='cuda', objective='rank:ndcg',
        max_depth=5, learning_rate=0.1, n_estimators=200, subsample=0.8, colsample_bytree=0.8,
    )
    ranker.fit(X[~is_test], y[~is_test], qid=qid_codes[~is_test])

    # Evaluate: NDCG per held-out group, and how often the ranker's #1 pick matches the real
    # was_chosen candidate WHEN that candidate's real outcome was actually good (top-half of its
    # own group's relevance range) -- a concrete, checkable improvement over the naive baseline
    # of "always trust rank_position==1" (the greedy score ordering already logged).
    test_df = labeled[is_test].copy()
    test_df['predicted'] = ranker.predict(X[is_test])

    ndcgs = []
    top1_match_ranker, top1_match_baseline, n_good_outcome_groups = 0, 0, 0
    for qid, grp in test_df.groupby('order_id'):
        if len(grp) < 2:
            continue
        # ndcg_score requires non-negative relevance -- shift by the group's own min. A constant
        # per-group shift doesn't change which candidate is relatively best/worst, only NDCG's
        # magnitude accounting, so this doesn't distort the comparison.
        rel_values = grp['relevance'].values
        true_rel = (rel_values - rel_values.min()).reshape(1, -1)
        pred_rel = grp['predicted'].values.reshape(1, -1)
        ndcgs.append(ndcg_score(true_rel, pred_rel))
        if not grp['was_chosen'].any():
            continue
        chosen_idx = grp['was_chosen'].values.argmax()
        if grp['relevance'].values[chosen_idx] < grp['relevance'].median():
            continue  # only count groups where the real dispatch actually did well -- the denominator below
        # BUG FIXED: this used to divide by ALL groups (n_groups_checked), not just the
        # "good outcome" ones being conditioned on here -- that conflated "matched AND good" as
        # a fraction of everything, not the intended P(matched | good), and produced a materially
        # wrong (too-low) number for both the ranker and the baseline. Caught cross-checking
        # against validate_value_augmented_ranking.py, which computes the same quantity correctly.
        n_good_outcome_groups += 1
        if grp['predicted'].values.argmax() == chosen_idx:
            top1_match_ranker += 1
        if grp['rank_position'].values[chosen_idx] == grp['rank_position'].values.min():
            top1_match_baseline += 1

    print(f'\nHeld-out groups evaluated: {len(ndcgs):,}, of which {n_good_outcome_groups:,} had a good real outcome')
    print(f'Mean NDCG: {np.mean(ndcgs):.4f}')
    print(f'Ranker #1 matches a good real outcome: {top1_match_ranker}/{n_good_outcome_groups} ({top1_match_ranker/n_good_outcome_groups:.1%})')
    print(f'Baseline (greedy rank_position==1) matches: {top1_match_baseline}/{n_good_outcome_groups} ({top1_match_baseline/n_good_outcome_groups:.1%})')

    return {'ranker': ranker, 'feature_columns': list(X.columns)}


def save_model(result: dict, out_path: str) -> None:
    result['ranker'].save_model(out_path)
    print(f'\nSaved ranker -> {out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='sim/training/ranker.json')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    result = train(seed=args.seed)
    save_model(result, args.out)
