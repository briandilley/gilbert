"""Tests for ShadesService — backend selection, capability gating, dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.shades import ShadesService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.shades import ShadeInfo, ShadesBackend


class StubShadesBackend(ShadesBackend):
    backend_name = "_stub_shades"
    supports_position = True
    supports_stop = True

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.stopped: list[str] = []
        self._shades = [
            ShadeInfo("1", "Bedroom Shade", "Bedroom"),
            ShadeInfo("2", "Living Room Shade", "Living Room"),
        ]
        self._positions: dict[str, float] = {s.shade_id: s.position for s in self._shades}

    async def initialize(self, config: dict[str, object]) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def list_shades(self) -> list[ShadeInfo]:
        return [
            ShadeInfo(
                shade_id=s.shade_id,
                name=s.name,
                area=s.area,
                supports_position=s.supports_position,
                supports_stop=s.supports_stop,
                position=self._positions[s.shade_id],
            )
            for s in self._shades
        ]

    async def get_position(self, shade_id: str) -> float:
        return self._positions[shade_id]

    async def set_position(self, shade_id: str, position: float) -> None:
        self._positions[shade_id] = position

    async def stop(self, shade_id: str) -> None:
        self.stopped.append(shade_id)


class OpenCloseOnlyBackend(ShadesBackend):
    """Backend that can only fully open or close — no positioning, no stop."""

    backend_name = "_stub_simple_shades"
    supports_position = False
    supports_stop = False

    def __init__(self) -> None:
        self._positions: dict[str, float] = {"a": 0.0}

    async def initialize(self, config: dict[str, object]) -> None: ...

    async def close(self) -> None: ...

    async def list_shades(self) -> list[ShadeInfo]:
        return [
            ShadeInfo(
                "a",
                "Garage Door",
                "Garage",
                supports_position=False,
                supports_stop=False,
                position=self._positions["a"],
            ),
        ]

    async def get_position(self, shade_id: str) -> float:
        return self._positions[shade_id]

    async def set_position(self, shade_id: str, position: float) -> None:
        # Snap to fully open / closed
        self._positions[shade_id] = 100.0 if position > 0 else 0.0


@pytest.fixture
def stub_backend() -> StubShadesBackend:
    return StubShadesBackend()


@pytest.fixture
def service(stub_backend: StubShadesBackend) -> ShadesService:
    svc = ShadesService()
    svc._backend = stub_backend
    svc._enabled = True
    return svc


@pytest.fixture
def resolver() -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)
    mock.get_capability.return_value = None
    mock.require_capability.side_effect = LookupError("not available")
    return mock


def test_service_info(service: ShadesService) -> None:
    info = service.service_info()
    assert info.name == "shades"
    assert "shades" in info.capabilities


async def test_start_disabled_without_config(resolver: ServiceResolver) -> None:
    svc = ShadesService()
    await svc.start(resolver)
    assert not svc._enabled


async def test_stop_closes_backend(
    service: ShadesService,
    stub_backend: StubShadesBackend,
) -> None:
    await service.stop()
    assert stub_backend.closed


def test_backend_registered() -> None:
    backends = ShadesBackend.registered_backends()
    assert "_stub_shades" in backends


def test_full_tool_set_with_capable_backend(service: ShadesService) -> None:
    names = {t.name for t in service.get_tools()}
    assert {
        "shades_list",
        "shades_status",
        "shades_open",
        "shades_close",
        "shades_set_position",
        "shades_stop",
    } == names


def test_set_position_hidden_when_backend_lacks_position() -> None:
    svc = ShadesService()
    svc._backend = OpenCloseOnlyBackend()
    svc._enabled = True
    names = {t.name for t in svc.get_tools()}
    assert "shades_set_position" not in names
    assert "shades_stop" not in names
    assert "shades_open" in names
    assert "shades_close" in names


async def test_open_by_area(
    service: ShadesService,
    stub_backend: StubShadesBackend,
) -> None:
    await service.execute_tool("shades_open", {"name": "bedroom"})
    assert stub_backend._positions["1"] == 100.0


async def test_close_by_name(
    service: ShadesService,
    stub_backend: StubShadesBackend,
) -> None:
    stub_backend._positions["1"] = 100.0
    await service.execute_tool("shades_close", {"name": "bedroom shade"})
    assert stub_backend._positions["1"] == 0.0


async def test_set_position(
    service: ShadesService,
    stub_backend: StubShadesBackend,
) -> None:
    out = await service.execute_tool(
        "shades_set_position",
        {"name": "living room", "position": 50},
    )
    assert "50%" in out
    assert stub_backend._positions["2"] == 50.0


async def test_stop_invokes_backend(
    service: ShadesService,
    stub_backend: StubShadesBackend,
) -> None:
    out = await service.execute_tool("shades_stop", {"name": "bedroom"})
    assert "Stopped" in out
    assert stub_backend.stopped == ["1"]


async def test_status_describes_position(
    service: ShadesService,
    stub_backend: StubShadesBackend,
) -> None:
    stub_backend._positions["1"] = 0.0
    stub_backend._positions["2"] = 100.0
    out_closed = await service.execute_tool("shades_status", {"name": "bedroom"})
    out_open = await service.execute_tool("shades_status", {"name": "living room"})
    assert "closed" in out_closed
    assert "open" in out_open


async def test_set_position_blocked_on_simple_backend() -> None:
    svc = ShadesService()
    svc._backend = OpenCloseOnlyBackend()
    svc._enabled = True
    out = await svc.execute_tool(
        "shades_set_position",
        {"name": "garage door", "position": 50},
    )
    assert "not supported" in out.lower()


async def test_stop_blocked_on_simple_backend() -> None:
    svc = ShadesService()
    svc._backend = OpenCloseOnlyBackend()
    svc._enabled = True
    out = await svc.execute_tool("shades_stop", {"name": "garage door"})
    assert "not supported" in out.lower()


async def test_no_match_friendly_error(service: ShadesService) -> None:
    out = await service.execute_tool("shades_open", {"name": "nonexistent"})
    assert "no shades" in out.lower()


def test_config_params_include_backend_dropdown() -> None:
    svc = ShadesService()
    keys_to_choices = {p.key: p.choices for p in svc.config_params()}
    assert "backend" in keys_to_choices
    assert "_stub_shades" in (keys_to_choices["backend"] or ())
