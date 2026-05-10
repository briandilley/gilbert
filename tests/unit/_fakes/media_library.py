"""Reusable in-memory ``MediaLibraryBackend`` fake for service tests.

Concrete subclass of the ABC — this IS the thing service tests run
against, NOT a mock of internal classes (per ``CLAUDE.md``: don't mock
the thing you're supposed to be testing). Mocks are reserved for
external dependencies (``plexapi.PlexServer``, ``httpx.AsyncClient``).

Configure failure injection via ``fail_next(...)`` and ``hang_next(...)``
to simulate timeouts and per-backend errors.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from gilbert.interfaces.media_library import (
    ContinueWatchingEntry,
    MediaClient,
    MediaItem,
    MediaKind,
    MediaLibraryBackend,
    MediaPlayCommand,
    MediaSearchFilters,
    MediaSession,
    RecentlyAddedEntry,
)


class FakeMediaLibraryBackend(MediaLibraryBackend):
    """In-memory media library backend for service tests."""

    backend_name = ""  # subclasses set; do not auto-register globally

    supports_now_playing = True
    supports_resume = True
    supports_continue_watching = True
    supports_recently_added = True
    supports_seek = True
    supports_per_user = True
    supports_next_episode = True

    def __init__(
        self,
        items: list[MediaItem] | None = None,
        clients: list[MediaClient] | None = None,
        sessions: list[MediaSession] | None = None,
        recently_added_entries: list[RecentlyAddedEntry] | None = None,
        continue_watching_entries: dict[str, list[ContinueWatchingEntry]]
        | None = None,
        backend_users: list[dict[str, str]] | None = None,
        next_episode_for: dict[str, MediaItem | None] | None = None,
        libraries: list[str] | None = None,
    ) -> None:
        self._items = list(items or [])
        self._clients = list(clients or [])
        self._sessions = list(sessions or [])
        self._recent = list(recently_added_entries or [])
        self._continue = dict(continue_watching_entries or {})
        self._backend_users = list(backend_users or [])
        self._next_eps = dict(next_episode_for or {})
        self._libraries = list(libraries or [])

        # Per-method failure injection.
        self._fail_next: dict[str, list[BaseException]] = defaultdict(list)
        self._hang_next: dict[str, list[float]] = defaultdict(list)

        # Call recording for assertions.
        self.search_calls: list[
            tuple[str, MediaSearchFilters | None, str]
        ] = []
        self.continue_calls: list[tuple[str, int]] = []
        self.recently_calls: list[tuple[MediaKind | None, int, str, str]] = []
        self.play_calls: list[tuple[MediaPlayCommand, str]] = []
        self.pause_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.seek_calls: list[tuple[str, float]] = []

    # ── Failure injection API ───────────────────────────────────────

    def fail_next(self, method: str, exc: BaseException) -> None:
        self._fail_next[method].append(exc)

    def hang_next(self, method: str, duration: float) -> None:
        self._hang_next[method].append(duration)

    def _maybe_inject(self, method: str) -> None:
        if self._fail_next.get(method):
            raise self._fail_next[method].pop(0)

    async def _maybe_hang(self, method: str) -> None:
        if self._hang_next.get(method):
            duration = self._hang_next[method].pop(0)
            await asyncio.sleep(duration)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        return None

    async def close(self) -> None:
        return None

    # ── Library queries ─────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        filters: MediaSearchFilters | None = None,
        backend_user_id: str = "",
    ) -> list[MediaItem]:
        await self._maybe_hang("search")
        self._maybe_inject("search")
        self.search_calls.append((query, filters, backend_user_id))
        needle = query.lower()
        # Filter by title-substring OR (when filters.genre matches the
        # item's genre tuple, allow the item through). This mirrors
        # backend behavior where the genre filter is applied separately
        # from full-text search.
        out: list[MediaItem] = []
        genre_match = filters.genre.lower() if filters and filters.genre else ""
        for item in self._items:
            title_match = needle in item.title.lower() or not needle
            genre_hit = bool(
                genre_match
                and any(g.lower() == genre_match for g in item.genres)
            )
            if title_match or genre_hit:
                out.append(item)
        if filters and filters.kinds:
            out = [item for item in out if item.kind in filters.kinds]
        if filters and filters.unwatched_only:
            out = [item for item in out if not item.is_watched]
        if filters:
            out = out[: filters.limit]
        return out

    async def get_item(
        self, item_id: str, backend_user_id: str = ""
    ) -> MediaItem | None:
        await self._maybe_hang("get_item")
        self._maybe_inject("get_item")
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    async def list_libraries(self, backend_user_id: str = "") -> list[str]:
        return list(self._libraries)

    async def list_backend_users(self) -> list[dict[str, str]]:
        return list(self._backend_users)

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
        library_section: str = "",
        backend_user_id: str = "",
    ) -> list[RecentlyAddedEntry]:
        await self._maybe_hang("recently_added")
        self._maybe_inject("recently_added")
        self.recently_calls.append(
            (kind, limit, library_section, backend_user_id)
        )
        out = list(self._recent)
        if kind is not None:
            out = [e for e in out if e.item.kind == kind]
        if library_section:
            out = [
                e for e in out if e.item.library_section == library_section
            ]
        return out[:limit]

    async def continue_watching(
        self,
        *,
        backend_user_id: str = "",
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]:
        await self._maybe_hang("continue_watching")
        self._maybe_inject("continue_watching")
        self.continue_calls.append((backend_user_id, limit))
        return list(self._continue.get(backend_user_id, []))[:limit]

    async def next_episode(
        self,
        show_id: str,
        *,
        backend_user_id: str = "",
    ) -> MediaItem | None:
        await self._maybe_hang("next_episode")
        self._maybe_inject("next_episode")
        return self._next_eps.get(show_id)

    # ── Clients & sessions ──────────────────────────────────────────

    async def list_clients(self) -> list[MediaClient]:
        await self._maybe_hang("list_clients")
        self._maybe_inject("list_clients")
        return list(self._clients)

    async def now_playing(self) -> list[MediaSession]:
        await self._maybe_hang("now_playing")
        self._maybe_inject("now_playing")
        return list(self._sessions)

    # ── Playback ────────────────────────────────────────────────────

    async def play(
        self,
        command: MediaPlayCommand,
        *,
        backend_user_id: str = "",
    ) -> None:
        await self._maybe_hang("play")
        self._maybe_inject("play")
        self.play_calls.append((command, backend_user_id))

    async def pause(self, client_id: str) -> None:
        self._maybe_inject("pause")
        self.pause_calls.append(client_id)

    async def resume(self, client_id: str) -> None:
        self._maybe_inject("resume")
        self.resume_calls.append(client_id)

    async def stop(self, client_id: str) -> None:
        self._maybe_inject("stop")
        self.stop_calls.append(client_id)

    async def seek(self, client_id: str, position_seconds: float) -> None:
        self._maybe_inject("seek")
        self.seek_calls.append((client_id, position_seconds))

    # ── Test helpers ────────────────────────────────────────────────

    def set_sessions(self, sessions: list[MediaSession]) -> None:
        self._sessions = list(sessions)

    def set_recently_added(
        self, entries: list[RecentlyAddedEntry]
    ) -> None:
        self._recent = list(entries)

    def add_item(self, item: MediaItem) -> None:
        self._items.append(item)

    def set_continue_watching(
        self, backend_user_id: str, entries: list[ContinueWatchingEntry]
    ) -> None:
        self._continue[backend_user_id] = list(entries)


__all__ = ["FakeMediaLibraryBackend"]
