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

    # ── Goal CRUD ─────────────────────────────────────────────────

    async def create_goal(
        self,
        *,
        owner_user_id: str,
        name: str,
        instruction: str,
        profile_id: str,
    ) -> Goal:
        if self._storage is None:
            raise RuntimeError("AutonomousAgentService.start() not called")
        now = datetime.now(UTC)
        goal = Goal(
            id=str(uuid.uuid4()),
            owner_user_id=owner_user_id,
            name=name,
            instruction=instruction,
            profile_id=profile_id,
            status=GoalStatus.ENABLED,
            created_at=now,
            updated_at=now,
        )
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        return goal

    async def get_goal(self, goal_id: str) -> Goal | None:
        if self._storage is None:
            raise RuntimeError("not started")
        raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
        return _goal_from_dict(raw) if raw else None

    async def list_goals(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Goal]:
        if self._storage is None:
            raise RuntimeError("not started")
        filters = []
        if owner_user_id is not None:
            filters.append(Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id))
        raw_list = await self._storage.query(
            Query(
                collection=_GOAL_COLLECTION,
                filters=filters,
                sort=[SortField(field="created_at", descending=True)],
                limit=1000,
            )
        )
        return [_goal_from_dict(r) for r in raw_list]

    async def update_goal(
        self,
        goal_id: str,
        *,
        name: str | None = None,
        instruction: str | None = None,
        profile_id: str | None = None,
        status: GoalStatus | None = None,
    ) -> Goal | None:
        if self._storage is None:
            raise RuntimeError("not started")
        raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
        if raw is None:
            return None
        goal = _goal_from_dict(raw)
        if name is not None:
            goal.name = name
        if instruction is not None:
            goal.instruction = instruction
        if profile_id is not None:
            goal.profile_id = profile_id
        if status is not None:
            goal.status = status
        goal.updated_at = datetime.now(UTC)
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        return goal

    async def delete_goal(self, goal_id: str) -> bool:
        if self._storage is None:
            raise RuntimeError("not started")
        raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
        if raw is None:
            return False
        await self._storage.delete(_GOAL_COLLECTION, goal_id)
        # Also delete associated runs
        runs = await self._storage.query(
            Query(
                collection=_RUN_COLLECTION,
                filters=[Filter(field="goal_id", op=FilterOp.EQ, value=goal_id)],
                limit=10_000,
            )
        )
        for r in runs:
            await self._storage.delete(_RUN_COLLECTION, r["id"])
        return True

    # ── Execution ─────────────────────────────────────────────────

    async def run_goal_now(self, goal_id: str) -> Run:
        """Execute a goal once (manual trigger). Persists a Run entity
        and returns it. Raises ValueError if the goal is in a non-runnable
        state.
        """
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")

        goal = await self.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"goal not found: {goal_id}")
        if goal.status == GoalStatus.COMPLETED:
            raise ValueError(f"goal {goal_id} is completed")
        if goal.status == GoalStatus.DISABLED:
            raise ValueError(f"goal {goal_id} is disabled")

        run = Run(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            triggered_by="manual",
            started_at=datetime.now(UTC),
            status=RunStatus.RUNNING,
        )
        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))

        # Build the user message that kicks the loop
        user_message = self._build_initial_user_message(goal)

        try:
            from gilbert.interfaces.auth import UserContext
            user_ctx = UserContext.SYSTEM if goal.owner_user_id == "system" else None
            # When user_ctx is None, AIService treats the call as system-driven.
            # A future task can fetch the actual UserContext for the owner.
            result = await self._ai.chat(
                user_message=user_message,
                conversation_id=None,  # fresh conversation per run
                user_ctx=user_ctx,
                ai_call=_AI_CALL_NAME,
                ai_profile=goal.profile_id,
            )
            run.status = RunStatus.COMPLETED
            run.final_message_text = result.response_text
            run.conversation_id = result.conversation_id
            if result.turn_usage:
                run.tokens_in = int(result.turn_usage.get("input_tokens", 0))
                run.tokens_out = int(result.turn_usage.get("output_tokens", 0))
            run.rounds_used = len(result.rounds) + 1
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = repr(exc)
            logger.exception("agent run failed: goal=%s run=%s", goal_id, run.id)

        run.ended_at = datetime.now(UTC)
        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))

        # Update goal counters
        goal.run_count += 1
        goal.last_run_at = run.ended_at
        goal.last_run_status = run.status
        goal.updated_at = datetime.now(UTC)
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))

        return run

    def _build_initial_user_message(self, goal: Goal) -> str:
        """Synthesize the user message that drives the AI loop.

        Format:
            You are an autonomous agent running a goal on behalf of user
            <owner>. Your goal is named "<name>". Goal instruction:

            <instruction>

            Take whatever action is appropriate to advance this goal. When
            you have fully completed the goal — for good — call the
            ``complete_goal`` tool with the goal_id and a short reason.
        """
        return (
            f"You are an autonomous agent running a goal on behalf of user "
            f"{goal.owner_user_id}. Your goal is named \"{goal.name}\". "
            f"Goal instruction:\n\n"
            f"{goal.instruction}\n\n"
            f"Take whatever action is appropriate to advance this goal. "
            f"When you have fully completed the goal — for good — call the "
            f"``complete_goal`` tool with goal_id={goal.id!r} and a short "
            f"reason. (The reason is logged on the run for the human owner.)"
        )


