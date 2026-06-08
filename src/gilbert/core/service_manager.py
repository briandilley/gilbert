"""Service manager — registration, dependency resolution, and lifecycle management."""

import asyncio
import logging

from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.service import (
    BackendEnablementProvider,
    Service,
    ServiceInfo,
    ServiceResolver,
)

logger = logging.getLogger(__name__)

# How long each service gets to honor ``stop()`` before we move on.
# Five seconds is generous for an in-process teardown (close DB
# connection, cancel a background task) and short enough that a single
# wedged service can't stretch a systemd restart from "instant" to
# "twenty seconds." Services that legitimately need longer (e.g. flushing
# a large write buffer) should do that work in a background task during
# normal operation, not during shutdown.
_SERVICE_STOP_TIMEOUT = 5.0


class ServiceManager(ServiceResolver):
    """Manages service registration, dependency resolution, startup, and discovery."""

    def __init__(self) -> None:
        self._registered: dict[str, Service] = {}
        self._capabilities: dict[str, list[str]] = {}  # capability -> [service_names]
        self._started: list[str] = []
        self._failed: set[str] = set()
        # Services left disabled by an unmet enablement dependency (ADR-0018):
        # name -> human-readable reason naming the missing prerequisite.
        self._disabled: dict[str, str] = {}
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
        """Resolve dependencies and start all services in topological order.

        Services are grouped into waves by their declared ``requires`` —
        a service joins the current wave when every capability it
        requires has already been published by an earlier wave. Within
        a wave the starts run concurrently via ``asyncio.gather``: a
        slow network-bound start (a backend doing a telnet handshake,
        an HTTP probe, a model load) no longer blocks every later
        independent service. Inter-wave ordering still respects the
        dependency graph because the next wave only forms once the
        current wave's ``capabilities`` have all been added to
        ``started_caps``.
        """
        remaining = dict(self._registered)
        started_caps: set[str] = set()

        while remaining:
            # A service is eligible for the current wave once every
            # capability it ``requires`` AND every capability named by an
            # enablement dependency (``requires_enabled``) has been published
            # by an earlier wave. Gating on the enablement-dep capability too
            # ensures the owning service is started before we evaluate it.
            ready = [
                name
                for name, svc in remaining.items()
                if self._wave_caps_satisfied(svc.service_info(), started_caps)
            ]

            if not ready:
                # Everything left has unsatisfied dependencies. A service
                # whose only unmet need is an enablement-dep capability (the
                # owning service never started) is *disabled* with a reason
                # rather than marked failed; a genuinely missing required
                # capability is a failure.
                for name, svc in remaining.items():
                    info = svc.service_info()
                    missing_required = info.requires - started_caps
                    missing_enable = {d.capability for d in info.requires_enabled} - started_caps
                    if missing_required:
                        logger.error(
                            "Service %s cannot start: missing required capabilities: %s",
                            name,
                            ", ".join(sorted(missing_required)),
                        )
                        self._failed.add(name)
                    else:
                        reason = "disabled — required service(s) not available: " + ", ".join(
                            sorted(missing_enable)
                        )
                        self._disabled[name] = reason
                        logger.info("Service %s %s", name, reason)
                break

            # Snapshot the wave so dict mutation during gather is safe.
            wave: list[tuple[str, Service, ServiceInfo]] = []
            for name in ready:
                svc = remaining.pop(name)
                wave.append((name, svc, svc.service_info()))

            # Evaluate enablement dependencies *before* starting. A service
            # whose prerequisite backend/service is off must not start, must
            # not publish its capabilities, and is recorded as disabled — the
            # prerequisite is never auto-enabled (ADR-0018).
            startable: list[tuple[str, Service, ServiceInfo]] = []
            for name, svc, info in wave:
                unmet_reason = self._unmet_enablement_reason(info)
                if unmet_reason is not None:
                    self._disabled[name] = unmet_reason
                    logger.info("Service %s disabled — %s", name, unmet_reason)
                    continue
                startable.append((name, svc, info))

            await asyncio.gather(
                *(self._start_one(name, svc, info) for name, svc, info in startable)
            )

            # Augment capabilities once the wave settles. Services in the
            # wave can't depend on each other (they entered the wave
            # together because their requires were already met), so it's
            # safe to defer the cap-set update until the wave completes.
            for name, _svc, info in startable:
                if name in self._started:
                    started_caps |= info.capabilities

        total = len(self._started)
        failed = len(self._failed)
        disabled = len(self._disabled)
        logger.info(
            "Service startup complete: %d started, %d failed, %d disabled",
            total,
            failed,
            disabled,
        )

    def _wave_caps_satisfied(self, info: ServiceInfo, started_caps: set[str]) -> bool:
        """A service can join the current wave once every capability it
        ``requires`` and every capability named by ``requires_enabled`` has
        been published by an earlier wave."""
        if not info.requires <= started_caps:
            return False
        return all(dep.capability in started_caps for dep in info.requires_enabled)

    def _unmet_enablement_reason(self, info: ServiceInfo) -> str | None:
        """Evaluate a service's enablement dependencies (ADR-0018).

        Returns a human-readable reason naming the first unmet prerequisite,
        or ``None`` when every enablement dependency is satisfied. Never
        enables a prerequisite as a side effect.
        """
        for dep in info.requires_enabled:
            owner = self.get_by_capability(dep.capability)
            if owner is None:
                return f"disabled — requires service providing '{dep.capability}' to be enabled"
            if dep.backend:
                if not isinstance(owner, BackendEnablementProvider) or not (
                    owner.is_backend_enabled(dep.backend)
                ):
                    return f"disabled — requires the '{dep.backend}' backend to be enabled"
            elif not owner.enabled:
                return f"disabled — requires the '{dep.capability}' service to be enabled"
        return None

    async def _start_one(
        self,
        name: str,
        svc: "Service",
        info: "ServiceInfo",
    ) -> None:
        """Start a single service; record success/failure; publish event."""
        try:
            await svc.start(self)
            self._started.append(name)
            logger.info("Service started: %s", name)
            await self._publish_event("service.started", name, info)
        except Exception:
            logger.exception("Service %s failed to start", name)
            self._failed.add(name)
            await self._publish_event("service.failed", name, info)

    async def stop_all(self) -> None:
        """Stop all started services in reverse order.

        Each ``stop()`` is bounded by ``_SERVICE_STOP_TIMEOUT`` so a
        single wedged service can't stall the whole shutdown. The
        previous behavior was an unbounded ``await``, which let one
        misbehaving service stretch a systemd restart to 20+ seconds
        while the cgroup waited for stragglers.
        """
        for name in reversed(self._started):
            svc = self._registered.get(name)
            if svc is None:
                continue
            try:
                await asyncio.wait_for(svc.stop(), timeout=_SERVICE_STOP_TIMEOUT)
                logger.info("Service stopped: %s", name)
            except TimeoutError:
                # Don't propagate — keep stopping the rest of the
                # services. The wedged one will be SIGKILLed with
                # the process; we just don't want it gating shutdown.
                logger.warning(
                    "Service stop timed out after %.1fs: %s (continuing shutdown)",
                    _SERVICE_STOP_TIMEOUT,
                    name,
                )
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

    def list_services(self) -> dict[str, Service]:
        """Return all registered services (started or not)."""
        return dict(self._registered)

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

    @property
    def disabled_services(self) -> dict[str, str]:
        """Services left disabled by an unmet enablement dependency.

        Maps service name -> human-readable reason (ADR-0018). A disabled
        service is deliberately not started because a prerequisite
        backend/service is off; it is neither ``started`` nor ``failed``.
        """
        return dict(self._disabled)

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

    async def restart_service(self, name: str, new_instance: Service | None = None) -> None:
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

        # Start the (new or existing) service. Reset ``_enabled`` first so
        # services whose ``start()`` early-returns on a disabled config don't
        # carry over a stale ``True`` from the previous run — otherwise
        # ``svc.enabled`` would keep reporting the service as on and UI gating
        # (nav, capability checks) would never hide it.
        svc = self._registered[name]
        if hasattr(svc, "_enabled"):
            svc._enabled = False
        try:
            await svc.start(self)
            if name not in self._started:
                self._started.append(name)
            self._failed.discard(name)
            self._disabled.pop(name, None)
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
            self._disabled.pop(svc_info.name, None)
            logger.info("Service registered and started: %s", svc_info.name)
            await self._publish_event("service.started", svc_info.name, svc_info)
        except Exception:
            logger.exception("Service %s failed to start after registration", svc_info.name)
            self._failed.add(svc_info.name)
            await self._publish_event("service.failed", svc_info.name, svc_info)

    async def start_service(self, name: str) -> None:
        """Start a single already-registered service.

        Used by hot-load paths (e.g. plugin install) where a service was
        registered after ``start_all()`` ran — typically inside a plugin's
        ``setup()`` callback — and now needs its lifecycle ``start()``
        invoked.  No-op if already started.
        """
        if name in self._started:
            return
        svc = self._registered.get(name)
        if svc is None:
            raise LookupError(f"Service not found: {name}")
        info = svc.service_info()
        try:
            await svc.start(self)
            self._started.append(name)
            self._failed.discard(name)
            self._disabled.pop(name, None)
            logger.info("Service started: %s", name)
            await self._publish_event("service.started", name, info)
        except Exception:
            logger.exception("Service %s failed to start", name)
            self._failed.add(name)
            await self._publish_event("service.failed", name, info)
            raise

    async def stop_and_unregister(self, name: str) -> None:
        """Stop a service and remove it from the manager entirely.

        Used by hot-unload paths (e.g. plugin uninstall).  Stops the
        service if running, removes its capability index entries, and
        drops it from the registered set.  Publishes ``service.stopped``.
        Safe to call on an already-stopped service.
        """
        svc = self._registered.get(name)
        if svc is None:
            raise LookupError(f"Service not found: {name}")

        info = svc.service_info()

        # Stop if running
        if name in self._started:
            try:
                await svc.stop()
                logger.info("Service stopped: %s", name)
            except Exception:
                logger.exception("Error stopping service: %s", name)
            self._started.remove(name)

        # Unindex capabilities
        for cap in info.capabilities:
            names = self._capabilities.get(cap, [])
            if name in names:
                names.remove(name)
            if not names:
                self._capabilities.pop(cap, None)

        self._registered.pop(name, None)
        self._failed.discard(name)
        self._disabled.pop(name, None)

        await self._publish_event("service.stopped", name, info)

    # --- Internal ---

    async def _publish_event(self, event_type: str, name: str, info: ServiceInfo) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                Event(
                    event_type=event_type,
                    data={
                        "service": name,
                        "capabilities": sorted(info.capabilities),
                    },
                    source=name,
                )
            )
        except Exception:
            logger.debug("Failed to publish service event: %s", event_type)
