"""AI backend interface — provider-agnostic AI conversation API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult


class MessageRole(StrEnum):
    """Roles in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"


class StopReason(StrEnum):
    """Why the AI stopped generating."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption for a single API call."""

    input_tokens: int
    output_tokens: int


@dataclass
class Message:
    """A single message in a conversation.

    Fields are progressively filled depending on role:
    - SYSTEM: content only
    - USER: content only
    - ASSISTANT: content (text reply) + optional tool_calls
    - TOOL_RESULT: tool_results only
    """

    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass(frozen=True)
class AIRequest:
    """Parameters for a single AI backend call."""

    messages: list[Message]
    system_prompt: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.7


@dataclass(frozen=True)
class AIResponse:
    """Result from a single AI backend call (one round, not the full loop)."""

    message: Message
    model: str
    stop_reason: StopReason = StopReason.END_TURN
    usage: TokenUsage | None = None


class AIBackend(ABC):
    """Abstract AI backend — provider-agnostic.

    Mirrors TTSBackend: initialize/close lifecycle, plus a generate method
    for single-round completion. The agentic loop is handled by AIService,
    not here.
    """

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize the backend with provider-specific configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    @abstractmethod
    async def generate(self, request: AIRequest) -> AIResponse:
        """Send a request and return the model's response (single round)."""
        ...
