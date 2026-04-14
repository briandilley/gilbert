"""Doorbell backend interface — detect ring events from doorbell hardware."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


@dataclass(frozen=True)
class RingEvent:
    """A single doorbell ring event."""

    camera_name: str
    timestamp: int  # epoch milliseconds


class DoorbellBackend(ABC):
    """Abstract doorbell detection backend. Implementation-agnostic."""

    _registry: dict[str, type["DoorbellBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            DoorbellBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["DoorbellBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the backend with provider-specific configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    async def list_doorbell_names(self) -> list[str]:
        """Return names of available doorbells/cameras. Override in backends."""
        return []

    @abstractmethod
    async def get_ring_events(self, lookback_seconds: int = 10) -> list[RingEvent]:
        """Return ring events within the lookback window."""
        ...


@runtime_checkable
class AvailableDoorbellLister(Protocol):
    """Protocol for anything that can report the currently-known
    doorbell names. Used by ``ConfigurationService._resolve_dynamic_choices``
    to populate the ``doorbell`` dropdown on settings pages without
    duck-typing the service instance.
    """

    @property
    def available_doorbells(self) -> list[str]:
        """Names of doorbells currently known to the service."""
        ...
