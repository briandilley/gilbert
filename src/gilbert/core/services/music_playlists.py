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
