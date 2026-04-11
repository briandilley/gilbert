"""UniFi Protect doorbell backend — detects ring events via the Protect API."""

import logging

from gilbert.integrations.unifi.client import UniFiClient
from gilbert.integrations.unifi.protect import UniFiProtect
from gilbert.interfaces.doorbell import DoorbellBackend, RingEvent

logger = logging.getLogger(__name__)


class UniFiProtectDoorbellBackend(DoorbellBackend):
    """Detects doorbell rings via UniFi Protect."""

    backend_name = "unifi"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="host", type=ToolParameterType.STRING,
                description="UniFi Protect controller URL.",
                default="", restart_required=True,
            ),
            ConfigParam(
                key="username", type=ToolParameterType.STRING,
                description="UniFi Protect username.",
                default="", restart_required=True, sensitive=True,
            ),
            ConfigParam(
                key="password", type=ToolParameterType.STRING,
                description="UniFi Protect password.",
                default="", restart_required=True, sensitive=True,
            ),
            ConfigParam(
                key="doorbell_names", type=ToolParameterType.ARRAY,
                description="Doorbells to monitor (empty = all).",
                default=[],
                choices_from="doorbells",
            ),
        ]

    def __init__(self) -> None:
        self._client: UniFiClient | None = None
        self._protect: UniFiProtect | None = None

    async def initialize(self, config: dict[str, object]) -> None:
        host = config.get("host")
        if not host:
            logger.warning("UniFi doorbell backend: no host configured")
            return

        username = str(config.get("username", ""))
        password = str(config.get("password", ""))
        if not username or not password:
            logger.warning("UniFi doorbell backend: no credentials configured")
            return

        self._client = UniFiClient(str(host), username, password)
        self._protect = UniFiProtect(self._client)
        logger.info("UniFi doorbell backend initialized (%s)", host)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._protect = None

    async def list_doorbell_names(self) -> list[str]:
        if self._protect is None:
            return []
        cameras = await self._protect.list_cameras()
        return [c.name for c in cameras if c.is_doorbell]

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
