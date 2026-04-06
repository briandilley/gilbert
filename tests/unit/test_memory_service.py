"""Tests for MemoryService — per-user persistent memories."""

import json
from typing import Any
from unittest.mock import patch

import pytest

from gilbert.core.services.memory import MemoryService
from gilbert.interfaces.auth import UserContext


# ── Fake storage ────────────────────────────────────────────


class FakeStorageBackend:
    """In-memory storage backend for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._indexes: list[Any] = []

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        record = self._data.get(collection, {}).get(key)
        if record is not None:
            return {**record, "_id": key}
        return None

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def query(self, query: Any) -> list[dict[str, Any]]:
        collection = query.collection
        results = []
        for key, data in self._data.get(collection, {}).items():
            record = {**data, "_id": key}
            # Apply filters
            match = True
            for f in (query.filters or []):
                if record.get(f.field) != f.value:
                    match = False
                    break
            if match:
                results.append(record)
        return results

    async def ensure_index(self, index_def: Any) -> None:
        self._indexes.append(index_def)


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorageBackend()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


def _set_user(user_id: str = "brian@example.com") -> UserContext:
    """Create and set a test user context."""
    user = UserContext(
        user_id=user_id,
        email=user_id,
        display_name="Brian",
        roles=frozenset({"user"}),
    )
    from gilbert.core.context import set_current_user
    set_current_user(user)
    return user


@pytest.fixture
def resolver() -> FakeResolver:
    r = FakeResolver()
    r.caps["entity_storage"] = FakeStorageService()
    return r


@pytest.fixture
async def memory_service(resolver: FakeResolver) -> MemoryService:
    svc = MemoryService()
    await svc.start(resolver)
    return svc


# ── Tests ───────────────────────────────────────────────────


class TestMemoryService:
    def test_service_info(self) -> None:
        svc = MemoryService()
        info = svc.service_info()
        assert info.name == "memory"
        assert "user_memory" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "entity_storage" in info.requires

    def test_tool_definitions(self) -> None:
        svc = MemoryService()
        tools = svc.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "memory"
        action_param = next(p for p in tools[0].parameters if p.name == "action")
        assert set(action_param.enum) == {"remember", "recall", "update", "forget", "list"}

    @pytest.mark.asyncio
    async def test_remember(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        result = await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Prefers metric units",
            "content": "Brian prefers metric units for all measurements",
            "source": "user",
        })
        assert "remember" in result.lower()

    @pytest.mark.asyncio
    async def test_list(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Likes coffee",
            "content": "Brian likes strong black coffee",
        })
        result = await memory_service.execute_tool("memory", {"action": "list"})
        assert "1 memory" in result
        assert "Likes coffee" in result

    @pytest.mark.asyncio
    async def test_recall(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        result = await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Test memory",
            "content": "Detailed content here",
        })
        # Extract memory ID from result
        memory_id = result.split("memory ")[-1].rstrip(")")
        recall_result = await memory_service.execute_tool("memory", {
            "action": "recall",
            "ids": [memory_id],
        })
        assert "Detailed content here" in recall_result
        assert "Accessed: 1 times" in recall_result

    @pytest.mark.asyncio
    async def test_update(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        result = await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Old summary",
            "content": "Old content",
        })
        memory_id = result.split("memory ")[-1].rstrip(")")
        update_result = await memory_service.execute_tool("memory", {
            "action": "update",
            "id": memory_id,
            "summary": "New summary",
        })
        assert "updated" in update_result.lower()

    @pytest.mark.asyncio
    async def test_forget(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        result = await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Temporary",
            "content": "Will be forgotten",
        })
        memory_id = result.split("memory ")[-1].rstrip(")")
        forget_result = await memory_service.execute_tool("memory", {
            "action": "forget",
            "id": memory_id,
        })
        assert "forgotten" in forget_result.lower()

        # List should be empty now
        list_result = await memory_service.execute_tool("memory", {"action": "list"})
        assert "no memories" in list_result.lower()

    @pytest.mark.asyncio
    async def test_ownership_isolation(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        result = await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Brian's memory",
            "content": "Private stuff",
        })
        memory_id = result.split("memory ")[-1].rstrip(")")

        # Switch to different user
        _set_user("alice@example.com")
        forget_result = await memory_service.execute_tool("memory", {
            "action": "forget",
            "id": memory_id,
        })
        assert "doesn't belong" in forget_result.lower()

        # Alice's list should be empty
        list_result = await memory_service.execute_tool("memory", {"action": "list"})
        assert "no memories" in list_result.lower()

    @pytest.mark.asyncio
    async def test_requires_authenticated_user(self, memory_service: MemoryService) -> None:
        from gilbert.core.context import set_current_user
        set_current_user(UserContext.GUEST)
        result = await memory_service.execute_tool("memory", {"action": "list"})
        assert "authenticated" in result.lower()

    @pytest.mark.asyncio
    async def test_get_user_summaries(self, memory_service: MemoryService) -> None:
        _set_user("brian@example.com")
        await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Prefers metric",
            "content": "Uses metric units",
        })
        await memory_service.execute_tool("memory", {
            "action": "remember",
            "summary": "Drives a Tesla",
            "content": "Has a Model 3",
        })
        summaries = await memory_service.get_user_summaries("brian@example.com")
        assert "Prefers metric" in summaries
        assert "Drives a Tesla" in summaries
        assert "2 stored" in summaries

    @pytest.mark.asyncio
    async def test_get_user_summaries_empty(self, memory_service: MemoryService) -> None:
        result = await memory_service.get_user_summaries("nobody@example.com")
        assert result == ""
