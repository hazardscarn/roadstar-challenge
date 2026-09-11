"""Run-type classification for Dispatch legs.

Extracted verbatim from analysis/data_analysis.ipynb (the CANONICAL, later 8-category version).

analysis/driver-analysis.ipynb defines an EARLIER, simpler 6-category function with the SAME
NAME (`classify_run_type`) that groups adjacency by ('NAME','TRIP_NUMBER') instead of by driver
across trip-number boundaries -- that version is superseded and must never be reused in
application code. See analysis/data_analysis.ipynb's own "Section 9" note and
research/roadstar_platform_plan.md for why trip-number-boundary-blind adjacency undercounts and
overcounts real deadhead-after-delivery legs.
"""
import pandas as pd


def classify_run_type(legs: pd.DataFrame) -> pd.Series:
    """Sort every Dispatch leg into one of 8 mutually exclusive run types.

    Adjacency is computed across each DRIVER's full chronological history, not reset at every
    trip-number boundary -- the truck doesn't know or care where one trip number ends and the
    next begins. Ordering within a trip still falls back to LS_LEG_SEQ.

    Requires columns: NAME, PLAN_DEPART, TRIP_NUMBER, LS_LEG_SEQ, LS_FROM_ZONE, LS_TO_ZONE,
    LS_LEG_DIST, WEIGHT_LBS, PALLETS, LS_FREIGHT, LS_MT_LOADED, LOAD_DESCRIPTION, LS_NUM_TOTAL,
    LS_FREIGHT2, LS_FREIGHT3, LS_FREIGHT4.
    """
    legs = legs.sort_values(['NAME', 'PLAN_DEPART', 'TRIP_NUMBER', 'LS_LEG_SEQ'])
    same_facility = legs['LS_FROM_ZONE'].astype(str) == legs['LS_TO_ZONE'].astype(str)
    zero_dist = legs['LS_LEG_DIST'] == 0
    has_freight = (legs['WEIGHT_LBS'].fillna(0) > 0) | (legs['PALLETS'].fillna(0) > 0) | legs['LS_FREIGHT'].notna()
    loaded, empty = legs['LS_MT_LOADED'] == 'L', legs['LS_MT_LOADED'] == 'E'
    pallet_run = legs['LOAD_DESCRIPTION'] == 'Empty pallets and palletized returns'
    multi_stop = (legs['LS_NUM_TOTAL'] > 1) | legs[['LS_FREIGHT2', 'LS_FREIGHT3', 'LS_FREIGHT4']].notna().any(axis=1)
    yard_zero = same_facility & zero_dist

    grp = legs.groupby('NAME')
    next_mt, prev_mt = grp['LS_MT_LOADED'].shift(-1), grp['LS_MT_LOADED'].shift(1)

    run_type = pd.Series('Point-to-point single load (FTL)', index=legs.index)
    run_type[loaded & multi_stop] = 'Multi-stop consolidated freight (LTL)'
    run_type[loaded & pallet_run] = 'Empty-pallet consolidation run'
    run_type[empty & (next_mt == 'L')] = 'Empty run to pickup (deadhead approach)'
    run_type[empty & (next_mt != 'L') & (prev_mt == 'L')] = 'Empty run after delivery (deadhead departure)'
    run_type[empty & (next_mt != 'L') & (prev_mt != 'L')] = 'Other / mid-chain empty repositioning'
    run_type[yard_zero & empty] = 'Yard shuttle (empty trailer)'
    run_type[yard_zero & loaded & has_freight] = 'Yard shuttle (loaded)'
    return run_type.reindex(legs.index)


def classify_trip_pattern(legs: pd.DataFrame) -> pd.Series:
    """Roll per-leg run_type up to one label per TRIP_NUMBER (zero-sum: sums to distinct trips).

    `legs` must already carry a `run_type` column (output of classify_run_type).
    """
    loaded_legs = legs[legs['LS_MT_LOADED'] == 'L']
    pickup_legs = loaded_legs.loc[loaded_legs.groupby('TRIP_NUMBER')['LS_LEG_SEQ'].idxmin()]
    core_type = pickup_legs.set_index('TRIP_NUMBER')['run_type']

    same_facility = legs['LS_FROM_ZONE'].astype(str) == legs['LS_TO_ZONE'].astype(str)
    real_deadhead = (legs['LS_MT_LOADED'] == 'E') & ~(same_facility & (legs['LS_LEG_DIST'] == 0))

    stats = pd.DataFrame({'TRIP_NUMBER': legs['TRIP_NUMBER'], '_deadhead': real_deadhead, '_loaded': legs['LS_MT_LOADED'] == 'L'}) \
        .groupby('TRIP_NUMBER').agg(num_loaded=('_loaded', 'sum'), has_deadhead=('_deadhead', 'any'))
    stats['core_type'] = core_type.reindex(stats.index)

    has_core = stats['core_type'].notna()
    round_trip = has_core & ~stats['has_deadhead'] & (stats['num_loaded'] > 1)
    single_leg = has_core & ~stats['has_deadhead'] & (stats['num_loaded'] == 1)
    with_deadhead = has_core & stats['has_deadhead']

    pattern = pd.Series('No load (pure repositioning trip)', index=stats.index)
    pattern[with_deadhead] = stats.loc[with_deadhead, 'core_type'] + ' + deadhead leg'
    pattern[round_trip] = stats.loc[round_trip, 'core_type'] + ' + loaded return (round trip)'
    pattern[single_leg] = stats.loc[single_leg, 'core_type'] + ' only (single load, no return)'
    return pattern
