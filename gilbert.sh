#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

build_frontend() {
    echo "Building frontend..."
    if [ ! -d "$SCRIPT_DIR/frontend/node_modules" ]; then
        echo "Installing frontend dependencies..."
        cd "$SCRIPT_DIR/frontend" && npm install
    fi
    cd "$SCRIPT_DIR/frontend" && npm run build
    rm -rf "$SCRIPT_DIR/src/gilbert/web/spa"
    cp -r "$SCRIPT_DIR/frontend/dist" "$SCRIPT_DIR/src/gilbert/web/spa"
    cd "$SCRIPT_DIR"
}

case "$1" in
    infra)
        echo "Starting infrastructure..."
        docker compose up -d
        ;;
    start)
        build_frontend
        echo "Starting Gilbert..."
        uv run python -m gilbert
        ;;
    dev)
        build_frontend
        echo "Starting Gilbert (dev)..."
        uv run python -m gilbert
        ;;
    build)
        build_frontend
        echo "Frontend built to src/gilbert/web/spa/"
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
        echo "Usage: gilbert.sh {infra|start|dev|build|stop}"
        exit 1
        ;;
esac
