"""Segment 1: source real lat/lngs for reference.locations.

Tier A (real_customer): the 59 named shippers in the historical Tlorder data, geocoded at each
of their real ship-to cities via Nominatim -- anchors the simulator's demand to real historical
lanes, not just city names.

Tier B (synthetic_facility): real industrial/warehouse/logistics buildings across the whole
Southern Ontario coverage box, pulled from OpenStreetMap via Overpass -- gives the Monte Carlo
simulator enough location variety that 1,000+ simulated orders don't all cluster on the same 151
addresses.

Both sources are free, no API key. Nominatim's usage policy requires <=1 req/sec and a real
User-Agent identifying the app -- enforced below, not optional politeness. Every raw response is
cached to sim/cache/ before being written to the DB, so re-running this script while debugging
never re-hits either service.
"""
import hashlib
import json
import random
import time
from pathlib import Path

import pandas as pd
import requests
from psycopg2.extras import execute_values

from sim.config import COVERAGE_BBOX
from sim.db import cursor

# Overpass over this bbox returns ~15,000 raw elements -- far more than the "hundreds to
# low-thousands" the architecture plan expects, and most of it is small/duplicate-ish tagging
# noise. Cap to a random sample of this size for enough geographic variety without bloating the
# table or blowing up insert time.
TIER_B_SAMPLE_SIZE = 2000

CACHE_DIR = Path(__file__).parent / 'cache'
NOMINATIM_CACHE = CACHE_DIR / 'nominatim'
OVERPASS_CACHE = CACHE_DIR / 'overpass'
USER_AGENT = 'roadstar-hackathon-sim/1.0 (research use, contact: davidacad10@gmail.com)'


