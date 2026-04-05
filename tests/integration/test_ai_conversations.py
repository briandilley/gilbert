"""Integration tests — AI conversation persistence with real SQLite."""

import pytest

from gilbert.core.services.ai import AIService
from gilbert.interfaces.ai import Message, MessageRole
from gilbert.interfaces.tools import ToolCall, ToolResult
from gilbert.storage.sqlite import SQLiteStorage


async def test_save_and_load_conversation(sqlite_storage: SQLiteStorage) -> None:
    """Round-trip a conversation through real storage."""
    messages = [
        Message(role=MessageRole.USER, content="Hello"),
        Message(role=MessageRole.ASSISTANT, content="Hi there!"),
        Message(role=MessageRole.USER, content="How are you?"),
        Message(role=MessageRole.ASSISTANT, content="I'm doing well."),
    ]

    # Save
    conv_id = "test-conv-1"
    data = {
        "messages": [AIService._serialize_message(m) for m in messages],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    await sqlite_storage.put("ai_conversations", conv_id, data)

    # Load
    loaded = await sqlite_storage.get("ai_conversations", conv_id)
    assert loaded is not None
    loaded_messages = [
        AIService._deserialize_message(m) for m in loaded["messages"]
    ]

    assert len(loaded_messages) == 4
    assert loaded_messages[0].role == MessageRole.USER
    assert loaded_messages[0].content == "Hello"
    assert loaded_messages[1].role == MessageRole.ASSISTANT
    assert loaded_messages[1].content == "Hi there!"


async def test_conversation_with_tool_calls(sqlite_storage: SQLiteStorage) -> None:
    """Tool calls and results survive serialization through real storage."""
    messages = [
        Message(role=MessageRole.USER, content="What's the weather?"),
        Message(
            role=MessageRole.ASSISTANT,
            content="Let me check.",
            tool_calls=[
                ToolCall(
                    tool_call_id="tc_1",
                    tool_name="get_weather",
                    arguments={"city": "Portland"},
                ),
            ],
        ),
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[
                ToolResult(tool_call_id="tc_1", content='{"temp": 55}'),
            ],
        ),
        Message(role=MessageRole.ASSISTANT, content="It's 55F in Portland."),
    ]

    conv_id = "test-conv-tools"
    data = {
        "messages": [AIService._serialize_message(m) for m in messages],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    await sqlite_storage.put("ai_conversations", conv_id, data)

    loaded = await sqlite_storage.get("ai_conversations", conv_id)
    assert loaded is not None
    loaded_messages = [
        AIService._deserialize_message(m) for m in loaded["messages"]
    ]

    # Verify tool call
    assert loaded_messages[1].tool_calls[0].tool_name == "get_weather"
    assert loaded_messages[1].tool_calls[0].arguments == {"city": "Portland"}

    # Verify tool result
    assert loaded_messages[2].role == MessageRole.TOOL_RESULT
    assert loaded_messages[2].tool_results[0].tool_call_id == "tc_1"
    assert loaded_messages[2].tool_results[0].content == '{"temp": 55}'
    assert not loaded_messages[2].tool_results[0].is_error


async def test_conversation_with_error_tool_result(
    sqlite_storage: SQLiteStorage,
) -> None:
    """Error flag on tool results survives round-trip."""
    messages = [
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[
                ToolResult(
                    tool_call_id="tc_err",
                    content="Connection refused",
                    is_error=True,
                ),
            ],
        ),
    ]

    data = {
        "messages": [AIService._serialize_message(m) for m in messages],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    await sqlite_storage.put("ai_conversations", "test-err", data)

    loaded = await sqlite_storage.get("ai_conversations", "test-err")
    assert loaded is not None
    loaded_messages = [
        AIService._deserialize_message(m) for m in loaded["messages"]
    ]

    assert loaded_messages[0].tool_results[0].is_error is True
    assert loaded_messages[0].tool_results[0].content == "Connection refused"
