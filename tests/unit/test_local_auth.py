"""Tests for LocalAuthBackend — password hashing and verification."""

from typing import Any

import pytest

from gilbert.integrations.local_auth import LocalAuthBackend
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.users import UserBackend

# --- Stub user backend ---


class StubUserBackend(UserBackend):
    """In-memory user backend for testing."""

    def __init__(self) -> None:
        self._users: dict[str, dict[str, Any]] = {}

    async def create_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        entity = {**data, "_id": user_id}
        self._users[user_id] = entity
        return entity

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._users.get(user_id)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        for u in self._users.values():
            if u.get("username", "").lower() == username.lower():
                return u
        return None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        for u in self._users.values():
            if u.get("email") == email:
                return u
        return None

    async def get_user_by_provider_link(
        self, provider_type: str, provider_user_id: str
    ) -> dict[str, Any] | None:
        return None

    async def update_user(self, user_id: str, data: dict[str, Any]) -> None:
        if user_id in self._users:
            self._users[user_id].update(data)

    async def delete_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)

    async def list_users(
        self, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        return list(self._users.values())

    async def add_provider_link(
        self, user_id: str, provider_type: str, provider_user_id: str
    ) -> None:
        pass

    async def remove_provider_link(self, user_id: str, provider_type: str) -> None:
        pass

    async def set_roles(self, user_id: str, roles: set[str]) -> None:
        if user_id in self._users:
            self._users[user_id]["roles"] = sorted(roles)

    async def get_roles(self, user_id: str) -> set[str]:
        u = self._users.get(user_id)
        return set(u.get("roles", [])) if u else set()

    async def put_provider_user(
        self, provider_type: str, provider_user_id: str, data: dict[str, Any]
    ) -> None:
        pass

    async def get_provider_user(
        self, provider_type: str, provider_user_id: str
    ) -> dict[str, Any] | None:
        return None

    async def list_provider_users(
        self, provider_type: str, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        return []


# --- Stub user service ---


class StubUserService(Service):
    def __init__(self, user_backend: StubUserBackend) -> None:
        self._backend = user_backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="users", capabilities=frozenset({"users"}))

    @property
    def backend(self) -> StubUserBackend:
        return self._backend


class StubResolver(ServiceResolver):
    def __init__(self, services: dict[str, Service]) -> None:
        self._by_cap = services

    def get_capability(self, capability: str) -> Service | None:
        return self._by_cap.get(capability)

    def require_capability(self, capability: str) -> Service:
        svc = self._by_cap.get(capability)
        if svc is None:
            raise LookupError(f"Missing: {capability}")
        return svc

    def get_all(self, capability: str) -> list[Service]:
        svc = self._by_cap.get(capability)
        return [svc] if svc else []


# --- Fixtures ---


@pytest.fixture
async def local_auth() -> LocalAuthBackend:
    backend = StubUserBackend()
    svc = LocalAuthBackend()
    await svc.initialize({})
    svc.set_user_backend(backend)

    # Create a test user with a hashed password.
    pw_hash = svc.hash_password("secret123")
    await backend.create_user("u1", {
        "email": "test@example.com",
        "display_name": "Test",
        "password_hash": pw_hash,
        "roles": ["user"],
    })

    return svc


# --- Tests ---


def test_provider_type() -> None:
    svc = LocalAuthBackend()
    assert svc.provider_type == "local"


async def test_authenticate_success(local_auth: LocalAuthBackend) -> None:
    info = await local_auth.authenticate({"email": "test@example.com", "password": "secret123"})
    assert info is not None
    assert info.email == "test@example.com"
    assert info.provider_type == "local"
    assert info.provider_user_id == "u1"


async def test_authenticate_wrong_password(local_auth: LocalAuthBackend) -> None:
    info = await local_auth.authenticate({"email": "test@example.com", "password": "wrong"})
    assert info is None


async def test_authenticate_unknown_email(local_auth: LocalAuthBackend) -> None:
    info = await local_auth.authenticate({"email": "nobody@example.com", "password": "secret123"})
    assert info is None


async def test_authenticate_empty_credentials(local_auth: LocalAuthBackend) -> None:
    assert await local_auth.authenticate({}) is None
    assert await local_auth.authenticate({"email": "", "password": ""}) is None


async def test_hash_password_produces_valid_hash(local_auth: LocalAuthBackend) -> None:
    h = local_auth.hash_password("mypassword")
    assert h.startswith("$argon2")
