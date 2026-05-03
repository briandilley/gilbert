# Autonomous Agent — Phase 4a: AutonomousAgentService (manual-run only) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Build a minimum-viable `AutonomousAgentService` that lets a user create a `Goal` entity, manually trigger a run via `agent.goal.run_now` WS RPC, and have the agent execute the goal's instruction by invoking `AIService.chat()` with `ai_call="agent.run"`. Agent-specific tools (`complete_goal`, `notify_user`) ship as ordinary `ToolProvider` registrations so they show up in tool discovery automatically.

**Architecture:** A new core service with `Goal` + `Run` entities, CRUD via WS RPCs, manual run via `agent.goal.run_now`. `_run_goal` synthesizes a user message from the goal's instruction, calls `AIService.chat(ai_call="agent.run")` with the goal's profile, captures the result into a `Run` entity. `AutonomousAgentService` itself implements `ToolProvider` to expose `complete_goal(goal_id, reason)`. `NotificationService` gains a `notify_user(user_id, message, urgency)` tool. Triggers (TIME, EVENT) are explicitly out of scope — Phase 4b. Cross-run notes/digest and materialized conversations beyond the per-run chat conversation are out of scope — Phase 4c.

**Tech Stack:** Python 3.12+, `uv run`, pytest with real SQLite via `sqlite_storage` fixture.

**Out of scope for this plan:**
- Automatic triggers (TIME via scheduler, EVENT via event bus) — Phase 4b
- Cross-run notes / digest summarization — Phase 4c
- Per-goal materialized conversation (this plan creates a fresh conversation per run via `chat()`) — Phase 4c
- Workspace integration beyond what `chat()` already provides
- Frontend UI — Phases 3b and 5

---

## File Structure

**Create:**
- `src/gilbert/interfaces/agent.py` — `Goal`, `Run`, `GoalStatus`, `RunStatus`, `AgentProvider` protocol
- `src/gilbert/core/services/agent.py` — `AutonomousAgentService` class
- `tests/unit/core/test_agent_service.py` — service tests
- `.claude/memory/memory-autonomous-agent-service.md` — memory file

**Modify:**
- `src/gilbert/core/services/notifications.py` — add `notify_user` `ToolDefinition` + `execute_tool` (so AI sessions can ping users via existing tool discovery)
- `src/gilbert/core/app.py` — register `AutonomousAgentService`
- `src/gilbert/interfaces/acl.py` — declare `agent.` RPC frames at user level
- `.claude/memory/MEMORIES.md` — index the new memory

---

## Tasks

### Task 1: interfaces/agent.py — entities and protocol

**Files:**
- Create: `src/gilbert/interfaces/agent.py`

- [ ] **Step 1: Write the file**

```python
"""Agent interface — Goal, Run entity dataclasses and AgentProvider protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class GoalStatus(StrEnum):
    """Goal lifecycle states."""

    ENABLED = "enabled"
    """Active and runnable."""

    DISABLED = "disabled"
    """Paused; no new runs may be started."""

    COMPLETED = "completed"
    """Terminal: agent declared the goal done via complete_goal tool."""


class RunStatus(StrEnum):
    """Run lifecycle states."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Goal:
    """A persistent autonomous-agent task description."""

    id: str
    owner_user_id: str
    """Identity the agent runs as. RBAC, profile, user_memory inherit."""

    name: str
    instruction: str
    """The goal text — what to do, how to know you're done."""

    profile_id: str
    """AI profile name routed via AIService.chat(ai_profile=)."""

    status: GoalStatus
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    last_run_status: RunStatus | None = None
    run_count: int = 0
    completed_at: datetime | None = None
    completed_reason: str | None = None


@dataclass
class Run:
    """One execution of a goal."""

    id: str
    goal_id: str
    triggered_by: str
    """Currently always "manual" in Phase 4a."""

    started_at: datetime
    status: RunStatus
    conversation_id: str = ""
    """The chat conversation created by AIService.chat(); empty until
    run completes successfully."""

    ended_at: datetime | None = None
    final_message_text: str | None = None
    rounds_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    complete_goal_called: bool = False
    complete_reason: str | None = None


@runtime_checkable
class AgentProvider(Protocol):
    """Capability protocol for the autonomous-agent service."""

    async def create_goal(
        self,
        *,
        owner_user_id: str,
        name: str,
        instruction: str,
        profile_id: str,
    ) -> Goal: ...

    async def get_goal(self, goal_id: str) -> Goal | None: ...

    async def list_goals(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Goal]: ...

    async def run_goal_now(self, goal_id: str) -> Run:
        """Execute a goal once. Returns the completed (or failed) Run."""
        ...

    async def declare_goal_complete(
        self,
        goal_id: str,
        run_id: str,
        reason: str,
    ) -> bool:
        """Mark a goal as COMPLETED. Returns False if already completed
        or run_id doesn't match the active run."""
        ...
```

