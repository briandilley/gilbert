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
}

run_migrations_unattended() {
    # Apply every pending migration without prompting. Used by the
    # ``update`` flow where we've already committed to upgrading.
    cd "$SCRIPT_DIR" && uv run python -m gilbert.cli.migrate up
}

check_pending_migrations() {
    # On ``start``: list pending migrations and, if there are any AND
    # we're on a TTY, ask the user whether to apply them before
    # launching. On non-TTY (systemd, CI, etc.) we print a warning and
    # skip — the user can run ``./gilbert.sh migrate up`` explicitly.
    #
    # ``gilbert.cli.migrate list`` exits 0 when nothing is pending and
    # 1 when there's work — that exit code is what we branch on.
    cd "$SCRIPT_DIR"
    local exit_code=0
    set +e
    uv run python -m gilbert.cli.migrate list
    exit_code=$?
    set -e
    if [ "$exit_code" -eq 0 ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        echo
        echo "Pending migrations detected, but stdin is not a TTY." >&2
        echo "Run './gilbert.sh migrate up' to apply them." >&2
        echo
        return 0
    fi
    echo
    read -r -p "Apply pending migrations now? [y/N] " reply
    case "$reply" in
        y|Y|yes|YES)
            run_migrations_unattended
            ;;
        *)
            echo "Skipping migrations. Run './gilbert.sh migrate up' when ready."
            ;;
    esac
}

pull_latest_plugin() {
    # Helper: fast-forward a single plugin checkout if it's a git
    # repo with a clean working tree. Plugins under local-plugins/
    # and installed-plugins/ are independent clones (not submodules),
    # so each gets its own ``git pull``. Non-git directories are
    # silently skipped — they're user code or extracted tarballs, not
    # something we know how to update.
    local plugin_dir="$1"
    local label
    label=$(basename "$plugin_dir")
    if [ ! -d "$plugin_dir/.git" ]; then
        return 0
    fi
    local dirty
    dirty=$(git -C "$plugin_dir" status --porcelain --untracked-files=no || true)
    if [ -n "$dirty" ]; then
        echo "  skipping $label — uncommitted changes" >&2
        return 0
    fi
    echo "  pulling $label..."
    if ! git -C "$plugin_dir" pull --ff-only --quiet; then
        echo "  $label: git pull failed (continuing)" >&2
    fi
}

