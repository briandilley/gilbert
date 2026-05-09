"""Unit tests for PushNotificationService.

Covers:
- The bounded-queue dispatcher: publisher unblocks even when a backend
  blocks indefinitely; the worker is what blocks.
- ContextVar preservation through the queue + per-route Task spawn.
- Filter logic: urgency floor, source allow/deny, quiet hours
  (wrap-around + DST 2026-03-08 / 2026-11-01 fixtures).
- Retry / backoff / URGENT escalation.
- Owner-scoping helper for the WS RPCs.
- AI tools + UI-block confirm flow for delete.
- Multi-user isolation.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.notifications import NotificationService
from gilbert.core.services.push_notifications import (
    MAX_RETRIES_CAP,
    PushNotificationService,
    _safe_repr,
    in_quiet_hours,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event
from gilbert.interfaces.notifications import (
    Notification,
    NotificationProvider,
    NotificationUrgency,
)
from gilbert.interfaces.push_notifications import (
    PushDeliveryResult,
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.ui import ToolOutput

pytestmark = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────────


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


class _FakeEventBusProvider:
    def __init__(self, bus: InMemoryEventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> InMemoryEventBus:
        return self._bus


class _FakeNotificationsProvider:
    """Records calls so we can assert URGENT-failure escalation fires."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        self.calls.append(
            {
                "user_id": user_id,
                "message": message,
                "urgency": urgency,
                "source": source,
                "source_ref": source_ref,
            }
        )
        return Notification(
            id="n",
            user_id=user_id,
            source=source,
            message=message,
            urgency=urgency,
            created_at=datetime.now(ZoneInfo("UTC")),
        )


class _FakeUserBackend:
    def __init__(self, users: dict[str, dict[str, Any]] | None = None) -> None:
        self.users: dict[str, dict[str, Any]] = users or {}

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        u = self.users.get(user_id)
        if u is None:
            return None
        return dict(u)


class _FakeUsersService:
    def __init__(self, backend: _FakeUserBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> _FakeUserBackend:
        return self._backend

    @property
    def allow_user_creation(self) -> bool:
        return True

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self._backend.users.values())


class _FakeResolver:
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


class _FakeConn:
    def __init__(
        self,
        user_id: str,
        *,
        admin: bool = False,
    ) -> None:
        self.user_ctx = UserContext(
            user_id=user_id,
            email=f"{user_id}@test",
            display_name=user_id,
        )
        self.user_id = user_id
        self.user_level = 0 if admin else 100


# ── Test backends ─────────────────────────────────────────────────────


class _FakeBackendBase(PushNotificationBackend):
    """Base test backend — DO NOT register it directly. Subclasses set
    ``backend_name`` to make the registry know about them."""

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="endpoint",
                type=ToolParameterType.STRING,
                description="Test endpoint",
                default="",
            ),
        ]

    async def initialize(self, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None


class _RecordingBackend(_FakeBackendBase):
    backend_name = "recording-backend"

    def __init__(self) -> None:
        self.calls: list[tuple[PushDestination, PushMessage]] = []
        self.delay: float = 0.0
        self.contextvar_observed: list[Any] = []
        self.observed_contextvar: contextvars.ContextVar[Any] | None = None

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.observed_contextvar is not None:
            self.contextvar_observed.append(self.observed_contextvar.get(None))
        self.calls.append((destination, message))
        return PushDeliveryResult(status=PushDeliveryStatus.DELIVERED, message="HTTP 200")


class _BlockingBackend(_FakeBackendBase):
    backend_name = "blocking-backend"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.unblock = asyncio.Event()

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        self.entered.set()
        await self.unblock.wait()
        return PushDeliveryResult(status=PushDeliveryStatus.DELIVERED, message="HTTP 200")


class _RetryBackend(_FakeBackendBase):
    backend_name = "retry-backend"

    def __init__(self) -> None:
        self.results: list[PushDeliveryResult] = []
        self.calls = 0

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


class _RaisingBackend(_FakeBackendBase):
    backend_name = "raising-backend"

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        raise RuntimeError("Bearer secret-token leaked")


# Side-effect: importing this module registers all four backends above.
_TEST_BACKEND_NAMES = (
    "recording-backend",
    "blocking-backend",
    "retry-backend",
    "raising-backend",
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def setup_service(
    sqlite_storage: StorageBackend,
) -> AsyncGenerator[
    tuple[PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend],
    None,
]:
    bus = InMemoryEventBus()
    notifications = _FakeNotificationsProvider()
    user_backend = _FakeUserBackend()
    users_svc = _FakeUsersService(user_backend)
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "notifications": notifications,
            "users": users_svc,
        }
    )
    svc = PushNotificationService()
    # Worker count is set BEFORE start since start() spawns the pool.
    svc._worker_count = 2
    await svc.start(resolver)
    # Retry knobs must be set AFTER start — start() calls
    # ``_apply_service_config`` which would otherwise overwrite our tweaks
    # with the defaults from an empty config section. Tests inspect these
    # fields and adjust in-place so retries don't actually wait the full
    # 1s + jitter on every transient error.
    svc._retry_initial_delay_s = 0.01
    svc._retry_factor = 1.0
    svc._retry_jitter_pct = 0.0
    svc._test_debounce_s = 0.0
    yield svc, bus, notifications, user_backend
    await svc.stop()


