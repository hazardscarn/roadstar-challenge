"""Freezes classify_run_type's output on hand-built fixtures.

This is the guard against silently regressing to the earlier 6-category version that lives in
analysis/driver-analysis.ipynb (same function name, per-trip-number adjacency instead of
per-driver-chronological). If this test breaks, someone changed the classification logic --
check it's deliberate, not a copy-paste from the wrong notebook.
"""
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.classify import classify_run_type, classify_trip_pattern

COLUMNS = [
    'NAME', 'PLAN_DEPART', 'TRIP_NUMBER', 'LS_LEG_SEQ', 'LS_FROM_ZONE', 'LS_TO_ZONE',
    'LS_LEG_DIST', 'WEIGHT_LBS', 'PALLETS', 'LS_FREIGHT', 'LS_MT_LOADED', 'LOAD_DESCRIPTION',
    'LS_NUM_TOTAL', 'LS_FREIGHT2', 'LS_FREIGHT3', 'LS_FREIGHT4',
]


def _row(**kwargs):
    base = dict(
        NAME='DriverX', PLAN_DEPART='2026-07-01', TRIP_NUMBER=1, LS_LEG_SEQ=1,
        LS_FROM_ZONE='A', LS_TO_ZONE='B', LS_LEG_DIST=10.0, WEIGHT_LBS=20000, PALLETS=10,
        LS_FREIGHT='BILL1', LS_MT_LOADED='L', LOAD_DESCRIPTION='General freight',
        LS_NUM_TOTAL=1, LS_FREIGHT2=None, LS_FREIGHT3=None, LS_FREIGHT4=None,
    )
    base.update(kwargs)
    return base


def test_single_ftl_leg():
    df = pd.DataFrame([_row()])
    result = classify_run_type(df)
    assert result.iloc[0] == 'Point-to-point single load (FTL)'


def test_multi_stop_is_ltl():
    df = pd.DataFrame([_row(LS_NUM_TOTAL=3)])
    assert classify_run_type(df).iloc[0] == 'Multi-stop consolidated freight (LTL)'


def test_pallet_return_run():
    df = pd.DataFrame([_row(LOAD_DESCRIPTION='Empty pallets and palletized returns')])
    assert classify_run_type(df).iloc[0] == 'Empty-pallet consolidation run'


def test_yard_shuttle_loaded():
    df = pd.DataFrame([_row(LS_FROM_ZONE='CPKGU', LS_TO_ZONE='CPKGU', LS_LEG_DIST=0.0)])
    assert classify_run_type(df).iloc[0] == 'Yard shuttle (loaded)'


def test_yard_shuttle_empty():
    df = pd.DataFrame([_row(LS_FROM_ZONE='CPKGU', LS_TO_ZONE='CPKGU', LS_LEG_DIST=0.0,
                             LS_MT_LOADED='E', WEIGHT_LBS=None, PALLETS=None, LS_FREIGHT=None)])
    assert classify_run_type(df).iloc[0] == 'Yard shuttle (empty trailer)'


def test_empty_to_pickup_and_after_delivery_cross_trip_number():
    # THE regression case: driver's empty leg (trip 1, last leg) is followed by a LOADED leg
    # in a DIFFERENT trip number (trip 2). Per-driver-chronological adjacency must see this as
    # "empty run to pickup" -- the earlier per-trip-number version would wrongly see prev/next
    # as null at the trip-1/trip-2 boundary and misclassify or fall through to "other".
    df = pd.DataFrame([
        _row(TRIP_NUMBER=1, LS_LEG_SEQ=1, PLAN_DEPART='2026-07-01 08:00', LS_MT_LOADED='L'),
        _row(TRIP_NUMBER=1, LS_LEG_SEQ=2, PLAN_DEPART='2026-07-01 08:00', LS_MT_LOADED='E',
             WEIGHT_LBS=None, PALLETS=None, LS_FREIGHT=None),
        _row(TRIP_NUMBER=2, LS_LEG_SEQ=1, PLAN_DEPART='2026-07-01 12:00', LS_MT_LOADED='L'),
    ])
    result = classify_run_type(df)
    assert result.iloc[1] == 'Empty run to pickup (deadhead approach)'


def test_trip_pattern_sums_to_distinct_trips():
    df = pd.DataFrame([
        _row(TRIP_NUMBER=1, LS_LEG_SEQ=1, LS_MT_LOADED='L'),
        _row(TRIP_NUMBER=2, LS_LEG_SEQ=1, LS_MT_LOADED='L'),
    ])
    df['run_type'] = classify_run_type(df)
    pattern = classify_trip_pattern(df)
    assert pattern.index.nunique() == df['TRIP_NUMBER'].nunique() == 2
