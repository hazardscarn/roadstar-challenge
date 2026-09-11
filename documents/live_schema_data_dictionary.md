# RoadStar Dispatch — `live` Schema Documentation & Data Dictionary

This document is the single-place reference for the real Postgres/Supabase schema behind the
RoadStar dispatch platform: every table in the `live` schema, what it's for, every column, and
how a quote turns into a trip, a geofenced dock visit, a detention bill, and an invoice.
It exists to show judges — in one place — how the pipeline described piecemeal in the hackathon
brief (quoting, ELD/HOS, TMS/dispatch, geofence + detention billing, maintenance) was actually
built as one real, queryable Postgres schema in Supabase, not five disjointed tools.

Companion diagrams (`documents/diagrams/`, exported from the same live schema, JPEG + editable
`.drawio` source):

| File | What it shows |
| --- | --- |
| `01_pipeline_data_flow.jpg` | The end-to-end **process flow** — quote → candidate scoring → assignment → geofenced trip in progress → detention billing → trip completion → costing/invoicing → ratings — labeled with the exact table each step reads/writes. |
| `02_erd_dispatch_pipeline.jpg` | **ER diagram**: `quote_requests`, `quote_candidate_snapshots`, `quote_recommendations`, `trips`, `driver_status` — full column list, types, PK/FK. |
| `03_erd_trip_lifecycle_billing.jpg` | **ER diagram**: `trips`, `geofence_events`, `geofence_dwell_state`, `detention_billing`, `trip_log`, `trip_costs`, `invoices`. |
| `04_erd_fleet_state_ratings.jpg` | **ER diagram**: `driver_state_snapshots`, `position_history`, `truck_maintenance_state`, `vehicle_inspections`, and the `driver_ratings`/`truck_ratings` views. |

Every ERD column line already carries its own data dictionary entry (`FK column_name: type  →
external_table` for anything pointing outside `live`) — this document adds the *why*, the
business rules, and the columns' plain-English meaning that don't fit in a diagram box.

## 1. How this maps to the hackathon brief

| Brief requirement | Built as |
| --- | --- |
| Quoting & rate tools | `quote_requests` + `quote_candidate_snapshots`/`quote_recommendations` (real scored candidates, not a black box) |
| TMS / dispatch | `trips` (the assignment record) + `driver_status` (live fleet state) |
| ELD / logbooks (HOS) | `driver_status.hos_remaining_hours` (live) and `driver_state_snapshots.hos_driving_hours_remaining` / `hos_duty_hours_remaining` / `hos_cycle1_hours_remaining` / `hos_cycle2_hours_remaining` (a real periodic snapshot of all four Canadian HOS limits — 13h driving, 14h on-duty, 70h/7d cycle 1, 120h/14d cycle 2) |
| Geofence & detention automated billing | `geofence_events` (timestamped arrival/departure), `geofence_dwell_state` (debounces GPS noise before confirming an event), `detention_billing` (auto-calculated from those timestamps, first 2h free) |
| Maintenance & accounting | `truck_maintenance_state`, `vehicle_inspections`, `trip_costs`, `invoices` |
| Deadhead & empty-mile reduction | `trip_log.post_delivery_deadhead_miles` (kept separate from `pre_pickup_deadhead_miles` on purpose — the real revenue-loss signal) |

## 2. Access control (RLS)

Every table below has Row Level Security enabled. Two policy shapes, applied consistently:

- **Managers read everything** — `exists (select 1 from public.profiles where user_id = auth.uid() and role = 'manager')`
- **Drivers read only their own rows** — `driver_id = (select driver_id from public.profiles where user_id = auth.uid())`, on the tables a driver has any business seeing (`driver_status`, `trips`, `vehicle_inspections`, `position_history`, `driver_state_snapshots`).

