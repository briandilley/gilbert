"""AI backend interface — provider-agnostic AI conversation API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from gilbert.interfaces.auth import UserContext

# Re-export so existing ``from gilbert.interfaces.ai import FileAttachment``
# imports keep working after the move to ``interfaces/attachments.py``.
__all__ = [
    "AIBackend",
    "AIBackendCapabilities",
    "AIBackendError",
    "AIContextProfile",
    "AIModelProvider",
    "AIProvider",
    "AIRequest",
    "AIResponse",
    "AISamplingProvider",
    "AIToolDiscoveryProvider",
    "ChatTurnResult",
    "FileAttachment",
    "MODEL_TIER_ADVANCED",
    "MODEL_TIER_LIGHT",
    "MODEL_TIER_STANDARD",
    "MODEL_TIERS",
    "Message",
    "MessageRole",
    "ModelInfo",
    "SharedConversationProvider",
    "StopReason",
    "StreamEvent",
    "StreamEventType",
    "TokenUsage",
]

# ── Model tiers ───────────────────────────────────────────────────

MODEL_TIER_LIGHT = "light"
MODEL_TIER_STANDARD = "standard"
MODEL_TIER_ADVANCED = "advanced"
MODEL_TIERS = (MODEL_TIER_LIGHT, MODEL_TIER_STANDARD, MODEL_TIER_ADVANCED)


@dataclass(frozen=True)
class ModelInfo:
    """Describes a model available from an AI backend."""

    id: str
    name: str
    description: str = ""


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
    """Token consumption for a single API call.

    Fields are normalized across providers so they're disjoint: ``input_tokens``
    excludes any tokens served from cache. Backends that receive provider
    responses where prompt counts include cache hits (OpenAI's
    ``prompt_tokens`` includes ``cached_tokens``) must subtract before
    populating ``input_tokens`` here.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    """Tokens written to the provider's prompt cache (Anthropic only).
    Typically billed at ~1.25× the regular input rate."""

    cache_read_tokens: int = 0
    """Tokens read from the provider's prompt cache. Typically billed at
    ~0.1× the regular input rate."""


@dataclass
class Message:
    """A single message in a conversation.

    Fields are progressively filled depending on role:
    - SYSTEM: content only
    - USER: content (+ optional attachments)
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
    attachments: list[FileAttachment] = field(default_factory=list)
    # True when the user cancelled this turn mid-flight via
    # ``chat.message.cancel``. Only meaningful on ASSISTANT rows — the
    # frontend surfaces it as a subtle icon on the turn bubble so it's
    # clear the turn was stopped on purpose, not that it errored.
    interrupted: bool = False
    # Per-round token + cost totals stamped onto every ASSISTANT row by
    # ``AIService._record_round_usage``. Shape:
    # ``{input_tokens, output_tokens, cache_creation_tokens,
    # cache_read_tokens, cost_usd}``. Persisted with the conversation so
    # history replay can surface per-round metrics, and summed across
    # assistant rows to reconstruct ``ChatTurnResult.turn_usage`` totals.
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class AIRequest:
    """Parameters for a single AI backend call."""

    messages: list[Message]
    system_prompt: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    model: str = ""


@dataclass(frozen=True)
class AIResponse:
    """Result from a single AI backend call (one round, not the full loop)."""

    message: Message
    model: str
    stop_reason: StopReason = StopReason.END_TURN
    usage: TokenUsage | None = None


class StreamEventType(StrEnum):
    """Kinds of incremental events a streaming backend can emit.

    A single ``generate_stream()`` call produces a sequence of these
    ending in exactly one ``MESSAGE_COMPLETE``. Text and tool_use events
    are fine-grained — backends that can't do true streaming inherit the
    default fallback in ``AIBackend.generate_stream`` which yields a
    single ``MESSAGE_COMPLETE`` and nothing else.
    """

    TEXT_DELTA = "text_delta"
    """A chunk of assistant text arrived. Append ``StreamEvent.text`` to
    the live buffer; do not treat as a full message."""

    TOOL_CALL_START = "tool_call_start"
    """The model began emitting a tool_use block. Carries the tool's
    ``tool_call_id`` and ``tool_name``; arguments arrive via subsequent
    ``TOOL_CALL_DELTA`` events and are final at ``TOOL_CALL_END``."""

    TOOL_CALL_DELTA = "tool_call_delta"
    """An incremental slice of a tool_use block's JSON input. Concatenate
    ``partial_json`` onto the running buffer for the matching
    ``tool_call_id``."""

    TOOL_CALL_END = "tool_call_end"
    """A tool_use block finished. The accumulated JSON is ready to parse
    into final ``ToolCall.arguments`` — handled inside the backend."""

    MESSAGE_COMPLETE = "message_complete"
    """The turn's full response is ready. ``StreamEvent.response`` carries
    a fully-populated ``AIResponse`` (text, tool_calls, stop_reason,
    usage). This is always the last event a stream emits, even for
    backends that don't implement true streaming."""


