# Autonomous Agent — Phase 3: NotificationService Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `NotificationService` (backend only) that persists notifications, publishes a `notification.received` bus event, exposes WebSocket RPCs for listing and managing notifications, and ensures notification events are delivered only to the target user via a content-level dispatch filter on `WsConnection`. Plus update the Phase 1 verification finding for section 1 to reflect the simpler architecture.

**Architecture:** A single new core service in `core/services/notifications.py` plus a small capability protocol in `interfaces/notifications.py`. Notification delivery to live WebSocket clients reuses the existing event-bus → `WsConnectionManager._dispatch_event` flow with a new `WsConnection.can_see_notification_event` filter (identical pattern to `can_see_chat_event` / `can_see_auth_event` / `can_see_workspace_event`). No separate `push_to_user` helper or per-user connection registry — that was overengineering uncovered during Phase 1 verification.

**Tech Stack:** Python 3.12+, `uv` for dependency/test management, pytest with mocks for unit tests + real SQLite for integration, `from __future__ import annotations` everywhere.

**Out of scope for this plan:**
- Frontend (bell icon, badge, dropdown, `/notifications` page, audible/visual signals) — Phase 3b plan, written after Phase 3a ships.
- `notify_user` as a global `ToolDefinition` (callable by any AI chat session) — Phase 4's `AutonomousAgentService` injects it as a per-run agent tool instead, with goal-context closure. A global `notify_user` tool is YAGNI for v1.
- The agent service itself — Phase 4 plan.

---

## File Structure

**Create:**
- `src/gilbert/interfaces/notifications.py` — `Notification` dataclass, `NotificationUrgency` enum, `NotificationProvider` capability protocol
- `src/gilbert/core/services/notifications.py` — `NotificationService` class
- `tests/unit/core/test_notification_service.py` — service tests (real SQLite via test fixture)
- `.claude/memory/memory-notification-service.md` — memory file

**Modify:**
- `src/gilbert/web/ws_protocol.py` — add `can_see_notification_event` filter to `WsConnection`; call it from `WsConnectionManager._dispatch_event`
- `src/gilbert/core/app.py` — register `NotificationService` instance during startup
- `.claude/memory/MEMORIES.md` — index the new memory
- `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` — section 1 update (push_to_user not needed; existing dispatch+filter is the pattern)

---

## Pre-flight

### Task 0: Correct verification finding for section 1

The original Phase 1 finding flagged `push_to_user` as "missing" and prescribed adding a per-user connection registry. Reading `WsConnectionManager._dispatch_event` revealed that the existing flow already routes events to all matching connections, with content-level filters per event family (`can_see_chat_event`, `can_see_auth_event`, `can_see_workspace_event`). Notifications fit that pattern; no parallel registry needed.

**Files:**
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 1)

- [ ] **Step 1: Replace section 1 of the verification doc**

Open `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md`. Find section 1 (the section starting with `## 1. push_to_user capability in web layer`). Replace its three lines (Status / Findings / Follow-up) with:

```markdown
**Status:** Not needed — existing dispatch handles it
**Findings:** `WsConnectionManager._dispatch_event` at `src/gilbert/web/ws_protocol.py:350` already routes every published bus event to every connection, applying per-event-type content filters (`can_see_chat_event`, `can_see_auth_event`, `can_see_workspace_event` on `WsConnection`). The original finding's prescription to add a `push_to_user` helper plus a `dict[user_id, set[WsConnection]]` registry was overengineering: notifications are bus events with `user_id` in `data`, and a single `can_see_notification_event` filter (matching the existing pattern) restricts delivery to the target user. No new registry, no new capability declaration, no separate push API needed.
**Follow-up:** Phase 3 NotificationService publishes a `notification.received` event with `user_id` in the data. Phase 3 also adds `WsConnection.can_see_notification_event` and wires it into `_dispatch_event`. That's the entire delivery path.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): correct push_to_user finding — existing dispatch + filter is the pattern"
```

---

## Phase 3a: Implementation (TDD)

### Task 1: Create `interfaces/notifications.py` with the data model

**Files:**
- Create: `src/gilbert/interfaces/notifications.py`

- [ ] **Step 1: Write the file**

Write `src/gilbert/interfaces/notifications.py` with **exactly** this content:

