-- Real user feedback: the Simulation Showcase only ever showed a handful of averages next to the
-- map; ask was for the direct sim-demo equivalents of what backtesting already showed (cycle-based
-- metrics, daily HOS utilization, trips-per-driver spread) -- as DISTRIBUTIONS, not just means, in
-- a collapsible panel that doesn't shrink the map. Computed once at run time (sim/engine/
-- fleet_metrics.py, reusing real_data_replay.py's own cycle-closing definitions) and stored here so
-- a reloaded past run shows the identical numbers without recomputing anything from flattened rows.
alter table simulation.runs add column if not exists fleet_metrics jsonb;