@dataclass(frozen=True)
class StreamEvent:
    """One event in a provider-neutral streaming response.

    See ``StreamEventType`` for the event vocabulary. Not every field is
    meaningful for every event — e.g. ``text`` is set for ``TEXT_DELTA``
    and empty otherwise, ``response`` is set for ``MESSAGE_COMPLETE`` and
    ``None`` otherwise. The frozen dataclass lets backends construct
    these cheaply and pass them through without defensive copies.
    """

    type: StreamEventType
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    partial_json: str = ""
    response: AIResponse | None = None


@dataclass(frozen=True)
class AIBackendCapabilities:
    """Feature flags a backend advertises so core code can branch cleanly.

    Defaults are conservative: a backend that doesn't override
    ``AIBackend.capabilities()`` is assumed to support neither streaming
    nor multimodal user-side attachments. The ``AIService`` agentic loop
    reads this to decide whether to iterate ``generate_stream()`` (which
    still works on non-streaming backends via the default fallback, but
    there's no point publishing text_delta events from a fallback that
    only emits ``MESSAGE_COMPLETE``).
    """

    streaming: bool = False
    """Backend emits real incremental ``TEXT_DELTA`` / ``TOOL_CALL_*``
    events from ``generate_stream``. When ``False``, the fallback
    implementation yields a single ``MESSAGE_COMPLETE`` event after
    ``generate()`` finishes."""

    attachments_user: bool = False
    """Backend can consume user-side multimodal ``FileAttachment`` blocks
    (images, documents, text) in ``AIRequest.messages``."""

    parallel_tool_calls: bool = False
    """Backend reliably emits multiple ``tool_use`` blocks in a single
    ``AIResponse`` when the model decides tools can run in parallel, and
    the backend's streaming parser correctly assembles all of them. When
    ``False``, the agentic loop still works — the model will simply emit
    one tool call per round and results are awaited serially. Turn this
    on only after verifying the backend's ``generate_stream`` handles
    concurrent ``tool_use`` blocks (not just the first one)."""



@dataclass
class AIContextProfile:
    """Named profile controlling tools, backend, and model for an AI interaction."""

    name: str
    description: str = ""
    tool_mode: str = "all"  # "all" | "include" | "exclude"
    tools: list[str] = field(default_factory=list)
    tool_roles: dict[str, str] = field(default_factory=dict)
    backend: str = ""
    model: str = ""


