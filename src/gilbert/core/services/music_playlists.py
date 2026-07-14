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
