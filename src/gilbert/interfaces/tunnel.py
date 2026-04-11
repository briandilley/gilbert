"""Tunnel backend interface — provide public HTTPS URLs for the local server."""

from abc import ABC, abstractmethod
from typing import Any


class TunnelBackend(ABC):
    """Abstract tunnel backend. Implementation-agnostic."""

    _registry: dict[str, type["TunnelBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            TunnelBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["TunnelBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def connect(self, local_port: int, config: dict[str, Any]) -> str:
        """Start the tunnel and return the public HTTPS URL."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the tunnel."""
        ...
