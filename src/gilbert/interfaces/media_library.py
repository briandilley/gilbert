"""Media library interface — ABC, dataclasses, capability protocol, and errors.

The ``MediaLibraryBackend`` ABC is the universal backend pattern (see
``memory-backend-pattern.md``): an ``__init_subclass__`` registry, a
``backend_name`` identifier, a ``backend_config_params()`` classmethod
for the Settings UI, and ``initialize()`` / ``close()`` lifecycle.

Concrete backends live in ``std-plugins/plex/`` and
``std-plugins/jellyfin/``. The ``MediaLibraryService`` aggregator holds
``dict[str, MediaLibraryBackend]`` (precedent: ``AuthService`` /
``KnowledgeService``), fans queries out, and merges results.

Layer rules (per ``CLAUDE.md``): this module imports from
``interfaces/configuration.py`` only. No imports from ``core/``,
``integrations/``, or any plugin.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gilbert.interfaces.configuration import ConfigParam

__all__ = [
    "BackendHealth",
    "ContinueWatchingEntry",
    "MediaClient",
    "MediaClientAmbiguousError",
    "MediaClientNotFoundError",
    "MediaItem",
    "MediaKind",
    "MediaLibraryBackend",
    "MediaLibraryError",
    "MediaLibraryProvider",
    "MediaLibraryUnavailableError",
    "MediaPlayCommand",
    "MediaPlaybackState",
    "MediaSearchFilters",
    "MediaSession",
    "RecentlyAddedEntry",
]


# ── Enums ───────────────────────────────────────────────────────────


class MediaKind(StrEnum):
    """What kind of thing a library item represents."""

    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"
    MUSIC_ARTIST = "music_artist"
    MUSIC_ALBUM = "music_album"
    MUSIC_TRACK = "music_track"
    MUSIC_VIDEO = "music_video"
    PHOTO = "photo"
    UNKNOWN = "unknown"


class MediaPlaybackState(StrEnum):
    """Current playback state of a media client / session."""

    PLAYING = "playing"
    PAUSED = "paused"
    BUFFERING = "buffering"
    STOPPED = "stopped"


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaItem:
    """Unified descriptor for a movie, show, episode, album, track, etc.

    ``id`` is opaque and backend-specific (Plex: ``ratingKey``; Jellyfin:
    item Id GUID). ``backend_name`` identifies which backend owns the id —
    callers must pass it back unchanged when resolving / playing. Two
    backends can share the same numeric id with no collision because
    ``(backend_name, id)`` is the actual key.

    ``added_at`` and ``last_viewed_at`` are UTC unix timestamps. Backends
    MUST normalize at the mapping boundary.

    ``view_offset_seconds`` is per the *querying* user. When this item is
    serialized into a button payload and clicked later, the service
    re-resolves the offset via ``get_item(item_id, backend_user_id=…)``
    using the clicker's mapped id — the embedded value is ignored for
    button-driven plays.
    """

    id: str
    backend_name: str
    server_id: str
    title: str
    kind: MediaKind
    sort_title: str = ""
    year: int | None = None
    duration_seconds: float = 0.0
    summary: str = ""
    rating: float | None = None
    content_rating: str = ""
    studio: str = ""
    genres: tuple[str, ...] = field(default_factory=tuple)
    actors: tuple[str, ...] = field(default_factory=tuple)
    directors: tuple[str, ...] = field(default_factory=tuple)
    poster_url: str = ""
    backdrop_url: str = ""
    parent_id: str = ""
    parent_title: str = ""
    grandparent_id: str = ""
    grandparent_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    library_section: str = ""
    added_at: float = 0.0
    last_viewed_at: float = 0.0
    view_count: int = 0
    view_offset_seconds: float = 0.0
    is_watched: bool = False


@dataclass(frozen=True)
class MediaClient:
    """A target the library backend can dispatch playback to.

    Plex calls these "Players" (Plex for Apple TV, Plex Web, …);
    Jellyfin calls them "Sessions" (devices with an active client
    connection). Each one has a stable identifier the playback API
    uses to address it.
    """

    client_id: str
    backend_name: str
    server_id: str
    name: str
    device: str = ""
    platform: str = ""
    address: str = ""
    user_id: str = ""
    is_online: bool = True
    supports_remote_control: bool = True
    supports_seek: bool = True
    supports_audio_stream_select: bool = False
    supports_subtitle_stream_select: bool = False
    last_seen_at: float = 0.0


@dataclass(frozen=True)
class MediaSession:
    """An in-progress playback session on a media client."""

    session_id: str
    backend_name: str
    client: MediaClient
    item: MediaItem
    state: MediaPlaybackState
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    backend_user_name: str = ""
    started_at: float = 0.0
    is_transcoding: bool = False
    quality_label: str = ""


@dataclass(frozen=True)
class RecentlyAddedEntry:
    """One slot in a recently-added feed."""

    item: MediaItem
    added_at: float


@dataclass(frozen=True)
class ContinueWatchingEntry:
    """One slot in a per-user continue-watching feed.

    For TV the entry may reference a *next-up* episode (offset 0) — the
    ``next_up`` flag distinguishes that case so the AI can phrase 'pick
    up where you left off' versus 'start the next episode'.
    """

    item: MediaItem
    next_up: bool = False


@dataclass(frozen=True)
class MediaPlayCommand:
    """Composed playback request.

    ``offset_seconds`` overrides the item's view_offset (the service
    sets it explicitly so behaviour is callable-controlled).

    ``idempotency_key`` lets the per-client lock dedupe AI-loop /
    network-retry repeats within a 5-second window.
    """

    item: MediaItem
    client: MediaClient
    offset_seconds: float = 0.0
    idempotency_key: str = ""


@dataclass(frozen=True)
class MediaSearchFilters:
    """Optional filters to narrow a library search.

    ``limit`` is service-side capped at 50 — see
    ``MediaLibraryService._fanout`` and ``memory-media-library-service.md``.
    """

    kinds: tuple[MediaKind, ...] = field(default_factory=tuple)
    library_section: str = ""
    year_from: int | None = None
    year_to: int | None = None
    genre: str = ""
    unwatched_only: bool = False
    limit: int = 30


@dataclass(frozen=True)
class BackendHealth:
    """Per-backend health status surfaced by ``list_backend_health``.

    Populated by the service as a side effect of every fan-out call:
    successful operations move the backend to ``healthy``; timeouts to
    ``degraded``; auth failures to ``unhealthy``.
    """

    backend_name: str
    status: str
    last_error: str = ""
    last_error_at: float = 0.0
    last_success_at: float = 0.0


# ── Errors ──────────────────────────────────────────────────────────


class MediaLibraryError(RuntimeError):
    """Base class for media-library domain errors. Callers can catch
    the family with one ``except``.
    """


class MediaLibraryUnavailableError(MediaLibraryError):
    """Raised when a backend can't fulfill a request — typically because
    configured credentials are missing/invalid or the upstream server
    is unreachable. Services catch this and surface the message in the
    tool result rather than crashing the AI turn.
    """


class MediaClientNotFoundError(MediaLibraryError):
    """Raised when the AI asks to play on a client that doesn't exist
    on any configured backend.
    """


class MediaClientAmbiguousError(MediaLibraryError):
    """Raised by ``find_client()`` when the caller-supplied name matches
    multiple clients and no disambiguation context is available. The
    ``candidates`` attribute carries the matches so the caller can
    surface choices to the user.
    """

    def __init__(self, message: str, candidates: list[MediaClient]) -> None:
        super().__init__(message)
        self.candidates = list(candidates)


# ── ABC ─────────────────────────────────────────────────────────────


class MediaLibraryBackend(ABC):
    """Abstract media library backend — search, browse, dispatch playback.

    Subclasses set ``backend_name = "plex"`` (etc.) and override the
    capability flags they actually support. ``__init_subclass__``
    registers the class in the shared ``_registry``.
    """

    _registry: dict[str, type[MediaLibraryBackend]] = {}
    backend_name: str = ""

    # --- Capability flags (overridden by concrete backends) ---

    supports_now_playing: bool = False
    """Backend can return active sessions with progress / state."""

    supports_resume: bool = False
    """Backend reports per-item view_offset and can start playback at it."""

    supports_continue_watching: bool = False
    """Backend can return a per-user 'on deck' / 'continue watching' list."""

    supports_recently_added: bool = False
    """Backend can return a recently-added feed."""

    supports_seek: bool = False
    """Backend's clients accept absolute-position seek commands."""

    supports_per_user: bool = False
    """Backend has a notion of multiple users (Jellyfin always; Plex
    only when Plex Home is configured) and the per-user APIs (resume,
    history) require a user mapping."""

    supports_next_episode: bool = False
    """Backend can resolve a SHOW (or SEASON) item to the user's
    next-unwatched / on-deck episode. Required by ``play_on``'s
    show-resolution logic."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            # Explicit write to the ABC's class attribute — NOT
            # ``cls._registry[...]`` which would create a per-subclass
            # shadow dict. Mirrors the MusicBackend / TTSBackend
            # convention.
            MediaLibraryBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[MediaLibraryBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    # --- Lifecycle ---

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the backend with provider-specific configuration."""

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""

    # --- Library queries ---

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        filters: MediaSearchFilters | None = None,
        backend_user_id: str = "",
    ) -> list[MediaItem]:
        """Full-text search across all libraries the user can see.

        ``backend_user_id`` is the Plex/Jellyfin user id Gilbert mapped
        the calling Gilbert user to. Empty string means 'use the
        backend's primary / admin user' — acceptable for shared-account
        deployments.
        """

    @abstractmethod
    async def get_item(
        self, item_id: str, backend_user_id: str = ""
    ) -> MediaItem | None:
        """Resolve an opaque id back into a fresh ``MediaItem``.

        Implementations should re-query the backend with the supplied
        ``backend_user_id`` so ``view_offset_seconds`` reflects the
        clicker's progress.
        """

    @abstractmethod
    async def list_libraries(self, backend_user_id: str = "") -> list[str]:
        """Return library section names (e.g. 'Movies', 'TV Shows')."""

    @abstractmethod
    async def list_backend_users(self) -> list[dict[str, str]]:
        """Return ``[{id, username, display_name}]`` for every user on
        this backend's server. Used by the Settings UI's User Mappings
        panel.
        """

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
        library_section: str = "",
        backend_user_id: str = "",
    ) -> list[RecentlyAddedEntry]:
        """Return the most-recently-added items.

        Backends that don't support this raise ``NotImplementedError``;
        the service guards on ``supports_recently_added`` before
        calling, so the default raise is defense-in-depth.
        """
        raise NotImplementedError(
            "This media library backend does not support recently-added"
        )

    async def continue_watching(
        self,
        *,
        backend_user_id: str = "",
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]:
        """Return the per-user continue-watching feed."""
        raise NotImplementedError(
            "This media library backend does not support continue-watching"
        )

    async def next_episode(
        self,
        show_id: str,
        *,
        backend_user_id: str = "",
    ) -> MediaItem | None:
        """Return the user's next-unwatched (or next-resumable) episode.

        Resolution policy:
        1. If any episode has ``view_offset_seconds > 0``, return that.
        2. Otherwise return the lowest (season, episode) with
           ``view_count == 0``.
        3. If the user has watched everything, return ``None``.
        """
        raise NotImplementedError(
            "This media library backend does not support next-episode resolution"
        )

    # --- Clients & sessions ---

    @abstractmethod
    async def list_clients(self) -> list[MediaClient]:
        """Return online, remote-controllable clients on this backend.

        Filtered to clients reachable by Gilbert. Offline clients
        (last-known) are returned with ``is_online=False`` so the AI
        can phrase 'the Apple TV is asleep'.
        """

    async def now_playing(self) -> list[MediaSession]:
        """Return active sessions across all clients on this backend."""
        raise NotImplementedError(
            "This media library backend does not support now-playing"
        )

    # --- Playback control ---

    @abstractmethod
    async def play(
        self,
        command: MediaPlayCommand,
        *,
        backend_user_id: str = "",
    ) -> None:
        """Start playing ``command.item`` on ``command.client``.

        Replaces any current playback on the target client. If the item
        has a non-zero view_offset and ``command.offset_seconds`` is 0,
        backends should resume from the offset (the service sets this
        explicitly so behaviour is callable-controlled).
        """

    @abstractmethod
    async def pause(self, client_id: str) -> None: ...

    @abstractmethod
    async def resume(self, client_id: str) -> None: ...

    @abstractmethod
    async def stop(self, client_id: str) -> None: ...

    async def seek(self, client_id: str, position_seconds: float) -> None:
        """Jump to ``position_seconds`` on ``client_id``.

        Backends that don't support seek raise ``NotImplementedError``;
        the service guards on ``supports_seek`` before calling.
        """
        raise NotImplementedError("This backend does not support seek")


