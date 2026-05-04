"""AutonomousAgentService — persists Goal/Run entities, executes goals via
AIService.chat(ai_call="agent.run"), exposes complete_goal as a tool.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.agent import (
    Goal,
    GoalStatus,
    Run,
    RunStatus,
)
from gilbert.interfaces.ai import AIProvider, MessageRole
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
)

logger = logging.getLogger(__name__)


_GOAL_COLLECTION = "agent_goals"
_RUN_COLLECTION = "agent_runs"
_AI_CALL_NAME = "agent.run"


class AutonomousAgentService(Service):
    """The autonomous-agent service.

    Capabilities declared:
    - ``agent`` — satisfies AgentProvider.
    - ``ai_tools`` — exposes the ``complete_goal`` tool.
    - ``ws_handlers`` — WS RPCs for goal/run CRUD and manual run.
    """

    tool_provider_name = "autonomous_agent"

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None
        self._ai: AIProvider | None = None
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="autonomous_agent",
            capabilities=frozenset({"agent", "ai_tools", "ws_handlers"}),
            ai_calls=frozenset({_AI_CALL_NAME}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("entity_storage missing or wrong type")
        self._storage = storage_svc.backend

        event_bus_svc = resolver.require_capability("event_bus")
        if not isinstance(event_bus_svc, EventBusProvider):
            raise RuntimeError("event_bus missing or wrong type")
        self._event_bus = event_bus_svc.bus

        ai_svc = resolver.require_capability("ai_chat")
        if not isinstance(ai_svc, AIProvider):
            raise RuntimeError("ai_chat missing or wrong type")
        self._ai = ai_svc

        await self._storage.ensure_index(
            IndexDefinition(
                collection=_GOAL_COLLECTION,
                fields=["status", "owner_user_id"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_RUN_COLLECTION,
                fields=["goal_id", "started_at"],
            )
        )
        logger.info("AutonomousAgentService started")

    async def stop(self) -> None:
        return None