- [ ] **Step 2: Verify imports**

```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.interfaces.agent import Goal, Run, GoalStatus, RunStatus, AgentProvider; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/interfaces/agent.py
git commit -m "interfaces: add Goal, Run, AgentProvider for autonomous agent"
```

---

### Task 2: AutonomousAgentService skeleton

**Files:**
- Create: `src/gilbert/core/services/agent.py`

- [ ] **Step 1: Write the file**

```python
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
```

- [ ] **Step 2: Verify imports**

```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.core.services.agent import AutonomousAgentService; print('ok')"
```

Expected: `ok`. If imports fail, inspect the error and adapt — `ai_chat` may not be the right capability name (check `src/gilbert/core/services/ai.py` `service_info()` for the actual name).

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/core/services/agent.py
git commit -m "agent: add AutonomousAgentService skeleton"
```

---

### Task 3: Test scaffold + Goal CRUD methods (TDD)

Write the test scaffold and implement four CRUD methods in one task.

**Files:**
- Create: `tests/unit/core/test_agent_service.py`
- Modify: `src/gilbert/core/services/agent.py`

- [ ] **Step 1: Inspect existing test patterns**

```bash
cd /home/assistant/gilbert && grep -l "_FakeResolver\|require_capability" tests/unit/core/test_notification_service.py
```

Reuse the `_FakeResolver`, `_FakeStorageProvider`, `_FakeEventBus` patterns from `tests/unit/core/test_notification_service.py` — copy them into the new test file (don't extract to a shared module yet; that's a future cleanup).

- [ ] **Step 2: Write the test scaffold**

Write `tests/unit/core/test_agent_service.py`:

```python
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
```

- [ ] **Step 3: Run all four tests — expect FAIL**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 4 FAILs (`AttributeError: 'AutonomousAgentService' object has no attribute 'create_goal'` etc.).

- [ ] **Step 4: Implement CRUD methods**

In `src/gilbert/core/services/agent.py`, append to the class:

```python


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
```

And outside the class, append the serialization helpers:

```python


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
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: implement Goal CRUD with serialization helpers"
```

---

### Task 4: `run_goal_now` execution

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing test**

```python


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
```

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: the 4 CRUD tests still pass; the 5 new tests fail.

- [ ] **Step 3: Implement `run_goal_now`**

In `src/gilbert/core/services/agent.py`, append to the class:

```python


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
```

- [ ] **Step 4: Run all tests — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 9 passed (4 CRUD + 5 run_now).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: implement run_goal_now via AIService.chat"
```

---

### Task 5: `complete_goal` tool + `declare_goal_complete` method

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing tests**

```python


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
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `declare_goal_complete`, `get_tools`, `execute_tool`**

Append to the class:

```python


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
```

And outside the class, append the tool definition:

```python


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
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: add complete_goal tool and declare_goal_complete method"
```

---

### Task 6: `notify_user` tool on NotificationService

**Files:**
- Modify: `src/gilbert/core/services/notifications.py`
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/core/test_notification_service.py`:

