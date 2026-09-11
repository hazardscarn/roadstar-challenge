# Ground Truth Loading

**What:** `sim/load_ground_truth.py` — loads the real Excel export into `ground_truth.*`
(remote Supabase), scoped to Ontario-to-Ontario orders (matching the region this whole build
targets), applying `documents/data_dictionary.md`'s Known Issue fixes at load time.

**Why scoped to ON-ON**: `reference.locations` and the whole simulation are scoped to the
Southern Ontario coverage region — loading cross-border data into `ground_truth` would just be
dead weight nothing downstream uses.

## Fixes applied at load time

- **Known Issue #1** (undispatched orders): `was_dispatched = TRIP_NUMBER is not null`, kept not
  dropped.
- **Known Issue #3** (negative distances): `distance_miles = abs(DISTANCE)`.
- **Known Issue #6** (inconsistent city text formatting): city names normalized (strip, upper,
  drop trailing province) before being used as a join key to `reference.locations`.
- **Known Issue #8** (`'<null>'` string sentinel): replaced with real `NULL` on every sheet before
  any other processing.
- **Known Issue #12** (38 blank placeholder driver rows): dropped via `DRIVER_ID.notna()` —
  confirmed exactly 38 dropped (169 raw rows -> 131 loaded).
- **`run_type`**: every leg labeled via `sim/classify.py`'s canonical classifier at load time, not
  left for a later pass.
- **Driver-truck join**: `Driver.DEFAULT_PUNIT` -> `Trucks.TRUCK_NUMBER`, loaded as-is (18 of 131
  drivers have it — a real, known gap in the source data, not a loading bug). Trailer
  type/capacity is deliberately left null on `driver_equipment` — no data supports a fixed
  trailer per driver, so it's resolved per order at assignment time instead
  (`sim/config.py CAPACITY_BY_LOAD_TYPE`).
- **`Trailers.LS_TRAILER1` non-join (Known Issue #5)**: the trailer roster is loaded anyway (425
  rows) for reference/documentation, but nothing joins to it — trailer capacity comes from the
  order's `LOAD_TYPE`, per the same config constant.

## A data-quality issue found during loading, not in the original 12

**Duplicate primary keys in the raw export** — neither `Tlorder.BILL_NUMBER` nor
`Dispatch.LS_LEG_ID` is actually unique within the ON-ON scope, despite both looking like natural
keys:

- **30 distinct `BILL_NUMBER` values are duplicated (69 rows involved, 39 excess over the unique
  count — matches the 2,109 -> 2,070 gap below exactly)**. Some are the same bill logged twice
  with origin/destination swapped (e.g. `409061`: Port Hope->Guelph and Guelph->Port Hope, same
  trip number, same timestamp) — looks like a data-entry duplication, not two real shipments.
  Others (`401260D`) are exact duplicate rows.
- **8 distinct `LS_LEG_ID` values are duplicated, and every one of the 8 is tripled, not just
  doubled (24 rows involved, 16 excess)** — the same duplicate-record export artifact already
  documented earlier this session (multiple identical rows for one physical leg).

**Handling**: `insert ... on conflict (bill_number/leg_id) do nothing` — the second and later
occurrences of a duplicate key are silently skipped, not double-counted, and the load doesn't
crash. This is why the loader's own progress printout (2,109 orders / 5,192 legs *processed*)
doesn't match the final row count in the database (2,070 orders / 5,176 legs *stored*) — the gap
is exactly these duplicates, not data loss. Confirmed by re-querying the raw Excel directly, not
just inferred from the row-count mismatch.

## Verification against the notebook's known-good numbers

The loaded `run_type` distribution matches `analysis/data_analysis.ipynb`'s validated numbers
**exactly**: 2,683 FTL / 1,089 empty-to-pickup / 342 LTL / 327 empty-after-delivery / 324
other-empty / 190 yard-shuttle-loaded / 174 pallet-run / 63 yard-shuttle-empty. Strong confirmation
the loader's `classify_run_type` call and ON-ON scoping are correct.

## Final row counts

| Table | Rows |
|---|---|
| `ground_truth.drivers` | 131 |
| `ground_truth.trucks` | 131 |
| `ground_truth.trailers` | 425 |
| `ground_truth.driver_equipment` | 18 |
| `ground_truth.historical_orders` | 2,070 |
| `ground_truth.historical_legs` | 5,176 |

**Location match rate**: 2,070/2,070 orders matched at least one end to `reference.locations`
(1,745 matched both ends) — expected to be high since a small number of very-high-volume shippers
(RONA, Lear, etc.) dominate order volume and were exactly what Segment 1 geocoded.
**Driver match rate**: 4,924 of 5,176 legs resolved a `driver_id` via the `NAME` -> `FIRST_NAME`
join (clean per the data dictionary); the remainder have a null/unmatched `NAME` on the leg
itself, not a join failure.
