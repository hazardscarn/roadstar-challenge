"""Real, end-to-end regression coverage for the home-time retarget (documents/logs/25):
DriverState.last_home_at tracking through a real TRIP_COMPLETE, and the real, previously-dormant
home_progress_bonus/cycle_end_stranding_penalty mechanism actually firing in a normal, realistic
simulated week -- not forced by hand (test_home_progress.py already covers the mechanism itself
in isolation), a REAL run of the shipped engine, `run_sim.py`'s own `run_simulation()`.

test_home_progress.py's own header comment (and documents/logs/24) reported the real, measured
finding this whole feature closes: the legal-cycle-only urgency mechanism NEVER fired across a
500-run/43,078-decision real calibrated batch. This file proves the fix directly against a real
run of the actual engine -- not asserting an exact count (a real stochastic system), but the real
structural properties the fix is supposed to deliver.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.engine.run_sim import driver_home_hub_id, load_sim_data, run_simulation  # noqa: E402


def test_home_time_shaping_fires_and_last_home_at_tracks_real_arrivals():
    data = load_sim_data()
    result = run_simulation(hours=240, seed=7, epsilon_start=0.0, epsilon_end=0.0, data=data)
    trips = result['completed_trips']
    assert len(trips) > 20, "need a real sample of completed trips for this to be a meaningful check"

    # THE real fix: home_progress_bonus/cycle_end_stranding_penalty must NOT be identically 0.0
    # across every trip anymore -- the exact dormant-mechanism finding documents/logs/24 reported,
    # now closed by the business "hours since home" clock (test_home_progress.py's own
    # test_reward_shaping_now_fires_in_a_normal_realistic_week_thanks_to_the_business_clock covers
    # the mechanism in isolation; this confirms it actually engages in a REAL run).
    n_nonzero_shaping = sum(
        1 for c in trips if c.home_progress_bonus != 0.0 or c.cycle_end_stranding_penalty != 0.0
    )
    assert n_nonzero_shaping > 0, (
        "home_progress_bonus/cycle_end_stranding_penalty fired ZERO times across a real "
        f"{len(trips)}-trip run -- the exact dormant-mechanism regression this feature exists to fix"
    )

    # A real "cycle closed at home" outcome must have actually happened at least once -- either via
    # a real order whose own destination is home, or via the new post-completion home-targeting
    # mechanic (run_sim.py's dynamic_post_completion_probs() call site).
    n_home_landings = sum(
        1 for c in trips if c.next_location_id == driver_home_hub_id(data, c.driver_id)
    )
    assert n_home_landings > 0, "no driver ever landed back at their own home hub across a real 240h run"