```python
"""Notifications interface — capability protocol and shared data types.

A notification is a small record addressed to a specific user, optionally
referencing a source object (e.g. an autonomous-agent goal/run). Services
publish notifications by calling ``NotificationProvider.notify_user``.
Delivery to live WebSocket clients is handled by the existing event-bus
dispatch with a per-user content filter — there is no separate push API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class NotificationUrgency(StrEnum):
    """How loudly a notification should signal its arrival.

    The frontend reads this to decide whether to play a sound, flash the
    title bar, or silently bump the badge. The backend just stamps it on
    the entity and the bus event.
    """

    INFO = "info"
    """Quiet — badge bump only."""

    NORMAL = "normal"
    """Default — badge bump with subtle visual update."""

    URGENT = "urgent"
    """Loud — sound, title-bar flash, animated badge."""


@dataclass
class Notification:
    """A single notification record."""

    id: str
    user_id: str
    """Recipient. Notifications are 1:1; fan-out is the caller's job."""

    source: str
    """Origin tag (e.g. ``"agent"``, ``"scheduler"``, ``"ingest"``).
    Free-form; consumers may switch on it for icon selection."""

    message: str
    urgency: NotificationUrgency
    created_at: datetime
    read: bool = False
    read_at: datetime | None = None
    source_ref: dict[str, Any] | None = None
    """Optional structured pointer back to whatever produced this
    notification — e.g. ``{"goal_id": "...", "run_id": "..."}``. The
    frontend uses it to deep-link from a notification to its source."""


@runtime_checkable
class NotificationProvider(Protocol):
    """Protocol exposed by the notifications service.

    Resolved by other services via
    ``resolver.get_capability("notifications")``. The runtime-checkable
    ``isinstance`` narrowing lets callers avoid importing the concrete
    ``NotificationService`` class.
    """

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        """Persist a notification and publish ``notification.received``."""
        ...
```

- [ ] **Step 2: Verify imports**

Run:
```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.interfaces.notifications import Notification, NotificationUrgency, NotificationProvider; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/interfaces/notifications.py
git commit -m "interfaces: add Notification, NotificationUrgency, NotificationProvider"
```

---

### Task 2: Create the service skeleton with `service_info()` and lifecycle

**Files:**
- Create: `src/gilbert/core/services/notifications.py`

- [ ] **Step 1: Write the skeleton**

Write `src/gilbert/core/services/notifications.py` with:

```python
"""NotificationService — persists user-addressed notifications and publishes
``notification.received`` bus events.

Live WebSocket delivery uses the existing event-bus dispatch
(``WsConnectionManager._dispatch_event``) with a per-user content filter
in ``WsConnection.can_see_notification_event``. No separate push API.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.notifications import (
    Notification,
    NotificationUrgency,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)

logger = logging.getLogger(__name__)


_COLLECTION = "notifications"
_NOTIFICATION_RECEIVED_EVENT = "notification.received"


class NotificationService(Service):
    """Persists notifications and publishes ``notification.received``.

    Capabilities declared:

    - ``notifications`` — satisfies ``NotificationProvider``.
    - ``ws_handlers`` — exposes RPCs for list / mark_read / mark_all_read
      / delete.
    """

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None  # EventBus instance from EventBusProvider

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="notifications",
            capabilities=frozenset({"notifications", "ws_handlers"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("entity_storage capability does not implement StorageProvider")
        self._storage = storage_svc.backend

        event_bus_svc = resolver.require_capability("event_bus")
        if not isinstance(event_bus_svc, EventBusProvider):
            raise RuntimeError("event_bus capability does not implement EventBusProvider")
        self._event_bus = event_bus_svc.bus

        await self._storage.ensure_index(
            IndexDefinition(
                collection=_COLLECTION,
                fields=("user_id", "read", "created_at"),
            )
        )
        logger.info("NotificationService started")

    async def stop(self) -> None:
        return None
```

- [ ] **Step 2: Verify imports**

```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.core.services.notifications import NotificationService; print('ok')"
```

Expected: `ok`.

If imports fail, inspect the error. The most likely failures:
- `Service` / `ServiceInfo` / `ServiceResolver` import paths — confirm the existing pattern in `src/gilbert/core/services/user_memory.py` lines 38, 38, 38.
- `StorageBackend` / `StorageProvider` paths — same: see `user_memory.py:39-46`.

If your imports diverge from those reference files, update yours to match.

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/core/services/notifications.py
git commit -m "notifications: add service skeleton (service_info, start, stop)"
```

---

### Task 3: Test scaffold + first behavior — `notify_user` persists a Notification

**Files:**
- Create: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Read an existing test in the repo to learn the SQLite fixture pattern**

```bash
cd /home/assistant/gilbert && grep -rln "SqliteBackend\|tmp_path\|in_memory" tests/unit/ tests/integration/ 2>/dev/null | head -10
```

Open one of those files and note how it constructs a real SQLite-backed `StorageBackend` for tests. The pattern likely uses `tmp_path` (pytest fixture) or an in-memory database. If the project has a shared `conftest.py` fixture, use that.

If you cannot find a clear pattern, fall back to constructing `gilbert.storage.sqlite.SqliteBackend` directly with a `tmp_path / "test.db"` path — but check the actual class name first via `ls src/gilbert/storage/`.

- [ ] **Step 2: Write the test scaffold**

Write `tests/unit/core/test_notification_service.py`. Adapt the SQLite construction to whatever pattern Step 1 surfaced. The tests below assume a `make_storage` helper that returns a working `StorageBackend`; write that helper using whatever pattern fits.

```python
"""Unit tests for NotificationService."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from gilbert.core.services.notifications import NotificationService
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.notifications import (
    Notification,
    NotificationUrgency,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend, StorageProvider

pytestmark = pytest.mark.asyncio


# ── Test doubles ──────────────────────────────────────────────────


class _FakeEventBus:
    """Captures published events without dispatching them."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class _FakeEventBusProvider:
    def __init__(self, bus: _FakeEventBus) -> None:
        self.bus = bus


class _FakeStorageProvider:
    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend


