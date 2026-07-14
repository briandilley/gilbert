"""MusicService playlist tools against a real SQLite entity store."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.music import MusicService
from gilbert.core.services.music_playlists import PLAYLISTS_COLLECTION, PlaylistStore
from gilbert.interfaces.context import UserContext, set_current_user
from gilbert.interfaces.music import MusicBackend, MusicItem, MusicItemKind, Playable
from gilbert.interfaces.service import Service, ServiceResolver
from gilbert.interfaces.speaker import NowPlaying, PlaybackState
from gilbert.interfaces.storage import (
    NamespacedStorageBackend,
    Query,
    StorageBackend,
    StorageProvider,
)
from gilbert.storage.sqlite import SQLiteStorage


def _user(user_id: str) -> UserContext:
    return UserContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id.title(),
        roles=frozenset({"user"}),
    )


@pytest.fixture
def alice() -> UserContext:
    return _user("alice")


@pytest.fixture
async def svc(sqlite_storage: SQLiteStorage) -> MusicService:
    service = MusicService()
    store = PlaylistStore(sqlite_storage)
    await store.ensure_indexes()
    service._playlists = store
    service._enabled = True
    return service


async def test_create_playlist_tool(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    out = await svc.execute_tool("create_playlist", {"name": "Workout"})
    assert "Workout" in out


async def test_create_playlist_rejects_duplicate(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("create_playlist", {"name": "workout"})
    assert "already have" in out.lower()


async def test_my_playlists_lists_only_callers(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    set_current_user(_user("bob"))
    await svc.execute_tool("create_playlist", {"name": "Bob Only"})

    out = await svc.execute_tool("my_playlists", {})
    assert "Bob Only" in out
    assert "Workout" not in out


async def test_show_playlist_denies_other_users(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    set_current_user(_user("bob"))
    out = await svc.execute_tool("show_playlist", {"name": "Workout"})
    assert "no playlist" in out.lower()


async def test_update_playlist_renames_and_sets_shuffle(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool(
        "update_playlist", {"name": "Workout", "new_name": "Gym", "shuffle": True}
    )
    out = await svc.execute_tool("my_playlists", {})
    assert "Gym" in out
    store = svc._playlists
    assert store is not None
    assert (await store.get_by_name("alice", "Gym")).shuffle is True


async def test_delete_playlist(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("delete_playlist", {"name": "Workout"})
    out = await svc.execute_tool("my_playlists", {})
    assert "Workout" not in out


async def test_playlist_tools_declare_user_role(svc: MusicService) -> None:
    names = {
        "create_playlist",
        "my_playlists",
        "show_playlist",
        "update_playlist",
        "delete_playlist",
        "add_to_playlist",
        "remove_from_playlist",
    }
    tools = {t.name: t for t in svc.get_tools() if t.name in names}
    assert set(tools) == names
    assert all(t.required_role == "user" for t in tools.values())
    assert all(t.slash_group == "music" for t in tools.values())


async def test_my_playlists_description_distinguishes_from_list_playlists(
    svc: MusicService,
) -> None:
    """The AI picks tools by description. ``list_playlists`` returns the
    LINKED service's read-only playlists; ``my_playlists`` returns
    Gilbert-owned editable ones. Without explicit wording the model will
    call the wrong one — and the cross-reference has to run BOTH ways, or
    "what playlists do I have?" lands on whichever the model saw first."""
    tools = {t.name: t for t in svc.get_tools()}

    mine = tools["my_playlists"].description
    assert "list_playlists" in mine
    assert "read-only" in mine.lower()
    assert "gilbert" in mine.lower()

    theirs = tools["list_playlists"].description
    assert "my_playlists" in theirs
    assert "read-only" in theirs.lower()


# --- SYSTEM-context refusal ---


async def test_playlist_tools_refuse_system_context(svc: MusicService) -> None:
    """``get_current_user()`` yields ``UserContext.SYSTEM`` on scheduled and
    email-triggered turns, and ``check_tool_access`` short-circuits to True
    for it. Without a guard those turns would create playlists owned by
    "system" that no human can ever see."""
    set_current_user(UserContext.SYSTEM)

    created = await svc.execute_tool("create_playlist", {"name": "Ghost"})
    assert "sign in" in created.lower()

    listed = await svc.execute_tool("my_playlists", {})
    assert "sign in" in listed.lower()

    for tool, args in (
        ("show_playlist", {"name": "Ghost"}),
        ("update_playlist", {"name": "Ghost", "new_name": "Ghost 2"}),
        ("delete_playlist", {"name": "Ghost"}),
    ):
        out = await svc.execute_tool(tool, args)
        assert "sign in" in out.lower(), tool

    # Nothing was written under the SYSTEM sentinel.
    rows = await svc._require_playlists()._storage.query(Query(collection=PLAYLISTS_COLLECTION))
    assert rows == []


# --- Null-argument handling ---


async def test_create_playlist_rejects_explicit_null_name(
    svc: MusicService, alice: UserContext
) -> None:
    """A model can emit ``{"name": null}``. ``str(None)`` is "None" — the
    playlist must not be created under that name."""
    set_current_user(alice)
    out = await svc.execute_tool("create_playlist", {"name": None})
    assert "None" not in out
    assert await svc._require_playlists().list_for("alice") == []


# --- Event emissions ---


class _RecordingEventBus:
    """In-memory event bus that records every publish."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    def subscribe(self, event_type: str, handler: Any) -> Any:
        return lambda: None

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        return lambda: None

    async def publish(self, event: Any) -> None:
        self.published.append(event)


