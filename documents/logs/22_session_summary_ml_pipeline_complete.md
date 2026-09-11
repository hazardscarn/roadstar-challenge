# Session Summary: ML/Simulation Pipeline Complete, Handing Off to UI Build

**What:** End-of-session checkpoint before starting a fresh session for the UI/dashboard build.
Everything below (files 16-21) is validated, working, and committed to the log index -- this file
is the one-page orientation for picking the project back up, not a re-derivation of any of it.

## What's built and validated

- **Simulation engine** (`sim/engine/`): discrete-event sim with real temporal realism (real order
  lead times, mid-route drivers as real candidates via projected next-state, just-in-time
  departure, decaying epsilon, training over a past calendar year), real reward economics
  (distance-tiered linehaul rate, unified breakdown cost, deterministic expected-lateness pricing,
  realistic 1-3/5,000 breakdown rate with real proactive maintenance), H3 hexagonal region
  features, and two real performance fixes (HOSLog O(n) scan, XGBoost DMatrix-per-call overhead).
- **Two trained models**: a Q-model (`sim/training/train_value_function.py`, RΒ²β‰ˆ0.13) and a
  state-value V(s) model (`sim/training/train_state_value_function.py`, RΒ²β‰ˆ0.27, FVI over 4
  rounds, truck-condition feature included with a shrinkage/clip fix after an earlier regression).
  Artifacts: `sim/training/value_function.pkl`, `sim/training/state_value_function.pkl`.
- **Validation, twice, both honest**: a paired significance test (t=-1.51, not significant vs.
  greedy -- an honest, reportable result, not hidden) AND a real-data backtest
  (`sim/backtest/real_data_replay.py`) showing a small real $ edge (+704 CAD/1,667 orders) plus a
  concrete missed-opportunity recovery result (130/130 real historical undispatched orders found a
  feasible candidate, ~$10.6K recovered on genuine freight). See `documents/results/
  real_data_backtest/` for deck-ready diagrams (methodology + results, `.drawio`/`.png`/`.jpg`).
- **Live scoring theory-proof** (`sim/live/score_quote.py`): proves the SAME trained-model scoring
  pipeline works against `live.*`-shaped data, not just sim data -- reuses `sim/engine/policy.py`
  directly, no parallel reimplementation. Returns a full reward-distribution breakdown per
  candidate (not just a winner), confirms mid-route/return-deadhead drivers genuinely compete once
  a real pickup date is given (verified: 4/10 top-ranked candidates were mid-route on a real test
  case). `sim/live/seed_test_fleet.py` seeds a local test fleet for this -- NOT a real live-data
  feed.
- **Real database, already provisioned**: Supabase project live now, `.env` already has
  `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` (Vite-prefixed -- frontend choice already
  anticipated) and `SUPABASE_DB_URL`. Full schema breakdown in `documents/schema_reference.md`.

## What's real schema, but NOT yet wired to any app code

The `live.*` schema (built earlier this session) is comprehensive and already has tables for
everything the UI needs, but nothing populates or reads them yet except the theory-proof script:

- `live.driver_status`, `live.trips` (now with projected next-state columns, sim/sql/029),
  `live.geofence_events`, `live.geofence_dwell_state`, `live.detention_billing`
- `live.quote_requests` / `live.quote_recommendations`
- `live.vehicle_inspections` (DVIR-shaped: brakes/tires/lights/fluids/coupling/trailer booleans +
  `overall_pass` generated column)
- `live.driver_ratings`, `live.truck_ratings`, `live.trip_log` (historical performance rollups)
- `live.truck_maintenance_state` (now with `maintenance_until`, sim/sql/029)

**Invoicing/costs -- resolved and built this same session, after this file was first drafted**:
`sim/sql/030_add_invoicing_and_costs.sql` adds `live.invoices` (CRA-compliant itemization --
linehaul/detention/fuel-surcharge/accessorial kept separate, generated subtotal/tax/total columns,
`delivery_province`-driven tax rate since HST/GST follows the delivery province not the carrier's
home province -- sourced from real 2026 Canadian invoicing requirements) and `live.trip_costs`
(internal margin view -- real operating/maintenance/breakdown/lateness cost vs. revenue). Both
applied to the remote schema already.

**Driver pay / payroll -- explicitly confirmed OUT OF SCOPE** by the user directly: no real
driver pay-rate data exists anywhere (`ground_truth.drivers.pay_type` is a real code, V/P/D, with
no accompanying $ value in the source export), and the brief's own financial ask is shipper-side
detention billing, not payroll. Do not build this without the user raising it again as new,
explicitly-scoped work.

## What's NOT built at all

- Any frontend code (no `package.json`, no dashboard directory contents beyond an empty
  `dashboard/api/`)
- The Vercel `api/score-quote.py` HTTP endpoint (the pipeline it would call is done and proven)
- Auth/RBAC (manager vs. driver roles)
- A real live-data feed populating `live.*` from actual GPS/dispatch events (only synthetic test
  seeding exists)
- Invoicing (schema + generation + email)
- `SUPABASE_SERVICE_ROLE_KEY` is not yet in `.env` -- needed server-side for the score-quote
  function (flagged earlier this session, still not added)

## Immediate next step

A fresh session will build the full one-stop web app (manager + driver views, live map, quote
flow, DVIR-style inspection form, order/trip tables, geofence-driven detention billing, invoicing)
-- see the handoff prompt given to the user for that session's exact brief, informed by this file,
the real project brief (`documents/1788654151601_Hackathon_Project_Brief.pdf`), and real 2026
fleet-management UI research.
