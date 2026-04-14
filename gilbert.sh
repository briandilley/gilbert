#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Exit code Gilbert uses to request a supervised restart. Matches
# ``EX_TEMPFAIL`` from ``sysexits.h`` — "temporary failure, try again."
# Any other exit (0, 130 from Ctrl+C, 143 from SIGTERM, any crash code)
# is treated as a terminal stop and the supervisor loop exits.
RESTART_EXIT_CODE=75

# Set by the SIGINT/SIGTERM trap so the supervisor loop knows to stop
# even if the signal arrives between Gilbert runs (e.g. during a
# ``uv sync``). Without this, hitting Ctrl+C during the sync would
# propagate the interrupt to uv but the loop would then cheerfully
# start Gilbert anyway.
SUPERVISOR_STOP=false

ensure_std_plugins() {
    # If std-plugins/ is empty (or missing plugin.yaml files), initialize
    # the git submodule so we pick up the first-party plugin repo. This
    # makes a fresh clone of Gilbert one-step: ``git clone … && cd gilbert
    # && ./gilbert.sh start`` just works, without needing a separate
    # ``git submodule update --init --recursive`` step.
    #
    # We check for any ``plugin.yaml`` under std-plugins/*/ rather than
    # just the directory existing, because ``git clone`` creates
    # std-plugins/ as an empty dir even when the submodule isn't
    # initialized.
    if ! compgen -G "$SCRIPT_DIR/std-plugins/*/plugin.yaml" > /dev/null; then
        echo "std-plugins/ is empty — initializing the git submodule..."
        cd "$SCRIPT_DIR" && git submodule update --init --recursive
    fi
}

sync_python_deps() {
    # Re-sync the uv workspace so any plugin deps that changed since
    # the last start (e.g. a plugin installed at runtime that declares
    # third-party deps in its own ``pyproject.toml``) are installed
    # into the venv before we launch Gilbert. This is idempotent and
    # fast when everything is already in sync.
    echo "Syncing Python dependencies..."
    cd "$SCRIPT_DIR" && uv sync
}

run_gilbert_supervised() {
    # Supervisor loop: run Gilbert, inspect its exit code, restart on
    # ``RESTART_EXIT_CODE`` (re-syncing the venv first so new plugin
    # deps land), and bail out on anything else. A SIGINT/SIGTERM trap
    # flips ``SUPERVISOR_STOP`` so Ctrl+C during a sync or a restart
    # cycle still breaks the loop cleanly.
    local exit_code
    trap 'SUPERVISOR_STOP=true' INT TERM

    ensure_std_plugins

    while true; do
        if [ "$SUPERVISOR_STOP" = "true" ]; then
            echo "Supervisor stopping."
            break
        fi

        sync_python_deps

        if [ "$SUPERVISOR_STOP" = "true" ]; then
            # Signal arrived during uv sync — stop before launching.
            echo "Supervisor stopping (interrupt during sync)."
            break
        fi

        echo "Starting Gilbert..."
        # Temporarily drop ``set -e`` so a non-zero exit from Gilbert
        # doesn't abort the script before we can inspect the code.
        set +e
        uv run python -m gilbert
        exit_code=$?
        set -e

        case "$exit_code" in
            0)
                echo "Gilbert stopped cleanly."
                break
                ;;
            "$RESTART_EXIT_CODE")
                if [ "$SUPERVISOR_STOP" = "true" ]; then
                    # Restart was requested, but then the user hit
                    # Ctrl+C during the shutdown — honor the stop.
                    echo "Restart requested, but supervisor is stopping."
                    break
                fi
                echo "Gilbert requested a restart — resyncing and relaunching..."
                continue
                ;;
            130)
                echo "Gilbert interrupted (Ctrl+C) — not restarting."
                break
                ;;
            143)
                echo "Gilbert terminated (SIGTERM) — not restarting."
                break
                ;;
            *)
                echo "Gilbert exited with code $exit_code — not restarting." >&2
                trap - INT TERM
                exit "$exit_code"
                ;;
        esac
    done
    trap - INT TERM
}

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
    start)
        build_frontend
        run_gilbert_supervised
        ;;
    dev)
        build_frontend
        run_gilbert_supervised
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
        echo "Usage: gilbert.sh {start|dev|build|stop}"
        exit 1
        ;;
esac
