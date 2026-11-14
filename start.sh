#!/usr/bin/env bash
# Start Detroit Pulse on a shared host: PostGIS + Redis (127.0.0.1 only), migrations,
# the API on 127.0.0.1:7710 and the pipeline, everything at nice 19 with capped threads.
#
#   ./start.sh                 # services + API + pipeline (all stages)
#   ./start.sh api             # services + API only
#   STAGES=extract,resolver ./start.sh
# Stop with ./stop.sh (add --services to also stop the containers).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs .pids data/sdr data/traintap

docker-compose up -d
echo "waiting for PostGIS and Redis health..."
for _ in $(seq 1 60); do
  pg=$(docker inspect -f '{{.State.Health.Status}}' pulse-postgis 2>/dev/null || true)
  rd=$(docker inspect -f '{{.State.Health.Status}}' pulse-redis 2>/dev/null || true)
  [[ "$pg" == healthy && "$rd" == healthy ]] && break
  sleep 2
done

uv sync --frozen --quiet
PY=.venv/bin/python             # run the venv python directly so the pid file holds the real process
$PY -m alembic upgrade head

# keep this box's other services responsive
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}" MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"

nice -n 19 $PY -m api >> logs/api.log 2>&1 &
echo $! > .pids/api.pid
echo "API on 127.0.0.1:${PULSE_API_PORT:-7710} (pid $(cat .pids/api.pid), log logs/api.log)"

if [[ "${1:-}" != "api" ]]; then
  nice -n 19 $PY run_pipeline.py --stages "${STAGES:-ingest,transcribe,extract,resolver}" \
    >> logs/pipeline.log 2>&1 &
  echo $! > .pids/pipeline.pid
  echo "pipeline (pid $(cat .pids/pipeline.pid), log logs/pipeline.log)"
fi