```python


# ── notify_user as a tool ─────────────────────────────────────────


async def test_notification_service_exposes_notify_user_tool(
    service: NotificationService,
) -> None:
    tools = service.get_tools(user_ctx=None)
    names = {t.name for t in tools}
    assert "notify_user" in names

    nu = next(t for t in tools if t.name == "notify_user")
    param_names = {p.name for p in nu.parameters}
    assert "user_id" in param_names
    assert "message" in param_names
    assert "urgency" in param_names


async def test_notify_user_tool_executes_and_persists_notification(
    service: NotificationService, sqlite_storage: StorageBackend,
) -> None:
    result = await service.execute_tool(
        "notify_user",
        {"user_id": "u_alice", "message": "hello via tool", "urgency": "urgent"},
    )
    assert "notified" in result.lower() or "sent" in result.lower()

    bus: _FakeEventBus = service._test_bus  # type: ignore[attr-defined]
    assert len(bus.published) == 1
    ev = bus.published[0]
    assert ev.data["user_id"] == "u_alice"
    assert ev.data["message"] == "hello via tool"
    assert ev.data["urgency"] == "urgent"


async def test_notify_user_tool_invalid_urgency_falls_back_to_normal(
    service: NotificationService,
) -> None:
    result = await service.execute_tool(
        "notify_user",
        {"user_id": "u_alice", "message": "hi", "urgency": "kaboom"},
    )
    assert "notified" in result.lower() or "sent" in result.lower()
    bus: _FakeEventBus = service._test_bus  # type: ignore[attr-defined]
    assert bus.published[-1].data["urgency"] == "normal"
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `get_tools` and `execute_tool` on `NotificationService`**

In `src/gilbert/core/services/notifications.py`, add the imports at the top:

```python
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
```

Then update `service_info()` to advertise `ai_tools`:

```python
    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="notifications",
            capabilities=frozenset({"notifications", "ws_handlers", "ai_tools"}),
        )
```

And append a `tool_provider_name` attribute and `get_tools` / `execute_tool` methods to the class:

```python


    tool_provider_name = "notifications"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        return [_NOTIFY_USER_TOOL]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "notify_user":
            raise KeyError(f"unknown tool: {name}")
        user_id = str(arguments.get("user_id", "")).strip()
        message = str(arguments.get("message", "")).strip()
        if not user_id or not message:
            return "error: notify_user requires user_id and message"
        urgency_raw = str(arguments.get("urgency", "normal")).lower()
        try:
            urgency = NotificationUrgency(urgency_raw)
        except ValueError:
            urgency = NotificationUrgency.NORMAL
        await self.notify_user(
            user_id=user_id,
            message=message,
            urgency=urgency,
            source="ai",
        )
        return f"notified user {user_id} ({urgency.value}): {message}"
```

And outside the class, after `_deserialize` was removed (or wherever the helpers go), append:

```python


_NOTIFY_USER_TOOL = ToolDefinition(
    name="notify_user",
    description=(
        "Send a notification to a specific user. The notification appears "
        "in their notifications panel and (depending on urgency) may "
        "trigger a sound or visual alert. Use this when you need to get "
        "a user's attention about something important."
    ),
    parameters=[
        ToolParameter(
            name="user_id",
            type=ToolParameterType.STRING,
            description="The recipient's user id.",
        ),
        ToolParameter(
            name="message",
            type=ToolParameterType.STRING,
            description="Short, one-sentence message the user will see.",
        ),
        ToolParameter(
            name="urgency",
            type=ToolParameterType.STRING,
            description=(
                "Urgency level: 'info' (silent), 'normal' (default), or "
                "'urgent' (sound + visual alert)."
            ),
            required=False,
            enum=["info", "normal", "urgent"],
        ),
    ],
    required_role="user",
)
```

- [ ] **Step 4: Run notification tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 12 passed (9 prior + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
git commit -m "notifications: expose notify_user as an AI tool"
```

---

### Task 7: WS RPCs for goal CRUD and run management

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing tests**

