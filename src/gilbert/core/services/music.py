"""Music service — wraps a MusicBackend as a discoverable service.



Thin orchestration layer. The backend (e.g. ``SonosMusic``) does the
heavy lifting of browsing favorites, searching, and resolving playable
URIs; this service exposes those operations as AI tools and slash
commands, and hands resolved URIs off to the speaker service for
playback.

No per-track metadata lookups — Sonos can't do ID-based retrieval across
linked services, so the tool surface is browse-first: list favorites and
playlists, then play by title or index.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.music import (
    LinkedMusicServiceLister,
    MusicBackend,
    MusicItem,
    MusicItemKind,
    MusicSearchUnavailableError,
    Playable,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import NowPlaying, PlaybackState
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


def _item_to_dict(item: MusicItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "kind": item.kind.value,
        "subtitle": item.subtitle,
        "service": item.service,
        "album_art_url": item.album_art_url,
        "duration_seconds": item.duration_seconds,
    }


def _now_playing_to_dict(np: NowPlaying) -> dict[str, Any]:
    return {
        "state": np.state.value,
        "is_playing": np.state == PlaybackState.PLAYING,
        "title": np.title,
        "artist": np.artist,
        "album": np.album,
        "album_art_url": np.album_art_url,
        "uri": np.uri,
        "duration_seconds": np.duration_seconds,
        "position_seconds": np.position_seconds,
    }


def _fuzzy_find(items: list[MusicItem], needle: str) -> MusicItem | None:
    """Find the first item whose title contains ``needle`` (case-insensitive).

    Falls back to a prefix match, then an exact-id match.
    """
    if not needle:
        return None
    low = needle.lower()
    for item in items:
        if item.title.lower() == low:
            return item
    for item in items:
        if low in item.title.lower():
            return item
    for item in items:
        if item.id == needle:
            return item
    return None


class MusicService(Service):
    """Browse, search, and play music through a ``MusicBackend``."""

    def __init__(self) -> None:
        self._backend: MusicBackend | None = None
        self._backend_name: str = "sonos"
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
        if self._speaker_svc is None and self._resolver is not None:
            self._speaker_svc = self._resolver.get_capability("speaker_control")
        return self._speaker_svc

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Music service disabled")
            return

        self._enabled = True
        self._config = section.get("settings", self._config)

        backend_name = section.get("backend", "sonos")
        self._backend_name = backend_name
        backends = MusicBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown music backend: {backend_name}")
        self._backend = backend_cls()

        await self._backend.initialize(self._config)
        logger.info("Music service started (backend=%s)", backend_name)

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "music"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Music backend type.",
                default="sonos", restart_required=True,
                choices=tuple(MusicBackend.registered_backends().keys()),
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
        if self._backend is not None:
            try:
                await self._backend.initialize(self._config)
            except Exception:
                logger.exception("Failed to re-initialize music backend after config change")

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # --- ConfigActionProvider ---
    #
    # The service forwards backend-declared actions directly — SonosMusic
    # owns the auth/test flow. If backends need no actions, this list is
    # just empty and no buttons render.

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=MusicBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    # --- Core operations ---

    def _require_backend(self) -> MusicBackend:
        if self._backend is None:
            raise RuntimeError("Music service is not enabled")
        return self._backend

    async def list_favorites(self) -> list[MusicItem]:
        return await self._require_backend().list_favorites()

    async def list_playlists(self) -> list[MusicItem]:
        return await self._require_backend().list_playlists()

    async def search(
        self,
        query: str,
        *,
        kind: MusicItemKind = MusicItemKind.TRACK,
        limit: int = 10,
    ) -> list[MusicItem]:
        return await self._require_backend().search(query, kind=kind, limit=limit)

    async def play_item(
        self,
        item: MusicItem,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
    ) -> Playable:
        """Resolve an item into a playable URI and start playback."""
        speaker_svc = self._get_speaker_svc()
        if speaker_svc is None:
            raise RuntimeError("Speaker service is not available — cannot play music")

        playable = await self._require_backend().resolve_playable(item)

        await speaker_svc.play_on_speakers(
            uri=playable.uri,
            speaker_names=speaker_names,
            volume=volume,
            title=playable.title or item.title,
            didl_meta=playable.didl_meta,
        )
        return playable

    def list_linked_services(self) -> list[str]:
        """Forward to the backend. Satisfies ``LinkedMusicServiceLister``
        so the configuration service can populate the preferred-service
        dropdown without reaching into the backend directly.
        """
        if isinstance(self._backend, LinkedMusicServiceLister):
            return self._backend.list_linked_services()
        return []

    async def now_playing(self, speaker_name: str | None = None) -> NowPlaying:
        speaker_svc = self._get_speaker_svc()
        if speaker_svc is None:
            raise RuntimeError("Speaker service is not available — cannot query playback")
        return cast(NowPlaying, await speaker_svc.get_now_playing(speaker_name))

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "music"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="list_favorites",
                slash_group="music",
                slash_command="favorites",
                slash_help="List Sonos favorites: /music favorites",
                description=(
                    "List the user's Sonos favorites (tracks, playlists, "
                    "radio stations)."
                ),
                required_role="everyone",
            ),
            ToolDefinition(
                name="list_playlists",
                slash_group="music",
                slash_command="playlists",
                slash_help="List saved Sonos playlists: /music playlists",
                description="List the user's saved Sonos playlists.",
                required_role="everyone",
            ),
            ToolDefinition(
                name="search_music",
                slash_group="music",
                slash_command="search",
                slash_help=(
                    "Search linked music service: /music search <query> [kind=tracks]"
                ),
                description=(
                    "Search the music service linked to Sonos "
                    "(default: Spotify). Returns tracks, albums, or "
                    "playlists matching the query."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Search query (song, artist, album, etc.).",
                    ),
                    ToolParameter(
                        name="kind",
                        type=ToolParameterType.STRING,
                        description="What to search for.",
                        required=False,
                        enum=["tracks", "albums", "playlists", "artists", "stations"],
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Maximum results (default 10).",
                        required=False,
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="play_music",
                slash_group="music",
                slash_command="play",
                slash_help=(
                    "Play by title or search: /music play <title> "
                    "[speakers=...] [source=favorites|playlists|search]"
                ),
                description=(
                    "Play music by title. By default searches favorites "
                    "first, then playlists, then runs a fresh search. "
                    "Set ``source`` to restrict the lookup."
                ),
                parameters=[
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description=(
                            "Title to match (track, playlist, or favorite name)."
                        ),
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100).",
                        required=False,
                    ),
                    ToolParameter(
                        name="source",
                        type=ToolParameterType.STRING,
                        description=(
                            "Restrict lookup: favorites, playlists, or search."
                        ),
                        required=False,
                        enum=["favorites", "playlists", "search"],
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="now_playing",
                slash_group="music",
                slash_command="now",
                slash_help="What's playing now: /music now [speaker]",
                description=(
                    "Get what's currently playing on a speaker: state, "
                    "title, artist, album, and progress. Speaker is "
                    "auto-picked (last-used → playing → first) if not given."
                ),
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Speaker name or alias. Omit to auto-pick.",
                        required=False,
                    ),
                ],
                required_role="everyone",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "list_favorites":
                return await self._tool_list_favorites()
            case "list_playlists":
                return await self._tool_list_playlists()
            case "search_music":
                return await self._tool_search(arguments)
            case "play_music":
                return await self._tool_play(arguments)
            case "now_playing":
                return await self._tool_now_playing(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_list_favorites(self) -> str:
        items = await self.list_favorites()
        return json.dumps({"favorites": [_item_to_dict(i) for i in items]})

    async def _tool_list_playlists(self) -> str:
        items = await self.list_playlists()
        return json.dumps({"playlists": [_item_to_dict(i) for i in items]})

    async def _tool_search(self, arguments: dict[str, Any]) -> str:
        query = arguments["query"]
        kind_str = arguments.get("kind", "tracks")
        limit = arguments.get("limit", 10)
        kind_map = {
            "tracks": MusicItemKind.TRACK,
            "albums": MusicItemKind.ALBUM,
            "playlists": MusicItemKind.PLAYLIST,
            "artists": MusicItemKind.ARTIST,
            "stations": MusicItemKind.STATION,
        }
        kind = kind_map.get(kind_str, MusicItemKind.TRACK)
        try:
            results = await self.search(query, kind=kind, limit=limit)
        except MusicSearchUnavailableError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({
            "kind": kind.value,
            "results": [_item_to_dict(i) for i in results],
        })

    async def _tool_play(self, arguments: dict[str, Any]) -> str:
        title = arguments["title"]
        speakers = arguments.get("speakers") or None
        volume = arguments.get("volume")
        source = arguments.get("source", "")

        item: MusicItem | None = None
        sources_tried: list[str] = []

        async def _try_favorites() -> MusicItem | None:
            items = await self.list_favorites()
            return _fuzzy_find(items, title)

        async def _try_playlists() -> MusicItem | None:
            items = await self.list_playlists()
            return _fuzzy_find(items, title)

        async def _try_search() -> MusicItem | None:
            try:
                results = await self.search(title, kind=MusicItemKind.TRACK, limit=1)
            except MusicSearchUnavailableError:
                return None
            return results[0] if results else None

        if source == "favorites":
            sources_tried.append("favorites")
            item = await _try_favorites()
        elif source == "playlists":
            sources_tried.append("playlists")
            item = await _try_playlists()
        elif source == "search":
            sources_tried.append("search")
            item = await _try_search()
        else:
            # Default: favorites → playlists → search
            sources_tried.append("favorites")
            item = await _try_favorites()
            if item is None:
                sources_tried.append("playlists")
                item = await _try_playlists()
            if item is None:
                sources_tried.append("search")
                item = await _try_search()

        if item is None:
            return json.dumps({
                "error": f"No music found matching '{title}'",
                "sources_tried": sources_tried,
            })

        try:
            playable = await self.play_item(item, speaker_names=speakers, volume=volume)
        except MusicSearchUnavailableError as exc:
            return json.dumps({"error": str(exc)})
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps({
            "status": "playing",
            "title": playable.title or item.title,
            "kind": item.kind.value,
            "service": item.service,
            "uri": playable.uri,
            "source": sources_tried[-1] if sources_tried else "",
        })

    async def _tool_now_playing(self, arguments: dict[str, Any]) -> str:
        speaker_name: str | None = arguments.get("speaker") or None
        try:
            now = await self.now_playing(speaker_name)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
        except KeyError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(_now_playing_to_dict(now))
