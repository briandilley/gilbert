"""Tests for MusicService — browse, search, and speaker playback integration."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.services.music import MusicService
from gilbert.interfaces.configuration import (
    BackendActionProvider,
    ConfigActionProvider,
)
from gilbert.interfaces.music import (
    MusicBackend,
    MusicItem,
    MusicItemKind,
    MusicSearchUnavailableError,
    Playable,
)
from gilbert.interfaces.service import ServiceResolver

# --- Stub backend ---

_FAVORITES = [
    MusicItem(
        id="fav-1",
        title="Horizon",
        kind=MusicItemKind.TRACK,
        subtitle="Parkway Drive",
        uri="x-sonos-spotify:spotify%3atrack%3aabc",
        service="Sonos Favorites",
    ),
    MusicItem(
        id="fav-2",
        title="Morning Jazz",
        kind=MusicItemKind.STATION,
        uri="",
        didl_meta="<DIDL-Lite>station</DIDL-Lite>",
        service="Sonos Favorites",
    ),
]

_PLAYLISTS = [
    MusicItem(
        id="SQ:1",
        title="BBQ Mix",
        kind=MusicItemKind.PLAYLIST,
        uri="file:///jffs/settings/savedqueues.rsq#1",
        service="Sonos Playlists",
    ),
    MusicItem(
        id="SQ:2",
        title="Workout",
        kind=MusicItemKind.PLAYLIST,
        uri="file:///jffs/settings/savedqueues.rsq#2",
        service="Sonos Playlists",
    ),
]

_SEARCH_RESULTS = [
    MusicItem(
        id="opaque-track-1",
        title="Black Dog",
        kind=MusicItemKind.TRACK,
        subtitle="Led Zeppelin",
        uri="",  # Search results need resolution
        service="Spotify",
    ),
]


class StubMusicBackend(MusicBackend):
    """In-memory music backend for testing."""

    backend_name = "_stub"

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.init_config: dict[str, object] = {}
        self.search_should_fail: bool = False
        self.search_calls: list[tuple[str, MusicItemKind, int]] = []

    async def initialize(self, config: dict[str, object]) -> None:
        self.init_config = config
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def list_favorites(self) -> list[MusicItem]:
        return list(_FAVORITES)

    async def list_playlists(self) -> list[MusicItem]:
        return list(_PLAYLISTS)

    async def search(
        self,
        query: str,
        *,
        kind: MusicItemKind = MusicItemKind.TRACK,
        limit: int = 10,
    ) -> list[MusicItem]:
        self.search_calls.append((query, kind, limit))
        if self.search_should_fail:
            raise MusicSearchUnavailableError("not linked")
        return list(_SEARCH_RESULTS)

    async def resolve_playable(self, item: MusicItem) -> Playable:
        # Container items (stations) have no URI but carry DIDL meta
        if item.uri or item.didl_meta:
            return Playable(
                uri=item.uri, didl_meta=item.didl_meta, title=item.title,
            )
        # Opaque search-result items need id → uri resolution
        return Playable(
            uri=f"x-sonos-spotify:spotify%3atrack%3a{item.id}",
            title=item.title,
        )


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
    svc = MusicService()
    svc._backend = stub_backend
    svc._enabled = True
    return svc


def _mock_speaker_svc() -> Any:
    """Build a speaker service mock that satisfies MusicService.play_item."""
    from gilbert.core.services.speaker import SpeakerService

    speaker_svc = MagicMock(spec=SpeakerService)
    speaker_svc.play_on_speakers = AsyncMock()
    return speaker_svc


def _resolver_with_speaker(speaker_svc: Any) -> ServiceResolver:
    resolver = AsyncMock(spec=ServiceResolver)

    def get_cap(cap: str) -> Any:
        if cap == "speaker_control":
            return speaker_svc
        return None

    resolver.get_capability.side_effect = get_cap
    resolver.require_capability.side_effect = LookupError("not available")
    return resolver


# --- Service info ---


def test_service_info(service: MusicService) -> None:
    info = service.service_info()
    assert info.name == "music"
    assert "music" in info.capabilities
    assert "ai_tools" in info.capabilities


def test_satisfies_config_action_provider() -> None:
    svc = MusicService()
    assert isinstance(svc, ConfigActionProvider)


# --- Lifecycle ---


async def test_start_disabled_without_config(resolver: ServiceResolver) -> None:
    svc = MusicService()
    await svc.start(resolver)
    assert not svc._enabled
    assert svc._backend is None


async def test_stop_closes_backend(
    service: MusicService, stub_backend: StubMusicBackend,
) -> None:
    await service.stop()
    assert stub_backend.closed


async def test_stop_when_disabled(resolver: ServiceResolver) -> None:
    svc = MusicService()
    await svc.start(resolver)
    await svc.stop()  # should not raise


# --- Tool provider ---


def test_tool_provider_name(service: MusicService) -> None:
    assert service.tool_provider_name == "music"


def test_get_tools(service: MusicService) -> None:
    tools = service.get_tools()
    names = [t.name for t in tools]
    assert set(names) == {
        "list_favorites",
        "list_playlists",
        "search_music",
        "play_music",
        "play_item",
        "now_playing",
    }


def test_all_user_facing_tools_grouped_under_music(service: MusicService) -> None:
    """Everything with a slash command lives under ``/music``. ``play_item``
    is an internal callback fired by the Play button on search results —
    it intentionally has no slash command because its required argument
    is a JSON-encoded MusicItem the user can't type by hand."""
    for tool in service.get_tools():
        if tool.slash_command:
            assert tool.slash_group == "music", tool.name


