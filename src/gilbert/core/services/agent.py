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
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.scheduler import (
    JobCallback,
    Schedule,
    SchedulerProvider,
)
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
        self._scheduler: SchedulerProvider | None = None
        self._event_bus_unsubscribers: dict[str, Any] = {}
        """goal_id → unsubscribe callable for EVENT triggers."""

        self._running_goals: set[str] = set()
        """In-progress goal IDs to skip duplicate trigger fires."""

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

        sched_svc = resolver.require_capability("scheduler")
        if not isinstance(sched_svc, SchedulerProvider):
            raise RuntimeError("scheduler missing or wrong type")
        self._scheduler = sched_svc

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
        trigger_type: str | None = None,
        trigger_config: dict[str, Any] | None = None,
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
            trigger_type=trigger_type,
            trigger_config=trigger_config,
        )
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        await self._arm_trigger(goal)
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
        trigger_type: str | None = None,
        trigger_config: dict[str, Any] | None = None,
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
        if trigger_type is not None:
            goal.trigger_type = trigger_type
        if trigger_config is not None:
            goal.trigger_config = trigger_config
        goal.updated_at = datetime.now(UTC)
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        # Re-arm: cheapest correct policy is always disarm then arm
        await self._disarm_trigger(goal)
        await self._arm_trigger(goal)
        return goal

    async def delete_goal(self, goal_id: str) -> bool:
        if self._storage is None:
            raise RuntimeError("not started")
        raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
        if raw is None:
            return False
        existing_goal = _goal_from_dict(raw)
        await self._disarm_trigger(existing_goal)
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
        # run_goal_now runs synchronously and returns the completed run
        # (not via _spawn_run — manual triggers want the result back).
        return await self._run_goal_internal(goal_id, "manual", {})

    async def declare_goal_complete(
        self,
        goal_id: str,
        run_id: str,
        reason: str,
    ) -> bool:
        """Mark a goal as COMPLETED. Idempotent — returns False if already
        completed. ``run_id`` is currently informational; future versions
        may validate it against an active run.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
        if raw is None:
            return False
        goal = _goal_from_dict(raw)
        if goal.status == GoalStatus.COMPLETED:
            return False
        goal.status = GoalStatus.COMPLETED
        goal.completed_at = datetime.now(UTC)
        goal.completed_reason = reason
        goal.updated_at = goal.completed_at
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        await self._disarm_trigger(goal)

        # Mark the most-recent run as having declared completion
        run_raw = await self._storage.get(_RUN_COLLECTION, run_id)
        if run_raw is not None:
            run_raw["complete_goal_called"] = True
            run_raw["complete_reason"] = reason
            await self._storage.put(_RUN_COLLECTION, run_id, run_raw)
        return True

    # ── ToolProvider implementation ───────────────────────────────

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        return [_COMPLETE_GOAL_TOOL]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "complete_goal":
            raise KeyError(f"unknown tool: {name}")
        goal_id = str(arguments.get("goal_id", ""))
        reason = str(arguments.get("reason", "")).strip() or "(no reason given)"
        if not goal_id:
            return "error: complete_goal requires goal_id"
        # We don't have a run_id available here — pass empty string. In
        # the v1 manual-run flow this is acceptable; Phase 4b's automatic
        # triggers will revisit this when runs are spawned by the service.
        ok = await self.declare_goal_complete(goal_id, run_id="", reason=reason)
        if ok:
            return f"goal {goal_id} marked complete: {reason}"
        return f"goal {goal_id} was already completed (no-op)"

    # ── WS handlers ───────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "agent.goal.create": self._ws_goal_create,
            "agent.goal.list": self._ws_goal_list,
            "agent.goal.get": self._ws_goal_get,
            "agent.goal.update": self._ws_goal_update,
            "agent.goal.delete": self._ws_goal_delete,
            "agent.goal.run_now": self._ws_goal_run_now,
            "agent.run.list": self._ws_run_list,
            "agent.run.get": self._ws_run_get,
        }

    async def _ws_goal_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        name = str(frame.get("name", "")).strip()
        instruction = str(frame.get("instruction", "")).strip()
        profile_id = str(frame.get("profile_id", "")).strip()
        if not name or not instruction or not profile_id:
            return {
                "type": "agent.goal.create.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "name, instruction, profile_id required",
            }
        goal = await self.create_goal(
            owner_user_id=conn.user_ctx.user_id,
            name=name,
            instruction=instruction,
            profile_id=profile_id,
        )
        return {
            "type": "agent.goal.create.result",
            "ref": frame.get("id"),
            "ok": True,
            "goal": _goal_to_dict(goal),
        }

    async def _ws_goal_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        goals = await self.list_goals(owner_user_id=conn.user_ctx.user_id)
        return {
            "type": "agent.goal.list.result",
            "ref": frame.get("id"),
            "goals": [_goal_to_dict(g) for g in goals],
        }

    async def _ws_goal_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        goal_id = str(frame.get("goal_id", ""))
        goal = await self.get_goal(goal_id)
        if goal is None or goal.owner_user_id != conn.user_ctx.user_id:
            return {
                "type": "agent.goal.get.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        return {
            "type": "agent.goal.get.result",
            "ref": frame.get("id"),
            "ok": True,
            "goal": _goal_to_dict(goal),
        }

    async def _ws_goal_update(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        goal_id = str(frame.get("goal_id", ""))
        existing = await self.get_goal(goal_id)
        if existing is None or existing.owner_user_id != conn.user_ctx.user_id:
            return {
                "type": "agent.goal.update.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        status_raw = frame.get("status")
        try:
            status_enum = GoalStatus(status_raw) if status_raw else None
        except ValueError:
            return {
                "type": "agent.goal.update.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "invalid status",
            }
        updated = await self.update_goal(
            goal_id,
            name=frame.get("name"),
            instruction=frame.get("instruction"),
            profile_id=frame.get("profile_id"),
            status=status_enum,
        )
        return {
            "type": "agent.goal.update.result",
            "ref": frame.get("id"),
            "ok": updated is not None,
            "goal": _goal_to_dict(updated) if updated else None,
        }

    async def _ws_goal_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        goal_id = str(frame.get("goal_id", ""))
        existing = await self.get_goal(goal_id)
        if existing is None or existing.owner_user_id != conn.user_ctx.user_id:
            return {
                "type": "agent.goal.delete.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        deleted = await self.delete_goal(goal_id)
        return {
            "type": "agent.goal.delete.result",
            "ref": frame.get("id"),
            "ok": deleted,
        }

    async def _ws_goal_run_now(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        goal_id = str(frame.get("goal_id", ""))
        existing = await self.get_goal(goal_id)
        if existing is None or existing.owner_user_id != conn.user_ctx.user_id:
            return {
                "type": "agent.goal.run_now.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        try:
            run = await self.run_goal_now(goal_id)
        except ValueError as exc:
            return {
                "type": "agent.goal.run_now.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": str(exc),
            }
        return {
            "type": "agent.goal.run_now.result",
            "ref": frame.get("id"),
            "ok": True,
            "run": _run_to_dict(run),
        }

    async def _ws_run_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        if self._storage is None:
            raise RuntimeError("not started")
        goal_id = str(frame.get("goal_id", ""))
        if not goal_id:
            return {
                "type": "agent.run.list.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "goal_id required",
            }
        # Owner-only access via the goal
        goal = await self.get_goal(goal_id)
        if goal is None or goal.owner_user_id != conn.user_ctx.user_id:
            return {
                "type": "agent.run.list.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        raw_list = await self._storage.query(
            Query(
                collection=_RUN_COLLECTION,
                filters=[Filter(field="goal_id", op=FilterOp.EQ, value=goal_id)],
                sort=[SortField(field="started_at", descending=True)],
                limit=int(frame.get("limit") or 100),
            )
        )
        return {
            "type": "agent.run.list.result",
            "ref": frame.get("id"),
            "ok": True,
            "runs": raw_list,
        }

    async def _ws_run_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        if self._storage is None:
            raise RuntimeError("not started")
        run_id = str(frame.get("run_id", ""))
        raw = await self._storage.get(_RUN_COLLECTION, run_id)
        if raw is None:
            return {
                "type": "agent.run.get.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        # Owner check via the parent goal
        goal = await self.get_goal(raw["goal_id"])
        if goal is None or goal.owner_user_id != conn.user_ctx.user_id:
            return {
                "type": "agent.run.get.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }
        return {
            "type": "agent.run.get.result",
            "ref": frame.get("id"),
            "ok": True,
            "run": raw,
        }

    # ── Trigger plumbing ──────────────────────────────────────────

    def _scheduler_job_name(self, goal_id: str) -> str:
        return f"agent_goal_{goal_id}"

    async def _arm_trigger(self, goal: Goal) -> None:
        """Arm a goal's trigger if it has one and is enabled."""
        if goal.status != GoalStatus.ENABLED:
            return
        if goal.trigger_type == "time":
            self._arm_time_trigger(goal)
        elif goal.trigger_type == "event":
            self._arm_event_trigger(goal)
        # else: no trigger — manual-only goal

    async def _disarm_trigger(self, goal: Goal) -> None:
        """Remove any active trigger for this goal."""
        if goal.trigger_type == "time":
            self._disarm_time_trigger(goal.id)
        elif goal.trigger_type == "event":
            self._disarm_event_trigger(goal.id)

    def _arm_time_trigger(self, goal: Goal) -> None:
        if self._scheduler is None or goal.trigger_config is None:
            return
        cfg = goal.trigger_config
        kind = cfg.get("kind", "interval")
        if kind == "interval":
            schedule = Schedule.every(seconds=float(cfg.get("seconds", 3600)))
        elif kind == "daily_at":
            schedule = Schedule.daily_at(
                hour=int(cfg.get("hour", 0)),
                minute=int(cfg.get("minute", 0)),
            )
        elif kind == "hourly_at":
            schedule = Schedule.hourly_at(minute=int(cfg.get("minute", 0)))
        else:
            logger.warning("unknown TIME trigger kind: %s", kind)
            return
        name = self._scheduler_job_name(goal.id)
        # add_job is not idempotent on name — remove first if present.
        if self._scheduler.get_job(name) is not None:
            self._scheduler.remove_job(name)
        callback = self._make_trigger_callback(goal.id, "time", {})
        self._scheduler.add_job(
            name=name,
            schedule=schedule,
            callback=callback,
            owner=goal.owner_user_id,
        )

    def _disarm_time_trigger(self, goal_id: str) -> None:
        if self._scheduler is None:
            return
        name = self._scheduler_job_name(goal_id)
        if self._scheduler.get_job(name) is not None:
            self._scheduler.remove_job(name)

    def _arm_event_trigger(self, goal: Goal) -> None:
        if self._event_bus is None or goal.trigger_config is None:
            return
        event_type = goal.trigger_config.get("event_type")
        if not event_type:
            logger.warning("EVENT trigger for goal %s missing event_type", goal.id)
            return
        filter_spec = goal.trigger_config.get("filter")

        async def _on_event(event: Any) -> None:
            if not self._event_matches_filter(event, filter_spec):
                return
            # Re-fetch goal at fire time in case it was disabled/deleted
            current = await self.get_goal(goal.id)
            if current is None or current.status != GoalStatus.ENABLED:
                return
            await self._spawn_run(
                goal.id,
                "event",
                {"event_type": event.event_type, "event_data": event.data},
            )

        unsubscribe = self._event_bus.subscribe(event_type, _on_event)
        # If a subscription already exists for this goal, drop the old one
        old = self._event_bus_unsubscribers.pop(goal.id, None)
        if old is not None:
            try:
                old()
            except Exception:
                logger.warning("failed to unsubscribe old EVENT handler for %s", goal.id)
        self._event_bus_unsubscribers[goal.id] = unsubscribe

    def _disarm_event_trigger(self, goal_id: str) -> None:
        unsubscribe = self._event_bus_unsubscribers.pop(goal_id, None)
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:
                logger.warning("failed to unsubscribe EVENT handler for %s", goal_id)

    def _event_matches_filter(
        self,
        event: Any,
        filter_spec: dict[str, Any] | None,
    ) -> bool:
        if not filter_spec:
            return True
        field = filter_spec.get("field")
        op = filter_spec.get("op", "eq")
        expected = filter_spec.get("value")
        if not field:
            return True
        actual = (event.data or {}).get(field)
        if op == "eq":
            return actual == expected
        elif op == "neq":
            return actual != expected
        elif op == "in":
            return actual in (expected or [])
        elif op == "contains":
            return expected in (actual or "")
        else:
            logger.warning("unknown filter op: %s", op)
            return False

    def _make_trigger_callback(
        self,
        goal_id: str,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> JobCallback:
        async def _fire() -> None:
            await self._spawn_run(goal_id, triggered_by, trigger_context)

        return _fire

    async def _spawn_run(
        self,
        goal_id: str,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> None:
        """Spawn a tracked background task that runs the goal once.

        Skip-while-running: if the goal is already running, log and
        return without creating a new Run.
        """
        if goal_id in self._running_goals:
            logger.info(
                "skipping %s trigger for goal %s; previous run still active",
                triggered_by,
                goal_id,
            )
            return
        self._running_goals.add(goal_id)

        async def _do_run() -> None:
            try:
                await self._run_goal_internal(goal_id, triggered_by, trigger_context)
            finally:
                self._running_goals.discard(goal_id)

        import asyncio

        asyncio.create_task(_do_run())

    async def _run_goal_internal(
        self,
        goal_id: str,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> Run:
        """Internal run path used by triggers; mirrors run_goal_now but
        respects the trigger context.
        """
        # The trigger_context is currently unused beyond logging; future
        # phases may include it in the prompt for EVENT-triggered runs.
        if trigger_context:
            logger.info(
                "agent run for goal %s triggered by %s with context %s",
                goal_id,
                triggered_by,
                trigger_context,
            )
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")
        goal = await self.get_goal(goal_id)
        if goal is None or goal.status != GoalStatus.ENABLED:
            return Run(
                id="",
                goal_id=goal_id,
                triggered_by=triggered_by,
                started_at=datetime.now(UTC),
                status=RunStatus.FAILED,
                error="goal not in ENABLED state at run-start time",
            )
        run = Run(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            triggered_by=triggered_by,
            started_at=datetime.now(UTC),
            status=RunStatus.RUNNING,
        )
        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))
        try:
            user_message = self._build_initial_user_message(goal)
            result = await self._ai.chat(
                user_message=user_message,
                conversation_id=None,
                user_ctx=None,
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
            f'{goal.owner_user_id}. Your goal is named "{goal.name}". '
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
        "trigger_type": g.trigger_type,
        "trigger_config": g.trigger_config,
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
        trigger_type=d.get("trigger_type"),
        trigger_config=d.get("trigger_config"),
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


_COMPLETE_GOAL_TOOL = ToolDefinition(
    name="complete_goal",
    description=(
        "Mark an autonomous-agent goal as fully and permanently complete. "
        "Call this only when the goal has been fully achieved and no future "
        "runs of it are needed. The goal will stop accepting new runs after "
        "this call."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id to mark complete.",
        ),
        ToolParameter(
            name="reason",
            type=ToolParameterType.STRING,
            description=(
                "A short human-readable explanation of why you consider the "
                "goal complete. Surfaced to the goal's owner."
            ),
        ),
    ],
    required_role="user",
)
