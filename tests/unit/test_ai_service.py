"""Tests for AIService — agentic loop, tool discovery, conversation persistence."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import AIService, _parse_frame_attachments
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    FileAttachment,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)

# --- Stubs ---


class StubAIBackend(AIBackend):
    """In-memory AI backend that returns predetermined responses."""

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.init_config: dict[str, Any] = {}
        self.requests: list[AIRequest] = []
        self._responses: list[AIResponse] = []
        self._call_idx = 0

    def queue_response(self, response: AIResponse) -> None:
        self._responses.append(response)

    async def initialize(self, config: dict[str, Any]) -> None:
        self.init_config = config
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def generate(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        if self._call_idx >= len(self._responses):
            return AIResponse(
                message=Message(role=MessageRole.ASSISTANT, content="default response"),
                model="stub",
            )
        resp = self._responses[self._call_idx]
        self._call_idx += 1
        return resp


class StubToolProviderService(Service):
    """A service that also implements the ToolProvider protocol."""

    def __init__(self, tools: list[ToolDefinition], results: dict[str, str]) -> None:
        self._tools = tools
        self._results = results

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="stub_tools",
            capabilities=frozenset({"ai_tools"}),
        )

    @property
    def tool_provider_name(self) -> str:
        return "stub_tools"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return list(self._tools)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._results:
            raise KeyError(f"Unknown tool: {name}")
        return self._results[name]


class UIBlockToolProviderService(Service):
    """Tool provider whose execute_tool returns a ToolOutput with UI blocks."""

    def __init__(self, tool_def: ToolDefinition) -> None:
        self._tool_def = tool_def

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ui_tool",
            capabilities=frozenset({"ai_tools"}),
        )

    @property
    def tool_provider_name(self) -> str:
        return "ui_tool"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [self._tool_def]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement

        return ToolOutput(
            text="tool picked something",
            ui_blocks=[
                UIBlock(
                    title="Pick one",
                    elements=[
                        UIElement(type="label", name="info", label="choose"),
                    ],
                ),
            ],
        )


class ErrorToolProviderService(Service):
    """Tool provider that raises on execution."""

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="error_tools",
            capabilities=frozenset({"ai_tools"}),
        )

    @property
    def tool_provider_name(self) -> str:
        return "error_tools"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [ToolDefinition(name="fail_tool", description="Always fails")]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("tool exploded")


# --- Fixtures ---


@pytest.fixture
def stub_backend() -> StubAIBackend:
    return StubAIBackend()


@pytest.fixture
def storage_backend() -> StorageBackend:
    backend = AsyncMock(spec=StorageBackend)
    backend.get = AsyncMock(return_value=None)
    backend.put = AsyncMock()
    return backend


@pytest.fixture
def storage_service(storage_backend: StorageBackend) -> StorageService:
    return StorageService(storage_backend)


@pytest.fixture
def resolver(
    storage_service: StorageService,
) -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(f"No service provides: {cap}")

    def get_cap(cap: str) -> Any:
        try:
            return require_cap(cap)
        except LookupError:
            return None

    def get_all(cap: str) -> list[Any]:
        return []

    mock.require_capability = require_cap
    mock.get_capability = get_cap
    mock.get_all = get_all
    return mock


@pytest.fixture
def ai_service(stub_backend: StubAIBackend) -> AIService:
    svc = AIService()
    svc._backend = stub_backend
    svc._enabled = True
    # Set tunable config directly for testing
    svc._config = {"api_key": "sk-test-key", "max_tokens": 1024, "temperature": 0.5}
    svc._system_prompt = "You are a test assistant."
    svc._max_tool_rounds = 5
    return svc


# --- Service Info ---


def test_service_info(ai_service: AIService) -> None:
    info = ai_service.service_info()
    assert info.name == "ai"
    assert "ai_chat" in info.capabilities
    assert "entity_storage" in info.requires
    assert "ai_tools" in info.optional


# --- Lifecycle ---


async def test_start_initializes_backend(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    await ai_service.start(resolver)
    assert stub_backend.initialized
    assert stub_backend.init_config["api_key"] == "sk-test-key"
    assert stub_backend.init_config["max_tokens"] == 1024


async def test_stop_closes_backend(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    await ai_service.start(resolver)
    await ai_service.stop()
    assert stub_backend.closed


# --- Chat (no tools) ---


async def test_chat_simple_response(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="Hello there!"),
            model="stub",
        )
    )
    await ai_service.start(resolver)

    text, conv_id, _ui, _tu, _atts, _rounds = await ai_service.chat("Hi")
    assert text == "Hello there!"
    assert conv_id  # non-empty UUID string
    assert len(stub_backend.requests) == 1

    req = stub_backend.requests[0]
    assert "You are a test assistant." in req.system_prompt
    assert "You are Gilbert" in req.system_prompt
    assert len(req.messages) == 1
    assert req.messages[0].role == MessageRole.USER
    assert req.messages[0].content == "Hi"


async def test_chat_continues_conversation(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="First reply"),
            model="stub",
        )
    )
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="Second reply"),
            model="stub",
        )
    )
    await ai_service.start(resolver)

    # First message
    _, conv_id, _ui, _tu, _atts, _rounds = await ai_service.chat("Hello")

    # Simulate storage returning the saved conversation
    saved_call = storage_backend.put.call_args  # type: ignore[union-attr]
    saved_data = saved_call[0][2]  # positional arg: data
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    # Second message in same conversation
    text, same_id, _ui, _tu, _atts, _rounds = await ai_service.chat("Follow up", conversation_id=conv_id)
    assert same_id == conv_id
    assert text == "Second reply"

    # Backend should have received 3 messages: user, assistant, user
    req = stub_backend.requests[1]
    assert len(req.messages) == 3
    assert req.messages[0].role == MessageRole.USER
    assert req.messages[1].role == MessageRole.ASSISTANT
    assert req.messages[2].role == MessageRole.USER


# --- Chat (with tools) ---


async def test_chat_with_tool_calls(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    storage_service: StorageService,
    storage_backend: StorageBackend,
) -> None:
    # Set up a tool provider
    tool_def = ToolDefinition(
        name="get_weather",
        description="Get weather",
        parameters=[
            ToolParameter(name="city", type=ToolParameterType.STRING, description="City name"),
        ],
    )
    tool_provider = StubToolProviderService(
        tools=[tool_def],
        results={"get_weather": '{"temp": 72, "condition": "sunny"}'},
    )

    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [tool_provider] if cap == "ai_tools" else []

    # Round 1: AI requests a tool call
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="Let me check the weather.",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc_1",
                        tool_name="get_weather",
                        arguments={"city": "Portland"},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    # Round 2: AI gives final response
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="It's 72F and sunny in Portland!",
            ),
            model="stub",
        )
    )

    await ai_service.start(resolver)
    text, _, _ui, _tu, _atts, _rounds = await ai_service.chat("What's the weather in Portland?")

    assert text == "It's 72F and sunny in Portland!"
    assert len(stub_backend.requests) == 2

    # Verify tool definitions were passed
    assert len(stub_backend.requests[0].tools) == 1
    assert stub_backend.requests[0].tools[0].name == "get_weather"

    # Verify tool result was fed back in second request
    second_req = stub_backend.requests[1]
    tool_result_msg = second_req.messages[-1]
    assert tool_result_msg.role == MessageRole.TOOL_RESULT
    assert len(tool_result_msg.tool_results) == 1
    assert tool_result_msg.tool_results[0].tool_call_id == "tc_1"
    assert "sunny" in tool_result_msg.tool_results[0].content


async def test_chat_ui_block_response_index_skips_empty_assistant_rows(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    storage_service: StorageService,
    storage_backend: StorageBackend,
) -> None:
    """Regression: response_index must reflect visible assistant rows only.

    When a chat turn goes through an agentic round (empty-content assistant
    with tool_calls, then tool_result, then final assistant with content),
    the frontend only ever sees the final row. If the backend counted every
    assistant row the response_index would be too high, leaving blocks
    unanchored at the bottom of the chat. This test pins the correct count.
    """
    tool_def = ToolDefinition(name="picker", description="pick")
    tool_provider = UIBlockToolProviderService(tool_def)

    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [tool_provider] if cap == "ai_tools" else []

    # Round 1: AI requests the tool (empty content, tool_calls set).
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc_pick",
                        tool_name="picker",
                        arguments={},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    # Round 2: AI gives final answer (non-empty content).
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="Here's what I found.",
            ),
            model="stub",
        )
    )

    await ai_service.start(resolver)
    _text, _cid, ui_blocks, _tu, _atts, _rounds = await ai_service.chat("pick something")

    # The call produced one visible assistant bubble, so the block must
    # anchor at response_index=0 — not 1, which would be the result of
    # counting the intermediate empty tool-use row.
    assert len(ui_blocks) == 1
    assert ui_blocks[0]["response_index"] == 0


async def test_chat_ui_block_response_index_across_multiple_turns(
    ai_service: AIService,
    stub_backend: StubAIBackend,
) -> None:
    """A second chat turn that calls a UI-block tool should anchor to its
    own assistant bubble, not the one from the previous turn.
    """
    tool_def = ToolDefinition(name="picker", description="pick")
    tool_provider = UIBlockToolProviderService(tool_def)

    # In-memory storage so the second chat() call sees the first's history.
    _store: dict[str, dict[str, Any]] = {}

    async def _get(collection: str, key: str) -> Any:
        return _store.get(f"{collection}:{key}")

    async def _put(collection: str, key: str, data: dict[str, Any]) -> None:
        _store[f"{collection}:{key}"] = data

    storage_backend = AsyncMock(spec=StorageBackend)
    storage_backend.get = AsyncMock(side_effect=_get)
    storage_backend.put = AsyncMock(side_effect=_put)
    storage_service = StorageService(storage_backend)

    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [tool_provider] if cap == "ai_tools" else []

    # Turn 1: tool call → final answer (2 assistant rows, 1 visible)
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc1",
                        tool_name="picker",
                        arguments={},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="first answer",
            ),
            model="stub",
        )
    )
    # Turn 2: same shape again — another 2 assistant rows, 1 visible
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc2",
                        tool_name="picker",
                        arguments={},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="second answer",
            ),
            model="stub",
        )
    )

    await ai_service.start(resolver)
    _, conv_id, ui_blocks_1, _, _atts, _rounds = await ai_service.chat("first")
    _, _, ui_blocks_2, _, _atts, _rounds = await ai_service.chat(
        "second",
        conversation_id=conv_id,
    )

    # First turn's block → first visible assistant (index 0)
    assert len(ui_blocks_1) == 1
    assert ui_blocks_1[0]["response_index"] == 0
    # Second turn's block → second visible assistant (index 1), NOT 3 or
    # 4 (which would happen if empty rows were counted).
    assert len(ui_blocks_2) == 1
    assert ui_blocks_2[0]["response_index"] == 1


async def test_chat_max_tool_rounds(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    """The agentic loop stops after max_tool_rounds even if AI keeps calling tools."""
    # Queue responses that always request tool calls
    for i in range(10):
        stub_backend.queue_response(
            AIResponse(
                message=Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(
                            tool_call_id=f"tc_{i}",
                            tool_name="unknown_tool",
                            arguments={},
                        )
                    ],
                ),
                model="stub",
                stop_reason=StopReason.TOOL_USE,
            )
        )

    await ai_service.start(resolver)
    text, _, _ui, _tu, _atts, _rounds = await ai_service.chat("loop forever")

    # max_tool_rounds=5, so at most 5 backend calls
    assert len(stub_backend.requests) == 5


# --- Max-Tokens Recovery ---


async def test_max_tokens_text_continues(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    """A text-only MAX_TOKENS response is followed by a continuation turn."""
    ai_service._max_continuation_rounds = 2
    # Round 1: partial text, cut at max_tokens
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="Part one of the answer",
            ),
            model="stub",
            stop_reason=StopReason.MAX_TOKENS,
        )
    )
    # Round 2: the continuation finishes the reply
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="and part two of the answer.",
            ),
            model="stub",
            stop_reason=StopReason.END_TURN,
        )
    )

    await ai_service.start(resolver)
    text, _, _ui, _tu, _atts, _rounds = await ai_service.chat("Give me a long answer")

    # Both chunks should appear in the returned text
    assert "Part one of the answer" in text
    assert "and part two of the answer." in text
    # The service made two backend calls
    assert len(stub_backend.requests) == 2
    # The second request carries the synthetic "please continue" user message
    second_req = stub_backend.requests[1]
    last_msg = second_req.messages[-1]
    assert last_msg.role == MessageRole.USER
    assert "continue" in last_msg.content.lower()


async def test_max_tokens_partial_tool_call_surfaces_error(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    """A MAX_TOKENS response with tool_calls is treated as unrecoverable."""
    # A single response that claims a tool_use but got cut off
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc_broken",
                        tool_name="some_tool",
                        arguments={"partial": "inpu"},  # pretend cut off
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.MAX_TOKENS,
        )
    )

    await ai_service.start(resolver)
    text, _, _ui, tool_usage, _atts, _rounds = await ai_service.chat("do the thing")

    # The loop broke out without executing or retrying
    assert len(stub_backend.requests) == 1
    # A surfacing error entry was added to tool_usage so the frontend can tell
    assert any(tu.get("is_error") for tu in tool_usage)
    assert any(tu.get("tool_name") == "<max_tokens_truncation>" for tu in tool_usage)
    # The reply text explains what happened
    assert "max_tokens" in text.lower() or "cut off" in text.lower()


async def test_max_tokens_continuation_cap(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    """Continuation stops after _max_continuation_rounds even if the model keeps truncating."""
    ai_service._max_continuation_rounds = 2
    # Queue enough MAX_TOKENS responses to exceed the cap
    for i in range(5):
        stub_backend.queue_response(
            AIResponse(
                message=Message(
                    role=MessageRole.ASSISTANT,
                    content=f"chunk {i}",
                ),
                model="stub",
                stop_reason=StopReason.MAX_TOKENS,
            )
        )

    await ai_service.start(resolver)
    text, _, _ui, _tu, _atts, _rounds = await ai_service.chat("keep going")

    # Initial call + 2 continuations = 3 backend calls total
    assert len(stub_backend.requests) == 3
    # The final text is annotated that it's still truncated
    assert "truncated" in text.lower()
    assert "chunk 0" in text
    assert "chunk 1" in text
    assert "chunk 2" in text


async def test_chat_emits_text_delta_events_for_streaming_backend(
    ai_service: AIService,
    resolver: ServiceResolver,
) -> None:
    """A backend that emits TEXT_DELTA stream events causes the AI
    service to publish ``chat.stream.text_delta`` events on the bus."""
    from gilbert.interfaces.ai import (
        AIBackendCapabilities,
        StreamEvent,
        StreamEventType,
    )

    class StreamingBackend(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None:
            pass

        async def close(self) -> None:
            pass

        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(
                message=Message(role=MessageRole.ASSISTANT, content="Hello world"),
                model="stub-stream",
            )

        def capabilities(self) -> AIBackendCapabilities:
            return AIBackendCapabilities(streaming=True, attachments_user=False)

        async def generate_stream(self, request: AIRequest):  # type: ignore[override]
            yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="Hello ")
            yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="world")
            yield StreamEvent(
                type=StreamEventType.MESSAGE_COMPLETE,
                response=AIResponse(
                    message=Message(
                        role=MessageRole.ASSISTANT,
                        content="Hello world",
                    ),
                    model="stub-stream",
                ),
            )

    ai_service._backend = StreamingBackend()

    # Capture published events.
    published: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(event_type: str, data: dict[str, Any]) -> None:
        published.append((event_type, data))

    ai_service._publish_event = fake_publish  # type: ignore[assignment]

    await ai_service.start(resolver)
    result = await ai_service.chat("Hi", user_ctx=UserContext.SYSTEM)

    text_deltas = [d for t, d in published if t == "chat.stream.text_delta"]
    assert len(text_deltas) == 2
    assert text_deltas[0]["text"] == "Hello "
    assert text_deltas[1]["text"] == "world"

    # A round_complete and turn_complete should also have fired.
    round_completes = [d for t, d in published if t == "chat.stream.round_complete"]
    turn_completes = [d for t, d in published if t == "chat.stream.turn_complete"]
    assert len(round_completes) == 1
    assert len(turn_completes) == 1

    # And the final text is still what the backend sent.
    assert result.response_text == "Hello world"


async def test_non_streaming_backend_emits_no_stream_events(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    """A backend whose capabilities report streaming=False produces no
    chat.stream.* events, even though chat() still calls generate_stream
    (via the ABC fallback)."""
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="plain"),
            model="stub",
        )
    )

    published: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(event_type: str, data: dict[str, Any]) -> None:
        published.append((event_type, data))

    ai_service._publish_event = fake_publish  # type: ignore[assignment]

    await ai_service.start(resolver)
    text, *_ = await ai_service.chat("hi")

    assert text == "plain"
    stream_events = [t for t, _ in published if t.startswith("chat.stream.")]
    assert stream_events == []


async def test_tool_attachments_land_on_final_assistant_message(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    storage_service: StorageService,
    storage_backend: StorageBackend,
) -> None:
    """A tool that returns a ToolResult with attachments lands those files
    on the final assistant Message, and the chat() return carries them."""

    class AttachingProvider(Service):
        def __init__(self) -> None:
            self._ref = FileAttachment(
                kind="document",
                name="po.pdf",
                media_type="application/pdf",
                workspace_skill="pdf",
                workspace_path="po-00006567.pdf",
            )

        def service_info(self) -> ServiceInfo:
            return ServiceInfo(
                name="att_tool",
                capabilities=frozenset({"ai_tools"}),
            )

        @property
        def tool_provider_name(self) -> str:
            return "att_tool"

        def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
            return [ToolDefinition(name="make_po", description="Make a PO")]

        async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return ToolResult(
                tool_call_id="",  # AIService fills this from the ToolCall.
                content="Built the PO",
                attachments=(self._ref,),
            )

    provider = AttachingProvider()
    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [provider] if cap == "ai_tools" else []

    # Round 1: AI calls the tool
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="Making the PO.",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc_make",
                        tool_name="make_po",
                        arguments={},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    # Round 2: final answer
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content="Here's your PO.",
            ),
            model="stub",
        )
    )

    await ai_service.start(resolver)
    result = await ai_service.chat("Make a PO")

    # chat() returns attachments on the ChatTurnResult NamedTuple.
    assert len(result.attachments) == 1
    att = result.attachments[0]
    assert att.name == "po.pdf"
    assert att.is_reference
    assert att.workspace_skill == "pdf"
    assert att.workspace_path == "po-00006567.pdf"

    # And they were persisted onto the final assistant message.
    put_call = storage_backend.put.call_args  # type: ignore[union-attr]
    saved = put_call[0][2]
    final_asst = [m for m in saved["messages"] if m["role"] == "assistant"][-1]
    assert "attachments" in final_asst
    assert len(final_asst["attachments"]) == 1
    assert final_asst["attachments"][0]["workspace_path"] == "po-00006567.pdf"


async def test_max_tokens_collapsed_history_drops_synthetic_user(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """After continuation, the persisted history has one user and one merged
    assistant message — the synthetic 'please continue' is stripped."""
    ai_service._max_continuation_rounds = 2
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="first"),
            model="stub",
            stop_reason=StopReason.MAX_TOKENS,
        )
    )
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="second"),
            model="stub",
            stop_reason=StopReason.END_TURN,
        )
    )

    await ai_service.start(resolver)
    await ai_service.chat("tell me")

    # Inspect what was persisted
    put_call = storage_backend.put.call_args  # type: ignore[union-attr]
    saved = put_call[0][2]
    roles = [m["role"] for m in saved["messages"]]
    # Exactly one user row (the original) and one assistant row (merged)
    assert roles.count("user") == 1
    assert roles.count("assistant") == 1
    assistant_msg = next(m for m in saved["messages"] if m["role"] == "assistant")
    assert "first" in assistant_msg["content"]
    assert "second" in assistant_msg["content"]
    # No synthetic continue message survived
    user_msg = next(m for m in saved["messages"] if m["role"] == "user")
    assert "continue" not in user_msg["content"].lower()


# --- Tool Errors ---


async def test_unknown_tool_returns_error_result(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc_bad",
                        tool_name="nonexistent",
                        arguments={},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="Sorry, couldn't do that."),
            model="stub",
        )
    )

    await ai_service.start(resolver)
    text, _, _ui, _tu, _atts, _rounds = await ai_service.chat("Do something impossible")

    # The error result was fed back
    second_req = stub_backend.requests[1]
    tool_result_msg = second_req.messages[-1]
    assert tool_result_msg.tool_results[0].is_error
    assert "unknown tool" in tool_result_msg.tool_results[0].content


async def test_tool_execution_error_returns_error_result(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    storage_service: StorageService,
) -> None:
    error_provider = ErrorToolProviderService()
    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [error_provider] if cap == "ai_tools" else []

    stub_backend.queue_response(
        AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    ToolCall(
                        tool_call_id="tc_err",
                        tool_name="fail_tool",
                        arguments={},
                    )
                ],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        )
    )
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="That failed."),
            model="stub",
        )
    )

    await ai_service.start(resolver)
    text, _, _ui, _tu, _atts, _rounds = await ai_service.chat("Run the bad tool")

    second_req = stub_backend.requests[1]
    tool_result_msg = second_req.messages[-1]
    assert tool_result_msg.tool_results[0].is_error
    assert "tool exploded" in tool_result_msg.tool_results[0].content


# --- Conversation Persistence ---


async def test_conversation_saved_to_storage(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="Saved!"),
            model="stub",
        )
    )
    await ai_service.start(resolver)
    _, conv_id, _ui, _tu, _atts, _rounds = await ai_service.chat("Save this")

    # Find the conversation save call among all put calls (profiles are also seeded)
    conv_calls = [
        c
        for c in storage_backend.put.call_args_list  # type: ignore[union-attr]
        if c[0][0] == "gilbert.ai_conversations"
    ]
    assert len(conv_calls) == 1
    assert conv_calls[0][0][1] == conv_id


# --- History Truncation ---


def test_truncate_history_within_limit(ai_service: AIService) -> None:
    messages = [
        Message(role=MessageRole.USER, content="msg1"),
        Message(role=MessageRole.ASSISTANT, content="reply1"),
    ]
    result = ai_service._truncate_history(messages)
    assert len(result) == 2


def test_truncate_history_preserves_tool_pairs() -> None:
    svc = AIService()
    svc._backend = StubAIBackend()
    svc._enabled = True
    svc._max_history_messages = 3
    messages = [
        Message(role=MessageRole.USER, content="old1"),
        Message(role=MessageRole.ASSISTANT, content="old2"),
        Message(role=MessageRole.USER, content="msg"),
        Message(
            role=MessageRole.ASSISTANT,
            tool_calls=[ToolCall(tool_call_id="tc", tool_name="t", arguments={})],
        ),
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[ToolResult(tool_call_id="tc", content="result")],
        ),
    ]
    result = svc._truncate_history(messages)
    # Last 3 would be: msg, assistant+tool_calls, tool_result
    # tool_result is at index 0 of truncated, so it pulls in the assistant message
    # Actually last 3 = messages[2:5] = [msg, assistant, tool_result]
    # First message is USER, so no adjustment needed
    assert result[0].role == MessageRole.USER


# --- Message Serialization Round-Trip ---


def test_message_serialize_deserialize() -> None:
    original = Message(
        role=MessageRole.ASSISTANT,
        content="Using a tool",
        tool_calls=[
            ToolCall(
                tool_call_id="tc_1",
                tool_name="search",
                arguments={"q": "test"},
            )
        ],
    )
    serialized = AIService._serialize_message(original)
    deserialized = AIService._deserialize_message(serialized)

    assert deserialized.role == original.role
    assert deserialized.content == original.content
    assert len(deserialized.tool_calls) == 1
    assert deserialized.tool_calls[0].tool_call_id == "tc_1"
    assert deserialized.tool_calls[0].tool_name == "search"
    assert deserialized.tool_calls[0].arguments == {"q": "test"}


def test_message_with_attachments_serialize_roundtrip() -> None:
    import base64

    image_payload = base64.b64encode(b"fake png bytes").decode()
    doc_payload = base64.b64encode(b"fake pdf bytes").decode()
    original = Message(
        role=MessageRole.USER,
        content="summarize please",
        attachments=[
            FileAttachment(
                kind="image",
                name="shot.png",
                media_type="image/png",
                data=image_payload,
            ),
            FileAttachment(
                kind="document",
                name="report.pdf",
                media_type="application/pdf",
                data=doc_payload,
            ),
            FileAttachment(
                kind="text",
                name="notes.md",
                media_type="text/markdown",
                text="# hello",
            ),
        ],
    )
    serialized = AIService._serialize_message(original)
    assert serialized["attachments"] == [
        {
            "kind": "image",
            "name": "shot.png",
            "media_type": "image/png",
            "data": image_payload,
        },
        {
            "kind": "document",
            "name": "report.pdf",
            "media_type": "application/pdf",
            "data": doc_payload,
        },
        {
            "kind": "text",
            "name": "notes.md",
            "media_type": "text/markdown",
            "text": "# hello",
        },
    ]
    deserialized = AIService._deserialize_message(serialized)
    assert len(deserialized.attachments) == 3
    assert deserialized.attachments[0].kind == "image"
    assert deserialized.attachments[0].data == image_payload
    assert deserialized.attachments[1].kind == "document"
    assert deserialized.attachments[1].name == "report.pdf"
    assert deserialized.attachments[2].kind == "text"
    assert deserialized.attachments[2].text == "# hello"


def test_deserialize_legacy_images_key() -> None:
    """Pre-attachments conversations stored images under the ``images`` key."""
    legacy = {
        "role": "user",
        "content": "old shot",
        "images": [
            {"media_type": "image/png", "data": "AAAA"},
            {"media_type": "image/jpeg", "data": "BBBB"},
        ],
    }
    msg = AIService._deserialize_message(legacy)
    assert len(msg.attachments) == 2
    assert msg.attachments[0].kind == "image"
    assert msg.attachments[0].data == "AAAA"
    assert msg.attachments[1].media_type == "image/jpeg"


def test_parse_frame_attachments_none_or_empty() -> None:
    assert _parse_frame_attachments(None) == []
    assert _parse_frame_attachments([]) == []


def test_parse_frame_attachments_accepts_image_document_text() -> None:
    import base64

    image_payload = base64.b64encode(b"hello image").decode()
    doc_payload = base64.b64encode(b"%PDF-1.4 fake").decode()
    result = _parse_frame_attachments(
        [
            {
                "kind": "image",
                "name": "a.png",
                "media_type": "IMAGE/PNG",
                "data": image_payload,
            },
            {
                "kind": "document",
                "name": "r.pdf",
                "media_type": "application/pdf",
                "data": doc_payload,
            },
            {
                "kind": "text",
                "name": "notes.md",
                "media_type": "text/markdown",
                "text": "# hi",
            },
        ]
    )
    assert [a.kind for a in result] == ["image", "document", "text"]
    assert result[0].media_type == "image/png"
    assert result[1].name == "r.pdf"
    assert result[2].text == "# hi"


def test_parse_frame_attachments_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        _parse_frame_attachments([{"kind": "video", "name": "x", "data": "x"}])


def test_parse_frame_attachments_rejects_bad_image_media_type() -> None:
    import base64

    payload = base64.b64encode(b"x").decode()
    with pytest.raises(ValueError, match="unsupported image media_type"):
        _parse_frame_attachments(
            [
                {"kind": "image", "media_type": "image/tiff", "data": payload},
            ]
        )


def test_parse_frame_attachments_rejects_bad_document_media_type() -> None:
    import base64

    payload = base64.b64encode(b"x").decode()
    with pytest.raises(ValueError, match="unsupported document media_type"):
        _parse_frame_attachments(
            [
                {
                    "kind": "document",
                    "name": "x.doc",
                    "media_type": "application/msword",
                    "data": payload,
                },
            ]
        )


def test_parse_frame_attachments_rejects_document_without_name() -> None:
    import base64

    payload = base64.b64encode(b"x").decode()
    with pytest.raises(ValueError, match="document requires a name"):
        _parse_frame_attachments(
            [
                {"kind": "document", "media_type": "application/pdf", "data": payload},
            ]
        )


def test_parse_frame_attachments_rejects_text_without_name() -> None:
    with pytest.raises(ValueError, match="text requires a name"):
        _parse_frame_attachments([{"kind": "text", "text": "hi"}])


def test_parse_frame_attachments_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="text must be a non-empty string"):
        _parse_frame_attachments([{"kind": "text", "name": "a.md", "text": ""}])


def test_parse_frame_attachments_rejects_bad_base64() -> None:
    with pytest.raises(ValueError, match="invalid base64"):
        _parse_frame_attachments(
            [
                {"kind": "image", "media_type": "image/png", "data": "not base64!!!"},
            ]
        )


def test_parse_frame_attachments_rejects_too_many() -> None:
    import base64

    payload = base64.b64encode(b"x").decode()
    items = [{"kind": "image", "media_type": "image/png", "data": payload}] * 9
    with pytest.raises(ValueError, match="too many attachments"):
        _parse_frame_attachments(items)


def test_parse_frame_attachments_rejects_oversize_image() -> None:
    import base64

    oversize = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()
    with pytest.raises(ValueError, match="image is too large"):
        _parse_frame_attachments(
            [
                {"kind": "image", "media_type": "image/png", "data": oversize},
            ]
        )


def test_parse_frame_attachments_rejects_oversize_text() -> None:
    big = "x" * (512 * 1024 + 1)
    with pytest.raises(ValueError, match="text is too large"):
        _parse_frame_attachments(
            [
                {"kind": "text", "name": "big.txt", "text": big},
            ]
        )


def test_parse_frame_attachments_converts_xlsx_to_text() -> None:
    """An xlsx document entry is converted to a markdown text attachment.

    The frontend sends xlsx as a document-kind base64 blob; the parser
    decodes the workbook, renders each sheet as a markdown table, and
    returns a ``kind="text"`` attachment so Anthropic sees readable rows.
    """
    import base64
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "People"
    ws.append(["Name", "Age", "City"])
    ws.append(["Alice", 30, "NYC"])
    ws.append(["Bob", 25, "SF"])
    buf = io.BytesIO()
    wb.save(buf)
    payload = base64.b64encode(buf.getvalue()).decode()

    result = _parse_frame_attachments(
        [
            {
                "kind": "document",
                "name": "roster.xlsx",
                "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "data": payload,
            },
        ]
    )
    assert len(result) == 1
    att = result[0]
    assert att.kind == "text"
    assert att.name == "roster.xlsx"
    assert att.media_type == "text/markdown"
    assert "## Sheet: People" in att.text
    assert "Name" in att.text and "Age" in att.text and "City" in att.text
    assert "Alice" in att.text and "30" in att.text and "NYC" in att.text
    assert "Bob" in att.text


def test_parse_frame_attachments_rejects_corrupt_xlsx() -> None:
    import base64

    bogus = base64.b64encode(b"not a real xlsx").decode()
    with pytest.raises(ValueError, match="could not read xlsx"):
        _parse_frame_attachments(
            [
                {
                    "kind": "document",
                    "name": "bad.xlsx",
                    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": bogus,
                },
            ]
        )


def test_tool_result_serialize_deserialize() -> None:
    original = Message(
        role=MessageRole.TOOL_RESULT,
        tool_results=[
            ToolResult(tool_call_id="tc_1", content="ok"),
            ToolResult(tool_call_id="tc_2", content="error", is_error=True),
        ],
    )
    serialized = AIService._serialize_message(original)
    deserialized = AIService._deserialize_message(serialized)

    assert deserialized.role == MessageRole.TOOL_RESULT
    assert len(deserialized.tool_results) == 2
    assert deserialized.tool_results[0].content == "ok"
    assert not deserialized.tool_results[0].is_error
    assert deserialized.tool_results[1].is_error


# --- Conversation State ---


async def test_set_and_get_conversation_state(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """State can be set and retrieved by key."""
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        )
    )
    await ai_service.start(resolver)

    _, conv_id, _, _, _atts, _rounds = await ai_service.chat("Hi")

    # Capture the saved conversation and return it on subsequent gets
    saved_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    # Set state
    await ai_service.set_conversation_state("my_key", {"score": 42}, conv_id)

    # The put call should include the state
    put_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    assert put_data["state"]["my_key"] == {"score": 42}

    # Mock get to return updated data
    storage_backend.get = AsyncMock(return_value=put_data)  # type: ignore[union-attr]

    result = await ai_service.get_conversation_state("my_key", conv_id)
    assert result == {"score": 42}


async def test_get_missing_state_returns_none(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """Getting a non-existent key returns None."""
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        )
    )
    await ai_service.start(resolver)
    _, conv_id, _, _, _atts, _rounds = await ai_service.chat("Hi")

    saved_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    result = await ai_service.get_conversation_state("nonexistent", conv_id)
    assert result is None


async def test_clear_conversation_state(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """Clearing a key removes it from state."""
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        )
    )
    await ai_service.start(resolver)
    _, conv_id, _, _, _atts, _rounds = await ai_service.chat("Hi")

    saved_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    # Set two keys
    await ai_service.set_conversation_state("a", 1, conv_id)
    put_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=put_data)  # type: ignore[union-attr]

    await ai_service.set_conversation_state("b", 2, conv_id)
    put_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=put_data)  # type: ignore[union-attr]

    # Clear key "a"
    await ai_service.clear_conversation_state("a", conv_id)
    put_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=put_data)  # type: ignore[union-attr]

    assert await ai_service.get_conversation_state("a", conv_id) is None
    assert await ai_service.get_conversation_state("b", conv_id) == 2


async def test_multiple_state_keys_coexist(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """Multiple keys can be stored independently."""
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        )
    )
    await ai_service.start(resolver)
    _, conv_id, _, _, _atts, _rounds = await ai_service.chat("Hi")

    saved_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    await ai_service.set_conversation_state("game", {"round": 1}, conv_id)
    put_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=put_data)  # type: ignore[union-attr]

    await ai_service.set_conversation_state("workflow", {"step": "review"}, conv_id)
    put_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=put_data)  # type: ignore[union-attr]

    assert await ai_service.get_conversation_state("game", conv_id) == {"round": 1}
    assert await ai_service.get_conversation_state("workflow", conv_id) == {"step": "review"}


async def test_state_uses_current_conversation_id(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """When no conversation_id is passed, uses the active one."""
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        )
    )
    await ai_service.start(resolver)
    _, conv_id, _, _, _atts, _rounds = await ai_service.chat("Hi")

    saved_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    # Should use _current_conversation_id implicitly
    await ai_service.set_conversation_state("key", "value")
    put_call = storage_backend.put.call_args[0]  # type: ignore[union-attr]
    assert put_call[1] == conv_id  # entity_id matches conv_id


async def test_state_injected_into_system_prompt(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    """Conversation state appears in the system prompt sent to the AI."""
    # First call to create conversation
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="first"),
            model="stub",
        )
    )
    await ai_service.start(resolver)
    _, conv_id, _, _, _atts, _rounds = await ai_service.chat("Hi")

    # Save state directly in the conversation data
    saved_data = storage_backend.put.call_args[0][2]  # type: ignore[union-attr]
    saved_data["state"] = {"guess_game": {"round": 3, "scores": {"alice": 10}}}
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    # Second call should see state in prompt
    stub_backend.queue_response(
        AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="second"),
            model="stub",
        )
    )
    await ai_service.chat("What's the score?", conversation_id=conv_id)

    req = stub_backend.requests[-1]
    assert "Active Conversation State" in req.system_prompt
    assert "guess_game" in req.system_prompt
    assert '"round": 3' in req.system_prompt


def test_format_state_for_context() -> None:
    """State formatting produces readable text."""
    state = {
        "game": {"round": 2, "players": ["alice"]},
        "simple": "active",
    }
    result = AIService._format_state_for_context(state)
    assert "## Active Conversation State" in result
    assert "### game" in result
    assert "### simple" in result
    assert "active" in result
    assert '"round": 2' in result


def test_format_state_empty() -> None:
    """Formatting empty state still produces a header."""
    result = AIService._format_state_for_context({})
    assert "## Active Conversation State" in result


# --- History load: tool_usage reconstruction ---


class _FakeConn:
    def __init__(self, user_id: str = "u1") -> None:
        self.user_id = user_id
        self.user_ctx = None  # unused by _ws_history_load


async def _run_history_load(
    ai_service: AIService,
    storage_backend: Any,
    stored_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Stub storage with a stored conversation and invoke _ws_history_load."""
    from gilbert.core.services.ai import _COLLECTION

    async def _get(collection: str, key: str) -> Any:
        if collection == _COLLECTION and key == "conv-1":
            return {
                "messages": stored_messages,
                "ui_blocks": [],
                "title": "Test",
                "shared": False,
            }
        return None

    storage_backend.get = AsyncMock(side_effect=_get)
    ai_service._storage = storage_backend
    conn = _FakeConn()
    return await ai_service._ws_history_load(
        conn,
        {"conversation_id": "conv-1", "id": "req-1"},
    )


