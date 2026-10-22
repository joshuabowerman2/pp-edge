#!/bin/sh
# One container runs both the always-on engine and the web app; they share the database.
set -e
edge init
edge run --export /data/site/index.json --results /data/site/results.html &
ENGINE=$!
edge serve --port "${PORT:-8080}" &
WEB=$!
# If either stops, stop the container so the platform restarts it.
while kill -0 $ENGINE 2>/dev/null && kill -0 $WEB 2>/dev/null; do sleep 15; done
echo "A process exited; restarting container." >&2
exit 1
