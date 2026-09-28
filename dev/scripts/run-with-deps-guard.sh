#!/bin/sh
# Dev only (docker-compose.override.yaml: backend, celery-worker, celery-beat).
#
# Runs "$@" only while the shared venv matches the local uv.lock/pyproject.toml:
#   1. Waits until python-deps-sentinel.sh has published a STAMP equal to the current files.
#   2. Starts the command, then re-hashes the files every POLL_SECONDS. On any change it
#      stops the command and exits 3; the service's restart policy (unless-stopped) brings
#      the container back, and step 1 holds it until the sentinel has synced the new files.
# Polling rather than an event watcher: a missed filesystem event can never leave a container
# running on stale dependencies.
set -u
cd /app

STAMP=/app/.venv/.lock-stamp
POLL_SECONDS=3

files_hash() { cat uv.lock pyproject.toml | sha256sum | cut -d' ' -f1; }

want=$(files_hash)
waited=0
until [ "$(cat "$STAMP" 2>/dev/null)" = "$want" ]; do
    if [ $((waited % 30)) -eq 0 ]; then
        echo "deps-guard: waiting for backend-deps to sync uv.lock (files $want)" >&2
    fi
    sleep 2
    waited=$((waited + 2))
    want=$(files_hash)
done

"$@" &
child=$!
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit $?' TERM INT

while kill -0 "$child" 2>/dev/null; do
    sleep "$POLL_SECONDS" &
    wait $!   # interruptible, so a stop signal is handled immediately
    if [ "$(files_hash)" != "$want" ]; then
        echo "deps-guard: uv.lock/pyproject.toml changed; restarting on the new dependencies" >&2
        kill -TERM "$child" 2>/dev/null
        wait "$child"
        exit 3
    fi
done
wait "$child"
exit $?
