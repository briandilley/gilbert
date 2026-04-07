"""AI service — orchestrates AI conversations, tool execution, and persistence."""

import json as _json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.credentials import ApiKeyCredential
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField, StorageBackend
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
    ToolResult,
)

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")

_COLLECTION = "ai_conversations"
_PROFILES_COLLECTION = "ai_profiles"
_ASSIGNMENTS_COLLECTION = "ai_profile_assignments"


@dataclass
class AIContextProfile:
    """Named profile that controls which tools are available for an AI interaction."""

    name: str
    description: str = ""
    tool_mode: str = "all"  # "all" | "include" | "exclude"
    tools: list[str] = field(default_factory=list)
    tool_roles: dict[str, str] = field(default_factory=dict)


# Built-in profiles seeded on first start
_BUILTIN_PROFILES = [
    AIContextProfile(
        name="default",
        description="All tools available — fallback for unassigned calls",
        tool_mode="all",
    ),
    AIContextProfile(
        name="human_chat",
        description="Human conversations via web or Slack — excludes internal service tools",
        tool_mode="exclude",
        tools=["sales_lead"],
    ),
    AIContextProfile(
        name="text_only",
        description="Text generation only, no tool access",
        tool_mode="include",
        tools=[],
    ),
    AIContextProfile(
        name="sales_agent",
        description="Sales lead qualification pipeline",
        tool_mode="include",
        tools=["sales_lead"],
    ),
]

