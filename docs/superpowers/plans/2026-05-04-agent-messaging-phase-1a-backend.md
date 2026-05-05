# Agent Messaging — Phase 1A: Backend Foundation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `AutonomousAgentService` end-to-end with a new `AgentService` that owns the new `Agent` entity (persona, system prompt, procedural rules, heartbeat, tool allowlist, avatar, lifetime cost), its sidecar entities (`AgentMemory`, `AgentTrigger`, `Commitment`, `InboxSignal`, `Run`), full WS RPC surface, and core agent tools (`complete_run`, `commitment_*`, `agent_memory_*`). The new service supports manual runs, time/event triggers, heartbeat, per-agent tool gating, and a configurable defaults bundle. The old `AutonomousAgentService`, its data, its memory file, and its tests are removed in this phase.

**Architecture:** A new core service `AgentService` in `src/gilbert/core/services/agent.py` that replaces `AutonomousAgentService` end-to-end. New entity collections (`agents`, `agent_memories`, `agent_triggers`, `agent_commitments`, `agent_inbox_signals`, `agent_runs`). `_run_agent_internal` runs the loop via `AIService.chat(ai_call="agent.run")` with system-prompt assembly that layers persona + system_prompt + procedural_rules + active-trigger context. Heartbeat is implemented as an `AgentTrigger(trigger_type="heartbeat")` registered with `SchedulerService`. Inbox signals are dispatched through a single `_signal_agent` function (idle → spawn run, busy → enqueue + drain between rounds). Phase 1A ships zero frontend changes — Phase 1B follows.

**Tech Stack:** Python 3.12+, `uv run` for everything, pytest with real SQLite via the existing `sqlite_storage` fixture, `StorageBackend`/`StorageProvider` capability protocol.

**Out of scope for Phase 1A:**
- Goals, war rooms, assignments, deliverables, dependencies — Phases 4 and 5
- Peer messaging (`agent_send_message`, `agent_delegate`, `agent_list`) — Phase 2
- Mid-stream interrupt — Phase 3
- Dreaming + memory promotion sweep (`agent_memory_review_and_promote` ships as a stub callable from runs but the gated dream-mode loop itself is Phase 7)
- Cross-user — Phase 6
- Frontend (the SPA continues to point at the old removed `agent.*` RPCs and renders broken; Phase 1B replaces it)

**Out of scope rationale (frontend):** Removing the old service and its RPCs in Phase 1A WILL break the existing `/agents` page until Phase 1B lands. That's deliberate — a coherent backend with a temporarily broken UI is preferable to a bridging shim that has to be undone. The two plans should be executed back-to-back.

---

## File Structure

**Create:**
- `src/gilbert/interfaces/agent.py` — REWRITE in place: `Agent`, `AgentMemory`, `AgentTrigger`, `Commitment`, `InboxSignal`, `Run`, all enums, `AgentProvider` protocol.
- `src/gilbert/core/services/agent.py` — REWRITE in place: `AgentService` class, replaces `AutonomousAgentService`.
- `tests/unit/test_agent_service.py` — REWRITE in place: tests for the new service.
- `tests/unit/test_agent_memory.py` — memory tools.
- `tests/unit/test_commitments.py` — commitment tools.
- `tests/unit/test_heartbeat.py` — heartbeat trigger & run flow.
- `tests/unit/test_agent_inbox.py` — InboxSignal lifecycle, _signal_agent dispatch, drain.
- `tests/unit/test_tool_gating.py` — per-agent allowlist enforcement.
- `.claude/memory/memory-agent-service.md` — replaces the old memory file.

**Modify:**
- `src/gilbert/core/app.py` — replace `AutonomousAgentService` registration with `AgentService`.
- `src/gilbert/interfaces/acl.py` — replace `agent.` RPC declarations with `agents.` declarations at user level.
- `.claude/memory/MEMORIES.md` — replace index entry for the old memory with the new one.

**Delete:**
- `.claude/memory/memory-autonomous-agent-service.md` — superseded.
- `tests/unit/core/test_agent_service.py` — old tests (path is `tests/unit/core/`, the new tests live at `tests/unit/`).
  - Note: only delete if it exists at that path; the new file at `tests/unit/test_agent_service.py` may collide — verify before deleting.

**Out-of-pocket changes that may surface during implementation:**
- Anywhere the old `AgentProvider` protocol was consumed: nothing in `core/` should depend on Goal-typed methods. If any does, refactor to use the new protocol or remove the dependency.
- Anywhere `agent.goal.run_now`, `agent.goal.create`, etc. RPCs are referenced in tests for non-agent services — those tests should be removed if obsolete or updated to use the new RPCs if still relevant.

---

## Tasks

### Task 1: Demolition — delete the old service

**Files:**
- Delete: `src/gilbert/core/services/agent.py` (in-place rewrite Tasks 9+; mark for replacement)
- Delete: `src/gilbert/interfaces/agent.py` (in-place rewrite Task 2; mark for replacement)
- Delete: `.claude/memory/memory-autonomous-agent-service.md`
- Delete: `tests/unit/core/test_agent_service.py` (and the directory if empty after)
- Modify: `src/gilbert/core/app.py` — remove the `AutonomousAgentService` import and registration. Leave a placeholder comment where the new registration will go.
- Modify: `.claude/memory/MEMORIES.md` — remove the line for `memory-autonomous-agent-service.md`.

The point of this task is to start from a clean state. The codebase will be temporarily broken (no agent service registered, frontend references missing RPCs) — subsequent tasks rebuild it.

- [ ] **Step 1: Find every reference to `AutonomousAgentService`**

Run: `grep -rn "AutonomousAgentService" src/ tests/ .claude/ docs/`
Expected: hits in `app.py`, `core/services/agent.py`, `interfaces/agent.py` (none — that file defines protocols), tests, the memory file, and possibly some docs/specs.

- [ ] **Step 2: Find every reference to old `agent.` RPCs**

Run: `grep -rn "\"agent\\.goal\\.\\|\"agent\\.run\\.\\|\"agent\\.event_types\\." src/ tests/`
Expected: hits in `interfaces/acl.py` (DEFAULT_RPC_PERMISSIONS), `core/services/agent.py` itself, possibly tests.

- [ ] **Step 3: Delete the memory file and remove its index entry**

```bash
rm .claude/memory/memory-autonomous-agent-service.md
```

Edit `.claude/memory/MEMORIES.md`: remove the line that links to it.

- [ ] **Step 4: Delete the old service file**

```bash
rm src/gilbert/core/services/agent.py
```

- [ ] **Step 5: Delete the old interface file**

```bash
rm src/gilbert/interfaces/agent.py
```

- [ ] **Step 6: Delete the old test file (if present)**

```bash
ls tests/unit/core/test_agent_service.py 2>/dev/null && rm tests/unit/core/test_agent_service.py
# Remove the directory if empty
rmdir tests/unit/core/ 2>/dev/null || true
```

- [ ] **Step 7: Remove old service registration from `app.py`**

Find in `src/gilbert/core/app.py`:
```python
from gilbert.core.services.agent import AutonomousAgentService
```
Remove this import.

Find the registration block (look for `AutonomousAgentService()`) and replace with a comment:
```python
# AgentService is registered below in Task 10
```

- [ ] **Step 8: Remove old `agent.` RPC declarations from acl.py**

In `src/gilbert/interfaces/acl.py`, find `DEFAULT_RPC_PERMISSIONS` and remove every entry whose key starts with `"agent."` (e.g., `"agent.goal.create"`, `"agent.run."`, etc.).

- [ ] **Step 9: Verify deletion is clean**

Run: `grep -rn "AutonomousAgentService\|gilbert\\.core\\.services\\.agent" src/ tests/ | grep -v __pycache__`
Expected: no hits (we deleted all imports too).

- [ ] **Step 10: Run remaining tests to see what breaks**

Run: `uv run pytest -q 2>&1 | tail -10`
Expected: many failures in tests that imported from the deleted modules. That is fine — we delete those tests in subsequent tasks if they depended on the old service. If a test fails for an unrelated reason, that's a real regression to fix.

Run: `uv run pytest --collect-only 2>&1 | grep -i "ImportError\|error" | head -5`
Expected: any remaining test files that fail to import will be flagged. Either delete (if obsolete) or update (if still relevant). For Phase 1A, anything that imported `gilbert.interfaces.agent` or `gilbert.core.services.agent` is likely obsolete; check each.

- [ ] **Step 11: Commit**

```bash
git add -u
git commit -m "agents: demolish AutonomousAgentService — Phase 1A start

Delete old service, interface, memory, and tests. Subsequent tasks
rebuild the new AgentService end-to-end. Codebase is intentionally
broken between this commit and Task 10 (service re-registration);
each task is a logical step in the rebuild and should be reviewed
in sequence.

Co-Authored-By: <agent>"
```

(Use the conventional Co-Authored-By trailer your repo uses.)

---

### Task 2: Define the new entity model

**Files:**
- Create: `src/gilbert/interfaces/agent.py`
- Test: `tests/unit/test_agent_entities.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_agent_entities.py`:

```python
"""Smoke tests for Agent entity dataclasses — round-trip + enum coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gilbert.interfaces.agent import (
    Agent,
    AgentMemory,
    AgentProvider,
    AgentStatus,
    AgentTrigger,
    Commitment,
    InboxSignal,
    MemoryState,
    Run,
    RunStatus,
)


def test_agent_dataclass_round_trip() -> None:
    a = Agent(
        id="ag_1",
        owner_user_id="usr_1",
        name="research-bot",
        role_label="Research Bot",
        persona="curious and methodical",
        system_prompt="follow up on every lead",
        procedural_rules="always cite sources",
        profile_id="standard",
        conversation_id="",
        status=AgentStatus.ENABLED,
        avatar_kind="emoji",
        avatar_value="🔬",
        lifetime_cost_usd=0.0,
        cost_cap_usd=None,
        tools_allowed=None,
        heartbeat_enabled=True,
        heartbeat_interval_s=1800,
        heartbeat_checklist="check the news",
        dream_enabled=False,
        dream_quiet_hours="22:00-06:00",
        dream_probability=0.1,
        dream_max_per_night=3,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert a.id == "ag_1"
    assert a.status is AgentStatus.ENABLED


def test_memory_state_enum_values() -> None:
    assert MemoryState.SHORT_TERM.value == "short_term"
    assert MemoryState.LONG_TERM.value == "long_term"


def test_run_status_terminal_states() -> None:
    terminals = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.TIMED_OUT}
    for status in RunStatus:
        if status is RunStatus.RUNNING:
            assert status not in terminals
        else:
            assert status in terminals


def test_agent_provider_is_runtime_checkable() -> None:
    """A test fake satisfies AgentProvider when it implements the methods."""

    class FakeAgentService:
        async def create_agent(self, **kwargs):
            return None

        async def get_agent(self, agent_id):
            return None

        async def list_agents(self, **kwargs):
            return []

        async def run_agent_now(self, agent_id, **kwargs):
            return None

    assert isinstance(FakeAgentService(), AgentProvider)


def test_inbox_signal_dataclass_round_trip() -> None:
    s = InboxSignal(
        id="sig_1",
        agent_id="ag_1",
        signal_kind="inbox",
        body="hello",
        sender_kind="user",
        sender_id="usr_1",
        sender_name="brian",
        source_conv_id="conv_1",
        source_message_id="msg_1",
        delegation_id="",
        metadata={},
        priority="normal",
        created_at=datetime.now(UTC),
        processed_at=None,
    )
    assert s.processed_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agent_entities.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gilbert.interfaces.agent'`.

- [ ] **Step 3: Write the entity definitions**

Create `src/gilbert/interfaces/agent.py`:

