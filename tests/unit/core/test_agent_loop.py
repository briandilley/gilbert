"""Unit tests for ``gilbert.core.agent_loop.run_loop``.

The fake backend takes a scripted list of ``(events, capabilities_kwargs)``
tuples — one per ``generate_stream`` call. Tests assemble scripts that
exercise specific loop behaviors (END_TURN, tool calls, budget hits,
etc.) and assert against the returned ``LoopResult``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from gilbert.core.agent_loop import (
    LoopResult,
    LoopStopReason,
    ToolHandler,
    run_loop,
)
from gilbert.interfaces.ai import (
    AIBackend,
    AIBackendCapabilities,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    StreamEvent,
    StreamEventType,
    TokenUsage,
)
from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult


def _msg_complete(
    *,
    text: str = "",
    tool_calls: list[ToolCall] | None = None,
    stop_reason: StopReason = StopReason.END_TURN,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> StreamEvent:
    """Build a single MESSAGE_COMPLETE event for the fake backend script."""
    return StreamEvent(
        type=StreamEventType.MESSAGE_COMPLETE,
        response=AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content=text,
                tool_calls=tool_calls or [],
            ),
            model="fake",
            stop_reason=stop_reason,
            usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
    )


class FakeAIBackend(AIBackend):
    """Backend that replays a pre-scripted list of stream events per round."""

    backend_name = ""  # don't register

    def __init__(
        self,
        scripts: list[list[StreamEvent]],
        *,
        parallel_tool_calls: bool = False,
        streaming: bool = True,
        raise_on_round: int | None = None,
    ) -> None:
        self._scripts = scripts
        self._round = 0
        self._caps = AIBackendCapabilities(
            streaming=streaming,
            parallel_tool_calls=parallel_tool_calls,
        )
        self._raise_on_round = raise_on_round
        self.requests_seen: list[AIRequest] = []

    async def initialize(self, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    def capabilities(self) -> AIBackendCapabilities:
        return self._caps

    async def generate(self, request: AIRequest) -> AIResponse:
        # Not used — run_loop calls generate_stream.
        raise NotImplementedError

    async def generate_stream(self, request: AIRequest) -> AsyncIterator[StreamEvent]:
        if self._raise_on_round is not None and self._round == self._raise_on_round:
            raise RuntimeError("scripted backend failure")
        self.requests_seen.append(request)
        if self._round >= len(self._scripts):
            raise AssertionError(
                f"FakeAIBackend out of script: round {self._round}, only "
                f"{len(self._scripts)} round(s) scripted"
            )
        events = self._scripts[self._round]
        self._round += 1
        for ev in events:
            yield ev


# pytest-asyncio convention used elsewhere in the repo
pytestmark = pytest.mark.asyncio


async def test_single_end_turn_round_terminates_immediately() -> None:
    backend = FakeAIBackend(scripts=[[_msg_complete(text="hello")]])
    initial = [Message(role=MessageRole.USER, content="hi")]

    result = await run_loop(
        backend=backend,
        system_prompt="you are a test bot",
        messages=initial,
        tools={},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    assert result.final_message.content == "hello"
    assert result.rounds_used == 1
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.error is None
    # full_message_history = initial + assistant
    assert len(result.full_message_history) == 2
    assert result.full_message_history[0] is initial[0]
    assert result.full_message_history[1].role == MessageRole.ASSISTANT


async def test_tool_call_round_then_end_turn() -> None:
    tool_def = ToolDefinition(
        name="echo",
        description="Echo the input",
        parameters=[],
    )
    invocations: list[dict[str, Any]] = []

    async def echo_handler(args: dict[str, Any]) -> str:
        invocations.append(args)
        return f"echoed: {args.get('text', '')}"

    round0 = [
        _msg_complete(
            text="let me echo",
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="echo", arguments={"text": "hi"})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="done")]

    backend = FakeAIBackend(scripts=[round0, round1])

    result = await run_loop(
        backend=backend,
        system_prompt="you are a test bot",
        messages=[Message(role=MessageRole.USER, content="hi")],
        tools={"echo": (tool_def, echo_handler)},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    assert result.rounds_used == 2
    assert invocations == [{"text": "hi"}]

    # History: user, assistant(tool_call), tool_result, assistant(end_turn)
    assert len(result.full_message_history) == 4
    assert result.full_message_history[1].role == MessageRole.ASSISTANT
    assert result.full_message_history[1].tool_calls[0].tool_name == "echo"
    assert result.full_message_history[2].role == MessageRole.TOOL_RESULT
    tr = result.full_message_history[2].tool_results[0]
    assert tr.tool_call_id == "t1"
    assert tr.content == "echoed: hi"
    assert tr.is_error is False
    assert result.full_message_history[3].content == "done"


async def test_max_rounds_terminates_loop() -> None:
    tool_def = ToolDefinition(name="loop", description="loop forever", parameters=[])

    async def loop_handler(args: dict[str, Any]) -> str:
        return "ok"

    # Every round emits another tool call — never END_TURN — so the loop
    # must terminate via MAX_ROUNDS.
    round_with_tool = [
        _msg_complete(
            text="",
            tool_calls=[
                ToolCall(tool_call_id=f"t", tool_name="loop", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    backend = FakeAIBackend(scripts=[round_with_tool] * 3)

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"loop": (tool_def, loop_handler)},
        max_rounds=3,
    )

    assert result.stop_reason == LoopStopReason.MAX_ROUNDS
    assert result.rounds_used == 3


async def test_parallel_tool_calls_dispatched_concurrently() -> None:
    tool_def = ToolDefinition(name="slow", description="slow", parameters=[])
    invocation_log: list[str] = []
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_handler(args: dict[str, Any]) -> str:
        invocation_log.append(f"start:{args['n']}")
        started.set()
        await proceed.wait()
        invocation_log.append(f"end:{args['n']}")
        return f"r{args['n']}"

    round0 = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="slow", arguments={"n": 1}),
                ToolCall(tool_call_id="t2", tool_name="slow", arguments={"n": 2}),
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="ok")]

    backend = FakeAIBackend(
        scripts=[round0, round1],
        parallel_tool_calls=True,
    )

    async def driver() -> LoopResult:
        return await run_loop(
            backend=backend,
            system_prompt="x",
            messages=[Message(role=MessageRole.USER, content="go")],
            tools={"slow": (tool_def, slow_handler)},
            max_rounds=10,
        )

    task = asyncio.create_task(driver())
    # Give run_loop a chance to enter the tool dispatch and start both
    await asyncio.wait_for(started.wait(), timeout=1.0)
    # Both should be started before either ends
    assert sorted(x for x in invocation_log if x.startswith("start:")) == [
        "start:1",
        "start:2",
    ]
    assert not any(x.startswith("end:") for x in invocation_log)
    proceed.set()
    result = await asyncio.wait_for(task, timeout=2.0)

    assert result.stop_reason == LoopStopReason.END_TURN
    # Both ends recorded; order between them is non-deterministic
    assert {x for x in invocation_log if x.startswith("end:")} == {"end:1", "end:2"}


async def test_tool_exception_becomes_error_tool_result_and_loop_continues() -> None:
    tool_def = ToolDefinition(name="boom", description="boom", parameters=[])

    async def boom_handler(args: dict[str, Any]) -> str:
        raise RuntimeError("kaboom")

    round0 = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="boom", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="recovered")]

    backend = FakeAIBackend(scripts=[round0, round1])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"boom": (tool_def, boom_handler)},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    assert result.error is None
    # The tool result message has is_error=True and a "tool failed" content
    tr_msg = result.full_message_history[2]
    assert tr_msg.role == MessageRole.TOOL_RESULT
    tr = tr_msg.tool_results[0]
    assert tr.is_error is True
    assert "tool failed" in tr.content
    assert "kaboom" in tr.content


async def test_backend_exception_returns_error_loop_result() -> None:
    backend = FakeAIBackend(scripts=[[]], raise_on_round=0)

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.ERROR
    assert isinstance(result.error, RuntimeError)
    assert "scripted backend failure" in str(result.error)
    # Loop ran one round (the failing one) and bailed
    assert result.rounds_used == 1


async def test_wall_clock_budget_exceeded_between_rounds() -> None:
    """The first round completes; the loop checks wall-clock before the
    next round starts and terminates with WALL_CLOCK.
    """
    tool_def = ToolDefinition(name="slow", description="slow", parameters=[])

    async def slow_handler(args: dict[str, Any]) -> str:
        # Burn at least 0.05s so the deadline check after this round trips
        await asyncio.sleep(0.05)
        return "ok"

    round_with_tool = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t", tool_name="slow", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    # Two scripted rounds — but wall-clock should kill us before round 2.
    backend = FakeAIBackend(scripts=[round_with_tool, round_with_tool])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"slow": (tool_def, slow_handler)},
        max_rounds=10,
        max_wall_clock_s=0.01,  # already exceeded after round 1's tool ran
    )

    assert result.stop_reason == LoopStopReason.WALL_CLOCK
    assert result.rounds_used == 1


async def test_token_budget_exceeded_between_rounds() -> None:
    """Round 1 records 60 tokens (10+5 default usage from `_msg_complete` is
    overridden here); the loop checks the cumulative total before the next
    round and terminates with TOKEN_BUDGET.
    """
    tool_def = ToolDefinition(name="loop", description="loop", parameters=[])

    async def handler(args: dict[str, Any]) -> str:
        return "ok"

    round_with_tool = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t", tool_name="loop", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
            input_tokens=40,
            output_tokens=20,
        )
    ]
    backend = FakeAIBackend(scripts=[round_with_tool, round_with_tool])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"loop": (tool_def, handler)},
        max_rounds=10,
        max_tokens=50,  # 60 cumulative > 50 → bail before round 2
    )

    assert result.stop_reason == LoopStopReason.TOKEN_BUDGET
    assert result.rounds_used == 1
    assert result.tokens_in == 40
    assert result.tokens_out == 20
