"""OCR backend interface — text extraction from images."""

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


@runtime_checkable
class OCRProvider(Protocol):
    """Capability protocol for cross-service OCR access.

    Plugins (notably the Mentra glasses plugin's camera tool) resolve
    this via ``resolver.get_capability("ocr")`` and ``isinstance``-
    check against ``OCRProvider`` rather than importing the concrete
    ``OCRService`` class. Mirrors the shape of ``VisionProvider``.

    Minimal surface — just the one method consumers actually use. If
    a backend has richer features (per-region results, confidence
    scores, etc.) they live on the concrete backend and are accessible
    via the ``ocr`` service's full API, not this capability protocol.
    """

    async def extract_text(self, image_bytes: bytes) -> str:
        """Return text extracted from the image bytes. Empty string
        on failure (no exception)."""
        ...


class OCRBackend(ABC):
    """Abstract OCR backend. Implementation-agnostic."""

    _registry: dict[str, type["OCRBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            OCRBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["OCRBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize with configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...

    @abstractmethod
    async def extract_text(self, image_bytes: bytes) -> str:
        """Extract text from an image.

        Args:
            image_bytes: Raw image data (PNG, JPEG, TIFF, etc.)

        Returns:
            Extracted text, or empty string on failure.
        """
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether the backend is ready to process images."""
        ...
