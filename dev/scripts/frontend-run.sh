#!/bin/sh
# Dev only (docker-compose.override.yaml, service frontend).
#
# node_modules (the frontend_node_modules volume) is derived from package.json +
# package-lock.json and nothing else:
#   - `npm ci` installs exactly the lock and never rewrites it (it fails if package.json and
#     the lock disagree; this script then waits for the files to be fixed on the host).
#   - Skipped when node_modules was already built from the current files (hash stamp).
#   - While the dev server runs, the files are re-hashed every POLL_SECONDS; on any change the
#     server is stopped and the script exits 3, so the restart policy (unless-stopped) brings
#     the container back through `npm ci`.
set -u
cd /app

STAMP=node_modules/.lock-stamp
POLL_SECONDS=3

files_hash() { cat package.json package-lock.json | sha256sum | cut -d' ' -f1; }

want=$(files_hash)
if [ "$(cat "$STAMP" 2>/dev/null)" != "$want" ]; then
    until npm ci --no-audit --no-fund; do
        echo "frontend-run: npm ci FAILED for files $want; waiting for package.json/package-lock.json to change" >&2
        while [ "$(files_hash)" = "$want" ]; do sleep "$POLL_SECONDS"; done
        want=$(files_hash)
    done
    echo "$want" > "$STAMP"
fi

"$@" &
child=$!
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit $?' TERM INT

while kill -0 "$child" 2>/dev/null; do
    sleep "$POLL_SECONDS" &
    wait $!
    if [ "$(files_hash)" != "$want" ]; then
        echo "frontend-run: package.json/package-lock.json changed; restarting through npm ci" >&2
        kill -TERM "$child" 2>/dev/null
        wait "$child"
        exit 3
    fi
done
wait "$child"
exit $?
