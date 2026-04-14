"""Plugin interface — contract for extending Gilbert."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gilbert.interfaces.service import ServiceEnumerator
    from gilbert.interfaces.storage import StorageBackend


@dataclass
class PluginMeta:
    """Metadata declared by a plugin."""

    name: str
    version: str
    description: str = ""
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class PluginContext:
    """Everything a plugin receives during setup."""

    services: ServiceEnumerator
    config: dict[str, Any]
    data_dir: Path
    storage: StorageBackend | None = None


class Plugin(ABC):
    """Interface that all plugins must implement."""

    @abstractmethod
    def metadata(self) -> PluginMeta: ...

    @abstractmethod
    async def setup(self, context: PluginContext) -> None:
        """Called when the plugin is loaded.

        Use ``context.services`` to register discoverable services with
        capabilities.  ``context.config`` contains the resolved configuration
        for this plugin and ``context.data_dir`` is a directory where the
        plugin may persist data.
        """
        ...

    @abstractmethod
    async def teardown(self) -> None:
        """Called when the plugin is unloaded. Clean up resources."""
        ...
