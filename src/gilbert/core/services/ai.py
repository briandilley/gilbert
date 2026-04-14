"""AI service — orchestrates AI conversations, tool execution, and persistence.

Also includes internal helpers for persona and user memory (previously
separate services, now merged into AIService).
"""

import json as _json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.core.slash_commands import (
    SlashCommandError,
    extract_command_name,
    format_usage,
    parse_slash_command,
)
from gilbert.interfaces.ai import (
    AIBackend,
    AIContextProfile,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Filter, FilterOp, IndexDefinition, Query, SortField, StorageBackend
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
    ToolResult,
)
from gilbert.interfaces.ui import ToolOutput, UIBlock

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")

_COLLECTION = "ai_conversations"
_PROFILES_COLLECTION = "ai_profiles"
_ASSIGNMENTS_COLLECTION = "ai_profile_assignments"

# ── Persona constants and helper ──────────────────────────────

_PERSONA_COLLECTION = "persona"
_PERSONA_ID = "active"

# Default persona shipped with Gilbert
DEFAULT_PERSONA = """\
You are Gilbert, a home and business automation assistant.

## Personality
- Casual, friendly, and professional.
- A bit sarcastic and occasionally funny — but never at the user's expense.
- Keep responses concise. Don't over-explain or narrate what you're doing under the hood.

## Announcements
- When making announcements over speakers after a period of silence, \
open with a brief, natural intro like "Hey team, Gilbert here" or \
"Quick heads up from Gilbert" — vary it each time, keep it fresh, \
don't repeat yourself.
- For rapid follow-up announcements, skip the intro.

## Data & information lookup
- Always check our own project data first before searching the web or \
saying you don't have information. Use project lookup and file search \
tools before falling back to web search.
- When someone asks to see a picture or image of something, first check \
if it matches a project name — then use the project files tool to find \
photos. Only search the web or knowledge base if project files come up empty.
- When someone asks about a person, vehicle, timeline, hours, or status, \
check the synced project data first — it's the most authoritative source \
for anything related to our work.

## Tool use
- When you use a tool, just confirm the result briefly. \
Don't reveal internal details (voice IDs, speaker UIDs, API endpoints, \
credential names, backend types) unless the user specifically asks about configuration.
- If something fails, give a clear, helpful message — not a stack trace.
- Only describe capabilities you actually have tools for. The tools available \
to you depend on the current user's role. If you don't have a tool for \
something, don't mention it at all — not even to say you can't do it. \
Just focus on what you CAN do.\
"""


class _PersonaHelper:
    """Internal helper — manages the AI persona text in entity storage."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._persona: str = DEFAULT_PERSONA
        self._is_customized: bool = False

    async def load(self) -> None:
        saved = await self._storage.get(_PERSONA_COLLECTION, _PERSONA_ID)
        if saved and saved.get("text"):
            self._persona = saved["text"]
            self._is_customized = saved.get("customized", False)
            logger.info("Persona loaded from storage (customized=%s)", self._is_customized)
        else:
            logger.info("No persona stored — using default")

    @property
    def persona(self) -> str:
        return self._persona

    @property
    def is_customized(self) -> bool:
        return self._is_customized

    async def update_persona(self, text: str) -> None:
        self._persona = text
        self._is_customized = True
        await self._storage.put(
            _PERSONA_COLLECTION, _PERSONA_ID, {"text": text, "customized": True}
        )
        logger.info("Persona updated (%d chars)", len(text))

    async def reset_persona(self) -> None:
        self._persona = DEFAULT_PERSONA
        self._is_customized = False
        await self._storage.put(
            _PERSONA_COLLECTION, _PERSONA_ID,
            {"text": DEFAULT_PERSONA, "customized": False},
        )
        logger.info("Persona reset to default")


# ── Memory helper ─────────────────────────────────────────────

_MEMORY_COLLECTION = "user_memories"


class _MemoryHelper:
    """Internal helper — per-user persistent memories."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def setup_indexes(self) -> None:
        await self._storage.ensure_index(IndexDefinition(
            collection=_MEMORY_COLLECTION,
            fields=["user_id"],
        ))

    async def get_user_summaries(self, user_id: str) -> str:
        memories = await self._get_user_memories(user_id)
        if not memories:
            return ""
        lines = [f"## Memories for this user ({len(memories)} stored)"]
        for m in memories:
            mid = m.get("_id", "")
            summary = m.get("summary", "")
            source = m.get("source", "user")
            lines.append(f"- [{mid}] {summary} ({source})")
        return "\n".join(lines)

    async def _get_user_memories(self, user_id: str) -> list[dict[str, Any]]:
        memories = await self._storage.query(Query(
            collection=_MEMORY_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
        ))

        def sort_key(m: dict[str, Any]) -> tuple[int, int, str]:
            source_rank = 0 if m.get("source") == "user" else 1
            access = -(m.get("access_count", 0))
            created = m.get("created_at", "")
            return (source_rank, access, created)

        memories.sort(key=sort_key)
        return memories

    async def remember(self, user_id: str, args: dict[str, Any]) -> str:
        summary = args.get("summary", "").strip()
        content = args.get("content", "").strip()
        source = args.get("source", "user")
        if not summary:
            return "I need a summary to remember."
        if not content:
            content = summary
        now = datetime.now(timezone.utc).isoformat()
        memory_id = f"memory_{uuid.uuid4().hex[:12]}"
        await self._storage.put(_MEMORY_COLLECTION, memory_id, {
            "memory_id": memory_id,
            "user_id": user_id,
            "summary": summary,
            "content": content,
            "source": source,
            "access_count": 0,
            "created_at": now,
            "updated_at": now,
        })
        logger.info("Memory created for %s: %s", user_id, summary[:60])
        return f"Got it, I'll remember that. (memory {memory_id})"

    async def recall(self, user_id: str, args: dict[str, Any]) -> str:
        ids: list[str] = args.get("ids", [])
        if not ids:
            return "I need memory IDs to recall. Use 'list' first to see available memories."
        results: list[str] = []
        for mid in ids:
            mid = str(mid)
            record = await self._storage.get(_MEMORY_COLLECTION, mid)
            if not record:
                results.append(f"[{mid}] Not found.")
                continue
            if record.get("user_id") != user_id:
                results.append(f"[{mid}] Not your memory.")
                continue
            record["access_count"] = record.get("access_count", 0) + 1
            await self._storage.put(_MEMORY_COLLECTION, mid, record)
            results.append(
                f"[{mid}] {record.get('summary', '')}\n"
                f"Content: {record.get('content', '')}\n"
                f"Source: {record.get('source', 'user')} | "
                f"Created: {record.get('created_at', '')} | "
                f"Accessed: {record['access_count']} times"
            )
        return "\n\n".join(results)

    async def update(self, user_id: str, args: dict[str, Any]) -> str:
        memory_id = args.get("id", "")
        if not memory_id:
            return "I need a memory ID to update."
        record = await self._storage.get(_MEMORY_COLLECTION, str(memory_id))
        if not record:
            return f"Memory {memory_id} not found."
        if record.get("user_id") != user_id:
            return f"Memory {memory_id} doesn't belong to you."
        summary = args.get("summary")
        content = args.get("content")
        if summary:
            record["summary"] = summary
        if content:
            record["content"] = content
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._storage.put(_MEMORY_COLLECTION, str(memory_id), record)
        logger.info("Memory updated for %s: %s", user_id, memory_id)
        return f"Memory {memory_id} updated."

    async def forget(self, user_id: str, args: dict[str, Any]) -> str:
        memory_id = args.get("id", "")
        if not memory_id:
            return "I need a memory ID to forget."
        record = await self._storage.get(_MEMORY_COLLECTION, str(memory_id))
        if not record:
            return f"Memory {memory_id} not found."
        if record.get("user_id") != user_id:
            return f"Memory {memory_id} doesn't belong to you."
        await self._storage.delete(_MEMORY_COLLECTION, str(memory_id))
        logger.info("Memory forgotten for %s: %s", user_id, memory_id)
        return f"Memory {memory_id} forgotten."

    async def list_memories(self, user_id: str) -> str:
        memories = await self._get_user_memories(user_id)
        if not memories:
            return "No memories stored for you yet."
        lines = [f"{len(memories)} memory/memories stored:"]
        for m in memories:
            mid = m.get("_id", "")
            summary = m.get("summary", "")
            source = m.get("source", "user")
            access = m.get("access_count", 0)
            lines.append(f"  [{mid}] {summary} ({source}) — accessed {access}x")
        return "\n".join(lines)


