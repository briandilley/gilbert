# Gilbert-Owned Music Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-user, named playlists that Gilbert stores itself — create, add/remove tracks, rename, delete, and play (optionally shuffled) through any music backend.

**Architecture:** Playlists are a `MusicService` feature backed by Gilbert's entity store, **not** a `MusicBackend` capability — nothing is ever written to the upstream music service (Spotify). Persistence + ACL live in a new focused module `core/services/music_playlists.py` (`PlaylistStore`); `music.py` owns only tool wiring and playback. Playback resolves each stored item via the existing `MusicBackend.resolve_playable()` and loads the speaker queue, modelled on `start_station` (`music.py:506-559`).

**Tech Stack:** Python 3.12+, uv, pytest, SQLite entity store.

**Spec:** `docs/superpowers/specs/2026-07-14-music-playlists-design.md`

## Global Constraints

- **Always use `uv run`** — never `pip`, never a bare `pytest`.
- **Layer rules:** `interfaces/` imports nothing from `core/`. `core/services/` never imports concrete backends. Use `StorageProvider` (protocol), never a concrete storage class.
- **Type hints on every signature.** `uv run mypy src/` must pass.
- **RBAC:** every `ToolDefinition` declares `required_role`. All eight playlist tools use `required_role="user"` — they need a caller identity and mutate that user's data.
- **Multi-user isolation is a hard rule:** every store method takes `owner_user_id` and filters on it. A user must never read, play, mutate, or delete another user's playlist.
- **DB tests hit a real SQLite database** (fixture `sqlite_storage` in `tests/conftest.py`) — never mock the DB. Mocks are for the music/speaker backends only.
- **No migration is needed** — the generic entity store accepts new collections without one.
- Collection name: `music_playlists`. Never rename it after first write.
- **`UserContext` lives in `gilbert.interfaces.auth`** (not `.context`) and its real fields are
  `user_id, email, display_name, roles: frozenset[str]` — there is no `username` or `role` field.
  `tests/integration/test_music_service_playlists.py` already defines a `_user(user_id)` helper that
  builds one correctly; reuse it. `set_current_user` / `get_current_user` come from
  `gilbert.interfaces.context`.
- Known pre-existing failures, unrelated to this work: `std-plugins/kokoro/tests/test_kokoro_integration.py`
  fails on a `KPipeline` import in this environment. Ignore them.
- **Every playlist tool handler must refuse the SYSTEM caller.** `get_current_user()` returns
  `UserContext.SYSTEM` (`user_id="system"`) on unauthenticated/system turns (scheduled jobs,
  `inbox_ai_chat`), and `AccessControlService` short-circuits RBAC to `True` for `"system"` — so
  without a guard, a scheduled turn creates playlists owned by `"system"` that no human can ever
  see. Task 3 added this guard to the five CRUD handlers (follow its shape — see the `weather.py`
  `_exec_set_home` precedent); **`add_to_playlist`, `remove_from_playlist`, and `play_playlist`
  need it too.**

---

### Task 1: `Playlist` type + `PlaylistStore` reads (create / list / get)

**Files:**
- Modify: `src/gilbert/interfaces/music.py` (add `Playlist`, extend `__all__`)
- Create: `src/gilbert/core/services/music_playlists.py`
- Test: `tests/integration/test_music_playlists.py`

**Interfaces:**
- Consumes: `MusicItem`, `MusicItemKind` from `gilbert.interfaces.music`; `StorageBackend`, `Query`, `Filter`, `FilterOp`, `IndexDefinition` from `gilbert.interfaces.storage`.
- Produces:
  - `Playlist(id, owner_user_id, name, items: tuple[MusicItem, ...], shuffle: bool, created_at, updated_at)` — frozen dataclass.
  - `PlaylistStore(storage: StorageBackend)` with `ensure_indexes()`, `create(owner_user_id, name, shuffle=False) -> Playlist`, `list_for(owner_user_id) -> list[Playlist]`, `get_by_name(owner_user_id, name) -> Playlist`.
  - Errors: `PlaylistError`, `PlaylistNotFoundError`, `DuplicatePlaylistNameError`, `PlaylistPositionError`.
  - Module constant `PLAYLISTS_COLLECTION = "music_playlists"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_music_playlists.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_music_playlists.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gilbert.core.services.music_playlists'`

- [ ] **Step 3: Add the `Playlist` dataclass**

In `src/gilbert/interfaces/music.py`, add `"Playlist"` to `__all__` (keep the list alphabetical) and add this dataclass after `Playable`:

```python
@dataclass(frozen=True)
class Playlist:
    """A Gilbert-owned, per-user playlist.

    Distinct from a ``MusicItem`` of kind ``PLAYLIST``, which is a
    *reference* to a playlist in the upstream service (read-only).
    A ``Playlist`` is stored by Gilbert, owned by one user, and freely
    editable — Gilbert never writes to the upstream service.

    ``items`` are point-in-time snapshots of ``MusicItem``s, so playback
    needs no re-search and the playlist survives a track vanishing from
    the upstream search index. ``shuffle`` is the playlist's *default*
    play order; a ``play_playlist`` call may override it either way.
    """

    id: str
    owner_user_id: str
    name: str
    items: tuple[MusicItem, ...] = ()
    shuffle: bool = False
    created_at: str = ""
    updated_at: str = ""
```

- [ ] **Step 4: Write `music_playlists.py`**

Create `src/gilbert/core/services/music_playlists.py`:

```python
"""Per-user, Gilbert-owned music playlists — persistence and ACL.

Split out of ``music.py`` so playlist storage stays independently
testable and ``music.py`` keeps to tool wiring + playback.

Every method takes ``owner_user_id`` and filters on it. There is no
"admin sees all" path: a playlist belonging to another user is reported
as *not found*, so the ACL denial is indistinguishable from absence and
nothing leaks about other users' libraries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gilbert.interfaces.music import MusicItem, MusicItemKind, Playlist
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
)

__all__ = [
    "PLAYLISTS_COLLECTION",
    "DuplicatePlaylistNameError",
    "PlaylistError",
    "PlaylistNotFoundError",
    "PlaylistPositionError",
    "PlaylistStore",
]

PLAYLISTS_COLLECTION = "music_playlists"


class PlaylistError(RuntimeError):
    """Base class for playlist errors."""


class PlaylistNotFoundError(PlaylistError):
    """No playlist with that name for this owner (or it isn't theirs)."""


class DuplicatePlaylistNameError(PlaylistError):
    """This owner already has a playlist with that name."""


class PlaylistPositionError(PlaylistError):
    """A 1-based track position was out of range."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _item_to_dict(item: MusicItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "kind": str(item.kind),
        "subtitle": item.subtitle,
        "uri": item.uri,
        "didl_meta": item.didl_meta,
        "album_art_url": item.album_art_url,
        "duration_seconds": item.duration_seconds,
        "service": item.service,
    }


def _item_from_dict(data: dict[str, Any]) -> MusicItem:
    return MusicItem(
        id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        kind=MusicItemKind(data.get("kind") or MusicItemKind.TRACK),
        subtitle=str(data.get("subtitle", "")),
        uri=str(data.get("uri", "")),
        didl_meta=str(data.get("didl_meta", "")),
        album_art_url=str(data.get("album_art_url", "")),
        duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
        service=str(data.get("service", "")),
    )


def _to_dict(playlist: Playlist) -> dict[str, Any]:
    return {
        "id": playlist.id,
        "owner_user_id": playlist.owner_user_id,
        "name": playlist.name,
        "shuffle": playlist.shuffle,
        "items": [_item_to_dict(i) for i in playlist.items],
        "created_at": playlist.created_at,
        "updated_at": playlist.updated_at,
    }


def _from_dict(data: dict[str, Any]) -> Playlist:
    raw_items = data.get("items") or []
    return Playlist(
        id=str(data.get("id", "")),
        owner_user_id=str(data.get("owner_user_id", "")),
        name=str(data.get("name", "")),
        items=tuple(_item_from_dict(i) for i in raw_items),
        shuffle=bool(data.get("shuffle", False)),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )


class PlaylistStore:
    """Storage + ACL for per-user playlists."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def ensure_indexes(self) -> None:
        await self._storage.ensure_index(
            IndexDefinition(
                collection=PLAYLISTS_COLLECTION, fields=["owner_user_id"]
            )
        )

    async def list_for(self, owner_user_id: str) -> list[Playlist]:
        rows = await self._storage.query(
            Query(
                collection=PLAYLISTS_COLLECTION,
                filters=[
                    Filter(
                        field="owner_user_id",
                        op=FilterOp.EQ,
                        value=owner_user_id,
                    )
                ],
            )
        )
        return [_from_dict(r) for r in rows]

    async def get_by_name(self, owner_user_id: str, name: str) -> Playlist:
        found = await self._find(owner_user_id, name)
        if found is None:
            raise PlaylistNotFoundError(f"No playlist named {name!r}")
        return found

    async def create(
        self,
        owner_user_id: str,
        name: str,
        shuffle: bool = False,
    ) -> Playlist:
        clean = name.strip()
        if not clean:
            raise PlaylistError("Playlist name cannot be empty")
        if await self._find(owner_user_id, clean) is not None:
            raise DuplicatePlaylistNameError(
                f"You already have a playlist named {clean!r}"
            )
        stamp = _now()
        playlist = Playlist(
            id=str(uuid4()),
            owner_user_id=owner_user_id,
            name=clean,
            items=(),
            shuffle=shuffle,
            created_at=stamp,
            updated_at=stamp,
        )
        await self._save(playlist)
        return playlist

    # --- internals ---

    async def _find(self, owner_user_id: str, name: str) -> Playlist | None:
        """Case-insensitive name lookup, scoped to the owner.

        Matching happens in Python rather than in the query because the
        entity store has no case-insensitive comparison operator.
        """
        target = name.strip().casefold()
        for playlist in await self.list_for(owner_user_id):
            if playlist.name.casefold() == target:
                return playlist
        return None

    async def _save(self, playlist: Playlist) -> None:
        await self._storage.put(
            PLAYLISTS_COLLECTION, playlist.id, _to_dict(playlist)
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_music_playlists.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/interfaces/music.py src/gilbert/core/services/music_playlists.py tests/integration/test_music_playlists.py
git commit -m "feat(music): Playlist type + PlaylistStore reads with per-owner ACL"
```

---

### Task 2: `PlaylistStore` mutations (add / remove / update / delete)

**Files:**
- Modify: `src/gilbert/core/services/music_playlists.py`
- Test: `tests/integration/test_music_playlists.py`

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces, on `PlaylistStore`:
  - `add_item(owner_user_id, name, item: MusicItem) -> Playlist`
  - `remove_at(owner_user_id, name, position: int) -> tuple[Playlist, MusicItem]` — `position` is **1-based**
  - `update(owner_user_id, name, new_name: str | None = None, shuffle: bool | None = None) -> Playlist`
  - `delete(owner_user_id, name) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_music_playlists.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_music_playlists.py -v`
Expected: FAIL — `AttributeError: 'PlaylistStore' object has no attribute 'add_item'`

- [ ] **Step 3: Implement the mutations**

Append these methods to `PlaylistStore` in `src/gilbert/core/services/music_playlists.py`, **above** the `# --- internals ---` marker:

```python
    async def add_item(
        self,
        owner_user_id: str,
        name: str,
        item: MusicItem,
    ) -> Playlist:
        """Append an item. Duplicates are allowed, as Spotify allows them."""
        playlist = await self.get_by_name(owner_user_id, name)
        updated = Playlist(
            id=playlist.id,
            owner_user_id=playlist.owner_user_id,
            name=playlist.name,
            items=(*playlist.items, item),
            shuffle=playlist.shuffle,
            created_at=playlist.created_at,
            updated_at=_now(),
        )
        await self._save(updated)
        return updated

    async def remove_at(
        self,
        owner_user_id: str,
        name: str,
        position: int,
    ) -> tuple[Playlist, MusicItem]:
        """Remove the item at a **1-based** position (as shown to users)."""
        playlist = await self.get_by_name(owner_user_id, name)
        if position < 1 or position > len(playlist.items):
            raise PlaylistPositionError(
                f"Position {position} is out of range for {playlist.name!r} "
                f"(1-{len(playlist.items)})"
                if playlist.items
                else f"{playlist.name!r} is empty"
            )
        items = list(playlist.items)
        removed = items.pop(position - 1)
        updated = Playlist(
            id=playlist.id,
            owner_user_id=playlist.owner_user_id,
            name=playlist.name,
            items=tuple(items),
            shuffle=playlist.shuffle,
            created_at=playlist.created_at,
            updated_at=_now(),
        )
        await self._save(updated)
        return updated, removed

    async def update(
        self,
        owner_user_id: str,
        name: str,
        new_name: str | None = None,
        shuffle: bool | None = None,
    ) -> Playlist:
        """Rename and/or change the stored shuffle default."""
        playlist = await self.get_by_name(owner_user_id, name)

        target_name = playlist.name
        if new_name is not None:
            clean = new_name.strip()
            if not clean:
                raise PlaylistError("Playlist name cannot be empty")
            # A rename that collides with a *different* playlist is a
            # conflict; renaming a playlist to a case-variant of its own
            # name is just a re-case and must be allowed.
            clash = await self._find(owner_user_id, clean)
            if clash is not None and clash.id != playlist.id:
                raise DuplicatePlaylistNameError(
                    f"You already have a playlist named {clean!r}"
                )
            target_name = clean

        updated = Playlist(
            id=playlist.id,
            owner_user_id=playlist.owner_user_id,
            name=target_name,
            items=playlist.items,
            shuffle=playlist.shuffle if shuffle is None else shuffle,
            created_at=playlist.created_at,
            updated_at=_now(),
        )
        await self._save(updated)
        return updated

    async def delete(self, owner_user_id: str, name: str) -> None:
        playlist = await self.get_by_name(owner_user_id, name)
        await self._storage.delete(PLAYLISTS_COLLECTION, playlist.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_music_playlists.py -v`
Expected: PASS (23 tests)

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/music_playlists.py tests/integration/test_music_playlists.py
git commit -m "feat(music): PlaylistStore add/remove/update/delete"
```

---

### Task 3: Wire storage into `MusicService` + CRUD tools

**Files:**
- Modify: `src/gilbert/core/services/music.py` (`__init__` ~line 221, `service_info` ~line 230, `start` ~line 249, `get_tools` ~line 688, `execute_tool` ~line 1061)
- Test: `tests/integration/test_music_service_playlists.py`

**Interfaces:**
- Consumes: `PlaylistStore` and its errors from Task 2; `get_current_user()` from `gilbert.interfaces.context`.
- Produces on `MusicService`:
  - `self._playlists: PlaylistStore | None`
  - Tools `create_playlist`, `my_playlists`, `show_playlist`, `update_playlist`, `delete_playlist`.
  - Private helper `_require_playlists() -> PlaylistStore`.
  - Events `music.playlist_created`, `music.playlist_updated`, `music.playlist_deleted`.

**Note on the two "playlist" tools:** the existing `list_playlists` tool returns the *linked service's* saved playlists (read-only). The new `my_playlists` returns *Gilbert-owned, editable* playlists. Their descriptions must say so explicitly or the AI will pick the wrong one.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_music_service_playlists.py`:

```python
"""MusicService playlist tools against a real SQLite entity store."""

import json
from typing import Any

import pytest

from gilbert.core.services.music import MusicService
from gilbert.core.services.music_playlists import PlaylistStore
from gilbert.interfaces.context import UserContext, set_current_user
from gilbert.storage.sqlite import SQLiteStorage


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


async def test_create_playlist_rejects_duplicate(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("create_playlist", {"name": "workout"})
    assert "already have" in out.lower()


async def test_my_playlists_lists_only_callers(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    set_current_user(_user("bob"))
    await svc.execute_tool("create_playlist", {"name": "Bob Only"})

    out = await svc.execute_tool("my_playlists", {})
    assert "Bob Only" in out
    assert "Workout" not in out


async def test_show_playlist_denies_other_users(
    svc: MusicService, alice: UserContext
) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_music_service_playlists.py -v`
Expected: FAIL — `KeyError: Unknown tool: create_playlist`

- [ ] **Step 3: Wire storage into the service**

In `src/gilbert/core/services/music.py`, add these imports alongside the existing ones:

```python
from gilbert.interfaces.context import get_current_user
from gilbert.interfaces.storage import StorageProvider
from gilbert.core.services.music_playlists import (
    DuplicatePlaylistNameError,
    PlaylistError,
    PlaylistNotFoundError,
    PlaylistPositionError,
    PlaylistStore,
)
```

In `__init__`, add:

```python
        self._playlists: PlaylistStore | None = None
```

In `service_info`, add `entity_storage` to `requires` and register the new events:

```python
    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="music",
            capabilities=frozenset({"music", "ai_tools"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"configuration", "speaker_control", "event_bus"}),
            events=frozenset({
                "music.playback_started",
                "music.playlist_created",
                "music.playlist_updated",
                "music.playlist_deleted",
            }),
            toggleable=True,
            toggle_description="Music playback and search",
        )
```

In `start()`, immediately after `self._enabled = True`, wire the store:

```python
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError(
                "entity_storage capability does not provide StorageProvider"
            )
        self._playlists = PlaylistStore(storage_svc.backend)
        await self._playlists.ensure_indexes()
```

Add this helper next to `_get_speaker_svc`:

```python
    def _require_playlists(self) -> PlaylistStore:
        if self._playlists is None:
            raise RuntimeError("Playlist storage is not available")
        return self._playlists
```

- [ ] **Step 4: Add the five CRUD tool definitions**

Append these to the list returned by `get_tools()` in `music.py`:

```python
            ToolDefinition(
                name="create_playlist",
                description=(
                    "Create a new empty playlist owned by you in Gilbert. "
                    "Gilbert playlists are editable; they are separate from "
                    "the read-only saved playlists on the linked music service."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name for the new playlist.",
                    ),
                    ToolParameter(
                        name="shuffle",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "Whether this playlist shuffles by default when played."
                        ),
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
                slash_group="music",
                slash_command="playlist-create",
                slash_help="Create a playlist: /music playlist-create <name>",
            ),
            ToolDefinition(
                name="my_playlists",
                description=(
                    "List the playlists you own in Gilbert (editable). Use "
                    "list_playlists instead for the read-only saved playlists "
                    "on the linked music service."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
                slash_group="music",
                slash_command="playlist-list",
                slash_help="List your playlists: /music playlist-list",
            ),
            ToolDefinition(
                name="show_playlist",
                description=(
                    "Show the tracks in one of your Gilbert playlists, with "
                    "1-based positions suitable for remove_from_playlist."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of your playlist.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
                slash_group="music",
                slash_command="playlist-show",
                slash_help="Show a playlist: /music playlist-show <name>",
            ),
            ToolDefinition(
                name="update_playlist",
                description=(
                    "Rename one of your Gilbert playlists and/or change whether "
                    "it shuffles by default when played."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Current name of your playlist.",
                    ),
                    ToolParameter(
                        name="new_name",
                        type=ToolParameterType.STRING,
                        description="New name for the playlist.",
                        required=False,
                    ),
                    ToolParameter(
                        name="shuffle",
                        type=ToolParameterType.BOOLEAN,
                        description="New default shuffle setting.",
                        required=False,
                    ),
                ],
                required_role="user",
                slash_group="music",
                slash_command="playlist-update",
                slash_help=(
                    "Rename / set shuffle: /music playlist-update <name> "
                    "[new_name=<name>] [shuffle=true]"
                ),
            ),
            ToolDefinition(
                name="delete_playlist",
                description="Delete one of your Gilbert playlists.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of your playlist.",
                    ),
                ],
                required_role="user",
                slash_group="music",
                slash_command="playlist-delete",
                slash_help="Delete a playlist: /music playlist-delete <name>",
            ),
```

- [ ] **Step 5: Add the tool handlers and dispatch**

Add these methods to `MusicService` in `music.py`:

```python
    # ── Playlist tools ───────────────────────────────────────────────

    async def _emit_playlist_event(
        self, event_type: str, playlist: Playlist
    ) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(
                event_type=event_type,
                data={
                    "playlist_id": playlist.id,
                    "name": playlist.name,
                    "owner_user_id": playlist.owner_user_id,
                    "track_count": len(playlist.items),
                },
                source="music",
            )
        )

    @staticmethod
    def _format_playlist(playlist: Playlist) -> str:
        if not playlist.items:
            return f"{playlist.name} is empty."
        lines = [
            f"{playlist.name} ({len(playlist.items)} tracks"
            f"{', shuffles by default' if playlist.shuffle else ''}):"
        ]
        for pos, item in enumerate(playlist.items, start=1):
            suffix = f" — {item.subtitle}" if item.subtitle else ""
            lines.append(f"{pos}. {item.title}{suffix}")
        return "\n".join(lines)

    async def _tool_create_playlist(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        name = str(arguments.get("name", "")).strip()
        shuffle = bool(arguments.get("shuffle", False))
        try:
            playlist = await self._require_playlists().create(
                user.user_id, name, shuffle=shuffle
            )
        except (DuplicatePlaylistNameError, PlaylistError) as exc:
            return str(exc)
        await self._emit_playlist_event("music.playlist_created", playlist)
        return f"Created playlist {playlist.name!r}."

    async def _tool_my_playlists(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        playlists = await self._require_playlists().list_for(user.user_id)
        if not playlists:
            return "You have no playlists yet."
        return "\n".join(
            f"{p.name} ({len(p.items)} tracks)"
            f"{' [shuffle]' if p.shuffle else ''}"
            for p in playlists
        )

    async def _tool_show_playlist(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        try:
            playlist = await self._require_playlists().get_by_name(
                user.user_id, str(arguments.get("name", ""))
            )
        except PlaylistNotFoundError as exc:
            return str(exc)
        return self._format_playlist(playlist)

    async def _tool_update_playlist(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        raw_shuffle = arguments.get("shuffle")
        try:
            playlist = await self._require_playlists().update(
                user.user_id,
                str(arguments.get("name", "")),
                new_name=(
                    str(arguments["new_name"])
                    if arguments.get("new_name")
                    else None
                ),
                shuffle=None if raw_shuffle is None else bool(raw_shuffle),
            )
        except (PlaylistNotFoundError, DuplicatePlaylistNameError, PlaylistError) as exc:
            return str(exc)
        await self._emit_playlist_event("music.playlist_updated", playlist)
        return f"Updated playlist {playlist.name!r}."

    async def _tool_delete_playlist(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        name = str(arguments.get("name", ""))
        store = self._require_playlists()
        try:
            playlist = await store.get_by_name(user.user_id, name)
            await store.delete(user.user_id, name)
        except PlaylistNotFoundError as exc:
            return str(exc)
        await self._emit_playlist_event("music.playlist_deleted", playlist)
        return f"Deleted playlist {playlist.name!r}."
```