Writes to `driver_status` / `trips` / `geofence_events` / `quote_recommendations` come only from
the telemetry simulator or the scoring backend, using the Postgres superuser/service-role
connection, which bypasses RLS by design — there's no insert/update policy for authenticated
roles on those tables because the browser never writes them directly.

## 3. Table-by-table data dictionary

### 3.1 `quote_requests` — a shipper's request for a truck

One row per quote, from the moment a dispatcher enters it to its resolution (`assigned` /
`expired` / `cancelled`).

| Column | Type | Notes |
| --- | --- | --- |
| `quote_id` | uuid, **PK** | `gen_random_uuid()` |
| `origin_geog` / `dest_geog` | geography(Point,4326) | Raw pin fallback for a manually-typed address; the real pipeline resolves through `origin_location_id`/`dest_location_id` instead |
| `origin_location_id` / `dest_location_id` | integer, **FK → reference.locations** | The real lane the quote is scored against |
| `requested_at` | timestamptz | Quote/booking time — default `now()` |
| `requested_pickup_at` | timestamptz | When the shipper wants pickup — drives the whole downstream ETA/promise chain |
| `weight_lbs`, `pallets`, `load_type` | integer / integer / text | Cargo profile |
| `service_type` | text, default `'FTL'` | `FTL` or `LTL` |
| `status` | text, default `'open'` | `open` → `assigned` → (`expired` or `cancelled`) |

### 3.2 `quote_candidate_snapshots` — every candidate the model actually scored

The persisted audit trail of `score_quote.py`'s full candidate list — not just the one chosen.
One row per (quote, candidate driver) at scoring time.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | bigserial, **PK** | |
| `quote_id` | uuid, **FK → quote_requests** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `truck_number` | text, **FK → ground_truth.trucks** | |
| `location_id` | integer, **FK → reference.locations** | Candidate's location at scoring time |
| `hos_remaining_hours` | numeric | Real-time HOS margin used in the score |
| `truck_breakdown_risk` | numeric | 0–1, from the truck's maintenance-state projection |
| `truck_pct_km_interval` / `truck_pct_days_interval` | numeric | How far into the service interval (by km / by days) the truck is |
| `deadhead_miles` | numeric | Empty miles to reach pickup |
| `planned_driving_hours` / `planned_duty_hours` | numeric | Projected hours this trip would consume |
| `score` | numeric | The model's value score for this candidate |
| `rank` | integer | 1 = top-ranked |
| `was_assigned` | boolean | True for exactly the candidate actually dispatched |
| `scored_at` | timestamptz | default `now()` |

### 3.3 `quote_recommendations` — the top-N shown to the dispatcher

A slimmer, ranked view for the UI (composite PK `(quote_id, rank)`).

| Column | Type | Notes |
| --- | --- | --- |
| `quote_id` | uuid, **PK, FK → quote_requests** | |
| `rank` | integer, **PK** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `expected_revenue` / `expected_margin` | numeric | |
| `deadhead_miles` | numeric | |
| `hos_feasible` | boolean | Pre-dispatch HOS compliance audit result |
| `eta_pickup` | timestamptz | |
| `model_version` | text | Which trained model produced this recommendation |

### 3.4 `trips` — the assignment

Created the moment a dispatcher assigns a candidate; the anchor almost every other table hangs
off of.

| Column | Type | Notes |
| --- | --- | --- |
| `trip_id` | uuid, **PK** | `gen_random_uuid()` |
| `quote_id` | uuid, **FK → quote_requests** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `status` | text | `assigned` → `in_transit` → `at_pickup`/`at_delivery` → `completed` (or `cancelled`) |
| `last_event` | text | Raw event code (`ASSGN`, `DOCKED`, `DEPSHIP`, `ARRCONS`, `SLOWDOWN`, `COMPLETE`, …) |
| `eta` | timestamptz | Also doubles as "assigned at" timestamp in several read paths |
| `origin_location_id` / `dest_location_id` | integer, **FK → reference.locations** | |
| `created_at` | timestamptz | default `now()` |
| `weight_lbs`, `pallets`, `load_type` | numeric / numeric / text | Denormalized from the quote for Orders-page rendering |
| `loaded_miles` / `pre_pickup_deadhead_miles` | numeric | |
| `projected_hos_remaining_hours`, `projected_truck_pct_km_interval`, `projected_truck_pct_days_interval` | numeric | The feature state PROJECTED forward to trip completion, at assignment time |

