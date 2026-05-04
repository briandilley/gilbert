"""Unit tests for AutonomousAgentService."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from gilbert.core.services.agent import AutonomousAgentService
from gilbert.interfaces.agent import GoalStatus, RunStatus
from gilbert.interfaces.ai import ChatTurnResult, FileAttachment
from gilbert.interfaces.events import Event
from gilbert.interfaces.storage import StorageBackend

pytestmark = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────


class _FakeEventBus:
    """Captures published events and supports subscribe/dispatch for trigger tests."""

    def __init__(self) -> None:
        self.published: list[Event] = []
        self.subscribers: dict[str, list[Any]] = {}

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        # Don't auto-dispatch; tests call dispatch() explicitly when they want
        # to simulate a published event reaching subscribers.

    def subscribe(self, event_type: str, handler: Any) -> Any:
        self.subscribers.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            handlers = self.subscribers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        return self.subscribe(pattern, handler)

    async def dispatch(self, event: Event) -> None:
        """Test-only: deliver an event to its subscribers."""
        # Exact-type subscribers
        for handler in list(self.subscribers.get(event.event_type, [])):
            await handler(event)
        # Wildcard subscribers
        for handler in list(self.subscribers.get("*", [])):
            await handler(event)


class _FakeScheduler:
    """Minimal SchedulerProvider stub for tests.

    Records add_job/remove_job calls so trigger tests can assert on them.
    """

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.jobs: dict[str, Any] = {}

    def add_job(
        self,
        name: str,
        schedule: Any,
        callback: Any,
        system: bool = False,
        enabled: bool = True,
        owner: str = "",
    ) -> Any:
        if name in self.jobs:
            raise ValueError(f"Job '{name}' already registered")
        self.added.append(
            {
                "name": name,
                "schedule": schedule,
                "callback": callback,
                "system": system,
                "enabled": enabled,
                "owner": owner,
            }
        )
        self.jobs[name] = callback
        # Return something JobInfo-shaped (only what callers actually inspect)
        from types import SimpleNamespace

        return SimpleNamespace(name=name, schedule=schedule, owner=owner)

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.removed.append(name)
        self.jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        return None

    def disable_job(self, name: str) -> None:
        return None

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.jobs.values())

    def get_job(self, name: str) -> Any:
        return self.jobs.get(name)

    async def run_now(self, name: str) -> None:
        cb = self.jobs.get(name)
        if cb is not None:
            await cb()


class _FakeEventBusProvider:
    def __init__(self, bus: _FakeEventBus) -> None:
        self.bus = bus


class _FakeStorageProvider:
    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend
        self.raw_backend = backend

    def create_namespaced(self, namespace: str) -> StorageBackend:
        return self.backend


class _FakeAIService:
    """Stub AIProvider that records calls and returns a canned ChatTurnResult."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response_text = "done"
        self.conversation_id = "conv-fake"

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        user_ctx: Any = None,
        system_prompt: str | None = None,
        ai_call: str | None = None,
        attachments: list[FileAttachment] | None = None,
        model: str = "",
        backend_override: str = "",
        ai_profile: str = "",
    ) -> ChatTurnResult:
        self.calls.append(
            {
                "user_message": user_message,
                "conversation_id": conversation_id,
                "ai_call": ai_call,
                "ai_profile": ai_profile,
            }
        )
        return ChatTurnResult(
            response_text=self.response_text,
            conversation_id=self.conversation_id,
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=False,
            model="fake-model",
            turn_usage={"input_tokens": 50, "output_tokens": 20, "cost_usd": 0.001},
        )


class _FakeResolver:
    def __init__(self, capabilities: dict[str, Any]) -> None:
        self._caps = capabilities

    def require_capability(self, key: str) -> Any:
        if key not in self._caps:
            raise RuntimeError(f"missing capability: {key}")
        return self._caps[key]

    def get_capability(self, key: str) -> Any:
        return self._caps.get(key)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def service(
    sqlite_storage: StorageBackend,
) -> tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler]:
    ai = _FakeAIService()
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    svc = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler,
        }
    )
    await svc.start(resolver)
    return svc, ai, bus, scheduler


# ── CRUD tests ────────────────────────────────────────────────────


