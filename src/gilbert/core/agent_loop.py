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
from dataclasses import dataclass, field
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
    raise NotImplementedError
