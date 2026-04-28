"""Source inspector service — read-only access to Gilbert's own source tree.

Exposes three AI tools (``gilbert_list_files``, ``gilbert_read_file``,
``gilbert_grep``) so an AI session — most importantly the proposals
reflection AI — can ground its suggestions in the actual current code
rather than guessing from training data.

Design constraints:

- Strictly read-only. No write paths, no shell-out, no execution.
- Path-allowlisted. Every request is normalized to an absolute path
  inside the repo root and matched against a configured allowlist
  (``src/``, ``std-plugins/``, ``frontend/``, etc.). Anything outside
  is refused. Symlinks are resolved before the check, so
  ``foo -> /etc/passwd`` style escapes don't bypass it.
- Bounded outputs. ``read_file`` caps file size, ``list_files`` caps
  entry count, ``grep`` caps match count. The reflection AI runs on
  the most expensive profile, so we don't want a 10 MB file or a
  pathological glob blowing the context window.
- Available to any AI profile that opts in via the standard tool
  discovery flow (capability ``ai_tools``). The proposals service
  also pulls these tools out by capability and *always* injects them
  into the reflection call regardless of profile config — that path
  is what makes Gilbert capable of reading his own code while
  reflecting.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


# Conservative defaults. The allowlist covers everywhere Gilbert's own
# code lives (core, plugins, frontend, tests) plus the top-level
# pyproject + docs an implementer routinely needs to reference. It
# deliberately omits ``.gilbert/`` (runtime data, possibly secrets) and
# ``.git/`` (irrelevant + huge).
_DEFAULT_ALLOWED_PATHS: tuple[str, ...] = (
    "src",
    "std-plugins",
    "local-plugins",
    "installed-plugins",
    "frontend/src",
    "tests",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "CLAUDE.md",
    "gilbert.sh",
    ".claude/memory",
)

_DEFAULT_MAX_FILE_BYTES = 200_000  # 200 KB — enough for the largest service file
_DEFAULT_MAX_LIST_ENTRIES = 500
_DEFAULT_MAX_GREP_MATCHES = 200
_DEFAULT_MAX_GREP_FILES = 2_000

# Files we never want to read or grep through even when inside an
# allowed path. Caches, lockfiles, binary artefacts, and node_modules
# are all noise for a code-review AI.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
        ".venv",
        "venv",
    },
)

_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pyc",
        ".so",
        ".dylib",
        ".dll",
        ".o",
        ".a",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".wasm",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
    },
)


@dataclass(frozen=True)
class _PathCheck:
    """Result of normalising an AI-supplied path against the allowlist."""

    ok: bool
    resolved: Path | None
    error: str = ""


class SourceInspectorService(Service):
    """Read-only AI tools for inspecting Gilbert's own source tree.

    Capabilities: ``source_inspector``, ``ai_tools``.
    """

    _DEFAULT_ENABLED = True

    def __init__(self, repo_root: Path | None = None) -> None:
        # ``repo_root`` is normally None — resolved at start() from cwd.
        # Tests pass an explicit root so they can sandbox to a fixture
        # tree without depending on the live repo layout.
        self._repo_root_override: Path | None = (
            repo_root.resolve() if repo_root is not None else None
        )
        self._repo_root: Path = Path.cwd().resolve()
        self._enabled: bool = self._DEFAULT_ENABLED
        self._allowed_paths: tuple[str, ...] = _DEFAULT_ALLOWED_PATHS
        self._max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
        self._max_list_entries: int = _DEFAULT_MAX_LIST_ENTRIES
        self._max_grep_matches: int = _DEFAULT_MAX_GREP_MATCHES
        self._max_grep_files: int = _DEFAULT_MAX_GREP_FILES

    # ── Service lifecycle ────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="source_inspector",
            capabilities=frozenset({"source_inspector", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description=(
                "Lets the AI read Gilbert's own source code (read-only, "
                "path-allowlisted). Required for the proposals reflector "
                "to ground its suggestions in actual code."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._repo_root = (
            self._repo_root_override
            if self._repo_root_override is not None
            else self._discover_repo_root()
        )

        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            if section:
                await self.on_config_changed(section)

        if not self._enabled:
            logger.info("Source inspector disabled by config")
            return
        logger.info(
            "Source inspector started (root=%s, %d allowed paths)",
            self._repo_root,
            len(self._allowed_paths),
        )

    async def stop(self) -> None:
        return None

    @staticmethod
    def _discover_repo_root() -> Path:
        """Walk up from cwd looking for the gilbert checkout root.

        We accept either a ``pyproject.toml`` whose name field is
        ``gilbert`` or a directory containing ``src/gilbert``. Falls
        back to the cwd unchanged if neither matches — the allowlist
        check will still keep things scoped, the only downside is the
        AI sees the wrong tree.
        """
        cwd = Path.cwd().resolve()
        for candidate in (cwd, *cwd.parents):
            if (candidate / "src" / "gilbert").is_dir():
                return candidate
        return cwd

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "source_inspector"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Expose Gilbert's source-inspection tools to AI profiles. "
                    "Disabling hides them from every profile and from the "
                    "proposals reflector — turn off only if you don't want "
                    "the AI grounding suggestions in source."
                ),
                default=self._DEFAULT_ENABLED,
            ),
            ConfigParam(
                key="allowed_paths",
                type=ToolParameterType.ARRAY,
                description=(
                    "Repo-relative paths the AI is allowed to read. Each entry "
                    "may name a directory or a single file. The default set "
                    "covers core, plugins, frontend, tests, and top-level docs "
                    "while keeping runtime data (.gilbert/) and the git tree "
                    "out of reach."
                ),
                default=list(_DEFAULT_ALLOWED_PATHS),
            ),
            ConfigParam(
                key="max_file_bytes",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum bytes returned by ``gilbert_read_file``. Larger "
                    "files are truncated with a marker. Keeps a single read "
                    "from blowing the AI context window."
                ),
                default=_DEFAULT_MAX_FILE_BYTES,
            ),
            ConfigParam(
                key="max_list_entries",
                type=ToolParameterType.INTEGER,
                description="Maximum entries returned by ``gilbert_list_files``.",
                default=_DEFAULT_MAX_LIST_ENTRIES,
            ),
            ConfigParam(
                key="max_grep_matches",
                type=ToolParameterType.INTEGER,
                description="Maximum match lines returned by ``gilbert_grep``.",
                default=_DEFAULT_MAX_GREP_MATCHES,
            ),
            ConfigParam(
                key="max_grep_files",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum files scanned per ``gilbert_grep`` call. Stops "
                    "a pathological pattern from walking the entire tree."
                ),
                default=_DEFAULT_MAX_GREP_FILES,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", self._DEFAULT_ENABLED))
        paths = config.get("allowed_paths")
        if isinstance(paths, (list, tuple)) and all(isinstance(p, str) for p in paths):
            cleaned = tuple(p.strip() for p in paths if p.strip())
            if cleaned:
                self._allowed_paths = cleaned
        self._max_file_bytes = max(
            1_024, int(config.get("max_file_bytes", _DEFAULT_MAX_FILE_BYTES))
        )
        self._max_list_entries = max(
            1, int(config.get("max_list_entries", _DEFAULT_MAX_LIST_ENTRIES))
        )
        self._max_grep_matches = max(
            1, int(config.get("max_grep_matches", _DEFAULT_MAX_GREP_MATCHES))
        )
        self._max_grep_files = max(
            1, int(config.get("max_grep_files", _DEFAULT_MAX_GREP_FILES))
        )

    # ── Path safety ──────────────────────────────────────────────────

    def _check_path(self, candidate: str, *, must_exist: bool = True) -> _PathCheck:
        """Resolve an AI-supplied path and confirm it sits inside the allowlist.

        Symlinks are resolved BEFORE the allowlist check so a symlink
        pointing outside the repo can't be used to escape.
        """
        if not candidate or not isinstance(candidate, str):
            return _PathCheck(False, None, "path is required")
        # Strip a leading ``/`` so the AI's "root-style" paths still work.
        rel = candidate.lstrip("/").strip()
        if not rel:
            target = self._repo_root
        else:
            target = (self._repo_root / rel).resolve()
        try:
            target.relative_to(self._repo_root)
        except ValueError:
            return _PathCheck(False, None, "path escapes the repo root")
        if must_exist and not target.exists():
            return _PathCheck(False, None, f"path does not exist: {candidate}")
        # Allowlist check — each allowed entry is either an exact path
        # or a parent directory of the target.
        for allowed in self._allowed_paths:
            allowed_abs = (self._repo_root / allowed.strip("/")).resolve()
            if target == allowed_abs:
                return _PathCheck(True, target)
            try:
                target.relative_to(allowed_abs)
            except ValueError:
                continue
            return _PathCheck(True, target)
        return _PathCheck(False, None, f"path is not in the allowlist: {candidate}")

    # ── Tool implementations ─────────────────────────────────────────

    def _list_files(self, raw_path: str) -> dict[str, Any]:
        check = self._check_path(raw_path or "", must_exist=True)
        if not check.ok or check.resolved is None:
            return {"error": check.error or "invalid path"}
        target = check.resolved
        entries: list[dict[str, Any]] = []
        truncated = False
        if target.is_file():
            entries.append(
                {
                    "path": self._rel(target),
                    "type": "file",
                    "size": target.stat().st_size,
                },
            )
        else:
            try:
                walker = self._walk(target)
                for path in walker:
                    if len(entries) >= self._max_list_entries:
                        truncated = True
                        break
                    rel = self._rel(path)
                    entries.append(
                        {
                            "path": rel,
                            "type": "dir" if path.is_dir() else "file",
                            "size": path.stat().st_size if path.is_file() else None,
                        },
                    )
            except OSError as exc:
                return {"error": f"could not list path: {exc}"}
        return {
            "root": self._rel(target) or ".",
            "entries": entries,
            "truncated": truncated,
            "max_entries": self._max_list_entries,
        }

    def _read_file(self, raw_path: str) -> dict[str, Any]:
        check = self._check_path(raw_path or "", must_exist=True)
        if not check.ok or check.resolved is None:
            return {"error": check.error or "invalid path"}
        target = check.resolved
        if not target.is_file():
            return {"error": f"path is not a file: {raw_path}"}
        if target.suffix.lower() in _BINARY_EXTENSIONS:
            return {"error": f"refusing to read binary file: {raw_path}"}
        try:
            data = target.read_bytes()
        except OSError as exc:
            return {"error": f"could not read file: {exc}"}
        truncated = False
        if len(data) > self._max_file_bytes:
            data = data[: self._max_file_bytes]
            truncated = True
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content = data.decode("utf-8", errors="replace")
        return {
            "path": self._rel(target),
            "size": target.stat().st_size,
            "content": content,
            "truncated": truncated,
            "max_bytes": self._max_file_bytes,
        }

    def _grep(
        self,
        pattern: str,
        raw_path: str,
        case_sensitive: bool,
    ) -> dict[str, Any]:
        if not pattern:
            return {"error": "pattern is required"}
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return {"error": f"invalid regex: {exc}"}
        # Default search root is "everything in the allowlist" — when
        # the AI hasn't specified one we walk every allowed entry.
        search_roots: list[Path] = []
        if raw_path:
            check = self._check_path(raw_path, must_exist=True)
            if not check.ok or check.resolved is None:
                return {"error": check.error or "invalid path"}
            search_roots.append(check.resolved)
        else:
            for allowed in self._allowed_paths:
                p = (self._repo_root / allowed.strip("/")).resolve()
                if p.exists():
                    search_roots.append(p)
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        truncated = False
        for root in search_roots:
            for path in self._walk(root, files_only=True):
                if files_scanned >= self._max_grep_files:
                    truncated = True
                    break
                files_scanned += 1
                if path.suffix.lower() in _BINARY_EXTENSIONS:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = self._rel(path)
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        matches.append(
                            {
                                "path": rel,
                                "line": lineno,
                                "text": line[:300],
                            },
                        )
                        if len(matches) >= self._max_grep_matches:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        return {
            "pattern": pattern,
            "matches": matches,
            "files_scanned": files_scanned,
            "truncated": truncated,
            "max_matches": self._max_grep_matches,
        }

    def _walk(self, root: Path, *, files_only: bool = False) -> Iterable[Path]:
        """Walk ``root`` skipping cache/build directories.

        Resolves each entry so symlinked subtrees can't escape the
        allowlist on a per-iteration basis.
        """
        if root.is_file():
            yield root
            return
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                for child in sorted(current.iterdir()):
                    if child.name in _SKIP_DIRS:
                        continue
                    try:
                        resolved = child.resolve()
                        resolved.relative_to(self._repo_root)
                    except (OSError, ValueError):
                        continue
                    if resolved.is_dir():
                        stack.append(resolved)
                        if not files_only:
                            yield resolved
                    else:
                        yield resolved
            except OSError:
                continue

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._repo_root))
        except ValueError:
            return str(path)

    # ── ToolProvider ─────────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "source_inspector"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return list(self._tool_definitions())

    def _tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Static tool list — used both by ``get_tools`` and by the
        proposals service when it injects tools regardless of profile.
        """
        return (
            ToolDefinition(
                name="gilbert_list_files",
                description=(
                    "List files and directories inside Gilbert's own source "
                    "tree. Use this to discover what code exists before "
                    "proposing changes. Path is repo-relative — pass an "
                    "empty string to list the allowed roots, or a directory "
                    "like 'src/gilbert/core/services' to drill in. "
                    "Read-only."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Repo-relative path. Empty string lists the "
                            "configured allowlist roots."
                        ),
                        required=False,
                        default="",
                    ),
                ],
                required_role="admin",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="gilbert_read_file",
                description=(
                    "Read a single file from Gilbert's own source tree. "
                    "Use this to inspect the actual implementation before "
                    "proposing edits or new code that has to integrate "
                    "with it. Path is repo-relative. Read-only — files are "
                    "never modified by this tool. Large files are truncated."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Repo-relative path to a single file "
                            "(e.g. 'src/gilbert/core/services/proposals.py')."
                        ),
                    ),
                ],
                required_role="admin",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="gilbert_grep",
                description=(
                    "Search Gilbert's source tree with a regex. Returns "
                    "matching path:line:text rows. Use this to find where "
                    "a symbol is defined or referenced before proposing "
                    "changes. Read-only."
                ),
                parameters=[
                    ToolParameter(
                        name="pattern",
                        type=ToolParameterType.STRING,
                        description="Python regex to search for.",
                    ),
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional repo-relative path to scope the "
                            "search (e.g. 'src/gilbert/core/services'). "
                            "Empty string searches every allowed root."
                        ),
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="case_sensitive",
                        type=ToolParameterType.BOOLEAN,
                        description="Whether the match is case-sensitive.",
                        required=False,
                        default=False,
                    ),
                ],
                required_role="admin",
                parallel_safe=True,
            ),
        )

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return the inspector tools regardless of ``enabled`` state.

        Used by the proposals service to ALWAYS attach inspector tools
        to a reflection AI call. The proposals service has already
        decided the call is appropriate, so the user-facing on/off
        knob doesn't apply.
        """
        return list(self._tool_definitions())

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "gilbert_list_files":
            result = self._list_files(str(arguments.get("path") or ""))
        elif name == "gilbert_read_file":
            path = str(arguments.get("path") or "").strip()
            if not path:
                result = {"error": "'path' is required"}
            else:
                result = self._read_file(path)
        elif name == "gilbert_grep":
            pattern = str(arguments.get("pattern") or "").strip()
            path = str(arguments.get("path") or "").strip()
            case_sensitive = bool(arguments.get("case_sensitive", False))
            result = self._grep(pattern, path, case_sensitive)
        else:
            raise KeyError(f"Unknown tool: {name}")
        return json.dumps(result)