### 3.5 `driver_status` — real-time fleet state (one row per driver, upserted)

| Column | Type | Notes |
| --- | --- | --- |
| `driver_id` | integer, **PK, FK → ground_truth.drivers** | |
| `updated_at` | timestamptz | default `now()` |
| `position` | geography(Point,4326) | Live lat/lon |
| `last_location_id` | integer, **FK → reference.locations** | Set only while inside a geofence |
| `speed_mph`, `odometer_km`, `fuel_pct` | numeric | Telematics |
| `hos_remaining_hours` | numeric | |
| `jurisdiction` | text | `'C'` (Canada, south of 60°N) or `'U'` (US) |
| `duty_status` | text | `driving` / `on_duty_not_driving` / `off_duty` |
| `current_trip_id` | uuid | |
| `truck_number` | text, **FK → ground_truth.trucks** | |
| `trailer_type`, `trailer_capacity_lbs`, `trailer_capacity_pallets` | text / integer / integer | Denormalized from `ground_truth.driver_equipment` for scoring speed |

### 3.6 `geofence_events` — the brief's "critical requirement"

The exact timestamp of every real dock arrival/departure, auto-recorded whenever a truck crosses
a geofence — this table is what makes automated detention billing possible at all.

| Column | Type | Notes |
| --- | --- | --- |
| `event_id` | bigserial, **PK** | |
| `trip_id` | uuid, **FK → trips** | |
| `location_id` | integer, **FK → reference.locations** | |
| `event_type` | text | `'arrival'` or `'departure'` |
| `occurred_at` | timestamptz | default `now()` |

### 3.7 `geofence_dwell_state` — debounces noisy real GPS before confirming an event

Composite PK `(trip_id, location_id)`. Real GPS pings flicker in/out of a geofence radius near
the boundary; this state machine requires the position to be stably inside/outside before it
writes a confirmed `geofence_events` row.

| Column | Type | Notes |
| --- | --- | --- |
| `trip_id` | uuid, **PK, FK → trips** | |
| `location_id` | integer, **PK, FK → reference.locations** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `inside_since` / `outside_since` | timestamptz | Debounce timers |
| `arrived_at` / `departed_at` | timestamptz | Confirmed once the debounce threshold is met |

### 3.8 `detention_billing` — automated, from real timestamps

One row per trip. `billable_hours` is a **generated column** — Postgres computes it directly from
`arrival_at`/`departure_at`, so it can never drift from the real geofence timestamps.

| Column | Type | Notes |
| --- | --- | --- |
| `trip_id` | uuid, **PK, FK → trips** | |
| `arrival_at` / `departure_at` | timestamptz | From `geofence_dwell_state` |
| `free_hours` | numeric, default `2` | The industry-standard free dock window the brief specifies |
| `billable_hours` | numeric, **generated** | `greatest(0, (departure_at - arrival_at) hours - free_hours)` |
| `amount` | numeric | `billable_hours × rate` |

### 3.9 `trip_log` — one row per completed trip (real or simulated, same shape)

The audit trail behind every driver/truck rating and the backtesting dataset.

