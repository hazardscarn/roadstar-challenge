# Domain Deep-Dive & EDA Plan

## 1. First — is the PDF's list "exclusive"? What exactly does it list?

Worth being precise here because the PDF doesn't actually contain one single "5 things" list —
it contains **several overlapping enumerations**, and none of them has exactly 5 items:

| Section | What it enumerates | Count |
|---|---|---|
| §3 "Target Architecture" | System Components: (1) Web Dispatcher Dashboard, (2) Separated Simulation Engine, (3) Driver Interface | 3 |
| §5 "Key Problem Areas & Bug/Edge Case Discovery" | Automated Detention Billing · Deadhead & Empty Mile Reduction · Automated HOS & Axle-Weight Compliance · Edge Case/Hidden Problem Discovery (bonus) | 4 |
| §6 "Technical & Functional Deliverables" (table) | Dispatcher Dashboard · Simulation Engine · Driver Interface · Pipeline & Integration | 4 |
| §7 "Judging Criteria" | 7 qualitative areas (listed in my previous message) | 7 |

If you had "5 things" in mind, it's most likely §5's problem-area list (4 named + the bonus one =
easy to misremember as 5), or a mental merge of a couple of these. Practically it doesn't matter
which exact grouping you were recalling — **the substantive question is the same either way: is
this the only scope, or can we go beyond it?** My read, with reasoning:

- The **PDF Project Brief is the authoritative, judged scenario** for this hackathon — it's the
  only document with a concrete financial framing, a specific operating region, a named
  architecture, a deliverables table, *and its own 7-point judging rubric*. That's not marketing
  copy, that's a spec.
- The **"About" page and sponsor deck's broader 7-category list** (fleet management, freight
  optimization, driver scheduling, load matching, mobile apps, AI predictive analytics,
  integration tools) isn't a competing, wider scope — it's describing **the same brief in looser
  marketing language**. Each category maps onto a piece of the one detailed scenario:
  - "Fleet management tools" → the dispatcher dashboard
  - "Freight optimization platforms" / "Load matching and dispatch solutions" → the
    deadhead/empty-mile matching engine
  - "Driver scheduling and routing systems" → the HOS compliance engine + routing
  - "Mobile apps for drivers and dispatchers" → the driver interface component
  - "AI-powered predictive analytics for logistics" → the edge-case/bonus discovery work
  - "Integration tools between trucking platforms" → the pipeline & integration component
- So: **not exclusive in the sense of "you may only build these 4 named things and nothing
  else,"** but also not a hint that you're free to ignore it and build something unrelated (e.g.
  a general fleet-maintenance scheduler) — everything the marketing material describes already
  lives inside this one brief. Treat the brief as the spine of your project.
- What genuinely **is** open is *emphasis and depth*, not scope. The judging rubric rewards depth
  in several independent dimensions (financial value, geofencing precision, problem discovery,
  mapping depth, HOS logic, simulation realism, UI/UX) rather than a binary "did you build all 4
  components" checklist. A team that builds 2 of the 4 components very well, with strong
  evidenced numbers and hidden-bug discovery, is very likely to score higher than a team that
  builds all 4 shallowly. **This is the practical answer to your question**: don't read it as a
  rigid checklist — read it as "these are the areas we will look at you through," and choose
  where to go deep.
- If you want certainty rather than my inference, this is a genuinely good question to drop in
  the hackathon's Discord `#tech-support`/general channel — "can we go deeper on some components
  than others, or must all 4 be present?" is a normal, expected clarifying question and costs you
  nothing to ask.

## 2. Domain deep-dive: how each area actually works in the real industry, and what in the data maps to it

### 2.1 The end-to-end order lifecycle (what all five sheets are snapshots of)

This is the backbone flow every TMS (including the McLeod-style system this data came from)
implements, and everything in the brief hangs off a stage of it:

