"""Segment 2 (calibration): seeds calibration.*.

Known-good numbers already validated in analysis/data_analysis.ipynb are recomputed here from
the same source Excel using the same canonical sim/classify.py -- not re-derived with different
logic that could quietly drift from the validated figures (run_type_transition, dwell_time_dist,
hos_remaining_at_completion). Two tables are genuinely new -- lane_frequency and
order_arrival_rate -- computed using reference.locations, which didn't exist when the notebook
was written.

Reads the raw Excel directly rather than ground_truth.historical_legs: the run_type-transition
computation needs driver-chronological ordering (NAME + PLAN_DEPART), which isn't a column
ground_truth.historical_legs currently stores -- simpler to recompute from the same source both
the notebook and the loader already trust than to add that column for this one use.
"""
import pandas as pd
from psycopg2.extras import execute_values

from sim.classify import classify_run_type
from sim.config import (
    ASSUMED_BREAKDOWN_COST_CAD, ASSUMED_CAPACITY_VALUE_RATE_PER_LB, ASSUMED_DETENTION_RATE_PER_HR_CAD,
    ASSUMED_OPERATING_COST_PER_MILE, DETENTION_FREE_HOURS, LINEHAUL_RATE_TIERS_CAD_PER_MILE,
)
from sim.db import cursor
from sim.load_ground_truth import EXCEL_PATH, _clean, _normalize_city, load_reference_locations_lookup


def load_and_classify():
    tl = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Tlorder'))
    disp = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Dispatch'))
    on_on_trips = set(tl.loc[(tl['ORIGPROV'] == 'ON') & (tl['DESTPROV'] == 'ON'), 'TRIP_NUMBER'].dropna())
    dispatch = disp[disp['TRIP_NUMBER'].isin(on_on_trips)].copy()
    dispatch['run_type'] = classify_run_type(dispatch)
    return tl, dispatch


def build_run_type_transition(dispatch: pd.DataFrame):
    d = dispatch.sort_values(['NAME', 'PLAN_DEPART', 'TRIP_NUMBER', 'LS_LEG_SEQ']).copy()
    d['next_run_type'] = d.groupby('NAME')['run_type'].shift(-1)
    transitions = d.dropna(subset=['next_run_type'])
    matrix = pd.crosstab(transitions['run_type'], transitions['next_run_type'])
    probs = matrix.div(matrix.sum(axis=1), axis=0)

    rows = [
        (frm, to, float(probs.loc[frm, to]))
        for frm in probs.index for to in probs.columns
        if probs.loc[frm, to] > 0
    ]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.run_type_transition (from_run_type, to_run_type, probability)
               values %s on conflict (from_run_type, to_run_type)
               do update set probability = excluded.probability""",
            rows,
        )
    print(f'  run_type_transition: {len(rows)} rows')


def build_dwell_time_dist(dispatch: pd.DataFrame, tl: pd.DataFrame):
    loaded_legs = dispatch[dispatch['LS_MT_LOADED'] == 'L'].copy()
    pickup_legs = loaded_legs.loc[loaded_legs.groupby('TRIP_NUMBER')['LS_LEG_SEQ'].idxmin()]
    delivery_legs = dispatch.loc[dispatch.groupby('TRIP_NUMBER')['LS_LEG_SEQ'].idxmax()]
    tl_agg = tl.groupby('TRIP_NUMBER').agg(
        ACTUAL_PICKUP=('ACTUAL_PICKUP', lambda s: pd.to_datetime(s, errors='coerce').min()),
        ACTUAL_DELIVERY=('ACTUAL_DELIVERY', lambda s: pd.to_datetime(s, errors='coerce').max()),
    ).reset_index()

    pu = pickup_legs.merge(tl_agg[['TRIP_NUMBER', 'ACTUAL_PICKUP']], on='TRIP_NUMBER')
    pu['arrive'] = pd.to_datetime(pu['LS_DET_PICK_ARRIVE'], errors='coerce')
    pu['dwell_h'] = (pu['ACTUAL_PICKUP'] - pu['arrive']).dt.total_seconds() / 3600
    pu = pu[pu['dwell_h'] >= 0]

    dv = delivery_legs.merge(tl_agg[['TRIP_NUMBER', 'ACTUAL_DELIVERY']], on='TRIP_NUMBER')
    dv['arrive'] = pd.to_datetime(dv['LS_DET_DELV_ARRIVE'], errors='coerce')
    dv['dwell_h'] = (dv['ACTUAL_DELIVERY'] - dv['arrive']).dt.total_seconds() / 3600
    dv = dv[dv['dwell_h'] >= 0]

    rows = []
    for phase, df in (('pickup', pu), ('delivery', dv)):
        for run_type, grp in df.groupby('run_type'):
            vals = grp['dwell_h'].dropna()
            if len(vals) >= 5:  # ignore run types with too few samples to trust a quantile
                rows.append((
                    run_type, phase,
                    float(vals.quantile(.25) * 60), float(vals.median() * 60), float(vals.quantile(.75) * 60),
                ))

    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.dwell_time_dist (run_type, phase, p25_minutes, median_minutes, p75_minutes)
               values %s on conflict (run_type, phase) do update set
               p25_minutes = excluded.p25_minutes, median_minutes = excluded.median_minutes, p75_minutes = excluded.p75_minutes""",
            rows,
        )
    print(f'  dwell_time_dist: {len(rows)} rows')


