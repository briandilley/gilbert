"""UniFi Protect doorbell backend — detects ring events via the Protect API."""

import logging
from typing import Any

from gilbert.integrations.unifi.client import UniFiClient
from gilbert.integrations.unifi.protect import UniFiProtect
from gilbert.interfaces.doorbell import DoorbellBackend, RingEvent

logger = logging.getLogger(__name__)


class UniFiProtectDoorbellBackend(DoorbellBackend):
    """Detects doorbell rings via UniFi Protect."""

    def __init__(self) -> None:
        self._client: UniFiClient | None = None
        self._protect: UniFiProtect | None = None

    async def initialize(self, config: dict[str, object]) -> None:
        host = config.get("host")
        if not host:
            logger.warning("UniFi doorbell backend: no host configured")
            return

        cred: dict[str, Any] = config.get("_resolved_credential") or {}  # type: ignore[assignment]
        username = cred.get("username", "")
        password = cred.get("password", "")
        if not username or not password:
            logger.warning("UniFi doorbell backend: no credentials resolved")
            return

        self._client = UniFiClient(str(host), username, password)
        self._protect = UniFiProtect(self._client)
        logger.info("UniFi doorbell backend initialized (%s)", host)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._protect = None

    async def get_ring_events(self, lookback_seconds: int = 10) -> list[RingEvent]:
        if self._protect is None:
            return []

        lookback_minutes = max(1, (lookback_seconds // 60) + 1)
        events = await self._protect.get_detection_events(
            lookback_minutes=lookback_minutes,
            event_types=["ring"],
        )

        return [
            RingEvent(
                camera_name=e.camera_name,
                timestamp=e.start,
            )
            for e in events
        ]
