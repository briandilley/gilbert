"""Tests for ServiceManager — registration, dependency resolution, lifecycle."""

import pytest
from unittest.mock import AsyncMock

from gilbert.core.service_manager import ServiceManager
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver


class StubService(Service):
    """Configurable stub for testing."""

    def __init__(
        self,
        name: str,
        capabilities: frozenset[str] = frozenset(),
        requires: frozenset[str] = frozenset(),
        optional: frozenset[str] = frozenset(),
        start_error: Exception | None = None,
    ) -> None:
        self._info = ServiceInfo(
            name=name,
            capabilities=capabilities,
            requires=requires,
            optional=optional,
        )
        self._start_error = start_error
        self.started = False
        self.stopped = False
        self.resolver_at_start: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return self._info

    async def start(self, resolver: ServiceResolver) -> None:
        if self._start_error:
            raise self._start_error
        self.resolver_at_start = resolver
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def manager() -> ServiceManager:
    return ServiceManager()


# --- Registration ---


def test_register(manager: ServiceManager) -> None:
    svc = StubService("test", capabilities=frozenset({"cap_a"}))
    manager.register(svc)
    caps = manager.list_capabilities()
    assert "cap_a" in caps
    assert "test" in caps["cap_a"]


def test_register_duplicate_name_raises(manager: ServiceManager) -> None:
    manager.register(StubService("test"))
    with pytest.raises(ValueError, match="already registered"):
        manager.register(StubService("test"))


# --- Startup and Dependency Resolution ---


async def test_start_single_service(manager: ServiceManager) -> None:
    svc = StubService("test", capabilities=frozenset({"cap_a"}))
    manager.register(svc)
    await manager.start_all()

    assert svc.started
    assert "test" in manager.started_services


async def test_start_respects_dependency_order(manager: ServiceManager) -> None:
    """Services start after their dependencies."""
    start_order: list[str] = []

    class OrderTracker(StubService):
        async def start(self, resolver: ServiceResolver) -> None:
            await super().start(resolver)
            start_order.append(self._info.name)

    storage = OrderTracker("storage", capabilities=frozenset({"entity_storage"}))
    bus = OrderTracker("event_bus", capabilities=frozenset({"event_bus"}))
    consumer = OrderTracker(
        "consumer",
        capabilities=frozenset({"consumer_cap"}),
        requires=frozenset({"entity_storage", "event_bus"}),
    )

    # Register in reverse order to prove topo-sort works
    manager.register(consumer)
    manager.register(storage)
    manager.register(bus)
    await manager.start_all()

    assert start_order.index("storage") < start_order.index("consumer")
    assert start_order.index("event_bus") < start_order.index("consumer")


async def test_missing_required_capability_skips_service(manager: ServiceManager) -> None:
    svc = StubService(
        "needs_weather",
        requires=frozenset({"weather"}),
    )
    manager.register(svc)
    await manager.start_all()

    assert not svc.started
    assert "needs_weather" in manager.failed_services


async def test_failed_start_skips_service(manager: ServiceManager) -> None:
    svc = StubService(
        "broken",
        capabilities=frozenset({"broken_cap"}),
        start_error=RuntimeError("boom"),
    )
    manager.register(svc)
    await manager.start_all()

    assert not svc.started
    assert "broken" in manager.failed_services


async def test_cascade_failure(manager: ServiceManager) -> None:
    """If a service fails, dependents that require its capability also fail."""
    broken = StubService(
        "broken",
        capabilities=frozenset({"needed_cap"}),
        start_error=RuntimeError("boom"),
    )
    dependent = StubService(
        "dependent",
        requires=frozenset({"needed_cap"}),
    )
    manager.register(broken)
    manager.register(dependent)
    await manager.start_all()

    assert "broken" in manager.failed_services
    assert "dependent" in manager.failed_services


async def test_optional_dependency_missing_still_starts(manager: ServiceManager) -> None:
    svc = StubService(
        "flexible",
        capabilities=frozenset({"flex_cap"}),
        optional=frozenset({"nonexistent"}),
    )
    manager.register(svc)
    await manager.start_all()

    assert svc.started
    assert "flexible" in manager.started_services


async def test_resolver_passed_to_start(manager: ServiceManager) -> None:
    storage = StubService("storage", capabilities=frozenset({"entity_storage"}))
    consumer = StubService(
        "consumer",
        requires=frozenset({"entity_storage"}),
    )
    manager.register(storage)
    manager.register(consumer)
    await manager.start_all()

    assert consumer.resolver_at_start is not None
    resolved = consumer.resolver_at_start.require_capability("entity_storage")
    assert resolved is storage


# --- Discovery ---


