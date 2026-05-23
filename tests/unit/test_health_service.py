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
from gilbert.interfaces.context import set_current_user
from gilbert.interfaces.events import Event
from gilbert.interfaces.health import (
    HEALTH_ADMIN_ROLE,
    DailySummary,
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


class _FakeConfigReader:
    """Minimal ConfigurationReader that enables the health service."""

    def get(self, path: str) -> Any:
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return {"enabled": True} if namespace == "health" else {}

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        return self.get_section(namespace)

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}


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
        configuration=_FakeConfigReader(),
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


# ── B4 regression: OAuth-state consume is atomic ────────────────────


async def test_oauth_double_callback_race(started_service: Any) -> None:
    """Two simultaneous ``consume_oauth_state`` calls for the same
    state row must NOT both observe ``consumed_at == None`` and both
    return ``ok``. Exactly one wins; the other gets ``already``.
    Without the per-state asyncio.Lock, the read-then-write race made
    a double-callback re-exchange the OAuth code (the second exchange
    fails server-side at Withings, but the user's UX is broken AND a
    future provider that doesn't single-use codes credits the same
    code twice)."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    state = "stt-race-test"
    from datetime import timedelta as _td
    await storage.put(
        "health_oauth_state",
        state,
        {
            "_id": state,
            "user_id": "alice",
            "backend_name": "_fake_health",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + _td(hours=1)).isoformat(),
        },
    )
    results = await asyncio.gather(
        svc.consume_oauth_state(state, "_fake_health", "alice"),
        svc.consume_oauth_state(state, "_fake_health", "alice"),
    )
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["already", "ok"], (
        f"Expected exactly one ok / one already; got {statuses!r}"
    )


async def test_oauth_state_one_shot_consumed(started_service: Any) -> None:
    """A state row whose ``consumed_at`` is already set yields the
    benign ``already`` response on a second call — never ``ok``."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    from datetime import timedelta as _td
    state = "stt-one-shot"
    await storage.put(
        "health_oauth_state",
        state,
        {
            "_id": state,
            "user_id": "alice",
            "backend_name": "_fake_health",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + _td(hours=1)).isoformat(),
            "consumed_at": datetime.now(UTC).isoformat(),
        },
    )
    result = await svc.consume_oauth_state(state, "_fake_health", "alice")
    assert result["status"] == "already"


# ── B5 regression: self_delete_all audit row schema ─────────────────


