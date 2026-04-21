"""Music service interface — browse, search, and resolve playable URIs.

Shaped around what a Sonos-style integration can actually do: list the
user's favorites and saved playlists, search within a linked music service,
and resolve selected items into playable URIs (with optional DIDL metadata
envelopes when the speaker needs them).

The old ID-based ``get_track``/``get_album`` lookups are intentionally
absent — they only made sense for backends that expose a public HTTP API
(like Spotify Web), and Sonos/SMAPI can't look up arbitrary items by ID
without having first seen them in a browse or search call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gilbert.interfaces.configuration import ConfigParam


@runtime_checkable
class LinkedMusicServiceLister(Protocol):
    """Protocol for anything that can report which upstream music
    services are currently linked on the user's system-level music
    platform (e.g. Sonos).

    Implemented by both the music backend (``SonosMusic``) and the
    ``MusicService`` wrapper, so callers can ask either one without
    caring which layer owns the mapping. Used by
    ``ConfigurationService._resolve_dynamic_choices`` to populate the
    ``preferred_service`` dropdown on the music settings page with the
    actual linked services, rather than a static list of every service
    Sonos supports.
    """

    def list_linked_services(self) -> list[str]:
        """Return the names of currently linked music services."""
        ...


class MusicItemKind(StrEnum):
    """What kind of thing a ``MusicItem`` represents."""

    TRACK = "track"
    ALBUM = "album"
    ARTIST = "artist"
    PLAYLIST = "playlist"
    STATION = "station"
    FAVORITE = "favorite"
    """A favorite whose underlying kind isn't known — play it as-is."""


@dataclass(frozen=True)
class MusicItem:
    """Unified descriptor for anything playable or browsable in the music system.

    Tracks, albums, playlists, and radio stations all share this shape so
    the service layer doesn't have to special-case per-kind dataclasses.

    ``id`` is opaque and backend-specific — callers should pass it back
    unchanged when resolving or playing the item. ``uri`` may be empty
    until ``resolve_playable()`` is called (search results and some
    favorites need to be resolved to a playable Sonos URI). ``didl_meta``
    is an optional DIDL-Lite metadata envelope that some speakers
    (notably Sonos radio stations) require alongside the URI.
    """

    id: str
    title: str
    kind: MusicItemKind
    subtitle: str = ""
    """Free-form secondary line: artist for tracks, owner for playlists, etc."""
    uri: str = ""
    didl_meta: str = ""
    album_art_url: str = ""
    duration_seconds: float = 0.0
    service: str = ""
    """Name of the source service (e.g. ``"Spotify"``, ``"Sonos Favorites"``)."""


@dataclass(frozen=True)
class Playable:
    """Resolved playback target for a ``MusicItem``.

    ``uri`` is handed to the speaker backend. ``didl_meta`` is passed along
    as ``play_uri``'s ``meta`` argument when non-empty — required for
    containers/stations that don't include inline metadata.
    """

    uri: str
    didl_meta: str = ""
    title: str = ""


class MusicBackend(ABC):
    """Abstract music backend — browse, search, and resolve playable URIs."""

    _registry: dict[str, type[MusicBackend]] = {}
    backend_name: str = ""
    supports_queue: bool = False
    """Declares whether this backend supports adding resolved items to a
    speaker queue. Backends whose playback path goes through a speaker
    that owns a persistent queue (e.g. Sonos + SMAPI) override this to
    ``True``; backends that only support one-shot playback leave it
    ``False`` and the queue tools stay hidden."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            MusicBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[MusicBackend]]:
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

    # --- Browse ---

    @abstractmethod
    async def list_favorites(self) -> list[MusicItem]:
        """Return the user's favorites from the music system.

        Favorites are a curated, service-agnostic set: tracks, playlists,
        stations, and albums the user has explicitly starred. This is the
        most reliable discovery surface — no auth, no search, just whatever
        the user has already chosen to keep around.
        """
        ...

    @abstractmethod
    async def list_playlists(self) -> list[MusicItem]:
        """Return the user's saved playlists."""
        ...

    # --- Search ---

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        kind: MusicItemKind = MusicItemKind.TRACK,
        limit: int = 10,
    ) -> list[MusicItem]:
        """Search within the linked/preferred music service.

        May require one-time authentication (e.g. Sonos SMAPI). Backends
        that can't search should raise ``MusicSearchUnavailableError`` rather
        than returning an empty list so the service layer can surface a
        helpful message.
        """
        ...

    # --- Playback resolution ---

    @abstractmethod
    async def resolve_playable(self, item: MusicItem) -> Playable:
        """Resolve a ``MusicItem`` into something the speaker can play.

        For items whose ``uri`` is already populated (most favorites,
        playlists), this is a pass-through. For search results that carry
        only an opaque ``id``, the backend converts it into a playable
        Sonos URI via ``sonos_uri_from_id`` or an equivalent mechanism.
        """
        ...


class MusicSearchUnavailableError(RuntimeError):
    """Raised when the backend can't perform a search.

    Typically because a required auth flow hasn't been completed yet (e.g.
    SoCo's ``MusicService`` needs a one-time SMAPI linking step). Services
    should catch this and present a legible message to the user.
    """


@dataclass(frozen=True)
class SearchResults:
    """Grouped search results by kind.

    Kept as a convenience wrapper for callers that want to display multiple
    result kinds in a single response (the existing ``/music search`` tool
    returns this shape). Backends are free to populate only the kind the
    caller asked for.
    """

    tracks: list[MusicItem] = field(default_factory=list)
    albums: list[MusicItem] = field(default_factory=list)
    playlists: list[MusicItem] = field(default_factory=list)
    stations: list[MusicItem] = field(default_factory=list)
