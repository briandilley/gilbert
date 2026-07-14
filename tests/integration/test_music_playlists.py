"""PlaylistStore against a real SQLite entity store."""

import pytest

from gilbert.core.services.music_playlists import (
    DuplicatePlaylistNameError,
    PlaylistNotFoundError,
    PlaylistPositionError,
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


async def test_add_item_appends_in_order(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track("t1", "One"))
    pl = await store.add_item("alice", "Workout", _track("t2", "Two"))
    assert [i.title for i in pl.items] == ["One", "Two"]


async def test_add_item_allows_duplicates(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track("t1", "One"))
    pl = await store.add_item("alice", "Workout", _track("t1", "One"))
    assert [i.title for i in pl.items] == ["One", "One"]


async def test_add_item_persists_full_snapshot(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track("t1", "One"))
    pl = await store.get_by_name("alice", "Workout")
    item = pl.items[0]
    assert item.uri == "spotify:track:t1"
    assert item.service == "Spotify"
    assert item.subtitle == "Parkway Drive"
    assert item.duration_seconds == 210.0
    assert item.kind == MusicItemKind.TRACK


async def test_add_item_denies_other_users_playlist(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    with pytest.raises(PlaylistNotFoundError):
        await store.add_item("bob", "Workout", _track())


async def test_remove_at_removes_by_1_based_position(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track("t1", "One"))
    await store.add_item("alice", "Workout", _track("t2", "Two"))
    pl, removed = await store.remove_at("alice", "Workout", 1)
    assert removed.title == "One"
    assert [i.title for i in pl.items] == ["Two"]


async def test_remove_at_rejects_out_of_range(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track("t1", "One"))
    with pytest.raises(PlaylistPositionError):
        await store.remove_at("alice", "Workout", 2)
    with pytest.raises(PlaylistPositionError):
        await store.remove_at("alice", "Workout", 0)


async def test_remove_at_denies_other_users_playlist(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track())
    with pytest.raises(PlaylistNotFoundError):
        await store.remove_at("bob", "Workout", 1)


async def test_update_renames(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    pl = await store.update("alice", "Workout", new_name="Gym")
    assert pl.name == "Gym"
    assert (await store.get_by_name("alice", "Gym")).name == "Gym"


async def test_update_rename_keeps_items_and_id(store: PlaylistStore) -> None:
    created = await store.create("alice", "Workout")
    await store.add_item("alice", "Workout", _track("t1", "One"))
    pl = await store.update("alice", "Workout", new_name="Gym")
    assert pl.id == created.id
    assert [i.title for i in pl.items] == ["One"]


async def test_update_rejects_rename_onto_existing_name(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.create("alice", "Chill")
    with pytest.raises(DuplicatePlaylistNameError):
        await store.update("alice", "Workout", new_name="chill")


async def test_update_rename_to_same_name_is_allowed(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    pl = await store.update("alice", "Workout", new_name="workout")
    assert pl.name == "workout"


async def test_update_sets_shuffle_default(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    pl = await store.update("alice", "Workout", shuffle=True)
    assert pl.shuffle is True
    pl = await store.update("alice", "Workout", shuffle=False)
    assert pl.shuffle is False


async def test_update_denies_other_users_playlist(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    with pytest.raises(PlaylistNotFoundError):
        await store.update("bob", "Workout", new_name="Stolen")


async def test_delete_removes_playlist(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    await store.delete("alice", "Workout")
    with pytest.raises(PlaylistNotFoundError):
        await store.get_by_name("alice", "Workout")


async def test_delete_denies_other_users_playlist(store: PlaylistStore) -> None:
    await store.create("alice", "Workout")
    with pytest.raises(PlaylistNotFoundError):
        await store.delete("bob", "Workout")
    assert (await store.get_by_name("alice", "Workout")).name == "Workout"