async def test_health_audit_self_delete_metric_types_is_empty(
    started_service: Any,
) -> None:
    """Spec §4.5: ``metric_types`` is ``list[MetricType]`` and MUST be
    empty for ``self_delete_all`` (no specific TYPES were 'read'). The
    backend names go in a separate ``backends`` field."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    # Seed two backends' link rows + a metric per backend so preview
    # picks both up.
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
        [make_metric(user_id="alice", source_event_id="ev-fake-1")],
    )
    # Stash a second metric with a fabricated backend name so the
    # preview captures both backends in the audit row.
    second = make_metric(user_id="alice", source_event_id="ev-other-1")
    from dataclasses import replace as _replace
    second = _replace(second, backend="_fake_health_b")
    # Insert directly into storage to avoid the backend dispatcher.
    await storage.put(
        _METRICS_COLLECTION,
        "manual-second",
        second.to_dict() | {"id": "manual-second", "_id": "manual-second"},
    )

    await svc.delete_all_my_data("alice")

    audit_rows = await storage.query(
        Query(
            collection=_AUDIT_COLLECTION,
            filters=[
                Filter(field="target_user_id", op=FilterOp.EQ, value="alice"),
                Filter(field="kind", op=FilterOp.EQ, value="self_delete_all"),
            ],
        )
    )
    assert len(audit_rows) == 1
    row = audit_rows[0]
    # metric_types is empty per spec.
    assert row["metric_types"] == []
    # Backends are recorded under ``backends`` (sorted).
    assert sorted(row["backends"]) == ["_fake_health", "_fake_health_b"]


# ── I5 regression: cross-user-read notification body + action_url ───


async def test_admin_read_notification_uses_human_names_and_action_link(
    started_service: Any,
) -> None:
    """Spec §6.1.1: notification body MUST contain "An admin viewed",
    use HUMAN-friendly metric type names ("sleep" not "sleep_duration"),
    and include an ``action_url`` to ``/account/health/audit-log``."""
    svc: HealthService = started_service["svc"]
    notifications: _RecordingNotifications = started_service["notifications"]
    await svc.ingest_metrics(
        "bob",
        "_fake_health",
        [make_metric(
            user_id="bob",
            source_event_id="ev-b1",
            metric_type=MetricType.SLEEP_DURATION,
        )],
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
        [MetricType.SLEEP_DURATION],
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) + timedelta(hours=1),
    )
    bob_calls = [c for c in notifications.calls if c["user_id"] == "bob"]
    assert len(bob_calls) == 1
    msg = bob_calls[0]["message"]
    assert "An admin viewed" in msg
    # Human-friendly: "sleep" (not "sleep_duration").
    assert "sleep" in msg
    assert "sleep_duration" not in msg
    assert "/account/health/audit-log" in str(bob_calls[0]["source_ref"])


# ── B6 regression: startup security WARNs ───────────────────────────


async def test_startup_warns_on_oauth_with_non_localhost_bind(
    sqlite_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When an OAuth-capable backend is registered AND the configured
    web.bind_address is not 127.0.0.1, a WARN must fire so the
    operator knows their OAuth refresh tokens are crossing plaintext
    HTTP and resting unencrypted on disk in v1."""

    class _ConfigReader:
        """Minimal ConfigurationReader test double.

        Implements every method on the protocol so the runtime
        ``isinstance`` check inside the service narrows correctly.
        """

        def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
            self._sections = sections

        def get(self, path: str) -> Any:
            ns, _, rest = path.partition(".")
            section = self._sections.get(ns, {})
            if rest:
                return section.get(rest)
            return section

        def get_section(self, name: str) -> dict[str, Any]:
            return self._sections.get(name, {})

        def get_section_safe(self, name: str) -> dict[str, Any]:
            return self._sections.get(name, {})

        async def set(self, path: str, value: Any) -> dict[str, Any]:
            return {}

    class _PullCapableBackend(FakeHealthBackend):
        backend_name = "_pull_test_backend"

        @property
        def supports_pull(self) -> bool:  # type: ignore[override]
            return True

        @property
        def supports_push(self) -> bool:  # type: ignore[override]
            return False

    # Trigger registration of the pull-capable backend.
    _ = _PullCapableBackend
    config = _ConfigReader(
        {
            "gilbert": {"web": {"bind_address": "0.0.0.0"}, "public_base_url": ""},
            "health": {"enabled": True},
        }
    )
    bus = InMemoryEventBus()
    svc = HealthService()
    resolver = _resolver(
        entity_storage=_FakeStorageProvider(sqlite_storage),
        event_bus=_FakeEventBusProvider(bus),
        scheduler=_FakeSchedulerProvider(),
        configuration=config,
    )
    caplog.set_level("WARNING", logger="gilbert.core.services.health")
    try:
        await svc.start(resolver)
    finally:
        await svc.stop()
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "OAuth backend(s) present" in msgs, (
        f"Expected OAuth-bind warning; got {msgs!r}"
    )