class _FakeResolver:
    """Minimal ``ServiceResolver`` for unit tests."""

    def __init__(self, capabilities: dict[str, Any]) -> None:
        self._caps = capabilities

    def require_capability(self, key: str) -> Any:
        if key not in self._caps:
            raise RuntimeError(f"capability not provided: {key}")
        return self._caps[key]

    def get_capability(self, key: str) -> Any:
        return self._caps.get(key)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def storage(tmp_path: Any) -> StorageBackend:
    """Build a real SQLite-backed StorageBackend.

    NOTE: replace this body with whatever pattern the rest of the
    project's tests use. See Step 1 of Task 3 — find an existing test
    that builds a StorageBackend and copy its construction.
    """
    # Placeholder — replace with the actual project pattern
    from gilbert.storage.sqlite import SqliteBackend  # adjust if path differs
    backend = SqliteBackend(str(tmp_path / "test.db"))
    await backend.initialize()
    return backend


@pytest.fixture
async def service(storage: StorageBackend) -> NotificationService:
    bus = _FakeEventBus()
    svc = NotificationService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(storage),
            "event_bus": _FakeEventBusProvider(bus),
        }
    )
    await svc.start(resolver)
    # Stash the bus on the service for assertions
    svc._test_bus = bus  # type: ignore[attr-defined]
    return svc


# ── Tests ─────────────────────────────────────────────────────────


async def test_notify_user_persists_a_notification(
    service: NotificationService, storage: StorageBackend
) -> None:
    n = await service.notify_user(
        user_id="u_alice",
        message="hello",
        urgency=NotificationUrgency.NORMAL,
        source="test",
    )

    assert isinstance(n, Notification)
    assert n.user_id == "u_alice"
    assert n.message == "hello"
    assert n.urgency == NotificationUrgency.NORMAL
    assert n.source == "test"
    assert n.read is False
    assert n.read_at is None
    assert n.source_ref is None
    assert n.id  # non-empty
    assert isinstance(n.created_at, datetime)

    # Verify entity exists in storage
    raw = await storage.get("notifications", n.id)
    assert raw is not None
    assert raw["user_id"] == "u_alice"
    assert raw["message"] == "hello"
```

- [ ] **Step 3: Run the test — expect FAIL**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_notify_user_persists_a_notification -v
```

Expected: FAIL — `NotificationService.notify_user` is not yet implemented.

If you get a fixture or import error instead of an `AttributeError` for `notify_user`, fix the test infrastructure first. Common issues:
- Wrong import path for `SqliteBackend` — search the codebase to find the right one.
- pytest-asyncio fixture mode — check the project's `pyproject.toml` `[tool.pytest.ini_options]` for `asyncio_mode`. If missing, add `asyncio_mode = "auto"` is not your call — instead, add `@pytest.mark.asyncio` to async fixtures (Python 3.12 + recent pytest-asyncio).

If the project uses a global `conftest.py` `storage` fixture, drop the inline fixture in this test file and rely on the project's.

- [ ] **Step 4: Implement `notify_user`**

In `src/gilbert/core/services/notifications.py`, append:

```python


    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        """Persist a notification and publish ``notification.received``.

        The bus event delivers the notification to live WebSocket
        connections of the target user via the per-event filter in
        ``WsConnection.can_see_notification_event``.
        """
        if self._storage is None or self._event_bus is None:
            raise RuntimeError("NotificationService.start() not called")

        notification = Notification(
            id=str(uuid.uuid4()),
            user_id=user_id,
            source=source,
            message=message,
            urgency=urgency,
            created_at=datetime.now(UTC),
            source_ref=source_ref,
        )
        await self._storage.put(_COLLECTION, notification.id, _serialize(notification))
        await self._event_bus.publish(
            Event(
                event_type=_NOTIFICATION_RECEIVED_EVENT,
                data=_serialize(notification),
                source="notifications",
                timestamp=notification.created_at,
            )
        )
        return notification


def _serialize(n: Notification) -> dict[str, Any]:
    """Convert a Notification to its persisted/wire dict form."""
    return {
        "id": n.id,
        "user_id": n.user_id,
        "source": n.source,
        "message": n.message,
        "urgency": n.urgency.value,
        "created_at": n.created_at.isoformat(),
        "read": n.read,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "source_ref": n.source_ref,
    }


def _deserialize(d: dict[str, Any]) -> Notification:
    """Reverse of ``_serialize``."""
    read_at_raw = d.get("read_at")
    return Notification(
        id=d["id"],
        user_id=d["user_id"],
        source=d.get("source", "system"),
        message=d["message"],
        urgency=NotificationUrgency(d.get("urgency", "normal")),
        created_at=datetime.fromisoformat(d["created_at"]),
        read=bool(d.get("read", False)),
        read_at=datetime.fromisoformat(read_at_raw) if read_at_raw else None,
        source_ref=d.get("source_ref"),
    )
```

- [ ] **Step 5: Run the test — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_notify_user_persists_a_notification -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
git commit -m "notifications: implement notify_user with persistence and event publish"
```

---

### Task 4: Test + verify — `notify_user` publishes a `notification.received` event

**Files:**
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Append the test**

```python


