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
class EnablementDep:
    """An *enablement* dependency on a named backend or service (ADR-0018).

    Distinct from ``ServiceInfo.requires`` — which only orders startup
    waves by published capability. An enablement dependency additionally
    requires the prerequisite to be *enabled*: a service won't start until
    the thing it depends on is actually on, and Gilbert never auto-enables
    the prerequisite to satisfy the dependency.

    - ``capability`` — the capability advertised by the service that owns
      the prerequisite (e.g. ``"ai_chat"`` for the AI service). The owning
      service must itself be started for the dependency to be evaluable.
    - ``backend`` — when non-empty, names a backend the owning service must
      report as enabled (e.g. ``"ollama"``); the owning service must
      implement :class:`BackendEnablementProvider`. When empty, the
      requirement is simply that the owning *service* is enabled (its
      ``Service.enabled`` property is ``True``).
    """

    capability: str
    backend: str = ""


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
    requires_enabled: tuple[EnablementDep, ...] = ()
    """Enablement dependencies (ADR-0018).

    Each entry names a backend/service that must be *enabled* before this
    service may start. When any is unmet the service is left disabled with a
    reason (see ``ServiceEnumerator.disabled_services``) rather than started
    or marked failed. The prerequisite is never auto-enabled.
    """


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
class BackendEnablementProvider(Protocol):
    """Capability for asking a service whether a named backend is enabled.

    Implemented by services that own a set of named backends (e.g. the AI
    service owning ``ollama`` / ``anthropic`` / …). The enablement-dependency
    mechanism (ADR-0018) and the enablement-aware ``doctor`` use this to
    answer "is backend X enabled?" without coupling to a concrete service.
    """

    def is_backend_enabled(self, backend_name: str) -> bool:
        """Return True if the named backend is currently enabled/active."""
        ...


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

    @property
    def disabled_services(self) -> dict[str, str]:
        """Services left disabled by an unmet enablement dependency.

        Maps service name -> human-readable reason (naming the missing
        prerequisite). Distinct from ``failed_services`` (which start was
        attempted and raised): a disabled service was deliberately not
        started because a prerequisite backend/service is off (ADR-0018).
        """
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
