"""Tests that PlayRequest.kind is propagated through SpeakerService.play_on_speakers
to the BrowserSpeakerBackend's speaker.browser.play event payload."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.context import set_current_user
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.speaker import PlayRequest


class _CapturingBus(EventBus):
    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)

    def subscribe(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def subscribe_pattern(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


class _BusProvider(EventBusProvider):
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> EventBus:
        return self._bus


@pytest.mark.asyncio
async def test_play_request_kind_appears_in_browser_event() -> None:
    bus = _CapturingBus()
    backend = BrowserSpeakerBackend()
    backend.set_event_bus_provider(_BusProvider(bus))
    await backend.initialize({})
    backend.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    set_current_user(UserContext(user_id="alice", email="", display_name="Alice", roles=frozenset()))
    try:
        await backend.play_uri(PlayRequest(uri="https://example/a.mp3", kind="chat_speech"))
    finally:
        set_current_user(None)

    assert len(bus.published) == 1
    data = bus.published[0].data
    assert data["kind"] == "chat_speech"
    assert data["user_id"] == "alice"


@pytest.mark.asyncio
async def test_play_request_kind_defaults_to_empty_string() -> None:
    bus = _CapturingBus()
    backend = BrowserSpeakerBackend()
    backend.set_event_bus_provider(_BusProvider(bus))
    await backend.initialize({})
    backend.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    set_current_user(UserContext(user_id="alice", email="", display_name="Alice", roles=frozenset()))
    try:
        await backend.play_uri(PlayRequest(uri="https://example/a.mp3"))
    finally:
        set_current_user(None)

    data = bus.published[0].data
    assert data["kind"] == ""


@pytest.mark.asyncio
async def test_speaker_service_threads_kind_to_browser_backend() -> None:
    """When SpeakerService.play_on_speakers(..., kind=...) is called with a
    browser:<user> target, the resulting event payload carries the kind."""
    from gilbert.core.services.speaker import SpeakerService

    bus = _CapturingBus()
    backend = BrowserSpeakerBackend()
    backend.set_event_bus_provider(_BusProvider(bus))
    await backend.initialize({})
    backend.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    svc = SpeakerService()
    svc._backends = {"browser": backend}  # type: ignore[attr-defined]

    set_current_user(UserContext(user_id="alice", email="", display_name="Alice", roles=frozenset()))
    try:
        await svc.play_on_speakers(
            uri="https://example/a.mp3",
            speaker_ids=["browser:alice"],
            kind="chat_speech",
            title="Gilbert",
        )
    finally:
        set_current_user(None)

    assert any(ev.data.get("kind") == "chat_speech" for ev in bus.published)


@pytest.mark.asyncio
async def test_speaker_service_threads_kind_through_browser_echo() -> None:
    """When a non-browser backend is primary and the caller has an active
    browser registration (echo opt-in), the echo event should also carry
    the kind classifier — not just the direct-target path."""
    from gilbert.core.services.speaker import SpeakerService
    from gilbert.interfaces.speaker import (
        SpeakerBackend,
        SpeakerInfo,
    )

    class _FakePrimaryBackend(SpeakerBackend):
        """Stand-in for a non-browser primary (e.g. Sonos). Records plays
        but doesn't publish browser events."""

        backend_name = "fake"
        supports_grouping = False
        supports_repeat = False

        def __init__(self) -> None:
            self.played: list[PlayRequest] = []

        async def initialize(self, config: dict) -> None:
            pass

        async def close(self) -> None:
            pass

        async def list_speakers(self) -> list[SpeakerInfo]:
            return [SpeakerInfo(speaker_id="primary-1", name="Primary", ip_address="")]

        async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
            return SpeakerInfo(speaker_id="primary-1", name="Primary", ip_address="")

        async def play_uri(self, request: PlayRequest) -> None:
            self.played.append(request)

        async def stop(self, speaker_ids: list[str] | None = None) -> None:
            pass

        async def get_volume(self, speaker_id: str) -> int:
            return 50

        async def set_volume(self, speaker_id: str, volume: int) -> None:
            pass

    bus = _CapturingBus()
    browser = BrowserSpeakerBackend()
    browser.set_event_bus_provider(_BusProvider(bus))
    await browser.initialize({})
    browser.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    primary = _FakePrimaryBackend()

    svc = SpeakerService()
    svc._backends = {"fake": primary, "browser": browser}  # type: ignore[attr-defined]
    svc._primary_backend = "fake"  # type: ignore[attr-defined]
    svc._event_bus_provider = _BusProvider(bus)  # type: ignore[attr-defined]

    set_current_user(UserContext(user_id="alice", email="", display_name="Alice", roles=frozenset()))
    try:
        await svc.play_on_speakers(
            uri="https://example/a.mp3",
            speaker_ids=["fake:primary-1"],
            kind="chat_speech",
            title="Gilbert",
        )
    finally:
        set_current_user(None)

    # Primary backend received the kind.
    assert len(primary.played) == 1
    assert primary.played[0].kind == "chat_speech"

    # Echo event was published with the kind too.
    echo_events = [e for e in bus.published if e.data.get("user_id") == "alice"]
    assert len(echo_events) >= 1
    assert any(e.data.get("kind") == "chat_speech" for e in echo_events)
