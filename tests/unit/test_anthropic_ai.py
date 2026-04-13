"""Tests for AnthropicAI backend — message translation and response parsing."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.integrations.anthropic_ai import AnthropicAI
from gilbert.interfaces.ai import (
    AIBackendError,
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
    mock_response.is_error = False
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


async def test_generate_raises_ai_backend_error_on_http_error(
    backend: AnthropicAI,
) -> None:
    """A 4xx response should surface Anthropic's error.message, not opaque HTTP text."""
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 400
    mock_response.json.return_value = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "messages.49: all messages must have non-empty content",
        },
    }

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
    )

    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(request)

    assert exc_info.value.status == 400
    assert "messages.49: all messages must have non-empty content" in str(exc_info.value)
    assert "400" in str(exc_info.value)

    await backend.close()


def test_build_messages_splits_slash_command_combined_row(backend: AnthropicAI) -> None:
    """Assistant rows carrying both tool_calls and tool_results must be split.

    Slash-command turns are persisted as a single assistant row with both a
    ``ToolCall`` and a ``ToolResult`` attached. Anthropic requires the
    ``tool_result`` to live on a user-role message immediately after the
    ``tool_use``, so the request builder must emit three Anthropic messages
    (assistant tool_use → user tool_result → assistant text) for each such
    row. Regression for the 400 "tool_use ids were found without tool_result
    blocks" error that broke every conversation containing a slash command.
    """
    messages = [
        Message(role=MessageRole.USER, content="/recap 7d"),
        Message(
            role=MessageRole.ASSISTANT,
            content="Here's your recap...",
            tool_calls=[ToolCall(
                tool_call_id="slash-abc123",
                tool_name="time_logs_recap",
                arguments={"days": 7},
            )],
            tool_results=[ToolResult(
                tool_call_id="slash-abc123",
                content="Recap: 7 days...",
                is_error=False,
            )],
        ),
        Message(role=MessageRole.USER, content="show me more"),
    ]

    built = backend._build_messages(messages)

    # Expected: user → assistant(tool_use) → user(tool_result) → assistant(text) → user
    assert len(built) == 5
    assert built[0] == {"role": "user", "content": "/recap 7d"}

    # Split row 1: assistant with only the tool_use block
    assert built[1]["role"] == "assistant"
    assert len(built[1]["content"]) == 1
    assert built[1]["content"][0]["type"] == "tool_use"
    assert built[1]["content"][0]["id"] == "slash-abc123"
    assert built[1]["content"][0]["name"] == "time_logs_recap"

    # Split row 2: user with the matching tool_result
    assert built[2]["role"] == "user"
    assert len(built[2]["content"]) == 1
    assert built[2]["content"][0]["type"] == "tool_result"
    assert built[2]["content"][0]["tool_use_id"] == "slash-abc123"
    assert built[2]["content"][0]["content"] == "Recap: 7 days..."
    assert "is_error" not in built[2]["content"][0]  # not set when False

    # Split row 3: assistant text for alternation
    assert built[3]["role"] == "assistant"
    assert built[3]["content"] == [{"type": "text", "text": "Here's your recap..."}]

    # The user's follow-up must still come after the split
    assert built[4] == {"role": "user", "content": "show me more"}


def test_build_messages_splits_slash_command_error_row(backend: AnthropicAI) -> None:
    """Errored slash-command rows must propagate is_error and fall back on empty text."""
    messages = [
        Message(role=MessageRole.USER, content="/bad"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",  # tool returned no text
            tool_calls=[ToolCall(
                tool_call_id="slash-deadbeef",
                tool_name="bad_tool",
                arguments={},
            )],
            tool_results=[ToolResult(
                tool_call_id="slash-deadbeef",
                content="boom",
                is_error=True,
            )],
        ),
    ]

    built = backend._build_messages(messages)

    assert len(built) == 4
    # tool_result carries is_error
    assert built[2]["content"][0]["is_error"] is True
    # Empty content falls back to placeholder so we don't ship an empty array
    assert built[3]["content"][0]["text"] == "(done)"


async def test_generate_raises_ai_backend_error_on_non_json_error(
    backend: AnthropicAI,
) -> None:
    """A 5xx with a non-JSON body should still produce a non-empty error message."""
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 502
    mock_response.json.side_effect = ValueError("not json")
    mock_response.text = "<html>Bad Gateway</html>"

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
    )

    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(request)

    assert exc_info.value.status == 502
    assert "Bad Gateway" in str(exc_info.value)

    await backend.close()


async def test_generate_raises_when_not_initialized(backend: AnthropicAI) -> None:
    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(request)
