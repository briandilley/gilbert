"""Unit tests for AutonomousAgentService."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from gilbert.core.services.agent import AutonomousAgentService
from gilbert.interfaces.agent import Goal, GoalStatus, Run, RunStatus
from gilbert.interfaces.ai import AIProvider, ChatTurnResult, FileAttachment
from gilbert.interfaces.events import Event
from gilbert.interfaces.storage import StorageBackend

pytestmark = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        return lambda: None


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
async def service(sqlite_storage: StorageBackend) -> tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus]:
    ai = _FakeAIService()
    bus = _FakeEventBus()
    svc = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
        }
    )
    await svc.start(resolver)
    return svc, ai, bus


# ── CRUD tests ────────────────────────────────────────────────────


async def test_create_goal_persists_with_enabled_status(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, ai, _bus = service
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
    svc, _ai, _bus = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    await svc.update_goal(g.id, status=GoalStatus.COMPLETED)

    with pytest.raises(ValueError, match="completed"):
        await svc.run_goal_now(g.id)


async def test_run_goal_now_rejects_disabled_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
