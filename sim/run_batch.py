"""Batch runner: run many simulation runs in parallel, save each to local Postgres.

Each worker process loads SimData and (optionally) the trained state-value model ONCE via a pool
initializer -- at any real batch size the network round-trip would otherwise dominate wall-clock time
far more than the simulation itself does. Each run gets its own seed (reproducible individually).

Temporal realism: each sim runs over PAST dates (default 12 months ending yesterday), orders carry
real lead times (CREATED_TIME -> ACTUAL_PICKUP from ground_truth.historical_orders), and a dispatch
decision fires at the LATER of booking time or 24h before requested pickup -- an order with more lead
time just sits on the books until its cutoff. See documents/logs/17 for details.

Exploration decays linearly across simulated elapsed time, not flat per-event: starts high (EPSILON_START),
decays to low (EPSILON_END) as the sim progresses through its history. Standard epsilon-greedy practice.
"""
import argparse
import multiprocessing as mp
import time

from sim.engine.run_sim import SimData, load_sim_data, run_simulation, save_run
from sim.config import EPSILON_START, EPSILON_END


_worker_data: SimData | None = None
_worker_value_fn = None  # None -> policy.zero_value_fn (run_simulation's own default)


def _init_worker(policy: str = 'zero'):
    """policy='trained' loads the state-value model ONCE per worker process (like SimData) --
    reloading it per run would be wasteful, and it's what actually drives dispatch decisions
    when passed through to run_simulation() (not just scoring after the fact).
    """
    global _worker_data, _worker_value_fn
    _worker_data = load_sim_data()
    if policy == 'trained':
        from sim.engine.value_function import load_state_value_model, make_value_fn
        booster, cols = load_state_value_model('sim/training/state_value_function.pkl')
        _worker_value_fn = make_value_fn(booster, cols, _worker_data)


def _run_one(args: tuple[int, float, float, bool, float, float]) -> dict:
    seed, hours, epsilon_start, epsilon_end, do_save, sim_days = args
    result = run_simulation(
        hours=hours, seed=seed, epsilon_start=epsilon_start, epsilon_end=epsilon_end,
        data=_worker_data, value_fn=_worker_value_fn,
        sim_start=None,  # let the engine pick a past date (one year before today)
    )
    if do_save:
        save_run(result)
    trips = result['completed_trips']
    return {
        'seed': seed,
        'completed': len(trips),
        'unassigned': result['unassigned_orders'],
        'total_operational_margin': sum(c.immediate_reward for c in trips),
        'total_reward': sum(c.reward_total for c in trips),
        'breakdowns': sum(1 for c in trips if c.trip_state.had_breakdown),
        'post_delivery_deadhead_trips': sum(1 for c in trips if c.trip_state.had_post_delivery_deadhead),
        'total_lateness_penalty': sum(c.lateness_penalty for c in trips),
    }


def run_batch(
    n_runs: int, hours: float, epsilon_start: float, epsilon_end: float, workers: int,
    policy: str = 'zero', save: bool = True, seed_offset: int = 0, sim_days: float | None = None,
) -> list[dict]:
    tasks = [(seed_offset + seed, hours, epsilon_start, epsilon_end, save, sim_days or (hours / 24)) for seed in range(n_runs)]
    print(f'Running {n_runs} sims ({hours:.0f}h each, eps {epsilon_start}->{epsilon_end}, policy={policy}) across {workers} worker processes...')
    t0 = time.time()
    with mp.Pool(processes=workers, initializer=_init_worker, initargs=(policy,)) as pool:
        summaries = []
        for i, summary in enumerate(pool.imap_unordered(_run_one, tasks), 1):
            summaries.append(summary)
            if i % max(1, n_runs // 20) == 0 or i == n_runs:
                print(f'  {i}/{n_runs} runs done ({time.time() - t0:.0f}s elapsed)')
    return summaries


def print_batch_summary(summaries: list[dict], elapsed_s: float, label: str = '') -> None:
    n = len(summaries)
    total_trips = sum(s['completed'] for s in summaries)
    total_unassigned = sum(s['unassigned'] for s in summaries)
    total_margin = sum(s.get('total_operational_margin', 0) for s in summaries)
    total_reward = sum(s['total_reward'] for s in summaries)
    total_breakdowns = sum(s['breakdowns'] for s in summaries)
    total_post_dh = sum(s.get('post_delivery_deadhead_trips', 0) for s in summaries)
    total_lateness = sum(s.get('total_lateness_penalty', 0) for s in summaries)
    realized_risk_cost = total_margin - total_reward
    print(f'\n=== Batch summary{f" [{label}]" if label else ""}: {n} runs, {elapsed_s:.0f}s total ({elapsed_s / n:.2f}s/run) ===')
    print(f'  total completed trips: {total_trips:,} (avg {total_trips / n:.1f}/run)')
    print(f'  total unassigned orders: {total_unassigned:,}')
    print(f'  operational margin (decision-time: revenue - deadhead - opp. cost - expected risk): '
          f'{total_margin:,.2f} CAD (avg {total_margin / max(total_trips, 1):,.2f}/trip)')
    print(f'  realized risk cost (post-delivery-deadhead + breakdowns - sec. pickup + lateness, after fact): '
          f'{realized_risk_cost:,.2f} CAD (avg {realized_risk_cost / max(total_trips, 1):,.2f}/trip)')
    print(f'  total reward, fully resolved: {total_reward:,.2f} CAD (avg {total_reward / max(total_trips, 1):,.2f}/trip)')
    print(f'  total breakdowns: {total_breakdowns:,} ({total_breakdowns / max(total_trips, 1):.1%} of trips)')
    print(f'  post-delivery-deadhead trips: {total_post_dh:,} ({total_post_dh / max(total_trips, 1):.1%} of trips)')
    print(f'  total lateness penalty: {total_lateness:,.2f} CAD (avg {total_lateness / max(total_trips, 1):,.2f}/trip)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--n-runs', type=int, default=1000)
    parser.add_argument('--hours', type=float, default=168.0)
    parser.add_argument('--epsilon-start', type=float, default=EPSILON_START)
    parser.add_argument('--epsilon-end', type=float, default=EPSILON_END)
    parser.add_argument('--workers', type=int, default=mp.cpu_count())
    parser.add_argument('--policy', choices=['zero', 'trained'], default='zero')
    parser.add_argument('--no-save', action='store_true', help="don't persist to local sim.*")
    parser.add_argument('--seed-offset', type=int, default=0)
    args = parser.parse_args()

    t0 = time.time()
    summaries = run_batch(
        args.n_runs, args.hours, args.epsilon_start, args.epsilon_end, args.workers,
        policy=args.policy, save=not args.no_save, seed_offset=args.seed_offset,
    )
    print_batch_summary(summaries, time.time() - t0, label=args.policy)