async def test_notify_user_publishes_notification_received_event(
    service: NotificationService,
) -> None:
    n = await service.notify_user(
        user_id="u_bob",
        message="ping",
        urgency=NotificationUrgency.URGENT,
        source="agent",
        source_ref={"goal_id": "g_42", "run_id": "r_99"},
    )

    bus: _FakeEventBus = service._test_bus  # type: ignore[attr-defined]
    assert len(bus.published) == 1
    ev = bus.published[0]

    assert ev.event_type == "notification.received"
    assert ev.source == "notifications"
    assert ev.data["id"] == n.id
    assert ev.data["user_id"] == "u_bob"
    assert ev.data["message"] == "ping"
    assert ev.data["urgency"] == "urgent"
    assert ev.data["source"] == "agent"
    assert ev.data["source_ref"] == {"goal_id": "g_42", "run_id": "r_99"}
    assert ev.data["read"] is False
```

- [ ] **Step 2: Run the test — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_notify_user_publishes_notification_received_event -v
```

Expected: PASS (the event publish was already implemented in Task 3).

- [ ] **Step 3: Run all tests in the module**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/core/test_notification_service.py
git commit -m "test(notifications): cover notification.received event publish"
```

---

### Task 5: Add WS connection content filter for notification events

**Files:**
- Modify: `src/gilbert/web/ws_protocol.py`

- [ ] **Step 1: Add the filter method to `WsConnection`**

Open `src/gilbert/web/ws_protocol.py`. Find the existing content-filter methods on `WsConnection` (`can_see_workspace_event`, around line 117). Add a new `can_see_notification_event` method following the same pattern. Insert it right after `can_see_workspace_event`:

```python
    def can_see_notification_event(self, event: Event) -> bool:
        """Content-level filter for notification events.

        Notifications are 1:1 — addressed to a specific user via the
        ``user_id`` field on ``event.data``. Connections only see
        notification events for their own user.
        """
        if not event.event_type.startswith("notification."):
            return True
        return event.data.get("user_id") == self.user_id
```

- [ ] **Step 2: Wire the filter into `_dispatch_event`**

Find `WsConnectionManager._dispatch_event` (around line 350). Add the new filter alongside the existing ones. The chain should look like:

```python
    async def _dispatch_event(self, event: Event) -> None:
        """Dispatch a bus event to all eligible connections."""
        for conn in self._connections:
            if not conn.matches_subscription(event.event_type):
                continue
            if not conn.can_see_event(event.event_type):
                continue
            if not conn.can_see_chat_event(event):
                continue
            if not conn.can_see_auth_event(event):
                continue
            if not conn.can_see_workspace_event(event):
                continue
            if not conn.can_see_notification_event(event):
                continue
            conn.send_event(event)
```

(The new line is the `can_see_notification_event` check.)

- [ ] **Step 3: Verify the file imports/loads**

```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.web.ws_protocol import WsConnection, WsConnectionManager; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Run the full repo test suite to confirm no regression**

```bash
cd /home/assistant/gilbert && uv run pytest -q 2>&1 | tail -5
```

Expected: pre-existing tests still pass. If any unrelated test fails, that's a pre-existing flake — note it but do not fix here.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/web/ws_protocol.py
git commit -m "ws: add can_see_notification_event filter restricting notification.* to target user"
```

---

### Task 6: Test the dispatch filter directly

**Files:**
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Append the test**

This test exercises the filter on `WsConnection` independently of the service.

```python


async def test_can_see_notification_event_filters_by_user_id() -> None:
    """The WsConnection filter should only allow notification events whose
    data.user_id matches the connection's user.
    """
    from datetime import UTC, datetime as _dt

    from gilbert.interfaces.auth import UserContext
    from gilbert.web.ws_protocol import WsConnection, WsConnectionManager

    # Build minimal connection objects bound to two different users
    manager = WsConnectionManager()
    alice_ctx = UserContext(user_id="u_alice", roles=frozenset({"user"}))
    bob_ctx = UserContext(user_id="u_bob", roles=frozenset({"user"}))
    alice_conn = WsConnection(user_ctx=alice_ctx, user_level=1, manager=manager)
    bob_conn = WsConnection(user_ctx=bob_ctx, user_level=1, manager=manager)

    notif_event = Event(
        event_type="notification.received",
        data={"user_id": "u_alice", "message": "hi"},
        source="notifications",
        timestamp=_dt.now(UTC),
    )

    # Alice's connection accepts; Bob's rejects
    assert alice_conn.can_see_notification_event(notif_event) is True
    assert bob_conn.can_see_notification_event(notif_event) is False

    # Non-notification events pass through unfiltered (returns True)
    other_event = Event(
        event_type="chat.message.created",
        data={"user_id": "u_alice"},
        source="chat",
        timestamp=_dt.now(UTC),
    )
    assert alice_conn.can_see_notification_event(other_event) is True
    assert bob_conn.can_see_notification_event(other_event) is True
```

- [ ] **Step 2: Run the test — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_can_see_notification_event_filters_by_user_id -v
```

Expected: PASS.

