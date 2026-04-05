"""AI service — orchestrates AI conversations, tool execution, and persistence."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.configuration import ConfigParam, Configurable
from gilbert.interfaces.credentials import ApiKeyCredential
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameterType,
    ToolProvider,
    ToolResult,
)

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")

_COLLECTION = "ai_conversations"


class AIService(Service):
    """Orchestrates AI conversations with tool use.

    Wraps an AIBackend (provider-specific) and adds:
    - Agentic loop (tool call → execute → feed back → repeat)
    - Tool discovery from registered ToolProvider services
    - Conversation persistence to storage
    - History truncation
    """

    def __init__(
        self,
        backend: AIBackend,
        credential_name: str,
    ) -> None:
        self._backend = backend
        self._credential_name = credential_name
        # Tunable config — loaded from ConfigurationService during start()
        self._config: dict[str, Any] = {}
        self._system_prompt: str = ""
        self._max_history_messages: int = 50
        self._max_tool_rounds: int = 10
        self._storage: StorageBackend | None = None
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ai",
            capabilities=frozenset({"ai_chat"}),
            requires=frozenset({"credentials", "entity_storage"}),
            optional=frozenset({"ai_tools", "configuration"}),
        )

    @property
    def backend(self) -> AIBackend:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.core.services.credentials import CredentialService
        from gilbert.core.services.storage import StorageService

        # Load tunable config from ConfigurationService if available
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("ai")
                self._apply_config(section)

        # Resolve credential
        cred_svc = resolver.require_capability("credentials")
        if not isinstance(cred_svc, CredentialService):
            raise TypeError("Expected CredentialService for 'credentials' capability")

        cred = cred_svc.require(self._credential_name)
        if not isinstance(cred, ApiKeyCredential):
            raise TypeError(
                f"Credential '{self._credential_name}' must be an api_key credential"
            )

        # Initialize backend
        init_config: dict[str, Any] = {**self._config, "api_key": cred.api_key}
        await self._backend.initialize(init_config)

        # Resolve storage
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageService):
            raise TypeError("Expected StorageService for 'entity_storage' capability")
        self._storage = storage_svc.backend

        # Save resolver for lazy tool discovery
        self._resolver = resolver

        logger.info("AI service started (credential=%s)", self._credential_name)

    def _apply_config(self, section: dict[str, Any]) -> None:
        """Apply tunable config values from a config section."""
        self._system_prompt = section.get("system_prompt", self._system_prompt)
        self._max_history_messages = section.get(
            "max_history_messages", self._max_history_messages
        )
        self._max_tool_rounds = section.get("max_tool_rounds", self._max_tool_rounds)
        self._config = section.get("settings", self._config)

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "ai"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="system_prompt", type=ToolParameterType.STRING,
                description="System prompt that defines the AI's personality and instructions.",
                default="You are Gilbert, an AI assistant for home and business automation.",
            ),
            ConfigParam(
                key="max_history_messages", type=ToolParameterType.INTEGER,
                description="Maximum conversation messages to include in each request.",
                default=50,
            ),
            ConfigParam(
                key="max_tool_rounds", type=ToolParameterType.INTEGER,
                description="Maximum agentic loop iterations (tool call rounds) per chat.",
                default=10,
            ),
            ConfigParam(
                key="settings.temperature", type=ToolParameterType.NUMBER,
                description="AI temperature (0.0=deterministic, 1.0=creative).",
                default=0.7,
            ),
            ConfigParam(
                key="settings.max_tokens", type=ToolParameterType.INTEGER,
                description="Maximum tokens in AI response.",
                default=4096,
            ),
            ConfigParam(
                key="settings.model", type=ToolParameterType.STRING,
                description="AI model identifier.",
                default="claude-sonnet-4-20250514",
            ),
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="AI backend provider.",
                default="anthropic", restart_required=True,
            ),
            ConfigParam(
                key="credential", type=ToolParameterType.STRING,
                description="Name of the API key credential to use.",
                restart_required=True,
            ),
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the AI service is enabled.",
                default=False, restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    async def stop(self) -> None:
        await self._backend.close()

    # --- Chat ---

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
    ) -> tuple[str, str]:
        """Send a user message and get an AI response (with full agentic loop).

        Args:
            user_message: The user's input text.
            conversation_id: Existing conversation ID, or None to start new.

        Returns:
            (response_text, conversation_id) tuple.
        """
        # Load or create conversation
        if conversation_id:
            messages = await self._load_conversation(conversation_id)
        else:
            conversation_id = str(uuid.uuid4())
            messages = []

        # Append user message
        messages.append(Message(role=MessageRole.USER, content=user_message))

        # Discover tools from all ai_tools providers
        tools_by_name = self._discover_tools()
        tool_defs = [defn for _, defn in tools_by_name.values()]

        # Agentic loop
        response: AIResponse | None = None
        for round_num in range(self._max_tool_rounds):
            truncated = self._truncate_history(messages)

            request = AIRequest(
                messages=truncated,
                system_prompt=self._system_prompt,
                tools=tool_defs if tool_defs else [],
                max_tokens=int(self._config.get("max_tokens", 4096)),
                temperature=float(self._config.get("temperature", 0.7)),
            )

            response = await self._backend.generate(request)
            self._log_api_call(request, response, round_num)

            # Append assistant message to history
            messages.append(response.message)

            # If no tool calls, we're done
            if response.stop_reason != StopReason.TOOL_USE or not response.message.tool_calls:
                break

            # Execute tool calls and append results
            tool_results = await self._execute_tool_calls(
                response.message.tool_calls, tools_by_name
            )
            messages.append(Message(role=MessageRole.TOOL_RESULT, tool_results=tool_results))
        else:
            logger.warning(
                "Agentic loop hit max rounds (%d) for conversation %s",
                self._max_tool_rounds,
                conversation_id,
            )

        # Persist conversation
        await self._save_conversation(conversation_id, messages)

        # Return final text response
        final_text = response.message.content if response else ""
        return final_text, conversation_id

    # --- Tool Discovery ---

    def _discover_tools(self) -> dict[str, tuple[ToolProvider, ToolDefinition]]:
        """Find all started services that implement ToolProvider and collect their tools."""
        tools_by_name: dict[str, tuple[ToolProvider, ToolDefinition]] = {}
        if self._resolver is None:
            return tools_by_name

        for svc in self._resolver.get_all("ai_tools"):
            if not isinstance(svc, ToolProvider):
                continue
            for tool_def in svc.get_tools():
                if tool_def.name in tools_by_name:
                    logger.warning(
                        "Duplicate tool name %r from %s (already registered by %s)",
                        tool_def.name,
                        svc.tool_provider_name,
                        tools_by_name[tool_def.name][0].tool_provider_name,
                    )
                    continue
                tools_by_name[tool_def.name] = (svc, tool_def)

        return tools_by_name

    # --- Tool Execution ---

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        tools_by_name: dict[str, tuple[ToolProvider, ToolDefinition]],
    ) -> list[ToolResult]:
        """Execute a batch of tool calls and return results."""
        results: list[ToolResult] = []
        for tc in tool_calls:
            provider_and_def = tools_by_name.get(tc.tool_name)
            if provider_and_def is None:
                results.append(ToolResult(
                    tool_call_id=tc.tool_call_id,
                    content=f"Error: unknown tool '{tc.tool_name}'",
                    is_error=True,
                ))
                continue
            provider, _ = provider_and_def
            try:
                result_text = await provider.execute_tool(tc.tool_name, tc.arguments)
                results.append(ToolResult(
                    tool_call_id=tc.tool_call_id,
                    content=result_text,
                ))
            except Exception as exc:
                logger.exception("Tool execution failed: %s", tc.tool_name)
                results.append(ToolResult(
                    tool_call_id=tc.tool_call_id,
                    content=f"Error executing tool: {exc}",
                    is_error=True,
                ))
        return results

    # --- Conversation Persistence ---

    async def _save_conversation(self, conv_id: str, messages: list[Message]) -> None:
        """Persist a conversation to storage."""
        if self._storage is None:
            return
        data = {
            "messages": [self._serialize_message(m) for m in messages],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._storage.put(_COLLECTION, conv_id, data)

    async def _load_conversation(self, conv_id: str) -> list[Message]:
        """Load a conversation from storage. Returns empty list if not found."""
        if self._storage is None:
            return []
        data = await self._storage.get(_COLLECTION, conv_id)
        if data is None:
            return []
        return [self._deserialize_message(m) for m in data.get("messages", [])]

    @staticmethod
    def _serialize_message(msg: Message) -> dict[str, Any]:
        d: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "tool_call_id": tc.tool_call_id,
                    "tool_name": tc.tool_name,
                    "arguments": tc.arguments,
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_results:
            d["tool_results"] = [
                {
                    "tool_call_id": tr.tool_call_id,
                    "content": tr.content,
                    "is_error": tr.is_error,
                }
                for tr in msg.tool_results
            ]
        return d

    @staticmethod
    def _deserialize_message(data: dict[str, Any]) -> Message:
        tool_calls = [
            ToolCall(
                tool_call_id=tc["tool_call_id"],
                tool_name=tc["tool_name"],
                arguments=tc["arguments"],
            )
            for tc in data.get("tool_calls", [])
        ]
        tool_results = [
            ToolResult(
                tool_call_id=tr["tool_call_id"],
                content=tr["content"],
                is_error=tr.get("is_error", False),
            )
            for tr in data.get("tool_results", [])
        ]
        return Message(
            role=MessageRole(data["role"]),
            content=data.get("content", ""),
            tool_calls=tool_calls,
            tool_results=tool_results,
        )

    # --- History Management ---

    def _truncate_history(self, messages: list[Message]) -> list[Message]:
        """Truncate to max_history_messages, preserving tool-call/result pairs."""
        if len(messages) <= self._max_history_messages:
            return list(messages)

        truncated = messages[-self._max_history_messages:]

        # If the first message is TOOL_RESULT, include the preceding assistant
        # message (which has the tool_calls) to keep the pair intact.
        while truncated and truncated[0].role == MessageRole.TOOL_RESULT:
            idx = messages.index(truncated[0])
            if idx > 0:
                truncated.insert(0, messages[idx - 1])
            else:
                break

        return truncated

    # --- Logging ---

    def _log_api_call(
        self, request: AIRequest, response: AIResponse, round_num: int
    ) -> None:
        usage_str = ""
        if response.usage:
            usage_str = (
                f" tokens={response.usage.input_tokens}+{response.usage.output_tokens}"
            )
        ai_logger.debug(
            "AI call round=%d model=%s stop=%s%s tools=%d messages=%d",
            round_num,
            response.model,
            response.stop_reason.value,
            usage_str,
            len(request.tools),
            len(request.messages),
        )