class AIBackend(ABC):
    """Abstract AI backend — provider-agnostic.

    Mirrors TTSBackend: initialize/close lifecycle, plus a generate method
    for single-round completion. The agentic loop is handled by AIService,
    not here.
    """

    _registry: dict[str, type[AIBackend]] = {}
    backend_name: str = ""
    """Short identifier used in config (e.g., ``"anthropic"``)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            AIBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[AIBackend]]:
        """Return ``{name: class}`` for all registered backends."""
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
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

    def available_models(self) -> list[ModelInfo]:
        """Return models this backend supports.

        Override in subclasses to advertise the models available for
        selection.  The default returns an empty list, meaning the
        backend uses whatever model it was initialized with and does
        not support per-request model switching.
        """
        return []

    def capabilities(self) -> AIBackendCapabilities:
        """Describe what this backend can do.

        Override in subclasses that implement true streaming, multimodal
        user attachments, etc. The default returns a capabilities object
        with every flag ``False``, which is the safe assumption for any
        backend that hasn't opted in.
        """
        return AIBackendCapabilities()

    async def generate_stream(
        self,
        request: AIRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream incremental events for one model round.

        The default implementation is a **synchronous-equivalent** fallback
        that calls ``generate()`` and yields a single ``MESSAGE_COMPLETE``
        event once the full response is in hand. Backends that support
        real streaming (SSE, websockets, etc.) should override this to
        emit ``TEXT_DELTA`` / ``TOOL_CALL_*`` events as chunks arrive, and
        still finish with exactly one ``MESSAGE_COMPLETE`` whose
        ``response`` field carries the fully-assembled ``AIResponse``.

        The ``AIService`` agentic loop is driven by this method
        unconditionally — it iterates events, forwards text deltas to
        the event bus for live frontend rendering, and reads the final
        ``response`` from ``MESSAGE_COMPLETE`` to decide whether to
        execute tools or break the loop. Non-streaming backends therefore
        cost nothing more than a single ``generate()`` call plus one
        event yield.
        """
        response = await self.generate(request)
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            response=response,
        )


class ChatTurnResult(NamedTuple):
    """Result of a full AI chat turn.

    Returned by ``AIService.chat()`` and described on the ``AIProvider``
    protocol. It's a ``NamedTuple`` (and not a frozen dataclass) so callers
    can still do ``text, conv_id, ui, tu, atts, rounds = await ai.chat(...)``
    the way they always have, while new callers can use attribute access
    (``result.attachments``) for clarity.

    Fields:

    - ``response_text``: the assistant's final textual reply, already
      collapsed across any max_tokens continuation rounds.
    - ``conversation_id``: the persistent conversation ID (UUID string).
    - ``ui_blocks``: serialized ``UIBlock`` dicts produced by tool calls
      during this turn, already tagged with ``response_index``.
    - ``tool_usage``: per-tool-call summaries for the UI's "what did it
      do" strip. Entries with ``is_error=True`` include the error reason.
    - ``attachments``: files the assistant wants the user to see on this
      turn (tool-produced PDFs, images, spreadsheets, …). May be empty.
      Inline mode carries the bytes; workspace-reference mode carries
      ``(workspace_skill, workspace_path)`` pointing at a file on disk
      that the frontend fetches via ``skills.workspace.download`` when
      the user clicks download.
    - ``rounds``: structured per-round breakdown of the AI's intermediate
      thinking, used by the frontend's turn-bubble UI to render every
      round's reasoning + tool calls in one cohesive card. Each entry
      is ``{reasoning: str, tools: [{tool_call_id, tool_name, arguments,
      result, is_error}, ...]}``. The final end-turn response is NOT
      included here — it's the ``response_text`` + ``attachments`` fields
      above. May be empty for turns that answered in a single round with
      no tool use.
    - ``interrupted``: True when the user stopped the turn mid-flight
      via ``chat.message.cancel``. Partial state (completed rounds,
      the user message, and anything persisted so far) is preserved;
      the frontend renders a subtle "interrupted" indicator on the
      turn bubble so it's clear the answer didn't finish organically.
    - ``model``: the model ID that actually handled this turn (echoed
      from the backend's AIResponse).
    """

    response_text: str
    conversation_id: str
    ui_blocks: list[dict[str, Any]]
    tool_usage: list[dict[str, Any]]
    attachments: list[FileAttachment]
    rounds: list[dict[str, Any]]
    interrupted: bool = False
    model: str = ""
    turn_usage: dict[str, Any] | None = None
    """Aggregate token + cost totals for the whole turn (summed over every
    AI round including the final end_turn round). Shape:
    ``{input_tokens, output_tokens, cache_creation_tokens,
    cache_read_tokens, cost_usd, rounds}``. ``None`` when no rounds ran
    (slash-command short-circuit, pre-AI errors)."""


