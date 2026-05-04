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
