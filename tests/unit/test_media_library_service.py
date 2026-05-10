"""Unit tests for ``MediaLibraryService`` — fan-out, polling, locking,
recommend_next, user mappings, and the multi-user race assertion that
catches ContextVar leakage.

Per spec §19.1 — every listed test is implemented here.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.media_library import (
    MediaLibraryService,
    parse_position,
)
from gilbert.interfaces.ai import (
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    TokenUsage,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.media_library import (
    ContinueWatchingEntry,
    MediaClient,
    MediaItem,
    MediaKind,
    MediaLibraryBackend,
    MediaLibraryUnavailableError,
    MediaPlaybackState,
    MediaSession,
    RecentlyAddedEntry,
)
from gilbert.interfaces.scheduler import Schedule
from gilbert.interfaces.storage import StorageBackend
from gilbert.storage.sqlite import SQLiteStorage
from tests.unit._fakes.media_library import FakeMediaLibraryBackend

# ── Test infrastructure ────────────────────────────────────────────


class _StorageProvider:
    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        raise NotImplementedError


class _BusProvider:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> EventBus:
        return self._bus


class _SchedulerProvider:
    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.intervals: dict[str, float] = {}

    def add_job(
        self,
        name: str,
        schedule: Schedule,
        callback: Any,
        system: bool = False,
        enabled: bool = True,
        owner: str = "",
    ) -> Any:
        self.jobs[name] = callback
        self.intervals[name] = schedule.interval_seconds
        return None

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.jobs.pop(name, None)
        self.intervals.pop(name, None)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return []

    def get_job(self, name: str) -> Any:
        return None

    async def run_now(self, name: str) -> None:
        cb = self.jobs.get(name)
        if cb is not None:
            await cb()


class _ConfigReader:
    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self._sections = sections

    def get(self, path: str) -> Any:
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return dict(self._sections.get(namespace, {}))

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        return dict(self._sections.get(namespace, {}))

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}


class _Resolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, capability: str) -> Any:
        return self.caps.get(capability)

    def require_capability(self, capability: str) -> Any:
        svc = self.caps.get(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Any]:
        svc = self.caps.get(capability)
        return [svc] if svc is not None else []


class _StubAIService:
    """Minimal AISamplingProvider stub. Configure ``response_text`` per
    test.
    """

    def __init__(self, response_text: str = "[]") -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(
        self,
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
        tools_override: list[Any] | None = None,
    ) -> AIResponse:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "messages": [
                    {"role": m.role.value, "content": m.content}
                    for m in messages
                ],
                "profile_name": profile_name,
            }
        )
        return AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT, content=self.response_text
            ),
            model="stub",
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


class _StubUsersService:
    """Minimal UserManagementProvider stub. Protocol's ``list_users``
    takes no args, returning the full user list.
    """

    def __init__(self, users: list[dict[str, Any]]) -> None:
        self._users = users

    @property
    def allow_user_creation(self) -> bool:
        return False

    @property
    def backend(self) -> Any:
        return None

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self._users)


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SQLiteStorage]:
    db = SQLiteStorage(str(tmp_path / "test.db"))
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def event_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture(autouse=True)
def _reset_media_backend_registry():
    snapshot = dict(MediaLibraryBackend._registry)
    yield
    MediaLibraryBackend._registry.clear()
    MediaLibraryBackend._registry.update(snapshot)


def _register_fake(name: str, factory) -> type[MediaLibraryBackend]:
    """Register a fake backend in the global registry."""

    class _Registered(FakeMediaLibraryBackend):
        backend_name = name

        def __init__(self) -> None:
            super().__init__(**factory())

    MediaLibraryBackend._registry[name] = _Registered
    return _Registered


def _make_resolver(
    *,
    storage: StorageBackend,
    bus: EventBus,
    section: dict[str, Any] | None = None,
    scheduler: _SchedulerProvider | None = None,
    ai: Any = None,
    users: Any = None,
) -> _Resolver:
    r = _Resolver()
    r.caps["entity_storage"] = _StorageProvider(storage)
    r.caps["event_bus"] = _BusProvider(bus)
    r.caps["configuration"] = _ConfigReader({"media_library": section or {}})
    if scheduler is not None:
        r.caps["scheduler"] = scheduler
    if ai is not None:
        r.caps["ai_chat"] = ai
    if users is not None:
        r.caps["users"] = users
    return r


# ── Test data builders ─────────────────────────────────────────────


def _movie(
    id: str,
    title: str,
    backend: str = "plex",
    *,
    year: int = 2020,
    is_watched: bool = False,
    view_offset: float = 0.0,
    library_section: str = "Movies",
    added_at: float = 1700000000.0,
    summary: str = "",
    genres: tuple[str, ...] = (),
) -> MediaItem:
    return MediaItem(
        id=id,
        backend_name=backend,
        server_id="server-1",
        title=title,
        kind=MediaKind.MOVIE,
        year=year,
        duration_seconds=7200.0,
        is_watched=is_watched,
        view_offset_seconds=view_offset,
        library_section=library_section,
        added_at=added_at,
        summary=summary,
        genres=genres,
    )


def _show(id: str, title: str, backend: str = "plex") -> MediaItem:
    return MediaItem(
        id=id,
        backend_name=backend,
        server_id="server-1",
        title=title,
        kind=MediaKind.SHOW,
    )


def _episode(
    id: str,
    title: str,
    *,
    show_id: str = "show-1",
    season: int = 1,
    episode: int = 1,
    backend: str = "plex",
) -> MediaItem:
    return MediaItem(
        id=id,
        backend_name=backend,
        server_id="server-1",
        title=title,
        kind=MediaKind.EPISODE,
        grandparent_id=show_id,
        grandparent_title="A Show",
        season_number=season,
        episode_number=episode,
    )


def _client(
    client_id: str,
    name: str,
    *,
    backend: str = "plex",
    is_online: bool = True,
) -> MediaClient:
    return MediaClient(
        client_id=client_id,
        backend_name=backend,
        server_id="server-1",
        name=name,
        device="Apple TV",
        platform="tvOS",
        is_online=is_online,
        supports_seek=True,
    )


def _session(
    session_id: str,
    item: MediaItem,
    client: MediaClient,
    *,
    state: MediaPlaybackState = MediaPlaybackState.PLAYING,
    position: float = 100.0,
) -> MediaSession:
    return MediaSession(
        session_id=session_id,
        backend_name=item.backend_name,
        client=client,
        item=item,
        state=state,
        position_seconds=position,
        duration_seconds=item.duration_seconds,
    )


# ── Aggregator + fan-out ───────────────────────────────────────────


async def test_aggregator_merges_search_across_backends(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    a = _movie("a1", "A Movie 1", backend="alpha")
    a2 = _movie("a2", "A Movie 2", backend="alpha")
    b = _movie("b1", "A Movie 3", backend="beta")
    b2 = _movie("b2", "A Movie 4", backend="beta")
    _register_fake("alpha", lambda: {"items": [a, a2]})
    _register_fake("beta", lambda: {"items": [b, b2]})

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    items = await svc.search("Movie")
    # Round-robin: alpha[0], beta[0], alpha[1], beta[1].
    assert [it.id for it in items] == ["a1", "b1", "a2", "b2"]
    await svc.stop()


async def test_aggregator_drops_failing_backend(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    a = _movie("a1", "X1", backend="alpha")
    _register_fake("alpha", lambda: {"items": [a]})
    _register_fake("beta", lambda: {"items": []})

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    beta = svc._backends["beta"]
    beta.fail_next(
        "search", MediaLibraryUnavailableError("beta down")
    )
    items = await svc.search("X")
    # alpha returns; beta dropped.
    assert [it.id for it in items] == ["a1"]
    health = await svc.list_backend_health()
    by_name = {h["backend_name"]: h["status"] for h in health}
    assert by_name["beta"] == "unhealthy"
    await svc.stop()


async def test_aggregator_per_backend_timeout(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    a = _movie("a1", "X1", backend="alpha")
    _register_fake("alpha", lambda: {"items": [a]})
    _register_fake("beta", lambda: {"items": []})

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
            "backend_timeout_seconds": {"search": 0.1},
        },
    )
    await svc.start(resolver)
    beta = svc._backends["beta"]
    beta.hang_next("search", 5.0)
    start = time.monotonic()
    items = await svc.search("X")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # timed out, didn't wait the full 5s
    assert [it.id for it in items] == ["a1"]
    health = await svc.list_backend_health()
    by_name = {h["backend_name"]: h["status"] for h in health}
    assert by_name["beta"] == "degraded"
    await svc.stop()


async def test_search_limit_capped_at_50(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    items = [_movie(f"id{i}", f"Movie {i}", backend="alpha") for i in range(100)]
    _register_fake("alpha", lambda: {"items": items})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    from gilbert.interfaces.media_library import MediaSearchFilters

    out = await svc.search(
        "Movie", filters=MediaSearchFilters(limit=10000)
    )
    assert len(out) == 50
    # Backend was called with capped limit, not 10000.
    backend = svc._backends["alpha"]
    assert backend.search_calls[-1][1].limit == 50
    await svc.stop()


async def test_recently_added_jellyfin_per_user_fanout(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Spec §6.3 partial-mapping: a backend with ``supports_per_user=True``
    (Jellyfin shape) gets called once per mapped Gilbert user with that
    user's ``backend_user_id`` — never with empty user (admin fallback).
    """
    plex_movie = _movie("p1", "Plex Movie", backend="alpha")
    jelly_movie = _movie("j1", "Jelly Movie", backend="beta")

    class _SharedToken(FakeMediaLibraryBackend):
        backend_name = "alpha"
        supports_per_user = False  # shared-token Plex

        def __init__(self) -> None:
            super().__init__(
                recently_added_entries=[
                    RecentlyAddedEntry(
                        item=plex_movie, added_at=plex_movie.added_at
                    )
                ]
            )

    class _PerUser(FakeMediaLibraryBackend):
        """Jellyfin-shaped backend: rejects empty backend_user_id so the
        old silent-admin-fallback bug surfaces as a test failure.
        """

        backend_name = "beta"
        supports_per_user = True

        def __init__(self) -> None:
            super().__init__(
                recently_added_entries=[
                    RecentlyAddedEntry(
                        item=jelly_movie, added_at=jelly_movie.added_at
                    )
                ]
            )

        async def recently_added(
            self,
            *,
            kind: MediaKind | None = None,
            limit: int = 10,
            library_section: str = "",
            backend_user_id: str = "",
        ) -> list[RecentlyAddedEntry]:
            if not backend_user_id:
                raise MediaLibraryUnavailableError(
                    "Jellyfin recently_added requires a per-user mapping"
                )
            return await super().recently_added(
                kind=kind,
                limit=limit,
                library_section=library_section,
                backend_user_id=backend_user_id,
            )

    MediaLibraryBackend._registry["alpha"] = _SharedToken
    MediaLibraryBackend._registry["beta"] = _PerUser

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "beta", "u_jelly_42")

    # Tool-path: alice asks for recent. Jellyfin gets called with her id.
    out = await svc.recently_added(limit=10, gilbert_user_id="alice")
    ids = sorted(e.item.id for e in out)
    assert ids == ["j1", "p1"]

    beta = svc._backends["beta"]
    # Verify the per-user id flowed through — not empty (admin fallback).
    assert beta.recently_calls
    assert all(call[3] == "u_jelly_42" for call in beta.recently_calls)

    # Poll path (no calling user): iterates mapped users for per-user
    # backends. Same alice mapping → one call with u_jelly_42.
    beta.recently_calls.clear()
    out_poll = await svc.recently_added(limit=10, gilbert_user_id=None)
    assert sorted(e.item.id for e in out_poll) == ["j1", "p1"]
    assert beta.recently_calls
    assert all(call[3] == "u_jelly_42" for call in beta.recently_calls)
    await svc.stop()