def test_play_item_has_no_slash_command(service: MusicService) -> None:
    """Regression guard: ``play_item`` is button-triggered only. If someone
    adds a slash command to it, the slash parser will choke on the JSON
    payload in the ``item`` argument."""
    tool = next(t for t in service.get_tools() if t.name == "play_item")
    assert not tool.slash_command
    assert not tool.slash_group


# --- Browse ---


async def test_tool_list_favorites(service: MusicService) -> None:
    result = await service.execute_tool("list_favorites", {})
    parsed = json.loads(result)
    assert len(parsed["favorites"]) == 2
    titles = [f["title"] for f in parsed["favorites"]]
    assert "Horizon" in titles
    assert "Morning Jazz" in titles
    # Station has its kind preserved
    station = next(f for f in parsed["favorites"] if f["title"] == "Morning Jazz")
    assert station["kind"] == "station"


async def test_tool_list_playlists(service: MusicService) -> None:
    result = await service.execute_tool("list_playlists", {})
    parsed = json.loads(result)
    assert len(parsed["playlists"]) == 2
    assert parsed["playlists"][0]["title"] == "BBQ Mix"
    assert parsed["playlists"][0]["kind"] == "playlist"


# --- Search ---


async def test_tool_search(
    service: MusicService, stub_backend: StubMusicBackend,
) -> None:
    from gilbert.interfaces.ui import ToolOutput

    result = await service.execute_tool(
        "search_music", {"query": "led zeppelin", "kind": "tracks", "limit": 5},
    )
    # Returns ToolOutput so the AI still sees the JSON via .text AND the
    # chat frontend gets interactive UI blocks via .ui_blocks.
    assert isinstance(result, ToolOutput)
    parsed = json.loads(result.text)
    assert parsed["kind"] == "track"
    assert len(parsed["results"]) == 1
    assert parsed["results"][0]["title"] == "Black Dog"
    assert parsed["results"][0]["subtitle"] == "Led Zeppelin"
    # Verify the backend was called with the parsed kind
    assert stub_backend.search_calls == [("led zeppelin", MusicItemKind.TRACK, 5)]