# Built-in profiles seeded on first start
_BUILTIN_PROFILES = [
    AIContextProfile(
        name="default",
        description="All tools available — fallback for unassigned calls",
        tool_mode="all",
    ),
    AIContextProfile(
        name="human_chat",
        description="Human conversations via web or Slack",
        tool_mode="all",
    ),
    AIContextProfile(
        name="text_only",
        description="Text generation only, no tool access",
        tool_mode="include",
        tools=[],
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

    def __init__(self) -> None:
        self._backend: AIBackend | None = None
        self._backend_name: str = "anthropic"
        self._enabled: bool = False
        # Tunable config — loaded from ConfigurationService during start()
        self._config: dict[str, Any] = {}
        self._system_prompt: str = ""
        self._max_history_messages: int = 50
        self._max_tool_rounds: int = 10
        self._storage: StorageBackend | None = None
        self._resolver: ServiceResolver | None = None
        self._acl_svc: Any | None = None
        self._current_conversation_id: str | None = None
        # AI context profiles
        self._profiles: dict[str, AIContextProfile] = {}
        self._assignments: dict[str, str] = {}  # call_name -> profile_name
        # Internal helpers (initialized in start())
        self._persona: _PersonaHelper | None = None
        self._memory: _MemoryHelper | None = None
        self._memory_enabled: bool = True

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ai",
            capabilities=frozenset({"ai_chat", "ai_tools", "ws_handlers", "persona", "user_memory"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"ai_tools", "configuration", "access_control"}),
            events=frozenset({"chat.conversation.renamed"}),
            toggleable=True,
            toggle_description="AI chat and tool execution",
        )

    @property
    def backend(self) -> AIBackend:
        if self._backend is None:
            raise RuntimeError("AI backend not initialized — service is disabled or not started")
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.storage import StorageProvider

        # Load tunable config from ConfigurationService if available
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section("ai")
                self._apply_config(section)

        # Check enabled — if False, skip backend init and return early
        if not section.get("enabled", False) and not self._enabled:
            logger.info("AI service disabled")
            return

        self._enabled = True

        # Create backend from registry (skip if already injected, e.g. in tests)
        if self._backend is None:
            backend_name = section.get("backend", "anthropic")
            self._backend_name = backend_name
            backends = AIBackend.registered_backends()
            backend_cls = backends.get(backend_name)
            if backend_cls is None:
                raise ValueError(f"Unknown AI backend: {backend_name}")
            self._backend = backend_cls()

        # Initialize backend with settings (includes API key)
        await self._backend.initialize(self._config)

        # Resolve storage
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError("Expected StorageProvider for 'entity_storage' capability")
        self._storage = storage_svc.backend

        # Initialize internal helpers
        self._persona = _PersonaHelper(self._storage)
        await self._persona.load()

        self._memory = _MemoryHelper(self._storage)
        await self._memory.setup_indexes()

        # Resolve access control (optional — if missing, no filtering)
        self._acl_svc = resolver.get_capability("access_control")

        # Save resolver for lazy tool discovery
        self._resolver = resolver

        # Load memory enabled setting
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                memory_section = config_svc.get_section("memory")
                self._memory_enabled = memory_section.get("enabled", True)

        # Load profiles and assignments
        await self._load_profiles()

        logger.info(
            "AI service started (profiles=%d, assignments=%d)",
            len(self._profiles),
            len(self._assignments),
        )

    def _apply_config(self, section: dict[str, Any]) -> None:
        """Apply tunable config values from a config section."""
        self._max_history_messages = section.get(
            "max_history_messages", self._max_history_messages
        )
        self._max_tool_rounds = section.get("max_tool_rounds", self._max_tool_rounds)
        self._config = section.get("settings", self._config)

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "ai"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        params = [
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
                key="backend", type=ToolParameterType.STRING,
                description="AI backend provider.",
                default="anthropic", restart_required=True,
                choices=tuple(AIBackend.registered_backends().keys()) or ("anthropic",),
            ),
            ConfigParam(
                key="default_persona", type=ToolParameterType.STRING,
                description="Default persona instructions for the AI assistant.",
                default=DEFAULT_PERSONA,
                multiline=True,
            ),
            ConfigParam(
                key="memory_enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the AI memory system is enabled.",
                default=True, restart_required=True,
            ),
        ]
        # Include backend-declared params under settings.*
        # Use the registry class (not an instance) so params are available even when disabled
        backends = AIBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(ConfigParam(
                    key=f"settings.{bp.key}", type=bp.type,
                    description=bp.description, default=bp.default,
                    restart_required=bp.restart_required, sensitive=bp.sensitive,
                    choices=bp.choices, choices_from=bp.choices_from,
                    multiline=bp.multiline, backend_param=True,
                ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=AIBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

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
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            ai_section = config_svc.get_section("ai")
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
    ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
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
            (response_text, conversation_id, ui_blocks, tool_usage) tuple.
            ``ui_blocks`` is a list of serialized UI block dicts (possibly empty).
            ``tool_usage`` is a list of {tool_name, is_error} dicts.
        """
        if self._backend is None:
            raise RuntimeError("AI service is not enabled")
        if user_ctx is None:
            user_ctx = get_current_user()
        # Load or create conversation
        if conversation_id:
            messages = await self._load_conversation(conversation_id)
        else:
            conversation_id = str(uuid.uuid4())
            messages = []

        self._current_conversation_id = conversation_id

        # ── Slash-command short-circuit ─────────────────────────────
        # If the user typed ``/<name> ...`` and ``<name>`` matches a tool
        # that opted in via ``ToolDefinition.slash_command``, bypass the
        # AI entirely and invoke the tool directly. Grouped commands
        # like ``/radio start`` match a two-word key in the registry;
        # plain ones match a single-word key. Unknown commands are
        # rejected with a helpful error rather than leaked to the AI.
        first_word = extract_command_name(user_message)
        if first_word is not None:
            slash_cmds = self._slash_commands_for_user(user_ctx)
            matched = self._match_slash_command(user_message, slash_cmds)
            if matched is not None:
                return await self._execute_slash_command(
                    user_message,
                    matched,
                    slash_cmds[matched],
                    messages,
                    conversation_id,
                    user_ctx,
                )
            # Unknown slash command — store the attempt and return an
            # actionable error without invoking the AI.
            available = sorted(slash_cmds.keys())
            if available:
                hint = "Available: " + ", ".join(f"/{c}" for c in available)
            else:
                hint = "No slash commands are available to you."
            error_text = (
                f"Unknown slash command '/{first_word}'. {hint}"
            )
            messages.append(Message(
                role=MessageRole.USER,
                content=user_message,
                author_id=user_ctx.user_id if user_ctx else "",
                author_name=user_ctx.display_name if user_ctx else "",
            ))
            messages.append(Message(
                role=MessageRole.ASSISTANT, content=error_text,
            ))
            await self._save_conversation(
                conversation_id, messages, user_ctx=user_ctx,
            )
            return error_text, conversation_id, [], [
                {"tool_name": f"/{first_word}", "is_error": True}
            ]

        # Append user message
        messages.append(Message(role=MessageRole.USER, content=user_message))

        # Resolve profile for this AI call
        profile = self.get_profile(ai_call)

        # Discover and filter tools based on profile
        tools_by_name = self._discover_tools(user_ctx=user_ctx, profile=profile)

        tool_defs = [defn for _, defn in tools_by_name.values()]

        # Add tools from active skills (additive — only tools that already
        # exist via ToolProviders, restoring any that the profile filtered out)
        if self._resolver:
            skills_svc = self._resolver.get_capability("skills")
            if skills_svc is not None:
                from gilbert.interfaces.skills import SkillsProvider

                if isinstance(skills_svc, SkillsProvider):
                    active = await skills_svc.get_active_skills(conversation_id)
                    if active:
                        skill_tool_names = skills_svc.get_active_allowed_tools(active)
                        if skill_tool_names:
                            # Re-discover unfiltered tools and add missing ones
                            all_tools = self._discover_tools(user_ctx=user_ctx)
                            for tname in skill_tool_names:
                                if tname not in tools_by_name and tname in all_tools:
                                    tools_by_name[tname] = all_tools[tname]
                            tool_defs = [defn for _, defn in tools_by_name.values()]

        # Resolve system prompt — always prepend current date/time
        date_ctx = self._current_datetime_context()
        if system_prompt is not None:
            effective_prompt = f"{date_ctx}\n\n{system_prompt}"
        else:
            effective_prompt = await self._build_system_prompt(
                user_ctx=user_ctx, conversation_id=conversation_id,
            )

        # Agentic loop
        response: AIResponse | None = None
        all_ui_blocks: list[UIBlock] = []
        tool_usage: list[dict[str, Any]] = []

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

            # Track tool usage for the response metadata. Arguments are
            # sanitized to drop injected ``_user_id`` / ``_room_members``
            # keys before the payload is sent to the frontend.
            for tc, tr in zip(response.message.tool_calls, tool_results):
                tool_usage.append({
                    "tool_name": tc.tool_name,
                    "is_error": tr.is_error,
                    "arguments": self._sanitize_tool_args(tc.arguments),
                    "result": tr.content,
                })
        else:
            logger.warning(
                "Agentic loop hit max rounds (%d) for conversation %s",
                self._max_tool_rounds,
                conversation_id,
            )

        # Count *visible* assistant messages to determine response_index
        # for UI blocks. The agentic loop appends one assistant row per
        # round — intermediate rounds carry tool_calls but no content and
        # are collapsed into the final answer by ``_ws_history_load``
        # (and never shown in the live frontend state). Counting them
        # here would push response_index past the frontend's index space
        # and leave blocks unanchored. Match the history loader by
        # counting only non-empty assistant rows.
        assistant_count = sum(
            1 for m in messages
            if m.role == MessageRole.ASSISTANT and m.content
        )
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
        return final_text, conversation_id, ui_block_dicts, tool_usage

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

    async def _build_system_prompt(
        self,
        user_ctx: UserContext | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """Build the full system prompt: base identity, persona, user memories, and active skills."""
        parts: list[str] = []

        # Always inject current date/time first
        parts.append(self._current_datetime_context())

        if self._system_prompt:
            parts.append(self._system_prompt)
        if self._persona is not None:
            parts.append(self._persona.persona)
            if not self._persona.is_customized:
                parts.append(
                    "IMPORTANT: The persona has not been customized yet. "
                    "At the start of the FIRST conversation only, briefly let the user know "
                    "they can customize your personality and behavior by asking you to "
                    "update the persona. Only mention this once — never bring it up again "
                    "in subsequent messages or conversations."
                )

        # Inject user memory summaries if available
        if user_ctx and user_ctx.user_id not in ("system", "guest"):
            if self._memory is not None and self._memory_enabled:
                try:
                    summaries = await self._memory.get_user_summaries(user_ctx.user_id)
                    if summaries:
                        parts.append(summaries)
                except Exception:
                    pass  # Memory unavailable — not critical

        # Inject skill system awareness and active skill instructions
        if self._resolver:
            skills_svc = self._resolver.get_capability("skills")
            if skills_svc is not None:
                parts.append(
                    "## Skills\n"
                    "This system supports skills — specialized instruction sets that "
                    "users can enable or disable per conversation. Skills may appear or "
                    "disappear between messages as the user toggles them. When skills "
                    "are active, their instructions will appear below. Follow them when "
                    "relevant. If a skill you were using disappears, the user disabled "
                    "it — stop following its instructions.\n\n"
                    "### Creating Skills\n"
                    "Users can ask you to create custom skills. When they do, guide them "
                    "through the process conversationally — you don't need to explain the "
                    "SKILL.md format to them. Instead:\n"
                    "1. Ask what the skill should help with — its purpose and when it should be used.\n"
                    "2. Ask about the specific steps, workflows, or guidelines it should follow.\n"
                    "3. Ask about any gotchas, edge cases, or important constraints.\n"
                    "4. Once you have enough information, use the `create_skill` tool to create it.\n\n"
                    "Scope: By default, create skills as personal (scope='user'). Only offer "
                    "to create a global skill if the user explicitly asks for it — the system "
                    "will enforce permissions automatically. Do NOT ask about scope unless "
                    "the user brings it up.\n\n"
                    "When building the SKILL.md content for `create_skill`:\n"
                    "- The frontmatter MUST include `name` (kebab-case, e.g. 'sales-outreach') "
                    "and `description` (1-2 sentences explaining what it does and when to use it).\n"
                    "- Optionally include `metadata.category` and `metadata.icon` for UI grouping.\n"
                    "- Optionally include `allowed-tools` (space-separated tool names) to declare "
                    "which tools the skill uses — these are existing tools, NOT scripts.\n"
                    "- The body should contain clear, actionable instructions: workflows, "
                    "decision trees, gotchas, templates, and examples.\n"
                    "- Entity-stored skills CANNOT execute scripts or read files from disk. "
                    "They CAN use any AI tools available in the conversation (search, "
                    "data lookups, web fetch, etc.).\n"
                    "- Keep the instructions focused and under 500 lines.\n"
                    "- After creating, let the user know they can enable it from the Skills "
                    "panel in chat settings."
                )
                if conversation_id:
                    try:
                        from gilbert.interfaces.skills import SkillsProvider

                        if isinstance(skills_svc, SkillsProvider):
                            skills_ctx = await skills_svc.build_skills_context(
                                conversation_id,
                            )
                            if skills_ctx:
                                parts.append(skills_ctx)
                    except Exception:
                        pass  # Skills unavailable — not critical

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
            if isinstance(self._acl_svc, AccessControlProvider):
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
    ) -> tuple[list[ToolResult], list[UIBlock]]:
        """Execute a batch of tool calls and return results + any UI blocks."""
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
                if isinstance(self._acl_svc, AccessControlProvider):
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
                tc.arguments["_user_roles"] = list(user_ctx.roles)

            # Inject room members if in a shared conversation
            conv_id = getattr(self, "_current_conversation_id", None)
            storage = getattr(self, "_storage", None)
            if conv_id and storage:
                conv_data = await storage.get(_COLLECTION, conv_id)
                if conv_data and conv_data.get("shared"):
                    tc.arguments["_room_members"] = [
                        {
                            "user_id": m.get("user_id", ""),
                            "display_name": m.get("display_name", ""),
                        }
                        for m in conv_data.get("members", [])
                    ]

            await self._publish_tool_event("chat.tool.started", {
                "conversation_id": self._current_conversation_id,
                "tool_name": tc.tool_name,
                "tool_call_id": tc.tool_call_id,
                "arguments": self._sanitize_tool_args(tc.arguments),
            })

            # Propagate caller identity through the async context so
            # tools can resolve it via core.context.get_current_user().
            if user_ctx is not None:
                from gilbert.core.context import set_current_user
                set_current_user(user_ctx)

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

                await self._publish_tool_event("chat.tool.completed", {
                    "conversation_id": self._current_conversation_id,
                    "tool_name": tc.tool_name,
                    "tool_call_id": tc.tool_call_id,
                    "is_error": False,
                    "result_preview": result_text[:200] if result_text else "",
                })
            except Exception as exc:
                logger.exception("Tool execution failed: %s", tc.tool_name)
                results.append(ToolResult(
                    tool_call_id=tc.tool_call_id,
                    content=f"Error executing tool: {exc}",
                    is_error=True,
                ))
                await self._publish_tool_event("chat.tool.completed", {
                    "conversation_id": self._current_conversation_id,
                    "tool_name": tc.tool_name,
                    "tool_call_id": tc.tool_call_id,
                    "is_error": True,
                    "result_preview": str(exc)[:200],
                })
        return results, ui_blocks

    # --- Slash-command execution ---

    @staticmethod
    def _resolve_slash_namespace(provider: ToolProvider) -> str:
        """Figure out the slash-command namespace for *provider*, if any.

        Resolution order:

        1. If the provider's class declares ``slash_namespace`` as a
           non-empty string, use it verbatim. Plugins use this to pick a
           short human-friendly prefix (e.g. ``"currev"`` instead of
           ``"current-data-sync"``).
        2. If the provider's class was defined in a plugin module
           (``gilbert_plugin_<name>``), derive the namespace from the
           sanitized plugin name. This guarantees every plugin tool gets
           a namespace even if the plugin author forgets to set one.
        3. Otherwise (core service), return ``""`` — no prefix.
        """
        explicit = getattr(type(provider), "slash_namespace", "") or ""
        if explicit:
            return str(explicit)
        module = type(provider).__module__ or ""
        prefix = "gilbert_plugin_"
        if module.startswith(prefix):
            # ``gilbert_plugin_current_data_sync.data_sync_service`` →
            # ``current_data_sync``
            tail = module[len(prefix):]
            return tail.split(".", 1)[0]
        return ""

    def _slash_commands_for_user(
        self, user_ctx: UserContext | None,
    ) -> dict[str, tuple[ToolProvider, ToolDefinition]]:
        """Return slash-enabled tools the user may invoke, keyed by full command name.

        Respects RBAC (via ``_discover_tools``) but ignores AI profile
        filtering — slash commands are user-initiated, not AI calls.

        The registry key is the full user-facing invocation string,
        reflecting both the plugin namespace (if any) and the tool's
        slash group (if any). Examples::

            "announce"                 # core, no group
            "radio start"              # core, group="radio", cmd="start"
            "currev.time_logs"         # plugin ns, no group
            "currev.sync status"       # plugin ns, group="sync", cmd="status"

        Plugin-sourced tools are automatically prefixed with their
        plugin namespace so they can't collide with core commands or
        with each other.
        """
        all_tools = self._discover_tools(user_ctx=user_ctx)
        result: dict[str, tuple[ToolProvider, ToolDefinition]] = {}
        for _tool_name, (provider, tool_def) in all_tools.items():
            cmd = tool_def.slash_command
            if not cmd:
                continue
            group = tool_def.slash_group
            local = f"{group} {cmd}" if group else cmd
            namespace = self._resolve_slash_namespace(provider)
            full_cmd = f"{namespace}.{local}" if namespace else local
            if full_cmd in result:
                logger.warning(
                    "Duplicate slash command %r from tool %r "
                    "(already registered by %r)",
                    full_cmd, tool_def.name, result[full_cmd][1].name,
                )
                continue
            result[full_cmd] = (provider, tool_def)
        return result

    @staticmethod
    def _match_slash_command(
        text: str,
        registry: dict[str, tuple[ToolProvider, ToolDefinition]],
    ) -> str | None:
        """Longest-prefix lookup from an input line to a registered command.

        Given raw input like ``"/radio start some args"`` and a registry
        whose keys may include both grouped forms like ``"radio start"``
        and plain forms like ``"announce"``, return the longest matching
        key or ``None``.

        The algorithm tries the two-word form first (``"radio start"``)
        and falls back to the first-word form (``"radio"``). Plugin
        namespaces (``"currev.radio"`` / ``"currev.radio start"``) work
        because they use the first space as the separator between group
        and subcommand — the dot-prefixed namespace stays attached to
        the group.
        """
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return None
        body = stripped[1:]
        if not body:
            return None
        parts = body.split(None, 2)
        if not parts:
            return None
        first = parts[0]
        # Prefer the two-word (grouped) form when it matches.
        if len(parts) >= 2:
            candidate = f"{first} {parts[1]}"
            if candidate in registry:
                return candidate
        if first in registry:
            return first
        return None

    async def _execute_slash_command(
        self,
        raw_text: str,
        cmd_name: str,
        entry: tuple[ToolProvider, ToolDefinition],
        messages: list[Message],
        conversation_id: str,
        user_ctx: UserContext | None,
    ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        """Parse, execute, and persist a slash command.

        Returns the same tuple shape as ``chat()`` so callers can't tell
        the difference between a slash command and an AI turn.
        """
        provider, tool_def = entry

        # Record the user's command as a user message (with author fields
        # so shared-room history renders the actor correctly).
        messages.append(Message(
            role=MessageRole.USER,
            content=raw_text,
            author_id=user_ctx.user_id if user_ctx else "",
            author_name=user_ctx.display_name if user_ctx else "",
        ))

        # Parse — errors are shown to the user as the assistant reply.
        # ``cmd_name`` is the matched full command (e.g. ``"radio start"``
        # or ``"currev.time_logs"``), passed explicitly so the parser
        # strips the correct prefix for grouped invocations.
        try:
            arguments = parse_slash_command(
                raw_text, tool_def, full_command=cmd_name,
            )
        except SlashCommandError as exc:
            error_text = str(exc)
            messages.append(Message(
                role=MessageRole.ASSISTANT, content=error_text,
            ))
            await self._save_conversation(
                conversation_id, messages, user_ctx=user_ctx,
            )
            return (
                error_text,
                conversation_id,
                [],
                [{
                    "tool_name": tool_def.name,
                    "is_error": True,
                    "arguments": {},
                    "result": error_text,
                }],
            )

        # Inject caller identity so tools can see who invoked them,
        # matching the AI-driven path in ``_execute_tool_calls``.
        if user_ctx is not None:
            arguments["_user_id"] = user_ctx.user_id
            arguments["_user_name"] = user_ctx.display_name
            arguments["_user_roles"] = list(user_ctx.roles)

        # Inject shared-room members if this is a room conversation.
        if self._storage is not None:
            conv_data = await self._storage.get(_COLLECTION, conversation_id)
            if conv_data and conv_data.get("shared"):
                arguments["_room_members"] = [
                    {
                        "user_id": m.get("user_id", ""),
                        "display_name": m.get("display_name", ""),
                    }
                    for m in conv_data.get("members", [])
                ]

        tool_call_id = f"slash-{uuid.uuid4().hex[:12]}"
        sanitized_args = self._sanitize_tool_args(arguments)

        await self._publish_tool_event("chat.tool.started", {
            "conversation_id": conversation_id,
            "tool_name": tool_def.name,
            "tool_call_id": tool_call_id,
            "arguments": sanitized_args,
        })

        # Propagate caller identity through the async context so
        # tools can resolve it via core.context.get_current_user().
        if user_ctx is not None:
            from gilbert.core.context import set_current_user
            set_current_user(user_ctx)

        ui_blocks: list[UIBlock] = []
        is_error = False
        try:
            raw_result = await provider.execute_tool(tool_def.name, arguments)
            if isinstance(raw_result, ToolOutput):
                result_text = raw_result.text
                import dataclasses as _dc

                for block in raw_result.ui_blocks:
                    if not block.block_id:
                        block = _dc.replace(block, block_id=str(uuid.uuid4()))
                    if not block.tool_name:
                        block = _dc.replace(block, tool_name=tool_def.name)
                    ui_blocks.append(block)
            else:
                result_text = raw_result
        except Exception as exc:
            logger.exception(
                "Slash command execution failed: /%s -> %s",
                cmd_name, tool_def.name,
            )
            result_text = f"Error executing /{cmd_name}: {exc}"
            is_error = True

        await self._publish_tool_event("chat.tool.completed", {
            "conversation_id": conversation_id,
            "tool_name": tool_def.name,
            "tool_call_id": tool_call_id,
            "is_error": is_error,
            "result_preview": result_text[:200] if result_text else "",
        })

        # Store the assistant turn with ToolCall/ToolResult metadata so
        # the frontend renders it identically to an AI-driven tool use.
        messages.append(Message(
            role=MessageRole.ASSISTANT,
            content=result_text,
            tool_calls=[ToolCall(
                tool_call_id=tool_call_id,
                tool_name=tool_def.name,
                arguments=sanitized_args,
            )],
            tool_results=[ToolResult(
                tool_call_id=tool_call_id,
                content=result_text,
                is_error=is_error,
            )],
        ))

        # Serialize UI blocks with position + submission state, matching
        # the chat() agentic loop so downstream rendering is uniform.
        # Count only visible assistant rows (non-empty content) so the
        # index aligns with what the frontend and history loader show;
        # intermediate tool-use rounds are invisible and would offset
        # the anchor otherwise.
        assistant_count = sum(
            1 for m in messages
            if m.role == MessageRole.ASSISTANT and m.content
        )
        response_index = max(0, assistant_count - 1)
        ui_block_dicts: list[dict[str, Any]] = []
        for block in ui_blocks:
            d = block.to_dict()
            d["response_index"] = response_index
            d["submitted"] = False
            d["submission"] = None
            ui_block_dicts.append(d)

        await self._save_conversation(
            conversation_id, messages, user_ctx=user_ctx,
            ui_blocks=ui_block_dicts,
        )

        tool_usage = [{
            "tool_name": tool_def.name,
            "is_error": is_error,
            "arguments": sanitized_args,
            "result": result_text,
        }]
        return result_text, conversation_id, ui_block_dicts, tool_usage

    # --- Tool Event Publishing ---

    async def _publish_tool_event(
        self, event_type: str, data: dict[str, Any],
    ) -> None:
        """Publish a tool execution event for real-time UI updates."""
        if self._resolver is None:
            return
        event_bus_svc = self._resolver.get_capability("event_bus")
        if event_bus_svc is None:
            return
        from gilbert.interfaces.events import Event, EventBusProvider

        if isinstance(event_bus_svc, EventBusProvider):
            await event_bus_svc.bus.publish(Event(
                event_type=event_type, data=data, source="ai",
            ))

    @staticmethod
    def _sanitize_tool_args(args: dict[str, Any]) -> dict[str, Any]:
        """Remove injected internal arguments before sending to frontend."""
        return {k: v for k, v in args.items() if not k.startswith("_")}

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
        if not self._enabled:
            return []
        tools = [
            ToolDefinition(
                name="rename_conversation",
                slash_command="rename",
                slash_help="Rename the current conversation: /rename <title>",
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
                slash_group="profile",
                slash_command="list",
                slash_help="List AI profiles and call assignments: /profile list",
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
                # No slash_command: the nested ARRAY + OBJECT params
                # (tools, tool_roles) don't translate cleanly to positional
                # shell form. Manage profiles via /roles/profiles in the UI
                # or let the AI call this tool directly.
            ),
            ToolDefinition(
                name="delete_ai_profile",
                slash_group="profile",
                slash_command="delete",
                slash_help="Delete an AI profile: /profile delete <name>",
                description="Delete an AI context profile. The 'default' profile cannot be deleted.",
                parameters=[
                    ToolParameter(name="name", type=ToolParameterType.STRING, description="Profile name to delete."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="assign_ai_profile",
                slash_group="profile",
                slash_command="assign",
                slash_help=(
                    "Assign a profile to an AI call name: "
                    "/profile assign <call_name> <profile>"
                ),
                description="Assign an AI context profile to a named AI call (e.g., 'human_chat', 'sales_initial_email').",
                parameters=[
                    ToolParameter(name="call_name", type=ToolParameterType.STRING, description="The AI call name."),
                    ToolParameter(name="profile", type=ToolParameterType.STRING, description="Profile name to assign."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="clear_ai_assignment",
                slash_group="profile",
                slash_command="unassign",
                slash_help=(
                    "Revert a call to the 'default' profile: "
                    "/profile unassign <call_name>"
                ),
                description="Remove a call's profile assignment, reverting it to the 'default' profile.",
                parameters=[
                    ToolParameter(name="call_name", type=ToolParameterType.STRING, description="The AI call name."),
                ],
                required_role="admin",
            ),
            # Persona tools
            ToolDefinition(
                name="get_persona",
                slash_group="persona",
                slash_command="show",
                slash_help="Show the current AI persona: /persona show",
                description="Get the current AI persona (personality, tone, and behavioral instructions).",
                required_role="everyone",
            ),
            ToolDefinition(
                name="update_persona",
                description=(
                    "Update the AI persona. This changes how Gilbert behaves, speaks, "
                    "and responds. The full persona text is replaced."
                ),
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The new persona text (full replacement).",
                    ),
                ],
                required_role="admin",
                # No slash_command: persona text is typically multi-line
                # (paragraphs of behavioral instructions); inline shell
                # quoting is impractical. Use /roles/persona in the UI.
            ),
            ToolDefinition(
                name="reset_persona",
                slash_group="persona",
                slash_command="reset",
                slash_help="Reset persona to the default: /persona reset",
                description="Reset the AI persona to the default.",
                required_role="admin",
            ),
        ]
        # Memory tool (only when enabled)
        if self._memory_enabled:
            tools.append(
                ToolDefinition(
                    name="memory",
                    slash_command="memory",
                    slash_help=(
                        "Manage memories: /memory <action> "
                        "[summary='...'] [content='...'] "
                        "(actions: remember, recall, update, forget, list)"
                    ),
                    description=(
                        "Manage persistent memories for the current user. "
                        "Use 'remember' when the user tells you something worth remembering "
                        "(preferences, project details, personal info). Use 'auto' source when "
                        "you notice something worth remembering that the user didn't explicitly ask to save. "
                        "Use 'list' to see what you remember about them. "
                        "Use 'recall' to load full content of specific memories by ID. "
                        "Use 'update' to modify a memory. Use 'forget' to delete one."
                    ),
                    parameters=[
                        ToolParameter(
                            name="action",
                            type=ToolParameterType.STRING,
                            description="Action to perform.",
                            enum=["remember", "recall", "update", "forget", "list"],
                        ),
                        ToolParameter(
                            name="summary",
                            type=ToolParameterType.STRING,
                            description="Short summary sentence (for remember, or update).",
                            required=False,
                        ),
                        ToolParameter(
                            name="content",
                            type=ToolParameterType.STRING,
                            description="Detailed memory content (for remember, or update).",
                            required=False,
                        ),
                        ToolParameter(
                            name="source",
                            type=ToolParameterType.STRING,
                            description="'user' if they explicitly asked to remember, 'auto' if you decided to.",
                            enum=["user", "auto"],
                            required=False,
                        ),
                        ToolParameter(
                            name="ids",
                            type=ToolParameterType.ARRAY,
                            description="Memory IDs to recall (for recall action).",
                            required=False,
                        ),
                        ToolParameter(
                            name="id",
                            type=ToolParameterType.STRING,
                            description="Memory ID (for update or forget).",
                            required=False,
                        ),
                    ],
                    required_role="user",
                ),
            )
        return tools

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
            case "get_persona":
                return await self._tool_get_persona()
            case "update_persona":
                return await self._tool_update_persona(arguments)
            case "reset_persona":
                return await self._tool_reset_persona()
            case "memory":
                return await self._tool_memory_action(arguments)
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

    # --- Persona tool handlers ---

    async def _tool_get_persona(self) -> str:
        persona_text = self._persona.persona if self._persona else DEFAULT_PERSONA
        return _json.dumps({"persona": persona_text})

    async def _tool_update_persona(self, arguments: dict[str, Any]) -> str:
        if self._persona is None:
            return _json.dumps({"error": "Persona not initialized"})
        text = arguments["text"]
        await self._persona.update_persona(text)
        return _json.dumps({"status": "updated", "length": len(text)})

    async def _tool_reset_persona(self) -> str:
        if self._persona is None:
            return _json.dumps({"error": "Persona not initialized"})
        await self._persona.reset_persona()
        return _json.dumps({"status": "reset"})

    # --- Memory tool handler ---

    async def _tool_memory_action(self, arguments: dict[str, Any]) -> str:
        if self._memory is None:
            return "Memory system not initialized."
        action = arguments.get("action", "")
        # Caller identity is injected into ``arguments`` by the tool executor
        # (both the AI-driven path in ``_execute_tool_calls`` and the slash
        # command path in ``_invoke_slash_command``). Fall back to the
        # contextvar for any callers that invoke this handler directly.
        user_id = arguments.get("_user_id") or get_current_user().user_id
        if user_id in ("system", "guest"):
            return "Memory requires an authenticated user."
        match action:
            case "remember":
                return await self._memory.remember(user_id, arguments)
            case "recall":
                return await self._memory.recall(user_id, arguments)
            case "update":
                return await self._memory.update(user_id, arguments)
            case "forget":
                return await self._memory.forget(user_id, arguments)
            case "list":
                return await self._memory.list_memories(user_id)
            case _:
                return f"Unknown memory action: {action}"

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
                from gilbert.interfaces.events import Event, EventBusProvider

                if isinstance(event_bus_svc, EventBusProvider):
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

    @staticmethod
    def _filter_blocks_for_user(
        blocks: list[dict[str, Any]], user_id: str,
    ) -> list[dict[str, Any]]:
        """Filter UI blocks by for_user/exclude_user targeting."""
        return [
            b for b in blocks
            if (not b.get("for_user") or b.get("for_user") == user_id)
            and b.get("exclude_user") != user_id
        ]

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
            "slash.commands.list": self._ws_slash_commands_list,
        }

    async def _ws_slash_commands_list(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return the slash commands the caller can invoke.

        Drives chat-input autocomplete. Results are filtered by RBAC so
        users only see commands they're actually allowed to run.
        """
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
        conn: WsConnection = conn

        slash_cmds = self._slash_commands_for_user(conn.user_ctx)
        commands: list[dict[str, Any]] = []
        for cmd_name, (provider, tool_def) in sorted(slash_cmds.items()):
            # ``cmd_name`` may contain a space (e.g. "radio start") or a
            # plugin-namespace dot (e.g. "currev.time_logs"); either way
            # it IS the full invocation so the usage string reflects the
            # grouped / namespaced form the user actually types.
            commands.append({
                "command": cmd_name,
                "group": tool_def.slash_group or "",
                "tool_name": tool_def.name,
                "provider": provider.tool_provider_name,
                "description": tool_def.description,
                "help": tool_def.slash_help or tool_def.description,
                "usage": format_usage(tool_def, full_command=cmd_name),
                "required_role": tool_def.required_role,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type.value,
                        "description": p.description,
                        "required": p.required,
                        "default": p.default,
                        "enum": p.enum,
                    }
                    for p in tool_def.parameters
                    if not p.name.startswith("_")
                ],
            })
        return {
            "type": "slash.commands.list.result",
            "ref": frame.get("id"),
            "commands": commands,
        }

    async def _ws_chat_send(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        # Slash commands bypass the AI entirely and are handled inside
        # chat(). In shared rooms they also bypass the mentions_gilbert
        # check — invoking a tool is always intentional. We use the
        # longest-prefix matcher here so grouped commands like
        # ``/radio start`` are detected correctly.
        is_slash_command = False
        if extract_command_name(message) is not None:
            slash_cmds = self._slash_commands_for_user(conn.user_ctx)
            is_slash_command = (
                self._match_slash_command(message, slash_cmds) is not None
            )

        try:
            if is_shared:
                from gilbert.core.chat import mentions_gilbert, build_room_context, publish_event

                addressed = mentions_gilbert(message) or is_slash_command
                tagged_message = f"[{conn.user_ctx.display_name}]: {message}"

                response_text = ""
                ui_blocks: list[dict[str, Any]] = []
                tool_usage: list[dict[str, Any]] = []

                if addressed:
                    # Slash commands need the raw "/cmd ..." text so the
                    # parser recognizes them; the AI-chat path uses the
                    # tagged form so Gilbert knows who said what.
                    chat_message = message if is_slash_command else tagged_message
                    response_text, conv_id, ui_blocks, tool_usage = await self.chat(
                        user_message=chat_message,
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
                gilbert = conn.manager.gilbert
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
                response_text, conv_id, ui_blocks, tool_usage = await self.chat(
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
            "ui_blocks": self._filter_blocks_for_user(ui_blocks, conn.user_id),
            "tool_usage": tool_usage,
        }

    async def _ws_conversation_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Create an empty named personal conversation."""
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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
                from gilbert.core.chat import build_room_context
                system_prompt = build_room_context(conv_data, conn.user_ctx)

            response_text, conv_id, ui_blocks, _tool_usage = await self.chat(
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
            from gilbert.core.chat import publish_event
            gilbert = conn.manager.gilbert
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
            "ui_blocks": self._filter_blocks_for_user(ui_blocks, conn.user_id),
        }

    async def _ws_history_load(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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
        # Walk persisted messages and build display messages. Assistant
        # turns may span multiple persisted rows — intermediate rounds
        # hold tool_calls with empty content, followed by tool_result
        # rows, then a final assistant row with the answer text. We fold
        # every tool_call/tool_result pair since the previous user message
        # into a single ``tool_usage`` list on the first displayable
        # assistant message that follows them, so the frontend sees one
        # assistant bubble per turn annotated with the tools it used.
        display_messages: list[dict[str, Any]] = []
        # Pairs collected for the next user-visible assistant message:
        # {tool_name, is_error, arguments, result}.
        pending_tool_usage: list[dict[str, Any]] = []
        # Tool calls seen but not yet paired with a result: call_id -> dict.
        pending_calls: dict[str, dict[str, Any]] = {}

        def _flush_unpaired_calls() -> None:
            """Turn any unpaired tool_calls into usage entries (no result)."""
            for call in pending_calls.values():
                pending_tool_usage.append({
                    "tool_name": call.get("tool_name", ""),
                    "is_error": False,
                    "arguments": self._sanitize_tool_args(
                        call.get("arguments", {}) or {},
                    ),
                    "result": "",
                })
            pending_calls.clear()

        for m in data.get("messages", []):
            role = m.get("role")
            visible_to = m.get("visible_to")
            if visible_to is not None and conn.user_id not in visible_to:
                continue

            if role == "tool_result":
                # Match results back to their calls and emit usage entries.
                for tr in m.get("tool_results", []) or []:
                    call_id = tr.get("tool_call_id", "")
                    call = pending_calls.pop(call_id, None)
                    if call is None:
                        # Result without a matching call — still surface it.
                        pending_tool_usage.append({
                            "tool_name": "",
                            "is_error": bool(tr.get("is_error", False)),
                            "arguments": {},
                            "result": tr.get("content", ""),
                        })
                        continue
                    pending_tool_usage.append({
                        "tool_name": call.get("tool_name", ""),
                        "is_error": bool(tr.get("is_error", False)),
                        "arguments": self._sanitize_tool_args(
                            call.get("arguments", {}) or {},
                        ),
                        "result": tr.get("content", ""),
                    })
                continue

            if role not in ("user", "assistant"):
                continue

            content = m.get("content", "")

            if role == "assistant":
                # Stash any tool_calls on this row for later pairing.
                for tc in m.get("tool_calls", []) or []:
                    call_id = tc.get("tool_call_id", "")
                    if call_id:
                        pending_calls[call_id] = tc

                # Slash-command rows carry their tool_results inline on the
                # same assistant row (rather than on a separate tool_result
                # row). Pair them here so the reloaded bubble shows the
                # actual tool output in its tool_usage panel instead of an
                # empty string. This mirrors the ``_build_messages`` heal
                # in ``AnthropicAI`` for replay — here we heal display.
                for tr in m.get("tool_results", []) or []:
                    call_id = tr.get("tool_call_id", "")
                    call = pending_calls.pop(call_id, None)
                    if call is None:
                        pending_tool_usage.append({
                            "tool_name": "",
                            "is_error": bool(tr.get("is_error", False)),
                            "arguments": {},
                            "result": tr.get("content", ""),
                        })
                        continue
                    pending_tool_usage.append({
                        "tool_name": call.get("tool_name", ""),
                        "is_error": bool(tr.get("is_error", False)),
                        "arguments": self._sanitize_tool_args(
                            call.get("arguments", {}) or {},
                        ),
                        "result": tr.get("content", ""),
                    })

                # Empty-content assistant rows are intermediate tool-use
                # rounds; don't emit them as their own bubble.
                if not content:
                    continue

            if role == "user":
                # New user turn — any unpaired calls from a prior turn
                # are orphans, surface them before moving on.
                _flush_unpaired_calls()
                # Reset pending usage; user messages never carry it.
                pending_tool_usage = []

            msg: dict[str, Any] = {"role": role, "content": content}
            if is_shared:
                msg["author_id"] = m.get("author_id", "")
                msg["author_name"] = m.get("author_name", "")

            if role == "assistant":
                _flush_unpaired_calls()
                if pending_tool_usage:
                    msg["tool_usage"] = pending_tool_usage
                    pending_tool_usage = []

            display_messages.append(msg)

        ui_blocks = self._filter_blocks_for_user(
            data.get("ui_blocks", []), conn.user_id,
        )

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
        from gilbert.core.chat import conv_summary
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
        conn: WsConnection = conn

        personal = await self.list_conversations(user_id=conn.user_id, limit=30)
        shared = await self.list_shared_conversations(user_id=conn.user_id, limit=30)

        conversations = [conv_summary(c, shared=True) for c in shared]
        conversations += [conv_summary(c, shared=False) for c in personal]

        return {"type": "chat.conversation.list.result", "ref": frame.get("id"), "conversations": conversations}

    async def _ws_conversation_rename(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.core.chat import check_conversation_access, publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        gilbert = conn.manager.gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.conversation.renamed", {"conversation_id": conversation_id, "title": title})

        return {"type": "chat.conversation.rename.result", "ref": frame.get("id"), "status": "ok", "title": title}

    async def _ws_conversation_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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
        from gilbert.core.chat import publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        gilbert = conn.manager.gilbert
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
        from gilbert.core.chat import publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        gilbert = conn.manager.gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.member.joined", {
                "conversation_id": conversation_id, "user_id": conn.user_id,
                "display_name": conn.user_ctx.display_name,
            })

        return {"type": "chat.room.join.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_room_leave(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.core.chat import publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
        conn: WsConnection = conn

        conversation_id = frame.get("conversation_id")
        if not conversation_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

        if self._storage is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        data = await self._storage.get(_COLLECTION, conversation_id)
        if data is None or not data.get("shared"):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

        gilbert = conn.manager.gilbert

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
        from gilbert.core.chat import publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        gilbert = conn.manager.gilbert
        if gilbert is not None:
            await publish_event(gilbert, "chat.member.kicked", {"conversation_id": conversation_id, "user_id": target_user})

        return {"type": "chat.room.kick.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_room_invite(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.core.chat import publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        gilbert = conn.manager.gilbert
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
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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
        from gilbert.core.chat import publish_event
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
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

        gilbert = conn.manager.gilbert

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
        from gilbert.interfaces.ws import WsConnectionBase as WsConnection
        conn: WsConnection = conn

        gilbert = conn.manager.gilbert
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