```
1. ORDER ENTRY      Customer calls/emails/EDIs a load request → Tlorder row created
                     (CREATED_TIME, ORIGCITY→DESTCITY, WEIGHT_LBS, LOAD_TYPE)
2. QUOTING          Dispatcher/planner prices the lane. NOT in this data — no rate/$ column
                     exists anywhere in Tlorder or Dispatch (see §4 below — important gap).
3. BOOKING/DISPATCH Order is assigned a TRIP_NUMBER, broken into one or more legs
                     (Dispatch rows), each given a driver + truck + trailer.
                     A trip may include an empty repositioning leg (LS_MT_LOADED='E')
                     before the loaded leg (LS_MT_LOADED='L') if the truck isn't already
                     at the pickup point — this is the "deadhead" the brief wants reduced.
4. PRE-TRIP CHECK   In a mature TMS: cross-check driver's remaining HOS (REMAINING_HOURS)
                     and equipment weight limits before confirming the assignment.
                     This data shows the *result* (assignments that were made) but not
                     whether this check was actually enforced — a good thing to test for.
5. PICKUP           Truck geofences into the shipper's yard → arrival timestamp logged
                     (LS_DET_PICK_ARRIVE). Loading happens (free for first 2 hrs contractually).
                     Truck geofences out → departure timestamp. Gap beyond 2h = detention.
6. IN-TRANSIT       Truck drives the lane. In production this streams live GPS; here, only
                     a snapshot (Driver.POSLAT/POSLONG) survives — no track history is provided,
                     which is exactly why the brief asks for a *simulated* telemetry feed.
7. DELIVERY         Same geofence-in/geofence-out pattern at the consignee
                     (LS_DET_DELV_ARRIVE), same detention logic applies.
8. STATUS LIFECYCLE Threaded through the whole trip: AVAIL → ASSGN → DISP → DEPSHIP →
                     ARRSHIP → PICKD → (drive) → ARRCONS → DEPCONS → COMPLETE. You see this
                     exact vocabulary in Driver.STATUS and Dispatch.LAST_FB_STATUS.
9. BILLING          Freight bill finalized, any detention/accessorial charges added,
                     invoice sent. NOT in this data either — no billing/invoice sheet exists.
```

Every "problem area" in the brief is a claim about breakage somewhere in steps 3–9. Understanding
this flow is what lets you tell a coherent story on demo day instead of presenting disconnected
features.

### 2.2 Detention billing — the real mechanics

In FTL trucking, a shipper/consignee gets a contractual **free time** window (commonly 2 hours,
sometimes 1–4 depending on the contract) to load or unload the truck. Every minute past that is
**detention time**, billed back to the shipper/consignee at a **detention rate** — industry-typical
rates run roughly **$50–$100/hour CAD**, sometimes with the first 30–60 minutes of overage forgiven
as a grace buffer. The mechanism that makes this billable *and defensible* is precise, timestamped
proof: a **geofence** (a virtual polygon or radius around the facility's GPS coordinates) triggers
an automatic "arrived" event when the truck's GPS crosses in, and a "departed" event when it
crosses back out. Without that automation, dispatchers rely on manual "check calls" — phone calls
to ask the driver "are you still there?" — which is exactly the fragmented, error-prone workflow
the brief opens with.

**What's in the data**: `Dispatch.LS_DET_PICK_ARRIVE` and `LS_DET_DELV_ARRIVE` are already the
arrival timestamps this whole mechanism depends on — meaning you can compute **actual historical
dwell time** (`some departure/status-change timestamp − arrival timestamp`) directly from real
data, not just simulate a made-up example. The nearest thing to a "departure" timestamp is the
next status transition after arrival in `LAST_FB_STATUS`/`LS_LAST_FB_STATUS_DATE`, or a
`LS_LEG_SEQ`+1 leg's departure — you'll need to reconstruct "departed" from the status sequence
since there isn't a single explicit `_DEPART` column. That reconstruction *is* the interesting
engineering problem, and it's a legitimate thing to explain in your demo.

### 2.3 Deadhead / empty-mile reduction — the real mechanics

"Deadhead" = miles driven with an empty trailer, earning no freight revenue, most commonly when a
truck delivers a load somewhere and there's no return (backhaul) freight lined up, so it either
drives home empty or repositions empty to the next pickup. Industry-wide, empty miles typically
run **15–25% of total fleet miles**; cutting even a few points of that is a large, real cost lever
because fuel/driver time is spent for zero revenue. The classic mitigation is a **backhaul
matching engine**: given a truck that just dropped a load near city X, search for any open order
whose *pickup* is near X and whose *destination* is roughly back toward the truck's home terminal
or next known commitment — this is a bipartite matching / assignment-optimization problem (can be
solved with a simple greedy nearest-neighbor heuristic, a proper assignment/ILP solver, or a
learned/RL policy if you want to go further).

**What's in the data**: `Dispatch.LS_MT_LOADED` (`E`=empty, `L`=loaded) is a ready-made label —
you can directly measure historical deadhead % by driver, by lane, by zone, and by time period
without needing to infer it. `LS_FROM_ZONE`/`LS_TO_ZONE` plus `ORIG_ZONE_DESC`/`DEST_ZONE_DESC`
give you the geography to build a lane graph and hunt for missed backhaul opportunities.

### 2.4 HOS & axle-weight compliance — the real mechanics

**Hours of Service (HOS)**: a regulatory safety framework (Transport Canada south of 60°N;
FMCSA in the US, since this fleet clearly runs both) that caps how long a driver can drive/work
before a mandatory rest. The core Canadian numbers are in the brief (13h driving / 14h on-duty /
16h elapsed window / 10h off-duty / 70h-per-7-days or 120h-per-14-days cycle). Drivers log this via
an **ELD (Electronic Logging Device)**, which auto-detects driving via the vehicle's motion and
timestamps every duty-status change (Off Duty → Sleeper Berth → Driving → On-Duty Not Driving).
Dispatchers are supposed to check a driver's remaining hours *before* assigning a load that would
push them past a limit mid-route — failing to do so is a common, expensive real-world failure mode
(a violation risks fines and, more urgently for the carrier, a stranded truck/load if the driver
legally cannot keep driving).

**Axle-weight compliance** is a *different* problem from total/gross weight, and often the more
subtle one: a truck can be well under its total legal gross weight (roughly 80,000 lb in the US,
~137,500 lb combination in Canada with proper axle spread) while still being **illegally
distributed** across its axles if cargo isn't positioned correctly (governed by "bridge formula"
rules tied to axle spacing). This is why the brief calls it "HOS **&** Axle-Weight" — two distinct
compliance checks bundled together.

