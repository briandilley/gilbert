"""Vision service — image understanding via a pluggable backend.

Provides image description capabilities for other services (e.g., knowledge
indexing). Backend-agnostic — the Anthropic implementation is one option.
"""

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.vision import VisionBackend

logger = logging.getLogger(__name__)


class VisionService(Service):
    """Image understanding via a pluggable vision backend.

    Capabilities: vision
    """

    def __init__(self, backend: VisionBackend) -> None:
        self._backend = backend
        self._settings: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="vision",
            capabilities=frozenset({"vision"}),
            optional=frozenset({"configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("vision")
                self._settings = section.get("settings", {})

        await self._backend.initialize(self._settings)

        if self._backend.available:
            logger.info("Vision service started")
        else:
            logger.warning("Vision service started without credentials — describe_image unavailable")

    async def stop(self) -> None:
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
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether vision-based image analysis is enabled.",
                default=True, restart_required=True,
            ),
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Vision backend provider.",
                default="anthropic", restart_required=True,
                choices=tuple(VisionBackend.registered_backends().keys()) or ("anthropic",),
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
        pass  # All vision params are restart_required

    # --- Public API ---

    @property
    def available(self) -> bool:
        """Whether the vision backend is ready."""
        return self._backend.available

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        """Analyze an image and return a text description."""
        return await self._backend.describe_image(image_bytes, media_type)
