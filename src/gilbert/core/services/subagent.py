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

import logging
import uuid
from typing import Any

from gilbert.core.subagents.types import get_agent_type, list_agent_types
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.context import (
    get_current_conversation_id,
    get_current_user,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
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
        ai = resolver.require_capability("ai_chat")
        if not isinstance(ai, AIProvider):
            raise RuntimeError("ai_chat capability does not implement AIProvider")
        self._ai = ai
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
            )
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "spawn_agent":
            raise KeyError(f"Unknown tool: {name}")
        agent_type = str(arguments.get("agent_type") or "")
        prompt = str(arguments.get("prompt") or "")
        if not agent_type or not prompt:
            raise ValueError("spawn_agent requires 'agent_type' and 'prompt'")
        # Inherit the caller's full identity for the subagent's RBAC.
        return await self.spawn(agent_type, prompt, user_ctx=get_current_user())

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
            )
        except Exception as exc:
            await self._publish_event(
                "chat.stream.subagent_failed",
                {**routing, "subagent_id": subagent_id, "agent_type": agent.id,
                 "reason": str(exc)},
            )
            raise
        await self._publish_event(
            "chat.stream.subagent_completed",
            {**routing, "subagent_id": subagent_id, "agent_type": agent.id},
        )
        return result.response_text
