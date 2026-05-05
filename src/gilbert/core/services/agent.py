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

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.agent import Agent, AgentStatus, InboxSignal, Run
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.scheduler import SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Filter, FilterOp, Query, StorageBackend, StorageProvider

logger = logging.getLogger(__name__)

# ── Name validation ──────────────────────────────────────────────────

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# ── Collection names ─────────────────────────────────────────────────

_AGENTS_COLLECTION = "agents"
_AGENT_MEMORIES_COLLECTION = "agent_memories"
_AGENT_TRIGGERS_COLLECTION = "agent_triggers"
_AGENT_COMMITMENTS_COLLECTION = "agent_commitments"
_AGENT_INBOX_SIGNALS_COLLECTION = "agent_inbox_signals"
_AGENT_RUNS_COLLECTION = "agent_runs"
_AI_CALL_NAME = "agent.run"


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

        # Bind scheduler capability
        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise RuntimeError(
                "scheduler capability does not implement SchedulerProvider"
            )
        self._scheduler = scheduler_svc

        # Task 5: index creation goes here.
        # Task 8: run rehydration goes here.
        # Task 10: heartbeat re-arming goes here.

        logger.info("AgentService started")

    async def stop(self) -> None:
        """Graceful shutdown — Task 8 will add run teardown here."""
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
            "dream_probability", "dream_max_per_night",
        }
        for k, v in patch.items():
            if k not in _allowed_patch_fields:
                raise ValueError(f"field not patchable: {k}")
            row[k] = v
        row["updated_at"] = _now().isoformat()
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)
        return _agent_from_dict(row)

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
        return True

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

    # ── Run orchestration (Task 8) ───────────────────────────────────

    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
    ) -> Run:
        """Trigger an immediate agent run. Implemented in Task 8."""
        raise NotImplementedError("filled in by Task 8")