# ── Capability protocol ─────────────────────────────────────────────


@runtime_checkable
class MediaLibraryProvider(Protocol):
    """Capability protocol for the media library aggregator.

    Exposes only the read-only, fan-out-safe operations. Mutations
    (play / pause / etc.) and admin operations (user mapping)
    require the concrete service — consumers needing them depend
    on it explicitly via the composition root.

    The method signatures match ``MediaLibraryService`` exactly
    (kwarg names included). ``@runtime_checkable`` only verifies
    attribute presence — kwarg-name drift would pass ``isinstance``
    but break callers at invocation time, so the spec mandates
    signature parity.
    """

    async def search(
        self,
        query: str,
        *,
        kind: MediaKind | None = None,
        gilbert_user_id: str | None = None,
        filters: MediaSearchFilters | None = None,
    ) -> list[MediaItem]: ...

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
        gilbert_user_id: str | None = None,
    ) -> list[RecentlyAddedEntry]: ...

    async def continue_watching(
        self,
        *,
        gilbert_user_id: str,
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]: ...

    async def list_clients(self) -> list[MediaClient]: ...

    async def now_playing(
        self, client_name: str | None = None
    ) -> list[MediaSession]: ...

    async def list_backend_health(self) -> list[dict[str, object]]: ...

    async def user_can_see(
        self,
        gilbert_user_id: str,
        backend_name: str,
        library_section: str,
    ) -> bool: ...


# Type alias for fan-out callable used by the aggregator's _fanout
# helper. Documented here so the helper signature stays stable across
# backends; not exported.
_FanoutOp = Callable[[MediaLibraryBackend], Awaitable[Any]]