async def test_get_service_by_name(manager: ServiceManager) -> None:
    svc = StubService("test", capabilities=frozenset({"cap_a"}))
    manager.register(svc)
    await manager.start_all()

    assert manager.get_service("test") is svc
    assert manager.get_service("nonexistent") is None


async def test_get_service_not_started(manager: ServiceManager) -> None:
    """get_service returns None for registered-but-not-started services."""
    svc = StubService("broken", start_error=RuntimeError("boom"))
    manager.register(svc)
    await manager.start_all()

    assert manager.get_service("broken") is None


async def test_get_by_capability(manager: ServiceManager) -> None:
    svc = StubService("test", capabilities=frozenset({"cap_a", "cap_b"}))
    manager.register(svc)
    await manager.start_all()

    assert manager.get_by_capability("cap_a") is svc
    assert manager.get_by_capability("cap_b") is svc
    assert manager.get_by_capability("nonexistent") is None


async def test_get_all_by_capability(manager: ServiceManager) -> None:
    svc1 = StubService("provider1", capabilities=frozenset({"shared_cap"}))
    svc2 = StubService("provider2", capabilities=frozenset({"shared_cap"}))
    manager.register(svc1)
    manager.register(svc2)
    await manager.start_all()

    all_providers = manager.get_all_by_capability("shared_cap")
    assert len(all_providers) == 2
    assert svc1 in all_providers
    assert svc2 in all_providers


async def test_require_capability_raises_if_missing(manager: ServiceManager) -> None:
    await manager.start_all()
    with pytest.raises(LookupError, match="nonexistent"):
        manager.require_capability("nonexistent")


# --- Shutdown ---


async def test_stop_all_in_reverse_order(manager: ServiceManager) -> None:
    stop_order: list[str] = []

    class StopTracker(StubService):
        async def stop(self) -> None:
            stop_order.append(self._info.name)

    storage = StopTracker("storage", capabilities=frozenset({"entity_storage"}))
    consumer = StopTracker(
        "consumer",
        requires=frozenset({"entity_storage"}),
    )
    manager.register(storage)
    manager.register(consumer)
    await manager.start_all()
    await manager.stop_all()

    assert stop_order == ["consumer", "storage"]


async def test_stop_handles_errors(manager: ServiceManager) -> None:
    class BadStop(StubService):
        async def stop(self) -> None:
            raise RuntimeError("stop failed")

    svc = BadStop("test")
    manager.register(svc)
    await manager.start_all()
    await manager.stop_all()  # should not raise


# --- Lifecycle Events ---


async def test_lifecycle_events_published(manager: ServiceManager) -> None:
    bus = AsyncMock()
    manager.set_event_bus(bus)

    svc = StubService("test", capabilities=frozenset({"cap_a"}))
    manager.register(svc)
    await manager.start_all()

    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert event.event_type == "service.started"
    assert event.data["service"] == "test"


async def test_failed_lifecycle_event(manager: ServiceManager) -> None:
    bus = AsyncMock()
    manager.set_event_bus(bus)

    svc = StubService("broken", start_error=RuntimeError("boom"))
    manager.register(svc)
    await manager.start_all()

    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert event.event_type == "service.failed"


# --- Hot-swap ---


async def test_restart_service_in_place(manager: ServiceManager) -> None:
    """Restart a service without replacing it."""
    svc = StubService("test", capabilities=frozenset({"cap_a"}))
    manager.register(svc)
    await manager.start_all()
    assert svc.started

    # Reset to verify it restarts
    svc.started = False
    await manager.restart_service("test")
    assert svc.started
    assert "test" in manager.started_services


async def test_restart_service_with_replacement(manager: ServiceManager) -> None:
    """Restart a service by swapping in a new instance."""
    old = StubService("test", capabilities=frozenset({"cap_a"}))
    manager.register(old)
    await manager.start_all()

    new = StubService("test", capabilities=frozenset({"cap_a", "cap_b"}))
    await manager.restart_service("test", new)

    assert new.started
    # New capabilities should be indexed
    assert manager.get_by_capability("cap_b") is new
    # Old capabilities should be removed if not in new
    assert manager.get_by_capability("cap_a") is new


async def test_restart_nonexistent_raises(manager: ServiceManager) -> None:
    with pytest.raises(LookupError, match="not found"):
        await manager.restart_service("nonexistent")


async def test_register_and_start(manager: ServiceManager) -> None:
    """Register and start a service after initial startup."""
    await manager.start_all()  # Empty start

    svc = StubService("late", capabilities=frozenset({"late_cap"}))
    await manager.register_and_start(svc)

    assert svc.started
    assert "late" in manager.started_services
    assert manager.get_by_capability("late_cap") is svc
