"""Source-update service — switch a running Gilbert instance to a different git branch.

Lets an admin point Gilbert at any branch on any locally-configured
remote via the settings UI. The action validates the target, writes a
sentinel file (``.gilbert/pending-branch.txt`` — two lines: remote then
branch), and triggers a supervised restart. ``gilbert.sh``'s supervisor
loop performs the actual ``git checkout`` + submodule update before
relaunching, so a broken Python import on the target branch can never
wedge the running instance mid-switch.

The supervisor also captures the pre-switch branch as a "last known
good" marker. If Gilbert crashes within a 90s probe window of the
post-switch boot, the supervisor rolls back to the LKG branch
automatically and writes ``.gilbert/last-rollback.json``. This service
surfaces that file's contents in the ``check`` action so the admin sees
what happened and can investigate.

This is a deploy-from-the-admin-UI mechanism — admin-only by design.
Anyone with write access to a configured remote can land code that
this lets them run on the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gilbert.interfaces.context import get_current_user
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("gilbert.source_update.audit")

# Sentinel + supervisor coordination files. All paths relative to the
# repo root, matching the rest of ``.gilbert/`` conventions.
_SENTINEL_PATH = Path(".gilbert/pending-branch.txt")
_LAST_ROLLBACK_PATH = Path(".gilbert/last-rollback.json")

# Whitelist for branch names — git accepts more, but anything outside
# ``[A-Za-z0-9_./-]`` invites shell-injection trouble in the supervisor.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/\-]{0,254}$")
# Remote names are tighter than branch names — git enforces no slashes
# in remote names (those are reserved for ``<remote>/<branch>`` refs).
_REMOTE_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\-]{0,63}$")

# Default remote name when no tracking remote can be inferred.
_DEFAULT_REMOTE = "origin"


class SourceUpdateService(Service):
    """Switch the running Gilbert instance to a different git branch.

    Admin-only. Exposes two config params (``target_remote``,
    ``target_branch``) and three actions on the settings page:
    ``check`` reports current branch + tracking remote + dirty status +
    last-rollback info; ``refresh_branches`` repopulates the
    remote/branch caches; ``apply`` validates the target, writes the
    sentinel, and calls ``Gilbert.request_restart()``.
    """

    def __init__(self) -> None:
        self._target_remote: str = _DEFAULT_REMOTE
        self._target_branch: str = ""
        self._repo_root: Path = Path.cwd()
        self._gilbert: Any = None
        # Last-known list of local git remote names (sorted).
        self._cached_remotes: list[str] = []
        # Per-remote branch caches — keyed by remote name. Populated on
        # service start and refreshed via the ``refresh_branches``
        # action. Read-only consumers go through the protocol
        # properties so the in-memory dict stays encapsulated.
        self._cached_branches_by_remote: dict[str, list[str]] = {}

    # --- Service ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="source_update",
            # Advertises ``source_update`` so the ConfigurationService's
            # dynamic-choices resolver can find this instance for the
            # ``git_remotes`` and ``target_remote_branches`` dropdowns.
            capabilities=frozenset({"source_update"}),
            requires=frozenset(),
            optional=frozenset({"configuration"}),
            # Not toggleable — disabling the update mechanism via UI
            # could leave an admin without recovery if Gilbert hangs
            # on the wrong branch and they can't SSH in.
            toggleable=False,
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._repo_root = _discover_repo_root()
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                self._target_branch = str(section.get("target_branch", "") or "")
                self._target_remote = (
                    str(section.get("target_remote", "") or "").strip()
                    or _DEFAULT_REMOTE
                )

        # Best-effort cache population. ``git remote`` failure leaves
        # the dropdowns empty until the user clicks Refresh; the
        # supervisor itself doesn't depend on this cache to do its job.
        try:
            self._cached_remotes = await self._fetch_remotes()
        except _GitError as exc:
            logger.warning(
                "Source-update service: could not list remotes (%s); "
                "use Refresh branches once the issue is resolved",
                exc,
            )
        for remote in self._cached_remotes:
            try:
                self._cached_branches_by_remote[remote] = (
                    await self._fetch_remote_branches(remote)
                )
            except _GitError as exc:
                logger.warning(
                    "Source-update service: branch cache for %r empty: %s",
                    remote,
                    exc,
                )

        logger.info(
            "Source-update service started — repo=%s target=%s/%s "
            "remotes=%d branches=%d",
            self._repo_root,
            self._target_remote,
            self._target_branch or "<unset>",
            len(self._cached_remotes),
            sum(len(v) for v in self._cached_branches_by_remote.values()),
        )

    async def stop(self) -> None:
        return

    def bind_gilbert(self, gilbert: Any) -> None:
        """Receive the host Gilbert app so ``apply`` can call ``request_restart()``.

        Called from ``Gilbert.start()`` after service registration,
        same pattern as ``PluginManagerService``.
        """
        self._gilbert = gilbert

    # --- GitRemoteLister + RemoteBranchLister capabilities ---

    @property
    def cached_remotes(self) -> list[str]:
        """Last-known local git remote names (alphabetical, deduplicated)."""
        return list(self._cached_remotes)

    @property
    def cached_target_remote_branches(self) -> list[str]:
        """Branches on the user's configured ``target_remote``."""
        return list(self._cached_branches_by_remote.get(self._target_remote, []))

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "source_update"

    @property
    def config_category(self) -> str:
        return "System"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="target_remote",
                type=ToolParameterType.STRING,
                description=(
                    "Git remote whose branches the ``Apply`` action "
                    "switches to. Pick from the locally-configured "
                    "remotes (``git remote``). Use ``Refresh branches`` "
                    "after running ``git remote add`` to repopulate "
                    "the dropdown."
                ),
                default=_DEFAULT_REMOTE,
                choices_from="git_remotes",
            ),
            ConfigParam(
                key="target_branch",
                type=ToolParameterType.STRING,
                description=(
                    "Branch on ``target_remote`` that the ``Apply`` "
                    "action will switch to. Selectable from the list "
                    "of branches the service knows about — use "
                    "``Refresh branches`` to repopulate after pushing "
                    "a new branch. Leave empty to do nothing. Setting "
                    "this value does **not** switch on its own — you "
                    "must click Apply."
                ),
                default="",
                choices_from="target_remote_branches",
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._target_branch = str(config.get("target_branch", "") or "")
        self._target_remote = (
            str(config.get("target_remote", "") or "").strip() or _DEFAULT_REMOTE
        )

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="check",
                label="Check status",
                description=(
                    "Show current branch + its tracking remote, the "
                    "configured target, whether the working tree is "
                    "clean, and the last auto-rollback (if any). "
                    "Does not modify any files."
                ),
                required_role="admin",
            ),
            ConfigAction(
                key="refresh_branches",
                label="Refresh remotes & branches",
                description=(
                    "Re-run ``git remote`` to repopulate the remote "
                    "list, then ``git fetch`` + ``git ls-remote --heads`` "
                    "against each remote to refresh the branch dropdown. "
                    "Use after pushing a new branch or adding a remote "
                    "if you don't see it in the picker."
                ),
                required_role="admin",
            ),
            ConfigAction(
                key="apply",
                label="Apply branch switch",
                description=(
                    "Validate ``target_remote``/``target_branch``, "
                    "refuse if the working tree is dirty, then restart "
                    "Gilbert. The supervisor loop runs ``git checkout`` "
                    "and updates submodules before relaunching. If the "
                    "new branch crashes within 90 seconds of boot the "
                    "supervisor automatically rolls back to the previous "
                    "branch."
                ),
                confirm=(
                    "This will restart Gilbert and switch to the configured "
                    "target branch. Continue?"
                ),
                required_role="admin",
            ),
        ]

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "check":
            return await self._action_check()
        if key == "refresh_branches":
            return await self._action_refresh_branches()
        if key == "apply":
            return await self._action_apply()
        return ConfigActionResult(
            status="error",
            message=f"Unknown source-update action: {key!r}",
        )

    # --- Action implementations ---

    async def _action_refresh_branches(self) -> ConfigActionResult:
        try:
            self._cached_remotes = await self._fetch_remotes()
        except _GitError as exc:
            return ConfigActionResult(status="error", message=str(exc))
        # Rebuild the per-remote branch caches from scratch — a remote
        # that's been removed shouldn't keep stale branches in the map.
        new_cache: dict[str, list[str]] = {}
        partial_failures: list[str] = []
        for remote in self._cached_remotes:
            try:
                await self._git("fetch", "--quiet", remote)
                new_cache[remote] = await self._fetch_remote_branches(remote)
            except _GitError as exc:
                partial_failures.append(f"{remote}: {exc}")
                # Preserve the previous cache for this remote rather
                # than blanking it — a transient network failure
                # shouldn't wipe a working dropdown.
                new_cache[remote] = self._cached_branches_by_remote.get(remote, [])
        self._cached_branches_by_remote = new_cache

        total_branches = sum(len(v) for v in new_cache.values())
        message = (
            f"Found {len(self._cached_remotes)} remote(s) and {total_branches} "
            "branch(es) total. The dropdowns are now up to date."
        )
        if partial_failures:
            message += "\nPartial failures:\n  " + "\n  ".join(partial_failures)
        return ConfigActionResult(
            status="ok" if not partial_failures else "pending",
            message=message,
            data={
                "remotes": list(self._cached_remotes),
                "branches_by_remote": {
                    r: list(v) for r, v in new_cache.items()
                },
                "partial_failures": partial_failures,
            },
        )

    async def _action_check(self) -> ConfigActionResult:
        try:
            current = await self._current_branch()
            current_remote = await self._tracking_remote(current)
            dirty_files = await self._dirty_files()
        except _GitError as exc:
            return ConfigActionResult(status="error", message=str(exc))

        rollback = self._read_last_rollback()
        parts = [
            f"On {current!r} (tracking: {current_remote});",
            "working tree dirty." if dirty_files else "working tree clean.",
        ]
        if rollback is not None:
            parts.append(
                "\nLast auto-rollback: "
                f"{rollback.get('from_remote', '?')}/{rollback.get('from_branch', '?')} "
                f"→ {rollback.get('to_remote', '?')}/{rollback.get('to_branch', '?')} "
                f"at {rollback.get('timestamp', '?')} "
                f"(exit {rollback.get('exit_code', '?')} after "
                f"{rollback.get('elapsed_seconds', '?')}s)."
            )
        return ConfigActionResult(
            status="ok",
            message=" ".join(parts),
            data={
                "current_branch": current,
                "current_remote": current_remote,
                "target_remote": self._target_remote,
                "target_branch": self._target_branch,
                "dirty": bool(dirty_files),
                "dirty_files": dirty_files,
                "last_rollback": rollback,
            },
        )

    async def _action_apply(self) -> ConfigActionResult:
        target_branch = self._target_branch.strip()
        target_remote = self._target_remote.strip() or _DEFAULT_REMOTE

        if not target_branch:
            return ConfigActionResult(
                status="error",
                message=(
                    "``target_branch`` is empty — pick a branch from "
                    "the dropdown (or run Refresh branches if it's "
                    "empty) before clicking Apply."
                ),
            )
        if not _BRANCH_RE.match(target_branch):
            return ConfigActionResult(
                status="error",
                message=(
                    f"Branch name {target_branch!r} contains characters "
                    "that could be shell-interpreted — refusing."
                ),
            )
        if not _REMOTE_RE.match(target_remote):
            return ConfigActionResult(
                status="error",
                message=(
                    f"Remote name {target_remote!r} contains characters "
                    "that could be shell-interpreted — refusing."
                ),
            )

        try:
            if not await self._remote_exists(target_remote):
                return ConfigActionResult(
                    status="error",
                    message=(
                        f"Remote {target_remote!r} is not configured "
                        "locally. Run ``git remote add`` on the host "
                        "and click Refresh branches."
                    ),
                )
            current = await self._current_branch()
            current_remote = await self._tracking_remote(current)
            dirty = await self._dirty_files()
            if dirty:
                return ConfigActionResult(
                    status="error",
                    message=(
                        "Working tree has uncommitted changes — "
                        "refusing to switch. Commit / stash / discard "
                        "them and try again. Modified:\n  "
                        + "\n  ".join(dirty)
                    ),
                )
            # Same-remote same-branch is a no-op. A different remote on
            # the same branch name IS a real switch (tracking gets
            # repointed), so we proceed in that case.
            if (
                target_branch == current
                and target_remote == current_remote
            ):
                return ConfigActionResult(
                    status="ok",
                    message=(
                        f"Already on {target_remote}/{target_branch} — "
                        "nothing to do."
                    ),
                )
            await self._git("fetch", "--quiet", target_remote)
            if not await self._branch_exists_on_remote(target_remote, target_branch):
                return ConfigActionResult(
                    status="error",
                    message=(
                        f"Branch {target_branch!r} does not exist on "
                        f"remote {target_remote!r} (checked via "
                        "``git ls-remote --heads`` after fetch)."
                    ),
                )
        except _GitError as exc:
            return ConfigActionResult(status="error", message=str(exc))

        # All checks passed — write the two-line sentinel and request
        # restart. Format is exactly:
        #
        #   <remote>\n
        #   <branch>\n
        #
        # No JSON envelope — the supervisor side parses with ``sed``,
        # which doesn't need a JSON parser. Whitespace is stripped on
        # the read side.
        sentinel = self._repo_root / _SENTINEL_PATH
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            f"{target_remote}\n{target_branch}\n", encoding="utf-8"
        )

        user_id = get_current_user().user_id or "unknown"
        audit_logger.info(
            "branch_switch_requested",
            extra={
                "user_id": user_id,
                "from_remote": current_remote,
                "from_branch": current,
                "to_remote": target_remote,
                "to_branch": target_branch,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

        if self._gilbert is None:
            logger.warning(
                "Branch sentinel written but SourceUpdateService is not "
                "bound to a Gilbert app; not requesting restart"
            )
            return ConfigActionResult(
                status="error",
                message=(
                    "Sentinel written but the service is not bound to "
                    "the running app — restart Gilbert manually with "
                    "``./gilbert.sh stop && ./gilbert.sh start`` to "
                    "apply the branch switch."
                ),
            )

        self._gilbert.request_restart()
        return ConfigActionResult(
            status="ok",
            message=(
                f"Branch switch to {target_remote}/{target_branch} "
                "queued. Gilbert is shutting down; the supervisor will "
                "run ``git checkout`` and relaunch automatically. If "
                "the new branch fails to boot within 90s, it'll be "
                "auto-rolled back to "
                f"{current_remote}/{current}."
            ),
            data={
                "from_remote": current_remote,
                "from_branch": current,
                "to_remote": target_remote,
                "to_branch": target_branch,
            },
        )

    # --- Last-rollback surfacing ---

    def _read_last_rollback(self) -> dict[str, Any] | None:
        """Read ``.gilbert/last-rollback.json`` if present.

        The supervisor writes this file when its auto-rollback fires.
        We surface its contents in the ``check`` action so the admin
        can see what happened without grepping logs. Returns ``None``
        when no rollback has occurred (or the file is malformed —
        better to omit than to render garbage).
        """
        path = self._repo_root / _LAST_ROLLBACK_PATH
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            logger.debug("last-rollback.json present but unreadable", exc_info=True)
        return None

    # --- Git helpers ---

    async def _current_branch(self) -> str:
        return (await self._git("symbolic-ref", "--short", "HEAD")).strip()

    async def _tracking_remote(self, branch: str) -> str:
        """Resolve the remote a local branch tracks, falling back to ``origin``.

        ``git config branch.<name>.remote`` returns empty / errors if
        the branch isn't tracking — that's normal for branches created
        from a SHA or for newly-initialized repos. We default to
        ``origin`` rather than failing because the dropdown / status
        line should always have *something* sensible to show.
        """
        try:
            out = await self._git("config", f"branch.{branch}.remote")
        except _GitError:
            return _DEFAULT_REMOTE
        return out.strip() or _DEFAULT_REMOTE

    async def _dirty_files(self) -> list[str]:
        # ``--untracked-files=no`` matches ``pull_latest`` in gilbert.sh —
        # untracked build artefacts (frontend caches, etc.) don't count
        # as "dirty" for the purposes of refusing a switch.
        out = await self._git("status", "--porcelain", "--untracked-files=no")
        return [line for line in out.splitlines() if line.strip()]

    async def _remote_exists(self, remote: str) -> bool:
        try:
            await self._git("remote", "get-url", remote)
            return True
        except _GitError:
            return False

    async def _branch_exists_on_remote(self, remote: str, branch: str) -> bool:
        out = await self._git("ls-remote", "--heads", remote, branch)
        return f"refs/heads/{branch}" in out

    async def _fetch_remotes(self) -> list[str]:
        out = await self._git("remote")
        names = {line.strip() for line in out.splitlines() if line.strip()}
        return sorted(names)

    async def _fetch_remote_branches(self, remote: str) -> list[str]:
        """List branches on ``remote`` via a single ``ls-remote --heads``.

        Output format is one line per ref:
            <sha><TAB>refs/heads/<branch-name>
        We strip the prefix, ignore non-heads refs and malformed lines,
        and return the names sorted alphabetically. Dedupe defensively
        in case a remote-helper emits something odd.
        """
        out = await self._git("ls-remote", "--heads", remote)
        names: list[str] = []
        for line in out.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
                continue
            names.append(parts[1][len("refs/heads/") :])
        return sorted(dict.fromkeys(names))

    async def _git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self._repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise _GitError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): "
                + (stderr.decode("utf-8", errors="replace").strip() or "no stderr")
            )
        return stdout.decode("utf-8", errors="replace")


class _GitError(RuntimeError):
    """Raised when a git subprocess invocation fails."""


def _discover_repo_root() -> Path:
    """Walk up from the current working directory until we find ``.git``.

    Matches how ``gilbert.sh`` resolves ``SCRIPT_DIR`` — both should
    land at the same repo root when Gilbert is launched via the
    supervisor.
    """
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current
