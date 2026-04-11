"""Tests for DoorbellService — ring detection, event publishing, and announcements."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.core.services.doorbell import DoorbellService
from gilbert.interfaces.doorbell import DoorbellBackend, RingEvent
from gilbert.interfaces.events import Event, EventBus


@pytest.fixture
def mock_backend() -> DoorbellBackend:
    b = AsyncMock(spec=DoorbellBackend)
    b.get_ring_events = AsyncMock(return_value=[])
    return b


@pytest.fixture
def mock_event_bus() -> EventBus:
    bus = AsyncMock(spec=EventBus)
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_speaker() -> MagicMock:
    speaker = MagicMock()
    speaker.announce = AsyncMock()
    return speaker


@pytest.fixture
def service(mock_backend: DoorbellBackend) -> DoorbellService:
    return DoorbellService(mock_backend)


def _ring(camera: str, ts: int = 1700000001000) -> RingEvent:
    return RingEvent(camera_name=camera, timestamp=ts)


class TestRingDetection:
    async def test_detects_new_ring(
        self, service: DoorbellService, mock_backend: DoorbellBackend, mock_event_bus: EventBus
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 1700000000000

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell", ts=1700000001000),
        ])

        await service._check_for_rings()

        mock_event_bus.publish.assert_awaited_once()
        event: Event = mock_event_bus.publish.call_args[0][0]
        assert event.event_type == "doorbell.ring"
        assert event.data["camera"] == "G4 Doorbell"

    async def test_ignores_old_ring(
        self, service: DoorbellService, mock_backend: DoorbellBackend, mock_event_bus: EventBus
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 1700000002000

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell", ts=1700000001000),
        ])

        await service._check_for_rings()

        mock_event_bus.publish.assert_not_awaited()

    async def test_updates_last_ring_ts(
        self, service: DoorbellService, mock_backend: DoorbellBackend, mock_event_bus: EventBus
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 1700000000000

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell", ts=1700000005000),
        ])

        await service._check_for_rings()

        assert service._last_ring_ts == 1700000005000

    async def test_filters_by_selected_doorbells(
        self, service: DoorbellService, mock_backend: DoorbellBackend, mock_event_bus: EventBus
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0
        service._doorbell_names = ["G4 Doorbell"]

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell"),
            _ring("Other Camera"),
        ])

        await service._check_for_rings()

        # Only the selected doorbell should trigger an event
        assert mock_event_bus.publish.call_count == 1
        event: Event = mock_event_bus.publish.call_args[0][0]
        assert event.data["door"] == "G4 Doorbell"

    async def test_backend_error_handled(
        self, service: DoorbellService, mock_backend: DoorbellBackend
    ) -> None:
        mock_backend.get_ring_events = AsyncMock(side_effect=Exception("network error"))

        await service._check_for_rings()  # Should not raise

    async def test_multiple_rings_processes_all(
        self, service: DoorbellService, mock_backend: DoorbellBackend, mock_event_bus: EventBus
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("Front Doorbell", ts=1700000001000),
            _ring("Rear Doorbell", ts=1700000002000),
        ])

        await service._check_for_rings()

        assert mock_event_bus.publish.await_count == 2
        assert service._last_ring_ts == 1700000002000


class TestAnnouncement:
    async def test_announces_on_ring(
        self,
        service: DoorbellService,
        mock_backend: DoorbellBackend,
        mock_event_bus: EventBus,
        mock_speaker: MagicMock,
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0
        service._doorbell_names = []  # empty = monitor all

        resolver = MagicMock()
        resolver.get_capability = MagicMock(return_value=mock_speaker)
        service._resolver = resolver

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell Pro"),
        ])

        await service._check_for_rings()

        mock_speaker.announce.assert_awaited_once_with(
            "Someone is at the G4 Doorbell Pro.",
            speaker_names=None,
        )

    async def test_announce_uses_configured_speakers(
        self,
        service: DoorbellService,
        mock_backend: DoorbellBackend,
        mock_event_bus: EventBus,
        mock_speaker: MagicMock,
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0
        service._speakers = ["Living Room", "Kitchen"]

        resolver = MagicMock()
        resolver.get_capability = MagicMock(return_value=mock_speaker)
        service._resolver = resolver

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell"),
        ])

        await service._check_for_rings()

        mock_speaker.announce.assert_awaited_once_with(
            "Someone is at the G4 Doorbell.",
            speaker_names=["Living Room", "Kitchen"],
        )

    async def test_no_speaker_service_no_crash(
        self,
        service: DoorbellService,
        mock_backend: DoorbellBackend,
        mock_event_bus: EventBus,
    ) -> None:
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0

        resolver = MagicMock()
        resolver.get_capability = MagicMock(return_value=None)
        service._resolver = resolver

        mock_backend.get_ring_events = AsyncMock(return_value=[
            _ring("G4 Doorbell"),
        ])

        await service._check_for_rings()  # Should not raise
