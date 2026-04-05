"""Configuration interface — config parameter descriptions and the Configurable protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.tools import ToolParameterType


@dataclass(frozen=True)
class ConfigParam:
    """Describes a single configurable parameter.

    Used by services to declare what they accept, enabling AI introspection
    and runtime configuration changes.
    """

    key: str
    type: ToolParameterType
    description: str
    default: Any = None
    restart_required: bool = False


@runtime_checkable
class Configurable(Protocol):
    """Protocol for services that accept runtime configuration.

    Services implementing this are auto-discovered by ConfigurationService.
    They describe their parameters (for AI introspection) and handle
    runtime config changes.
    """

    @property
    def config_namespace(self) -> str:
        """Config section name this service owns (e.g., 'ai', 'tts')."""
        ...

    def config_params(self) -> list[ConfigParam]:
        """Describe all configurable parameters."""
        ...

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Called with the full config section when tunable params change."""
        ...
