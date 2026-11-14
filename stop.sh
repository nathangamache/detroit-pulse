#!/usr/bin/env bash
# Stop the API and pipeline started by ./start.sh. `--services` also stops PostGIS/Redis
# (their named volumes are kept).
set -uo pipefail
cd "$(dirname "$0")"
for name in pipeline api; do
  f=".pids/$name.pid"
  if [[ -f "$f" ]]; then
    pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" && echo "stopped $name ($pid)"
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
    fi
    rm -f "$f"
  fi
done
if [[ "${1:-}" == "--services" ]]; then
  docker-compose stop
fi