Import `Playlist` from `gilbert.interfaces.music` at the top of `music.py` (add it to the existing import from that module).

Add these cases to the `match name:` block in `execute_tool`:

```python
            case "create_playlist":
                return await self._tool_create_playlist(arguments)
            case "my_playlists":
                return await self._tool_my_playlists(arguments)
            case "show_playlist":
                return await self._tool_show_playlist(arguments)
            case "update_playlist":
                return await self._tool_update_playlist(arguments)
            case "delete_playlist":
                return await self._tool_delete_playlist(arguments)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_music_service_playlists.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Verify nothing else broke**

Run: `uv run pytest tests/unit/test_music_service.py tests/integration/ -v`
Expected: PASS. If `test_music_service.py` fails because `MusicService.start()` now requires `entity_storage`, fix the test's resolver stub to provide an `entity_storage` capability whose `.backend` is a `SQLiteStorage` — do not weaken the service's `requires`.

- [ ] **Step 8: Commit**

```bash
git add src/gilbert/core/services/music.py tests/integration/test_music_service_playlists.py tests/unit/test_music_service.py
git commit -m "feat(music): playlist CRUD tools scoped to the calling user"
```

---

### Task 4: `add_to_playlist` / `remove_from_playlist`

**Files:**
- Modify: `src/gilbert/core/services/music.py`
- Test: `tests/integration/test_music_service_playlists.py`

**Interfaces:**
- Consumes: `PlaylistStore.add_item` / `remove_at` (Task 2); `MusicService.search()`, `MusicService.now_playing()`.
- Produces: tools `add_to_playlist(name, query?, track_id?)` and `remove_from_playlist(name, position)`.

**Resolution order for `add_to_playlist`** — exactly this, and no other:
1. `track_id` given → resolve it by calling `search(track_id, limit=5)` and taking the hit whose `id == track_id`. If no hit matches, return an error telling the user to search and add by query instead. **Why the search:** `MusicBackend` deliberately has no by-id lookup (`interfaces/music.py:8-11`) — SMAPI-style backends can't fetch an arbitrary item by id without having seen it in a browse/search first — so a search that round-trips the id is the only way to turn an id back into a `MusicItem`.
2. `query` given → `search(query, limit=1)`, take the top hit. Error if there are no results.
3. Neither → `now_playing()`; build a `MusicItem` from it. Error if nothing is playing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_music_service_playlists.py`:

```python
from unittest.mock import AsyncMock

from gilbert.interfaces.music import MusicItem, MusicItemKind
from gilbert.interfaces.speaker import NowPlaying, PlaybackState


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
    svc.search = AsyncMock(return_value=[_hit("t1", "Horizon")])
    await svc.execute_tool("create_playlist", {"name": "Workout"})

    out = await svc.execute_tool(
        "add_to_playlist", {"name": "Workout", "query": "horizon"}
    )
    assert "Horizon" in out
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    assert [i.title for i in pl.items] == ["Horizon"]


async def test_add_to_playlist_by_query_no_results(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(return_value=[])
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool(
        "add_to_playlist", {"name": "Workout", "query": "nothing"}
    )
    assert "no" in out.lower()


async def test_add_to_playlist_uses_now_playing_when_no_args(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    svc.now_playing = AsyncMock(
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
    svc.now_playing = AsyncMock(
        return_value=NowPlaying(state=PlaybackState.STOPPED)
    )
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("add_to_playlist", {"name": "Workout"})
    assert "nothing is playing" in out.lower()


async def test_add_to_playlist_denies_other_users(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    svc.search = AsyncMock(return_value=[_hit()])
    set_current_user(_user("bob"))
    out = await svc.execute_tool(
        "add_to_playlist", {"name": "Workout", "query": "horizon"}
    )
    assert "no playlist" in out.lower()


async def test_remove_from_playlist_by_position(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")]])
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "one"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "two"})

    out = await svc.execute_tool(
        "remove_from_playlist", {"name": "Workout", "position": 1}
    )
    assert "One" in out
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    assert [i.title for i in pl.items] == ["Two"]


async def test_remove_from_playlist_out_of_range(
    svc: MusicService, alice: UserContext
) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool(
        "remove_from_playlist", {"name": "Workout", "position": 3}
    )
    assert "empty" in out.lower() or "range" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_music_service_playlists.py -v`
Expected: FAIL — `KeyError: Unknown tool: add_to_playlist`

- [ ] **Step 3: Add the two tool definitions**

Append to `get_tools()` in `music.py`:

