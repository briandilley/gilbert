"""MusicService playlist tools against a real SQLite entity store."""

from typing import Any

import pytest

from gilbert.core.services.music import MusicService
from gilbert.core.services.music_playlists import PLAYLISTS_COLLECTION, PlaylistStore
from gilbert.interfaces.context import UserContext, set_current_user
from gilbert.interfaces.music import MusicBackend, MusicItem, Playable
from gilbert.interfaces.service import Service, ServiceResolver
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
    }
    tools = {t.name: t for t in svc.get_tools() if t.name in names}
    assert set(tools) == names
    assert all(t.required_role == "user" for t in tools.values())


async def test_my_playlists_description_distinguishes_from_list_playlists(
    svc: MusicService,
) -> None:
    """The AI picks tools by description. ``list_playlists`` returns the
    LINKED service's read-only playlists; ``my_playlists`` returns
    Gilbert-owned editable ones. Without explicit wording the model will
    call the wrong one."""
    tools = {t.name: t for t in svc.get_tools()}
    mine = tools["my_playlists"].description
    assert "list_playlists" in mine
    assert "read-only" in mine.lower()
    assert "gilbert" in mine.lower()


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


def test_service_info_requires_entity_storage() -> None:
    info = MusicService().service_info()
    assert "entity_storage" in info.requires
    assert {
        "music.playlist_created",
        "music.playlist_updated",
        "music.playlist_deleted",
    } <= info.events
