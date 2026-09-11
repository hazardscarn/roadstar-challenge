# Data Dictionary — `1788655393951_Hackathon_Data.xlsx`

## What this file actually is

The sheet names and column naming convention (`TLORDER`/`Tlorder`, `DISPATCH`, `LS_*` prefixes,
`POSLAT`/`POSLONG`, `DRIVER_CYCLE`, `HOS_*`) match the schema of **McLeod Software's
LoadMaster/PowerBroker TMS** — a widely used real trucking dispatch system. This is almost
certainly a raw export from Road Star Trucking's actual production TMS database, lightly
anonymized (driver names replaced with `Driver1`, `Driver2`, …; emails synthesized). It is
**not** a purpose-built hackathon dataset — it's a real operational data dump, which is why it's
messy: it carries join artifacts, dead columns, and inconsistent formatting typical of a live
system-of-record rather than a curated CSV.

It is a **historical, completed-loads snapshot**, not a live feed: every row in `Dispatch` has
`STATUS = 'COMPLETE'` and `LS_LEG_STAT = 'FINISHED'`. There is no streaming GPS breadcrumb trail —
`Driver.POSLAT/POSLONG` is a single last-known point per driver, not a time series. This is
consistent with the Project Brief explicitly asking teams to build a **separate simulation
engine** to generate live-looking telemetry — the brief assumes you don't have real-time data,
because you don't.

Dates cluster **July–September 2026** (order creation) and **June–August 2026** (dispatch/planned
departure), i.e. the weeks immediately before/during the hackathon — this is recent real data, not
a stale archive.

## Sheets at a glance

| Sheet | Rows | Cols | Grain | Role |
|---|---|---|---|---|
| `Tlorder` | 4,031 | 33 | One row per **customer order / bill of lading** (an FTL or LTL shipment request) | "What needs to move" |
| `Dispatch` | 10,479 | 57 | One row per **dispatch leg** (a single driver/truck segment of a trip — multi-leg trips have several rows) | "How it actually moved" |
| `Driver` | 169 | 50 | One row per **driver** (snapshot of current HOS clock / duty state / last position) | Driver roster + live HOS state |
| `Trucks` | 131 | 1 | One row per **power unit ID** | Fleet roster (IDs only, no specs) |
| `Trailers` | 425 | 6 | One row per **trailer ID** | Trailer roster with equipment specs |

## Cross-sheet relationships

```
Tlorder.TRIP_NUMBER  <---->  Dispatch.TRIP_NUMBER      (order <-> its dispatch legs; mostly clean, see caveats)
Dispatch.NAME        <---->  Driver.FIRST_NAME         (leg <-> driver; clean match)
Driver.DEFAULT_PUNIT <---->  Trucks.TRUCK_NUMBER        (driver <-> assigned power unit; clean match)
Dispatch.LS_TRAILER1 <-/->  Trailers.TRAILER_NUMBER    (leg <-> trailer; BROKEN, see Known Issues #5)
```

A single customer order (`Tlorder` row) can require several dispatch legs (`Dispatch` rows) —
e.g. an empty repositioning move (`LS_MT_LOADED = 'E'`) followed by the loaded move
(`LS_MT_LOADED = 'L'`). `LS_LEG_SEQ` / `LEG_SEQUENCE` gives the order of legs within a trip.

---

## Sheet: `Tlorder` (customer orders)

