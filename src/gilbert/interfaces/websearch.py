"""Web search interface — backend-agnostic search abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebSearchResult:
    """A single search result."""

    title: str
    url: str
    snippet: str


class WebSearchBackend(ABC):
    """Abstract interface for web search providers."""

    _registry: dict[str, type["WebSearchBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            WebSearchBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["WebSearchBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize the backend with configuration (including API key)."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""

    @abstractmethod
    async def search(
        self, query: str, count: int = 5,
    ) -> list[WebSearchResult]:
        """Execute a web search and return results."""
