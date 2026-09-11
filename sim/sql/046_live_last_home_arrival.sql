-- Home-time retarget (documents/logs/25_home_time_research_and_methodology_references.md): the
-- real last moment a driver was AT their home hub -- the business-cadence companion to the legal
-- HOS cycle clocks, which were confirmed dormant almost always (documents/logs/24). Written by
-- telemetry_simulator.py's _complete_trip() when a trip's real destination is the driver's own
-- home hub; read (and chain-walk PROJECTED forward through the driver's real trip queue, not read
-- as a static value) by score_quote.py's project_driver_state().
alter table live.driver_status add column if not exists last_home_arrival_at timestamptz;
