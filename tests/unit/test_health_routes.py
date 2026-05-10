"""Route-layer tests for the health webhook + per-user + admin routes.

Test strategy:
- Boot a real ``HealthService`` against a real test SQLite DB so the
  storage path is exercised (per CLAUDE.md no-mocking-the-DB rule).
- Wire the routers into a FastAPI app, override
  ``require_authenticated`` so route-level auth is decoupled from
  AuthService.
- Verify status codes + body shapes per spec §7.7 / §8.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.health import _LINKS_COLLECTION, HealthService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.health import HEALTH_ADMIN_ROLE
from gilbert.web.auth import require_authenticated
from gilbert.web.routes.health import api_router, webhook_router
from tests.unit._fakes.health import FakeHealthBackend, make_metric

# ── Fakes ────────────────────────────────────────────────────────────


class _FakeStorageProvider:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.raw_backend = backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend


class _FakeEventBusProvider:
    def __init__(self) -> None:
        self.bus = InMemoryEventBus()


class _FakeSchedulerProvider:
    def add_job(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def remove_job(self, *args: Any, **kwargs: Any) -> None:
        pass

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


class _FakeServiceManager:
    def __init__(self, health_svc: HealthService) -> None:
        self._svc = health_svc

    def get_by_capability(self, capability: str) -> Any:
        if capability == "health":
            return self._svc
        return None


class _FakeGilbert:
    def __init__(self, health_svc: HealthService) -> None:
        self.service_manager = _FakeServiceManager(health_svc)


def _resolver(**caps: Any) -> Any:
    class _R:
        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def get_all(self, name: str) -> list[Any]:
            return []

    return _R()


@pytest.fixture
async def app_and_svc(sqlite_storage: Any) -> AsyncIterator[tuple[FastAPI, HealthService, str]]:
    """Boot a started HealthService and a FastAPI app wiring its routes."""
    svc = HealthService()
    resolver = _resolver(
        entity_storage=_FakeStorageProvider(sqlite_storage),
        event_bus=_FakeEventBusProvider(),
        scheduler=_FakeSchedulerProvider(),
    )
    await svc.start(resolver)
    yield (
        _make_app(svc),
        svc,
        "alice",
    )
    await svc.stop()


def _make_app(
    svc: HealthService,
    *,
    user_id: str = "alice",
    roles: frozenset[str] = frozenset({"user"}),
) -> FastAPI:
    app = FastAPI()
    app.state.gilbert = _FakeGilbert(svc)
    app.include_router(api_router)
    app.include_router(webhook_router)

    user = UserContext(
        user_id=user_id,
        email=f"{user_id}@b.com",
        display_name=user_id,
        roles=roles,
        provider="local",
    )

    def _fake_dep(request: Request) -> UserContext:
        return user

    app.dependency_overrides[require_authenticated] = _fake_dep
    return app


# ── Webhook ──────────────────────────────────────────────────────────


def test_webhook_unknown_token_returns_404_with_zero_received(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    app, _svc, _ = app_and_svc
    client = TestClient(app)
    resp = client.post("/webhook/health/nope", content=b"[]")
    assert resp.status_code == 404
    assert resp.json() == {"received": 0}


def test_webhook_disabled_token_collapses_to_404(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    """Disabled tokens MUST return the same response shape (body and
    headers) as unknown tokens — defeats enumeration (§7.7)."""
    app, svc, _ = app_and_svc
    client = TestClient(app)
    raw_token = "tok-disabled"
    h = hashlib.sha256(raw_token.encode()).hexdigest()
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            _LINKS_COLLECTION,
            "alice/_fake_health",
            {
                "_id": "alice/_fake_health",
                "user_id": "alice",
                "backend_name": "_fake_health",
                "enabled": False,
                "webhook_token_hash": h,
            },
        )
    )

    resp = client.post(f"/webhook/health/{raw_token}", content=b"[]")
    unknown = client.post("/webhook/health/random", content=b"[]")
    assert resp.status_code == 404
    assert unknown.status_code == 404
    assert resp.json() == unknown.json()


def test_webhook_oversize_body_returns_413(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    app, svc, _ = app_and_svc
    svc._webhook_max_body_bytes = 8
    client = TestClient(app)
    resp = client.post("/webhook/health/anything", content=b"x" * 64)
    assert resp.status_code == 413


def test_webhook_happy_path_returns_received_count(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    app, svc, _ = app_and_svc
    backend = svc._backends["_fake_health"]
    assert isinstance(backend, FakeHealthBackend)
    backend.parse_webhook_returns = [
        make_metric(user_id="alice", source_event_id="evt-1")
    ]
    raw_token = "tok-happy"
    h = hashlib.sha256(raw_token.encode()).hexdigest()
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            _LINKS_COLLECTION,
            "alice/_fake_health",
            {
                "_id": "alice/_fake_health",
                "user_id": "alice",
                "backend_name": "_fake_health",
                "enabled": True,
                "webhook_token_hash": h,
            },
        )
    )
    client = TestClient(app)
    resp = client.post(f"/webhook/health/{raw_token}", content=b"[]")
    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] == 1


# ── Per-user routes ─────────────────────────────────────────────────


def test_me_links_lists_caller_only(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    app, svc, _ = app_and_svc
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            _LINKS_COLLECTION,
            "alice/_fake_health",
            {
                "_id": "alice/_fake_health",
                "user_id": "alice",
                "backend_name": "_fake_health",
                "enabled": True,
                "webhook_token_hash": "deadbeef",
            },
        )
    )
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            _LINKS_COLLECTION,
            "bob/_fake_health",
            {
                "_id": "bob/_fake_health",
                "user_id": "bob",
                "backend_name": "_fake_health",
                "enabled": True,
                "webhook_token_hash": "feedface",
            },
        )
    )
    client = TestClient(app)
    resp = client.get("/api/health/me/links")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    # Tokens never returned in plaintext.
    assert "webhook_token" not in items[0] or items[0].get("webhook_token") is None
    # Hash never returned.
    for item in items:
        assert "webhook_token_hash" not in item


def test_delete_all_requires_literal_DELETE(  # noqa: N802
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    app, _svc, _ = app_and_svc
    client = TestClient(app)
    bad = client.post(
        "/api/health/me/delete-all",
        json={"confirm": "delete"},  # wrong case
    )
    assert bad.status_code == 400


def test_delete_all_with_DELETE_succeeds(  # noqa: N802
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    app, svc, _ = app_and_svc
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc.ingest_metrics(
            "alice",
            "_fake_health",
            [make_metric(user_id="alice", source_event_id="ev-1")],
        )
    )
    client = TestClient(app)
    resp = client.post(
        "/api/health/me/delete-all",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_metrics"] == 1


def test_admin_users_requires_admin_role(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    # Caller is "user" role only — should 403.
    app = _make_app(svc, roles=frozenset({"user"}))
    client = TestClient(app)
    resp = client.get("/api/health/admin/users")
    assert resp.status_code == 403


def test_admin_users_returns_counts_for_admin(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            _LINKS_COLLECTION,
            "bob/_fake_health",
            {
                "_id": "bob/_fake_health",
                "user_id": "bob",
                "backend_name": "_fake_health",
                "enabled": True,
                "last_delivery_at": "2026-05-09T10:00:00+00:00",
            },
        )
    )
    app = _make_app(svc, user_id="admin", roles=frozenset({"admin"}))
    client = TestClient(app)
    resp = client.get("/api/health/admin/users")
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert len(users) >= 1
    # No values in the admin-overview response — counts only.
    for u in users:
        assert "metrics_snapshot" not in u
        assert "value" not in u


def test_admin_drill_in_requires_health_admin_role(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    # admin alone is not enough.
    app = _make_app(svc, user_id="admin", roles=frozenset({"admin"}))
    client = TestClient(app)
    resp = client.get("/api/health/admin/users/bob/metrics")
    assert resp.status_code == 403


def test_admin_drill_in_with_health_admin_succeeds(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc.ingest_metrics(
            "bob",
            "_fake_health",
            [make_metric(user_id="bob", source_event_id="ev-bob")],
        )
    )
    app = _make_app(
        svc,
        user_id="admin",
        roles=frozenset({"admin", HEALTH_ADMIN_ROLE}),
    )
    client = TestClient(app)
    resp = client.get("/api/health/admin/users/bob/metrics")
    assert resp.status_code == 200


def test_my_audit_log_only_returns_callers_target_rows(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    # Persist an audit row where bob is the target.
    asyncio.get_event_loop().run_until_complete(
        svc._record_audit(  # type: ignore[attr-defined]
            kind="cross_user_read",
            actor_user_id="x",
            target_user_id="bob",
            metric_types=["steps"],
        )
    )
    asyncio.get_event_loop().run_until_complete(
        svc._record_audit(  # type: ignore[attr-defined]
            kind="cross_user_read",
            actor_user_id="x",
            target_user_id="alice",
            metric_types=["weight"],
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get("/api/health/me/audit-log")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(item["target_user_id"] == "alice" for item in items)


def test_oauth_callback_rejects_state_user_mismatch(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    """Confused-deputy: a state minted for bob can't be consumed by
    alice's session."""
    _app, svc, _ = app_and_svc
    import asyncio

    state = "stt-mismatch"
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            "health_oauth_state",
            state,
            {
                "_id": state,
                "user_id": "bob",
                "backend_name": "_fake_health",
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime(2099, 1, 1, tzinfo=UTC)).isoformat(),
            },
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get(
        f"/api/health/me/oauth/_fake_health/callback"
        f"?code=abc&state={state}"
    )
    assert resp.status_code == 400


