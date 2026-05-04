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


@dataclass(frozen=True)
class RuntimeDependency:
    """A non-pip runtime dependency a plugin needs.

    Plugins declare these via ``Plugin.runtime_dependencies()`` so that
    ``gilbert doctor`` can sanity-check the host before / after install
    without core having to know about any specific plugin's external
    binaries (Chromium, Xvfb, ffmpeg, tesseract, etc.).

    - ``name`` — short label shown in the doctor report.
    - ``description`` — what the dep is and why the plugin needs it.
    - ``check_cmd`` — shell command that exits 0 when the dep is
      satisfied, non-zero otherwise. Run via ``/bin/sh -c``.
    - ``install_hint`` — human-readable instructions for the operator
      when the check fails. Always shown alongside the failure.
    - ``auto_install_cmd`` — optional shell command that ``gilbert
      doctor --install`` will run to install the dep. Reserve this
      for safe, user-scoped installs (e.g. ``playwright install
      chromium`` writes to a per-user cache). Leave empty for things
      that need sudo or interactive prompts (apt, brew, manual
      downloads). Default empty.
    """

    name: str
    description: str
    check_cmd: str
    install_hint: str
    auto_install_cmd: str = ""


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

    def runtime_dependencies(self) -> list[RuntimeDependency]:
        """Declare external runtime dependencies the plugin needs.

        Override to declare non-pip deps (browser binaries, system
        packages, etc.). The default returns ``[]`` — most plugins
        only need their pip dependencies, declared in their
        ``pyproject.toml``.

        ``gilbert doctor`` calls this on every loaded plugin and
        runs the declared ``check_cmd`` for each. Plugins may inspect
        ``platform.system()`` to vary the shape of the returned list
        across OSes (e.g. ``apt-get install`` on Linux, ``brew
        install`` on macOS).
        """
        return []