async def test_tool_search_unavailable(
    service: MusicService, stub_backend: StubMusicBackend,
) -> None:
    stub_backend.search_should_fail = True
    # Error path still returns a plain JSON string — no UI on failure.
    result = await service.execute_tool("search_music", {"query": "anything"})
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert "error" in parsed


async def test_tool_search_returns_ui_block_per_result(
    service: MusicService, stub_backend: StubMusicBackend,
) -> None:
    """Regression guard for the search-and-play flow.

    Each search result must render as its own UIBlock carrying an
    inline Play button whose value is a JSON-encoded MusicItem that
    ``play_item`` can rehydrate without another search round-trip.
    This is what distinguishes the interactive flow from the old
    JSON-only response."""
    from gilbert.interfaces.ui import ToolOutput

    # Extend the stub to return three tracks for this test
    stub_backend.search_calls.clear()
    many = [
        MusicItem(
            id=f"opaque-{i}",
            title=f"Song {i}",
            kind=MusicItemKind.TRACK,
            subtitle=f"Artist {i}",
            album_art_url=f"https://art/{i}.jpg" if i % 2 == 0 else "",
            service="Spotify",
        )
        for i in range(3)
    ]

    async def fake_search(
        query: str, *, kind: MusicItemKind = MusicItemKind.TRACK, limit: int = 10,
    ) -> list[MusicItem]:
        return many

    stub_backend.search = fake_search  # type: ignore[method-assign]

    result = await service.execute_tool("search_music", {"query": "anything"})
    assert isinstance(result, ToolOutput)
    assert len(result.ui_blocks) == 3

    for block, item in zip(result.ui_blocks, many, strict=True):
        # The block routes back to the play_item tool (not the search tool)
        assert block.tool_name == "play_item"
        assert block.title == item.title

        # Only even-indexed items had artwork in the fixture
        has_image = any(el.type == "image" for el in block.elements)
        assert has_image is bool(item.album_art_url), item.title

        # Buttons element carries a single Play option whose value is
        # the JSON-encoded item. Decoding it must round-trip to the
        # original MusicItem shape.
        button_el = next(el for el in block.elements if el.type == "buttons")
        assert button_el.name == "item"
        assert len(button_el.options) == 1
        assert button_el.options[0].label == "Play"
        decoded = json.loads(button_el.options[0].value)
        assert decoded["id"] == item.id
        assert decoded["title"] == item.title
        assert decoded["kind"] == item.kind.value


async def test_tool_search_empty_results_still_returns_tool_output(
    service: MusicService, stub_backend: StubMusicBackend,
) -> None:
    """No results → no UI blocks, but still a ToolOutput so the chat
    frontend can tell "found nothing" apart from "search failed"."""
    from gilbert.interfaces.ui import ToolOutput

    async def fake_search(*args: Any, **kwargs: Any) -> list[MusicItem]:
        return []

    stub_backend.search = fake_search  # type: ignore[method-assign]
    result = await service.execute_tool("search_music", {"query": "nothing"})
    assert isinstance(result, ToolOutput)
    assert result.ui_blocks == []
    parsed = json.loads(result.text)
    assert parsed["results"] == []
    assert "error" not in parsed  # Empty is not an error — "not found" is a valid result


async def test_tool_search_default_kind_is_tracks(
    service: MusicService, stub_backend: StubMusicBackend,
) -> None:
    await service.execute_tool("search_music", {"query": "foo"})
    assert stub_backend.search_calls[0][1] == MusicItemKind.TRACK


# --- Play ---


async def test_tool_play_requires_speaker_service(service: MusicService) -> None:
    result = await service.execute_tool("play_music", {"title": "Horizon"})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "Speaker service" in parsed["error"]


