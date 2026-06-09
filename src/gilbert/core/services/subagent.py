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
from typing import Any

from gilbert.core.subagents.types import get_agent_type, list_agent_types
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

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

    # --- Service ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="subagent",
            capabilities=frozenset({"subagent"}),
            requires=frozenset({"ai_chat"}),
            toggleable=True,
            toggle_description=(
                "Let the AI spawn ephemeral subagents to work on focused tasks in a fresh context."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
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
        self._enabled = bool(config.get("enabled", True))
        self._preamble = str(config.get("preamble") or _DEFAULT_PREAMBLE)
        for t in list_agent_types():
            value = config.get(_prompt_key(t.id))
            self._type_prompts[t.id] = str(value) if value else t.system_prompt

    # --- engine ---

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
        text. The subagent cannot ask the user anything (headless preamble; the
        spawn tool excludes interactive tools in a later slice).
        """
        if self._ai is None:
            raise RuntimeError("subagent service not started")
        agent = get_agent_type(agent_type)
        if agent is None:
            raise ValueError(f"Unknown agent type: {agent_type}")

        type_prompt = self._type_prompts.get(agent.id, agent.system_prompt)
        system_prompt = f"{self._preamble}\n\n{type_prompt}"

        result = await self._ai.chat(
            user_message=prompt,
            conversation_id=None,
            user_ctx=user_ctx,
            system_prompt=system_prompt,
            ai_call=f"subagent.{agent.id}",
            ai_profile=agent.profile_name,
            max_tool_rounds=agent.max_rounds,
        )
        return result.response_text
