"""Tests for StorageService tool provider."""

import json
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.storage import StorageService
from gilbert.interfaces.storage import StorageBackend


@pytest.fixture
def storage_backend() -> StorageBackend:
    backend = AsyncMock(spec=StorageBackend)
    backend.put = AsyncMock()
    backend.get = AsyncMock(return_value=None)
    backend.query = AsyncMock(return_value=[])
    backend.list_collections = AsyncMock(return_value=[])
    return backend


@pytest.fixture
def service(storage_backend: StorageBackend) -> StorageService:
    return StorageService(storage_backend)


# --- Service info ---


def test_service_info(service: StorageService) -> None:
    info = service.service_info()
    assert "entity_storage" in info.capabilities
    assert "ai_tools" in info.capabilities


def test_tool_provider_name(service: StorageService) -> None:
    assert service.tool_provider_name == "storage"


def test_get_tools(service: StorageService) -> None:
    tools = service.get_tools()
    names = [t.name for t in tools]
    assert "store_entity" in names
    assert "get_entity" in names
    assert "query_entities" in names
    assert "list_collections" in names


# --- store_entity ---


async def test_tool_store_entity(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    result = await service.execute_tool("store_entity", {
        "collection": "notes",
        "id": "note-1",
        "data": {"title": "Hello", "body": "World"},
    })
    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["collection"] == "notes"
    assert parsed["id"] == "note-1"
    storage_backend.put.assert_called_once_with(  # type: ignore[union-attr]
        "notes", "note-1", {"title": "Hello", "body": "World"}
    )


# --- get_entity ---


async def test_tool_get_entity_found(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    storage_backend.get = AsyncMock(  # type: ignore[union-attr]
        return_value={"title": "Hello", "body": "World"}
    )
    result = await service.execute_tool("get_entity", {
        "collection": "notes",
        "id": "note-1",
    })
    parsed = json.loads(result)
    assert parsed["title"] == "Hello"


async def test_tool_get_entity_not_found(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    storage_backend.get = AsyncMock(return_value=None)  # type: ignore[union-attr]
    result = await service.execute_tool("get_entity", {
        "collection": "notes",
        "id": "missing",
    })
    parsed = json.loads(result)
    assert "error" in parsed


# --- query_entities ---


async def test_tool_query_entities(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    storage_backend.query = AsyncMock(  # type: ignore[union-attr]
        return_value=[{"_id": "1", "name": "Alice"}, {"_id": "2", "name": "Bob"}]
    )
    result = await service.execute_tool("query_entities", {
        "collection": "users",
        "filters": [{"field": "name", "op": "eq", "value": "Alice"}],
        "limit": 10,
    })
    parsed = json.loads(result)
    assert len(parsed) == 2

    # Verify the query was built correctly
    call_args = storage_backend.query.call_args[0][0]  # type: ignore[union-attr]
    assert call_args.collection == "users"
    assert len(call_args.filters) == 1
    assert call_args.filters[0].field == "name"
    assert call_args.filters[0].op.value == "eq"
    assert call_args.limit == 10


async def test_tool_query_entities_no_filters(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    storage_backend.query = AsyncMock(return_value=[])  # type: ignore[union-attr]
    result = await service.execute_tool("query_entities", {"collection": "empty"})
    parsed = json.loads(result)
    assert parsed == []


async def test_tool_query_entities_with_sort(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    storage_backend.query = AsyncMock(return_value=[])  # type: ignore[union-attr]
    await service.execute_tool("query_entities", {
        "collection": "items",
        "sort": [{"field": "created_at", "descending": True}],
    })
    call_args = storage_backend.query.call_args[0][0]  # type: ignore[union-attr]
    assert len(call_args.sort) == 1
    assert call_args.sort[0].field == "created_at"
    assert call_args.sort[0].descending is True


# --- list_collections ---


async def test_tool_list_collections(
    service: StorageService, storage_backend: StorageBackend
) -> None:
    storage_backend.list_collections = AsyncMock(  # type: ignore[union-attr]
        return_value=["notes", "users", "events"]
    )
    result = await service.execute_tool("list_collections", {})
    parsed = json.loads(result)
    assert parsed == ["notes", "users", "events"]


# --- Unknown tool ---


async def test_tool_unknown_raises(service: StorageService) -> None:
    with pytest.raises(KeyError, match="Unknown tool"):
        await service.execute_tool("nonexistent", {})
