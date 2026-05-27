"""sslip.io internal-URL backend.

Derives a LAN-reachable hostname from the host's outbound IP using a
wildcard-DNS service. ``sslip.io`` (and the compatible ``nip.io``)
resolve ``<ip>.sslip.io`` — in either dotted (``192.168.1.50.sslip.io``)
or dashed (``192-168-1-50.sslip.io``) form — back to that IP. We emit
the dashed form, which avoids resolver quirks around IP-looking labels.

Vendor-free: no third-party Python dependency. The "service" is purely
a DNS naming convention applied to a detected IP, so there is nothing
to install or tear down.
"""

import logging
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.internal_url import InternalUrlBackend
from gilbert.interfaces.net import detect_outbound_ip
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

# Ports that are implicit in their scheme and so omitted from the URL.
_DEFAULT_PORTS = {("http", 80), ("https", 443)}


class SslipInternalUrlBackend(InternalUrlBackend):
    """Internal-URL backend using a wildcard-DNS suffix (sslip.io)."""

    backend_name = "sslip"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="dns_suffix",
                type=ToolParameterType.STRING,
                description=(
                    "Wildcard-DNS suffix that resolves <ip>.<suffix> to that "
                    "IP (e.g. sslip.io, nip.io)."
                ),
                default="sslip.io",
                restart_required=True,
            ),
            ConfigParam(
                key="ip_override",
                type=ToolParameterType.STRING,
                description=(
                    "Override the auto-detected LAN IP. Leave blank to "
                    "auto-detect the host's outbound interface."
                ),
                default="",
                restart_required=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_resolve",
                label="Test resolution",
                description=(
                    "Show the LAN hostname this backend would produce from "
                    "the detected (or overridden) IP."
                ),
            ),
        ]

    def __init__(self) -> None:
        self._internal_url: str = ""

    async def resolve(self, local_port: int, scheme: str, config: dict[str, Any]) -> str:
        ip = (config.get("ip_override") or "").strip() or detect_outbound_ip()
        if not ip:
            raise RuntimeError(
                "Could not detect the host's outbound LAN IP — set an "
                "ip_override in the internal-URL settings."
            )
        suffix = (config.get("dns_suffix") or "sslip.io").strip().strip(".")
        host = f"{ip.replace('.', '-')}.{suffix}"
        port_part = "" if (scheme, local_port) in _DEFAULT_PORTS else f":{local_port}"
        self._internal_url = f"{scheme}://{host}{port_part}"
        logger.info("sslip.io internal URL resolved: %s", self._internal_url)
        return self._internal_url

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_resolve":
            return await self._action_test_resolve(payload)
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_resolve(self, payload: dict[str, Any]) -> ConfigActionResult:
        raw_config = payload.get("config")
        config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
        ip = (config.get("ip_override") or "").strip() or detect_outbound_ip()
        if not ip:
            return ConfigActionResult(
                status="error",
                message=(
                    "Couldn't detect an outbound LAN IP. Set an ip_override "
                    "to the address browsers use to reach this machine."
                ),
            )
        suffix = (config.get("dns_suffix") or "sslip.io").strip().strip(".")
        host = f"{ip.replace('.', '-')}.{suffix}"
        return ConfigActionResult(
            status="ok",
            message=(
                f"Would resolve to {host} (IP {ip}). Add the matching "
                "https URL to your OAuth provider's authorized redirect URIs."
            ),
            data={"host": host, "ip": ip},
        )
