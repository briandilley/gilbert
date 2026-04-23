"""Tests for RadioDJService — context-aware music DJ."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.core.services.radio_dj import RadioDJService
from gilbert.interfaces.events import Event
from gilbert.interfaces.music import MusicItem, MusicItemKind, Playable

# --- Fakes ---


class FakeStorage:
    """In-memory storage backend for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data

    async def query(self, query: Any) -> list[dict[str, Any]]:
        col = self._data.get(query.collection, {})
        return list(col.values())

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def exists(self, collection: str, key: str) -> bool:
        return key in self._data.get(collection, {})

    async def count(self, query: Any) -> int:
        return len(self._data.get(query.collection, {}))

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: Any) -> None:
        pass

    async def list_indexes(self, collection: str) -> list[Any]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


class FakeStorageService:
    def __init__(self) -> None:
        self._backend = FakeStorage()

    @property
    def backend(self) -> FakeStorage:
        return self._backend

    @property
    def raw_backend(self) -> FakeStorage:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        from gilbert.interfaces.storage import NamespacedStorageBackend

        return NamespacedStorageBackend(self._backend, namespace)

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeEventBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.published: list[Event] = []

    def subscribe(self, event_type: str, handler: Any) -> Any:
        self.handlers.setdefault(event_type, []).append(handler)
        return lambda: self.handlers[event_type].remove(handler)

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        self.handlers.setdefault(pattern, []).append(handler)
        return lambda: self.handlers[pattern].remove(handler)

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class FakeEventBusSvc:
    def __init__(self) -> None:
        self.bus = FakeEventBus()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="event_bus", capabilities=frozenset({"event_bus"}))


class FakePresenceService:
    def __init__(self, users: list[str] | None = None) -> None:
        self._users = users or []

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="presence", capabilities=frozenset({"presence"}))

    async def who_is_here(self) -> list[Any]:
        from gilbert.interfaces.presence import PresenceState, UserPresence

        return [UserPresence(user_id=uid, state=PresenceState.PRESENT) for uid in self._users]


class FakeMusicService:
    """Fake music service mimicking the new MusicService shape.

    Exposes the same surface the radio DJ calls: ``search`` returns a
    list of ``MusicItem`` (new interface), ``play_item`` delegates to the
    speaker service (matching the real service's behavior), and
    ``now_playing`` returns whatever the test has set.
    """

    def __init__(self, speaker_svc: Any = None) -> None:
        self.last_search: str | None = None
        self.last_kind: MusicItemKind | None = None
        self.speaker_svc = speaker_svc
        # Test hook: what now_playing() should return. None means "stopped".
        self.current_now_playing: Any = None
        # Test hook: set to True to simulate empty search results.
        self.empty_results: bool = False
        # Recorded on each play_item call — lets tests verify the DJ
        # passes initiator="dj" rather than defaulting to "user".
        self.last_play_initiator: str = ""

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="music", capabilities=frozenset({"music"}))

    async def search(
        self,
        query: str,
        *,
        kind: MusicItemKind = MusicItemKind.TRACK,
        limit: int = 10,
    ) -> list[MusicItem]:
        self.last_search = query
        self.last_kind = kind
        if self.empty_results:
            return []
        return [
            MusicItem(
                id="pl_123",
                title=f"Best of {query}",
                kind=kind,
                uri="x-sonos-spotify:spotify%3aplaylist%3apl_123",
                service="Spotify",
            ),
        ]

    async def play_item(
        self,
        item: MusicItem,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        initiator: str = "user",
    ) -> Playable:
        # Mirror the real MusicService.play_item: resolve + delegate to speakers.
        self.last_play_initiator = initiator
        if self.speaker_svc is not None:
            await self.speaker_svc.play_on_speakers(
                uri=item.uri,
                speaker_names=speaker_names,
                volume=volume,
                title=item.title,
                didl_meta=item.didl_meta,
            )
        return Playable(uri=item.uri, didl_meta=item.didl_meta, title=item.title)

    async def now_playing(self, speaker_name: str | None = None) -> Any:
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        if self.current_now_playing is None:
            return NowPlaying(state=PlaybackState.STOPPED)
        return self.current_now_playing


