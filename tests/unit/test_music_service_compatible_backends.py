"""Tests for MusicBackend.compatible_speaker_backends classmethod
and MusicService._validate_compatible_speakers enforcement."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from gilbert.interfaces.music import MusicBackend, MusicItemKind, MusicSearchUnavailableError


class _DemoMusic(MusicBackend):
    """Minimal concrete subclass for default-behavior testing."""
    backend_name = "demo"

    async def initialize(self, config: dict) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_favorites(self):
        return []

    async def list_playlists(self):
        return []

    async def search(self, query: str, *, kind: MusicItemKind = MusicItemKind.TRACK, limit: int = 10):
        return []

    async def resolve_playable(self, item):
        pass


def test_default_compatible_speaker_backends_is_wildcard():
    assert _DemoMusic.compatible_speaker_backends() == frozenset({"*"})


# --- helpers for service-level tests ---


class _SonosOnlyMusic(MusicBackend):
    """Music backend that only works with sonos speakers."""

    backend_name = "_sonos_only_test"

    async def initialize(self, config: dict) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_favorites(self):
        return []

    async def list_playlists(self):
        return []

    async def search(self, query: str, *, kind: MusicItemKind = MusicItemKind.TRACK, limit: int = 10):
        return []

    async def resolve_playable(self, item):
        from gilbert.interfaces.music import Playable
        return Playable(uri=item.uri or "x-sonos:track", title=item.title)

    @classmethod
    def compatible_speaker_backends(cls) -> frozenset[str]:
        return frozenset({"sonos"})


class _WildcardMusic(MusicBackend):
    """Music backend that works with any speaker backend."""

    backend_name = "_wildcard_test"

    async def initialize(self, config: dict) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_favorites(self):
        return []

    async def list_playlists(self):
        return []

    async def search(self, query: str, *, kind: MusicItemKind = MusicItemKind.TRACK, limit: int = 10):
        return []

    async def resolve_playable(self, item):
        from gilbert.interfaces.music import Playable
        return Playable(uri=item.uri or "http://stream/track", title=item.title)

    @classmethod
    def compatible_speaker_backends(cls) -> frozenset[str]:
        return frozenset({"*"})


def _make_speaker_svc(resolved: dict[str, str]) -> MagicMock:
    """Return a speaker service mock whose resolve_names returns ``resolved``."""
    from gilbert.interfaces.speaker import SpeakerProvider

    svc = MagicMock(spec=SpeakerProvider)
    svc.resolve_names = AsyncMock(return_value=resolved)
    svc.play_on_speakers = AsyncMock()
    svc.enqueue_on_speakers = AsyncMock()
    svc.play_queue_on_speakers = AsyncMock(return_value=True)
    return svc


def _make_music_service(backend: MusicBackend, speaker_svc: MagicMock):
    """Build a MusicService directly wired to the given backend and speaker svc."""
    from gilbert.core.services.music import MusicService

    svc = MusicService()
    svc._backend = backend
    svc._enabled = True
    svc._speaker_svc = speaker_svc
    return svc


# --- Tests ---


@pytest.mark.asyncio
async def test_music_service_rejects_incompatible_speaker_target():
    """MusicService.play_item should raise when target speaker's backend isn't compatible."""
    from gilbert.interfaces.music import MusicItem

    backend = _SonosOnlyMusic()
    speaker_svc = _make_speaker_svc({"My Browser": "browser:abc123"})
    svc = _make_music_service(backend, speaker_svc)

    item = MusicItem(
        id="track-1",
        title="Test Track",
        kind=MusicItemKind.TRACK,
        uri="x-sonos:track",
    )
    with pytest.raises(MusicSearchUnavailableError, match="can't play"):
        await svc.play_item(item, speaker_names=["My Browser"])


@pytest.mark.asyncio
async def test_music_service_wildcard_compatibility_accepts_any_speaker():
    """MusicService with wildcard music backend accepts any speaker target."""
    from gilbert.interfaces.music import MusicItem, Playable

    backend = _WildcardMusic()
    speaker_svc = _make_speaker_svc({"My Browser": "browser:abc123"})
    svc = _make_music_service(backend, speaker_svc)

    item = MusicItem(
        id="track-1",
        title="Test Track",
        kind=MusicItemKind.TRACK,
        uri="http://stream/track",
    )
    # Should not raise — wildcard backend accepts browser speakers
    result = await svc.play_item(item, speaker_names=["My Browser"])
    assert isinstance(result, Playable)
    speaker_svc.play_on_speakers.assert_awaited_once()


@pytest.mark.asyncio
async def test_music_service_accepts_compatible_speaker():
    """MusicService.play_item succeeds when the speaker backend is in compatible set."""
    from gilbert.interfaces.music import MusicItem, Playable

    backend = _SonosOnlyMusic()
    speaker_svc = _make_speaker_svc({"Kitchen": "sonos:RINCON_abc"})
    svc = _make_music_service(backend, speaker_svc)

    item = MusicItem(
        id="track-1",
        title="Test Track",
        kind=MusicItemKind.TRACK,
        uri="x-sonos:track",
    )
    result = await svc.play_item(item, speaker_names=["Kitchen"])
    assert isinstance(result, Playable)
    speaker_svc.play_on_speakers.assert_awaited_once()


@pytest.mark.asyncio
async def test_music_service_empty_speaker_names_skips_validation():
    """Empty or None speaker_names bypasses validation (no speakers to check)."""
    from gilbert.interfaces.music import MusicItem, Playable

    backend = _SonosOnlyMusic()
    # resolve_names won't be called so anything in the mock is fine
    speaker_svc = _make_speaker_svc({})
    svc = _make_music_service(backend, speaker_svc)

    item = MusicItem(
        id="track-1",
        title="Test Track",
        kind=MusicItemKind.TRACK,
        uri="x-sonos:track",
    )
    # None speaker_names — should not raise
    result = await svc.play_item(item, speaker_names=None)
    assert isinstance(result, Playable)


@pytest.mark.asyncio
async def test_music_service_rejects_incompatible_speaker_in_add_to_queue():
    """add_to_queue also validates speaker compatibility."""
    from gilbert.interfaces.music import MusicItem

    backend = _SonosOnlyMusic()
    backend.supports_queue = True
    speaker_svc = _make_speaker_svc({"My Browser": "browser:abc123"})
    svc = _make_music_service(backend, speaker_svc)

    item = MusicItem(
        id="track-1",
        title="Test Track",
        kind=MusicItemKind.TRACK,
        uri="x-sonos:track",
    )
    with pytest.raises(MusicSearchUnavailableError, match="can't play"):
        await svc.add_to_queue(item, speaker_names=["My Browser"])


@pytest.mark.asyncio
async def test_music_service_rejects_incompatible_speaker_in_play_queue():
    """play_queue also validates speaker compatibility."""
    backend = _SonosOnlyMusic()
    backend.supports_queue = True
    speaker_svc = _make_speaker_svc({"My Browser": "browser:abc123"})
    svc = _make_music_service(backend, speaker_svc)

    with pytest.raises(MusicSearchUnavailableError, match="can't play"):
        await svc.play_queue(speaker_names=["My Browser"])


@pytest.mark.asyncio
async def test_music_service_rejects_incompatible_speaker_in_start_station():
    """start_station also validates speaker compatibility (via play_item delegation)."""
    from gilbert.interfaces.music import MusicItem

    backend = _SonosOnlyMusic()
    backend.supports_stations = True

    async def fake_start_station(seed, limit=30):
        return [MusicItem(id="t1", title="Track 1", kind=MusicItemKind.TRACK, uri="x-sonos:t1")]

    backend.start_station = fake_start_station  # type: ignore[method-assign]
    speaker_svc = _make_speaker_svc({"My Browser": "browser:abc123"})
    svc = _make_music_service(backend, speaker_svc)

    with pytest.raises(MusicSearchUnavailableError, match="can't play"):
        await svc.start_station(seed="some artist", speaker_names=["My Browser"])


@pytest.mark.asyncio
async def test_validate_compatible_speakers_returns_resolved_mapping():
    """_validate_compatible_speakers returns the resolved {name: id} dict."""
    backend = _SonosOnlyMusic()
    speaker_svc = _make_speaker_svc({"Kitchen": "sonos:RINCON_abc"})
    svc = _make_music_service(backend, speaker_svc)

    result = await svc._validate_compatible_speakers(["Kitchen"])
    assert result == {"Kitchen": "sonos:RINCON_abc"}


@pytest.mark.asyncio
async def test_validate_compatible_speakers_empty_input_returns_empty():
    """_validate_compatible_speakers with empty list returns empty dict."""
    backend = _SonosOnlyMusic()
    speaker_svc = _make_speaker_svc({})
    svc = _make_music_service(backend, speaker_svc)

    result = await svc._validate_compatible_speakers([])
    assert result == {}

    result = await svc._validate_compatible_speakers(None)
    assert result == {}


# --- Tests for supports_loop filtering by compatible backends ---


def test_supports_loop_false_when_no_compatible_speaker_loaded():
    """A Sonos music backend with only a browser speaker loaded reports supports_loop=False."""
    from gilbert.core.services.music import MusicService
    from gilbert.interfaces.speaker import SpeakerProvider

    music_svc = MusicService()
    music_svc._backend = MagicMock(supports_loop=True)
    music_svc._backend.compatible_speaker_backends.return_value = frozenset({"sonos"})

    # Speaker service with a browser backend that supports_repeat=True; sonos not loaded
    fake_speaker_svc = MagicMock(spec=SpeakerProvider)
    fake_browser_backend = MagicMock(supports_repeat=True)
    fake_speaker_svc.backends = {"browser": fake_browser_backend}
    music_svc._speaker_svc = fake_speaker_svc

    assert music_svc.supports_loop is False


def test_supports_loop_true_when_compatible_speaker_with_repeat_loaded():
    """A Sonos music backend with a Sonos speaker backend loaded reports supports_loop=True."""
    from gilbert.core.services.music import MusicService
    from gilbert.interfaces.speaker import SpeakerProvider

    music_svc = MusicService()
    music_svc._backend = MagicMock(supports_loop=True)
    music_svc._backend.compatible_speaker_backends.return_value = frozenset({"sonos"})

    fake_speaker_svc = MagicMock(spec=SpeakerProvider)
    fake_sonos_backend = MagicMock(supports_repeat=True)
    fake_speaker_svc.backends = {"sonos": fake_sonos_backend}
    music_svc._speaker_svc = fake_speaker_svc

    assert music_svc.supports_loop is True


def test_supports_loop_wildcard_compat_any_repeat_capable_backend_qualifies():
    """A wildcard music backend reports supports_loop=True if any loaded speaker backend supports repeat."""
    from gilbert.core.services.music import MusicService
    from gilbert.interfaces.speaker import SpeakerProvider

    music_svc = MusicService()
    music_svc._backend = MagicMock(supports_loop=True)
    music_svc._backend.compatible_speaker_backends.return_value = frozenset({"*"})

    fake_speaker_svc = MagicMock(spec=SpeakerProvider)
    fake_browser_backend = MagicMock(supports_repeat=True)
    fake_speaker_svc.backends = {"browser": fake_browser_backend}
    music_svc._speaker_svc = fake_speaker_svc

    assert music_svc.supports_loop is True


# --- Regression: magic alias must flow through compatibility validation ---


@pytest.mark.asyncio
async def test_music_service_rejects_my_browser_alias_when_music_is_sonos_only():
    """The 'my browser' alias must resolve through compatibility validation,
    not silently pass through. Otherwise a Sonos-only music backend would
    dispatch Spotify URIs to a browser tab that can't play them.

    This exercises the real SpeakerService.resolve_names (not a mock) to
    confirm the alias path is handled — the regression being tested is that
    resolve_names used to return {} for magic aliases, causing validation to
    pass vacuously.
    """
    from typing import Any
    from unittest.mock import AsyncMock

    from gilbert.core.services.music import MusicService
    from gilbert.core.services.speaker import SpeakerService
    from gilbert.core.services.storage import StorageService
    from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
    from gilbert.interfaces.auth import UserContext
    from gilbert.interfaces.context import set_current_user
    from gilbert.interfaces.music import MusicItem
    from gilbert.interfaces.service import ServiceResolver
    from gilbert.interfaces.storage import StorageBackend

    class _MinimalStorage(StorageBackend):
        async def initialize(self) -> None: pass
        async def close(self) -> None: pass
        async def put(self, c, i, d): pass
        async def get(self, c, i): return None
        async def delete(self, c, i): pass
        async def exists(self, c, i): return False
        async def query(self, q): return []
        async def delete_query(self, q): return 0
        async def count(self, q): return 0
        async def list_collections(self): return []
        async def drop_collection(self, c): pass
        async def ensure_index(self, i): pass
        async def list_indexes(self, c): return []
        async def ensure_foreign_key(self, fk): pass
        async def list_foreign_keys(self, c): return []

    storage_svc = StorageService(_MinimalStorage())

    resolver = MagicMock(spec=ServiceResolver)
    resolver.get_capability.side_effect = lambda cap: storage_svc if cap == "entity_storage" else None
    resolver.require_capability.side_effect = lambda cap: (
        storage_svc if cap == "entity_storage" else (_ for _ in ()).throw(LookupError(cap))
    )

    # Build a real SpeakerService with only a browser backend loaded
    speaker_svc = SpeakerService()
    browser_backend = BrowserSpeakerBackend()
    await browser_backend.initialize({})
    speaker_svc._backends = {"browser": browser_backend}
    speaker_svc._enabled = True
    await speaker_svc.start(resolver)

    # Wire MusicService with sonos-only backend and the real speaker service
    music_svc = MusicService()
    music_svc._backend = _SonosOnlyMusic()
    music_svc._enabled = True
    music_svc._speaker_svc = speaker_svc

    set_current_user(UserContext(user_id="alice", display_name="Alice", email="", roles=frozenset({"user"})))

    item = MusicItem(id="track-1", title="Test Track", kind=MusicItemKind.TRACK, uri="x-sonos:track")
    with pytest.raises(MusicSearchUnavailableError, match="can't play"):
        await music_svc.play_item(item, speaker_names=["my browser"])
