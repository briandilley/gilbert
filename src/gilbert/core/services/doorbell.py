"""Doorbell service — detects ring events from UniFi Protect and publishes events.

Registers a system timer with the scheduler to poll for ring events every
few seconds. When a new ring is detected, publishes a ``doorbell.ring``
event on the event bus with the camera/door name and timestamp.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.scheduler import Schedule
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

# Default poll interval — matches the existing shop-assistant (5 seconds)
_DEFAULT_POLL_INTERVAL = 5.0

# How far back to look for ring events on each poll (seconds).
# Slightly longer than the poll interval to avoid missing events.
_RING_LOOKBACK_SECONDS = 10


class DoorbellService(Service):
    """Detects doorbell ring events via UniFi Protect.

    Publishes ``doorbell.ring`` events on the event bus when a ring is detected.
    Uses the scheduler service for periodic polling.
    """

    def __init__(self) -> None:
        self._event_bus: EventBus | None = None
        self._protect: Any = None  # UniFiProtect instance from presence backend
        self._poll_interval: float = _DEFAULT_POLL_INTERVAL
        self._last_ring_ts: float = 0.0  # epoch ms of last seen ring
        self._doorbell_names: dict[str, str] = {}  # camera name → friendly door name

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="doorbell",
            capabilities=frozenset({"doorbell"}),
            requires=frozenset({"scheduler", "event_bus"}),
            optional=frozenset({"configuration", "presence"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.core.services.event_bus import EventBusService

        # Event bus (required)
        event_bus_svc = resolver.require_capability("event_bus")
        if isinstance(event_bus_svc, EventBusService):
            self._event_bus = event_bus_svc.bus

        # Config
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("doorbell")
                self._apply_config(section)

        # Get UniFi Protect from the presence service's backend
        presence_svc = resolver.get_capability("presence")
        if presence_svc is not None:
            from gilbert.core.services.presence import PresenceService

            if isinstance(presence_svc, PresenceService):
                backend = presence_svc.backend
                # The UniFi backend exposes its protect subsystem
                protect = getattr(backend, "_protect", None)
                if protect is not None:
                    self._protect = protect
                    logger.info("Doorbell using UniFi Protect from presence service")

        if self._protect is None:
            logger.warning("Doorbell service has no Protect backend — ring detection disabled")

        # Initialize last ring timestamp to now (don't trigger on old events)
        self._last_ring_ts = time.time() * 1000

        # Register with scheduler
        from gilbert.core.services.scheduler import SchedulerService

        scheduler = resolver.require_capability("scheduler")
        if isinstance(scheduler, SchedulerService):
            scheduler.add_job(
                name="doorbell-poll",
                schedule=Schedule.every(self._poll_interval),
                callback=self._check_for_rings,
                system=True,
            )

        logger.info(
            "Doorbell service started (poll_interval=%.1fs, doors=%d)",
            self._poll_interval,
            len(self._doorbell_names),
        )

    def _apply_config(self, section: dict[str, Any]) -> None:
        poll = section.get("poll_interval_seconds")
        if poll is not None:
            self._poll_interval = float(poll)
        names = section.get("doorbell_names")
        if isinstance(names, dict):
            self._doorbell_names = names

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "doorbell"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether doorbell monitoring is enabled.",
                default=False, restart_required=True,
            ),
            ConfigParam(
                key="poll_interval_seconds", type=ToolParameterType.NUMBER,
                description="How often to poll for ring events (seconds).",
                default=_DEFAULT_POLL_INTERVAL,
            ),
            ConfigParam(
                key="doorbell_names", type=ToolParameterType.OBJECT,
                description="Map camera names to friendly door names (e.g., {'G4 Doorbell': 'Front Door'}).",
                default={},
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    async def stop(self) -> None:
        pass  # Scheduler handles task cancellation

    # --- Ring detection ---

    async def _check_for_rings(self) -> None:
        """Poll UniFi Protect for new ring events."""
        if self._protect is None:
            return

        try:
            lookback_minutes = max(1, int(_RING_LOOKBACK_SECONDS / 60) + 1)
            events = await self._protect.get_detection_events(
                lookback_minutes=lookback_minutes,
                event_types=["ring"],
            )
        except Exception:
            logger.debug("Failed to poll for ring events", exc_info=True)
            return

        for event in events:
            event_ts = event.start
            if event_ts <= self._last_ring_ts:
                continue

            # New ring detected
            self._last_ring_ts = event_ts
            camera_name = event.camera_name
            door_name = self._doorbell_names.get(camera_name, camera_name)

            logger.info("Doorbell ring detected: %s (%s)", door_name, camera_name)

            if self._event_bus is not None:
                await self._event_bus.publish(Event(
                    event_type="doorbell.ring",
                    data={
                        "door": door_name,
                        "camera": camera_name,
                        "timestamp": _epoch_ms_to_iso(event_ts),
                    },
                    source="doorbell",
                ))


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, OSError):
        return ""
