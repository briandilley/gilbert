"""Vision service — image understanding via a pluggable backend.

Provides image description capabilities for other services (e.g., knowledge
indexing). Backend-agnostic — the Anthropic implementation is one option.
"""

import logging
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
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.vision import VisionBackend

logger = logging.getLogger(__name__)


class VisionService(Service):
    """Image understanding via a pluggable vision backend.

    Capabilities: vision
    """

    def __init__(self) -> None:
        self._backend: VisionBackend | None = None
        self._backend_name: str = "anthropic"
        self._enabled: bool = False
        self._settings: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="vision",
            capabilities=frozenset({"vision"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Image analysis and description",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Vision service disabled")
            return

        self._enabled = True

        self._settings = section.get("settings", self._settings)

        backend_name = section.get("backend", "anthropic")
        self._backend_name = backend_name
        backends = VisionBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown vision backend: {backend_name}")
        self._backend = backend_cls()

        await self._backend.initialize(self._settings)
        logger.info("Vision service started")

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "vision"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Vision backend provider.",
                default="anthropic", restart_required=True,
                choices=tuple(VisionBackend.registered_backends().keys()),
            ),
        ]
        backends = VisionBackend.registered_backends()
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
        pass  # All vision params are restart_required

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=VisionBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    # --- Public API ---

    @property
    def available(self) -> bool:
        """Whether the vision backend is ready."""
        return self._backend is not None and self._backend.available

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        """Analyze an image and return a text description."""
        if self._backend is None:
            raise RuntimeError("Vision service is not enabled")
        return await self._backend.describe_image(image_bytes, media_type)