def build_hos_remaining_at_completion(dispatch: pd.DataFrame):
    """NOTE: REMAINING_HOURS is a live snapshot in the source data, not a true historical value
    for the trip it's attached to (a real limitation found and worked through earlier this
    session). Used here only to give the simulator's driver-initialization a realistic-looking
    STARTING distribution shape by run type -- not presented as historically accurate.
    """
    last_leg = dispatch.loc[dispatch.groupby(['NAME', 'TRIP_NUMBER'])['LS_LEG_SEQ'].idxmax()]
    medians = last_leg.groupby('run_type')['REMAINING_HOURS'].median()
    rows = [(rt, float(v)) for rt, v in medians.items() if pd.notna(v)]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.hos_remaining_at_completion (run_type, median_hours)
               values %s on conflict (run_type) do update set median_hours = excluded.median_hours""",
            rows,
        )
    print(f'  hos_remaining_at_completion: {len(rows)} rows')


def build_lane_frequency(tl: pd.DataFrame, location_lookup: dict[str, int]):
    on_on = tl[(tl['ORIGPROV'] == 'ON') & (tl['DESTPROV'] == 'ON')].copy()
    on_on['origin_id'] = on_on['ORIGCITY'].apply(lambda c: location_lookup.get(_normalize_city(c)))
    on_on['dest_id'] = on_on['DESTCITY'].apply(lambda c: location_lookup.get(_normalize_city(c)))
    matched = on_on.dropna(subset=['origin_id', 'dest_id'])

    freq = matched.groupby(['origin_id', 'dest_id']).size().reset_index(name='weight')
    rows = [(int(r.origin_id), int(r.dest_id), float(r.weight)) for r in freq.itertuples()]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.lane_frequency (origin_location_id, dest_location_id, weight)
               values %s on conflict (origin_location_id, dest_location_id) do update set weight = excluded.weight""",
            rows,
        )
    print(f'  lane_frequency: {len(rows)} distinct lanes ({len(matched)}/{len(on_on)} orders matched both ends)')


