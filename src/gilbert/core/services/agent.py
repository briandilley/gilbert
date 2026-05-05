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
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.agent import Agent, InboxSignal, Run
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.scheduler import SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend, StorageProvider

logger = logging.getLogger(__name__)

# ── Collection names ─────────────────────────────────────────────────

_AGENTS_COLLECTION = "agents"
_AGENT_MEMORIES_COLLECTION = "agent_memories"
_AGENT_TRIGGERS_COLLECTION = "agent_triggers"
_AGENT_COMMITMENTS_COLLECTION = "agent_commitments"
_AGENT_INBOX_SIGNALS_COLLECTION = "agent_inbox_signals"
_AGENT_RUNS_COLLECTION = "agent_runs"
_AI_CALL_NAME = "agent.run"


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

    # ── AgentProvider stubs (filled in by later tasks) ───────────────

    async def create_agent(
        self,
        *,
        owner_user_id: str,
        name: str,
        **fields: Any,
    ) -> Agent:
        """Create a new Agent entity. Implemented in Task 5."""
        raise NotImplementedError("filled in by Task 5")

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch an Agent by ID. Implemented in Task 5."""
        raise NotImplementedError("filled in by Task 5")

    async def list_agents(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Agent]:
        """List agents, optionally filtered by owner. Implemented in Task 5."""
        raise NotImplementedError("filled in by Task 5")

    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
    ) -> Run:
        """Trigger an immediate agent run. Implemented in Task 8."""
        raise NotImplementedError("filled in by Task 8")
