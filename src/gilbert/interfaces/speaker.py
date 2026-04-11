"""Speaker system interface — discover, group, and play audio on speakers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class PlaybackState(StrEnum):
    """Current playback state of a speaker."""

    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    TRANSITIONING = "transitioning"


@dataclass(frozen=True)
class SpeakerInfo:
    """Information about a discovered speaker."""

    speaker_id: str
    name: str
    ip_address: str
    model: str = ""
    group_id: str = ""
    group_name: str = ""
    is_group_coordinator: bool = False
    volume: int = 0
    state: PlaybackState = PlaybackState.STOPPED


@dataclass(frozen=True)
class SpeakerGroup:
    """A group of speakers playing in sync."""

    group_id: str
    name: str
    coordinator_id: str
    member_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlayRequest:
    """Request to play audio on one or more speakers."""

    uri: str
    speaker_ids: list[str] = field(default_factory=list)
    volume: int | None = None
    title: str = ""
    position_seconds: float | None = None


class SpeakerBackend(ABC):
    """Abstract speaker system backend. Implementation-agnostic."""

    _registry: dict[str, type["SpeakerBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            SpeakerBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["SpeakerBackend"]]:
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

    # --- Discovery ---

    @abstractmethod
    async def list_speakers(self) -> list[SpeakerInfo]:
        """List all discovered speakers."""
        ...

    @abstractmethod
    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        """Get a speaker by ID. Returns None if not found."""
        ...

    # --- Playback ---

    @abstractmethod
    async def play_uri(self, request: PlayRequest) -> None:
        """Play audio from a URI on the specified speakers.

        If speaker_ids is empty, plays on all speakers.
        """
        ...

    @abstractmethod
    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        """Stop playback on the specified speakers (or all if None)."""
        ...

    async def clear_queue(self, speaker_ids: list[str] | None = None) -> None:
        """Clear the playback queue. Override in backends that have queues."""

    # --- Volume ---

    @abstractmethod
    async def get_volume(self, speaker_id: str) -> int:
        """Get volume for a speaker (0-100)."""
        ...

    @abstractmethod
    async def set_volume(self, speaker_id: str, volume: int) -> None:
        """Set volume for a speaker (0-100)."""
        ...

    # --- Grouping (optional — not all backends support this) ---

    @property
    def supports_grouping(self) -> bool:
        """Whether this backend supports speaker grouping."""
        return False

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        """Get the current playback state of a speaker.

        Default returns STOPPED. Override for backends that support
        transport state queries.
        """
        return PlaybackState.STOPPED

    async def list_groups(self) -> list[SpeakerGroup]:
        """List current speaker groups."""
        raise NotImplementedError("This backend does not support grouping")

    async def group_speakers(self, speaker_ids: list[str]) -> SpeakerGroup:
        """Group speakers together. Smart implementations should avoid
        re-grouping if the speakers are already in the desired configuration.

        Returns the resulting group.
        """
        raise NotImplementedError("This backend does not support grouping")

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        """Remove speakers from their groups, returning them to standalone."""
        raise NotImplementedError("This backend does not support grouping")

    # --- Snapshot / Restore (optional — for announce-and-resume) ---

    async def snapshot(self, speaker_ids: list[str]) -> None:
        """Save the current playback state of speakers for later restore.

        Called before an announcement so playback can resume after.
        Default is a no-op — backends that support it should override.
        """

    async def restore(self, speaker_ids: list[str]) -> None:
        """Restore speakers to the state saved by the last ``snapshot()``.

        Default is a no-op — backends that support it should override.
        """


@runtime_checkable
class SpeakerProvider(Protocol):
    """Protocol for services providing speaker control capabilities."""

    @property
    def backend(self) -> SpeakerBackend:
        """Access the speaker backend."""
        ...
