"""Tests for DoorbellService — ring detection and event publishing."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.core.services.doorbell import DoorbellService
from gilbert.core.services.scheduler import SchedulerService
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.service import ServiceResolver
from gilbert.integrations.unifi.protect import DetectionEvent, UniFiProtect


@pytest.fixture
def mock_protect() -> UniFiProtect:
    p = AsyncMock(spec=UniFiProtect)
    p.get_detection_events = AsyncMock(return_value=[])
    return p


@pytest.fixture
def mock_event_bus() -> EventBus:
    bus = AsyncMock(spec=EventBus)
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def scheduler() -> SchedulerService:
    return SchedulerService()


@pytest.fixture
def service() -> DoorbellService:
    return DoorbellService()


def _ring_event(camera: str, ts: int = 1700000001000) -> DetectionEvent:
    return DetectionEvent(
        event_id="evt1",
        camera_name=camera,
        event_type="ring",
        start=ts,
    )


class TestRingDetection:
    async def test_detects_new_ring(
        self, service: DoorbellService, mock_protect: UniFiProtect, mock_event_bus: EventBus
    ) -> None:
        service._protect = mock_protect
        service._event_bus = mock_event_bus
        service._last_ring_ts = 1700000000000  # before the ring event

        mock_protect.get_detection_events = AsyncMock(return_value=[
            _ring_event("G4 Doorbell", ts=1700000001000),
        ])

        await service._check_for_rings()

        mock_event_bus.publish.assert_awaited_once()
        event: Event = mock_event_bus.publish.call_args[0][0]
        assert event.event_type == "doorbell.ring"
        assert event.data["camera"] == "G4 Doorbell"

    async def test_ignores_old_ring(
        self, service: DoorbellService, mock_protect: UniFiProtect, mock_event_bus: EventBus
    ) -> None:
        service._protect = mock_protect
        service._event_bus = mock_event_bus
        service._last_ring_ts = 1700000002000  # after the ring event

        mock_protect.get_detection_events = AsyncMock(return_value=[
            _ring_event("G4 Doorbell", ts=1700000001000),
        ])

        await service._check_for_rings()

        mock_event_bus.publish.assert_not_awaited()

    async def test_updates_last_ring_ts(
        self, service: DoorbellService, mock_protect: UniFiProtect, mock_event_bus: EventBus
    ) -> None:
        service._protect = mock_protect
        service._event_bus = mock_event_bus
        service._last_ring_ts = 1700000000000

        mock_protect.get_detection_events = AsyncMock(return_value=[
            _ring_event("G4 Doorbell", ts=1700000005000),
        ])

        await service._check_for_rings()

        assert service._last_ring_ts == 1700000005000

    async def test_uses_friendly_door_name(
        self, service: DoorbellService, mock_protect: UniFiProtect, mock_event_bus: EventBus
    ) -> None:
        service._protect = mock_protect
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0
        service._doorbell_names = {"G4 Doorbell": "Front Door"}

        mock_protect.get_detection_events = AsyncMock(return_value=[
            _ring_event("G4 Doorbell"),
        ])

        await service._check_for_rings()

        event: Event = mock_event_bus.publish.call_args[0][0]
        assert event.data["door"] == "Front Door"
        assert event.data["camera"] == "G4 Doorbell"

    async def test_no_protect_skips(self, service: DoorbellService) -> None:
        """No crash when protect is not configured."""
        service._protect = None
        await service._check_for_rings()  # Should not raise

    async def test_protect_error_handled(
        self, service: DoorbellService, mock_protect: UniFiProtect
    ) -> None:
        service._protect = mock_protect
        mock_protect.get_detection_events = AsyncMock(side_effect=Exception("network error"))

        await service._check_for_rings()  # Should not raise

    async def test_multiple_rings_processes_all(
        self, service: DoorbellService, mock_protect: UniFiProtect, mock_event_bus: EventBus
    ) -> None:
        service._protect = mock_protect
        service._event_bus = mock_event_bus
        service._last_ring_ts = 0

        mock_protect.get_detection_events = AsyncMock(return_value=[
            _ring_event("Front Doorbell", ts=1700000001000),
            _ring_event("Rear Doorbell", ts=1700000002000),
        ])

        await service._check_for_rings()

        assert mock_event_bus.publish.await_count == 2
        # Last ring ts should be the most recent
        assert service._last_ring_ts == 1700000002000
