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


async def test_empty_root_password_warns(storage: Any, caplog: Any) -> None:
    svc = UserService(root_password_hash="", default_roles=["user"])
    resolver = StubResolver({"entity_storage": StubStorageService(storage)})
    import logging

    with caplog.at_level(logging.WARNING, logger="gilbert.core.services.users"):
        await svc.start(resolver)

    assert any("no password" in r.message for r in caplog.records), (
        "expected a warning when root is created with an empty password hash"
    )


async def test_nonempty_root_password_does_not_warn(storage: Any, caplog: Any) -> None:
    svc = UserService(root_password_hash="hashed_pw", default_roles=["user"])
    resolver = StubResolver({"entity_storage": StubStorageService(storage)})
    import logging

    with caplog.at_level(logging.WARNING, logger="gilbert.core.services.users"):
        await svc.start(resolver)

    assert not any("no password" in r.message for r in caplog.records)


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


# --- resolve_user_id_by_name ---


async def test_resolve_user_id_by_name_exact_display_name(
    user_service: UserService,
) -> None:
    """Full display-name match (case-insensitive) returns 1.0 confidence."""
    await user_service.create_user(
        "u1", {"email": "alice@example.com", "display_name": "Alice Smith"}
    )
    match = await user_service.resolve_user_id_by_name("alice smith")
    assert match is not None
    assert match.user_id == "u1"
    assert match.confidence == 1.0


async def test_resolve_user_id_by_name_first_name(
    user_service: UserService,
) -> None:
    """First-name match returns 0.8 confidence."""
    await user_service.create_user(
        "u1", {"email": "alice@example.com", "display_name": "Alice Smith"}
    )
    match = await user_service.resolve_user_id_by_name("ALICE")
    assert match is not None
    assert match.user_id == "u1"
    assert match.confidence == 0.8


async def test_resolve_user_id_by_name_email_local_part(
    user_service: UserService,
) -> None:
    """Email local-part match returns 0.7 confidence."""
    await user_service.create_user(
        "u1", {"email": "ahandle@example.com", "display_name": "Alice Smith"}
    )
    match = await user_service.resolve_user_id_by_name("ahandle")
    assert match is not None
    assert match.user_id == "u1"
    assert match.confidence == 0.7


async def test_resolve_user_id_by_name_returns_none_for_no_match(
    user_service: UserService,
) -> None:
    await user_service.create_user(
        "u1", {"email": "alice@example.com", "display_name": "Alice Smith"}
    )
    assert await user_service.resolve_user_id_by_name("zelda") is None


async def test_resolve_user_id_by_name_returns_none_when_input_empty(
    user_service: UserService,
) -> None:
    assert await user_service.resolve_user_id_by_name("") is None
    assert await user_service.resolve_user_id_by_name("   ") is None


async def test_resolve_user_id_by_name_returns_none_on_ambiguous_match(
    user_service: UserService,
) -> None:
    """Two users with the same first name → ambiguous → None.

    Don't silently pick one — the caller would rather fall back to
    asking a human than risk attributing actions to the wrong person.
    """
    await user_service.create_user(
        "u1",
        {"username": "asmith", "email": "a@example.com", "display_name": "Alice Smith"},
    )
    await user_service.create_user(
        "u2",
        {"username": "ajones", "email": "b@example.com", "display_name": "Alice Jones"},
    )
    assert await user_service.resolve_user_id_by_name("alice") is None


async def test_resolve_user_id_by_name_prefers_higher_priority_match(
    user_service: UserService,
) -> None:
    """When the same string matches at multiple priority levels for
    different users, the higher-priority bucket wins.

    Here ``alice`` is the full display name of u1, AND the email local
    part of u2. The full-display match (1.0) takes priority — no
    ambiguity, no None.
    """
    await user_service.create_user(
        "u1",
        {"username": "alice", "email": "x@example.com", "display_name": "alice"},
    )
    await user_service.create_user(
        "u2",
        {"username": "bob", "email": "alice@example.com", "display_name": "Bob"},
    )
    match = await user_service.resolve_user_id_by_name("alice")
    assert match is not None
    assert match.user_id == "u1"
    assert match.confidence == 1.0


async def test_resolve_user_id_by_name_skips_system_pseudo_users(
    user_service: UserService,
) -> None:
    """root/system/guest are filtered out — they're not real targets."""
    # ``root`` is auto-created by the service fixture; querying for
    # its display name must not return it.
    match = await user_service.resolve_user_id_by_name("root")
    assert match is None


# --- WS handler tests ---


class _FakeConn:
    """Minimal stand-in for a WsConnection.

    Carries an optional ``user_id`` so handlers that read the caller's
    identity (``users.prefs.*``) can be driven from tests without
    standing up the auth stack.
    """

    def __init__(self, user_id: str = "") -> None:
        self.user_id = user_id


# --- User prefs (metadata + RPC) ---


async def test_get_user_pref_returns_default_when_user_missing(
    user_service: UserService,
) -> None:
    val = await user_service.get_user_pref("nope", "test.pref", False)
    assert val is False


