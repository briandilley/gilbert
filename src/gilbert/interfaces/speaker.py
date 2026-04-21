"""Speaker system interface — discover, group, and play audio on speakers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


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
    didl_meta: str = ""
    """Optional DIDL-Lite metadata envelope for items that need one.

    Legacy UPnP field preserved for callers that still construct
    DIDL-Lite envelopes by hand. The aiosonos-based Sonos backend
    ignores it (the WebSocket API builds its own metadata), but
    non-Sonos backends can still use it.
    """
    announce: bool = False
    """When true, play as a short announcement overlay rather than
    replacing current playback.

    The Sonos backend maps this to its native ``audio_clip`` WebSocket
    API, which ducks the music, plays the clip, and automatically
    restores playback when finished — no snapshot/restore dance
    required. Other backends can treat this as a hint or ignore it.
    """


@dataclass(frozen=True)
class NowPlaying:
    """What a speaker is currently playing.

    Backends that can't introspect the current track return a NowPlaying
    with ``state`` set (from ``get_playback_state``) and the metadata
    fields empty.
    """

    state: PlaybackState = PlaybackState.STOPPED
    title: str = ""
    artist: str = ""
    album: str = ""
    album_art_url: str = ""
    uri: str = ""
    duration_seconds: float = 0.0
    position_seconds: float = 0.0


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

    async def enqueue_uri(self, request: PlayRequest) -> None:
        """Append audio to the speaker's queue without replacing playback.

        Default raises ``NotImplementedError`` — backends with a
        persistent queue (e.g. Sonos) should override to add the URI at
        the end of the queue. Callers should guard on the music service's
        ``supports_queue`` flag rather than catching the exception.
        """
        raise NotImplementedError(
            "This speaker backend does not support queue operations"
        )

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

    async def get_now_playing(self, speaker_id: str) -> NowPlaying:
        """Get metadata about the track/stream currently playing on a speaker.

        The default implementation only reports the transport state — subclasses
        that can read track metadata from the device should override to populate
        title/artist/album/uri/duration/position.
        """
        state = await self.get_playback_state(speaker_id)
        return NowPlaying(state=state)

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

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
    ) -> str:
        """Announce ``text`` over speakers via text-to-speech.

        If ``speaker_names`` is ``None``, the service's configured
        default speakers are used. If ``volume`` is ``None``, the
        service's configured default announce volume is used.

        Returns an implementation-defined confirmation string (typically
        the path or URL of the generated audio file).
        """
        ...


@runtime_checkable
class CachedSpeakerLister(Protocol):
    """Protocol for anything that can report the currently-cached speakers.

    Used by ``ConfigurationService._resolve_dynamic_choices`` to
    populate ``speakers`` dropdowns on settings pages without
    duck-typing the service instance. Cache is refreshed on service
    start; consumers read it synchronously.
    """

    @property
    def cached_speakers(self) -> list[SpeakerInfo]:
        """Return the last-known speaker list from the service cache."""
        ...
