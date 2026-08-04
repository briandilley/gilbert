"""Tests for AIService.resume_turn — server-initiated coordinator resume."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import _COLLECTION, AIService
from gilbert.interfaces.ai import ChatTurnResult, ConversationResumer
from gilbert.interfaces.auth import UserContext


def _user() -> UserContext:
    return UserContext(user_id="u1", email="u@x.com", display_name="U")


@pytest.mark.asyncio
async def test_resume_turn_drives_one_chat_turn() -> None:
    svc = AIService()
    store: dict[str, dict[str, Any]] = {f"{_COLLECTION}:c1": {"messages": []}}
    backend = AsyncMock()
    backend.get = AsyncMock(side_effect=lambda c, k: store.get(f"{c}:{k}"))
    backend.put = AsyncMock()
    svc._storage = backend
    svc._chat_profile = "standard"

    calls: list[dict[str, Any]] = []

    async def _fake_chat(user_message: str, **kw: Any) -> ChatTurnResult:
        calls.append({"user_message": user_message, **kw})
        return ChatTurnResult(
            response_text="done",
            conversation_id="c1",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )

    svc.chat = _fake_chat  # type: ignore[method-assign]

    assert isinstance(svc, ConversationResumer)
    await svc.resume_turn("c1", _user(), "continue the task")

    assert len(calls) == 1
    assert calls[0]["user_message"] == "continue the task"
    assert calls[0]["conversation_id"] == "c1"
    assert calls[0]["ai_profile"] == "standard"


@pytest.mark.asyncio
async def test_resume_turn_noops_when_conversation_missing() -> None:
    svc = AIService()
    backend = AsyncMock()
    backend.get = AsyncMock(return_value=None)
    svc._storage = backend
    called = False

    async def _fake_chat(*a: Any, **k: Any) -> ChatTurnResult:
        nonlocal called
        called = True
        return ChatTurnResult("", "c1", [], [], [], [])

    svc.chat = _fake_chat  # type: ignore[method-assign]
    await svc.resume_turn("gone", _user(), "x")
    assert called is False


@pytest.mark.asyncio
async def test_conv_turn_lock_is_stable_per_conversation() -> None:
    svc = AIService()
    assert svc._conv_turn_lock("c1") is svc._conv_turn_lock("c1")
    assert svc._conv_turn_lock("c1") is not svc._conv_turn_lock("c2")