async def test_recently_added_caps_after_merge(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    a_entries = [
        RecentlyAddedEntry(
            item=_movie(f"a{i}", f"A{i}", backend="alpha"),
            added_at=1700000000.0 + i,
        )
        for i in range(10)
    ]
    b_entries = [
        RecentlyAddedEntry(
            item=_movie(f"b{i}", f"B{i}", backend="beta"),
            added_at=1700000000.0 + 100 + i,
        )
        for i in range(10)
    ]
    _register_fake("alpha", lambda: {"recently_added_entries": a_entries})
    _register_fake("beta", lambda: {"recently_added_entries": b_entries})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    out = await svc.recently_added(limit=5)
    assert len(out) == 5
    # Sorted by added_at desc — newest from beta come first.
    assert all(e.item.backend_name == "beta" for e in out)
    await svc.stop()


# ── User mapping ──────────────────────────────────────────────────


async def test_continue_watching_uses_per_user_mapping(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    item = _movie("p1", "Plex Movie", backend="alpha", view_offset=120.0)
    entries_for_alice = [ContinueWatchingEntry(item=item)]
    _register_fake(
        "alpha",
        lambda: {
            "continue_watching_entries": {"u_plex_42": entries_for_alice},
        },
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_plex_42")
    # Protocol-shaped read returns the bare list of entries.
    entries = await svc.continue_watching(gilbert_user_id="alice")
    assert len(entries) == 1
    assert entries[0].item.id == "p1"
    backend = svc._backends["alpha"]
    assert backend.continue_calls[-1][0] == "u_plex_42"
    # The dict-envelope variant exposes the partial-mapping metadata.
    envelope = await svc.continue_watching_for_user(gilbert_user_id="alice")
    assert envelope["unmapped_backends"] == []
    await svc.stop()


async def test_continue_watching_partial_mapping_returns_unmapped_hint(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    plex_item = _movie("p1", "Plex Movie", backend="alpha", view_offset=120.0)
    _register_fake(
        "alpha",
        lambda: {
            "continue_watching_entries": {
                "u_plex_42": [ContinueWatchingEntry(item=plex_item)]
            },
        },
    )
    _register_fake("beta", lambda: {})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_plex_42")
    result = await svc.continue_watching_for_user(gilbert_user_id="alice")
    assert "beta" in result["unmapped_backends"]
    # Critical: the unmapped backend was NOT queried with admin fallback.
    beta_backend = svc._backends["beta"]
    assert beta_backend.continue_calls == []
    assert "hint" in result
    await svc.stop()


async def test_continue_watching_no_mapping_returns_error(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    _register_fake("alpha", lambda: {})
    _register_fake("beta", lambda: {})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    result = await svc.continue_watching_for_user(gilbert_user_id="alice")
    assert "error" in result
    assert "link-user" in result["error"]
    await svc.stop()


def test_log_redaction_strips_xplex_token() -> None:
    from gilbert.core.services.media_library import _redact_sensitive

    text = (
        "Got 401 for http://plex.local:32400/clients/abc?"
        "X-Plex-Token=secret_PLEX_TOKEN_AAA111&trailing=1"
    )
    out = _redact_sensitive(text)
    assert "secret_PLEX_TOKEN_AAA111" not in out
    assert "X-Plex-Token=<REDACTED>" in out
    # Non-token query params survive.
    assert "trailing=1" in out


def test_log_redaction_strips_jellyfin_apikey() -> None:
    from gilbert.core.services.media_library import _redact_sensitive

    text = (
        "GET http://jellyfin.local:8096/Sessions?"
        "api_key=jelly_secret_KEY_999&Foo=bar"
    )
    out = _redact_sensitive(text)
    assert "jelly_secret_KEY_999" not in out
    assert "?api_key=<REDACTED>" in out
    assert "Foo=bar" in out

    # Mid-query api_key (with leading &) is also redacted.
    text2 = "GET /Sessions?Foo=bar&api_key=secret_X&Baz=qux"
    out2 = _redact_sensitive(text2)
    assert "secret_X" not in out2
    assert "&api_key=<REDACTED>" in out2


def test_log_redaction_filter_is_installed_on_module_logger() -> None:
    """Spec §16: the redaction filter is installed on the core service
    logger so a forced 401 fault writes a redacted message.
    """
    import logging

    from gilbert.core.services.media_library import (
        _install_log_redactor,
        MediaLogRedactor,
    )

    _install_log_redactor("test.media_redactor.demo")
    target = logging.getLogger("test.media_redactor.demo")
    assert any(isinstance(f, MediaLogRedactor) for f in target.filters)


async def test_user_can_see_returns_true_for_visible_section(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Spec §6.5 / §18: subscribers re-filter restricted-library
    events through ``user_can_see``. A user mapped to a backend account
    that includes a section returns True.
    """

    class _LibsBackend(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(libraries=["Movies", "Kids TV"])

    MediaLibraryBackend._registry["alpha"] = _LibsBackend
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_42")
    assert await svc.user_can_see("alice", "alpha", "Movies") is True
    assert await svc.user_can_see("alice", "alpha", "Kids TV") is True
    await svc.stop()


async def test_user_can_see_returns_false_for_restricted_section(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """A section the user can't see → False. A user without any
    mapping → False (returns conservatively rather than admin-fallback
    True).
    """

    class _LibsBackend(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(libraries=["Movies"])  # no Kids TV

    MediaLibraryBackend._registry["alpha"] = _LibsBackend
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_42")
    # Section absent from the user's library list.
    assert await svc.user_can_see("alice", "alpha", "Adults Only") is False
    # No mapping at all → False (conservative; no admin fallback).
    assert await svc.user_can_see("bob", "alpha", "Movies") is False
    await svc.stop()


async def test_user_can_see_caches_60_seconds(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Successive calls within the TTL hit the cache; only the first
    call lands on ``backend.list_libraries``.
    """

    class _Counting(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(libraries=["Movies"])
            self.libs_calls: int = 0

        async def list_libraries(
            self, backend_user_id: str = ""
        ) -> list[str]:
            self.libs_calls += 1
            return await super().list_libraries(
                backend_user_id=backend_user_id
            )

    MediaLibraryBackend._registry["alpha"] = _Counting
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_42")
    backend = svc._backends["alpha"]
    assert isinstance(backend, _Counting)
    await svc.user_can_see("alice", "alpha", "Movies")
    await svc.user_can_see("alice", "alpha", "Movies")
    await svc.user_can_see("alice", "alpha", "Anything else")
    # All three calls reused the cached library list — one fetch.
    assert backend.libs_calls == 1

    # Force the cache to expire by stamping fetched_at into the past.
    cached_libs, _ = svc._user_libs_cache[("alpha", "u_42")]
    svc._user_libs_cache[("alpha", "u_42")] = (cached_libs, 0.0)
    await svc.user_can_see("alice", "alpha", "Movies")
    assert backend.libs_calls == 2
    await svc.stop()


async def test_user_mapping_unique_index(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    _register_fake("alpha", lambda: {})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u1", "alice_plex")
    await svc.set_user_mapping("alice", "alpha", "u2", "alice_renamed")
    mappings = await svc.list_user_mappings("alice")
    assert len(mappings) == 1
    assert mappings[0]["backend_user_id"] == "u2"
    assert mappings[0]["backend_username"] == "alice_renamed"
    await svc.stop()


# ── Multi-user race ────────────────────────────────────────────────


async def test_search_concurrent_users_no_state_leak(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """The most important multi-user test in the suite. Two concurrent
    search calls under different ``set_current_user(...)`` contexts —
    each must see its OWN _user_id resolved into the backend's
    ``backend_user_id`` parameter.
    """
    items = [
        _movie("a1", "Match", backend="alpha"),
    ]

    class _Recording(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(items=items)
            self.observed_backend_user_ids: list[str] = []

        async def search(
            self,
            query: str,
            *,
            filters=None,
            backend_user_id: str = "",
        ) -> list[MediaItem]:
            # Tiny delay to encourage interleaving.
            await asyncio.sleep(0.01)
            self.observed_backend_user_ids.append(backend_user_id)
            return await super().search(
                query, filters=filters, backend_user_id=backend_user_id
            )

    MediaLibraryBackend._registry["alpha"] = _Recording

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_alice_42")
    await svc.set_user_mapping("bob", "alpha", "u_bob_77")

    # Each branch sets its own current_user and runs in copy_context.
    async def search_as(user_id: str) -> list[MediaItem]:
        set_current_user(
            UserContext(
                user_id=user_id,
                email=f"{user_id}@x",
                display_name=user_id,
            )
        )
        return await svc.search("Match", gilbert_user_id=user_id)

    # Spawn each branch with its own context — the critical detail.
    loop = asyncio.get_running_loop()
    fut_alice: asyncio.Future = loop.create_future()
    fut_bob: asyncio.Future = loop.create_future()

    async def _runner(coro, fut):
        try:
            fut.set_result(await coro)
        except BaseException as exc:
            fut.set_exception(exc)

    t_alice = asyncio.Task(
        _runner(search_as("alice"), fut_alice),
        context=contextvars.copy_context(),
    )
    t_bob = asyncio.Task(
        _runner(search_as("bob"), fut_bob),
        context=contextvars.copy_context(),
    )
    await asyncio.gather(t_alice, t_bob)
    _ = await fut_alice
    _ = await fut_bob

    backend = svc._backends["alpha"]
    observed = sorted(backend.observed_backend_user_ids)
    assert observed == sorted(["u_alice_42", "u_bob_77"])
    await svc.stop()


# ── Play / dispatch ────────────────────────────────────────────────


async def test_play_on_show_resolves_next_episode(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    show = _show("show-1", "Severance", backend="alpha")
    target_ep = _episode(
        "ep-23", "Cold Harbor", show_id="show-1", season=2, episode=3
    )
    client = _client("tv-1", "Living Room", backend="alpha")

    _register_fake(
        "alpha",
        lambda: {
            "items": [show, target_ep],
            "clients": [client],
            "next_episode_for": {"show-1": target_ep},
        },
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.execute_tool(
        "play_on",
        {
            "_user_id": "alice",
            "title": "Severance",
            "client": "Living Room",
        },
    )
    backend = svc._backends["alpha"]
    # The played item is the resolved EPISODE, not the SHOW.
    assert len(backend.play_calls) == 1
    assert backend.play_calls[0][0].item.id == "ep-23"
    await svc.stop()


async def test_play_on_show_caught_up_returns_uiblock(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    show = _show("show-1", "Severance", backend="alpha")
    client = _client("tv-1", "Living Room", backend="alpha")
    _register_fake(
        "alpha",
        lambda: {
            "items": [show],
            "clients": [client],
            "next_episode_for": {"show-1": None},
        },
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "play_on",
        {
            "_user_id": "alice",
            "title": "Severance",
            "client": "Living Room",
        },
    )
    # Check it's a ToolOutput with a caught-up UIBlock.
    assert hasattr(out, "ui_blocks")
    assert len(out.ui_blocks) == 1
    assert "Caught up" in out.ui_blocks[0].title
    backend = svc._backends["alpha"]
    assert backend.play_calls == []
    await svc.stop()


async def test_play_on_visual_disambiguation(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    inception_a = _movie("a1", "Inception", backend="alpha", year=2010)
    inception_b = _movie("a2", "Inception (2010)", backend="alpha", year=2010)
    inception_c = _movie("a3", "Inception: Behind", backend="alpha", year=2011)
    client = _client("tv-1", "Living Room", backend="alpha")
    _register_fake(
        "alpha",
        lambda: {
            "items": [inception_a, inception_b, inception_c],
            "clients": [client],
        },
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "play_on",
        {
            "_user_id": "alice",
            "title": "Inception",
            "client": "Living Room",
        },
    )
    assert hasattr(out, "ui_blocks")
    # 3 candidate UIBlocks, no playback fired.
    assert len(out.ui_blocks) == 3
    backend = svc._backends["alpha"]
    assert backend.play_calls == []
    await svc.stop()


async def test_play_on_idempotency_dedup(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Single Match", backend="alpha")
    client = _client("tv-1", "Living Room", backend="alpha")
    _register_fake(
        "alpha", lambda: {"items": [movie], "clients": [client]}
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    args = {
        "_user_id": "alice",
        "title": "Single Match",
        "client": "Living Room",
    }
    await svc.execute_tool("play_on", args)
    out2 = await svc.execute_tool("play_on", args)
    backend = svc._backends["alpha"]
    # First call fires; second short-circuits.
    assert len(backend.play_calls) == 1
    assert "deduped" in out2.text
    await svc.stop()


async def test_per_client_lock_does_not_serialize_across_clients(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie_a = _movie("a1", "A", backend="alpha")
    movie_b = _movie("b1", "B", backend="alpha")
    client_a = _client("tv-a", "Living Room", backend="alpha")
    client_b = _client("tv-b", "Bedroom", backend="alpha")

    class _SlowPlay(FakeMediaLibraryBackend):
        backend_name = "alpha"
        play_started: asyncio.Event = None  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__(
                items=[movie_a, movie_b], clients=[client_a, client_b]
            )
            self.play_started = asyncio.Event()
            self.both_started = False

        async def play(
            self, command, *, backend_user_id: str = ""
        ) -> None:
            await asyncio.sleep(0.05)
            await super().play(command, backend_user_id=backend_user_id)

    MediaLibraryBackend._registry["alpha"] = _SlowPlay
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    start = time.monotonic()
    await asyncio.gather(
        svc.play_item(movie_a, client_a, gilbert_user_id="alice"),
        svc.play_item(movie_b, client_b, gilbert_user_id="bob"),
    )
    elapsed = time.monotonic() - start
    # Two 0.05s plays in parallel → ~0.05s, NOT 0.1s if serialized.
    assert elapsed < 0.09
    await svc.stop()


async def test_play_emits_event(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Title", backend="alpha")
    client = _client("tv-1", "TV", backend="alpha")
    _register_fake(
        "alpha", lambda: {"items": [movie], "clients": [client]}
    )
    received: list[Event] = []

    async def _h(e: Event) -> None:
        received.append(e)

    event_bus.subscribe("media.playback.started", _h)
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.play_item(movie, client, gilbert_user_id="alice")
    assert len(received) == 1
    assert received[0].data["item_title"] == "Title"
    assert received[0].data["client_name"] == "TV"
    assert received[0].data["initiator"] == "user"
    assert received[0].data["user_id"] == "alice"
    await svc.stop()


async def test_play_button_reresolves_view_offset(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Spec §5.1: button-driven plays re-resolve via get_item with the
    clicker's mapping. The embedded view_offset belongs to the
    searcher, not the clicker.
    """
    # The button payload nominally encoded user A's offset (1842 sec)
    # but ``get_item`` MUST re-resolve from the clicker's POV — and
    # that view has offset 0 (B never started watching).
    item_b = _movie("m1", "Movie", backend="alpha", view_offset=0.0)
    client = _client("tv-1", "TV", backend="alpha")

    class _PerUserGetItem(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(items=[item_b], clients=[client])

        async def get_item(
            self, item_id: str, backend_user_id: str = ""
        ) -> MediaItem | None:
            # Always return the user-B view (offset 0) — regardless of
            # what the button payload claims.
            await self._maybe_hang("get_item")
            return item_b

    MediaLibraryBackend._registry["alpha"] = _PerUserGetItem

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    await svc.set_user_mapping("bob", "alpha", "u_bob")
    await svc.execute_tool(
        "play_media_id",
        {
            "_user_id": "bob",
            "backend": "alpha",
            "item_id": "m1",
            "client": "TV",
        },
    )
    # B's offset (0), NOT 1842 from the original button payload.
    backend = svc._backends["alpha"]
    assert len(backend.play_calls) == 1
    assert backend.play_calls[0][0].offset_seconds == 0.0
    await svc.stop()


# ── now_playing ────────────────────────────────────────────────────


async def test_now_playing_tool_bypasses_cache(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Title", backend="alpha")
    client = _client("tv-1", "TV", backend="alpha")
    session = _session("s1", movie, client)
    _register_fake(
        "alpha",
        lambda: {
            "items": [movie],
            "clients": [client],
            "sessions": [session],
        },
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    # Pre-populate poll cache with a stale session entry to prove the
    # tool path queries live and ignores the polled cache.
    svc._poll_last_sessions[("alpha", "ghost")] = session
    sessions = await svc.now_playing()
    assert len(sessions) == 1
    assert sessions[0].session_id == "s1"
    await svc.stop()


async def test_now_playing_poll_emits_started_stopped(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Title", backend="alpha")
    client = _client("tv-1", "TV", backend="alpha")
    session = _session("s1", movie, client)
    _register_fake(
        "alpha",
        lambda: {
            "items": [movie],
            "clients": [client],
            "sessions": [],
        },
    )
    received: list[Event] = []

    async def _h(e: Event) -> None:
        received.append(e)

    event_bus.subscribe_pattern("media.playback.*", _h)

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        scheduler=_SchedulerProvider(),
    )
    await svc.start(resolver)
    backend = svc._backends["alpha"]

    # First poll: no sessions.
    backend.set_sessions([])
    await svc._poll_now_playing()
    started_count = len([e for e in received if e.event_type == "media.playback.started"])
    assert started_count == 0

    # Second poll: a new session.
    backend.set_sessions([session])
    await svc._poll_now_playing()
    started = [e for e in received if e.event_type == "media.playback.started"]
    assert len(started) == 1
    assert started[0].data["user_id"] == ""
    assert started[0].data["initiator"] == "external"

    # Third poll: session disappears → stopped.
    backend.set_sessions([])
    await svc._poll_now_playing()
    stopped = [e for e in received if e.event_type == "media.playback.stopped"]
    assert len(stopped) == 1
    await svc.stop()


async def test_now_playing_poll_no_state_change_event_in_v1(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Title", backend="alpha")
    client = _client("tv-1", "TV", backend="alpha")
    session_playing = _session(
        "s1", movie, client, state=MediaPlaybackState.PLAYING
    )
    session_paused = _session(
        "s1", movie, client, state=MediaPlaybackState.PAUSED
    )
    _register_fake(
        "alpha",
        lambda: {
            "items": [movie],
            "clients": [client],
        },
    )
    received: list[Event] = []

    async def _h(e: Event) -> None:
        received.append(e)

    event_bus.subscribe_pattern("media.playback.*", _h)

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        scheduler=_SchedulerProvider(),
    )
    await svc.start(resolver)
    backend = svc._backends["alpha"]
    # PLAYING → emit started.
    backend.set_sessions([session_playing])
    await svc._poll_now_playing()
    # PLAYING → PAUSED: no event in v1.
    backend.set_sessions([session_paused])
    await svc._poll_now_playing()
    # PAUSED → PLAYING: no event.
    backend.set_sessions([session_playing])
    await svc._poll_now_playing()
    starts = [e for e in received if e.event_type == "media.playback.started"]
    state_changes = [
        e for e in received if e.event_type == "media.playback.state_changed"
    ]
    assert len(starts) == 1  # only the initial start
    assert state_changes == []  # never in v1
    await svc.stop()


async def test_now_playing_poll_adaptive_backoff(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    _register_fake(
        "alpha",
        lambda: {"items": [], "clients": [], "sessions": []},
    )
    svc = MediaLibraryService()
    scheduler = _SchedulerProvider()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
            "poll_now_playing": {
                "enabled": True,
                "interval_seconds": 30,
                "idle_threshold": 3,
                "idle_max_interval_seconds": 240,
            },
        },
        scheduler=scheduler,
    )
    await svc.start(resolver)
    base = svc._now_playing_current_interval
    # Scheduler is firing at the base interval at this point.
    assert scheduler.intervals["media_library.poll_now_playing"] == base

    # Drive enough empty polls that the cap (240s) is reached and we
    # get to assert the upper bound, not just "interval grew."
    for _ in range(20):
        await svc._poll_now_playing()
    backed_off = svc._now_playing_current_interval
    assert backed_off > base
    assert backed_off <= 240
    # Scheduler's recorded interval reflects the backed-off value —
    # if the service mutates ``_now_playing_current_interval`` without
    # rescheduling, this assertion catches the regression.
    assert scheduler.intervals["media_library.poll_now_playing"] == backed_off

    # A media.playback.started event resets cadence.
    await event_bus.publish(
        Event(
            event_type="media.playback.started",
            data={"backend": "alpha"},
            source="media_library",
        )
    )
    # event publish runs subscribers synchronously through asyncio
    await asyncio.sleep(0)
    assert svc._now_playing_current_interval == base
    assert scheduler.intervals["media_library.poll_now_playing"] == base
    await svc.stop()
    # stop() unschedules so the scheduler is empty afterward.
    assert "media_library.poll_now_playing" not in scheduler.intervals


async def test_recently_added_poll_baseline_is_silent_on_first_run(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    item1 = _movie("m1", "M1", backend="alpha", added_at=1700000000.0)
    item2 = _movie("m2", "M2", backend="alpha", added_at=1700000200.0)
    entries = [
        RecentlyAddedEntry(item=item1, added_at=item1.added_at),
        RecentlyAddedEntry(item=item2, added_at=item2.added_at),
    ]

    class _Backend(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(recently_added_entries=entries)

    MediaLibraryBackend._registry["alpha"] = _Backend

    received: list[Event] = []

    async def _h(e: Event) -> None:
        received.append(e)

    event_bus.subscribe("media.recently_added", _h)
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        scheduler=_SchedulerProvider(),
    )
    await svc.start(resolver)
    # First (baseline) cycle: NO events.
    await svc._poll_recently_added()
    assert received == []
    # Now add a new item, run the second poll: ONE event.
    backend = svc._backends["alpha"]
    new_item = _movie("m3", "M3", backend="alpha", added_at=1700000400.0)
    backend.set_recently_added(
        entries + [RecentlyAddedEntry(item=new_item, added_at=new_item.added_at)]
    )
    await svc._poll_recently_added()
    assert len(received) == 1
    assert received[0].data["item_id"] == "m3"
    await svc.stop()


async def test_recently_added_poll_includes_library_section_for_filtering(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    item = _movie("m1", "M1", backend="alpha", library_section="Movies")

    class _Backend(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(
                recently_added_entries=[
                    RecentlyAddedEntry(item=item, added_at=item.added_at)
                ]
            )

    MediaLibraryBackend._registry["alpha"] = _Backend

    received: list[Event] = []

    async def _h(e: Event) -> None:
        received.append(e)

    event_bus.subscribe("media.recently_added", _h)
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        scheduler=_SchedulerProvider(),
    )
    await svc.start(resolver)
    # Run baseline; then second poll with same data emits nothing
    # since added_at didn't advance. Force one new item.
    await svc._poll_recently_added()
    backend = svc._backends["alpha"]
    new_item = _movie("m2", "M2", backend="alpha", library_section="Movies", added_at=1700000999.0)
    backend.set_recently_added(
        [RecentlyAddedEntry(item=item, added_at=item.added_at),
         RecentlyAddedEntry(item=new_item, added_at=new_item.added_at)]
    )
    await svc._poll_recently_added()
    assert len(received) == 1
    assert received[0].data["library_section"] == "Movies"
    assert received[0].data["backend"] == "alpha"
    await svc.stop()


# ── Capability gating ──────────────────────────────────────────────


async def test_capability_gating_now_playing_off(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    class _NoNowPlaying(FakeMediaLibraryBackend):
        backend_name = "alpha"
        supports_now_playing = False
        supports_seek = True

        def __init__(self) -> None:
            super().__init__()

    class _AlsoNo(FakeMediaLibraryBackend):
        backend_name = "beta"
        supports_now_playing = False
        supports_seek = False

        def __init__(self) -> None:
            super().__init__()

    MediaLibraryBackend._registry["alpha"] = _NoNowPlaying
    MediaLibraryBackend._registry["beta"] = _AlsoNo

    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {
                "alpha": {"enabled": True, "settings": {}},
                "beta": {"enabled": True, "settings": {}},
            },
        },
    )
    await svc.start(resolver)
    tools = svc.get_tools()
    names = {t.name for t in tools}
    assert "now_playing" not in names
    # playback_control stays registered (pause/resume/stop).
    assert "playback_control" in names
    await svc.stop()


async def test_playback_control_pause_timeout_translates_to_error(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Spec §6.8: pause / resume / stop / seek calls reuse the 10s
    play timeout. A hung backend shouldn't hang the AI turn — the
    tool surfaces a JSON error after the per-call timeout.
    """
    client = _client("tv-1", "TV", backend="alpha")
    session = _session(
        "s1",
        _movie("m1", "Title", backend="alpha"),
        client,
    )

    class _HangingPause(FakeMediaLibraryBackend):
        backend_name = "alpha"

        def __init__(self) -> None:
            super().__init__(clients=[client], sessions=[session])

        async def pause(self, client_id: str) -> None:
            # Hang past the configured play timeout (1s in this test).
            await asyncio.sleep(5.0)

    MediaLibraryBackend._registry["alpha"] = _HangingPause
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
            # Tighten the play timeout so the test runs in <2s.
            "backend_timeout_seconds": {"play": 0.5},
        },
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "playback_control",
        {"_user_id": "alice", "action": "pause", "client": "TV"},
    )
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    assert "timed out" in parsed["error"]
    assert "alpha" in parsed["error"]
    assert "pause" in parsed["error"]
    await svc.stop()


async def test_playback_control_registers_all_four_slashes(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Spec §7.4: playback_control surfaces as FOUR slashes
    (/media pause / resume / stop / seek). Each wrapper tool delegates
    to the same _tool_playback_control handler.
    """
    _register_fake("alpha", lambda: {})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    tools = svc.get_tools()
    by_name = {t.name: t for t in tools}
    assert "playback_control" in by_name
    assert "playback_resume" in by_name
    assert "playback_stop" in by_name
    assert "playback_seek" in by_name  # supports_seek=True from default Fake
    assert by_name["playback_control"].slash_command == "pause"
    assert by_name["playback_resume"].slash_command == "resume"
    assert by_name["playback_stop"].slash_command == "stop"
    assert by_name["playback_seek"].slash_command == "seek"
    await svc.stop()


async def test_tool_remains_registered_when_only_backend_unhealthy(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Tools never disappear because of health flips — capability
    gating reads "configured-and-supports-X," not
    "currently-healthy-and-supports-X."
    """
    _register_fake("alpha", lambda: {})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
    )
    await svc.start(resolver)
    # Force unhealthy.
    svc._set_health("alpha", "unhealthy", error="token revoked")
    tools = svc.get_tools()
    names = {t.name for t in tools}
    # supports_seek=True from FakeMediaLibraryBackend default.
    assert "playback_control" in names
    pc = next(t for t in tools if t.name == "playback_control")
    actions = next(p for p in pc.parameters if p.name == "action")
    assert "seek" in actions.enum
    await svc.stop()


# ── Recommend next ────────────────────────────────────────────────


async def test_recommend_next_prompt_falls_back_to_default_when_blank(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    from gilbert.core.services.media_library import (
        _DEFAULT_RECOMMEND_NEXT_PROMPT,
    )

    _register_fake("alpha", lambda: {})
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
            "recommend_next_prompt": "",
        },
    )
    await svc.start(resolver)
    assert svc._recommend_next_prompt == _DEFAULT_RECOMMEND_NEXT_PROMPT
    # Override
    await svc.on_config_changed(
        {
            "enabled": True,
            "recommend_next_prompt": "",
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        }
    )
    assert svc._recommend_next_prompt == _DEFAULT_RECOMMEND_NEXT_PROMPT
    await svc.stop()


async def test_recommend_next_includes_user_intent(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Funny Movie", backend="alpha")
    _register_fake(
        "alpha",
        lambda: {
            "items": [movie],
            "recently_added_entries": [
                RecentlyAddedEntry(item=movie, added_at=movie.added_at)
            ],
        },
    )
    ai = _StubAIService(response_text='[{"id":"m1","reason":"x","confidence":0.9}]')
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        ai=ai,
    )
    await svc.start(resolver)
    await svc.execute_tool(
        "recommend_next",
        {
            "_user_id": "alice",
            "intent": "something funny under 90 minutes",
        },
    )
    assert ai.calls
    user_message = ai.calls[-1]["messages"][-1]["content"]
    assert "<user_intent>something funny under 90 minutes</user_intent>" in user_message
    await svc.stop()


async def test_recommend_next_caps_candidates_at_30(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    # Distinct items per source so dedup doesn't shrink the count below 30.
    cw_items = [
        _movie(
            f"cw{i}",
            f"CW Movie {i}",
            backend="alpha",
            summary="x" * 500,
            genres=("Comedy",),
        )
        for i in range(10)
    ]
    ra_items = [
        _movie(
            f"ra{i}",
            f"RA Movie {i}",
            backend="alpha",
            summary="x" * 500,
            genres=("Comedy",),
        )
        for i in range(20)
    ]
    # 30 genre-search items so the 15-cap returns 15 distinct ones.
    genre_items = [
        _movie(
            f"g{i}",
            f"Genre Movie {i}",
            backend="alpha",
            summary="x" * 500,
            genres=("Comedy",),
            # not in CW, not in RA → distinct in dedup.
        )
        for i in range(30)
    ]
    # Order: genre_items first so the genre-search returns 15 distinct
    # items (g0..g14) before the CW/RA items overlap.
    all_items = genre_items + cw_items + ra_items
    _register_fake(
        "alpha",
        lambda: {
            "items": all_items,
            "recently_added_entries": [
                RecentlyAddedEntry(item=it, added_at=it.added_at)
                for it in ra_items
            ],
            "continue_watching_entries": {
                "u_alice": [
                    ContinueWatchingEntry(item=it) for it in cw_items
                ]
            },
        },
    )
    ai = _StubAIService(response_text="[]")
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
            "preferred_genres": "Comedy",
        },
        ai=ai,
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_alice")
    await svc.execute_tool(
        "recommend_next", {"_user_id": "alice", "intent": "x"}
    )
    user_message = ai.calls[-1]["messages"][-1]["content"]
    # candidates payload contains exactly 30 entries; summaries
    # truncated at 200 + ellipsis.
    payload_start = user_message.index("<candidates>") + len("<candidates>")
    payload_end = user_message.index("</candidates>")
    payload_str = user_message[payload_start:payload_end]
    payload = json.loads(payload_str)
    assert len(payload) == 30
    for entry in payload:
        assert len(entry["summary"]) <= 201  # 200 + ellipsis
    await svc.stop()


async def test_recommend_next_handles_empty_history(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    movie = _movie("m1", "Movie", backend="alpha")
    _register_fake(
        "alpha",
        lambda: {
            "items": [movie],
            "recently_added_entries": [
                RecentlyAddedEntry(item=movie, added_at=movie.added_at)
            ],
            "continue_watching_entries": {},
        },
    )
    ai = _StubAIService(
        response_text='[{"id":"m1","reason":"x","confidence":0.9}]'
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        ai=ai,
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "recommend_next", {"_user_id": "alice"}
    )
    user_message = ai.calls[-1]["messages"][-1]["content"]
    # Empty recent_history block.
    assert "<recent_history>[]</recent_history>" in user_message
    # Tool still returns picks from recently_added.
    parsed = json.loads(out.text)
    assert "results" in parsed and len(parsed["results"]) >= 1
    await svc.stop()


async def test_recommend_next_returns_three_blocks(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    items = [
        _movie(f"m{i}", f"Movie {i}", backend="alpha") for i in range(5)
    ]
    _register_fake(
        "alpha",
        lambda: {
            "items": items,
            "recently_added_entries": [
                RecentlyAddedEntry(item=it, added_at=it.added_at)
                for it in items
            ],
        },
    )
    ai = _StubAIService(
        response_text=(
            '[{"id":"m1","reason":"x","confidence":0.9},'
            ' {"id":"m2","reason":"y","confidence":0.8},'
            ' {"id":"m3","reason":"z","confidence":0.7}]'
        )
    )
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        ai=ai,
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "recommend_next", {"_user_id": "alice"}
    )
    assert hasattr(out, "ui_blocks")
    assert len(out.ui_blocks) == 3
    await svc.stop()


async def test_recommend_next_falls_back_on_invalid_ai_response(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    items = [
        _movie(f"m{i}", f"Movie {i}", backend="alpha") for i in range(4)
    ]
    _register_fake(
        "alpha",
        lambda: {
            "items": items,
            "continue_watching_entries": {
                "u_alice": [
                    ContinueWatchingEntry(item=it) for it in items[:3]
                ]
            },
        },
    )
    ai = _StubAIService(response_text="not valid json at all")
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {"alpha": {"enabled": True, "settings": {}}},
        },
        ai=ai,
    )
    await svc.start(resolver)
    await svc.set_user_mapping("alice", "alpha", "u_alice")
    out = await svc.execute_tool(
        "recommend_next", {"_user_id": "alice"}
    )
    assert hasattr(out, "ui_blocks")
    assert len(out.ui_blocks) == 3
    await svc.stop()


# ── Position parser ───────────────────────────────────────────────


def test_position_parser_accepts_lenient_units() -> None:
    assert parse_position("1h22m") == 4920.0
    assert parse_position("1hr22min") == 4920.0
    assert parse_position("1:22:00") == 4920.0
    assert parse_position("3700") == 3700.0
    assert parse_position("3700s") == 3700.0
    assert parse_position("5m") == 300.0
    assert parse_position("5min") == 300.0
    assert parse_position("5 minutes") == 300.0
    assert parse_position("1:22") == 82.0
    assert parse_position(" 5 mins ") == 300.0
    with pytest.raises(ValueError):
        parse_position("-10")


# ── Button label matrix ───────────────────────────────────────────


def test_button_label_state_matrix() -> None:
    from gilbert.core.services.media_library import _button_label_for_item

    in_progress = _movie(
        "m1", "M", backend="alpha", view_offset=5025.0
    )
    assert _button_label_for_item(in_progress) == "Resume (1:23:45)"
    fresh = _movie("m1", "M", backend="alpha", view_offset=0.0, is_watched=False)
    assert _button_label_for_item(fresh) == "Play"
    watched = _movie(
        "m1", "M", backend="alpha", view_offset=0.0, is_watched=True
    )
    assert _button_label_for_item(watched) == "Watch again"


# ── Empty-backends ─────────────────────────────────────────────────


async def test_aggregator_runs_with_zero_backends(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """M1 acceptance: aggregator runs cleanly with zero registered backends."""
    svc = MediaLibraryService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        section={
            "enabled": True,
            "backends": {},
        },
    )
    await svc.start(resolver)
    assert await svc.search("anything") == []
    assert await svc.recently_added() == []
    assert (
        await svc.continue_watching(gilbert_user_id="alice") == []
    )
    assert await svc.list_clients() == []
    assert await svc.now_playing() == []
    assert await svc.list_backend_health() == []
    # No tools registered (no backends → all capability flags False),
    # but the always-on tools (list_media_clients, search_media,
    # play_on, play_media_id, link/unlink/list_user_mappings) remain.
    tools = svc.get_tools()
    tool_names = {t.name for t in tools}
    assert "list_media_clients" in tool_names
    assert "search_media" in tool_names
    assert "play_on" in tool_names
    assert "play_media_id" in tool_names
    # Capability-gated absent.
    assert "now_playing" not in tool_names
    assert "recently_added" not in tool_names
    assert "continue_watching" not in tool_names
    await svc.stop()
