"""Tests for AIService — agentic loop, tool discovery, conversation persistence."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import AIService
from gilbert.core.services.credentials import CredentialService
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    TokenUsage,
)
from gilbert.interfaces.credentials import ApiKeyCredential
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

    def get_tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._results:
            raise KeyError(f"Unknown tool: {name}")
        return self._results[name]


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

    def get_tools(self) -> list[ToolDefinition]:
        return [ToolDefinition(name="fail_tool", description="Always fails")]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("tool exploded")


# --- Fixtures ---


@pytest.fixture
def stub_backend() -> StubAIBackend:
    return StubAIBackend()


@pytest.fixture
def cred_service() -> CredentialService:
    return CredentialService({
        "anthropic": ApiKeyCredential(api_key="sk-test-key"),
    })


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
    cred_service: CredentialService,
    storage_service: StorageService,
) -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "credentials":
            return cred_service
        if cap == "entity_storage":
            return storage_service
        raise LookupError(f"No service provides: {cap}")

    def get_cap(cap: str) -> Any:
        if cap == "configuration":
            return None  # No ConfigurationService in tests
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
    svc = AIService(backend=stub_backend, credential_name="anthropic")
    # Set tunable config directly for testing
    svc._config = {"max_tokens": 1024, "temperature": 0.5}
    svc._system_prompt = "You are a test assistant."
    svc._max_tool_rounds = 5
    return svc


# --- Service Info ---


def test_service_info(ai_service: AIService) -> None:
    info = ai_service.service_info()
    assert info.name == "ai"
    assert "ai_chat" in info.capabilities
    assert "credentials" in info.requires
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
    stub_backend.queue_response(AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content="Hello there!"),
        model="stub",
    ))
    await ai_service.start(resolver)

    text, conv_id = await ai_service.chat("Hi")
    assert text == "Hello there!"
    assert conv_id  # non-empty UUID string
    assert len(stub_backend.requests) == 1

    req = stub_backend.requests[0]
    assert req.system_prompt == "You are a test assistant."
    assert req.max_tokens == 1024
    assert req.temperature == 0.5
    assert len(req.messages) == 1
    assert req.messages[0].role == MessageRole.USER
    assert req.messages[0].content == "Hi"


async def test_chat_continues_conversation(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
    storage_backend: StorageBackend,
) -> None:
    stub_backend.queue_response(AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content="First reply"),
        model="stub",
    ))
    stub_backend.queue_response(AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content="Second reply"),
        model="stub",
    ))
    await ai_service.start(resolver)

    # First message
    _, conv_id = await ai_service.chat("Hello")

    # Simulate storage returning the saved conversation
    saved_call = storage_backend.put.call_args  # type: ignore[union-attr]
    saved_data = saved_call[0][2]  # positional arg: data
    storage_backend.get = AsyncMock(return_value=saved_data)  # type: ignore[union-attr]

    # Second message in same conversation
    text, same_id = await ai_service.chat("Follow up", conversation_id=conv_id)
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
    cred_service: CredentialService,
    storage_service: StorageService,
    storage_backend: StorageBackend,
) -> None:
    # Set up a tool provider
    tool_def = ToolDefinition(
        name="get_weather",
        description="Get weather",
        parameters=[
            ToolParameter(
                name="city", type=ToolParameterType.STRING, description="City name"
            ),
        ],
    )
    tool_provider = StubToolProviderService(
        tools=[tool_def],
        results={"get_weather": '{"temp": 72, "condition": "sunny"}'},
    )

    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "credentials":
            return cred_service
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [tool_provider] if cap == "ai_tools" else []

    # Round 1: AI requests a tool call
    stub_backend.queue_response(AIResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content="Let me check the weather.",
            tool_calls=[ToolCall(
                tool_call_id="tc_1",
                tool_name="get_weather",
                arguments={"city": "Portland"},
            )],
        ),
        model="stub",
        stop_reason=StopReason.TOOL_USE,
    ))
    # Round 2: AI gives final response
    stub_backend.queue_response(AIResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content="It's 72F and sunny in Portland!",
        ),
        model="stub",
    ))

    await ai_service.start(resolver)
    text, _ = await ai_service.chat("What's the weather in Portland?")

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


async def test_chat_max_tool_rounds(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    """The agentic loop stops after max_tool_rounds even if AI keeps calling tools."""
    # Queue responses that always request tool calls
    for i in range(10):
        stub_backend.queue_response(AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                tool_calls=[ToolCall(
                    tool_call_id=f"tc_{i}",
                    tool_name="unknown_tool",
                    arguments={},
                )],
            ),
            model="stub",
            stop_reason=StopReason.TOOL_USE,
        ))

    await ai_service.start(resolver)
    text, _ = await ai_service.chat("loop forever")

    # max_tool_rounds=5, so at most 5 backend calls
    assert len(stub_backend.requests) == 5


# --- Tool Errors ---


async def test_unknown_tool_returns_error_result(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    resolver: ServiceResolver,
) -> None:
    stub_backend.queue_response(AIResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            tool_calls=[ToolCall(
                tool_call_id="tc_bad",
                tool_name="nonexistent",
                arguments={},
            )],
        ),
        model="stub",
        stop_reason=StopReason.TOOL_USE,
    ))
    stub_backend.queue_response(AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content="Sorry, couldn't do that."),
        model="stub",
    ))

    await ai_service.start(resolver)
    text, _ = await ai_service.chat("Do something impossible")

    # The error result was fed back
    second_req = stub_backend.requests[1]
    tool_result_msg = second_req.messages[-1]
    assert tool_result_msg.tool_results[0].is_error
    assert "unknown tool" in tool_result_msg.tool_results[0].content


async def test_tool_execution_error_returns_error_result(
    ai_service: AIService,
    stub_backend: StubAIBackend,
    cred_service: CredentialService,
    storage_service: StorageService,
) -> None:
    error_provider = ErrorToolProviderService()
    resolver = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "credentials":
            return cred_service
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    resolver.require_capability = require_cap
    resolver.get_capability = lambda cap: None
    resolver.get_all = lambda cap: [error_provider] if cap == "ai_tools" else []

    stub_backend.queue_response(AIResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            tool_calls=[ToolCall(
                tool_call_id="tc_err",
                tool_name="fail_tool",
                arguments={},
            )],
        ),
        model="stub",
        stop_reason=StopReason.TOOL_USE,
    ))
    stub_backend.queue_response(AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content="That failed."),
        model="stub",
    ))

    await ai_service.start(resolver)
    text, _ = await ai_service.chat("Run the bad tool")

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
    stub_backend.queue_response(AIResponse(
        message=Message(role=MessageRole.ASSISTANT, content="Saved!"),
        model="stub",
    ))
    await ai_service.start(resolver)
    _, conv_id = await ai_service.chat("Save this")

    storage_backend.put.assert_called_once()  # type: ignore[union-attr]
    call_args = storage_backend.put.call_args[0]  # type: ignore[union-attr]
    assert call_args[0] == "ai_conversations"
    assert call_args[1] == conv_id


# --- History Truncation ---


def test_truncate_history_within_limit(ai_service: AIService) -> None:
    messages = [
        Message(role=MessageRole.USER, content="msg1"),
        Message(role=MessageRole.ASSISTANT, content="reply1"),
    ]
    result = ai_service._truncate_history(messages)
    assert len(result) == 2


def test_truncate_history_preserves_tool_pairs() -> None:
    svc = AIService(backend=StubAIBackend(), credential_name="x")
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
        tool_calls=[ToolCall(
            tool_call_id="tc_1",
            tool_name="search",
            arguments={"q": "test"},
        )],
    )
    serialized = AIService._serialize_message(original)
    deserialized = AIService._deserialize_message(serialized)

    assert deserialized.role == original.role
    assert deserialized.content == original.content
    assert len(deserialized.tool_calls) == 1
    assert deserialized.tool_calls[0].tool_call_id == "tc_1"
    assert deserialized.tool_calls[0].tool_name == "search"
    assert deserialized.tool_calls[0].arguments == {"q": "test"}


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
