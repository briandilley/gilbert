"""Tests for AccessControlService — role hierarchy, tool permissions, RBAC checks."""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.access_control import AccessControlService
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import ToolDefinition


class StubStorage(StorageBackend):
    """Minimal in-memory storage for ACL tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[entity_id] = {"_id": entity_id, **data}

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        self._data.get(collection, {}).pop(entity_id, None)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return entity_id in self._data.get(collection, {})

    async def query(self, query: Any) -> list[dict[str, Any]]:
        entities = list(self._data.get(query.collection, {}).values())
        if query.sort:
            for s in reversed(query.sort):
                entities.sort(key=lambda e: e.get(s.field, 0), reverse=s.descending)
        return entities

    async def count(self, query: Any) -> int:
        return len(await self.query(query))

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: Any) -> None:
        pass

    async def list_indexes(self, collection: str) -> list[Any]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


@pytest.fixture
def stub_storage() -> StubStorage:
    return StubStorage()


@pytest.fixture
def storage_service(stub_storage: StubStorage) -> StorageService:
    return StorageService(stub_storage)


@pytest.fixture
def resolver(storage_service: StorageService) -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    mock.require_capability.side_effect = require_cap
    mock.get_capability.return_value = None
    return mock


@pytest.fixture
async def service(resolver: ServiceResolver) -> AccessControlService:
    svc = AccessControlService()
    await svc.start(resolver)
    return svc


def _user(roles: frozenset[str]) -> UserContext:
    return UserContext(user_id="test", email="", display_name="", roles=roles)


def _tool(name: str, required_role: str = "user") -> ToolDefinition:
    return ToolDefinition(name=name, description="test", required_role=required_role)


# --- Built-in roles ---


class TestBuiltinRoles:
    async def test_seeds_builtin_roles(self, service: AccessControlService) -> None:
        roles = await service.list_roles()
        names = {r["name"] for r in roles}
        assert "admin" in names
        assert "user" in names
        assert "everyone" in names

    async def test_builtin_levels(self, service: AccessControlService) -> None:
        assert service.get_role_level("admin") == 0
        assert service.get_role_level("user") == 100
        assert service.get_role_level("everyone") == 200

    async def test_unknown_role_defaults_to_everyone_level(self, service: AccessControlService) -> None:
        assert service.get_role_level("nonexistent") == 200

    async def test_cannot_delete_builtin(self, service: AccessControlService) -> None:
        with pytest.raises(ValueError, match="Cannot delete built-in"):
            await service.delete_role("admin")

    async def test_cannot_change_builtin_level(self, service: AccessControlService) -> None:
        with pytest.raises(ValueError, match="Cannot change level"):
            await service.update_role("admin", level=50)

    async def test_can_change_builtin_description(self, service: AccessControlService) -> None:
        role = await service.update_role("user", description="Updated desc")
        assert role["description"] == "Updated desc"


# --- Custom roles ---


class TestCustomRoles:
    async def test_create_custom_role(self, service: AccessControlService) -> None:
        role = await service.create_role("manager", level=50, description="Middle management")
        assert role["name"] == "manager"
        assert role["level"] == 50
        assert role["builtin"] is False
        assert service.get_role_level("manager") == 50

    async def test_create_duplicate_raises(self, service: AccessControlService) -> None:
        await service.create_role("ops", level=75)
        with pytest.raises(ValueError, match="already exists"):
            await service.create_role("ops", level=80)

    async def test_update_custom_role(self, service: AccessControlService) -> None:
        await service.create_role("ops", level=75)
        role = await service.update_role("ops", level=60)
        assert role["level"] == 60
        assert service.get_role_level("ops") == 60

    async def test_delete_custom_role(self, service: AccessControlService) -> None:
        await service.create_role("temp", level=150)
        await service.delete_role("temp")
        assert service.get_role_level("temp") == 200  # back to default


# --- Effective level ---


class TestEffectiveLevel:
    async def test_admin_user(self, service: AccessControlService) -> None:
        assert service.get_effective_level(_user(frozenset({"admin"}))) == 0

    async def test_regular_user(self, service: AccessControlService) -> None:
        assert service.get_effective_level(_user(frozenset({"user"}))) == 100

    async def test_everyone_user(self, service: AccessControlService) -> None:
        assert service.get_effective_level(_user(frozenset({"everyone"}))) == 200

    async def test_multi_role_takes_lowest(self, service: AccessControlService) -> None:
        assert service.get_effective_level(_user(frozenset({"user", "admin"}))) == 0

    async def test_no_roles_defaults_to_everyone(self, service: AccessControlService) -> None:
        assert service.get_effective_level(_user(frozenset())) == 200

    async def test_system_user_bypasses(self, service: AccessControlService) -> None:
        assert service.get_effective_level(UserContext.SYSTEM) == -1


# --- Tool access checks ---


class TestToolAccess:
    async def test_admin_can_access_admin_tool(self, service: AccessControlService) -> None:
        assert service.check_tool_access(_user(frozenset({"admin"})), _tool("x", "admin"))

    async def test_user_cannot_access_admin_tool(self, service: AccessControlService) -> None:
        assert not service.check_tool_access(_user(frozenset({"user"})), _tool("x", "admin"))

    async def test_user_can_access_user_tool(self, service: AccessControlService) -> None:
        assert service.check_tool_access(_user(frozenset({"user"})), _tool("x", "user"))

    async def test_user_can_access_everyone_tool(self, service: AccessControlService) -> None:
        assert service.check_tool_access(_user(frozenset({"user"})), _tool("x", "everyone"))

    async def test_everyone_can_access_everyone_tool(self, service: AccessControlService) -> None:
        assert service.check_tool_access(_user(frozenset({"everyone"})), _tool("x", "everyone"))

    async def test_everyone_cannot_access_user_tool(self, service: AccessControlService) -> None:
        assert not service.check_tool_access(_user(frozenset({"everyone"})), _tool("x", "user"))

    async def test_system_always_passes(self, service: AccessControlService) -> None:
        assert service.check_tool_access(UserContext.SYSTEM, _tool("x", "admin"))

    async def test_custom_role_at_level_50(self, service: AccessControlService) -> None:
        await service.create_role("manager", level=50)
        user = _user(frozenset({"manager"}))
        # Can access user (100) and everyone (200) tools
        assert service.check_tool_access(user, _tool("x", "user"))
        assert service.check_tool_access(user, _tool("x", "everyone"))
        # Cannot access admin (0) tools
        assert not service.check_tool_access(user, _tool("x", "admin"))


# --- Tool overrides ---


class TestToolOverrides:
    async def test_set_override(self, service: AccessControlService) -> None:
        await service.set_tool_override("announce", "everyone")
        # Now "everyone" role can access "announce"
        tool = _tool("announce", "user")  # default would be "user"
        assert service.check_tool_access(_user(frozenset({"everyone"})), tool)

    async def test_clear_override(self, service: AccessControlService) -> None:
        await service.set_tool_override("announce", "everyone")
        await service.clear_tool_override("announce")
        tool = _tool("announce", "user")
        # Back to default: "everyone" can't use a "user" tool
        assert not service.check_tool_access(_user(frozenset({"everyone"})), tool)

    async def test_override_unknown_role_raises(self, service: AccessControlService) -> None:
        with pytest.raises(ValueError, match="Unknown role"):
            await service.set_tool_override("x", "nonexistent")


# --- AI Tools ---


class TestTools:
    def test_tool_names(self, service: AccessControlService) -> None:
        names = [t.name for t in service.get_tools()]
        assert "list_roles" in names
        assert "create_role" in names
        assert "update_role" in names
        assert "delete_role" in names
        assert "get_tool_permissions" in names
        assert "set_tool_permission" in names
        assert "clear_tool_permission" in names

    def test_read_tools_are_everyone(self, service: AccessControlService) -> None:
        tools = {t.name: t for t in service.get_tools()}
        assert tools["list_roles"].required_role == "everyone"
        assert tools["get_tool_permissions"].required_role == "everyone"

    def test_write_tools_are_admin(self, service: AccessControlService) -> None:
        tools = {t.name: t for t in service.get_tools()}
        assert tools["create_role"].required_role == "admin"
        assert tools["update_role"].required_role == "admin"
        assert tools["delete_role"].required_role == "admin"
        assert tools["set_tool_permission"].required_role == "admin"

    async def test_tool_list_roles(self, service: AccessControlService) -> None:
        result = await service.execute_tool("list_roles", {})
        parsed = json.loads(result)
        assert len(parsed) >= 3
        names = {r["name"] for r in parsed}
        assert "admin" in names

    async def test_tool_create_role(self, service: AccessControlService) -> None:
        result = await service.execute_tool("create_role", {
            "name": "ops", "level": 75, "description": "Operations",
        })
        parsed = json.loads(result)
        assert parsed["status"] == "created"

    async def test_tool_delete_builtin_fails(self, service: AccessControlService) -> None:
        result = await service.execute_tool("delete_role", {"name": "admin"})
        parsed = json.loads(result)
        assert "error" in parsed

    async def test_tool_unknown_raises(self, service: AccessControlService) -> None:
        with pytest.raises(KeyError, match="Unknown tool"):
            await service.execute_tool("nonexistent", {})


# =============================================================================
# Collection ACL tests
# =============================================================================


class TestCollectionACL:
    async def test_default_read_is_user(self, service: AccessControlService) -> None:
        """Without explicit ACL, read defaults to 'user' level."""
        assert service.check_collection_read(_user(frozenset({"user"})), "anything")

    async def test_default_write_is_admin(self, service: AccessControlService) -> None:
        """Without explicit ACL, write defaults to 'admin' level."""
        assert not service.check_collection_write(_user(frozenset({"user"})), "anything")
        assert service.check_collection_write(_user(frozenset({"admin"})), "anything")

    async def test_set_collection_acl(self, service: AccessControlService) -> None:
        await service.set_collection_acl("speaker_aliases", read_role="everyone", write_role="user")
        assert service.check_collection_read(_user(frozenset({"everyone"})), "speaker_aliases")
        assert service.check_collection_write(_user(frozenset({"user"})), "speaker_aliases")

    async def test_set_collection_acl_restricts_sensitive(self, service: AccessControlService) -> None:
        await service.set_collection_acl("users", read_role="admin", write_role="admin")
        assert not service.check_collection_read(_user(frozenset({"user"})), "users")
        assert service.check_collection_read(_user(frozenset({"admin"})), "users")

    async def test_clear_collection_acl(self, service: AccessControlService) -> None:
        await service.set_collection_acl("test", read_role="admin", write_role="admin")
        await service.clear_collection_acl("test")
        # Back to defaults
        assert service.check_collection_read(_user(frozenset({"user"})), "test")

    async def test_system_bypasses_collection_acl(self, service: AccessControlService) -> None:
        await service.set_collection_acl("locked", read_role="admin", write_role="admin")
        assert service.check_collection_read(UserContext.SYSTEM, "locked")
        assert service.check_collection_write(UserContext.SYSTEM, "locked")

    async def test_tool_list_collection_acls(self, service: AccessControlService) -> None:
        await service.set_collection_acl("test_col", read_role="user", write_role="admin")
        result = await service.execute_tool("list_collection_acls", {})
        parsed = json.loads(result)
        assert len(parsed) >= 1
        assert any(a["collection"] == "test_col" for a in parsed)

    async def test_tool_set_collection_acl(self, service: AccessControlService) -> None:
        result = await service.execute_tool("set_collection_acl", {
            "collection": "my_col", "read_role": "everyone", "write_role": "user",
        })
        parsed = json.loads(result)
        assert parsed["status"] == "set"


# =============================================================================
# Chat visibility override tests
# =============================================================================


class TestChatVisibility:
    async def test_default_chat_enabled(self, service: AccessControlService) -> None:
        """Tools with chat_enabled=True are enabled by default."""
        tool = ToolDefinition(name="test_tool", description="test")
        assert service.is_tool_chat_enabled(tool) is True

    async def test_default_chat_disabled(self, service: AccessControlService) -> None:
        """Tools with chat_enabled=False are disabled by default."""
        tool = ToolDefinition(name="test_tool", description="test", chat_enabled=False)
        assert service.is_tool_chat_enabled(tool) is False

    async def test_override_enables_disabled_tool(self, service: AccessControlService) -> None:
        """Admin can override chat_enabled=False to True."""
        tool = ToolDefinition(name="internal_tool", description="test", chat_enabled=False)
        await service.set_chat_override("internal_tool", True)
        assert service.is_tool_chat_enabled(tool) is True

    async def test_override_disables_enabled_tool(self, service: AccessControlService) -> None:
        """Admin can override chat_enabled=True to False."""
        tool = ToolDefinition(name="public_tool", description="test", chat_enabled=True)
        await service.set_chat_override("public_tool", False)
        assert service.is_tool_chat_enabled(tool) is False

    async def test_clear_override_reverts_to_default(self, service: AccessControlService) -> None:
        """Clearing an override reverts to the tool's declared default."""
        tool = ToolDefinition(name="my_tool", description="test", chat_enabled=False)
        await service.set_chat_override("my_tool", True)
        assert service.is_tool_chat_enabled(tool) is True
        await service.clear_chat_override("my_tool")
        assert service.is_tool_chat_enabled(tool) is False

    async def test_override_persists_to_storage(
        self, service: AccessControlService, stub_storage: StubStorage
    ) -> None:
        """Chat overrides are persisted in storage."""
        await service.set_chat_override("some_tool", False)
        # StorageService wraps with "gilbert." namespace
        doc = await stub_storage.get("gilbert.acl_chat_overrides", "some_tool")
        assert doc is not None
        assert doc["chat_enabled"] is False

    async def test_clear_removes_from_storage(
        self, service: AccessControlService, stub_storage: StubStorage
    ) -> None:
        """Clearing an override removes it from storage."""
        await service.set_chat_override("some_tool", False)
        await service.clear_chat_override("some_tool")
        # StorageService wraps with "gilbert." namespace
        doc = await stub_storage.get("gilbert.acl_chat_overrides", "some_tool")
        assert doc is None

    async def test_tool_set_chat_enabled(self, service: AccessControlService) -> None:
        """The AI tool for setting chat visibility works."""
        result = await service.execute_tool("set_tool_chat_enabled", {
            "tool_name": "my_tool", "chat_enabled": False,
        })
        parsed = json.loads(result)
        assert parsed["status"] == "set"
        tool = ToolDefinition(name="my_tool", description="test")
        assert service.is_tool_chat_enabled(tool) is False

    async def test_tool_clear_chat_enabled(self, service: AccessControlService) -> None:
        """The AI tool for clearing chat visibility works."""
        await service.set_chat_override("my_tool", False)
        result = await service.execute_tool("clear_tool_chat_enabled", {
            "tool_name": "my_tool",
        })
        parsed = json.loads(result)
        assert parsed["status"] == "cleared"
        tool = ToolDefinition(name="my_tool", description="test")
        assert service.is_tool_chat_enabled(tool) is True
