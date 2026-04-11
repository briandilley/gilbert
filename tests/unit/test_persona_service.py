"""Tests for _PersonaHelper — persona storage, editing, and defaults."""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import DEFAULT_PERSONA, AIService, _PersonaHelper
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.storage import StorageBackend


class StubStorageBackend(StorageBackend):
    """Minimal in-memory storage for persona tests."""

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
        return list(self._data.get(query.collection, {}).values())

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
def stub_storage() -> StubStorageBackend:
    return StubStorageBackend()


@pytest.fixture
def helper(stub_storage: StubStorageBackend) -> _PersonaHelper:
    return _PersonaHelper(stub_storage)


# --- Lifecycle ---


async def test_load_uses_default_when_no_saved(
    helper: _PersonaHelper,
) -> None:
    await helper.load()
    assert helper.persona == DEFAULT_PERSONA
    assert helper.is_customized is False


async def test_load_loads_saved_persona(
    helper: _PersonaHelper, stub_storage: StubStorageBackend
) -> None:
    await stub_storage.put("persona", "active", {
        "text": "Custom persona text",
        "customized": True,
    })
    await helper.load()
    assert helper.persona == "Custom persona text"
    assert helper.is_customized is True


# --- Update ---


async def test_update_persona(
    helper: _PersonaHelper, stub_storage: StubStorageBackend
) -> None:
    await helper.load()
    await helper.update_persona("Be a pirate.")
    assert helper.persona == "Be a pirate."
    assert helper.is_customized is True

    saved = await stub_storage.get("persona", "active")
    assert saved is not None
    assert saved["text"] == "Be a pirate."
    assert saved["customized"] is True


async def test_reset_persona(
    helper: _PersonaHelper, stub_storage: StubStorageBackend
) -> None:
    await helper.load()
    await helper.update_persona("Custom")
    await helper.reset_persona()
    assert helper.persona == DEFAULT_PERSONA
    assert helper.is_customized is False

    saved = await stub_storage.get("persona", "active")
    assert saved is not None
    assert saved["customized"] is False


# --- Tools via AIService ---


@pytest.fixture
def storage_service(stub_storage: StubStorageBackend) -> StorageService:
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
    mock.get_all.return_value = []
    return mock


def test_ai_service_has_persona_tools() -> None:
    """AIService exposes persona tools."""
    from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

    class _StubBackend(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None: pass
        async def close(self) -> None: pass
        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(message=Message(role=MessageRole.ASSISTANT, content=""), model="stub")

    svc = AIService()
    svc._backend = _StubBackend()
    svc._enabled = True
    tools = svc.get_tools()
    names = [t.name for t in tools]
    assert "get_persona" in names
    assert "update_persona" in names
    assert "reset_persona" in names


async def test_tool_get_persona(
    resolver: ServiceResolver,
) -> None:
    from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

    class _StubBackend(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None: pass
        async def close(self) -> None: pass
        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(message=Message(role=MessageRole.ASSISTANT, content=""), model="stub")

    svc = AIService()
    svc._backend = _StubBackend()
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    result = await svc.execute_tool("get_persona", {})
    parsed = json.loads(result)
    assert parsed["persona"] == DEFAULT_PERSONA


async def test_tool_update_persona(
    resolver: ServiceResolver,
) -> None:
    from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

    class _StubBackend(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None: pass
        async def close(self) -> None: pass
        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(message=Message(role=MessageRole.ASSISTANT, content=""), model="stub")

    svc = AIService()
    svc._backend = _StubBackend()
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    result = await svc.execute_tool("update_persona", {"text": "Be helpful."})
    parsed = json.loads(result)
    assert parsed["status"] == "updated"
    assert svc._persona is not None
    assert svc._persona.persona == "Be helpful."
    assert svc._persona.is_customized is True


async def test_tool_reset_persona(
    resolver: ServiceResolver,
) -> None:
    from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

    class _StubBackend(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None: pass
        async def close(self) -> None: pass
        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(message=Message(role=MessageRole.ASSISTANT, content=""), model="stub")

    svc = AIService()
    svc._backend = _StubBackend()
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    await svc.execute_tool("update_persona", {"text": "Custom"})
    result = await svc.execute_tool("reset_persona", {})
    parsed = json.loads(result)
    assert parsed["status"] == "reset"
    assert svc._persona is not None
    assert svc._persona.persona == DEFAULT_PERSONA
    assert svc._persona.is_customized is False


async def test_tool_unknown_raises(
    resolver: ServiceResolver,
) -> None:
    from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

    class _StubBackend(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None: pass
        async def close(self) -> None: pass
        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(message=Message(role=MessageRole.ASSISTANT, content=""), model="stub")

    svc = AIService()
    svc._backend = _StubBackend()
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    with pytest.raises(KeyError, match="Unknown tool"):
        await svc.execute_tool("nonexistent", {})