```python
"""Agent interface — entity dataclasses + AgentProvider protocol.

Replaces the old Goal/Run model with a multi-agent design:

- Agent — durable identity (persona, system prompt, procedural rules,
  heartbeat config, tool allowlist, avatar, lifetime cost).
- AgentMemory — per-agent learned facts; SHORT_TERM / LONG_TERM split.
- AgentTrigger — time / event / heartbeat trigger config rows.
- Commitment — opt-in short-lived follow-ups, surfaced in heartbeats.
- InboxSignal — durable wake-up tracking; message content lives in
  conversation rows, signal lifecycle (created → processed) lives here.
- Run — one execution of an agent's loop. Keyed by agent_id.

See docs/superpowers/specs/2026-05-04-agent-messaging-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


# ── Enums ────────────────────────────────────────────────────────────


class AgentStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class MemoryState(StrEnum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


# ── Entities ─────────────────────────────────────────────────────────


@dataclass
class Agent:
    """Durable agent identity. The addressable thing in the multi-agent
    model — peers send to agents by name, goals are assigned to agents.
    """

    id: str
    owner_user_id: str
    name: str                       # slug-friendly; unique within owner
    role_label: str                 # free-form descriptor
    persona: str                    # the "soul" — long-form identity prompt
    system_prompt: str              # role-specific instructions on persona
    procedural_rules: str           # workflow rulebook (AGENTS.md analogue)
    profile_id: str                 # AI profile (model + sampling params)
    conversation_id: str            # personal conv, lazy-created on first run
    status: AgentStatus
    avatar_kind: str                # "emoji" | "icon" | "image"
    avatar_value: str               # emoji char, lucide icon, or workspace_file:<id>
    lifetime_cost_usd: float
    cost_cap_usd: float | None      # auto-DISABLED when exceeded
    tools_allowed: list[str] | None # None = all tools; list = strict allowlist
    heartbeat_enabled: bool
    heartbeat_interval_s: int
    heartbeat_checklist: str
    dream_enabled: bool
    dream_quiet_hours: str
    dream_probability: float
    dream_max_per_night: int
    created_at: datetime
    updated_at: datetime


@dataclass
class AgentMemory:
    """Per-agent learned fact. Distinct from per-user user_memory.

    Recent SHORT_TERM entries are written by the agent during runs.
    LONG_TERM entries are loaded into prompt context (top-K). Promotion
    from SHORT_TERM → LONG_TERM happens during dream-mode runs in
    Phase 7; in Phase 1 the agent can promote/demote manually."""

    id: str
    agent_id: str
    content: str
    state: MemoryState
    kind: str                       # "fact" | "preference" | "decision" |
                                    # "daily" | "dream"
    tags: frozenset[str]
    score: float                    # promotion-engine scoring; defaults 0.0
    created_at: datetime
    last_used_at: datetime | None


@dataclass
class AgentTrigger:
    """Triggers that fire an agent run. Time/event are configurable;
    heartbeat is implicit per-agent (one row per agent when
    heartbeat_enabled=True)."""

    id: str
    agent_id: str
    trigger_type: str               # "time" | "event" | "heartbeat"
    trigger_config: dict[str, Any]  # heartbeat: {interval_s}; time/event:
                                    # {kind, seconds, hour, minute, ...}
    enabled: bool


@dataclass
class Commitment:
    """Self-imposed short-lived follow-up reminder. Surfaced in the
    heartbeat prompt's DUE COMMITMENTS block when due_at <= now and
    completed_at is None."""

    id: str
    agent_id: str
    content: str
    due_at: datetime
    created_at: datetime
    completed_at: datetime | None
    completion_note: str


@dataclass
class InboxSignal:
    """Durable wake-up tracking. Message content lives in chat rows;
    this row tracks 'signal X is pending for agent Y, hasn't been
    processed yet.'"""

    id: str
    agent_id: str
    signal_kind: str                # "inbox" | "deliverable_ready" |
                                    # "goal_assigned" | "delegation"
    body: str                       # human-readable summary
    sender_kind: str                # "agent" | "user" | "system"
    sender_id: str
    sender_name: str
    source_conv_id: str             # conv where the message content lives
    source_message_id: str
    delegation_id: str              # for delegations
    metadata: dict[str, Any]        # signal-specific extra
    priority: str                   # "urgent" | "normal"
    created_at: datetime
    processed_at: datetime | None


@dataclass
class Run:
    """One execution of an agent's loop, keyed by agent_id."""

    id: str
    agent_id: str
    triggered_by: str               # "manual" | "time" | "event" |
                                    # "heartbeat" | "dream" | "inbox" |
                                    # "deliverable_ready" | "goal_assigned"
    trigger_context: dict[str, Any]
    started_at: datetime
    status: RunStatus
    conversation_id: str
    delegation_id: str              # populated if handling a delegation
    ended_at: datetime | None
    final_message_text: str | None
    rounds_used: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error: str | None
    awaiting_user_input: bool
    pending_question: str | None
    pending_actions: list[dict[str, Any]] = field(default_factory=list)


# ── Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class AgentProvider(Protocol):
    """Capability protocol for the agent service. Consumers should
    isinstance-check against this rather than the concrete service."""

    async def create_agent(
        self,
        *,
        owner_user_id: str,
        name: str,
        **fields: Any,
    ) -> Agent: ...

    async def get_agent(self, agent_id: str) -> Agent | None: ...

    async def list_agents(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Agent]: ...

    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
    ) -> Run: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_agent_entities.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/agent.py tests/unit/test_agent_entities.py
git commit -m "agents: define new entity model (Agent, AgentMemory, AgentTrigger, Commitment, InboxSignal, Run)

Phase 1A Task 2 — pure dataclasses + AgentProvider protocol. No
service implementation yet.

Co-Authored-By: <agent>"
```

---

### Task 3: AgentService skeleton + service_info + lifecycle

**Files:**
- Create: `src/gilbert/core/services/agent.py`
- Test: `tests/unit/test_agent_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_agent_service.py`:

```python
"""AgentService — start/stop lifecycle + service_info."""

from __future__ import annotations

import pytest

from gilbert.core.services.agent import AgentService
from gilbert.interfaces.agent import AgentProvider
from gilbert.interfaces.service import ServiceInfo


def test_service_info_declares_capabilities() -> None:
    svc = AgentService()
    info = svc.service_info()
    assert isinstance(info, ServiceInfo)
    assert info.name == "agent"
    assert "agent" in info.capabilities       # AgentProvider satisfier
    assert "ai_tools" in info.capabilities    # ToolProvider
    assert "ws_handlers" in info.capabilities # WsHandlerProvider
    assert "entity_storage" in info.requires
    assert "ai_chat" in info.requires
    assert "scheduler" in info.requires
    assert "event_bus" in info.requires
    assert "agent.run" in info.ai_calls


def test_agent_service_satisfies_agent_provider() -> None:
    """Runtime-checkable protocol verification — the concrete service
    must satisfy AgentProvider so consumers can use the protocol."""
    svc = AgentService()
    assert isinstance(svc, AgentProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agent_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gilbert.core.services.agent'`.

- [ ] **Step 3: Write the service skeleton**

Create `src/gilbert/core/services/agent.py`:

```python
"""AgentService — owns Agent entities and runs agent loops via AIService.

Replaces the old AutonomousAgentService. See
docs/superpowers/specs/2026-05-04-agent-messaging-design.md and
docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.agent import (
    Agent,
    AgentStatus,
    AgentTrigger,
    Commitment,
    InboxSignal,
    Run,
    RunStatus,
)
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.scheduler import SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend, StorageProvider

logger = logging.getLogger(__name__)


# Collection names — single source of truth.
_AGENTS_COLLECTION = "agents"
_AGENT_MEMORIES_COLLECTION = "agent_memories"
_AGENT_TRIGGERS_COLLECTION = "agent_triggers"
_AGENT_COMMITMENTS_COLLECTION = "agent_commitments"
_AGENT_INBOX_SIGNALS_COLLECTION = "agent_inbox_signals"
_AGENT_RUNS_COLLECTION = "agent_runs"

_AI_CALL_NAME = "agent.run"


class AgentService(Service):
    """Replaces AutonomousAgentService.

    Capabilities declared:
    - ``agent`` — satisfies AgentProvider.
    - ``ai_tools`` — exposes complete_run, commitment_*, agent_memory_*,
      and (Phase 2+) agent_send_message, agent_delegate, agent_list.
    - ``ws_handlers`` — agents.* WS RPCs.
    """

    tool_provider_name = "agent"

    config_namespace = "agent_service"
    config_category = "Intelligence"

    slash_namespace = "agents"

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None
        self._ai: AIProvider | None = None
        self._resolver: ServiceResolver | None = None
        self._scheduler: SchedulerProvider | None = None

        # Per-agent state — keys are agent_ids (owner-scoped, no leakage).
        self._running_agents: set[str] = set()
        """In-progress agent ids; duplicate trigger fires skip silently."""

        self._inboxes: dict[str, list[InboxSignal]] = {}
        """In-memory cache of unprocessed signals; rehydrated on start."""

        # Cached config — refreshed by on_config_changed.
        self._defaults: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="agent",
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

        logger.info("AgentService started")

    async def stop(self) -> None:
        logger.info("AgentService stopped")

    # ── AgentProvider stubs (filled in by later tasks) ───────────────

    async def create_agent(self, *, owner_user_id: str, name: str, **fields: Any) -> Agent:
        raise NotImplementedError("filled in by Task 5")

    async def get_agent(self, agent_id: str) -> Agent | None:
        raise NotImplementedError("filled in by Task 5")

    async def list_agents(self, *, owner_user_id: str | None = None) -> list[Agent]:
        raise NotImplementedError("filled in by Task 5")

    async def run_agent_now(self, agent_id: str, *, user_message: str | None = None) -> Run:
        raise NotImplementedError("filled in by Task 8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_agent_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_service.py
git commit -m "agents: AgentService skeleton — start/stop, service_info, capability protocol

Phase 1A Task 3. Lifecycle binds storage / event bus / ai_chat /
scheduler capabilities. CRUD methods are NotImplementedError stubs
filled in by Task 5; run_agent_now stub filled by Task 8.

Co-Authored-By: <agent>"
```

---

### Task 4: Register AgentService in app.py

**Files:**
- Modify: `src/gilbert/core/app.py`

- [ ] **Step 1: Find the registration site**

Run: `grep -n "register\|services\\." src/gilbert/core/app.py | head -30`
Expected: a list of `service_manager.register(...)` calls or similar. Find the point where services like `NotificationService`, `SchedulerService`, etc. are registered.

- [ ] **Step 2: Add import**

In `src/gilbert/core/app.py`:

```python
from gilbert.core.services.agent import AgentService
```

- [ ] **Step 3: Add registration**

