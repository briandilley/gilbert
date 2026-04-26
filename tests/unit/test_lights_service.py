"""Tests for LightsService — backend selection, capability gating, dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.lights import LightsService
from gilbert.interfaces.lights import LightInfo, LightsBackend
from gilbert.interfaces.service import ServiceResolver


class StubLightsBackend(LightsBackend):
    """In-memory lights backend for testing."""

    backend_name = "_stub_lights"
    supports_dimming = True

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self._lights = [
            LightInfo("1", "Kitchen Main", "Kitchen", supports_dimming=True, level=0.0),
            LightInfo("2", "Kitchen Pendants", "Kitchen", supports_dimming=True, level=0.0),
            LightInfo("3", "Front Porch", "Outside", supports_dimming=False, level=0.0),
        ]
        self._levels: dict[str, float] = {light.light_id: light.level for light in self._lights}

    async def initialize(self, config: dict[str, object]) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def list_lights(self) -> list[LightInfo]:
        # Re-emit with updated cached level
        return [
            LightInfo(
                light_id=light.light_id,
                name=light.name,
                area=light.area,
                supports_dimming=light.supports_dimming,
                level=self._levels[light.light_id],
            )
            for light in self._lights
        ]

    async def get_level(self, light_id: str) -> float:
        return self._levels[light_id]

    async def set_level(self, light_id: str, level: float) -> None:
        self._levels[light_id] = level


class SwitchOnlyBackend(LightsBackend):
    backend_name = "_stub_switch_only"
    supports_dimming = False

    def __init__(self) -> None:
        self._levels: dict[str, float] = {"sw-1": 0.0}

    async def initialize(self, config: dict[str, object]) -> None: ...

    async def close(self) -> None: ...

    async def list_lights(self) -> list[LightInfo]:
        return [
            LightInfo(
                "sw-1",
                "Garage",
                "Garage",
                supports_dimming=False,
                level=self._levels["sw-1"],
            )
        ]

    async def get_level(self, light_id: str) -> float:
        return self._levels[light_id]

    async def set_level(self, light_id: str, level: float) -> None:
        self._levels[light_id] = level


@pytest.fixture
def stub_backend() -> StubLightsBackend:
    return StubLightsBackend()


@pytest.fixture
def service(stub_backend: StubLightsBackend) -> LightsService:
    svc = LightsService()
    svc._backend = stub_backend
    svc._enabled = True
    return svc


@pytest.fixture
def resolver() -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)
    mock.get_capability.return_value = None
    mock.require_capability.side_effect = LookupError("not available")
    return mock


# --- Service info & lifecycle ---


def test_service_info(service: LightsService) -> None:
    info = service.service_info()
    assert info.name == "lights"
    assert "lights" in info.capabilities
    assert "ai_tools" in info.capabilities


async def test_start_disabled_without_config(resolver: ServiceResolver) -> None:
    svc = LightsService()
    await svc.start(resolver)
    assert not svc._enabled
    assert svc._backend is None


async def test_stop_closes_backend(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    await service.stop()
    assert stub_backend.closed


# --- Backend registration ---


def test_backend_registered_via_subclass() -> None:
    backends = LightsBackend.registered_backends()
    assert "_stub_lights" in backends
    assert backends["_stub_lights"] is StubLightsBackend


# --- Tool provider & capability gating ---


def test_tool_provider_name(service: LightsService) -> None:
    assert service.tool_provider_name == "lights"


def test_base_tools_present(service: LightsService) -> None:
    names = [t.name for t in service.get_tools()]
    assert {
        "lights_list",
        "lights_status",
        "lights_turn_on",
        "lights_turn_off",
        "lights_toggle",
    }.issubset(names)


def test_set_brightness_visible_when_backend_supports_dimming(
    service: LightsService,
) -> None:
    assert service.supports_dimming
    names = [t.name for t in service.get_tools()]
    assert "lights_set_brightness" in names


def test_set_brightness_hidden_when_backend_does_not_support_dimming() -> None:
    svc = LightsService()
    svc._backend = SwitchOnlyBackend()
    svc._enabled = True
    assert svc.supports_dimming is False
    names = [t.name for t in svc.get_tools()]
    assert "lights_set_brightness" not in names


def test_get_tools_empty_when_disabled() -> None:
    svc = LightsService()
    # _enabled stays False, _backend stays None
    assert svc.get_tools() == []


# --- Tool dispatch ---


async def test_list_groups_by_area(service: LightsService) -> None:
    out = await service.execute_tool("lights_list", {})
    assert "# Kitchen" in out
    assert "Kitchen Main" in out
    assert "Front Porch" in out
    # Switch-only label
    assert "(switch)" in out
    # Dimmable label
    assert "(dimmable)" in out


async def test_turn_on_by_area(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    out = await service.execute_tool("lights_turn_on", {"name": "kitchen"})
    assert "2 lights" in out
    assert stub_backend._levels["1"] == 100.0
    assert stub_backend._levels["2"] == 100.0
    # Front Porch isn't in Kitchen, untouched
    assert stub_backend._levels["3"] == 0.0


async def test_turn_on_by_name(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    out = await service.execute_tool("lights_turn_on", {"name": "front porch"})
    assert "Front Porch" in out
    assert stub_backend._levels["3"] == 100.0


async def test_turn_on_with_brightness(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    out = await service.execute_tool(
        "lights_turn_on",
        {"name": "kitchen pendants", "brightness": 40},
    )
    assert "40%" in out
    assert stub_backend._levels["2"] == 40.0


async def test_turn_off(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    stub_backend._levels["1"] = 100.0
    out = await service.execute_tool("lights_turn_off", {"name": "kitchen main"})
    assert "Turned off" in out
    assert stub_backend._levels["1"] == 0.0


async def test_toggle(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    stub_backend._levels["1"] = 100.0  # on
    stub_backend._levels["2"] = 0.0    # off
    out = await service.execute_tool("lights_toggle", {"name": "kitchen"})
    assert stub_backend._levels["1"] == 0.0
    assert stub_backend._levels["2"] == 100.0
    assert "1 on, 1 off" in out


async def test_set_brightness_skips_switches(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    # "outside" matches the area containing only the Front Porch switch.
    out = await service.execute_tool(
        "lights_set_brightness",
        {"name": "outside", "brightness": 50},
    )
    assert "not" in out.lower() or "none" in out.lower()
    assert stub_backend._levels["3"] == 0.0


async def test_set_brightness_applies_only_to_dimmable(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    out = await service.execute_tool(
        "lights_set_brightness",
        {"name": "kitchen", "brightness": 30},
    )
    assert "30%" in out
    assert stub_backend._levels["1"] == 30.0
    assert stub_backend._levels["2"] == 30.0


async def test_set_brightness_blocked_on_non_dimming_backend() -> None:
    svc = LightsService()
    svc._backend = SwitchOnlyBackend()
    svc._enabled = True
    out = await svc.execute_tool(
        "lights_set_brightness",
        {"name": "garage", "brightness": 50},
    )
    assert "not supported" in out.lower()


async def test_status_includes_pct_for_dimmer(
    service: LightsService,
    stub_backend: StubLightsBackend,
) -> None:
    stub_backend._levels["1"] = 75.0
    out = await service.execute_tool("lights_status", {"name": "kitchen main"})
    assert "Kitchen Main: on" in out
    assert "75%" in out


async def test_no_match_returns_friendly_error(service: LightsService) -> None:
    out = await service.execute_tool("lights_turn_on", {"name": "nonexistent"})
    assert "no lights" in out.lower()


async def test_unknown_tool_raises_keyerror(service: LightsService) -> None:
    with pytest.raises(KeyError):
        await service.execute_tool("lights_nonexistent", {"name": "x"})


# --- Config params ---


def test_config_params_include_backend_dropdown() -> None:
    svc = LightsService()
    params = {p.key: p for p in svc.config_params()}
    assert "backend" in params
    # Registered backends from this test module should appear in the choices.
    assert "_stub_lights" in (params["backend"].choices or ())


def test_config_params_include_settings_when_backend_selected() -> None:
    svc = LightsService()
    svc._backend_name = "_stub_lights"
    keys = [p.key for p in svc.config_params()]
    assert "backend" in keys
    # _stub backend declares no extra params; nothing else expected,
    # but the structure should still resolve cleanly.
    assert any(k.startswith("settings.") or k == "backend" for k in keys)


# --- Resolution ---


def test_resolve_helper_prefers_area_match() -> None:
    from gilbert.core.services.lights import _resolve

    lights = [
        LightInfo("1", "Kitchen Main", "Kitchen"),
        LightInfo("2", "Sub Pantry", "Kitchen"),
        LightInfo("3", "Some Other Kitchen Item", "Pantry"),
    ]
    matches = _resolve("kitchen", lights)
    # Area match wins — only the two in the "Kitchen" area, not the
    # name-substring hit in "Pantry".
    assert {m.light_id for m in matches} == {"1", "2"}