pull_latest() {
    # Fast-forward the core repo to origin, then bring the std-plugins
    # submodule and every git-managed plugin under local-plugins/ and
    # installed-plugins/ along. Refuses to run when the working tree
    # is dirty because a hard pull on top of WIP changes would either
    # reject with a merge conflict or surprise the user. Individual
    # plugins with dirty trees are skipped with a warning rather than
    # aborting the whole update — they're independent checkouts.
    cd "$SCRIPT_DIR"
    local dirty
    dirty=$(git status --porcelain --untracked-files=no || true)
    if [ -n "$dirty" ]; then
        echo "Refusing to update — working tree has uncommitted changes:" >&2
        echo "$dirty" | sed 's/^/  /' >&2
        return 1
    fi
    echo "Pulling latest Gilbert from origin..."
    git pull --ff-only
    echo "Updating std-plugins submodule..."
    git submodule update --init --recursive --remote std-plugins

    # Independent plugin checkouts. local-plugins/ holds user / org
    # plugins; installed-plugins/ holds plugins cloned at runtime
    # from a GitHub URL. Both can be plain git checkouts pinned to
    # their own remotes.
    local any_plugins=false
    for parent in "$SCRIPT_DIR/local-plugins" "$SCRIPT_DIR/installed-plugins"; do
        [ -d "$parent" ] || continue
        for plugin in "$parent"/*/; do
            [ -d "$plugin" ] || continue
            if [ ! "$any_plugins" = "true" ]; then
                echo "Updating plugin checkouts..."
                any_plugins=true
            fi
            pull_latest_plugin "${plugin%/}"
        done
    done
}

# Probe window for the post-switch auto-rollback. If Gilbert exits with
# a crash code within this many seconds of the supervisor launching it
# on a freshly-switched branch, the supervisor reverts to the
# pre-switch ("last known good") branch before retrying. 90s is enough
# for a slow first-boot under uv sync; faster crashes (import errors,
# missing-symbol failures) fire well inside it.
LKG_PROBE_WINDOW=90

_lkg_branch_path() { echo "$SCRIPT_DIR/.gilbert/lkg-branch.txt"; }
_post_switch_marker_path() { echo "$SCRIPT_DIR/.gilbert/post-switch-start.txt"; }
_last_rollback_path() { echo "$SCRIPT_DIR/.gilbert/last-rollback.json"; }

clear_lkg_markers() {
    # Clear the pre-switch "last known good" branch marker and the
    # post-switch boot timestamp. Called after a successful run on
    # the new branch (Gilbert exited cleanly, requested a restart,
    # or ran longer than the probe window before crashing) — at that
    # point the switch is "committed" and we don't roll back on
    # subsequent crashes.
    rm -f "$(_lkg_branch_path)" "$(_post_switch_marker_path)"
}

apply_pending_branch() {
    # Honor a pending branch switch requested via the SourceUpdateService
    # action on the settings page. The service has already validated
    # that the target branch + remote exist and the working tree was
    # clean at the time it wrote the sentinel; here we do the mechanical
    # git work before the next ``uv sync``. On any failure we remove
    # the sentinel and continue on the current branch — the user will
    # see the error in the stderr log and can re-apply via the UI once
    # they've resolved the underlying issue.
    #
    # Sentinel format is two lines:
    #     <target_remote>
    #     <target_branch>
    local sentinel="$SCRIPT_DIR/.gilbert/pending-branch.txt"
    [ -f "$sentinel" ] || return 0

    local target_remote target_branch
    target_remote=$(sed -n '1p' "$sentinel" | tr -d '[:space:]')
    target_branch=$(sed -n '2p' "$sentinel" | tr -d '[:space:]')
    if [ -z "$target_remote" ] || [ -z "$target_branch" ]; then
        echo "Pending branch sentinel malformed (need remote + branch on separate lines) — removing." >&2
        rm -f "$sentinel"
        return 0
    fi

    echo "Applying pending branch switch: $target_remote/$target_branch"

    # Re-check working-tree clean — the user could have touched files
    # between the service action and the supervisor restart.
    local dirty
    dirty=$(git -C "$SCRIPT_DIR" status --porcelain --untracked-files=no || true)
    if [ -n "$dirty" ]; then
        echo "Refusing to switch — working tree no longer clean:" >&2
        echo "$dirty" | sed 's/^/  /' >&2
        rm -f "$sentinel"
        return 0
    fi

    # Capture the pre-switch state as the LKG marker BEFORE any
    # destructive git work, so a fetch or checkout failure leaves us
    # able to recover. Default the tracking remote to "origin" when
    # the current branch isn't tracking anything (newly-initialized
    # repo or branches created from a SHA).
    local current_branch current_remote
    current_branch=$(git -C "$SCRIPT_DIR" symbolic-ref --short HEAD 2>/dev/null || echo "")
    if [ -n "$current_branch" ]; then
        current_remote=$(git -C "$SCRIPT_DIR" config "branch.$current_branch.remote" 2>/dev/null || echo "")
        [ -z "$current_remote" ] && current_remote="origin"
        mkdir -p "$SCRIPT_DIR/.gilbert"
        printf '%s\n%s\n' "$current_remote" "$current_branch" > "$(_lkg_branch_path)"
    fi

    if ! git -C "$SCRIPT_DIR" fetch --quiet "$target_remote" "$target_branch"; then
        echo "Failed to fetch $target_remote/$target_branch — aborting switch." >&2
        rm -f "$sentinel" "$(_lkg_branch_path)"
        return 0
    fi

    # ``switch -C`` force-creates the local branch from the specified
    # remote ref, repointing tracking if the local branch already
    # existed against a different remote. The user explicitly clicked
    # "switch to $target_remote/$target_branch," so repointing matches
    # intent — no surprise. ``--track`` sets upstream so ``git pull``
    # on the new branch follows the same remote.
    if ! git -C "$SCRIPT_DIR" switch -C "$target_branch" --track "$target_remote/$target_branch"; then
        echo "git switch failed — aborting." >&2
        rm -f "$sentinel" "$(_lkg_branch_path)"
        return 0
    fi

    # Submodule SHAs are pinned per branch; update so plugin sources
    # match what the new branch expects.
    git -C "$SCRIPT_DIR" submodule update --init --recursive

    rm -f "$sentinel"
    # Drop the post-switch marker so the supervisor's exit-code handler
    # knows we're inside the probe window and may need to roll back.
    date +%s > "$(_post_switch_marker_path)"
    echo "Switched to $target_remote/$target_branch (HEAD: $(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "?"))"
}

maybe_rollback_to_lkg() {
    # Returns 0 if a rollback fired and the caller should ``continue``
    # the supervisor loop on the rolled-back branch, or 1 if no
    # rollback is applicable. Three preconditions for a rollback:
    #
    #   1. The post-switch probe marker exists (a recent switch happened).
    #   2. The LKG marker exists (we know where to roll back to).
    #   3. The elapsed seconds since the post-switch boot is under
    #      LKG_PROBE_WINDOW (the crash was inside the probe window).
    #
    # On a successful rollback we write ``.gilbert/last-rollback.json``
    # so the SourceUpdateService's ``check`` action can surface what
    # happened in the next session.
    local exit_code=$1
    local elapsed=$2
    local post_switch lkg
    post_switch=$(_post_switch_marker_path)
    lkg=$(_lkg_branch_path)

    [ -f "$post_switch" ] || return 1
    [ -f "$lkg" ] || return 1
    [ "$elapsed" -lt "$LKG_PROBE_WINDOW" ] || return 1

    local lkg_remote lkg_branch
    lkg_remote=$(sed -n '1p' "$lkg" | tr -d '[:space:]')
    lkg_branch=$(sed -n '2p' "$lkg" | tr -d '[:space:]')
    if [ -z "$lkg_remote" ] || [ -z "$lkg_branch" ]; then
        echo "LKG marker malformed — cannot rollback." >&2
        clear_lkg_markers
        return 1
    fi

    local from_branch from_remote
    from_branch=$(git -C "$SCRIPT_DIR" symbolic-ref --short HEAD 2>/dev/null || echo "unknown")
    from_remote=$(git -C "$SCRIPT_DIR" config "branch.$from_branch.remote" 2>/dev/null || echo "")
    [ -z "$from_remote" ] && from_remote="origin"

    echo "Auto-rollback: $from_remote/$from_branch crashed after ${elapsed}s (exit $exit_code), reverting to $lkg_remote/$lkg_branch" >&2

    if ! git -C "$SCRIPT_DIR" fetch --quiet "$lkg_remote" "$lkg_branch"; then
        echo "Rollback fetch failed — manual recovery required (run ./gilbert.sh stop, fix, then ./gilbert.sh start)." >&2
        clear_lkg_markers
        return 1
    fi
    if ! git -C "$SCRIPT_DIR" switch -C "$lkg_branch" --track "$lkg_remote/$lkg_branch"; then
        echo "Rollback checkout failed — manual recovery required." >&2
        clear_lkg_markers
        return 1
    fi
    git -C "$SCRIPT_DIR" submodule update --init --recursive

    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    # Write the rollback record. JSON keys mirror the structure the
    # SourceUpdateService's ``check`` action surfaces. ``cat`` with a
    # heredoc keeps the shell-side templating simple — no escapes
    # needed because remote / branch are constrained to a safe regex
    # by the service before we ever wrote the sentinel.
    cat > "$(_last_rollback_path)" <<JSON
{
  "from_remote": "$from_remote",
  "from_branch": "$from_branch",
  "to_remote": "$lkg_remote",
  "to_branch": "$lkg_branch",
  "exit_code": $exit_code,
  "elapsed_seconds": $elapsed,
  "timestamp": "$ts"
}
JSON

    clear_lkg_markers
    echo "Rolled back to $lkg_remote/$lkg_branch — relaunching."
    return 0
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

        # Handle a pending branch switch BEFORE sync_python_deps so the
        # uv sync picks up the new branch's dependency manifest.
        apply_pending_branch

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
        local launch_start elapsed
        launch_start=$(date +%s)
        set +e
        uv run python -m gilbert 2> >(tee -a "$stderr_log_abs" >&2)
        exit_code=$?
        set -e
        elapsed=$(($(date +%s) - launch_start))
        echo "===== Gilbert exited with code $exit_code at $(date -Iseconds) (ran ${elapsed}s) =====" \
            >> "$stderr_log_abs"

        case "$exit_code" in
            0)
                # Clean exit means the switch (if any) booted at least
                # once successfully — commit it by clearing markers.
                clear_lkg_markers
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
                # If the restart was requested mid-probe-window we
                # still consider the switch "successful enough" —
                # Gilbert booted, took the restart request, and shut
                # down gracefully. Clear LKG so subsequent crashes
                # don't fall back to a state we already promoted past.
                clear_lkg_markers
                crash_count=0
                echo "Gilbert requested a restart — resyncing and relaunching..."
                continue
                ;;
            130)
                clear_lkg_markers
                echo "Gilbert interrupted (Ctrl+C) — not restarting."
                break
                ;;
            143)
                clear_lkg_markers
                echo "Gilbert terminated (SIGTERM) — not restarting."
                break
                ;;
            *)
                # Crash path. First check whether this is a "broken
                # switch" inside the post-switch probe window — if so,
                # roll back to LKG immediately rather than burning
                # MAX_CRASH_RESTARTS attempts on a known-broken branch.
                if maybe_rollback_to_lkg "$exit_code" "$elapsed"; then
                    crash_count=0
                    continue
                fi
                # Ran longer than the probe window? The switch
                # succeeded; this is a normal post-boot crash. Clear
                # the markers and let the existing crash-retry logic
                # handle it.
                if [ -f "$(_post_switch_marker_path)" ] && [ "$elapsed" -ge "$LKG_PROBE_WINDOW" ]; then
                    clear_lkg_markers
                fi
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
    # npm workspaces: install runs from the repo root so frontend AND
    # every plugin's frontend/ directory share a single node_modules
    # tree. Plugin TS files (under std-plugins/<name>/frontend/) can
    # then resolve react / @tanstack/react-query / etc. by walking up
    # to the repo-root node_modules — same way uv hoists Python deps
    # across plugin pyproject.toml workspace members.
    # Reinstall when node_modules is missing OR when the lockfile is
    # newer than the sentinel npm writes inside node_modules on a
    # successful install. The latter catches the case where someone
    # bumps a dep (package.json + package-lock.json change) but
    # node_modules is stale from a prior install — the build would
    # otherwise fail with a missing-package resolve error.
    local lock="$SCRIPT_DIR/package-lock.json"
    local installed_marker="$SCRIPT_DIR/node_modules/.package-lock.json"
    if [ ! -d "$SCRIPT_DIR/node_modules" ] \
       || [ ! -f "$installed_marker" ] \
       || [ "$lock" -nt "$installed_marker" ]; then
        echo "Installing frontend dependencies (npm workspaces)..."
        # If a pre-workspace standalone install exists, blow it away so
        # npm rebuilds the hoisted layout cleanly.
        if [ -d "$SCRIPT_DIR/frontend/node_modules" ] && [ ! -L "$SCRIPT_DIR/frontend/node_modules" ]; then
            rm -rf "$SCRIPT_DIR/frontend/node_modules"
        fi
        cd "$SCRIPT_DIR" && npm install
    fi
    cd "$SCRIPT_DIR/frontend" && npm run build
    rm -rf "$SCRIPT_DIR/src/gilbert/web/spa"
    cp -r "$SCRIPT_DIR/frontend/dist" "$SCRIPT_DIR/src/gilbert/web/spa"
    cd "$SCRIPT_DIR"
}

case "$1" in
    start)
        sync_python_deps
        check_pending_migrations
        build_frontend
        run_gilbert_supervised
        ;;
    dev)
        sync_python_deps
        check_pending_migrations
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
    update)
        # Pull latest from origin (refuses if dirty), update submodules,
        # re-sync Python deps, then run every pending migration. Leaves
        # Gilbert stopped — user re-launches with ``./gilbert.sh start``.
        pull_latest
        sync_python_deps
        run_migrations_unattended
        echo
        echo "Update complete. Run './gilbert.sh start' to launch."
        ;;
    migrate)
        # Forward subcommands to ``gilbert.cli.migrate``:
        #   ./gilbert.sh migrate list    — print pending
        #   ./gilbert.sh migrate status  — applied + pending
        #   ./gilbert.sh migrate up      — apply every pending
        shift
        cd "$SCRIPT_DIR" && uv run python -m gilbert.cli.migrate "$@"
        ;;
    doctor)
        # Iterate every loaded plugin and run its declared runtime
        # dependency checks. The implementation lives in
        # ``gilbert.cli.doctor`` so plugins can declare their own
        # external (non-pip) deps via ``Plugin.runtime_dependencies()``
        # and core stays plugin-agnostic.
        shift
        cd "$SCRIPT_DIR" && uv run python -m gilbert.cli.doctor "$@"
        ;;
    *)
        echo "Usage: gilbert.sh {start|dev|build|stop|update|migrate|doctor}"
        exit 1
        ;;
esac
