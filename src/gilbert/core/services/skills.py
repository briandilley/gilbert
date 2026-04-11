"""Skill service — Agent Skills standard (agentskills.io) support for Gilbert."""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import json
import logging
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml


async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking function in the default thread pool executor."""
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
    return await loop.run_in_executor(None, func, *args)

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.skills import SkillCatalogEntry, SkillContent
from gilbert.interfaces.storage import IndexDefinition, Query
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
)
from gilbert.interfaces.ws import WsHandlerProvider

logger = logging.getLogger(__name__)

_SKILL_FILENAME = "SKILL.md"
_SKILL_COLLECTION = "skill_definitions"
_ACTIVE_SKILLS_KEY = "active_skills"
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "users"}
_RESOURCE_DIRS = {"scripts", "references", "assets"}
_MAX_SCAN_DEPTH = 4


class SkillService(Service, ToolProvider, WsHandlerProvider):
    """Discovers, manages, and serves Agent Skills to the AI system."""

    def __init__(self) -> None:
        self._enabled: bool = False
        self._directories: list[str] = ["./skills"]
        self._cache_dir: str = ".gilbert/skill-cache"
        self._user_dir: str = ".gilbert/skills"
        self._catalog: dict[str, SkillCatalogEntry] = {}
        self._content_cache: dict[str, SkillContent] = {}
        self._resolver: ServiceResolver | None = None
        self._acl_svc: Any = None
        self._storage: Any = None
        user_dir = Path(self._user_dir)
        self._user_skills_dir = user_dir if user_dir.is_absolute() else Path.cwd() / user_dir

    # ── Service interface ────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="skills",
            capabilities=frozenset({"skills", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"access_control", "ai_chat", "configuration"}),
            toggleable=True,
            toggle_description="Custom AI skills",
        )

    @property
    def _ai_svc(self) -> Any:
        """Resolve AIService lazily — it may start after SkillService."""
        if self._resolver is None:
            return None
        return self._resolver.get_capability("ai_chat")

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Check enabled and load config
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section(self.config_namespace)
                if not section.get("enabled", True):
                    logger.info("Skills service disabled")
                    return
                self._directories = section.get("directories", ["./skills"])
                self._cache_dir = section.get("cache_dir", ".gilbert/skill-cache")
                self._user_dir = section.get("user_dir", ".gilbert/skills")
                user_dir = Path(self._user_dir)
                self._user_skills_dir = user_dir if user_dir.is_absolute() else Path.cwd() / user_dir

        self._enabled = True
        self._acl_svc = resolver.get_capability("access_control")

        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None:
            self._storage = getattr(storage_svc, "backend", storage_svc)
            await self._storage.ensure_index(
                IndexDefinition(collection=_SKILL_COLLECTION, fields=["owner_id"])
            )

        self._discover_skills()
        await self._load_entity_skills()
        logger.info("Skills service started — %d skills discovered", len(self._catalog))

    async def stop(self) -> None:
        pass

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "skills"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam

        return [
            ConfigParam(
                key="directories", type=ToolParameterType.ARRAY,
                description="Directories to scan for skill definitions.",
                default=["./skills"], restart_required=True,
            ),
            ConfigParam(
                key="cache_dir", type=ToolParameterType.STRING,
                description="Directory for cached remote skills.",
                default=".gilbert/skill-cache", restart_required=True,
            ),
            ConfigParam(
                key="user_dir", type=ToolParameterType.STRING,
                description="Directory for user-installed skills.",
                default=".gilbert/skills", restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        pass  # All skill params are restart_required

    # ── ToolProvider interface ───────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "skills"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="manage_skills",
                description=(
                    "Install, uninstall, update, or list available skills. "
                    "Install from GitHub URLs or archive URLs (.zip, .tar.gz). "
                    "Admins can install globally (for all users) or personally. "
                    "Non-admins install for themselves only."
                ),
                parameters=[
                    ToolParameter(
                        name="action",
                        type=ToolParameterType.STRING,
                        description="Action to perform.",
                        enum=["install", "uninstall", "update", "list", "delete"],
                    ),
                    ToolParameter(
                        name="url",
                        type=ToolParameterType.STRING,
                        description="GitHub URL for install/update.",
                        required=False,
                    ),
                    ToolParameter(
                        name="skill_name",
                        type=ToolParameterType.STRING,
                        description="Skill name for uninstall/update.",
                        required=False,
                    ),
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Subdirectory path if the repo contains multiple skills.",
                        required=False,
                    ),
                    ToolParameter(
                        name="scope",
                        type=ToolParameterType.STRING,
                        description=(
                            "Install scope: 'global' (all users, admin only) "
                            "or 'user' (just for you). You MUST ask the user which "
                            "scope they want before installing."
                        ),
                        required=False,
                        enum=["global", "user"],
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="read_skill_file",
                description=(
                    "Read a file from an active skill's directory "
                    "(references, assets, scripts, etc.)."
                ),
                parameters=[
                    ToolParameter(
                        name="skill_name",
                        type=ToolParameterType.STRING,
                        description="Name of the active skill.",
                    ),
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Relative path within the skill directory.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="run_skill_script",
                description=(
                    "Execute a script bundled with an active skill. "
                    "Scripts run in the user's workspace directory. "
                    "Output files are written to the workspace, not the skill directory."
                ),
                parameters=[
                    ToolParameter(
                        name="skill_name",
                        type=ToolParameterType.STRING,
                        description="Name of the active skill.",
                    ),
                    ToolParameter(
                        name="script",
                        type=ToolParameterType.STRING,
                        description="Relative path to the script within the skill directory.",
                    ),
                    ToolParameter(
                        name="arguments",
                        type=ToolParameterType.ARRAY,
                        description="Command-line arguments to pass to the script.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="browse_skill_workspace",
                description="List files in your workspace for a skill (created by script runs).",
                parameters=[
                    ToolParameter(
                        name="skill_name",
                        type=ToolParameterType.STRING,
                        description="Name of the skill.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="read_skill_workspace_file",
                description="Read a text file from your skill workspace.",
                parameters=[
                    ToolParameter(
                        name="skill_name",
                        type=ToolParameterType.STRING,
                        description="Name of the skill.",
                    ),
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Relative path within the workspace.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="create_skill",
                description=(
                    "Create or update a text-based skill. The skill_md parameter "
                    "must be a complete SKILL.md formatted string with YAML frontmatter "
                    "(name, description required) followed by markdown instructions. "
                    "These skills are stored in the system and can use any tools "
                    "available in the conversation context."
                ),
                parameters=[
                    ToolParameter(
                        name="skill_md",
                        type=ToolParameterType.STRING,
                        description=(
                            "Complete SKILL.md content. Must start with --- YAML frontmatter "
                            "(name, description) followed by --- then markdown instructions."
                        ),
                    ),
                    ToolParameter(
                        name="scope",
                        type=ToolParameterType.STRING,
                        description="Scope: 'global' (admin only) or 'user' (personal).",
                        enum=["global", "user"],
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "manage_skills":
                return await self._tool_manage_skills(arguments)
            case "create_skill":
                return await self._tool_create_skill(arguments)
            case "read_skill_file":
                return await self._tool_read_skill_file(arguments)
            case "run_skill_script":
                return await self._tool_run_skill_script(arguments)
            case "browse_skill_workspace":
                return await self._tool_browse_workspace(arguments)
            case "read_skill_workspace_file":
                return await self._tool_read_workspace_file(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    # ── WsHandlerProvider interface ──────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "skills.list": self._ws_skills_list,
            "skills.conversation.active": self._ws_skills_active,
            "skills.conversation.toggle": self._ws_skills_toggle,
            "skills.workspace.browse": self._ws_workspace_browse,
            "skills.workspace.download": self._ws_workspace_download,
        }

    # ── Public API for AIService integration ─────────────────────────

    def get_catalog(
        self, user_ctx: Any = None,
    ) -> list[tuple[str, SkillCatalogEntry]]:
        """Return (catalog_key, entry) pairs, filtered by user visibility and role."""
        user_id = getattr(user_ctx, "user_id", None) if user_ctx else None
        result: list[tuple[str, SkillCatalogEntry]] = []

        for key, e in self._catalog.items():
            if e.scope == "user" and e.owner_id != user_id:
                continue
            result.append((key, e))

        if user_ctx is not None and self._acl_svc is not None:
            from gilbert.core.services.access_control import AccessControlService

            if isinstance(self._acl_svc, AccessControlService):
                user_level = self._acl_svc.get_effective_level(user_ctx)
                result = [
                    (k, e) for k, e in result
                    if user_level <= self._acl_svc.get_role_level(e.required_role)
                ]
        return result

    async def get_active_skills(self, conversation_id: str) -> list[str]:
        """Return active skill names for a conversation."""
        if self._ai_svc is None:
            return []
        try:
            value = await self._ai_svc.get_conversation_state(
                _ACTIVE_SKILLS_KEY, conversation_id,
            )
            return value if isinstance(value, list) else []
        except RuntimeError:
            return []

    async def set_active_skills(
        self, conversation_id: str, skills: list[str],
    ) -> None:
        """Update active skills for a conversation."""
        if self._ai_svc is None:
            return
        valid = [s for s in skills if s in self._catalog]
        await self._ai_svc.set_conversation_state(
            _ACTIVE_SKILLS_KEY, valid, conversation_id,
        )

    def get_skill_content(self, name: str) -> SkillContent | None:
        """Load full skill instructions + resource list."""
        if name in self._content_cache:
            return self._content_cache[name]
        entry = self._catalog.get(name)
        if entry is None:
            return None

        # Entity-stored skills are cached at load time
        if entry.location is None:
            return None

        _, body = self._parse_skill_md(entry.location)
        if body is None:
            return None

        resources = self._enumerate_resources(entry.location.parent)
        content = SkillContent(catalog=entry, instructions=body, resources=resources)
        self._content_cache[name] = content
        return content

    async def build_skills_context(self, conversation_id: str) -> str:
        """Build formatted instructions for all active skills in a conversation."""
        active = await self.get_active_skills(conversation_id)
        if not active:
            return ""

        parts: list[str] = ["## Active Skills"]
        parts.append(
            "The following skills have been activated for this conversation. "
            "Follow their instructions when relevant to the user's request."
        )
        for skill_name in active:
            content = self.get_skill_content(skill_name)
            if content is None:
                continue
            section = f"\n### {skill_name}\n"
            section += f'<skill_content name="{skill_name}">\n'
            section += content.instructions.strip()
            if content.catalog.location is not None:
                section += f"\n\nSkill directory: {content.catalog.location.parent}"
                if content.resources:
                    section += "\n\n<skill_resources>\n"
                    for res in content.resources:
                        section += f"  <file>{res}</file>\n"
                    section += "</skill_resources>"
            section += "\n</skill_content>"
            parts.append(section)
        return "\n\n".join(parts)

    def get_active_allowed_tools(self, active_skills: list[str]) -> set[str]:
        """Return union of allowed_tools from a set of active skills."""
        tools: set[str] = set()
        for name in active_skills:
            entry = self._catalog.get(name)
            if entry and entry.allowed_tools:
                tools.update(entry.allowed_tools)
        return tools

    # ── Workspace ────────────────────────────────────────────────────

    def _get_workspace(self, user_id: str, skill_name: str) -> Path:
        """Get (and create) a user's workspace directory for a skill."""
        workspace = self._user_skills_dir.parent / "skill-workspaces" / user_id / skill_name
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    # ── Skill Discovery ──────────────────────────────────────────────

    def _discover_skills(self) -> None:
        """Scan configured directories for SKILL.md files."""
        self._catalog.clear()
        self._content_cache.clear()

        # Scan configured directories (shipped/global skills)
        for directory in self._directories:
            skill_dir = Path(directory)
            if not skill_dir.is_absolute():
                skill_dir = Path.cwd() / skill_dir
            if not skill_dir.is_dir():
                logger.debug("Skill directory not found: %s", skill_dir)
                continue
            self._scan_directory(skill_dir)

        # Scan global installed skills
        if self._user_skills_dir.is_dir():
            self._scan_directory(self._user_skills_dir)

        # Scan per-user installed skills
        users_dir = self._user_skills_dir / "users"
        if users_dir.is_dir():
            for user_dir in sorted(users_dir.iterdir()):
                if not user_dir.is_dir():
                    continue
                owner_id = user_dir.name
                for entry in sorted(user_dir.iterdir()):
                    if entry.is_dir() and (entry / _SKILL_FILENAME).is_file():
                        self._load_skill(
                            entry / _SKILL_FILENAME,
                            scope="user",
                            owner_id=owner_id,
                        )

    async def _load_entity_skills(self) -> None:
        """Load skills stored in entity storage into the catalog."""
        if self._storage is None:
            return
        docs = await self._storage.query(Query(collection=_SKILL_COLLECTION))
        for doc in docs:
            skill_md = doc.get("skill_md", "")
            if not skill_md:
                continue
            scope = doc.get("scope", "global")
            owner_id = doc.get("owner_id", "")
            doc_id = doc.get("_id", "")

            frontmatter, body = self._parse_skill_md_text(skill_md)
            if frontmatter is None or not frontmatter.get("description"):
                continue

            name = frontmatter.get("name", doc_id)
            metadata = frontmatter.get("metadata", {}) or {}
            allowed_tools_raw = frontmatter.get("allowed-tools", "")
            if isinstance(allowed_tools_raw, str):
                allowed_tools = allowed_tools_raw.split() if allowed_tools_raw else []
            elif isinstance(allowed_tools_raw, list):
                allowed_tools = [str(t) for t in allowed_tools_raw]
            else:
                allowed_tools = []

            catalog_key = f"{owner_id}:{name}" if scope == "user" else name
            if catalog_key in self._catalog:
                continue

            self._catalog[catalog_key] = SkillCatalogEntry(
                name=name,
                description=frontmatter["description"],
                location=None,
                category=str(metadata.get("category", "")),
                icon=str(metadata.get("icon", "")),
                required_role=str(metadata.get("required-role", "user")),
                allowed_tools=allowed_tools,
                scope=scope,
                owner_id=owner_id,
            )
            # Cache content immediately — no file to read later
            self._content_cache[catalog_key] = SkillContent(
                catalog=self._catalog[catalog_key],
                instructions=body or "",
            )
        logger.debug("Loaded %d entity-stored skills", len(docs))

    def _scan_directory(self, base: Path, depth: int = 0) -> None:
        """Recursively scan for SKILL.md files up to max depth."""
        if depth > _MAX_SCAN_DEPTH:
            return
        try:
            for entry in sorted(base.iterdir()):
                if not entry.is_dir() or entry.name in _SKIP_DIRS:
                    continue
                skill_md = entry / _SKILL_FILENAME
                if skill_md.is_file():
                    self._load_skill(skill_md)
                else:
                    self._scan_directory(entry, depth + 1)
        except PermissionError:
            logger.debug("Permission denied scanning: %s", base)

    def _load_skill(
        self,
        skill_md: Path,
        scope: str = "global",
        owner_id: str = "",
    ) -> None:
        """Parse a SKILL.md file and add to catalog."""
        frontmatter, _ = self._parse_skill_md(skill_md)
        if frontmatter is None:
            return

        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")

        if not description:
            logger.warning("Skipping skill at %s: missing description", skill_md)
            return

        if not name:
            name = skill_md.parent.name
            logger.debug("Skill at %s has no name, using directory name: %s", skill_md, name)

        if name != skill_md.parent.name:
            logger.debug(
                "Skill name %r doesn't match directory %r", name, skill_md.parent.name,
            )

        metadata = frontmatter.get("metadata", {}) or {}
        category = str(metadata.get("category", ""))
        icon = str(metadata.get("icon", ""))
        required_role = str(metadata.get("required-role", "user"))

        allowed_tools_raw = frontmatter.get("allowed-tools", "")
        if isinstance(allowed_tools_raw, str):
            allowed_tools = allowed_tools_raw.split() if allowed_tools_raw else []
        elif isinstance(allowed_tools_raw, list):
            allowed_tools = [str(t) for t in allowed_tools_raw]
        else:
            allowed_tools = []

        # Use namespaced key for user skills to avoid collisions
        catalog_key = f"{owner_id}:{name}" if scope == "user" else name

        if catalog_key in self._catalog:
            logger.warning(
                "Duplicate skill %r at %s (already loaded from %s)",
                catalog_key, skill_md, self._catalog[catalog_key].location,
            )
            return

        self._catalog[catalog_key] = SkillCatalogEntry(
            name=name,
            description=description,
            location=skill_md,
            category=category,
            icon=icon,
            required_role=required_role,
            allowed_tools=allowed_tools,
            scope=scope,
            owner_id=owner_id,
        )
        logger.debug("Loaded skill: %s (scope=%s, owner=%s)", name, scope, owner_id or "-")

    # ── SKILL.md Parsing ─────────────────────────────────────────────

    @staticmethod
    def _parse_skill_md_text(text: str) -> tuple[dict[str, Any] | None, str | None]:
        """Parse SKILL.md formatted text into (frontmatter_dict, body_str)."""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
        if not match:
            return None, None

        yaml_str, body = match.group(1), match.group(2)

        try:
            frontmatter = yaml.safe_load(yaml_str)
        except yaml.YAMLError:
            try:
                fixed = re.sub(
                    r"^(\w+):\s+(.+:.+)$",
                    lambda m: f'{m.group(1)}: "{m.group(2)}"',
                    yaml_str,
                    flags=re.MULTILINE,
                )
                frontmatter = yaml.safe_load(fixed)
            except yaml.YAMLError:
                return None, None

        if not isinstance(frontmatter, dict):
            return None, None

        return frontmatter, body.strip() if body else ""

    @staticmethod
    def _parse_skill_md(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        """Parse a SKILL.md file into (frontmatter_dict, body_str)."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Cannot read %s: %s", path, exc)
            return None, None
        fm, body = SkillService._parse_skill_md_text(text)
        if fm is None:
            logger.warning("No valid YAML frontmatter in %s", path)
        return fm, body

    @staticmethod
    def _enumerate_resources(skill_dir: Path) -> list[str]:
        """List bundled resource files in a skill directory."""
        resources: list[str] = []
        for subdir_name in _RESOURCE_DIRS:
            subdir = skill_dir / subdir_name
            if not subdir.is_dir():
                continue
            for file_path in sorted(subdir.rglob("*")):
                if file_path.is_file():
                    resources.append(str(file_path.relative_to(skill_dir)))
        return resources

    # ── Remote Skill Fetching ────────────────────────────────────────

    def _fetch_skill_source(self, url: str) -> Path:
        """Fetch a skill source from a URL — auto-detects git repos vs archives."""
        lower = url.lower()
        if lower.endswith((".zip", ".tar.gz", ".tgz")):
            return self._fetch_from_archive(url)
        return self._fetch_from_github(url)

    def _fetch_from_archive(self, url: str) -> Path:
        """Download and extract a zip or tar.gz archive into the skill cache."""
        cache_dir = Path.cwd() / self._cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Derive a directory name from the URL
        filename = url.rstrip("/").split("/")[-1].split("?")[0]
        for suffix in (".tar.gz", ".tgz", ".zip"):
            if filename.lower().endswith(suffix):
                dirname = filename[: -len(suffix)]
                break
        else:
            dirname = filename
        target = cache_dir / dirname

        # Download
        logger.debug("Downloading skill archive: %s", url)
        response = httpx.get(url, follow_redirects=True, timeout=60)
        response.raise_for_status()
        data = response.content

        # Remove previous extraction
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)

        # Extract
        lower = url.lower()
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(target)
        elif lower.endswith((".tar.gz", ".tgz")):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                tf.extractall(target, filter="data")

        # If the archive extracted a single top-level directory, use that
        entries = [e for e in target.iterdir() if e.is_dir() and e.name not in _SKIP_DIRS]
        if len(entries) == 1 and not (target / _SKILL_FILENAME).exists():
            return entries[0]

        return target

    def _fetch_from_github(self, url: str) -> Path:
        """Clone or update a GitHub repository into the skill cache."""
        cache_dir = Path.cwd() / self._cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

        repo_name = url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        target = cache_dir / repo_name

        if target.exists():
            logger.debug("Updating cached skill repo: %s", repo_name)
            subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only"],
                check=True,
                capture_output=True,
            )
        else:
            logger.debug("Cloning skill repo: %s", url)
            subprocess.run(
                ["git", "clone", url, str(target)],
                check=True,
                capture_output=True,
            )

        return target

    def _install_from_repo(
        self,
        repo_dir: Path,
        dest_parent: Path,
        subpath: str | None = None,
        scope: str = "global",
        owner_id: str = "",
    ) -> list[str]:
        """Discover and install skills from a cloned repo."""
        installed: list[str] = []
        dest_parent.mkdir(parents=True, exist_ok=True)

        search_root = repo_dir / subpath if subpath else repo_dir

        skill_md = search_root / _SKILL_FILENAME
        if skill_md.is_file():
            name = self._install_skill_dir(search_root, dest_parent, scope, owner_id)
            if name:
                installed.append(name)
            return installed

        if search_root.is_dir():
            for entry in sorted(search_root.iterdir()):
                if entry.is_dir() and (entry / _SKILL_FILENAME).is_file():
                    name = self._install_skill_dir(entry, dest_parent, scope, owner_id)
                    if name:
                        installed.append(name)

        return installed

    def _install_skill_dir(
        self,
        source: Path,
        dest_parent: Path,
        scope: str = "global",
        owner_id: str = "",
    ) -> str | None:
        """Copy a skill directory to the destination."""
        skill_md = source / _SKILL_FILENAME
        frontmatter, _ = self._parse_skill_md(skill_md)
        if frontmatter is None:
            return None

        name = frontmatter.get("name", source.name)
        target = dest_parent / name

        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

        self._load_skill(target / _SKILL_FILENAME, scope=scope, owner_id=owner_id)
        self._content_cache.pop(name, None)

        logger.info("Installed skill: %s → %s (scope=%s)", name, target, scope)
        return name

    def _uninstall_skill(self, catalog_key: str, user_id: str) -> bool:
        """Remove an installed skill. Users can only remove their own."""
        entry = self._catalog.get(catalog_key)
        if entry is None:
            return False

        # Users can only uninstall their own user-scoped skills
        if entry.scope == "user" and entry.owner_id != user_id:
            return False

        # Non-user-scoped skills: only allow if under the user_skills_dir
        if entry.scope == "global":
            try:
                entry.location.parent.relative_to(self._user_skills_dir.resolve())
            except ValueError:
                return False  # Can't uninstall shipped skills

        skill_dir = entry.location.parent
        shutil.rmtree(skill_dir, ignore_errors=True)
        self._catalog.pop(catalog_key, None)
        self._content_cache.pop(catalog_key, None)
        logger.info("Uninstalled skill: %s", catalog_key)
        return True

    def _resolve_install_dest(
        self, scope: str, user_id: str,
    ) -> Path:
        """Resolve the installation directory for a given scope."""
        if scope == "user":
            return self._user_skills_dir / "users" / user_id
        return self._user_skills_dir

    def _is_admin(self, user_id: str) -> bool:
        """Check if a user has admin role."""
        if self._acl_svc is None:
            return False
        from gilbert.core.services.access_control import AccessControlService

        if not isinstance(self._acl_svc, AccessControlService):
            return False
        # Admin level is 0
        return self._acl_svc.get_role_level("admin") >= 0

    # ── Blocking helpers (run in thread pool) ──────────────────────

    def _do_install(
        self, url: str, subpath: str | None, scope: str, user_id: str,
    ) -> list[str]:
        """Blocking install: fetch + extract + copy. Must run in executor."""
        source_dir = self._fetch_skill_source(url)
        dest = self._resolve_install_dest(scope, user_id)
        owner_id = user_id if scope == "user" else ""
        return self._install_from_repo(
            source_dir, dest, subpath, scope=scope, owner_id=owner_id,
        )

    def _do_run_script(
        self, skill_dir: Path, script_path: str, script_args: list[Any],
        workspace: Path,
    ) -> str:
        """Blocking script execution. Must run in executor."""
        target = (skill_dir / script_path).resolve()

        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            return json.dumps({"error": "Path traversal not allowed"})

        if not target.is_file():
            return json.dumps({"error": f"Script not found: {script_path}"})

        suffix = target.suffix.lower()
        if suffix == ".py":
            cmd = ["python3", str(target)] + [str(a) for a in script_args]
        elif suffix == ".sh":
            cmd = ["bash", str(target)] + [str(a) for a in script_args]
        elif suffix in (".ts", ".js"):
            cmd = ["node", str(target)] + [str(a) for a in script_args]
        else:
            cmd = [str(target)] + [str(a) for a in script_args]

        try:
            result = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            if len(output) > 30_000:
                output = output[:30_000] + "\n\n[... truncated at 30,000 characters]"
            return output if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Script timed out after 30 seconds"})
        except OSError as exc:
            return json.dumps({"error": f"Cannot execute script: {exc}"})

    @staticmethod
    def _list_workspace_files(workspace: Path) -> list[dict[str, Any]]:
        """List files in a workspace directory. Blocking — run in executor."""
        files: list[dict[str, Any]] = []
        for f in sorted(workspace.rglob("*")):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "path": str(f.relative_to(workspace)),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc,
                    ).isoformat(),
                })
        return files

    # ── Tool Implementations ─────────────────────────────────────────

    async def _tool_manage_skills(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action", "")
        user_id = arguments.get("_user_id", "system")
        user_roles = arguments.get("_user_roles")

        if action == "list":
            entries = [
                {
                    "name": e.name,
                    "description": e.description,
                    "category": e.category,
                    "scope": e.scope,
                }
                for e in self._catalog.values()
                if e.scope == "global" or e.owner_id == user_id
            ]
            return json.dumps({"skills": entries, "count": len(entries)})

        if action == "install":
            url = arguments.get("url", "")
            if not url:
                return json.dumps({"error": "url is required for install"})

            scope = arguments.get("scope", "")
            is_admin = self._is_admin_user(user_roles)

            if not scope:
                if is_admin:
                    return json.dumps({
                        "needs_scope": True,
                        "message": (
                            "Please ask the user whether to install this skill globally "
                            "(available to all users) or just for themselves. "
                            "Then call this tool again with scope='global' or scope='user'."
                        ),
                    })
                scope = "user"

            if scope == "global" and not is_admin:
                scope = "user"

            try:
                installed = await _to_thread(
                    self._do_install, url, arguments.get("path"), scope, user_id,
                )
                if not installed:
                    return json.dumps({"error": "No valid skills found at that URL"})
                results = []
                for name in installed:
                    catalog_key = f"{user_id}:{name}" if scope == "user" else name
                    entry = self._catalog.get(catalog_key)
                    results.append({
                        "name": name,
                        "description": entry.description if entry else "",
                        "scope": scope,
                    })
                return json.dumps({"installed": results})
            except (subprocess.CalledProcessError, httpx.HTTPError, OSError) as exc:
                return json.dumps({"error": f"Install failed: {exc}"})

        if action == "uninstall":
            name = arguments.get("skill_name", "")
            if not name:
                return json.dumps({"error": "skill_name is required for uninstall"})
            # Try user-scoped key first, then global
            catalog_key = f"{user_id}:{name}"
            if catalog_key not in self._catalog:
                catalog_key = name
            if await _to_thread(self._uninstall_skill, catalog_key, user_id):
                return json.dumps({"uninstalled": name})
            return json.dumps({"error": f"Cannot uninstall '{name}' (not found or built-in)"})

        if action == "update":
            url = arguments.get("url", "")
            name = arguments.get("skill_name", "")
            if url:
                scope = arguments.get("scope", "user")
                if scope == "global" and not self._is_admin_user(user_roles):
                    scope = "user"
                try:
                    installed = await _to_thread(
                        self._do_install, url, arguments.get("path"), scope, user_id,
                    )
                    return json.dumps({"updated": installed})
                except (subprocess.CalledProcessError, httpx.HTTPError, OSError) as exc:
                    return json.dumps({"error": f"Git operation failed: {exc}"})
            if name:
                # Try user-scoped key first, then global
                catalog_key = f"{user_id}:{name}"
                if catalog_key not in self._catalog:
                    catalog_key = name
                entry = self._catalog.get(catalog_key)
                if entry is None:
                    return json.dumps({"error": f"Skill '{name}' not found"})
                self._catalog.pop(catalog_key, None)
                self._content_cache.pop(catalog_key, None)
                self._load_skill(entry.location, scope=entry.scope, owner_id=entry.owner_id)
                return json.dumps({"updated": [name]})
            return json.dumps({"error": "url or skill_name required for update"})

        if action == "delete":
            name = arguments.get("skill_name", "")
            if not name:
                return json.dumps({"error": "skill_name is required for delete"})
            # Resolve catalog key
            catalog_key = f"{user_id}:{name}"
            if catalog_key not in self._catalog:
                catalog_key = name
            entry = self._catalog.get(catalog_key)
            if entry is None:
                return json.dumps({"error": f"Skill '{name}' not found"})
            # Only entity-stored skills can be deleted this way
            if entry.location is not None:
                return json.dumps({
                    "error": f"Skill '{name}' is file-based. Use 'uninstall' instead.",
                })
            # Ownership check
            if entry.scope == "user" and entry.owner_id != user_id:
                return json.dumps({"error": "Cannot delete another user's skill"})
            if entry.scope == "global" and not self._is_admin_user(user_roles):
                return json.dumps({"error": "Only admins can delete global skills"})
            # Remove from storage
            if self._storage is not None:
                # Find and delete the document
                docs = await self._storage.query(Query(collection=_SKILL_COLLECTION))
                for doc in docs:
                    fm, _ = self._parse_skill_md_text(doc.get("skill_md", ""))
                    if fm and fm.get("name") == name and doc.get("owner_id", "") == entry.owner_id:
                        await self._storage.delete(_SKILL_COLLECTION, doc["_id"])
                        break
            self._catalog.pop(catalog_key, None)
            self._content_cache.pop(catalog_key, None)
            return json.dumps({"deleted": name})

        return json.dumps({"error": f"Unknown action: {action}"})

    async def _tool_create_skill(self, arguments: dict[str, Any]) -> str:
        """Create or update an entity-stored skill."""
        skill_md = arguments.get("skill_md", "")
        user_id = arguments.get("_user_id", "system")
        user_roles = arguments.get("_user_roles")
        scope = arguments.get("scope", "user")

        if not skill_md:
            return json.dumps({"error": "skill_md is required"})

        # Parse and validate
        frontmatter, body = self._parse_skill_md_text(skill_md)
        if frontmatter is None:
            return json.dumps({
                "error": "Invalid SKILL.md format. Must start with --- YAML frontmatter ---",
            })

        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        if not name or not description:
            return json.dumps({
                "error": "YAML frontmatter must include 'name' and 'description' fields",
            })

        # Enforce scope
        if scope == "global" and not self._is_admin_user(user_roles):
            scope = "user"

        owner_id = user_id if scope == "user" else ""
        catalog_key = f"{owner_id}:{name}" if scope == "user" else name

        # Check for conflict with file-based skills
        existing = self._catalog.get(catalog_key)
        if existing is not None and existing.location is not None:
            return json.dumps({
                "error": f"A file-based skill named '{name}' already exists",
            })

        # Store in entity storage
        if self._storage is None:
            return json.dumps({"error": "Entity storage not available"})

        now = datetime.now(timezone.utc).isoformat()

        # Check for existing entity-stored skill with same name (update case)
        doc_id = None
        docs = await self._storage.query(Query(collection=_SKILL_COLLECTION))
        for doc in docs:
            fm, _ = self._parse_skill_md_text(doc.get("skill_md", ""))
            if fm and fm.get("name") == name and doc.get("owner_id", "") == owner_id:
                doc_id = doc["_id"]
                break

        doc_data: dict[str, Any] = {
            "skill_md": skill_md,
            "scope": scope,
            "owner_id": owner_id,
            "updated_at": now,
        }
        if doc_id is None:
            doc_data["created_at"] = now
            doc_id = f"skill_{name}"
        await self._storage.put(_SKILL_COLLECTION, doc_id, doc_data)

        # Update catalog
        metadata = frontmatter.get("metadata", {}) or {}
        allowed_tools_raw = frontmatter.get("allowed-tools", "")
        if isinstance(allowed_tools_raw, str):
            allowed_tools = allowed_tools_raw.split() if allowed_tools_raw else []
        elif isinstance(allowed_tools_raw, list):
            allowed_tools = [str(t) for t in allowed_tools_raw]
        else:
            allowed_tools = []

        entry = SkillCatalogEntry(
            name=name,
            description=description,
            location=None,
            category=str(metadata.get("category", "")),
            icon=str(metadata.get("icon", "")),
            required_role=str(metadata.get("required-role", "user")),
            allowed_tools=allowed_tools,
            scope=scope,
            owner_id=owner_id,
        )
        self._catalog[catalog_key] = entry
        self._content_cache[catalog_key] = SkillContent(
            catalog=entry, instructions=body or "",
        )

        action_word = "Updated" if existing else "Created"
        return json.dumps({
            "status": action_word.lower(),
            "name": name,
            "description": description,
            "scope": scope,
        })

    def _is_admin_user(self, user_roles: list[str] | None) -> bool:
        """Check if user roles include admin-level access."""
        if self._acl_svc is None:
            return True  # No ACL = no restrictions
        if not user_roles:
            return False
        from gilbert.core.services.access_control import AccessControlService

        if not isinstance(self._acl_svc, AccessControlService):
            return True
        admin_level = self._acl_svc.get_role_level("admin")
        user_level = min(self._acl_svc.get_role_level(r) for r in user_roles)
        return user_level <= admin_level

    async def _tool_read_skill_file(self, arguments: dict[str, Any]) -> str:
        skill_name = arguments.get("skill_name", "")
        rel_path = arguments.get("path", "")
        user_id = arguments.get("_user_id", "system")

        entry = self._resolve_skill_entry(skill_name, user_id)
        if entry is None:
            return json.dumps({"error": f"Skill '{skill_name}' not found"})

        skill_dir = entry.location.parent
        target = (skill_dir / rel_path).resolve()

        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            return json.dumps({"error": "Path traversal not allowed"})

        if not target.is_file():
            return json.dumps({"error": f"File not found: {rel_path}"})

        try:
            content = await _to_thread(target.read_text, "utf-8")
            if len(content) > 50_000:
                content = content[:50_000] + "\n\n[... truncated at 50,000 characters]"
            return content
        except (OSError, UnicodeDecodeError) as exc:
            return json.dumps({"error": f"Cannot read file: {exc}"})

    async def _tool_run_skill_script(self, arguments: dict[str, Any]) -> str:
        skill_name = arguments.get("skill_name", "")
        script_path = arguments.get("script", "")
        script_args = arguments.get("arguments", []) or []
        user_id = arguments.get("_user_id", "system")

        entry = self._resolve_skill_entry(skill_name, user_id)
        if entry is None:
            return json.dumps({"error": f"Skill '{skill_name}' not found"})

        skill_dir = entry.location.parent
        workspace = self._get_workspace(user_id, skill_name)

        return await _to_thread(
            self._do_run_script, skill_dir, script_path, script_args, workspace,
        )

    async def _tool_browse_workspace(self, arguments: dict[str, Any]) -> str:
        skill_name = arguments.get("skill_name", "")
        user_id = arguments.get("_user_id", "system")

        workspace = self._get_workspace(user_id, skill_name)
        files = await _to_thread(self._list_workspace_files, workspace)
        return json.dumps({"workspace": str(workspace), "files": files})

    async def _tool_read_workspace_file(self, arguments: dict[str, Any]) -> str:
        skill_name = arguments.get("skill_name", "")
        rel_path = arguments.get("path", "")
        user_id = arguments.get("_user_id", "system")

        workspace = self._get_workspace(user_id, skill_name)
        target = (workspace / rel_path).resolve()

        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            return json.dumps({"error": "Path traversal not allowed"})

        if not target.is_file():
            return json.dumps({"error": f"File not found: {rel_path}"})

        try:
            content = await _to_thread(target.read_text, "utf-8")
            if len(content) > 50_000:
                content = content[:50_000] + "\n\n[... truncated at 50,000 characters]"
            return content
        except (OSError, UnicodeDecodeError) as exc:
            return json.dumps({"error": f"Cannot read file: {exc}"})

    def _resolve_skill_entry(
        self, skill_name: str, user_id: str,
    ) -> SkillCatalogEntry | None:
        """Resolve a skill by name, checking user-scoped first then global."""
        user_key = f"{user_id}:{skill_name}"
        entry = self._catalog.get(user_key)
        if entry is not None:
            return entry
        return self._catalog.get(skill_name)

    # ── WebSocket Handlers ───────────────────────────────────────────

    async def _ws_skills_list(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        user_ctx = getattr(conn, "user_ctx", None)
        catalog = self.get_catalog(user_ctx)
        return {
            "type": "skills.list.result",
            "ref": frame.get("id"),
            "skills": [
                {
                    "key": key,
                    "name": e.name,
                    "description": e.description,
                    "category": e.category,
                    "icon": e.icon,
                    "scope": e.scope,
                }
                for key, e in catalog
            ],
        }

    async def _ws_skills_active(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        conversation_id = frame.get("conversation_id", "")
        if not conversation_id:
            return {
                "type": "skills.conversation.active.result",
                "ref": frame.get("id"),
                "active_skills": [],
            }
        active = await self.get_active_skills(conversation_id)
        return {
            "type": "skills.conversation.active.result",
            "ref": frame.get("id"),
            "active_skills": active,
        }

    def _resolve_catalog_key(self, skill_name: str, user_id: str | None) -> str | None:
        """Find the catalog key for a skill name, checking user-scoped then global."""
        if user_id:
            user_key = f"{user_id}:{skill_name}"
            if user_key in self._catalog:
                return user_key
        if skill_name in self._catalog:
            return skill_name
        return None

    async def _ws_skills_toggle(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        conversation_id = frame.get("conversation_id", "")
        skill_name = frame.get("skill", "")
        enabled = frame.get("enabled", True)

        if not conversation_id or not skill_name:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "conversation_id and skill are required",
            }

        user_id = getattr(conn, "user_id", None)
        catalog_key = self._resolve_catalog_key(skill_name, user_id)
        if catalog_key is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 404,
                "error": f"Skill '{skill_name}' not found",
            }

        active = await self.get_active_skills(conversation_id)
        if enabled and catalog_key not in active:
            active.append(catalog_key)
        elif not enabled and catalog_key in active:
            active.remove(catalog_key)

        await self.set_active_skills(conversation_id, active)

        return {
            "type": "skills.conversation.toggle.result",
            "ref": frame.get("id"),
            "skill": skill_name,
            "enabled": enabled,
            "active_skills": active,
        }

    async def _ws_workspace_browse(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        user_id = getattr(conn, "user_id", "system")
        skill_name = frame.get("skill_name", "")
        if not skill_name:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "skill_name is required",
            }
        workspace = self._get_workspace(user_id, skill_name)
        files = await _to_thread(self._list_workspace_files, workspace)
        return {
            "type": "skills.workspace.browse.result",
            "ref": frame.get("id"),
            "skill_name": skill_name,
            "files": files,
        }

    async def _ws_workspace_download(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        user_id = getattr(conn, "user_id", "system")
        skill_name = frame.get("skill_name", "")
        rel_path = frame.get("path", "")

        if not skill_name or not rel_path:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "skill_name and path are required",
            }

        workspace = self._get_workspace(user_id, skill_name)
        target = (workspace / rel_path).resolve()

        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 403,
                "error": "Path traversal not allowed",
            }

        if not target.is_file():
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 404,
                "error": f"File not found: {rel_path}",
            }

        try:
            data = await _to_thread(target.read_bytes)
            return {
                "type": "skills.workspace.download.result",
                "ref": frame.get("id"),
                "skill_name": skill_name,
                "path": rel_path,
                "filename": target.name,
                "size": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        except OSError as exc:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 500,
                "error": f"Cannot read file: {exc}",
            }