async def test_playlist_crud_emits_events(svc: MusicService, alice: UserContext) -> None:
    """The three playlist events are the only way other systems learn a
    playlist changed. Without this test every ``_emit_playlist_event`` call
    could be deleted and nothing would fail."""
    bus = _RecordingEventBus()
    svc._event_bus = bus  # type: ignore[assignment]
    set_current_user(alice)

    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("update_playlist", {"name": "Workout", "new_name": "Cardio"})
    await svc.execute_tool("delete_playlist", {"name": "Cardio"})

    assert [e.event_type for e in bus.published] == [
        "music.playlist_created",
        "music.playlist_updated",
        "music.playlist_deleted",
    ]
    playlist_id = bus.published[0].data["playlist_id"]
    assert playlist_id
    for ev in bus.published:
        assert ev.data["playlist_id"] == playlist_id
        # The owner rides on ``user_id`` — the key the WS fan-out's
        # per-user filter (``can_see_music_event``) reads to keep these
        # owner-scoped events off other users' connections.
        assert ev.data["user_id"] == "alice"
        assert "owner_user_id" not in ev.data
        assert ev.source == "music"
    assert bus.published[0].data["name"] == "Workout"
    assert bus.published[1].data["name"] == "Cardio"


# --- start() wiring ---


class _StubMusicBackend(MusicBackend):
    backend_name = "_playlist_stub"

    async def initialize(self, config: dict[str, object]) -> None: ...

    async def close(self) -> None: ...

    async def list_favorites(self) -> list[MusicItem]:
        return []

    async def list_playlists(self) -> list[MusicItem]:
        return []

    async def search(self, query: str, *, kind: Any = None, limit: int = 10) -> list[MusicItem]:
        return []

    async def resolve_playable(self, item: MusicItem) -> Playable:
        return Playable(uri="")


class _StorageSvc:
    """Satisfies ``StorageProvider`` over a real SQLite backend."""

    def __init__(self, storage: SQLiteStorage) -> None:
        self._storage = storage

    @property
    def backend(self) -> StorageBackend:
        return self._storage

    @property
    def raw_backend(self) -> StorageBackend:
        return self._storage

    def create_namespaced(self, namespace: str) -> NamespacedStorageBackend:
        raise NotImplementedError


class _ConfigSvc:
    """Satisfies ``ConfigurationReader`` with the music section enabled."""

    _SECTION = {"enabled": True, "backend": "_playlist_stub", "settings": {}}

    def get(self, path: str) -> Any:
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return dict(self._SECTION)

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        return dict(self._SECTION)

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        raise NotImplementedError