async def test_tool_play_favorite_by_title(
    stub_backend: StubMusicBackend,
) -> None:
    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    result = await service.execute_tool("play_music", {
        "title": "Horizon",
        "speakers": ["Kitchen"],
    })
    parsed = json.loads(result)

    assert parsed["status"] == "playing"
    assert parsed["title"] == "Horizon"
    assert parsed["source"] == "favorites"
    # Direct-URI favorite skips search entirely
    assert stub_backend.search_calls == []

    speaker_svc.play_on_speakers.assert_awaited_once()
    call_kwargs = speaker_svc.play_on_speakers.call_args[1]
    assert call_kwargs["uri"].startswith("x-sonos-spotify:")
    assert call_kwargs["speaker_names"] == ["Kitchen"]


async def test_tool_play_playlist_fallback(
    stub_backend: StubMusicBackend,
) -> None:
    """When no favorite matches, falls through to playlists."""
    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    result = await service.execute_tool("play_music", {"title": "Workout"})
    parsed = json.loads(result)

    assert parsed["status"] == "playing"
    assert parsed["source"] == "playlists"
    assert parsed["kind"] == "playlist"


async def test_tool_play_search_fallback(
    stub_backend: StubMusicBackend,
) -> None:
    """When no favorite or playlist matches, runs a fresh search."""
    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    result = await service.execute_tool("play_music", {"title": "Black Dog"})
    parsed = json.loads(result)

    assert parsed["status"] == "playing"
    assert parsed["source"] == "search"
    assert parsed["title"] == "Black Dog"
    # Search results lack a direct URI, so resolve_playable constructs one
    call_kwargs = speaker_svc.play_on_speakers.call_args[1]
    assert call_kwargs["uri"].startswith("x-sonos-spotify:")


async def test_tool_play_no_match_returns_error(
    stub_backend: StubMusicBackend,
) -> None:
    """When nothing matches anywhere, reports the sources tried."""
    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    # Empty the search response so all three sources fail
    _SEARCH_RESULTS.clear()
    try:
        result = await service.execute_tool("play_music", {"title": "xyzzy"})
        parsed = json.loads(result)
        assert "error" in parsed
        assert parsed["sources_tried"] == ["favorites", "playlists", "search"]
    finally:
        _SEARCH_RESULTS.append(MusicItem(
            id="opaque-track-1",
            title="Black Dog",
            kind=MusicItemKind.TRACK,
            subtitle="Led Zeppelin",
            uri="",
            service="Spotify",
        ))


async def test_tool_play_restricted_source(
    stub_backend: StubMusicBackend,
) -> None:
    """``source=favorites`` means only favorites are consulted."""
    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    result = await service.execute_tool("play_music", {
        "title": "Workout",  # Only in playlists
        "source": "favorites",
    })
    parsed = json.loads(result)
    assert "error" in parsed
    assert parsed["sources_tried"] == ["favorites"]


async def test_tool_play_carries_didl_meta(
    stub_backend: StubMusicBackend,
) -> None:
    """Stations carry DIDL metadata through to the speaker service."""
    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    await service.execute_tool("play_music", {"title": "Morning Jazz"})
    call_kwargs = speaker_svc.play_on_speakers.call_args[1]
    assert call_kwargs["didl_meta"] == "<DIDL-Lite>station</DIDL-Lite>"


# --- play_item (button-triggered) ---


async def test_item_payload_round_trips() -> None:
    """The payload helper is invertible: encode → decode → same fields.

    Every field that ``resolve_playable`` might need has to survive the
    round-trip, or the Play button would silently lose information that
    wasn't captured in the bare ``id``."""
    from gilbert.core.services.music import _item_from_payload, _item_to_payload

    original = MusicItem(
        id="opaque-1",
        title="Blues Rock Mix",
        kind=MusicItemKind.PLAYLIST,
        subtitle="Spotify",
        uri="x-rincon-cpcontainer:foo",
        didl_meta="<DIDL-Lite>container</DIDL-Lite>",
        album_art_url="https://art/1.jpg",
        duration_seconds=279.0,
        service="Spotify",
    )

    decoded = _item_from_payload(_item_to_payload(original))
    assert decoded == original