def build_order_arrival_rate(tl: pd.DataFrame):
    """Poisson lambda per (hour_of_day, day_of_week) cell -- average order-creation count per
    calendar-date actually observed for that cell, from the real CREATED_TIME timestamps.
    """
    on_on = tl[(tl['ORIGPROV'] == 'ON') & (tl['DESTPROV'] == 'ON')].copy()
    on_on['created'] = pd.to_datetime(on_on['CREATED_TIME'], errors='coerce')
    on_on = on_on.dropna(subset=['created'])
    on_on['hour'], on_on['dow'], on_on['date'] = on_on['created'].dt.hour, on_on['created'].dt.dayofweek, on_on['created'].dt.date

    counts = on_on.groupby(['hour', 'dow']).size()
    days_per_dow = on_on.groupby('dow')['date'].nunique()
    rows = [
        (int(hour), int(dow), float(cnt / days_per_dow.get(dow, 1)))
        for (hour, dow), cnt in counts.items()
    ]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.order_arrival_rate (hour_of_day, day_of_week, lambda)
               values %s on conflict (hour_of_day, day_of_week) do update set lambda = excluded.lambda""",
            rows,
        )
    print(f'  order_arrival_rate: {len(rows)} (hour, day-of-week) cells')


def build_order_lead_time_hours(tl: pd.DataFrame):
    """REAL lead-time samples (CREATED_TIME -> ACTUAL_PICKUP) -- how far in advance an order was
    actually booked before pickup. Replaces the old assumption that an order must be dispatched
    the instant it appears (documents/logs/17): real freight has quotes/advance bookings, not
    same-instant dispatch only. Bootstrap-sampled at sim runtime, same treatment as order_pool's
    weight/pallets/load_type -- real observed values, not a fitted curve.
    """
    on_on = tl[(tl['ORIGPROV'] == 'ON') & (tl['DESTPROV'] == 'ON')].copy()
    on_on['created'] = pd.to_datetime(on_on['CREATED_TIME'], errors='coerce')
    on_on['pickup'] = pd.to_datetime(on_on['ACTUAL_PICKUP'], errors='coerce')
    on_on = on_on.dropna(subset=['created', 'pickup'])
    on_on['lead_hours'] = (on_on['pickup'] - on_on['created']).dt.total_seconds() / 3600
    valid = on_on[on_on['lead_hours'] >= 0]

    with cursor() as cur:
        cur.execute('truncate table calibration.order_lead_time_hours')
        execute_values(
            cur,
            'insert into calibration.order_lead_time_hours (lead_hours) values %s',
            [(float(h),) for h in valid['lead_hours']],
        )
    print(f'  order_lead_time_hours: {len(valid)} real samples (median {valid["lead_hours"].median():.1f}h)')


def build_assumptions():
    """Every synthesized $/probability constant, one row each, with the reasoning inline --
    everything downstream (reward.py, backtests, the live inference function, the dashboard)
    reads these rows instead of hardcoding the value a second time. sim/config.py is where the
    Python constants live; this is what makes them the runtime source of truth.
    """
    # One row per linehaul-rate distance tier -- see sim/config.py's LINEHAUL_RATE_TIERS_CAD_PER_MILE
    # for the sourcing (real 2026 short-haul-premium market-rate research, not a flat guess).
    tier_labels = ['0_50mi', '50_100mi', '100_150mi', '150mi_plus']
    rate_tier_rows = [
        (f'linehaul_rate_per_mile_{label}', rate, 'CAD/mile',
         f'Synthesized but sourced -- real short-haul-premium market-rate research, tier {label}.')
        for label, (_, rate) in zip(tier_labels, LINEHAUL_RATE_TIERS_CAD_PER_MILE)
    ]
    rows = [
        *rate_tier_rows,
        ('operating_cost_per_mile', ASSUMED_OPERATING_COST_PER_MILE, 'CAD/mile',
         'Synthesized -- cost to run the truck, distinct from the linehaul rate the shipper pays.'),
        ('detention_rate_per_hr_cad', ASSUMED_DETENTION_RATE_PER_HR_CAD, 'CAD/hour',
         'Synthesized -- mid-point of the $50-100/hr industry range in domain_deep_dive_and_eda_plan.md.'),
        ('detention_free_hours', DETENTION_FREE_HOURS, 'hours',
         'From the Project Brief -- contractual assumption, not derived from data.'),
        ('breakdown_cost_cad', ASSUMED_BREAKDOWN_COST_CAD, 'CAD',
         'Synthesized but sourced -- real tow+repair+downtime cost research; used both as the '
         'decision-time expected-cost basis and the realized-event penalty (see maintenance.py).'),
        ('capacity_value_rate_per_lb', ASSUMED_CAPACITY_VALUE_RATE_PER_LB, 'CAD/lb',
         'Synthesized -- proxy for the reward function\'s opportunity-cost-of-unused-capacity term.'),
    ]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into calibration.assumptions (key, value, unit, rationale)
               values %s on conflict (key) do update set
               value = excluded.value, unit = excluded.unit, rationale = excluded.rationale""",
            rows,
        )
    print(f'  assumptions: {len(rows)} rows')


if __name__ == '__main__':
    print('Loading and classifying ON-ON dispatch legs...')
    tl, dispatch = load_and_classify()

    print('calibration.run_type_transition:')
    build_run_type_transition(dispatch)

    print('calibration.dwell_time_dist:')
    build_dwell_time_dist(dispatch, tl)

    print('calibration.hos_remaining_at_completion:')
    build_hos_remaining_at_completion(dispatch)

    print('calibration.lane_frequency (needs reference.locations):')
    lookup = load_reference_locations_lookup()
    build_lane_frequency(tl, lookup)

    print('calibration.order_arrival_rate:')
    build_order_arrival_rate(tl)

    print('calibration.order_lead_time_hours:')
    build_order_lead_time_hours(tl)

    print('calibration.assumptions:')
    build_assumptions()

    print('done.')