```python
            ToolDefinition(
                name="add_to_playlist",
                description=(
                    "Add a track to one of your Gilbert playlists. Give "
                    "'track_id' to add a specific search hit, or 'query' to "
                    "search and add the top match, or neither to add the track "
                    "that is currently playing."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of your playlist.",
                    ),
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Search text; the top hit is added.",
                        required=False,
                    ),
                    ToolParameter(
                        name="track_id",
                        type=ToolParameterType.STRING,
                        description="Id of a track from a previous search.",
                        required=False,
                    ),
                ],
                required_role="user",
                slash_group="music",
                slash_command="playlist-add",
                slash_help=(
                    "Add a track: /music playlist-add <name> [query] "
                    "(no query adds the current track)"
                ),
            ),
            ToolDefinition(
                name="remove_from_playlist",
                description=(
                    "Remove a track from one of your Gilbert playlists by its "
                    "1-based position, as shown by show_playlist."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of your playlist.",
                    ),
                    ToolParameter(
                        name="position",
                        type=ToolParameterType.INTEGER,
                        description="1-based position of the track to remove.",
                    ),
                ],
                required_role="user",
                slash_group="music",
                slash_command="playlist-remove",
                slash_help="Remove a track: /music playlist-remove <name> <position>",
            ),
```

- [ ] **Step 4: Implement the handlers**

Add to `MusicService`:

```python
    async def _resolve_item_to_add(self, arguments: dict[str, Any]) -> MusicItem:
        """Resolve what add_to_playlist should add.

        Order: explicit track_id, then query, then the now-playing track.
        Backends have no by-id lookup (see interfaces/music.py), so a
        track_id is confirmed by searching for it and matching the id.
        """
        track_id = str(arguments.get("track_id", "")).strip()
        query = str(arguments.get("query", "")).strip()

        if track_id:
            for hit in await self.search(track_id, limit=5):
                if hit.id == track_id:
                    return hit
            raise PlaylistError(
                f"Couldn't resolve track id {track_id!r} — search for the "
                f"track and add it by query instead."
            )

        if query:
            hits = await self.search(query, limit=1)
            if not hits:
                raise PlaylistError(f"No results for {query!r}.")
            return hits[0]

        now = await self.now_playing()
        if not now.uri or not now.title:
            raise PlaylistError(
                "Nothing is playing — give a query or a track_id to add."
            )
        return MusicItem(
            id=now.uri,
            title=now.title,
            kind=MusicItemKind.TRACK,
            subtitle=now.artist,
            uri=now.uri,
            album_art_url=now.album_art_url,
            duration_seconds=now.duration_seconds,
        )

    async def _tool_add_to_playlist(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        name = str(arguments.get("name", ""))
        store = self._require_playlists()
        try:
            item = await self._resolve_item_to_add(arguments)
            playlist = await store.add_item(user.user_id, name, item)
        except (PlaylistNotFoundError, PlaylistError) as exc:
            return str(exc)
        await self._emit_playlist_event("music.playlist_updated", playlist)
        return f"Added {item.title!r} to {playlist.name!r}."

    async def _tool_remove_from_playlist(self, arguments: dict[str, Any]) -> str:
        user = get_current_user()
        try:
            position = int(arguments.get("position", 0))
        except (TypeError, ValueError):
            return "Position must be a whole number."
        try:
            playlist, removed = await self._require_playlists().remove_at(
                user.user_id, str(arguments.get("name", "")), position
            )
        except (PlaylistNotFoundError, PlaylistPositionError, PlaylistError) as exc:
            return str(exc)
        await self._emit_playlist_event("music.playlist_updated", playlist)
        return f"Removed {removed.title!r} from {playlist.name!r}."
```

Ensure `MusicItemKind` is imported in `music.py` (add to the existing `gilbert.interfaces.music` import if absent).

Add the dispatch cases in `execute_tool`:

```python
            case "add_to_playlist":
                return await self._tool_add_to_playlist(arguments)
            case "remove_from_playlist":
                return await self._tool_remove_from_playlist(arguments)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_music_service_playlists.py -v`
Expected: PASS (14 tests)

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/music.py tests/integration/test_music_service_playlists.py
git commit -m "feat(music): add_to_playlist (query / track_id / now-playing) + remove_from_playlist"
```

---

### Task 5: `play_playlist` with shuffle

**Files:**
- Modify: `src/gilbert/core/services/music.py`
- Test: `tests/integration/test_music_service_playlists.py`

**Interfaces:**
- Consumes: `MusicService.play_item()` (clears the queue and starts playback), `MusicService.add_to_queue()`, `MusicService.supports_queue`, `PlaylistStore.get_by_name`.
- Produces: tool `play_playlist(name, shuffle?, speaker_names?, volume?)` and method `play_playlist(...) -> str`.

**Behavior** (mirrors `start_station`, `music.py:506-559`):
1. Effective shuffle = the `shuffle` argument when supplied, else the playlist's stored `shuffle`. An explicit `false` overrides a stored `true`.
2. If shuffling, shuffle a **copy** of the items — never mutate stored order.
3. `play_item(first)` then `add_to_queue(rest)` when `supports_queue`; a no-queue backend just plays the first track (graceful degradation, no error).
4. Items that fail to resolve are skipped, not fatal. Report the count.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_music_service_playlists.py`.

**Note:** `supports_queue` is a read-only property on `MusicService`, so these
tests override it via `monkeypatch.setattr` — which pytest reverts after each
test. Do **not** assign to `type(svc).supports_queue` directly: that mutates the
class for the rest of the session and leaks into unrelated tests.

