"""Radio DJ service — context-aware music selection based on presence and user preferences.

Automatically selects music genres based on who is present, learns individual
preferences (likes/vetoes) over time, and rotates through default genres when
no preferences are known.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.music import SearchResults, TrackInfo
from gilbert.interfaces.presence import PresenceState, UserPresence
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Storage collections (namespaced under radio_dj.*)
_PREFS_COLLECTION = "preferences"
_STATE_COLLECTION = "state"
_STATE_ENTITY_ID = "dj_state"

# Defaults
_DEFAULT_GENRES = [
    "classic rock",
    "90s hits",
    "blues rock",
    "indie rock",
    "funk",
    "80s hits",
]
_DEFAULT_MIN_SWITCH_MINUTES = 15
_DEFAULT_VOLUME = 35
_DEFAULT_POLL_INTERVAL = 60


class RadioDJService(Service):
    """Context-aware music DJ that selects genres based on who's present.

    Tracks user likes/vetoes, rotates through default genres on cold start,
    and reacts to presence changes for automatic genre switching.
    """

    def __init__(self) -> None:
        # Config
        self._default_genres: list[str] = list(_DEFAULT_GENRES)
        self._min_switch_minutes: int = _DEFAULT_MIN_SWITCH_MINUTES
        self._default_volume: int = _DEFAULT_VOLUME
        self._speakers: list[str] = []
        self._stop_when_empty: bool = True
        self._poll_interval: int = _DEFAULT_POLL_INTERVAL

        # Dependencies (resolved in start())
        self._music_svc: Any = None
        self._speaker_svc: Any = None
        self._presence_svc: Any = None
        self._scheduler_svc: Any = None
        self._storage: Any = None
        self._event_bus: EventBus | None = None

        # Runtime state
        self._active: bool = False
        self._current_genre: str | None = None
        self._last_genre_switch: datetime | None = None
        self._genre_rotation_index: int = 0
        self._present_users: set[str] = set()
        self._stopped_by_empty: bool = False

        # Unsub callables for event bus
        self._unsubs: list[Any] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="radio_dj",
            capabilities=frozenset({"radio_dj", "ai_tools"}),
            requires=frozenset({"music", "speaker_control", "scheduler"}),
            optional=frozenset({"presence", "entity_storage", "event_bus", "configuration"}),
            events=frozenset({"radio_dj.genre.changed", "radio_dj.started", "radio_dj.stopped", "radio_dj.track.liked", "radio_dj.track.vetoed"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.scheduler import Schedule

        # Required dependencies
        self._music_svc = resolver.require_capability("music")
        self._speaker_svc = resolver.require_capability("speaker_control")
        self._scheduler_svc = resolver.require_capability("scheduler")

        # Optional dependencies
        self._presence_svc = resolver.get_capability("presence")

        # Storage
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None:
            backend = getattr(storage_svc, "backend", None)
            if backend is not None:
                from gilbert.interfaces.storage import NamespacedStorageBackend

                self._storage = NamespacedStorageBackend(backend, "radio_dj")

        # Event bus
        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            bus = getattr(event_bus_svc, "bus", None)
            if bus is not None:
                self._event_bus = bus
                self._unsubs.append(
                    self._event_bus.subscribe("presence.arrived", self._on_presence_arrived)
                )
                self._unsubs.append(
                    self._event_bus.subscribe("presence.departed", self._on_presence_departed)
                )

        # Configuration
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            get_section = getattr(config_svc, "get_section", None)
            if get_section is not None:
                section = get_section("radio_dj")
                self._apply_config(section)

        # Restore persisted state
        await self._restore_state()

        # Register polling job
        self._scheduler_svc.add_job(
            name="radio-dj-poll",
            schedule=Schedule.every(self._poll_interval),
            callback=self._poll,
            system=True,
        )

        logger.info(
            "Radio DJ service started (poll=%ds, genres=%d, volume=%d)",
            self._poll_interval,
            len(self._default_genres),
            self._default_volume,
        )

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._persist_state()

    # --- Configuration ---

    def _apply_config(self, section: dict[str, Any]) -> None:
        if "default_genres" in section:
            self._default_genres = list(section["default_genres"])
        if "min_switch_interval" in section:
            self._min_switch_minutes = int(section["min_switch_interval"])
        if "default_volume" in section:
            self._default_volume = int(section["default_volume"])
        if "speakers" in section:
            self._speakers = list(section["speakers"])
        if "stop_when_empty" in section:
            self._stop_when_empty = bool(section["stop_when_empty"])
        if "poll_interval" in section:
            self._poll_interval = int(section["poll_interval"])

    @property
    def config_namespace(self) -> str:
        return "radio_dj"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Enable/disable the radio DJ.", default=False,
                restart_required=True,
            ),
            ConfigParam(
                key="default_genres", type=ToolParameterType.ARRAY,
                description="Genre rotation for cold start.",
                default=_DEFAULT_GENRES,
            ),
            ConfigParam(
                key="min_switch_interval", type=ToolParameterType.INTEGER,
                description="Minimum minutes between auto genre switches.",
                default=_DEFAULT_MIN_SWITCH_MINUTES,
            ),
            ConfigParam(
                key="default_volume", type=ToolParameterType.INTEGER,
                description="Playback volume (0-100).",
                default=_DEFAULT_VOLUME,
            ),
            ConfigParam(
                key="speakers", type=ToolParameterType.ARRAY,
                description="Speaker names (empty = all).",
                default=[],
            ),
            ConfigParam(
                key="stop_when_empty", type=ToolParameterType.BOOLEAN,
                description="Stop playback when nobody is present.",
                default=True,
            ),
            ConfigParam(
                key="poll_interval", type=ToolParameterType.INTEGER,
                description="Seconds between presence polls.",
                default=_DEFAULT_POLL_INTERVAL,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    # --- State persistence ---

    async def _persist_state(self) -> None:
        if self._storage is None:
            return
        try:
            await self._storage.put(_STATE_COLLECTION, _STATE_ENTITY_ID, {
                "active": self._active,
                "current_genre": self._current_genre,
                "genre_rotation_index": self._genre_rotation_index,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            logger.warning("Failed to persist radio DJ state", exc_info=True)

    async def _restore_state(self) -> None:
        if self._storage is None:
            return
        try:
            state = await self._storage.get(_STATE_COLLECTION, _STATE_ENTITY_ID)
            if state is not None:
                self._genre_rotation_index = state.get("genre_rotation_index", 0)
                # Don't restore active/current_genre — start fresh each boot
        except Exception:
            logger.warning("Failed to restore radio DJ state", exc_info=True)

    # --- Preference management ---

    async def _get_preferences(self, user_id: str) -> dict[str, Any]:
        if self._storage is None:
            return {"user_id": user_id, "likes": [], "vetoes": []}
        doc = await self._storage.get(_PREFS_COLLECTION, f"prefs:{user_id}")
        if doc is None:
            return {"user_id": user_id, "likes": [], "vetoes": []}
        return doc

    async def _save_preferences(self, user_id: str, prefs: dict[str, Any]) -> None:
        if self._storage is None:
            return
        prefs["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._storage.put(_PREFS_COLLECTION, f"prefs:{user_id}", prefs)

    async def _add_like(self, user_id: str, genre: str) -> None:
        prefs = await self._get_preferences(user_id)
        likes: list[str] = prefs.get("likes", [])
        # Case-insensitive dedup
        if genre.lower() not in [g.lower() for g in likes]:
            likes.append(genre)
            prefs["likes"] = likes
            await self._save_preferences(user_id, prefs)

    async def _add_veto(self, user_id: str, genre: str) -> None:
        prefs = await self._get_preferences(user_id)
        vetoes: list[str] = prefs.get("vetoes", [])
        likes: list[str] = prefs.get("likes", [])
        # Add to vetoes (case-insensitive dedup)
        if genre.lower() not in [g.lower() for g in vetoes]:
            vetoes.append(genre)
        # Remove from likes if present
        prefs["likes"] = [g for g in likes if g.lower() != genre.lower()]
        prefs["vetoes"] = vetoes
        await self._save_preferences(user_id, prefs)

    # --- Genre selection ---

    async def _get_present_user_ids(self) -> set[str]:
        if self._presence_svc is None:
            return set()
        try:
            who_is_here = getattr(self._presence_svc, "who_is_here", None)
            if who_is_here is not None:
                here = await who_is_here()
                return {p.user_id for p in here}
        except Exception:
            logger.debug("Failed to get presence", exc_info=True)
        return set()

    async def select_genre(self, present_users: set[str]) -> str | None:
        """Select the best genre for the current set of present users.

        Returns None only if there are no default genres configured.
        """
        if not present_users:
            return self._next_default_genre()

        # Gather votes and vetoes from all present users
        votes: Counter[str] = Counter()
        all_vetoes: set[str] = set()

        for user_id in present_users:
            prefs = await self._get_preferences(user_id)
            for genre in prefs.get("likes", []):
                votes[genre.lower()] += 1
            for genre in prefs.get("vetoes", []):
                all_vetoes.add(genre.lower())

        # Pick highest-voted non-vetoed genre
        for genre, _count in votes.most_common():
            if genre not in all_vetoes:
                return genre

        # All voted genres are vetoed — fall back to default rotation, skipping vetoed
        return self._next_default_genre(skip=all_vetoes)

    def _next_default_genre(self, skip: set[str] | None = None) -> str | None:
        if not self._default_genres:
            return None
        skip = skip or set()
        # Try each genre in rotation order, skipping vetoed ones
        for i in range(len(self._default_genres)):
            idx = (self._genre_rotation_index + i) % len(self._default_genres)
            genre = self._default_genres[idx]
            if genre.lower() not in skip:
                self._genre_rotation_index = (idx + 1) % len(self._default_genres)
                return genre
        # All defaults are vetoed — return first default anyway
        genre = self._default_genres[self._genre_rotation_index % len(self._default_genres)]
        self._genre_rotation_index = (self._genre_rotation_index + 1) % len(self._default_genres)
        return genre

    def _can_switch_genre(self) -> bool:
        if self._last_genre_switch is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_genre_switch).total_seconds()
        return elapsed >= self._min_switch_minutes * 60

    # --- Playback ---

    async def _play_genre(self, genre: str) -> bool:
        """Search for a playlist matching the genre and start playback.

        Returns True if playback started successfully.
        """
        try:
            results: SearchResults = await self._music_svc.search(
                f"{genre} playlist", limit=1,
            )
            if not results.playlists:
                logger.warning("No playlists found for genre: %s", genre)
                return False

            playlist = results.playlists[0]
            uri = playlist.external_url or f"spotify:playlist:{playlist.playlist_id}"

            await self._speaker_svc.play_on_speakers(
                uri=uri,
                speaker_names=self._speakers or None,
                volume=self._default_volume,
                title=f"Radio DJ: {genre}",
            )

            old_genre = self._current_genre
            self._current_genre = genre
            self._last_genre_switch = datetime.now(timezone.utc)
            await self._persist_state()

            if self._event_bus and old_genre != genre:
                await self._event_bus.publish(Event(
                    event_type="radio_dj.genre.changed",
                    data={
                        "old_genre": old_genre,
                        "new_genre": genre,
                    },
                    source="radio_dj",
                ))

            logger.info("Radio DJ playing: %s (playlist: %s)", genre, playlist.name)
            return True

        except Exception:
            logger.warning("Failed to play genre: %s", genre, exc_info=True)
            return False

    async def _stop_playback(self) -> None:
        try:
            await self._speaker_svc.stop_speakers(self._speakers or None)
        except Exception:
            logger.warning("Failed to stop playback", exc_info=True)

    # --- Public control API ---

    async def start_radio(self, genre: str | None = None) -> str:
        """Start the radio, optionally with a specific genre."""
        self._active = True
        self._stopped_by_empty = False

        if genre is None:
            present = await self._get_present_user_ids()
            genre = await self.select_genre(present)

        if genre is None:
            return "No genres configured."

        ok = await self._play_genre(genre)
        if ok:
            if self._event_bus:
                await self._event_bus.publish(Event(
                    event_type="radio_dj.started",
                    data={"genre": genre},
                    source="radio_dj",
                ))
            return f"Radio started — playing {genre}"
        return f"Failed to find music for '{genre}'"

    async def stop_radio(self) -> str:
        """Stop the radio."""
        self._active = False
        self._stopped_by_empty = False
        await self._stop_playback()
        self._current_genre = None
        await self._persist_state()
        if self._event_bus:
            await self._event_bus.publish(Event(
                event_type="radio_dj.stopped",
                data={},
                source="radio_dj",
            ))
        return "Radio stopped."

    async def request_genre(self, query: str) -> str:
        """Play a specific genre/mood/artist immediately."""
        self._active = True
        self._stopped_by_empty = False
        ok = await self._play_genre(query)
        if ok:
            return f"Now playing: {query}"
        return f"Couldn't find music for '{query}'"

    async def skip_track(self) -> str:
        """Skip to the next track (re-search and play same genre)."""
        if not self._active or not self._current_genre:
            return "Radio is not playing."
        ok = await self._play_genre(self._current_genre)
        if ok:
            return f"Skipped — still playing {self._current_genre}"
        return "Failed to skip track."

    async def like_current(self, user_id: str) -> str:
        """Like the current genre for a user."""
        if not self._current_genre:
            return "Nothing is playing right now."
        await self._add_like(user_id, self._current_genre)
        if self._event_bus:
            await self._event_bus.publish(Event(
                event_type="radio_dj.track.liked",
                data={"user_id": user_id, "genre": self._current_genre},
                source="radio_dj",
            ))
        return f"Liked: {self._current_genre}"

    async def dislike_current(self, user_id: str) -> str:
        """Dislike (veto) the current genre and skip."""
        if not self._current_genre:
            return "Nothing is playing right now."
        genre = self._current_genre
        await self._add_veto(user_id, genre)
        if self._event_bus:
            await self._event_bus.publish(Event(
                event_type="radio_dj.track.vetoed",
                data={"user_id": user_id, "genre": genre},
                source="radio_dj",
            ))
        # Switch to a different genre
        present = await self._get_present_user_ids()
        new_genre = await self.select_genre(present)
        if new_genre and new_genre.lower() != genre.lower():
            await self._play_genre(new_genre)
            return f"Vetoed {genre} — now playing {new_genre}"
        return f"Vetoed {genre}"

    async def veto_genre(self, user_id: str, genre: str) -> str:
        """Ban a genre for a user."""
        await self._add_veto(user_id, genre)
        # If currently playing the vetoed genre, switch
        if self._active and self._current_genre and self._current_genre.lower() == genre.lower():
            present = await self._get_present_user_ids()
            new_genre = await self.select_genre(present)
            if new_genre and new_genre.lower() != genre.lower():
                await self._play_genre(new_genre)
                return f"Vetoed {genre} — switched to {new_genre}"
        return f"Vetoed {genre}"

    async def get_status(self) -> dict[str, Any]:
        """Get the current DJ status."""
        present = await self._get_present_user_ids()
        return {
            "active": self._active,
            "current_genre": self._current_genre,
            "present_users": sorted(present),
            "default_genres": self._default_genres,
            "volume": self._default_volume,
            "speakers": self._speakers,
            "min_switch_interval_minutes": self._min_switch_minutes,
        }

    # --- Polling and event handling ---

    async def _poll(self) -> None:
        """Periodic poll: check presence, rotate genre if needed."""
        if not self._active:
            return

        present = await self._get_present_user_ids()

        # Stop if empty
        if not present and self._stop_when_empty:
            if not self._stopped_by_empty:
                logger.info("Radio DJ: no one present, stopping playback")
                await self._stop_playback()
                self._stopped_by_empty = True
            return

        # Resume if people arrived and we stopped due to empty
        if present and self._stopped_by_empty:
            self._stopped_by_empty = False
            genre = await self.select_genre(present)
            if genre:
                await self._play_genre(genre)
            self._present_users = present
            return

        # Check if presence changed and we can switch
        if present != self._present_users and self._can_switch_genre():
            genre = await self.select_genre(present)
            if genre and genre.lower() != (self._current_genre or "").lower():
                await self._play_genre(genre)

        self._present_users = present

    async def _on_presence_arrived(self, event: Event) -> None:
        """Handle a presence.arrived event — recalculate genre immediately."""
        if not self._active:
            return

        user_id = event.data.get("user_id", "")
        if user_id:
            self._present_users.add(user_id)

        # Resume if we stopped due to empty shop
        if self._stopped_by_empty:
            self._stopped_by_empty = False
            genre = await self.select_genre(self._present_users)
            if genre:
                await self._play_genre(genre)
            return

        # Bypass throttle on arrival — recalculate genre
        genre = await self.select_genre(self._present_users)
        if genre and genre.lower() != (self._current_genre or "").lower():
            await self._play_genre(genre)

    async def _on_presence_departed(self, event: Event) -> None:
        """Handle a presence.departed event — stop if empty."""
        if not self._active:
            return

        user_id = event.data.get("user_id", "")
        self._present_users.discard(user_id)

        present = await self._get_present_user_ids()
        self._present_users = present

        if not present and self._stop_when_empty:
            logger.info("Radio DJ: last person left, stopping playback")
            await self._stop_playback()
            self._stopped_by_empty = True
        elif self._can_switch_genre():
            genre = await self.select_genre(present)
            if genre and genre.lower() != (self._current_genre or "").lower():
                await self._play_genre(genre)

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "radio_dj"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="radio_start",
                description="Start the radio DJ. Optionally specify a genre to begin with.",
                parameters=[
                    ToolParameter(
                        name="genre",
                        type=ToolParameterType.STRING,
                        description="Genre or mood to start with (optional).",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radio_stop",
                description="Stop the radio DJ.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_request",
                description="Request a specific genre, mood, or artist to play now.",
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Genre, mood, or artist to play.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radio_skip",
                description="Skip the current track.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_like",
                description="Like the current genre/song. Records the genre as a preference for the user.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_dislike",
                description="Dislike the current genre/song. Vetoes the genre and switches to something else.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_veto",
                description="Ban a genre so it won't be played for this user.",
                parameters=[
                    ToolParameter(
                        name="genre",
                        type=ToolParameterType.STRING,
                        description="The genre to ban.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radio_status",
                description="Get the current radio DJ status: what's playing, who's here, preferences.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_set_preferences",
                description="Set a user's music preferences directly.",
                parameters=[
                    ToolParameter(
                        name="user_id",
                        type=ToolParameterType.STRING,
                        description="The user ID to set preferences for.",
                    ),
                    ToolParameter(
                        name="likes",
                        type=ToolParameterType.ARRAY,
                        description="Genres the user likes.",
                        required=False,
                    ),
                    ToolParameter(
                        name="vetoes",
                        type=ToolParameterType.ARRAY,
                        description="Genres the user vetoes.",
                        required=False,
                    ),
                ],
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "radio_start":
                return await self._tool_start(arguments)
            case "radio_stop":
                return await self.stop_radio()
            case "radio_request":
                return await self.request_genre(arguments["query"])
            case "radio_skip":
                return await self.skip_track()
            case "radio_like":
                return await self._tool_like(arguments)
            case "radio_dislike":
                return await self._tool_dislike(arguments)
            case "radio_veto":
                return await self._tool_veto(arguments)
            case "radio_status":
                return await self._tool_status()
            case "radio_set_preferences":
                return await self._tool_set_preferences(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_start(self, arguments: dict[str, Any]) -> str:
        genre = arguments.get("genre")
        return await self.start_radio(genre=genre)

    async def _tool_like(self, arguments: dict[str, Any]) -> str:
        from gilbert.core.context import get_current_user

        ctx = get_current_user()
        user_id = ctx.user_id if ctx else "unknown"
        return await self.like_current(user_id)

    async def _tool_dislike(self, arguments: dict[str, Any]) -> str:
        from gilbert.core.context import get_current_user

        ctx = get_current_user()
        user_id = ctx.user_id if ctx else "unknown"
        return await self.dislike_current(user_id)

    async def _tool_veto(self, arguments: dict[str, Any]) -> str:
        from gilbert.core.context import get_current_user

        ctx = get_current_user()
        user_id = ctx.user_id if ctx else "unknown"
        return await self.veto_genre(user_id, arguments["genre"])

    async def _tool_status(self) -> str:
        status = await self.get_status()
        return json.dumps(status)

    async def _tool_set_preferences(self, arguments: dict[str, Any]) -> str:
        user_id = arguments["user_id"]
        likes = arguments.get("likes")
        vetoes = arguments.get("vetoes")
        prefs = await self._get_preferences(user_id)
        if likes is not None:
            prefs["likes"] = likes
        if vetoes is not None:
            prefs["vetoes"] = vetoes
        await self._save_preferences(user_id, prefs)
        return json.dumps({"status": "ok", "user_id": user_id, "likes": prefs["likes"], "vetoes": prefs["vetoes"]})
