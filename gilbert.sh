#!/usr/bin/env bash
set -e

case "$1" in
    infra)
        echo "Starting infrastructure..."
        docker compose up -d
        ;;
    start)
        echo "Starting Gilbert..."
        uv run python -m gilbert
        ;;
    stop)
        PID_FILE=".gilbert/gilbert.pid"
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            echo "Stopping Gilbert (PID $PID)..."
            kill "$PID" 2>/dev/null || echo "Process not running"
            rm -f "$PID_FILE"
        else
            echo "No PID file found — Gilbert may not be running"
        fi
        ;;
    *)
        echo "Usage: gilbert.sh {infra|start|stop}"
        exit 1
        ;;
esac
