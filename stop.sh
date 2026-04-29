#!/bin/bash

cd "$(dirname "$0")"

echo "Stopping Detroit Pulse services..."

for service in api pipeline frontend; do
    pidfile=".pids/${service}.pid"
    if [ -f "$pidfile" ]; then
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "  ✓ ${service} stopped (PID $pid)"
        else
            echo "  - ${service} was not running"
        fi
        rm "$pidfile"
    fi
done

echo "Done."