| Column | Type | Description |
|---|---|---|
| `CREATED_TIME` | datetime | When the order was entered into the TMS |
| `BILL_NUMBER` | str/int | Order / bill-of-lading number. Suffixes like `D`, `-AA` mark split/added freight bills off a parent order |
| `TRIP_NUMBER` | float | Linked dispatch trip number, once the order has been booked/dispatched. **Null for 316 orders (7.8%) — these are orders that were *never dispatched***, see Known Issues #1 |
| `CALLNAME` | str | Shipper/customer name (59 distinct — see full list in analysis; dominated by RONA, Lear Corp, CPK Interior Products, Mondelez/Uber Freight, Electrolux, etc.) |
| `ORIGCITY` / `ORIGPROV` / `ORIGPC` | str | Pickup city / province-state / postal-zip code |
| `ACTUAL_PICKUP` | datetime | Actual pickup timestamp (6.3% null = not yet picked up) |
| `PICK_UP_DRIVER` / `PICK_UP_DRIVER2` | str | Driver code(s) who performed pickup (4-letter driver initials code, a *different* driver identifier scheme than `Driver.FIRST_NAME` — see Known Issues #2). `_DRIVER2` is a team-driver slot, 95%+ null (most legs are solo-driven) |
| `PICK_UP_TRIP` | float | Trip number at pickup (usually same as `TRIP_NUMBER`) |
| `DESTCITY` / `DESTPROV` / `DESTPC` | str | Delivery city / province-state / postal-zip code |
| `ACTUAL_DELIVERY` | datetime | Actual delivery timestamp |
| `DELIVERY_DRIVER` / `DELIVERY_DRIVER2` | str | Driver code(s) who performed delivery |
| `DELIVERY_TRIP` | float | Trip number at delivery |
| `DISTANCE` | float | Total order distance in miles. **59 rows have a negative value** — see Known Issues #3 |
| `SERVICE_LEVEL` | str | `REG` (regular, 94.6%) or `FLAT` (flat-rate, 5.4%) |
| `TEMP_CONTROLLED` | bool | Whether the load requires reefer temperature control |
| `TRIP_NUMBER_1`, `BILL_NUMBER_1`, `DETAIL_LINE_ID`, `LEG_SEQUENCE`, `CURRENTLY_ASSIGNED`, `ROW_TIMESTAMP`, `INS_TIMESTAMP` | mixed | **Duplicate/join-artifact columns** from a joined order-detail table. `TRIP_NUMBER_1` is byte-identical to `TRIP_NUMBER` wherever both are populated; treat these seven columns as redundant except that their null pattern (all null together, 316 rows) flags the undispatched orders |
| `LOAD_TYPE` | str | `Dry Van` (83.5%), `Reefer` (11.1%), `Flatbed` (5.4%) |
| `LOAD_DESCRIPTION` | str | Free-text-ish commodity description, but actually only **50 distinct canned values** (e.g. "Automotive seating and interior components", "Building materials, lumber, and hardware") — effectively a categorical field, safe to one-hot/embed |
| `WEIGHT_LBS` | int | Shipment weight, 502–48,000 lbs (48,000 ≈ US legal max gross minus tractor/trailer weight — sanity-checks as realistic) |
| `PALLETS` | int | Pallet count, 8–28 |
| `TEMPERATURE` | str | `Ambient` (89%) or a target reefer temp (`60 F`, `36 F`, `0 F`, `35 F`, `50 F`) |

## Sheet: `Dispatch` (dispatch legs / movements)

| Column | Type | Description |
|---|---|---|
| `LS_FREIGHT` / `LS_FREIGHT_1` | object | Freight/order number this leg is carrying (identical duplicate pair — join artifact). 34.9% null on **empty/repositioning legs**, which by definition carry no freight |
| `TRIP_NUMBER` | int | Trip this leg belongs to (matches `Tlorder.TRIP_NUMBER`) |
| `LS_LEG_ID` | int | Unique ID for this leg |
| `LS_LEG_SEQ` | int | Leg's order within the trip (1, 2, 3, …) |
| `NAME` | str | Driver assigned to this leg — matches `Driver.FIRST_NAME` |
| `LS_FROM_ZONE` / `LS_TO_ZONE`, `ORIGIN_ZONE` / `DESTINATION_ZONE` | str | **Facility/customer short-codes**, not stable geocodes — see Known Issues #4 |
| `LS_MT_LOADED` | str | `L` = loaded move, `E` = empty/deadhead move. **This is your ready-made "deadhead" label** for empty-mile-reduction work |
| `LS_LEG_STAT` | str | Always `FINISHED` in this extract (historical data only) |
| `LS_LEG_DIST` | float | Distance of this specific leg |
| `LS_LEG_WGT` | float | Weight carried on this leg |
| `LS_EXPECTED_DATE` | datetime | Planned date; **sentinel value `1980-01-01` in 68% of rows** means "not applicable/not set" — treat as null, not as an actual 1980 date |
| `STATUS` | str | Always `COMPLETE` (historical extract) |
| `LTL_STATUS` | str | Check-call status at last update: `DELVNG`, `PICKD`, `DISP`, `PICKNG`, `DECLINE` (60% null — mostly populated only for LTL-relevant legs) |
| `PICKUP_BY` / `DELIVER_BY` | datetime | Scheduled appointment window |
| `LS_ACTIVE_LEG` | int | Which leg (by sequence) was active on the trip |
| `LS_TRAILER1` / `LS_TRAILER2` | object/str | Trailer assigned to the leg. **`LS_TRAILER2` is 100% null (dead column)**. `LS_TRAILER1` does not join to the `Trailers` sheet — see Known Issues #5 |
| `LS_FREIGHT2/3/4` | object | Additional freight bill numbers for multi-stop LTL legs consolidating several orders (89–99% null — only populated on true multi-pickup/multi-drop legs) |
| `ORIG_ZONE_DESC`, `DEST_ZONE_DESC`, `ETA_ZONE_DESC`, `CURRENT_ZONE_DESC`, `LEGO_ZONE_DESC`, `LEGD_ZONE_DESC` | str | Human-readable "City, PROV" text for each zone code. **Inconsistent formatting**: `"MILTON, ON"` vs `"MILTON,ON"` vs `"MILTON ,ON"` — normalize before using as a join/group key or geocoding input |
| `EXTRA_STOPS` | str | 100% null — **dead column** |
| `PLAN_DEPART`, `LS_PLANNED_DEPARTURE`, `LS_SCHEDULED_ARRIVAL` | datetime | Planning timestamps |
| `LS_NUM_PU` / `LS_NUM_DEL` / `LS_NUM_TOTAL` | int | Number of pickups / deliveries / total stops on the leg (0–5) — your LTL multi-stop routing signal |
| `LS_PAY_DRIVER_MT_LEGS` | bool/null | Whether driver is paid for empty (MT) legs |
| `LS_PICKUP_BY(_END)`, `LS_DELIVER_BY(_END)` | datetime | Appointment window start/end (66–68% null — set mainly for scheduled-appointment freight) |
| `LAST_FB_STATUS` | str | Freight-bill lifecycle status — a genuine **event-log / process field**: `COMPLETE`, `DISP`, `SPTLD` (spotted trailer), `STOPOFF`, `DEPSHIP`, `ASSGN`, `PICKD`, `ARRCONS`, `ARRSHIP`, `AVAIL`, `DROMT` (drop empty), `DOCKED`. Combined with `LS_LAST_FB_STATUS_DATE`, this is a timestamped status sequence per leg — usable for process-mining/ETA modeling |
| `LS_DET_PICK_ARRIVE` / `LS_DET_DELV_ARRIVE` | datetime | **Actual dock arrival/departure timestamps at pickup and delivery** — this is the exact data the Project Brief's "Geofence & Detention Automated Billing" requirement is built on. ~64% populated; can be used to *compute real historical detention exposure*, not just simulate it |
| `LS_LAST_FB_STATUS_DATE` | datetime | Timestamp of the last status update |
| `LS_NUM_LEGS` | int | Total legs in the parent trip |
| `REMAINING_HOURS` | float | Driver's remaining legal drive hours at time of leg (7.6% null) |
| `HOS_VIOLATION_AT` | datetime | Projected time an HOS violation would occur if driving continued. **Sentinel dates in `2023-11`/`2023-12`** appear — likely a system default/epoch artifact, not real 2023 events; treat with suspicion, don't take literally |
| `LS_DANGEROUS_GOODS` | bool | Always `False` in this extract — **dead column for modeling purposes** (no variance) |
| `LS_TEMP_CONTROLLED` | bool | Reefer requirement flag for the leg |
| `LOAD_TYPE`, `LOAD_DESCRIPTION`, `WEIGHT_LBS`, `PALLETS`, `TEMPERATURE` | mixed | Same meaning as in `Tlorder`, joined down to leg level (34.9% null = empty/deadhead legs, which correctly carry no freight attributes) |

## Sheet: `Driver`

| Column | Type | Description |
|---|---|---|
| `DRIVER_ID` | float | Numeric driver ID, 1–169. **38 rows (22.5%) are entirely blank** except a handful of static fields — these look like deactivated/placeholder driver slots; filter them out before modeling |
| `HOME_ZONE` | str | Home terminal zone code (`RSTAR`, `WHIT`) |
| `DRIVER_TYPE` | str | `C` = Company driver, `O` = Owner-operator (standard trucking terminology) |
| `PAY_TYPE` | str | `V`, `P`, `D` — pay-structure code, values not self-explanatory in the export; confirm meaning with the host if it matters to your model (dominated by `V`, 71%) |
| `EMAIL`, `FIRST_NAME` | str | Anonymized identifiers (`DriverN@email.com`, `DriverN`) |
| `DRIVER_CYCLE` | float | `7` or `8` — this is the **US FMCSA cycle** (70hrs/7days or 60hrs/7days... commonly the 7-day vs 8-day cycle election), distinct from the Canadian Cycle 1/Cycle 2 the Project Brief describes |
| `DRIVER_CYCLE_ZONE` | str | `U` (US) / `C` (Canada) — which jurisdiction's HOS rules currently govern this driver. **This is a strong signal the fleet runs cross-border US/Canada freight**, which the Project Brief's HOS section (Canada-only) doesn't fully address — a good "edge case" to raise |
| `REMAINING_HOURS` | float | Current remaining legal drive hours |
| `CUMULATIVE_HOURS`, `DAYS_7`, `DAYS_8`, `DAYS_14`, `HOS_CUMULATIVE_HOURS` | float | **100% null — dead columns**, not populated in this export |
| `HOURS_UPDATED` | datetime | Last time the HOS clock was refreshed |
| `REMAINING_HOURS_CAN_7/8/14` | float | Remaining hours under Canadian Cycle 1 (7-day) and Cycle 2 (14-day) rules, and an 8-day variant |
| `REMAINING_HOURS_US_7/8` | float | Remaining hours under US 7-day/8-day cycle rules |
| `CURRENT_DUTY` | float | Numeric duty-status code (0–4) — mapping not labeled in the export (typically Off-Duty/Sleeper/Driving/On-Duty-Not-Driving in ELD systems); confirm before using as ground truth |
| `DUTY_AT` | datetime | Time the current duty status began |
| `HOS_VIOLATION_AT` | datetime | Projected violation time (same 2023 sentinel-date caveat as in `Dispatch`) |
| `ALT_DRIVER_CYCLE` | float | Always `7` where populated — near-constant, low modeling value |
| `ALT_DRIVER_CYCLE_ZONE` | str | 100% null — **dead column** |
| `CURRENT_DRIVER_CYCLE` / `CURRENT_DRIVER_CYCLE_ZONE` | object | Currently-active cycle and jurisdiction |
| `DOT_CLOCK_REMAIN_HOURS` | float | 96% null — sparse, use with caution |
| `LAST_SAT_ZONE`, `LAST_SAT_DATE`, `LAST_SAT_LOC` | mixed | Last satellite/telematics ping: zone code (mixes zone-code strings and raw ZIP integers — inconsistent typing), timestamp, and a human-readable location string (e.g. `"0.21M W of MILTON, ON"`) |
| `POSLAT` / `POSLONG` | str | Last known position in `DDMMSSN`/`DDDMMSSW`-style encoded strings (e.g. `0433201N`, `0795300W`) — **not decimal degrees**; needs parsing/conversion before plotting on any map |
| `MSG_NR` | float | Telematics message sequence number |
| `ACTIVE_IN_DISP` | float | Always `1` where populated — no variance |
| `STATUS` | str | Driver's current dispatch status: `AVAIL`, `ASSGN`, `DISP`, `DEPSHIP`, `ARRSHIP`, `DEPCONS`, `ARRCONS`, `PICKD`, `YARD`, `VACATION`, `UNAVL` — the standard load lifecycle vocabulary, same family as `Dispatch.LAST_FB_STATUS` |
| `TERMINAL_ZONE` | str | Assigned terminal |
| `LAST_TRIP`, `CURRENT_TRIP`, `NEXT_TRIP` | float | Trip-number pointers (`0` used as a "none" placeholder, not an actual trip 0) |
| `FINAL_DESTINATION` / `FINAL_DESTINATION_DESC` | object | Where the driver is ultimately headed (zone code / description; code column mixes zone strings and raw ZIP ints) |
| `DELIVER_BY` | datetime | Deadline for current assignment |
| `ASSIGNED_PUNIT` | str | 100% null — **dead column** (use `DEFAULT_PUNIT` instead) |
| `DEFAULT_PUNIT` | object | Truck number assigned to the driver — joins cleanly to `Trucks.TRUCK_NUMBER` |
| `OTHER_CODE` | str | Driver classification tag (`LOCAL`, `COMPANY`, `O/O F CAP`, `O/O 0 FUEL`, `PRED M`, …) |
| `ETA_ZONE` / `ETA_ZONE_DESC` / `ETA_DATE` | mixed | Next destination and expected arrival |
| `LAST_LOC_DATE` | datetime | Timestamp of last position fix |

## Sheet: `Trucks`

Single column, `TRUCK_NUMBER` (131 unique IDs, e.g. `B3339`). **No make/model/year/odometer/axle
data despite the "About" page's promise of "truck/trailer specs"** — for truck-side specs
(axle weight limits, fuel type, etc.) you'll need to either synthesize plausible values or ask
the hosts for a richer extract.

## Sheet: `Trailers`

| Column | Type | Description |
|---|---|---|
| `TRAILER_NUMBER` | str | ID, prefixed `DV` (Dry Van, 300 units) or `RF` (Reefer, 125 units) |
| `TRAILER_TYPE` | str | `Dry Van` or `Reefer` |
| `CAPACITY_LBS` | int | `44500` (Dry Van) or `43500` (Reefer) — only two values, essentially a lookup by type |
| `LENGTH_FT` | int | Constant `53` — no variance |
| `INSIDE_HEIGHT_FT` | float | Constant `8.5` — no variance |
| `WIDTH_IN` | int | Constant `98` — no variance |

Note: no Flatbed trailers are listed here even though `Tlorder.LOAD_TYPE` includes `Flatbed`
(5.4% of orders) — another sheet-to-sheet gap.

---

## Known data-quality issues (fix/decide on these before modeling)

1. **7.8% of `Tlorder` rows were never dispatched** (`TRIP_NUMBER` and the whole `_1`-suffixed
   join-artifact block are null together, 316 rows). These are quoted-but-not-booked orders —
   potentially your closest real-world analog to the brief's "missed load quotes" framing.
   Decide explicitly whether to include or exclude them per analysis.
2. **Two incompatible driver identifier schemes**: `Tlorder.PICK_UP_DRIVER`/`DELIVERY_DRIVER`
   use 4-letter initials codes (`PHIA`, `ASIN`, …), while `Dispatch.NAME`/`Driver.FIRST_NAME`
   use `DriverN`. There is **no direct key between them** in the provided sheets — you cannot
   join `Tlorder` to `Driver` directly without an external crosswalk. Flag this if your project
   needs "which driver worked which order."
3. **`DISTANCE` in `Tlorder` is negative for 59 rows** (down to -3,308.3 mi), and every one of
   them is also an undispatched order (#1 above). Likely a sign-convention artifact from
   quoting/estimation logic rather than true GPS distance — treat `abs(DISTANCE)` as the usable
   value, or exclude undispatched rows from distance-based features entirely.
4. **Zone codes are not stable geocodes.** 324 of 623 zone codes (52%) map to *multiple different*
   city descriptions (e.g. zone `LGELE` appears against 16 different cities including North York,
   Milton, Brampton, Toronto). Zone codes appear to be **customer/facility account codes**, reused
   across a customer's various ship-to addresses — not a 1:1 city key. **Do not use zone codes as
   a proxy for a fixed lat/long** — geocode the free-text `*_ZONE_DESC` fields instead (after
   normalizing formatting — see #6), or geocode `ORIGCITY/ORIGPROV`/`DESTCITY/DESTPROV` from
   `Tlorder`, which are cleaner.
5. **`Dispatch.LS_TRAILER1` has zero overlap with `Trailers.TRAILER_NUMBER`** (0 of 408 distinct
   values match). The trailer IDs referenced in dispatch (`215345`, `R53438`, `FB9872`, …) use an
   entirely different numbering scheme than the trailer roster (`DV001`, `RF030`, …). **The
   trailer-spec join the Project Brief implies (weight capacity for axle-weight compliance) does
   not actually work out of the box** — you'll need to either match by `TRAILER_TYPE` only
   (Dry Van/Reefer, ignoring exact unit) or treat trailer specs as fleet-average constants.
6. **Inconsistent text formatting** in city/province description fields: `"MILTON, ON"` vs
   `"MILTON,ON"` vs `"MILTON ,ON"` are all present for the same city. Normalize (strip whitespace,
   enforce `"CITY, PROV"`) before grouping, joining, or geocoding.
7. **Mixed types within single columns**, likely from Excel's type-per-cell export of a
   heterogeneous DB column: `DESTPC`/`ORIGPC` mix Canadian postal codes (str) and US ZIPs (int);
   `LS_TRAILER1`, `LAST_SAT_ZONE`, `FINAL_DESTINATION`, `ETA_ZONE` mix zone-code strings and raw
   ZIP integers. Cast defensively (`astype(str)`) before any string operation.
8. **Sentinel values disguised as real data**: the literal string `'<null>'` (not a true NaN),
   the date `1980-01-01` (Unix-epoch-adjacent default, 68% of `Dispatch.LS_EXPECTED_DATE`), and
   `0` used as a "no trip" placeholder in `Driver.LAST_TRIP/CURRENT_TRIP/NEXT_TRIP`. None of these
   will be caught by a plain `.isna()` check — clean them explicitly first.
9. **Fully dead (constant or 100%-null) columns** — drop or ignore for modeling:
   `Driver.CUMULATIVE_HOURS`, `DAYS_7`, `DAYS_8`, `DAYS_14`, `HOS_CUMULATIVE_HOURS`,
   `ALT_DRIVER_CYCLE_ZONE`, `ASSIGNED_PUNIT`, `ACTIVE_IN_DISP`; `Dispatch.LS_TRAILER2`,
   `EXTRA_STOPS`, `LS_DANGEROUS_GOODS`, `STATUS`, `LS_LEG_STAT`.
10. **Redundant/duplicate columns** from source joins: `Tlorder.TRIP_NUMBER_1` (≡`TRIP_NUMBER`),
    `Dispatch.LS_FREIGHT_1` (≡`LS_FREIGHT`) — safe to drop one of each pair.
11. **`POSLAT`/`POSLONG` are encoded strings, not decimal degrees** (`0433201N` = 43°32'01"N).
    Must be parsed before any mapping library can use them.
12. **38 of 169 `Driver` rows are entirely blank** (placeholder driver IDs) — filter before
    computing fleet-wide statistics or they'll silently deflate averages/denominators.