If the test fails because `UserContext` has a different signature (e.g. requires more fields or has a different module path), inspect `src/gilbert/interfaces/auth.py` and adapt the constructor call. Required-vs-optional fields on `UserContext` can vary by project version.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_notification_service.py
git commit -m "test(notifications): cover WsConnection.can_see_notification_event filter"
```

---

### Task 7: WS RPC — `notification.list`

**Files:**
- Modify: `src/gilbert/core/services/notifications.py`
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Write the failing test**

Append:

```python


async def test_notification_list_returns_user_notifications_with_unread_count(
    service: NotificationService,
) -> None:
    # Three notifications for alice (one read), one for bob
    a1 = await service.notify_user(user_id="u_alice", message="m1", source="t")
    a2 = await service.notify_user(user_id="u_alice", message="m2", source="t")
    a3 = await service.notify_user(user_id="u_alice", message="m3", source="t")
    b1 = await service.notify_user(user_id="u_bob", message="b1", source="t")

    # Mark a1 as read by directly editing storage to skip mark_read coupling
    raw = await service._storage.get("notifications", a1.id)
    assert raw is not None
    raw["read"] = True
    raw["read_at"] = a1.created_at.isoformat()
    await service._storage.put("notifications", a1.id, raw)

    # Build a fake WsConnection-like context for the RPC handler
    handlers = service.get_ws_handlers()
    list_handler = handlers["notification.list"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext
            self.user_ctx = UserContext(user_id=user_id, roles=frozenset({"user"}))
            self.user_level = 1

        @property
        def user_id(self) -> str:
            return self.user_ctx.user_id

    alice_conn = _Conn("u_alice")
    result = await list_handler(alice_conn, {"id": "frame-1"})

    assert result is not None
    assert result["unread_count"] == 2  # a2, a3 are unread
    items = result["items"]
    assert len(items) == 3  # all of alice's, regardless of read state
    item_ids = {i["id"] for i in items}
    assert item_ids == {a1.id, a2.id, a3.id}
    assert b1.id not in item_ids  # bob's notification not visible to alice
```

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_notification_list_returns_user_notifications_with_unread_count -v
```

Expected: FAIL — `get_ws_handlers` not implemented.

- [ ] **Step 3: Add `get_ws_handlers` and the `list` handler**

In `src/gilbert/core/services/notifications.py`, add to the `NotificationService` class:

```python


    def get_ws_handlers(self) -> dict[str, Any]:
        """Return the frame-type → handler map for this service.

        Implements ``WsHandlerProvider`` (structurally — the protocol is
        runtime-checkable, so explicit inheritance isn't required).
        """
        return {
            "notification.list": self._ws_list,
            "notification.mark_read": self._ws_mark_read,
            "notification.mark_all_read": self._ws_mark_all_read,
            "notification.delete": self._ws_delete,
        }

    async def _ws_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handler for ``notification.list`` frames.

        Args (in ``frame``):
        - ``filter``: ``{read?: bool, source?: str, since?: ISO8601 str}``
        - ``limit``: int (default 100)

        Returns ``{type, ref, items: [...], unread_count: int}``.
        """
        if self._storage is None:
            raise RuntimeError("NotificationService.start() not called")

        user_id = conn.user_ctx.user_id
        filt = frame.get("filter") or {}
        limit = int(frame.get("limit") or 100)

        filters = [Filter(field="user_id", op=FilterOp.EQ, value=user_id)]
        if "read" in filt:
            filters.append(
                Filter(field="read", op=FilterOp.EQ, value=bool(filt["read"]))
            )
        if "source" in filt:
            filters.append(
                Filter(field="source", op=FilterOp.EQ, value=str(filt["source"]))
            )
        if "since" in filt:
            filters.append(
                Filter(field="created_at", op=FilterOp.GT, value=str(filt["since"]))
            )

        items_raw = await self._storage.query(
            Query(
                collection=_COLLECTION,
                filters=filters,
                sort=[("created_at", "desc")],
                limit=limit,
            )
        )
        # Compute unread count for this user (independent of filter)
        unread_raw = await self._storage.query(
            Query(
                collection=_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                    Filter(field="read", op=FilterOp.EQ, value=False),
                ],
            )
        )

        return {
            "type": "notification.list.result",
            "ref": frame.get("id"),
            "items": items_raw,
            "unread_count": len(unread_raw),
        }

    async def _ws_mark_read(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handler for ``notification.mark_read`` frames.

        Implementation comes in Task 8.
        """
        return {
            "type": "notification.mark_read.result",
            "ref": frame.get("id"),
            "ok": False,
            "error": "not_implemented",
        }

    async def _ws_mark_all_read(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        return {
            "type": "notification.mark_all_read.result",
            "ref": frame.get("id"),
            "ok": False,
            "error": "not_implemented",
        }

    async def _ws_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        return {
            "type": "notification.delete.result",
            "ref": frame.get("id"),
            "ok": False,
            "error": "not_implemented",
        }
```

The `_ws_mark_read` / `_ws_mark_all_read` / `_ws_delete` stubs return `not_implemented` for now; subsequent tasks fill them in.

- [ ] **Step 4: Run — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_notification_list_returns_user_notifications_with_unread_count -v
```

Expected: PASS.

If the storage `Query` API differs from what the handler uses, inspect the actual `StorageBackend.query` signature in `src/gilbert/interfaces/storage.py` and adapt. Common deviations: `Query(collection, filters=...)` vs positional args; sort spec format (tuples vs Strings); etc.

- [ ] **Step 5: Run all tests in the module**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
git commit -m "notifications: implement notification.list WS RPC"
```

---

### Task 8: WS RPC — `notification.mark_read` (single)

**Files:**
- Modify: `src/gilbert/core/services/notifications.py`
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Write the failing test**

Append:

```python


async def test_notification_mark_read_sets_read_and_read_at(
    service: NotificationService,
) -> None:
    n = await service.notify_user(user_id="u_alice", message="m", source="t")
    handlers = service.get_ws_handlers()
    handler = handlers["notification.mark_read"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext
            self.user_ctx = UserContext(user_id=user_id, roles=frozenset({"user"}))
            self.user_level = 1

    result = await handler(_Conn("u_alice"), {"id": "f1", "notification_id": n.id})

    assert result is not None
    assert result["ok"] is True

    raw = await service._storage.get("notifications", n.id)
    assert raw is not None
    assert raw["read"] is True
    assert raw["read_at"] is not None


async def test_notification_mark_read_rejects_other_users_notifications(
    service: NotificationService,
) -> None:
    n = await service.notify_user(user_id="u_alice", message="m", source="t")
    handlers = service.get_ws_handlers()
    handler = handlers["notification.mark_read"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext
            self.user_ctx = UserContext(user_id=user_id, roles=frozenset({"user"}))
            self.user_level = 1

    # Bob tries to mark Alice's notification as read
    result = await handler(_Conn("u_bob"), {"id": "f1", "notification_id": n.id})

    assert result is not None
    assert result["ok"] is False
    assert "not_found" in result.get("error", "") or "forbidden" in result.get("error", "")

    # Confirm the notification is still unread
    raw = await service._storage.get("notifications", n.id)
    assert raw is not None
    assert raw["read"] is False
```

- [ ] **Step 2: Run — expect FAIL** (both tests)

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py::test_notification_mark_read_sets_read_and_read_at tests/unit/core/test_notification_service.py::test_notification_mark_read_rejects_other_users_notifications -v
```

Expected: FAIL — `_ws_mark_read` returns `not_implemented`.

- [ ] **Step 3: Implement `_ws_mark_read`**

Replace the stub `_ws_mark_read` body in `src/gilbert/core/services/notifications.py` with:

```python
    async def _ws_mark_read(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Mark one notification as read. Owner-only."""
        if self._storage is None:
            raise RuntimeError("NotificationService.start() not called")

        notification_id = frame.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id:
            return {
                "type": "notification.mark_read.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "missing notification_id",
            }

        raw = await self._storage.get(_COLLECTION, notification_id)
        if raw is None or raw.get("user_id") != conn.user_ctx.user_id:
            # Treat "not yours" the same as "doesn't exist" so we don't leak
            # the existence of other users' notifications.
            return {
                "type": "notification.mark_read.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }

        if not raw.get("read"):
            raw["read"] = True
            raw["read_at"] = datetime.now(UTC).isoformat()
            await self._storage.put(_COLLECTION, notification_id, raw)

        return {
            "type": "notification.mark_read.result",
            "ref": frame.get("id"),
            "ok": True,
        }
```

- [ ] **Step 4: Run — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
git commit -m "notifications: implement notification.mark_read with owner-only auth"
```

---

### Task 9: WS RPC — `notification.mark_all_read`

**Files:**
- Modify: `src/gilbert/core/services/notifications.py`
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Write the failing test**

```python


async def test_notification_mark_all_read_marks_all_user_notifications(
    service: NotificationService,
) -> None:
    a1 = await service.notify_user(user_id="u_alice", message="1", source="t")
    a2 = await service.notify_user(user_id="u_alice", message="2", source="t")
    b1 = await service.notify_user(user_id="u_bob", message="3", source="t")

    handlers = service.get_ws_handlers()
    handler = handlers["notification.mark_all_read"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext
            self.user_ctx = UserContext(user_id=user_id, roles=frozenset({"user"}))
            self.user_level = 1

    result = await handler(_Conn("u_alice"), {"id": "f1"})

    assert result is not None
    assert result["count"] == 2

    a1_raw = await service._storage.get("notifications", a1.id)
    a2_raw = await service._storage.get("notifications", a2.id)
    b1_raw = await service._storage.get("notifications", b1.id)
    assert a1_raw is not None and a1_raw["read"] is True
    assert a2_raw is not None and a2_raw["read"] is True
    # Bob's notification is untouched
    assert b1_raw is not None and b1_raw["read"] is False
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `_ws_mark_all_read`**

Replace the stub:

```python
    async def _ws_mark_all_read(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Mark every unread notification for this user as read."""
        if self._storage is None:
            raise RuntimeError("NotificationService.start() not called")

        user_id = conn.user_ctx.user_id
        unread = await self._storage.query(
            Query(
                collection=_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                    Filter(field="read", op=FilterOp.EQ, value=False),
                ],
            )
        )

        now_iso = datetime.now(UTC).isoformat()
        for raw in unread:
            raw["read"] = True
            raw["read_at"] = now_iso
            await self._storage.put(_COLLECTION, raw["id"], raw)

        return {
            "type": "notification.mark_all_read.result",
            "ref": frame.get("id"),
            "count": len(unread),
        }
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
git commit -m "notifications: implement notification.mark_all_read"
```

---

### Task 10: WS RPC — `notification.delete`

**Files:**
- Modify: `src/gilbert/core/services/notifications.py`
- Modify: `tests/unit/core/test_notification_service.py`

- [ ] **Step 1: Write the failing tests**

```python


async def test_notification_delete_removes_user_notification(
    service: NotificationService,
) -> None:
    n = await service.notify_user(user_id="u_alice", message="m", source="t")
    handlers = service.get_ws_handlers()
    handler = handlers["notification.delete"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext
            self.user_ctx = UserContext(user_id=user_id, roles=frozenset({"user"}))
            self.user_level = 1

    result = await handler(_Conn("u_alice"), {"id": "f1", "notification_id": n.id})

    assert result is not None
    assert result["ok"] is True

    raw = await service._storage.get("notifications", n.id)
    assert raw is None


async def test_notification_delete_rejects_other_users(
    service: NotificationService,
) -> None:
    n = await service.notify_user(user_id="u_alice", message="m", source="t")
    handlers = service.get_ws_handlers()
    handler = handlers["notification.delete"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext
            self.user_ctx = UserContext(user_id=user_id, roles=frozenset({"user"}))
            self.user_level = 1

    result = await handler(_Conn("u_bob"), {"id": "f1", "notification_id": n.id})

    assert result is not None
    assert result["ok"] is False

    # Notification still exists
    raw = await service._storage.get("notifications", n.id)
    assert raw is not None
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `_ws_delete`**

Replace the stub:

```python
    async def _ws_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Delete one of the calling user's notifications."""
        if self._storage is None:
            raise RuntimeError("NotificationService.start() not called")

        notification_id = frame.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id:
            return {
                "type": "notification.delete.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "missing notification_id",
            }

        raw = await self._storage.get(_COLLECTION, notification_id)
        if raw is None or raw.get("user_id") != conn.user_ctx.user_id:
            return {
                "type": "notification.delete.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_found",
            }

        await self._storage.delete(_COLLECTION, notification_id)
        return {
            "type": "notification.delete.result",
            "ref": frame.get("id"),
            "ok": True,
        }
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
git commit -m "notifications: implement notification.delete with owner-only auth"
```

---

### Task 11: Register `NotificationService` in `app.py`

**Files:**
- Modify: `src/gilbert/core/app.py`

- [ ] **Step 1: Find the existing service registration block**

```bash
cd /home/assistant/gilbert && grep -n "service_manager.register\|NotificationService\|user_memory\|UserMemoryService" /home/assistant/gilbert/src/gilbert/core/app.py | head -20
```

- [ ] **Step 2: Add the registration**

`NotificationService` depends on `entity_storage` and `event_bus` capabilities. Register it AFTER those services are registered but BEFORE any service that consumes the `notifications` capability (Phase 4's agent service will).

Open `app.py`. Find a good insertion point — typically after `UserMemoryService` registration (which has similar dependencies). Add:

```python
    from gilbert.core.services.notifications import NotificationService
    notification_service = NotificationService()
    service_manager.register(ServiceInstance(notification_service))
```

(Adapt to the project's exact `ServiceInstance` / `register` pattern — match what the surrounding code does.)

- [ ] **Step 3: Verify the app imports**

```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.core.app import *; print('ok')" 2>&1 | tail -5
```

Expected: `ok` (or no errors related to NotificationService).

- [ ] **Step 4: Run the full repo test suite**

```bash
cd /home/assistant/gilbert && uv run pytest -q 2>&1 | tail -5
```

Expected: all pre-existing tests still pass; new notification tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/app.py
git commit -m "app: register NotificationService alongside UserMemoryService"
```

---

### Task 12: Final pass — mypy + ruff + full repo test

**Files:** All notification files plus the modified `ws_protocol.py` and `app.py`.

- [ ] **Step 1: mypy clean**

```bash
cd /home/assistant/gilbert && uv run mypy src/gilbert/interfaces/notifications.py src/gilbert/core/services/notifications.py src/gilbert/web/ws_protocol.py
```

Expected: 0 errors. Fix any issues introduced by this phase. Pre-existing issues in `ws_protocol.py` are not in scope unless this plan's changes regressed them.

- [ ] **Step 2: ruff format + check**

```bash
cd /home/assistant/gilbert && uv run ruff format src/gilbert/interfaces/notifications.py src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
cd /home/assistant/gilbert && uv run ruff check src/gilbert/interfaces/notifications.py src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
```

Expected: format clean (or normalizes whitespace); check reports no errors.

- [ ] **Step 3: Run all 9 notification tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_notification_service.py -v
```

Expected: 9 passed.

- [ ] **Step 4: Run the full repo test suite**

```bash
cd /home/assistant/gilbert && uv run pytest -q 2>&1 | tail -10
```

Expected: only the same 2 pre-existing anthropic-plugin failures from Phase 1; no new failures.

- [ ] **Step 5: Commit any formatting changes**

```bash
cd /home/assistant/gilbert && git add src/gilbert/interfaces/notifications.py src/gilbert/core/services/notifications.py tests/unit/core/test_notification_service.py
cd /home/assistant/gilbert && git diff --cached --quiet || git commit -m "notifications: ruff formatting pass"
```

---

### Task 13: Memory + index

**Files:**
- Create: `.claude/memory/memory-notification-service.md`
- Modify: `.claude/memory/MEMORIES.md`

- [ ] **Step 1: Create the memory file**

Write `.claude/memory/memory-notification-service.md`:

```markdown
# NotificationService

## Summary
Persists user-addressed notifications and publishes ``notification.received`` bus
events. Lives in ``src/gilbert/core/services/notifications.py``.

## Details
**Capabilities declared:** ``notifications`` (satisfies
``NotificationProvider``), ``ws_handlers``.

**Public method:** ``notify_user(*, user_id, message, urgency, source,
source_ref)`` — persists a ``Notification`` entity to the
``notifications`` collection and publishes ``notification.received``
on the bus with the entity's serialized form as ``data``.

**WS RPCs:** ``notification.list`` (filterable, returns items + unread_count),
``notification.mark_read``, ``notification.mark_all_read``,
``notification.delete``. All RBAC-checked against the calling user — you
can only see, mark, or delete your own notifications.

**Live delivery to WebSocket clients:** uses the existing event-bus →
``WsConnectionManager._dispatch_event`` flow. ``WsConnection`` has a
``can_see_notification_event`` content filter that rejects events whose
``data["user_id"]`` does not match the connection's user. There is NO
separate ``push_to_user`` helper or per-user connection registry —
existing dispatch + per-event-type filter is the established pattern.

**Indexes:** ``(user_id, read, created_at)``.

**Audible/visual signal logic** lives entirely in the frontend. The
backend stamps an ``urgency`` field (``info`` / ``normal`` / ``urgent``)
and lets the UI decide.

## Related
- ``src/gilbert/interfaces/notifications.py``
- ``src/gilbert/core/services/notifications.py``
- ``tests/unit/core/test_notification_service.py``
- ``src/gilbert/web/ws_protocol.py:can_see_notification_event``
- ``docs/superpowers/specs/2026-05-03-autonomous-agent-design.md``
- ``docs/superpowers/plans/2026-05-03-autonomous-agent-phase-3-notification-backend.md``
```

- [ ] **Step 2: Add to the index**

Append to `.claude/memory/MEMORIES.md` (preserving everything else):

```markdown
- [NotificationService](memory-notification-service.md) — user-addressed notifications + WS dispatch via per-event filter
```

Match the existing line style.

- [ ] **Step 3: Commit**

```bash
git add .claude/memory/memory-notification-service.md .claude/memory/MEMORIES.md
git commit -m "memory: index NotificationService"
```

---

## Phase 3a Complete

At this point:
- `NotificationService` exists, implements `NotificationProvider`, exposes 4 WS RPCs.
- 9 tests pass (notify_user persist + event publish; dispatch filter; list; mark_read x2; mark_all_read; delete x2).
- The service is registered in `app.py` and starts cleanly.
- WebSocket clients receive notification events filtered to their own user_id via the existing dispatch + content filter.
- mypy clean, ruff clean, full repo suite still passes.
- Memory entry indexed.

Phase 3b (frontend: bell icon, badge, dropdown, `/notifications` page, WS frame handler, audible/visual signals) gets its own plan after this ships. Phase 4 (`AutonomousAgentService`) can now use `NotificationProvider.notify_user` from its agent built-in `notify_user` tool without waiting for the UI.

---

## Self-Review Notes

Spec coverage check (Phase 3 backend portion):
- [x] `NotificationService` with `notify_user(user_id, message, urgency, source, source_ref)` — Tasks 3, 4
- [x] Persists `notifications` entity collection — Task 3
- [x] Publishes `notification.received` event with entity data — Task 4
- [x] Per-event content filter restricts delivery to target user — Tasks 5, 6
- [x] WS RPCs: list, mark_read, mark_all_read, delete with owner-only auth — Tasks 7-10
- [x] Indexes `(user_id, read, created_at)` — Task 2 (`ensure_index` in `start()`)
- [x] Service registered in app.py — Task 11

Out-of-scope items that the spec mentioned but Phase 3a defers:
- `notify_user` as a global ToolDefinition — deferred (Phase 4 wires it as a per-run agent tool with goal context).
- Notification dedupe / grouping — explicitly out of scope per spec.
- `audible_urgencies` config param on the service — frontend concern, deferred to Phase 3b.

Type consistency: `Notification`, `NotificationUrgency`, and `NotificationProvider` are defined once in `interfaces/notifications.py` and re-used everywhere. Storage uses `_serialize` / `_deserialize` consistently for the dict<->dataclass boundary.

Placeholder scan: the `_ws_mark_read`, `_ws_mark_all_read`, `_ws_delete` stubs in Task 7 return `not_implemented` deliberately as TDD intermediate state — they are filled in by Tasks 8-10. No surviving placeholders after Task 10.
