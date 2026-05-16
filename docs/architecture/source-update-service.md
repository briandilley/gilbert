# Source Update Service

## Summary
Admin-only mechanism to switch the running Gilbert instance to a different git branch on any locally-configured remote. Surfaces in the settings UI as two dropdowns (``target_remote`` / ``target_branch``) plus three action buttons (``check``, ``refresh_branches``, ``apply``). The action validates the target, writes a two-line sentinel file (``.gilbert/pending-branch.txt``), and triggers a supervised restart; ``gilbert.sh`` performs the actual ``git switch`` + submodule update before relaunching, so a broken Python import on the target branch can't wedge the running instance mid-switch. If the new branch crashes within a 90s probe window, the supervisor auto-rolls back to the last-known-good branch.

## Details

### Service
- ``src/gilbert/core/services/source_update.py`` — ``SourceUpdateService``. Implements ``Service`` + ``Configurable`` + ``ConfigActionProvider``.
- Service info: ``name="source_update"``, ``capabilities={"source_update"}``, no requires, ``optional={"configuration"}``, ``toggleable=False``. Not toggleable on purpose — disabling the update mechanism via UI would strand an admin who needs to switch branches to recover from a broken deploy.
- Bound to the host Gilbert app via ``bind_gilbert(self)`` (called in ``Gilbert.start()`` right after registration, same pattern as ``PluginManagerService``). Used to invoke ``Gilbert.request_restart()`` once the sentinel is on disk.
- Config namespace: ``source_update``. Config category: ``System``.
- Config params:
  - ``target_remote`` (string, default ``"origin"``, ``choices_from="git_remotes"``) — which locally-configured git remote to switch to.
  - ``target_branch`` (string, default ``""``, ``choices_from="target_remote_branches"``) — branch on the selected remote. Dropdown reflects the *currently saved* ``target_remote``, so changing remote then saving exposes the new remote's branches without a fresh ``refresh_branches`` call (the caches are kept per-remote).
- Config actions:
  - ``check`` (admin) — read-only. Reports current branch + tracking remote, target settings, dirty-tree status, last-rollback (if any). Returns ``data["dirty_files"]`` (list), ``data["dirty"]`` (bool), ``data["current_remote"]``, ``data["target_remote"]``, ``data["last_rollback"]``.
  - ``refresh_branches`` (admin) — read-only. Re-runs ``git remote`` to populate the remote list, then ``git fetch`` + ``git ls-remote --heads`` against each remote to rebuild the branch cache. Partial failures (one remote unreachable) preserve the prior cache for that remote and surface as ``status="pending"`` with the failure messages.
  - ``apply`` (admin) — destructive. Validates → writes sentinel → calls ``request_restart()``. Has a ``confirm`` prompt so the UI shows a confirmation dialog.
- Validation chain in ``_action_apply``:
  1. ``target_branch`` non-empty.
  2. Branch name matches ``_BRANCH_RE`` (``^[A-Za-z0-9._][A-Za-z0-9._/\-]{0,254}$``) — rejects shell-injection patterns.
  3. Remote name matches ``_REMOTE_RE`` (``^[A-Za-z0-9._][A-Za-z0-9._\-]{0,63}$``) — tighter than the branch regex (no slashes; remote names are flat).
  4. Configured remote exists locally (``git remote get-url <name>`` succeeds).
  5. ``(current_branch, current_remote)`` differs from ``(target_branch, target_remote)`` (no-op early-return if both match — same branch name on a different remote IS a real switch because tracking gets repointed).
  6. Working tree clean (``git status --porcelain --untracked-files=no``).
  7. ``git fetch <target_remote>`` succeeds.
  8. ``git ls-remote --heads <target_remote> <target_branch>`` returns a matching ref.
- Audit log: ``logging.getLogger("gilbert.source_update.audit")`` records every ``branch_switch_requested`` event with ``user_id`` (from the request contextvar), ``from_remote``/``from_branch``, ``to_remote``/``to_branch``, and ISO timestamp.
- All git subprocess calls go through ``_git(*args)`` which raises ``_GitError`` on non-zero exit.

