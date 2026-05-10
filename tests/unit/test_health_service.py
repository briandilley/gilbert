"""Tests for ``HealthService`` — ingestion, ACL seeding, cascade,
multi-user isolation, and the auth.user.deleted subscription.

DB tests use a real test SQLite database per CLAUDE.md — the storage
layer is the boundary, mocking it would defeat the test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.health import (
    _ACL_COLLECTION,
    _AUDIT_COLLECTION,
    _LINKS_COLLECTION,
    _METRICS_COLLECTION,
    _ROLES_COLLECTION,
    _SUMMARIES_COLLECTION,
    HealthService,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event
from gilbert.interfaces.health import (
    HEALTH_ADMIN_ROLE,
    MetricType,
)
from gilbert.interfaces.notifications import (
    Notification,
    NotificationUrgency,
)
from gilbert.interfaces.storage import Filter, FilterOp, Query
from gilbert.storage.sqlite import SQLiteStorage
from tests.unit._fakes.health import FakeHealthBackend, make_metric

# ── Resolver / fakes ────────────────────────────────────────────────


class _FakeStorageProvider:
    def __init__(self, backend: SQLiteStorage) -> None:
        self.backend = backend
        self.raw_backend = backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend


class _FakeEventBusProvider:
    def __init__(self, bus: InMemoryEventBus | None = None) -> None:
        self.bus = bus or InMemoryEventBus()


class _FakeSchedulerProvider:
    def __init__(self) -> None:
        self.added_jobs: list[str] = []
        self.removed_jobs: list[str] = []

    def add_job(self, *args: Any, **kwargs: Any) -> Any:
        name = kwargs.get("name", args[0] if args else "")
        self.added_jobs.append(name)

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.removed_jobs.append(name)

    def enable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def disable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_jobs(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def get_job(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def run_now(self, *args: Any, **kwargs: Any) -> None:
        pass


class _RecordingNotifications:
    """Real NotificationProvider satisfying the runtime_checkable protocol."""

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
            id="n1",
            user_id=user_id,
            source=source,
            message=message,
            urgency=urgency,
            created_at=datetime.now(UTC),
            source_ref=source_ref,
        )


def _resolver(**caps: Any) -> Any:
    class _Resolver:
        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def get_all(self, name: str) -> list[Any]:
            return []

    return _Resolver()


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
async def started_service(sqlite_storage: SQLiteStorage) -> Any:
    """Boot a HealthService against a real SQLite DB + a fake bus."""
    bus = InMemoryEventBus()
    scheduler = _FakeSchedulerProvider()
    notifications = _RecordingNotifications()
    svc = HealthService()
    resolver = _resolver(
        entity_storage=_FakeStorageProvider(sqlite_storage),
        event_bus=_FakeEventBusProvider(bus),
        scheduler=scheduler,
        notifications=notifications,
    )
    await svc.start(resolver)
    yield {
        "svc": svc,
        "bus": bus,
        "storage": sqlite_storage,
        "scheduler": scheduler,
        "notifications": notifications,
    }
    await svc.stop()


# ── ACL seeding ─────────────────────────────────────────────────────


async def test_acl_seeded_for_each_collection(started_service: Any) -> None:
    storage: SQLiteStorage = started_service["storage"]
    expected = {
        _METRICS_COLLECTION: HEALTH_ADMIN_ROLE,
        _LINKS_COLLECTION: HEALTH_ADMIN_ROLE,
        _SUMMARIES_COLLECTION: HEALTH_ADMIN_ROLE,
        _AUDIT_COLLECTION: HEALTH_ADMIN_ROLE,
        "health_oauth_state": "admin",
    }
    for collection, read_role in expected.items():
        row = await storage.get(_ACL_COLLECTION, collection)
        assert row is not None, f"ACL row missing for {collection}"
        assert row["read_role"] == read_role
        assert row["write_role"] == "admin"


async def test_health_admin_role_seeded_at_level_zero(started_service: Any) -> None:
    storage: SQLiteStorage = started_service["storage"]
    row = await storage.get(_ROLES_COLLECTION, HEALTH_ADMIN_ROLE)
    assert row is not None
    assert row["level"] == 0


async def test_health_admin_role_not_granted_to_anyone(
    started_service: Any,
) -> None:
    """Operators must grant the role explicitly via /roles/users."""
    storage: SQLiteStorage = started_service["storage"]
    # Walk the users collection (if any) and check none carry the role
    # from the seeded state. The fixture creates no users so this is
    # a smoke check that seeding doesn't auto-grant.
    rows = await storage.query(Query(collection="users"))
    for u in rows:
        roles = u.get("roles") or []
        assert HEALTH_ADMIN_ROLE not in roles


# ── Ingestion ───────────────────────────────────────────────────────


async def test_ingest_persists_and_publishes_event(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    bus: InMemoryEventBus = started_service["bus"]
    received: list[Event] = []

    async def _handler(evt: Event) -> None:
        received.append(evt)

    bus.subscribe("health.metric.received", _handler)

    metrics = [make_metric(user_id="alice", source_event_id="evt-1")]
    n = await svc.ingest_metrics("alice", "_fake_health", metrics)
    assert n == 1
    # One row persisted.
    storage: SQLiteStorage = started_service["storage"]
    rows = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert len(rows) == 1
    assert len(received) == 1


async def test_ingest_dedup_skips_event_publish(started_service: Any) -> None:
    """Replay-flood absorbs without amplification — the second ingest
    of the same source_event_id replaces the row but DOES NOT emit a
    second ``health.metric.received`` event."""
    svc: HealthService = started_service["svc"]
    bus: InMemoryEventBus = started_service["bus"]
    received: list[Event] = []

    async def _handler(evt: Event) -> None:
        received.append(evt)

    bus.subscribe("health.metric.received", _handler)

    metric = make_metric(user_id="alice", source_event_id="evt-dup")
    await svc.ingest_metrics("alice", "_fake_health", [metric])
    await svc.ingest_metrics("alice", "_fake_health", [metric])

    storage: SQLiteStorage = started_service["storage"]
    rows = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    # Last-write-wins on the dedup key — exactly one row at any time.
    assert len(rows) == 1
    # Exactly one event for the first newly-persisted insert.
    assert len(received) == 1


async def test_ingest_dedup_fallback_on_user_backend_type_recorded_at(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    when = datetime(2026, 5, 9, 7, 0, tzinfo=UTC)
    m1 = make_metric(user_id="alice", recorded_at=when, source_event_id="")
    m2 = make_metric(user_id="alice", recorded_at=when, source_event_id="")
    await svc.ingest_metrics("alice", "_fake_health", [m1])
    await svc.ingest_metrics("alice", "_fake_health", [m2])
    rows = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert len(rows) == 1


async def test_per_user_write_cap_drops_overflow(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    svc._per_user_daily_write_cap = 3  # tighten for the test
    metrics = [
        make_metric(
            user_id="alice",
            source_event_id=f"evt-{i}",
            recorded_at=datetime(2026, 5, 9, 7, i, tzinfo=UTC),
        )
        for i in range(10)
    ]
    n = await svc.ingest_metrics("alice", "_fake_health", metrics)
    assert n == 3


async def test_ingest_owner_filter_on_read(started_service: Any) -> None:
    """A user's tools never see another user's metrics — the read API
    filters by user_id BEFORE returning anything."""
    svc: HealthService = started_service["svc"]
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [make_metric(user_id="alice", source_event_id="ev-a")],
    )
    await svc.ingest_metrics(
        "bob",
        "_fake_health",
        [make_metric(user_id="bob", source_event_id="ev-b")],
    )

    set_current_user(
        UserContext(user_id="alice", email="a@b", display_name="alice")
    )
    rows = await svc.read_metrics(
        "alice",
        [],
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) + timedelta(hours=1),
    )
    assert all(r.user_id == "alice" for r in rows)


async def test_read_metrics_rejects_other_users(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    set_current_user(
        UserContext(user_id="alice", email="a@b", display_name="alice")
    )
    with pytest.raises(PermissionError):
        await svc.read_metrics(
            "bob",
            [],
            datetime.now(UTC) - timedelta(hours=1),
            datetime.now(UTC) + timedelta(hours=1),
        )


async def test_health_admin_can_cross_read(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    await svc.ingest_metrics(
        "bob",
        "_fake_health",
        [make_metric(user_id="bob", source_event_id="ev-b")],
    )
    set_current_user(
        UserContext(
            user_id="alice",
            email="a@b",
            display_name="alice",
            roles=frozenset({HEALTH_ADMIN_ROLE}),
        )
    )
    # Direct read goes through can_read_metrics and is permitted.
    rows = await svc.read_metrics(
        "bob",
        [],
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) + timedelta(hours=1),
    )
    assert any(r.user_id == "bob" for r in rows)


async def test_admin_read_metrics_audits_and_notifies(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    notifications: _RecordingNotifications = started_service["notifications"]
    await svc.ingest_metrics(
        "bob",
        "_fake_health",
        [make_metric(user_id="bob", source_event_id="ev-b1")],
    )
    actor = UserContext(
        user_id="alice",
        email="a@b",
        display_name="alice",
        roles=frozenset({HEALTH_ADMIN_ROLE}),
    )
    await svc.admin_read_metrics(
        actor,
        "bob",
        [MetricType.STEPS],
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) + timedelta(hours=1),
    )
    storage: SQLiteStorage = started_service["storage"]
    audit_rows = await storage.query(
        Query(
            collection=_AUDIT_COLLECTION,
            filters=[
                Filter(field="target_user_id", op=FilterOp.EQ, value="bob"),
            ],
        )
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor_user_id"] == "alice"
    assert audit_rows[0]["kind"] == "cross_user_read"
    # NotificationProvider was called for the target user.
    assert any(c["user_id"] == "bob" for c in notifications.calls)


async def test_admin_read_metrics_without_role_rejected(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    actor = UserContext(
        user_id="alice",
        email="a@b",
        display_name="alice",
        # No HEALTH_ADMIN_ROLE — even ``admin`` alone is not enough.
        roles=frozenset({"admin"}),
    )
    with pytest.raises(PermissionError):
        await svc.admin_read_metrics(
            actor,
            "bob",
            [MetricType.STEPS],
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(hours=1),
        )


# ── Cascade on auth.user.deleted ────────────────────────────────────


async def test_auth_user_deleted_cascades(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    bus: InMemoryEventBus = started_service["bus"]
    storage: SQLiteStorage = started_service["storage"]

    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [make_metric(user_id="alice", source_event_id="ev-a")],
    )
    await svc.ingest_metrics(
        "bob",
        "_fake_health",
        [make_metric(user_id="bob", source_event_id="ev-b")],
    )
    # Cascade fires when the bus publishes auth.user.deleted.
    received_deletes: list[Event] = []

    async def _on_deleted(evt: Event) -> None:
        received_deletes.append(evt)

    bus.subscribe("health.metric.deleted", _on_deleted)
    await bus.publish(
        Event(
            event_type="auth.user.deleted",
            data={"user_id": "bob", "deleted_at": datetime.now(UTC).isoformat()},
            source="auth",
        )
    )

    rows = await storage.query(Query(collection=_METRICS_COLLECTION))
    assert all(r["user_id"] == "alice" for r in rows)
    assert len(received_deletes) == 1
    assert received_deletes[0].data["scope"] == "user-deleted"


# ── Multi-user isolation (per spec §16.4) ───────────────────────────


async def test_concurrent_ingest_no_cross_user_leak(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]

    async def _ingest_for(user_id: str, source_event_id: str) -> None:
        metric = make_metric(user_id=user_id, source_event_id=source_event_id)
        await svc.ingest_metrics(user_id, "_fake_health", [metric])

    await asyncio.gather(
        _ingest_for("alice", "ev-a"),
        _ingest_for("bob", "ev-b"),
    )
    rows_alice = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    rows_bob = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="bob")],
        )
    )
    assert len(rows_alice) == 1
    assert rows_alice[0]["user_id"] == "alice"
    assert len(rows_bob) == 1
    assert rows_bob[0]["user_id"] == "bob"


# ── Right-to-delete ─────────────────────────────────────────────────


async def test_preview_delete_all_returns_counts(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [
            make_metric(user_id="alice", source_event_id=f"ev-{i}",
                        recorded_at=datetime(2026, 5, 9, 7, i, tzinfo=UTC))
            for i in range(3)
        ],
    )
    preview = await svc.preview_delete_all("alice")
    assert preview["metric_count"] == 3
    assert preview["backends"] == ["_fake_health"]


async def test_delete_all_my_data_cascades_and_disconnects(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    fake_backend = svc._backends.get("_fake_health")
    assert isinstance(fake_backend, FakeHealthBackend)

    # Persist a link row + metric.
    await storage.put(
        _LINKS_COLLECTION,
        "alice/_fake_health",
        {
            "_id": "alice/_fake_health",
            "user_id": "alice",
            "backend_name": "_fake_health",
            "enabled": True,
        },
    )
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [make_metric(user_id="alice", source_event_id="ev-1")],
    )

    result = await svc.delete_all_my_data("alice")
    assert result["deleted_metrics"] == 1
    assert "_fake_health" in result["disconnected_backends"]
    assert "alice" in fake_backend.disconnect_calls

    # Audit row survives the cascade.
    audit_rows = await storage.query(
        Query(
            collection=_AUDIT_COLLECTION,
            filters=[Filter(field="target_user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert any(r["kind"] == "self_delete_all" for r in audit_rows)


async def test_delete_all_logs_warn_on_disconnect_failure(
    started_service: Any,
) -> None:
    """Local cleanup proceeds even if the upstream revoke fails."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    fake_backend = svc._backends["_fake_health"]
    assert isinstance(fake_backend, FakeHealthBackend)
    fake_backend.disconnect_raises = RuntimeError("upstream-down")

    await storage.put(
        _LINKS_COLLECTION,
        "alice/_fake_health",
        {
            "_id": "alice/_fake_health",
            "user_id": "alice",
            "backend_name": "_fake_health",
            "enabled": True,
        },
    )
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [make_metric(user_id="alice", source_event_id="ev-1")],
    )

    result = await svc.delete_all_my_data("alice")
    assert result["deleted_metrics"] == 1
    assert "_fake_health" in result["upstream_revoke_failures"]
    # Local link row gone.
    rows = await storage.query(
        Query(
            collection=_LINKS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert rows == []


# ── Webhook dispatch ────────────────────────────────────────────────


async def test_ingest_webhook_unknown_token_returns_not_found(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    result = await svc.ingest_webhook(
        token="nope",
        body=b"{}",
        headers={},
        remote_addr="1.2.3.4",
    )
    assert result.status == "not_found"


async def test_ingest_webhook_disabled_collapses_to_not_found(
    started_service: Any,
) -> None:
    """Disabled tokens collapse to 404 to defeat enumeration (§7.7)."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    import hashlib

    raw_token = "tok-1234"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await storage.put(
        _LINKS_COLLECTION,
        "alice/_fake_health",
        {
            "_id": "alice/_fake_health",
            "user_id": "alice",
            "backend_name": "_fake_health",
            "enabled": False,
            "webhook_token_hash": token_hash,
        },
    )
    result = await svc.ingest_webhook(
        token=raw_token,
        body=b"[]",
        headers={},
        remote_addr="1.2.3.4",
    )
    assert result.status == "not_found"


async def test_ingest_webhook_oversize_body_413(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    svc._webhook_max_body_bytes = 16
    result = await svc.ingest_webhook(
        token="anything",
        body=b"x" * 64,
        headers={},
        remote_addr="1.2.3.4",
    )
    assert result.status == "payload_too_large"


async def test_ingest_webhook_happy_path(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    fake_backend = svc._backends["_fake_health"]
    assert isinstance(fake_backend, FakeHealthBackend)
    fake_backend.parse_webhook_returns = [make_metric(user_id="alice", source_event_id="evt-1")]

    import hashlib

    raw_token = "tok-happy"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await storage.put(
        _LINKS_COLLECTION,
        "alice/_fake_health",
        {
            "_id": "alice/_fake_health",
            "user_id": "alice",
            "backend_name": "_fake_health",
            "enabled": True,
            "webhook_token_hash": token_hash,
        },
    )
    result = await svc.ingest_webhook(
        token=raw_token,
        body=b'[{"type":"steps","value":1,"unit":"count","recorded_at":"2026-05-09T07:00:00+00:00"}]',
        headers={},
        remote_addr="1.2.3.4",
    )
    assert result.status == "ok"
    assert result.received == 1


# ── Tool: health_delete_my_data — preview/confirm UIBlock ───────────


async def test_health_delete_my_data_preview_returns_uiblock(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    out = await svc.execute_tool(
        "health_delete_my_data",
        {"_user_id": "alice"},
    )
    # Without confirm=DELETE the helper returns a ToolOutput with a
    # UI block — the model cannot one-shot the delete.
    assert hasattr(out, "ui_blocks")
    assert len(out.ui_blocks) == 1


async def test_health_delete_my_data_with_confirm_deletes(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [make_metric(user_id="alice", source_event_id="ev-1")],
    )
    out = await svc.execute_tool(
        "health_delete_my_data",
        {"_user_id": "alice", "confirm": "DELETE"},
    )
    assert isinstance(out, str)
    rows = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert rows == []


# ── Tool surface invariants ────────────────────────────────────────


def test_tool_surface_includes_nine_tools_and_no_slash_for_delete() -> None:
    svc = HealthService()
    svc._enabled = True
    tools = svc.get_tools()
    by_name = {t.name: t for t in tools}
    assert {
        "health_now",
        "latest_health",
        "health_summary",
        "health_trend",
        "sleep_last_night",
        "steps_today",
        "weight_trend",
        "health_links",
        "health_delete_my_data",
    } == set(by_name)

    delete_tool = by_name["health_delete_my_data"]
    assert delete_tool.slash_command is None  # NO slash command


def test_tool_surface_user_id_never_a_parameter() -> None:
    """No tool accepts a ``user_id`` argument from the model — they
    read the injected ``_user_id`` from arguments."""
    svc = HealthService()
    svc._enabled = True
    for tool in svc.get_tools():
        names = {p.name for p in tool.parameters}
        assert "user_id" not in names, f"{tool.name} accepts user_id"


async def test_tool_missing_user_id_rejected() -> None:
    svc = HealthService()
    with pytest.raises(PermissionError):
        await svc.execute_tool("steps_today", {})