def _cache_path(cache_dir: Path, key: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f'{hashlib.md5(key.encode()).hexdigest()}.json'


def in_coverage(lat: float, lon: float) -> bool:
    """True if the point falls inside the Southern Ontario coverage box (sim/config.py)."""
    b = COVERAGE_BBOX
    return b['min_lat'] <= lat <= b['max_lat'] and b['min_lon'] <= lon <= b['max_lon']


def nominatim_geocode(query: str) -> dict | None:
    """One Nominatim lookup, cached. Returns the top result dict or None."""
    path = _cache_path(NOMINATIM_CACHE, query)
    if path.exists():
        results = json.loads(path.read_text())
    else:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': query, 'format': 'json', 'limit': 1, 'countrycodes': 'ca'},
            headers={'User-Agent': USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        path.write_text(json.dumps(results))
        time.sleep(1)  # Nominatim usage policy: max 1 req/sec
    return results[0] if results else None


def geocode_real_customers(tlorder: pd.DataFrame) -> list[dict]:
    """Tier A: distinct (CALLNAME, city) pairs from ON-ON orders."""
    on_on = tlorder[(tlorder['ORIGPROV'] == 'ON') & (tlorder['DESTPROV'] == 'ON')]
    pairs = pd.concat([
        on_on[['CALLNAME', 'ORIGCITY']].rename(columns={'ORIGCITY': 'CITY'}),
        on_on[['CALLNAME', 'DESTCITY']].rename(columns={'DESTCITY': 'CITY'}),
    ]).dropna().drop_duplicates()

    rows = []
    for i, (_, row) in enumerate(pairs.iterrows()):
        query = f"{row['CALLNAME']}, {row['CITY']}, Ontario, Canada"
        print(f'  [{i + 1}/{len(pairs)}] {query}')
        result = nominatim_geocode(query)
        if result is None:
            # fall back to just the city if the specific business name doesn't resolve
            result = nominatim_geocode(f"{row['CITY']}, Ontario, Canada")
        if result is None:
            print(f'    -> no match, skipping')
            continue
        lat, lon = float(result['lat']), float(result['lon'])
        if not in_coverage(lat, lon):
            print(f'    -> outside coverage region ({lat:.3f},{lon:.3f}), excluded')
            continue
        rows.append({
            'label': f"{row['CALLNAME']} ({row['CITY']})",
            'city': row['CITY'],
            'tier': 'real_customer',
            'lat': lat,
            'lon': lon,
            'source': 'nominatim',
        })
    return rows


def overpass_industrial_pois() -> list[dict]:
    """Tier B: real industrial/warehouse buildings across the coverage bounding box."""
    path = _cache_path(OVERPASS_CACHE, 'coverage_bbox_industrial')
    if path.exists():
        data = json.loads(path.read_text())
    else:
        bbox = COVERAGE_BBOX
        query = f"""
        [out:json][timeout:90];
        (
          node["building"="warehouse"]({bbox['min_lat']},{bbox['min_lon']},{bbox['max_lat']},{bbox['max_lon']});
          node["landuse"="industrial"]({bbox['min_lat']},{bbox['min_lon']},{bbox['max_lat']},{bbox['max_lon']});
          way["building"="industrial"]({bbox['min_lat']},{bbox['min_lon']},{bbox['max_lat']},{bbox['max_lon']});
          way["building"="warehouse"]({bbox['min_lat']},{bbox['min_lon']},{bbox['max_lat']},{bbox['max_lon']});
        );
        out center;
        """
        resp = requests.post(
            'https://overpass-api.de/api/interpreter',
            data={'data': query},
            headers={'User-Agent': USER_AGENT},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        path.write_text(json.dumps(data))

    elements = data.get('elements', [])
    print(f'  Overpass returned {len(elements)} raw elements (queried with an earlier, looser bbox)')

    # The cached query used an older, looser bbox -- re-filter to the current (tighter, precise)
    # COVERAGE_BBOX here rather than re-querying Overpass, then sample from what's left.
    in_region = []
    for el in elements:
        center = el.get('center', el if 'lat' in el else None)
        if center and in_coverage(center['lat'], center['lon']):
            in_region.append(el)
    print(f'  {len(in_region)} of those fall inside the current coverage box')

    if len(in_region) > TIER_B_SAMPLE_SIZE:
        random.seed(42)  # reproducible sample
        in_region = random.sample(in_region, TIER_B_SAMPLE_SIZE)
    print(f'  sampling down to {len(in_region)}')

    rows = []
    for el in in_region:
        center = el.get('center', el if 'lat' in el else None)
        if not center:
            continue
        name = el.get('tags', {}).get('name')
        label = f'{name} (synthetic, osm#{el["id"]})' if name else f'Synthetic facility osm#{el["id"]}'
        rows.append({
            'label': label,
            'city': None,
            'tier': 'synthetic_facility',
            'lat': center['lat'],
            'lon': center['lon'],
            'source': 'overpass',
        })
    return rows


def insert_locations(rows: list[dict]):
    """Bulk insert -- one round trip, not one per row (Tier B alone can be thousands of rows)."""
    if not rows:
        return
    values = [
        (row['label'], row['city'], row['tier'], f"POINT({row['lon']} {row['lat']})", row['source'])
        for row in rows
    ]
    with cursor() as cur:
        execute_values(
            cur,
            """
            insert into reference.locations (label, city, tier, geog, source, radius_m)
            values %s
            on conflict (label) do nothing
            """,
            values,
            template="(%s, %s, %s, ST_GeogFromText(%s), %s, 120)",
        )


if __name__ == '__main__':
    tlorder = pd.read_excel(
        'data/1788655393951_Hackathon_Data.xlsx', sheet_name='Tlorder'
    ).replace('<null>', pd.NA)

    print('Tier A: geocoding real named shippers via Nominatim...')
    tier_a = geocode_real_customers(tlorder)
    print(f'  -> {len(tier_a)} real_customer locations geocoded')
    insert_locations(tier_a)

    print('Tier B: pulling synthetic facility pool via Overpass...')
    tier_b = overpass_industrial_pois()
    print(f'  -> {len(tier_b)} synthetic_facility locations found')
    insert_locations(tier_b)

    print('done.')
