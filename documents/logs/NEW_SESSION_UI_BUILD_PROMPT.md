Paste this as the opening message of the new session.

---

Build the full one-stop web app for the RoadStar Southern Ontario dispatch platform. Read
`documents/logs/22_session_summary_ml_pipeline_complete.md` first for what's already built and
validated (the whole sim/ML pipeline, two trained models, a live-scoring theory-proof) β€” don't
re-derive or re-question any of that; it's done. Also read
`documents/1788654151601_Hackathon_Project_Brief.pdf` (the real judging brief) and
`documents/schema_reference.md` (the real Supabase schema, already provisioned β€” `.env` has real
credentials).

## What to build

A React + Vite app (Tailwind already implied by `.env`'s `VITE_` prefixes) with two roles sharing
one login: **Manager/Dispatcher** and **Driver**. This needs to look genuinely good β€” polished,
modern, not a generic admin-template look. Look online for real 2026 fleet-management/TMS
dashboard UI inspiration before designing screens (role-based dashboards, live map as the
dispatcher home screen, KPI bands, mobile-first driver views are current real patterns β€” verify
and go further, don't just copy). Use real component libraries (shadcn/ui, Tailwind, a real charts
library) β€” make deliberate visual choices, not defaults.

### Role-based access β€” real, not just two different screens

Manager sees everything fleet-wide; a driver sees ONLY their own truck, their own current trip,
and their own history β€” never another driver's data, and this must be enforced at the database
level (RLS), not just by hiding UI elements client-side. The exact pattern is already spec'd in
`research/roadstar_platform_plan.md` Section 8.3 β€” follow it, don't redesign it:

- **Supabase Auth with a `role` claim** (`manager`/`driver`) on the user β€” checked both
  client-side (which screens/routes render) AND server-side (RLS policies). Never rely on
  client-side role gating alone for anything touching `live.*` writes.
- **A mapping column is needed**: add `auth_user_id` to `ground_truth.drivers` (or a small
  `profiles` table if that's cleaner) linking a Supabase Auth user to their real `driver_id` β€”
  this doesn't exist yet, it's new schema this session needs to add.
- **Driver-scoped RLS policy pattern** (from the plan, adapt as needed): scope
  `live.trips`/`live.vehicle_inspections`/`live.driver_status`/`live.trip_log`/
  `live.driver_ratings` rows to
  `WHERE driver_id = (select driver_id from ground_truth.drivers where auth_user_id = auth.uid())`
  for the driver role; manager role reads unrestricted (or restricted only by being authenticated
  or a real `manager` claim, not by driver identity).
- **Manager-side inspection gate** (the brief's actual compliance requirement): the
  candidate-filter query behind quote scoring should exclude a driver without a passing
  inspection in the last 24h β€” `AND EXISTS (SELECT 1 FROM live.vehicle_inspections WHERE
  driver_id = d.driver_id AND submitted_at > now() - interval '24 hours' AND overall_pass)`. This
  belongs in the scoring pipeline (`sim/live/score_quote.py`'s live fleet snapshot query), not
  just a UI warning banner.

Screen inventory below is per-role; a driver never gets the manager's fleet-wide views (live map
of everyone, all orders, billing) and a manager should be able to see everything a driver sees
for any specific driver (drill-down), not the reverse.

### Manager / Dispatcher views
- **Live map** (OSRM-backed, real routes not straight lines) as the centerpiece β€” every truck as a
  marker, color-coded by status, click for details (driver, current trip, ETA, HOS remaining,
  load). Satellite toggle (the brief explicitly requires this). Historical breadcrumb trails.
- **Quote panel**: enter origin/dest/weight/pallets/load type + **real pickup date** (not just
  "now" β€” this matters, see the live-scoring log for why) β†’ calls the scoring pipeline
  (`sim/live/score_quote.py`'s logic, needs a real HTTP endpoint wrapping it β€” Vercel Python
  function per the original architecture plan) β†’ shows top-N ranked options as cards: driver,
  expected reward/revenue, deadhead miles, ETA, HOS feasibility. One-click assign.
- **Live dispatch monitoring**: every active trip's GPS/status, updated on a real interval (the
  user said every 5 min), with ETA info (on-time/early/delayed badges) β€” this needs either a
  polling refresh or Supabase Realtime subscriptions on `live.driver_status`/`live.trips`.
- **Orders view**: completed / ongoing / upcoming, filterable/sortable table.
- **Trip history log**: labeled, browsable (backed by `live.trip_log`).
- **Order table**: proper columns, not a dump β€” backed by `live.quote_requests`/whatever the real
  order-tracking table ends up being (may need a new table β€” check what's missing).
- **Geofencing display**: per-order/sub-order, show geofence trigger events and dwell time at each
  stop (backed by `live.geofence_events`/`live.geofence_dwell_state` β€” schema exists, needs a real
  geofence-trigger mechanism wired to the live position feed, not just the UI).
- **Fleet health / maintenance warnings** panel (`live.truck_maintenance_state`, already has
  `maintenance_until`).
- **Billing**: a button that generates a PDF invoice and emails it. Schema now exists
  (`sim/sql/030_add_invoicing_and_costs.sql`, already applied) β€” `live.invoices` (CRA-compliant
  itemization: linehaul/detention/fuel-surcharge/accessorial kept SEPARATE, not pre-summed;
  subtotal/tax_amount/total_amount are generated columns; `delivery_province` drives the tax rate
  since HST/GST follows the province of DELIVERY, not the carrier's home province β€” sourced, see
  the migration's own header comment) and `live.trip_costs` (internal margin view: real
  operating/maintenance/breakdown/lateness costs vs. revenue, NOT shown to the shipper). Build the
  PDF generation + email send against these tables; the schema/tax-rate research is already done,
  don't re-research it.
- **Driver pay / payroll is explicitly OUT OF SCOPE** β€” confirmed with the user directly. No real
  driver pay-rate data exists anywhere in the source (`ground_truth.drivers.pay_type` is a real
  code, V/P/D, with no accompanying $ rate anywhere in the export), and the hackathon brief's own
  financial ask is shipper-side detention billing, not payroll. Do not build a driver-pay table or
  feature; if the user asks for it later, treat it as new scope requiring its own real-data check
  first, same as every other $ figure in this project.

### Driver views
Scope boundary: a driver's whole app is about THEM β€” their assigned truck, their current trip,
their own history. No fleet-wide visibility, no other drivers' data (enforced by the RLS policy
above, not just by not linking to it in the nav).

- **Pre-trip inspection form**, required before a driver can go available β€” backed by the real
  `live.vehicle_inspections` schema (brakes/tires/lights/fluids/coupling/trailer + defects notes).
  Look up real DVIR requirements (FMCSA 49 CFR Β§396.11) for what fields a real inspection needs β€”
  the schema already matches the real minimum categories, verify nothing's missing.
  `overall_pass=false` should visibly block the driver from being marked available.
  **Do NOT invent a compliance requirement not in the schema/brief without flagging it** β€” DVIR
  research is for filling out the checklist fields correctly, not for adding scope.
- **Personal stats**: performance/driving stats, backed by `live.driver_ratings` (on-time rate,
  avg deadhead, avg load fill, HOS-stranding-risk history).
- **Truck info**: their current assigned truck's condition/maintenance status.
- **Current trip / live status**: where they are, what's next, ELD-style duty status.

## Real constraints, not optional

- **HOS 13h/14h/16h + 70h/7day + 120h/14day** β€” real Canadian regulation, already correctly
  modeled in `sim/engine/hos.py`; the live/driver views must surface it accurately, not
  approximate it further.
- **Geofence-triggered detention billing**: brief's "critical requirement" β€” arrival/departure
  timestamps auto-recorded, free 2h then billed. Schema exists (`live.detention_billing`'s
  `billable_hours` is already a generated column); the geofence-trigger mechanism itself
  (`ST_DWithin` check against `reference.locations`, per the original architecture plan) needs
  building β€” check `research/roadstar_platform_plan.md` Section 1 for the buffered
  arrival/departure state-machine design already spec'd (`live.geofence_dwell_state` exists for
  exactly this).
- **`SUPABASE_SERVICE_ROLE_KEY` is not yet in `.env`** β€” needed server-side for the scoring
  function (never expose client-side). Ask the user for it or have them add it before that piece
  can work.

## Sequencing suggestion (adjust as needed, don't treat as rigid)

1. Scaffold the app (Vite + React + Tailwind + shadcn/ui + Supabase client), auth, role routing.
2. Wire the Vercel `api/score-quote.py` endpoint around the already-proven `sim/live/score_quote.py`
   logic β€” this de-risks the hardest, highest-value piece first.
3. Manager: live map + quote panel + assign flow (the demo centerpiece).
4. Driver: inspection form + personal stats + current trip view.
5. Orders/trips tables, trip history, geofencing display.
6. Design + build the invoicing schema/flow last (genuinely new scope, not just wiring existing
   tables).

## How to work

Same standard as the rest of this project: ground design choices in real sources (the brief, the
existing schema, real DVIR/invoice/TMS-UI research), verify claims before making them, flag gaps
honestly instead of quietly filling them with guesses, and don't add scope the user didn't ask for
without surfacing it as a question first. This platform is meant to win β€” make the UI genuinely
impressive, not merely functional, but don't sacrifice correctness (HOS logic, real routing, real
$ math) for polish.