@runtime_checkable
class AIProvider(Protocol):
    """Protocol for services providing conversational AI capabilities.

    Callers resolve this via ``resolver.get_capability("ai")`` and
    ``isinstance(svc, AIProvider)`` to type-narrow before invoking
    the ``chat`` entry point. Kept deliberately narrow — only the
    fields the existing greeting/roast/etc callers actually use. If
    a new caller needs another method, add it here rather than
    casting or using ``getattr``.
    """

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        user_ctx: UserContext | None = None,
        system_prompt: str | None = None,
        ai_call: str | None = None,
        attachments: list[FileAttachment] | None = None,
        model: str = "",
        backend_override: str = "",
        ai_profile: str = "",
        max_tool_rounds: int | None = None,
        between_rounds_callback: Any = None,
        mid_round_interrupt: Any = None,
    ) -> ChatTurnResult:
        """Run a full AI chat turn. See ``ChatTurnResult`` for the shape.

        ``backend_override`` forces a specific backend name (e.g.
        ``"anthropic"``); empty string means "resolve from profile or
        fall through to default". The distinct name keeps the method
        body free of shadowing between the external-facing string and
        the internal resolved ``AIBackend`` instance.

        ``max_tool_rounds`` overrides the service-level
        ``ai.settings.max_tool_rounds`` for this call only — useful for
        autonomous-agent runs that need a higher cap than the
        human-in-the-loop default.

        ``between_rounds_callback`` is an optional async callable
        ``() -> list[Message]`` invoked between each tool round (after
        round 0). It may return a list of ``Message`` objects to inject
        into the in-memory message list at that point. Used by the
        autonomous-agent service to deliver mid-run user messages the
        user typed while the run was already in flight, so the model
        sees them on the next round rather than waiting for the run to
        finish.

        ``mid_round_interrupt`` is an optional sync callable
        ``() -> bool`` checked between tool-call execution groups
        within a single round. When it returns ``True``, the remaining
        un-run tool calls in the current round receive stub
        ``ToolResult`` rows and execution returns early. Used by the
        AgentService to interrupt a busy run when an ``urgent`` peer
        signal arrives. ``None`` means "never interrupt"; a callback
        that always returns ``False`` is bit-identical.
        """
        ...


@runtime_checkable
class SharedConversationProvider(Protocol):
    """Protocol for the AI service's shared-conversation listing.

    The WebSocket handshake uses this to seed each connection's set of
    shared rooms without importing the concrete ``AIService`` class.
    """

    async def list_shared_conversations(
        self,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class AIModelProvider(Protocol):
    """Protocol for querying enabled AI models.

    Used by ``ConfigurationService`` to resolve dynamic choices for
    model tier config params without importing the concrete AI service.
    """

    def get_enabled_models(self) -> list[ModelInfo]:
        """Return the models currently enabled on the active backend."""
        ...


@runtime_checkable
class AISamplingProvider(Protocol):
    """Protocol for one-shot AI completion (no conversation, no tool loop).

    Used by ``MCPService`` to service ``sampling/createMessage`` requests
    from remote MCP servers without importing the concrete AI service.
    """

    def has_profile(self, name: str) -> bool:
        """Return True if a profile with this name exists."""
        ...

    async def complete_one_shot(
        self,
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
        tools_override: list[ToolDefinition] | None = None,
    ) -> AIResponse:
        ...


@runtime_checkable
class AIToolDiscoveryProvider(Protocol):
    """Protocol for filtered AI tool discovery.

    Used by ``MCPServerService`` to enumerate the tools an external MCP
    client can see (profile + RBAC applied) without importing the
    concrete AI service.
    """

    def discover_tools(
        self,
        *,
        user_ctx: UserContext,
        profile_name: str | None = None,
    ) -> dict[str, Any]:
        ...
