"""AI backend interface — provider-agnostic AI conversation API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult


class AIBackendError(RuntimeError):
    """Raised by an ``AIBackend`` when the upstream provider rejects a request.

    Backends should raise this with a user-legible ``message`` (ideally the
    upstream error reason) so that callers like the chat handler can surface
    it to the end user instead of opaque HTTP status text.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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

    Shared-conversation fields (optional):
    - author_id / author_name: who sent this message
    - visible_to: list of user_ids who can see it (None = everyone)
    """

    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    author_id: str = ""
    author_name: str = ""
    visible_to: list[str] | None = None


@dataclass(frozen=True)
class AIRequest:
    """Parameters for a single AI backend call."""

    messages: list[Message]
    system_prompt: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)


@dataclass(frozen=True)
class AIResponse:
    """Result from a single AI backend call (one round, not the full loop)."""

    message: Message
    model: str
    stop_reason: StopReason = StopReason.END_TURN
    usage: TokenUsage | None = None


@dataclass
class AIContextProfile:
    """Named profile that controls which tools are available for an AI interaction."""

    name: str
    description: str = ""
    tool_mode: str = "all"  # "all" | "include" | "exclude"
    tools: list[str] = field(default_factory=list)
    tool_roles: dict[str, str] = field(default_factory=dict)


class AIBackend(ABC):
    """Abstract AI backend — provider-agnostic.

    Mirrors TTSBackend: initialize/close lifecycle, plus a generate method
    for single-round completion. The agentic loop is handled by AIService,
    not here.
    """

    _registry: dict[str, type["AIBackend"]] = {}
    backend_name: str = ""
    """Short identifier used in config (e.g., ``"anthropic"``)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            AIBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["AIBackend"]]:
        """Return ``{name: class}`` for all registered backends."""
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        """Describe backend-specific configuration parameters.

        Returned params are included in the owning service's config under
        the ``settings`` namespace. Override in concrete backends.
        """
        return []

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
