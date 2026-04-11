"""Memory service — per-user persistent memories with AI tools.

Stores memories in the entity system, scoped by user. AI can remember,
recall, update, forget, and list memories for the current user.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_MEMORY_COLLECTION = "user_memories"


class MemoryService(Service):
    """Per-user persistent memories with AI tool access.

    Capabilities: user_memory, ai_tools
    """

    def __init__(self) -> None:
        self._storage: Any = None  # StorageBackend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="memory",
            capabilities=frozenset({"user_memory", "ai_tools"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.storage import IndexDefinition

        storage_svc = resolver.require_capability("entity_storage")
        self._storage = getattr(storage_svc, "backend", storage_svc)

        # Index for fast per-user lookups
        await self._storage.ensure_index(IndexDefinition(
            collection=_MEMORY_COLLECTION,
            fields=["user_id"],
        ))

        logger.info("Memory service started")

    # ── Public API ──────────────────────────────────────────────

    async def get_user_summaries(self, user_id: str) -> str:
        """Get formatted memory summaries for a user (for AI system prompt).

        Returns a string with one line per memory, or empty string if none.
        """
        memories = await self._get_user_memories(user_id)
        if not memories:
            return ""

        lines = [f"## Memories for this user ({len(memories)} stored)"]
        for m in memories:
            mid = m.get("_id", "")
            summary = m.get("summary", "")
            source = m.get("source", "user")
            lines.append(f"- [{mid}] {summary} ({source})")
        return "\n".join(lines)

    async def _get_user_memories(self, user_id: str) -> list[dict[str, Any]]:
        """Get all memories for a user, sorted by access count desc."""
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        memories = await self._storage.query(Query(
            collection=_MEMORY_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
        ))

        # Sort: user-created first, then by access count, then by recency
        def sort_key(m: dict[str, Any]) -> tuple[int, int, str]:
            source_rank = 0 if m.get("source") == "user" else 1
            access = -(m.get("access_count", 0))
            created = m.get("created_at", "")
            return (source_rank, access, created)

        memories.sort(key=sort_key)
        return memories

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "memory"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the AI memory system is enabled.",
                default=True, restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        pass

    # ── ToolProvider Protocol ───────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "memory"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="memory",
                description=(
                    "Manage persistent memories for the current user. "
                    "Use 'remember' when the user tells you something worth remembering "
                    "(preferences, project details, personal info). Use 'auto' source when "
                    "you notice something worth remembering that the user didn't explicitly ask to save. "
                    "Use 'list' to see what you remember about them. "
                    "Use 'recall' to load full content of specific memories by ID. "
                    "Use 'update' to modify a memory. Use 'forget' to delete one."
                ),
                parameters=[
                    ToolParameter(
                        name="action",
                        type=ToolParameterType.STRING,
                        description="Action to perform.",
                        enum=["remember", "recall", "update", "forget", "list"],
                    ),
                    ToolParameter(
                        name="summary",
                        type=ToolParameterType.STRING,
                        description="Short summary sentence (for remember, or update).",
                        required=False,
                    ),
                    ToolParameter(
                        name="content",
                        type=ToolParameterType.STRING,
                        description="Detailed memory content (for remember, or update).",
                        required=False,
                    ),
                    ToolParameter(
                        name="source",
                        type=ToolParameterType.STRING,
                        description="'user' if they explicitly asked to remember, 'auto' if you decided to.",
                        enum=["user", "auto"],
                        required=False,
                    ),
                    ToolParameter(
                        name="ids",
                        type=ToolParameterType.ARRAY,
                        description="Memory IDs to recall (for recall action).",
                        required=False,
                    ),
                    ToolParameter(
                        name="id",
                        type=ToolParameterType.STRING,
                        description="Memory ID (for update or forget).",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "memory":
            raise KeyError(f"Unknown tool: {name}")

        action = arguments.get("action", "")
        user = get_current_user()
        user_id = user.user_id

        if user_id in ("system", "guest"):
            return "Memory requires an authenticated user."

        match action:
            case "remember":
                return await self._remember(user_id, arguments)
            case "recall":
                return await self._recall(user_id, arguments)
            case "update":
                return await self._update(user_id, arguments)
            case "forget":
                return await self._forget(user_id, arguments)
            case "list":
                return await self._list(user_id)
            case _:
                return f"Unknown memory action: {action}"

    async def _remember(self, user_id: str, args: dict[str, Any]) -> str:
        summary = args.get("summary", "").strip()
        content = args.get("content", "").strip()
        source = args.get("source", "user")

        if not summary:
            return "I need a summary to remember."
        if not content:
            content = summary

        now = datetime.now(timezone.utc).isoformat()
        memory_id = f"memory_{uuid.uuid4().hex[:12]}"

        await self._storage.put(_MEMORY_COLLECTION, memory_id, {
            "memory_id": memory_id,
            "user_id": user_id,
            "summary": summary,
            "content": content,
            "source": source,
            "access_count": 0,
            "created_at": now,
            "updated_at": now,
        })

        logger.info("Memory created for %s: %s", user_id, summary[:60])
        return f"Got it, I'll remember that. (memory {memory_id})"

    async def _recall(self, user_id: str, args: dict[str, Any]) -> str:
        ids: list[str] = args.get("ids", [])
        if not ids:
            return "I need memory IDs to recall. Use 'list' first to see available memories."

        results: list[str] = []
        for mid in ids:
            mid = str(mid)
            record = await self._storage.get(_MEMORY_COLLECTION, mid)
            if not record:
                results.append(f"[{mid}] Not found.")
                continue
            if record.get("user_id") != user_id:
                results.append(f"[{mid}] Not your memory.")
                continue

            # Bump access count
            record["access_count"] = record.get("access_count", 0) + 1
            await self._storage.put(_MEMORY_COLLECTION, mid, record)

            results.append(
                f"[{mid}] {record.get('summary', '')}\n"
                f"Content: {record.get('content', '')}\n"
                f"Source: {record.get('source', 'user')} | "
                f"Created: {record.get('created_at', '')} | "
                f"Accessed: {record['access_count']} times"
            )

        return "\n\n".join(results)

    async def _update(self, user_id: str, args: dict[str, Any]) -> str:
        memory_id = args.get("id", "")
        if not memory_id:
            return "I need a memory ID to update."

        record = await self._storage.get(_MEMORY_COLLECTION, str(memory_id))
        if not record:
            return f"Memory {memory_id} not found."
        if record.get("user_id") != user_id:
            return f"Memory {memory_id} doesn't belong to you."

        summary = args.get("summary")
        content = args.get("content")

        if summary:
            record["summary"] = summary
        if content:
            record["content"] = content
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

        await self._storage.put(_MEMORY_COLLECTION, str(memory_id), record)
        logger.info("Memory updated for %s: %s", user_id, memory_id)
        return f"Memory {memory_id} updated."

    async def _forget(self, user_id: str, args: dict[str, Any]) -> str:
        memory_id = args.get("id", "")
        if not memory_id:
            return "I need a memory ID to forget."

        record = await self._storage.get(_MEMORY_COLLECTION, str(memory_id))
        if not record:
            return f"Memory {memory_id} not found."
        if record.get("user_id") != user_id:
            return f"Memory {memory_id} doesn't belong to you."

        await self._storage.delete(_MEMORY_COLLECTION, str(memory_id))
        logger.info("Memory forgotten for %s: %s", user_id, memory_id)
        return f"Memory {memory_id} forgotten."

    async def _list(self, user_id: str) -> str:
        memories = await self._get_user_memories(user_id)
        if not memories:
            return "No memories stored for you yet."

        lines = [f"{len(memories)} memory/memories stored:"]
        for m in memories:
            mid = m.get("_id", "")
            summary = m.get("summary", "")
            source = m.get("source", "user")
            access = m.get("access_count", 0)
            lines.append(f"  [{mid}] {summary} ({source}) — accessed {access}x")

        return "\n".join(lines)
