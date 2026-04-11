"""Tests for AnthropicAI backend — message translation and response parsing."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.integrations.anthropic_ai import AnthropicAI
from gilbert.interfaces.ai import (
    AIRequest,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)


@pytest.fixture
def backend() -> AnthropicAI:
    return AnthropicAI()


# --- Initialization ---


async def test_initialize_requires_api_key(backend: AnthropicAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_initialize_creates_client(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None
    await backend.close()


async def test_initialize_custom_model(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test", "model": "claude-opus-4-20250514"})
    assert backend._model == "claude-opus-4-20250514"
    await backend.close()


async def test_close_clears_client(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    await backend.close()
    assert backend._client is None


# --- Request Building ---


def test_build_messages_user() -> None:
    backend = AnthropicAI()
    messages = [Message(role=MessageRole.USER, content="Hello")]
    result = backend._build_messages(messages)
    assert result == [{"role": "user", "content": "Hello"}]


def test_build_messages_assistant_text_only() -> None:
    backend = AnthropicAI()
    messages = [Message(role=MessageRole.ASSISTANT, content="Hi there")]
    result = backend._build_messages(messages)
    assert result == [{"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]}]


def test_build_messages_assistant_with_tool_calls() -> None:
    backend = AnthropicAI()
    messages = [Message(
        role=MessageRole.ASSISTANT,
        content="Let me check.",
        tool_calls=[ToolCall(
            tool_call_id="tc_1",
            tool_name="search",
            arguments={"q": "test"},
        )],
    )]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "Let me check."}
    assert content[1] == {
        "type": "tool_use",
        "id": "tc_1",
        "name": "search",
        "input": {"q": "test"},
    }


def test_build_messages_tool_result() -> None:
    backend = AnthropicAI()
    messages = [Message(
        role=MessageRole.TOOL_RESULT,
        tool_results=[
            ToolResult(tool_call_id="tc_1", content="found it"),
            ToolResult(tool_call_id="tc_2", content="failed", is_error=True),
        ],
    )]
    result = backend._build_messages(messages)
    assert result[0]["role"] == "user"
    content = result[0]["content"]
    assert len(content) == 2
    assert content[0] == {
        "type": "tool_result",
        "tool_use_id": "tc_1",
        "content": "found it",
    }
    assert content[1] == {
        "type": "tool_result",
        "tool_use_id": "tc_2",
        "content": "failed",
        "is_error": True,
    }


def test_build_messages_skips_system() -> None:
    backend = AnthropicAI()
    messages = [
        Message(role=MessageRole.SYSTEM, content="You are helpful"),
        Message(role=MessageRole.USER, content="Hi"),
    ]
    result = backend._build_messages(messages)
    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_build_tools() -> None:
    tools = [
        ToolDefinition(
            name="search",
            description="Search for things",
            parameters=[
                ToolParameter(
                    name="query",
                    type=ToolParameterType.STRING,
                    description="Search query",
                ),
            ],
        ),
    ]
    result = AnthropicAI._build_tools(tools)
    assert len(result) == 1
    assert result[0]["name"] == "search"
    assert result[0]["description"] == "Search for things"
    assert result[0]["input_schema"]["properties"]["query"]["type"] == "string"


def test_build_request_body_includes_system() -> None:
    backend = AnthropicAI()
    backend._model = "test-model"
    backend._max_tokens = 100
    backend._temperature = 0.3
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
        system_prompt="Be helpful",
    )
    body = backend._build_request_body(request)
    assert body["system"] == "Be helpful"
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.3


def test_build_request_body_omits_empty_system() -> None:
    backend = AnthropicAI()
    backend._model = "test-model"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert "system" not in body


def test_build_request_body_omits_empty_tools() -> None:
    backend = AnthropicAI()
    backend._model = "test-model"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert "tools" not in body


# --- Response Parsing ---


def test_parse_text_response() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Hello!"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.message.role == MessageRole.ASSISTANT
    assert response.model == "claude-test"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


def test_parse_tool_use_response() -> None:
    backend = AnthropicAI()
    data = {
        "content": [
            {"type": "text", "text": "Checking..."},
            {
                "type": "tool_use",
                "id": "tu_123",
                "name": "search",
                "input": {"q": "weather"},
            },
        ],
        "model": "claude-test",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Checking..."
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].tool_call_id == "tu_123"
    assert response.message.tool_calls[0].tool_name == "search"
    assert response.message.tool_calls[0].arguments == {"q": "weather"}
    assert response.stop_reason == StopReason.TOOL_USE


def test_parse_max_tokens_response() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Truncated..."}],
        "model": "claude-test",
        "stop_reason": "max_tokens",
    }
    response = backend._parse_response(data)
    assert response.stop_reason == StopReason.MAX_TOKENS


def test_parse_no_usage() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Hi"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
    }
    response = backend._parse_response(data)
    assert response.usage is None


def test_parse_multiple_tool_calls() -> None:
    backend = AnthropicAI()
    data = {
        "content": [
            {"type": "tool_use", "id": "tc_1", "name": "a", "input": {}},
            {"type": "tool_use", "id": "tc_2", "name": "b", "input": {"x": 1}},
        ],
        "model": "claude-test",
        "stop_reason": "tool_use",
    }
    response = backend._parse_response(data)
    assert len(response.message.tool_calls) == 2
    assert response.message.tool_calls[0].tool_name == "a"
    assert response.message.tool_calls[1].tool_name == "b"


# --- Generate (integration with mock HTTP) ---


async def test_generate_calls_api(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "content": [{"type": "text", "text": "API response"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    mock_response.raise_for_status = MagicMock()

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
        system_prompt="Be helpful",
    )
    response = await backend.generate(request)

    assert response.message.content == "API response"
    backend._client.post.assert_called_once()
    call_kwargs = backend._client.post.call_args
    assert call_kwargs[0][0] == "/messages"

    await backend.close()


async def test_generate_raises_when_not_initialized(backend: AnthropicAI) -> None:
    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(request)