### Caches (dropdown sources)
- ``SourceUpdateService.cached_remotes: list[str]`` — alphabetical local remote names. Satisfies ``GitRemoteLister`` (``src/gilbert/interfaces/source_update.py``), wired to ``choices_from="git_remotes"``.
- ``SourceUpdateService.cached_target_remote_branches: list[str]`` — branches on the *currently configured* ``target_remote``. Satisfies ``RemoteBranchLister``, wired to ``choices_from="target_remote_branches"``.
- Internal storage: ``_cached_branches_by_remote: dict[str, list[str]]`` — keyed by remote so the dropdown can swap instantly when the user changes target_remote (only need a fresh ``refresh_branches`` to pick up newly-pushed branches on a remote, not to switch which remote you're looking at).
- Populated best-effort in ``start()``: ``git remote`` to list, then ``git ls-remote --heads <remote>`` per remote. A failure on one remote logs a warning and leaves that remote's cache empty without aborting startup.
- ``refresh_branches`` action rebuilds the cache from scratch (drops entries for removed remotes), but preserves the prior cache for a remote whose fetch fails this round.
- Parser ignores non-``refs/heads/`` refs (tags, HEAD), malformed lines, and dedupes via ``dict.fromkeys`` before sorting alphabetically.

### Sentinel file format
- Path: ``.gilbert/pending-branch.txt``. **Two lines:**
  ```
  <target_remote>
  <target_branch>
  ```
- No JSON envelope — the supervisor side parses with ``sed -n '1p'`` / ``sed -n '2p'`` and ``tr -d '[:space:]'``. Whitespace is stripped on the read side.
- Written by the service on a successful ``apply``; consumed and deleted by ``gilbert.sh`` before the next launch.

### Supervisor: apply_pending_branch
- ``apply_pending_branch()`` in ``gilbert.sh`` — runs inside ``run_gilbert_supervised``'s while loop, **before** ``sync_python_deps``, so ``uv sync`` picks up the new branch's dependency manifest.
- Steps:
  1. Parse the two-line sentinel; bail if either line is empty.
  2. Re-check working tree clean (user could have touched files between action and restart).
  3. **Write the LKG marker** (``.gilbert/lkg-branch.txt`` — same two-line format as the sentinel) with the current branch's name + its tracking remote (or ``origin`` if no tracking remote is set). This MUST happen before any destructive git work so a fetch/checkout failure leaves us able to recover.
  4. ``git fetch --quiet <target_remote> <target_branch>``.
  5. ``git switch -C <target_branch> --track <target_remote>/<target_branch>`` — force-create / repoint the local branch from the remote ref. ``-C`` makes the switch idempotent across re-runs; ``--track`` sets upstream so ``git pull`` follows the same remote afterwards.
  6. ``git submodule update --init --recursive``.
  7. Delete the sentinel. Write ``.gilbert/post-switch-start.txt`` (single line: ``date +%s``) so the supervisor's exit-code handler knows we're in the probe window.
- On any failure (fetch, checkout, etc.): remove both the sentinel and the LKG marker, continue on the current branch. User sees the error in ``.gilbert/stderr.log`` and re-applies via the UI.

### Supervisor: auto-rollback (LKG)
- Constant: ``LKG_PROBE_WINDOW=90`` seconds in ``gilbert.sh``. Window starts when Gilbert is launched after a switch and ends when it exits or runs longer than the window.
- Marker files written by ``apply_pending_branch``:
  - ``.gilbert/lkg-branch.txt`` — pre-switch ``<remote>\n<branch>\n``. The "where to roll back to."
  - ``.gilbert/post-switch-start.txt`` — Unix epoch of the post-switch launch. The probe-window anchor.
- ``maybe_rollback_to_lkg(exit_code, elapsed)`` in ``gilbert.sh`` — returns 0 (rollback fired) or 1 (no rollback applicable). Preconditions: both marker files present, ``elapsed < LKG_PROBE_WINDOW``. Steps: fetch + ``git switch -C`` to the LKG ref, update submodules, write ``.gilbert/last-rollback.json``, clear LKG markers.
- ``.gilbert/last-rollback.json`` — record of the most recent rollback. Schema:
  ```json
  {
    "from_remote": "...", "from_branch": "...",
    "to_remote": "...", "to_branch": "...",
    "exit_code": 1, "elapsed_seconds": 12,
    "timestamp": "2026-05-17T14:23:11Z"
  }
  ```
  Read by ``SourceUpdateService._read_last_rollback()`` (returns ``None`` on absence / malformed JSON) and surfaced in the ``check`` action's ``data["last_rollback"]`` and ``message``. Not auto-cleared — overwritten on the next rollback.
- Supervisor loop integration: on a crash exit code, call ``maybe_rollback_to_lkg`` first; if it fires, reset ``crash_count`` and ``continue``. If we ran past the probe window without rollback, ``clear_lkg_markers`` so subsequent crashes are treated as normal post-boot crashes (existing ``MAX_CRASH_RESTARTS=3`` flow). On clean exit / restart-requested / SIGINT / SIGTERM, also clear markers — the switch is committed.
- No auto-rollback fires on the rolled-back branch crashing too. At that point, ``MAX_CRASH_RESTARTS`` catches a loop and exits; admin intervenes manually.

### Security posture
- All settings + actions are admin-only (``required_role="admin"``).
- Multi-remote, but no arbitrary URL acceptance — the user can only target remotes that are already configured locally via ``git remote add``. The action validates this via ``git remote get-url <name>``.
- Branch and remote names go through regex whitelists before reaching the shell. The supervisor side passes them through ``git -C`` with explicit quoting; the service-side check is belt-and-suspenders.
- Confirmation prompt on the ``apply`` action.
- The mechanism is, by design, "any admin can run code on the server by pushing to a configured remote and clicking Apply." Treat write access to any of your configured remotes as production deploy access.

### Tests
- ``tests/unit/test_source_update.py`` — covers all three actions, multi-remote application (origin → upstream/feature/x, same-branch-different-remote = real switch), unknown-remote rejection, shell-injection in either remote OR branch name, dirty-tree refusal, branch-existence check, sentinel two-line format, ``request_restart`` invocation, last-rollback JSON surfacing in ``check`` (including malformed file → silently ignored), branch + remote regexes, refresh partial-failure preserving prior cache, dedup + garbage-line parser resilience, both ``RemoteBranchLister`` and ``GitRemoteLister`` protocol satisfaction, ``_discover_repo_root`` walk-up.

## Related
- ``gilbert.sh:apply_pending_branch``, ``gilbert.sh:maybe_rollback_to_lkg``, ``gilbert.sh:clear_lkg_markers`` — supervisor-side sentinel + LKG handling.
- ``src/gilbert/core/app.py:request_restart`` — graceful exit hook the service triggers.
- ``src/gilbert/core/services/plugin_manager.py:bind_gilbert`` — the pattern used to give a service a reference to the host app.
- ``src/gilbert/interfaces/configuration.py`` — ``ConfigAction`` / ``ConfigActionResult`` shapes consumed by the UI.
- ``src/gilbert/core/services/configuration.py`` — ``_resolve_dynamic_choices`` knows the ``git_remotes`` and ``target_remote_branches`` source names.
