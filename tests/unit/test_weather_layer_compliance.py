"""Layer-compliance audit for the weather feature.

Greps source files for forbidden cross-layer imports. The architecture
rules:

- ``interfaces/weather.py`` must not import from ``gilbert.core.*``,
  ``gilbert.integrations.*``, ``gilbert.storage.*``, or
  ``gilbert.web.*``.
- ``core/services/weather.py`` must not import from
  ``gilbert.integrations.*``, ``gilbert.storage.*`` (entity storage
  goes through the ``StorageProvider`` capability), ``gilbert.web.*``,
  or any specific plugin module.
- ``std-plugins/open-meteo/*.py`` must not import from
  ``gilbert.core.services.*``, ``gilbert.integrations.*``,
  ``gilbert.storage.*``, or ``gilbert.web.*``.

Each rule maps to a focused regex test below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestInterfacesWeather:
    def test_no_core_imports(self) -> None:
        text = _read(REPO_ROOT / "src/gilbert/interfaces/weather.py")
        for forbidden in (
            r"\bfrom gilbert\.core\.",
            r"\bimport gilbert\.core\.",
            r"\bfrom gilbert\.integrations\.",
            r"\bimport gilbert\.integrations\.",
            r"\bfrom gilbert\.storage\.",
            r"\bimport gilbert\.storage\.",
            r"\bfrom gilbert\.web\.",
            r"\bimport gilbert\.web\.",
        ):
            assert not re.search(forbidden, text), (
                f"interfaces/weather.py violates layer rule: matches {forbidden!r}"
            )


class TestCoreWeatherService:
    def test_no_concrete_backend_imports(self) -> None:
        text = _read(REPO_ROOT / "src/gilbert/core/services/weather.py")
        for forbidden in (
            r"\bfrom gilbert\.integrations\.",
            r"\bimport gilbert\.integrations\.",
            r"\bfrom gilbert\.storage\.",
            r"\bimport gilbert\.storage\.",
            r"\bfrom gilbert\.web\.",
            r"\bimport gilbert\.web\.",
            # Plugin imports (any std-plugin module) — never allowed in core.
            r"\bfrom gilbert_plugin_",
            r"\bimport gilbert_plugin_",
        ):
            assert not re.search(forbidden, text), (
                f"core/services/weather.py violates layer rule: matches {forbidden!r}"
            )


class TestOpenMeteoPlugin:
    @pytest.mark.parametrize(
        "filename",
        ["open_meteo_weather.py", "weather_codes.py", "plugin.py"],
    )
    def test_no_core_or_storage_imports(self, filename: str) -> None:
        text = _read(REPO_ROOT / "std-plugins/open-meteo" / filename)
        for forbidden in (
            r"\bfrom gilbert\.core\.services\.",
            r"\bimport gilbert\.core\.services\.",
            r"\bfrom gilbert\.integrations\.",
            r"\bimport gilbert\.integrations\.",
            r"\bfrom gilbert\.storage\.",
            r"\bimport gilbert\.storage\.",
            r"\bfrom gilbert\.web\.",
            r"\bimport gilbert\.web\.",
        ):
            assert not re.search(forbidden, text), (
                f"std-plugins/open-meteo/{filename} violates layer rule: "
                f"matches {forbidden!r}"
            )

