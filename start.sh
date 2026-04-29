#!/bin/bash
cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

echo "================================================"
echo "  DETROIT PULSE — Starting services"
echo "================================================"
echo ""

echo "Waiting for PostgreSQL..."
until docker exec detroitpulse-postgres pg_isready -U detroit -q 2>/dev/null; do
    sleep 1
done
echo "  ✓ PostgreSQL ready"

echo "Waiting for Redis..."
until docker exec detroitpulse-redis redis-cli ping 2>/dev/null | grep -q PONG; do
    sleep 1
done
echo "  ✓ Redis ready"

echo ""
echo "Building frontend..."
cd "$SCRIPT_DIR/frontend"
node_modules/.bin/vite build > "$SCRIPT_DIR/logs/frontend_build.log" 2>&1
if [ $? -eq 0 ]; then
    echo "  ✓ Frontend built"
else
    echo "  ✗ Frontend build failed — check logs/frontend_build.log"
    exit 1
fi
cd "$SCRIPT_DIR"

echo ""
echo "Starting services..."
mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/.pids"
export OLLAMA_NUM_PARALLEL=4
export OLLAMA_MAX_LOADED_MODELS=2

# API server on port 8080 — serves both API and built frontend
echo "  → Starting API + frontend server on port 8080"
"$SCRIPT_DIR/.venv/bin/uvicorn" api.main:app \
    --host 0.0.0.0 --port 8080 \
    > "$SCRIPT_DIR/logs/api.log" 2>&1 &
echo $! > "$SCRIPT_DIR/.pids/api.pid"
echo "    PID: $(cat $SCRIPT_DIR/.pids/api.pid)"

# Wait for API
until curl -s http://localhost:8080/health > /dev/null 2>&1; do
    sleep 1
done
echo "  ✓ API + frontend ready at http://localhost"

# Pipeline
echo "  → Starting pipeline"
"$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/run_pipeline.py" \
    > "$SCRIPT_DIR/logs/pipeline.log" 2>&1 &
echo $! > "$SCRIPT_DIR/.pids/pipeline.pid"
echo "    PID: $(cat $SCRIPT_DIR/.pids/pipeline.pid)"

echo ""
echo "================================================"
echo "  All services running"
echo "================================================"
echo "  App:      http://localhost"
echo "  API docs: http://localhost/docs"
echo "  Health:   http://localhost/health"
echo ""
echo "  tail -f logs/pipeline_debug.log"
echo "  Stop:     ./stop.sh"
echo "================================================"
echo ""

cleanup() {
    echo ""
    echo "Stopping services..."
    kill $(cat "$SCRIPT_DIR/.pids/api.pid")      2>/dev/null && echo "  ✓ API stopped"
    kill $(cat "$SCRIPT_DIR/.pids/pipeline.pid") 2>/dev/null && echo "  ✓ Pipeline stopped"
    rm -f "$SCRIPT_DIR/.pids/"*.pid
    exit 0
}
trap cleanup SIGINT SIGTERM

tail -f "$SCRIPT_DIR/logs/api.log" \
        "$SCRIPT_DIR/logs/pipeline.log" &
wait