async def test_history_load_attaches_tool_usage_to_final_assistant(
    ai_service: AIService,
    storage_backend: Any,
) -> None:
    """Intermediate tool-use rounds fold into the turn's rounds list and
    the no-tool assistant row becomes the turn's final answer."""
    stored = [
        {"role": "user", "content": "What's the weather?"},
        # Round 1: AI calls get_weather, with reasoning.
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [
                {
                    "tool_call_id": "call-1",
                    "tool_name": "get_weather",
                    "arguments": {"city": "Portland", "_user_id": "u1"},
                }
            ],
        },
        # Tool result row.
        {
            "role": "tool_result",
            "content": "",
            "tool_results": [
                {
                    "tool_call_id": "call-1",
                    "content": "72F and sunny",
                    "is_error": False,
                }
            ],
        },
        # Final assistant message with the answer.
        {"role": "assistant", "content": "It's 72F and sunny in Portland."},
    ]
    result = await _run_history_load(ai_service, storage_backend, stored)

    turns = result["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["user_message"]["content"] == "What's the weather?"
    assert len(turn["rounds"]) == 1
    rnd = turn["rounds"][0]
    assert rnd["reasoning"] == "Let me check."
    assert len(rnd["tools"]) == 1
    tool = rnd["tools"][0]
    assert tool["tool_name"] == "get_weather"
    assert tool["result"] == "72F and sunny"
    assert tool["is_error"] is False
    # Injected identity keys stripped before delivery.
    assert tool["arguments"] == {"city": "Portland"}
    # The no-tool assistant row becomes the turn's final answer.
    assert turn["final_content"] == "It's 72F and sunny in Portland."
    assert turn["incomplete"] is False


async def test_history_load_multiple_tool_rounds_collected(
    ai_service: AIService,
    storage_backend: Any,
) -> None:
    """Two AI rounds in one turn produce two entries in the turn's rounds list."""
    stored = [
        {"role": "user", "content": "Plan my evening."},
        {
            "role": "assistant",
            "content": "First, the weather.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "tool_name": "get_weather",
                    "arguments": {"city": "SF"},
                }
            ],
        },
        {
            "role": "tool_result",
            "tool_results": [
                {
                    "tool_call_id": "c1",
                    "content": "Rainy",
                    "is_error": False,
                }
            ],
        },
        {
            "role": "assistant",
            "content": "Now restaurants.",
            "tool_calls": [
                {
                    "tool_call_id": "c2",
                    "tool_name": "find_restaurants",
                    "arguments": {"cuisine": "thai"},
                }
            ],
        },
        {
            "role": "tool_result",
            "tool_results": [
                {
                    "tool_call_id": "c2",
                    "content": "Kin Khao, Lers Ros",
                    "is_error": False,
                }
            ],
        },
        {"role": "assistant", "content": "Try Kin Khao — bring an umbrella."},
    ]
    result = await _run_history_load(ai_service, storage_backend, stored)

    turns = result["turns"]
    assert len(turns) == 1
    rounds = turns[0]["rounds"]
    assert len(rounds) == 2
    assert rounds[0]["reasoning"] == "First, the weather."
    assert rounds[0]["tools"][0]["tool_name"] == "get_weather"
    assert rounds[0]["tools"][0]["result"] == "Rainy"
    assert rounds[1]["reasoning"] == "Now restaurants."
    assert rounds[1]["tools"][0]["tool_name"] == "find_restaurants"
    assert rounds[1]["tools"][0]["result"] == "Kin Khao, Lers Ros"
    assert turns[0]["final_content"] == "Try Kin Khao — bring an umbrella."


