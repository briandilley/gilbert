"""Anthropic Claude AI backend — AI via the Anthropic Messages API."""

import json
import logging
from typing import Any

import httpx

from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    TokenUsage,
)
from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")

_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_API_VERSION = "2023-06-01"


class AnthropicAI(AIBackend):
    """AI backend using the Anthropic Messages API via httpx."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._model: str = _DEFAULT_MODEL

    async def initialize(self, config: dict[str, Any]) -> None:
        api_key = config.get("api_key")
        if not api_key:
            raise ValueError("AnthropicAI requires 'api_key' in config")

        self._model = str(config.get("model", _DEFAULT_MODEL))

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "x-api-key": str(api_key),
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            timeout=120.0,
        )
        logger.info("Anthropic AI backend initialized (model=%s)", self._model)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def generate(self, request: AIRequest) -> AIResponse:
        if self._client is None:
            raise RuntimeError("AnthropicAI not initialized")

        body = self._build_request_body(request)

        ai_logger.debug("Anthropic request: model=%s messages=%d", self._model, len(body["messages"]))

        resp = await self._client.post("/messages", json=body)
        resp.raise_for_status()
        data = resp.json()

        ai_logger.debug(
            "Anthropic response: stop_reason=%s usage=%s",
            data.get("stop_reason"),
            data.get("usage"),
        )

        return self._parse_response(data)

    # --- Request Building ---

    def _build_request_body(self, request: AIRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": self._build_messages(request.messages),
        }

        if request.system_prompt:
            body["system"] = request.system_prompt

        if request.tools:
            body["tools"] = self._build_tools(request.tools)

        if request.temperature is not None:
            body["temperature"] = request.temperature

        return body

    def _build_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to Anthropic content block format."""
        result: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # System messages are handled via the top-level 'system' param
                continue

            if msg.role == MessageRole.USER:
                result.append({"role": "user", "content": msg.content})

            elif msg.role == MessageRole.ASSISTANT:
                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append({
                        "type": "tool_use",
                        "id": tc.tool_call_id,
                        "name": tc.tool_name,
                        "input": tc.arguments,
                    })
                result.append({"role": "assistant", "content": content})

            elif msg.role == MessageRole.TOOL_RESULT:
                content_blocks: list[dict[str, Any]] = []
                for tr in msg.tool_results:
                    block: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_call_id,
                        "content": tr.content,
                    }
                    if tr.is_error:
                        block["is_error"] = True
                    content_blocks.append(block)
                result.append({"role": "user", "content": content_blocks})

        return result

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert tool definitions to Anthropic tool schema format."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.to_json_schema(),
            }
            for tool in tools
        ]

    # --- Response Parsing ---

    def _parse_response(self, data: dict[str, Any]) -> AIResponse:
        """Parse Anthropic API response into an AIResponse."""
        content_text = ""
        tool_calls: list[ToolCall] = []

        for block in data.get("content", []):
            if block["type"] == "text":
                content_text += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCall(
                    tool_call_id=block["id"],
                    tool_name=block["name"],
                    arguments=block.get("input", {}),
                ))

        # Map Anthropic stop_reason to our enum
        raw_stop = data.get("stop_reason", "end_turn")
        if raw_stop == "tool_use":
            stop_reason = StopReason.TOOL_USE
        elif raw_stop == "max_tokens":
            stop_reason = StopReason.MAX_TOKENS
        else:
            stop_reason = StopReason.END_TURN

        # Parse usage
        usage = None
        raw_usage = data.get("usage")
        if raw_usage:
            usage = TokenUsage(
                input_tokens=raw_usage.get("input_tokens", 0),
                output_tokens=raw_usage.get("output_tokens", 0),
            )

        message = Message(
            role=MessageRole.ASSISTANT,
            content=content_text,
            tool_calls=tool_calls,
        )

        return AIResponse(
            message=message,
            model=data.get("model", self._model),
            stop_reason=stop_reason,
            usage=usage,
        )
