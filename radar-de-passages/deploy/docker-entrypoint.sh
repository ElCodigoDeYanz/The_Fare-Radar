#!/bin/sh
# Mounted volumes (Fly.io, Docker named volumes on some hosts) arrive owned by root.
set -eu
DATA_DIR="$(dirname "${RADAR_DB:-/data/radar.sqlite3}")"
mkdir -p "$DATA_DIR"
if [ "$(id -u)" = "0" ]; then
  chown -R radar:radar "$DATA_DIR"
  exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi
exec "$@"