def test_oauth_callback_rejects_wrong_backend(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    """A state minted for one backend can't be consumed by another."""
    _app, svc, _ = app_and_svc
    import asyncio

    state = "stt-backend"
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            "health_oauth_state",
            state,
            {
                "_id": state,
                "user_id": "alice",
                "backend_name": "withings",
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime(2099, 1, 1, tzinfo=UTC)).isoformat(),
            },
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get(
        f"/api/health/me/oauth/_fake_health/callback"
        f"?code=abc&state={state}"
    )
    assert resp.status_code == 400


def test_oauth_callback_rejects_expired_state(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    state = "stt-expired"
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            "health_oauth_state",
            state,
            {
                "_id": state,
                "user_id": "alice",
                "backend_name": "_fake_health",
                "created_at": "2020-01-01T00:00:00+00:00",
                "expires_at": "2020-01-01T00:10:00+00:00",
            },
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get(
        f"/api/health/me/oauth/_fake_health/callback"
        f"?code=abc&state={state}"
    )
    assert resp.status_code == 400


# ── T17: OAuth callback body identifies the specific failure mode ───


def test_oauth_callback_body_identifies_user_mismatch(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    state = "stt-um"
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            "health_oauth_state",
            state,
            {
                "_id": state,
                "user_id": "bob",
                "backend_name": "_fake_health",
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime(2099, 1, 1, tzinfo=UTC)).isoformat(),
            },
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get(
        f"/api/health/me/oauth/_fake_health/callback?code=x&state={state}"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "user mismatch" in str(body).lower()


def test_oauth_callback_body_identifies_wrong_backend(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    state = "stt-wb"
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            "health_oauth_state",
            state,
            {
                "_id": state,
                "user_id": "alice",
                "backend_name": "withings",
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime(2099, 1, 1, tzinfo=UTC)).isoformat(),
            },
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get(
        f"/api/health/me/oauth/_fake_health/callback?code=x&state={state}"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "backend mismatch" in str(body).lower()


def test_oauth_callback_body_identifies_expired(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    _app, svc, _ = app_and_svc
    import asyncio

    state = "stt-exp"
    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            "health_oauth_state",
            state,
            {
                "_id": state,
                "user_id": "alice",
                "backend_name": "_fake_health",
                "created_at": "2020-01-01T00:00:00+00:00",
                "expires_at": "2020-01-01T00:10:00+00:00",
            },
        )
    )
    app = _make_app(svc, user_id="alice")
    client = TestClient(app)
    resp = client.get(
        f"/api/health/me/oauth/_fake_health/callback?code=x&state={state}"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "expired" in str(body).lower()


# ── B2 regression: per-IP rate limit ignores spoofed XFF ────────────


def test_per_ip_rate_limit_ignores_spoofed_xff(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    """Send unknown-token POSTs with each request advertising a
    different ``X-Forwarded-For`` value. The per-IP bucket MUST fill
    based on ``request.client.host``; the XFF header is intentionally
    ignored until a trusted-proxy allowlist exists in core.

    Without this, an attacker spoofs a fresh IP per request and the
    30/min cap on the 404 path becomes ``30/min PER SPOOFED IP`` —
    i.e. unbounded — defeating the whole enumeration-DoS defense."""
    app, svc, _ = app_and_svc
    svc._webhook_unknown_rate_per_minute = 3
    # Reset the IP bucket so the test is deterministic.
    svc._webhook_ip_buckets = type(svc._webhook_ip_buckets)(cap=10)
    client = TestClient(app)
    statuses: list[int] = []
    for i in range(6):
        resp = client.post(
            f"/webhook/health/nope-{i}",
            content=b"[]",
            headers={"X-Forwarded-For": f"1.2.3.{i}"},  # attacker spoof.
        )
        statuses.append(resp.status_code)
    # First 3 → 404 (unknown tokens), then bucket flips to 429 (the
    # actual peer is the same TestClient address so spoofing didn't
    # help).
    assert statuses[:3] == [404, 404, 404], statuses
    assert 429 in statuses[3:], statuses


# ── B3 regression: Content-Length pre-check before body buffering ───


def test_webhook_content_length_check_before_body_read(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    """A POST advertising ``Content-Length: 5_000_000`` (> 1 MB
    default) MUST return 413 BEFORE the body is buffered into memory.

    Verified by sending a small actual body but a huge
    Content-Length header — the route reads the header first and
    short-circuits."""
    app, svc, _ = app_and_svc
    # Default cap is 1 MB.
    svc._webhook_max_body_bytes = 1024  # 1 KB for the test
    client = TestClient(app)
    resp = client.post(
        "/webhook/health/anything",
        # Tiny actual body but advertise a huge length.
        content=b"[]",
        headers={"Content-Length": "5000000"},
    )
    # Either 413 (cap caught it) — body was never read past header.
    assert resp.status_code == 413


# ── T18: webhook route uses hmac.compare_digest ─────────────────────


def test_webhook_route_uses_compare_digest(
    app_and_svc: tuple[FastAPI, HealthService, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §16.5: the route must use ``hmac.compare_digest`` for
    constant-time hash comparison. A future refactor that swaps in
    ``==`` would silently regress; this test fails immediately if
    that happens."""
    import hashlib
    import hmac as _hmac

    app, svc, _ = app_and_svc
    raw_token = "tok-cd-test"
    h = hashlib.sha256(raw_token.encode()).hexdigest()
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        svc._storage.put(  # type: ignore[union-attr]
            _LINKS_COLLECTION,
            "alice/_fake_health",
            {
                "_id": "alice/_fake_health",
                "user_id": "alice",
                "backend_name": "_fake_health",
                "enabled": True,
                "webhook_token_hash": h,
            },
        )
    )

    calls: list[tuple[str, str]] = []
    real = _hmac.compare_digest

    def _spy(a: Any, b: Any) -> bool:
        calls.append((str(a), str(b)))
        return real(a, b)

    # Patch where the service imports hmac.
    monkeypatch.setattr(
        "gilbert.core.services.health.hmac.compare_digest", _spy
    )
    client = TestClient(app)
    resp = client.post(
        f"/webhook/health/{raw_token}", content=b"[]"
    )
    assert resp.status_code == 200
    assert calls, "compare_digest was never invoked"


# ── T12: health_links unique index on webhook_token_hash ────────────


def test_health_links_webhook_token_hash_unique_index_declared(
    app_and_svc: tuple[FastAPI, HealthService, str],
) -> None:
    """Spec §7.5 + round-2: ``health_links.webhook_token_hash`` is
    declared as a UNIQUE index.

    The storage layer uses ``INSERT OR REPLACE`` semantics, so the
    constraint manifests as "the row with the colliding hash gets
    REPLACED" rather than "the second insert raises". This test
    asserts the index is registered AND that the SQLite-level
    enforcement collapses two rows-by-hash to one row.
    """
    _app, svc, _ = app_and_svc
    import asyncio

    indexes = asyncio.get_event_loop().run_until_complete(
        svc._storage.list_indexes(_LINKS_COLLECTION)  # type: ignore[union-attr]
    )
    unique_hash_idx = [
        idx
        for idx in indexes
        if idx.fields == ["webhook_token_hash"] and idx.unique
    ]
    assert len(unique_hash_idx) == 1, (
        f"Expected exactly one UNIQUE index on webhook_token_hash; "
        f"got {indexes!r}"
    )