async def test_history_load_turn_boundary_resets_usage(
    ai_service: AIService,
    storage_backend: Any,
) -> None:
    """Tool usage from turn N must not leak into turn N+1."""
    stored = [
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "tool_name": "get_weather",
                    "arguments": {"city": "LA"},
                }
            ],
        },
        {
            "role": "tool_result",
            "tool_results": [{"tool_call_id": "c1", "content": "Hot"}],
        },
        {"role": "assistant", "content": "Hot in LA."},
        # Next turn — no tools.
        {"role": "user", "content": "Thanks."},
        {"role": "assistant", "content": "You're welcome."},
    ]
    result = await _run_history_load(ai_service, storage_backend, stored)

    turns = result["turns"]
    assert len(turns) == 2
    # Turn 1 has rounds + final
    assert len(turns[0]["rounds"]) == 1
    assert turns[0]["rounds"][0]["tools"][0]["tool_name"] == "get_weather"
    assert turns[0]["final_content"] == "Hot in LA."
    # Turn 2 is plain: no rounds, just a final
    assert len(turns[1]["rounds"]) == 0
    assert turns[1]["final_content"] == "You're welcome."


async def test_history_load_plain_reply_has_no_tool_usage(
    ai_service: AIService,
    storage_backend: Any,
) -> None:
    """Assistant replies that called no tools produce a turn with empty rounds."""
    stored = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello."},
    ]
    result = await _run_history_load(ai_service, storage_backend, stored)

    turns = result["turns"]
    assert len(turns) == 1
    assert turns[0]["rounds"] == []
    assert turns[0]["final_content"] == "Hello."
    assert turns[0]["user_message"]["content"] == "Hi"


async def test_history_load_incomplete_turn_marked(
    ai_service: AIService,
    storage_backend: Any,
) -> None:
    """A turn that ends with a tool_result and no closing assistant text
    is flagged ``incomplete=True`` so the UI can render an indicator."""
    stored = [
        {"role": "user", "content": "Run forever"},
        {
            "role": "assistant",
            "content": "Working on it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "tool_name": "run",
                    "arguments": {},
                }
            ],
        },
        {
            "role": "tool_result",
            "tool_results": [{"tool_call_id": "c1", "content": "ok"}],
        },
        # No closing assistant message — loop hit max_tool_rounds or
        # crashed.
    ]
    result = await _run_history_load(ai_service, storage_backend, stored)

    turns = result["turns"]
    assert len(turns) == 1
    assert turns[0]["incomplete"] is True
    assert turns[0]["final_content"] == ""
    assert len(turns[0]["rounds"]) == 1
    assert turns[0]["rounds"][0]["tools"][0]["result"] == "ok"
