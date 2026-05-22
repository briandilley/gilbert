"""Tests for the per-user "echo to browser" fan-out in SpeakerService.

Covers the ``_maybe_echo_to_browser`` / ``_maybe_echo_stop_to_browser``
gating + emission logic without requiring the full Service.start()
lifecycle — we instantiate the service and inject fake dependencies
directly so each branch (no registration / active registration,
primary=browser, no user, no bus) is testable in isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gilbert.interfaces.context import set_current_conversation_id, set_current_user
from gilbert.core.services.speaker import SpeakerService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend


def _alice() -> UserContext:
    return UserContext(
        user_id="user-alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    )


def _system() -> UserContext:
    return UserContext(
        user_id="system",
        email="",
        display_name="System",
        roles=frozenset(),
    )


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class _FakeBusProvider:
    def __init__(self) -> None:
        self.bus = _FakeBus()


def _make_svc(*, with_browser_backend: bool = True) -> SpeakerService:
    """Build a SpeakerService wired for echo tests.

    ``_backend_name`` is set to "sonos" so the primary backend is not
    "browser" by default.  A real ``BrowserSpeakerBackend`` is inserted
    under the ``"browser"`` key so activation lookups work exactly as
    they do in production.
    """
    s = SpeakerService()
    s._backend_name = "sonos"  # primary != browser by default
    s._primary_backend = "sonos"
    s._event_bus_provider = _FakeBusProvider()
    if with_browser_backend:
        browser = BrowserSpeakerBackend()
        s._backends["browser"] = browser
    return s


@pytest.fixture
def svc() -> SpeakerService:
    return _make_svc()


# ── _maybe_echo_to_browser ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_echo_publishes_when_user_has_active_registration(
    svc: SpeakerService,
) -> None:
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")

    set_current_user(_alice())
    set_current_conversation_id("conv-42")

    await svc._maybe_echo_to_browser(
        uri="http://192.168.1.42:8000/output/speaker/announce-xyz.mp3",
        volume=60,
        title="hello",
        announce=True,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    bus: _FakeBus = svc._event_bus_provider.bus
    assert len(bus.published) == 1
    ev = bus.published[0]
    assert ev.event_type == "speaker.browser.play"
    assert ev.data["user_id"] == "user-alice"
    assert ev.data["conversation_id"] == "conv-42"
    # URL stripped to relative so the SPA resolves against its origin.
    assert ev.data["url"] == "/output/speaker/announce-xyz.mp3"
    assert ev.data["volume"] == 60
    assert ev.data["announce"] is True
    assert ev.source == "speaker.echo"


@pytest.mark.asyncio
async def test_echo_silent_when_user_has_no_active_registration(
    svc: SpeakerService,
) -> None:
    # No activate() call → no registration → echo must not fire.
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_silent_when_primary_is_browser(svc: SpeakerService) -> None:
    # Primary backend already publishes a ``speaker.browser.play``;
    # echoing a second copy would double-play in the user's tab.
    svc._backend_name = "browser"
    svc._primary_backend = "browser"
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_silent_for_system_user(svc: SpeakerService) -> None:
    # Activate "system" user just in case, but echo must still not fire.
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="system", display_name="System")
    set_current_user(_system())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_silent_when_event_bus_missing(svc: SpeakerService) -> None:
    svc._event_bus_provider = None  # type: ignore[assignment]
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    set_current_user(_alice())

    # Doesn't raise — fan-out silently no-ops when wiring is incomplete.
    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )


@pytest.mark.asyncio
async def test_echo_silent_when_browser_backend_missing(svc: SpeakerService) -> None:
    # No browser backend in _backends at all.
    svc._backends.pop("browser", None)
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_defaults_volume_when_none(svc: SpeakerService) -> None:
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=None,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    assert svc._event_bus_provider.bus.published[0].data["volume"] == 80


@pytest.mark.asyncio
async def test_echo_clamps_volume(svc: SpeakerService) -> None:
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=250,
        title="",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )
    assert svc._event_bus_provider.bus.published[-1].data["volume"] == 100


# ── _maybe_echo_stop_to_browser ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_echo_publishes_when_user_has_active_registration(
    svc: SpeakerService,
) -> None:
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    set_current_user(_alice())

    await svc._maybe_echo_stop_to_browser()

    bus: _FakeBus = svc._event_bus_provider.bus
    assert len(bus.published) == 1
    assert bus.published[0].event_type == "speaker.browser.stop"
    assert bus.published[0].data == {"user_id": "user-alice"}


@pytest.mark.asyncio
async def test_stop_echo_silent_when_no_registration(svc: SpeakerService) -> None:
    # No activate() call → no registration → silent.
    set_current_user(_alice())

    await svc._maybe_echo_stop_to_browser()

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_stop_echo_silent_when_primary_is_browser(svc: SpeakerService) -> None:
    svc._backend_name = "browser"
    svc._primary_backend = "browser"
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="user-alice", display_name="Alice")
    set_current_user(_alice())

    await svc._maybe_echo_stop_to_browser()

    assert svc._event_bus_provider.bus.published == []


# ── speaker.info WS handler — drives the SPA's "is echo a no-op?" check ─


@pytest.mark.asyncio
async def test_speaker_info_reports_backend_when_enabled(
    svc: SpeakerService,
) -> None:
    svc._enabled = True
    svc._primary_backend = "sonos"
    svc._backends = {"sonos": MagicMock()}
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["type"] == "gilbert.result"
    assert reply["enabled"] is True
    assert reply["primary_backend"] == "sonos"
    assert "sonos" in reply["active_backends"]


@pytest.mark.asyncio
async def test_speaker_info_blank_backend_when_disabled(
    svc: SpeakerService,
) -> None:
    # When the service is toggled off the backend fields are empty so
    # the SPA doesn't gate UI on stale values.
    svc._enabled = False
    svc._primary_backend = "sonos"  # would be set from prior boot
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["type"] == "gilbert.result"
    assert reply["enabled"] is False
    assert reply["primary_backend"] == ""
    assert reply["active_backends"] == []


@pytest.mark.asyncio
async def test_speaker_info_reports_browser_primary(
    svc: SpeakerService,
) -> None:
    # This is the case the SPA cares about — it disables the echo
    # toggle when primary_backend == "browser" and it is the only
    # active backend.
    svc._enabled = True
    svc._primary_backend = "browser"
    svc._backends = {"browser": MagicMock()}
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["primary_backend"] == "browser"
    assert reply["active_backends"] == ["browser"]


@pytest.mark.asyncio
async def test_speaker_info_multi_backend_active_backends_sorted(
    svc: SpeakerService,
) -> None:
    # With two backends loaded both appear in active_backends (sorted).
    svc._enabled = True
    svc._primary_backend = "sonos"
    svc._backends = {"sonos": MagicMock(), "browser": MagicMock()}
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["enabled"] is True
    assert reply["primary_backend"] == "sonos"
    assert reply["active_backends"] == ["browser", "sonos"]


def test_speaker_service_advertises_ws_handlers_capability() -> None:
    info = SpeakerService().service_info()
    assert "ws_handlers" in info.capabilities


def test_speaker_service_exposes_speaker_info_handler() -> None:
    svc = SpeakerService()
    handlers = svc.get_ws_handlers()
    assert "speaker.info" in handlers


# ── Task 14: explicit-target-set check ────────────────────────────────


@pytest.fixture
def speaker_service_browser_echo() -> SpeakerService:
    """Fixture for browser-echo tests with multiple backends wired.

    The browser backend is present but the caller (alice) starts with
    NO active registration. Individual tests activate as needed.
    """
    s = _make_svc()
    return s


@pytest.mark.asyncio
async def test_echo_does_not_fire_when_user_has_no_active_registration(
    speaker_service_browser_echo: SpeakerService,
) -> None:
    """No registered tab = no echo, regardless of any previous pref."""
    svc = speaker_service_browser_echo  # caller = alice
    # No activation
    events_before = list(svc._event_bus_provider.bus.published)

    set_current_user(UserContext(
        user_id="alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    ))

    await svc._maybe_echo_to_browser(
        uri="http://example.com/x.mp3",
        volume=80,
        title="test",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    echo_events = [
        e for e in svc._event_bus_provider.bus.published[len(events_before):]
        if e.event_type == "speaker.browser.play" and e.source == "speaker.echo"
    ]
    assert echo_events == [], "Echo must NOT fire when caller has no active browser registration"


@pytest.mark.asyncio
async def test_echo_fires_when_user_has_active_registration(
    speaker_service_browser_echo: SpeakerService,
) -> None:
    svc = speaker_service_browser_echo  # caller = alice
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="alice", display_name="Alice")

    events_before = list(svc._event_bus_provider.bus.published)

    set_current_user(UserContext(
        user_id="alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    ))

    await svc._maybe_echo_to_browser(
        uri="http://example.com/x.mp3",
        volume=80,
        title="test",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living"],
    )

    echo_events = [
        e for e in svc._event_bus_provider.bus.published[len(events_before):]
        if e.event_type == "speaker.browser.play" and e.source == "speaker.echo"
        and e.data.get("user_id") == "alice"
    ]
    assert len(echo_events) == 1


@pytest.mark.asyncio
async def test_echo_skips_when_callers_browser_in_target_set(
    speaker_service_browser_echo: SpeakerService,
) -> None:
    """Echo must not fire when caller's own browser ID is already a target."""
    svc = speaker_service_browser_echo
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="alice", display_name="Alice")
    events_before = list(svc._event_bus_provider.bus.published)

    # Caller is alice (per fixture); activated; primary_backend != "browser"
    set_current_user(UserContext(
        user_id="alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    ))

    await svc._maybe_echo_to_browser(
        uri="http://example.com/x.mp3",
        volume=80,
        title="test",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["sonos:living", "browser:alice"],
    )

    echo_events = [
        e for e in svc._event_bus_provider.bus.published[len(events_before):]
        if e.event_type == "speaker.browser.play" and e.source == "speaker.echo"
    ]
    assert echo_events == [], (
        "Echo must skip when caller's browser is in the explicit target set"
    )


@pytest.mark.asyncio
async def test_echo_fires_when_targeting_other_users_browser(
    speaker_service_browser_echo: SpeakerService,
) -> None:
    """Echo for caller's browser fires even if a DIFFERENT user's browser is a target."""
    svc = speaker_service_browser_echo  # caller is alice
    browser: BrowserSpeakerBackend = svc._backends["browser"]  # type: ignore[assignment]
    browser.activate(conn_id="c1", user_id="alice", display_name="Alice")
    events_before = list(svc._event_bus_provider.bus.published)

    set_current_user(UserContext(
        user_id="alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    ))

    await svc._maybe_echo_to_browser(
        uri="http://example.com/x.mp3",
        volume=80,
        title="test",
        announce=False,
        position_seconds=None,
        explicit_target_ids=["browser:bob"],
    )

    echo_for_alice = [
        e for e in svc._event_bus_provider.bus.published[len(events_before):]
        if e.event_type == "speaker.browser.play"
        and e.source == "speaker.echo"
        and e.data.get("user_id") == "alice"
    ]
    assert len(echo_for_alice) == 1, "Alice's echo fires for non-alice browser targets"