async def test_create_goal_persists_with_enabled_status(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    goal = await svc.create_goal(
        owner_user_id="u_alice",
        name="Watch invoices",
        instruction="Check for overdue invoices and notify me.",
        profile_id="default",
    )
    assert goal.id
    assert goal.owner_user_id == "u_alice"
    assert goal.name == "Watch invoices"
    assert goal.profile_id == "default"
    assert goal.status == GoalStatus.ENABLED
    assert goal.run_count == 0
    assert isinstance(goal.created_at, datetime)


async def test_get_goal_returns_persisted_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="y", profile_id="default"
    )
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.id == g.id
    assert fetched.name == "x"


async def test_list_goals_filters_by_owner(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    a1 = await svc.create_goal(
        owner_user_id="u_alice", name="A1", instruction="i", profile_id="default"
    )
    a2 = await svc.create_goal(
        owner_user_id="u_alice", name="A2", instruction="i", profile_id="default"
    )
    b1 = await svc.create_goal(
        owner_user_id="u_bob", name="B1", instruction="i", profile_id="default"
    )

    alice = await svc.list_goals(owner_user_id="u_alice")
    assert {g.id for g in alice} == {a1.id, a2.id}

    everyone = await svc.list_goals()
    assert {g.id for g in everyone} == {a1.id, a2.id, b1.id}


async def test_delete_goal_removes_it(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    deleted = await svc.delete_goal(g.id)
    assert deleted is True
    assert await svc.get_goal(g.id) is None
    # Re-deleting returns False
    assert await svc.delete_goal(g.id) is False


# ── Run execution tests ───────────────────────────────────────────


async def test_run_goal_now_invokes_ai_chat_with_correct_args(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="Investigate the topic and report back.",
        profile_id="my_profile",
    )

    run = await svc.run_goal_now(g.id)

    assert run.goal_id == g.id
    assert run.status == RunStatus.COMPLETED
    assert run.final_message_text == "done"
    assert run.conversation_id == "conv-fake"
    assert run.tokens_in == 50
    assert run.tokens_out == 20
    assert run.error is None
    assert isinstance(run.started_at, datetime)
    assert run.ended_at is not None

    # AIService.chat was called once with the right routing
    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["ai_profile"] == "my_profile"
    assert call["ai_call"] == "agent.run"
    # The user_message includes the goal instruction
    assert "Investigate the topic" in call["user_message"]


async def test_run_goal_now_updates_goal_run_count_and_last_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    await svc.run_goal_now(g.id)
    await svc.run_goal_now(g.id)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 2
    assert fetched.last_run_status == RunStatus.COMPLETED
    assert fetched.last_run_at is not None


async def test_run_goal_now_returns_failed_run_on_chat_exception(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    async def raising_chat(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("backend on fire")

    # Replace the chat method on the fake to raise
    ai.chat = raising_chat  # type: ignore[method-assign]

    run = await svc.run_goal_now(g.id)
    assert run.status == RunStatus.FAILED
    assert run.error is not None
    assert "backend on fire" in run.error


async def test_run_goal_now_rejects_completed_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    await svc.update_goal(g.id, status=GoalStatus.COMPLETED)

    with pytest.raises(ValueError, match="completed"):
        await svc.run_goal_now(g.id)


async def test_run_goal_now_rejects_disabled_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    await svc.update_goal(g.id, status=GoalStatus.DISABLED)

    with pytest.raises(ValueError, match="disabled"):
        await svc.run_goal_now(g.id)


# ── complete_goal tool tests ──────────────────────────────────────


async def test_declare_goal_complete_marks_goal_completed(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    run = await svc.run_goal_now(g.id)

    ok = await svc.declare_goal_complete(
        goal_id=g.id,
        run_id=run.id,
        reason="found and chased all overdue invoices",
    )
    assert ok is True

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.status == GoalStatus.COMPLETED
    assert fetched.completed_at is not None
    assert fetched.completed_reason == "found and chased all overdue invoices"


async def test_declare_goal_complete_idempotent(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    run = await svc.run_goal_now(g.id)

    ok1 = await svc.declare_goal_complete(g.id, run.id, "first")
    ok2 = await svc.declare_goal_complete(g.id, run.id, "second")
    assert ok1 is True
    assert ok2 is False  # already completed; second call is a no-op

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.completed_reason == "first"  # first wins


async def test_complete_goal_tool_definition_exposed(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    tools = svc.get_tools(user_ctx=None)
    names = {t.name for t in tools}
    assert "complete_goal" in names

    cg = next(t for t in tools if t.name == "complete_goal")
    param_names = {p.name for p in cg.parameters}
    assert "goal_id" in param_names
    assert "reason" in param_names


async def test_complete_goal_tool_executes(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    result = await svc.execute_tool(
        "complete_goal",
        {"goal_id": g.id, "reason": "all done"},
    )
    # The tool returns a status string the AI sees as a tool result
    assert "complete" in result.lower() or "ok" in result.lower()

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.status == GoalStatus.COMPLETED
    assert fetched.completed_reason == "all done"


# ── WS RPC tests ──────────────────────────────────────────────────


def _make_conn(user_id: str, level: int = 100) -> Any:
    """Build a minimal ws connection-shaped object for handler tests."""
    from gilbert.interfaces.auth import UserContext

    class _Conn:
        def __init__(self) -> None:
            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
            self.user_level = level

        @property
        def user_id(self) -> str:
            return self.user_ctx.user_id

    return _Conn()


async def test_ws_agent_goal_create_persists_owned_by_caller(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.create"]

    result = await handler(
        _make_conn("u_alice"),
        {
            "id": "f1",
            "name": "Watch X",
            "instruction": "watch X",
            "profile_id": "default",
        },
    )

    assert result is not None
    assert result["ok"] is True
    goal_id = result["goal"]["id"]

    fetched = await svc.get_goal(goal_id)
    assert fetched is not None
    assert fetched.owner_user_id == "u_alice"
    assert fetched.name == "Watch X"


async def test_ws_agent_goal_list_returns_only_callers_goals(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    a = await svc.create_goal(
        owner_user_id="u_alice", name="A", instruction="i", profile_id="default"
    )
    b = await svc.create_goal(
        owner_user_id="u_bob", name="B", instruction="i", profile_id="default"
    )

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.list"]
    result = await handler(_make_conn("u_alice"), {"id": "f1"})

    assert result is not None
    ids = {g["id"] for g in result["goals"]}
    assert a.id in ids
    assert b.id not in ids


async def test_ws_agent_goal_delete_owner_only(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="A", instruction="i", profile_id="default"
    )

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.delete"]

    # Bob attempts; should be rejected
    bob_result = await handler(_make_conn("u_bob"), {"id": "f1", "goal_id": g.id})
    assert bob_result is not None
    assert bob_result["ok"] is False

    # Goal still exists
    assert await svc.get_goal(g.id) is not None

    # Alice succeeds
    alice_result = await handler(_make_conn("u_alice"), {"id": "f2", "goal_id": g.id})
    assert alice_result is not None
    assert alice_result["ok"] is True
    assert await svc.get_goal(g.id) is None


async def test_ws_agent_goal_run_now_triggers_a_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="A", instruction="i", profile_id="default"
    )

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.run_now"]
    result = await handler(_make_conn("u_alice"), {"id": "f1", "goal_id": g.id})

    assert result is not None
    assert result["ok"] is True
    assert result["run"]["status"] == "completed"
    assert result["run"]["goal_id"] == g.id


async def test_ws_agent_run_list_filters_by_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="A", instruction="i", profile_id="default"
    )
    await svc.run_goal_now(g.id)
    await svc.run_goal_now(g.id)

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.run.list"]
    result = await handler(_make_conn("u_alice"), {"id": "f1", "goal_id": g.id})

    assert result is not None
    assert len(result["runs"]) == 2
    for r in result["runs"]:
        assert r["goal_id"] == g.id


# ── Trigger tests ─────────────────────────────────────────────────


async def test_create_time_trigger_goal_arms_scheduler_job(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="Hourly check",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 3600},
    )

    assert len(scheduler.added) == 1
    job = scheduler.added[0]
    assert job["name"] == f"agent_goal_{g.id}"
    assert job["owner"] == "u_alice"


async def test_create_daily_at_time_trigger_arms_scheduler_job(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="Morning brief",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "daily_at", "hour": 7, "minute": 0},
    )

    assert len(scheduler.added) == 1
    assert scheduler.added[0]["name"] == f"agent_goal_{g.id}"


async def test_disable_goal_disarms_scheduler_job(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 600},
    )
    assert len(scheduler.added) == 1

    await svc.update_goal(g.id, status=GoalStatus.DISABLED)

    assert scheduler.removed == [f"agent_goal_{g.id}"]


async def test_delete_goal_disarms_trigger(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 600},
    )

    await svc.delete_goal(g.id)

    assert scheduler.removed == [f"agent_goal_{g.id}"]


async def test_time_trigger_callback_spawns_a_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 60},
    )

    # Invoke the registered callback to simulate the scheduler firing
    await scheduler.run_now(f"agent_goal_{g.id}")

    # Allow the spawned task to complete
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1
    assert fetched.last_run_status == RunStatus.COMPLETED


async def test_skip_while_running_drops_concurrent_trigger(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    """If a trigger fires while a previous run is still in flight,
    the second tick is silently skipped (no duplicate Run entity).
    """
    svc, ai, _bus, scheduler = service

    # Make chat() block until we release it
    proceed = asyncio.Event()
    original_chat = ai.chat

    async def slow_chat(*args: Any, **kwargs: Any) -> Any:
        await proceed.wait()
        return await original_chat(*args, **kwargs)

    ai.chat = slow_chat  # type: ignore[method-assign]

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 60},
    )

    job_name = f"agent_goal_{g.id}"

    # Fire twice in quick succession
    task1 = asyncio.create_task(scheduler.run_now(job_name))
    await asyncio.sleep(0)  # let task1 enter _spawn_run and add to running set
    task2 = asyncio.create_task(scheduler.run_now(job_name))

    # Release the slow chat so task1 finishes
    proceed.set()
    await asyncio.gather(task1, task2)
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1  # second trigger was skipped


async def test_event_trigger_subscribes_and_fires_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="React to leads",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_type": "lead.created"},
    )

    # Now publish a matching event — note: tests use _FakeEventBus which has
    # a subscribe method we need to implement
    from datetime import UTC
    from datetime import datetime as _dt

    from gilbert.interfaces.events import Event

    ev = Event(
        event_type="lead.created",
        data={"lead_id": "L42"},
        source="crm",
        timestamp=_dt.now(UTC),
    )
    # _FakeEventBus.dispatch() — added in Task 4 to deliver events to subscribers
    await bus.dispatch(ev)
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1


