"""Unit tests for MCPService sampling callback.

Covers every gate in ``_on_sampling_request``:

1. Feature flag off → refuse.
2. Stdio transport → refuse (sampling is for remote servers only).
3. Unknown profile → refuse.
4. AI capability missing → refuse.
5. Budget exhausted → refuse.
6. Empty request → refuse.
7. Happy path → returns a ``CreateMessageResult``.
8. Budget accounting consumes the right number of tokens (actual
   backend usage when available, maxTokens fallback otherwise).
9. Budget tracker sliding-window eviction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from mcp import types as mcp_types

from gilbert.core.services.mcp import MCPService, _SamplingBudget
from gilbert.interfaces.ai import (
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    TokenUsage,
)
from gilbert.interfaces.mcp import MCPAuthConfig, MCPServerRecord
from tests.unit.test_mcp_service import FakeACL, FakeStorage


def _record(
    *,
    transport: str = "http",
    allow_sampling: bool = True,
    sampling_profile: str = "standard",
    budget_tokens: int = 1000,
    budget_window: int = 60,
) -> MCPServerRecord:
    return MCPServerRecord(
        id="srv",
        name="Sampling",
        slug="sampling",
        transport=transport,  # type: ignore[arg-type]
        url="https://example.com/mcp" if transport != "stdio" else None,
        command=("true",) if transport == "stdio" else (),
        owner_id="alice",
        allow_sampling=allow_sampling,
        sampling_profile=sampling_profile,
        sampling_budget_tokens=budget_tokens,
        sampling_budget_window_seconds=budget_window,
        auth=MCPAuthConfig() if transport == "stdio" else MCPAuthConfig(),
    )


def _params(
    *,
    messages: list[dict[str, str]] | None = None,
    max_tokens: int = 256,
) -> mcp_types.CreateMessageRequestParams:
    sdk_messages = [
        mcp_types.SamplingMessage(
            role=m["role"],  # type: ignore[arg-type]
            content=mcp_types.TextContent(type="text", text=m["text"]),
        )
        for m in (messages or [{"role": "user", "text": "hello"}])
    ]
    return mcp_types.CreateMessageRequestParams(
        messages=sdk_messages,
        maxTokens=max_tokens,
        systemPrompt="you are a helper",
    )


@dataclass
class _FakeAIService:
    """Minimal stand-in that satisfies the ``AISamplingProvider`` Protocol.
    Records every call so tests can assert the messages, system prompt,
    and profile reached the service intact."""

    _profiles: dict[str, Any]
    response: AIResponse
    calls: list[dict[str, Any]]

    def has_profile(self, name: str) -> bool:
        return name in self._profiles

    async def complete_one_shot(
        self,
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
    ) -> AIResponse:
        self.calls.append(
            {
                "messages": [{"role": m.role.value, "content": m.content} for m in messages],
                "system_prompt": system_prompt,
                "profile_name": profile_name,
                "max_tokens": max_tokens,
            },
        )
        return self.response


class _FakeResolver:
    def __init__(self, caps: dict[str, Any]) -> None:
        self._caps = caps

    def get_capability(self, name: str) -> Any:
        return self._caps.get(name)

    def require_capability(self, name: str) -> Any:
        if name in self._caps:
            return self._caps[name]
        raise LookupError(name)

    def get_all(self, name: str) -> list[Any]:
        return []


def _make_svc(ai_svc: _FakeAIService | None = None) -> MCPService:
    svc = MCPService()
    svc._enabled = True
    svc._storage = FakeStorage()
    svc._acl_svc = FakeACL()
    caps: dict[str, Any] = {}
    if ai_svc is not None:
        caps["ai"] = ai_svc
    svc._resolver = _FakeResolver(caps)  # type: ignore[assignment]
    return svc


def _default_response(
    *,
    text: str = "hi back",
    input_tokens: int = 10,
    output_tokens: int = 20,
    stop: StopReason = StopReason.END_TURN,
) -> AIResponse:
    return AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content=text),
        model="fake-model",
        stop_reason=stop,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class TestSamplingGates:
    @pytest.mark.asyncio
    async def test_refuses_when_disabled(self) -> None:
        svc = _make_svc()
        record = _record(allow_sampling=False)
        result = await svc._on_sampling_request(record, _params())
        assert isinstance(result, mcp_types.ErrorData)
        assert "not enabled" in result.message

    @pytest.mark.asyncio
    async def test_refuses_stdio_transport(self) -> None:
        svc = _make_svc()
        record = _record(transport="stdio")
        result = await svc._on_sampling_request(record, _params())
        assert isinstance(result, mcp_types.ErrorData)
        assert "remote" in result.message.lower()

    @pytest.mark.asyncio
    async def test_refuses_when_ai_service_missing(self) -> None:
        svc = _make_svc()  # no ai cap
        record = _record()
        result = await svc._on_sampling_request(record, _params())
        assert isinstance(result, mcp_types.ErrorData)
        assert "AI service unavailable" in result.message

    @pytest.mark.asyncio
    async def test_refuses_unknown_profile(self) -> None:
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=_default_response(),
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record(sampling_profile="ghost_profile")
        result = await svc._on_sampling_request(record, _params())
        assert isinstance(result, mcp_types.ErrorData)
        assert "ghost_profile" in result.message
        assert ai.calls == []

    @pytest.mark.asyncio
    async def test_refuses_empty_messages(self) -> None:
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=_default_response(),
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record()
        params = mcp_types.CreateMessageRequestParams(messages=[], maxTokens=128)
        result = await svc._on_sampling_request(record, params)
        assert isinstance(result, mcp_types.ErrorData)
        assert "no messages" in result.message
        assert ai.calls == []

    @pytest.mark.asyncio
    async def test_refuses_when_budget_exhausted(self) -> None:
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=_default_response(),
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record(budget_tokens=100)
        # Seed the budget up to its cap so the next call is rejected.
        budget = _SamplingBudget(max_tokens=100, window_seconds=60)
        budget.consume(100)
        svc._sampling_budgets[record.id] = budget

        result = await svc._on_sampling_request(
            record,
            _params(max_tokens=50),
        )
        assert isinstance(result, mcp_types.ErrorData)
        assert "budget exhausted" in result.message
        assert ai.calls == []


class TestSamplingHappyPath:
    @pytest.mark.asyncio
    async def test_success_returns_create_message_result(self) -> None:
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=_default_response(text="hello from Gilbert"),
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record()
        result = await svc._on_sampling_request(
            record,
            _params(
                messages=[
                    {"role": "user", "text": "Say hi"},
                    {"role": "assistant", "text": "Hi there"},
                    {"role": "user", "text": "Now say hello to Gilbert"},
                ],
            ),
        )
        assert isinstance(result, mcp_types.CreateMessageResult)
        assert result.role == "assistant"
        assert result.content.type == "text"  # type: ignore[union-attr]
        assert "hello from Gilbert" in result.content.text  # type: ignore[union-attr]
        assert result.model == "fake-model"

        # The AI call received the full message list, system prompt,
        # and the configured profile name.
        assert len(ai.calls) == 1
        call = ai.calls[0]
        assert call["profile_name"] == "standard"
        assert call["system_prompt"] == "you are a helper"
        assert len(call["messages"]) == 3
        assert call["messages"][0]["role"] == "user"
        assert call["messages"][1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_consumes_actual_token_usage(self) -> None:
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=_default_response(input_tokens=12, output_tokens=8),
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record(budget_tokens=1000)

        await svc._on_sampling_request(record, _params(max_tokens=500))
        budget = svc._sampling_budgets[record.id]
        # 12 + 8 = 20, NOT the 500 maxTokens ceiling — usage should
        # reflect what the backend actually used.
        assert budget.used() == 20

    @pytest.mark.asyncio
    async def test_consumes_max_tokens_when_usage_unavailable(self) -> None:
        response = AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="no usage"),
            model="fake",
            stop_reason=StopReason.END_TURN,
            usage=None,
        )
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=response,
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record(budget_tokens=1000)
        await svc._on_sampling_request(record, _params(max_tokens=200))
        budget = svc._sampling_budgets[record.id]
        # Conservative fallback: when usage is missing, consume the
        # full cap so a broken backend can't bypass the budget.
        assert budget.used() == 200

    @pytest.mark.asyncio
    async def test_max_tokens_stop_reason_maps_to_max(self) -> None:
        ai = _FakeAIService(
            _profiles={"standard": object()},
            response=_default_response(stop=StopReason.MAX_TOKENS),
            calls=[],
        )
        svc = _make_svc(ai)
        record = _record()
        result = await svc._on_sampling_request(record, _params())
        assert isinstance(result, mcp_types.CreateMessageResult)
        assert result.stopReason == "maxTokens"


class TestSamplingBudget:
    def test_sliding_window_evicts_old_events(self) -> None:
        import time

        budget = _SamplingBudget(max_tokens=100, window_seconds=1.0)
        budget.consume(60)
        assert budget.used() == 60
        # Monkey-patch the deque with stale events to simulate the
        # sliding window without waiting.
        old_ts = time.monotonic() - 5.0
        budget._events.clear()
        budget._events.append((old_ts, 60))
        # ``used`` prunes before summing.
        assert budget.used() == 0
        assert budget.can_admit(100) is True

    def test_can_admit_rejects_over_cap(self) -> None:
        budget = _SamplingBudget(max_tokens=100, window_seconds=60)
        budget.consume(80)
        assert budget.can_admit(21) is False
        assert budget.can_admit(20) is True