```python
def _set_queue_support(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(
        MusicService, "supports_queue", property(lambda self: value)
    )


async def test_play_playlist_plays_first_and_queues_rest_in_order(
    svc: MusicService, alice: UserContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(
        side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")], [_hit("t3", "Three")]]
    )
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    for q in ("one", "two", "three"):
        await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": q})

    played: list[str] = []
    queued: list[str] = []
    svc.play_item = AsyncMock(side_effect=lambda item, **kw: played.append(item.title))
    svc.add_to_queue = AsyncMock(side_effect=lambda item, **kw: queued.append(item.title))
    _set_queue_support(monkeypatch, True)

    out = await svc.execute_tool("play_playlist", {"name": "Workout"})
    assert played == ["One"]
    assert queued == ["Two", "Three"]
    assert "3" in out


async def test_play_playlist_shuffle_arg_reorders_but_keeps_stored_order(
    svc: MusicService, alice: UserContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(
        side_effect=[[_hit(f"t{i}", f"Track{i}")] for i in range(1, 9)]
    )
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    for i in range(1, 9):
        await svc.execute_tool(
            "add_to_playlist", {"name": "Workout", "query": f"track{i}"}
        )

    order: list[str] = []
    svc.play_item = AsyncMock(side_effect=lambda item, **kw: order.append(item.title))
    svc.add_to_queue = AsyncMock(side_effect=lambda item, **kw: order.append(item.title))
    _set_queue_support(monkeypatch, True)

    await svc.execute_tool("play_playlist", {"name": "Workout", "shuffle": True})

    expected = {f"Track{i}" for i in range(1, 9)}
    assert set(order) == expected  # same items...
    store = svc._playlists
    assert store is not None
    pl = await store.get_by_name("alice", "Workout")
    stored = [i.title for i in pl.items]
    assert stored == [f"Track{i}" for i in range(1, 9)]  # ...stored order untouched


async def test_play_playlist_uses_stored_shuffle_default(
    svc: MusicService, alice: UserContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")]])
    await svc.execute_tool("create_playlist", {"name": "Workout", "shuffle": True})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "one"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "two"})

    shuffled: list[Any] = []
    monkeypatch.setattr(
        "gilbert.core.services.music.random.shuffle",
        lambda seq: shuffled.append(list(seq)),
    )
    svc.play_item = AsyncMock()
    svc.add_to_queue = AsyncMock()
    _set_queue_support(monkeypatch, True)

    await svc.execute_tool("play_playlist", {"name": "Workout"})
    assert shuffled, "stored shuffle=True should have shuffled without an argument"


async def test_play_playlist_shuffle_false_overrides_stored_true(
    svc: MusicService, alice: UserContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")]])
    await svc.execute_tool("create_playlist", {"name": "Workout", "shuffle": True})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "one"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "two"})

    shuffled: list[Any] = []
    monkeypatch.setattr(
        "gilbert.core.services.music.random.shuffle",
        lambda seq: shuffled.append(list(seq)),
    )
    svc.play_item = AsyncMock()
    svc.add_to_queue = AsyncMock()
    _set_queue_support(monkeypatch, True)

    await svc.execute_tool("play_playlist", {"name": "Workout", "shuffle": False})
    assert not shuffled, "explicit shuffle=False must override the stored default"


async def test_play_playlist_skips_unresolvable_items(
    svc: MusicService, alice: UserContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")]])
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "one"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "two"})

    svc.play_item = AsyncMock()
    svc.add_to_queue = AsyncMock(side_effect=RuntimeError("gone"))
    _set_queue_support(monkeypatch, True)

    out = await svc.execute_tool("play_playlist", {"name": "Workout"})
    assert "1 of 2" in out or "unavailable" in out.lower()


async def test_play_playlist_empty(svc: MusicService, alice: UserContext) -> None:
    set_current_user(alice)
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    out = await svc.execute_tool("play_playlist", {"name": "Workout"})
    assert "empty" in out.lower()


async def test_play_playlist_without_queue_plays_first_track(
    svc: MusicService, alice: UserContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_current_user(alice)
    svc.search = AsyncMock(side_effect=[[_hit("t1", "One")], [_hit("t2", "Two")]])
    await svc.execute_tool("create_playlist", {"name": "Workout"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "one"})
    await svc.execute_tool("add_to_playlist", {"name": "Workout", "query": "two"})

    played: list[str] = []
    svc.play_item = AsyncMock(side_effect=lambda item, **kw: played.append(item.title))
    svc.add_to_queue = AsyncMock()
    _set_queue_support(monkeypatch, False)

    out = await svc.execute_tool("play_playlist", {"name": "Workout"})
    assert played == ["One"]
    svc.add_to_queue.assert_not_awaited()
    assert out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_music_service_playlists.py -v`
Expected: FAIL — `KeyError: Unknown tool: play_playlist`

- [ ] **Step 3: Implement `play_playlist`**

Add `import random` to the top of `music.py`, then add:

```python
    async def play_playlist(
        self,
        name: str,
        shuffle: bool | None = None,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        initiator: str = "user",
    ) -> str:
        """Play one of the caller's playlists, optionally shuffled.

        Mirrors ``start_station``: play the first item (which clears the
        queue and starts playback), then enqueue the rest when the
        backend supports a queue. A backend with no queue plays the
        first track rather than erroring.

        ``shuffle`` overrides the playlist's stored default in either
        direction; passing ``None`` uses the stored default. Shuffling
        reorders a *copy* — the stored order is never mutated.
        """
        user = get_current_user()
        playlist = await self._require_playlists().get_by_name(user.user_id, name)
        if not playlist.items:
            return f"{playlist.name} is empty — add some tracks first."

        items = list(playlist.items)
        if playlist.shuffle if shuffle is None else shuffle:
            random.shuffle(items)

        total = len(items)
        first, rest = items[0], items[1:]
        await self.play_item(
            first,
            speaker_names=speaker_names,
            volume=volume,
            initiator=initiator,
        )
        queued = 1

        if rest and self.supports_queue:
            for item in rest:
                try:
                    await self.add_to_queue(
                        item,
                        speaker_names=speaker_names,
                        initiator=initiator,
                    )
                except (RuntimeError, NotImplementedError):
                    logger.exception(
                        "Failed to enqueue playlist track %s; skipping",
                        item.title,
                    )
                    continue
                queued += 1

        if queued < total:
            return (
                f"Playing {playlist.name} — queued {queued} of {total} "
                f"({total - queued} unavailable)."
            )
        return f"Playing {playlist.name} — {queued} tracks."

    async def _tool_play_playlist(self, arguments: dict[str, Any]) -> str:
        raw_shuffle = arguments.get("shuffle")
        raw_speakers = arguments.get("speaker_names")
        try:
            return await self.play_playlist(
                str(arguments.get("name", "")),
                shuffle=None if raw_shuffle is None else bool(raw_shuffle),
                speaker_names=(
                    [str(s) for s in raw_speakers]
                    if isinstance(raw_speakers, list)
                    else None
                ),
                volume=(
                    int(arguments["volume"])
                    if arguments.get("volume") is not None
                    else None
                ),
            )
        except (PlaylistNotFoundError, PlaylistError) as exc:
            return str(exc)
```

