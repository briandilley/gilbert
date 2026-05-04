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
        for handler in list(self.subscribers.get(event.event_type, [])):
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
