"""Doorbell service — detects ring events and announces via speakers.

Uses a DoorbellBackend to poll for ring events. When a new ring is detected,
publishes a ``doorbell.ring`` event on the event bus and announces over speakers.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.doorbell import DoorbellBackend
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

# Default poll interval (seconds)
_DEFAULT_POLL_INTERVAL = 5.0

# How far back to look for ring events on each poll (seconds).
_RING_LOOKBACK_SECONDS = 10


class DoorbellService(Service):
    """Detects doorbell ring events and announces via speakers.

    Publishes ``doorbell.ring`` events on the event bus when a ring is detected.
    Uses the scheduler service for periodic polling.
    """

    def __init__(self) -> None:
        self._backend: DoorbellBackend | None = None
        self._backend_name: str = "unifi"
        self._enabled: bool = False
        self._event_bus: EventBus | None = None
        self._resolver: ServiceResolver | None = None
        self._poll_interval: float = _DEFAULT_POLL_INTERVAL
        self._last_ring_ts: float = 0.0  # epoch ms of last seen ring
        self._doorbell_names: list[str] = []  # selected doorbell/camera names to monitor
        self._speakers: list[str] = []  # speaker names for announcements
        self._available_doorbells: list[str] = []  # cached from backend

    @property
    def available_doorbells(self) -> list[str]:
        """Cached list of doorbell names from the backend."""
        return list(self._available_doorbells)

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="doorbell",
            capabilities=frozenset({"doorbell"}),
            requires=frozenset({"scheduler", "event_bus"}),
            optional=frozenset(
                {"configuration", "credentials", "speaker_control", "text_to_speech"}
            ),
            events=frozenset({"doorbell.ring"}),
            toggleable=True,
            toggle_description="Doorbell ring detection",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Event bus (required)
        event_bus_svc = resolver.require_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        # Config
        full_section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                full_section = config_svc.get_section("doorbell")
                self._apply_config(full_section)

        if not full_section.get("enabled", False):
            logger.info("Doorbell service disabled")
            return

        self._enabled = True

        # Resolve backend
        backend_name = full_section.get("backend", "unifi")
        self._backend_name = backend_name
        backends = DoorbellBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown doorbell backend: {backend_name}")
        self._backend = backend_cls()

        # Initialize backend with settings
        settings: dict[str, object] = dict(full_section.get("settings", {}))
        await self._backend.initialize(settings)

        # Cache available doorbell names for dynamic choices
        try:
            self._available_doorbells = await self._backend.list_doorbell_names()
        except Exception:
            logger.debug("Could not cache doorbell names on start")

        # Initialize last ring timestamp to now (don't trigger on old events)
        self._last_ring_ts = time.time() * 1000

        # Register with scheduler
        scheduler = resolver.require_capability("scheduler")
        if isinstance(scheduler, SchedulerProvider):
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
        settings = section.get("settings", {})
        if isinstance(settings, dict):
            names = settings.get("doorbell_names")
            if isinstance(names, list):
                self._doorbell_names = names
        speakers = section.get("speakers")
        if isinstance(speakers, list):
            self._speakers = speakers

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "doorbell"

    @property
    def config_category(self) -> str:
        return "Monitoring"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="poll_interval_seconds",
                type=ToolParameterType.NUMBER,
                description="How often to poll for ring events (seconds).",
                default=_DEFAULT_POLL_INTERVAL,
            ),
            ConfigParam(
                key="speakers",
                type=ToolParameterType.ARRAY,
                description="Speaker names for doorbell announcements (empty = all speakers).",
                default=[],
                choices_from="speakers",
            ),
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Doorbell backend provider.",
                default="unifi",
                restart_required=True,
                choices=tuple(DoorbellBackend.registered_backends().keys()) or ("unifi",),
            ),
        ]
        backends = DoorbellBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=DoorbellBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
        self._enabled = False

    # --- Ring detection ---

    async def _check_for_rings(self) -> None:
        """Poll the backend for new ring events."""
        if self._backend is None:
            return
        try:
            events = await self._backend.get_ring_events(
                lookback_seconds=_RING_LOOKBACK_SECONDS,
            )
        except Exception:
            logger.debug("Failed to poll for ring events", exc_info=True)
            return

        for event in events:
            if event.timestamp <= self._last_ring_ts:
                continue

            # New ring detected
            self._last_ring_ts = event.timestamp
            camera_name = event.camera_name

            # If specific doorbells are selected, only monitor those
            if self._doorbell_names and camera_name not in self._doorbell_names:
                continue

            door_name = camera_name
            logger.info("Doorbell ring detected: %s", door_name)

            if self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        event_type="doorbell.ring",
                        data={
                            "door": door_name,
                            "camera": camera_name,
                            "timestamp": _epoch_ms_to_iso(event.timestamp),
                        },
                        source="doorbell",
                    )
                )

            await self._announce(door_name)

    # --- Announcement ---

    async def _announce(self, door_name: str) -> None:
        """Announce a doorbell ring via speakers."""
        if self._resolver is None:
            return

        from gilbert.interfaces.speaker import SpeakerProvider

        speaker_svc = self._resolver.get_capability("speaker_control")
        if not isinstance(speaker_svc, SpeakerProvider):
            logger.debug("No speaker service — doorbell not announced: %s", door_name)
            return

        text = f"Someone is at the {door_name}."

        try:
            await speaker_svc.announce(
                text,
                speaker_names=self._speakers or None,
                context=f"Doorbell ring at the {door_name} — alert tone",
            )
        except Exception:
            logger.warning("Failed to announce doorbell ring", exc_info=True)


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
        return dt.isoformat()
    except (ValueError, OSError):
        return ""
