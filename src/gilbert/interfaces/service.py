"""Service interface — discoverable, lifecycle-managed services with capabilities."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ServiceInfo:
    """Static metadata a service declares about itself."""

    name: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    requires: frozenset[str] = field(default_factory=frozenset)
    optional: frozenset[str] = field(default_factory=frozenset)
    ai_calls: frozenset[str] = field(default_factory=frozenset)
    events: frozenset[str] = field(default_factory=frozenset)
    toggleable: bool = False
    """If True, this service can be enabled/disabled via the Settings UI."""
    toggle_description: str = ""
    """Human-readable description shown in the Services toggle section."""


class ServiceResolver(ABC):
    """Read-only view passed to Service.start() for pulling dependencies."""

    @abstractmethod
    def get_capability(self, capability: str) -> Service | None:
        """Get a service providing the given capability, or None."""
        ...

    @abstractmethod
    def require_capability(self, capability: str) -> Service:
        """Get a service providing the given capability, or raise LookupError."""
        ...

    @abstractmethod
    def get_all(self, capability: str) -> list[Service]:
        """Get all services providing the given capability."""
        ...


class Service(ABC):
    """Interface for a discoverable, lifecycle-managed service."""

    @abstractmethod
    def service_info(self) -> ServiceInfo:
        """Declare this service's name, capabilities, and dependencies."""
        ...

    async def start(self, resolver: ServiceResolver) -> None:
        """Called after all required dependencies are available.
        Use resolver to fetch them. Override if needed."""

    @property
    def enabled(self) -> bool:
        """Whether this service is actively running.

        Toggleable services set ``self._enabled`` during ``start()``;
        this property exposes that flag publicly.  Non-toggleable services
        that never set ``_enabled`` default to ``True``.
        """
        return getattr(self, "_enabled", True)

    async def stop(self) -> None:
        """Called during shutdown, in reverse-start order. Override if needed."""


def background_warmup(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    log: logging.Logger | None = None,
) -> asyncio.Task[Any]:
    """Spawn a backend warmup coroutine as a fire-and-forget task.

    Many backends do slow network work in ``initialize()`` or ``start()`` —
    a telnet handshake to a Lutron repeater, an OAuth refresh against a
    cloud thermostat, a mDNS settle wait for Sonos. Awaiting those calls
    inline blocks the entire ``ServiceManager.start_all`` wave behind a
    single laggard. Spawn them through this helper instead: ``start()``
    returns immediately, the handshake runs concurrently, and any
    backend method that actually needs the connection serializes through
    the backend's own per-resource lock (e.g. ``shared_bridge``'s
    ``asyncio.Lock``).

    The helper:
    - Names the task for observability (``ps`` / debugger / ``asyncio.all_tasks()``).
    - Copies the current context (logging/trace ids) into the task.
    - Swallows exceptions but logs them with the provided logger so a
      flaky external service can't take the gilbert startup down.

    Usage::

        async def initialize(self, config):
            self._host = config.get("host", "")
            if self._host:
                background_warmup(
                    self._connect(),
                    name="lutron-lights-warmup",
                    log=logger,
                )
    """
    runtime_log = log or logging.getLogger("gilbert.warmup")

    async def _runner() -> None:
        try:
            await coro
        except Exception:
            runtime_log.exception("background warmup failed: %s", name)

    ctx = contextvars.copy_context()
    return asyncio.create_task(_runner(), name=name, context=ctx)


@runtime_checkable
class ServiceEnumerator(Protocol):
    """Protocol for enumerating and managing registered services.

    Used by ConfigurationService to discover Configurable services
    and restart them on config changes.
    """

    @property
    def started_services(self) -> list[str]:
        """Names of successfully started services."""
        ...

    @property
    def failed_services(self) -> set[str]:
        """Names of services that failed to start."""
        ...

    def get_service(self, name: str) -> Service | None:
        """Get a service by name."""
        ...

    def list_services(self) -> dict[str, Service]:
        """Return all registered services."""
        ...

    async def restart_service(self, name: str, new_instance: Service | None = None) -> None:
        """Restart a service, optionally replacing it with a new instance."""
        ...