class FakeSpeakerService:
    def __init__(self) -> None:
        self.play_on_speakers = AsyncMock()
        self.stop_speakers = AsyncMock()
        self.announce = AsyncMock()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="speaker", capabilities=frozenset({"speaker_control"}))


class FakeSchedulerService:
    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="scheduler", capabilities=frozenset({"scheduler"}))

    def add_job(
        self, name: str, schedule: Any, callback: Any, system: bool = False, **kwargs: Any
    ) -> Any:
        self.jobs[name] = {"schedule": schedule, "callback": callback, "system": system}
        return MagicMock()

    def remove_job(self, name: str, **kwargs: Any) -> None:
        self.jobs.pop(name, None)


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


# --- Fixtures ---


@pytest.fixture
def speaker_svc() -> FakeSpeakerService:
    return FakeSpeakerService()


@pytest.fixture
def music_svc(speaker_svc: FakeSpeakerService) -> FakeMusicService:
    return FakeMusicService(speaker_svc=speaker_svc)


@pytest.fixture
def scheduler_svc() -> FakeSchedulerService:
    return FakeSchedulerService()


@pytest.fixture
def storage_svc() -> FakeStorageService:
    return FakeStorageService()


@pytest.fixture
def event_bus_svc() -> FakeEventBusSvc:
    return FakeEventBusSvc()


@pytest.fixture
def presence_svc() -> FakePresenceService:
    return FakePresenceService(users=["alice", "bob"])


@pytest.fixture
def resolver(
    music_svc: FakeMusicService,
    speaker_svc: FakeSpeakerService,
    scheduler_svc: FakeSchedulerService,
    storage_svc: FakeStorageService,
    event_bus_svc: FakeEventBusSvc,
    presence_svc: FakePresenceService,
) -> FakeResolver:
    r = FakeResolver()
    r.caps["music"] = music_svc
    r.caps["speaker_control"] = speaker_svc
    r.caps["scheduler"] = scheduler_svc
    r.caps["entity_storage"] = storage_svc
    r.caps["event_bus"] = event_bus_svc
    r.caps["presence"] = presence_svc
    return r


@pytest.fixture
def dj() -> RadioDJService:
    return RadioDJService()


@pytest.fixture
async def started_dj(
    dj: RadioDJService,
    resolver: FakeResolver,
) -> RadioDJService:
    await dj.start(resolver)
    return dj


# --- Service lifecycle ---


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_registers_poll_job(
        self, started_dj: RadioDJService, scheduler_svc: FakeSchedulerService
    ) -> None:
        assert "radio-dj-poll" in scheduler_svc.jobs
        assert scheduler_svc.jobs["radio-dj-poll"]["system"] is True

    @pytest.mark.asyncio
    async def test_start_subscribes_to_presence_events(
        self, started_dj: RadioDJService, event_bus_svc: FakeEventBusSvc
    ) -> None:
        assert "presence.arrived" in event_bus_svc.bus.handlers
        assert "presence.departed" in event_bus_svc.bus.handlers

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(
        self, started_dj: RadioDJService, event_bus_svc: FakeEventBusSvc
    ) -> None:
        await started_dj.stop()
        assert len(event_bus_svc.bus.handlers.get("presence.arrived", [])) == 0
        assert len(event_bus_svc.bus.handlers.get("presence.departed", [])) == 0

    @pytest.mark.asyncio
    async def test_service_info(self, dj: RadioDJService) -> None:
        info = dj.service_info()
        assert info.name == "radio_dj"
        assert "radio_dj" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "music" in info.requires
        assert "speaker_control" in info.requires
        assert "scheduler" in info.requires


# --- Genre selection ---


