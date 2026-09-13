-- Real user feedback: a 2h pickup WINDOW was unnecessary complexity -- a specific pickup time is
-- enough. Also adds a real OSRM-derived delivery ETA (sim/engine/run_sim.py's get_route(), the
-- same distance/duration function the simulator and live scoring already use), computed once at
-- generation time (sim/live/generate_dispatch_day.py) rather than left for the frontend to guess.
--
-- dispatch.day_orders only ever holds disposable, regeneratable demo data (idempotent generator,
-- sim/live/generate_dispatch_day.py) -- any existing day is cleared before this runs, so no
-- backfill is needed for the new NOT NULL columns.
alter table dispatch.day_orders drop column pickup_window_start;
alter table dispatch.day_orders drop column pickup_window_end;
alter table dispatch.day_orders add column pickup_at timestamptz not null;
alter table dispatch.day_orders add column delivery_eta timestamptz not null;
