"""PlaylistStore against a real SQLite entity store."""

import pytest

from gilbert.core.services.music_playlists import (
    DuplicatePlaylistNameError,
    PlaylistNotFoundError,
    PlaylistStore,
)
from gilbert.interfaces.music import MusicItem, MusicItemKind
from gilbert.storage.sqlite import SQLiteStorage


def _track(track_id: str = "t1", title: str = "Horizon") -> MusicItem:
    return MusicItem(
        id=track_id,
        title=title,
        kind=MusicItemKind.TRACK,
        subtitle="Parkway Drive",
        uri=f"spotify:track:{track_id}",
        service="Spotify",
        duration_seconds=210.0,
    )


@pytest.fixture
async def store(sqlite_storage: SQLiteStorage) -> PlaylistStore:
    s = PlaylistStore(sqlite_storage)
    await s.ensure_indexes()
    return s


async def test_create_returns_named_empty_playlist(store: PlaylistStore) -> None:
    pl = await store.create("alice", "Workout")
    assert pl.name == "Workout"
    assert pl.owner_user_id == "alice"
    assert pl.items == ()
    assert pl.shuffle is False
    assert pl.id


async def test_create_with_shuffle_default(store: PlaylistStore) -> None:
    pl = await store.create("alice", "Party", shuffle=True)
    assert pl.shuffle is True


async def test_get_by_name_roundtrips_items(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    fetched = await store.get_by_name("alice", "Workout")
    assert fetched.name == "Workout"
    assert fetched.items == ()


async def test_get_by_name_is_case_insensitive(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    fetched = await store.get_by_name("alice", "wOrKoUt")
    assert fetched.name == "Workout"


async def test_duplicate_name_rejected_case_insensitively(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    with pytest.raises(DuplicatePlaylistNameError):
        await store.create("alice", "workout")


async def test_same_name_allowed_for_different_owners(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    bob = await store.create("bob", "Workout")
    assert bob.owner_user_id == "bob"


async def test_list_for_returns_only_own_playlists(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.create("alice", "Chill")
    await store.create("bob", "Bob Only")
    names = {p.name for p in await store.list_for("alice")}
    assert names == {"Workout", "Chill"}


async def test_get_by_name_denies_other_users_playlist(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    with pytest.raises(PlaylistNotFoundError):
        await store.get_by_name("bob", "Workout")


async def test_get_by_name_unknown_raises(store: PlaylistStore) -> None:
    with pytest.raises(PlaylistNotFoundError):
        await store.get_by_name("alice", "Nope")
