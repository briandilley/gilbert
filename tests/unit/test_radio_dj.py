"""Tests for RadioDJService — context-aware music DJ."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.core.services.radio_dj import RadioDJService
from gilbert.interfaces.events import Event
from gilbert.interfaces.music import PlaylistInfo, SearchResults
from gilbert.interfaces.speaker import PlayRequest


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
        return [
            UserPresence(user_id=uid, state=PresenceState.PRESENT)
            for uid in self._users
        ]


class FakeMusicService:
    def __init__(self) -> None:
        self.last_search: str | None = None

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="music", capabilities=frozenset({"music"}))

    async def search(self, query: str, *, limit: int = 10) -> SearchResults:
        self.last_search = query
        return SearchResults(
            playlists=[
                PlaylistInfo(
                    playlist_id="pl_123",
                    name=f"Best of {query}",
                    external_url=f"spotify:playlist:pl_123",
                ),
            ],
        )


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

    def add_job(self, name: str, schedule: Any, callback: Any, system: bool = False, **kwargs: Any) -> Any:
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
def music_svc() -> FakeMusicService:
    return FakeMusicService()


@pytest.fixture
def speaker_svc() -> FakeSpeakerService:
    return FakeSpeakerService()


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
        await started_dj._save_preferences("alice", {
            "user_id": "alice", "likes": ["rock", "jazz"], "vetoes": [],
        })
        await started_dj._save_preferences("bob", {
            "user_id": "bob", "likes": ["rock"], "vetoes": [],
        })
        genre = await started_dj.select_genre({"alice", "bob"})
        assert genre == "rock"

    @pytest.mark.asyncio
    async def test_vetoed_genre_excluded(
        self, started_dj: RadioDJService,
    ) -> None:
        """Vetoed genres are excluded from selection."""
        await started_dj._save_preferences("alice", {
            "user_id": "alice", "likes": ["rock", "jazz"], "vetoes": [],
        })
        await started_dj._save_preferences("bob", {
            "user_id": "bob", "likes": ["rock"], "vetoes": ["rock"],
        })
        genre = await started_dj.select_genre({"alice", "bob"})
        # Rock is vetoed by Bob, so jazz should win
        assert genre == "jazz"

    @pytest.mark.asyncio
    async def test_all_voted_genres_vetoed_falls_back(
        self, started_dj: RadioDJService,
    ) -> None:
        """When all voted genres are vetoed, fall back to default rotation."""
        await started_dj._save_preferences("alice", {
            "user_id": "alice", "likes": ["rock"], "vetoes": [],
        })
        await started_dj._save_preferences("bob", {
            "user_id": "bob", "likes": [], "vetoes": ["rock"],
        })
        genre = await started_dj.select_genre({"alice", "bob"})
        # Should get a default genre (not rock since it's vetoed)
        assert genre is not None
        assert genre.lower() != "rock"

    @pytest.mark.asyncio
    async def test_no_preferences_uses_defaults(
        self, started_dj: RadioDJService,
    ) -> None:
        """Users with no preferences fall back to default rotation."""
        genre = await started_dj.select_genre({"alice"})
        assert genre in [g.lower() for g in started_dj._default_genres] or genre in started_dj._default_genres


# --- Throttle logic ---


class TestThrottle:
    def test_can_switch_when_never_switched(self, dj: RadioDJService) -> None:
        assert dj._can_switch_genre() is True

    def test_cannot_switch_too_soon(self, dj: RadioDJService) -> None:
        dj._last_genre_switch = datetime.now(timezone.utc)
        assert dj._can_switch_genre() is False

    def test_can_switch_after_interval(self, dj: RadioDJService) -> None:
        dj._last_genre_switch = datetime.now(timezone.utc) - timedelta(minutes=20)
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
    async def test_like_current_records_preference(
        self, started_dj: RadioDJService
    ) -> None:
        await started_dj.start_radio(genre="jazz")
        result = await started_dj.like_current("alice")
        assert "jazz" in result.lower()
        prefs = await started_dj._get_preferences("alice")
        assert "jazz" in prefs["likes"]

    @pytest.mark.asyncio
    async def test_dislike_current_vetoes_and_switches(
        self, started_dj: RadioDJService
    ) -> None:
        await started_dj.start_radio(genre="country")
        result = await started_dj.dislike_current("alice")
        assert "vetoed" in result.lower() or "country" in result.lower()
        prefs = await started_dj._get_preferences("alice")
        assert "country" in prefs["vetoes"]


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


# --- AI Tools ---


class TestTools:
    def test_tool_definitions(self, dj: RadioDJService) -> None:
        dj._enabled = True
        tools = dj.get_tools()
        names = {t.name for t in tools}
        assert names == {
            "radio_start", "radio_stop", "radio_request", "radio_skip",
            "radio_like", "radio_dislike", "radio_veto", "radio_status",
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
        result = await started_dj.execute_tool("radio_set_preferences", {
            "user_id": "alice",
            "likes": ["rock", "jazz"],
            "vetoes": ["country"],
        })
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
            e for e in event_bus_svc.bus.published
            if e.event_type == "radio_dj.genre.changed"
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
    async def test_veto_genre_records_preference(
        self, started_dj: RadioDJService
    ) -> None:
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
        result = await started_dj.veto_genre("alice", "country")
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
        await dj.on_config_changed({
            "default_volume": 50,
            "min_switch_interval": 30,
            "speakers": ["shop"],
            "stop_when_empty": False,
        })
        assert dj._default_volume == 50
        assert dj._min_switch_minutes == 30
        assert dj._speakers == ["shop"]
        assert dj._stop_when_empty is False