# ── Helpers ───────────────────────────────────────────────────────────


async def _publish_notification(
    bus: InMemoryEventBus,
    *,
    notification_id: str = "n_1",
    user_id: str = "u_alice",
    urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    source: str = "agent",
    message: str = "hello",
) -> None:
    await bus.publish(
        Event(
            event_type="notification.received",
            data={
                "id": notification_id,
                "user_id": user_id,
                "source": source,
                "message": message,
                "urgency": urgency.value,
                "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                "read": False,
                "source_ref": None,
            },
            source="notifications",
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
    )


async def _wait_until(
    predicate: Any, timeout: float = 2.0, interval: float = 0.01
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for predicate")


async def _create_route(
    storage: StorageBackend,
    *,
    route_id: str,
    user_id: str,
    backend_name: str = "recording-backend",
    enabled: bool = True,
    urgency_floor: str = "info",
    source_allow: list[str] | None = None,
    source_deny: list[str] | None = None,
    quiet_hours_start: str | None = None,
    quiet_hours_end: str | None = None,
    quiet_hours_timezone: str | None = None,
    destination_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "user_id": user_id,
        "label": f"route-{route_id}",
        "backend_name": backend_name,
        "destination_data": destination_data or {"endpoint": "x"},
        "enabled": enabled,
        "urgency_floor": urgency_floor,
        "source_allow": source_allow or [],
        "source_deny": source_deny or [],
        "quiet_hours_start": quiet_hours_start,
        "quiet_hours_end": quiet_hours_end,
        "quiet_hours_timezone": quiet_hours_timezone,
        "last_delivered_at": None,
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }
    await storage.put("push_notification_routes", route_id, row)
    return row


# ── Quiet-hour math ──────────────────────────────────────────────────


async def test_in_quiet_hours_basic_window() -> None:
    tz = "America/Los_Angeles"
    # 23:00 PT inside 22:00-07:00 → quiet
    assert in_quiet_hours(
        datetime(2026, 5, 9, 6, 0, tzinfo=ZoneInfo("UTC")),  # 23:00 PT prev day
        "22:00",
        "07:00",
        tz,
    )
    # 12:00 PT outside 22:00-07:00 → not quiet
    assert not in_quiet_hours(
        datetime(2026, 5, 9, 19, 0, tzinfo=ZoneInfo("UTC")),  # 12:00 PT
        "22:00",
        "07:00",
        tz,
    )


async def test_in_quiet_hours_dst_spring_forward() -> None:
    """2026-03-08 02:00 PT → 03:00 PT; the 02:30 wall-clock doesn't
    exist, but 03:30 absolutely lies inside 22:00–07:00."""
    tz = "America/Los_Angeles"
    # 03:30 PT on the spring-forward day, in UTC = 10:30 UTC
    assert in_quiet_hours(
        datetime(2026, 3, 8, 10, 30, tzinfo=ZoneInfo("UTC")),
        "22:00",
        "07:00",
        tz,
    )


async def test_in_quiet_hours_dst_fall_back() -> None:
    """2026-11-01: 02:00 PDT becomes 01:00 PST; 06:30 still lies in the
    quiet window in either offset."""
    tz = "America/Los_Angeles"
    # 06:30 PST (after fall-back) — 14:30 UTC
    assert in_quiet_hours(
        datetime(2026, 11, 1, 14, 30, tzinfo=ZoneInfo("UTC")),
        "22:00",
        "07:00",
        tz,
    )
    # 07:00 — outside the window (end is exclusive).
    assert not in_quiet_hours(
        datetime(2026, 11, 1, 15, 0, tzinfo=ZoneInfo("UTC")),
        "22:00",
        "07:00",
        tz,
    )


async def test_in_quiet_hours_disabled_when_bounds_missing() -> None:
    assert not in_quiet_hours(
        datetime(2026, 5, 9, 23, 0, tzinfo=ZoneInfo("UTC")),
        None,
        None,
        "UTC",
    )


async def test_in_quiet_hours_handles_garbage_input() -> None:
    assert not in_quiet_hours(
        datetime(2026, 5, 9, 23, 0, tzinfo=ZoneInfo("UTC")),
        "not a time",
        "07:00",
        "UTC",
    )


async def test_safe_repr_strips_bot_token_and_bearer() -> None:
    exc = RuntimeError("Bearer ya29.secret-token broke")
    out = _safe_repr(exc)
    assert "Bearer ya29.secret-token" not in out
    assert "<redacted>" in out


async def test_safe_repr_strips_telegram_path_token() -> None:
    exc = RuntimeError(
        "https://api.telegram.org/bot1234567890:ABCDEFXYZ_secret/sendMessage failed"
    )
    out = _safe_repr(exc)
    assert "1234567890:ABCDEFXYZ_secret" not in out
    assert "<redacted>" in out


async def test_safe_repr_strips_discord_webhook_url() -> None:
    exc = RuntimeError(
        "Bad call to https://discord.com/api/webhooks/123/AbCdEfGhIjK"
    )
    out = _safe_repr(exc)
    assert "AbCdEfGhIjK" not in out
    assert "<redacted>" in out


# ── Dispatcher pipeline ──────────────────────────────────────────────


async def test_publisher_unblocks_when_backend_blocks(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    """The bus subscriber returns immediately. With a backend that
    blocks indefinitely, ``bus.publish`` MUST resolve quickly because
    the queue absorbs the job."""
    svc, bus, _notifications, _users = setup_service
    blocking = svc._backends["blocking-backend"]
    assert isinstance(blocking, _BlockingBackend)
    await _create_route(
        sqlite_storage,
        route_id="r_block",
        user_id="u_block",
        backend_name="blocking-backend",
    )
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await _publish_notification(bus, user_id="u_block", notification_id="n_b")
    elapsed = loop.time() - t0
    assert elapsed < 0.5, f"publisher took {elapsed:.2f}s — should return immediately"
    # Worker is now stuck in send(); make sure it actually entered.
    await asyncio.wait_for(blocking.entered.wait(), timeout=1.0)
    blocking.unblock.set()


async def test_queue_overflow_drops_with_warning(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With queue_max=1, the second publish drops the job and WARNs."""
    svc, bus, _notifications, _users = setup_service
    # Replace the queue with a 1-slot one and pause workers.
    blocking = svc._backends["blocking-backend"]
    assert isinstance(blocking, _BlockingBackend)
    await _create_route(
        sqlite_storage,
        route_id="r_o",
        user_id="u_o",
        backend_name="blocking-backend",
    )
    # Stop existing workers, swap queue, restart.
    for w in svc._workers:
        w.cancel()
    await asyncio.gather(*svc._workers, return_exceptions=True)
    svc._workers = []
    svc._queue = asyncio.Queue(maxsize=1)
    svc._queue_max = 1
    # Single worker — guaranteed busy after the first job.
    svc._workers = [asyncio.create_task(svc._worker_loop())]
    # First publish — worker grabs it and blocks in send().
    await _publish_notification(bus, user_id="u_o", notification_id="n_o1")
    await asyncio.wait_for(blocking.entered.wait(), timeout=1.0)
    # Second publish — queue still empty (worker already pulled), so this fills it.
    await _publish_notification(bus, user_id="u_o", notification_id="n_o2")
    # Third publish — queue has 1, capacity 1 → overflow.
    import logging

    with caplog.at_level(logging.WARNING):
        await _publish_notification(bus, user_id="u_o", notification_id="n_o3")
    assert any(
        "fan-out queue full" in r.message for r in caplog.records
    ), "expected queue-full WARN"
    blocking.unblock.set()


_test_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_test_var", default=""
)


async def test_contextvars_propagate_through_workers(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    rec = svc._backends["recording-backend"]
    assert isinstance(rec, _RecordingBackend)
    rec.observed_contextvar = _test_var
    await _create_route(
        sqlite_storage,
        route_id="r_ctx",
        user_id="u_ctx",
        backend_name="recording-backend",
    )
    # Set the sentinel and publish under that context.
    _test_var.set("alice-sentinel")
    await _publish_notification(bus, user_id="u_ctx", notification_id="n_ctx")
    await _wait_until(lambda: rec.calls)
    assert rec.contextvar_observed[-1] == "alice-sentinel"


async def test_contextvars_dont_leak_across_concurrent_users(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    rec = svc._backends["recording-backend"]
    assert isinstance(rec, _RecordingBackend)
    rec.observed_contextvar = _test_var
    await _create_route(
        sqlite_storage,
        route_id="r_a",
        user_id="u_a",
        backend_name="recording-backend",
    )
    await _create_route(
        sqlite_storage,
        route_id="r_b",
        user_id="u_b",
        backend_name="recording-backend",
    )

    async def publish_with_var(value: str, user_id: str, notif_id: str) -> None:
        ctx = contextvars.copy_context()

        async def runner() -> None:
            _test_var.set(value)
            await _publish_notification(
                bus, user_id=user_id, notification_id=notif_id
            )

        await asyncio.Task(runner(), context=ctx)

    await asyncio.gather(
        publish_with_var("alice", "u_a", "n_a"),
        publish_with_var("bob", "u_b", "n_b"),
    )
    await _wait_until(lambda: len(rec.calls) >= 2)
    # Each call's observed sentinel must match the user it was for.
    by_user: dict[str, str] = {}
    for (dest, _msg), observed in zip(rec.calls, rec.contextvar_observed, strict=True):
        by_user[dest.user_id] = observed
    assert by_user["u_a"] == "alice"
    assert by_user["u_b"] == "bob"


# ── Filter logic ─────────────────────────────────────────────────────


async def test_urgency_floor_filter(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    rec = svc._backends["recording-backend"]
    assert isinstance(rec, _RecordingBackend)
    await _create_route(
        sqlite_storage,
        route_id="r_uf",
        user_id="u_uf",
        backend_name="recording-backend",
        urgency_floor="urgent",
    )
    # NORMAL — should be filtered out.
    await _publish_notification(
        bus, user_id="u_uf", urgency=NotificationUrgency.NORMAL, notification_id="n1"
    )
    await asyncio.sleep(0.1)
    assert len(rec.calls) == 0
    # URGENT — should pass.
    await _publish_notification(
        bus, user_id="u_uf", urgency=NotificationUrgency.URGENT, notification_id="n2"
    )
    await _wait_until(lambda: len(rec.calls) == 1)


async def test_source_allow_and_deny(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    rec = svc._backends["recording-backend"]
    assert isinstance(rec, _RecordingBackend)
    await _create_route(
        sqlite_storage,
        route_id="r_src",
        user_id="u_src",
        backend_name="recording-backend",
        source_allow=["agent"],
        source_deny=["scheduler"],
    )
    # source=inbox → not in allow → drop
    await _publish_notification(
        bus, user_id="u_src", source="inbox", notification_id="n1"
    )
    # source=scheduler → explicitly denied → drop
    await _publish_notification(
        bus, user_id="u_src", source="scheduler", notification_id="n2"
    )
    # source=agent → in allow → pass
    await _publish_notification(
        bus, user_id="u_src", source="agent", notification_id="n3"
    )
    await _wait_until(lambda: len(rec.calls) >= 1)
    await asyncio.sleep(0.1)  # let any spurious calls land
    assert len(rec.calls) == 1
    assert rec.calls[0][1].source == "agent"


# ── Retry / escalation ───────────────────────────────────────────────


async def test_transient_then_delivered_retries(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    retry = svc._backends["retry-backend"]
    assert isinstance(retry, _RetryBackend)
    retry.results = [
        PushDeliveryResult(status=PushDeliveryStatus.TRANSIENT_ERROR, message="boom"),
        PushDeliveryResult(status=PushDeliveryStatus.TRANSIENT_ERROR, message="boom"),
        PushDeliveryResult(status=PushDeliveryStatus.DELIVERED, message="HTTP 200"),
    ]
    await _create_route(
        sqlite_storage,
        route_id="r_retry",
        user_id="u_r",
        backend_name="retry-backend",
    )
    await _publish_notification(bus, user_id="u_r", notification_id="n_retry")
    await _wait_until(lambda: retry.calls >= 3, timeout=3.0)
    assert retry.calls == 3


async def test_rejected_does_not_retry(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    retry = svc._backends["retry-backend"]
    assert isinstance(retry, _RetryBackend)
    retry.results = [
        PushDeliveryResult(status=PushDeliveryStatus.REJECTED, message="HTTP 401"),
    ]
    await _create_route(
        sqlite_storage,
        route_id="r_rej",
        user_id="u_rej",
        backend_name="retry-backend",
    )
    await _publish_notification(bus, user_id="u_rej", notification_id="n_rej")
    await asyncio.sleep(0.2)
    assert retry.calls == 1


async def test_urgent_exhaustion_emits_push_failure_notification(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, notifications, _users = setup_service
    retry = svc._backends["retry-backend"]
    assert isinstance(retry, _RetryBackend)
    svc._max_retries = 1
    retry.results = [
        PushDeliveryResult(status=PushDeliveryStatus.TRANSIENT_ERROR, message="503"),
    ]
    await _create_route(
        sqlite_storage,
        route_id="r_urg",
        user_id="u_urg",
        backend_name="retry-backend",
    )
    await _publish_notification(
        bus,
        user_id="u_urg",
        urgency=NotificationUrgency.URGENT,
        notification_id="n_urg",
    )
    await _wait_until(lambda: notifications.calls, timeout=3.0)
    call = notifications.calls[0]
    assert call["source"] == "push_failure"
    assert call["urgency"] is NotificationUrgency.URGENT
    assert call["user_id"] == "u_urg"


async def test_max_retries_capped_at_constant() -> None:
    # The cap is enforced inside _deliver_with_retry via min(...).
    assert MAX_RETRIES_CAP == 8


async def test_raise_in_send_logs_and_returns(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc, bus, _notifications, _users = setup_service
    await _create_route(
        sqlite_storage,
        route_id="r_raise",
        user_id="u_raise",
        backend_name="raising-backend",
    )
    import logging

    with caplog.at_level(logging.ERROR):
        await _publish_notification(bus, user_id="u_raise", notification_id="n_raise")
        await asyncio.sleep(0.2)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "secret-token" not in text
    assert "<redacted>" in text


# ── Multi-user isolation ────────────────────────────────────────────


async def test_two_users_dont_see_each_others_routes(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, bus, _notifications, _users = setup_service
    rec = svc._backends["recording-backend"]
    assert isinstance(rec, _RecordingBackend)
    await _create_route(
        sqlite_storage,
        route_id="r_alice",
        user_id="u_alice",
        backend_name="recording-backend",
    )
    await _create_route(
        sqlite_storage,
        route_id="r_bob",
        user_id="u_bob",
        backend_name="recording-backend",
    )
    await asyncio.gather(
        _publish_notification(bus, user_id="u_alice", notification_id="n_a"),
        _publish_notification(bus, user_id="u_bob", notification_id="n_b"),
    )
    await _wait_until(lambda: len(rec.calls) >= 2)
    user_ids = {dest.user_id for dest, _msg in rec.calls}
    assert user_ids == {"u_alice", "u_bob"}
    # Each route was hit once and only once.
    route_ids = [dest.route_id for dest, _msg in rec.calls]
    assert sorted(route_ids) == ["r_alice", "r_bob"]


# ── WS RPCs (owner-scoping helper) ───────────────────────────────────


async def test_ws_routes_create_owner_scope(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
) -> None:
    svc, _bus, _notifications, _users = setup_service
    conn = _FakeConn("u_alice")
    result = await svc._ws_routes_create(
        conn,
        {
            "id": "1",
            "backend_name": "recording-backend",
            "label": "Phone",
            "destination_data": {"endpoint": "x"},
        },
    )
    assert result["ok"]
    route = result["route"]
    assert route["user_id"] == "u_alice"


async def test_ws_routes_update_rejects_other_user(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, _bus, _notifications, _users = setup_service
    await _create_route(
        sqlite_storage,
        route_id="r_alice",
        user_id="u_alice",
        backend_name="recording-backend",
    )
    bob = _FakeConn("u_bob")
    result = await svc._ws_routes_update(
        bob,
        {"id": "1", "route_id": "r_alice", "label": "Hijack"},
    )
    assert not result["ok"]
    assert result["error"] == "not_owner"


async def test_ws_routes_update_admin_cannot_mutate_other_user(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, _bus, _notifications, _users = setup_service
    await _create_route(
        sqlite_storage,
        route_id="r_alice",
        user_id="u_alice",
        backend_name="recording-backend",
    )
    admin = _FakeConn("u_admin", admin=True)
    result = await svc._ws_routes_update(
        admin,
        {"id": "1", "route_id": "r_alice", "label": "Hijack"},
    )
    assert not result["ok"]
    assert result["error"] == "not_owner"


async def test_ws_routes_list_admin_can_read_other_user(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, _bus, _notifications, _users = setup_service
    await _create_route(
        sqlite_storage,
        route_id="r_alice",
        user_id="u_alice",
        backend_name="recording-backend",
    )
    admin = _FakeConn("u_admin", admin=True)
    result = await svc._ws_routes_list(
        admin, {"id": "1", "user_id": "u_alice"}
    )
    assert result["ok"]
    assert len(result["routes"]) == 1


async def test_ws_routes_test_debounces(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, _bus, _notifications, _users = setup_service
    svc._test_debounce_s = 30.0
    await _create_route(
        sqlite_storage,
        route_id="r_t",
        user_id="u_t",
        backend_name="recording-backend",
    )
    conn = _FakeConn("u_t")
    first = await svc._ws_routes_test(conn, {"id": "1", "route_id": "r_t"})
    assert first["ok"]
    second = await svc._ws_routes_test(conn, {"id": "2", "route_id": "r_t"})
    assert not second["ok"]
    assert second["status"] == "debounced"


async def test_ws_backends_list_includes_destination_params(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
) -> None:
    svc, _bus, _notifications, _users = setup_service
    conn = _FakeConn("u_x")
    result = await svc._ws_backends_list(conn, {"id": "1"})
    assert result["ok"]
    by_name = {b["name"]: b for b in result["backends"]}
    assert "recording-backend" in by_name
    params = by_name["recording-backend"]["destination_params"]
    assert any(p["key"] == "endpoint" for p in params)


# ── AI tools ────────────────────────────────────────────────────────


async def test_ai_tool_list_when_empty(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
) -> None:
    svc, *_ = setup_service
    out = await svc.execute_tool(
        "list_my_notification_routes", {"_user_id": "u_empty"}
    )
    assert isinstance(out, str)
    assert "no push-notification routes" in out


async def test_ai_tool_create_then_list(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
) -> None:
    svc, *_ = setup_service
    create = await svc.execute_tool(
        "create_notification_route",
        {
            "_user_id": "u_c",
            "backend_name": "recording-backend",
            "label": "Phone",
            "destination": {"endpoint": "x"},
        },
    )
    assert "Created route" in str(create)
    listed = await svc.execute_tool(
        "list_my_notification_routes", {"_user_id": "u_c"}
    )
    assert "Phone" in str(listed)


async def test_ai_tool_delete_returns_confirm_uiblock_then_deletes(
    setup_service: tuple[
        PushNotificationService, InMemoryEventBus, _FakeNotificationsProvider, _FakeUserBackend,
    ],
    sqlite_storage: StorageBackend,
) -> None:
    svc, *_ = setup_service
    await _create_route(
        sqlite_storage,
        route_id="r_del",
        user_id="u_del",
        backend_name="recording-backend",
    )
    preview = await svc.execute_tool(
        "delete_notification_route",
        {"_user_id": "u_del", "route_id": "r_del"},
    )
    assert isinstance(preview, ToolOutput)
    assert preview.ui_blocks
    assert preview.ui_blocks[0].tool_name == "delete_notification_route"
    confirmed = await svc.execute_tool(
        "delete_notification_route",
        {"_user_id": "u_del", "route_id": "r_del", "confirm": True},
    )
    assert "Deleted route" in str(confirmed)


# ── Subscribed-on-start sanity check ────────────────────────────────


async def test_unsubscribes_on_stop(sqlite_storage: StorageBackend) -> None:
    bus = InMemoryEventBus()
    svc = PushNotificationService()
    svc._worker_count = 1
    svc._test_debounce_s = 0.0
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
        }
    )
    await svc.start(resolver)
    assert "notification.received" in bus._subscribers
    assert len(bus._subscribers["notification.received"]) == 1
    await svc.stop()
    # After stop, the subscriber must be removed.
    handlers = bus._subscribers.get("notification.received") or []
    assert len(handlers) == 0


# ── End-to-end: real NotificationService + push fan-out ─────────────


async def test_end_to_end_with_real_notification_service(
    sqlite_storage: StorageBackend,
) -> None:
    """notify_user → bus event → push fan-out → backend.send.

    Also asserts the publisher unblocks before the backend completes
    its (delayed) send: the bus subscriber returns immediately while
    the worker handles delivery in the background.
    """
    bus = InMemoryEventBus()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
        }
    )
    notif_svc = NotificationService()
    push_svc = PushNotificationService()
    push_svc._worker_count = 1
    push_svc._test_debounce_s = 0.0
    push_svc._retry_initial_delay_s = 0.01
    await notif_svc.start(resolver)
    await push_svc.start(resolver)
    try:
        await _create_route(
            sqlite_storage,
            route_id="r_e2e",
            user_id="u_e2e",
            backend_name="recording-backend",
        )
        rec = push_svc._backends["recording-backend"]
        assert isinstance(rec, _RecordingBackend)
        rec.delay = 0.5  # backend "takes time"

        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await notif_svc.notify_user(
            user_id="u_e2e",
            message="hello e2e",
            urgency=NotificationUrgency.NORMAL,
            source="agent",
        )
        elapsed = loop.time() - t0
        assert elapsed < 0.2, f"notify_user took {elapsed:.2f}s — should not block"
        await _wait_until(lambda: rec.calls, timeout=2.0)
        dest, msg = rec.calls[0]
        assert dest.user_id == "u_e2e"
        assert msg.body == "hello e2e"
        assert msg.source == "agent"
    finally:
        await push_svc.stop()
        await notif_svc.stop()