class _Resolver(ServiceResolver):
    def __init__(self, caps: dict[str, Any]) -> None:
        self._caps = caps

    def get_capability(self, capability: str) -> Any:
        return self._caps.get(capability)

    def require_capability(self, capability: str) -> Any:
        svc = self._caps.get(capability)
        if svc is None:
            raise LookupError(f"No service provides {capability!r}")
        return svc

    def get_all(self, capability: str) -> list[Service]:
        svc = self._caps.get(capability)
        return [svc] if svc is not None else []


async def test_start_wires_playlist_store_from_entity_storage(
    sqlite_storage: SQLiteStorage, alice: UserContext
) -> None:
    """The real ``start()`` path must resolve ``entity_storage`` and build
    a working PlaylistStore — the other tests inject one directly, so
    without this nothing covers the wiring the service declares in
    ``requires``."""
    storage_svc = _StorageSvc(sqlite_storage)
    assert isinstance(storage_svc, StorageProvider)

    svc = MusicService()
    await svc.start(_Resolver({"configuration": _ConfigSvc(), "entity_storage": storage_svc}))

    assert svc._enabled is True
    assert svc._playlists is not None

    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Wired"})
    rows = await sqlite_storage.query(Query(collection=PLAYLISTS_COLLECTION))
    assert [r["name"] for r in rows] == ["Wired"]
    assert rows[0]["owner_user_id"] == "alice"


# --- add_to_playlist / remove_from_playlist ---


def _hit(track_id: str = "t1", title: str = "Horizon") -> MusicItem:
    return MusicItem(
        id=track_id,
        title=title,
        kind=MusicItemKind.TRACK,
        subtitle="Parkway Drive",
        uri=f"spotify:track:{track_id}",
        service="Spotify",
    )


async def test_add_to_playlist_by_query(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(return_value=[_hit("t1", "Horizon")])  # type: ignore[method-assign]
    await svc.execute_tool("create_playlist", {"name": "Workout"})

    out = await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "horizon"})
    assert "Horizon" in out
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    assert [i.title for i in pl.items] == ["Horizon"]


async def test_add_to_playlist_by_track_id(svc: MusicService, alice: UserContext) -> None:
    """``MusicBackend`` has no by-id lookup (see interfaces/music.py), so a
    track_id is resolved by searching for it and matching the returned id —
    not by taking the top hit blindly."""
    set_current_user(alice)
    svc.search = AsyncMock(  # type: ignore[method-assign]
        return_value=[_hit("other", "Wrong Song"), _hit("t2", "Right Song")]
    )
    await svc.execute_tool("create_playlist", {"name": "Workout"})

    out = await svc.execute_tool("add_to_playlist", {"name": "Workout", "track_id": "t2"})
    assert "Right Song" in out
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    assert [i.id for i in pl.items] == ["t2"]


async def test_add_to_playlist_track_id_no_match(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(return_value=[_hit("other", "Wrong Song")])  # type: ignore[method-assign]
    await svc.execute_tool("create_playlist", {"name": "Workout"})

    out = await svc.execute_tool("add_to_playlist", {"name": "Workout", "track_id": "t9"})
    assert "search" in out.lower()
    store = svc._playlists
    assert store is not None
    assert (await store.get_by_name("alice", "Workout")).items == ()


async def test_add_to_playlist_by_query_no_results(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(return_value=[])  # type: ignore[method-assign]
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "nothing"})
    assert "no results" in out.lower()
    store = svc._playlists
    assert store is not None
    assert (await store.get_by_name("alice", "Workout")).items == ()


