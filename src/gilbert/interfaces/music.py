"""Music service interface — search, browse, and resolve playable URIs."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ArtistInfo:
    """Information about a music artist."""

    artist_id: str
    name: str
    external_url: str = ""


@dataclass(frozen=True)
class AlbumInfo:
    """Information about a music album."""

    album_id: str
    name: str
    artists: list[ArtistInfo] = field(default_factory=list)
    album_art_url: str = ""
    release_date: str = ""
    total_tracks: int = 0
    external_url: str = ""


@dataclass(frozen=True)
class TrackInfo:
    """Full metadata for a music track."""

    track_id: str
    name: str
    artists: list[ArtistInfo] = field(default_factory=list)
    album: AlbumInfo | None = None
    duration_seconds: float = 0.0
    track_number: int = 0
    uri: str = ""
    external_url: str = ""
    preview_url: str = ""
    explicit: bool = False


@dataclass(frozen=True)
class PlaylistInfo:
    """Information about a playlist."""

    playlist_id: str
    name: str
    description: str = ""
    owner: str = ""
    track_count: int = 0
    external_url: str = ""
    image_url: str = ""


@dataclass(frozen=True)
class PlaylistDetail:
    """A playlist with its tracks."""

    playlist: PlaylistInfo
    tracks: list[TrackInfo] = field(default_factory=list)


@dataclass(frozen=True)
class SearchResults:
    """Results from a music search."""

    tracks: list[TrackInfo] = field(default_factory=list)
    albums: list[AlbumInfo] = field(default_factory=list)
    playlists: list[PlaylistInfo] = field(default_factory=list)


class MusicBackend(ABC):
    """Abstract music backend. Implementation-agnostic."""

    _registry: dict[str, type["MusicBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            MusicBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["MusicBackend"]]:
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

    # --- Search ---

    @abstractmethod
    async def search(self, query: str, *, limit: int = 10) -> SearchResults:
        """Search for tracks, albums, and playlists."""
        ...

    # --- Track info ---

    @abstractmethod
    async def get_track(self, track_id: str) -> TrackInfo | None:
        """Get full metadata for a track by ID."""
        ...

    @abstractmethod
    async def get_album(self, album_id: str) -> AlbumInfo | None:
        """Get album information by ID."""
        ...

    @abstractmethod
    async def get_album_tracks(self, album_id: str) -> list[TrackInfo]:
        """Get all tracks in an album."""
        ...

    # --- Playlists ---

    @abstractmethod
    async def get_playlist(self, playlist_id: str) -> PlaylistDetail | None:
        """Get a playlist with its tracks."""
        ...

    # --- Playback URIs ---

    @abstractmethod
    async def get_playable_uri(self, track_id: str) -> str:
        """Get a URI that can be passed to a speaker system for playback.

        The returned URI format depends on the backend and the speaker system.
        For example, Spotify returns 'spotify:track:xxx' URIs that Sonos can
        play natively.
        """
        ...
