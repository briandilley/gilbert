"""Tunnel service — provides public HTTPS URLs via a pluggable backend.

Wraps a TunnelBackend (ngrok, etc.) as a discoverable service so external
services (Google OAuth, webhooks) can reach Gilbert over HTTPS.
"""

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.tunnel import TunnelBackend

logger = logging.getLogger(__name__)


class TunnelService(Service):
    """Manages a public HTTPS tunnel via a pluggable backend.

    Capabilities: ``tunnel``.
    """

    def __init__(self, backend: TunnelBackend, local_port: int = 8765) -> None:
        self._backend = backend
        self._local_port = local_port
        self._public_url: str = ""
        self._settings: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tunnel",
            capabilities=frozenset({"tunnel"}),
            optional=frozenset({"configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("tunnel")
                self._settings = section.get("settings", {})

        self._public_url = await self._backend.connect(self._local_port, self._settings)
        logger.info("Tunnel started: %s -> localhost:%d", self._public_url, self._local_port)

    async def stop(self) -> None:
        await self._backend.disconnect()
        self._public_url = ""

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "tunnel"

    @property
    def config_category(self) -> str:
        return "Infrastructure"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the public tunnel is enabled.",
                default=False, restart_required=True,
            ),
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Tunnel backend provider.",
                default="ngrok", restart_required=True,
                choices=tuple(TunnelBackend.registered_backends().keys()) or ("ngrok",),
            ),
        ]
        for bp in self._backend.backend_config_params():
            params.append(ConfigParam(
                key=f"settings.{bp.key}", type=bp.type,
                description=bp.description, default=bp.default,
                restart_required=bp.restart_required, sensitive=bp.sensitive,
                choices=bp.choices, choices_from=bp.choices_from,
                multiline=bp.multiline, backend_param=True,
            ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        pass  # All tunnel params are restart_required

    # --- Public API ---

    @property
    def public_url(self) -> str:
        """The public HTTPS URL (e.g., ``https://abc123.ngrok.io``)."""
        return self._public_url

    def public_url_for(self, path: str) -> str:
        """Build a full public URL for a path (e.g., ``/auth/callback``)."""
        base = self._public_url.rstrip("/")
        path = path if path.startswith("/") else f"/{path}"
        return f"{base}{path}"
