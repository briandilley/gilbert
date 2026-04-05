"""Service manager — registration, dependency resolution, and lifecycle management."""

import logging
from typing import Any

from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


class ServiceManager(ServiceResolver):
    """Manages service registration, dependency resolution, startup, and discovery."""

    def __init__(self) -> None:
        self._registered: dict[str, Service] = {}
        self._capabilities: dict[str, list[str]] = {}  # capability -> [service_names]
        self._started: list[str] = []
        self._failed: set[str] = set()
        self._event_bus: EventBus | None = None

    def register(self, service: Service) -> None:
        """Register a service. Must be called before start_all()."""
        info = service.service_info()
        if info.name in self._registered:
            raise ValueError(f"Service already registered: {info.name}")

        self._registered[info.name] = service
        for cap in info.capabilities:
            self._capabilities.setdefault(cap, []).append(info.name)

        logger.info(
            "Service registered: %s (provides: %s)",
            info.name,
            ", ".join(sorted(info.capabilities)) or "none",
        )

    async def start_all(self) -> None:
        """Resolve dependencies and start all services in topological order."""
        remaining = dict(self._registered)
        started_caps: set[str] = set()

        while remaining:
            # Find services whose requires are all satisfied
            ready = [
                name
                for name, svc in remaining.items()
                if svc.service_info().requires <= started_caps
            ]

            if not ready:
                # Everything left has unsatisfied dependencies
                for name, svc in remaining.items():
                    missing = svc.service_info().requires - started_caps
                    logger.error(
                        "Service %s cannot start: missing required capabilities: %s",
                        name,
                        ", ".join(sorted(missing)),
                    )
                    self._failed.add(name)
                break

            for name in ready:
                svc = remaining.pop(name)
                info = svc.service_info()
                try:
                    await svc.start(self)
                    self._started.append(name)
                    started_caps |= info.capabilities
                    logger.info("Service started: %s", name)
                    await self._publish_event("service.started", name, info)
                except Exception:
                    logger.exception("Service %s failed to start", name)
                    self._failed.add(name)
                    await self._publish_event("service.failed", name, info)

        total = len(self._started)
        failed = len(self._failed)
        logger.info(
            "Service startup complete: %d started, %d failed", total, failed
        )

    async def stop_all(self) -> None:
        """Stop all started services in reverse order."""
        for name in reversed(self._started):
            svc = self._registered.get(name)
            if svc is None:
                continue
            try:
                await svc.stop()
                logger.info("Service stopped: %s", name)
            except Exception:
                logger.exception("Error stopping service: %s", name)
        self._started.clear()

    def set_event_bus(self, bus: EventBus) -> None:
        """Set the event bus for publishing lifecycle events."""
        self._event_bus = bus

    # --- Discovery API ---

    def get_service(self, name: str) -> Service | None:
        """Get a service by name (only if started)."""
        if name in self._started:
            return self._registered.get(name)
        return None

    def get_by_capability(self, capability: str) -> Service | None:
        """Get the first started service providing a capability."""
        for name in self._capabilities.get(capability, []):
            if name in self._started:
                return self._registered[name]
        return None

    def get_all_by_capability(self, capability: str) -> list[Service]:
        """Get all started services providing a capability."""
        return [
            self._registered[name]
            for name in self._capabilities.get(capability, [])
            if name in self._started
        ]

    def list_capabilities(self) -> dict[str, list[str]]:
        """List all registered capabilities and their providing service names."""
        return {cap: list(names) for cap, names in self._capabilities.items()}

    @property
    def started_services(self) -> list[str]:
        """Names of all successfully started services."""
        return list(self._started)

    @property
    def failed_services(self) -> set[str]:
        """Names of all services that failed to start."""
        return set(self._failed)

    # --- ServiceResolver implementation ---

    def get_capability(self, capability: str) -> Service | None:
        return self.get_by_capability(capability)

    def require_capability(self, capability: str) -> Service:
        svc = self.get_by_capability(capability)
        if svc is None:
            raise LookupError(f"No started service provides capability: {capability}")
        return svc

    def get_all(self, capability: str) -> list[Service]:
        return self.get_all_by_capability(capability)

    # --- Hot-swap ---

    async def restart_service(
        self, name: str, new_instance: Service | None = None
    ) -> None:
        """Restart a service, optionally replacing it with a new instance.

        Stops the old service, swaps in the new instance (if given),
        and starts it. Used for hot-swapping structural config changes.
        """
        old = self._registered.get(name)
        if old is None:
            raise LookupError(f"Service not found: {name}")

        # Stop old
        if name in self._started:
            try:
                await old.stop()
                logger.info("Service stopped for restart: %s", name)
            except Exception:
                logger.exception("Error stopping service %s during restart", name)

        if new_instance is not None:
            # Unindex old capabilities
            old_info = old.service_info()
            for cap in old_info.capabilities:
                names = self._capabilities.get(cap, [])
                if name in names:
                    names.remove(name)

            # Register new instance
            self._registered[name] = new_instance
            new_info = new_instance.service_info()
            for cap in new_info.capabilities:
                self._capabilities.setdefault(cap, []).append(name)

        # Start the (new or existing) service
        svc = self._registered[name]
        try:
            await svc.start(self)
            if name not in self._started:
                self._started.append(name)
            self._failed.discard(name)
            logger.info("Service restarted: %s", name)
            await self._publish_event("service.started", name, svc.service_info())
        except Exception:
            logger.exception("Service %s failed to restart", name)
            if name in self._started:
                self._started.remove(name)
            self._failed.add(name)
            await self._publish_event("service.failed", name, svc.service_info())

    async def register_and_start(self, service: Service) -> None:
        """Register a new service and immediately start it.

        Used for enabling previously-disabled services at runtime.
        """
        self.register(service)
        svc_info = service.service_info()
        try:
            await service.start(self)
            self._started.append(svc_info.name)
            logger.info("Service registered and started: %s", svc_info.name)
            await self._publish_event("service.started", svc_info.name, svc_info)
        except Exception:
            logger.exception("Service %s failed to start after registration", svc_info.name)
            self._failed.add(svc_info.name)
            await self._publish_event("service.failed", svc_info.name, svc_info)

    # --- Internal ---

    async def _publish_event(self, event_type: str, name: str, info: ServiceInfo) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(Event(
                event_type=event_type,
                data={
                    "service": name,
                    "capabilities": sorted(info.capabilities),
                },
                source=name,
            ))
        except Exception:
            logger.debug("Failed to publish service event: %s", event_type)
