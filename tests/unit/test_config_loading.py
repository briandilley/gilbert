"""Tests for configuration loading — layered merging with plugin defaults."""

from pathlib import Path
from unittest.mock import patch

import yaml

from gilbert.config import PluginsConfig, _deep_merge, load_config


class TestDeepMerge:
    """Tests for the recursive deep-merge helper."""

    def test_simple_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        assert _deep_merge(base, override) == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3, "c": 4}}
        assert _deep_merge(base, override) == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_override_replaces_non_dict(self) -> None:
        base = {"x": [1, 2]}
        override = {"x": [3, 4]}
        assert _deep_merge(base, override) == {"x": [3, 4]}


class TestPluginsConfigModel:
    """Tests for the PluginsConfig Pydantic model."""

    def test_default_empty(self) -> None:
        cfg = PluginsConfig()
        assert cfg.directories == []
        assert cfg.sources == []
        assert cfg.config == {}

    def test_from_dict(self) -> None:
        cfg = PluginsConfig.model_validate({
            "directories": ["/path/to/plugins"],
            "sources": [{"source": "./local-plugin", "enabled": True}],
            "config": {"my-plugin": {"key": "value"}},
        })
        assert cfg.directories == ["/path/to/plugins"]
        assert len(cfg.sources) == 1
        assert cfg.config == {"my-plugin": {"key": "value"}}


class TestConfigWithPluginDefaults:
    """Tests for load_config with plugin_defaults parameter."""

    def test_plugin_defaults_merged(self, tmp_path: Path) -> None:
        # Write a minimal gilbert.yaml
        config_file = tmp_path / "gilbert.yaml"
        config_file.write_text(yaml.dump({
            "plugins": {"directories": [], "sources": []},
        }))

        with (
            patch("gilbert.config.DEFAULT_CONFIG_PATH", config_file),
            patch("gilbert.config.OVERRIDE_CONFIG_PATH", tmp_path / "nonexistent.yaml"),
            patch("gilbert.config.DATA_DIR", tmp_path / ".gilbert"),
        ):
            result = load_config(plugin_defaults={
                "my-plugin": {"interval": 30, "debug": False},
            })

        assert result.plugins.config == {"my-plugin": {"interval": 30, "debug": False}}

    def test_user_overrides_win_over_plugin_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "gilbert.yaml"
        config_file.write_text(yaml.dump({
            "plugins": {"directories": [], "sources": []},
        }))

        override_file = tmp_path / "override.yaml"
        override_file.write_text(yaml.dump({
            "plugins": {"config": {"my-plugin": {"interval": 60}}},
        }))

        with (
            patch("gilbert.config.DEFAULT_CONFIG_PATH", config_file),
            patch("gilbert.config.OVERRIDE_CONFIG_PATH", override_file),
            patch("gilbert.config.DATA_DIR", tmp_path / ".gilbert"),
        ):
            result = load_config(plugin_defaults={
                "my-plugin": {"interval": 30, "debug": False},
            })

        # User override wins for interval, plugin default retained for debug
        assert result.plugins.config["my-plugin"]["interval"] == 60
        assert result.plugins.config["my-plugin"]["debug"] is False

    def test_legacy_list_format_migrated(self, tmp_path: Path) -> None:
        config_file = tmp_path / "gilbert.yaml"
        config_file.write_text(yaml.dump({
            "plugins": [
                {"source": "./some-plugin", "enabled": True},
            ],
        }))

        with (
            patch("gilbert.config.DEFAULT_CONFIG_PATH", config_file),
            patch("gilbert.config.OVERRIDE_CONFIG_PATH", tmp_path / "nonexistent.yaml"),
            patch("gilbert.config.DATA_DIR", tmp_path / ".gilbert"),
        ):
            result = load_config()

        assert len(result.plugins.sources) == 1
        assert result.plugins.sources[0].source == "./some-plugin"

    def test_no_plugin_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "gilbert.yaml"
        config_file.write_text(yaml.dump({
            "plugins": {"directories": ["/some/dir"]},
        }))

        with (
            patch("gilbert.config.DEFAULT_CONFIG_PATH", config_file),
            patch("gilbert.config.OVERRIDE_CONFIG_PATH", tmp_path / "nonexistent.yaml"),
            patch("gilbert.config.DATA_DIR", tmp_path / ".gilbert"),
        ):
            result = load_config()

        assert result.plugins.directories == ["/some/dir"]
        assert result.plugins.config == {}
