"""User presence interface — track whether users are present, nearby, or away."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


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


@dataclass(frozen=True)
class PresenceObservation:
    """A raw entity the presence backend has detected.

    One row per stable "thing" the backend recognizes — a UniFi face
    name, an access-badge holder, a Wi-Fi device hostname or MAC, a BLE
    beacon id, etc. Observations are surfaced unmapped (no associated
    user_id) until an admin maps them to a Gilbert user via the
    presence-mapping screen.

    The presence service merges incoming observations into the
    ``presence_observations`` collection, preserving the
    ``mapped_user_id`` set by the UI across polls and bumping
    ``last_seen`` as the backend keeps reporting the same thing.

    Fields:

    - ``backend``: stable identifier for the backend that produced this
      observation (e.g. ``"unifi:protect"``, ``"unifi:access"``,
      ``"unifi:network"``). Combined with ``thing_id`` it forms the
      composite primary key in storage.
    - ``thing_id``: the backend's internal identifier for the thing
      (e.g. a face name, MAC, badge id, BLE address). Must be stable
      across polls so the upsert can find the prior row.
    - ``label``: human-readable label for the mapping screen. Backends
      should send the best-effort display name they know about.
    - ``kind``: rough category (``"face"``, ``"badge"``, ``"wifi"``,
      ``"ble"``, …). Used by the UI to group / icon the rows.
    - ``last_seen`` / ``first_seen``: ISO 8601 timestamps; the service
      preserves ``first_seen`` across polls and bumps ``last_seen``
      every time the same observation comes in again.
    - ``signal_strength``: optional 0.0-1.0 confidence/RSSI-normalized
      value if the backend has one; ``None`` otherwise.
    """

    backend: str
    thing_id: str
    label: str = ""
    kind: str = ""
    first_seen: str = ""
    last_seen: str = ""
    signal_strength: float | None = None


@dataclass(frozen=True)
class PresenceDetection:
    """One day's worth of detections for a single user from a single source.

    The presence service records one of these per (user_id, date, source)
    every time the backend reports that user as present or nearby, and
    rolls up first/last seen times and the observation count across the
    day. Rows older than ``presence.history_retention_days`` are pruned
    by a background sweep.

    Other services / tools query the rolled-up history through
    ``PresenceHistoryProvider.get_detection_history``.
    """

    user_id: str
    date: str  # ISO 8601 calendar date ("YYYY-MM-DD"), in the configured timezone.
    source: str  # The backend that recorded the detection (e.g. "unifi:protect").
    first_seen: str  # ISO 8601 datetime — earliest detection that day.
    last_seen: str  # ISO 8601 datetime — most recent detection that day.
    observation_count: int = 1  # Number of polls in the day that saw this user.


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

    async def get_observations(self) -> list["PresenceObservation"]:
        """Return raw observations (mapped + unmapped things) the backend
        has detected this cycle.

        Default implementation returns an empty list so backends that
        haven't yet been retrofitted for the mapping screen keep
        working — they continue to drive ``UserPresence`` exclusively
        through ``get_all_presence``. Backends that opt in should
        override this and yield one ``PresenceObservation`` per stable
        thing they recognize, regardless of whether a user mapping
        exists.
        """
        return []

    async def apply_thing_mappings(self, mappings: dict[str, str]) -> None:
        """Adopt admin-edited mappings from the presence service.

        ``mappings`` is a dict of ``f"{backend}:{thing_id}" -> user_id``
        (or ``""`` to unmap). The presence service calls this whenever
        the mapping store changes so the backend's internal name /
        device resolver can reflect the new authoritative pairing on
        the next poll. Default is a no-op so legacy backends keep
        functioning unchanged.
        """
        return None


@runtime_checkable
class PresenceProvider(Protocol):
    """Protocol for querying user presence from a service."""

    async def who_is_here(self) -> list[UserPresence]:
        """Get all users who are present or nearby."""
        ...


@runtime_checkable
class PresenceHistoryProvider(Protocol):
    """Protocol for querying recent-detection history from the presence service.

    Other services (the greet tool, notification cadence pickers,
    "when was X last around" lookups, etc.) call this instead of asking
    the backend directly so they get a consistent rolled-up view across
    every source the presence service has stored.
    """

    async def get_detection_history(
        self,
        user_id: str,
        since: str = "",
        until: str = "",
    ) -> list[PresenceDetection]:
        """Return detection rows for the user between two ISO dates.

        ``since`` / ``until`` are inclusive ``YYYY-MM-DD`` calendar dates;
        empty strings mean "no bound" (use the full retention window).
        Rows are returned sorted by date ascending.
        """
        ...
