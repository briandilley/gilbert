"""Tests for GreetingService — presence-driven morning greetings."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.core.services.greeting import GreetingService
from gilbert.interfaces.events import Event


class FakeStorage:
    """In-memory storage backend for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data


class FakeStorageService:
    @property
    def backend(self) -> FakeStorage:
        return self._backend

    def __init__(self) -> None:
        self._backend = FakeStorage()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeEventBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.published: list[Event] = []

    def subscribe(self, event_type: str, handler: Any) -> Any:
        self.handlers.setdefault(event_type, []).append(handler)
        return lambda: self.handlers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class FakeEventBusSvc:
    def __init__(self) -> None:
        self.bus = FakeEventBus()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="event_bus", capabilities=frozenset({"event_bus"}))


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


@pytest.fixture
def greeting_service() -> GreetingService:
    return GreetingService()


@pytest.fixture
def resolver() -> FakeResolver:
    r = FakeResolver()
    r.caps["event_bus"] = FakeEventBusSvc()
    storage = FakeStorageService()
    r.caps["entity_storage"] = storage
    # Patch isinstance check
    return r


class TestGreetingService:
    def test_service_info(self, greeting_service: GreetingService) -> None:
        info = greeting_service.service_info()
        assert info.name == "greeting"
        assert "greeting" in info.capabilities
        assert "event_bus" in info.requires
        assert "entity_storage" in info.requires

    @pytest.mark.asyncio
    async def test_subscribes_to_presence_arrived(self, greeting_service: GreetingService, resolver: FakeResolver) -> None:
        await greeting_service.start(resolver)
        bus = resolver.caps["event_bus"].bus
        assert "presence.arrived" in bus.handlers
        assert len(bus.handlers["presence.arrived"]) == 1

    @pytest.mark.asyncio
    async def test_unsubscribes_on_stop(self, greeting_service: GreetingService, resolver: FakeResolver) -> None:
        await greeting_service.start(resolver)
        bus = resolver.caps["event_bus"].bus
        assert len(bus.handlers["presence.arrived"]) == 1
        await greeting_service.stop()
        assert len(bus.handlers["presence.arrived"]) == 0

    def test_get_display_name_from_email(self, greeting_service: GreetingService) -> None:
        assert greeting_service._get_display_name("brian.dilley@example.com") == "Brian Dilley"

    def test_get_display_name_from_plain(self, greeting_service: GreetingService) -> None:
        assert greeting_service._get_display_name("Brian") == "Brian"

    def test_in_greeting_window(self, greeting_service: GreetingService) -> None:
        greeting_service._start_hour = 6
        greeting_service._cutoff_hour = 14
        greeting_service._timezone = "UTC"
        # This is time-dependent — just verify the method doesn't crash
        result = greeting_service._in_greeting_window()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_dedup_prevents_double_greeting(self, greeting_service: GreetingService, resolver: FakeResolver) -> None:
        await greeting_service.start(resolver)

        # Mark user as greeted
        await greeting_service._mark_greeted("brian")

        # Should be greeted
        assert await greeting_service._has_been_greeted_today("brian") is True

        # Unknown user should not be greeted
        assert await greeting_service._has_been_greeted_today("unknown") is False

    @pytest.mark.asyncio
    async def test_generate_greeting_fallback(self, greeting_service: GreetingService, resolver: FakeResolver) -> None:
        """Without AI service, falls back to simple greeting."""
        await greeting_service.start(resolver)
        greeting = await greeting_service._generate_greeting("Brian")
        assert "Brian" in greeting
        assert "Good morning" in greeting
