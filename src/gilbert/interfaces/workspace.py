"""Workspace protocol — capability interface for conversation file workspaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Protocol for managing per-conversation file workspaces.

    Workspaces organise files by purpose:

    - ``uploads/`` — files the user attached to the chat
    - ``outputs/`` — deliverables the AI produced for the user
    - ``scratch/`` — intermediate scripts, analysis artifacts, temp data
    """

    def get_workspace_root(self, user_id: str, conversation_id: str) -> Path:
        """Top-level workspace dir for a user × conversation pair.

        Returns (and creates)::

            .gilbert/workspaces/users/<user_id>/conversations/<conv_id>/
        """
        ...

    def get_upload_dir(self, user_id: str, conversation_id: str) -> Path:
        """Directory for user-uploaded files.

        Returns (and creates) ``<workspace_root>/uploads/``.
        """
        ...

    def get_output_dir(self, user_id: str, conversation_id: str) -> Path:
        """Directory for AI-produced deliverables.

        Returns (and creates) ``<workspace_root>/outputs/``.
        """
        ...

    def get_scratch_dir(self, user_id: str, conversation_id: str) -> Path:
        """Directory for intermediate/working files.

        Returns (and creates) ``<workspace_root>/scratch/``.
        """
        ...

    async def register_file(
        self,
        *,
        conversation_id: str,
        user_id: str,
        category: str,
        filename: str,
        rel_path: str,
        media_type: str,
        size: int,
        created_by: str = "ai",
        original_name: str = "",
        skill_name: str = "",
        description: str = "",
        derived_from: str | None = None,
        derivation_method: str | None = None,
        derivation_script: str | None = None,
        derivation_notes: str | None = None,
        reusable: bool = False,
    ) -> dict[str, Any]:
        """Register a file in the workspace file registry."""
        ...

    async def list_files(
        self, conversation_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """List registered files for a conversation."""
        ...

    async def build_workspace_manifest(self, conversation_id: str) -> str:
        """Build a system prompt fragment describing the conversation's files."""
        ...

    def resolve_file_path(
        self,
        user_id: str,
        rel_path: str,
        conversation_id: str | None,
    ) -> tuple[Path | None, str | None]:
        """Resolve a workspace-relative path to a real on-disk ``Path``.

        Returns ``(resolved_path, error_message)``. ``error_message`` is
        non-empty on failure (path traversal, file missing, etc.); the
        path is the absolute file location otherwise. Implementations
        should also try legacy layouts so old workspace references keep
        resolving as the on-disk shape evolves.
        """
        ...

    async def resolve_deliverable_for_dependent(
        self,
        *,
        file_id: str,
        viewing_agent_id: str,
        viewing_goal_id: str,
    ) -> tuple[Path | None, str | None]:
        """Resolve a workspace-file path for cross-goal viewing via a
        Deliverable + GoalDependency edge.

        Returns ``(path, None)`` iff the file is referenced by a READY
        Deliverable on a goal that ``viewing_goal_id`` has a satisfied
        ``GoalDependency`` on. Returns ``(None, error_message)`` on any
        rejection (file not registered as a deliverable; deliverable is
        DRAFT or OBSOLETE; no matching dependency edge; etc.).
        """
        ...
