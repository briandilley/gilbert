"""Doorbell backend interface — detect ring events from doorbell hardware."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class RingEvent:
    """A single doorbell ring event."""

    camera_name: str
    timestamp: int  # epoch milliseconds


class DoorbellBackend(ABC):
    """Abstract doorbell detection backend. Implementation-agnostic."""

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the backend with provider-specific configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    @abstractmethod
    async def get_ring_events(self, lookback_seconds: int = 10) -> list[RingEvent]:
        """Return ring events within the lookback window."""
        ...