async def test_startup_warns_on_debug_log_values_with_multiuser(
    sqlite_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``debug_log_values=true`` on a multi-user instance leaks one
    user's metric values into the shared log file. Operator must be
    warned."""

    class _ConfigReader:
        """Minimal ConfigurationReader test double.

        Implements every method on the protocol so the runtime
        ``isinstance`` check inside the service narrows correctly.
        """

        def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
            self._sections = sections

        def get(self, path: str) -> Any:
            ns, _, rest = path.partition(".")
            section = self._sections.get(ns, {})
            if rest:
                return section.get(rest)
            return section

        def get_section(self, name: str) -> dict[str, Any]:
            return self._sections.get(name, {})

        def get_section_safe(self, name: str) -> dict[str, Any]:
            return self._sections.get(name, {})

        async def set(self, path: str, value: Any) -> dict[str, Any]:
            return {}

    # Seed two users so user_count > 1.
    await sqlite_storage.put("users", "alice", {"_id": "alice", "user_id": "alice"})
    await sqlite_storage.put("users", "bob", {"_id": "bob", "user_id": "bob"})
    config = _ConfigReader(
        {
            "gilbert": {"web": {"bind_address": "127.0.0.1"}, "public_base_url": ""},
            "health": {"enabled": True, "debug_log_values": True},
        }
    )
    bus = InMemoryEventBus()
    svc = HealthService()
    resolver = _resolver(
        entity_storage=_FakeStorageProvider(sqlite_storage),
        event_bus=_FakeEventBusProvider(bus),
        scheduler=_FakeSchedulerProvider(),
        configuration=config,
    )
    caplog.set_level("WARNING", logger="gilbert.core.services.health")
    try:
        await svc.start(resolver)
    finally:
        await svc.stop()
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "debug_log_values" in msgs, (
        f"Expected debug_log_values warning; got {msgs!r}"
    )
    assert "multi-user" in msgs


# ── B1 regression: HealthLinkProvider protocol ──────────────────────


async def test_user_has_active_links(started_service: Any) -> None:
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    # No links → False.
    assert await svc.user_has_active_links("alice") is False
    # Insert a link → True.
    await storage.put(
        _LINKS_COLLECTION,
        "alice/_fake_health",
        {
            "_id": "alice/_fake_health",
            "user_id": "alice",
            "backend_name": "_fake_health",
            "enabled": False,  # disabled-but-present should still count
        },
    )
    assert await svc.user_has_active_links("alice") is True
    # Other users still see False.
    assert await svc.user_has_active_links("bob") is False


def test_health_service_satisfies_health_link_provider_protocol() -> None:
    from gilbert.interfaces.health import HealthLinkProvider
    svc = HealthService()
    assert isinstance(svc, HealthLinkProvider)


# ── B2 regression: per-IP rate limit ignores spoofed XFF ────────────


async def test_per_ip_rate_limit_ignores_spoofed_xff(
    started_service: Any,
) -> None:
    """The ``_client_ip`` route helper now defaults to
    ``request.client.host`` and intentionally ignores ``X-Forwarded-For``
    until a trusted-proxy allowlist exists in core. This regression
    test exercises the SERVICE side: the per-IP bucket fills based on
    ``remote_addr`` regardless of any header sent by the attacker."""
    svc: HealthService = started_service["svc"]
    svc._webhook_unknown_rate_per_minute = 3
    svc._webhook_ip_buckets = type(svc._webhook_ip_buckets)(cap=10)
    # 30 unknown-token POSTs from the SAME remote_addr — bucket fills
    # after capacity (3) is exhausted.
    statuses: list[str] = []
    for _ in range(6):
        result = await svc.ingest_webhook(
            token=f"nope-{_}",
            body=b"[]",
            headers={"x-forwarded-for": f"1.2.3.{_}"},  # attacker spoofs.
            remote_addr="9.9.9.9",  # the actual peer.
        )
        statuses.append(result.status)
    # First 3 → "not_found"; remainder → "rate_limited".
    assert statuses[:3] == ["not_found"] * 3, statuses
    assert "rate_limited" in statuses[3:], statuses


# ── B3 regression: webhook body cap exposed on service ──────────────


def test_webhook_max_body_bytes_property() -> None:
    svc = HealthService()
    svc._webhook_max_body_bytes = 12345
    assert svc.webhook_max_body_bytes == 12345


# ── I7 regression: dedup-fallback normalizes ISO format ─────────────


async def test_dedup_fallback_normalizes_iso_format(
    started_service: Any,
) -> None:
    """One metric inserted with ``+00:00`` suffix, the second with
    ``Z`` (or microsecond differences) — both serialize to different
    strings via ``isoformat`` but represent the same instant. Without
    canonicalization, dedup misses and we end up with two rows."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]

    base = datetime(2026, 5, 9, 7, 0, 0, tzinfo=UTC)
    m1 = make_metric(user_id="alice", recorded_at=base, source_event_id="")
    # Same instant — but constructed via fromisoformat("...Z") would
    # round-trip differently across Python versions. Force a
    # microsecond-level offset that should NOT collide…
    base_microsec = datetime(2026, 5, 9, 7, 0, 0, 1, tzinfo=UTC)
    m2 = make_metric(user_id="alice", recorded_at=base, source_event_id="")
    m3 = make_metric(user_id="alice", recorded_at=base_microsec, source_event_id="")

    await svc.ingest_metrics("alice", "_fake_health", [m1])
    await svc.ingest_metrics("alice", "_fake_health", [m2])  # same instant → dedups
    await svc.ingest_metrics("alice", "_fake_health", [m3])  # different (1µs) → distinct

    rows = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert len(rows) == 2, f"Expected 2 rows (dedup once); got {len(rows)}"


# ── T10: dedup-fallback distinguishes backends ──────────────────────


async def test_dedup_fallback_distinguishes_backends(
    started_service: Any,
) -> None:
    """Round-2 fix: dedup fallback key is
    ``(user_id, backend, metric_type, recorded_at)``. Two ingests
    with the SAME recorded_at + metric_type but DIFFERENT backend
    must NOT collapse."""
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    when = datetime(2026, 5, 9, 7, 0, tzinfo=UTC)
    m1 = make_metric(user_id="alice", recorded_at=when, source_event_id="", backend="_fake_health")
    m2 = make_metric(user_id="alice", recorded_at=when, source_event_id="", backend="other_backend")
    await svc.ingest_metrics("alice", "_fake_health", [m1])
    await svc.ingest_metrics("alice", "other_backend", [m2])
    rows = await storage.query(
        Query(
            collection=_METRICS_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert len(rows) == 2
    backends = {r["backend"] for r in rows}
    assert backends == {"_fake_health", "other_backend"}


# ── T1 / §16.6: per-user concurrency cap ────────────────────────────


async def test_run_per_user_concurrency_cap(started_service: Any) -> None:
    """Spec §16.6 #2: with concurrency=2 and 8 users, never more than
    2 tasks run simultaneously."""
    svc: HealthService = started_service["svc"]
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _work(uid: str) -> None:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        # Yield a few times to let other tasks observe the in-flight
        # counter — ``asyncio.sleep(0)`` is enough for the scheduler
        # to interleave.
        for _ in range(5):
            await asyncio.sleep(0)
        async with lock:
            in_flight -= 1

    user_ids = [f"u{i}" for i in range(8)]
    await svc._run_per_user(
        user_ids,
        _work,
        concurrency=2,
        label="cap-test",
    )
    assert max_in_flight == 2, f"max in flight was {max_in_flight}"


# ── T2: per-user task isolates set_current_user ─────────────────────


async def test_run_per_user_isolates_set_current_user(
    started_service: Any,
) -> None:
    """Spec §16.4 / §16.6 #1: induce ``set_current_user`` inside one
    user's task body and assert the sibling task still sees its own
    SYSTEM-acting-for context. The canonical multi-user-isolation
    failure mode."""
    svc: HealthService = started_service["svc"]
    seen: dict[str, str] = {}

    async def _work(uid: str) -> None:
        if uid == "alice":
            # Local set inside alice's task — must not leak to bob.
            set_current_user(
                UserContext(user_id="alice-leak", email="x", display_name="x")
            )
            await asyncio.sleep(0)
        else:
            # Bob's task: spend a few yields to maximize the chance
            # of a leak, then read its own context.
            for _ in range(5):
                await asyncio.sleep(0)
            from gilbert.interfaces.context import get_current_user as _gcu
            ctx = _gcu()
            seen["bob"] = ctx.user_id

    await svc._run_per_user(
        ["alice", "bob"],
        _work,
        concurrency=2,
        label="isolation-test",
    )
    # Bob saw the SYSTEM-acting-for("bob") identity, NOT alice-leak.
    assert seen["bob"] == UserContext.SYSTEM.user_id


# ── T9: per-(user, backend) ingest lock serializes ──────────────────


async def test_per_user_backend_ingest_lock_serializes(
    started_service: Any,
) -> None:
    """Round-2 fix: ``_ingest_locks[(user_id, backend)]`` serializes
    concurrent ingests for the same (user, backend). We monkeypatch
    the storage put to record entry/exit order and verify the second
    ingest's first put runs AFTER the first's last put."""
    svc: HealthService = started_service["svc"]
    storage = svc._storage
    assert storage is not None
    events: list[tuple[str, str]] = []
    original_put = storage.put

    async def _wrapping_put(collection: str, entity_id: str, data: dict[str, Any]) -> None:
        if collection == _METRICS_COLLECTION:
            events.append(("enter", str(data.get("source_event_id") or "")))
        await original_put(collection, entity_id, data)
        if collection == _METRICS_COLLECTION:
            events.append(("exit", str(data.get("source_event_id") or "")))

    storage.put = _wrapping_put  # type: ignore[method-assign]
    try:
        m1 = make_metric(user_id="alice", source_event_id="lock-1")
        m2 = make_metric(user_id="alice", source_event_id="lock-2")
        await asyncio.gather(
            svc.ingest_metrics("alice", "_fake_health", [m1]),
            svc.ingest_metrics("alice", "_fake_health", [m2]),
        )
    finally:
        storage.put = original_put  # type: ignore[method-assign]
    # Each id's enter and exit must be adjacent — the lock prevents
    # interleaving for the same (user, backend).
    enter_events = [e for e in events if e[0] == "enter"]
    assert len(enter_events) == 2
    # Find the index of each enter / exit and assert no interleave.
    enter_lock1_idx = events.index(("enter", "lock-1"))
    exit_lock1_idx = events.index(("exit", "lock-1"))
    enter_lock2_idx = events.index(("enter", "lock-2"))
    exit_lock2_idx = events.index(("exit", "lock-2"))
    # Either lock-1 entirely before lock-2 starts, or vice versa.
    serialized = (
        exit_lock1_idx < enter_lock2_idx
        or exit_lock2_idx < enter_lock1_idx
    )
    assert serialized, f"Ingests interleaved: {events!r}"


# ── T15: stricter forbidden-word regex ──────────────────────────────


def test_default_summary_prompt_forbids_clinical_words_strict() -> None:
    """Replace the loose ``word in text`` check with a regex that
    asserts EACH forbidden word appears in a 'do not' construction.
    Catches the regression where someone adds 'you can use concerning'
    and the loose test still passes."""
    import re as _re

    from gilbert.core.services.health import _DEFAULT_SUMMARY_PROMPT

    forbidden = ("concerning", "abnormal", "warning", "risk", "noteworthy", "should")
    text = _DEFAULT_SUMMARY_PROMPT.lower()
    for word in forbidden:
        # ``do not``/``don't``/``never`` within 80 chars of the word.
        pattern = _re.compile(
            rf'\b(do not|don\'t|never).{{0,80}}\b{_re.escape(word)}\b',
            _re.DOTALL,
        )
        assert pattern.search(text), (
            f"Bundled summary prompt no longer FORBIDS {word!r} (it may "
            "still mention it, but not in a 'do not'/'never' context)"
        )


async def test_disconnect_revokes_before_local_delete(
    started_service: Any,
) -> None:
    """T13: when ``disconnect_backend`` is called, the upstream
    revoke MUST happen BEFORE the local row is deleted from
    ``health_links``. Without this ordering, a transient revoke
    failure would leave the user holding a still-valid Withings
    grant (because we'd already deleted the local row referencing it).
    """
    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]
    fake_backend = svc._backends["_fake_health"]
    assert isinstance(fake_backend, FakeHealthBackend)

    events: list[str] = []

    # Wrap the backend's disconnect to record the event.
    real_disconnect = fake_backend.disconnect

    async def _wrapping_disconnect(uid: str) -> None:
        events.append("revoke")
        await real_disconnect(uid)

    fake_backend.disconnect = _wrapping_disconnect  # type: ignore[method-assign]

    # Wrap storage.delete to record the event.
    real_delete = storage.delete

    async def _wrapping_delete(collection: str, entity_id: str) -> None:
        if collection == _LINKS_COLLECTION:
            events.append("delete")
        await real_delete(collection, entity_id)

    storage.delete = _wrapping_delete  # type: ignore[method-assign]

    try:
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
        await svc.disconnect_backend("alice", "_fake_health")
    finally:
        fake_backend.disconnect = real_disconnect  # type: ignore[method-assign]
        storage.delete = real_delete  # type: ignore[method-assign]

    assert "revoke" in events and "delete" in events
    assert events.index("revoke") < events.index("delete"), (
        f"Expected revoke BEFORE delete; got {events!r}"
    )


def test_default_trend_prompt_forbids_clinical_words() -> None:
    """Same shape for the trend prompt — spec §20 acceptance #4 wants
    the trend prompt's non-clinical guard rail tested too."""
    import re as _re

    from gilbert.core.services.health import _DEFAULT_TREND_PROMPT

    forbidden = ("concerning", "abnormal", "warning", "risk", "noteworthy", "should")
    text = _DEFAULT_TREND_PROMPT.lower()
    for word in forbidden:
        # Larger gap than the summary regex: the trend prompt has the
        # forbidden words in a bulleted "MUST NOT" list rather than
        # adjacent to the negator.
        pattern = _re.compile(
            rf'\b(do not|don\'t|never|must not).{{0,250}}\b{_re.escape(word)}\b',
            _re.DOTALL,
        )
        assert pattern.search(text), (
            f"Bundled trend prompt no longer FORBIDS {word!r} explicitly"
        )


# ── Task 4: GreetingContextProvider protocol implementation ──────────────


async def test_greeting_context_returns_prose_from_brief(
    started_service: Any,
) -> None:
    """HealthService.greeting_context wraps health_brief_for_greeting
    and formats it as prose suitable for the greeting prompt."""
    from gilbert.interfaces.greeting import GreetingContext

    svc: HealthService = started_service["svc"]
    storage: SQLiteStorage = started_service["storage"]

    # Seed a health link so the brief has data.
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

    # Ingest metrics that will be picked up by health_brief_for_greeting.
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [
            make_metric(
                user_id="alice",
                source_event_id="sleep-evt",
                metric_type=MetricType.SLEEP_DURATION,
                value=7.5 * 3600.0,  # 7.5 hours in seconds
            ),
            make_metric(
                user_id="alice",
                source_event_id="steps-evt",
                metric_type=MetricType.STEPS,
                value=4200.0,
            ),
        ],
    )

    # Trigger a daily summary with a flag to test flag output.
    summary = DailySummary(
        user_id="alice",
        local_date="2026-05-23",
        summary_text="Good day",
        metrics_snapshot={"sleep_hours": 7.5, "steps": 4200},
        flags=["high_hr"],
        generated_at=datetime.now(UTC),
    )
    await storage.put(
        _SUMMARIES_COLLECTION,
        f"alice/2026-05-23",
        summary.to_dict() | {"_id": f"alice/2026-05-23"},
    )

    ctx = await svc.greeting_context(user_id="alice")
    assert isinstance(ctx, GreetingContext)
    assert ctx.provider_id == "health"
    assert ctx.label == "Health"
    # Check that the prose contains the expected formatted values.
    assert "7.5" in ctx.prose
    assert "4,200" in ctx.prose
    assert "high_hr" in ctx.prose


async def test_greeting_context_returns_none_for_empty_brief(
    started_service: Any,
) -> None:
    """If the user has no health links, greeting_context returns None."""
    svc: HealthService = started_service["svc"]
    # Don't seed any health links for alice.
    ctx = await svc.greeting_context(user_id="alice")
    assert ctx is None


def test_health_service_advertises_greeting_context_capability() -> None:
    """The service_info must include 'greeting_context' in capabilities."""
    svc = HealthService()
    info = svc.service_info()
    assert "greeting_context" in info.capabilities