async def test_item_payload_rejects_garbage() -> None:
    """Bad JSON → ValueError, not a silent miss that eats the click."""
    from gilbert.core.services.music import _item_from_payload

    with pytest.raises(ValueError, match="Malformed"):
        _item_from_payload("not json")
    with pytest.raises(ValueError, match="JSON object"):
        _item_from_payload("[1, 2, 3]")
    with pytest.raises(ValueError, match="Unknown music item kind"):
        _item_from_payload('{"kind": "bogus"}')


async def test_tool_play_item_round_trips_from_button_value(
    stub_backend: StubMusicBackend,
) -> None:
    """End-to-end: a search result's Play button payload handed back
    to ``play_item`` reaches the speaker service with the right URI.

    This is the happy-path for the search-and-click flow — the search
    tool returns a UI block whose button value is the JSON below, and
    when the user clicks it the chat frontend submits the form as
    ``{"item": "<json>"}`` to the ``play_item`` tool."""
    from gilbert.core.services.music import _item_to_payload

    speaker_svc = _mock_speaker_svc()
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    item = MusicItem(
        id="opaque-search-result",
        title="Black Dog",
        kind=MusicItemKind.TRACK,
        subtitle="Led Zeppelin",
        uri="",
        service="Spotify",
    )
    payload = _item_to_payload(item)

    result = await service.execute_tool(
        "play_item",
        {"item": payload, "speakers": ["Kitchen"], "volume": 40},
    )
    # play_item returns a plain str status dict (no UI blocks on success —
    # the user already clicked, there's nothing else to show them).
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["status"] == "playing"
    assert parsed["title"] == "Black Dog"
    assert parsed["source"] == "search"

    speaker_svc.play_on_speakers.assert_awaited_once()
    call_kwargs = speaker_svc.play_on_speakers.call_args[1]
    # The stub resolver turned the bare id into an x-sonos-spotify URI
    assert "opaque-search-result" in call_kwargs["uri"]
    assert call_kwargs["speaker_names"] == ["Kitchen"]
    assert call_kwargs["volume"] == 40


async def test_tool_play_item_missing_payload_returns_error(
    service: MusicService,
) -> None:
    result = await service.execute_tool("play_item", {})
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert "error" in parsed
    assert "Missing" in parsed["error"]


async def test_tool_play_item_malformed_payload_returns_error(
    service: MusicService,
) -> None:
    """A garbled button value should produce a readable error instead
    of crashing the tool dispatcher."""
    result = await service.execute_tool("play_item", {"item": "not json"})
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert "error" in parsed


# --- Now playing ---


async def test_now_playing_requires_speaker_service(service: MusicService) -> None:
    result = await service.execute_tool("now_playing", {})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "Speaker service" in parsed["error"]


async def test_now_playing_delegates_to_speaker(
    stub_backend: StubMusicBackend,
) -> None:
    from gilbert.interfaces.speaker import NowPlaying, PlaybackState

    speaker_svc = _mock_speaker_svc()
    speaker_svc.get_now_playing = AsyncMock(return_value=NowPlaying(
        state=PlaybackState.PLAYING,
        title="Black Dog",
        artist="Led Zeppelin",
        album="Led Zeppelin IV",
        album_art_url="https://example.com/art.jpg",
        uri="spotify:track:abc",
        duration_seconds=296.0,
        position_seconds=42.5,
    ))
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    result = await service.execute_tool("now_playing", {"speaker": "Kitchen"})
    parsed = json.loads(result)

    assert parsed["state"] == "playing"
    assert parsed["is_playing"] is True
    assert parsed["title"] == "Black Dog"
    assert parsed["artist"] == "Led Zeppelin"
    speaker_svc.get_now_playing.assert_awaited_once_with("Kitchen")


