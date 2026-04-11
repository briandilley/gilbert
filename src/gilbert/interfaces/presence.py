"""User presence interface — track whether users are present, nearby, or away."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class PresenceState(StrEnum):
    """Where a user is relative to the monitored location."""

    PRESENT = "present"
    NEARBY = "nearby"
    AWAY = "away"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UserPresence:
    """Presence info for a single user."""

    user_id: str
    state: PresenceState
    since: str = ""  # ISO 8601 timestamp of last state change
    source: str = ""  # which provider reported this (e.g., "unifi", "bluetooth")


class PresenceBackend(ABC):
    """Abstract presence detection backend. Implementation-agnostic."""

    _registry: dict[str, type["PresenceBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            PresenceBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["PresenceBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
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

    @abstractmethod
    async def get_presence(self, user_id: str) -> UserPresence:
        """Get the current presence state for a user."""
        ...

    @abstractmethod
    async def get_all_presence(self) -> list[UserPresence]:
        """Get presence state for all tracked users."""
        ...

    @abstractmethod
    async def list_tracked_users(self) -> list[str]:
        """List user IDs that this backend is tracking."""
        ...
