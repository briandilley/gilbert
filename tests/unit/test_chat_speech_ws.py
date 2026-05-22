"""WS RPC tests for chat.read_aloud.{get,set}."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.services.ai import AIService
from gilbert.interfaces.auth import UserContext


class _InMemoryStorage:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    async def get(self, collection: str, key: str):
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, value: dict) -> None:
        self._data.setdefault(collection, {})[key] = value


class _FakeConn:
    """Minimal stand-in for WsConnectionBase."""

    def __init__(self, user_id: str = "alice") -> None:
        self.user_id = user_id
        # UserContext may require email and frozenset roles — adjust if your
        # check reveals a different constructor signature.
        self.user_ctx = self._make_user_ctx(user_id)
        self.manager = type("M", (), {"gilbert": None})()

    @staticmethod
    def _make_user_ctx(user_id: str) -> UserContext:
        # Inspect UserContext signature and build accordingly. If it
        # requires email and frozenset, this is the form:
        import inspect
        sig = inspect.signature(UserContext)
        kwargs: dict[str, Any] = {"user_id": user_id, "display_name": user_id.title()}
        if "email" in sig.parameters:
            kwargs["email"] = f"{user_id}@example.com"
        if "roles" in sig.parameters:
            kwargs["roles"] = frozenset()
        return UserContext(**kwargs)


@pytest.fixture
async def svc_with_conv() -> AIService:
    s = AIService()
    s._storage = _InMemoryStorage()  # type: ignore[assignment]
    # Seed a personal conversation owned by alice.
    await s._storage.put(
        "ai_conversations",
        "conv-1",
        {"user_id": "alice", "messages": []},
    )
    return s


@pytest.mark.asyncio
async def test_get_returns_false_when_unset(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_get(
        _FakeConn(), {"id": "r1", "conversation_id": "conv-1"}
    )
    assert result["type"] == "chat.read_aloud.get.result"
    assert result["enabled"] is False


@pytest.mark.asyncio
async def test_set_persists_and_echoes(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_set(
        _FakeConn(),
        {"id": "r2", "conversation_id": "conv-1", "enabled": True},
    )
    assert result["type"] == "chat.read_aloud.set.result"
    assert result["enabled"] is True

    got = await svc_with_conv._ws_chat_read_aloud_get(
        _FakeConn(), {"id": "r3", "conversation_id": "conv-1"}
    )
    assert got["enabled"] is True


@pytest.mark.asyncio
async def test_set_denied_for_non_member(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_set(
        _FakeConn(user_id="bob"),
        {"id": "r4", "conversation_id": "conv-1", "enabled": True},
    )
    assert result["type"] == "gilbert.error"
    assert result.get("code") == 403


@pytest.mark.asyncio
async def test_missing_conversation_returns_error(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_get(
        _FakeConn(), {"id": "r5", "conversation_id": "does-not-exist"}
    )
    assert result["type"] == "gilbert.error"
    assert result.get("code") == 404


@pytest.mark.asyncio
async def test_missing_conversation_id_returns_400(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_set(
        _FakeConn(), {"id": "r6", "enabled": True}
    )
    assert result["type"] == "gilbert.error"
    assert result.get("code") == 400