async def test_event_trigger_filter_skips_non_matching(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="Watch high-value leads",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={
            "event_type": "lead.created",
            "filter": {"field": "value", "op": "eq", "value": "high"},
        },
    )

    from datetime import UTC
    from datetime import datetime as _dt

    from gilbert.interfaces.events import Event

    # Event with no value — filter rejects
    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={"lead_id": "L42"},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.02)
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 0

    # Event with matching value — filter accepts
    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={"lead_id": "L43", "value": "high"},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1


async def test_event_trigger_disarms_on_disable(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_type": "lead.created"},
    )

    await svc.update_goal(g.id, status=GoalStatus.DISABLED)

    from datetime import UTC
    from datetime import datetime as _dt

    from gilbert.interfaces.events import Event

    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 0


async def test_start_rearms_existing_enabled_goals(
    sqlite_storage: StorageBackend,
) -> None:
    """Enabled goals with triggers must be re-armed on service startup so
    the system survives restarts.
    """
    # Seed the storage with a goal directly (simulating a previous process
    # that created the goal then exited)
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    ai = _FakeAIService()

    # First service instance creates a goal
    svc1 = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler,
        }
    )
    await svc1.start(resolver)
    g = await svc1.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 600},
    )
    assert len(scheduler.added) == 1
    await svc1.stop()

    # Simulate a process restart with a fresh scheduler (jobs lost)
    scheduler2 = _FakeScheduler()
    svc2 = AutonomousAgentService()
    resolver2 = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler2,
        }
    )
    await svc2.start(resolver2)

    # The new scheduler should have the goal's trigger re-armed
    assert len(scheduler2.added) == 1
    assert scheduler2.added[0]["name"] == f"agent_goal_{g.id}"


