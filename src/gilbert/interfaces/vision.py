"""Vision backend interface — image understanding abstraction."""

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


@runtime_checkable
class VisionProvider(Protocol):
    """Capability protocol for cross-service image-description access.

    Other services (notably the camera service) resolve this via
    ``resolver.get_capability("vision")`` and ``isinstance``-check
    against ``VisionProvider`` rather than coupling to the concrete
    ``VisionService`` class.

    The minimal surface (``describe_image``) is intentional — this
    protocol exists so the camera service can request a snapshot
    description without depending on every existing ``VisionBackend``
    implementation. Properties like ``model_name`` / ``available`` are
    deferred until a concrete need motivates touching every backend.
    """

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        """Return a text description of the image bytes."""
        ...


class VisionBackend(ABC):
    """Abstract vision backend. Implementation-agnostic."""

    _registry: dict[str, type["VisionBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            VisionBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["VisionBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize with configuration (API key, model, etc.)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...

    @abstractmethod
    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        """Analyze an image and return a text description.

        Args:
            image_bytes: Raw image data (PNG, JPEG, etc.)
            media_type: MIME type — "image/png", "image/jpeg", etc.

        Returns:
            Plain text description, or empty string on failure.
        """
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether the backend is ready to process images."""
        ...
