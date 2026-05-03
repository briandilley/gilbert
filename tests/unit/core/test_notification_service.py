"""Unit tests for NotificationService."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from gilbert.core.services.notifications import NotificationService
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.notifications import (
    Notification,
    NotificationUrgency,
)
from gilbert.interfaces.storage import StorageBackend

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
    from datetime import UTC
    from datetime import datetime as _dt

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

            # Match whatever fields UserContext requires (see Task 6 for the working set)
            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
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


async def test_notification_mark_read_sets_read_and_read_at(
    service: NotificationService,
) -> None:
    n = await service.notify_user(user_id="u_alice", message="m", source="t")
    handlers = service.get_ws_handlers()
    handler = handlers["notification.mark_read"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext

            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
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

            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
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

            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
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


async def test_notification_delete_removes_user_notification(
    service: NotificationService,
) -> None:
    n = await service.notify_user(user_id="u_alice", message="m", source="t")
    handlers = service.get_ws_handlers()
    handler = handlers["notification.delete"]

    class _Conn:
        def __init__(self, user_id: str) -> None:
            from gilbert.interfaces.auth import UserContext

            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
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

            self.user_ctx = UserContext(
                user_id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
            self.user_level = 1

    result = await handler(_Conn("u_bob"), {"id": "f1", "notification_id": n.id})

    assert result is not None
    assert result["ok"] is False

    # Notification still exists
    raw = await service._storage.get("notifications", n.id)
    assert raw is not None
