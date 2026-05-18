"""Tests for AIService.get_speech_pref / set_speech_pref + RBAC."""

from __future__ import annotations

import pytest

from gilbert.core.services.ai import _CHAT_SPEECH_COLLECTION, AIService


class _InMemoryStorage:
    """Minimal entity-store stand-in for AIService unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    async def get(self, collection: str, key: str):
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, value: dict) -> None:
        self._data.setdefault(collection, {})[key] = value

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def query(self, collection: str, **kw):
        return list(self._data.get(collection, {}).values())


@pytest.fixture
def svc() -> AIService:
    s = AIService()
    s._storage = _InMemoryStorage()  # type: ignore[assignment]
    return s


@pytest.mark.asyncio
async def test_get_speech_pref_defaults_to_false(svc: AIService) -> None:
    assert (await svc.get_speech_pref("alice", "conv-1")) is False


@pytest.mark.asyncio
async def test_set_then_get_round_trip(svc: AIService) -> None:
    await svc.set_speech_pref("alice", "conv-1", True)
    assert (await svc.get_speech_pref("alice", "conv-1")) is True

    await svc.set_speech_pref("alice", "conv-1", False)
    assert (await svc.get_speech_pref("alice", "conv-1")) is False


@pytest.mark.asyncio
async def test_prefs_are_user_scoped(svc: AIService) -> None:
    await svc.set_speech_pref("alice", "conv-1", True)
    assert (await svc.get_speech_pref("bob", "conv-1")) is False


@pytest.mark.asyncio
async def test_collection_id_format(svc: AIService) -> None:
    """Verifies the persisted key is f"{user}:{conv}" so a single conv with
    multiple members produces N rows, not one row that gets stomped."""
    await svc.set_speech_pref("alice", "conv-1", True)
    storage = svc._storage  # type: ignore[attr-defined]
    assert "alice:conv-1" in storage._data[_CHAT_SPEECH_COLLECTION]