**What's in the data**: `Driver.REMAINING_HOURS`, the `REMAINING_HOURS_CAN_7/8/14` and
`REMAINING_HOURS_US_7/8` fields, `DRIVER_CYCLE_ZONE` (which jurisdiction currently governs), and
`Dispatch.REMAINING_HOURS`/`HOS_VIOLATION_AT` give you real material to build and backtest an HOS
risk engine. **Axle weight is only partially supportable**: you have `Tlorder.WEIGHT_LBS` (total
shipment weight) but **no axle configuration, no truck spec sheet, and no per-axle scale data
anywhere in the five sheets** — `Trucks` is a bare list of IDs with zero attributes. So a true
axle-weight compliance check isn't buildable as specified; the honest, judge-impressing move is to
say so explicitly and either (a) approximate using total-weight-vs-legal-gross-limit as a
simplification, or (b) flag it as a data gap you'd need from the host to do properly — that's a
legitimate "problem discovery" finding in its own right.

### 2.5 Mapping, Track & Trace, and the Simulation Engine — the real mechanics

Real fleet-tracking systems (Samsara, Motive/KeepTruckin, Geotab, etc.) push a GPS ping every
30–120 seconds from an in-cab device, giving continuous "breadcrumbs" you can draw as a polyline.
This dataset doesn't have that — `Driver.POSLAT/POSLONG` is **one point per driver**, and note
it's encoded as a **DMS-style string** (`0433201N` = 43°32′01″N), not decimal degrees, so it needs
parsing before any mapping library (Leaflet/Mapbox/Google Maps all expect decimal degrees) can
plot it. This absence is exactly why the brief spins out a whole separate "Simulation Engine"
component: since you don't have a real breadcrumb trail, you're expected to **generate** a
plausible one — e.g., interpolate a path between two known cities' coordinates (via a routing API
or straight-line/road-snap approximation), walk a point along it at a speed sampled from a
realistic distribution, and inject randomness for dwell times and "traffic slowdown" events. The
grounding move that separates a believable simulation from an arbitrary one is **fitting your
random distributions to the real data** — e.g., pull actual leg distances (`LS_LEG_DIST`) and
real historical dwell durations (from §2.2) so your simulated truck "behaves" like the real fleet
does, statistically, even though no single simulated trip is a real trip.

### 2.6 Driver Interface / digital logbook — the real mechanics

The mobile/ELD app a driver actually uses does three things: (1) shows the current/next load with
pickup/delivery details, (2) lets the driver log duty-status changes (which is legally required —
paper logbooks are largely obsolete in Canada/US for these fleets under the ELD mandate), and (3)
shows a route/map. This is the most "off-the-shelf UI, least differentiated" component of the four
— a reasonable place to spend the least of your week.

## 3. A material gap worth internalizing before you build anything financial

**There is no price, rate, revenue, or invoice field anywhere in the five sheets.** I checked
every column name across `Tlorder` (33 cols) and `Dispatch` (57 cols) specifically for this —
none exists. The brief's headline financial framing ($1,000–$7,000 per missed quote, $15k–$50k/month
recoverable) **cannot be validated against this dataset directly** — you have the operational
facts (was it dispatched, how long was the dwell, how many empty miles) but not the dollars. You
have two honest options: (a) apply a reasonable, clearly-labeled industry assumption (e.g., a flat
detention rate of ~$65/hr CAD, or an approximate FTL linehaul rate per mile) so your "$ recovered"
number is a documented estimate rather than an invented one, or (b) ask the hosts directly whether
a rate table exists that wasn't included in this extract — plausible, since a McLeod-style TMS
absolutely has one (a `RATE`/`INVOICE` table) that just wasn't exported. Either way, **say this
out loud in your demo** — "we identified that rate data wasn't in the provided extract, so our
dollar estimates use a documented industry-standard assumption of $X/hour" is a "problem
discovery" point in itself, and prevents a judge from catching an unexplained, seemingly-invented
number.