async def test_add_to_playlist_uses_now_playing_when_no_args(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    svc.now_playing = AsyncMock(  # type: ignore[method-assign]
        return_value=NowPlaying(
            state=PlaybackState.PLAYING,
            title="Horizon",
            artist="Parkway Drive",
            uri="spotify:track:t1",
            duration_seconds=210.0,
        )
    )
    await svc.execute_tool("create_playlist", {"name": "Workout"})

    out = await svc.execute_tool("add_to_playlist", {"name": "Workout"})
    assert "Horizon" in out
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    assert pl.items[0].title == "Horizon"
    assert pl.items[0].uri == "spotify:track:t1"
    assert pl.items[0].subtitle == "Parkway Drive"


async def test_add_to_playlist_no_args_nothing_playing(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    svc.now_playing = AsyncMock(return_value=NowPlaying(state=PlaybackState.STOPPED))  # type: ignore[method-assign]
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("add_to_playlist", {"name": "Workout"})
    assert "nothing is playing" in out.lower()


async def test_add_to_playlist_treats_null_args_as_absent(
    svc: MusicService, alice: UserContext
) -> None:
    """A model can emit ``{"query": null, "track_id": null}``. ``str(None)``
    is the literal string "None" — it must not become a search query."""
    set_current_user(alice)
    svc.search = AsyncMock(return_value=[_hit()])  # type: ignore[method-assign]
    svc.now_playing = AsyncMock(return_value=NowPlaying(state=PlaybackState.STOPPED))  # type: ignore[method-assign]
    await svc.execute_tool("create_playlist", {"name": "Workout"})

    out = await svc.execute_tool(
        "add_to_playlist", {"name": "Workout", "query": None, "track_id": None}
    )
    assert "nothing is playing" in out.lower()
    svc.search.assert_not_awaited()


async def test_add_to_playlist_denies_other_users(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    svc.search = AsyncMock(return_value=[_hit()])  # type: ignore[method-assign]
    set_current_user(_user("bob"))
    out = await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "horizon"})
    assert "no playlist" in out.lower()


async def test_remove_from_playlist_by_position(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")]])  # type: ignore[method-assign]
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "one"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "two"})

    out = await svc.execute_tool("remove_from_playlist", {"name": "Workout", "position": 1})
    assert "One" in out
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    assert [i.title for i in pl.items] == ["Two"]


async def test_remove_from_playlist_out_of_range(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("remove_from_playlist", {"name": "Workout", "position": 3})
    assert "empty" in out.lower() or "range" in out.lower()


async def test_playlist_mutation_emits_updated_event(svc: MusicService, alice: UserContext) -> None:
    """Adding and removing tracks are the two mutations the SPA has to
    re-render for — both must publish ``music.playlist_updated``."""
    bus = _RecordingEventBus()
    svc._event_bus = bus  # type: ignore[assignment]
    svc.search = AsyncMock(return_value=[_hit("t1", "Horizon")])  # type: ignore[method-assign]
    set_current_user(alice)

    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "horizon"})
    await svc.execute_tool("remove_from_playlist", {"name": "Workout", "position": 1})

    assert [e.event_type for e in bus.published] == [
        "music.playlist_created",
        "music.playlist_updated",
        "music.playlist_updated",
    ]
    # Owner-scoped: the WS fan-out filter reads ``user_id``.
    assert [e.data["user_id"] for e in bus.published] == ["alice"] * 3
    assert bus.published[1].data["track_count"] == 1
    assert bus.published[2].data["track_count"] == 0


async def test_add_remove_playlist_tools_refuse_system_context(svc: MusicService) -> None:
    """Same guard as the five CRUD tools: a SYSTEM turn (scheduled job,
    inbox-triggered chat) must not mutate — or create — playlist data under
    the SYSTEM sentinel."""
    set_current_user(UserContext.SYSTEM)
    svc.search = AsyncMock(return_value=[_hit()])  # type: ignore[method-assign]
    svc.now_playing = AsyncMock(  # type: ignore[method-assign]
        return_value=NowPlaying(state=PlaybackState.PLAYING, title="Horizon", uri="u")
    )

    for tool, args in (
        ("add_to_playlist", {"name": "Ghost", "query": "horizon"}),
        ("add_to_playlist", {"name": "Ghost"}),
        ("remove_from_playlist", {"name": "Ghost", "position": 1}),
    ):
        out = await svc.execute_tool(tool, args)
        assert "sign in" in out.lower(), tool

    rows = await svc._require_playlists()._storage.query(Query(collection=PLAYLISTS_COLLECTION))
    assert rows == []


def test_service_info_requires_entity_storage() -> None:
    info = MusicService().service_info()
    assert "entity_storage" in info.requires
    assert {
        "music.playlist_created",
        "music.playlist_updated",
        "music.playlist_deleted",
    } <= info.events
