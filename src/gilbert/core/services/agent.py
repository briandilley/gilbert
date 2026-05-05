"""AgentService — durable agent identity, lifecycle, and run orchestration.

This service owns the Agent, AgentMemory, AgentTrigger, Commitment,
InboxSignal, and Run entity collections. It exposes:

- CRUD for agents and related entities (Task 5 / Task 7 / Task 9).
- Agent run orchestration via ``run_agent_now`` (Task 8).
- Heartbeat re-arming via the scheduler (Task 10).
- Inbox signal dispatch (Task 11).
- WS RPC handlers for the SPA (Task 6).
- AI tool definitions (Task 14).

Task 3 establishes the skeleton: start/stop lifecycle, service_info,
and NotImplementedError stubs for the four AgentProvider methods.
Task 5 implements CRUD + RBAC helper.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.interfaces.agent import (
    Agent,
    AgentMemory,
    AgentStatus,
    Commitment,
    InboxSignal,
    MemoryState,
    Run,
    RunStatus,
)
from gilbert.interfaces.ai import AIProvider, AIToolDiscoveryProvider
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Filter, FilterOp, Query, StorageBackend, StorageProvider
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType

logger = logging.getLogger(__name__)

# ── Name validation ──────────────────────────────────────────────────

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# ── Default ConfigParam values ───────────────────────────────────────

_DEFAULT_PERSONA = "You are an autonomous AI agent."
_DEFAULT_SYSTEM_PROMPT = (
    "Take whatever action is appropriate to advance the goals you have "
    "been assigned. Use your tools deliberately. End your turn briefly "
    "when there is nothing pressing."
)
_DEFAULT_PROCEDURAL_RULES = (
    "When you ask a question or need user input, MUST call "
    "request_user_input first so the user gets a notification.\n\n"
    "When you make a follow-up commitment, call commitment_create.\n\n"
    "When you learn a durable fact about the user or their context, "
    "call agent_memory_save with kind='preference' or kind='fact'."
)
_DEFAULT_HEARTBEAT_CHECKLIST = (
    "1. Are there any due commitments to action?\n"
    "2. Anything inbound in your inbox you haven't seen?\n"
    "3. Any goals assigned to you that are blocked?\n"
    "4. If nothing pressing, end your turn briefly."
)
_DEFAULT_TOOL_GROUPS: dict[str, list[str]] = {
    "files": ["read_workspace_file", "write_skill_workspace_file", "run_workspace_script"],
    "knowledge": ["search_knowledge"],
    "communication": ["notify_user"],
    "self": ["agent_memory_save", "agent_memory_search", "commitment_create", "commitment_complete"],
}

# ── Collection names ─────────────────────────────────────────────────

_AGENTS_COLLECTION = "agents"
_AGENT_MEMORIES_COLLECTION = "agent_memories"
_AGENT_TRIGGERS_COLLECTION = "agent_triggers"
_AGENT_COMMITMENTS_COLLECTION = "agent_commitments"
_AGENT_INBOX_SIGNALS_COLLECTION = "agent_inbox_signals"
_AGENT_RUNS_COLLECTION = "agent_runs"
_AI_CALL_NAME = "agent.run"

_CORE_AGENT_TOOLS: frozenset[str] = frozenset({
    # Phase 1A — agent self-management
    "complete_run",
    "request_user_input",
    "notify_user",
    "commitment_create",
    "commitment_complete",
    "commitment_list",
    "agent_memory_save",
    "agent_memory_search",
    "agent_memory_review_and_promote",
    # Phase 2 will add agent_list, agent_send_message, agent_delegate.
    # Phase 4 will add goal_post.
})


# ── ToolDefinitions (Task 14) ────────────────────────────────────────

_TOOL_COMPLETE_RUN = ToolDefinition(
    name="complete_run",
    description=(
        "Flag the current agent run as having met its success criteria. "
        "Use this when you've completed the work you were triggered for "
        "and have nothing else to do this turn. Reason is logged onto the "
        "Run entity."
    ),
    parameters=[
        ToolParameter(
            name="reason",
            type=ToolParameterType.STRING,
            description="One-line success reason logged onto the Run.",
            required=True,
        ),
    ],
    slash_command="complete_run",
    slash_help="Mark the current run as successfully complete.",
)

_TOOL_COMMITMENT_CREATE = ToolDefinition(
    name="commitment_create",
    description=(
        "Create a follow-up commitment for yourself. Surfaces in the "
        "next heartbeat whose schedule is at-or-after due_at."
    ),
    parameters=[
        ToolParameter(
            name="content",
            type=ToolParameterType.STRING,
            description="What to follow up on",
            required=True,
        ),
        ToolParameter(
            name="due_in_seconds",
            type=ToolParameterType.NUMBER,
            description="Surface at-or-after this many seconds from now.",
            required=False,
        ),
        ToolParameter(
            name="due_at",
            type=ToolParameterType.STRING,
            description="ISO-8601 absolute time alternative to due_in_seconds.",
            required=False,
        ),
    ],
)

_TOOL_COMMITMENT_COMPLETE = ToolDefinition(
    name="commitment_complete",
    description="Mark a previously-created commitment as complete.",
    parameters=[
        ToolParameter(
            name="commitment_id",
            type=ToolParameterType.STRING,
            description="The commitment id.",
            required=True,
        ),
        ToolParameter(
            name="note",
            type=ToolParameterType.STRING,
            description="Optional completion note.",
            required=False,
        ),
    ],
)

_TOOL_COMMITMENT_LIST = ToolDefinition(
    name="commitment_list",
    description="List your commitments. By default only unfinished ones.",
    parameters=[
        ToolParameter(
            name="include_completed",
            type=ToolParameterType.BOOLEAN,
            description="Include already-completed commitments.",
            required=False,
        ),
    ],
)

_TOOL_AGENT_MEMORY_SAVE = ToolDefinition(
    name="agent_memory_save",
    description=(
        "Save a learned fact to your own memory. SHORT_TERM by default; "
        "use kind='preference' or kind='decision' or kind='fact' as "
        "appropriate. Tags are free-form."
    ),
    parameters=[
        ToolParameter(
            name="content",
            type=ToolParameterType.STRING,
            description="The memory text.",
            required=True,
        ),
        ToolParameter(
            name="kind",
            type=ToolParameterType.STRING,
            description="'fact' | 'preference' | 'decision' | 'daily' | 'dream'.",
            required=False,
        ),
        ToolParameter(
            name="tags",
            type=ToolParameterType.ARRAY,
            description="Free-form tags.",
            required=False,
        ),
    ],
)

_TOOL_AGENT_MEMORY_SEARCH = ToolDefinition(
    name="agent_memory_search",
    description="Search your own memories by substring match. Recency-ordered.",
    parameters=[
        ToolParameter(
            name="query",
            type=ToolParameterType.STRING,
            description="Substring to match. Empty = recent.",
            required=False,
        ),
        ToolParameter(
            name="limit",
            type=ToolParameterType.NUMBER,
            description="Max results (default 20).",
            required=False,
        ),
    ],
)

_TOOL_AGENT_MEMORY_PROMOTE = ToolDefinition(
    name="agent_memory_review_and_promote",
    description=(
        "Review recent SHORT_TERM memories and promote durable ones to "
        "LONG_TERM with a score. Pass an array of {memory_id, score, "
        "decision} triplets (decision='promote'|'demote'|'keep')."
    ),
    parameters=[
        ToolParameter(
            name="reviews",
            type=ToolParameterType.ARRAY,
            description="List of {memory_id, score, decision}.",
            required=True,
        ),
    ],
)


# ── Module-level helpers ─────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _agent_to_dict(a: Agent) -> dict[str, Any]:
    """Storage row representation. ``status`` serializes as .value; datetimes as ISO."""
    return {
        "_id": a.id,
        "owner_user_id": a.owner_user_id,
        "name": a.name,
        "role_label": a.role_label,
        "persona": a.persona,
        "system_prompt": a.system_prompt,
        "procedural_rules": a.procedural_rules,
        "profile_id": a.profile_id,
        "conversation_id": a.conversation_id,
        "status": a.status.value,
        "avatar_kind": a.avatar_kind,
        "avatar_value": a.avatar_value,
        "lifetime_cost_usd": a.lifetime_cost_usd,
        "cost_cap_usd": a.cost_cap_usd,
        "tools_allowed": a.tools_allowed,
        "heartbeat_enabled": a.heartbeat_enabled,
        "heartbeat_interval_s": a.heartbeat_interval_s,
        "heartbeat_checklist": a.heartbeat_checklist,
        "dream_enabled": a.dream_enabled,
        "dream_quiet_hours": a.dream_quiet_hours,
        "dream_probability": a.dream_probability,
        "dream_max_per_night": a.dream_max_per_night,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _agent_from_dict(row: dict[str, Any]) -> Agent:
    return Agent(
        id=row["_id"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        role_label=row.get("role_label", ""),
        persona=row.get("persona", ""),
        system_prompt=row.get("system_prompt", ""),
        procedural_rules=row.get("procedural_rules", ""),
        profile_id=row.get("profile_id", "standard"),
        conversation_id=row.get("conversation_id", ""),
        status=AgentStatus(row.get("status", "enabled")),
        avatar_kind=row.get("avatar_kind", "emoji"),
        avatar_value=row.get("avatar_value", "🤖"),
        lifetime_cost_usd=float(row.get("lifetime_cost_usd", 0.0)),
        cost_cap_usd=row.get("cost_cap_usd"),
        tools_allowed=row.get("tools_allowed"),
        heartbeat_enabled=bool(row.get("heartbeat_enabled", True)),
        heartbeat_interval_s=int(row.get("heartbeat_interval_s", 1800)),
        heartbeat_checklist=row.get("heartbeat_checklist", ""),
        dream_enabled=bool(row.get("dream_enabled", False)),
        dream_quiet_hours=row.get("dream_quiet_hours", "22:00-06:00"),
        dream_probability=float(row.get("dream_probability", 0.1)),
        dream_max_per_night=int(row.get("dream_max_per_night", 3)),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _memory_to_dict(m: AgentMemory) -> dict[str, Any]:
    return {
        "_id": m.id,
        "agent_id": m.agent_id,
        "content": m.content,
        "state": m.state.value,
        "kind": m.kind,
        "tags": sorted(m.tags),
        "score": m.score,
        "created_at": m.created_at.isoformat(),
        "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
    }


def _memory_from_dict(row: dict[str, Any]) -> AgentMemory:
    return AgentMemory(
        id=row["_id"],
        agent_id=row["agent_id"],
        content=row.get("content", ""),
        state=MemoryState(row.get("state", "short_term")),
        kind=row.get("kind", "fact"),
        tags=frozenset(row.get("tags", [])),
        score=float(row.get("score", 0.0)),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=(
            datetime.fromisoformat(row["last_used_at"])
            if row.get("last_used_at") else None
        ),
    )


def _run_to_dict(r: Run) -> dict[str, Any]:
    return {
        "_id": r.id,
        "agent_id": r.agent_id,
        "triggered_by": r.triggered_by,
        "trigger_context": r.trigger_context,
        "started_at": r.started_at.isoformat(),
        "status": r.status.value,
        "conversation_id": r.conversation_id,
        "delegation_id": r.delegation_id,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "final_message_text": r.final_message_text,
        "rounds_used": r.rounds_used,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "cost_usd": r.cost_usd,
        "error": r.error,
        "awaiting_user_input": r.awaiting_user_input,
        "pending_question": r.pending_question,
        "pending_actions": list(r.pending_actions),
    }


def _run_from_dict(row: dict[str, Any]) -> Run:
    return Run(
        id=row["_id"],
        agent_id=row["agent_id"],
        triggered_by=row.get("triggered_by", "manual"),
        trigger_context=row.get("trigger_context", {}),
        started_at=datetime.fromisoformat(row["started_at"]),
        status=RunStatus(row.get("status", "running")),
        conversation_id=row.get("conversation_id", ""),
        delegation_id=row.get("delegation_id", ""),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else None,
        final_message_text=row.get("final_message_text"),
        rounds_used=int(row.get("rounds_used", 0)),
        tokens_in=int(row.get("tokens_in", 0)),
        tokens_out=int(row.get("tokens_out", 0)),
        cost_usd=float(row.get("cost_usd", 0.0)),
        error=row.get("error"),
        awaiting_user_input=bool(row.get("awaiting_user_input", False)),
        pending_question=row.get("pending_question"),
        pending_actions=list(row.get("pending_actions", [])),
    )


def _signal_to_dict(s: InboxSignal) -> dict[str, Any]:
    return {
        "_id": s.id,
        "agent_id": s.agent_id,
        "signal_kind": s.signal_kind,
        "body": s.body,
        "sender_kind": s.sender_kind,
        "sender_id": s.sender_id,
        "sender_name": s.sender_name,
        "source_conv_id": s.source_conv_id,
        "source_message_id": s.source_message_id,
        "delegation_id": s.delegation_id,
        "metadata": s.metadata,
        "priority": s.priority,
        "created_at": s.created_at.isoformat(),
        "processed_at": s.processed_at.isoformat() if s.processed_at else None,
    }


def _signal_from_dict(row: dict[str, Any]) -> InboxSignal:
    return InboxSignal(
        id=row["_id"],
        agent_id=row["agent_id"],
        signal_kind=row.get("signal_kind", "inbox"),
        body=row.get("body", ""),
        sender_kind=row.get("sender_kind", "user"),
        sender_id=row.get("sender_id", ""),
        sender_name=row.get("sender_name", ""),
        source_conv_id=row.get("source_conv_id", ""),
        source_message_id=row.get("source_message_id", ""),
        delegation_id=row.get("delegation_id", ""),
        metadata=row.get("metadata", {}),
        priority=row.get("priority", "normal"),
        created_at=datetime.fromisoformat(row["created_at"]),
        processed_at=(
            datetime.fromisoformat(row["processed_at"])
            if row.get("processed_at") else None
        ),
    )


def _commitment_from_dict(row: dict[str, Any]) -> Commitment:
    return Commitment(
        id=row["_id"],
        agent_id=row["agent_id"],
        content=row.get("content", ""),
        due_at=datetime.fromisoformat(row["due_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
        completion_note=row.get("completion_note", ""),
    )


def _commitment_to_dict(c: Commitment) -> dict[str, Any]:
    return {
        "_id": c.id,
        "agent_id": c.agent_id,
        "content": c.content,
        "due_at": c.due_at.isoformat(),
        "created_at": c.created_at.isoformat(),
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "completion_note": c.completion_note,
    }


class AgentService(Service):
    """Manages durable agent identities and run orchestration.

    Capabilities declared:

    - ``agent`` — satisfies ``AgentProvider``.
    - ``ai_tools`` — exposes AI tool definitions (Task 14).
    - ``ws_handlers`` — exposes RPC handlers (Task 6).

    Requires:

    - ``entity_storage`` — persists all agent entity collections.
    - ``event_bus`` — publishes state-change events.
    - ``ai_chat`` — drives agent runs.
    - ``scheduler`` — re-arms heartbeat triggers.
    """

    tool_provider_name = "agent"
    config_namespace = "agent_service"
    config_category = "Intelligence"
    slash_namespace = "agents"

    def __init__(self) -> None:
        # Entity storage backend (bound in start())
        self._storage: StorageBackend | None = None

        # Raw EventBus instance from EventBusProvider (bound in start())
        self._event_bus: Any = None

        # AIProvider capability (bound in start())
        self._ai: AIProvider | None = None

        # AIToolDiscoveryProvider capability (bound in start())
        # — used by agents.tools.list_available to enumerate tools.
        self._tool_discovery: AIToolDiscoveryProvider | None = None

        # ServiceResolver reference for late-bound capability lookups
        self._resolver: ServiceResolver | None = None

        # SchedulerProvider capability (bound in start())
        self._scheduler: SchedulerProvider | None = None

        # Agent IDs that currently have a run in progress
        self._running_agents: set[str] = set()

        # Per-agent inbox queues: agent_id → list of pending InboxSignals
        self._inboxes: dict[str, list[InboxSignal]] = {}

        # Service-level defaults merged into create_agent calls
        self._defaults: dict[str, Any] = {}

    # ── Configurable ────────────────────────────────────────────────

    def config_params(self) -> list[ConfigParam]:
        """Describe all operator-tunable defaults for new agents."""
        return [
            ConfigParam(
                key="default_persona",
                type=ToolParameterType.STRING,
                description="Default persona text injected into new agents' system prompt.",
                default=_DEFAULT_PERSONA,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_system_prompt",
                type=ToolParameterType.STRING,
                description="Default system-prompt body for new agents.",
                default=_DEFAULT_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_procedural_rules",
                type=ToolParameterType.STRING,
                description="Default procedural rules injected into new agents' system prompt.",
                default=_DEFAULT_PROCEDURAL_RULES,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_heartbeat_interval_s",
                type=ToolParameterType.NUMBER,
                description="Default heartbeat interval in seconds for new agents.",
                default=1800,
            ),
            ConfigParam(
                key="default_heartbeat_checklist",
                type=ToolParameterType.STRING,
                description="Default heartbeat checklist for new agents.",
                default=_DEFAULT_HEARTBEAT_CHECKLIST,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_dream_enabled",
                type=ToolParameterType.BOOLEAN,
                description="Whether dreaming is enabled by default for new agents.",
                default=False,
            ),
            ConfigParam(
                key="default_dream_quiet_hours",
                type=ToolParameterType.STRING,
                description="Default dream quiet-hours window for new agents (e.g. '22:00-06:00').",
                default="22:00-06:00",
            ),
            ConfigParam(
                key="default_dream_probability",
                type=ToolParameterType.NUMBER,
                description="Default probability (0–1) that a dream run fires in each heartbeat.",
                default=0.1,
            ),
            ConfigParam(
                key="default_dream_max_per_night",
                type=ToolParameterType.INTEGER,
                description="Default maximum dream runs allowed per night for new agents.",
                default=3,
            ),
            ConfigParam(
                key="default_profile_id",
                type=ToolParameterType.STRING,
                description="Default AI profile ID used for new agents.",
                default="standard",
            ),
            ConfigParam(
                key="default_avatar_kind",
                type=ToolParameterType.STRING,
                description="Default avatar kind for new agents (e.g. 'emoji', 'url').",
                default="emoji",
            ),
            ConfigParam(
                key="default_avatar_value",
                type=ToolParameterType.STRING,
                description="Default avatar value for new agents (emoji character or image URL).",
                default="🤖",
            ),
            ConfigParam(
                key="default_tools_allowed",
                type=ToolParameterType.STRING,
                description=(
                    "Comma-separated list of tool names new agents are allowed to call. "
                    "Leave empty to allow all tools."
                ),
                default="",
            ),
            ConfigParam(
                key="tool_groups",
                type=ToolParameterType.OBJECT,
                description=(
                    "JSON object grouping tool names by category for the UI. "
                    "Keys are group labels; values are lists of tool names."
                ),
                default=_DEFAULT_TOOL_GROUPS,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Cache the full config section as ``_defaults``.

        Normalizes ``default_tools_allowed``: an empty string is stored as
        ``None`` (meaning "all tools allowed"); a non-empty string is split on
        commas into a list.
        """
        self._defaults = dict(config)
        raw_allowed = config.get("default_tools_allowed", "")
        if isinstance(raw_allowed, str):
            stripped = raw_allowed.strip()
            self._defaults["default_tools_allowed"] = (
                None if not stripped else [t.strip() for t in stripped.split(",")]
            )

    # ── Service lifecycle ────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="agent",
            capabilities=frozenset({"agent", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "event_bus", "ai_chat", "scheduler"}),
            ai_calls=frozenset({_AI_CALL_NAME}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        """Bind capabilities and prepare the service for requests."""
        self._resolver = resolver

        # Bind entity storage
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError(
                "entity_storage capability does not implement StorageProvider"
            )
        self._storage = storage_svc.backend

        # Bind event bus
        event_bus_svc = resolver.require_capability("event_bus")
        if not isinstance(event_bus_svc, EventBusProvider):
            raise RuntimeError(
                "event_bus capability does not implement EventBusProvider"
            )
        self._event_bus = event_bus_svc.bus

        # Bind AI chat capability
        ai_svc = resolver.require_capability("ai_chat")
        if not isinstance(ai_svc, AIProvider):
            raise RuntimeError(
                "ai_chat capability does not implement AIProvider"
            )
        self._ai = ai_svc

        # The same service must also expose AIToolDiscoveryProvider so the
        # agents.tools.list_available handler can enumerate tools without
        # importing the concrete AI service. AIService satisfies both, so
        # this is a hard requirement.
        if not isinstance(ai_svc, AIToolDiscoveryProvider):
            raise RuntimeError(
                "ai_chat capability does not implement AIToolDiscoveryProvider"
            )
        self._tool_discovery = ai_svc

        # Bind scheduler capability
        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise RuntimeError(
                "scheduler capability does not implement SchedulerProvider"
            )
        self._scheduler = scheduler_svc

        # Task 5: index creation goes here.
        # Task 8: run rehydration goes here.

        # Re-arm heartbeats for every ENABLED agent on service start.
        rows = await self._storage.query(
            Query(collection=_AGENTS_COLLECTION, filters=[])
        )
        for r in rows:
            a = _agent_from_dict(r)
            if a.status is AgentStatus.ENABLED and a.heartbeat_enabled:
                await self._arm_heartbeat(a)

        # Restore unprocessed inbox signals into in-memory cache.
        await self._rehydrate_inboxes()

        logger.info("AgentService started")

    async def stop(self) -> None:
        """Graceful shutdown — disarm all heartbeat scheduler jobs."""
        self._inboxes.clear()
        if self._storage:
            rows = await self._storage.query(Query(collection=_AGENTS_COLLECTION, filters=[]))
            for r in rows:
                await self._disarm_heartbeat(r["_id"])
        logger.info("AgentService stopped")

    # ── AgentProvider — CRUD (Task 5) ───────────────────────────────

    async def create_agent(
        self,
        *,
        owner_user_id: str,
        name: str,
        **fields: Any,
    ) -> Agent:
        """Create and persist a new Agent entity."""
        if self._storage is None:
            raise RuntimeError("not started")
        if not _NAME_PATTERN.match(name):
            raise ValueError(f"name {name!r} must match {_NAME_PATTERN.pattern}")

        # Uniqueness: same-owner, same-name collision rejected.
        existing = await self._storage.query(
            Query(
                collection=_AGENTS_COLLECTION,
                filters=[
                    Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id),
                    Filter(field="name", op=FilterOp.EQ, value=name),
                ],
            )
        )
        if existing:
            raise ValueError(f"name already in use: {name}")

        defaults = self._defaults
        now = _now()
        a = Agent(
            id=f"ag_{uuid.uuid4().hex[:12]}",
            owner_user_id=owner_user_id,
            name=name,
            role_label=fields.get("role_label", ""),
            persona=fields.get("persona", defaults.get("default_persona", "")),
            system_prompt=fields.get("system_prompt", defaults.get("default_system_prompt", "")),
            procedural_rules=fields.get("procedural_rules", defaults.get("default_procedural_rules", "")),
            profile_id=fields.get("profile_id", defaults.get("default_profile_id", "standard")),
            conversation_id="",
            status=AgentStatus.ENABLED,
            avatar_kind=fields.get("avatar_kind", defaults.get("default_avatar_kind", "emoji")),
            avatar_value=fields.get("avatar_value", defaults.get("default_avatar_value", "🤖")),
            lifetime_cost_usd=0.0,
            cost_cap_usd=fields.get("cost_cap_usd"),
            tools_allowed=fields.get("tools_allowed", defaults.get("default_tools_allowed")),
            heartbeat_enabled=fields.get("heartbeat_enabled", True),
            heartbeat_interval_s=fields.get(
                "heartbeat_interval_s",
                int(defaults.get("default_heartbeat_interval_s", 1800)),
            ),
            heartbeat_checklist=fields.get("heartbeat_checklist", defaults.get("default_heartbeat_checklist", "")),
            dream_enabled=fields.get("dream_enabled", bool(defaults.get("default_dream_enabled", False))),
            dream_quiet_hours=fields.get("dream_quiet_hours", defaults.get("default_dream_quiet_hours", "22:00-06:00")),
            dream_probability=fields.get(
                "dream_probability",
                float(defaults.get("default_dream_probability", 0.1)),
            ),
            dream_max_per_night=fields.get(
                "dream_max_per_night",
                int(defaults.get("default_dream_max_per_night", 3)),
            ),
            created_at=now,
            updated_at=now,
        )
        await self._storage.put(_AGENTS_COLLECTION, a.id, _agent_to_dict(a))
        await self._arm_heartbeat(a)
        await self._publish("agent.created", {"agent_id": a.id, "owner_user_id": a.owner_user_id})
        return a

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch an Agent by ID. Returns None if not found."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return None
        return _agent_from_dict(row)

    async def list_agents(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Agent]:
        """List agents, optionally filtered by owner."""
        if self._storage is None:
            raise RuntimeError("not started")
        filters = (
            []
            if owner_user_id is None
            else [Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id)]
        )
        rows = await self._storage.query(Query(collection=_AGENTS_COLLECTION, filters=filters))
        return [_agent_from_dict(r) for r in rows]

    async def update_agent(self, agent_id: str, patch: dict[str, Any]) -> Agent:
        """Apply a partial update to an agent. Only known fields may be patched."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            raise KeyError(agent_id)
        _allowed_patch_fields = {
            "role_label", "persona", "system_prompt", "procedural_rules",
            "profile_id", "avatar_kind", "avatar_value", "cost_cap_usd",
            "tools_allowed", "heartbeat_enabled", "heartbeat_interval_s",
            "heartbeat_checklist", "dream_enabled", "dream_quiet_hours",
            "dream_probability", "dream_max_per_night", "status",
        }
        for k, v in patch.items():
            if k not in _allowed_patch_fields:
                raise ValueError(f"field not patchable: {k}")
            if k == "status":
                # Coerce to AgentStatus to validate, then store the canonical
                # string value. Accepts either an AgentStatus or its .value.
                row[k] = AgentStatus(v).value if not isinstance(v, AgentStatus) else v.value
            else:
                row[k] = v
        row["updated_at"] = _now().isoformat()
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)
        updated = _agent_from_dict(row)
        # Re-arm (or disarm) heartbeat whenever any agent field changes.
        # A DISABLED agent must never have an armed heartbeat, regardless of
        # heartbeat_enabled. _arm_heartbeat is idempotent and a no-op when
        # heartbeat_enabled=False.
        if updated.status is AgentStatus.ENABLED and updated.heartbeat_enabled:
            await self._arm_heartbeat(updated)
        else:
            await self._disarm_heartbeat(agent_id)
        await self._publish("agent.updated", {"agent_id": updated.id})
        return updated

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete the agent and cascade-delete its memories, triggers,
        commitments, inbox signals, and runs.

        Returns True if the agent existed and was deleted, False if not found.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return False
        await self._disarm_heartbeat(agent_id)
        await self._storage.delete(_AGENTS_COLLECTION, agent_id)
        # Cascade-delete related collections.
        for coll in (
            _AGENT_MEMORIES_COLLECTION,
            _AGENT_TRIGGERS_COLLECTION,
            _AGENT_COMMITMENTS_COLLECTION,
            _AGENT_INBOX_SIGNALS_COLLECTION,
            _AGENT_RUNS_COLLECTION,
        ):
            related = await self._storage.query(
                Query(
                    collection=coll,
                    filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
                )
            )
            for r in related:
                await self._storage.delete(coll, r["_id"])
        await self._publish("agent.deleted", {"agent_id": agent_id})
        return True

    # ── Heartbeat scheduling (Task 10) ──────────────────────────────

    async def _arm_heartbeat(self, a: Agent) -> None:
        """Register a heartbeat scheduler job for this agent.

        Idempotent: removes any existing job first, then adds a fresh one.
        A no-op if the scheduler is not bound or heartbeat is disabled.
        """
        if self._scheduler is None or not a.heartbeat_enabled:
            return
        job_name = f"heartbeat_{a.id}"

        async def _cb() -> None:
            await self._on_heartbeat_fired(a.id)

        try:
            self._scheduler.remove_job(job_name)
        except Exception:
            pass
        self._scheduler.add_job(
            name=job_name,
            schedule=Schedule.every(a.heartbeat_interval_s),
            callback=_cb,
            system=True,
        )

    async def _disarm_heartbeat(self, agent_id: str) -> None:
        """Remove the heartbeat scheduler job for *agent_id*, if any."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(f"heartbeat_{agent_id}")
        except Exception:
            pass

    async def _on_heartbeat_fired(self, agent_id: str) -> None:
        """Scheduler callback — fire a heartbeat run if the agent is
        still ENABLED and not already running."""
        a = await self.get_agent(agent_id)
        if a is None or a.status is not AgentStatus.ENABLED:
            await self._disarm_heartbeat(agent_id)
            return
        if agent_id in self._running_agents:
            # In-flight run; skip silently. The heartbeat re-fires next interval.
            return
        try:
            self._running_agents.add(agent_id)
            await self._run_agent_internal(
                a, triggered_by="heartbeat",
                trigger_context={}, user_message=None,
            )
        finally:
            self._running_agents.discard(agent_id)

    # ── InboxSignal dispatch (Task 11) ──────────────────────────────────

    async def _signal_agent(
        self,
        *,
        agent_id: str,
        signal_kind: str,
        body: str,
        sender_kind: str,
        sender_id: str,
        sender_name: str,
        source_conv_id: str = "",
        source_message_id: str = "",
        delegation_id: str = "",
        metadata: dict[str, Any] | None = None,
        priority: str = "normal",
    ) -> InboxSignal:
        """Create, persist, and dispatch an InboxSignal for *agent_id*.

        If the agent is currently idle (not in ``_running_agents``) and
        ENABLED, a new run is spawned via ``asyncio.create_task``; the
        dispatcher returns immediately without waiting for the run to finish.

        If the agent is busy, the signal is enqueued in the in-memory cache
        and persisted; the next round will drain it via ``_drain_inbox``.
        """
        if self._storage is None:
            raise RuntimeError("not started")

        sig = InboxSignal(
            id=f"sig_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            signal_kind=signal_kind,
            body=body,
            sender_kind=sender_kind,
            sender_id=sender_id,
            sender_name=sender_name,
            source_conv_id=source_conv_id,
            source_message_id=source_message_id,
            delegation_id=delegation_id,
            metadata=metadata or {},
            priority=priority,
            created_at=_now(),
            processed_at=None,
        )

        # Persist before touching in-memory state so restart survives a crash here.
        await self._storage.put(
            _AGENT_INBOX_SIGNALS_COLLECTION, sig.id, _signal_to_dict(sig)
        )

        # Append to in-memory inbox cache.
        self._inboxes.setdefault(agent_id, []).append(sig)

        # If the agent is idle and ENABLED, fire a run immediately.
        if agent_id not in self._running_agents:
            a = await self.get_agent(agent_id)
            if a is not None and a.status is AgentStatus.ENABLED:
                asyncio.create_task(
                    self._run_with_signal(agent_id, signal_kind, sig),
                    name=f"agent-run-{agent_id}",
                )

        return sig

    async def _run_with_signal(
        self,
        agent_id: str,
        signal_kind: str,
        sig: InboxSignal,
    ) -> None:
        """Spawn point for signal-triggered agent runs.

        Re-checks that the agent is still idle (race-safe), re-fetches it
        (could have been disabled between dispatch and now), then runs the
        agent under the ``_running_agents`` guard.
        """
        if agent_id in self._running_agents:
            # Raced with another trigger — skip; the in-flight run will
            # pick up the signal via _drain_inbox on its next round.
            return
        a = await self.get_agent(agent_id)
        if a is None or a.status is not AgentStatus.ENABLED:
            return
        self._running_agents.add(agent_id)
        try:
            await self._run_agent_internal(
                a,
                triggered_by=signal_kind,
                trigger_context={"signal_id": sig.id, "sender_id": sig.sender_id},
                user_message=None,
            )
        finally:
            self._running_agents.discard(agent_id)

    async def _drain_inbox(self, agent_id: str) -> list[InboxSignal]:
        """Pop all pending inbox signals for *agent_id*, mark them processed,
        and return them so the caller can include them in the next round's prompt.
        """
        if self._storage is None:
            return []

        sigs = self._inboxes.pop(agent_id, [])
        now_iso = _now().isoformat()
        for sig in sigs:
            row = await self._storage.get(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id)
            if row is not None:
                row["processed_at"] = now_iso
                await self._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id, row)
        return sigs

    async def _rehydrate_inboxes(self) -> None:
        """Restore unprocessed InboxSignals from storage into the in-memory cache.

        Called during ``start()`` so signals survive process restarts.

        Decision: ``FilterOp.EQ`` with ``value=None`` generates ``= NULL`` in
        SQL which never matches.  Instead we use ``FilterOp.EXISTS`` with
        ``value=False`` which generates ``IS NULL`` — the correct SQL predicate.
        """
        if self._storage is None:
            return
        rows = await self._storage.query(
            Query(
                collection=_AGENT_INBOX_SIGNALS_COLLECTION,
                filters=[Filter(field="processed_at", op=FilterOp.EXISTS, value=False)],
            )
        )
        count = 0
        for row in rows:
            sig = _signal_from_dict(row)
            self._inboxes.setdefault(sig.agent_id, []).append(sig)
            count += 1
        if count:
            logger.info("Rehydrated %d unprocessed inbox signal(s)", count)

    async def _load_agent_for_caller(
        self,
        agent_id: str,
        *,
        caller_user_id: str,
        admin: bool = False,
    ) -> Agent:
        """Fetch an agent and enforce ownership.

        Raises:
            KeyError: agent does not exist.
            PermissionError: agent exists but belongs to another user.
        """
        a = await self.get_agent(agent_id)
        if a is None:
            raise KeyError(agent_id)
        if not admin and a.owner_user_id != caller_user_id:
            raise PermissionError(
                f"agent {agent_id} not accessible to user {caller_user_id}"
            )
        return a

    # ── AgentMemory (Task 7) ────────────────────────────────────────────

    async def save_memory(
        self,
        *,
        agent_id: str,
        content: str,
        kind: str = "fact",
        tags: frozenset[str] | set[str] | None = None,
        state: MemoryState = MemoryState.SHORT_TERM,
    ) -> AgentMemory:
        """Create and persist a new AgentMemory for the given agent."""
        if self._storage is None:
            raise RuntimeError("not started")
        m = AgentMemory(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            content=content,
            state=state,
            kind=kind,
            tags=frozenset(tags or ()),
            score=0.0,
            created_at=_now(),
            last_used_at=None,
        )
        await self._storage.put(_AGENT_MEMORIES_COLLECTION, m.id, _memory_to_dict(m))
        return m

    async def search_memory(
        self,
        *,
        agent_id: str,
        query: str,
        limit: int = 20,
        state: MemoryState | None = None,
        kind: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[AgentMemory]:
        """Naive substring search over an agent's memories.

        Filters by ``agent_id`` first (indexed filter), then applies a
        case-insensitive substring match on ``content``. Optional ``state``
        filter restricts to SHORT_TERM or LONG_TERM only. Optional ``kind``
        is an exact-match filter on ``kind``. Optional ``tags`` is an
        any-match filter — a memory matches if any of its tags appears in
        the requested set. Results are sorted by ``created_at`` descending
        and capped at ``limit``.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_AGENT_MEMORIES_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        out: list[AgentMemory] = []
        q = query.lower()
        for r in rows:
            if state is not None and r.get("state") != state.value:
                continue
            if kind is not None and r.get("kind") != kind:
                continue
            if tags is not None:
                row_tags = frozenset(r.get("tags", []))
                if not (tags & row_tags):
                    continue
            content = str(r.get("content", "")).lower()
            if not q or q in content:
                out.append(_memory_from_dict(r))
        # Sort recency descending, then cap.
        out.sort(key=lambda m: m.created_at, reverse=True)
        return out[:limit]

    async def promote_memory(
        self,
        *,
        memory_id: str,
        score: float,
        state: MemoryState = MemoryState.LONG_TERM,
    ) -> AgentMemory:
        """Promote a memory to a new state with an updated relevance score."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_MEMORIES_COLLECTION, memory_id)
        if row is None:
            raise KeyError(memory_id)
        row["state"] = state.value
        row["score"] = score
        await self._storage.put(_AGENT_MEMORIES_COLLECTION, memory_id, row)
        return _memory_from_dict(row)

    # ── Run orchestration (Task 8) ───────────────────────────────────

    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
        triggered_by: str = "manual",
        trigger_context: dict[str, Any] | None = None,
    ) -> Run:
        """Trigger an immediate agent run, awaiting completion.

        Verifies the agent exists and is ENABLED, guards against concurrent
        runs, then delegates to ``_run_agent_internal``.
        """
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")
        a = await self.get_agent(agent_id)
        if a is None:
            raise ValueError(f"agent not found: {agent_id}")
        if a.status is not AgentStatus.ENABLED:
            raise ValueError(f"agent {agent_id} is {a.status.value}")
        if agent_id in self._running_agents:
            raise ValueError(f"agent {agent_id} has a run in progress")

        self._running_agents.add(agent_id)
        try:
            run = await asyncio.shield(
                self._run_agent_internal(
                    a,
                    triggered_by=triggered_by,
                    trigger_context=trigger_context or {},
                    user_message=user_message,
                )
            )
        finally:
            self._running_agents.discard(agent_id)
        return run

    async def _run_agent_internal(
        self,
        a: Agent,
        *,
        triggered_by: str,
        trigger_context: dict[str, Any],
        user_message: str | None,
    ) -> Run:
        """Inner run loop — invoked under _running_agents guard.

        Builds the system prompt, synthesizes a trigger message if needed,
        calls ``self._ai.chat`` with ai_call='agent.run', and persists the
        Run entity with cost/token totals.
        """
        run = Run(
            id=f"run_{uuid.uuid4().hex[:12]}",
            agent_id=a.id,
            triggered_by=triggered_by,
            trigger_context=dict(trigger_context),
            started_at=_now(),
            status=RunStatus.RUNNING,
            conversation_id=a.conversation_id,
            delegation_id="",
            ended_at=None,
            final_message_text=None,
            rounds_used=0,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            error=None,
            awaiting_user_input=False,
            pending_question=None,
            pending_actions=[],
        )
        await self._storage.put(_AGENT_RUNS_COLLECTION, run.id, _run_to_dict(run))  # type: ignore[union-attr]
        await self._publish(
            "agent.run.started",
            {"agent_id": a.id, "run_id": run.id, "triggered_by": triggered_by},
        )

        try:
            system_prompt = await self._build_system_prompt(a, triggered_by, trigger_context)
            user_msg = user_message or self._synthesize_trigger_message(triggered_by, trigger_context)

            from gilbert.interfaces.auth import UserContext
            user_ctx = UserContext.from_user_id(a.owner_user_id) if hasattr(UserContext, "from_user_id") else None

            result = await self._ai.chat(  # type: ignore[union-attr]
                user_message=user_msg,
                conversation_id=a.conversation_id or None,
                user_ctx=user_ctx,
                system_prompt=system_prompt,
                ai_call=_AI_CALL_NAME,
                ai_profile=a.profile_id,
            )

            # ChatTurnResult uses `response_text`; map to run.final_message_text.
            run.final_message_text = result.response_text
            run.conversation_id = result.conversation_id
            tu = result.turn_usage or {}
            run.rounds_used = int(tu.get("rounds", 0))
            run.tokens_in = int(tu.get("input_tokens", 0))
            run.tokens_out = int(tu.get("output_tokens", 0))
            run.cost_usd = float(tu.get("cost_usd", 0.0))
            run.status = RunStatus.COMPLETED
            run.ended_at = _now()

            # Capture conv_id back on the agent row if just created.
            if a.conversation_id == "" and run.conversation_id:
                fresh = await self._storage.get(_AGENTS_COLLECTION, a.id)  # type: ignore[union-attr]
                if fresh is not None:
                    fresh["conversation_id"] = run.conversation_id
                    await self._storage.put(_AGENTS_COLLECTION, a.id, fresh)  # type: ignore[union-attr]

            await self._accumulate_cost(a.id, run.cost_usd)

        except Exception as exc:
            logger.exception("agent run failed: %s", a.id)
            run.status = RunStatus.FAILED
            run.error = repr(exc)
            run.ended_at = _now()

        await self._storage.put(_AGENT_RUNS_COLLECTION, run.id, _run_to_dict(run))  # type: ignore[union-attr]
        await self._publish(
            "agent.run.completed",
            {
                "agent_id": a.id,
                "run_id": run.id,
                "status": run.status.value,
                "cost_usd": run.cost_usd,
            },
        )
        return run

    def _synthesize_trigger_message(self, triggered_by: str, ctx: dict[str, Any]) -> str:
        """Return a synthetic user message describing why the agent was triggered."""
        if triggered_by == "manual":
            return "Run manually triggered. Take whatever action is appropriate."
        if triggered_by == "heartbeat":
            return "Heartbeat — periodic self-check."
        if triggered_by == "time":
            return "Scheduled trigger fired."
        if triggered_by == "event":
            etype = ctx.get("event_type", "?")
            return f"Event '{etype}' fired. See trigger context for the payload."
        return f"Trigger: {triggered_by}."

    def _compute_allowed_tool_names(self, a: Agent, *, available: set[str]) -> set[str]:
        """Compute the tool name set for an agent's run.

        - tools_allowed=None → all available tools (legacy behavior).
        - tools_allowed=[…] → core ∪ allowlist, intersected with available.

        Tools removed from the available set (e.g., plugin uninstalled) are
        silently dropped — they just don't appear in the run.
        """
        if a.tools_allowed is None:
            return set(available)
        keep = (set(_CORE_AGENT_TOOLS) | set(a.tools_allowed)) & set(available)
        return keep

    async def _build_system_prompt(
        self,
        a: Agent,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> str:
        """Assemble the full system prompt from persona, rules, and context blocks."""
        parts = [a.persona, a.system_prompt, a.procedural_rules]

        if triggered_by == "heartbeat":
            due = await self._due_commitments(a.id)
            checklist = a.heartbeat_checklist or "(no checklist configured)"
            due_block = (
                "\n".join(
                    f"- [{c.id}] {c.content} (due {c.due_at.isoformat()})"
                    for c in due
                )
                or "(none)"
            )
            parts.append(
                f"HEARTBEAT — periodic self-check. Read your checklist below "
                f"and decide if anything needs action right now. If nothing is "
                f"pressing, end your turn briefly.\n\n"
                f"CHECKLIST:\n{checklist}\n\n"
                f"DUE COMMITMENTS:\n{due_block}"
            )

        # Long-term memory block (top-K by recency).
        long_term = await self.search_memory(
            agent_id=a.id, query="", limit=20, state=MemoryState.LONG_TERM,
        )
        if long_term:
            mem_block = "\n".join(f"- {m.content}" for m in long_term)
            parts.append(f"LONG-TERM MEMORY:\n{mem_block}")

        return "\n\n---\n\n".join(p for p in parts if p)

    async def create_commitment(
        self, *, agent_id: str, content: str, due_at: datetime,
    ) -> Commitment:
        """Persist a new commitment and return it."""
        if self._storage is None:
            raise RuntimeError("not started")
        c = Commitment(
            id=f"com_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            content=content,
            due_at=due_at,
            created_at=_now(),
            completed_at=None,
            completion_note="",
        )
        await self._storage.put(_AGENT_COMMITMENTS_COLLECTION, c.id, _commitment_to_dict(c))
        return c

    async def complete_commitment(
        self, commitment_id: str, *, note: str = "",
    ) -> Commitment:
        """Mark a commitment complete with an optional note."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_COMMITMENTS_COLLECTION, commitment_id)
        if row is None:
            raise KeyError(commitment_id)
        row["completed_at"] = _now().isoformat()
        row["completion_note"] = note
        await self._storage.put(_AGENT_COMMITMENTS_COLLECTION, commitment_id, row)
        return _commitment_from_dict(row)

    async def list_commitments(
        self, *, agent_id: str, include_completed: bool = False,
    ) -> list[Commitment]:
        """Return commitments for *agent_id*, sorted by due_at ascending.

        By default only unfinished commitments are returned; pass
        ``include_completed=True`` to include completed ones.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_AGENT_COMMITMENTS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        out: list[Commitment] = []
        for r in rows:
            if not include_completed and r.get("completed_at"):
                continue
            out.append(_commitment_from_dict(r))
        out.sort(key=lambda c: c.due_at)
        return out

    async def _due_commitments(self, agent_id: str) -> list[Commitment]:
        """Return commitments for *agent_id* that are due now and not completed."""
        if self._storage is None:
            return []
        rows = await self._storage.query(
            Query(
                collection=_AGENT_COMMITMENTS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        out: list[Commitment] = []
        for r in rows:
            if r.get("completed_at"):
                continue
            due = datetime.fromisoformat(r["due_at"])
            if due <= _now():
                out.append(_commitment_from_dict(r))
        return out

    async def _accumulate_cost(self, agent_id: str, delta: float) -> None:
        """Add *delta* to the agent's lifetime_cost_usd; auto-DISABLE on cap breach."""
        if delta <= 0 or self._storage is None:
            return
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return
        new_total = float(row.get("lifetime_cost_usd", 0.0)) + delta
        row["lifetime_cost_usd"] = new_total
        cap = row.get("cost_cap_usd")
        if cap is not None and new_total >= float(cap):
            row["status"] = AgentStatus.DISABLED.value
            logger.warning(
                "Agent %s auto-DISABLED at cost cap %s (cumulative %s)",
                agent_id, cap, new_total,
            )
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)

    async def list_runs(self, *, agent_id: str, limit: int = 50) -> list[Run]:
        """Return up to *limit* runs for *agent_id*, sorted most-recent first."""
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_AGENT_RUNS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return [_run_from_dict(r) for r in rows[:limit]]

    # ── ToolProvider (Task 14) ───────────────────────────────────────

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        """Return the 7 core agent tool definitions."""
        return [
            _TOOL_COMPLETE_RUN,
            _TOOL_COMMITMENT_CREATE,
            _TOOL_COMMITMENT_COMPLETE,
            _TOOL_COMMITMENT_LIST,
            _TOOL_AGENT_MEMORY_SAVE,
            _TOOL_AGENT_MEMORY_SEARCH,
            _TOOL_AGENT_MEMORY_PROMOTE,
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call by name. Raises KeyError for unknown tools."""
        if name == "complete_run":
            return await self._exec_complete_run(arguments)
        if name == "commitment_create":
            return await self._exec_commitment_create(arguments)
        if name == "commitment_complete":
            return await self._exec_commitment_complete(arguments)
        if name == "commitment_list":
            return await self._exec_commitment_list(arguments)
        if name == "agent_memory_save":
            return await self._exec_memory_save(arguments)
        if name == "agent_memory_search":
            return await self._exec_memory_search(arguments)
        if name == "agent_memory_review_and_promote":
            return await self._exec_memory_promote(arguments)
        raise KeyError(f"unknown tool: {name}")

    async def _exec_complete_run(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        reason = str(args.get("reason", "")).strip() or "(no reason given)"
        if not agent_id:
            return "error: complete_run requires _agent_id (injected by runtime)"
        rows = await self._storage.query(  # type: ignore[union-attr]
            Query(
                collection=_AGENT_RUNS_COLLECTION,
                filters=[
                    Filter(field="agent_id", op=FilterOp.EQ, value=agent_id),
                    Filter(field="status", op=FilterOp.EQ, value="running"),
                ],
            )
        )
        if not rows:
            return f"no active run for agent {agent_id}"
        row = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)[0]
        row["status"] = RunStatus.COMPLETED.value
        row["ended_at"] = _now().isoformat()
        row["final_message_text"] = reason
        await self._storage.put(_AGENT_RUNS_COLLECTION, row["_id"], row)  # type: ignore[union-attr]
        return f"run {row['_id']} marked complete: {reason}"

    async def _exec_commitment_create(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        content = str(args.get("content", "")).strip()
        if not agent_id or not content:
            return "error: commitment_create requires _agent_id and content"
        if args.get("due_at"):
            due_at = datetime.fromisoformat(str(args["due_at"]))
        else:
            seconds = float(args.get("due_in_seconds", 1800))
            due_at = _now() + timedelta(seconds=seconds)
        c = await self.create_commitment(agent_id=agent_id, content=content, due_at=due_at)
        return f"commitment {c.id} created, due {c.due_at.isoformat()}"

    async def _exec_commitment_complete(self, args: dict[str, Any]) -> str:
        cid = str(args.get("commitment_id", ""))
        if not cid:
            return "error: commitment_complete requires commitment_id"
        c = await self.complete_commitment(cid, note=str(args.get("note", "")))
        return f"commitment {c.id} completed"

    async def _exec_commitment_list(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: commitment_list requires _agent_id"
        include = bool(args.get("include_completed", False))
        cs = await self.list_commitments(agent_id=agent_id, include_completed=include)
        if not cs:
            return "(no commitments)"
        lines = [
            f"- [{c.id}] {c.content} — due {c.due_at.isoformat()}"
            + (f" (completed: {c.completion_note})" if c.completed_at else "")
            for c in cs
        ]
        return "\n".join(lines)

    async def _exec_memory_save(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        content = str(args.get("content", "")).strip()
        if not agent_id or not content:
            return "error: agent_memory_save requires _agent_id and content"
        kind = str(args.get("kind", "fact"))
        tags_raw = args.get("tags") or []
        tags = frozenset(str(t) for t in tags_raw if str(t).strip())
        m = await self.save_memory(agent_id=agent_id, content=content, kind=kind, tags=tags)
        return f"memory {m.id} saved (state={m.state.value}, kind={m.kind})"

    async def _exec_memory_search(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: agent_memory_search requires _agent_id"
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 20))
        out = await self.search_memory(agent_id=agent_id, query=query, limit=limit)
        if not out:
            return "(no matches)"
        return "\n".join(f"- [{m.id}] ({m.state.value}, {m.kind}) {m.content}" for m in out)

    async def _exec_memory_promote(self, args: dict[str, Any]) -> str:
        reviews = args.get("reviews") or []
        if not isinstance(reviews, list):
            return "error: reviews must be an array"
        applied = 0
        for r in reviews:
            if not isinstance(r, dict):
                continue
            mid = str(r.get("memory_id", ""))
            decision = str(r.get("decision", ""))
            if not mid or decision not in {"promote", "demote", "keep"}:
                continue
            if decision == "promote":
                await self.promote_memory(memory_id=mid, score=float(r.get("score", 0.5)))
                applied += 1
            elif decision == "demote":
                await self.promote_memory(
                    memory_id=mid,
                    score=float(r.get("score", 0.0)),
                    state=MemoryState.SHORT_TERM,
                )
                applied += 1
            # 'keep' is a no-op
        return f"reviewed {len(reviews)} memories, applied {applied}"

    # ── Tool argument injection (Task 15) ────────────────────────────

    def _inject_agent_id(
        self, agent_id: str, tools_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Wrap each tool handler so _agent_id is injected into args.

        Expects tools_dict shape: ``dict[name, tuple[ToolDefinition, callable]]``
        matching agent_loop.run_loop's expected shape.

        The wrapped handler accepts the same arguments dict and mutates it
        to include ``_agent_id`` if absent (caller's value wins if present).
        """
        wrapped: dict[str, Any] = {}
        for name, entry in tools_dict.items():
            tool_def, handler = entry

            async def _wrapped(args: dict[str, Any], _h: Any = handler) -> Any:
                new_args = dict(args)
                new_args.setdefault("_agent_id", agent_id)
                return await _h(new_args)

            wrapped[name] = (tool_def, _wrapped)
        return wrapped

    # ── WsHandlerProvider ────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "agents.create": self._ws_create,
            "agents.get": self._ws_get,
            "agents.list": self._ws_list,
            "agents.update": self._ws_update,
            "agents.delete": self._ws_delete,
            "agents.set_status": self._ws_set_status,
            "agents.run_now": self._ws_run_now,
            "agents.get_defaults": self._ws_get_defaults,
            "agents.runs.list": self._ws_runs_list,
            "agents.commitments.list": self._ws_commitments_list,
            "agents.commitments.create": self._ws_commitments_create,
            "agents.commitments.complete": self._ws_commitments_complete,
            "agents.memories.list": self._ws_memories_list,
            "agents.memories.set_state": self._ws_memories_set_state,
            "agents.tools.list_available": self._ws_tools_list_available,
            "agents.tools.list_groups": self._ws_tools_list_groups,
        }

    def _is_admin(self, conn: Any) -> bool:
        return getattr(conn, "user_level", 999) <= 0

    def _caller_user_id(self, conn: Any) -> str:
        uid = getattr(conn, "user_id", "") or ""
        if not uid:
            raise PermissionError("anonymous caller")
        return uid

    async def _ws_create(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller_user_id(conn)
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        # Drop unknown fields; create_agent accepts a tight allowlist.
        allowed_fields = {
            "role_label", "persona", "system_prompt", "procedural_rules",
            "profile_id", "avatar_kind", "avatar_value", "cost_cap_usd",
            "tools_allowed", "heartbeat_enabled", "heartbeat_interval_s",
            "heartbeat_checklist", "dream_enabled", "dream_quiet_hours",
            "dream_probability", "dream_max_per_night",
        }
        fields = {k: v for k, v in params.items() if k in allowed_fields}
        a = await self.create_agent(owner_user_id=owner, name=name, **fields)
        return {"agent": _agent_to_dict(a)}

    async def _ws_get(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        a = await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        return {"agent": _agent_to_dict(a)}

    async def _ws_list(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        admin = self._is_admin(conn)
        if admin and params.get("owner_user_id") is not None:
            agents = await self.list_agents(owner_user_id=str(params["owner_user_id"]))
        elif admin:
            agents = await self.list_agents()
        else:
            agents = await self.list_agents(owner_user_id=self._caller_user_id(conn))
        return {"agents": [_agent_to_dict(a) for a in agents]}

    async def _ws_update(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        patch = params.get("patch") or {}
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        a = await self.update_agent(agent_id, patch)
        return {"agent": _agent_to_dict(a)}

    async def _ws_delete(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        ok = await self.delete_agent(agent_id)
        return {"deleted": ok}

    async def _ws_set_status(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        status_raw = str(params.get("status", "")).strip()
        try:
            status = AgentStatus(status_raw)
        except ValueError:
            raise ValueError(f"unknown status: {status_raw}") from None
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        # Route through update_agent so the agent.updated event fires and
        # heartbeat lifecycle is handled in one place.
        updated = await self.update_agent(agent_id, {"status": status.value})
        return {"agent": _agent_to_dict(updated)}

    async def _ws_run_now(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        user_message = params.get("user_message")
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        run = await self.run_agent_now(agent_id, user_message=user_message)
        return {"run_id": run.id, "status": run.status.value}

    async def _ws_get_defaults(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"defaults": dict(self._defaults)}

    async def _ws_runs_list(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        limit = int(params.get("limit", 50))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        runs = await self.list_runs(agent_id=agent_id, limit=limit)
        return {"runs": [_run_to_dict(r) for r in runs]}

    async def _ws_commitments_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        include_completed = bool(params.get("include_completed", False))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        cs = await self.list_commitments(
            agent_id=agent_id, include_completed=include_completed,
        )
        return {"commitments": [_commitment_to_dict(c) for c in cs]}

    async def _ws_commitments_create(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        content = str(params.get("content", "")).strip()
        if not content:
            raise ValueError("content is required")
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        # Resolve due_at: prefer explicit due_at; fall back to due_in_seconds.
        due_at_raw = params.get("due_at")
        due_in_seconds = params.get("due_in_seconds")
        if due_at_raw:
            due_at = datetime.fromisoformat(str(due_at_raw))
        elif due_in_seconds is not None:
            due_at = _now() + timedelta(seconds=int(due_in_seconds))
        else:
            raise ValueError("due_at or due_in_seconds is required")
        c = await self.create_commitment(
            agent_id=agent_id, content=content, due_at=due_at,
        )
        return {"commitment": _commitment_to_dict(c)}

    async def _ws_commitments_complete(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        commitment_id = str(params.get("commitment_id", ""))
        note = str(params.get("note", ""))
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_COMMITMENTS_COLLECTION, commitment_id)
        if row is None:
            raise KeyError(commitment_id)
        # Authorize via the owning agent.
        await self._load_agent_for_caller(
            row["agent_id"], caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        c = await self.complete_commitment(commitment_id, note=note)
        return {"commitment": _commitment_to_dict(c)}

    async def _ws_memories_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        state_raw = params.get("state")
        state = MemoryState(state_raw) if state_raw else None
        kind = params.get("kind")
        kind_str = str(kind) if kind else None
        tags_raw = params.get("tags")
        tags: frozenset[str] | None = None
        if tags_raw:
            tags = frozenset(str(t) for t in tags_raw if str(t).strip())
        q = str(params.get("q", ""))
        limit = int(params.get("limit", 50))
        memories = await self.search_memory(
            agent_id=agent_id, query=q, limit=limit,
            state=state, kind=kind_str, tags=tags,
        )
        return {"memories": [_memory_to_dict(m) for m in memories]}

    async def _ws_memories_set_state(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        memory_id = str(params.get("memory_id", ""))
        state_raw = str(params.get("state", ""))
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_MEMORIES_COLLECTION, memory_id)
        if row is None:
            raise KeyError(memory_id)
        await self._load_agent_for_caller(
            row["agent_id"], caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        updated = await self.promote_memory(
            memory_id=memory_id,
            score=float(row.get("score", 0.0)),
            state=MemoryState(state_raw),
        )
        return {"memory": _memory_to_dict(updated)}

    async def _ws_tools_list_available(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate tools the caller could grant to an agent.

        Delegates to the bound AIToolDiscoveryProvider — same path used
        by the MCP server tools-preview endpoint. Flattens the discovery
        result into a list of ``{name, description, required_role,
        provider}`` objects sorted by name.
        """
        if self._tool_discovery is None:
            raise RuntimeError("not started")
        # Ensure caller is authenticated; the tools list is non-sensitive
        # but we keep the same gate as every other handler.
        self._caller_user_id(conn)
        user_ctx = getattr(conn, "user_ctx", None)
        discovered = self._tool_discovery.discover_tools(user_ctx=user_ctx)
        tools: list[dict[str, Any]] = []
        for name, entry in discovered.items():
            # discover_tools returns dict[str, tuple[ToolProvider, ToolDefinition]].
            if isinstance(entry, tuple) and len(entry) == 2:
                provider, tool_def = entry
                provider_name = getattr(provider, "tool_provider_name", "")
            else:
                tool_def = entry
                provider_name = ""
            tools.append({
                "name": getattr(tool_def, "name", name),
                "description": getattr(tool_def, "description", ""),
                "required_role": getattr(tool_def, "required_role", "user"),
                "provider": provider_name,
            })
        tools.sort(key=lambda t: t["name"])
        return {"tools": tools}

    async def _ws_tools_list_groups(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the configured ``tool_groups`` map for the SPA picker."""
        self._caller_user_id(conn)
        return {"groups": dict(self._defaults.get("tool_groups", {}))}

    # ── Event publishing helper ─────────────────────────────────────

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event on the bus, no-op if the bus is not bound."""
        if self._event_bus is None:
            return
        await self._event_bus.publish(Event(event_type=event_type, data=data, source="agent"))
