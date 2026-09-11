-- Replaces raw lat/lon as a model feature with a proper geographic region -- trees split
-- axis-aligned, so raw lat/lon can only approximate a real 2D area via many sequential
-- thresholds, and with sparse per-location data (2,110 locations, some visited only a handful
-- of times) that's a recipe for overfitting to coordinate noise rather than learning real
-- geography (found directly: a specific remote location outscored a real hub in a smoke test).
-- region_id (from k-means over all real location coordinates, sim/cluster_locations.py) gives
-- trees a natural categorical split on an actual cluster boundary instead.
alter table reference.locations add column region_id integer;

create table calibration.region_clusters (
  region_id integer primary key,
  centroid_lat numeric,
  centroid_lon numeric,
  location_count integer
);
