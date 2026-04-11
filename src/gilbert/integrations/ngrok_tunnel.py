"""Ngrok tunnel backend — public HTTPS URLs via pyngrok."""

import logging
from typing import Any

from gilbert.interfaces.tunnel import TunnelBackend

logger = logging.getLogger(__name__)


class NgrokTunnel(TunnelBackend):
    """Tunnel backend using ngrok via pyngrok."""

    backend_name = "ngrok"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="api_key", type=ToolParameterType.STRING,
                description="Ngrok auth token.",
                sensitive=True, restart_required=True,
            ),
            ConfigParam(
                key="domain", type=ToolParameterType.STRING,
                description="Custom ngrok domain (e.g., 'myapp.ngrok.io').",
                default="", restart_required=True,
            ),
        ]

    def __init__(self) -> None:
        self._tunnel: Any = None
        self._public_url: str = ""

    async def connect(self, local_port: int, config: dict[str, Any]) -> str:
        from pyngrok import conf, ngrok

        api_key = config.get("api_key", "")
        domain = config.get("domain", "")

        if api_key:
            conf.get_default().auth_token = api_key
            logger.info("Ngrok auth token configured")

        options: dict[str, Any] = {"addr": str(local_port)}
        if domain:
            options["domain"] = domain

        self._tunnel = ngrok.connect(**options)
        self._public_url = self._tunnel.public_url

        # Ensure HTTPS
        if self._public_url.startswith("http://"):
            self._public_url = self._public_url.replace("http://", "https://", 1)

        logger.info("Ngrok tunnel started: %s -> localhost:%d", self._public_url, local_port)
        return self._public_url

    async def disconnect(self) -> None:
        if self._tunnel is not None:
            from pyngrok import ngrok

            try:
                ngrok.disconnect(self._tunnel.public_url)
            except Exception:
                logger.debug("Error disconnecting ngrok tunnel")
            self._tunnel = None
            self._public_url = ""
            logger.info("Ngrok tunnel stopped")
