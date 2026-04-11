"""Persona service — manages the AI assistant's personality and behavior."""

import json
import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_COLLECTION = "persona"
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


class PersonaService(Service):
    """Manages the AI persona — personality, tone, and behavioral instructions.

    The persona is stored in the entity system and can be edited at runtime
    via AI tools. The AI service reads the active persona to build its
    system prompt.
    """

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._persona: str = DEFAULT_PERSONA
        self._is_customized: bool = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="persona",
            capabilities=frozenset({"persona", "ai_tools"}),
            requires=frozenset({"entity_storage"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.core.services.storage import StorageService

        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageService):
            raise TypeError("Expected StorageService for entity_storage")
        self._storage = storage_svc.backend

        # Load saved persona if one exists
        saved = await self._storage.get(_COLLECTION, _PERSONA_ID)
        if saved and saved.get("text"):
            self._persona = saved["text"]
            self._is_customized = saved.get("customized", False)
            logger.info("Persona loaded from storage (customized=%s)", self._is_customized)
        else:
            logger.info("No persona stored — using default")

    async def stop(self) -> None:
        pass

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "persona"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="default_persona", type=ToolParameterType.STRING,
                description="Default persona instructions for the AI assistant.",
                default=DEFAULT_PERSONA,
                multiline=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Persona is managed via entity storage, not config — but the default
        # can be changed here for fresh installs.
        pass

    # --- Public API ---

    @property
    def persona(self) -> str:
        """The current active persona text."""
        return self._persona

    @property
    def is_customized(self) -> bool:
        """Whether the persona has been explicitly set by a user."""
        return self._is_customized

    async def update_persona(self, text: str) -> None:
        """Replace the active persona."""
        self._persona = text
        self._is_customized = True
        if self._storage:
            await self._storage.put(
                _COLLECTION, _PERSONA_ID, {"text": text, "customized": True}
            )
        logger.info("Persona updated (%d chars)", len(text))

    async def reset_persona(self) -> None:
        """Reset to the default persona."""
        self._persona = DEFAULT_PERSONA
        self._is_customized = False
        if self._storage:
            await self._storage.put(
                _COLLECTION, _PERSONA_ID, {"text": DEFAULT_PERSONA, "customized": False}
            )
        logger.info("Persona reset to default")

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "persona"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="get_persona",
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
            ),
            ToolDefinition(
                name="reset_persona",
                description="Reset the AI persona to the default.",
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "get_persona":
                return json.dumps({"persona": self._persona})
            case "update_persona":
                text = arguments["text"]
                await self.update_persona(text)
                return json.dumps({"status": "updated", "length": len(text)})
            case "reset_persona":
                await self.reset_persona()
                return json.dumps({"status": "reset"})
            case _:
                raise KeyError(f"Unknown tool: {name}")