| Column | Type | Notes |
| --- | --- | --- |
| `trip_id` | uuid, **PK, FK → trips** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `truck_number` | text, **FK → ground_truth.trucks** | |
| `completed_at` | timestamptz | default `now()` |
| `loaded_miles`, `pre_pickup_deadhead_miles`, `post_delivery_deadhead_miles` | numeric | The latter kept separate on purpose — the real revenue-loss signal |
| `pickup_dwell_hours` / `delivery_dwell_hours` | numeric | |
| `on_time` | boolean | Delivered within the appointment window |
| `load_fill_ratio` | numeric | |
| `hos_stranding_risk` | numeric | 0–1, how thin the driver's HOS margin was on this trip |
| `breakdown_occurred` / `breakdown_repair_hours` | boolean / numeric | default `false` / `0` |
| `reward_total` | numeric | The realized immediate reward, for backtesting the trained model |

### 3.10 `trip_costs` — internal margin view (never shown to the shipper)

| Column | Type | Notes |
| --- | --- | --- |
| `trip_id` | uuid, **PK, FK → trips** | |
| `loaded_miles`, `deadhead_miles`, `operating_cost_per_mile` | numeric | `operating_cost_per_mile` sourced from `calibration.assumptions` at run time |
| `fuel_and_operating_cost` | numeric, **generated** | `(loaded + deadhead miles) × cost/mile` |
| `maintenance_risk_cost` | numeric, default `0` | Expected breakdown cost at decision time |
| `realized_breakdown_cost` | numeric, default `0` | Only if this trip actually broke down |
| `lateness_penalty_cost` | numeric, default `0` | |
| `total_cost` | numeric, **generated** | Sum of the above |
| `revenue` | numeric | Denormalized from the quote/invoice |
| `margin` | numeric, **generated** | `revenue − total_cost` |

### 3.11 `invoices` — CRA-compliant, sent to the shipper

An invoice over CAD 150 must show the recipient, date, description of supply, and GST/HST
**separately** from the subtotal — reflected below as separate generated columns, not one bundled
total. Ontario HST is 13%, and CRA rules say tax follows the **delivery** province, not the
carrier's home province — hence `delivery_province` is its own column, not a hardcoded rate.

| Column | Type | Notes |
| --- | --- | --- |
| `invoice_id` | uuid, **PK** | |
| `invoice_number` | text, unique | Human-facing sequential number |
| `trip_id` | uuid, **FK → trips** | |
| `quote_id` | uuid, **FK → quote_requests** | |
| `issued_at` / `due_at` | timestamptz | |
| `bill_to_name` / `bill_to_address` | text | |
| `delivery_province` | text | Drives which province's HST/GST rate applies |
| `linehaul_amount`, `detention_amount`, `fuel_surcharge_amount`, `accessorial_amount` | numeric | Kept SEPARATE line items (not pre-summed) for CRA-compliant itemization |
| `subtotal` | numeric, **generated** | Sum of the 4 line items |
| `tax_rate` | numeric, default `0.13` | |
| `tax_amount` / `total_amount` | numeric, **generated** | |
| `status` | text, default `'draft'` | `draft` / `sent` / `paid` / `overdue` / `void` |
| `sent_to_email`, `sent_at`, `pdf_url` | text / timestamptz / text | |

### 3.12 `driver_state_snapshots` — periodic feature-state audit trail

A row every 15 minutes (live) or at every real trip-lifecycle event (simulation) — literally the
same feature vector the scoring model sees, persisted so Trip History can show *why* a decision
looked the way it did after the fact.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | bigserial, **PK** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `truck_number` | text, **FK → ground_truth.trucks** | |
| `trip_id` | uuid, **FK → trips** | |
| `snapshot_at` | timestamptz | default `now()` |
| `lat` / `lon` | double precision | |
| `duty_status` | text | |
| `hos_driving_hours_remaining` | numeric | 13h daily driving limit |
| `hos_duty_hours_remaining` | numeric | 14h daily on-duty limit |
| `hos_cycle1_hours_remaining` | numeric | 70h / 7-day cycle |
| `hos_cycle2_hours_remaining` | numeric | 120h / 14-day cycle |
| `truck_breakdown_risk`, `truck_pct_km_interval`, `truck_pct_days_interval` | numeric | |
| `inspection_ok` | boolean | Latest pre-trip inspection on file |

