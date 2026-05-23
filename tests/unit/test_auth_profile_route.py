"""HTTP integration tests for /auth/me and POST /auth/profile.

These cover the ``UserContext.tz`` precursor that landed alongside the
push-notification fan-out feature:

- ``GET /auth/me`` echoes the user's ``tz`` (``None`` for fresh users,
  populated after a successful POST).
- ``POST /auth/profile`` with a valid IANA timezone updates the user
  and returns the refreshed value.
- ``POST /auth/profile`` with an invalid IANA name returns 400.
- ``POST /auth/profile`` with ``null`` clears the field.
- ``POST /auth/profile`` without auth returns 401.

Storage is a real SQLite database via the project ``sqlite_storage``
fixture; the user backend is the real ``StorageUserBackend``. The auth
service is a small stub that exposes only ``user_has_password`` since
that's the single hook ``/auth/me`` reaches into.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from gilbert.interfaces.auth import UserContext
from gilbert.storage.sqlite import SQLiteStorage
from gilbert.storage.user_storage import StorageUserBackend
from gilbert.web.auth import get_user_context, require_authenticated
from gilbert.web.routes.auth import router as auth_router

pytestmark = pytest.mark.asyncio


# ── Test doubles ─────────────────────────────────────────────────────


class _FakeUsersService:
    """Satisfies ``isinstance(svc, UserManagementProvider)`` while
    delegating to a real ``StorageUserBackend``."""

    def __init__(self, backend: StorageUserBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> StorageUserBackend:
        return self._backend

    @property
    def allow_user_creation(self) -> bool:
        return True

    async def list_users(self) -> list[dict[str, Any]]:
        return []

    async def resolve_user_id_by_name(self, name: str) -> None:
        return None


class _FakeAuthService:
    """Stub auth service. ``/auth/me`` calls ``user_has_password`` to
    decide whether to render a Change Password form. Everything else
    is unused."""

    async def user_has_password(self, user_id: str) -> bool:
        return True


class _FakeServiceManager:
    def __init__(self, users: _FakeUsersService, auth: _FakeAuthService) -> None:
        self._users = users
        self._auth = auth

    def get_by_capability(self, capability: str) -> Any:
        if capability == "users":
            return self._users
        if capability == "authentication":
            return self._auth
        return None


class _FakeGilbert:
    def __init__(self, users: _FakeUsersService, auth: _FakeAuthService) -> None:
        self.service_manager = _FakeServiceManager(users, auth)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def user_backend(sqlite_storage: SQLiteStorage) -> StorageUserBackend:
    backend = StorageUserBackend(sqlite_storage)
    await backend.ensure_indexes()
    return backend


_ALICE = UserContext(
    user_id="u_alice",
    email="alice@example.com",
    display_name="Alice",
    roles=frozenset({"user"}),
    provider="local",
)


@pytest.fixture
async def alice(user_backend: StorageUserBackend) -> UserContext:
    """Persist a real user with no ``tz`` set. Mirrors a freshly created
    account that hasn't visited the timezone editor yet."""
    await user_backend.create_user(
        "u_alice",
        {
            "username": "alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        },
    )
    return _ALICE


@pytest.fixture
def app(user_backend: StorageUserBackend) -> FastAPI:
    app = FastAPI()
    app.state.gilbert = _FakeGilbert(
        _FakeUsersService(user_backend), _FakeAuthService()
    )
    app.include_router(auth_router)
    return app


def _override_user(app: FastAPI, user: UserContext | None) -> None:
    """Pin both ``get_user_context`` and ``require_authenticated`` to
    *user*. ``None`` simulates an unauthenticated caller — only
    ``require_authenticated`` raises 401 in that case (mirroring the
    real middleware which sets ``state.user = SYSTEM``)."""
    from fastapi import HTTPException

    def _ctx(request: Request) -> UserContext:
        return user if user is not None else UserContext.SYSTEM

    def _required(request: Request) -> UserContext:
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return user

    app.dependency_overrides[get_user_context] = _ctx
    app.dependency_overrides[require_authenticated] = _required


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


# ── /auth/me ─────────────────────────────────────────────────────────


async def test_me_returns_tz_none_for_fresh_user(
    app: FastAPI,
    alice: UserContext,
    client: httpx.AsyncClient,
) -> None:
    _override_user(app, alice)
    resp = await client.get("/auth/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "u_alice"
    assert body["tz"] is None


async def test_me_returns_tz_after_profile_update(
    app: FastAPI,
    alice: UserContext,
    client: httpx.AsyncClient,
    user_backend: StorageUserBackend,
) -> None:
    """After POST /auth/profile sets a tz, the value is persisted in
    real storage and reflected in /auth/me on the next request."""
    _override_user(app, alice)
    post = await client.post("/auth/profile", json={"tz": "America/Los_Angeles"})
    assert post.status_code == 200, post.text
    assert post.json()["tz"] == "America/Los_Angeles"

    # Round-trip via the real backend confirms it actually landed.
    fetched = await user_backend.get_user("u_alice")
    assert fetched is not None
    assert fetched["tz"] == "America/Los_Angeles"

    # /auth/me reflects the new tz when the request's UserContext mirrors
    # the freshly-rebuilt session (in production the auth middleware
    # rebuilds it on every request).
    refreshed_ctx = UserContext(
        user_id=alice.user_id,
        email=alice.email,
        display_name=alice.display_name,
        roles=alice.roles,
        provider=alice.provider,
        tz="America/Los_Angeles",
    )
    _override_user(app, refreshed_ctx)
    me = await client.get("/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["tz"] == "America/Los_Angeles"


# ── POST /auth/profile ──────────────────────────────────────────────


async def test_profile_post_valid_tz_persists(
    app: FastAPI,
    alice: UserContext,
    client: httpx.AsyncClient,
    user_backend: StorageUserBackend,
) -> None:
    _override_user(app, alice)
    resp = await client.post("/auth/profile", json={"tz": "Europe/Paris"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"user_id": "u_alice", "tz": "Europe/Paris"}

    fetched = await user_backend.get_user("u_alice")
    assert fetched is not None
    assert fetched["tz"] == "Europe/Paris"


async def test_profile_post_null_clears_tz(
    app: FastAPI,
    alice: UserContext,
    client: httpx.AsyncClient,
    user_backend: StorageUserBackend,
) -> None:
    """Posting ``{"tz": null}`` clears the field. Mirrors the SPA's
    "Clear" button on the TimezoneCard."""
    await user_backend.update_user("u_alice", {"tz": "Europe/Paris"})
    _override_user(app, alice)
    resp = await client.post("/auth/profile", json={"tz": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tz"] is None
    fetched = await user_backend.get_user("u_alice")
    assert fetched is not None
    assert fetched["tz"] is None


async def test_profile_post_invalid_tz_rejects_400(
    app: FastAPI,
    alice: UserContext,
    client: httpx.AsyncClient,
    user_backend: StorageUserBackend,
) -> None:
    _override_user(app, alice)
    resp = await client.post("/auth/profile", json={"tz": "Bogus/Zone"})
    assert resp.status_code == 400, resp.text
    # The persisted value MUST NOT have changed.
    fetched = await user_backend.get_user("u_alice")
    assert fetched is not None
    assert fetched["tz"] is None


async def test_profile_post_unauthenticated_returns_401(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    _override_user(app, None)
    resp = await client.post("/auth/profile", json={"tz": "UTC"})
    assert resp.status_code == 401