## 4. Concrete EDA plan — hypotheses to test against the data, in priority order

Each of these produces a number or a chart you can put directly in a demo slide.

1. **Detention exposure, today.** For each leg with both `LS_DET_PICK_ARRIVE`/`LS_DET_DELV_ARRIVE`
   populated, reconstruct dwell time (arrival → next status timestamp/departure), flag dwell > 2h,
   sum the excess hours, multiply by an assumed detention rate. Segment by `CALLNAME` (customer)
   and `LOAD_TYPE` to find your worst-offending customers/lanes. This is your single best "Workflow
   Speed & Financial Value" slide.
2. **Deadhead ratio.** `% of legs (and % of miles) where LS_MT_LOADED == 'E'`, sliced by driver,
   by zone, and by month. Compare against the ~15–25% industry benchmark mentioned above — are
   they above or below it? Then build a simple lane-pair analysis: for each `(from_zone, to_zone)`
   pair with high empty-leg volume, check whether a `(to_zone, from_zone)` loaded order existed
   within a reasonable time window — a first-pass, offline measurement of "how much backhaul
   matching would have been possible."
3. **Undispatched-quote leakage.** Compare the 316 never-dispatched `Tlorder` rows against the
   ~3,700 dispatched ones: does lead time (creation → required pickup date), customer, lane, or
   load type predict whether an order gets dispatched? This is a natural binary classification
   problem and a direct analog to the brief's "missed quote" framing — you can even try to give
   the 316 rows a $ estimate using the same assumed-rate approach as #1, cast as "potential
   dispatch-desk revenue leakage we identified in the historical record."
4. **HOS risk at time of dispatch.** Distribution of `Dispatch.REMAINING_HOURS` at leg assignment
   — how often is a leg assigned to a driver with, say, under 3 hours remaining relative to the
   leg's expected drive time? Cross-reference with `HOS_VIOLATION_AT` to see how often the
   projected violation time actually falls inside the leg's own planned window. Also check whether
   drivers whose `DRIVER_CYCLE_ZONE` is `U` vs `C` show different violation-risk patterns near
   the US/Canada border lanes — this is where the "dual jurisdiction" edge case would surface
   empirically.
5. **On-time performance.** `ACTUAL_PICKUP` vs `PICKUP_BY`, `ACTUAL_DELIVERY` vs `DELIVER_BY` (or
   the `Dispatch` equivalents) → % late by customer and by lane. Directly supports "workflow speed"
   and gives you a natural "before" baseline your matching/compliance engine's simulated "after"
   can be framed against.
6. **Zone-code hygiene, quantified.** You already know 324/623 zone codes (52%) map to multiple
   cities — turn this into a concrete "N% of trips would have been mis-plotted on a naive
   zone-code-based map" statistic; this is a crisp, evidence-backed "hidden bug" finding for the
   bonus criterion.
7. **Equipment gap check.** `Tlorder.LOAD_TYPE` includes `Flatbed` (218 orders, 5.4%) but the
   `Trailers` roster has zero flatbed units (only Dry Van/Reefer) — quantify how many historical
   flatbed orders would have had no matching equipment in the roster as provided, and decide how
   your project should represent flatbed capacity (assume external/leased trailers, or exclude
   flatbed from scope and say why).
8. **Multi-stop complexity vs. reliability.** Does `LS_NUM_LEGS` (or `LS_NUM_TOTAL` stops)
   correlate with longer dwell times or later deliveries? If so, that's a nice, non-obvious
   "problem discovery" finding: complexity itself is a driver of the fragmentation cost the brief
   describes, not just "5 disjointed software systems."
9. **Driver utilization snapshot.** At a fixed point in time (e.g. using `Driver.STATUS`), how
   many drivers are `AVAIL` vs `ASSGN`/`DISP`, and how many `Tlorder` rows are undispatched at
   that same moment? A mismatch (available capacity sitting idle while orders wait) is your
   clearest, most visual "manual matching is inefficient" evidence — and a good one to visualize
   live in your dashboard demo.

Running #1–#3 first gives you defensible numbers for the two heaviest-weighted judging criteria
(financial value, problem discovery) before you've written a line of dashboard code — worth doing
in that order.
