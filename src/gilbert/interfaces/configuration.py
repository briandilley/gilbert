"""Configuration interface — config parameter descriptions and the Configurable protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.tools import ToolParameterType


@dataclass(frozen=True)
class ConfigParam:
    """Describes a single configurable parameter.

    Used by services to declare what they accept, enabling AI introspection,
    runtime configuration changes, and auto-generated web UI forms.
    """

    key: str
    type: ToolParameterType
    description: str
    default: Any = None
    restart_required: bool = False
    sensitive: bool = False
    """Mask value in the UI (for passwords, API keys, etc.)."""
    choices: tuple[str, ...] | None = None
    """Fixed set of allowed values — renders as a dropdown in the UI."""
    multiline: bool = False
    """Render as a multi-line textarea instead of a single-line input."""
    choices_from: str = ""
    """Dynamic choices resolved at runtime (e.g., ``"speakers"`` to list speaker names)."""
    backend_param: bool = False
    """True if this param is declared by a backend, not the service itself."""


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

    @property
    def config_category(self) -> str:
        """UI grouping category (e.g., 'Media', 'Intelligence', 'Security')."""
        ...

    def config_params(self) -> list[ConfigParam]:
        """Describe all configurable parameters."""
        ...

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Called with the full config section when tunable params change."""
        ...
