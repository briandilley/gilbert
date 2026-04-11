"""Service interface — discoverable, lifecycle-managed services with capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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

    async def stop(self) -> None:
        """Called during shutdown, in reverse-start order. Override if needed."""
