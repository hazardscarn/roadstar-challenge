"""Debug/inspection tool: export one sim run's raw sim.* rows to a multi-sheet Excel workbook,
plus the REAL training_transitions rows extract_transitions.py produced for that run. Lets a
human look at the actual shape of the data, and directly verify the engine is a real time-ordered
discrete-event simulation (not randomly generated).

History: an earlier version of this file surfaced two real gaps (sim.assignments.immediate_margin
was the reward AT ASSIGNMENT TIME only, missing realized post-delivery-deadhead/breakdown costs;
driver position/HOS-remaining at decision time weren't captured at all) -- both fixed
(sim/sql/013_add_reward_total.sql, 014_add_decision_state.sql). A THIRD bug was caught by cross-
checking this export's numbers by hand: sim.assignments.deadhead_miles was actually storing
deadhead_cost (a dollar figure), not miles -- run_sim.py's save_run() passed the wrong field.
Fixed, batch + extraction re-run. The 'training_transitions (REAL)' sheet below reads the actual
extract_transitions.py output for this run, not a draft preview.
"""
import argparse

import pandas as pd

from sim.db import cursor


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Excel has no timezone-aware datetime type -- psycopg2 returns timestamptz columns as
    tz-aware Python datetimes, which openpyxl refuses outright. Strip to naive (values are all
    already UTC from Postgres, so this loses no information, just the explicit tz tag).
    """
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]) and getattr(df[col].dt, 'tz', None) is not None:
            df[col] = df[col].dt.tz_localize(None)
        elif df[col].dtype == object and len(df) and hasattr(df[col].iloc[0], 'tzinfo') and df[col].iloc[0] is not None and df[col].iloc[0].tzinfo is not None:
            df[col] = pd.to_datetime(df[col], utc=True).dt.tz_localize(None)
    return df


def most_recent_sim_id() -> str:
    with cursor(local=True) as cur:
        cur.execute('select sim_id from sim.runs order by started_at desc limit 1')
        return cur.fetchone()[0]


def _verify_time_ordering(events_df: pd.DataFrame) -> tuple[bool, str]:
    """The actual proof this is a real discrete-event simulation, not random rows: within each
    trip, leg_seq (the order events were generated in) must line up with event_time (the
    simulated clock) -- every event must happen no earlier than the one before it in the same
    trip. Returns (all_correct, message) so the caller can print AND embed the result.
    """
    bad_trips = []
    for trip_id, grp in events_df.sort_values(['trip_id', 'leg_seq']).groupby('trip_id'):
        times = grp['event_time'].tolist()
        if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
            bad_trips.append(trip_id)
    if bad_trips:
        return False, f'FAILED: {len(bad_trips)} trip(s) have out-of-order timestamps: {bad_trips[:5]}'
    n_trips = events_df['trip_id'].nunique()
    return True, f'PASSED: all {n_trips} trips have monotonically non-decreasing event_time across their full status sequence.'


def _verify_reward_algebra(transitions_df: pd.DataFrame) -> str:
    """Second correctness check, the one that caught bug #3 above: hos_maintenance_risk_penalty is
    algebraically recovered (order_revenue - deadhead_cost - opportunity_cost_penalty -
    immediate_reward) from real stored fields, and both risk components it represents
    (hos_stranding_risk_penalty, maintenance_risk_penalty) are non-negative by construction in
    reward.py -- so this column must never be negative. A wrong deadhead figure made it go
    negative before the fix.
    """
    if transitions_df.empty:
        return 'no training_transitions rows for this run yet -- run sim/training/extract_transitions.py first.'
    n_bad = (transitions_df['hos_maintenance_risk_penalty'] < -0.01).sum()
    if n_bad:
        return f'FAILED: {n_bad} row(s) have a negative combined risk penalty -- should be impossible.'
    return f'PASSED: all {len(transitions_df)} rows have a non-negative combined risk penalty (min={transitions_df["hos_maintenance_risk_penalty"].min():.4f}).'


def export(sim_id: str, out_path: str) -> None:
    with cursor(local=True) as cur:
        cur.execute('select * from sim.runs where sim_id = %s', (sim_id,))
        runs_df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

        cur.execute('select * from sim.orders where sim_id = %s order by created_at', (sim_id,))
        orders_df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

        cur.execute('select * from sim.assignments where sim_id = %s order by assigned_at', (sim_id,))
        assignments_df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

        cur.execute('select * from sim.trip_events where sim_id = %s order by trip_id, leg_seq', (sim_id,))
        events_df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

        cur.execute(
            """select sim_id, decision_time, driver_id, ST_AsText(driver_position::geometry) as driver_position_wkt,
                      driver_hos_remaining, driver_jurisdiction, truck_number, trailer_type,
                      trailer_capacity_lbs, trailer_capacity_pallets, order_id, order_weight,
                      order_load_type, order_service_type, order_loaded_miles, dest_distance_to_hub_km,
                      dest_local_order_density, promised_delivery_at,
                      order_revenue, deadhead_m_to_pickup, load_fill_ratio,
                      opportunity_cost_penalty, deadhead_cost, hos_maintenance_risk_penalty,
                      lateness_penalty, planned_driving_hours, planned_duty_hours, actual_duration_hours,
                      total_committed_distance_miles, driver_pool_size,
                      truck_breakdown_risk, truck_pct_km_interval, truck_pct_days_interval,
                      hour_of_day, day_of_week, action_taken, was_exploration,
                      immediate_reward, target_value, (target_value - immediate_reward) as continuation_value
               from training_transitions where sim_id = %s order by decision_time""",
            (sim_id,),
        )
        transitions_df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

    runs_df, orders_df, assignments_df, events_df, transitions_df = (
        _strip_tz(df) for df in (runs_df, orders_df, assignments_df, events_df, transitions_df)
    )

    ordering_ok, ordering_msg = _verify_time_ordering(events_df)
    algebra_msg = _verify_reward_algebra(transitions_df)
    print(f'Time-ordering check: {ordering_msg}')
    print(f'Reward-algebra check: {algebra_msg}')

    # --- Global chronological view: the SAME events as the 'sim_trip_events' sheet, but sorted
    # purely by the simulated clock (event_time) instead of grouped by trip -- this is what makes
    # the discrete-event nature visible: different trips/drivers interleave in real time order,
    # exactly as they would on an actual dispatch floor, not as isolated random sequences.
    time_ordered = events_df.sort_values('event_time').reset_index(drop=True)
    time_ordered.insert(0, 'global_seq', range(1, len(time_ordered) + 1))

    note = pd.DataFrame([{'note': msg} for msg in [
        f"TIME-ORDERING CHECK: {ordering_msg}",
        f"REWARD-ALGEBRA CHECK: {algebra_msg}",
        "See 'trip_events TIME-ORDERED' -- sorted by the simulated clock across ALL trips/drivers "
        "in this run, not grouped by trip. Different drivers' events interleave in real "
        "chronological order (a discrete-event simulation), which is what proves this isn't "
        "randomly generated rows -- a random generator would have no reason to keep every trip's "
        "own internal sequence (ASSGN before DISP before ARRSHIP...) valid AND consistent with a "
        "single shared clock across 100+ simultaneous trips.",
        "'training_transitions (REAL)' is the ACTUAL output of sim/training/extract_transitions.py "
        "for this run -- one row per assignment decision. target_value is what "
        "train_value_function.py will fit V(s) against (the fully-resolved realized reward, "
        "including post-delivery-deadhead/breakdown/lateness costs discovered after the decision "
        "was made). driver_position_wkt / driver_hos_remaining are the driver's real position/"
        "legal hours remaining at the moment this decision was made -- not their position after "
        "the trip. hos_maintenance_risk_penalty is algebraically recovered (not separately "
        "modeled) from reward.py's combined hos_stranding_risk_penalty + maintenance_risk_penalty "
        "-- distinct from lateness_penalty, which IS a real, separately-modeled appointment-"
        "lateness cost (promised_delivery_at vs. the trip's actual completion, convex in hours "
        "late past a small grace buffer). truck_pct_km_interval / truck_pct_days_interval are the "
        "two independent maintenance-overdue dimensions (odometer and calendar time) driving "
        "truck_breakdown_risk -- whichever is worse.",
    ]])

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        note.to_excel(writer, sheet_name='READ ME FIRST', index=False)
        transitions_df.to_excel(writer, sheet_name='training_transitions (REAL)', index=False)
        runs_df.to_excel(writer, sheet_name='sim_runs (1 row)', index=False)
        orders_df.to_excel(writer, sheet_name='sim_orders', index=False)
        assignments_df.to_excel(writer, sheet_name='sim_assignments', index=False)
        events_df.to_excel(writer, sheet_name='sim_trip_events (by trip)', index=False)
        time_ordered.to_excel(writer, sheet_name='trip_events TIME-ORDERED', index=False)

    print(f'Exported sim_id={sim_id}')
    print(f'  sim_runs: {len(runs_df)}, sim_orders: {len(orders_df)}, '
          f'sim_assignments: {len(assignments_df)}, sim_trip_events: {len(events_df)}, '
          f'training_transitions: {len(transitions_df)}')
    print(f'  -> {out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sim-id', default=None, help='defaults to the most recently started run')
    parser.add_argument('--out', default='sim_run_preview.xlsx')
    args = parser.parse_args()

    sim_id = args.sim_id or most_recent_sim_id()
    export(sim_id, args.out)
