#!/bin/sh
# Dev only (docker-compose.override.yaml, service backend-deps).
#
# The ONLY process that writes the shared backend venv (the backend_venv volume). uv's own
# sync lock lives in each container's private /tmp, so it cannot serialize backend,
# celery-worker and celery-beat against each other; one writer can.
#
# Every POLL_SECONDS: if uv.lock/pyproject.toml changed, sync the venv to them. Every
# VERIFY_SECONDS: re-check the venv even without a change (catches manual installs).
# After a successful sync, publish the synced file hash to STAMP; run-with-deps-guard.sh
# only starts a service once STAMP matches the current files.
#
# UV_LOCKED=1 (set by compose) means uv never rewrites uv.lock: an inconsistent
# pyproject.toml/uv.lock pair fails here, loudly, and the services stay paused until the
# files are fixed on the host.
set -u
cd /app

STAMP=/app/.venv/.lock-stamp
POLL_SECONDS=2
VERIFY_SECONDS=60

files_hash() { cat uv.lock pyproject.toml | sha256sum | cut -d' ' -f1; }

# PID 1 ignores SIGTERM unless trapped; exit promptly on `docker compose stop`.
trap 'exit 0' TERM INT

attempted=""
last_attempt=0
while :; do
    want=$(files_hash)
    now=$(date +%s)
    if [ "$want" != "$attempted" ] || [ $((now - last_attempt)) -ge "$VERIFY_SECONDS" ]; then
        changed=$([ "$want" != "$attempted" ] && echo yes || echo no)
        attempted=$want
        last_attempt=$now
        if uv sync --check --quiet 2>/dev/null || uv sync; then
            echo "$want" > "$STAMP.tmp" && mv "$STAMP.tmp" "$STAMP"
            [ "$changed" = yes ] && echo "deps-sentinel: venv matches uv.lock (files $want)"
        else
            rm -f "$STAMP"
            echo "deps-sentinel: uv sync FAILED for files $want. Python services stay paused" \
                 "until uv.lock and pyproject.toml are consistent (run 'uv lock' on the host)." >&2
        fi
    fi
    sleep "$POLL_SECONDS"
done
