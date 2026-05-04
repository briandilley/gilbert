#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Exit code Gilbert uses to request a supervised restart. Matches
# ``EX_TEMPFAIL`` from ``sysexits.h`` — "temporary failure, try again."
# Any other exit (0, 130 from Ctrl+C, 143 from SIGTERM, any crash code)
# is treated as a terminal stop and the supervisor loop exits.
RESTART_EXIT_CODE=75

# Captures Gilbert's stderr (in addition to showing it on the terminal)
# so glibc abort messages, native-extension tracebacks, and anything else
# that bypasses Python's logging framework survive a crash. Python's own
# logs still go to ``.gilbert/gilbert.log`` via the logging config.
STDERR_LOG=".gilbert/stderr.log"

# How many consecutive crashes (non-zero / non-signal / non-restart exits)
# to tolerate before giving up, and how long to wait between attempts. A
# clean exit, Ctrl+C, SIGTERM, or an explicit restart all reset the
# counter; only back-to-back crashes count.
MAX_CRASH_RESTARTS=3
CRASH_RESTART_DELAY=20

# Set by the SIGINT/SIGTERM trap so the supervisor loop knows to stop
# even if the signal arrives between Gilbert runs (e.g. during a
# ``uv sync``). Without this, hitting Ctrl+C during the sync would
# propagate the interrupt to uv but the loop would then cheerfully
# start Gilbert anyway.
SUPERVISOR_STOP=false

refresh_std_plugins() {
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
        return
    fi

    # Already initialized — opportunistically pull the latest commits
    # from the submodule's tracked branch so a routine ``./gilbert.sh
    # start`` picks up plugin updates without an explicit
    # ``git submodule update --remote`` step.
    #
    # Only auto-refresh when the parent (core) working tree is clean.
    # If the user has WIP changes here, the recorded submodule SHA may
    # be part of that WIP — silently bumping it would stomp deliberate
    # local state. ``--untracked-files=no`` ignores untracked clutter
    # (build outputs, scratch files); the ``grep -v`` filters out the
    # submodule pointer itself, which is the line we're trying to
    # advance.
    local parent_dirty
    parent_dirty=$(
        git -C "$SCRIPT_DIR" status --porcelain --untracked-files=no \
            | grep -v '^.. std-plugins$' \
            || true
    )
    if [ -n "$parent_dirty" ]; then
        echo "Skipping std-plugins refresh — uncommitted changes in core:"
        echo "$parent_dirty" | sed 's/^/  /'
        return
    fi

    echo "Refreshing std-plugins submodule from remote..."
    cd "$SCRIPT_DIR" && git submodule update --init --recursive --remote std-plugins
}

sync_python_deps() {
    # Re-sync the uv workspace so any plugin deps that changed since
    # the last start (e.g. a plugin installed at runtime that declares
    # third-party deps in its own ``pyproject.toml``) are installed
    # into the venv before we launch Gilbert. This is idempotent and
    # fast when everything is already in sync.
    echo "Syncing Python dependencies..."
    cd "$SCRIPT_DIR" && uv sync

    # The playwright python package is in the workspace, but the
    # actual Chromium binary it drives lives in a separate
    # per-user cache. Auto-fetch it on first start so the browser
    # plugin works out of the box. Idempotent: playwright skips
    # already-installed browsers.
    if uv run python -c 'import playwright' >/dev/null 2>&1; then
        if ! uv run python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().__enter__(); p.chromium.executable_path' >/dev/null 2>&1; then
            echo "Fetching Chromium for the browser plugin (one-time, ~170 MB)..."
            uv run playwright install chromium || \
                echo "  (skipped — run 'uv run playwright install chromium' manually if needed)"
        fi
    fi
}

run_gilbert_supervised() {
    # Supervisor loop: run Gilbert, inspect its exit code, restart on
    # ``RESTART_EXIT_CODE`` (re-syncing the venv first so new plugin
    # deps land), and bail out on anything else. A SIGINT/SIGTERM trap
    # flips ``SUPERVISOR_STOP`` so Ctrl+C during a sync or a restart
    # cycle still breaks the loop cleanly.
    local exit_code
    local crash_count=0
    local stderr_log_abs="$SCRIPT_DIR/$STDERR_LOG"
    trap 'SUPERVISOR_STOP=true' INT TERM

    refresh_std_plugins
    mkdir -p "$(dirname "$stderr_log_abs")"

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
        {
            echo
            echo "===== Gilbert starting at $(date -Iseconds) ====="
        } >> "$stderr_log_abs"
        # Temporarily drop ``set -e`` so a non-zero exit from Gilbert
        # doesn't abort the script before we can inspect the code.
        # Duplicate stderr to ``$STDERR_LOG`` so glibc abort messages and
        # other non-Python-logging output survive a crash.
        set +e
        uv run python -m gilbert 2> >(tee -a "$stderr_log_abs" >&2)
        exit_code=$?
        set -e
        echo "===== Gilbert exited with code $exit_code at $(date -Iseconds) =====" \
            >> "$stderr_log_abs"

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
                crash_count=0
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
                crash_count=$((crash_count + 1))
                if [ "$crash_count" -ge "$MAX_CRASH_RESTARTS" ]; then
                    echo "Gilbert crashed $crash_count times in a row (last exit $exit_code) — giving up. See $STDERR_LOG." >&2
                    trap - INT TERM
                    exit "$exit_code"
                fi
                echo "Gilbert exited with code $exit_code — attempt $crash_count/$MAX_CRASH_RESTARTS, restarting in ${CRASH_RESTART_DELAY}s..." >&2
                # ``sleep`` is interruptible; if the user hits Ctrl+C
                # during the delay the trap flips SUPERVISOR_STOP and we
                # break out on the next iteration. ``|| true`` keeps
                # ``set -e`` from aborting on a signal-killed sleep.
                sleep "$CRASH_RESTART_DELAY" || true
                if [ "$SUPERVISOR_STOP" = "true" ]; then
                    echo "Supervisor stopping (interrupt during crash-restart delay)."
                    break
                fi
                continue
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
    browser-doctor)
        # Sanity-check the host for the browser plugin's runtime deps.
        # Exit non-zero on any FAIL so CI / scripts can branch on it.
        echo "browser-doctor: checking host for browser-plugin requirements"
        FAIL=0
        check() {
            local label="$1"
            local cmd="$2"
            if eval "$cmd" >/dev/null 2>&1; then
                echo "  PASS  $label"
            else
                echo "  FAIL  $label"
                FAIL=1
            fi
        }
        check "playwright python package"   "uv run python -c 'import playwright' 2>&1"
        check "chromium browser binary"     "uv run python -c 'from playwright.sync_api import sync_playwright; sync_playwright().__enter__().chromium.executable_path'"
        check "Xvfb (VNC live login)"       "command -v Xvfb"
        check "x11vnc (VNC live login)"     "command -v x11vnc"
        check "websockify (VNC live login)" "command -v websockify"
        if [ "$FAIL" -ne 0 ]; then
            echo
            echo "One or more checks failed. Install hints:"
            echo "  uv run playwright install chromium       # browser binary"
            echo "  uv run playwright install-deps chromium  # OS shared libs (Linux, sudo)"
            echo "  apt-get install xvfb x11vnc websockify   # VNC live-login extras"
            exit 1
        fi
        echo
        echo "All browser-plugin checks PASS."
        ;;
    *)
        echo "Usage: gilbert.sh {start|dev|build|stop|browser-doctor}"
        exit 1
        ;;
esac
