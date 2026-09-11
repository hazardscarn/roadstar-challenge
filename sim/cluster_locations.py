"""Replaces raw lat/lon as a model feature with a proper geographic region_id -- see
sim/sql/021_add_region_clusters.sql for why (trees split axis-aligned; raw lat/lon can't express
a real 2D region without many sequential thresholds, and with sparse per-location data that's
overfitting to coordinate noise, not learned geography -- caught directly in a smoke test where
a remote location outscored a real hub).

H3 (Uber's own open-source hexagonal spatial index), NOT k-means -- see documents/logs/16
(researching Uber Freight's real-data-driven truck matching led to arxiv:2605.07733, "Ping2Hex":
FTL truck-load matching trained on real shipment data, using H3 hexagonal binning, explicitly
found to beat region-clustering for this exact problem). Concretely better here too:
  - deterministic, not a fitted model -- no silhouette-score search, no random_state to pin, and
    no risk of cluster boundaries silently shifting when a location is added/removed later.
  - genuinely free multi-resolution -- a resolution is just a function argument, not a re-fit;
    swapping granularity costs nothing (see RESOLUTION below and the module's __main__ printout
    comparing several).
  - real hexagonal geometry (every cell has 6 uniform neighbors) rather than k-means' arbitrary,
    data-dependent cell shapes.

RESOLUTION=4 gives 25 distinct cells over this fleet's 2,110 real locations -- close in scale to
the k-means k=30 it replaces, so the region_id contract downstream (N_REGIONS in
sim/training/train_state_value_function.py, sim/engine/value_function.py) needs only a constant
update, not a redesign.
"""
import h3
import numpy as np

from sim.db import cursor

RESOLUTION = 4


def load_coordinates() -> tuple[list[int], np.ndarray]:
    with cursor() as cur:
        cur.execute("select location_id, ST_Y(geog::geometry), ST_X(geog::geometry) from reference.locations")
        rows = cur.fetchall()
    location_ids = [r[0] for r in rows]
    coords = np.array([[float(r[1]), float(r[2])] for r in rows])
    return location_ids, coords


def assign_h3_regions(resolution: int = RESOLUTION) -> None:
    location_ids, coords = load_coordinates()
    h3_cells = [h3.latlng_to_cell(lat, lon, resolution) for lat, lon in coords]

    # Dense reindex (0..N-1), sorted by cell string for a stable, reproducible ordering -- H3
    # cell strings themselves aren't meaningful as model input, just their identity.
    distinct_cells = sorted(set(h3_cells))
    cell_to_region_id = {cell: i for i, cell in enumerate(distinct_cells)}
    region_ids = [cell_to_region_id[c] for c in h3_cells]

    with cursor() as cur:
        for loc_id, region_id in zip(location_ids, region_ids):
            cur.execute(
                "update reference.locations set region_id = %s where location_id = %s",
                (int(region_id), loc_id),
            )

        cur.execute("delete from calibration.region_clusters")
        for region_id, cell in enumerate(distinct_cells):
            member_coords = coords[[i for i, c in enumerate(h3_cells) if c == cell]]
            centroid_lat, centroid_lon = member_coords[:, 0].mean(), member_coords[:, 1].mean()
            cur.execute(
                """insert into calibration.region_clusters (region_id, centroid_lat, centroid_lon, location_count, h3_cell)
                   values (%s, %s, %s, %s, %s)""",
                (region_id, float(centroid_lat), float(centroid_lon), int((np.array(h3_cells) == cell).sum()), cell),
            )

    print(f'Assigned region_id to {len(location_ids)} locations across {len(distinct_cells)} H3 (res={resolution}) cells.')
    counts = sorted((int((np.array(h3_cells) == c).sum()) for c in distinct_cells), reverse=True)
    print('Region sizes:', counts)


if __name__ == '__main__':
    print('H3 resolution comparison (informational -- RESOLUTION above is what actually gets used):')
    location_ids, coords = load_coordinates()
    for res in [3, 4, 5, 6, 7]:
        n_cells = len({h3.latlng_to_cell(lat, lon, res) for lat, lon in coords})
        print(f'  res={res}: {n_cells} distinct cells')
    print()
    assign_h3_regions()