```python


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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="A", instruction="i", profile_id="default"
    )

    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.delete"]

    # Bob attempts; should be rejected
    bob_result = await handler(
        _make_conn("u_bob"), {"id": "f1", "goal_id": g.id}
    )
    assert bob_result is not None
    assert bob_result["ok"] is False

    # Goal still exists
    assert await svc.get_goal(g.id) is not None

    # Alice succeeds
    alice_result = await handler(
        _make_conn("u_alice"), {"id": "f2", "goal_id": g.id}
    )
    assert alice_result is not None
    assert alice_result["ok"] is True
    assert await svc.get_goal(g.id) is None


async def test_ws_agent_goal_run_now_triggers_a_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus],
) -> None:
    svc, _ai, _bus = service
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
    svc, _ai, _bus = service
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
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement WS handlers**

In `src/gilbert/core/services/agent.py`, append to the class:

```python


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

    async def _ws_goal_create(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    async def _ws_goal_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        goals = await self.list_goals(owner_user_id=conn.user_ctx.user_id)
        return {
            "type": "agent.goal.list.result",
            "ref": frame.get("id"),
            "goals": [_goal_to_dict(g) for g in goals],
        }

    async def _ws_goal_get(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    async def _ws_goal_update(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    async def _ws_goal_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    async def _ws_goal_run_now(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    async def _ws_run_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    async def _ws_run_get(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
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
```

- [ ] **Step 2: Run all tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 18 passed (13 prior + 5 new).

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: implement WS RPCs for goal CRUD + run_now + run list/get"
```

---

### Task 8: ACL declarations + register service in app.py

**Files:**
- Modify: `src/gilbert/interfaces/acl.py`
- Modify: `src/gilbert/core/app.py`

- [ ] **Step 1: Add ACL entries**

In `src/gilbert/interfaces/acl.py`, find `DEFAULT_RPC_PERMISSIONS`. Add near the other user-level entries (around line 92):

```python
    # Autonomous agent: user-level; handlers enforce per-user ownership.
    "agent.": 100,
```

Run:
```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.interfaces.acl import DEFAULT_RPC_PERMISSIONS; print(DEFAULT_RPC_PERMISSIONS.get('agent.'))"
```

Expected: `100`.

- [ ] **Step 2: Register service in app.py**

```bash
cd /home/assistant/gilbert && grep -n "NotificationService\b" src/gilbert/core/app.py | head -5
```

Find the `NotificationService` registration. Add `AutonomousAgentService` registration immediately after, matching the same pattern:

```python
        from gilbert.core.services.agent import AutonomousAgentService

        self.service_manager.register(AutonomousAgentService())
```

Verify the app loads:
```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.core.app import *; print('ok')" 2>&1 | tail -5
```

Expected: `ok`.

- [ ] **Step 3: Run full repo test suite**

```bash
cd /home/assistant/gilbert && uv run pytest -q 2>&1 | tail -5
```

Expected: only the 2 pre-existing anthropic-plugin failures.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/interfaces/acl.py src/gilbert/core/app.py
git commit -m "agent: declare ACL and register AutonomousAgentService"
```

---

### Task 9: Quality gate + memory entry

**Files:**
- Modify (potentially) all phase 4a files for ruff
- Create: `.claude/memory/memory-autonomous-agent-service.md`
- Modify: `.claude/memory/MEMORIES.md`

- [ ] **Step 1: mypy + ruff**

```bash
cd /home/assistant/gilbert && uv run mypy src/gilbert/interfaces/agent.py src/gilbert/core/services/agent.py src/gilbert/core/services/notifications.py
cd /home/assistant/gilbert && uv run ruff format src/gilbert/interfaces/agent.py src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
cd /home/assistant/gilbert && uv run ruff check src/gilbert/interfaces/agent.py src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
```

Fix any errors introduced. Re-run tests if formatter changed anything:

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py tests/unit/core/test_notification_service.py -v
```

Expected: 18 + 12 = 30 passed.

- [ ] **Step 2: Write the memory file**

Write `.claude/memory/memory-autonomous-agent-service.md`:

```markdown
# AutonomousAgentService

## Summary
Persists Goal/Run entities and executes goals via ``AIService.chat(ai_call="agent.run")``.
Lives in ``src/gilbert/core/services/agent.py``.

## Details
**Capabilities declared:** ``agent`` (satisfies ``AgentProvider``),
``ai_tools`` (exposes the ``complete_goal`` tool), ``ws_handlers``.

**Ai call name:** ``agent.run`` — registered via ``ai_calls`` in
``ServiceInfo``. Operators can route this call to a distinct profile
via the AI profile assignment table.

**Execution model:** ``run_goal_now(goal_id)`` synthesizes a user
message from the goal's instruction, calls ``AIService.chat()`` with
``ai_call="agent.run"`` and ``ai_profile=goal.profile_id``, captures the
result into a ``Run`` entity. Each run gets its own chat conversation
(``Run.conversation_id`` from ``ChatTurnResult.conversation_id``). The
existing chat machinery handles tool dispatch, streaming, persistence,
and usage recording.

**Agent built-in tools (v1):**
- ``complete_goal(goal_id, reason)`` — exposed as a ``ToolProvider`` tool
  by AutonomousAgentService itself. Marks the goal as ``COMPLETED`` and
  prevents future runs.
- ``notify_user(user_id, message, urgency)`` — exposed by
  ``NotificationService`` (not ``AutonomousAgentService``); the agent
  discovers it through the normal AI tool-discovery flow.

**Goal lifecycle:** ``ENABLED`` → ``DISABLED`` (manual pause) → ``ENABLED``
or ``COMPLETED`` (terminal — no more runs).

**RBAC:** all ``agent.*`` WS RPCs are user-level. Handlers enforce
per-user ownership: a user can only see/run/edit/delete their own goals.
Set in ``DEFAULT_RPC_PERMISSIONS``.

**Triggers:** v1 supports manual triggers only (``agent.goal.run_now``
RPC). Automatic TIME and EVENT triggers are Phase 4b.

**Cross-run memory & materialized conversations:** v1 does not implement
notes, digests, or per-goal conversation materialization. Each run
creates its own fresh conversation. Phase 4c will materialize a single
conversation per goal and add a notes scratchpad + auto-digest.

## Related
- ``src/gilbert/interfaces/agent.py``
- ``src/gilbert/core/services/agent.py``
- ``tests/unit/core/test_agent_service.py``
- ``docs/superpowers/specs/2026-05-03-autonomous-agent-design.md``
- ``docs/superpowers/plans/2026-05-03-autonomous-agent-phase-4a-agent-service.md``
- ``.claude/memory/memory-notification-service.md`` (notify_user tool)
- ``.claude/memory/memory-agent-loop.md`` (run_loop primitive — currently
  unused by AgentService; available for future direct callers)
```

- [ ] **Step 3: Add to index**

Append to `.claude/memory/MEMORIES.md`:

```markdown
- [AutonomousAgentService](memory-autonomous-agent-service.md) — Goal/Run entities + run_goal_now via AIService.chat
```

- [ ] **Step 4: Commit memory + any formatting**

```bash
git add .claude/memory/memory-autonomous-agent-service.md .claude/memory/MEMORIES.md src/gilbert/interfaces/agent.py src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git diff --cached --quiet || git commit -m "agent: memory entry + ruff formatting pass"
```

---

## Phase 4a Complete

- AutonomousAgentService implements `AgentProvider`, exposes `complete_goal` tool, registered in app.py.
- 18 agent tests + 12 notification tests passing.
- `notify_user` tool on NotificationService discoverable by any AI session.
- Manual `run_now` works via WS RPC. Each run gets its own chat conversation.
- ACL declared user-level for `agent.*` frames.

What's missing (deferred):
- Automatic triggers (Phase 4b): scheduler-driven TIME triggers, event-bus EVENT triggers.
- Cross-run memory (Phase 4c): notes scratchpad, auto-digest summary, materialized per-goal conversation.
- Frontend UI (Phase 5).
