"""Tests for BrowserSpeakerBackend — bus-event playback into a user's SPA tab."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.context import set_current_conversation_id, set_current_user
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.speaker import PlayRequest

# --- Stub bus + provider ---


class StubBus(EventBus):
    """Captures every published event so tests can assert against them."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def subscribe(self, event_type: str, handler: Any) -> Any:
        return lambda: None

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        return lambda: None

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class StubBusProvider:
    """Satisfies ``EventBusProvider`` for ``set_event_bus_provider``."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> EventBus:
        return self._bus


def _alice() -> UserContext:
    return UserContext(
        user_id="user-alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    )


def _bob() -> UserContext:
    return UserContext(
        user_id="user-bob",
        email="bob@example.com",
        display_name="Bob",
        roles=frozenset({"user"}),
    )


@pytest.fixture
def backend() -> BrowserSpeakerBackend:
    return BrowserSpeakerBackend()


@pytest.fixture
def bus() -> StubBus:
    return StubBus()


# --- Tests ---


@pytest.mark.asyncio
async def test_set_event_bus_provider_accepts_provider(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    # No assertion error from the warning path means the bus is wired.
    set_current_user(_alice())
    await backend.play_uri(PlayRequest(uri="http://x/y.mp3"))
    assert len(bus.published) == 1


@pytest.mark.asyncio
async def test_set_event_bus_provider_ignores_non_provider(
    backend: BrowserSpeakerBackend,
) -> None:
    backend.set_event_bus_provider(object())
    await backend.initialize({})
    set_current_user(_alice())
    with pytest.raises(RuntimeError, match="no event bus"):
        await backend.play_uri(PlayRequest(uri="http://x/y.mp3"))


@pytest.mark.asyncio
async def test_list_speakers_returns_one_entry_per_active_user(
    backend: BrowserSpeakerBackend,
) -> None:
    await backend.initialize({"display_name": "Headphones"})

    backend.activate(conn_id="conn-1", user_id="user-alice", display_name="Alice")
    speakers = await backend.list_speakers()
    assert len(speakers) == 1
    assert speakers[0].speaker_id == "user-alice"
    assert speakers[0].name == "Alice's Browser"

    backend.activate(conn_id="conn-2", user_id="user-bob", display_name="Bob")
    speakers = await backend.list_speakers()
    assert len(speakers) == 2
    assert {s.speaker_id for s in speakers} == {"user-alice", "user-bob"}
    assert {s.name for s in speakers} == {"Alice's Browser", "Bob's Browser"}


@pytest.mark.asyncio
async def test_list_speakers_empty_for_system_user(
    backend: BrowserSpeakerBackend,
) -> None:
    await backend.initialize({})
    set_current_user(UserContext.SYSTEM)
    assert await backend.list_speakers() == []


@pytest.mark.asyncio
async def test_get_speaker_returns_info_for_active_registration(
    backend: BrowserSpeakerBackend,
) -> None:
    await backend.initialize({})
    backend.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    info = await backend.get_speaker("browser:user-alice")
    assert info is not None
    assert info.speaker_id == "user-alice"
    # Non-activated user returns None.
    assert (await backend.get_speaker("browser:user-bob")) is None


@pytest.mark.asyncio
async def test_get_speaker_returns_none_after_deactivation(
    backend: BrowserSpeakerBackend,
) -> None:
    await backend.initialize({})
    backend.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    assert await backend.get_speaker("browser:user-alice") is not None
    backend.deactivate(conn_id="c1")
    assert await backend.get_speaker("browser:user-alice") is None


@pytest.mark.asyncio
async def test_play_uri_publishes_event_scoped_to_caller(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(_alice())
    set_current_conversation_id("conv-42")

    await backend.play_uri(
        PlayRequest(
            uri="http://host/clip.mp3",
            volume=55,
            title="hello",
            announce=True,
        )
    )

    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.event_type == "speaker.browser.play"
    assert event.data["user_id"] == "user-alice"
    assert event.data["url"] == "http://host/clip.mp3"
    assert event.data["volume"] == 55
    assert event.data["conversation_id"] == "conv-42"
    assert event.data["announce"] is True


@pytest.mark.asyncio
async def test_play_uri_does_not_enforce_cross_user_at_backend_layer(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    """Cross-user RBAC is enforced by SpeakerService._check_browser_target_permissions,
    NOT by the backend itself.  The backend publishes to whatever target_user_id is
    passed in speaker_ids; the service is the gating layer.
    """
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(_alice())
    # Backend does not raise — service-level enforcement is tested separately
    # in test_speaker_service_browser_permissions.py.
    await backend.play_uri(
        PlayRequest(uri="http://x", speaker_ids=["user-bob"])
    )
    assert len(bus.published) == 1
    assert bus.published[0].data["user_id"] == "user-bob"


@pytest.mark.asyncio
async def test_play_uri_volume_clamps_to_0_100(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(_alice())
    await backend.play_uri(PlayRequest(uri="http://x", volume=999))
    await backend.play_uri(PlayRequest(uri="http://x", volume=-5))
    assert bus.published[0].data["volume"] == 100
    assert bus.published[1].data["volume"] == 0


@pytest.mark.asyncio
async def test_play_uri_defaults_to_configured_volume(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({"default_volume": 35})
    set_current_user(_alice())
    await backend.play_uri(PlayRequest(uri="http://x"))
    assert bus.published[0].data["volume"] == 35


@pytest.mark.asyncio
async def test_stop_publishes_event_only_for_current_user(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(_alice())

    # No speaker_ids → stop our own browser.
    await backend.stop()
    assert bus.published[-1].event_type == "speaker.browser.stop"
    assert bus.published[-1].data["user_id"] == "user-alice"

    bus.published.clear()
    # Foreign speaker id → no-op (don't emit a stop for someone else).
    await backend.stop(speaker_ids=["browser:user-bob"])
    assert bus.published == []


@pytest.mark.asyncio
async def test_stop_silent_for_system_user(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(UserContext.SYSTEM)
    await backend.stop()
    assert bus.published == []


@pytest.mark.asyncio
async def test_get_volume_returns_configured_default(
    backend: BrowserSpeakerBackend,
) -> None:
    await backend.initialize({"default_volume": 42})
    assert await backend.get_volume("browser:user-alice") == 42


@pytest.mark.asyncio
async def test_set_volume_is_no_op(backend: BrowserSpeakerBackend) -> None:
    await backend.initialize({"default_volume": 70})
    await backend.set_volume("browser:user-alice", 5)
    # No-op — get_volume still returns the configured default.
    assert await backend.get_volume("browser:user-alice") == 70


def test_backend_registers_under_browser_name() -> None:
    import gilbert.integrations.browser_speaker  # noqa: F401
    from gilbert.interfaces.speaker import SpeakerBackend

    assert "browser" in SpeakerBackend.registered_backends()
    assert (
        SpeakerBackend.registered_backends()["browser"] is BrowserSpeakerBackend
    )


# --- to_browser_url: scheme/host stripping for Gilbert-minted URLs ---
# (Module-level helper in ``interfaces/speaker.py``; both BrowserSpeakerBackend
# and the SpeakerService fan-out path use it. Tests live here because
# the browser case is where the behavior matters most.)


def test_to_browser_url_strips_scheme_and_host_for_output_urls() -> None:
    from gilbert.interfaces.speaker import to_browser_url
    url = "http://192.168.1.42:8000/output/speaker/announce-abc.mp3"
    assert to_browser_url(url) == "/output/speaker/announce-abc.mp3"


def test_to_browser_url_preserves_query_string() -> None:
    from gilbert.interfaces.speaker import to_browser_url
    url = "http://x:8000/output/speaker/foo.mp3?ttl=600"
    assert to_browser_url(url) == "/output/speaker/foo.mp3?ttl=600"


def test_to_browser_url_leaves_external_urls_alone() -> None:
    from gilbert.interfaces.speaker import to_browser_url
    url = "https://podcast.example.com/episode-12.mp3"
    assert to_browser_url(url) == url


def test_to_browser_url_leaves_already_relative_urls_alone() -> None:
    from gilbert.interfaces.speaker import to_browser_url
    assert to_browser_url("/output/speaker/foo.mp3") == "/output/speaker/foo.mp3"


def test_to_browser_url_leaves_non_output_paths_alone() -> None:
    from gilbert.interfaces.speaker import to_browser_url
    url = "http://example.com/api/some-file.mp3"
    assert to_browser_url(url) == url


def test_to_browser_url_handles_empty_string() -> None:
    from gilbert.interfaces.speaker import to_browser_url
    assert to_browser_url("") == ""


@pytest.mark.asyncio
async def test_play_uri_publishes_event_for_native_id_target(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    """speaker_ids at backend boundary are already-stripped native ids
    (the ``browser:`` prefix is stripped by SpeakerService._route_ids).
    play_uri must resolve them as user ids, not look for a stale prefix.
    """
    backend.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})

    # Caller is SYSTEM (matching the scheduler's AI fire path).
    # Target IS Alice (post-strip native id, no "browser:" prefix).
    set_current_user(UserContext.SYSTEM)

    await backend.play_uri(
        PlayRequest(
            uri="http://example.com/x.mp3",
            speaker_ids=["user-alice"],  # native, post-strip
            volume=80,
            title="Test",
        )
    )

    # Event should have published with user_id="user-alice"
    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.event_type == "speaker.browser.play"
    assert event.data["user_id"] == "user-alice"


@pytest.mark.asyncio
async def test_play_uri_allows_system_caller_to_target_any_user(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    """SYSTEM (and admin) callers can target any user's browser.
    Service-level RBAC is the gate; the backend no longer double-enforces it.
    """
    backend.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(UserContext.SYSTEM)

    # Should NOT raise PermissionError — SYSTEM bypasses the backend gate.
    await backend.play_uri(
        PlayRequest(uri="http://x/clip.mp3", speaker_ids=["user-alice"], volume=80)
    )
    assert len(bus.published) == 1


@pytest.mark.asyncio
async def test_play_uri_rewrites_gilbert_minted_url_to_relative(
    backend: BrowserSpeakerBackend, bus: StubBus
) -> None:
    backend.set_event_bus_provider(StubBusProvider(bus))
    await backend.initialize({})
    set_current_user(_alice())
    set_current_conversation_id("conv-rewrite")

    # This is what SpeakerService._audio_url() actually produces.
    await backend.play_uri(
        PlayRequest(
            uri="http://192.168.1.42:8000/output/speaker/announce-xyz.mp3",
            volume=80,
            title="rewrite me",
        )
    )

    assert len(bus.published) == 1
    assert (
        bus.published[0].data["url"]
        == "/output/speaker/announce-xyz.mp3"
    )
