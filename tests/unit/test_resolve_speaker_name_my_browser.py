"""Tests for the magic 'my browser' aliases in resolve_speaker_name."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.speaker import SpeakerService
from gilbert.core.services.storage import StorageService
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.context import set_current_user
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.storage import StorageBackend


class StubStorageBackend(StorageBackend):
    """Minimal in-memory storage for alias tests."""

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
        return []

    async def count(self, query: Any) -> int:
        return 0

    async def list_collections(self) -> list[str]:
        return []

    async def drop_collection(self, collection: str) -> None:
        pass

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
def storage_service(stub_storage: StubStorageBackend) -> StorageService:
    return StorageService(stub_storage)


@pytest.fixture
def resolver(storage_service: StorageService) -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)

    def get_capability(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        return None

    def require_capability(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(f"Missing capability: {cap}")

    mock.get_capability.side_effect = get_capability
    mock.require_capability.side_effect = require_capability
    return mock


@pytest.fixture
async def svc(resolver: ServiceResolver) -> SpeakerService:
    s = SpeakerService()
    backend = BrowserSpeakerBackend()
    await backend.initialize({})
    s._backends = {"browser": backend}
    s._enabled = True
    await s.start(resolver)
    return s


def _user(uid: str = "alice") -> UserContext:
    return UserContext(user_id=uid, display_name=uid.title(), email="", roles=frozenset({"user"}))


@pytest.mark.parametrize("alias", ["my browser", "my speaker", "for me", "me"])
@pytest.mark.asyncio
async def test_my_browser_aliases_resolve_to_current_user(
    svc: SpeakerService, alias: str
) -> None:
    set_current_user(_user("alice"))
    assert await svc.resolve_speaker_name(alias) == "browser:alice"


@pytest.mark.parametrize("alias", ["My Browser", "MY SPEAKER", "  for me  ", "Me"])
@pytest.mark.asyncio
async def test_my_browser_aliases_case_and_whitespace_insensitive(
    svc: SpeakerService, alias: str
) -> None:
    set_current_user(_user("alice"))
    assert await svc.resolve_speaker_name(alias) == "browser:alice"


@pytest.mark.asyncio
async def test_my_browser_does_not_consult_backend(
    svc: SpeakerService, monkeypatch
) -> None:
    """The aliases must short-circuit before any backend call."""
    backend = svc._backends["browser"]
    called: list[Any] = []
    async def fake_list_speakers() -> list[Any]:
        called.append("list_speakers")
        return []
    monkeypatch.setattr(backend, "list_speakers", fake_list_speakers)

    set_current_user(_user("alice"))
    await svc.resolve_speaker_name("my browser")
    assert called == [], "resolve_speaker_name('my browser') must not hit the backend"
