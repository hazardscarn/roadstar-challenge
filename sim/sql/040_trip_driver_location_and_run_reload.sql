-- Real user feedback: "is it possible for us to load simulations by sim ID from the supabase
-- schemas as an option instead of always triggering a new one" -- reconstructing a past run's
-- full playback (trajectories included) from simulation.* needs to know where each trip's driver
-- actually started from (the deadhead leg's origin) -- captured transiently during the original
-- run (c.driver_location_id) but never persisted. Added so runs generated FROM NOW ON can be
-- fully reloaded; runs persisted before this migration simply show no deadhead leg on reload
-- (graceful degradation, not a crash -- the loaded leg and every $ figure still reconstruct fully).
alter table simulation.trips add column if not exists driver_location_id integer references reference.locations;

-- Real user feedback: reloading a past run was slow (re-fetching route geometry -- including
-- live OSRM calls for anything not in the cache -- for every trip). Persisting the trajectory
-- computed once at RUN time makes reload a pure Supabase read, no geometry recompute at all.
alter table simulation.trips add column if not exists trajectory jsonb;

-- The "deadhead avoided" positive-framed KPI (real user feedback: show the wins, not a net
-- figure that nets against an unavoidable structural cost) -- persisted per run so the runs list
-- (/api/simulation/runs) can show it without recomputing.
alter table simulation.runs add column if not exists n_deadhead_avoided integer;
alter table simulation.runs add column if not exists deadhead_avoided_value numeric;
