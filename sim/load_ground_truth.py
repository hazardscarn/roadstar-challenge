"""Segment 2: load the real historical Excel export into ground_truth.*, applying every fix in
documents/data_dictionary.md's "Known data-quality issues" list at load time, not after.

Scope: Ontario-to-Ontario orders only (ORIGPROV='ON' & DESTPROV='ON'), matching the region this
whole build targets (sim/config.py's COVERAGE_BBOX) and the scope already established across
analysis/data_analysis.ipynb.
"""
import re

import pandas as pd
from psycopg2.extras import execute_values

from sim.classify import classify_run_type
from sim.config import CAPACITY_BY_LOAD_TYPE
from sim.db import cursor

EXCEL_PATH = 'data/1788655393951_Hackathon_Data.xlsx'


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Known Issue #8: '<null>' is a literal string sentinel, not a real NaN."""
    return df.replace('<null>', pd.NA)


def _v(x):
    """pd.NA/NaN -> None. psycopg2 can't adapt pandas' NA sentinel directly."""
    return None if pd.isna(x) else x


def _normalize_city(text) -> str | None:
    """Known Issue #6: 'MILTON, ON' / 'MILTON,ON' / 'MILTON ,ON' are the same city."""
    if pd.isna(text):
        return None
    return re.sub(r'\s+', ' ', str(text).strip().upper()).split(',')[0].strip()


def load_reference_locations_lookup() -> dict[str, int]:
    """city (normalized) -> location_id, for best-effort joins from historical rows to the
    real geocoded locations built in Segment 1. Best-effort by design: the historical export's
    city names don't perfectly match the geocoded set (different shippers, different eras of
    the same TMS), so unmatched rows just get a null location_id -- fine for backtesting, which
    reads distances/timestamps straight from the Excel columns, not from this join.
    """
    with cursor() as cur:
        cur.execute("select location_id, city from reference.locations where city is not null")
        rows = cur.fetchall()
    lookup = {}
    for location_id, city in rows:
        norm = _normalize_city(city)
        if norm and norm not in lookup:  # first match wins, arbitrary but deterministic
            lookup[norm] = location_id
    return lookup


def load_drivers():
    drv = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Driver'))
    drv = drv.dropna(subset=['DRIVER_ID'])  # Known Issue #12: 38 blank placeholder rows
    rows = [
        (
            int(r.DRIVER_ID), _v(r.HOME_ZONE), _v(r.DRIVER_TYPE), _v(r.PAY_TYPE), _v(r.DRIVER_CYCLE),
            _v(r.DRIVER_CYCLE_ZONE), _v(r.DEFAULT_PUNIT), _v(r.TERMINAL_ZONE), _v(r.OTHER_CODE),
        )
        for r in drv.itertuples()
    ]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into ground_truth.drivers
               (driver_id, home_zone, driver_type, pay_type, driver_cycle, driver_cycle_zone,
                default_punit, terminal_zone, other_code)
               values %s on conflict (driver_id) do nothing""",
            rows,
        )
    print(f'  drivers: {len(rows)} loaded (of {len(drv) + 38} raw rows, 38 blank placeholders dropped)')

    # driver_equipment: DEFAULT_PUNIT -> Trucks join only (18 of ~131 active drivers have it --
    # a real, known gap, not a loading bug). Trailer type/capacity is deliberately left null:
    # no data supports a FIXED trailer per driver, so compatibility/capacity is resolved per
    # order at assignment time from the order's own load_type (sim/config.py CAPACITY_BY_LOAD_TYPE).
    equipped = drv.dropna(subset=['DEFAULT_PUNIT'])
    eq_rows = [(int(r.DRIVER_ID), r.DEFAULT_PUNIT) for r in equipped.itertuples()]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into ground_truth.driver_equipment (driver_id, truck_number)
               values %s on conflict (driver_id) do nothing""",
            eq_rows,
        )
    print(f'  driver_equipment: {len(eq_rows)} loaded (drivers with a known DEFAULT_PUNIT)')


def load_trucks():
    trucks = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Trucks'))
    rows = [(r.TRUCK_NUMBER,) for r in trucks.itertuples()]
    with cursor() as cur:
        execute_values(
            cur,
            "insert into ground_truth.trucks (truck_number) values %s on conflict (truck_number) do nothing",
            rows,
        )
    print(f'  trucks: {len(rows)} loaded')


def load_trailers():
    trailers = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Trailers'))
    rows = [
        (r.TRAILER_NUMBER, r.TRAILER_TYPE, int(r.CAPACITY_LBS))
        for r in trailers.itertuples()
    ]
    with cursor() as cur:
        execute_values(
            cur,
            """insert into ground_truth.trailers (trailer_number, trailer_type, capacity_lbs)
               values %s on conflict (trailer_number) do nothing""",
            rows,
        )
    print(f'  trailers: {len(rows)} loaded (roster only -- Known Issue #5: does not join to Dispatch.LS_TRAILER1)')