class TestGenreSelection:
    @pytest.mark.asyncio
    async def test_cold_start_rotates_defaults(self, started_dj: RadioDJService) -> None:
        """With no users, genres rotate through defaults."""
        genre1 = await started_dj.select_genre(set())
        genre2 = await started_dj.select_genre(set())
        # Should get different genres on consecutive calls
        assert genre1 is not None
        assert genre2 is not None
        assert genre1 != genre2

    @pytest.mark.asyncio
    async def test_votes_select_most_popular(
        self, started_dj: RadioDJService, storage_svc: FakeStorageService
    ) -> None:
        """Genre with most votes wins."""
        # Alice likes rock and jazz, Bob likes rock
        await started_dj._save_preferences(
            "alice",
            {
                "user_id": "alice",
                "likes": ["rock", "jazz"],
                "vetoes": [],
            },
        )
        await started_dj._save_preferences(
            "bob",
            {
                "user_id": "bob",
                "likes": ["rock"],
                "vetoes": [],
            },
        )
        genre = await started_dj.select_genre({"alice", "bob"})
        assert genre == "rock"

    @pytest.mark.asyncio
    async def test_vetoed_genre_excluded(
        self,
        started_dj: RadioDJService,
    ) -> None:
        """Vetoed genres are excluded from selection."""
        await started_dj._save_preferences(
            "alice",
            {
                "user_id": "alice",
                "likes": ["rock", "jazz"],
                "vetoes": [],
            },
        )
        await started_dj._save_preferences(
            "bob",
            {
                "user_id": "bob",
                "likes": ["rock"],
                "vetoes": ["rock"],
            },
        )
        genre = await started_dj.select_genre({"alice", "bob"})
        # Rock is vetoed by Bob, so jazz should win
        assert genre == "jazz"

    @pytest.mark.asyncio
    async def test_all_voted_genres_vetoed_falls_back(
        self,
        started_dj: RadioDJService,
    ) -> None:
        """When all voted genres are vetoed, fall back to default rotation."""
        await started_dj._save_preferences(
            "alice",
            {
                "user_id": "alice",
                "likes": ["rock"],
                "vetoes": [],
            },
        )
        await started_dj._save_preferences(
            "bob",
            {
                "user_id": "bob",
                "likes": [],
                "vetoes": ["rock"],
            },
        )
        genre = await started_dj.select_genre({"alice", "bob"})
        # Should get a default genre (not rock since it's vetoed)
        assert genre is not None
        assert genre.lower() != "rock"

    @pytest.mark.asyncio
    async def test_no_preferences_uses_defaults(
        self,
        started_dj: RadioDJService,
    ) -> None:
        """Users with no preferences fall back to default rotation."""
        genre = await started_dj.select_genre({"alice"})
        assert (
            genre in [g.lower() for g in started_dj._default_genres]
            or genre in started_dj._default_genres
        )


# --- Throttle logic ---


class TestThrottle:
    def test_can_switch_when_never_switched(self, dj: RadioDJService) -> None:
        assert dj._can_switch_genre() is True

    def test_cannot_switch_too_soon(self, dj: RadioDJService) -> None:
        dj._last_genre_switch = datetime.now(UTC)
        assert dj._can_switch_genre() is False

    def test_can_switch_after_interval(self, dj: RadioDJService) -> None:
        dj._last_genre_switch = datetime.now(UTC) - timedelta(minutes=20)
        assert dj._can_switch_genre() is True


