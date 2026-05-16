# Source Update Service

## Summary
Admin-only "switch the running Gilbert instance to a different git branch on ``origin``" mechanism. Surfaces in the settings UI as a config param (``target_branch``) plus two action buttons (``check``, ``apply``). The action validates the target, writes a sentinel file (``.gilbert/pending-branch.txt``), and triggers a supervised restart; the ``gilbert.sh`` loop performs the actual ``git checkout`` + submodule update before relaunching, so a broken Python import on the target branch can never wedge the running instance mid-switch.

## Details

### Service
- ``src/gilbert/core/services/source_update.py`` — ``SourceUpdateService``. Implements ``Service`` + ``Configurable`` + ``ConfigActionProvider``.
- Service info: ``name="source_update"``, no capabilities, no requires, ``optional={"configuration"}``, ``toggleable=False``. Not toggleable on purpose — disabling the update mechanism via UI would strand an admin who needs to switch branches to recover from a broken deploy.
- Bound to the host Gilbert app via ``bind_gilbert(self)`` (called in ``Gilbert.start()`` right after registration, same pattern as ``PluginManagerService``). Used to invoke ``Gilbert.request_restart()`` once the sentinel is on disk.
- Config namespace: ``source_update``. Config category: ``System``.
- Config params: ``target_branch`` (string, default ""). Setting the value alone does NOT switch — the user must click ``Apply``.
- Config actions:
  - ``check`` (admin) — read-only. Reports current branch, origin URL, and dirty status. Returns the file list in ``data["dirty_files"]`` and a bool in ``data["dirty"]``.
  - ``apply`` (admin) — destructive. Validates → writes sentinel → calls ``request_restart()``. Has a ``confirm`` prompt so the UI shows a confirmation dialog.
- Validation chain in ``_action_apply``:
  1. ``target_branch`` non-empty.
  2. Branch name matches ``_BRANCH_RE`` (``^[A-Za-z0-9._][A-Za-z0-9._/\-]{0,254}$``) — rejects shell-injection patterns like ``feature/foo; rm -rf /`` or ``--upload-pack=evil`` so the supervisor's shell handling stays safe.
  3. Current branch differs from target (no-op early-return if same).
  4. Working tree clean (matches ``pull_latest`` in ``gilbert.sh`` — uses ``git status --porcelain --untracked-files=no``).
  5. ``git fetch origin`` succeeds.
  6. ``git ls-remote --heads origin <branch>`` returns a matching ref.
- Audit log: ``logging.getLogger("gilbert.source_update.audit")`` records every ``branch_switch_requested`` event with ``user_id`` (from the request contextvar), ``from_branch``, ``to_branch``, and ISO timestamp.
- All git subprocess calls go through ``_git(*args)`` which raises ``_GitError`` on non-zero exit; the action methods catch ``_GitError`` and surface the stderr in the ``ConfigActionResult.message``.

### Sentinel file
- Path: ``.gilbert/pending-branch.txt``. Single line — the target branch name, nothing else. Format chosen so the shell side can ``cat`` it directly without a JSON parser.
- Written by the service on a successful ``apply`` action; read and consumed by ``gilbert.sh`` before the next launch.

### Supervisor handler
- ``apply_pending_branch()`` in ``gilbert.sh`` — runs inside ``run_gilbert_supervised``'s while loop, **before** ``sync_python_deps``, so ``uv sync`` picks up the new branch's dependency manifest.
- Re-checks the working tree is clean (the user could have touched files between the action and the restart), fetches the target, runs ``git checkout``, fast-forwards to ``origin/<target>`` (``--ff-only``, no implicit merges), runs ``git submodule update --init --recursive``, and removes the sentinel.
- On any failure (fetch error, checkout conflict, etc.) the sentinel is removed and the loop continues on the current branch. The user sees the error in ``.gilbert/stderr.log`` and can re-apply via the UI once they've fixed the underlying issue. No auto-rollback to a "last known good" branch is implemented.

### Security posture
- All settings + actions are admin-only.
- Strict to ``origin`` — the action never reads a URL or arbitrary remote name from the user. Switching means "deploy a branch from whichever remote is named ``origin`` locally."
- Branch name regex prevents shell injection. The supervisor side passes the branch name through ``git -C "$SCRIPT_DIR" checkout "$target"`` with explicit quoting; the service-side check is belt-and-suspenders.
- Confirmation prompt on the ``apply`` action so an errant double-click can't restart mid-conversation.
- The mechanism is, by design, "any admin can run code on the server by pushing to ``origin`` and clicking Apply." Treat ``origin`` write access as production deploy access.

### Tests
- ``tests/unit/test_source_update.py`` — 20 tests covering both actions, the dirty-tree refusal, branch-existence check, shell-injection rejection, sentinel write, ``request_restart`` invocation, no-op when already on target, and the regex pattern + ``_discover_repo_root`` helper.

## Related
- ``gilbert.sh:apply_pending_branch`` — supervisor-side sentinel consumer.
- ``src/gilbert/core/app.py:request_restart`` — graceful exit hook the service triggers.
- ``src/gilbert/core/services/plugin_manager.py:bind_gilbert`` — the pattern used to give a service a reference to the host app.
- ``src/gilbert/interfaces/configuration.py`` — ``ConfigAction`` / ``ConfigActionResult`` shapes consumed by the UI.
