"""Internal-URL service — a LAN-reachable hostname via a pluggable backend.

Wraps an ``InternalUrlBackend`` (sslip.io, etc.) as a discoverable
service so consumers that only need LAN reachability — chiefly OAuth
redirects, where the provider rejects raw IPs but the redirect itself
travels through the user's browser — can build a valid hostname URL
without depending on a public tunnel.

Sibling to ``TunnelService`` but a distinct capability (``internal_url``)
with explicitly internal semantics: see ``InternalUrlProvider``.
"""

import logging
from typing import Any

# Side-effect import: registers the bundled sslip.io backend so it's
# discoverable via InternalUrlBackend.registered_backends().
import gilbert.integrations.sslip_internal_url  # noqa: F401
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.internal_url import InternalUrlBackend
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


class InternalUrlService(Service):
    """Manages a LAN-reachable hostname for the local server via a backend.

    Capabilities: ``internal_url``.
    """

    def __init__(self) -> None:
        self._backend: InternalUrlBackend | None = None
        self._backend_name: str = "sslip"
        self._enabled: bool = False
        self._local_port: int = 8000
        self._scheme: str = "http"
        self._internal_url: str = ""
        self._settings: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="internal_url",
            capabilities=frozenset({"internal_url"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="LAN-reachable hostname for OAuth redirects",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)
            self._scheme, self._local_port = _web_scheme_and_port(
                config_svc.get_section("web")
            )

        if not section.get("enabled", False):
            logger.info("Internal-URL service disabled")
            return

        self._enabled = True
        self._settings = section.get("settings", self._settings)

        backend_name = section.get("backend", "sslip")
        self._backend_name = backend_name
        backends = InternalUrlBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown internal-URL backend: {backend_name}")
        self._backend = backend_cls()

        try:
            self._internal_url = await self._backend.resolve(
                self._local_port, self._scheme, self._settings
            )
        except Exception:
            # A failure to derive the hostname (e.g. no network) leaves the
            # service running but inert — internal_url_for() returns "" and
            # consumers fall back, matching the tunnel-not-connected case.
            logger.exception("Internal-URL backend failed to resolve a hostname")
            self._internal_url = ""
            return

        logger.info(
            "Internal URL ready: %s -> localhost:%d", self._internal_url, self._local_port
        )

    async def stop(self) -> None:
        self._internal_url = ""

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "internal_url"

    @property
    def config_category(self) -> str:
        return "Infrastructure"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Internal-URL backend provider.",
                default="sslip",
                restart_required=True,
                choices=tuple(InternalUrlBackend.registered_backends().keys()),
            ),
        ]
        backends = InternalUrlBackend.registered_backends()
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
        pass  # All internal-URL params are restart_required

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=InternalUrlBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    # --- Public API (InternalUrlProvider) ---

    @property
    def internal_url(self) -> str:
        """The LAN-reachable base URL (e.g. ``https://192-168-1-50.sslip.io:8443``)."""
        if self._backend is None:
            return ""
        return self._internal_url

    def internal_url_for(self, path: str) -> str:
        """Build a full internal URL for a path (e.g. ``/auth/callback``)."""
        if self._backend is None or not self._internal_url:
            return ""
        base = self._internal_url.rstrip("/")
        path = path if path.startswith("/") else f"/{path}"
        return f"{base}{path}"


def _web_scheme_and_port(web_section: dict[str, Any]) -> tuple[str, int]:
    """Derive the local listener's scheme + port from the ``web`` config.

    When TLS is enabled (the default), browsers — and therefore OAuth
    redirects — reach Gilbert over ``https`` on ``tls.https_port``.
    Otherwise it's plain ``http`` on ``web.port``.
    """
    raw_tls = web_section.get("tls")
    tls: dict[str, Any] = raw_tls if isinstance(raw_tls, dict) else {}
    if tls.get("enabled", True):
        return "https", int(tls.get("https_port", 8443))
    return "http", int(web_section.get("port", 8000))
