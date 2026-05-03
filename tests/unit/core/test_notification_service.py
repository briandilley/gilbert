"""Unit tests for NotificationService."""

from __future__ import annotations

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


class _FakeEventBus(EventBus):
    """Captures published events without dispatching them."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def subscribe(self, event_type: str, handler: Any) -> Any:
        return lambda: None

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        return lambda: None

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class _FakeEventBusProvider:
    def __init__(self, bus: _FakeEventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> _FakeEventBus:
        return self._bus


class _FakeStorageProvider:
    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        from gilbert.interfaces.storage import NamespacedStorageBackend

        return NamespacedStorageBackend(self._backend, namespace)


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

    def get_all(self, key: str) -> list[Any]:
        svc = self._caps.get(key)
        return [svc] if svc else []


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def service(sqlite_storage: StorageBackend) -> NotificationService:
    bus = _FakeEventBus()
    svc = NotificationService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
        }
    )
    await svc.start(resolver)
    # Stash the bus on the service for assertions
    svc._test_bus = bus  # type: ignore[attr-defined]
    return svc


# ── Tests ─────────────────────────────────────────────────────────


async def test_notify_user_persists_a_notification(
    service: NotificationService, sqlite_storage: StorageBackend
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
    raw = await sqlite_storage.get("notifications", n.id)
    assert raw is not None
    assert raw["user_id"] == "u_alice"
    assert raw["message"] == "hello"


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


async def test_can_see_notification_event_filters_by_user_id() -> None:
    """The WsConnection filter should only allow notification events whose
    data.user_id matches the connection's user.
    """
    from datetime import UTC, datetime as _dt

    from gilbert.interfaces.auth import UserContext
    from gilbert.web.ws_protocol import WsConnection, WsConnectionManager

    # Build minimal connection objects bound to two different users
    manager = WsConnectionManager()
    alice_ctx = UserContext(
        user_id="u_alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    )
    bob_ctx = UserContext(
        user_id="u_bob",
        email="bob@example.com",
        display_name="Bob",
        roles=frozenset({"user"}),
    )
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
