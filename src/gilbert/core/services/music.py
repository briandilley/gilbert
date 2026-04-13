"""Music service — wraps a MusicBackend as a discoverable service with speaker integration."""

import json
import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.music import (
    AlbumInfo,
    ArtistInfo,
    MusicBackend,
    PlaylistDetail,
    PlaylistInfo,
    SearchResults,
    TrackInfo,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


def _track_to_dict(t: TrackInfo) -> dict[str, Any]:
    """Serialize a TrackInfo to a JSON-friendly dict."""
    d: dict[str, Any] = {
        "track_id": t.track_id,
        "name": t.name,
        "artists": [{"artist_id": a.artist_id, "name": a.name, "url": a.external_url} for a in t.artists],
        "duration_seconds": t.duration_seconds,
        "track_number": t.track_number,
        "uri": t.uri,
        "external_url": t.external_url,
        "explicit": t.explicit,
    }
    if t.album:
        d["album"] = {
            "album_id": t.album.album_id,
            "name": t.album.name,
            "album_art_url": t.album.album_art_url,
            "release_date": t.album.release_date,
            "total_tracks": t.album.total_tracks,
            "external_url": t.album.external_url,
        }
    if t.preview_url:
        d["preview_url"] = t.preview_url
    return d


def _album_to_dict(a: AlbumInfo) -> dict[str, Any]:
    """Serialize an AlbumInfo to a JSON-friendly dict."""
    return {
        "album_id": a.album_id,
        "name": a.name,
        "artists": [{"artist_id": ar.artist_id, "name": ar.name, "url": ar.external_url} for ar in a.artists],
        "album_art_url": a.album_art_url,
        "release_date": a.release_date,
        "total_tracks": a.total_tracks,
        "external_url": a.external_url,
    }


def _playlist_to_dict(p: PlaylistInfo) -> dict[str, Any]:
    """Serialize a PlaylistInfo to a JSON-friendly dict."""
    return {
        "playlist_id": p.playlist_id,
        "name": p.name,
        "description": p.description,
        "owner": p.owner,
        "track_count": p.track_count,
        "external_url": p.external_url,
        "image_url": p.image_url,
    }


class MusicService(Service):
    """Exposes a MusicBackend as a service with search, metadata, and speaker playback."""

    def __init__(self) -> None:
        self._backend: MusicBackend | None = None
        self._backend_name: str = "spotify"
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._speaker_svc: Any | None = None
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="music",
            capabilities=frozenset({"music", "ai_tools"}),
            optional=frozenset({"configuration", "speaker_control"}),
            toggleable=True,
            toggle_description="Music playback and search",
        )

    @property
    def backend(self) -> MusicBackend | None:
        return self._backend

    def _get_speaker_svc(self) -> Any:
        """Lazily resolve the speaker service.

        The speaker service may start after the music service (it's optional),
        so we resolve it on first use rather than at startup.
        """
        if self._speaker_svc is None and self._resolver is not None:
            self._speaker_svc = self._resolver.get_capability("speaker_control")
        return self._speaker_svc

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Load config
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Music service disabled")
            return

        self._enabled = True
        self._config = section.get("settings", self._config)

        backend_name = section.get("backend", "spotify")
        self._backend_name = backend_name
        try:
            import gilbert.integrations.spotify_music  # noqa: F401
        except ImportError:
            pass
        backends = MusicBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown music backend: {backend_name}")
        self._backend = backend_cls()

        # Initialize backend with settings (includes credentials)
        await self._backend.initialize(self._config)
        logger.info("Music service started")

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "music"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.spotify_music  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Music backend type.",
                default="spotify", restart_required=True,
                choices=tuple(MusicBackend.registered_backends().keys()) or ("spotify",),
            ),
        ]
        backends = MusicBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(ConfigParam(
                    key=f"settings.{bp.key}", type=bp.type,
                    description=bp.description, default=bp.default,
                    restart_required=bp.restart_required, sensitive=bp.sensitive,
                    choices=bp.choices, choices_from=bp.choices_from,
                    multiline=bp.multiline, backend_param=True,
                ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config = config.get("settings", self._config)

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # --- Core operations ---

    async def search(self, query: str, *, limit: int = 10) -> SearchResults:
        """Search for music."""
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        return await self._backend.search(query, limit=limit)

    async def get_track(self, track_id: str) -> TrackInfo | None:
        """Get track metadata."""
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        return await self._backend.get_track(track_id)

    async def get_album(self, album_id: str) -> AlbumInfo | None:
        """Get album metadata."""
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        return await self._backend.get_album(album_id)

    async def get_album_tracks(self, album_id: str) -> list[TrackInfo]:
        """Get all tracks in an album."""
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        return await self._backend.get_album_tracks(album_id)

    async def get_playlist(self, playlist_id: str) -> PlaylistDetail | None:
        """Get a playlist with its tracks."""
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        return await self._backend.get_playlist(playlist_id)

    async def play_track(
        self,
        track_id: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        position_seconds: float | None = None,
    ) -> TrackInfo:
        """Play a track on speakers via the speaker service.

        Returns the track metadata for display purposes.
        """
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        speaker_svc = self._get_speaker_svc()
        if speaker_svc is None:
            raise RuntimeError("Speaker service is not available — cannot play music")

        track = await self._backend.get_track(track_id)
        if track is None:
            raise KeyError(f"Track not found: {track_id}")

        uri = await self._backend.get_playable_uri(track_id)
        title = f"{track.name} — {', '.join(a.name for a in track.artists)}"

        await speaker_svc.play_on_speakers(
            uri=uri,
            speaker_names=speaker_names,
            volume=volume,
            title=title,
            position_seconds=position_seconds,
        )

        return track

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "music"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        tools = [
            ToolDefinition(
                name="search_music",
                slash_group="music",
                slash_command="search",
                slash_help="Search for tracks/albums/playlists: /music search <query>",
                description="Search for tracks, albums, and playlists.",
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Search query (song name, artist, etc.).",
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Maximum results per type (default 10).",
                        required=False,
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="get_track_info",
                slash_group="music",
                slash_command="track",
                slash_help="Track metadata: /music track <track_id>",
                description="Get full metadata for a track (name, artist, album, art, duration, links).",
                parameters=[
                    ToolParameter(
                        name="track_id",
                        type=ToolParameterType.STRING,
                        description="The track ID.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="get_album_info",
                slash_group="music",
                slash_command="album",
                slash_help="Album metadata + tracks: /music album <album_id>",
                description="Get album metadata and track listing.",
                parameters=[
                    ToolParameter(
                        name="album_id",
                        type=ToolParameterType.STRING,
                        description="The album ID.",
                    ),
                    ToolParameter(
                        name="include_tracks",
                        type=ToolParameterType.BOOLEAN,
                        description="Include full track listing (default true).",
                        required=False,
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="get_playlist",
                slash_group="music",
                slash_command="playlist",
                slash_help="Playlist + tracks: /music playlist <playlist_id>",
                description="Get a playlist with its tracks.",
                parameters=[
                    ToolParameter(
                        name="playlist_id",
                        type=ToolParameterType.STRING,
                        description="The playlist ID.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="play_track",
                slash_group="music",
                slash_command="play",
                slash_help=(
                    "Play a track: /music play <track_id> speakers=..."
                ),
                description=(
                    "Play a music track on speakers. Optionally start at a specific "
                    "position in the song. If no speakers specified, uses last-used or all."
                ),
                parameters=[
                    ToolParameter(
                        name="track_id",
                        type=ToolParameterType.STRING,
                        description="The track ID to play.",
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, uses last-used or all.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100).",
                        required=False,
                    ),
                    ToolParameter(
                        name="position_seconds",
                        type=ToolParameterType.NUMBER,
                        description="Start playback at this position in seconds (e.g., 12.5 to start at the 12.5s mark).",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]
        return tools

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "search_music":
                return await self._tool_search(arguments)
            case "get_track_info":
                return await self._tool_get_track(arguments)
            case "get_album_info":
                return await self._tool_get_album(arguments)
            case "get_playlist":
                return await self._tool_get_playlist(arguments)
            case "play_track":
                return await self._tool_play_track(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_search(self, arguments: dict[str, Any]) -> str:
        query = arguments["query"]
        limit = arguments.get("limit", 10)
        results = await self.search(query, limit=limit)
        return json.dumps({
            "tracks": [_track_to_dict(t) for t in results.tracks],
            "albums": [_album_to_dict(a) for a in results.albums],
            "playlists": [_playlist_to_dict(p) for p in results.playlists],
        })

    async def _tool_get_track(self, arguments: dict[str, Any]) -> str:
        track = await self.get_track(arguments["track_id"])
        if track is None:
            return json.dumps({"error": "Track not found"})
        return json.dumps(_track_to_dict(track))

    async def _tool_get_album(self, arguments: dict[str, Any]) -> str:
        album_id = arguments["album_id"]
        include_tracks = arguments.get("include_tracks", True)

        album = await self.get_album(album_id)
        if album is None:
            return json.dumps({"error": "Album not found"})

        result = _album_to_dict(album)
        if include_tracks:
            tracks = await self.get_album_tracks(album_id)
            result["tracks"] = [_track_to_dict(t) for t in tracks]

        return json.dumps(result)

    async def _tool_get_playlist(self, arguments: dict[str, Any]) -> str:
        detail = await self.get_playlist(arguments["playlist_id"])
        if detail is None:
            return json.dumps({"error": "Playlist not found"})

        result = _playlist_to_dict(detail.playlist)
        result["tracks"] = [_track_to_dict(t) for t in detail.tracks]
        return json.dumps(result)

    async def _tool_play_track(self, arguments: dict[str, Any]) -> str:
        track_id = arguments["track_id"]
        speaker_names: list[str] = arguments.get("speakers", [])
        volume: int | None = arguments.get("volume")
        position: float | None = arguments.get("position_seconds")

        try:
            track = await self.play_track(
                track_id=track_id,
                speaker_names=speaker_names or None,
                volume=volume,
                position_seconds=position,
            )
        except RuntimeError as e:
            logger.error("play_track failed: %s", e)
            return json.dumps({"error": str(e)})
        except KeyError as e:
            logger.error("play_track failed: %s", e)
            return json.dumps({"error": str(e)})

        result = _track_to_dict(track)
        result["status"] = "playing"
        if position:
            result["started_at_seconds"] = position
        return json.dumps(result)