async def test_now_playing_auto_pick(
    stub_backend: StubMusicBackend,
) -> None:
    from gilbert.interfaces.speaker import NowPlaying, PlaybackState

    speaker_svc = _mock_speaker_svc()
    speaker_svc.get_now_playing = AsyncMock(
        return_value=NowPlaying(state=PlaybackState.STOPPED),
    )
    resolver = _resolver_with_speaker(speaker_svc)

    service = MusicService()
    service._backend = stub_backend
    service._enabled = True
    await service.start(resolver)

    result = await service.execute_tool("now_playing", {})
    parsed = json.loads(result)
    assert parsed["state"] == "stopped"
    assert parsed["is_playing"] is False
    speaker_svc.get_now_playing.assert_awaited_once_with(None)


def test_now_playing_tool_exposed(service: MusicService) -> None:
    tools = service.get_tools()
    tool = next(t for t in tools if t.name == "now_playing")
    assert tool.slash_group == "music"
    assert tool.slash_command == "now"
    assert tool.required_role == "everyone"


# --- ConfigActionProvider forwarding ---


class _ActionableBackend(MusicBackend):
    """Backend that implements BackendActionProvider for wiring tests."""

    backend_name = "_actionable"

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict]] = []

    async def initialize(self, config: dict) -> None: ...
    async def close(self) -> None: ...
    async def list_favorites(self) -> list[MusicItem]:
        return []
    async def list_playlists(self) -> list[MusicItem]:
        return []
    async def search(self, query: str, *, kind: Any = None, limit: int = 10) -> list[MusicItem]:
        return []
    async def resolve_playable(self, item: MusicItem) -> Playable:
        return Playable(uri="")

    @classmethod
    def backend_actions(cls) -> list:
        from gilbert.interfaces.configuration import ConfigAction

        return [ConfigAction(key="probe", label="Probe", description="Test probe")]

    async def invoke_backend_action(self, key: str, payload: dict) -> Any:
        from gilbert.interfaces.configuration import ConfigActionResult

        self.invocations.append((key, payload))
        return ConfigActionResult(status="ok", message=f"probed {key}")


async def test_config_actions_forwarded_from_backend() -> None:
    svc = MusicService()
    svc._backend = _ActionableBackend()
    actions = svc.config_actions()
    # The service now returns actions from EVERY registered backend so
    # the UI can display the right set when the user changes the
    # backend dropdown without saving. The _ActionableBackend's 'probe'
    # should appear, tagged with its backend name.
    probe_actions = [a for a in actions if a.key == "probe"]
    assert len(probe_actions) == 1
    assert probe_actions[0].backend_action is True
    assert probe_actions[0].backend == "_actionable"


async def test_invoke_config_action_forwarded_to_backend() -> None:
    backend = _ActionableBackend()
    assert isinstance(backend, BackendActionProvider)
    svc = MusicService()
    svc._backend = backend
    result = await svc.invoke_config_action("probe", {"foo": "bar"})
    assert result.status == "ok"
    assert backend.invocations == [("probe", {"foo": "bar"})]


async def test_invoke_config_action_no_backend() -> None:
    svc = MusicService()
    result = await svc.invoke_config_action("anything", {})
    assert result.status == "error"


# --- Config parsing ---


def test_config_music_defaults() -> None:
    config = GilbertConfig.model_validate({})
    assert config.music.enabled is False
    assert config.music.backend == "sonos"
    assert config.music.settings == {}


def test_config_music_full() -> None:
    raw = {
        "music": {
            "enabled": True,
            "backend": "sonos",
            "settings": {"preferred_service": "Spotify"},
        }
    }
    config = GilbertConfig.model_validate(raw)
    assert config.music.enabled is True
    assert config.music.settings["preferred_service"] == "Spotify"


# --- Unknown tool ---


async def test_tool_unknown_raises(service: MusicService) -> None:
    with pytest.raises(KeyError, match="Unknown tool"):
        await service.execute_tool("nonexistent", {})
