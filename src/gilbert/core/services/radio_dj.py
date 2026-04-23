"""Radio DJ service — context-aware music selection based on presence and user preferences.

Automatically selects music genres based on who is present, learns individual
preferences (likes/vetoes) over time, and rotates through default genres when
no preferences are known.
"""

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.music import (
    MusicItem,
    MusicItemKind,
    MusicSearchUnavailableError,
    Playable,
)
from gilbert.interfaces.presence import PresenceProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import NowPlaying, PlaybackState
from gilbert.interfaces.storage import StorageProvider
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
        self._enabled: bool = False
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
        # True when the DJ believes the current playback was started by
        # itself. Flipped to False by the ``music.playback_started``
        # event handler when a user-initiated play/queue action fires.
        # Auto-switch paths consult ``_should_auto_switch`` which checks
        # this AND the current playback state — so the DJ defers while
        # the user's chosen music is playing, and reclaims when the
        # speaker goes quiet.
        self._dj_owns_playback: bool = True

        # Unsub callables for event bus
        self._unsubs: list[Any] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="radio_dj",
            capabilities=frozenset({"radio_dj", "ai_tools"}),
            requires=frozenset({"music", "speaker_control", "scheduler"}),
            optional=frozenset({"presence", "entity_storage", "event_bus", "configuration"}),
            events=frozenset(
                {
                    "radio_dj.genre.changed",
                    "radio_dj.started",
                    "radio_dj.stopped",
                    "radio_dj.track.liked",
                    "radio_dj.track.vetoed",
                }
            ),
            toggleable=True,
            toggle_description="AI radio DJ announcements",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        # Check enabled
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)
            if not section.get("enabled", False):
                logger.info("Radio DJ service disabled")
                return

        self._enabled = True

        from gilbert.interfaces.scheduler import Schedule

        # Required dependencies
        self._music_svc = resolver.require_capability("music")
        self._speaker_svc = resolver.require_capability("speaker_control")
        self._scheduler_svc = resolver.require_capability("scheduler")

        # Optional dependencies
        self._presence_svc = resolver.get_capability("presence")

        # Storage
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None and isinstance(storage_svc, StorageProvider):
            from gilbert.interfaces.storage import NamespacedStorageBackend

            self._storage = NamespacedStorageBackend(storage_svc.backend, "radio_dj")

        # Event bus
        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None and isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus
            self._unsubs.append(
                self._event_bus.subscribe("presence.arrived", self._on_presence_arrived)
            )
            self._unsubs.append(
                self._event_bus.subscribe("presence.departed", self._on_presence_departed)
            )
            # Learn when a user plays music outside the DJ (via /music
            # play, the AI's play_music tool, a Play button on a search
            # result, etc.) so we can stop clobbering their selection
            # on the next presence tick.
            self._unsubs.append(
                self._event_bus.subscribe(
                    "music.playback_started", self._on_music_playback_started
                )
            )

        # Configuration
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section("radio_dj")
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

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="default_genres",
                type=ToolParameterType.ARRAY,
                description="Genre rotation for cold start.",
                default=_DEFAULT_GENRES,
            ),
            ConfigParam(
                key="min_switch_interval",
                type=ToolParameterType.INTEGER,
                description="Minimum minutes between auto genre switches.",
                default=_DEFAULT_MIN_SWITCH_MINUTES,
            ),
            ConfigParam(
                key="default_volume",
                type=ToolParameterType.INTEGER,
                description="Playback volume (0-100).",
                default=_DEFAULT_VOLUME,
            ),
            ConfigParam(
                key="speakers",
                type=ToolParameterType.ARRAY,
                description="Speaker names (empty = all).",
                default=[],
                choices_from="speakers",
            ),
            ConfigParam(
                key="stop_when_empty",
                type=ToolParameterType.BOOLEAN,
                description="Stop playback when nobody is present.",
                default=True,
            ),
            ConfigParam(
                key="poll_interval",
                type=ToolParameterType.INTEGER,
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
            await self._storage.put(
                _STATE_COLLECTION,
                _STATE_ENTITY_ID,
                {
                    "active": self._active,
                    "current_genre": self._current_genre,
                    "genre_rotation_index": self._genre_rotation_index,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
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
        return dict(doc)

    async def _save_preferences(self, user_id: str, prefs: dict[str, Any]) -> None:
        if self._storage is None:
            return
        prefs["updated_at"] = datetime.now(UTC).isoformat()
        await self._storage.put(_PREFS_COLLECTION, f"prefs:{user_id}", prefs)

    async def _add_like(self, user_id: str, genre: str) -> None:
        prefs = await self._get_preferences(user_id)
        likes: list[str] = prefs.get("likes", [])
        # Case-insensitive dedup
        if genre.lower() not in [g.lower() for g in likes]:
            likes.append(genre)
            prefs["likes"] = likes
            await self._save_preferences(user_id, prefs)

    async def _add_liked_track(self, user_id: str, now: NowPlaying) -> None:
        """Record a specific track as liked by a user (dedup by title+artist)."""
        if not now.title:
            return
        prefs = await self._get_preferences(user_id)
        liked: list[dict[str, Any]] = prefs.get("liked_tracks", [])
        key = (now.title.lower(), now.artist.lower())
        if any((t.get("title", "").lower(), t.get("artist", "").lower()) == key for t in liked):
            return
        liked.append(
            {
                "title": now.title,
                "artist": now.artist,
                "album": now.album,
                "uri": now.uri,
            }
        )
        prefs["liked_tracks"] = liked
        await self._save_preferences(user_id, prefs)

    async def _add_vetoed_track(self, user_id: str, now: NowPlaying) -> None:
        """Record a specific track as vetoed by a user (dedup by title+artist)."""
        if not now.title:
            return
        prefs = await self._get_preferences(user_id)
        vetoed: list[dict[str, Any]] = prefs.get("vetoed_tracks", [])
        liked: list[dict[str, Any]] = prefs.get("liked_tracks", [])
        key = (now.title.lower(), now.artist.lower())

        def _track_key(t: dict[str, Any]) -> tuple[str, str]:
            return (t.get("title", "").lower(), t.get("artist", "").lower())

        if not any(_track_key(t) == key for t in vetoed):
            vetoed.append(
                {
                    "title": now.title,
                    "artist": now.artist,
                    "album": now.album,
                    "uri": now.uri,
                }
            )
        # Remove from liked if present
        prefs["liked_tracks"] = [t for t in liked if _track_key(t) != key]
        prefs["vetoed_tracks"] = vetoed
        await self._save_preferences(user_id, prefs)

    async def _get_now_playing(self) -> NowPlaying | None:
        """Best-effort query of the current track from the music service.

        Returns None if the music service can't report it (e.g. the speaker
        backend doesn't support track introspection, or the call errors).
        """
        if self._music_svc is None:
            return None
        try:
            # _music_svc is Any (duck-typed); MusicService.now_playing is the source of truth.
            return cast(NowPlaying, await self._music_svc.now_playing())
        except Exception:
            logger.debug("Failed to query music.now_playing", exc_info=True)
            return None

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
            if isinstance(self._presence_svc, PresenceProvider):
                here = await self._presence_svc.who_is_here()
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
        elapsed = (datetime.now(UTC) - self._last_genre_switch).total_seconds()
        return elapsed >= self._min_switch_minutes * 60

    async def _should_auto_switch(self) -> bool:
        """Ownership guard: should the DJ be allowed to change the music now?

        Returns ``False`` when a user has taken manual control (via
        ``/music play``, the play_music tool, a button click, etc.) AND
        their music is still playing — the DJ would rudely clobber it.

        When the DJ doesn't currently own playback we query
        ``now_playing``: if the speaker's gone quiet we reclaim
        ownership and allow the switch, because "silent speakers + users
        present" is exactly what the DJ exists to fill.
        """
        if self._dj_owns_playback:
            return True
        now = await self._get_now_playing()
        if now is None or now.state != PlaybackState.PLAYING:
            # User's music has stopped — reclaim the floor.
            self._dj_owns_playback = True
            return True
        # Someone else's music is still playing — back off.
        logger.debug(
            "Radio DJ deferring auto-switch: user-initiated playback "
            "is still PLAYING (uri=%s title=%r)",
            now.uri,
            now.title,
        )
        return False

    async def _on_music_playback_started(self, event: Event) -> None:
        """Flip ownership off whenever a non-DJ play action reaches the speaker.

        The DJ remains ``_active`` — it just defers to the user. Next
        presence tick or arrival where ``_should_auto_switch`` returns
        True (i.e. user's music stopped) will let the DJ resume."""
        initiator = str(event.data.get("initiator") or "")
        if initiator == "dj":
            return
        if self._dj_owns_playback:
            logger.info(
                "Radio DJ yielding playback to %s-initiated play (uri=%s title=%r)",
                initiator or "external",
                event.data.get("uri"),
                event.data.get("title"),
            )
        self._dj_owns_playback = False

    # --- Playback ---

    async def _play_genre(self, genre: str) -> bool:
        """Search for a playlist matching the genre and start playback.

        Uses the music service's generic search → resolve → play flow, so
        it works with any ``MusicBackend`` that implements the interface
        (currently ``SonosMusic``). Returns True if playback started
        successfully.
        """
        try:
            try:
                results: list[MusicItem] = await self._music_svc.search(
                    genre,
                    kind=MusicItemKind.PLAYLIST,
                    limit=1,
                )
            except MusicSearchUnavailableError as exc:
                logger.warning("Radio DJ: %s", exc)
                return False

            if not results:
                logger.warning("No playlists found for genre: %s", genre)
                return False

            playlist = results[0]

            try:
                playable: Playable = await self._music_svc.play_item(
                    playlist,
                    speaker_names=self._speakers or None,
                    volume=self._default_volume,
                    initiator="dj",
                )
            except (RuntimeError, MusicSearchUnavailableError) as exc:
                logger.warning("Radio DJ playback failed for %s: %s", genre, exc)
                return False

            old_genre = self._current_genre
            self._current_genre = genre
            self._last_genre_switch = datetime.now(UTC)
            # We just played our own track — take ownership back. The
            # event handler will NOT flip this to False for our own
            # emission because the event carries ``initiator="dj"``.
            self._dj_owns_playback = True
            await self._persist_state()

            if self._event_bus and old_genre != genre:
                await self._event_bus.publish(
                    Event(
                        event_type="radio_dj.genre.changed",
                        data={
                            "old_genre": old_genre,
                            "new_genre": genre,
                        },
                        source="radio_dj",
                    )
                )

            logger.info(
                "Radio DJ playing: %s (playlist: %s, uri: %s)",
                genre,
                playlist.title,
                playable.uri,
            )
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
                await self._event_bus.publish(
                    Event(
                        event_type="radio_dj.started",
                        data={"genre": genre},
                        source="radio_dj",
                    )
                )
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
            await self._event_bus.publish(
                Event(
                    event_type="radio_dj.stopped",
                    data={},
                    source="radio_dj",
                )
            )
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
        """Like whatever's playing right now, wherever it came from.

        There are four state combinations to handle cleanly:

        1. DJ running + speaker reports a track → record track like +
           genre like, return "Liked: Song — Artist (genre)".
        2. DJ running + speaker can't report a track (backend doesn't
           support now_playing, or returned None) → record genre like
           only, return "Liked: {genre}". Preserves the pre-fix
           behavior so users whose speaker backend is track-blind
           still get genre preferences logged.
        3. DJ NOT running + speaker reports a track → record track
           like only (no genre to credit), return
           "Liked: Song — Artist". This is the case the user reported
           as broken — tracks playing via Spotify / AirPlay /
           Sonos Radio / any non-DJ source were previously rejected
           outright.
        4. Nothing at all — no track and no DJ → return
           "Nothing is playing right now."

        The key shift from the old implementation is that we consult
        the speaker BEFORE bailing on an empty ``_current_genre``.
        """
        now = await self._get_now_playing()
        genre = self._current_genre
        have_track = now is not None and bool(now.title)

        # Case 4: truly nothing to like.
        if not have_track and not genre:
            return "Nothing is playing right now."

        # Case 1 / 3: record the specific track when the speaker has it.
        if have_track:
            assert now is not None  # narrow for mypy
            await self._add_liked_track(user_id, now)

        # Case 1 / 2: record the genre when the DJ is running.
        if genre:
            await self._add_like(user_id, genre)

        if self._event_bus:
            await self._event_bus.publish(
                Event(
                    event_type="radio_dj.track.liked",
                    data={
                        "user_id": user_id,
                        "genre": genre or "",
                        "title": now.title if have_track and now else "",
                        "artist": now.artist if have_track and now else "",
                    },
                    source="radio_dj",
                )
            )

        if have_track and genre:
            assert now is not None
            return f"Liked: {now.title} — {now.artist} ({genre})"
        if have_track:
            assert now is not None
            return f"Liked: {now.title} — {now.artist}"
        # genre-only path (case 2)
        return f"Liked: {genre}"

    async def dislike_current(self, user_id: str) -> str:
        """Dislike whatever's playing: veto the track, and if the DJ is
        running, veto its genre and switch to something else.

        Same four-state matrix as ``like_current`` with one caveat:
        when the DJ isn't running we can record the veto but we can't
        force-skip audio from an external source. The return message
        spells that out so the user isn't confused.
        """
        now = await self._get_now_playing()
        genre = self._current_genre
        have_track = now is not None and bool(now.title)

        if not have_track and not genre:
            return "Nothing is playing right now."

        if have_track:
            assert now is not None
            await self._add_vetoed_track(user_id, now)

        if genre:
            await self._add_veto(user_id, genre)

        if self._event_bus:
            await self._event_bus.publish(
                Event(
                    event_type="radio_dj.track.vetoed",
                    data={
                        "user_id": user_id,
                        "genre": genre or "",
                        "title": now.title if have_track and now else "",
                        "artist": now.artist if have_track and now else "",
                    },
                    source="radio_dj",
                )
            )

        if not genre:
            # DJ isn't driving playback — we can remember the veto but
            # we can't skip someone else's audio source.
            assert have_track and now is not None  # implied by the guard above
            return (
                f"Vetoed {now.title} — {now.artist}. I won't queue it "
                "again. (The track isn't from the radio DJ, so I can't "
                "skip it directly — change it on the speaker yourself.)"
            )

        # DJ is running — switch to a different genre.
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
        """Get the current DJ status, including the track currently playing if known."""
        present = await self._get_present_user_ids()
        status: dict[str, Any] = {
            "active": self._active,
            "current_genre": self._current_genre,
            "present_users": sorted(present),
            "default_genres": self._default_genres,
            "volume": self._default_volume,
            "speakers": self._speakers,
            "min_switch_interval_minutes": self._min_switch_minutes,
        }
        now = await self._get_now_playing()
        if now is not None:
            status["now_playing"] = {
                "state": now.state.value,
                "is_playing": now.state == PlaybackState.PLAYING,
                "title": now.title,
                "artist": now.artist,
                "album": now.album,
                "uri": now.uri,
                "duration_seconds": now.duration_seconds,
                "position_seconds": now.position_seconds,
            }
        return status

    # --- Polling and event handling ---

    async def _poll(self) -> None:
        """Periodic poll: check presence, rotate genre if needed."""
        if not self._active:
            return

        present = await self._get_present_user_ids()

        # Stop if empty. Only "our" playback is safe to stop — cutting
        # the user's own music when the room empties is the exact
        # behavior that made the DJ feel invasive.
        if not present and self._stop_when_empty:
            if not self._stopped_by_empty and self._dj_owns_playback:
                logger.info("Radio DJ: no one present, stopping playback")
                await self._stop_playback()
                self._stopped_by_empty = True
            return

        # Resume if people arrived and we stopped due to empty
        if present and self._stopped_by_empty:
            if not await self._should_auto_switch():
                return
            self._stopped_by_empty = False
            genre = await self.select_genre(present)
            if genre:
                await self._play_genre(genre)
            self._present_users = present
            return

        # Check if presence changed and we can switch
        if present != self._present_users and self._can_switch_genre():
            if await self._should_auto_switch():
                genre = await self.select_genre(present)
                if genre and genre.lower() != (self._current_genre or "").lower():
                    await self._play_genre(genre)

        self._present_users = present

    async def _on_presence_arrived(self, event: Event) -> None:
        """Handle a presence.arrived event.

        Arrivals used to bypass ``min_switch_interval`` so every new
        person walking in would trigger an instant genre change.
        That felt like the DJ was "playing stuff whenever it wants";
        arrivals now honor the same throttle as the poll loop so
        repeated comings and goings can't thrash playback.
        """
        if not self._active:
            return

        user_id = event.data.get("user_id", "")
        if user_id:
            self._present_users.add(user_id)

        # Resume if we stopped due to empty shop — but only if we still
        # own the floor (user hasn't started their own music since).
        if self._stopped_by_empty:
            if not await self._should_auto_switch():
                return
            self._stopped_by_empty = False
            genre = await self.select_genre(self._present_users)
            if genre:
                await self._play_genre(genre)
            return

        if not self._can_switch_genre():
            return
        if not await self._should_auto_switch():
            return

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
            # Same "only stop our own playback" rule as ``_poll`` — the
            # user's externally-chosen music keeps playing regardless.
            if self._dj_owns_playback:
                logger.info("Radio DJ: last person left, stopping playback")
                await self._stop_playback()
                self._stopped_by_empty = True
        elif self._can_switch_genre() and await self._should_auto_switch():
            genre = await self.select_genre(present)
            if genre and genre.lower() != (self._current_genre or "").lower():
                await self._play_genre(genre)

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "radio_dj"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="radio_start",
                slash_group="radio",
                slash_command="start",
                slash_help="Start the radio DJ: /radio start [genre]",
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
                slash_group="radio",
                slash_command="stop",
                slash_help="Stop the radio DJ: /radio stop",
                description="Stop the radio DJ.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_request",
                slash_group="radio",
                slash_command="request",
                slash_help="Request music: /radio request <genre|mood|artist>",
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
                slash_group="radio",
                slash_command="skip",
                slash_help="Skip the current track: /radio skip",
                description="Skip the current track.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_like",
                slash_group="radio",
                slash_command="like",
                slash_help="Like what's playing: /radio like",
                description=(
                    "Record that the user likes what's playing right now. "
                    "Works for ANY track on the speaker regardless of "
                    "source — radio DJ, Spotify, AirPlay, Sonos Radio, "
                    "etc. This is the tool to call for generic 'I like "
                    "this song' / 'this is a great track' / 'save this' "
                    "messages. It queries the speaker's now-playing, "
                    "saves the track to the user's liked_tracks list, "
                    "and (if the radio DJ is also running) credits the "
                    "current genre."
                ),
                required_role="user",
            ),
            ToolDefinition(
                name="radio_dislike",
                slash_group="radio",
                slash_command="dislike",
                slash_help="Dislike what's playing and switch: /radio dislike",
                description=(
                    "Record that the user dislikes what's playing right "
                    "now. Works for ANY track on the speaker regardless "
                    "of source. Always records the track veto; when the "
                    "radio DJ is actively running it ALSO vetoes the "
                    "genre and switches to something else. When playback "
                    "is from an external source (Spotify, AirPlay, …) "
                    "we can only remember the veto — we can't force-skip "
                    "audio we don't own."
                ),
                required_role="user",
            ),
            ToolDefinition(
                name="radio_veto",
                slash_group="radio",
                slash_command="veto",
                slash_help="Ban a genre: /radio veto <genre>",
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
                slash_group="radio",
                slash_command="status",
                slash_help="Current radio state: /radio status",
                description="Get the current radio DJ status: what's playing, who's here, preferences.",
                required_role="user",
            ),
            ToolDefinition(
                name="radio_set_preferences",
                slash_group="radio",
                slash_command="prefs",
                slash_help="Set a user's music prefs: /radio prefs <user_id> [likes=a,b] [vetoes=c,d]",
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
        return json.dumps(
            {"status": "ok", "user_id": user_id, "likes": prefs["likes"], "vetoes": prefs["vetoes"]}
        )