# --- Start/stop ---


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_radio(
        self, started_dj: RadioDJService, speaker_svc: FakeSpeakerService
    ) -> None:
        result = await started_dj.start_radio()
        assert started_dj._active is True
        assert "started" in result.lower() or "playing" in result.lower()
        speaker_svc.play_on_speakers.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_radio_with_genre(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        result = await started_dj.start_radio(genre="jazz")
        assert "jazz" in result.lower()
        assert music_svc.last_search is not None
        assert "jazz" in music_svc.last_search

    @pytest.mark.asyncio
    async def test_stop_radio(
        self, started_dj: RadioDJService, speaker_svc: FakeSpeakerService
    ) -> None:
        await started_dj.start_radio()
        result = await started_dj.stop_radio()
        assert started_dj._active is False
        assert "stopped" in result.lower()
        speaker_svc.stop_speakers.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_emits_event(
        self, started_dj: RadioDJService, event_bus_svc: FakeEventBusSvc
    ) -> None:
        await started_dj.start_radio()
        event_bus_svc.bus.published.clear()
        await started_dj.stop_radio()
        types = [e.event_type for e in event_bus_svc.bus.published]
        assert "radio_dj.stopped" in types


# --- Preferences ---


class TestPreferences:
    @pytest.mark.asyncio
    async def test_add_like(self, started_dj: RadioDJService) -> None:
        await started_dj._add_like("alice", "rock")
        prefs = await started_dj._get_preferences("alice")
        assert "rock" in prefs["likes"]

    @pytest.mark.asyncio
    async def test_add_like_deduplicates(self, started_dj: RadioDJService) -> None:
        await started_dj._add_like("alice", "Rock")
        await started_dj._add_like("alice", "rock")
        prefs = await started_dj._get_preferences("alice")
        assert len(prefs["likes"]) == 1

    @pytest.mark.asyncio
    async def test_add_veto(self, started_dj: RadioDJService) -> None:
        await started_dj._add_veto("alice", "country")
        prefs = await started_dj._get_preferences("alice")
        assert "country" in prefs["vetoes"]

    @pytest.mark.asyncio
    async def test_veto_removes_from_likes(self, started_dj: RadioDJService) -> None:
        await started_dj._add_like("alice", "country")
        await started_dj._add_veto("alice", "country")
        prefs = await started_dj._get_preferences("alice")
        assert "country" not in prefs["likes"]
        assert "country" in prefs["vetoes"]

    @pytest.mark.asyncio
    async def test_like_current_records_preference(self, started_dj: RadioDJService) -> None:
        await started_dj.start_radio(genre="jazz")
        result = await started_dj.like_current("alice")
        assert "jazz" in result.lower()
        prefs = await started_dj._get_preferences("alice")
        assert "jazz" in prefs["likes"]

    @pytest.mark.asyncio
    async def test_dislike_current_vetoes_and_switches(self, started_dj: RadioDJService) -> None:
        await started_dj.start_radio(genre="country")
        result = await started_dj.dislike_current("alice")
        assert "vetoed" in result.lower() or "country" in result.lower()
        prefs = await started_dj._get_preferences("alice")
        assert "country" in prefs["vetoes"]

    @pytest.mark.asyncio
    async def test_like_current_records_track_when_known(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """When the music service can report the current track, like_current also
        records the specific track (title/artist) so preferences aren't just genre-level."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="Black Dog",
            artist="Led Zeppelin",
            album="Led Zeppelin IV",
            uri="spotify:track:abc",
        )
        await started_dj.start_radio(genre="classic rock")
        result = await started_dj.like_current("alice")
        assert "Black Dog" in result
        prefs = await started_dj._get_preferences("alice")
        assert "classic rock" in prefs["likes"]
        liked_tracks = prefs.get("liked_tracks", [])
        assert len(liked_tracks) == 1
        assert liked_tracks[0]["title"] == "Black Dog"
        assert liked_tracks[0]["artist"] == "Led Zeppelin"

    @pytest.mark.asyncio
    async def test_dislike_current_records_track_when_known(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """Disliking also records the specific track as vetoed, not just the genre."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="Achy Breaky Heart",
            artist="Billy Ray Cyrus",
        )
        await started_dj.start_radio(genre="country")
        await started_dj.dislike_current("alice")

        prefs = await started_dj._get_preferences("alice")
        assert "country" in prefs["vetoes"]
        vetoed_tracks = prefs.get("vetoed_tracks", [])
        assert len(vetoed_tracks) == 1
        assert vetoed_tracks[0]["title"] == "Achy Breaky Heart"

    @pytest.mark.asyncio
    async def test_like_then_dislike_moves_track(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """Vetoing a previously-liked track removes it from liked_tracks."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="Changeling",
            artist="The Doors",
        )
        await started_dj.start_radio(genre="classic rock")
        await started_dj.like_current("alice")
        await started_dj.dislike_current("alice")

        prefs = await started_dj._get_preferences("alice")
        liked = prefs.get("liked_tracks", [])
        vetoed = prefs.get("vetoed_tracks", [])
        assert not any(t["title"] == "Changeling" for t in liked)
        assert any(t["title"] == "Changeling" for t in vetoed)

    @pytest.mark.asyncio
    async def test_like_current_without_dj_uses_speaker_now_playing(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """The ``asdasdf``-chat bug regression test.

        The radio DJ is NOT running (no ``_current_genre``), but a
        track is playing on the speaker via an external source —
        Spotify direct, AirPlay, Sonos Radio, whatever. The user says
        "I like this song" and the AI calls ``radio_like``. Previously
        the tool bailed with "Nothing is playing right now." because
        it only looked at DJ state. The fix is to consult the
        speaker's now-playing and record a track-level like.
        """
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        # DJ not running — ``started_dj`` fixture gives us a started
        # service but we never called ``start_radio``, so
        # ``_current_genre`` is empty.
        assert not started_dj._current_genre

        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="What You Won't Do for Love",
            artist="Bobby Caldwell",
            album="What You Won't Do for Love",
            uri="spotify:track:xyz",
        )

        result = await started_dj.like_current("alice")

        assert "Bobby Caldwell" in result
        assert "What You Won't Do for Love" in result
        # No genre was mentioned because the DJ wasn't driving playback.
        assert "(" not in result  # no "(genre)" parenthetical

        prefs = await started_dj._get_preferences("alice")
        liked_tracks = prefs.get("liked_tracks", [])
        assert len(liked_tracks) == 1
        assert liked_tracks[0]["title"] == "What You Won't Do for Love"
        assert liked_tracks[0]["artist"] == "Bobby Caldwell"
        # And no genre-level like was recorded — there's no genre to
        # credit when the DJ isn't picking tracks.
        assert prefs.get("likes", []) == []

    @pytest.mark.asyncio
    async def test_like_current_with_nothing_playing_anywhere(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """When the DJ is idle AND the speaker reports no track, the
        tool should still cleanly report that nothing is playing."""
        music_svc.current_now_playing = None
        assert not started_dj._current_genre

        result = await started_dj.like_current("alice")
        assert "nothing is playing" in result.lower()

    @pytest.mark.asyncio
    async def test_like_current_dj_running_no_track_metadata(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """Backcompat: when the DJ is running but the speaker can't
        report a specific track (backend doesn't implement
        now_playing, for example), the genre-only like path still
        works. Regression check for the fix to the
        ``_get_now_playing`` → early-bail case."""
        music_svc.current_now_playing = None
        await started_dj.start_radio(genre="jazz")

        result = await started_dj.like_current("alice")
        assert "jazz" in result.lower()

        prefs = await started_dj._get_preferences("alice")
        assert "jazz" in prefs.get("likes", [])
        assert prefs.get("liked_tracks", []) == []

    @pytest.mark.asyncio
    async def test_dislike_current_without_dj_uses_speaker_now_playing(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """Same bug, dislike side: DJ not running, external source
        playing — the veto should be recorded but the tool should
        explain it can't force-skip audio it doesn't own."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        assert not started_dj._current_genre
        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="Friday",
            artist="Rebecca Black",
        )

        result = await started_dj.dislike_current("alice")

        assert "Friday" in result
        assert "can't skip" in result.lower() or "change it" in result.lower()

        prefs = await started_dj._get_preferences("alice")
        vetoed = prefs.get("vetoed_tracks", [])
        assert len(vetoed) == 1
        assert vetoed[0]["title"] == "Friday"
        # No genre-level veto because the DJ wasn't driving playback.
        assert prefs.get("vetoes", []) == []

    @pytest.mark.asyncio
    async def test_get_status_includes_now_playing(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """The status should reflect the track currently playing on the speaker."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="Kashmir",
            artist="Led Zeppelin",
            album="Physical Graffiti",
            duration_seconds=514.0,
            position_seconds=61.0,
        )
        await started_dj.start_radio(genre="classic rock")
        status = await started_dj.get_status()
        assert "now_playing" in status
        assert status["now_playing"]["title"] == "Kashmir"
        assert status["now_playing"]["is_playing"] is True
        assert status["now_playing"]["position_seconds"] == 61.0

    @pytest.mark.asyncio
    async def test_get_status_handles_missing_now_playing(
        self, started_dj: RadioDJService, music_svc: FakeMusicService
    ) -> None:
        """When the backend reports STOPPED with no metadata, the status still includes
        the now_playing field (with empty strings) — a clear signal to the caller."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        music_svc.current_now_playing = NowPlaying(state=PlaybackState.STOPPED)
        status = await started_dj.get_status()
        assert status["now_playing"]["title"] == ""
        assert status["now_playing"]["is_playing"] is False


# --- Polling ---


class TestPolling:
    @pytest.mark.asyncio
    async def test_poll_does_nothing_when_inactive(
        self, started_dj: RadioDJService, speaker_svc: FakeSpeakerService
    ) -> None:
        await started_dj._poll()
        speaker_svc.play_on_speakers.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_stops_when_empty(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
        presence_svc: FakePresenceService,
    ) -> None:
        await started_dj.start_radio()
        speaker_svc.play_on_speakers.reset_mock()
        # Simulate nobody present
        presence_svc._users = []
        await started_dj._poll()
        speaker_svc.stop_speakers.assert_called()

    @pytest.mark.asyncio
    async def test_poll_resumes_when_people_arrive(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
        presence_svc: FakePresenceService,
    ) -> None:
        await started_dj.start_radio()
        # Empty
        presence_svc._users = []
        await started_dj._poll()
        speaker_svc.play_on_speakers.reset_mock()
        # People return
        presence_svc._users = ["alice"]
        await started_dj._poll()
        speaker_svc.play_on_speakers.assert_called()


# --- Event handling ---


class TestEventHandling:
    @pytest.mark.asyncio
    async def test_on_arrival_resumes_stopped_radio(
        self, started_dj: RadioDJService, speaker_svc: FakeSpeakerService
    ) -> None:
        started_dj._active = True
        started_dj._stopped_by_empty = True
        started_dj._present_users = set()

        event = Event(
            event_type="presence.arrived",
            data={"user_id": "alice"},
            source="presence",
        )
        await started_dj._on_presence_arrived(event)
        assert started_dj._stopped_by_empty is False
        speaker_svc.play_on_speakers.assert_called()

    @pytest.mark.asyncio
    async def test_on_departure_stops_if_empty(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
        presence_svc: FakePresenceService,
    ) -> None:
        started_dj._active = True
        started_dj._present_users = {"alice"}
        presence_svc._users = []

        event = Event(
            event_type="presence.departed",
            data={"user_id": "alice"},
            source="presence",
        )
        await started_dj._on_presence_departed(event)
        speaker_svc.stop_speakers.assert_called()
        assert started_dj._stopped_by_empty is True

    @pytest.mark.asyncio
    async def test_on_arrival_does_nothing_when_inactive(
        self, started_dj: RadioDJService, speaker_svc: FakeSpeakerService
    ) -> None:
        started_dj._active = False
        event = Event(
            event_type="presence.arrived",
            data={"user_id": "alice"},
            source="presence",
        )
        await started_dj._on_presence_arrived(event)
        speaker_svc.play_on_speakers.assert_not_called()

    @pytest.mark.asyncio
    async def test_arrival_honors_throttle(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
    ) -> None:
        """Regression guard for "DJ plays whenever it wants": arrivals
        used to bypass min_switch_interval. Now they respect it, so
        people walking in and out can't thrash playback."""
        started_dj._active = True
        # A recent genre switch → throttle NOT elapsed.
        started_dj._last_genre_switch = datetime.now(UTC)
        started_dj._current_genre = "jazz"
        started_dj._present_users = {"alice"}

        event = Event(
            event_type="presence.arrived",
            data={"user_id": "bob"},
            source="presence",
        )
        await started_dj._on_presence_arrived(event)
        speaker_svc.play_on_speakers.assert_not_called()

    @pytest.mark.asyncio
    async def test_external_play_disarms_dj_auto_switch(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
        music_svc: FakeMusicService,
        presence_svc: FakePresenceService,
    ) -> None:
        """End-to-end: user plays their own music while the DJ is
        active → a ``music.playback_started`` event with
        initiator="user" flips _dj_owns_playback off. The next auto-
        switch attempt defers and does NOT call play_on_speakers."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        started_dj._active = True
        started_dj._current_genre = "jazz"
        started_dj._last_genre_switch = datetime.now(UTC) - timedelta(hours=1)
        started_dj._present_users = {"alice"}
        presence_svc._users = ["alice", "bob"]  # presence changes → would switch

        # Simulate the event the MusicService would emit when the user
        # runs /music play or clicks a Play button.
        user_event = Event(
            event_type="music.playback_started",
            data={
                "uri": "spotify:track:userpick",
                "title": "User's Song",
                "kind": "track",
                "initiator": "user",
            },
            source="music",
        )
        await started_dj._on_music_playback_started(user_event)
        assert started_dj._dj_owns_playback is False

        # The user's track is still playing according to now_playing.
        music_svc.current_now_playing = NowPlaying(
            state=PlaybackState.PLAYING,
            title="User's Song",
            uri="spotify:track:userpick",
        )

        speaker_svc.play_on_speakers.reset_mock()
        await started_dj._poll()
        # DJ deferred — user's music is still playing.
        speaker_svc.play_on_speakers.assert_not_called()

    @pytest.mark.asyncio
    async def test_dj_reclaims_playback_when_external_music_stops(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
        music_svc: FakeMusicService,
        presence_svc: FakePresenceService,
    ) -> None:
        """Once the user's music ends (now_playing reports not-PLAYING),
        the DJ can take the floor again on the next presence tick."""
        from gilbert.interfaces.speaker import NowPlaying, PlaybackState

        started_dj._active = True
        started_dj._dj_owns_playback = False  # user had taken over
        started_dj._current_genre = "jazz"
        started_dj._last_genre_switch = datetime.now(UTC) - timedelta(hours=1)
        started_dj._present_users = {"alice"}
        presence_svc._users = ["alice", "bob"]

        # Speaker is silent — user's track ended.
        music_svc.current_now_playing = NowPlaying(state=PlaybackState.STOPPED)

        await started_dj._poll()
        # DJ reclaimed ownership and auto-switched.
        assert started_dj._dj_owns_playback is True
        speaker_svc.play_on_speakers.assert_called()

    @pytest.mark.asyncio
    async def test_dj_play_keeps_ownership_via_initiator_filter(
        self,
        started_dj: RadioDJService,
        music_svc: FakeMusicService,
    ) -> None:
        """The DJ's own play_item emits initiator="dj"; the handler
        must NOT flip ownership off for its own emission."""
        started_dj._dj_owns_playback = True
        dj_event = Event(
            event_type="music.playback_started",
            data={
                "uri": "spotify:playlist:dj",
                "title": "Best of Jazz",
                "kind": "playlist",
                "initiator": "dj",
            },
            source="music",
        )
        await started_dj._on_music_playback_started(dj_event)
        assert started_dj._dj_owns_playback is True

    @pytest.mark.asyncio
    async def test_dj_passes_initiator_dj_to_music_service(
        self,
        started_dj: RadioDJService,
        music_svc: FakeMusicService,
    ) -> None:
        """Regression guard for the ownership feedback loop: every DJ
        play must tag itself so the event handler can filter self-
        emissions. Without this, the DJ would disarm itself on its
        own plays."""
        await started_dj._play_genre("jazz")
        assert music_svc.last_play_initiator == "dj"


# --- AI Tools ---


class TestTools:
    def test_tool_definitions(self, dj: RadioDJService) -> None:
        dj._enabled = True
        tools = dj.get_tools()
        names = {t.name for t in tools}
        assert names == {
            "radio_start",
            "radio_stop",
            "radio_request",
            "radio_skip",
            "radio_like",
            "radio_dislike",
            "radio_veto",
            "radio_status",
            "radio_set_preferences",
        }

    def test_admin_tools_require_admin_role(self, dj: RadioDJService) -> None:
        dj._enabled = True
        tools = dj.get_tools()
        admin_tools = [t for t in tools if t.required_role == "admin"]
        assert len(admin_tools) == 1
        assert admin_tools[0].name == "radio_set_preferences"

    @pytest.mark.asyncio
    async def test_tool_start(self, started_dj: RadioDJService) -> None:
        result = await started_dj.execute_tool("radio_start", {})
        assert "started" in result.lower() or "playing" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_stop(self, started_dj: RadioDJService) -> None:
        await started_dj.start_radio()
        result = await started_dj.execute_tool("radio_stop", {})
        assert "stopped" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_request(self, started_dj: RadioDJService) -> None:
        result = await started_dj.execute_tool("radio_request", {"query": "funk"})
        assert "funk" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_status(self, started_dj: RadioDJService) -> None:
        result = await started_dj.execute_tool("radio_status", {})
        import json

        status = json.loads(result)
        assert "active" in status
        assert "current_genre" in status
        assert "default_genres" in status

    @pytest.mark.asyncio
    async def test_tool_set_preferences(self, started_dj: RadioDJService) -> None:
        result = await started_dj.execute_tool(
            "radio_set_preferences",
            {
                "user_id": "alice",
                "likes": ["rock", "jazz"],
                "vetoes": ["country"],
            },
        )
        import json

        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["likes"] == ["rock", "jazz"]
        assert data["vetoes"] == ["country"]

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, started_dj: RadioDJService) -> None:
        with pytest.raises(KeyError):
            await started_dj.execute_tool("radio_nope", {})

    @pytest.mark.asyncio
    async def test_tool_skip_when_not_playing(self, started_dj: RadioDJService) -> None:
        result = await started_dj.execute_tool("radio_skip", {})
        assert "not playing" in result.lower()


# --- Genre change events ---


class TestGenreChangeEvents:
    @pytest.mark.asyncio
    async def test_genre_change_emits_event(
        self, started_dj: RadioDJService, event_bus_svc: FakeEventBusSvc
    ) -> None:
        await started_dj.start_radio(genre="rock")
        event_bus_svc.bus.published.clear()
        await started_dj.request_genre("jazz")
        genre_events = [
            e for e in event_bus_svc.bus.published if e.event_type == "radio_dj.genre.changed"
        ]
        assert len(genre_events) == 1
        assert genre_events[0].data["old_genre"] == "rock"
        assert genre_events[0].data["new_genre"] == "jazz"

    @pytest.mark.asyncio
    async def test_start_emits_started_event(
        self, started_dj: RadioDJService, event_bus_svc: FakeEventBusSvc
    ) -> None:
        event_bus_svc.bus.published.clear()
        await started_dj.start_radio(genre="rock")
        types = [e.event_type for e in event_bus_svc.bus.published]
        assert "radio_dj.started" in types


# --- Veto ---


class TestVeto:
    @pytest.mark.asyncio
    async def test_veto_genre_records_preference(self, started_dj: RadioDJService) -> None:
        result = await started_dj.veto_genre("alice", "country")
        assert "vetoed" in result.lower()
        prefs = await started_dj._get_preferences("alice")
        assert "country" in prefs["vetoes"]

    @pytest.mark.asyncio
    async def test_veto_current_genre_switches(
        self,
        started_dj: RadioDJService,
        speaker_svc: FakeSpeakerService,
    ) -> None:
        await started_dj.start_radio(genre="country")
        speaker_svc.play_on_speakers.reset_mock()
        await started_dj.veto_genre("alice", "country")
        # Should have switched to a different genre
        speaker_svc.play_on_speakers.assert_called()


# --- Configuration ---


class TestConfig:
    def test_config_params_list(self, dj: RadioDJService) -> None:
        params = dj.config_params()
        keys = {p.key for p in params}
        assert "default_genres" in keys
        assert "min_switch_interval" in keys
        assert "default_volume" in keys
        assert "speakers" in keys
        assert "stop_when_empty" in keys
        assert "poll_interval" in keys

    def test_config_namespace(self, dj: RadioDJService) -> None:
        assert dj.config_namespace == "radio_dj"

    @pytest.mark.asyncio
    async def test_apply_config(self, dj: RadioDJService) -> None:
        await dj.on_config_changed(
            {
                "default_volume": 50,
                "min_switch_interval": 30,
                "speakers": ["shop"],
                "stop_when_empty": False,
            }
        )
        assert dj._default_volume == 50
        assert dj._min_switch_minutes == 30
        assert dj._speakers == ["shop"]
        assert dj._stop_when_empty is False
