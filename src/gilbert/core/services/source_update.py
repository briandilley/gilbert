"""Source-update service — switch a running Gilbert instance to a different git branch.

Lets an admin point Gilbert at any branch on the configured ``origin``
remote via the settings UI. The action validates the branch exists,
refuses if the working tree is dirty, writes a sentinel file
(``.gilbert/pending-branch.txt``), and triggers a supervised restart.
``gilbert.sh``'s supervisor loop reads the sentinel before re-launching:
fetch + checkout + submodule update happen there, so a broken Python
import on the target branch can never wedge the running instance
mid-switch.

This is a deploy-from-the-admin-UI mechanism — admin-only by design.
Anyone with write access to ``origin`` can land code that this lets
them run on the server.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("gilbert.source_update.audit")

# Sentinel file the supervisor reads before re-launching Gilbert.
# Path is relative to the repo root, same convention as the rest of
# ``.gilbert/`` artefacts. The file contains only the target branch
# name; no JSON envelope, so the shell side can ``cat`` it directly.
_SENTINEL_PATH = Path(".gilbert/pending-branch.txt")
# Whitelist for branch names — git accepts more, but anything outside
# ``[A-Za-z0-9_./-]`` invites shell-injection trouble in the supervisor.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/\-]{0,254}$")


class SourceUpdateService(Service):
    """Switch the running Gilbert instance to a different git branch.

    Admin-only. Exposes one config param (``target_branch``) and two
    actions on the settings page: ``check`` reports the current branch,
    remote, and dirty status; ``apply`` validates the target, writes
    the sentinel, and calls ``Gilbert.request_restart()``.
    """

    def __init__(self) -> None:
        self._target_branch: str = ""
        self._repo_root: Path = Path.cwd()
        self._gilbert: Any = None

    # --- Service ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="source_update",
            capabilities=frozenset(),
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
        logger.info(
            "Source-update service started — repo=%s target_branch=%r",
            self._repo_root,
            self._target_branch,
        )

    async def stop(self) -> None:
        return

    def bind_gilbert(self, gilbert: Any) -> None:
        """Receive the host Gilbert app so ``apply`` can call ``request_restart()``.

        Called from ``Gilbert.start()`` after service registration,
        same pattern as ``PluginManagerService``.
        """
        self._gilbert = gilbert

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
                key="target_branch",
                type=ToolParameterType.STRING,
                description=(
                    "Branch on ``origin`` that the ``Apply`` action will "
                    "switch to. Leave empty to do nothing. Setting this "
                    "value does **not** switch on its own — you must "
                    "click Apply."
                ),
                default="",
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._target_branch = str(config.get("target_branch", "") or "")

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="check",
                label="Check status",
                description=(
                    "Show current branch, the configured ``origin`` "
                    "remote, and whether the working tree is clean. "
                    "Does not modify any files."
                ),
                required_role="admin",
            ),
            ConfigAction(
                key="apply",
                label="Apply branch switch",
                description=(
                    "Validate ``target_branch`` exists on ``origin``, "
                    "refuse if the working tree is dirty, then restart "
                    "Gilbert. The supervisor loop runs ``git checkout`` "
                    "and updates submodules before relaunching."
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
        if key == "apply":
            return await self._action_apply()
        return ConfigActionResult(
            status="error",
            message=f"Unknown source-update action: {key!r}",
        )

    # --- Action implementations ---

    async def _action_check(self) -> ConfigActionResult:
        try:
            current = await self._current_branch()
            origin_url = await self._origin_url()
            dirty_files = await self._dirty_files()
        except _GitError as exc:
            return ConfigActionResult(status="error", message=str(exc))
        return ConfigActionResult(
            status="ok",
            message=(
                f"On {current!r} (origin: {origin_url or 'unset'}); "
                + ("working tree dirty." if dirty_files else "working tree clean.")
            ),
            data={
                "current_branch": current,
                "origin_url": origin_url,
                "target_branch": self._target_branch,
                "dirty": bool(dirty_files),
                "dirty_files": dirty_files,
            },
        )

    async def _action_apply(self) -> ConfigActionResult:
        branch = self._target_branch.strip()
        if not branch:
            return ConfigActionResult(
                status="error",
                message=(
                    "``target_branch`` is empty — set a branch name in "
                    "the settings field before clicking Apply."
                ),
            )
        if not _BRANCH_RE.match(branch):
            return ConfigActionResult(
                status="error",
                message=(
                    f"Branch name {branch!r} contains characters that "
                    "could be shell-interpreted — refusing."
                ),
            )

        try:
            current = await self._current_branch()
            dirty = await self._dirty_files()
            if dirty:
                return ConfigActionResult(
                    status="error",
                    message=(
                        "Working tree has uncommitted changes — refusing "
                        "to switch. Commit / stash / discard them and "
                        "try again. Modified:\n  " + "\n  ".join(dirty)
                    ),
                )
            if branch == current:
                return ConfigActionResult(
                    status="ok",
                    message=f"Already on branch {branch!r} — nothing to do.",
                )
            await self._fetch_origin()
            if not await self._branch_exists_on_origin(branch):
                return ConfigActionResult(
                    status="error",
                    message=(
                        f"Branch {branch!r} does not exist on ``origin`` "
                        "(checked via ``git ls-remote --heads`` after fetch)."
                    ),
                )
        except _GitError as exc:
            return ConfigActionResult(status="error", message=str(exc))

        # All checks passed — write the sentinel and request restart.
        sentinel = self._repo_root / _SENTINEL_PATH
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(branch + "\n", encoding="utf-8")

        user_id = get_current_user().user_id or "unknown"
        audit_logger.info(
            "branch_switch_requested",
            extra={
                "user_id": user_id,
                "from_branch": current,
                "to_branch": branch,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

        if self._gilbert is None:
            # Shouldn't happen — Gilbert.start() wires the binding —
            # but degrade gracefully so the user sees a useful error.
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
                f"Branch switch to {branch!r} queued. Gilbert is shutting "
                "down; the supervisor will run ``git checkout`` and "
                "relaunch automatically. The UI will reconnect once the "
                "new process binds to the WebSocket."
            ),
            data={"from_branch": current, "to_branch": branch},
        )

    # --- Git helpers ---

    async def _current_branch(self) -> str:
        return (await self._git("symbolic-ref", "--short", "HEAD")).strip()

    async def _origin_url(self) -> str:
        try:
            return (await self._git("remote", "get-url", "origin")).strip()
        except _GitError:
            return ""

    async def _dirty_files(self) -> list[str]:
        # ``--untracked-files=no`` matches ``pull_latest`` in gilbert.sh —
        # untracked build artefacts (frontend caches, etc.) don't count
        # as "dirty" for the purposes of refusing a switch.
        out = await self._git("status", "--porcelain", "--untracked-files=no")
        return [line for line in out.splitlines() if line.strip()]

    async def _fetch_origin(self) -> None:
        # Refresh the remote so ``ls-remote --heads`` reflects the
        # current truth. A bare ``--quiet`` keeps the supervisor log
        # readable; errors still surface via the non-zero exit code.
        await self._git("fetch", "--quiet", "origin")

    async def _branch_exists_on_origin(self, branch: str) -> bool:
        # ``git ls-remote --heads origin <branch>`` exits 0 either way;
        # an empty stdout means no matching ref.
        out = await self._git("ls-remote", "--heads", "origin", branch)
        return f"refs/heads/{branch}" in out

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
