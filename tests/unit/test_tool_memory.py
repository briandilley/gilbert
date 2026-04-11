"""Tests for _ToolMemoryHelper — per-user key-value store for tools/skills."""

from typing import Any

import pytest

from gilbert.core.services.ai import AIService, _ToolMemoryHelper, _tool_memory_entity_id, _TOOL_MEMORY_COLLECTION


# ── Fake storage (same pattern as test_memory_service) ─────


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
            match = True
            for f in query.filters or []:
                if record.get(f.field) != f.value:
                    match = False
                    break
            if match:
                results.append(record)
        return results

    async def ensure_index(self, index_def: Any) -> None:
        self._indexes.append(index_def)


@pytest.fixture
def fake_storage() -> FakeStorageBackend:
    return FakeStorageBackend()


@pytest.fixture
async def svc(fake_storage: FakeStorageBackend) -> _ToolMemoryHelper:
    helper = _ToolMemoryHelper(fake_storage)
    await helper.setup_indexes()
    return helper


USER = "brian@example.com"
USER2 = "alice@example.com"
NS = "web_search"
NS2 = "sales_agent"


# ── Tests ───────────────────────────────────────────────────


class TestToolMemoryHelper:
    def test_ai_service_has_tool_memory_capability(self) -> None:
        svc = AIService()
        info = svc.service_info()
        assert "tool_memory" in info.capabilities

    @pytest.mark.asyncio
    async def test_put_and_get(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "query", "python async")
        result = await svc.get(USER, NS, "query")
        assert result == "python async"

    @pytest.mark.asyncio
    async def test_get_missing(self, svc: _ToolMemoryHelper) -> None:
        result = await svc.get(USER, NS, "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_upsert(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "prefs", {"safe": True})
        await svc.put(USER, NS, "prefs", {"safe": False})
        result = await svc.get(USER, NS, "prefs")
        assert result == {"safe": False}

    @pytest.mark.asyncio
    async def test_put_preserves_created_at(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "key1", "v1")
        eid = _tool_memory_entity_id(USER, NS, "key1")
        record1 = await svc._storage.get(_TOOL_MEMORY_COLLECTION, eid)
        created1 = record1["created_at"]

        await svc.put(USER, NS, "key1", "v2")
        record2 = await svc._storage.get(_TOOL_MEMORY_COLLECTION, eid)
        assert record2["created_at"] == created1
        assert record2["updated_at"] >= created1

    @pytest.mark.asyncio
    async def test_delete(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "temp", "data")
        assert await svc.delete(USER, NS, "temp") is True
        assert await svc.get(USER, NS, "temp") is None

    @pytest.mark.asyncio
    async def test_delete_missing(self, svc: _ToolMemoryHelper) -> None:
        assert await svc.delete(USER, NS, "nope") is False

    @pytest.mark.asyncio
    async def test_list_keys(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "a", 1)
        await svc.put(USER, NS, "b", 2)
        await svc.put(USER, NS2, "c", 3)  # different namespace
        keys = await svc.list_keys(USER, NS)
        assert sorted(keys) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_get_all(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "x", "hello")
        await svc.put(USER, NS, "y", 42)
        result = await svc.get_all(USER, NS)
        assert result == {"x": "hello", "y": 42}

    @pytest.mark.asyncio
    async def test_delete_all(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "a", 1)
        await svc.put(USER, NS, "b", 2)
        await svc.put(USER, NS2, "keep", "this")
        count = await svc.delete_all(USER, NS)
        assert count == 2
        assert await svc.get(USER, NS, "a") is None
        assert await svc.get(USER, NS2, "keep") == "this"

    @pytest.mark.asyncio
    async def test_user_isolation(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "secret", "brian_data")
        await svc.put(USER2, NS, "secret", "alice_data")
        assert await svc.get(USER, NS, "secret") == "brian_data"
        assert await svc.get(USER2, NS, "secret") == "alice_data"

    @pytest.mark.asyncio
    async def test_cross_namespace_access(self, svc: _ToolMemoryHelper) -> None:
        """Tools can read each other's namespaces."""
        await svc.put(USER, NS, "shared_key", "shared_value")
        result = await svc.get(USER, NS, "shared_key")
        assert result == "shared_value"

    @pytest.mark.asyncio
    async def test_complex_values(self, svc: _ToolMemoryHelper) -> None:
        value = {"nested": {"list": [1, 2, 3], "bool": True, "null": None}}
        await svc.put(USER, NS, "complex", value)
        result = await svc.get(USER, NS, "complex")
        assert result == value

    @pytest.mark.asyncio
    async def test_get_user_summaries(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "recent_queries", ["python", "sqlite"])
        await svc.put(USER, NS2, "lead", {"company": "Acme"})
        summaries = await svc.get_user_summaries(USER)
        assert "Tool Memories" in summaries
        assert "2 stored" in summaries
        assert NS in summaries
        assert NS2 in summaries
        assert "recent_queries" in summaries
        assert "lead" in summaries

    @pytest.mark.asyncio
    async def test_get_user_summaries_empty(self, svc: _ToolMemoryHelper) -> None:
        result = await svc.get_user_summaries("nobody@example.com")
        assert result == ""

    @pytest.mark.asyncio
    async def test_delete_wrong_user(self, svc: _ToolMemoryHelper) -> None:
        await svc.put(USER, NS, "mine", "data")
        assert await svc.delete(USER2, NS, "mine") is False
        assert await svc.get(USER, NS, "mine") == "data"
