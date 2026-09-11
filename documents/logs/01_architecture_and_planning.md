# Architecture & Implementation Planning

**What:** Two documents were written before any code: an architecture spec and an implementation
sequence.

- `research/roadstar_platform_plan.md` — the **architecture**: where simulated/live order
  coordinates come from (real lat/lngs, not the historical export's zone codes), a concrete
  buffered geofence function, the full database schema (reference/ground_truth/calibration/sim/
  live), the discrete-event Monte Carlo simulation design, the ADP value-function model with
  epsilon-greedy exploration, and the dashboard/RBAC design.
- The Claude Code plan-mode file (`~/.claude/plans/jolly-napping-bubble.md`) — the **build
  sequence** on top of that: what gets built in what order, in what files, for a solo build.

**Why:** Two earlier drafts (`research/temp_plans/*.md`) were written by a session without this
project's accumulated data-quality findings from the EDA work (`analysis/*.ipynb`). They had real
gaps: no plan for where simulated orders' coordinates actually come from, geofencing named as a
requirement but never turned into executable code, and no dashboard/UI section at all despite it
being a required, judged deliverable. `roadstar_platform_plan.md` explicitly supersedes both and
fixes all three gaps.

**Key decisions made during planning, revised since:**
- Model inference hosting: originally planned as a Supabase Edge Function (Deno), **revised to a
  Vercel Python serverless function** once the team confirmed Vercel hosting for the frontend —
  avoids exporting the trained XGBoost model to ONNX/WASM for no benefit, and avoids running a
  third always-on service that could cold-start-sleep mid-demo.
- Simulation/training data storage: originally planned as Supabase tables mirroring the live
  schema, **revised to a local Postgres+PostGIS container** for `sim.*` and
  `training_transitions` once the actual data volume (1,000+ simulation runs) was considered —
  see `02_infrastructure_setup.md`.
