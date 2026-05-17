"""Tests for the per-user "echo to browser" fan-out in SpeakerService.

Covers the ``_maybe_echo_to_browser`` / ``_maybe_echo_stop_to_browser``
gating + emission logic without requiring the full Service.start()
lifecycle — we instantiate the service and inject fake dependencies
directly so each branch (pref off / on, primary=browser, no user, no
bus, no users svc) is testable in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.context import set_current_conversation_id, set_current_user
from gilbert.core.services.speaker import SpeakerService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event


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


class _FakeUsersSvc:
    """Implements just enough of UserPrefReader to drive the gate."""

    def __init__(self, prefs: dict[str, dict[str, Any]] | None = None) -> None:
        self._prefs = prefs or {}
        self.raise_on_get = False

    async def get_user_pref(
        self, user_id: str, key: str, default: object = None
    ) -> object:
        if self.raise_on_get:
            raise RuntimeError("simulated lookup failure")
        return self._prefs.get(user_id, {}).get(key, default)

    async def set_user_pref(self, user_id: str, key: str, value: object) -> None:
        self._prefs.setdefault(user_id, {})[key] = value


@pytest.fixture
def svc() -> SpeakerService:
    s = SpeakerService()
    s._backend_name = "sonos"  # primary != browser by default
    s._event_bus_provider = _FakeBusProvider()
    s._users_svc = _FakeUsersSvc()
    return s


# ── _maybe_echo_to_browser ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_echo_publishes_when_pref_enabled(svc: SpeakerService) -> None:
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())
    set_current_conversation_id("conv-42")

    await svc._maybe_echo_to_browser(
        uri="http://192.168.1.42:8000/output/speaker/announce-xyz.mp3",
        volume=60,
        title="hello",
        announce=True,
        position_seconds=None,
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
async def test_echo_silent_when_pref_disabled(svc: SpeakerService) -> None:
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": False}
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_silent_when_primary_is_browser(svc: SpeakerService) -> None:
    # Primary backend already publishes a ``speaker.browser.play``;
    # echoing a second copy would double-play in the user's tab.
    svc._backend_name = "browser"
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_silent_for_system_user(svc: SpeakerService) -> None:
    svc._users_svc._prefs["system"] = {"speaker.browser_echo": True}
    set_current_user(_system())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_silent_when_event_bus_missing(svc: SpeakerService) -> None:
    svc._event_bus_provider = None  # type: ignore[assignment]
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())

    # Doesn't raise — fan-out silently no-ops when wiring is incomplete.
    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
    )


@pytest.mark.asyncio
async def test_echo_silent_when_users_svc_missing(svc: SpeakerService) -> None:
    svc._users_svc = None  # type: ignore[assignment]
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
    )

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_swallows_pref_lookup_errors(svc: SpeakerService) -> None:
    svc._users_svc.raise_on_get = True
    set_current_user(_alice())

    # A flaky storage backend should not raise to the caller — the
    # primary play already succeeded; the secondary path is best-effort.
    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=80,
        title="",
        announce=False,
        position_seconds=None,
    )
    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_echo_defaults_volume_when_none(svc: SpeakerService) -> None:
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=None,
        title="",
        announce=False,
        position_seconds=None,
    )

    assert svc._event_bus_provider.bus.published[0].data["volume"] == 80


@pytest.mark.asyncio
async def test_echo_clamps_volume(svc: SpeakerService) -> None:
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())

    await svc._maybe_echo_to_browser(
        uri="http://x/output/speaker/foo.mp3",
        volume=250,
        title="",
        announce=False,
        position_seconds=None,
    )
    assert svc._event_bus_provider.bus.published[-1].data["volume"] == 100


# ── _maybe_echo_stop_to_browser ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_echo_publishes_when_pref_enabled(svc: SpeakerService) -> None:
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())

    await svc._maybe_echo_stop_to_browser()

    bus: _FakeBus = svc._event_bus_provider.bus
    assert len(bus.published) == 1
    assert bus.published[0].event_type == "speaker.browser.stop"
    assert bus.published[0].data == {"user_id": "user-alice"}


@pytest.mark.asyncio
async def test_stop_echo_silent_when_pref_disabled(svc: SpeakerService) -> None:
    # No pref set at all → default False → silent.
    set_current_user(_alice())

    await svc._maybe_echo_stop_to_browser()

    assert svc._event_bus_provider.bus.published == []


@pytest.mark.asyncio
async def test_stop_echo_silent_when_primary_is_browser(svc: SpeakerService) -> None:
    svc._backend_name = "browser"
    svc._users_svc._prefs["user-alice"] = {"speaker.browser_echo": True}
    set_current_user(_alice())

    await svc._maybe_echo_stop_to_browser()

    assert svc._event_bus_provider.bus.published == []


# ── speaker.info WS handler — drives the SPA's "is echo a no-op?" check ─


@pytest.mark.asyncio
async def test_speaker_info_reports_backend_when_enabled(
    svc: SpeakerService,
) -> None:
    svc._enabled = True
    svc._backend_name = "sonos"
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["type"] == "gilbert.result"
    assert reply["enabled"] is True
    assert reply["backend"] == "sonos"


@pytest.mark.asyncio
async def test_speaker_info_blank_backend_when_disabled(
    svc: SpeakerService,
) -> None:
    # When the service is toggled off the backend name is meaningless —
    # report empty so the SPA doesn't gate UI on a stale value.
    svc._enabled = False
    svc._backend_name = "sonos"  # would be set from prior boot
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["type"] == "gilbert.result"
    assert reply["enabled"] is False
    assert reply["backend"] == ""


@pytest.mark.asyncio
async def test_speaker_info_reports_browser_primary(
    svc: SpeakerService,
) -> None:
    # This is the case the SPA cares about — it disables the echo
    # toggle when the response says ``backend == "browser"``.
    svc._enabled = True
    svc._backend_name = "browser"
    reply = await svc._ws_speaker_info(None, {"id": "1"})
    assert reply["backend"] == "browser"


def test_speaker_service_advertises_ws_handlers_capability() -> None:
    info = SpeakerService().service_info()
    assert "ws_handlers" in info.capabilities


def test_speaker_service_exposes_speaker_info_handler() -> None:
    svc = SpeakerService()
    handlers = svc.get_ws_handlers()
    assert "speaker.info" in handlers