### 3.13 `position_history` — the brief's "Historical Breadcrumbs"

| Column | Type | Notes |
| --- | --- | --- |
| `id` | bigserial, **PK** | |
| `trip_id` | uuid, **FK → trips** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `position` | geography(Point,4326) | |
| `speed_mph` | numeric | |
| `recorded_at` | timestamptz, not null | |

### 3.14 `truck_maintenance_state` — one row per truck, projected forward

No real odometer/service-history data exists in the source export — this is a synthesized rule
for demo purposes, stated plainly rather than presented as backtested.

| Column | Type | Notes |
| --- | --- | --- |
| `truck_number` | text, **PK, FK → ground_truth.trucks** | |
| `cumulative_km_since_service` | numeric, default `0` | |
| `last_service_at` | timestamptz | |
| `service_interval_km` | numeric, default `25000` | |
| `service_interval_days` | integer, default `90` | |
| `maintenance_until` | timestamptz | Set while the truck is out of service |

### 3.15 `vehicle_inspections` — pre-trip DVIR

`overall_pass` is a **generated** column — a driver can't be dispatched on a failing inspection
by mistake, because the pass/fail isn't a separately-editable field.

| Column | Type | Notes |
| --- | --- | --- |
| `inspection_id` | uuid, **PK** | |
| `driver_id` | integer, **FK → ground_truth.drivers** | |
| `truck_number` | text, **FK → ground_truth.trucks** | |
| `submitted_at` | timestamptz | default `now()` |
| `odometer_km` | numeric | |
| `brakes_ok`, `tires_ok`, `lights_ok`, `fluid_levels_ok`, `coupling_ok`, `trailer_ok` | boolean | |
| `defects_noted` | text | |
| `overall_pass` | boolean, **generated** | `AND` of all 6 checks above |

### 3.16 `driver_ratings` (view) / `truck_ratings` (view)

Not maintained tables — computed live from `trip_log` on every read, so there's exactly one
source of truth.

**`driver_ratings`**: `driver_id`, `trips_completed`, `on_time_rate`, `avg_post_delivery_deadhead_miles`, `avg_hos_stranding_risk`, `avg_load_fill_ratio`.

**`truck_ratings`**: `truck_number`, `trips_completed`, `breakdown_count`, `avg_repair_hours_when_broken`, `avg_load_fill_ratio`.

## 4. External tables referenced (not part of `live`, shown for completeness)

| Table | Key columns | Role |
| --- | --- | --- |
| `ground_truth.drivers` | `driver_id` (PK), `home_zone`, `driver_type`, `pay_type`, `driver_cycle` (1 = 70h/7d, 2 = 120h/14d), `terminal_zone` | The real driver roster |
| `ground_truth.trucks` | `truck_number` (PK) | The real truck roster |
| `ground_truth.driver_equipment` | `driver_id`, `truck_number`, `trailer_type`, `trailer_capacity_lbs`, `trailer_capacity_pallets` | Real driver ↔ truck pairing on file |
| `reference.locations` | `location_id` (PK), `label`, `city`, `tier`, `geog`, `radius_m` (geofence radius) | Every real facility/terminal, geocoded |
| `public.profiles` | `user_id` (PK, FK → `auth.users`), `role` (`manager`/`driver`), `driver_id` | Drives every RLS policy above |

## 5. Note on `simulation.*`

A parallel schema, `simulation`, mirrors this exact shape (`simulation.trips`,
`simulation.trip_log`, `simulation.detention_billing`, `simulation.invoices`, …) plus a
`run_id`/`runs` header table — used ONLY by the Simulation Showcase, so a full simulated week can
be replayed and re-inspected without ever writing a row into `live.*`. Documented separately
(`sim/sql/038_simulation_schema.sql`) since it's a demo/showcase concern, not part of the live
production schema.