def load_orders(location_lookup: dict[str, int]) -> pd.DataFrame:
    tl = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Tlorder'))
    on_on = tl[(tl['ORIGPROV'] == 'ON') & (tl['DESTPROV'] == 'ON')].copy()

    on_on['was_dispatched'] = on_on['TRIP_NUMBER'].notna()  # Known Issue #1
    on_on['distance_miles'] = on_on['DISTANCE'].abs()        # Known Issue #3

    rows = []
    for r in on_on.itertuples():
        origin_id = location_lookup.get(_normalize_city(r.ORIGCITY))
        dest_id = location_lookup.get(_normalize_city(r.DESTCITY))
        temp_controlled = None if pd.isna(r.TEMP_CONTROLLED) else bool(r.TEMP_CONTROLLED)
        rows.append((
            str(r.BILL_NUMBER), _v(r.TRIP_NUMBER), _v(r.CALLNAME), origin_id, dest_id,
            _v(r.ACTUAL_PICKUP), _v(r.ACTUAL_DELIVERY),
            _v(r.distance_miles), _v(r.SERVICE_LEVEL), temp_controlled, _v(r.LOAD_TYPE),
            _v(r.LOAD_DESCRIPTION), None if pd.isna(r.WEIGHT_LBS) else int(r.WEIGHT_LBS),
            None if pd.isna(r.PALLETS) else int(r.PALLETS), _v(r.TEMPERATURE), bool(r.was_dispatched),
        ))
    with cursor() as cur:
        execute_values(
            cur,
            """insert into ground_truth.historical_orders
               (bill_number, trip_number, callname, origin_location_id, dest_location_id,
                actual_pickup, actual_delivery, distance_miles, service_level, temp_controlled,
                load_type, load_description, weight_lbs, pallets, temperature, was_dispatched)
               values %s on conflict (bill_number) do nothing""",
            rows,
        )
    print(f'  historical_orders: {len(rows)} loaded (ON-ON only, of {len(tl)} total rows in the sheet)')
    matched = sum(1 for row in rows if row[3] or row[4])
    print(f'    location match rate: {matched}/{len(rows)} orders got at least one end matched to reference.locations')
    return on_on


def load_legs(on_on_trip_numbers: set):
    disp = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Dispatch'))
    on_on = disp[disp['TRIP_NUMBER'].isin(on_on_trip_numbers)].copy()
    on_on['run_type'] = classify_run_type(on_on)

    rows = []
    for r in on_on.itertuples():
        rows.append((
            int(r.LS_LEG_ID), int(r.TRIP_NUMBER), int(r.LS_LEG_SEQ), _v(r.NAME),
            _v(r.LS_MT_LOADED), _v(r.LS_LEG_DIST),
            None if pd.isna(r.LS_LEG_WGT) else float(r.LS_LEG_WGT),
            r.LS_DET_PICK_ARRIVE if isinstance(r.LS_DET_PICK_ARRIVE, pd.Timestamp) else None,
            r.LS_DET_DELV_ARRIVE if isinstance(r.LS_DET_DELV_ARRIVE, pd.Timestamp) else None,
            _v(r.LAST_FB_STATUS), r.run_type,
        ))

    # NAME (e.g. "Driver13") -> ground_truth.drivers.driver_id via Driver.FIRST_NAME -- clean
    # join per the data dictionary, unlike the Tlorder 4-letter-code driver identifiers.
    drv = _clean(pd.read_excel(EXCEL_PATH, sheet_name='Driver')).dropna(subset=['DRIVER_ID'])
    name_to_id = dict(zip(drv['FIRST_NAME'], drv['DRIVER_ID'].astype(int)))

    final_rows = [
        (leg_id, trip_number, leg_seq, name_to_id.get(name), mt_loaded, leg_dist, leg_weight,
         det_pick, det_delv, last_fb, run_type)
        for (leg_id, trip_number, leg_seq, name, mt_loaded, leg_dist, leg_weight,
             det_pick, det_delv, last_fb, run_type) in rows
    ]

    with cursor() as cur:
        execute_values(
            cur,
            """insert into ground_truth.historical_legs
               (leg_id, trip_number, leg_seq, driver_id, mt_loaded, leg_dist, leg_weight,
                det_pick_arrive, det_delv_arrive, last_fb_status, run_type)
               values %s on conflict (leg_id) do nothing""",
            final_rows,
        )
    print(f'  historical_legs: {len(final_rows)} loaded (ON-ON only, of {len(disp)} total rows in the sheet)')
    print('  run_type distribution:')
    print(on_on['run_type'].value_counts().to_string())


if __name__ == '__main__':
    print('Loading ground_truth.* from the historical Excel export...')
    print('drivers/trucks/trailers:')
    load_trucks()
    load_trailers()
    load_drivers()  # driver_equipment references trucks -- must load trucks first

    print('reference.locations lookup:')
    lookup = load_reference_locations_lookup()
    print(f'  {len(lookup)} distinct normalized cities available for matching')

    print('orders:')
    on_on_orders = load_orders(lookup)

    print('legs:')
    load_legs(set(on_on_orders['TRIP_NUMBER'].dropna().unique()))

    print('done.')
