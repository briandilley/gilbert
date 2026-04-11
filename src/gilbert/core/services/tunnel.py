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

    def __init__(self) -> None:
        self._backend: TunnelBackend | None = None
        self._backend_name: str = "ngrok"
        self._enabled: bool = False
        self._local_port: int = 8000
        self._public_url: str = ""
        self._settings: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tunnel",
            capabilities=frozenset({"tunnel"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Public tunnel for external access",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section(self.config_namespace)
                web_section = config_svc.get_section("web")
                if web_section.get("port") is not None:
                    self._local_port = int(web_section["port"])

        if not section.get("enabled", False):
            logger.info("Tunnel service disabled")
            return

        self._enabled = True
        self._settings = section.get("settings", self._settings)

        backend_name = section.get("backend", "ngrok")
        self._backend_name = backend_name
        backends = TunnelBackend.registered_backends()
        if backend_name not in backends:
            # Import known backends to trigger registration
            try:
                import gilbert.integrations.ngrok_tunnel  # noqa: F401
            except ImportError:
                pass
            backends = TunnelBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown tunnel backend: {backend_name}")
        self._backend = backend_cls()

        self._public_url = await self._backend.connect(self._local_port, self._settings)
        logger.info("Tunnel started: %s -> localhost:%d", self._public_url, self._local_port)

    async def stop(self) -> None:
        if self._backend is not None:
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
        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.ngrok_tunnel  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Tunnel backend provider.",
                default="ngrok", restart_required=True,
                choices=tuple(TunnelBackend.registered_backends().keys()) or ("ngrok",),
            ),
        ]
        backends = TunnelBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
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
        if self._backend is None:
            return ""
        return self._public_url

    def public_url_for(self, path: str) -> str:
        """Build a full public URL for a path (e.g., ``/auth/callback``)."""
        if self._backend is None:
            return ""
        base = self._public_url.rstrip("/")
        path = path if path.startswith("/") else f"/{path}"
        return f"{base}{path}"
