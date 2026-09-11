# Location Sourcing: Where Simulated/Live Order Coordinates Come From

**What:** `sim/geocode_locations.py` — populates `reference.locations` with real, geocoded points.
This is the hard blocker Segment 1 exists for: every other piece of the system (OSRM routing,
geofencing, the simulator's order generator, the dashboard map) needs a real lat/lng for every
place a truck goes, and the historical export doesn't have one — `Tlorder.ORIGCITY`/`DESTCITY`
are city names, and `Dispatch` zone codes are account codes, not geocodes (a known issue in the
source data, confirmed column-by-column in `documents/data_dictionary.md`).

**One thing worth being explicit about, since it's easy to assume otherwise: OSRM does not find
locations.** Given two lat/longs it already knows, OSRM computes the real driving distance,
duration, and route between them — that's it. It has no notion of "where is RONA's Milton
warehouse." Finding *where things are* is a geocoding/place-search problem, solved here by two
different free OpenStreetMap-backed services, both used *before* OSRM ever gets called:

## Tier A — the 59 real shippers (`tier = 'real_customer'`)

**What "59" actually is**: not a location-count decision — it's a fact about the historical data.
`Tlorder.CALLNAME` has 59 distinct named shippers (RONA, Lear Corp, CPK Interior Products,
Mondelez/Uber Freight, Electrolux, etc.). Each shipper ships to/from multiple cities, so the real
unit geocoded is the **151 distinct (shipper, city) pairs** across those 59 names, not 59 points.

**API used**: [Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap's geocoder) —
free, no API key. Query shape: `"<CALLNAME>, <CITY>, Ontario, Canada"`, falling back to just
`"<CITY>, Ontario, Canada"` if the specific business name doesn't resolve (anonymized/aggregated
business names in the source data don't always match a real single address).

**Usage policy compliance (not optional)**: max 1 request/second, and a real `User-Agent` header
identifying the app — both enforced in `sim/geocode_locations.py`, not left as informal
politeness. Every raw JSON response is cached to `sim/cache/nominatim/` (keyed by an MD5 hash of
the query) before being written to the database, so re-running the script during debugging never
re-hits the service or risks a rate-limit ban.

**Why this tier matters**: it anchors the simulator's demand generation to *real* historical
lanes — order generation draws from `calibration.lane_frequency`, which is built from these real
(shipper, city) pairs, so simulated freight looks like the fleet's actual freight mix, not
uniform random noise across the region.

## Tier B — the synthetic facility pool (`tier = 'synthetic_facility'`)

**Why it's needed**: 151 real points isn't enough origin/destination variety for 1,000+ Monte
Carlo simulation runs without everything converging onto the same handful of lanes.

**API used**: [Overpass API](https://overpass-api.de/) (OpenStreetMap's place-search/query
service) — free, no API key. One query, covering the entire Southern Ontario coverage bounding box
(`sim/config.py`'s `COVERAGE_BBOX`, a rough envelope of London/Milton/Barrie/Peterborough/
Pickering/Niagara Falls), asking for every node/way tagged `building=warehouse`,
`landuse=industrial`, or `building=industrial`. This is real infrastructure data — actual
industrial parks and warehouse buildings that exist in Southern Ontario, not invented addresses.

**Raw result**: Overpass returned **14,709 elements** for the full bounding box — far more than
needed (and more than the architecture doc's own "hundreds to low-thousands" expectation).
**Sampled down to 2,000** (fixed random seed for reproducibility) rather than inserting all of
it — enough geographic variety for 1,000+ simulation runs without bloating the table or the
insert time.

**A real inefficiency hit and fixed while building this**: the first version of the insert code
wrote one row at a time (`cur.execute` in a loop) over the network to remote Supabase. With
14,709 raw Tier B candidates that was on track to take many minutes just for round trips — fixed
by switching to a single bulk `execute_values` insert. Same fix pattern as everywhere else in this
build: no per-row network round-trips for bulk data.

**A deliberately accepted simplification**: the architecture doc's original design mentioned an
optional PostGIS `ST_DWithin` filter to keep only Tier B points within ~15km of a 400-series
highway centerline. That filter is **not implemented** — per the plan's own effort-budget note,
the bounding-box filter plus a manual spot-check is an accepted substitute given the time
available, not an oversight.

## The coverage box, and a correction made while building it

The Project Brief lists 4 named boundary cities (North: Barrie, East: Peterborough & Pickering,
West: London, South: Niagara Falls) plus 2 terminal hubs (London, Milton). Two issues were found
and fixed while turning that into an actual filter:

1. **A quad, not a pentagon**: the first attempt treated the 4 boundary cities as vertices of a
   5-point shape. The brief actually describes 4 independent cardinal edges — i.e. a rectangle
   (North=Barrie's latitude, South=Niagara Falls' latitude, West=London's longitude,
   East=whichever of Peterborough/Pickering is further east — Peterborough).
2. **The 4-edges-from-4-cities box was still wrong**: London's own latitude (42.98) is south of
   Niagara Falls' latitude (43.11), so using Niagara Falls strictly as the south edge would have
   put London itself outside its own box. Fixed by taking the min/max **envelope** across all 6
   named places (4 boundary cities + London + Milton) instead of one city per edge — this
   guarantees every one of them is actually inside. Verified directly: London, Barrie,
   Peterborough, Pickering, Niagara Falls, and Milton all fall inside the resulting box.
3. **A ~5km pad was added on every edge**: without it, a real address slightly south/west of a
   boundary city's own geocoded center point (a razor-thin edge) was being wrongly excluded from
   that same city — caught when a real London business (`LIXIL CANADA INC.`) got excluded from
   a box whose west edge *was* London's center point.

Final box (`sim/config.py COVERAGE_BBOX`): lat `[42.93, 44.44]`, lon `[-81.30, -78.27]`.

**A prior version of this pipeline had no geographic filter on Tier A at all** — the first run
inserted 43 real-but-out-of-region shippers (Sudbury, Timmins, Cochrane, Kapuskasing, Windsor,
Kingston, several Ottawa-area towns) that a real shipper (mostly RONA) genuinely ships to/from,
just outside the intended coverage region. Confirms a pattern already found earlier in the EDA
work (a real historical trip to Val Caron/Wahnapitae/New Liskeard, deep Northern Ontario) — real
freight, just out of scope for this hackathon's defined region. All 43 were removed once the
filter was added; every remaining location is now confirmed inside the box (verified: 0 rows
outside it).

## What actually got inserted (verified, after the fix)

| Tier | Count |
|---|---|
| `terminal_hub` | 2 |
| `real_customer` | 108 (of 151 candidate shipper/city pairs; 43 excluded as out-of-region) |
| `synthetic_facility` | 2,000 (sampled from 14,067 in-region candidates, out of 14,709 raw Overpass results) |
| **Total** | **2,110** |

**One known limitation**: a handful of `real_customer` labels share identical coordinates (e.g.,
two different Milton businesses both resolved to the same point) — this is Nominatim's
city-centroid fallback firing when the specific (anonymized) business name didn't resolve to a
real street address, not a bug. Acceptable for simulation purposes (it still gives a real city
location); would need real addresses to fix if ever needed for the live system.
