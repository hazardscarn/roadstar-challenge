# Calibration Tables and OSRM Route Cache

**What:** `sim/build_calibration.py` (seeds `calibration.run_type_transition`,
`dwell_time_dist`, `hos_remaining_at_completion`, `lane_frequency`, `order_arrival_rate`,
`assumptions`) and `sim/osrm_cache.py` (seeds `calibration.lane_routes`). This is what makes the
simulator statistically realistic instead of arbitrary — every random draw the sim engine makes
(what run type comes next, how long a dwell takes, how many orders arrive this hour, which lane,
what the route looks like) now has a real number behind it instead of a guess.

## Why this reads the Excel again instead of `ground_truth.*`

`build_calibration.py`'s transition-matrix computation needs driver-chronological ordering
(`NAME` + `PLAN_DEPART` + `TRIP_NUMBER` + `LS_LEG_SEQ`) — the same adjacency logic validated
earlier for `classify_run_type`. `ground_truth.historical_legs` doesn't store `PLAN_DEPART`
(it wasn't needed for anything the loader itself does), so recomputing straight from the same
source Excel via the same canonical `sim/classify.py` was simpler than adding a column to
`ground_truth` for this one script — and it means these numbers are provably the same ones
already validated in `analysis/data_analysis.ipynb`, not a second implementation that could
quietly drift.

## What's seeded verbatim vs. freshly computed

- **`run_type_transition`** (40 rows) — the driver-chronological adjacency matrix from
  `data_analysis.ipynb` cell 34, turned into per-row conditional probabilities.
- **`dwell_time_dist`** (10 rows, p25/median/p75 in minutes, by run type × pickup/delivery) —
  pickup dwell = first loaded leg's `ACTUAL_PICKUP` (from `Tlorder`) minus `LS_DET_PICK_ARRIVE`;
  delivery dwell = last leg's `ACTUAL_DELIVERY` minus `LS_DET_DELV_ARRIVE`. Run types with fewer
  than 5 samples are dropped rather than trusting a noisy quantile.
- **`hos_remaining_at_completion`** (8 rows, median hours by run type) — **carries forward the
  caveat already found this session**: `REMAINING_HOURS` is a live snapshot in the source data,
  not a true historical value for the trip it's attached to. Used only to give the simulator's
  driver-initialization a realistic-looking *starting* distribution shape, never presented as
  historically accurate.
- **`lane_frequency`** (135 distinct lanes, freshly computed) — real ON-ON order counts per
  origin/destination pair, matched to `reference.locations` by normalized city name (1,777 of
  2,109 orders matched both ends — the same best-effort join used in the ground-truth loader).
  Couldn't exist before this session: needs `reference.locations`, which didn't exist when the
  notebook was written.
- **`order_arrival_rate`** (85 hour×day-of-week cells, freshly computed) — Poisson λ per cell,
  from real `CREATED_TIME` timestamps divided by the number of distinct calendar dates actually
  observed for that day-of-week.
- **`assumptions`** (6 rows) — every synthesized $/probability constant from `sim/config.py`
  (linehaul rate, operating cost, detention rate, detention-free hours, both breakdown-cost
  figures) with its rationale written inline. This is the runtime source of truth judge-visible
  components read from — `sim/config.py` is just where the Python-side constant lives.

## OSRM route cache scope — hub-anchored, not full pairwise

The platform plan calls for routes covering "every historical lane + reference.locations pairs."
Read literally as full pairwise, that's ~2,110² ≈ 4.45M routes — no sim mechanic needs a route
between two arbitrary locations that was never a real lane and isn't a hub leg (order generation
draws from real historical lanes via `lane_frequency`; empty-repositioning is hub-anchored).
Scoped instead to: every location routed to/from **both** terminal hubs, both directions (a
truck's deadhead-to-hub and dispatch-from-hub distance/ETA/geometry — 8,436 pairs), plus the 135
real historical lanes not already covered. **8,486 distinct routes total, all 8,486 succeeded**
(zero unreachable in the OSM road graph) — self-hosted OSRM at `http://localhost:5000`
(`roadstar-osrm` container), 16 concurrent requests, low latency since it's local.

`overview=simplified` + `geometries=geojson`: full-resolution geometry isn't needed for
map-marker animation and would bloat storage for no benefit on Supabase's free tier. Final table:
**9.2 MB** for all 8,486 rows including geometry.

**Sanity check**: London↔Milton in the cache — 143.4 km / 114.3 min — matches the earlier
manually-verified OSRM query from infrastructure setup exactly.

## Config addition

`sim/config.py` gained `OSRM_BASE_URL = 'http://localhost:5000'` — the one place any future
script (dashboard's live ETA lookups included) should read the OSRM endpoint from.