async def test_start_marks_stale_running_runs_as_failed(
    sqlite_storage: StorageBackend,
) -> None:
    """A run left in RUNNING state across a process restart should be
    marked FAILED so the goal isn't permanently stuck.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    ai = _FakeAIService()

    svc1 = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler,
        }
    )
    await svc1.start(resolver)

    g = await svc1.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
    )

    # Insert a stale RUNNING run directly
    stale_run_id = "stale-run-1"
    started_at = _dt(2026, 5, 1, tzinfo=UTC)  # well in the past
    await sqlite_storage.put(
        "agent_runs",
        stale_run_id,
        {
            "id": stale_run_id,
            "goal_id": g.id,
            "triggered_by": "manual",
            "started_at": started_at.isoformat(),
            "status": "running",
            "conversation_id": "",
            "ended_at": None,
            "final_message_text": None,
            "rounds_used": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "error": None,
            "complete_goal_called": False,
            "complete_reason": None,
        },
    )

    await svc1.stop()

    # Restart
    svc2 = AutonomousAgentService()
    resolver2 = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": _FakeScheduler(),
        }
    )
    await svc2.start(resolver2)

    raw = await sqlite_storage.get("agent_runs", stale_run_id)
    assert raw is not None
    assert raw["status"] == "failed"
    assert "process_restarted" in (raw.get("error") or "")


async def test_ws_agent_goal_create_with_time_trigger(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service
    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.create"]

    result = await handler(
        _make_conn("u_alice"),
        {
            "id": "f1",
            "name": "Hourly check",
            "instruction": "i",
            "profile_id": "default",
            "trigger_type": "time",
            "trigger_config": {"kind": "interval", "seconds": 3600},
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["goal"]["trigger_type"] == "time"
    assert len(scheduler.added) == 1


# ── Materialized conversation tests ───────────────────────────────


async def test_first_run_captures_new_conversation_id_on_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    assert g.conversation_id == ""

    # First run: chat() returns conversation_id "conv-fake"
    await svc.run_goal_now(g.id)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.conversation_id == "conv-fake"


async def test_subsequent_runs_reuse_goal_conversation_id(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    await svc.run_goal_now(g.id)
    # First call passed conversation_id=None
    assert ai.calls[-1]["conversation_id"] is None

    await svc.run_goal_now(g.id)
    # Second call passes the captured conversation_id
    assert ai.calls[-1]["conversation_id"] == "conv-fake"


# ── author_instruction tests ──────────────────────────────────────


async def test_author_instruction_rewrites_via_complete_one_shot(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service

    # The fake AIService needs to expose has_profile + complete_one_shot
    # to satisfy the AISamplingProvider isinstance check. Patch them on
    # the shared fake.
    from gilbert.interfaces.ai import AIResponse, Message, MessageRole, StopReason, TokenUsage

    ai.has_profile = lambda name: True  # type: ignore[attr-defined]
    captured_calls: list[dict[str, Any]] = []

    async def fake_complete(
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
        tools_override: list[Any] | None = None,
    ) -> AIResponse:
        captured_calls.append(
            {
                "system_prompt": system_prompt,
                "profile_name": profile_name,
                "user_message_content": messages[0].content if messages else "",
            }
        )
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="Revised: do X better."),
            model="fake",
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    ai.complete_one_shot = fake_complete  # type: ignore[attr-defined]

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.author_instruction"]

    # No goal_id — drafting a new goal
    result = await handler(
        _make_conn("u_alice"),
        {
            "id": "f1",
            "goal_id": "",
            "current_text": "do X",
            "instruction": "be more specific",
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["new_text"] == "Revised: do X better."

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert "be more specific" in call["user_message_content"]
    assert "do X" in call["user_message_content"]
    assert call["profile_name"] == "standard"


async def test_author_instruction_owner_only_for_existing_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service

    # Wire up complete_one_shot stub
    from gilbert.interfaces.ai import AIResponse, Message, MessageRole, StopReason, TokenUsage

    ai.has_profile = lambda name: True  # type: ignore[attr-defined]

    async def fake_complete(**kwargs: Any) -> AIResponse:
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="fake",
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    ai.complete_one_shot = fake_complete  # type: ignore[attr-defined]

    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.author_instruction"]

    # Bob tries to author Alice's goal — should be rejected
    bob_result = await handler(
        _make_conn("u_bob"),
        {"id": "f1", "goal_id": g.id, "current_text": "i", "instruction": "rewrite"},
    )
    assert bob_result is not None
    assert bob_result["ok"] is False
    assert "not_found" in str(bob_result.get("error", ""))

    # Alice succeeds
    alice_result = await handler(
        _make_conn("u_alice"),
        {"id": "f2", "goal_id": g.id, "current_text": "i", "instruction": "rewrite"},
    )
    assert alice_result is not None
    assert alice_result["ok"] is True


async def test_author_instruction_requires_instruction(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, _scheduler = service
    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.author_instruction"]
    result = await handler(
        _make_conn("u_alice"),
        {"id": "f1", "goal_id": "", "current_text": "x", "instruction": ""},
    )
    assert result is not None
    assert result["ok"] is False
    assert "instruction" in result["error"].lower()


# ── Multi-event trigger tests ─────────────────────────────────────


async def test_event_trigger_with_multiple_event_types_subscribes_to_all(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="React to leads or invoices",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_types": ["lead.created", "invoice.overdue"]},
    )

    from datetime import UTC
    from datetime import datetime as _dt

    from gilbert.interfaces.events import Event

    # Both event types should fire the goal
    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1

    await bus.dispatch(
        Event(
            event_type="invoice.overdue",
            data={},
            source="billing",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 2

    # Unrelated event does not fire
    await bus.dispatch(
        Event(
            event_type="weather.changed",
            data={},
            source="x",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 2


async def test_event_trigger_legacy_singular_still_works(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    """Goals persisted with the old shape (singular event_type) must still
    arm correctly after a service restart.
    """
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_type": "lead.created"},
    )

    from datetime import UTC
    from datetime import datetime as _dt

    from gilbert.interfaces.events import Event

    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={},
            source="x",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1


async def test_disarm_event_trigger_releases_all_subscriptions(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_types": ["a.x", "b.y", "c.z"]},
    )

    # Three subscriptions registered
    assert len(svc._event_bus_unsubscribers[g.id]) == 3

    await svc.update_goal(g.id, status=GoalStatus.DISABLED)

    # All released
    assert g.id not in svc._event_bus_unsubscribers


async def test_observed_event_types_populated_by_wildcard(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    from datetime import UTC
    from datetime import datetime as _dt

    from gilbert.interfaces.events import Event

    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={},
            source="x",
            timestamp=_dt.now(UTC),
        )
    )
    await bus.dispatch(
        Event(
            event_type="invoice.overdue",
            data={},
            source="x",
            timestamp=_dt.now(UTC),
        )
    )
    # notification.* events are filtered out
    await bus.dispatch(
        Event(
            event_type="notification.received",
            data={"user_id": "u"},
            source="x",
            timestamp=_dt.now(UTC),
        )
    )

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.event_types.list"]
    result = await handler(_make_conn("u_alice"), {"id": "f1"})

    assert result is not None
    assert result["ok"] is True
    types = set(result["event_types"])
    assert "lead.created" in types
    assert "invoice.overdue" in types
    assert "notification.received" not in types  # filtered


# ── Interrupted-run honoring ──────────────────────────────────────


async def test_interrupted_chat_marks_run_failed_not_completed(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    """When AIService.chat() returns ChatTurnResult(interrupted=True) —
    e.g. because the WS handler task was cancelled mid-stream — the Run
    must be marked FAILED with error="interrupted" rather than COMPLETED
    with empty text. Otherwise the goal looks like it succeeded when it
    actually didn't.
    """
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    # Patch the fake to return interrupted=True
    from gilbert.interfaces.ai import ChatTurnResult

    async def interrupted_chat(*args: Any, **kwargs: Any) -> ChatTurnResult:
        return ChatTurnResult(
            response_text="[INTERRUPTED BY USER ...]",
            conversation_id="conv-x",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=True,
            model="fake",
            turn_usage={"input_tokens": 100, "output_tokens": 5},
        )

    ai.chat = interrupted_chat  # type: ignore[method-assign]

    run = await svc.run_goal_now(g.id)

    assert run.status == RunStatus.FAILED
    assert run.error == "interrupted"
    # The conversation_id should NOT be captured onto the goal — we
    # don't want the goal locked to an abandoned conversation.
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.conversation_id == ""


# ── Goal-status persistence across complete_goal tool calls ───────


async def test_complete_goal_tool_persists_goal_status_after_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    """When the agent calls complete_goal mid-run, the resulting
    COMPLETED status must survive the run's end-of-loop counter update.
    Previously the run loop re-wrote the goal from a stale local copy,
    clobbering the COMPLETED status back to ENABLED.
    """
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    # Make the fake chat call invoke the complete_goal tool mid-run via
    # the service's own execute_tool path — this mirrors how real chat()
    # would dispatch a model-emitted tool call.
    from gilbert.interfaces.ai import ChatTurnResult

    async def chat_that_completes_goal(*args: Any, **kwargs: Any) -> ChatTurnResult:
        # Simulate the agent's chat run: invoke complete_goal during it.
        await svc.execute_tool(
            "complete_goal",
            {"goal_id": g.id, "reason": "all done"},
        )
        return ChatTurnResult(
            response_text="goal complete",
            conversation_id="conv-x",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=False,
            model="fake",
            turn_usage={"input_tokens": 100, "output_tokens": 5},
        )

    ai.chat = chat_that_completes_goal  # type: ignore[method-assign]

    run = await svc.run_goal_now(g.id)

    # The run completed normally
    assert run.status == RunStatus.COMPLETED

    # Goal status MUST be COMPLETED, not clobbered back to ENABLED
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.status == GoalStatus.COMPLETED
    assert fetched.completed_reason == "all done"
    assert fetched.completed_at is not None
    # Counter updates still happened
    assert fetched.run_count == 1


async def test_complete_goal_tool_flags_active_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    """When complete_goal fires from inside a chat() call, the active
    Run gets complete_goal_called=True and complete_reason set, instead
    of being silently dropped because run_id was empty.
    """
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    from gilbert.interfaces.ai import ChatTurnResult

    async def chat_that_completes_goal(*args: Any, **kwargs: Any) -> ChatTurnResult:
        await svc.execute_tool(
            "complete_goal",
            {"goal_id": g.id, "reason": "shipped"},
        )
        return ChatTurnResult(
            response_text="ok",
            conversation_id="conv-y",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=False,
            model="fake",
            turn_usage={"input_tokens": 50, "output_tokens": 5},
        )

    ai.chat = chat_that_completes_goal  # type: ignore[method-assign]

    run = await svc.run_goal_now(g.id)
    assert run.complete_goal_called is True
    assert run.complete_reason == "shipped"

    # Re-fetch from storage to confirm persistence
    raw = await svc._storage.get("agent_runs", run.id)
    assert raw is not None
    assert raw["complete_goal_called"] is True
    assert raw["complete_reason"] == "shipped"


# ── Wall-clock budget tests ───────────────────────────────────────


async def test_run_times_out_when_chat_exceeds_wall_clock_budget(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
    )
    # Override wall-clock cap to a tiny value so we trip it deterministically
    await svc.update_goal(g.id, name="x")  # no-op to force re-save
    raw = await svc._storage.get("agent_goals", g.id)
    raw["max_wall_clock_s_override"] = 0.05
    await svc._storage.put("agent_goals", g.id, raw)

    # Make chat sleep longer than the budget
    async def slow_chat(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.5)
        from gilbert.interfaces.ai import ChatTurnResult
        return ChatTurnResult(
            response_text="late",
            conversation_id="conv-x",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=False,
            model="fake",
            turn_usage={"input_tokens": 1, "output_tokens": 1},
        )

    ai.chat = slow_chat  # type: ignore[method-assign]

    run = await svc.run_goal_now(g.id)
    assert run.status == RunStatus.TIMED_OUT
    assert "0.05" in (run.error or "") or "exceeded" in (run.error or "")

    # Goal counters still updated
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1
    assert fetched.last_run_status == RunStatus.TIMED_OUT
