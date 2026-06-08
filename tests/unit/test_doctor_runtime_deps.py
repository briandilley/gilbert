"""Tests for the doctor → ``runtime_dependencies(config)`` seam.

``doctor`` must call ``Plugin.runtime_dependencies()`` *signature-robustly*:
pass the resolved config to overrides that accept it, and call zero-arg for
overrides that don't (so existing submodule overrides keep working until they
are migrated — ADR-0008).
"""

from typing import Any

from gilbert.cli.doctor import _call_runtime_dependencies
from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, RuntimeDependency


class ConfigAwarePlugin(Plugin):
    """Override that accepts (and uses) the resolved config."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(name="config-aware", version="1.0.0")

    async def setup(self, context: PluginContext) -> None:  # pragma: no cover
        pass

    async def teardown(self) -> None:  # pragma: no cover
        pass

    def runtime_dependencies(self, config: dict[str, Any] | None = None) -> list[RuntimeDependency]:
        cfg = config or {}
        if not cfg.get("enabled"):
            return []
        return [
            RuntimeDependency(
                name="daemon",
                description="needs the daemon",
                check_cmd="true",
                install_hint="install it",
            )
        ]


class LegacyPlugin(Plugin):
    """Legacy override that takes no config argument (must still work)."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(name="legacy", version="1.0.0")

    async def setup(self, context: PluginContext) -> None:  # pragma: no cover
        pass

    async def teardown(self) -> None:  # pragma: no cover
        pass

    def runtime_dependencies(self) -> list[RuntimeDependency]:  # type: ignore[override]
        return [
            RuntimeDependency(
                name="legacy-dep",
                description="legacy dep",
                check_cmd="true",
                install_hint="install it",
            )
        ]


def test_config_aware_receives_config_enabled() -> None:
    plugin = ConfigAwarePlugin()
    deps = _call_runtime_dependencies(plugin, {"enabled": True})
    assert [d.name for d in deps] == ["daemon"]


def test_config_aware_receives_config_disabled() -> None:
    plugin = ConfigAwarePlugin()
    deps = _call_runtime_dependencies(plugin, {"enabled": False})
    assert deps == []


def test_legacy_zero_arg_override_still_called() -> None:
    """A legacy override that takes no parameter is called zero-arg — the
    config-passing path must not break it."""
    plugin = LegacyPlugin()
    deps = _call_runtime_dependencies(plugin, {"enabled": True})
    assert [d.name for d in deps] == ["legacy-dep"]
