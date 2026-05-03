"""Pure async loop primitive for AI tool-use loops.

Used by both ``AIService.chat()`` (after refactor) and the upcoming
``AutonomousAgentService.run_goal()``. The loop drives one
``AIBackend.generate_stream()`` per round, consumes events to find
``MESSAGE_COMPLETE``, and on ``TOOL_USE`` executes tools (in parallel
when the backend supports it) and iterates. Termination: ``END_TURN``,
backend ``MAX_TOKENS``, max-rounds, wall-clock budget, token budget,
or unrecoverable error.

This module is intentionally service-free — no event bus, no scheduler,
no storage. Streaming/persistence/UI concerns belong to the caller.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
    StreamEventType,
)
from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult


class LoopStopReason(StrEnum):
    """Why ``run_loop`` returned.

    Distinct from ``StopReason`` (which is per-round, backend-emitted)
    because the loop has its own termination conditions on top of what
    the model decides.
    """

    END_TURN = "end_turn"
    """Model emitted END_TURN — natural completion."""

    MAX_ROUNDS = "max_rounds"
    """Hit the loop's per-call round cap before END_TURN."""

    WALL_CLOCK = "wall_clock"
    """Wall-clock deadline elapsed between rounds."""

    TOKEN_BUDGET = "token_budget"
    """Cumulative tokens (input + output across all rounds) exceeded the cap."""

    MAX_TOKENS = "max_tokens"
    """Backend hit its per-round output cap on the last round. The loop does
    not implement continuation in this primitive; callers that need
    chat-style continuation can wrap ``run_loop`` and re-invoke it."""

    ERROR = "error"
    """Unrecoverable error during the loop. ``LoopResult.error`` holds it."""


ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]
"""Async callable that executes a tool. Takes the parsed argument dict,
returns the tool's textual result. Exceptions are caught by the loop
and formatted as error tool-result messages."""


@dataclass
class LoopResult:
    """The outcome of one ``run_loop`` call."""

    final_message: Message
    """The last assistant ``Message`` from the loop. For successful
    completion this is the END_TURN response; for budget/error
    terminations it's whatever the last round produced (may be empty)."""

    full_message_history: list[Message]
    """Initial messages + every assistant + every tool_result message
    accumulated by the loop. Caller persists this verbatim if it cares
    to record the run."""

    stop_reason: LoopStopReason

    rounds_used: int
    tokens_in: int
    tokens_out: int

    error: Exception | None = None
    """Set when ``stop_reason == LoopStopReason.ERROR``. Otherwise None."""


async def run_loop(
    *,
    backend: AIBackend,
    system_prompt: str,
    messages: list[Message],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
    max_rounds: int,
    max_wall_clock_s: float | None = None,
    max_tokens: int | None = None,
    model: str = "",
) -> LoopResult:
    """Drive one AI tool-use loop end-to-end.

    See module docstring for the contract. All arguments are keyword-only —
    every additional knob in the future should also be keyword-only so the
    call sites stay readable.
    """
    history = list(messages)
    tokens_in = 0
    tokens_out = 0
    final_message = Message(role=MessageRole.ASSISTANT, content="")
    rounds_used = 0
    deadline: float | None = (
        time.monotonic() + max_wall_clock_s if max_wall_clock_s is not None else None
    )

    for _ in range(max_rounds):
        if deadline is not None and time.monotonic() >= deadline:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.WALL_CLOCK,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        if max_tokens is not None and (tokens_in + tokens_out) >= max_tokens:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.TOKEN_BUDGET,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        rounds_used += 1
        request = AIRequest(
            messages=history,
            system_prompt=system_prompt,
            tools=[t[0] for t in tools.values()],
            model=model,
        )
        response = None
        try:
            async for ev in backend.generate_stream(request):
                if ev.type == StreamEventType.MESSAGE_COMPLETE:
                    response = ev.response
        except Exception as exc:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.ERROR,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=exc,
            )
        if response is None:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.ERROR,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=RuntimeError("backend stream ended without MESSAGE_COMPLETE"),
            )

        if response.usage:
            tokens_in += response.usage.input_tokens
            tokens_out += response.usage.output_tokens
        final_message = response.message
        history.append(response.message)

        if response.stop_reason == StopReason.END_TURN:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.END_TURN,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        if response.stop_reason == StopReason.MAX_TOKENS:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.MAX_TOKENS,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        if response.stop_reason == StopReason.TOOL_USE:
            if not response.message.tool_calls:
                return LoopResult(
                    final_message=final_message,
                    full_message_history=history,
                    stop_reason=LoopStopReason.ERROR,
                    rounds_used=rounds_used,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    error=RuntimeError(
                        "backend returned TOOL_USE stop reason with no tool_calls"
                    ),
                )
            if backend.capabilities().parallel_tool_calls and len(response.message.tool_calls) > 1:
                tool_results = await _execute_tool_calls_parallel(
                    response.message.tool_calls, tools
                )
            else:
                tool_results = await _execute_tool_calls_sequential(
                    response.message.tool_calls, tools
                )
            history.append(Message(role=MessageRole.TOOL_RESULT, tool_results=tool_results))
            continue

        # Any other stop_reason (e.g. an enum we don't yet handle) falls
        # through to MAX_ROUNDS via the post-loop return.
        break

    return LoopResult(
        final_message=final_message,
        full_message_history=history,
        stop_reason=LoopStopReason.MAX_ROUNDS,
        rounds_used=rounds_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


async def _execute_tool_calls_sequential(
    tool_calls: list[ToolCall],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
) -> list[ToolResult]:
    """Execute tool calls one at a time, in order. Errors are caught and
    formatted as error tool results so the loop continues with whatever
    the agent decides to do next.
    """
    results: list[ToolResult] = []
    for tc in tool_calls:
        result = await _invoke_one_tool(tc, tools)
        results.append(result)
    return results


async def _invoke_one_tool(
    tc: ToolCall,
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
) -> ToolResult:
    pair = tools.get(tc.tool_name)
    if pair is None:
        return ToolResult(
            tool_call_id=tc.tool_call_id,
            content=f"tool not found: {tc.tool_name}",
            is_error=True,
        )
    _, handler = pair
    try:
        content = await handler(tc.arguments)
    except Exception as exc:  # tools failing must not crash the loop
        return ToolResult(
            tool_call_id=tc.tool_call_id,
            content=f"tool failed: {exc!r}",
            is_error=True,
        )
    return ToolResult(
        tool_call_id=tc.tool_call_id,
        content=content,
        is_error=False,
    )


async def _execute_tool_calls_parallel(
    tool_calls: list[ToolCall],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
) -> list[ToolResult]:
    """Execute tool calls concurrently. Each invocation is independently
    wrapped in error handling so one failure doesn't poison the others.
    Result order matches the input order so ``zip(tool_calls, results)``
    is meaningful.
    """
    return await asyncio.gather(*(_invoke_one_tool(tc, tools) for tc in tool_calls))