async def test_get_user_pref_returns_default_when_key_missing(
    user_service: UserService,
) -> None:
    # Root exists but has no ``test.pref`` metadata key.
    val = await user_service.get_user_pref(
        _ROOT_USER_ID, "test.pref", False
    )
    assert val is False


async def test_set_and_get_user_pref_roundtrips(
    user_service: UserService,
) -> None:
    await user_service.set_user_pref(_ROOT_USER_ID, "test.pref", True)
    val = await user_service.get_user_pref(
        _ROOT_USER_ID, "test.pref", False
    )
    assert val is True


async def test_set_user_pref_preserves_other_metadata(
    user_service: UserService,
) -> None:
    # Seed an unrelated key, then write our pref — the original must
    # survive.
    await user_service.set_user_pref(_ROOT_USER_ID, "ui.theme", "dark")
    await user_service.set_user_pref(_ROOT_USER_ID, "test.pref", True)
    assert (
        await user_service.get_user_pref(_ROOT_USER_ID, "ui.theme", None) == "dark"
    )
    assert (
        await user_service.get_user_pref(
            _ROOT_USER_ID, "test.pref", False
        )
        is True
    )


async def test_set_user_pref_unknown_user_raises(
    user_service: UserService,
) -> None:
    with pytest.raises(KeyError):
        await user_service.set_user_pref("ghost", "x", "y")


async def test_ws_prefs_get_self_only(
    user_service: UserService,
) -> None:
    # Unauthenticated connection — refused.
    reply = await user_service._ws_user_prefs_get(
        _FakeConn(user_id=""), {"id": "1", "key": "test.pref"}
    )
    assert reply["type"] == "gilbert.error"
    assert reply["code"] == 401


async def test_ws_prefs_get_requires_key(
    user_service: UserService,
) -> None:
    reply = await user_service._ws_user_prefs_get(
        _FakeConn(user_id=_ROOT_USER_ID), {"id": "1"}
    )
    assert reply["type"] == "gilbert.error"
    assert reply["code"] == 400


async def test_ws_prefs_get_returns_value(
    user_service: UserService,
) -> None:
    await user_service.set_user_pref(_ROOT_USER_ID, "test.pref", True)
    reply = await user_service._ws_user_prefs_get(
        _FakeConn(user_id=_ROOT_USER_ID),
        {"id": "1", "key": "test.pref", "default": False},
    )
    assert reply["type"] == "gilbert.result"
    assert reply["value"] is True


async def test_ws_prefs_get_returns_default_when_unset(
    user_service: UserService,
) -> None:
    reply = await user_service._ws_user_prefs_get(
        _FakeConn(user_id=_ROOT_USER_ID),
        {"id": "1", "key": "test.pref", "default": False},
    )
    assert reply["type"] == "gilbert.result"
    assert reply["value"] is False


async def test_ws_prefs_set_self_only(user_service: UserService) -> None:
    reply = await user_service._ws_user_prefs_set(
        _FakeConn(user_id=""),
        {"id": "1", "key": "test.pref", "value": True},
    )
    assert reply["type"] == "gilbert.error"
    assert reply["code"] == 401


async def test_ws_prefs_set_persists(user_service: UserService) -> None:
    reply = await user_service._ws_user_prefs_set(
        _FakeConn(user_id=_ROOT_USER_ID),
        {"id": "1", "key": "test.pref", "value": True},
    )
    assert reply["type"] == "gilbert.result"
    assert reply["ok"] is True
    # Round-trips on disk.
    assert (
        await user_service.get_user_pref(
            _ROOT_USER_ID, "test.pref", False
        )
        is True
    )


async def test_ws_prefs_set_uses_connection_identity(
    user_service: UserService,
) -> None:
    # Even if the frame contained a ``user_id``, the handler ignores it
    # and uses ``conn.user_id``. Verify by passing an obviously bogus
    # frame ``user_id`` — the persisted value should land on the
    # *connection's* user, not the frame's.
    await user_service._ws_user_prefs_set(
        _FakeConn(user_id=_ROOT_USER_ID),
        {
            "id": "1",
            "key": "test.pref",
            "value": True,
            "user_id": "attacker",
        },
    )
    # Root flipped.
    assert (
        await user_service.get_user_pref(
            _ROOT_USER_ID, "test.pref", False
        )
        is True
    )
    # No "attacker" user exists, so nothing got created for that id.
    assert await user_service.get_user("attacker") is None


async def test_ws_create_user(user_service: UserService) -> None:
    frame = {
        "id": "1",
        "username": "alice",
        "password": "secret123",
        "display_name": "Alice",
        "email": "alice@example.com",
    }
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
    svc = UserService(
        root_password_hash="hashed_pw", default_roles=["user"], allow_user_creation=False
    )
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
    await user_service.create_user(
        "u_del", {"username": "todelete", "email": "", "display_name": "Del"}
    )
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
