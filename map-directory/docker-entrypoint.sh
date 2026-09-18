#!/bin/sh
# Drop root before uvicorn starts (2.1, R17 "Dockerfile USER").
#
# The image used to run the whole server as root. The one thing root was
# needed for is the data volume: Fly (and docker compose's named volumes)
# mount /data owned by root, so a container that starts as a non-root
# user cannot create the database, its WAL, the logs/ directory or a
# backup snapshot. So the entrypoint runs as root just long enough to hand
# the volume to the `app` user, then execs uvicorn as that user. chown is
# metadata-only, so a multi-GB database costs nothing here.
#
# The env is NOT reset on the way down: every secret Fly injects
# (API_TOKEN, INGEST_TOKEN, FLY_API_TOKEN for self-update) must reach the
# app. `fly ssh console` still lands as root; it goes through Fly's own
# agent, not this script.
set -eu
data_dir="$(dirname "${DATABASE_PATH:-/data/weather.db}")"
# A relative path is refused BEFORE it is resolved: resolving `.` names
# the working directory (/app in the image, the checkout in CI), and
# chowning that is exactly the mistake the root check exists to stop
# (CI, 2026-09-07: `DATABASE_PATH=weather.db` chowned the repository).
case "$data_dir" in
  /*) ;;
  *)
    echo "docker-entrypoint: refusing DATABASE_PATH=${DATABASE_PATH:-unset}: it must be an absolute path to a file inside a data directory" >&2
    exit 1 ;;
esac
# Resolve `..` and symlinks before the root check below: a literal check
# let `/data/x/../../weather.db` name `/` (round-three review, SEC info).
# `realpath -m` (coreutils) accepts a path that does not exist yet.
if command -v realpath >/dev/null 2>&1; then
  data_dir="$(realpath -m "$data_dir" 2>/dev/null || echo "$data_dir")"
fi
if [ "$(id -u)" = "0" ]; then
  # Bounded (2.1 pre-release review, INF-4): DATABASE_PATH=/data, a
  # natural value for a mount point, made this `chown -R app:app /` as
  # root on every boot. Refuse the roots that cannot be a data directory,
  # and hand the directory over ONLY when root still owns it -- an
  # already-migrated volume is left alone, which also keeps a boot from
  # walking a multi-GB tree for nothing.
  case "$data_dir" in
    ""|"/"|"."|"//")
      echo "docker-entrypoint: refusing to chown '$data_dir' (DATABASE_PATH=${DATABASE_PATH:-unset} must name a file inside a data directory)" >&2
      exit 1 ;;
  esac
  mkdir -p "$data_dir"
  owner="$(stat -c %u "$data_dir" 2>/dev/null || echo 0)"
  if [ "$owner" = "0" ]; then
    if ! chown -R app:app "$data_dir"; then
      echo "docker-entrypoint: could not chown $data_dir to app; the server may fail to write" >&2
    fi
  fi
  # HOME must be writable by the app user: /app is root-owned image
  # content. /tmp, not the data directory (round two, I2): nothing in the
  # app reads HOME, and whatever a library caches under ~/.cache would
  # otherwise land on the paid volume beside the database.
  export HOME=/tmp
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=app --regid=app --init-groups "$@"
  elif command -v runuser >/dev/null 2>&1; then
    exec runuser -u app -- "$@"
  fi
  echo "docker-entrypoint: neither setpriv nor runuser found; RUNNING AS ROOT" >&2
fi
exec "$@"
