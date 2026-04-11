"""OCR service — text extraction from images via a pluggable backend.

Provides optical character recognition for document indexing.
Backend-agnostic — the Tesseract implementation is one option.
"""

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.ocr import OCRBackend
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


class OCRService(Service):
    """Text extraction from images via a pluggable OCR backend.

    Capabilities: ocr
    """

    def __init__(self) -> None:
        self._backend: OCRBackend | None = None
        self._backend_name: str = "tesseract"
        self._enabled: bool = False
        self._settings: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ocr",
            capabilities=frozenset({"ocr"}),
            requires=frozenset(),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Optical character recognition",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("ocr")

        if not section.get("enabled", True):
            logger.info("OCR service disabled")
            return

        self._enabled = True
        self._settings = section.get("settings", {})

        backend_name = str(section.get("backend", "tesseract"))
        self._backend_name = backend_name

        try:
            import gilbert.integrations.tesseract_ocr  # noqa: F401
        except ImportError:
            pass

        backends = OCRBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown OCR backend: {backend_name}")

        self._backend = backend_cls()
        await self._backend.initialize(self._settings)

        if self._backend.available:
            logger.info("OCR service started")
        else:
            logger.info("OCR service started (backend not available — OCR disabled)")

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
        self._enabled = False

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "ocr"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.tesseract_ocr  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="OCR backend provider.",
                default="tesseract", restart_required=True,
                choices=tuple(OCRBackend.registered_backends().keys()) or ("tesseract",),
            ),
        ]
        backends = OCRBackend.registered_backends()
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
        pass  # All OCR params are restart_required

    # --- Public API ---

    @property
    def available(self) -> bool:
        """Whether the OCR backend is ready."""
        return self._backend is not None and self._backend.available

    async def extract_text(self, image_bytes: bytes) -> str:
        """Extract text from an image.

        Args:
            image_bytes: Raw image data (PNG, JPEG, TIFF, etc.)

        Returns:
            Extracted text, or empty string if OCR is unavailable or fails.
        """
        if self._backend is None:
            return ""
        return await self._backend.extract_text(image_bytes)
