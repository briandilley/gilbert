"""Tests for MusicService — search, metadata, playback integration."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.services.music import MusicService
from gilbert.interfaces.music import (
    AlbumInfo,
    ArtistInfo,
    MusicBackend,
    PlaylistDetail,
    PlaylistInfo,
    SearchResults,
    TrackInfo,
)
from gilbert.interfaces.service import ServiceResolver


# --- Stub backend ---

_ARTIST = ArtistInfo(artist_id="ar-1", name="Test Artist", external_url="https://example.com/artist")
_ALBUM = AlbumInfo(
    album_id="al-1",
    name="Test Album",
    artists=[_ARTIST],
    album_art_url="https://example.com/art.jpg",
    release_date="2024-01-15",
    total_tracks=12,
    external_url="https://example.com/album",
)
_TRACKS = [
    TrackInfo(
        track_id="tr-1",
        name="First Song",
        artists=[_ARTIST],
        album=_ALBUM,
        duration_seconds=213.5,
        track_number=1,
        uri="spotify:track:tr-1",
        external_url="https://example.com/track/1",
        explicit=False,
    ),
    TrackInfo(
        track_id="tr-2",
        name="Second Song",
        artists=[_ARTIST],
        album=_ALBUM,
        duration_seconds=186.2,
        track_number=2,
        uri="spotify:track:tr-2",
        external_url="https://example.com/track/2",
        preview_url="https://example.com/preview/2",
        explicit=True,
    ),
]
_PLAYLIST = PlaylistInfo(
    playlist_id="pl-1",
    name="Test Playlist",
    description="A test playlist",
    owner="testuser",
    track_count=2,
    external_url="https://example.com/playlist",
    image_url="https://example.com/playlist.jpg",
)


class StubMusicBackend(MusicBackend):
    """In-memory music backend for testing."""

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.init_config: dict[str, object] = {}

    async def initialize(self, config: dict[str, object]) -> None:
        self.init_config = config
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def search(self, query: str, *, limit: int = 10) -> SearchResults:
        return SearchResults(tracks=list(_TRACKS), albums=[_ALBUM], playlists=[_PLAYLIST])

    async def get_track(self, track_id: str) -> TrackInfo | None:
        for t in _TRACKS:
            if t.track_id == track_id:
                return t
        return None

    async def get_album(self, album_id: str) -> AlbumInfo | None:
        if album_id == _ALBUM.album_id:
            return _ALBUM
        return None

    async def get_album_tracks(self, album_id: str) -> list[TrackInfo]:
        if album_id == _ALBUM.album_id:
            return list(_TRACKS)
        return []

    async def get_playlist(self, playlist_id: str) -> PlaylistDetail | None:
        if playlist_id == _PLAYLIST.playlist_id:
            return PlaylistDetail(playlist=_PLAYLIST, tracks=list(_TRACKS))
        return None

    async def get_playable_uri(self, track_id: str) -> str:
        return f"spotify:track:{track_id}"


# --- Fixtures ---


@pytest.fixture
def stub_backend() -> StubMusicBackend:
    return StubMusicBackend()


@pytest.fixture
def resolver() -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)
    mock.get_capability.return_value = None
    mock.require_capability.side_effect = LookupError("not available")
    return mock


@pytest.fixture
def service(stub_backend: StubMusicBackend) -> MusicService:
    svc = MusicService(stub_backend)
    svc._config = {"client_id": "test-client-id", "client_secret": "test-client-secret"}
    return svc


# --- Service info ---


def test_service_info(service: MusicService) -> None:
    info = service.service_info()
    assert info.name == "music"
    assert "music" in info.capabilities
    assert "ai_tools" in info.capabilities


# --- Lifecycle ---


async def test_start_initializes_backend(
    service: MusicService, stub_backend: StubMusicBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    assert stub_backend.initialized
    assert stub_backend.init_config["client_id"] == "test-client-id"
    assert stub_backend.init_config["client_secret"] == "test-client-secret"


async def test_stop_closes_backend(
    service: MusicService, stub_backend: StubMusicBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.stop()
    assert stub_backend.closed


# --- Tool provider ---


def test_tool_provider_name(service: MusicService) -> None:
    assert service.tool_provider_name == "music"


def test_get_tools(service: MusicService) -> None:
    tools = service.get_tools()
    names = [t.name for t in tools]
    assert "search_music" in names
    assert "get_track_info" in names
    assert "get_album_info" in names
    assert "get_playlist" in names
    assert "play_track" in names


# --- Search ---


async def test_tool_search(service: MusicService, resolver: ServiceResolver) -> None:
    await service.start(resolver)
    result = await service.execute_tool("search_music", {"query": "test song"})
    parsed = json.loads(result)

    assert len(parsed["tracks"]) == 2
    assert parsed["tracks"][0]["name"] == "First Song"
    assert parsed["tracks"][0]["duration_seconds"] == 213.5
    assert parsed["tracks"][0]["artists"][0]["name"] == "Test Artist"

    assert len(parsed["albums"]) == 1
    assert parsed["albums"][0]["name"] == "Test Album"
    assert parsed["albums"][0]["album_art_url"] == "https://example.com/art.jpg"

    assert len(parsed["playlists"]) == 1
    assert parsed["playlists"][0]["name"] == "Test Playlist"


# --- Track info ---


async def test_tool_get_track(service: MusicService, resolver: ServiceResolver) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_track_info", {"track_id": "tr-1"})
    parsed = json.loads(result)

    assert parsed["name"] == "First Song"
    assert parsed["duration_seconds"] == 213.5
    assert parsed["track_number"] == 1
    assert parsed["uri"] == "spotify:track:tr-1"
    assert parsed["album"]["name"] == "Test Album"
    assert parsed["album"]["album_art_url"] == "https://example.com/art.jpg"


async def test_tool_get_track_not_found(
    service: MusicService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_track_info", {"track_id": "nonexistent"})
    parsed = json.loads(result)
    assert "error" in parsed


async def test_tool_get_track_with_preview(
    service: MusicService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_track_info", {"track_id": "tr-2"})
    parsed = json.loads(result)
    assert parsed["preview_url"] == "https://example.com/preview/2"
    assert parsed["explicit"] is True


# --- Album info ---


async def test_tool_get_album(service: MusicService, resolver: ServiceResolver) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_album_info", {"album_id": "al-1"})
    parsed = json.loads(result)

    assert parsed["name"] == "Test Album"
    assert parsed["release_date"] == "2024-01-15"
    assert parsed["total_tracks"] == 12
    assert len(parsed["tracks"]) == 2


async def test_tool_get_album_without_tracks(
    service: MusicService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool(
        "get_album_info", {"album_id": "al-1", "include_tracks": False}
    )
    parsed = json.loads(result)
    assert parsed["name"] == "Test Album"
    assert "tracks" not in parsed


async def test_tool_get_album_not_found(
    service: MusicService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_album_info", {"album_id": "nonexistent"})
    parsed = json.loads(result)
    assert "error" in parsed


# --- Playlist ---


async def test_tool_get_playlist(service: MusicService, resolver: ServiceResolver) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_playlist", {"playlist_id": "pl-1"})
    parsed = json.loads(result)

    assert parsed["name"] == "Test Playlist"
    assert parsed["owner"] == "testuser"
    assert len(parsed["tracks"]) == 2


async def test_tool_get_playlist_not_found(
    service: MusicService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_playlist", {"playlist_id": "nonexistent"})
    parsed = json.loads(result)
    assert "error" in parsed


# --- Play track ---


async def test_tool_play_track_no_speakers(
    service: MusicService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("play_track", {"track_id": "tr-1"})
    parsed = json.loads(result)
    assert "error" in parsed  # No speaker service available


async def test_tool_play_track_with_speakers(
    stub_backend: StubMusicBackend,
) -> None:
    """Test play_track when speaker service is available."""
    from gilbert.core.services.speaker import SpeakerService
    from gilbert.interfaces.speaker import PlayRequest, PlaybackState, SpeakerBackend, SpeakerInfo

    # Create a minimal speaker backend mock
    mock_speaker_backend = AsyncMock(spec=SpeakerBackend)
    mock_speaker_backend.supports_grouping = False
    mock_speaker_backend.list_speakers = AsyncMock(return_value=[
        SpeakerInfo(speaker_id="s1", name="Kitchen", ip_address="10.0.0.1"),
    ])
    mock_speaker_backend.play_uri = AsyncMock()

    speaker_svc = MagicMock(spec=SpeakerService)
    speaker_svc.play_on_speakers = AsyncMock()
    speaker_svc.backend = mock_speaker_backend

    resolver = AsyncMock(spec=ServiceResolver)

    def get_cap(cap: str) -> Any:
        if cap == "speaker_control":
            return speaker_svc
        return None

    resolver.get_capability.side_effect = get_cap
    resolver.require_capability.side_effect = LookupError("not available")

    service = MusicService(stub_backend)
    await service.start(resolver)

    result = await service.execute_tool("play_track", {
        "track_id": "tr-1",
        "speakers": ["Kitchen"],
        "position_seconds": 12.5,
    })
    parsed = json.loads(result)

    assert parsed["status"] == "playing"
    assert parsed["name"] == "First Song"
    assert parsed["started_at_seconds"] == 12.5

    # Verify play_on_speakers was called with correct args
    speaker_svc.play_on_speakers.assert_awaited_once()
    call_kwargs = speaker_svc.play_on_speakers.call_args[1]
    assert call_kwargs["uri"] == "spotify:track:tr-1"
    assert call_kwargs["position_seconds"] == 12.5
    assert call_kwargs["speaker_names"] == ["Kitchen"]


# --- Config parsing ---


def test_config_music_defaults() -> None:
    config = GilbertConfig.model_validate({})
    assert config.music.enabled is False
    assert config.music.backend == "spotify"
    assert config.music.settings == {}


def test_config_music_full() -> None:
    raw = {
        "music": {
            "enabled": True,
            "backend": "spotify",
            "settings": {"client_id": "xxx", "client_secret": "yyy"},
        }
    }
    config = GilbertConfig.model_validate(raw)
    assert config.music.enabled is True
    assert config.music.settings["client_id"] == "xxx"


# --- Credential type ---



# --- Unknown tool ---


async def test_tool_unknown_raises(service: MusicService, resolver: ServiceResolver) -> None:
    await service.start(resolver)
    with pytest.raises(KeyError, match="Unknown tool"):
        await service.execute_tool("nonexistent", {})
