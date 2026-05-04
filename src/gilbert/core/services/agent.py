"""AutonomousAgentService — persists Goal/Run entities, executes goals via
AIService.chat(ai_call="agent.run"), exposes complete_goal as a tool.
"""

from __future__ import annotations

import asyncio
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
from gilbert.interfaces.events import Event, EventBusProvider
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

# TODO: ConfigParam-ify these once AgentService becomes Configurable.
_DEFAULT_MAX_WALL_CLOCK_S = 600.0   # 10 minutes per run
_DEFAULT_MAX_ROUNDS = 50            # agent-tuned; higher than chat's default


# TODO: Per CLAUDE.md "AI prompts are always configurable", this default
# should be exposed as a ``ConfigParam(multiline=True, ai_prompt=True)``
# on AutonomousAgentService once service config infrastructure is added
# (currently AgentService isn't ``Configurable``). For now it's a
# module constant — same shape ConfigurationService used pre-Configurable.
_DEFAULT_AUTHOR_INSTRUCTION_PROMPT = """\
You are helping a user write a clear, effective instruction for an
autonomous AI agent. The instruction tells the agent what to do, how
to know it has succeeded, and any constraints to respect.

You will receive:
1. The current draft of the instruction (may be empty for a new goal).
2. A natural-language change request describing what the user wants
   different about the instruction.

Return the complete revised instruction text — not a diff, not a
commentary, just the new full instruction text. The output will replace
the user's current draft directly.

Aim for:
- Plain language a capable person could follow without re-reading.
- Concrete success criteria when possible (e.g., "stop after notifying
  me, regardless of whether anything was found").
- Brevity — multiple paragraphs only when genuinely needed.
- Explicit mention of any tools the agent should prefer or avoid.
"""


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
        self._event_bus_unsubscribers: dict[str, list[Any]] = {}
        """goal_id → list of unsubscribe callables (one per subscribed event_type)."""

        self._running_goals: set[str] = set()
        """In-progress goal IDs to skip duplicate trigger fires."""

        self._observed_event_types: set[str] = set()
        """Event types seen on the bus since process start. Populated by
        a wildcard subscription registered in start()."""

        self._wildcard_unsubscribe: Any = None

        self._active_runs: dict[str, str] = {}
        """goal_id → currently-active run_id. Populated by ``_run_goal_internal``
        for the duration of a run; cleared in finally. Used by the
        ``complete_goal`` tool so it can flag the right Run entity rather
        than passing an empty run_id."""

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="autonomous_agent",
            capabilities=frozenset({"agent", "ai_tools", "ws_handlers"}),
            requires=frozenset(
                {"entity_storage", "event_bus", "ai_chat", "scheduler"}
            ),
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
        # Track every event_type seen — feeds the agent.event_types.list
        # RPC so the goal-create UI can suggest known events without us
        # maintaining a static registry.
        async def _record_event(event: Any) -> None:
            try:
                etype = getattr(event, "event_type", "") or ""
                if etype and not etype.startswith("notification."):
                    # Skip notification.* — they're per-user noise that
                    # would dominate the suggestions list.
                    self._observed_event_types.add(etype)
            except Exception:
                pass

        self._wildcard_unsubscribe = self._event_bus.subscribe_pattern("*", _record_event)

        await self._mark_orphaned_runs_failed()
        await self._rearm_enabled_goals()
        logger.info("AutonomousAgentService started")

    async def stop(self) -> None:
        if self._wildcard_unsubscribe is not None:
            try:
                self._wildcard_unsubscribe()
            except Exception:
                pass
            self._wildcard_unsubscribe = None
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
        cost_cap_usd: float | None = None,
        stateless: bool = False,
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
            cost_cap_usd=cost_cap_usd,
            stateless=stateless,
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
        cost_cap_usd: float | None = None,
        stateless: bool | None = None,
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
        if cost_cap_usd is not None:
            goal.cost_cap_usd = cost_cap_usd
        if stateless is not None:
            goal.stateless = stateless
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
        # Shield the run from outer task cancellation — if the WS RPC
        # handler calling us is cancelled (user navigates away, page
        # refresh, connection drop), the agent run should still finish
        # to its own conclusion (END_TURN, max-rounds, an explicit
        # cancel from inside the loop). Without this, an interrupted
        # run leaves a half-finished conversation and a misleading
        # "completed" Run entity.
        return await asyncio.shield(
            self._run_goal_internal(goal_id, "manual", {})
        )

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
        # Look up the active run for this goal so declare_goal_complete
        # can flag the right Run entity. ``_run_goal_internal`` populates
        # this dict for the lifetime of the run.
        run_id = self._active_runs.get(goal_id, "")
        ok = await self.declare_goal_complete(goal_id, run_id=run_id, reason=reason)
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
            "agent.goal.author_instruction": self._ws_goal_author_instruction,
            "agent.event_types.list": self._ws_event_types_list,
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
        trigger_type = frame.get("trigger_type")
        trigger_config = frame.get("trigger_config")
        # Validate trigger_type
        if trigger_type not in (None, "", "time", "event"):
            return {
                "type": "agent.goal.create.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "trigger_type must be 'time' or 'event'",
            }
        # Empty string normalizes to None (manual-only)
        if trigger_type == "":
            trigger_type = None
        cost_cap_usd_raw = frame.get("cost_cap_usd")
        cost_cap_usd: float | None = None
        if cost_cap_usd_raw is not None:
            try:
                cost_cap_usd = float(cost_cap_usd_raw)
            except (TypeError, ValueError):
                cost_cap_usd = None
        stateless_raw = frame.get("stateless")
        stateless = bool(stateless_raw) if stateless_raw is not None else False
        goal = await self.create_goal(
            owner_user_id=conn.user_ctx.user_id,
            name=name,
            instruction=instruction,
            profile_id=profile_id,
            trigger_type=trigger_type,
            trigger_config=trigger_config if isinstance(trigger_config, dict) else None,
            cost_cap_usd=cost_cap_usd,
            stateless=stateless,
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
        trigger_type = frame.get("trigger_type")
        trigger_config = frame.get("trigger_config")
        cost_cap_usd_raw = frame.get("cost_cap_usd")
        cost_cap_usd_update: float | None = None
        if cost_cap_usd_raw is not None:
            try:
                cost_cap_usd_update = float(cost_cap_usd_raw)
            except (TypeError, ValueError):
                cost_cap_usd_update = None
        stateless_raw = frame.get("stateless")
        stateless_update: bool | None = None
        if stateless_raw is not None:
            stateless_update = bool(stateless_raw)
        updated = await self.update_goal(
            goal_id,
            name=frame.get("name"),
            instruction=frame.get("instruction"),
            profile_id=frame.get("profile_id"),
            status=status_enum,
            trigger_type=trigger_type if trigger_type in (None, "time", "event") else None,
            trigger_config=trigger_config if isinstance(trigger_config, dict) else None,
            cost_cap_usd=cost_cap_usd_update,
            stateless=stateless_update,
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

    async def _ws_event_types_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the sorted list of event types observed since process
        start. Useful for populating event-trigger pickers in the UI.
        """
        return {
            "type": "agent.event_types.list.result",
            "ref": frame.get("id"),
            "ok": True,
            "event_types": sorted(self._observed_event_types),
        }

    async def _ws_goal_author_instruction(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Rewrite a goal's instruction text via AI based on a user
        change request.

        Frame fields:
          ``goal_id`` — optional. When non-empty, owner-only auth is
            applied. When empty, the request is treated as drafting for
            a new (unsaved) goal — any logged-in user can call.
          ``current_text`` — the current instruction draft (may be empty)
          ``instruction`` — natural-language change request
          ``ai_profile`` — optional profile name (default: "standard")
        """
        if self._resolver is None:
            return {
                "type": "agent.goal.author_instruction.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "service not started",
            }

        goal_id = str(frame.get("goal_id", "") or "")
        current_text = str(frame.get("current_text", "") or "")
        instruction = str(frame.get("instruction", "") or "").strip()
        ai_profile = str(frame.get("ai_profile", "") or "").strip()

        if not instruction:
            return {
                "type": "agent.goal.author_instruction.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "instruction is required",
            }

        # Ownership check (only when authoring an existing goal)
        if goal_id:
            existing = await self.get_goal(goal_id)
            if existing is None or existing.owner_user_id != conn.user_ctx.user_id:
                return {
                    "type": "agent.goal.author_instruction.result",
                    "ref": frame.get("id"),
                    "ok": False,
                    "error": "not_found",
                }

        # Resolve AI sampling capability
        from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole

        ai_svc = self._resolver.get_capability("ai_chat")
        if not isinstance(ai_svc, AISamplingProvider):
            return {
                "type": "agent.goal.author_instruction.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "AI service unavailable",
            }

        profile_name = ai_profile or "standard"
        if not ai_svc.has_profile(profile_name):
            profile_name = "standard"

        user_message = (
            f"=== CURRENT INSTRUCTION ===\n{current_text}\n=== END CURRENT INSTRUCTION ===\n\n"
            f"=== CHANGE REQUEST ===\n{instruction}\n=== END CHANGE REQUEST ===\n\n"
            "Return the complete revised instruction below."
        )

        try:
            response = await ai_svc.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_message)],
                system_prompt=_DEFAULT_AUTHOR_INSTRUCTION_PROMPT,
                profile_name=profile_name,
                tools_override=[],
            )
        except Exception as exc:
            logger.exception("agent.goal.author_instruction AI call failed")
            return {
                "type": "agent.goal.author_instruction.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": f"AI call failed: {exc}",
            }

        new_text = (response.message.content or "").strip()
        if not new_text:
            return {
                "type": "agent.goal.author_instruction.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "AI returned empty instruction",
            }

        return {
            "type": "agent.goal.author_instruction.result",
            "ref": frame.get("id"),
            "ok": True,
            "new_text": new_text,
            "profile_used": profile_name,
        }

    # ── Restart safety ────────────────────────────────────────────

    async def _mark_orphaned_runs_failed(self) -> None:
        """Find any runs left in RUNNING state from a previous process
        and mark them FAILED.
        """
        if self._storage is None:
            return
        running = await self._storage.query(
            Query(
                collection=_RUN_COLLECTION,
                filters=[
                    Filter(field="status", op=FilterOp.EQ, value="running"),
                ],
                limit=10_000,
            )
        )
        for raw in running:
            raw["status"] = "failed"
            raw["error"] = "process_restarted"
            raw["ended_at"] = datetime.now(UTC).isoformat()
            await self._storage.put(_RUN_COLLECTION, raw["id"], raw)
        if running:
            logger.info(
                "marked %d orphaned RUNNING runs as FAILED on startup",
                len(running),
            )

    async def _rearm_enabled_goals(self) -> None:
        """Re-arm triggers for every enabled goal on startup."""
        goals = await self.list_goals()
        for g in goals:
            if g.status != GoalStatus.ENABLED:
                continue
            if g.trigger_type:
                try:
                    await self._arm_trigger(g)
                except Exception:
                    logger.exception(
                        "failed to re-arm trigger for goal %s on startup",
                        g.id,
                    )

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
        cfg = goal.trigger_config

        # Support both shapes:
        # - new:    {"event_types": ["a", "b", ...]}
        # - legacy: {"event_type": "a"}
        event_types: list[str] = []
        et_plural = cfg.get("event_types")
        if isinstance(et_plural, list):
            event_types = [str(x) for x in et_plural if isinstance(x, str) and x]
        elif cfg.get("event_type"):
            event_types = [str(cfg["event_type"])]

        if not event_types:
            logger.warning("EVENT trigger for goal %s has no event_types", goal.id)
            return

        filter_spec = cfg.get("filter")

        # Drop any prior subscriptions before re-arming
        self._disarm_event_trigger(goal.id)

        unsubscribers: list[Any] = []
        for et in event_types:
            handler = self._make_event_handler(goal.id, et, filter_spec)
            unsub = self._event_bus.subscribe(et, handler)
            unsubscribers.append(unsub)
        self._event_bus_unsubscribers[goal.id] = unsubscribers

    def _make_event_handler(
        self,
        goal_id: str,
        event_type: str,
        filter_spec: dict[str, Any] | None,
    ) -> Any:
        """Build a closure that fires _spawn_run for one event type."""

        async def _on_event(event: Any) -> None:
            if not self._event_matches_filter(event, filter_spec):
                return
            current = await self.get_goal(goal_id)
            if current is None or current.status != GoalStatus.ENABLED:
                return
            await self._spawn_run(
                goal_id,
                "event",
                {
                    "event_type": getattr(event, "event_type", event_type),
                    "event_data": getattr(event, "data", {}),
                },
            )

        return _on_event

    def _disarm_event_trigger(self, goal_id: str) -> None:
        unsubs = self._event_bus_unsubscribers.pop(goal_id, None)
        if not unsubs:
            return
        for unsub in unsubs:
            try:
                unsub()
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
        # Decide the conversation_id this run will use upfront so the UI
        # can deep-link to the live conversation as soon as the run is
        # listed. Without this, in-progress runs have an empty
        # conversation_id and the UI has nowhere to navigate.
        if goal.stateless:
            run_conv_id = str(uuid.uuid4())
        elif goal.conversation_id:
            run_conv_id = goal.conversation_id
        else:
            run_conv_id = str(uuid.uuid4())

        # Ensure the conversation exists in storage with source="agent"
        # tagging so the regular chat conversation list can exclude it
        # and the agent UI can identify agent-owned conversations.
        # If the row already exists (subsequent runs of a stateful goal),
        # patch the field; if not, create a stub.
        conv_collection = "ai_conversations"
        existing_conv_row = await self._storage.get(conv_collection, run_conv_id)
        if existing_conv_row is None:
            now_iso = datetime.now(UTC).isoformat()
            await self._storage.put(
                conv_collection,
                run_conv_id,
                {
                    "id": run_conv_id,
                    "user_id": goal.owner_user_id,
                    "title": goal.name,
                    "messages": [],
                    "ui_blocks": [],
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "source": "agent",
                    "agent_goal_id": goal.id,
                },
            )
        elif existing_conv_row.get("source") != "agent":
            existing_conv_row["source"] = "agent"
            existing_conv_row["agent_goal_id"] = goal.id
            await self._storage.put(conv_collection, run_conv_id, existing_conv_row)

        run = Run(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            triggered_by=triggered_by,
            started_at=datetime.now(UTC),
            status=RunStatus.RUNNING,
            conversation_id=run_conv_id,
        )
        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))
        await self._event_bus.publish(
            Event(
                event_type="agent.run.started",
                data={
                    "goal_id": goal_id,
                    "run_id": run.id,
                    "owner_user_id": goal.owner_user_id,
                    "triggered_by": triggered_by,
                },
                source="autonomous_agent",
                timestamp=run.started_at,
            )
        )

        # Track the active run for this goal so the complete_goal tool
        # can flag the right Run entity (the tool dispatcher only gets
        # the goal_id from the model — it doesn't know which run is in
        # flight). Cleared in finally regardless of success/failure.
        self._active_runs[goal_id] = run.id
        result = None
        try:
            user_message = self._build_initial_user_message(goal)
            max_wall_clock_s = (
                goal.max_wall_clock_s_override
                if goal.max_wall_clock_s_override is not None
                else _DEFAULT_MAX_WALL_CLOCK_S
            )
            try:
                result = await asyncio.wait_for(
                    self._ai.chat(
                        user_message=user_message,
                        conversation_id=run_conv_id,
                        user_ctx=None,
                        ai_call=_AI_CALL_NAME,
                        ai_profile=goal.profile_id,
                        max_tool_rounds=(
                            goal.max_rounds_override
                            if goal.max_rounds_override is not None
                            else _DEFAULT_MAX_ROUNDS
                        ),
                    ),
                    timeout=max_wall_clock_s,
                )
            except TimeoutError:
                run.status = RunStatus.TIMED_OUT
                run.error = (
                    f"wall-clock budget {max_wall_clock_s}s exceeded; "
                    f"chat was cancelled mid-stream"
                )
                run.ended_at = datetime.now(UTC)
                # Persist the run as TIMED_OUT and update goal counters
                await self._storage.put(
                    _RUN_COLLECTION, run.id, _run_to_dict(run)
                )
                # Re-read goal for fresh state then update counters
                fresh_goal_raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
                fresh_goal = (
                    _goal_from_dict(fresh_goal_raw) if fresh_goal_raw else goal
                )
                fresh_goal.run_count += 1
                fresh_goal.last_run_at = run.ended_at
                fresh_goal.last_run_status = run.status
                fresh_goal.updated_at = datetime.now(UTC)
                await self._storage.put(
                    _GOAL_COLLECTION, fresh_goal.id, _goal_to_dict(fresh_goal)
                )
                self._active_runs.pop(goal_id, None)
                await self._event_bus.publish(
                    Event(
                        event_type="agent.run.completed",
                        data={
                            "goal_id": goal_id,
                            "run_id": run.id,
                            "owner_user_id": fresh_goal.owner_user_id,
                            "status": run.status.value,
                            "rounds_used": run.rounds_used,
                            "tokens_in": run.tokens_in,
                            "tokens_out": run.tokens_out,
                            "interrupted": False,
                        },
                        source="autonomous_agent",
                        timestamp=run.ended_at,
                    )
                )
                return run
            run.final_message_text = result.response_text
            # Sanity check: chat() should have used the id we passed.
            # If not, prefer what chat() actually wrote to the conversation
            # store (we want the id that has the messages).
            if result.conversation_id and result.conversation_id != run.conversation_id:
                run.conversation_id = result.conversation_id
            # Re-assert source="agent" tag in case chat() rewrote the
            # conversation row from scratch.
            try:
                conv_after = await self._storage.get("ai_conversations", run.conversation_id)
                if conv_after is not None and conv_after.get("source") != "agent":
                    conv_after["source"] = "agent"
                    conv_after["agent_goal_id"] = goal.id
                    await self._storage.put("ai_conversations", run.conversation_id, conv_after)
            except Exception:
                logger.warning("failed to re-tag agent conversation %s", run.conversation_id)
            if result.turn_usage:
                run.tokens_in = int(result.turn_usage.get("input_tokens", 0))
                run.tokens_out = int(result.turn_usage.get("output_tokens", 0))
            run.rounds_used = len(result.rounds) + 1
            if getattr(result, "interrupted", False):
                # AIService caught a CancelledError mid-stream and emitted
                # the [INTERRUPTED BY USER] sentinel as the final assistant
                # message. The chat returned successfully but the run did
                # not actually finish its work.
                run.status = RunStatus.FAILED
                run.error = "interrupted"
            else:
                run.status = RunStatus.COMPLETED
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = repr(exc)
            logger.exception("agent run failed: goal=%s run=%s", goal_id, run.id)
        finally:
            self._active_runs.pop(goal_id, None)

        run.ended_at = datetime.now(UTC)

        # Re-read the goal from storage in case ``declare_goal_complete``
        # mutated it mid-run (the complete_goal tool flips status to
        # COMPLETED). Without this re-read, the local ``goal`` variable
        # holds the pre-run state and we'd clobber the COMPLETED status
        # when persisting counter updates below.
        fresh_goal_raw = await self._storage.get(_GOAL_COLLECTION, goal_id)
        if fresh_goal_raw is not None:
            fresh_goal = _goal_from_dict(fresh_goal_raw)
        else:
            fresh_goal = goal

        # Mirror declare_goal_complete state onto the run if it was set
        # via the tool during this run. ``declare_goal_complete`` already
        # set run.complete_goal_called via the run_id we registered in
        # _active_runs, but if the goal was completed by some other path,
        # this catches it.
        if (
            fresh_goal.status == GoalStatus.COMPLETED
            and fresh_goal.completed_reason
            and not run.complete_goal_called
        ):
            run.complete_goal_called = True
            run.complete_reason = fresh_goal.completed_reason

        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))

        # Skip conversation capture for stateless goals — every run
        # creates a fresh conversation that the run entity references
        # but the goal does not.
        if (
            run.status == RunStatus.COMPLETED
            and not fresh_goal.stateless
            and not fresh_goal.conversation_id
            and run.conversation_id
        ):
            fresh_goal.conversation_id = run.conversation_id

        # Accumulate cost and enforce cap
        run_cost = 0.0
        if run.status != RunStatus.FAILED:  # don't bill failed runs that did nothing
            try:
                # result is set when the chat call succeeded — fall back to
                # 0 for TIMED_OUT/interrupted runs that may not have a usage stamp.
                if result is not None and result.turn_usage:
                    run_cost = float(result.turn_usage.get("cost_usd", 0.0) or 0.0)
            except (AttributeError, TypeError):
                pass

        fresh_goal.lifetime_cost_usd += run_cost
        fresh_goal.run_count += 1
        fresh_goal.last_run_at = run.ended_at
        fresh_goal.last_run_status = run.status
        fresh_goal.updated_at = datetime.now(UTC)

        cost_cap_exceeded = (
            fresh_goal.cost_cap_usd is not None
            and fresh_goal.lifetime_cost_usd >= fresh_goal.cost_cap_usd
        )
        if cost_cap_exceeded and fresh_goal.status == GoalStatus.ENABLED:
            fresh_goal.status = GoalStatus.DISABLED

        await self._storage.put(_GOAL_COLLECTION, fresh_goal.id, _goal_to_dict(fresh_goal))

        # If cost cap was just exceeded, disarm triggers and notify owner
        if cost_cap_exceeded:
            await self._disarm_trigger(fresh_goal)
            await self._notify_cost_cap_exceeded(fresh_goal)

        await self._event_bus.publish(
            Event(
                event_type="agent.run.completed",
                data={
                    "goal_id": goal_id,
                    "run_id": run.id,
                    "owner_user_id": fresh_goal.owner_user_id,
                    "status": run.status.value,
                    "rounds_used": run.rounds_used,
                    "tokens_in": run.tokens_in,
                    "tokens_out": run.tokens_out,
                    "interrupted": (run.status == RunStatus.FAILED and run.error == "interrupted"),
                },
                source="autonomous_agent",
                timestamp=run.ended_at or datetime.now(UTC),
            )
        )

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

    async def _notify_cost_cap_exceeded(self, goal: Goal) -> None:
        """Notify the goal's owner that the lifetime cost cap was hit
        and the goal has been auto-disabled.
        """
        if self._resolver is None:
            return
        from gilbert.interfaces.notifications import NotificationProvider, NotificationUrgency

        notif_svc = self._resolver.get_capability("notifications")
        if not isinstance(notif_svc, NotificationProvider):
            logger.warning(
                "cost cap exceeded for goal %s but notifications unavailable",
                goal.id,
            )
            return
        try:
            await notif_svc.notify_user(
                user_id=goal.owner_user_id,
                message=(
                    f"Goal '{goal.name}' has been disabled — its lifetime "
                    f"cost (${goal.lifetime_cost_usd:.2f}) exceeded the cap "
                    f"of ${goal.cost_cap_usd:.2f}."
                ),
                urgency=NotificationUrgency.URGENT,
                source="agent",
                source_ref={"goal_id": goal.id, "kind": "cost_cap"},
            )
        except Exception:
            logger.exception("failed to notify cost-cap on goal %s", goal.id)


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
        "conversation_id": g.conversation_id,
        "last_run_at": g.last_run_at.isoformat() if g.last_run_at else None,
        "last_run_status": g.last_run_status.value if g.last_run_status else None,
        "run_count": g.run_count,
        "completed_at": g.completed_at.isoformat() if g.completed_at else None,
        "completed_reason": g.completed_reason,
        "max_rounds_override": g.max_rounds_override,
        "max_wall_clock_s_override": g.max_wall_clock_s_override,
        "cost_cap_usd": g.cost_cap_usd,
        "lifetime_cost_usd": g.lifetime_cost_usd,
        "stateless": g.stateless,
    }


def _goal_from_dict(d: dict[str, Any]) -> Goal:
    last_run_status_raw = d.get("last_run_status")
    completed_at_raw = d.get("completed_at")
    last_run_at_raw = d.get("last_run_at")
    max_rounds_raw = d.get("max_rounds_override")
    max_wall_clock_raw = d.get("max_wall_clock_s_override")
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
        conversation_id=d.get("conversation_id", ""),
        last_run_at=datetime.fromisoformat(last_run_at_raw) if last_run_at_raw else None,
        last_run_status=RunStatus(last_run_status_raw) if last_run_status_raw else None,
        run_count=int(d.get("run_count", 0)),
        completed_at=datetime.fromisoformat(completed_at_raw) if completed_at_raw else None,
        completed_reason=d.get("completed_reason"),
        max_rounds_override=int(max_rounds_raw) if max_rounds_raw is not None else None,
        max_wall_clock_s_override=float(max_wall_clock_raw) if max_wall_clock_raw is not None else None,
        cost_cap_usd=d.get("cost_cap_usd"),
        lifetime_cost_usd=float(d.get("lifetime_cost_usd", 0.0) or 0.0),
        stateless=bool(d.get("stateless", False)),
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
