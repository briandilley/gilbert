"""Skill system data model — based on the Agent Skills open standard (agentskills.io)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SkillCatalogEntry:
    """Tier 1 metadata — cheap to load, used for listing and matching."""

    name: str
    description: str
    location: Path | None = None
    category: str = ""
    icon: str = ""
    required_role: str = "user"
    allowed_tools: list[str] = field(default_factory=list)
    scope: str = "global"
    owner_id: str = ""


@dataclass(frozen=True)
class SkillContent:
    """Tier 2 — full instructions loaded on activation."""

    catalog: SkillCatalogEntry
    instructions: str
    resources: list[str] = field(default_factory=list)


@runtime_checkable
class SkillsProvider(Protocol):
    """Protocol for querying active skills from a service."""

    async def get_active_skills(self, conversation_id: str) -> list[str]:
        """Get active skill names for a conversation."""
        ...

    def get_active_allowed_tools(self, active_skills: list[str]) -> set[str]:
        """Get the set of tool names allowed by the given active skills."""
        ...

    async def build_skills_context(self, conversation_id: str) -> str:
        """Build a system prompt fragment describing active skills."""
        ...

    def get_workspace_path(
        self,
        user_id: str,
        skill_name: str,
        conversation_id: str | None = None,
    ) -> Path:
        """Resolve (and create) a workspace directory.

        Returns the per-conversation workspace when ``conversation_id``
        is set, the legacy per-user workspace otherwise. Creates the
        directory on demand. Callers outside of tool handlers (HTTP
        upload endpoints, sync scripts, …) use this to land files in
        the same tree that tools read from. The ``skill_name`` need
        not be a registered skill — synthetic names like
        ``"chat-uploads"`` are allowed for content that belongs under
        a particular conversation's workspace but isn't tied to any
        real skill.
        """
        ...