Add the tool definition to `get_tools()`:

```python
            ToolDefinition(
                name="play_playlist",
                description=(
                    "Play one of your Gilbert playlists. Set 'shuffle' to "
                    "override the playlist's own default play order."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of your playlist.",
                    ),
                    ToolParameter(
                        name="shuffle",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "Shuffle for this play only. Omit to use the "
                            "playlist's stored default."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="speaker_names",
                        type=ToolParameterType.ARRAY,
                        description="Speakers to play on. Omit for the default.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume 0-100.",
                        required=False,
                    ),
                ],
                required_role="user",
                slash_group="music",
                slash_command="playlist-play",
                slash_help=(
                    "Play a playlist: /music playlist-play <name> [shuffle=true]"
                ),
            ),
```

Add the dispatch case:

```python
            case "play_playlist":
                return await self._tool_play_playlist(arguments)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_music_service_playlists.py -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/music.py tests/integration/test_music_service_playlists.py
git commit -m "feat(music): play_playlist with ephemeral + stored shuffle"
```

---

### Task 6: Fix the stale Spotify scope comment, docs, and full verification

**Files:**
- Modify: `std-plugins/sonos/sonos_music.py:66-74`
- Modify: `README.md` (music tools list, if one exists)
- Test: full suite

**Interfaces:**
- Consumes: everything above. Produces nothing new.

- [ ] **Step 1: Fix the stale scope comment**

`sonos_music.py:69` claims the scopes "Intentionally omit `*-modify-*` scopes" but line 72 already requests `user-library-modify`. The scope string is correct; the comment is wrong. Replace the comment block above `_DEFAULT_SCOPES` with:

```python
# Scopes Gilbert requests at link time. Covers search (no scope needed),
# user library (for "my liked songs" and liking a track), and reading
# user-owned playlists (for "my playlists"). ``user-read-private`` gets
# the user's display name + country for UX niceties.
#
# Playlist *write* scopes (``playlist-modify-public`` /
# ``playlist-modify-private``) are deliberately NOT requested: Gilbert
# never edits playlists in the upstream service. Gilbert-owned playlists
# (see core/services/music_playlists.py) are stored locally and per-user,
# which avoids forcing every operator to re-authorize and avoids letting
# one household member edit another's Spotify account.
```

- [ ] **Step 2: Update the README if it lists music commands**

Run: `grep -n "music search\|/music" README.md`

If a music slash-command list exists, add the eight new commands to it:

```
/music playlist-create <name>          Create a playlist
/music playlist-list                   List your playlists
/music playlist-show <name>            Show a playlist's tracks
/music playlist-add <name> [query]     Add a track (no query = current track)
/music playlist-remove <name> <pos>    Remove a track by position
/music playlist-update <name> ...      Rename / set default shuffle
/music playlist-delete <name>          Delete a playlist
/music playlist-play <name> [shuffle]  Play a playlist
```

If no such list exists, skip this step — do not invent a new README section.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest`
Expected: PASS, no regressions.

- [ ] **Step 4: Lint and type-check**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/`
Expected: clean. Fix anything reported.

- [ ] **Step 5: Run the architecture audit**

Invoke the `validate-architecture` skill and fix anything it flags. Expected clean:
- `core/services/music_playlists.py` imports only from `interfaces/`.
- `music.py` uses the `StorageProvider` protocol, not a concrete storage class.
- All eight tools declare `required_role`.
- Every playlist read/write is scoped by `owner_user_id`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs(music): correct Spotify scope comment; document playlist commands"
```

---

## Self-Review

**Spec coverage:**
- Playlist entity + `music_playlists` collection + `owner_user_id` index → Task 1
- Snapshot semantics → Task 1 (`_item_to_dict`), asserted in Task 2
- Name uniqueness per owner, case-insensitive → Tasks 1, 2
- Eight tools, all `required_role="user"` → Tasks 3, 4, 5
- `add_to_playlist` 3-way resolution incl. now-playing → Task 4
- `update_playlist` = rename + shuffle default → Tasks 2, 3
- Shuffle: stored default + ephemeral override in both directions → Tasks 2, 5
- Playback via `play_item` + `add_to_queue`, degrading without a queue → Task 5
- Partial-resolve skip with count → Task 5
- Per-user isolation on every op → Tasks 1, 2, 3, 4 (explicit deny tests)
- Events `playlist_created/updated/deleted` → Task 3
- `list_playlists` vs `my_playlists` disambiguation → Task 3
- Stale scope comment → Task 6
- Out of scope (reordering, sharing, SPA UI, speaker shuffle, upstream writes) → not implemented anywhere ✓

**Type consistency:** `PlaylistStore` methods are named identically in every task (`create`, `list_for`, `get_by_name`, `add_item`, `remove_at`, `update`, `delete`). `Playlist.items` is a `tuple[MusicItem, ...]` throughout. Positions are 1-based everywhere they appear.
