"""AI service — orchestrates AI conversations, tool execution, and persistence."""

import json as _json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
            capabilities=frozenset({"ai_chat", "ai_tools", "ws_handlers"}),
            requires=frozenset({"credentials", "entity_storage", "persona"}),
            optional=frozenset({"ai_tools", "configuration", "access_control"}),
            events=frozenset({"chat.conversation.renamed"}),
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
    ) -> tuple[str, str, list[dict[str, Any]]]:
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
            (response_text, conversation_id, ui_blocks) tuple.  ``ui_blocks``
            is a list of serialized UI block dicts (possibly empty).
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

        # Resolve system prompt — always prepend current date/time
        date_ctx = self._current_datetime_context()
        if system_prompt is not None:
            effective_prompt = f"{date_ctx}\n\n{system_prompt}"
        else:
            effective_prompt = await self._build_system_prompt(user_ctx=user_ctx)

        # Agentic loop
        from gilbert.interfaces.ui import UIBlock

        response: AIResponse | None = None
        all_ui_blocks: list[UIBlock] = []

        for round_num in range(self._max_tool_rounds):
            truncated = self._truncate_history(messages)

            # Dynamically append conversation state each round so tool-call
            # mutations are visible to subsequent AI rounds.
            conv_state = await self._load_conversation_state(conversation_id)
            if conv_state:
                round_prompt = (
                    f"{effective_prompt}\n\n"
                    f"{self._format_state_for_context(conv_state)}"
                )
            else:
                round_prompt = effective_prompt

            request = AIRequest(
                messages=truncated,
                system_prompt=round_prompt,
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
            tool_results, round_ui_blocks = await self._execute_tool_calls(
                response.message.tool_calls, tools_by_name,
                user_ctx=user_ctx, profile=profile,
            )
            all_ui_blocks.extend(round_ui_blocks)
            messages.append(Message(role=MessageRole.TOOL_RESULT, tool_results=tool_results))
        else:
            logger.warning(
                "Agentic loop hit max rounds (%d) for conversation %s",
                self._max_tool_rounds,
                conversation_id,
            )

        # Count assistant messages to determine response_index for UI blocks
        assistant_count = sum(1 for m in messages if m.role == MessageRole.ASSISTANT)
        response_index = max(0, assistant_count - 1)

        # Serialize UI blocks with position and submission state
        ui_block_dicts: list[dict[str, Any]] = []
        for block in all_ui_blocks:
            d = block.to_dict()
            d["response_index"] = response_index
            d["submitted"] = False
            d["submission"] = None
            ui_block_dicts.append(d)

        # Persist conversation with user ownership and UI blocks
        await self._save_conversation(
            conversation_id, messages, user_ctx, ui_blocks=ui_block_dicts,
        )

        # Return final text response
        final_text = response.message.content if response else ""
        return final_text, conversation_id, ui_block_dicts

    # --- System Prompt ---

    @staticmethod
    def _current_datetime_context() -> str:
        """Build a date/time context string in Los Angeles timezone."""
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo("America/Los_Angeles"))
        except Exception:
            now = datetime.now(timezone.utc)
        today = now.strftime("%A, %B %d, %Y")
        time_str = now.strftime("%I:%M %p %Z")
        yesterday = (now - timedelta(days=1)).strftime("%A, %B %d, %Y")
        return (
            f"Current date and time: {today} at {time_str}. "
            f"Yesterday was {yesterday}."
        )

    async def _build_system_prompt(self, user_ctx: UserContext | None = None) -> str:
        """Build the full system prompt: base identity, persona, and user memories."""
        parts: list[str] = []

        # Always inject current date/time first
        parts.append(self._current_datetime_context())

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
    ) -> tuple[list[ToolResult], list["UIBlock"]]:
        """Execute a batch of tool calls and return results + any UI blocks."""
        from gilbert.interfaces.ui import ToolOutput, UIBlock

        results: list[ToolResult] = []
        ui_blocks: list[UIBlock] = []
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

            # Inject user context so tools can identify the caller
            if user_ctx is not None:
                tc.arguments["_user_id"] = user_ctx.user_id
                tc.arguments["_user_name"] = user_ctx.display_name

            try:
                raw_result = await provider.execute_tool(tc.tool_name, tc.arguments)

                # Normalize: tools may return str (backward compat) or ToolOutput
                if isinstance(raw_result, ToolOutput):
                    result_text = raw_result.text
                    for block in raw_result.ui_blocks:
                        import dataclasses as _dc
                        # Auto-assign block_id if missing
                        if not block.block_id:
                            block = _dc.replace(block, block_id=str(uuid.uuid4()))
                        # Tag with tool name if not set
                        if not block.tool_name:
                            block = _dc.replace(block, tool_name=tc.tool_name)
                        ui_blocks.append(block)
                else:
                    result_text = raw_result

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
        return results, ui_blocks

    # --- Conversation Persistence ---

    async def _save_conversation(
        self,
        conv_id: str,
        messages: list[Message],
        user_ctx: UserContext | None = None,
        ui_blocks: list[dict[str, Any]] | None = None,
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

        # Merge new UI blocks with any existing ones
        if ui_blocks:
            existing_blocks: list[dict[str, Any]] = data.get("ui_blocks", [])
            existing_blocks.extend(ui_blocks)
            data["ui_blocks"] = existing_blocks

        await self._storage.put(_COLLECTION, conv_id, data)

    async def list_conversations(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List personal (non-shared) conversations, optionally filtered by owning user."""
        if self._storage is None:
            return []
        filters: list[Filter] = []
        if user_id:
            filters.append(Filter(field="user_id", op=FilterOp.EQ, value=user_id))
        results = await self._storage.query(
            Query(
                collection=_COLLECTION,
                filters=filters,
                sort=[SortField(field="updated_at", descending=True)],
                limit=limit * 2,  # fetch extra to account for shared filtering
            )
        )
        # Exclude shared conversations — those are listed separately.
        # Can't use NEQ filter because shared=None (missing field) doesn't match.
        return [c for c in results if not c.get("shared")][:limit]

    async def list_shared_conversations(
        self, user_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List shared conversations visible to user_id.

        Returns conversations where the user is a member, plus public rooms
        they haven't joined yet (so they can see and join them).
        """
        if self._storage is None:
            return []
        shared = await self._storage.query(
            Query(
                collection=_COLLECTION,
                filters=[Filter(field="shared", op=FilterOp.EQ, value=True)],
                sort=[SortField(field="updated_at", descending=True)],
                limit=200,
            )
        )
        results = []
        for conv in shared:
            members = conv.get("members", [])
            invites = conv.get("invites", [])
            is_member = any(m.get("user_id") == user_id for m in members)
            is_invited = any(inv.get("user_id") == user_id for inv in invites)
            is_public = conv.get("visibility") == "public"
            if is_member or is_invited or is_public:
                conv["_is_member"] = is_member
                conv["_is_invited"] = is_invited
                results.append(conv)
                if len(results) >= limit:
                    break
        return results

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
        if msg.author_id:
            d["author_id"] = msg.author_id
        if msg.author_name:
            d["author_name"] = msg.author_name
        if msg.visible_to is not None:
            d["visible_to"] = msg.visible_to
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
            author_id=data.get("author_id", ""),
            author_name=data.get("author_name", ""),
            visible_to=data.get("visible_to"),
        )

    # --- Conversation State ---

    def _resolve_conversation_id(self, conversation_id: str | None) -> str:
        """Resolve to an explicit or the current conversation ID."""
        cid = conversation_id or self._current_conversation_id
        if not cid:
            raise RuntimeError("No active conversation")
        return cid

    async def get_conversation_state(
        self, key: str, conversation_id: str | None = None,
    ) -> Any | None:
        """Read a state entry from a conversation.

        Args:
            key: Namespace key (e.g. ``"guess_game"``).
            conversation_id: Explicit conversation ID, or ``None`` to use the
                currently active conversation.

        Returns:
            The stored value, or ``None`` if the key doesn't exist.
        """
        if self._storage is None:
            return None
        cid = self._resolve_conversation_id(conversation_id)
        data = await self._storage.get(_COLLECTION, cid)
        if data is None:
            return None
        return data.get("state", {}).get(key)

    async def set_conversation_state(
        self, key: str, value: Any, conversation_id: str | None = None,
    ) -> None:
        """Write a state entry to a conversation.

        The value must be JSON-serialisable.  It is persisted immediately so
        that subsequent agentic-loop rounds see the update.

        Args:
            key: Namespace key (e.g. ``"guess_game"``).
            value: Any JSON-serialisable value.
            conversation_id: Explicit conversation ID, or ``None`` to use the
                currently active conversation.
        """
        if self._storage is None:
            return
        cid = self._resolve_conversation_id(conversation_id)
        data = await self._storage.get(_COLLECTION, cid) or {}
        state: dict[str, Any] = data.get("state", {})
        state[key] = value
        data["state"] = state
        await self._storage.put(_COLLECTION, cid, data)

    async def clear_conversation_state(
        self, key: str, conversation_id: str | None = None,
    ) -> None:
        """Remove a state entry from a conversation.

        Args:
            key: Namespace key to remove.
            conversation_id: Explicit conversation ID, or ``None`` to use the
                currently active conversation.
        """
        if self._storage is None:
            return
        cid = self._resolve_conversation_id(conversation_id)
        data = await self._storage.get(_COLLECTION, cid)
        if data is None:
            return
        state: dict[str, Any] = data.get("state", {})
        if key in state:
            del state[key]
            data["state"] = state
            await self._storage.put(_COLLECTION, cid, data)

    async def _load_conversation_state(self, conv_id: str) -> dict[str, Any]:
        """Load all state entries for a conversation."""
        if self._storage is None:
            return {}
        data = await self._storage.get(_COLLECTION, conv_id)
        if data is None:
            return {}
        return data.get("state", {})

    @staticmethod
    def _format_state_for_context(state: dict[str, Any]) -> str:
        """Render conversation state as a text block for the system prompt."""
        parts: list[str] = ["## Active Conversation State"]
        for key, value in state.items():
            parts.append(f"\n### {key}")
            if isinstance(value, (dict, list)):
                parts.append(_json.dumps(value, indent=2, default=str))
            else:
                parts.append(str(value))
        return "\n".join(parts)

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

    # --- WebSocket RPC handlers ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "chat.message.send": self._ws_chat_send,
            "chat.form.submit": self._ws_form_submit,
            "chat.history.load": self._ws_history_load,
            "chat.conversation.list": self._ws_conversation_list,
            "chat.conversation.create": self._ws_conversation_create,
            "chat.conversation.rename": self._ws_conversation_rename,
            "chat.conversation.delete": self._ws_conversation_delete,
            "chat.room.create": self._ws_room_create,
            "chat.room.join": self._ws_room_join,
            "chat.room.leave": self._ws_room_leave,
            "chat.room.kick": self._ws_room_kick,
            "chat.room.invite": self._ws_room_invite,
            "chat.room.invite_revoke": self._ws_room_invite_revoke,
            "chat.room.invite_respond": self._ws_room_invite_respond,
            "chat.user.list": self._ws_chat_list_users,
        }

    async def _ws_chat_send(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        message = frame.get("message", "").strip()
        if not message:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "message is required", "code": 400}

        conversation_id = frame.get("conversation_id") or None

        # Check if this is a shared room
        is_shared = False
        conv_data = None
        if conversation_id and self._storage:
            conv_data = await self._storage.get(_COLLECTION, conversation_id)
            if conv_data:
                is_shared = conv_data.get("shared", False)

        try:
            if is_shared:
                from gilbert.web.chat_helpers import mentions_gilbert, build_room_context, publish_event

                addressed = mentions_gilbert(message)
                tagged_message = f"[{conn.user_ctx.display_name}]: {message}"

                response_text = ""
                ui_blocks: list[dict[str, Any]] = []

                if addressed:
                    response_text, conv_id, ui_blocks = await self.chat(
                        user_message=tagged_message,
                        conversation_id=conversation_id,
                        user_ctx=conn.user_ctx,
                        system_prompt=build_room_context(conv_data, conn.user_ctx),
                        ai_call="human_chat",
                    )
                else:
                    # Store message without invoking AI
                    conv_id = conversation_id
                    messages = await self._load_conversation(conversation_id)
                    messages.append(Message(
                        role=MessageRole.USER, content=tagged_message,
                        author_id=conn.user_ctx.user_id,
                        author_name=conn.user_ctx.display_name,
                    ))
                    await self._save_conversation(conv_id, messages, user_ctx=conn.user_ctx)

                # Broadcast to room members
                gilbert = conn.manager._gilbert
                if gilbert:
                    await publish_event(gilbert, "chat.message.created", {
                        "conversation_id": conv_id,
                        "author_id": conn.user_ctx.user_id,
                        "author_name": conn.user_ctx.display_name,
                        "content": response_text,
                        "user_message": message,
                        "ui_blocks": ui_blocks,
                    })
            else:
                # Personal chat — normal AI flow
                response_text, conv_id, ui_blocks = await self.chat(
                    user_message=message,
                    conversation_id=conversation_id,
                    user_ctx=conn.user_ctx,
                    ai_call="human_chat",
                )
        except Exception as exc:
            logger.warning("chat.message.send failed", exc_info=True)
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": str(exc), "code": 500}

        return {
            "type": "chat.message.send.result",
            "ref": frame.get("id"),
            "response": response_text,
            "conversation_id": conv_id,
            "ui_blocks": ui_blocks,
        }

    async def _ws_conversation_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Create an empty named personal conversation."""
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        title = (frame.get("title") or "").strip() or "New conversation"

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        conv_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await self._storage.put(_COLLECTION, conv_id, {
            "title": title,
            "user_id": conn.user_ctx.user_id,
            "messages": [],
            "created_at": now,
            "updated_at": now,
        })

        return {
            "type": "chat.conversation.create.result",
            "ref": frame.get("id"),
            "conversation_id": conv_id,
            "title": title,
        }

    async def _ws_form_submit(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        block_id = frame.get("block_id")
        values = frame.get("values", {})

        if not conversation_id or not block_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and block_id required", "code": 400}

        # Mark block as submitted in storage and check if shared room
        block_title = "Form"
        is_shared = False
        conv_data = None
        if self._storage is not None:
            conv_data = await self._storage.get(_COLLECTION, conversation_id)
            if conv_data:
                is_shared = conv_data.get("shared", False)
                for block in conv_data.get("ui_blocks", []):
                    if block.get("block_id") == block_id:
                        block["submitted"] = True
                        block["submission"] = values
                        block_title = block.get("title") or "Form"
                        break
                await self._storage.put(_COLLECTION, conversation_id, conv_data)

        # Build text message for AI
        form_message = f"[{conn.user_ctx.display_name} submitted: {block_title}]\n"
        for k, v in values.items():
            form_message += f"- {k}: {v}\n"

        try:
            system_prompt = None
            if is_shared and conv_data:
                from gilbert.web.chat_helpers import build_room_context
                system_prompt = build_room_context(conv_data, conn.user_ctx)

            response_text, conv_id, ui_blocks = await self.chat(
                user_message=form_message,
                conversation_id=conversation_id,
                user_ctx=conn.user_ctx,
                system_prompt=system_prompt,
                ai_call="human_chat",
            )
        except Exception as exc:
            logger.warning("chat.form.submit failed", exc_info=True)
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": str(exc), "code": 500}

        # Broadcast to room members in shared rooms
        if is_shared:
            from gilbert.web.chat_helpers import publish_event
            gilbert = conn.manager._gilbert
            if gilbert:
                await publish_event(gilbert, "chat.message.created", {
                    "conversation_id": conv_id,
                    "author_id": conn.user_ctx.user_id,
                    "author_name": conn.user_ctx.display_name,
                    "content": response_text,
                    "user_message": "",
                    "ui_blocks": ui_blocks,
                })

        return {
            "type": "chat.form.submit.result",
            "ref": frame.get("id"),
            "response": response_text,
            "conversation_id": conv_id,
            "ui_blocks": ui_blocks,
        }

    async def _ws_history_load(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        if not conversation_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Conversation not found", "code": 404}

        is_shared = data.get("shared", False)
        display_messages = []
        for m in data.get("messages", []):
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            visible_to = m.get("visible_to")
            if visible_to is not None and conn.user_id not in visible_to:
                continue
            msg: dict[str, Any] = {"role": role, "content": m.get("content", "")}
            if is_shared:
                msg["author_id"] = m.get("author_id", "")
                msg["author_name"] = m.get("author_name", "")
            display_messages.append(msg)

        ui_blocks = [
            b for b in data.get("ui_blocks", [])
            if (not b.get("for_user") or b.get("for_user") == conn.user_id)
            and b.get("exclude_user") != conn.user_id
        ]

        result: dict[str, Any] = {
            "type": "chat.history.load.result",
            "ref": frame.get("id"),
            "messages": display_messages,
            "ui_blocks": ui_blocks,
            "shared": is_shared,
            "title": data.get("title", ""),
        }
        if is_shared:
            result["members"] = data.get("members", [])
            result["invites"] = [
                {"user_id": inv["user_id"], "display_name": inv.get("display_name", "")}
                for inv in data.get("invites", [])
            ]
        return result

    async def _ws_conversation_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import conv_summary
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        personal = await self.list_conversations(user_id=conn.user_id, limit=30)
        shared = await self.list_shared_conversations(user_id=conn.user_id, limit=30)

        conversations = [conv_summary(c, shared=True) for c in shared]
        conversations += [conv_summary(c, shared=False) for c in personal]

        return {"type": "chat.conversation.list.result", "ref": frame.get("id"), "conversations": conversations}

    async def _ws_conversation_rename(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import check_conversation_access, publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        title = (frame.get("title") or "").strip()
        if not conversation_id or not title:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and title required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Conversation not found", "code": 404}

        err = check_conversation_access(data, conn.user_ctx)
        if err:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": err, "code": 403}

        data["title"] = title
        await self._storage.put(_COLLECTION, conversation_id, data)

        gilbert = conn.manager._gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.conversation.renamed", {"conversation_id": conversation_id, "title": title})

        return {"type": "chat.conversation.rename.result", "ref": frame.get("id"), "status": "ok", "title": title}

    async def _ws_conversation_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        if not conversation_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Conversation not found", "code": 404}
        if data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Use room destroy for shared conversations", "code": 400}
        conv_owner = data.get("user_id", "")
        if conv_owner and conn.user_id != "system" and conv_owner != conn.user_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Access denied", "code": 403}

        await self._storage.delete(_COLLECTION, conversation_id)
        return {"type": "chat.conversation.delete.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_room_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        title = (frame.get("title") or "").strip()
        if not title:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "title required", "code": 400}
        visibility = frame.get("visibility", "public")

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        import uuid as _uuid
        from datetime import datetime, timezone as tz
        conv_id = str(_uuid.uuid4())
        now = datetime.now(tz.utc).isoformat()

        members = [{"user_id": conn.user_id, "display_name": conn.user_ctx.display_name, "role": "owner", "joined_at": now}]
        data = {
            "shared": True,
            "visibility": visibility,
            "title": title,
            "user_id": conn.user_id,
            "members": members,
            "messages": [],
            "created_at": now,
            "updated_at": now,
        }
        await self._storage.put(_COLLECTION, conv_id, data)

        gilbert = conn.manager._gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.conversation.created", {
                "conversation_id": conv_id, "title": title, "shared": True,
                "members": members, "visibility": visibility,
            })

        return {
            "type": "chat.room.create.result", "ref": frame.get("id"),
            "conversation_id": conv_id, "title": title,
            "members": [{"user_id": m["user_id"], "display_name": m["display_name"]} for m in members],
        }

    async def _ws_room_join(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        if not conversation_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

        members = data.get("members", [])
        if any(m.get("user_id") == conn.user_id for m in members):
            return {"type": "chat.room.join.result", "ref": frame.get("id"), "status": "already_member"}

        from datetime import datetime, timezone as tz
        members.append({"user_id": conn.user_id, "display_name": conn.user_ctx.display_name, "role": "member", "joined_at": datetime.now(tz.utc).isoformat()})
        data["members"] = members
        await self._storage.put(_COLLECTION, conversation_id, data)

        gilbert = conn.manager._gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.member.joined", {
                "conversation_id": conversation_id, "user_id": conn.user_id,
                "display_name": conn.user_ctx.display_name,
            })

        return {"type": "chat.room.join.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_room_leave(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        if not conversation_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

        gilbert = conn.manager._gilbert

        # Owner leaving destroys the room
        if data.get("user_id") == conn.user_id:
            await self._storage.delete(_COLLECTION, conversation_id)
            if gilbert is not None:
                await publish_event(gilbert, "chat.conversation.destroyed", {"conversation_id": conversation_id})
            return {"type": "chat.room.leave.result", "ref": frame.get("id"), "status": "destroyed"}

        members = [m for m in data.get("members", []) if m.get("user_id") != conn.user_id]
        data["members"] = members
        await self._storage.put(_COLLECTION, conversation_id, data)
        if gilbert is not None:
            await publish_event(gilbert, "chat.member.left", {"conversation_id": conversation_id, "user_id": conn.user_id})

        return {"type": "chat.room.leave.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_room_kick(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        target_user = frame.get("user_id")
        if not conversation_id or not target_user:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and user_id required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}
        if data.get("user_id") != conn.user_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Only the room owner can kick members", "code": 403}

        members = [m for m in data.get("members", []) if m.get("user_id") != target_user]
        data["members"] = members
        await self._storage.put(_COLLECTION, conversation_id, data)

        gilbert = conn.manager._gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.member.kicked", {"conversation_id": conversation_id, "user_id": target_user})

        return {"type": "chat.room.kick.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_room_invite(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        user_ids = frame.get("user_ids", [])
        # Support single user_id for backwards compat
        if not user_ids and frame.get("user_id"):
            user_ids = [{"user_id": frame["user_id"], "display_name": frame.get("display_name", "")}]
        if not conversation_id or not user_ids:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and user_ids required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

        members = data.get("members", [])
        invites = data.get("invites", [])
        member_ids = {m.get("user_id") for m in members}
        invite_ids = {inv.get("user_id") for inv in invites}

        from datetime import datetime, timezone as tz
        now = datetime.now(tz.utc).isoformat()
        invited = []

        for entry in user_ids:
            target_user = entry.get("user_id") if isinstance(entry, dict) else entry
            display_name = entry.get("display_name", "") if isinstance(entry, dict) else ""
            if target_user in member_ids or target_user in invite_ids:
                continue
            invites.append({
                "user_id": target_user,
                "display_name": display_name,
                "invited_by": conn.user_id,
                "invited_at": now,
            })
            invite_ids.add(target_user)
            invited.append({"user_id": target_user, "display_name": display_name})

        data["invites"] = invites
        await self._storage.put(_COLLECTION, conversation_id, data)

        gilbert = conn.manager._gilbert
        if gilbert is not None:
            for inv in invited:
                await publish_event(gilbert, "chat.invite.created", {
                    "conversation_id": conversation_id,
                    "title": data.get("title", ""),
                    "user_id": inv["user_id"],
                    "display_name": inv["display_name"],
                    "invited_by": conn.user_id,
                    "invited_by_name": conn.user_ctx.display_name,
                })

        return {"type": "chat.room.invite.result", "ref": frame.get("id"), "status": "ok", "invited": invited}

    async def _ws_room_invite_revoke(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        target_user = frame.get("user_id")
        if not conversation_id or not target_user:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "conversation_id and user_id required", "code": 400,
            }

        if self._storage is None:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "Storage not available", "code": 503,
            }

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "Room not found", "code": 404,
            }

        invites = data.get("invites", [])
        data["invites"] = [
            inv for inv in invites if inv.get("user_id") != target_user
        ]
        await self._storage.put(_COLLECTION, conversation_id, data)

        return {
            "type": "chat.room.invite_revoke.result",
            "ref": frame.get("id"), "status": "ok",
        }

    async def _ws_room_invite_respond(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.web.chat_helpers import publish_event
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        action = frame.get("action")  # "accept" or "decline"
        if not conversation_id or action not in ("accept", "decline"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and action (accept/decline) required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

        invites = data.get("invites", [])
        invite = next((inv for inv in invites if inv.get("user_id") == conn.user_id), None)
        if invite is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "No pending invite found", "code": 404}

        # Remove the invite
        data["invites"] = [inv for inv in invites if inv.get("user_id") != conn.user_id]

        gilbert = conn.manager._gilbert

        if action == "accept":
            from datetime import datetime, timezone as tz
            members = data.get("members", [])
            members.append({
                "user_id": conn.user_id,
                "display_name": conn.user_ctx.display_name,
                "role": "member",
                "joined_at": datetime.now(tz.utc).isoformat(),
            })
            data["members"] = members
            await self._storage.put(_COLLECTION, conversation_id, data)

            if gilbert is not None:
                await publish_event(gilbert, "chat.member.joined", {
                    "conversation_id": conversation_id,
                    "user_id": conn.user_id,
                    "display_name": conn.user_ctx.display_name,
                })
        else:
            await self._storage.put(_COLLECTION, conversation_id, data)

            if gilbert is not None:
                await publish_event(gilbert, "chat.invite.declined", {
                    "conversation_id": conversation_id,
                    "user_id": conn.user_id,
                })

        return {"type": "chat.room.invite_respond.result", "ref": frame.get("id"), "status": "ok", "action": action}

    async def _ws_chat_list_users(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """List all users for invite modal."""
        from gilbert.web.ws_protocol import WsConnection
        conn: WsConnection = conn

        gilbert = conn.manager._gilbert
        if gilbert is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Service unavailable", "code": 503}

        user_svc = gilbert.service_manager.get_by_capability("users")
        if user_svc is None:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "User service unavailable", "code": 503,
            }

        users = await user_svc.list_users(limit=200)
        user_list = [
            {
                "user_id": u.get("_id", ""),
                "display_name": u.get("display_name", u.get("username", "")),
            }
            for u in users
            if u.get("_id") != "system"
        ]

        return {"type": "chat.user.list.result", "ref": frame.get("id"), "users": user_list}
