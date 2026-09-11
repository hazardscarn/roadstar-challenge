"""Paired significance test: OLD trained model vs. NEW trained model (home-base-return retarget,
documents/logs/23/24), same standard documents/logs/17/18 already established -- same batch of
seeds, epsilon=0 (no exploration confound, isolates the policy's own choices), paired t-test.

Reports overall total_reward (does the retarget change the headline number at all) AND the
home-base-specific outcomes the original handoff explicitly said matter more than overall reward
(distance from home when a decision is made, home_progress_bonus/cycle_end_stranding_penalty
incidence) -- since overall reward could move for reasons unrelated to this fix.
"""
import argparse
import multiprocessing as mp
import time

import numpy as np
from scipy import stats

from sim.engine.run_sim import SimData, load_sim_data, run_simulation
from sim.engine.value_function import load_state_value_model, make_value_fn

_worker_data: SimData | None = None
_worker_value_fns: dict[str, object] = {}


def _init_worker(model_paths: dict[str, str]):
    global _worker_data, _worker_value_fns
    _worker_data = load_sim_data()
    for label, path in model_paths.items():
        booster, cols = load_state_value_model(path)
        _worker_value_fns[label] = make_value_fn(booster, cols, _worker_data)


def _run_one(args: tuple[int, str, float]) -> dict:
    seed, label, hours = args
    result = run_simulation(
        hours=hours, seed=seed, epsilon_start=0.0, epsilon_end=0.0,  # epsilon=0 -- pure exploit, no confound
        data=_worker_data, value_fn=_worker_value_fns[label], sim_start=None,
    )
    trips = result['completed_trips']
    n = max(len(trips), 1)
    return {
        'seed': seed, 'label': label,
        'completed': len(trips),
        'unassigned': result['unassigned_orders'],
        'total_reward': sum(c.reward_total for c in trips),
        'avg_reward_per_trip': sum(c.reward_total for c in trips) / n,
        'avg_distance_to_home_landing': sum(c.distance_to_home_miles_landing for c in trips) / n,
        'total_home_progress_bonus': sum(c.home_progress_bonus for c in trips),
        'n_nonzero_home_progress_bonus': sum(1 for c in trips if c.home_progress_bonus != 0.0),
        'total_cycle_end_stranding_penalty': sum(c.cycle_end_stranding_penalty for c in trips),
        'n_nonzero_cycle_end_stranding_penalty': sum(1 for c in trips if c.cycle_end_stranding_penalty > 0.0),
        'total_deadhead_miles': sum(c.deadhead_miles for c in trips),
    }


