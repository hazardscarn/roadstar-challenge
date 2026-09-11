-- Records the actual H3 cell each region_id corresponds to (sim/cluster_locations.py's k-means
-- replaced with H3 hexagonal indexing -- see that file's docstring and documents/logs/16 for why:
-- Uber's own open-source H3 library, and the real-data finding in arxiv:2605.07733 (Ping2Hex,
-- FTL truck-load matching) that H3 hexagons beat k-means region clustering for this exact
-- problem). Kept for transparency/debuggability -- region_id alone (a dense reindex of distinct
-- H3 cells) isn't human-checkable against a map the way the raw H3 cell string is.
alter table calibration.region_clusters add column h3_cell text;
