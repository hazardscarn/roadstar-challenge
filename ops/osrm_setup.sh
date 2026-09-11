#!/usr/bin/env bash
# One-time OSRM build for the Ontario road network. Run once; re-running is safe (idempotent
# on the extract/partition/customize steps since they check for existing output files).
# Usage: ops/osrm_setup.sh [--serve]
#   --serve  also start the routing server on :5000 after the build finishes (foreground)
set -euo pipefail

# ghcr.io image, not osrm/osrm-backend on Docker Hub -- the Docker Hub image is amd64-only and
# fails with "exec format error" on this machine's arm64 (GB10/Grace-Blackwell) host.
IMAGE="ghcr.io/project-osrm/osrm-backend:latest"
DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/ops/osrm-data"
mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

if [ ! -f ontario-latest.osm.pbf ]; then
  echo "[osrm_setup] downloading Ontario OSM extract..."
  wget -q --show-progress https://download.geofabrik.de/north-america/canada/ontario-latest.osm.pbf
fi

if [ ! -f ontario-latest.osrm ]; then
  echo "[osrm_setup] osrm-extract..."
  docker run -t -v "${DATA_DIR}:/data" ${IMAGE} osrm-extract -p /opt/car.lua /data/ontario-latest.osm.pbf
fi

if [ ! -f ontario-latest.osrm.partition ]; then
  echo "[osrm_setup] osrm-partition..."
  docker run -t -v "${DATA_DIR}:/data" ${IMAGE} osrm-partition /data/ontario-latest.osrm
fi

if [ ! -f ontario-latest.osrm.cell_metrics ]; then
  echo "[osrm_setup] osrm-customize..."
  docker run -t -v "${DATA_DIR}:/data" ${IMAGE} osrm-customize /data/ontario-latest.osrm
fi

echo "[osrm_setup] build complete."

if [ "${1:-}" = "--serve" ]; then
  echo "[osrm_setup] starting routing server on :5000..."
  docker run -t -i -p 5000:5000 -v "${DATA_DIR}:/data" ${IMAGE} osrm-routed --algorithm mld /data/ontario-latest.osrm
fi