def run_comparison(n_seeds: int, hours: float, old_model: str, new_model: str, workers: int, seed_offset: int = 0):
    model_paths = {'old': old_model, 'new': new_model}
    tasks = []
    for seed in range(seed_offset, seed_offset + n_seeds):
        tasks.append((seed, 'old', hours))
        tasks.append((seed, 'new', hours))

    print(f'Running {n_seeds} paired seeds x 2 models ({hours:.0f}h each, epsilon=0) across {workers} workers...')
    t0 = time.time()
    with mp.Pool(processes=workers, initializer=_init_worker, initargs=(model_paths,)) as pool:
        results = list(pool.imap_unordered(_run_one, tasks))
    print(f'  done in {time.time() - t0:.0f}s')

    by_seed = {}
    for r in results:
        by_seed.setdefault(r['seed'], {})[r['label']] = r

    old_totals, new_totals = [], []
    old_avg_dist, new_avg_dist = [], []
    total_hpb_new, total_cesp_new = 0.0, 0.0
    n_hpb_new, n_cesp_new = 0, 0
    total_hpb_old, total_cesp_old = 0.0, 0.0
    for seed, pair in sorted(by_seed.items()):
        if 'old' not in pair or 'new' not in pair:
            continue
        old_totals.append(pair['old']['total_reward'])
        new_totals.append(pair['new']['total_reward'])
        old_avg_dist.append(pair['old']['avg_distance_to_home_landing'])
        new_avg_dist.append(pair['new']['avg_distance_to_home_landing'])
        total_hpb_new += pair['new']['total_home_progress_bonus']
        n_hpb_new += pair['new']['n_nonzero_home_progress_bonus']
        total_cesp_new += pair['new']['total_cycle_end_stranding_penalty']
        n_cesp_new += pair['new']['n_nonzero_cycle_end_stranding_penalty']
        total_hpb_old += pair['old']['total_home_progress_bonus']
        total_cesp_old += pair['old']['total_cycle_end_stranding_penalty']

    old_totals, new_totals = np.array(old_totals), np.array(new_totals)
    old_avg_dist, new_avg_dist = np.array(old_avg_dist), np.array(new_avg_dist)

    t_reward, p_reward = stats.ttest_rel(new_totals, old_totals)
    t_dist, p_dist = stats.ttest_rel(new_avg_dist, old_avg_dist)

    n_completed_old = sum(pair['old']['completed'] for pair in by_seed.values() if 'old' in pair)
    n_completed_new = sum(pair['new']['completed'] for pair in by_seed.values() if 'new' in pair)

    print(f'\n=== Paired comparison, {len(old_totals)} matched seeds ===')
    print(f'OLD model  ({old_model}): total_reward mean={old_totals.mean():,.2f} (n_trips={n_completed_old:,})')
    print(f'NEW model  ({new_model}): total_reward mean={new_totals.mean():,.2f} (n_trips={n_completed_new:,})')
    print(f'  paired t-test on per-run total_reward: t={t_reward:.3f}, p={p_reward:.4f}')
    print(f'\navg distance-to-home-at-landing, OLD: {old_avg_dist.mean():.2f}mi, NEW: {new_avg_dist.mean():.2f}mi')
    print(f'  paired t-test: t={t_dist:.3f}, p={p_dist:.4f}')
    print(f'\nhome_progress_bonus incidence -- OLD model runs: {n_completed_old:,} trips scored under the NEW '
          f'reward function too (reward.py is shared code), total={total_hpb_old:,.2f}')
    print(f'home_progress_bonus incidence -- NEW model runs: nonzero on {n_hpb_new:,}/{n_completed_new:,} trips, '
          f'total={total_hpb_new:,.2f} CAD')
    print(f'cycle_end_stranding_penalty incidence -- OLD model runs: total={total_cesp_old:,.2f}')
    print(f'cycle_end_stranding_penalty incidence -- NEW model runs: nonzero on {n_cesp_new:,}/{n_completed_new:,} '
          f'trips, total={total_cesp_new:,.2f} CAD')
    print('\n(Both OLD and NEW model runs use the SAME shared reward.py -- the home_progress_bonus/'
          'cycle_end_stranding_penalty columns exist and are computed either way; what differs is '
          'whether the VALUE FUNCTION driving dispatch choices was trained with the new state '
          'features. See documents/logs/24 for why these may show near-zero incidence regardless: '
          'a real, measured coverage gap in this fleet\'s calibrated demand.)')

    return {
        'old_totals': old_totals, 'new_totals': new_totals, 't_reward': t_reward, 'p_reward': p_reward,
        't_dist': t_dist, 'p_dist': p_dist,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--n-seeds', type=int, default=60)
    parser.add_argument('--hours', type=float, default=168.0)
    parser.add_argument('--old-model', default='sim/training/state_value_function.pkl')
    parser.add_argument('--new-model', default='sim/training/state_value_function_v2_home_progress.pkl')
    parser.add_argument('--workers', type=int, default=mp.cpu_count())
    parser.add_argument('--seed-offset', type=int, default=20000)
    args = parser.parse_args()

    run_comparison(args.n_seeds, args.hours, args.old_model, args.new_model, args.workers, args.seed_offset)