# Built-in call→profile assignments seeded on first start
_BUILTIN_ASSIGNMENTS: dict[str, str] = {
    "human_chat": "human_chat",
    "greeting": "text_only",
    "roast": "default",
}


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
        self._persona_svc: Any | None = None
        self._acl_svc: Any | None = None
        self._current_conversation_id: str | None = None
        # AI context profiles
        self._profiles: dict[str, AIContextProfile] = {}
        self._assignments: dict[str, str] = {}  # call_name -> profile_name

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ai",
            capabilities=frozenset({"ai_chat", "ai_tools"}),
            requires=frozenset({"credentials", "entity_storage", "persona"}),
            optional=frozenset({"ai_tools", "configuration", "access_control"}),
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

        # Resolve persona service
        self._persona_svc = resolver.require_capability("persona")

        # Resolve access control (optional — if missing, no filtering)
        self._acl_svc = resolver.get_capability("access_control")

        # Save resolver for lazy tool discovery
        self._resolver = resolver

        # Load profiles and assignments
        await self._load_profiles()

        logger.info(
            "AI service started (credential=%s, profiles=%d, assignments=%d)",
            self._credential_name,
            len(self._profiles),
            len(self._assignments),
        )

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

    # --- AI Context Profiles ---

    async def _load_profiles(self) -> None:
        """Load profiles and assignments from storage, seeding built-ins on first run."""
        if self._storage is None:
            # No storage — use built-ins in memory only
            self._profiles = {p.name: p for p in _BUILTIN_PROFILES}
            self._assignments = dict(_BUILTIN_ASSIGNMENTS)
            return

        # Seed built-in profiles if they don't exist yet
        for bp in _BUILTIN_PROFILES:
            existing = await self._storage.get(_PROFILES_COLLECTION, bp.name)
            if existing is None:
                await self._storage.put(_PROFILES_COLLECTION, bp.name, {
                    "name": bp.name,
                    "description": bp.description,
                    "tool_mode": bp.tool_mode,
                    "tools": bp.tools,
                    "tool_roles": bp.tool_roles,
                })

        # Seed built-in assignments
        for call_name, profile_name in _BUILTIN_ASSIGNMENTS.items():
            existing = await self._storage.get(_ASSIGNMENTS_COLLECTION, call_name)
            if existing is None:
                await self._storage.put(_ASSIGNMENTS_COLLECTION, call_name, {
                    "call_name": call_name,
                    "profile": profile_name,
                })

        # Also seed from config (config overrides built-ins)
        config_svc = self._resolver.get_capability("configuration") if self._resolver else None
        if config_svc is not None:
            get_section = getattr(config_svc, "get_section", None)
            if get_section:
                ai_section = get_section("ai")
                config_profiles = ai_section.get("profiles", {})
                for name, pdata in config_profiles.items():
                    if isinstance(pdata, dict):
                        await self._storage.put(_PROFILES_COLLECTION, name, {
                            "name": name,
                            "description": pdata.get("description", ""),
                            "tool_mode": pdata.get("tool_mode", "all"),
                            "tools": pdata.get("tools", []),
                            "tool_roles": pdata.get("tool_roles", {}),
                        })

        # Load all profiles from storage
        await self._refresh_profiles()

    async def _refresh_profiles(self) -> None:
        """Reload profiles and assignments from storage into memory."""
        if self._storage is None:
            return

        # Load profiles
        profile_docs = await self._storage.query(Query(collection=_PROFILES_COLLECTION))
        self._profiles = {}
        for doc in profile_docs:
            name = doc.get("name", "")
            if name:
                self._profiles[name] = AIContextProfile(
                    name=name,
                    description=doc.get("description", ""),
                    tool_mode=doc.get("tool_mode", "all"),
                    tools=doc.get("tools", []),
                    tool_roles=doc.get("tool_roles", {}),
                )

        # Load assignments
        assignment_docs = await self._storage.query(Query(collection=_ASSIGNMENTS_COLLECTION))
        self._assignments = {}
        for doc in assignment_docs:
            call_name = doc.get("call_name", "")
            profile = doc.get("profile", "")
            if call_name and profile:
                self._assignments[call_name] = profile

    def get_profile(self, ai_call: str | None) -> AIContextProfile | None:
        """Resolve the profile for an AI call. Returns None if no profile applies."""
        if ai_call is None:
            return None
        profile_name = self._assignments.get(ai_call, "default")
        return self._profiles.get(profile_name)

    def list_profiles(self) -> list[AIContextProfile]:
        """List all defined profiles."""
        return sorted(self._profiles.values(), key=lambda p: p.name)

    def list_assignments(self) -> dict[str, str]:
        """List all call→profile assignments."""
        return dict(self._assignments)

    async def set_profile(self, profile: AIContextProfile) -> None:
        """Create or update a profile."""
        if self._storage is not None:
            await self._storage.put(_PROFILES_COLLECTION, profile.name, {
                "name": profile.name,
                "description": profile.description,
                "tool_mode": profile.tool_mode,
                "tools": profile.tools,
                "tool_roles": profile.tool_roles,
            })
        self._profiles[profile.name] = profile
        logger.info("Profile '%s' saved (mode=%s, tools=%d)", profile.name, profile.tool_mode, len(profile.tools))

    async def delete_profile(self, name: str) -> None:
        """Delete a profile."""
        if name == "default":
            raise ValueError("Cannot delete the 'default' profile")
        if self._storage is not None:
            await self._storage.delete(_PROFILES_COLLECTION, name)
        self._profiles.pop(name, None)
        logger.info("Profile '%s' deleted", name)

    async def set_assignment(self, call_name: str, profile_name: str) -> None:
        """Assign a profile to an AI call."""
        if profile_name not in self._profiles:
            raise ValueError(f"Unknown profile: {profile_name}")
        if self._storage is not None:
            await self._storage.put(_ASSIGNMENTS_COLLECTION, call_name, {
                "call_name": call_name,
                "profile": profile_name,
            })
        self._assignments[call_name] = profile_name
        logger.info("Call '%s' assigned to profile '%s'", call_name, profile_name)

    async def clear_assignment(self, call_name: str) -> None:
        """Remove a call→profile assignment (reverts to default)."""
        if self._storage is not None:
            await self._storage.delete(_ASSIGNMENTS_COLLECTION, call_name)
        self._assignments.pop(call_name, None)
        logger.info("Call '%s' assignment cleared", call_name)

    # --- Chat ---

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        user_ctx: UserContext | None = None,
        system_prompt: str | None = None,
        ai_call: str | None = None,
    ) -> tuple[str, str]:
        """Send a user message and get an AI response (with full agentic loop).

        Args:
            user_message: The user's input text.
            conversation_id: Existing conversation ID, or None to start new.
            user_ctx: Optional user context. Falls back to contextvar if None.
            system_prompt: Override the system prompt entirely. When ``None``,
                uses the default persona + user memories.
            ai_call: Named AI interaction. Resolved to an AI context profile
                that controls which tools are available and their role
                requirements. When ``None``, all tools are available.

        Returns:
            (response_text, conversation_id) tuple.
        """
        if user_ctx is None:
            user_ctx = get_current_user()
        # Load or create conversation
        if conversation_id:
            messages = await self._load_conversation(conversation_id)
        else:
            conversation_id = str(uuid.uuid4())
            messages = []

        self._current_conversation_id = conversation_id

        # Append user message
        messages.append(Message(role=MessageRole.USER, content=user_message))

        # Resolve profile for this AI call
        profile = self.get_profile(ai_call)

        # Discover and filter tools based on profile
        tools_by_name = self._discover_tools(user_ctx=user_ctx, profile=profile)

        tool_defs = [defn for _, defn in tools_by_name.values()]

        # Resolve system prompt
        effective_prompt = (
            system_prompt
            if system_prompt is not None
            else await self._build_system_prompt(user_ctx=user_ctx)
        )

        # Agentic loop
        response: AIResponse | None = None
        for round_num in range(self._max_tool_rounds):
            truncated = self._truncate_history(messages)

            request = AIRequest(
                messages=truncated,
                system_prompt=effective_prompt,
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
                response.message.tool_calls, tools_by_name,
                user_ctx=user_ctx, profile=profile,
            )
            messages.append(Message(role=MessageRole.TOOL_RESULT, tool_results=tool_results))
        else:
            logger.warning(
                "Agentic loop hit max rounds (%d) for conversation %s",
                self._max_tool_rounds,
                conversation_id,
            )

        # Persist conversation with user ownership
        await self._save_conversation(conversation_id, messages, user_ctx)

        # Return final text response
        final_text = response.message.content if response else ""
        return final_text, conversation_id

    # --- System Prompt ---

    async def _build_system_prompt(self, user_ctx: UserContext | None = None) -> str:
        """Build the full system prompt: base identity, persona, and user memories."""
        parts: list[str] = []
        if self._system_prompt:
            parts.append(self._system_prompt)
        if self._persona_svc is not None:
            from gilbert.core.services.persona import PersonaService

            if isinstance(self._persona_svc, PersonaService):
                parts.append(self._persona_svc.persona)
                if not self._persona_svc.is_customized:
                    parts.append(
                        "IMPORTANT: The persona has not been customized yet. "
                        "At the start of the FIRST conversation only, briefly let the user know "
                        "they can customize your personality and behavior by asking you to "
                        "update the persona. Only mention this once — never bring it up again "
                        "in subsequent messages or conversations."
                    )

        # Inject user memory summaries if available
        if user_ctx and user_ctx.user_id not in ("system", "guest") and self._resolver:
            memory_svc = self._resolver.get_capability("user_memory")
            if memory_svc is not None:
                try:
                    from gilbert.core.services.memory import MemoryService

                    if isinstance(memory_svc, MemoryService):
                        summaries = await memory_svc.get_user_summaries(user_ctx.user_id)
                        if summaries:
                            parts.append(summaries)
                except Exception:
                    pass  # Memory unavailable — not critical

        return "\n\n".join(parts) if parts else ""

    # --- Tool Discovery ---

    def _discover_tools(
        self,
        user_ctx: UserContext | None = None,
        profile: AIContextProfile | None = None,
    ) -> dict[str, tuple[ToolProvider, ToolDefinition]]:
        """Find all started services that implement ToolProvider and collect their tools.

        If a *profile* is provided, tools are filtered by its tool_mode:
        - ``all``: all tools (RBAC still applies)
        - ``include``: only tools named in ``profile.tools``
        - ``exclude``: all tools except those named in ``profile.tools``

        If the profile defines ``tool_roles``, those override each tool's
        ``required_role`` for RBAC checks within this call.
        """
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

        # Apply profile tool filtering
        if profile is not None:
            if profile.tool_mode == "include":
                include_set = set(profile.tools)
                tools_by_name = {
                    name: v for name, v in tools_by_name.items()
                    if name in include_set
                }
            elif profile.tool_mode == "exclude":
                exclude_set = set(profile.tools)
                tools_by_name = {
                    name: v for name, v in tools_by_name.items()
                    if name not in exclude_set
                }
            # "all" = no filtering

        # Apply RBAC permissions (with optional profile role overrides)
        if user_ctx is not None and self._acl_svc is not None:
            from gilbert.core.services.access_control import AccessControlService

            if isinstance(self._acl_svc, AccessControlService):
                tool_roles = profile.tool_roles if profile else {}
                before = len(tools_by_name)
                filtered: dict[str, tuple[ToolProvider, ToolDefinition]] = {}
                for name, (prov, tdef) in tools_by_name.items():
                    # Use profile role override if present, else tool's default
                    effective_role = tool_roles.get(name, tdef.required_role)
                    role_level = self._acl_svc.get_role_level(effective_role)
                    user_level = self._acl_svc.get_effective_level(user_ctx)
                    if user_level <= role_level:
                        filtered[name] = (prov, tdef)
                removed = before - len(filtered)
                if removed:
                    logger.debug(
                        "Filtered %d tools for user %s (effective level %d)",
                        removed, user_ctx.user_id,
                        self._acl_svc.get_effective_level(user_ctx),
                    )
                tools_by_name = filtered

        return tools_by_name

    # --- Tool Execution ---

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        tools_by_name: dict[str, tuple[ToolProvider, ToolDefinition]],
        user_ctx: UserContext | None = None,
        profile: AIContextProfile | None = None,
    ) -> list[ToolResult]:
        """Execute a batch of tool calls and return results."""
        results: list[ToolResult] = []
        tool_roles = profile.tool_roles if profile else {}

        for tc in tool_calls:
            provider_and_def = tools_by_name.get(tc.tool_name)
            if provider_and_def is None:
                results.append(ToolResult(
                    tool_call_id=tc.tool_call_id,
                    content=f"Error: unknown tool '{tc.tool_name}'",
                    is_error=True,
                ))
                continue
            provider, tool_def = provider_and_def

            # Defense in depth: re-check permission before execution.
            # Uses profile tool_roles overrides for consistency with _discover_tools.
            if user_ctx is not None and user_ctx.user_id != "system" and self._acl_svc is not None:
                from gilbert.core.services.access_control import AccessControlService

                if isinstance(self._acl_svc, AccessControlService):
                    effective_role = tool_roles.get(tc.tool_name, tool_def.required_role)
                    role_level = self._acl_svc.get_role_level(effective_role)
                    user_level = self._acl_svc.get_effective_level(user_ctx)
                    if user_level > role_level:
                        results.append(ToolResult(
                            tool_call_id=tc.tool_call_id,
                            content=f"Permission denied: tool '{tc.tool_name}' requires higher privileges",
                            is_error=True,
                        ))
                        continue

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

    async def _save_conversation(
        self,
        conv_id: str,
        messages: list[Message],
        user_ctx: UserContext | None = None,
    ) -> None:
        """Persist a conversation to storage with optional user ownership."""
        if self._storage is None:
            return
        # Load existing data to preserve fields like title
        existing = await self._storage.get(_COLLECTION, conv_id) or {}
        data: dict[str, Any] = {
            **existing,
            "messages": [self._serialize_message(m) for m in messages],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if user_ctx is not None and user_ctx.user_id != "system":
            data["user_id"] = user_ctx.user_id
        await self._storage.put(_COLLECTION, conv_id, data)

    async def list_conversations(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List conversations, optionally filtered by owning user."""
        if self._storage is None:
            return []
        filters: list[Filter] = []
        if user_id:
            filters.append(Filter(field="user_id", op=FilterOp.EQ, value=user_id))
        return await self._storage.query(
            Query(
                collection=_COLLECTION,
                filters=filters,
                sort=[SortField(field="updated_at", descending=True)],
                limit=limit,
            )
        )

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

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "ai"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="rename_conversation",
                description="Rename the current chat conversation to a user-specified title.",
                parameters=[
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="The new title for this conversation.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="list_ai_profiles",
                description="List all AI context profiles and their call assignments.",
                required_role="admin",
            ),
            ToolDefinition(
                name="set_ai_profile",
                description=(
                    "Create or update an AI context profile. "
                    "tool_mode: 'all' (every tool), 'include' (only listed), 'exclude' (all except listed). "
                    "tool_roles: per-tool role overrides within this profile."
                ),
                parameters=[
                    ToolParameter(name="name", type=ToolParameterType.STRING, description="Profile name."),
                    ToolParameter(name="description", type=ToolParameterType.STRING, description="What this profile is for.", required=False),
                    ToolParameter(name="tool_mode", type=ToolParameterType.STRING, description="'all', 'include', or 'exclude'.", required=False, enum=["all", "include", "exclude"]),
                    ToolParameter(name="tools", type=ToolParameterType.ARRAY, description="Tool names for include/exclude mode.", required=False),
                    ToolParameter(name="tool_roles", type=ToolParameterType.OBJECT, description="Per-tool role overrides: {tool_name: role_name}.", required=False),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="delete_ai_profile",
                description="Delete an AI context profile. The 'default' profile cannot be deleted.",
                parameters=[
                    ToolParameter(name="name", type=ToolParameterType.STRING, description="Profile name to delete."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="assign_ai_profile",
                description="Assign an AI context profile to a named AI call (e.g., 'human_chat', 'sales_initial_email').",
                parameters=[
                    ToolParameter(name="call_name", type=ToolParameterType.STRING, description="The AI call name."),
                    ToolParameter(name="profile", type=ToolParameterType.STRING, description="Profile name to assign."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="clear_ai_assignment",
                description="Remove a call's profile assignment, reverting it to the 'default' profile.",
                parameters=[
                    ToolParameter(name="call_name", type=ToolParameterType.STRING, description="The AI call name."),
                ],
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "rename_conversation":
                return await self._tool_rename_conversation(arguments)
            case "list_ai_profiles":
                return self._tool_list_profiles()
            case "set_ai_profile":
                return await self._tool_set_profile(arguments)
            case "delete_ai_profile":
                return await self._tool_delete_profile(arguments)
            case "assign_ai_profile":
                return await self._tool_assign_profile(arguments)
            case "clear_ai_assignment":
                return await self._tool_clear_assignment(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    def _tool_list_profiles(self) -> str:
        profiles = []
        for p in self.list_profiles():
            profiles.append({
                "name": p.name,
                "description": p.description,
                "tool_mode": p.tool_mode,
                "tools": p.tools,
                "tool_roles": p.tool_roles,
            })
        return _json.dumps({
            "profiles": profiles,
            "assignments": self.list_assignments(),
        })

    async def _tool_set_profile(self, arguments: dict[str, Any]) -> str:
        name = arguments.get("name", "").strip()
        if not name:
            return _json.dumps({"error": "Profile name is required"})
        existing = self._profiles.get(name)
        profile = AIContextProfile(
            name=name,
            description=arguments.get("description", existing.description if existing else ""),
            tool_mode=arguments.get("tool_mode", existing.tool_mode if existing else "all"),
            tools=arguments.get("tools", existing.tools if existing else []),
            tool_roles=arguments.get("tool_roles", existing.tool_roles if existing else {}),
        )
        await self.set_profile(profile)
        return _json.dumps({"status": "saved", "profile": name})

    async def _tool_delete_profile(self, arguments: dict[str, Any]) -> str:
        try:
            await self.delete_profile(arguments["name"])
            return _json.dumps({"status": "deleted"})
        except (KeyError, ValueError) as e:
            return _json.dumps({"error": str(e)})

    async def _tool_assign_profile(self, arguments: dict[str, Any]) -> str:
        try:
            await self.set_assignment(arguments["call_name"], arguments["profile"])
            return _json.dumps({"status": "assigned"})
        except ValueError as e:
            return _json.dumps({"error": str(e)})

    async def _tool_clear_assignment(self, arguments: dict[str, Any]) -> str:
        await self.clear_assignment(arguments["call_name"])
        return _json.dumps({"status": "cleared"})

    async def _tool_rename_conversation(self, arguments: dict[str, Any]) -> str:
        title = arguments.get("title", "").strip()
        if not title:
            return _json.dumps({"error": "Title is required"})
        if not self._current_conversation_id or not self._storage:
            return _json.dumps({"error": "No active conversation"})

        data = await self._storage.get("ai_conversations", self._current_conversation_id)
        if data is None:
            return _json.dumps({"error": "Conversation not found"})

        data["title"] = title
        await self._storage.put("ai_conversations", self._current_conversation_id, data)

        # Emit event so WebSocket clients can update their UI
        if self._resolver:
            event_bus_svc = self._resolver.get_capability("event_bus")
            if event_bus_svc is not None:
                from gilbert.core.services.event_bus import EventBusService
                from gilbert.interfaces.events import Event

                if isinstance(event_bus_svc, EventBusService):
                    await event_bus_svc.bus.publish(Event(
                        event_type="chat.conversation.renamed",
                        data={
                            "conversation_id": self._current_conversation_id,
                            "title": title,
                        },
                        source="ai",
                    ))

        return _json.dumps({"status": "renamed", "title": title})

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
