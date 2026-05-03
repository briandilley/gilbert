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
