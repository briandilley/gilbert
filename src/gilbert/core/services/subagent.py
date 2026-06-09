"""Subagent engine — spawn ephemeral, headless agents in a fresh context.

A subagent is a one-shot, autonomous run: a fresh conversation seeded with a
shared headless preamble + an agent type's system prompt, driven on the type's
AI profile (model + tool gating) with a bounded round budget, returning its
final message. It cannot ask the user anything. This service is the engine;
the user-facing ``spawn_agent`` tool, the live UI, and the ``deep-research``
type are added in later slices.

First-party orchestration of the AI capability — lives in core, resolves
``ai_chat`` (``AIProvider``) via the resolver, and never names a backend/model
(the type's profile owns those).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from typing import Any

from gilbert.core.subagents.types import get_agent_type, list_agent_types
from gilbert.interfaces.ai import AIProvider, ConversationMessagePoster
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.context import (
    get_current_conversation_id,
    get_current_user,
    set_workspace_conversation_id,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.workspace import WorkspaceProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_DEFAULT_PREAMBLE = (
    "You are a subagent launched to complete a single task autonomously. You "
    "cannot ask the user questions or wait for input — make reasonable "
    "assumptions and proceed. Your final message is returned verbatim as the "
    "result to the agent that launched you; it is not shown to the user "
    "directly. Be thorough, then stop."
)


def _prompt_key(type_id: str) -> str:
    """Config key for a type's system-prompt override (``general-purpose`` ->
    ``general_purpose_system_prompt``)."""
    return f"{type_id.replace('-', '_')}_system_prompt"


class SubagentService(Service):
    """Engine that runs a single ephemeral subagent and returns its result."""

    def __init__(self) -> None:
        self._enabled = True
        self._ai: AIProvider | None = None
        self._workspace: WorkspaceProvider | None = None
        self._preamble = _DEFAULT_PREAMBLE
        self._type_prompts: dict[str, str] = {t.id: t.system_prompt for t in list_agent_types()}
        self._resolver: ServiceResolver | None = None

    # --- Service ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="subagent",
            capabilities=frozenset({"subagent", "ai_tools"}),
            requires=frozenset({"ai_chat"}),
            toggleable=True,
            toggle_description=(
                "Let the AI spawn ephemeral subagents to work on focused tasks in a fresh context."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Toggleable service: determine ``self._enabled`` from config HERE.
        # ``ServiceManager.restart_service`` resets ``_enabled`` to False before
        # calling start() (so a disabled service can't carry a stale True) and
        # relies on start() to restore it. Without this, toggling the service on
        # triggers a restart that leaves it stuck disabled — its tools
        # (including /research) silently vanish.
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe("subagent")
            if not section.get("enabled", True):
                self._enabled = False
                logger.info("Subagent service disabled")
                return
        ai = resolver.require_capability("ai_chat")
        if not isinstance(ai, AIProvider):
            raise RuntimeError("ai_chat capability does not implement AIProvider")
        self._ai = ai
        self._enabled = True
        ws = resolver.get_capability("workspace")
        self._workspace = ws if isinstance(ws, WorkspaceProvider) else None
        logger.info("Subagent service started")

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "subagent"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Allow the AI to spawn subagents.",
                default=True,
            ),
            ConfigParam(
                key="preamble",
                type=ToolParameterType.STRING,
                description=(
                    "Shared headless preamble prepended to every subagent's "
                    "system prompt. Encodes the autonomy / no-user-feedback "
                    "contract."
                ),
                default=_DEFAULT_PREAMBLE,
                multiline=True,
                ai_prompt=True,
            ),
        ]
        for t in list_agent_types():
            params.append(
                ConfigParam(
                    key=_prompt_key(t.id),
                    type=ToolParameterType.STRING,
                    description=f"System prompt for the '{t.id}' subagent type.",
                    default=t.system_prompt,
                    multiline=True,
                    ai_prompt=True,
                )
            )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Use ``get(key, default)`` (not ``or default``) so an operator who
        # deliberately blanks a prompt gets the empty value, never a silent
        # revert to the bundled constant (per the AI-prompt rule).
        self._enabled = bool(config.get("enabled", True))
        self._preamble = str(config.get("preamble", _DEFAULT_PREAMBLE))
        for t in list_agent_types():
            self._type_prompts[t.id] = str(config.get(_prompt_key(t.id), t.system_prompt))

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "subagent"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        types = list_agent_types()
        type_lines = "\n".join(f"- {t.id}: {t.description}" for t in types)
        return [
            ToolDefinition(
                name="spawn_agent",
                description=(
                    "Launch a subagent to work on a focused task autonomously in "
                    "a fresh context, then return its final report. The subagent "
                    "cannot ask you or the user questions — give it a complete, "
                    "self-contained task. Available agent types:\n" + type_lines
                ),
                parameters=[
                    ToolParameter(
                        name="agent_type",
                        type=ToolParameterType.STRING,
                        description="Which agent type to launch.",
                        enum=[t.id for t in types],
                    ),
                    ToolParameter(
                        name="prompt",
                        type=ToolParameterType.STRING,
                        description=(
                            "The complete task for the subagent. Include all "
                            "context it needs; it has a fresh context and cannot "
                            "ask follow-up questions."
                        ),
                    ),
                ],
                required_role="user",
                # interactive=True keeps spawn_agent out of headless subagent
                # runs, so subagents can't spawn more subagents (no nesting).
                interactive=True,
                # Conservative for v1: no parallel fan-out of (expensive)
                # sub-chats until a per-turn spawn/cost cap exists.
                parallel_safe=False,
            ),
            ToolDefinition(
                name="deep_research",
                description=(
                    "Run a deep web-research task: an autonomous agent searches "
                    "the web, reads pages, cross-checks sources, and returns a "
                    "cited Markdown report. Use for questions that need current "
                    "information or synthesis across multiple sources. (Sugar "
                    "over spawn_agent with the 'deep-research' type.)"
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description=(
                            "The research question or task, stated completely "
                            "and self-contained — the agent cannot ask follow-ups."
                        ),
                    ),
                ],
                slash_command="research",
                slash_help="Deep web research: /research <question>",
                required_role="user",
                # Orchestration tool: keep it out of headless subagent runs
                # (a subagent calling deep_research would nest).
                interactive=True,
                parallel_safe=False,
            ),
        ]

    def _web_search_available(self) -> bool:
        """Whether a web-search backend is enabled (deep research needs one)."""
        if self._resolver is None:
            return False
        return self._resolver.get_capability("websearch") is not None

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "spawn_agent":
            agent_type = str(arguments.get("agent_type") or "")
            prompt = str(arguments.get("prompt") or "")
            if not agent_type or not prompt:
                raise ValueError("spawn_agent requires 'agent_type' and 'prompt'")
            # Inherit the caller's full identity for the subagent's RBAC.
            return await self.spawn(agent_type, prompt, user_ctx=get_current_user())
        if name == "deep_research":
            query = str(arguments.get("query") or "")
            if not query:
                raise ValueError("deep_research requires 'query'")
            if not self._web_search_available():
                return (
                    "Deep research needs a web-search backend, but none is "
                    "enabled. Enable a web-search provider (for example the "
                    "Tavily plugin) under Settings → Intelligence, then try again."
                )
            parent_conv = get_current_conversation_id()
            caller = get_current_user()
            self._run_in_background(
                self._run_research_background(query, parent_conv, caller)
            )
            return (
                f"\U0001f50d Researching “{query}” in the background — I’ll post the "
                "report here when it’s ready. You can keep chatting."
            )
        raise KeyError(f"Unknown tool: {name}")

    # --- background helpers ---

    def _run_in_background(self, coro: Any) -> None:
        """Detach a coroutine as a tracked task, preserving request context."""
        asyncio.create_task(coro, context=contextvars.copy_context())

    async def _run_research_background(
        self,
        query: str,
        parent_conversation_id: str | None,
        user_ctx: UserContext | None,
    ) -> None:
        """Run a deep-research subagent off the parent turn and deliver the
        result into the parent conversation. Never raises — a detached task's
        failure must be delivered, not lost."""
        # Scope workspace writes to the PARENT conversation so the report (and
        # any media the agent saves) is linkable from the user's chat.
        if parent_conversation_id:
            set_workspace_conversation_id(parent_conversation_id)
        try:
            report = await self.spawn("deep-research", query, user_ctx=user_ctx)
            rel_path = await self._write_report(
                parent_conversation_id,
                user_ctx.user_id if user_ctx else "system",
                report,
            )
            if rel_path and parent_conversation_id:
                url = f"/api/chat/download/{parent_conversation_id}/{rel_path}"
                lead = report.strip().split("\n\n", 1)[0][:400]
                message = f"**Research complete.** [Open the report]({url})\n\n{lead}"
            else:
                # No workspace — degrade to delivering the report inline.
                message = f"**Research complete.**\n\n{report}"
            await self._deliver(parent_conversation_id, message)
        except Exception as exc:  # noqa: BLE001 — deliver, don't crash
            logger.exception("Deep research background run failed")
            await self._publish_event(
                "chat.stream.subagent_failed",
                {
                    "conversation_id": parent_conversation_id,
                    "subagent_id": "",
                    "agent_type": "deep-research",
                    "reason": str(exc),
                    "visible_to": [user_ctx.user_id] if user_ctx and user_ctx.user_id else None,
                },
            )
            await self._deliver(
                parent_conversation_id, f"Deep research failed: {exc}"
            )

    async def _write_report(
        self, conversation_id: str | None, user_id: str, content: str
    ) -> str | None:
        """Write the report markdown to outputs/ in the conversation workspace.
        Returns the rel_path, or None when no workspace is available."""
        if self._workspace is None or not conversation_id:
            return None
        filename = f"research-{uuid.uuid4().hex[:8]}.md"
        rel_path = f"outputs/{filename}"
        out_dir = self._workspace.get_output_dir(user_id, conversation_id)
        target = out_dir / filename
        target.write_text(content, encoding="utf-8")
        await self._workspace.register_file(
            conversation_id=conversation_id,
            user_id=user_id,
            category="output",
            filename=filename,
            rel_path=rel_path,
            media_type="text/markdown",
            size=len(content.encode("utf-8")),
            created_by="ai",
            description="Deep research report",
        )
        return rel_path

    async def _deliver(self, conversation_id: str | None, content: str) -> None:
        """Post the result into the parent conversation (best-effort)."""
        if not conversation_id or not isinstance(self._ai, ConversationMessagePoster):
            return
        await self._ai.append_assistant_message(conversation_id, content)

    # --- engine ---

    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort publish to the event bus for live UI. No-op without one."""
        if self._resolver is None:
            return
        bus_svc = self._resolver.get_capability("event_bus")
        if bus_svc is None:
            return
        from gilbert.interfaces.events import Event, EventBusProvider

        if isinstance(bus_svc, EventBusProvider):
            await bus_svc.bus.publish(Event(event_type=event_type, data=data, source="subagent"))

    def _event_routing(self) -> dict[str, Any]:
        """Parent conversation + audience for subagent lifecycle events.

        Read from the chat-turn ContextVars: a subagent runs inside the
        spawning tool call, so these point at the PARENT chat. ``visible_to``
        scopes the event to the caller (the WS bridge applies it for
        ``chat.stream.*`` events). Both may be absent (direct, non-chat call)
        — then the event simply isn't routed to any conversation/user.
        """
        user = get_current_user()
        return {
            "conversation_id": get_current_conversation_id(),
            "visible_to": [user.user_id] if user.user_id else None,
        }

    async def spawn(
        self,
        agent_type: str,
        prompt: str,
        user_ctx: UserContext | None = None,
    ) -> str:
        """Run one ephemeral subagent of ``agent_type`` on ``prompt``.

        Drives a fresh chat turn (no parent history) with the shared preamble +
        the type's prompt, on the type's AI profile and round budget, inheriting
        the caller's identity for RBAC. Returns the subagent's final message
        text. The subagent cannot ask the user anything: the headless preamble
        plus ``headless=True`` on the chat call exclude all interactive tools —
        including ``spawn_agent`` itself, so a subagent can't spawn more
        subagents (no nesting).
        """
        if not self._enabled:
            raise RuntimeError("subagent service is disabled")
        if self._ai is None:
            raise RuntimeError("subagent service not started")
        agent = get_agent_type(agent_type)
        if agent is None:
            raise ValueError(f"Unknown agent type: {agent_type}")

        type_prompt = self._type_prompts.get(agent.id, agent.system_prompt)
        system_prompt = f"{self._preamble}\n\n{type_prompt}"

        subagent_id = uuid.uuid4().hex
        routing = self._event_routing()
        await self._publish_event(
            "chat.stream.subagent_started",
            {**routing, "subagent_id": subagent_id, "agent_type": agent.id},
        )
        try:
            result = await self._ai.chat(
                user_message=prompt,
                conversation_id=None,
                user_ctx=user_ctx,
                system_prompt=system_prompt,
                ai_call=f"subagent.{agent.id}",
                ai_profile=agent.profile_name,
                max_tool_rounds=agent.max_rounds,
                headless=True,
                # Tag the ephemeral subagent conversation so it's excluded from
                # the user's chat list (it's persisted for debugging, not shown).
                source="subagent",
            )
        except Exception as exc:
            await self._publish_event(
                "chat.stream.subagent_failed",
                {**routing, "subagent_id": subagent_id, "agent_type": agent.id, "reason": str(exc)},
            )
            raise
        await self._publish_event(
            "chat.stream.subagent_completed",
            {**routing, "subagent_id": subagent_id, "agent_type": agent.id},
        )
        return result.response_text
