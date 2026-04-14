"""Tests for UserService — user CRUD, root user, and protections."""

from typing import Any

import pytest

from gilbert.core.services.users import _ROOT_USER_ID, UserService
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import NamespacedStorageBackend, StorageBackend

# --- Stub resolver ---


class StubStorageService(Service):
    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend
        self.raw_backend = backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="storage",
            capabilities=frozenset({"entity_storage"}),
        )

    def create_namespaced(self, namespace: str) -> Any:
        return NamespacedStorageBackend(self.backend, namespace)


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
async def storage(sqlite_storage: Any) -> Any:
    return sqlite_storage


@pytest.fixture
async def user_service(storage: Any) -> UserService:
    svc = UserService(root_password_hash="hashed_pw", default_roles=["user"])
    resolver = StubResolver({"entity_storage": StubStorageService(storage)})
    await svc.start(resolver)
    return svc


# --- Tests ---


async def test_root_user_created_on_start(user_service: UserService) -> None:
    root = await user_service.get_user(_ROOT_USER_ID)
    assert root is not None
    assert root["username"] == "root"
    assert root["is_root"] is True
    assert "admin" in root["roles"]
    assert root["password_hash"] == "hashed_pw"


async def test_root_user_not_duplicated(storage: Any) -> None:
    """Starting twice should not fail or duplicate the root user."""
    svc1 = UserService(root_password_hash="hash1", default_roles=["user"])
    resolver = StubResolver({"entity_storage": StubStorageService(storage)})
    await svc1.start(resolver)

    svc2 = UserService(root_password_hash="hash2", default_roles=["user"])
    await svc2.start(resolver)

    root = await svc2.get_user(_ROOT_USER_ID)
    assert root is not None
    assert root["password_hash"] == "hash2"  # Updated


async def test_create_user_applies_default_roles(user_service: UserService) -> None:
    user = await user_service.create_user("u1", {"email": "a@b.com", "display_name": "A"})
    assert "user" in user["roles"]


async def test_delete_root_rejected(user_service: UserService) -> None:
    with pytest.raises(ValueError, match="root"):
        await user_service.delete_user(_ROOT_USER_ID)


async def test_add_provider_link_to_root_rejected(user_service: UserService) -> None:
    with pytest.raises(ValueError, match="root"):
        await user_service.add_provider_link(_ROOT_USER_ID, "google", "123")


async def test_create_and_get_user(user_service: UserService) -> None:
    await user_service.create_user("u1", {"email": "test@example.com", "display_name": "Test"})
    user = await user_service.get_user("u1")
    assert user is not None
    assert user["email"] == "test@example.com"


async def test_get_user_by_email(user_service: UserService) -> None:
    await user_service.create_user("u1", {"email": "test@example.com", "display_name": "Test"})
    user = await user_service.get_user_by_email("test@example.com")
    assert user is not None
    assert user["_id"] == "u1"


async def test_get_user_by_email_not_found(user_service: UserService) -> None:
    user = await user_service.get_user_by_email("nobody@example.com")
    assert user is None


async def test_tool_list_users_strips_password(user_service: UserService) -> None:
    result = await user_service.execute_tool("list_users", {})
    import json

    users = json.loads(result)
    for u in users:
        assert "password_hash" not in u


# --- WS handler tests ---


class _FakeConn:
    """Minimal stand-in for a WsConnection."""
    pass


async def test_ws_create_user(user_service: UserService) -> None:
    frame = {"id": "1", "username": "alice", "password": "secret123", "display_name": "Alice", "email": "alice@example.com"}
    result = await user_service._ws_user_create(_FakeConn(), frame)
    assert result is not None
    assert result["status"] == "ok"
    assert result["user"]["username"] == "alice"
    assert "password_hash" not in result["user"]

    # User should exist in the backend
    user = await user_service.get_user(result["user"]["_id"])
    assert user is not None
    assert user["email"] == "alice@example.com"


async def test_ws_create_user_duplicate_username(user_service: UserService) -> None:
    frame = {"id": "1", "username": "bob", "password": "secret123", "display_name": "Bob"}
    await user_service._ws_user_create(_FakeConn(), frame)

    frame2 = {"id": "2", "username": "bob", "password": "other", "display_name": "Bob2"}
    result = await user_service._ws_user_create(_FakeConn(), frame2)
    assert result is not None
    assert result["type"] == "gilbert.error"
    assert result["code"] == 409


async def test_ws_create_user_disabled(storage: Any) -> None:
    svc = UserService(root_password_hash="hashed_pw", default_roles=["user"], allow_user_creation=False)
    resolver = StubResolver({"entity_storage": StubStorageService(storage)})
    await svc.start(resolver)

    frame = {"id": "1", "username": "alice", "password": "secret123"}
    result = await svc._ws_user_create(_FakeConn(), frame)
    assert result is not None
    assert result["type"] == "gilbert.error"
    assert result["code"] == 403


async def test_ws_create_user_missing_fields(user_service: UserService) -> None:
    # Missing username
    result = await user_service._ws_user_create(_FakeConn(), {"id": "1", "password": "x"})
    assert result is not None
    assert result["code"] == 400

    # Missing password
    result = await user_service._ws_user_create(_FakeConn(), {"id": "2", "username": "test"})
    assert result is not None
    assert result["code"] == 400


async def test_ws_delete_user(user_service: UserService) -> None:
    await user_service.create_user("u_del", {"username": "todelete", "email": "", "display_name": "Del"})
    result = await user_service._ws_user_delete(_FakeConn(), {"id": "1", "user_id": "u_del"})
    assert result is not None
    assert result["status"] == "ok"

    user = await user_service.get_user("u_del")
    assert user is None


async def test_ws_delete_root_rejected(user_service: UserService) -> None:
    result = await user_service._ws_user_delete(_FakeConn(), {"id": "1", "user_id": "root"})
    assert result is not None
    assert result["type"] == "gilbert.error"
    assert result["code"] == 403
