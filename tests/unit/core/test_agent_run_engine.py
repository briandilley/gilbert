"""Unit tests for the shared AgentRunEngine."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.agent_run import AgentRunEngine, RunSpec
from gilbert.interfaces.ai import ChatTurnResult


class _FakeAI:
    """Records chat() kwargs; returns a scripted sequence of response texts."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._texts = list(texts or ["hello"])

    async def chat(self, **kw: Any) -> ChatTurnResult:
        self.calls.append(kw)
        text = self._texts.pop(0) if self._texts else ""
        return ChatTurnResult(
            response_text=text,
            conversation_id=kw.get("conversation_id") or "conv-1",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )


class _RaisingAI:
    async def chat(self, **kw: Any) -> ChatTurnResult:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_engine_passes_spec_to_chat() -> None:
    ai = _FakeAI()
    spec = RunSpec(
        system_prompt="sys",
        user_message="do it",
        ai_profile="standard",
        tool_filter=("all", []),
        max_rounds=7,
        headless=True,
        ai_call="subagent.x",
        source="subagent",
    )
    res = await AgentRunEngine().run(spec, ai=ai, conversation_id="c1")
    assert res.text == "hello"
    call = ai.calls[0]
    assert call["system_prompt"] == "sys"
    assert call["user_message"] == "do it"
    assert call["ai_profile"] == "standard"
    assert call["max_tool_rounds"] == 7
    assert call["headless"] is True
    assert call["tool_filter"] == ("all", [])
    assert call["ai_call"] == "subagent.x"


@pytest.mark.asyncio
async def test_wall_clock_zero_stops_immediately() -> None:
    ai = _FakeAI()
    spec = RunSpec(system_prompt="s", max_wall_clock_s=0.0)
    res = await AgentRunEngine().run(spec, ai=ai, conversation_id="c1")
    stop = ai.calls[0]["should_stop_callback"]
    assert stop is not None and stop() is True
    assert res.was_stopped is True


@pytest.mark.asyncio
async def test_no_wall_clock_passes_caller_stop_through() -> None:
    ai = _FakeAI()
    sentinel_called = {"n": 0}

    def caller_stop() -> bool:
        sentinel_called["n"] += 1
        return False

    spec = RunSpec(system_prompt="s", should_stop_callback=caller_stop)
    await AgentRunEngine().run(spec, ai=ai, conversation_id="c1")
    assert ai.calls[0]["should_stop_callback"] is caller_stop


@pytest.mark.asyncio
async def test_lifecycle_events_started_then_completed() -> None:
    ai = _FakeAI()
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(et: str, payload: dict[str, Any]) -> None:
        events.append((et, payload))

    spec = RunSpec(system_prompt="s", agent_type="quick-answer", user_message="q")
    await AgentRunEngine().run(
        spec, ai=ai, conversation_id="c1", subagent_id="sid1", on_event=on_event
    )
    assert [e[0] for e in events] == [
        "chat.stream.subagent_started",
        "chat.stream.subagent_completed",
    ]
    assert events[0][1]["subagent_id"] == "sid1"
    assert events[0][1]["agent_type"] == "quick-answer"
    assert events[0][1]["subagent_conversation_id"] == "c1"
    assert events[0][1]["query"] == "q"


@pytest.mark.asyncio
async def test_lifecycle_stopped_event_when_stopped() -> None:
    ai = _FakeAI()
    events: list[str] = []

    async def on_event(et: str, payload: dict[str, Any]) -> None:
        events.append(et)

    spec = RunSpec(system_prompt="s", max_wall_clock_s=0.0)
    await AgentRunEngine().run(spec, ai=ai, conversation_id="c1", on_event=on_event)
    assert events[-1] == "chat.stream.subagent_stopped"


@pytest.mark.asyncio
async def test_failed_event_and_reraise() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(et: str, payload: dict[str, Any]) -> None:
        events.append((et, payload))

    spec = RunSpec(system_prompt="s")
    with pytest.raises(RuntimeError, match="boom"):
        await AgentRunEngine().run(spec, ai=_RaisingAI(), on_event=on_event)
    assert events[0][0] == "chat.stream.subagent_started"
    assert events[1][0] == "chat.stream.subagent_failed"
    assert events[1][1]["reason"] == "boom"


@pytest.mark.asyncio
async def test_synthesis_fallback_on_empty() -> None:
    ai = _FakeAI(texts=["x", "FULL ANSWER"])
    spec = RunSpec(system_prompt="s", synthesize_on_empty=True)
    res = await AgentRunEngine().run(spec, ai=ai, conversation_id="c1")
    assert res.text == "FULL ANSWER"
    assert len(ai.calls) == 2
    assert ai.calls[1]["max_tool_rounds"] == 2


@pytest.mark.asyncio
async def test_no_synthesis_when_disabled() -> None:
    ai = _FakeAI(texts=["x", "FULL ANSWER"])
    spec = RunSpec(system_prompt="s", synthesize_on_empty=False)
    res = await AgentRunEngine().run(spec, ai=ai, conversation_id="c1")
    assert res.text == "x"
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_no_synthesis_without_conversation_id() -> None:
    ai = _FakeAI(texts=["x", "FULL ANSWER"])
    spec = RunSpec(system_prompt="s", synthesize_on_empty=True)
    res = await AgentRunEngine().run(spec, ai=ai, conversation_id=None)
    assert res.text == "x"
    assert len(ai.calls) == 1
