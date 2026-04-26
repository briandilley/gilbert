"""Tests for ThermostatService — backend selection, capability gating, dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.thermostat import ThermostatService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.thermostat import ThermostatBackend, ThermostatInfo


class StubThermostatBackend(ThermostatBackend):
    """Full-feature in-memory thermostat backend for testing."""

    backend_name = "_stub_thermostat"
    supports_cooling = True
    supports_heating = True
    supports_fan_mode = True
    supports_humidity = True

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self._zones: dict[str, ThermostatInfo] = {
            "z1": ThermostatInfo(
                thermostat_id="z1",
                name="Upstairs",
                area="Main HVAC",
                supports_cooling=True,
                supports_heating=True,
                supports_fan_mode=True,
                has_humidity_sensor=True,
                current_temperature=70.0,
                current_humidity=42.0,
                heat_setpoint=68.0,
                cool_setpoint=76.0,
                mode="heat",
                fan_mode="auto",
                temperature_unit="F",
            ),
            "z2": ThermostatInfo(
                thermostat_id="z2",
                name="Downstairs",
                area="Main HVAC",
                supports_cooling=True,
                supports_heating=True,
                supports_fan_mode=True,
                has_humidity_sensor=True,
                current_temperature=72.0,
                current_humidity=40.0,
                heat_setpoint=66.0,
                cool_setpoint=78.0,
                mode="auto",
                fan_mode="auto",
                temperature_unit="F",
            ),
            "z3": ThermostatInfo(
                thermostat_id="z3",
                name="Garage",
                area="Outbuildings",
                supports_cooling=False,
                supports_heating=True,
                supports_fan_mode=False,
                has_humidity_sensor=False,
                current_temperature=58.0,
                heat_setpoint=55.0,
                mode="heat",
                temperature_unit="F",
            ),
        }
        self.set_setpoint_calls: list[tuple[str, float | None, float | None]] = []
        self.set_mode_calls: list[tuple[str, str]] = []
        self.set_fan_calls: list[tuple[str, str]] = []

    async def initialize(self, config: dict[str, object]) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def list_thermostats(self) -> list[ThermostatInfo]:
        return list(self._zones.values())

    async def get_status(self, thermostat_id: str) -> ThermostatInfo:
        return self._zones[thermostat_id]

    async def set_setpoint(
        self,
        thermostat_id: str,
        *,
        heat: float | None = None,
        cool: float | None = None,
    ) -> None:
        self.set_setpoint_calls.append((thermostat_id, heat, cool))
        info = self._zones[thermostat_id]
        self._zones[thermostat_id] = ThermostatInfo(
            **{
                **info.__dict__,
                "heat_setpoint": heat if heat is not None else info.heat_setpoint,
                "cool_setpoint": cool if cool is not None else info.cool_setpoint,
            }
        )

    async def set_mode(self, thermostat_id: str, mode: str) -> None:
        self.set_mode_calls.append((thermostat_id, mode))

    async def set_fan_mode(self, thermostat_id: str, fan_mode: str) -> None:
        self.set_fan_calls.append((thermostat_id, fan_mode))


class HeatOnlyBackend(ThermostatBackend):
    backend_name = "_stub_heat_only"
    supports_cooling = False
    supports_heating = True
    supports_fan_mode = False
    supports_humidity = False

    def __init__(self) -> None:
        self._zones = {
            "h1": ThermostatInfo(
                thermostat_id="h1",
                name="Cabin",
                area="Cabin",
                supports_cooling=False,
                supports_heating=True,
                supports_fan_mode=False,
                current_temperature=64.0,
                heat_setpoint=60.0,
                mode="heat",
                temperature_unit="F",
            )
        }

    async def initialize(self, config: dict[str, object]) -> None: ...
    async def close(self) -> None: ...
    async def list_thermostats(self) -> list[ThermostatInfo]:
        return list(self._zones.values())
    async def get_status(self, thermostat_id: str) -> ThermostatInfo:
        return self._zones[thermostat_id]
    async def set_setpoint(
        self,
        thermostat_id: str,
        *,
        heat: float | None = None,
        cool: float | None = None,
    ) -> None: ...
    async def set_mode(self, thermostat_id: str, mode: str) -> None: ...


@pytest.fixture
def stub_backend() -> StubThermostatBackend:
    return StubThermostatBackend()


@pytest.fixture
def service(stub_backend: StubThermostatBackend) -> ThermostatService:
    svc = ThermostatService()
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


def test_service_info(service: ThermostatService) -> None:
    info = service.service_info()
    assert info.name == "thermostats"
    assert "thermostats" in info.capabilities
    assert "ai_tools" in info.capabilities


async def test_start_disabled_without_config(resolver: ServiceResolver) -> None:
    svc = ThermostatService()
    await svc.start(resolver)
    assert not svc._enabled
    assert svc._backend is None


async def test_stop_closes_backend(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    await service.stop()
    assert stub_backend.closed


# --- Backend registration ---


def test_backend_registered_via_subclass() -> None:
    backends = ThermostatBackend.registered_backends()
    assert "_stub_thermostat" in backends
    assert backends["_stub_thermostat"] is StubThermostatBackend


# --- Tool provider & capability gating ---


def test_tool_provider_name(service: ThermostatService) -> None:
    assert service.tool_provider_name == "thermostats"


def test_base_tools_present(service: ThermostatService) -> None:
    names = [t.name for t in service.get_tools()]
    assert {
        "thermostats_list",
        "thermostats_status",
        "thermostats_set_mode",
    }.issubset(names)


def test_full_feature_backend_exposes_all_tools(service: ThermostatService) -> None:
    names = {t.name for t in service.get_tools()}
    assert {
        "thermostats_set_heat",
        "thermostats_set_cool",
        "thermostats_set_range",
        "thermostats_set_fan_mode",
    }.issubset(names)


def test_heat_only_backend_hides_cool_and_range_and_fan() -> None:
    svc = ThermostatService()
    svc._backend = HeatOnlyBackend()
    svc._enabled = True
    names = {t.name for t in svc.get_tools()}
    assert "thermostats_set_heat" in names
    assert "thermostats_set_cool" not in names
    assert "thermostats_set_range" not in names
    assert "thermostats_set_fan_mode" not in names


def test_get_tools_empty_when_disabled() -> None:
    svc = ThermostatService()
    assert svc.get_tools() == []


# --- Tool dispatch: list ---


async def test_list_groups_by_area(service: ThermostatService) -> None:
    out = await service.execute_tool("thermostats_list", {})
    assert "# Main HVAC" in out
    assert "# Outbuildings" in out
    assert "Upstairs" in out
    assert "Garage" in out
    # Status formatting includes the temperature.
    assert "70°F" in out


async def test_list_includes_humidity_when_present(service: ThermostatService) -> None:
    out = await service.execute_tool("thermostats_list", {})
    assert "42% RH" in out


# --- Tool dispatch: status ---


async def test_status_by_name(
    service: ThermostatService,
) -> None:
    out = await service.execute_tool("thermostats_status", {"name": "upstairs"})
    assert "Upstairs" in out
    assert "70°F" in out
    assert "mode=heat" in out


async def test_status_by_area_returns_all_zones(
    service: ThermostatService,
) -> None:
    out = await service.execute_tool("thermostats_status", {"name": "Main HVAC"})
    assert "Upstairs" in out
    assert "Downstairs" in out


# --- Tool dispatch: set mode ---


async def test_set_mode_normalizes_case(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    await service.execute_tool(
        "thermostats_set_mode",
        {"name": "upstairs", "mode": "COOL"},
    )
    assert stub_backend.set_mode_calls == [("z1", "cool")]


async def test_set_mode_rejects_invalid(service: ThermostatService) -> None:
    out = await service.execute_tool(
        "thermostats_set_mode",
        {"name": "upstairs", "mode": "warmer"},
    )
    assert "must be one of" in out.lower()


async def test_set_mode_by_area_dispatches_to_all(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    await service.execute_tool(
        "thermostats_set_mode",
        {"name": "Main HVAC", "mode": "off"},
    )
    assert {tid for tid, _ in stub_backend.set_mode_calls} == {"z1", "z2"}


# --- Tool dispatch: set heat / cool / range ---


async def test_set_heat_setpoint(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    out = await service.execute_tool(
        "thermostats_set_heat",
        {"name": "upstairs", "temperature": 70},
    )
    assert "Upstairs" in out
    assert "70" in out
    assert stub_backend.set_setpoint_calls == [("z1", 70.0, None)]


async def test_set_cool_skips_heat_only_zones(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    # Garage is heat-only — asking for cool by name should report nothing eligible.
    out = await service.execute_tool(
        "thermostats_set_cool",
        {"name": "garage", "temperature": 75},
    )
    assert "support" in out.lower()
    assert stub_backend.set_setpoint_calls == []


async def test_set_range_validates_order(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    out = await service.execute_tool(
        "thermostats_set_range",
        {"name": "upstairs", "heat": 78, "cool": 70},
    )
    assert "lower" in out.lower()
    assert stub_backend.set_setpoint_calls == []


async def test_set_range_dispatches(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    await service.execute_tool(
        "thermostats_set_range",
        {"name": "downstairs", "heat": 66, "cool": 78},
    )
    assert stub_backend.set_setpoint_calls == [("z2", 66.0, 78.0)]


async def test_set_heat_blocked_on_no_match(service: ThermostatService) -> None:
    out = await service.execute_tool(
        "thermostats_set_heat",
        {"name": "nonexistent", "temperature": 70},
    )
    assert "no thermostats" in out.lower()


async def test_set_heat_requires_temperature(service: ThermostatService) -> None:
    out = await service.execute_tool(
        "thermostats_set_heat",
        {"name": "upstairs"},
    )
    assert "temperature" in out.lower()


# --- Tool dispatch: set fan mode ---


async def test_set_fan_mode_dispatches(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    await service.execute_tool(
        "thermostats_set_fan_mode",
        {"name": "upstairs", "mode": "circulate"},
    )
    assert stub_backend.set_fan_calls == [("z1", "circulate")]


async def test_set_fan_mode_rejects_invalid(service: ThermostatService) -> None:
    out = await service.execute_tool(
        "thermostats_set_fan_mode",
        {"name": "upstairs", "mode": "turbo"},
    )
    assert "must be one of" in out.lower()


async def test_set_fan_mode_skips_zones_without_support(
    service: ThermostatService,
    stub_backend: StubThermostatBackend,
) -> None:
    out = await service.execute_tool(
        "thermostats_set_fan_mode",
        {"name": "garage", "mode": "auto"},
    )
    # Garage has supports_fan_mode=False at the device level.
    assert "support" in out.lower()
    assert stub_backend.set_fan_calls == []


# --- Capability gating at execute time ---


async def test_set_cool_blocked_on_cool_unsupported_backend() -> None:
    svc = ThermostatService()
    svc._backend = HeatOnlyBackend()
    svc._enabled = True
    out = await svc.execute_tool(
        "thermostats_set_cool",
        {"name": "cabin", "temperature": 70},
    )
    assert "not supported" in out.lower()


async def test_set_fan_mode_blocked_on_no_fan_backend() -> None:
    svc = ThermostatService()
    svc._backend = HeatOnlyBackend()
    svc._enabled = True
    out = await svc.execute_tool(
        "thermostats_set_fan_mode",
        {"name": "cabin", "mode": "auto"},
    )
    assert "not supported" in out.lower()


# --- Resolution helper ---


def test_resolve_helper_prefers_area_match() -> None:
    from gilbert.core.services.thermostat import _resolve

    items = [
        ThermostatInfo(thermostat_id="1", name="Upstairs", area="Main HVAC"),
        ThermostatInfo(thermostat_id="2", name="Downstairs", area="Main HVAC"),
        ThermostatInfo(thermostat_id="3", name="Garage", area="Outbuildings"),
    ]
    matches = _resolve("main hvac", items)
    assert {m.thermostat_id for m in matches} == {"1", "2"}


async def test_unknown_tool_raises_keyerror(service: ThermostatService) -> None:
    with pytest.raises(KeyError):
        await service.execute_tool("thermostats_nonexistent", {"name": "x"})


# --- Config params ---


def test_config_params_include_backend_dropdown() -> None:
    svc = ThermostatService()
    params = {p.key: p for p in svc.config_params()}
    assert "backend" in params
    assert "_stub_thermostat" in (params["backend"].choices or ())


def test_config_params_include_settings_when_backend_selected() -> None:
    svc = ThermostatService()
    svc._backend_name = "_stub_thermostat"
    keys = [p.key for p in svc.config_params()]
    assert "backend" in keys


def test_format_status_omits_humidity_when_absent() -> None:
    from gilbert.core.services.thermostat import _format_status

    info = ThermostatInfo(
        thermostat_id="x",
        name="Garage",
        area="Outbuildings",
        current_temperature=58.0,
        heat_setpoint=55.0,
        mode="heat",
        temperature_unit="F",
    )
    out = _format_status(info)
    assert "RH" not in out
    assert "fan=" not in out
    assert "heat 55°F" in out


def test_format_status_includes_unit() -> None:
    from gilbert.core.services.thermostat import _format_status

    info = ThermostatInfo(
        thermostat_id="x",
        name="Cabin",
        current_temperature=20.0,
        heat_setpoint=18.0,
        mode="heat",
        temperature_unit="C",
    )
    assert "20°C" in _format_status(info)


