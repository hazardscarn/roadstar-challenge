-- Real user feedback: ground_truth.drivers' own terminal_zone/home_zone columns are nearly blank
-- (130/131 drivers report the same generic 'RSTAR' code, not a real per-driver location) --
-- confirmed directly, not assumed. driver_home_hub_id() used to fall back on that field,
-- collapsing the ENTIRE demo fleet onto one hub (London). This table is the real fix: each
-- driver's real historical-leg-derived home anchor where we have one (53/131 drivers), and a
-- real-data-informed ASSUMED one (seeded, reproducible) for the rest -- see
-- sim/calibrate_driver_home_hub.py for how it's computed, and its own header comment for why.
create table if not exists calibration.driver_home_hub (
  driver_id int primary key references ground_truth.drivers(driver_id),
  hub_location_id int not null references reference.locations(location_id),
  source text not null check (source in ('historical', 'assumed')),
  computed_at timestamptz not null default now()
);
