"""Internal-URL backend interface — a LAN-reachable hostname for the
local server.

Distinct from ``TunnelBackend``: a tunnel exposes Gilbert to the public
internet, whereas an internal-URL backend only yields a hostname that
resolves to Gilbert on the local network (typically via a wildcard-DNS
service like sslip.io, e.g. ``192-168-1-50.sslip.io`` → ``192.168.1.50``).

The motivating use case is OAuth redirects: providers like Google reject
raw IP addresses as redirect URIs but accept a real DNS hostname. The
redirect travels through the user's browser, so LAN reachability is
sufficient — the OAuth provider's servers never fetch the URL.
"""

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


class InternalUrlBackend(ABC):
    """Abstract internal-URL backend. Implementation-agnostic."""

    _registry: dict[str, type["InternalUrlBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            InternalUrlBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["InternalUrlBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def resolve(self, local_port: int, scheme: str, config: dict[str, Any]) -> str:
        """Return the LAN-reachable base URL for the local server.

        Args:
            local_port: The port the local server listens on.
            scheme: ``"http"`` or ``"https"`` — the local listener's scheme.
            config: Backend-specific settings (e.g. DNS suffix, IP override).

        Returns:
            A base URL such as ``https://192-168-1-50.sslip.io:8443``.

        Raises:
            RuntimeError: When the hostname can't be derived (e.g. the
            outbound LAN IP couldn't be detected).
        """
        ...


@runtime_checkable
class InternalUrlProvider(Protocol):
    """Protocol for accessing the internal URL from a service.

    NOT interchangeable with ``TunnelProvider``: the URL is only
    reachable from the LAN, never from the public internet. Consumers
    that need internet reachability (webhooks, server-to-server
    callbacks) must use ``TunnelProvider`` instead.
    """

    @property
    def internal_url(self) -> str:
        """The current LAN-reachable base URL, or empty string if unavailable."""
        ...

    def internal_url_for(self, path: str) -> str:
        """Build an internal URL for the given request path, or empty
        string when no internal URL is active."""
        ...
