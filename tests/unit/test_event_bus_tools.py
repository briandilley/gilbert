"""Tests for EventBusService tool provider."""

import json

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.event_bus import EventBusService
from gilbert.interfaces.events import Event


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def service(bus: InMemoryEventBus) -> EventBusService:
    return EventBusService(bus)


# --- Service info ---


def test_service_info(service: EventBusService) -> None:
    info = service.service_info()
    assert "event_bus" in info.capabilities
    assert "ai_tools" in info.capabilities


def test_tool_provider_name(service: EventBusService) -> None:
    assert service.tool_provider_name == "event_bus"


def test_get_tools(service: EventBusService) -> None:
    tools = service.get_tools()
    assert len(tools) == 1
    assert tools[0].name == "publish_event"


# --- publish_event ---


async def test_tool_publish_event(service: EventBusService, bus: InMemoryEventBus) -> None:
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    bus.subscribe("user.reminder", handler)

    result = await service.execute_tool("publish_event", {
        "event_type": "user.reminder",
        "data": {"message": "Take out the trash"},
        "source": "ai",
    })
    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["event_type"] == "user.reminder"
    assert "timestamp" in parsed

    assert len(received) == 1
    assert received[0].data["message"] == "Take out the trash"
    assert received[0].source == "ai"


async def test_tool_publish_event_defaults(
    service: EventBusService, bus: InMemoryEventBus
) -> None:
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    bus.subscribe("test.event", handler)

    await service.execute_tool("publish_event", {"event_type": "test.event"})

    assert len(received) == 1
    assert received[0].data == {}
    assert received[0].source == "ai"


# --- Unknown tool ---


async def test_tool_unknown_raises(service: EventBusService) -> None:
    with pytest.raises(KeyError, match="Unknown tool"):
        await service.execute_tool("nonexistent", {})