After the `# AgentService is registered below in Task 10` placeholder from Task 1, replace it with the actual registration. Match the pattern of nearby service registrations (typically `await self.service_manager.register(AgentService())` or similar — copy the convention from `NotificationService`'s registration).

- [ ] **Step 4: Verify registration is recognized**

Write a one-shot smoke test under `tests/unit/test_agent_service.py`:

```python
async def test_agent_service_is_registered_in_app(tmp_path) -> None:
    """Verify AgentService is wired into the composition root."""
    from gilbert.core.app import Gilbert

    # Inspect via class introspection — the registration sequence
    # in Gilbert.start() is what actually wires it; we sanity-check
    # the import path by verifying the class is present.
    import gilbert.core.app as app_module
    src = open(app_module.__file__).read()
    assert "AgentService" in src
    assert "from gilbert.core.services.agent import AgentService" in src
```

Run: `uv run pytest tests/unit/test_agent_service.py::test_agent_service_is_registered_in_app -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/app.py tests/unit/test_agent_service.py
git commit -m "agents: register AgentService in composition root

Phase 1A Task 4.

Co-Authored-By: <agent>"
```

---

### Task 5: Agent CRUD storage methods + RBAC helper

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_service.py`

This task implements the in-Python CRUD methods on the service. WS RPC handlers come in Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_agent_service.py`:

```python
import pytest
from datetime import datetime, UTC
from gilbert.interfaces.agent import Agent, AgentStatus
# Use existing test fixtures for storage + service start; assume
# `agent_service_started` fixture exists or build one inline.


@pytest.fixture
async def started_agent_service(sqlite_storage_provider, event_bus, ai_provider, scheduler):
    """Start an AgentService against real fixtures.

    Uses the existing project pytest fixtures: ``sqlite_storage_provider``
    is the entity-storage fake/real backend, ``event_bus`` is the bus
    capability, ``ai_provider`` is a mocked AIProvider, ``scheduler``
    is a mocked SchedulerProvider. If those fixture names don't exist,
    look in conftest.py and adapt — every existing service test in
    tests/unit/ uses some variant of this.
    """
    from gilbert.core.services.agent import AgentService

    svc = AgentService()
    resolver = _build_test_resolver(
        entity_storage=sqlite_storage_provider,
        event_bus=event_bus,
        ai_chat=ai_provider,
        scheduler=scheduler,
    )
    await svc.start(resolver)
    yield svc
    await svc.stop()


async def test_create_agent_round_trip(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="research-bot",
        role_label="Research",
        persona="curious",
        system_prompt="follow up",
        procedural_rules="cite sources",
        profile_id="standard",
    )
    assert a.id
    assert a.owner_user_id == "usr_1"
    assert a.name == "research-bot"
    assert a.status is AgentStatus.ENABLED
    fetched = await svc.get_agent(a.id)
    assert fetched is not None
    assert fetched.name == "research-bot"


async def test_list_agents_filters_by_owner(started_agent_service):
    svc = started_agent_service
    await svc.create_agent(owner_user_id="usr_1", name="a1")
    await svc.create_agent(owner_user_id="usr_1", name="a2")
    await svc.create_agent(owner_user_id="usr_2", name="b1")

    only_usr_1 = await svc.list_agents(owner_user_id="usr_1")
    assert {a.name for a in only_usr_1} == {"a1", "a2"}

    only_usr_2 = await svc.list_agents(owner_user_id="usr_2")
    assert {a.name for a in only_usr_2} == {"b1"}

    everyone = await svc.list_agents()
    assert len(everyone) == 3


async def test_create_agent_unique_name_per_owner(started_agent_service):
    svc = started_agent_service
    await svc.create_agent(owner_user_id="usr_1", name="dup")
    with pytest.raises(ValueError, match="name already in use"):
        await svc.create_agent(owner_user_id="usr_1", name="dup")
    # Different owner — same name OK.
    await svc.create_agent(owner_user_id="usr_2", name="dup")


async def test_update_agent_patches_fields(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    updated = await svc.update_agent(a.id, {"role_label": "New Label", "persona": "new persona"})
    assert updated.role_label == "New Label"
    assert updated.persona == "new persona"
    assert updated.name == "x"  # unchanged


async def test_delete_agent_removes_row(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    deleted = await svc.delete_agent(a.id)
    assert deleted is True
    assert await svc.get_agent(a.id) is None


async def test_load_agent_for_caller_owner_match(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    found = await svc._load_agent_for_caller(a.id, caller_user_id="usr_1")
    assert found.id == a.id


async def test_load_agent_for_caller_owner_mismatch(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    with pytest.raises(PermissionError):
        await svc._load_agent_for_caller(a.id, caller_user_id="usr_2")
```

- [ ] **Step 2: Build the test resolver helper**

Inspect `tests/unit/conftest.py` and any existing `_build_test_resolver` helper used by similar service tests. The exact helper name varies per project. If none exists, create one in `tests/unit/conftest.py`:

```python
@pytest.fixture
def _build_test_resolver():
    """Return a callable that builds a ServiceResolver with the given
    capabilities. Used by service-level integration tests."""

    def _build(**caps):
        from gilbert.interfaces.service import ServiceResolver

        class _Resolver:
            def require_capability(self, name):
                if name not in caps:
                    raise LookupError(name)
                return caps[name]

            def get_capability(self, name):
                return caps.get(name)

            def get_all(self, name):
                return []

        return _Resolver()

    return _build
```

If a helper already exists with different name, adapt the test fixture to use it.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_agent_service.py -v`
Expected: FAIL with `NotImplementedError` on the CRUD methods.

- [ ] **Step 4: Implement CRUD on AgentService**

In `src/gilbert/core/services/agent.py`, add helpers and replace the stub methods:

```python
import re
import uuid

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _now() -> datetime:
    return datetime.now(UTC)


def _agent_to_dict(a: Agent) -> dict[str, Any]:
    """Storage row representation. ``status`` and other StrEnums
    serialize as their .value; datetimes as ISO strings."""
    return {
        "_id": a.id,
        "owner_user_id": a.owner_user_id,
        "name": a.name,
        "role_label": a.role_label,
        "persona": a.persona,
        "system_prompt": a.system_prompt,
        "procedural_rules": a.procedural_rules,
        "profile_id": a.profile_id,
        "conversation_id": a.conversation_id,
        "status": a.status.value,
        "avatar_kind": a.avatar_kind,
        "avatar_value": a.avatar_value,
        "lifetime_cost_usd": a.lifetime_cost_usd,
        "cost_cap_usd": a.cost_cap_usd,
        "tools_allowed": a.tools_allowed,
        "heartbeat_enabled": a.heartbeat_enabled,
        "heartbeat_interval_s": a.heartbeat_interval_s,
        "heartbeat_checklist": a.heartbeat_checklist,
        "dream_enabled": a.dream_enabled,
        "dream_quiet_hours": a.dream_quiet_hours,
        "dream_probability": a.dream_probability,
        "dream_max_per_night": a.dream_max_per_night,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _agent_from_dict(row: dict[str, Any]) -> Agent:
    return Agent(
        id=row["_id"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        role_label=row.get("role_label", ""),
        persona=row.get("persona", ""),
        system_prompt=row.get("system_prompt", ""),
        procedural_rules=row.get("procedural_rules", ""),
        profile_id=row.get("profile_id", "standard"),
        conversation_id=row.get("conversation_id", ""),
        status=AgentStatus(row.get("status", "enabled")),
        avatar_kind=row.get("avatar_kind", "emoji"),
        avatar_value=row.get("avatar_value", "🤖"),
        lifetime_cost_usd=float(row.get("lifetime_cost_usd", 0.0)),
        cost_cap_usd=row.get("cost_cap_usd"),
        tools_allowed=row.get("tools_allowed"),
        heartbeat_enabled=bool(row.get("heartbeat_enabled", True)),
        heartbeat_interval_s=int(row.get("heartbeat_interval_s", 1800)),
        heartbeat_checklist=row.get("heartbeat_checklist", ""),
        dream_enabled=bool(row.get("dream_enabled", False)),
        dream_quiet_hours=row.get("dream_quiet_hours", "22:00-06:00"),
        dream_probability=float(row.get("dream_probability", 0.1)),
        dream_max_per_night=int(row.get("dream_max_per_night", 3)),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


# ── On the AgentService class ────────────────────────────────────────

    async def create_agent(self, *, owner_user_id: str, name: str, **fields: Any) -> Agent:
        if self._storage is None:
            raise RuntimeError("not started")
        if not _NAME_PATTERN.match(name):
            raise ValueError(f"name {name!r} must match {_NAME_PATTERN.pattern}")

        # Uniqueness — same-owner, same-name collision rejected.
        existing = await self._storage.query(_AGENTS_COLLECTION, {"owner_user_id": owner_user_id, "name": name})
        if existing:
            raise ValueError(f"name already in use: {name}")

        defaults = self._defaults
        now = _now()
        a = Agent(
            id=f"ag_{uuid.uuid4().hex[:12]}",
            owner_user_id=owner_user_id,
            name=name,
            role_label=fields.get("role_label", ""),
            persona=fields.get("persona", defaults.get("default_persona", "")),
            system_prompt=fields.get("system_prompt", defaults.get("default_system_prompt", "")),
            procedural_rules=fields.get("procedural_rules", defaults.get("default_procedural_rules", "")),
            profile_id=fields.get("profile_id", defaults.get("default_profile_id", "standard")),
            conversation_id="",
            status=AgentStatus.ENABLED,
            avatar_kind=fields.get("avatar_kind", defaults.get("default_avatar_kind", "emoji")),
            avatar_value=fields.get("avatar_value", defaults.get("default_avatar_value", "🤖")),
            lifetime_cost_usd=0.0,
            cost_cap_usd=fields.get("cost_cap_usd"),
            tools_allowed=fields.get("tools_allowed", defaults.get("default_tools_allowed")),
            heartbeat_enabled=fields.get("heartbeat_enabled", True),
            heartbeat_interval_s=fields.get("heartbeat_interval_s", int(defaults.get("default_heartbeat_interval_s", 1800))),
            heartbeat_checklist=fields.get("heartbeat_checklist", defaults.get("default_heartbeat_checklist", "")),
            dream_enabled=fields.get("dream_enabled", bool(defaults.get("default_dream_enabled", False))),
            dream_quiet_hours=fields.get("dream_quiet_hours", defaults.get("default_dream_quiet_hours", "22:00-06:00")),
            dream_probability=fields.get("dream_probability", float(defaults.get("default_dream_probability", 0.1))),
            dream_max_per_night=fields.get("dream_max_per_night", int(defaults.get("default_dream_max_per_night", 3))),
            created_at=now,
            updated_at=now,
        )
        await self._storage.put(_AGENTS_COLLECTION, a.id, _agent_to_dict(a))
        return a

    async def get_agent(self, agent_id: str) -> Agent | None:
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return None
        return _agent_from_dict(row)

    async def list_agents(self, *, owner_user_id: str | None = None) -> list[Agent]:
        if self._storage is None:
            raise RuntimeError("not started")
        query = {} if owner_user_id is None else {"owner_user_id": owner_user_id}
        rows = await self._storage.query(_AGENTS_COLLECTION, query)
        return [_agent_from_dict(r) for r in rows]

    async def update_agent(self, agent_id: str, patch: dict[str, Any]) -> Agent:
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            raise KeyError(agent_id)
        # Apply patch — only known fields are accepted; unknown keys raise.
        ALLOWED = {
            "role_label", "persona", "system_prompt", "procedural_rules",
            "profile_id", "avatar_kind", "avatar_value", "cost_cap_usd",
            "tools_allowed", "heartbeat_enabled", "heartbeat_interval_s",
            "heartbeat_checklist", "dream_enabled", "dream_quiet_hours",
            "dream_probability", "dream_max_per_night",
        }
        for k, v in patch.items():
            if k not in ALLOWED:
                raise ValueError(f"field not patchable: {k}")
            row[k] = v
        row["updated_at"] = _now().isoformat()
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)
        return _agent_from_dict(row)

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete the agent and cascade-delete its memories, triggers,
        commitments, inbox signals, and runs.

        Phase 1A: no goals exist yet, so no goal-side cascade. Phase 4
        will add: do NOT delete the agent's goal assignments (other
        assignees may exist); instead remove the assignment row, and
        if the goal had no remaining DRIVER, transition it to BLOCKED.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return False
        await self._storage.delete(_AGENTS_COLLECTION, agent_id)
        # Cascade
        for coll in (
            _AGENT_MEMORIES_COLLECTION,
            _AGENT_TRIGGERS_COLLECTION,
            _AGENT_COMMITMENTS_COLLECTION,
            _AGENT_INBOX_SIGNALS_COLLECTION,
            _AGENT_RUNS_COLLECTION,
        ):
            for r in await self._storage.query(coll, {"agent_id": agent_id}):
                await self._storage.delete(coll, r["_id"])
        return True

    async def _load_agent_for_caller(
        self, agent_id: str, *, caller_user_id: str, admin: bool = False,
    ) -> Agent:
        """Fetch an agent and enforce ownership.

        Raises:
            KeyError: agent does not exist.
            PermissionError: agent exists but belongs to another user.
        """
        a = await self.get_agent(agent_id)
        if a is None:
            raise KeyError(agent_id)
        if not admin and a.owner_user_id != caller_user_id:
            raise PermissionError(
                f"agent {agent_id} not accessible to user {caller_user_id}"
            )
        return a
```

Adjust the import block at the top of the file to include `re`, `uuid`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_agent_service.py -v`
Expected: all 7 new tests pass plus the 2 from Task 3 = 9 total.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_service.py tests/unit/conftest.py
git commit -m "agents: AgentService CRUD + RBAC ownership helper

Phase 1A Task 5. create/get/list/update/delete + _load_agent_for_caller.
Same-owner same-name uniqueness enforced. Cascade delete of memories /
triggers / commitments / inbox-signals / runs.

Co-Authored-By: <agent>"
```

---

### Task 6: WS RPCs — agents.create / get / list / update / delete / set_status / run_now

**Files:**
- Modify: `src/gilbert/core/services/agent.py` — add WsHandlerProvider methods + handlers.
- Modify: `src/gilbert/interfaces/acl.py` — declare `agents.` permissions at user level.
- Modify: `tests/unit/test_agent_service.py` — RPC handler tests.

The WS frame router calls `get_ws_handlers()` to learn how to dispatch. We register handlers under `"agents.<verb>"` keys.

- [ ] **Step 1: Write failing RPC tests**

Append to `tests/unit/test_agent_service.py`:

```python
class _FakeConn:
    def __init__(self, user_id: str, user_level: int = 100):
        self.user_id = user_id
        self.user_level = user_level
        self.user_ctx = type("U", (), {"user_id": user_id, "roles": frozenset()})()


async def test_ws_rpc_create_agent_returns_id(started_agent_service):
    svc = started_agent_service
    handlers = svc.get_ws_handlers()
    assert "agents.create" in handlers

    conn = _FakeConn("usr_1")
    result = await handlers["agents.create"](
        conn, {"name": "x", "role_label": "Tester"},
    )
    assert "agent" in result
    assert result["agent"]["name"] == "x"
    assert result["agent"]["owner_user_id"] == "usr_1"


async def test_ws_rpc_list_filters_by_caller_unless_admin(started_agent_service):
    svc = started_agent_service
    h = svc.get_ws_handlers()

    # User 1 creates 2.
    await h["agents.create"](_FakeConn("usr_1"), {"name": "a1"})
    await h["agents.create"](_FakeConn("usr_1"), {"name": "a2"})
    # User 2 creates 1.
    await h["agents.create"](_FakeConn("usr_2"), {"name": "b1"})

    # User 1 sees their own only.
    res = await h["agents.list"](_FakeConn("usr_1"), {})
    assert {a["name"] for a in res["agents"]} == {"a1", "a2"}

    # Admin sees all.
    admin = _FakeConn("usr_admin", user_level=0)
    res = await h["agents.list"](admin, {})
    assert {a["name"] for a in res["agents"]} == {"a1", "a2", "b1"}


async def test_ws_rpc_update_rejects_cross_user(started_agent_service):
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["agents.create"](_FakeConn("usr_1"), {"name": "x"})
    agent_id = res["agent"]["_id"]
    with pytest.raises(PermissionError):
        await h["agents.update"](_FakeConn("usr_2"), {"agent_id": agent_id, "patch": {"role_label": "X"}})


async def test_ws_rpc_set_status_toggles(started_agent_service):
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["agents.create"](_FakeConn("usr_1"), {"name": "x"})
    agent_id = res["agent"]["_id"]
    out = await h["agents.set_status"](_FakeConn("usr_1"), {"agent_id": agent_id, "status": "disabled"})
    assert out["agent"]["status"] == "disabled"


async def test_ws_rpc_delete_cascades(started_agent_service, sqlite_storage_provider):
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["agents.create"](_FakeConn("usr_1"), {"name": "x"})
    agent_id = res["agent"]["_id"]
    out = await h["agents.delete"](_FakeConn("usr_1"), {"agent_id": agent_id})
    assert out["deleted"] is True
    assert await svc.get_agent(agent_id) is None
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_agent_service.py -v -k "ws_rpc"`
Expected: 5 failures — `get_ws_handlers` doesn't exist yet.

- [ ] **Step 3: Implement WsHandlerProvider on AgentService**

In `src/gilbert/core/services/agent.py`, add at class level (near the bottom of the class):

```python
    # ── WsHandlerProvider ────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "agents.create": self._ws_create,
            "agents.get": self._ws_get,
            "agents.list": self._ws_list,
            "agents.update": self._ws_update,
            "agents.delete": self._ws_delete,
            "agents.set_status": self._ws_set_status,
            "agents.run_now": self._ws_run_now,
            "agents.get_defaults": self._ws_get_defaults,
        }

    def _is_admin(self, conn: Any) -> bool:
        return getattr(conn, "user_level", 999) <= 0

    def _caller_user_id(self, conn: Any) -> str:
        uid = getattr(conn, "user_id", "") or ""
        if not uid:
            raise PermissionError("anonymous caller")
        return uid

    async def _ws_create(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller_user_id(conn)
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        # Drop unknown fields; create_agent accepts a tight allowlist.
        allowed_fields = {
            "role_label", "persona", "system_prompt", "procedural_rules",
            "profile_id", "avatar_kind", "avatar_value", "cost_cap_usd",
            "tools_allowed", "heartbeat_enabled", "heartbeat_interval_s",
            "heartbeat_checklist", "dream_enabled", "dream_quiet_hours",
            "dream_probability", "dream_max_per_night",
        }
        fields = {k: v for k, v in params.items() if k in allowed_fields}
        a = await self.create_agent(owner_user_id=owner, name=name, **fields)
        return {"agent": _agent_to_dict(a)}

    async def _ws_get(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        a = await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        return {"agent": _agent_to_dict(a)}

    async def _ws_list(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        admin = self._is_admin(conn)
        if admin and params.get("owner_user_id") is not None:
            agents = await self.list_agents(owner_user_id=str(params["owner_user_id"]))
        elif admin:
            agents = await self.list_agents()
        else:
            agents = await self.list_agents(owner_user_id=self._caller_user_id(conn))
        return {"agents": [_agent_to_dict(a) for a in agents]}

    async def _ws_update(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        patch = params.get("patch") or {}
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        a = await self.update_agent(agent_id, patch)
        return {"agent": _agent_to_dict(a)}

    async def _ws_delete(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        ok = await self.delete_agent(agent_id)
        return {"deleted": ok}

    async def _ws_set_status(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        status_raw = str(params.get("status", "")).strip()
        try:
            status = AgentStatus(status_raw)
        except ValueError:
            raise ValueError(f"unknown status: {status_raw}")
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        a = await self.update_agent(agent_id, {})  # touch updated_at
        # Direct status flip (not in patch ALLOWED set deliberately —
        # status flips go via this RPC, not through a generic patch).
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        row["status"] = status.value
        row["updated_at"] = _now().isoformat()
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)
        return {"agent": _agent_from_dict(row).__dict__ | {"status": status.value}}

    async def _ws_run_now(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        user_message = params.get("user_message")
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        run = await self.run_agent_now(agent_id, user_message=user_message)
        return {"run_id": run.id, "status": run.status.value}

    async def _ws_get_defaults(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"defaults": dict(self._defaults)}
```

Note: `_ws_set_status` returns a slightly hacky dict — replace with a proper helper if `_agent_to_dict` is preferred. Cleaning the return shape is a 2-line fix; functionally the test only inspects `["agent"]["status"]`.

Actually, simplify the set_status:

```python
    async def _ws_set_status(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        status_raw = str(params.get("status", "")).strip()
        try:
            status = AgentStatus(status_raw)
        except ValueError:
            raise ValueError(f"unknown status: {status_raw}")
        await self._load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        row["status"] = status.value
        row["updated_at"] = _now().isoformat()
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)
        return {"agent": _agent_to_dict(_agent_from_dict(row))}
```

- [ ] **Step 4: Add agents.* RPC permissions to acl.py**

In `src/gilbert/interfaces/acl.py`, find `DEFAULT_RPC_PERMISSIONS` and add (the value `100` is the user level; copy whatever constant the file uses):

```python
    "agents.": 100,  # all agents.* RPCs are user-level by default
```

Or if entries are listed individually:

```python
    "agents.create": 100,
    "agents.get": 100,
    "agents.list": 100,
    "agents.update": 100,
    "agents.delete": 100,
    "agents.set_status": 100,
    "agents.run_now": 100,
    "agents.get_defaults": 100,
```

Match the file's existing convention. (Phase 4 will add `goals.*` and `deliverables.*`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_agent_service.py -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/agent.py src/gilbert/interfaces/acl.py tests/unit/test_agent_service.py
git commit -m "agents: WS RPCs (agents.create/get/list/update/delete/set_status/run_now)

Phase 1A Task 6. Per-user RBAC enforced via _load_agent_for_caller;
admin sees-all on list. agents.run_now is wired but the actual run
is implemented in Task 8.

Co-Authored-By: <agent>"
```

---

### Task 7: AgentMemory — storage + tools (save/search/promote)

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Create: `tests/unit/test_agent_memory.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_agent_memory.py`:

```python
"""AgentMemory — save/search/promote tools."""

from __future__ import annotations

import pytest

from gilbert.interfaces.agent import MemoryState


async def test_save_memory_persists(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    mem = await svc.save_memory(
        agent_id=a.id,
        content="user prefers TypeScript",
        kind="preference",
        tags={"lang"},
    )
    assert mem.id
    assert mem.state is MemoryState.SHORT_TERM  # default
    assert mem.kind == "preference"
    assert "lang" in mem.tags


async def test_search_memory_returns_matches(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    await svc.save_memory(agent_id=a.id, content="user likes hot tea")
    await svc.save_memory(agent_id=a.id, content="user dislikes cilantro")

    out = await svc.search_memory(agent_id=a.id, query="tea")
    assert any("tea" in m.content for m in out)


async def test_promote_memory_changes_state(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    mem = await svc.save_memory(agent_id=a.id, content="durable fact")
    promoted = await svc.promote_memory(memory_id=mem.id, score=0.95)
    assert promoted.state is MemoryState.LONG_TERM
    assert promoted.score == pytest.approx(0.95)


async def test_memory_isolated_per_agent(started_agent_service):
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_1", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_1", name="a2")
    await svc.save_memory(agent_id=a1.id, content="agent 1 fact")
    a2_mems = await svc.search_memory(agent_id=a2.id, query="agent")
    assert a2_mems == []  # agent 2 doesn't see agent 1's memories
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_agent_memory.py -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement memory methods**

In `src/gilbert/core/services/agent.py`, add helpers + methods:

```python
from gilbert.interfaces.agent import AgentMemory, MemoryState


def _memory_to_dict(m: AgentMemory) -> dict[str, Any]:
    return {
        "_id": m.id,
        "agent_id": m.agent_id,
        "content": m.content,
        "state": m.state.value,
        "kind": m.kind,
        "tags": sorted(m.tags),
        "score": m.score,
        "created_at": m.created_at.isoformat(),
        "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
    }


def _memory_from_dict(row: dict[str, Any]) -> AgentMemory:
    return AgentMemory(
        id=row["_id"],
        agent_id=row["agent_id"],
        content=row.get("content", ""),
        state=MemoryState(row.get("state", "short_term")),
        kind=row.get("kind", "fact"),
        tags=frozenset(row.get("tags", [])),
        score=float(row.get("score", 0.0)),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=(
            datetime.fromisoformat(row["last_used_at"])
            if row.get("last_used_at") else None
        ),
    )


# ── On the AgentService class ────────────────────────────────────────

    async def save_memory(
        self,
        *,
        agent_id: str,
        content: str,
        kind: str = "fact",
        tags: frozenset[str] | set[str] | None = None,
        state: MemoryState = MemoryState.SHORT_TERM,
    ) -> AgentMemory:
        if self._storage is None:
            raise RuntimeError("not started")
        m = AgentMemory(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            content=content,
            state=state,
            kind=kind,
            tags=frozenset(tags or ()),
            score=0.0,
            created_at=_now(),
            last_used_at=None,
        )
        await self._storage.put(_AGENT_MEMORIES_COLLECTION, m.id, _memory_to_dict(m))
        return m

    async def search_memory(
        self,
        *,
        agent_id: str,
        query: str,
        limit: int = 20,
        state: MemoryState | None = None,
    ) -> list[AgentMemory]:
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(_AGENT_MEMORIES_COLLECTION, {"agent_id": agent_id})
        out: list[AgentMemory] = []
        q = query.lower()
        for r in rows:
            if state is not None and r.get("state") != state.value:
                continue
            content = str(r.get("content", "")).lower()
            if not q or q in content:
                out.append(_memory_from_dict(r))
            if len(out) >= limit:
                break
        # Sort: recency descending.
        out.sort(key=lambda m: m.created_at, reverse=True)
        return out[:limit]

    async def promote_memory(
        self,
        *,
        memory_id: str,
        score: float,
        state: MemoryState = MemoryState.LONG_TERM,
    ) -> AgentMemory:
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_MEMORIES_COLLECTION, memory_id)
        if row is None:
            raise KeyError(memory_id)
        row["state"] = state.value
        row["score"] = score
        await self._storage.put(_AGENT_MEMORIES_COLLECTION, memory_id, row)
        return _memory_from_dict(row)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_agent_memory.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_memory.py
git commit -m "agents: AgentMemory storage + save/search/promote methods

Phase 1A Task 7. Naive substring match for search; embedding-based
retrieval is a Phase 7 concern. Memory state defaults to SHORT_TERM
on save; promote_memory flips to LONG_TERM with a score.

Co-Authored-By: <agent>"
```

---

### Task 8: Run lifecycle — _run_agent_internal + run_agent_now

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_service.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_agent_service.py`:

```python
async def test_run_agent_now_creates_run_row(started_agent_service, ai_provider_mock):
    """Ensure run_agent_now spawns a run, calls AIService.chat, and
    persists a Run entity in `agent_runs` collection."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")

    # ai_provider_mock.chat is configured to return a successful turn
    # with a final assistant message and zero tool calls — the loop
    # END_TURNs immediately.
    run = await svc.run_agent_now(a.id, user_message="hello")
    assert run.agent_id == a.id
    assert run.triggered_by == "manual"

    # Wait briefly for the background-spawn to settle, since we use
    # asyncio.shield internally.
    import asyncio
    await asyncio.sleep(0.1)

    runs = await svc.list_runs(agent_id=a.id)
    assert len(runs) == 1
    assert runs[0].id == run.id
```

This test depends on a `ai_provider_mock` fixture; if not present in conftest, build a minimal one:

```python
# In tests/unit/conftest.py
@pytest.fixture
def ai_provider_mock():
    """Minimal AIProvider double whose chat() returns a canned successful turn."""
    class _AI:
        async def chat(self, *args, **kwargs):
            from gilbert.interfaces.ai import ChatTurnResult
            return ChatTurnResult(
                final_message_text="ok",
                conversation_id="conv_1",
                ui_blocks=[],
                tool_uses=[],
                turn_usage={"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "rounds": 1},
                interrupted=False,
            )

        def has_profile(self, name): return True

    return _AI()
```

(If the project uses different return types from `chat()`, mirror them.)

- [ ] **Step 2: Implement run_agent_now**

In `src/gilbert/core/services/agent.py`:

```python
    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
        triggered_by: str = "manual",
        trigger_context: dict[str, Any] | None = None,
    ) -> Run:
        """Synchronous entry — awaits the run to completion. Use only
        in tests or when the caller can block. Production callers
        should use start_agent_run."""
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")
        a = await self.get_agent(agent_id)
        if a is None:
            raise ValueError(f"agent not found: {agent_id}")
        if a.status is not AgentStatus.ENABLED:
            raise ValueError(f"agent {agent_id} is {a.status.value}")
        if agent_id in self._running_agents:
            raise ValueError(f"agent {agent_id} has a run in progress")

        self._running_agents.add(agent_id)
        try:
            run = await asyncio.shield(
                self._run_agent_internal(
                    a, triggered_by=triggered_by, trigger_context=trigger_context or {},
                    user_message=user_message,
                )
            )
        finally:
            self._running_agents.discard(agent_id)
        return run

    async def _run_agent_internal(
        self,
        a: Agent,
        *,
        triggered_by: str,
        trigger_context: dict[str, Any],
        user_message: str | None,
    ) -> Run:
        """Inner loop — invoked under _running_agents guard.

        1. Build system prompt from persona + system_prompt + procedural_rules
           + trigger-specific block + memory + commitments + inbox.
        2. Resolve allowed tools (force-include core + tools_allowed gate).
        3. Synthesize user message from trigger context if not provided.
        4. Call self._ai.chat(ai_call="agent.run", ...) and persist Run row.
        """
        run = Run(
            id=f"run_{uuid.uuid4().hex[:12]}",
            agent_id=a.id,
            triggered_by=triggered_by,
            trigger_context=dict(trigger_context),
            started_at=_now(),
            status=RunStatus.RUNNING,
            conversation_id=a.conversation_id,
            delegation_id="",
            ended_at=None,
            final_message_text=None,
            rounds_used=0,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            error=None,
            awaiting_user_input=False,
            pending_question=None,
            pending_actions=[],
        )
        await self._storage.put(_AGENT_RUNS_COLLECTION, run.id, _run_to_dict(run))

        try:
            system_prompt = await self._build_system_prompt(a, triggered_by, trigger_context)
            user_msg = user_message or self._synthesize_trigger_message(triggered_by, trigger_context)
            from gilbert.interfaces.auth import UserContext
            user_ctx = UserContext.from_user_id(a.owner_user_id) if hasattr(UserContext, "from_user_id") else None

            result = await self._ai.chat(
                user_message=user_msg,
                conversation_id=a.conversation_id or None,
                user_ctx=user_ctx,
                system_prompt=system_prompt,
                ai_call=_AI_CALL_NAME,
                ai_profile=a.profile_id,
            )

            # AIService.chat returns a ChatTurnResult-like; adapt to our fields.
            run.final_message_text = result.final_message_text
            run.conversation_id = result.conversation_id
            run.rounds_used = int(result.turn_usage.get("rounds", 0))
            run.tokens_in = int(result.turn_usage.get("tokens_in", 0))
            run.tokens_out = int(result.turn_usage.get("tokens_out", 0))
            run.cost_usd = float(result.turn_usage.get("cost_usd", 0.0))
            run.status = RunStatus.COMPLETED
            run.ended_at = _now()

            # Capture conv_id back on the agent if it just got created.
            if a.conversation_id == "" and run.conversation_id:
                fresh = await self._storage.get(_AGENTS_COLLECTION, a.id)
                if fresh is not None:
                    fresh["conversation_id"] = run.conversation_id
                    await self._storage.put(_AGENTS_COLLECTION, a.id, fresh)

            # Cost accounting + cap check.
            await self._accumulate_cost(a.id, run.cost_usd)

        except Exception as exc:
            logger.exception("agent run failed: %s", a.id)
            run.status = RunStatus.FAILED
            run.error = repr(exc)
            run.ended_at = _now()

        await self._storage.put(_AGENT_RUNS_COLLECTION, run.id, _run_to_dict(run))
        return run

    def _synthesize_trigger_message(self, triggered_by: str, ctx: dict[str, Any]) -> str:
        if triggered_by == "manual":
            return "Run manually triggered. Take whatever action is appropriate."
        if triggered_by == "heartbeat":
            return "Heartbeat — periodic self-check."
        if triggered_by == "time":
            return "Scheduled trigger fired."
        if triggered_by == "event":
            etype = ctx.get("event_type", "?")
            return f"Event '{etype}' fired. See trigger context for the payload."
        return f"Trigger: {triggered_by}."

    async def _build_system_prompt(
        self, a: Agent, triggered_by: str, trigger_context: dict[str, Any],
    ) -> str:
        parts = [a.persona, a.system_prompt, a.procedural_rules]

        # Trigger-specific block
        if triggered_by == "heartbeat":
            due = await self._due_commitments(a.id)
            checklist = a.heartbeat_checklist or "(no checklist configured)"
            due_block = "\n".join(f"- [{c.id}] {c.content} (due {c.due_at.isoformat()})" for c in due) or "(none)"
            parts.append(
                f"HEARTBEAT — periodic self-check. Read your checklist below "
                f"and decide if anything needs action right now. If nothing is "
                f"pressing, end your turn briefly.\n\n"
                f"CHECKLIST:\n{checklist}\n\n"
                f"DUE COMMITMENTS:\n{due_block}"
            )

        # Long-term memory block (top-K by recency for now).
        long_term = await self.search_memory(
            agent_id=a.id, query="", limit=20, state=MemoryState.LONG_TERM,
        )
        if long_term:
            mem_block = "\n".join(f"- {m.content}" for m in long_term)
            parts.append(f"LONG-TERM MEMORY:\n{mem_block}")

        return "\n\n---\n\n".join(p for p in parts if p)

    async def _due_commitments(self, agent_id: str) -> list[Commitment]:
        if self._storage is None:
            return []
        rows = await self._storage.query(_AGENT_COMMITMENTS_COLLECTION, {"agent_id": agent_id})
        out = []
        for r in rows:
            if r.get("completed_at"):
                continue
            due = datetime.fromisoformat(r["due_at"])
            if due <= _now():
                out.append(_commitment_from_dict(r))
        return out

    async def _accumulate_cost(self, agent_id: str, delta: float) -> None:
        if delta <= 0 or self._storage is None:
            return
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return
        new_total = float(row.get("lifetime_cost_usd", 0.0)) + delta
        row["lifetime_cost_usd"] = new_total
        cap = row.get("cost_cap_usd")
        if cap is not None and new_total >= float(cap):
            row["status"] = AgentStatus.DISABLED.value
            logger.warning("Agent %s auto-DISABLED at cost cap %s (cumulative %s)", agent_id, cap, new_total)
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)

    async def list_runs(self, *, agent_id: str, limit: int = 50) -> list[Run]:
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(_AGENT_RUNS_COLLECTION, {"agent_id": agent_id})
        rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return [_run_from_dict(r) for r in rows[:limit]]
```

Add the run helpers:

```python
def _run_to_dict(r: Run) -> dict[str, Any]:
    return {
        "_id": r.id,
        "agent_id": r.agent_id,
        "triggered_by": r.triggered_by,
        "trigger_context": r.trigger_context,
        "started_at": r.started_at.isoformat(),
        "status": r.status.value,
        "conversation_id": r.conversation_id,
        "delegation_id": r.delegation_id,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "final_message_text": r.final_message_text,
        "rounds_used": r.rounds_used,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "cost_usd": r.cost_usd,
        "error": r.error,
        "awaiting_user_input": r.awaiting_user_input,
        "pending_question": r.pending_question,
        "pending_actions": list(r.pending_actions),
    }


def _run_from_dict(row: dict[str, Any]) -> Run:
    return Run(
        id=row["_id"],
        agent_id=row["agent_id"],
        triggered_by=row.get("triggered_by", "manual"),
        trigger_context=row.get("trigger_context", {}),
        started_at=datetime.fromisoformat(row["started_at"]),
        status=RunStatus(row.get("status", "running")),
        conversation_id=row.get("conversation_id", ""),
        delegation_id=row.get("delegation_id", ""),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else None,
        final_message_text=row.get("final_message_text"),
        rounds_used=int(row.get("rounds_used", 0)),
        tokens_in=int(row.get("tokens_in", 0)),
        tokens_out=int(row.get("tokens_out", 0)),
        cost_usd=float(row.get("cost_usd", 0.0)),
        error=row.get("error"),
        awaiting_user_input=bool(row.get("awaiting_user_input", False)),
        pending_question=row.get("pending_question"),
        pending_actions=list(row.get("pending_actions", [])),
    )


def _commitment_from_dict(row: dict[str, Any]) -> Commitment:
    return Commitment(
        id=row["_id"],
        agent_id=row["agent_id"],
        content=row.get("content", ""),
        due_at=datetime.fromisoformat(row["due_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
        completion_note=row.get("completion_note", ""),
    )
```

- [ ] **Step 3: Run test to verify pass**

Run: `uv run pytest tests/unit/test_agent_service.py::test_run_agent_now_creates_run_row -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_service.py tests/unit/conftest.py
git commit -m "agents: run lifecycle — run_agent_now + _run_agent_internal

Phase 1A Task 8. Spawns a Run row, calls AIService.chat with
ai_call='agent.run', captures final message + cost into the Run.
System prompt assembly layers persona + system_prompt +
procedural_rules + trigger-specific block + LONG_TERM memory.
Cost accounting flips agent to DISABLED on cap exceed.

Co-Authored-By: <agent>"
```

---

### Task 9: Commitments — storage + tools

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Create: `tests/unit/test_commitments.py`

- [ ] **Step 1: Write failing tests**

```python
"""Commitment — create/complete/list and heartbeat surfacing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def test_create_commitment_persists(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    c = await svc.create_commitment(
        agent_id=a.id,
        content="check inbox",
        due_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    assert c.id
    assert c.content == "check inbox"
    assert c.completed_at is None


async def test_complete_commitment_marks_done(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    c = await svc.create_commitment(agent_id=a.id, content="foo", due_at=datetime.now(UTC))
    completed = await svc.complete_commitment(c.id, note="handled")
    assert completed.completed_at is not None
    assert completed.completion_note == "handled"


async def test_due_commitments_filters_by_time_and_unfinished(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    past = await svc.create_commitment(agent_id=a.id, content="past", due_at=datetime.now(UTC) - timedelta(seconds=1))
    future = await svc.create_commitment(agent_id=a.id, content="future", due_at=datetime.now(UTC) + timedelta(hours=1))
    await svc.complete_commitment(past.id, note="done")  # mark past as done

    due = await svc._due_commitments(a.id)
    # past is completed → excluded; future is not yet due → excluded
    assert due == []

    # Add a new past one that's still pending
    pending = await svc.create_commitment(agent_id=a.id, content="pending", due_at=datetime.now(UTC) - timedelta(seconds=5))
    due = await svc._due_commitments(a.id)
    assert len(due) == 1
    assert due[0].id == pending.id
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_commitments.py -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement commitment methods**

In `src/gilbert/core/services/agent.py`:

```python
def _commitment_to_dict(c: Commitment) -> dict[str, Any]:
    return {
        "_id": c.id,
        "agent_id": c.agent_id,
        "content": c.content,
        "due_at": c.due_at.isoformat(),
        "created_at": c.created_at.isoformat(),
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "completion_note": c.completion_note,
    }


# ── On AgentService ──────────────────────────────────────────────────

    async def create_commitment(
        self, *, agent_id: str, content: str, due_at: datetime,
    ) -> Commitment:
        if self._storage is None:
            raise RuntimeError("not started")
        c = Commitment(
            id=f"com_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            content=content,
            due_at=due_at,
            created_at=_now(),
            completed_at=None,
            completion_note="",
        )
        await self._storage.put(_AGENT_COMMITMENTS_COLLECTION, c.id, _commitment_to_dict(c))
        return c

    async def complete_commitment(
        self, commitment_id: str, *, note: str = "",
    ) -> Commitment:
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_COMMITMENTS_COLLECTION, commitment_id)
        if row is None:
            raise KeyError(commitment_id)
        row["completed_at"] = _now().isoformat()
        row["completion_note"] = note
        await self._storage.put(_AGENT_COMMITMENTS_COLLECTION, commitment_id, row)
        return _commitment_from_dict(row)

    async def list_commitments(
        self, *, agent_id: str, include_completed: bool = False,
    ) -> list[Commitment]:
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(_AGENT_COMMITMENTS_COLLECTION, {"agent_id": agent_id})
        out = []
        for r in rows:
            if not include_completed and r.get("completed_at"):
                continue
            out.append(_commitment_from_dict(r))
        out.sort(key=lambda c: c.due_at)
        return out
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_commitments.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_commitments.py
git commit -m "agents: Commitment storage + create/complete/list

Phase 1A Task 9. _due_commitments helper feeds the heartbeat prompt;
list_commitments is the user-facing list (default unfinished only).

Co-Authored-By: <agent>"
```

---

### Task 10: Heartbeat trigger registration + scheduler integration

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Create: `tests/unit/test_heartbeat.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_heartbeat.py`:

```python
"""Heartbeat trigger — registration and run flow."""

from __future__ import annotations

import pytest


async def test_heartbeat_trigger_registered_on_agent_create(
    started_agent_service, scheduler_mock,
):
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="x",
        heartbeat_enabled=True,
        heartbeat_interval_s=600,
    )
    # Scheduler should have been asked to add a job named heartbeat_<id>.
    assert any(name == f"heartbeat_{a.id}" for name in scheduler_mock.added_jobs)


async def test_heartbeat_disabled_skips_registration(
    started_agent_service, scheduler_mock,
):
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x", heartbeat_enabled=False,
    )
    assert all(name != f"heartbeat_{a.id}" for name in scheduler_mock.added_jobs)


async def test_heartbeat_unregistered_on_delete(
    started_agent_service, scheduler_mock,
):
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x", heartbeat_enabled=True,
    )
    await svc.delete_agent(a.id)
    assert f"heartbeat_{a.id}" in scheduler_mock.removed_jobs


async def test_heartbeat_triggered_run_uses_checklist_in_prompt(
    started_agent_service, ai_provider_mock,
):
    """When the heartbeat job fires, a run is spawned with
    triggered_by='heartbeat' and the system prompt contains the
    checklist text."""
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x", heartbeat_enabled=True,
        heartbeat_checklist="check the news",
    )
    # Manually invoke the heartbeat handler the way scheduler would.
    await svc._on_heartbeat_fired(a.id)

    # AI mock should have been called with system_prompt containing the checklist.
    last_call = ai_provider_mock.last_call_kwargs
    assert "check the news" in last_call["system_prompt"]
```

You'll need a `scheduler_mock` fixture that records `add_job` and `remove_job` calls — adapt or create:

```python
# tests/unit/conftest.py
@pytest.fixture
def scheduler_mock():
    class _Sched:
        def __init__(self):
            self.added_jobs = []
            self.removed_jobs = []

        async def add_job(self, name, callback, schedule):
            self.added_jobs.append(name)

        async def remove_job(self, name):
            self.removed_jobs.append(name)

    return _Sched()
```

Wire it into `started_agent_service` so it's the scheduler the service binds to.

Also extend `ai_provider_mock` to record kwargs:

```python
class _AI:
    def __init__(self):
        self.last_call_kwargs = {}

    async def chat(self, **kwargs):
        self.last_call_kwargs = kwargs
        # ...same return as before
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_heartbeat.py -v`
Expected: FAIL — heartbeat methods don't exist.

- [ ] **Step 3: Implement heartbeat trigger registration**

In `src/gilbert/core/services/agent.py`:

```python
from gilbert.interfaces.scheduler import Schedule

# ── On AgentService ──────────────────────────────────────────────────

    async def _arm_heartbeat(self, a: Agent) -> None:
        """Register a heartbeat scheduler job for this agent.

        Idempotent: removing first then adding handles re-arms after
        config edits."""
        if self._scheduler is None or not a.heartbeat_enabled:
            return
        job_name = f"heartbeat_{a.id}"

        async def _cb() -> None:
            await self._on_heartbeat_fired(a.id)

        try:
            await self._scheduler.remove_job(job_name)
        except Exception:
            pass
        await self._scheduler.add_job(
            name=job_name,
            callback=_cb,
            schedule=Schedule(kind="interval", seconds=a.heartbeat_interval_s),
        )

    async def _disarm_heartbeat(self, agent_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            await self._scheduler.remove_job(f"heartbeat_{agent_id}")
        except Exception:
            pass

    async def _on_heartbeat_fired(self, agent_id: str) -> None:
        """Scheduler job callback — fire a heartbeat run if the agent
        is still ENABLED and not already running."""
        a = await self.get_agent(agent_id)
        if a is None or a.status is not AgentStatus.ENABLED:
            await self._disarm_heartbeat(agent_id)
            return
        if agent_id in self._running_agents:
            # In-flight run; skip silently. The heartbeat will fire again
            # next interval.
            return
        try:
            self._running_agents.add(agent_id)
            await self._run_agent_internal(
                a, triggered_by="heartbeat",
                trigger_context={}, user_message=None,
            )
        finally:
            self._running_agents.discard(agent_id)
```

Hook arm/disarm into the lifecycle:

- In `create_agent` (Task 5), after `await self._storage.put(...)`, add: `await self._arm_heartbeat(a)`.
- In `update_agent` (Task 5), after the put, if `heartbeat_enabled` or `heartbeat_interval_s` was changed, call `_arm_heartbeat` (or `_disarm_heartbeat` if `heartbeat_enabled=False`).
- In `delete_agent` (Task 5), before deletion: `await self._disarm_heartbeat(agent_id)`.

Also in `start()`, after binding the scheduler, add:

```python
        # Re-arm heartbeats for every enabled agent.
        rows = await self._storage.query(_AGENTS_COLLECTION, {})
        for r in rows:
            a = _agent_from_dict(r)
            if a.status is AgentStatus.ENABLED and a.heartbeat_enabled:
                await self._arm_heartbeat(a)
```

In `stop()`, mirror with disarm:

```python
        rows = await self._storage.query(_AGENTS_COLLECTION, {}) if self._storage else []
        for r in rows:
            await self._disarm_heartbeat(r["_id"])
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_heartbeat.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_heartbeat.py tests/unit/conftest.py
git commit -m "agents: heartbeat trigger registration + run flow

Phase 1A Task 10. Per-agent scheduler job named heartbeat_<id>;
fired callbacks invoke _run_agent_internal with
triggered_by='heartbeat'. Re-armed on service start; disarmed on
delete and on stop. heartbeat_checklist is included in the run's
system prompt via _build_system_prompt (Task 8).

Co-Authored-By: <agent>"
```

---

### Task 11: InboxSignal + _signal_agent dispatch + drain

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Create: `tests/unit/test_agent_inbox.py`

- [ ] **Step 1: Write failing tests**

```python
"""InboxSignal + _signal_agent dispatch.

Phase 1A scope: signal lifecycle (create / drain / process) and the
restart-recovery rehydration. No producers in Phase 1 except
'user_message_during_busy' — peer messaging producers are Phase 2."""

from __future__ import annotations

import pytest

from gilbert.interfaces.agent import InboxSignal


async def test_signal_persists_inbox_row(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    # Mark agent as busy to force enqueue path.
    svc._running_agents.add(a.id)
    sig = await svc._signal_agent(
        agent_id=a.id,
        signal_kind="inbox",
        body="hello",
        sender_kind="user", sender_id="usr_1", sender_name="brian",
        source_conv_id="conv_1", source_message_id="msg_1",
        metadata={},
    )
    assert sig.id
    assert sig.processed_at is None
    # Cache contains it.
    assert any(s.id == sig.id for s in svc._inboxes.get(a.id, []))


async def test_drain_marks_signals_processed(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    svc._running_agents.add(a.id)
    sig = await svc._signal_agent(
        agent_id=a.id, signal_kind="inbox", body="hi",
        sender_kind="user", sender_id="usr_1", sender_name="brian",
        source_conv_id="c", source_message_id="m", metadata={},
    )
    drained = await svc._drain_inbox(a.id)
    assert drained == [sig.id] or [s.id for s in drained] == [sig.id]
    # Cache empty.
    assert svc._inboxes.get(a.id, []) == []
    # Storage row marked processed.
    row = await svc._storage.get("agent_inbox_signals", sig.id)
    assert row["processed_at"] is not None


async def test_inbox_rehydrated_on_start(started_agent_service, sqlite_storage_provider):
    """Drop a pending InboxSignal row directly into storage, restart
    the service, and verify the in-memory cache picked it up."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    storage = sqlite_storage_provider.backend
    await storage.put("agent_inbox_signals", "sig_pre", {
        "_id": "sig_pre",
        "agent_id": a.id,
        "signal_kind": "inbox",
        "body": "hi",
        "sender_kind": "user",
        "sender_id": "usr_1",
        "sender_name": "brian",
        "source_conv_id": "c",
        "source_message_id": "m",
        "delegation_id": "",
        "metadata": {},
        "priority": "normal",
        "created_at": "2026-05-04T10:00:00+00:00",
        "processed_at": None,
    })
    # Stop and restart the service (use the same storage backend).
    await svc.stop()
    await svc.start(svc._resolver)

    assert any(s.id == "sig_pre" for s in svc._inboxes.get(a.id, []))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_agent_inbox.py -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement InboxSignal**

In `src/gilbert/core/services/agent.py`:

```python
def _signal_to_dict(s: InboxSignal) -> dict[str, Any]:
    return {
        "_id": s.id,
        "agent_id": s.agent_id,
        "signal_kind": s.signal_kind,
        "body": s.body,
        "sender_kind": s.sender_kind,
        "sender_id": s.sender_id,
        "sender_name": s.sender_name,
        "source_conv_id": s.source_conv_id,
        "source_message_id": s.source_message_id,
        "delegation_id": s.delegation_id,
        "metadata": s.metadata,
        "priority": s.priority,
        "created_at": s.created_at.isoformat(),
        "processed_at": s.processed_at.isoformat() if s.processed_at else None,
    }


def _signal_from_dict(row: dict[str, Any]) -> InboxSignal:
    return InboxSignal(
        id=row["_id"],
        agent_id=row["agent_id"],
        signal_kind=row.get("signal_kind", "inbox"),
        body=row.get("body", ""),
        sender_kind=row.get("sender_kind", "user"),
        sender_id=row.get("sender_id", ""),
        sender_name=row.get("sender_name", ""),
        source_conv_id=row.get("source_conv_id", ""),
        source_message_id=row.get("source_message_id", ""),
        delegation_id=row.get("delegation_id", ""),
        metadata=row.get("metadata", {}),
        priority=row.get("priority", "normal"),
        created_at=datetime.fromisoformat(row["created_at"]),
        processed_at=(
            datetime.fromisoformat(row["processed_at"])
            if row.get("processed_at") else None
        ),
    )


# ── On AgentService ──────────────────────────────────────────────────

    async def _signal_agent(
        self,
        *,
        agent_id: str,
        signal_kind: str,
        body: str,
        sender_kind: str,
        sender_id: str,
        sender_name: str,
        source_conv_id: str = "",
        source_message_id: str = "",
        delegation_id: str = "",
        metadata: dict[str, Any] | None = None,
        priority: str = "normal",
    ) -> InboxSignal:
        """Dispatch a wake-up signal.

        - Idle agent → spawn run with triggered_by=signal_kind.
        - Busy agent → persist + enqueue in cache; drained between rounds.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        sig = InboxSignal(
            id=f"sig_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            signal_kind=signal_kind,
            body=body,
            sender_kind=sender_kind,
            sender_id=sender_id,
            sender_name=sender_name,
            source_conv_id=source_conv_id,
            source_message_id=source_message_id,
            delegation_id=delegation_id,
            metadata=metadata or {},
            priority=priority,
            created_at=_now(),
            processed_at=None,
        )
        await self._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id, _signal_to_dict(sig))
        self._inboxes.setdefault(agent_id, []).append(sig)
        # If idle, fire a run.
        if agent_id not in self._running_agents:
            agent = await self.get_agent(agent_id)
            if agent and agent.status is AgentStatus.ENABLED:
                # Spawn async — don't block the signal sender.
                asyncio.create_task(
                    self._run_with_signal(agent_id, signal_kind, sig),
                    name=f"agent-run-{agent_id}",
                )
        return sig

    async def _run_with_signal(self, agent_id: str, signal_kind: str, sig: InboxSignal) -> None:
        """Wrap _run_agent_internal in the running-agent guard."""
        if agent_id in self._running_agents:
            return
        a = await self.get_agent(agent_id)
        if a is None:
            return
        self._running_agents.add(agent_id)
        try:
            await self._run_agent_internal(
                a,
                triggered_by=signal_kind,
                trigger_context={"signal_id": sig.id, "sender_id": sig.sender_id},
                user_message=None,
            )
        finally:
            self._running_agents.discard(agent_id)

    async def _drain_inbox(self, agent_id: str) -> list[InboxSignal]:
        """Mark every cached signal for this agent as processed and
        return them. Called between rounds via the chat callback."""
        sigs = self._inboxes.pop(agent_id, [])
        if self._storage is None:
            return sigs
        now = _now().isoformat()
        for s in sigs:
            row = await self._storage.get(_AGENT_INBOX_SIGNALS_COLLECTION, s.id)
            if row is not None:
                row["processed_at"] = now
                await self._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, s.id, row)
        return sigs

    async def _rehydrate_inboxes(self) -> None:
        if self._storage is None:
            return
        rows = await self._storage.query(
            _AGENT_INBOX_SIGNALS_COLLECTION, {"processed_at": None}
        )
        for r in rows:
            sig = _signal_from_dict(r)
            self._inboxes.setdefault(sig.agent_id, []).append(sig)
```

In `start()`, after the existing setup, add:

```python
        await self._rehydrate_inboxes()
```

Reset cache in `stop()`:

```python
        self._inboxes.clear()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_agent_inbox.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_inbox.py
git commit -m "agents: InboxSignal + _signal_agent dispatch

Phase 1A Task 11. Single dispatch point for wake-up signals: idle
agents spawn a run (asyncio.create_task with named task), busy
agents enqueue to in-memory cache + persist to agent_inbox_signals.
_drain_inbox marks signals processed between rounds; _rehydrate_inboxes
restores the cache on service start so signals survive a process
restart.

Co-Authored-By: <agent>"
```

---

### Task 12: Defaults via ConfigParam + agents.get_defaults

**Files:**
- Modify: `src/gilbert/core/services/agent.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_agent_service.py`:

```python
async def test_config_params_includes_defaults(started_agent_service):
    svc = started_agent_service
    params = svc.config_params()
    keys = {p.key for p in params}
    expected = {
        "default_persona", "default_system_prompt", "default_procedural_rules",
        "default_heartbeat_interval_s", "default_heartbeat_checklist",
        "default_dream_enabled", "default_dream_quiet_hours",
        "default_dream_probability", "default_dream_max_per_night",
        "default_profile_id", "default_avatar_kind", "default_avatar_value",
        "default_tools_allowed", "tool_groups",
    }
    assert expected.issubset(keys)


async def test_default_persona_is_ai_prompt_flagged(started_agent_service):
    svc = started_agent_service
    params = {p.key: p for p in svc.config_params()}
    assert params["default_persona"].ai_prompt is True
    assert params["default_persona"].multiline is True
    assert params["default_system_prompt"].ai_prompt is True
    assert params["default_procedural_rules"].ai_prompt is True
    assert params["default_heartbeat_checklist"].ai_prompt is True


async def test_on_config_changed_caches_defaults(started_agent_service):
    svc = started_agent_service
    await svc.on_config_changed({"default_persona": "I am helpful."})
    assert svc._defaults["default_persona"] == "I am helpful."


async def test_agents_get_defaults_rpc_returns_current(started_agent_service):
    svc = started_agent_service
    await svc.on_config_changed({"default_persona": "X"})
    h = svc.get_ws_handlers()
    res = await h["agents.get_defaults"](_FakeConn("usr_1"), {})
    assert res["defaults"]["default_persona"] == "X"
```

- [ ] **Step 2: Implement config_params + on_config_changed**

In `src/gilbert/core/services/agent.py`, near the top add module constants:

```python
_DEFAULT_PERSONA = "You are an autonomous AI agent."
_DEFAULT_SYSTEM_PROMPT = (
    "Take whatever action is appropriate to advance the goals you have "
    "been assigned. Use your tools deliberately. End your turn briefly "
    "when there is nothing pressing."
)
_DEFAULT_PROCEDURAL_RULES = (
    "When you ask a question or need user input, MUST call "
    "request_user_input first so the user gets a notification.\n\n"
    "When you make a follow-up commitment, call commitment_create.\n\n"
    "When you learn a durable fact about the user or their context, "
    "call agent_memory_save with kind='preference' or kind='fact'."
)
_DEFAULT_HEARTBEAT_CHECKLIST = (
    "1. Are there any due commitments to action?\n"
    "2. Anything inbound in your inbox you haven't seen?\n"
    "3. Any goals assigned to you that are blocked?\n"
    "4. If nothing pressing, end your turn briefly."
)
_DEFAULT_TOOL_GROUPS = {
    "files": ["read_workspace_file", "write_skill_workspace_file", "run_workspace_script"],
    "knowledge": ["search_knowledge"],
    "communication": ["notify_user"],
    "self": ["agent_memory_save", "agent_memory_search", "commitment_create", "commitment_complete"],
}


# In imports:
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType


# ── On AgentService ──────────────────────────────────────────────────

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="default_persona",
                type=ToolParameterType.STRING,
                description="Default persona ('soul') for new agents.",
                default=_DEFAULT_PERSONA,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_system_prompt",
                type=ToolParameterType.STRING,
                description="Default role-specific system prompt for new agents.",
                default=_DEFAULT_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_procedural_rules",
                type=ToolParameterType.STRING,
                description="Default workflow rulebook for new agents.",
                default=_DEFAULT_PROCEDURAL_RULES,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_heartbeat_checklist",
                type=ToolParameterType.STRING,
                description="Default heartbeat self-check checklist for new agents.",
                default=_DEFAULT_HEARTBEAT_CHECKLIST,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_heartbeat_interval_s",
                type=ToolParameterType.NUMBER,
                description="Default heartbeat interval (seconds).",
                default=1800,
            ),
            ConfigParam(
                key="default_dream_enabled",
                type=ToolParameterType.BOOLEAN,
                description="Default dreaming opt-in.",
                default=False,
            ),
            ConfigParam(
                key="default_dream_quiet_hours",
                type=ToolParameterType.STRING,
                description="Default quiet-hours window (HH:MM-HH:MM, owner's local TZ).",
                default="22:00-06:00",
            ),
            ConfigParam(
                key="default_dream_probability",
                type=ToolParameterType.NUMBER,
                description="Default per-quiet-heartbeat probability (0..1).",
                default=0.1,
            ),
            ConfigParam(
                key="default_dream_max_per_night",
                type=ToolParameterType.NUMBER,
                description="Default nightly dream cap.",
                default=3,
            ),
            ConfigParam(
                key="default_profile_id",
                type=ToolParameterType.STRING,
                description="Default AI profile id for new agents.",
                default="standard",
            ),
            ConfigParam(
                key="default_avatar_kind",
                type=ToolParameterType.STRING,
                description="Default avatar kind ('emoji' | 'icon' | 'image').",
                default="emoji",
            ),
            ConfigParam(
                key="default_avatar_value",
                type=ToolParameterType.STRING,
                description="Default avatar value (emoji char, icon name, or workspace_file:<id>).",
                default="🤖",
            ),
            ConfigParam(
                key="default_tools_allowed",
                type=ToolParameterType.STRING,
                description=(
                    "Default tool allowlist. Empty = all tools (None); "
                    "comma-separated names = strict allowlist on top of core."
                ),
                default="",
            ),
            ConfigParam(
                key="tool_groups",
                type=ToolParameterType.OBJECT,
                description=(
                    "Curated tool groups for the create-agent UI's "
                    "ToolPicker. JSON object: {group_name: [tool_name, ...]}."
                ),
                default=_DEFAULT_TOOL_GROUPS,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Snapshot the config into self._defaults; create_agent reads from here.
        self._defaults = dict(config)
        # Normalize default_tools_allowed: empty string → None.
        raw_allowed = config.get("default_tools_allowed", "")
        if isinstance(raw_allowed, str):
            stripped = raw_allowed.strip()
            self._defaults["default_tools_allowed"] = (
                None if not stripped else [t.strip() for t in stripped.split(",")]
            )
```

- [ ] **Step 3: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_agent_service.py -v -k "config_params or on_config_changed or get_defaults"`
Expected: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_service.py
git commit -m "agents: ConfigParam defaults + on_config_changed

Phase 1A Task 12. Persona / system_prompt / procedural_rules /
heartbeat_checklist all flagged ai_prompt=True so they show in the
prompt-author UI. tool_groups is operator-editable JSON for UI
grouping. on_config_changed normalizes default_tools_allowed (empty
string → None for 'all tools').

Co-Authored-By: <agent>"
```

---

### Task 13: Per-agent tool gating (force-include core + allowlist)

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Create: `tests/unit/test_tool_gating.py`

- [ ] **Step 1: Write failing tests**

```python
"""Per-agent tool gating — force-include core + tools_allowed allowlist."""

from __future__ import annotations

import pytest


_CORE = {
    "complete_run", "request_user_input", "notify_user",
    "commitment_create", "commitment_complete", "commitment_list",
    "agent_memory_save", "agent_memory_search",
    "agent_memory_review_and_promote",
}


async def test_tools_allowed_none_means_all(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x", tools_allowed=None)
    keep = svc._compute_allowed_tool_names(a, available={"complete_run", "search_knowledge", "lights.set"})
    assert keep == {"complete_run", "search_knowledge", "lights.set"}


async def test_tools_allowed_empty_list_keeps_only_core(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x", tools_allowed=[])
    keep = svc._compute_allowed_tool_names(a, available={"complete_run", "search_knowledge", "lights.set"})
    assert keep == {"complete_run"}  # only core that's available


async def test_tools_allowed_extra_unioned_with_core(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x", tools_allowed=["search_knowledge"])
    available = {"complete_run", "search_knowledge", "lights.set"}
    keep = svc._compute_allowed_tool_names(a, available=available)
    assert keep == {"complete_run", "search_knowledge"}


async def test_core_tools_constant_matches_spec():
    """The core force-include set is documented in the spec; if it
    drifts, tests catch it."""
    from gilbert.core.services.agent import _CORE_AGENT_TOOLS
    assert _CORE == _CORE_AGENT_TOOLS
```

- [ ] **Step 2: Implement gating**

In `src/gilbert/core/services/agent.py`:

```python
_CORE_AGENT_TOOLS: frozenset[str] = frozenset({
    # Phase 1A
    "complete_run",
    "request_user_input",
    "notify_user",
    "commitment_create",
    "commitment_complete",
    "commitment_list",
    "agent_memory_save",
    "agent_memory_search",
    "agent_memory_review_and_promote",
    # Phase 2 will add agent_list, agent_send_message, agent_delegate.
    # Phase 4 will add goal_post.
})


# ── On AgentService ──────────────────────────────────────────────────

    def _compute_allowed_tool_names(self, a: Agent, *, available: set[str]) -> set[str]:
        """Compute the tool name set for an agent's run.

        - tools_allowed=None → all available tools (legacy behavior).
        - tools_allowed=[…] → core ∪ allowlist, intersected with available.

        Tools removed from the available set (e.g., plugin uninstalled)
        are silently dropped — they just won't appear in the run.
        """
        if a.tools_allowed is None:
            return set(available)
        keep = (set(_CORE_AGENT_TOOLS) | set(a.tools_allowed)) & set(available)
        return keep
```

- [ ] **Step 3: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_tool_gating.py -v`
Expected: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_tool_gating.py
git commit -m "agents: per-agent tool gating (_compute_allowed_tool_names)

Phase 1A Task 13. _CORE_AGENT_TOOLS is the force-include set;
tools_allowed=None keeps all available tools, [...] is a strict
allowlist unioned with core. Phase 2/4 will add to _CORE_AGENT_TOOLS
as new self-management tools land.

Co-Authored-By: <agent>"
```

---

### Task 14: Agent tools — complete_run + memory + commitment ToolDefinitions

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_service.py`

This task implements the `ToolProvider` half of the service. Tools are exposed via `get_tools(user_ctx)` and called via `execute_tool(name, arguments)`. Tool argument injection: every tool execution receives `_user_id`, `_conversation_id`, and `_agent_id` injected by the runtime; tools never trust caller identity from arguments.

- [ ] **Step 1: Write failing tests**

```python
async def test_get_tools_returns_core_set(started_agent_service):
    svc = started_agent_service
    tools = svc.get_tools(user_ctx=None)
    names = {t.name for t in tools}
    assert "complete_run" in names
    assert "commitment_create" in names
    assert "commitment_complete" in names
    assert "commitment_list" in names
    assert "agent_memory_save" in names
    assert "agent_memory_search" in names
    assert "agent_memory_review_and_promote" in names


async def test_execute_complete_run_marks_run(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    # Synthesize an active run row to flag.
    run = await svc.run_agent_now(a.id, user_message="hi")  # creates a Run

    out = await svc.execute_tool("complete_run", {
        "_agent_id": a.id, "_user_id": "usr_1",
        "_conversation_id": run.conversation_id,
        "reason": "did the thing",
    })
    assert "marked" in out.lower()


async def test_execute_commitment_create(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    out = await svc.execute_tool("commitment_create", {
        "_agent_id": a.id, "_user_id": "usr_1",
        "content": "check sonarr", "due_in_seconds": 1800,
    })
    assert "scheduled" in out.lower() or "created" in out.lower()


async def test_execute_agent_memory_save(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    out = await svc.execute_tool("agent_memory_save", {
        "_agent_id": a.id, "_user_id": "usr_1",
        "content": "user prefers dark mode", "kind": "preference",
    })
    assert "saved" in out.lower()
    mems = await svc.search_memory(agent_id=a.id, query="dark")
    assert any("dark mode" in m.content for m in mems)
```

- [ ] **Step 2: Implement ToolProvider methods**

In `src/gilbert/core/services/agent.py`:

```python
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)


# Tool definitions — top-level so tests can inspect.

_TOOL_COMPLETE_RUN = ToolDefinition(
    name="complete_run",
    description=(
        "Flag the current agent run as having met its success criteria. "
        "Use this when you've completed the work you were triggered for "
        "and have nothing else to do this turn. Reason is logged onto the "
        "Run entity."
    ),
    parameters=[
        ToolParameter(
            name="reason",
            type=ToolParameterType.STRING,
            description="One-line success reason logged onto the Run.",
            required=True,
        ),
    ],
    slash_command="complete_run",
    slash_help="Mark the current run as successfully complete.",
)

_TOOL_COMMITMENT_CREATE = ToolDefinition(
    name="commitment_create",
    description=(
        "Create a follow-up commitment for yourself. Surfaces in the "
        "next heartbeat whose schedule is at-or-after due_at."
    ),
    parameters=[
        ToolParameter(name="content", type=ToolParameterType.STRING, description="What to follow up on", required=True),
        ToolParameter(name="due_in_seconds", type=ToolParameterType.NUMBER, description="Surface at-or-after this many seconds from now.", required=False),
        ToolParameter(name="due_at", type=ToolParameterType.STRING, description="ISO-8601 absolute time alternative to due_in_seconds.", required=False),
    ],
)

_TOOL_COMMITMENT_COMPLETE = ToolDefinition(
    name="commitment_complete",
    description="Mark a previously-created commitment as complete.",
    parameters=[
        ToolParameter(name="commitment_id", type=ToolParameterType.STRING, description="The commitment id.", required=True),
        ToolParameter(name="note", type=ToolParameterType.STRING, description="Optional completion note.", required=False),
    ],
)

_TOOL_COMMITMENT_LIST = ToolDefinition(
    name="commitment_list",
    description="List your commitments. By default only unfinished ones.",
    parameters=[
        ToolParameter(name="include_completed", type=ToolParameterType.BOOLEAN, description="Include already-completed commitments.", required=False),
    ],
)

_TOOL_AGENT_MEMORY_SAVE = ToolDefinition(
    name="agent_memory_save",
    description=(
        "Save a learned fact to your own memory. SHORT_TERM by default; "
        "use kind='preference' or kind='decision' or kind='fact' as "
        "appropriate. Tags are free-form."
    ),
    parameters=[
        ToolParameter(name="content", type=ToolParameterType.STRING, description="The memory text.", required=True),
        ToolParameter(name="kind", type=ToolParameterType.STRING, description="'fact' | 'preference' | 'decision' | 'daily' | 'dream'.", required=False),
        ToolParameter(name="tags", type=ToolParameterType.ARRAY, description="Free-form tags.", required=False),
    ],
)

_TOOL_AGENT_MEMORY_SEARCH = ToolDefinition(
    name="agent_memory_search",
    description="Search your own memories by substring match. Recency-ordered.",
    parameters=[
        ToolParameter(name="query", type=ToolParameterType.STRING, description="Substring to match. Empty = recent.", required=False),
        ToolParameter(name="limit", type=ToolParameterType.NUMBER, description="Max results (default 20).", required=False),
    ],
)

_TOOL_AGENT_MEMORY_PROMOTE = ToolDefinition(
    name="agent_memory_review_and_promote",
    description=(
        "Review recent SHORT_TERM memories and promote durable ones to "
        "LONG_TERM with a score. Pass an array of {memory_id, score, "
        "decision} triplets (decision='promote'|'demote'|'keep')."
    ),
    parameters=[
        ToolParameter(name="reviews", type=ToolParameterType.ARRAY, description="List of {memory_id, score, decision}.", required=True),
    ],
)


# ── On AgentService ──────────────────────────────────────────────────

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        return [
            _TOOL_COMPLETE_RUN,
            _TOOL_COMMITMENT_CREATE,
            _TOOL_COMMITMENT_COMPLETE,
            _TOOL_COMMITMENT_LIST,
            _TOOL_AGENT_MEMORY_SAVE,
            _TOOL_AGENT_MEMORY_SEARCH,
            _TOOL_AGENT_MEMORY_PROMOTE,
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "complete_run":
            return await self._exec_complete_run(arguments)
        if name == "commitment_create":
            return await self._exec_commitment_create(arguments)
        if name == "commitment_complete":
            return await self._exec_commitment_complete(arguments)
        if name == "commitment_list":
            return await self._exec_commitment_list(arguments)
        if name == "agent_memory_save":
            return await self._exec_memory_save(arguments)
        if name == "agent_memory_search":
            return await self._exec_memory_search(arguments)
        if name == "agent_memory_review_and_promote":
            return await self._exec_memory_promote(arguments)
        raise KeyError(f"unknown tool: {name}")

    async def _exec_complete_run(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        reason = str(args.get("reason", "")).strip() or "(no reason given)"
        if not agent_id:
            return "error: complete_run requires _agent_id (injected by runtime)"
        # Find the active run for this agent — the most recent RUNNING.
        rows = await self._storage.query(_AGENT_RUNS_COLLECTION, {"agent_id": agent_id, "status": "running"})
        if not rows:
            return f"no active run for agent {agent_id}"
        row = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)[0]
        row["status"] = RunStatus.COMPLETED.value
        row["ended_at"] = _now().isoformat()
        row["final_message_text"] = reason
        await self._storage.put(_AGENT_RUNS_COLLECTION, row["_id"], row)
        return f"run {row['_id']} marked complete: {reason}"

    async def _exec_commitment_create(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        content = str(args.get("content", "")).strip()
        if not agent_id or not content:
            return "error: commitment_create requires _agent_id and content"
        if "due_at" in args and args["due_at"]:
            due_at = datetime.fromisoformat(str(args["due_at"]))
        else:
            seconds = float(args.get("due_in_seconds", 1800))
            from datetime import timedelta
            due_at = _now() + timedelta(seconds=seconds)
        c = await self.create_commitment(agent_id=agent_id, content=content, due_at=due_at)
        return f"commitment {c.id} created, due {c.due_at.isoformat()}"

    async def _exec_commitment_complete(self, args: dict[str, Any]) -> str:
        cid = str(args.get("commitment_id", ""))
        if not cid:
            return "error: commitment_complete requires commitment_id"
        c = await self.complete_commitment(cid, note=str(args.get("note", "")))
        return f"commitment {c.id} completed"

    async def _exec_commitment_list(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: commitment_list requires _agent_id"
        include = bool(args.get("include_completed", False))
        cs = await self.list_commitments(agent_id=agent_id, include_completed=include)
        if not cs:
            return "(no commitments)"
        lines = [
            f"- [{c.id}] {c.content} — due {c.due_at.isoformat()}"
            + (f" (completed: {c.completion_note})" if c.completed_at else "")
            for c in cs
        ]
        return "\n".join(lines)

    async def _exec_memory_save(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        content = str(args.get("content", "")).strip()
        if not agent_id or not content:
            return "error: agent_memory_save requires _agent_id and content"
        kind = str(args.get("kind", "fact"))
        tags_raw = args.get("tags") or []
        tags = frozenset(str(t) for t in tags_raw if str(t).strip())
        m = await self.save_memory(agent_id=agent_id, content=content, kind=kind, tags=tags)
        return f"memory {m.id} saved (state={m.state.value}, kind={m.kind})"

    async def _exec_memory_search(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: agent_memory_search requires _agent_id"
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 20))
        out = await self.search_memory(agent_id=agent_id, query=query, limit=limit)
        if not out:
            return "(no matches)"
        return "\n".join(f"- [{m.id}] ({m.state.value}, {m.kind}) {m.content}" for m in out)

    async def _exec_memory_promote(self, args: dict[str, Any]) -> str:
        reviews = args.get("reviews") or []
        if not isinstance(reviews, list):
            return "error: reviews must be an array"
        applied = 0
        for r in reviews:
            if not isinstance(r, dict):
                continue
            mid = str(r.get("memory_id", ""))
            decision = str(r.get("decision", ""))
            if not mid or decision not in {"promote", "demote", "keep"}:
                continue
            if decision == "promote":
                await self.promote_memory(memory_id=mid, score=float(r.get("score", 0.5)))
                applied += 1
            elif decision == "demote":
                await self.promote_memory(memory_id=mid, score=float(r.get("score", 0.0)), state=MemoryState.SHORT_TERM)
                applied += 1
            # 'keep' is a no-op
        return f"reviewed {len(reviews)} memories, applied {applied}"
```

- [ ] **Step 3: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_agent_service.py -v -k "tools or complete_run or commitment or memory"`
Expected: all relevant tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_service.py
git commit -m "agents: ToolProvider — complete_run + commitment + memory tools

Phase 1A Task 14. Tool argument injection: every tool reads _agent_id
from injected arguments (the runtime fills them; tools never trust
caller identity from regular arguments). Slash commands declared on
complete_run.

Co-Authored-By: <agent>"
```

---

### Task 15: Tool runtime injection in _run_agent_internal

**Files:**
- Modify: `src/gilbert/core/services/agent.py`

This task ensures `_agent_id` is injected into every tool call when the loop runs the agent's tools. The pattern: `AIService.chat()` accepts a `tool_arg_injector` callback that gets called per-tool-execution.

If `chat()` doesn't have such a hook, alternative: wrap the tools themselves before passing them in.

- [ ] **Step 1: Inspect AIService.chat signature**

Run: `grep -n "def chat\|tool_arg\|inject" src/gilbert/core/services/ai.py | head -20`

Look for whether `chat()` has an injection hook. If not, identify how tool execution is dispatched — most likely it's via the `tools` dict passed in, and we wrap each tool's handler before passing.

- [ ] **Step 2: Implement injection wrapper**

In `src/gilbert/core/services/agent.py`:

```python
    def _inject_agent_id(self, agent_id: str, tools_dict: dict[str, Any]) -> dict[str, Any]:
        """Wrap each tool handler so _agent_id is injected into args.

        The handler shape is (tool_def, callable) per agent_loop semantics.
        We replace the callable with a closure that mutates arguments.
        """
        wrapped: dict[str, Any] = {}
        for name, entry in tools_dict.items():
            tool_def, handler = entry

            async def _wrapped(args: dict[str, Any], _h=handler) -> Any:
                args = dict(args)
                args.setdefault("_agent_id", agent_id)
                return await _h(args)

            wrapped[name] = (tool_def, _wrapped)
        return wrapped
```

In `_run_agent_internal`, when building the tools dict to pass to `chat()`, run it through `_inject_agent_id(a.id, tools)`. The exact integration point depends on how `chat()` consumes tools — adapt as needed.

- [ ] **Step 3: Add a test that verifies the injection happens**

```python
async def test_tool_injection_adds_agent_id(started_agent_service):
    svc = started_agent_service
    captured = {}

    async def fake_handler(args):
        captured.update(args)
        return "ok"

    tools = {"foo": (object(), fake_handler)}
    wrapped = svc._inject_agent_id("ag_test", tools)
    await wrapped["foo"][1]({"x": 1})
    assert captured["_agent_id"] == "ag_test"
    assert captured["x"] == 1
```

Run: `uv run pytest tests/unit/test_agent_service.py::test_tool_injection_adds_agent_id -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/test_agent_service.py
git commit -m "agents: tool argument injection (_agent_id)

Phase 1A Task 15. Wrap every tool handler before passing to
AIService.chat so _agent_id is injected on every tool call. Tools
read identity from injected arguments only — never from
caller-supplied arg shapes.

Co-Authored-By: <agent>"
```

---

### Task 16: Memory file + index update

**Files:**
- Create: `.claude/memory/memory-agent-service.md`
- Modify: `.claude/memory/MEMORIES.md`

- [ ] **Step 1: Write the new memory file**

Create `.claude/memory/memory-agent-service.md`:

```markdown
# AgentService

## Summary
Replaces AutonomousAgentService with the multi-agent design from
docs/superpowers/specs/2026-05-04-agent-messaging-design.md. Agent is a
durable identity (persona + system_prompt + procedural_rules + heartbeat
+ memory + commitments + tool allowlist + avatar). Lives in
src/gilbert/core/services/agent.py.

## Details

**Capabilities declared:** ``agent`` (satisfies AgentProvider),
``ai_tools``, ``ws_handlers``.

**Requires:** ``entity_storage``, ``event_bus``, ``ai_chat``,
``scheduler``.

**AI call name:** ``agent.run`` (via ``ai_calls`` in ServiceInfo).
Operators can route to a distinct profile via the AI profile
assignment table.

**Entities:**
- ``agents`` collection — Agent rows.
- ``agent_memories`` — AgentMemory rows (SHORT_TERM / LONG_TERM split).
- ``agent_triggers`` — time / event / heartbeat triggers (Phase 1A only
  implements heartbeat; time/event will follow in a later task on this
  same phase or be deferred to Phase 2).
- ``agent_commitments`` — opt-in follow-up reminders.
- ``agent_inbox_signals`` — durable wake-up tracking. Message content
  lives in chat conversations; this row tracks signal lifecycle.
- ``agent_runs`` — Run rows, keyed by agent_id.

**Loop model:** ``run_agent_now`` is the synchronous entry; loops fire
under ``_running_agents`` guard, wrapped in ``asyncio.shield`` so a WS
disconnect doesn't cancel the run. ``_run_agent_internal`` synthesizes
a user message from trigger context, calls
``AIService.chat(ai_call="agent.run")``, captures the result.

**Heartbeat:** when ``Agent.heartbeat_enabled=True``, a SchedulerService
job named ``heartbeat_<agent_id>`` is registered at
``heartbeat_interval_s``. Firing the job invokes
``_on_heartbeat_fired`` which spawns a run with
``triggered_by="heartbeat"``.

**InboxSignal dispatch:** ``_signal_agent`` is the single dispatch
point. Idle agents get a fresh run spawned (asyncio.create_task);
busy agents get the signal enqueued to in-memory cache + persisted to
``agent_inbox_signals``. ``_drain_inbox`` between rounds marks signals
processed. ``_rehydrate_inboxes`` on service start restores the cache
so signals survive process restart.

**Per-agent tool gating:** ``_compute_allowed_tool_names`` returns the
final tool name set: if ``tools_allowed=None`` → all available; if a
list → core ∪ allowlist intersected with available. Core (force-include)
set: ``_CORE_AGENT_TOOLS`` constant.

**Tools (Phase 1A):** ``complete_run``, ``commitment_create``,
``commitment_complete``, ``commitment_list``, ``agent_memory_save``,
``agent_memory_search``, ``agent_memory_review_and_promote``. Future
phases add ``agent_send_message``, ``agent_delegate``, ``agent_list``
(Phase 2), ``goal_*`` (Phase 4), ``deliverable_*`` (Phase 5).

**WS RPCs (Phase 1A):** ``agents.create / get / list / update / delete /
set_status / run_now / get_defaults``. Per-user RBAC; admin sees-all.
``agents.tools.list_available`` and ``agents.tools.list_groups`` are
pending Phase 1B (they support the frontend ToolPicker).

**Defaults (ConfigParam):** ``default_persona``,
``default_system_prompt``, ``default_procedural_rules``,
``default_heartbeat_checklist`` are flagged ``ai_prompt=True`` for the
prompt-author UI. ``tool_groups`` is operator-editable JSON for the UI.

**RBAC:** all ``agents.*`` WS RPCs are user-level. Handlers enforce
per-user ownership via ``_load_agent_for_caller``.

**Multi-user isolation:** ``_running_agents`` and ``_inboxes`` are
keyed by agent_id (owner-scoped). ``asyncio.create_task`` for spawned
loops carries copy_context() (Phase 1A relies on this default).

## Related
- ``src/gilbert/interfaces/agent.py``
- ``src/gilbert/core/services/agent.py``
- ``tests/unit/test_agent_service.py``
- ``tests/unit/test_agent_memory.py``
- ``tests/unit/test_commitments.py``
- ``tests/unit/test_heartbeat.py``
- ``tests/unit/test_agent_inbox.py``
- ``tests/unit/test_tool_gating.py``
- ``docs/superpowers/specs/2026-05-04-agent-messaging-design.md``
- ``docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md``
- ``.claude/memory/memory-agent-loop.md`` (run_loop primitive)
```

- [ ] **Step 2: Update the memory index**

In `.claude/memory/MEMORIES.md`, add (preserving alphabetical or chronological order — match the file's convention):

```markdown
- [AgentService](memory-agent-service.md) — Replaces AutonomousAgentService; durable Agent identity with heartbeat, memory, commitments, tool gating
```

- [ ] **Step 3: Commit**

```bash
git add .claude/memory/memory-agent-service.md .claude/memory/MEMORIES.md
git commit -m "docs(memory): AgentService memory file + index update

Phase 1A Task 16. Captures the new service's shape, entities, loop
model, and RBAC story so future Claude sessions don't re-derive it.

Co-Authored-By: <agent>"
```

---

### Task 17: Final verification — full suite + architecture sweep

**Files:**
- (potentially): minor fixes anywhere the architecture sweep flags issues.

- [ ] **Step 1: Run the full unit test suite**

Run: `uv run pytest -q`
Expected: all pass. If something breaks because the old service was deleted but a downstream dependency wasn't found in Task 1, fix in this step.

- [ ] **Step 2: Run mypy**

Run: `uv run mypy src/`
Expected: clean (or matches the project's existing tolerated baseline). Fix any new errors introduced by Phase 1A.

- [ ] **Step 3: Run ruff**

Run: `uv run ruff check src/ tests/`
Expected: clean.

- [ ] **Step 4: Architecture sweep — layer imports**

Run: `grep -rEn "^from gilbert\\.(integrations|web)|^import gilbert\\.(integrations|web)" src/gilbert/core/services/agent.py`
Expected: no results.

Run: `grep -n "isinstance" src/gilbert/core/services/agent.py | grep -v "Protocol\|Provider\|Reader\|StorageBackend"`
Expected: nothing — all isinstance checks must be against ABCs/Protocols, not concrete classes.

- [ ] **Step 5: Architecture sweep — slash commands**

Run: `uv run pytest tests/unit/test_slash_command_uniqueness.py -v`
Expected: PASS. If a duplicate or invalid identifier slipped through, fix it.

- [ ] **Step 6: Architecture sweep — AI prompts configurable**

Run: `grep -n "system_prompt=" src/gilbert/core/services/agent.py | grep -v "self\\._\|ConfigParam\|default="`
Expected: no hardcoded literal prompts in `system_prompt=` call sites — only `self._foo_prompt` references or ConfigParam defaults.

- [ ] **Step 7: Run a quick end-to-end smoke test**

Start the local server (`./gilbert.sh start` or whatever the project uses) and check the logs for "AgentService started" with no exceptions. Frontend will be broken (Phase 1B), but the backend should be clean.

- [ ] **Step 8: Final commit if any tweaks were needed**

```bash
git add -u
git commit -m "agents: Phase 1A verification fixes

[List any specific tweaks made during verification]

Co-Authored-By: <agent>"
```

If no tweaks were needed, skip this step.

---

## Self-Review

After writing the plan, the validator should re-read the spec at `docs/superpowers/specs/2026-05-04-agent-messaging-design.md` and verify:

1. **Spec coverage (Phase 1A scope):**
   - ✅ Agent entity with all 22 fields → Task 2
   - ✅ AgentMemory + state/kind → Tasks 2, 7
   - ✅ AgentTrigger entity → Task 2 (heartbeat trigger registration in Task 10)
   - ✅ Commitment entity + tools → Tasks 2, 9, 14
   - ✅ InboxSignal + dispatch → Tasks 2, 11
   - ✅ Run entity + lifecycle → Tasks 2, 8
   - ✅ AgentProvider protocol → Task 2
   - ✅ AgentService skeleton + lifecycle → Task 3
   - ✅ Composition root registration → Task 4
   - ✅ Agent CRUD + RBAC → Task 5
   - ✅ WS RPCs → Task 6
   - ✅ Heartbeat behavior → Task 10
   - ✅ Defaults via ConfigParam → Task 12
   - ✅ Per-agent tool gating → Task 13
   - ✅ Phase 1 core tools → Task 14
   - ✅ Tool arg injection → Task 15
   - ✅ Memory file → Task 16
   - ✅ Test files: test_agent_service.py, test_agent_memory.py, test_commitments.py, test_heartbeat.py, test_agent_inbox.py, test_tool_gating.py

2. **Out of scope confirmed:**
   - Goals / war rooms / assignments → Phase 4
   - Peer messaging tools → Phase 2
   - Mid-stream interrupt → Phase 3
   - Dreaming gate → Phase 7
   - Cross-user → Phase 6
   - Frontend → Phase 1B

3. **Type consistency check:**
   - `Agent`, `AgentMemory`, `AgentTrigger`, `Commitment`, `InboxSignal`, `Run` defined in Task 2 — every later reference uses these names verbatim.
   - `AgentStatus`, `MemoryState`, `RunStatus` enums defined in Task 2 — `.value` used consistently for storage.
   - `_CORE_AGENT_TOOLS` defined in Task 13 — referenced in Task 14 tools.

4. **No placeholders:**
   - No "TBD" / "TODO" / "implement later".
   - Every step has runnable code or explicit instructions.
   - Every test has assertion content.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration. Use the `superpowers:subagent-driven-development` sub-skill.

2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch with checkpoints.

**Phase 1B (frontend) follows after Phase 1A ships.** The frontend at `/agents` will be broken until 1B lands — that's deliberate (clean backend, no bridging shims).

**Which approach?**