def _goal_to_dict(g: Goal) -> dict[str, Any]:
    return {
        "id": g.id,
        "owner_user_id": g.owner_user_id,
        "name": g.name,
        "instruction": g.instruction,
        "profile_id": g.profile_id,
        "status": g.status.value,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
        "last_run_at": g.last_run_at.isoformat() if g.last_run_at else None,
        "last_run_status": g.last_run_status.value if g.last_run_status else None,
        "run_count": g.run_count,
        "completed_at": g.completed_at.isoformat() if g.completed_at else None,
        "completed_reason": g.completed_reason,
    }


def _goal_from_dict(d: dict[str, Any]) -> Goal:
    last_run_status_raw = d.get("last_run_status")
    completed_at_raw = d.get("completed_at")
    last_run_at_raw = d.get("last_run_at")
    return Goal(
        id=d["id"],
        owner_user_id=d["owner_user_id"],
        name=d["name"],
        instruction=d["instruction"],
        profile_id=d["profile_id"],
        status=GoalStatus(d["status"]),
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        last_run_at=datetime.fromisoformat(last_run_at_raw) if last_run_at_raw else None,
        last_run_status=RunStatus(last_run_status_raw) if last_run_status_raw else None,
        run_count=int(d.get("run_count", 0)),
        completed_at=datetime.fromisoformat(completed_at_raw) if completed_at_raw else None,
        completed_reason=d.get("completed_reason"),
    )


def _run_to_dict(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "goal_id": r.goal_id,
        "triggered_by": r.triggered_by,
        "started_at": r.started_at.isoformat(),
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "status": r.status.value,
        "conversation_id": r.conversation_id,
        "final_message_text": r.final_message_text,
        "rounds_used": r.rounds_used,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "error": r.error,
        "complete_goal_called": r.complete_goal_called,
        "complete_reason": r.complete_reason,
    }


def _run_from_dict(d: dict[str, Any]) -> Run:
    ended_at_raw = d.get("ended_at")
    return Run(
        id=d["id"],
        goal_id=d["goal_id"],
        triggered_by=d.get("triggered_by", "manual"),
        started_at=datetime.fromisoformat(d["started_at"]),
        status=RunStatus(d["status"]),
        conversation_id=d.get("conversation_id", ""),
        ended_at=datetime.fromisoformat(ended_at_raw) if ended_at_raw else None,
        final_message_text=d.get("final_message_text"),
        rounds_used=int(d.get("rounds_used", 0)),
        tokens_in=int(d.get("tokens_in", 0)),
        tokens_out=int(d.get("tokens_out", 0)),
        error=d.get("error"),
        complete_goal_called=bool(d.get("complete_goal_called", False)),
        complete_reason=d.get("complete_reason"),
    )
