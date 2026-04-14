"""Authentication interfaces — UserContext, AuthInfo, and AuthBackend ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


@dataclass(frozen=True)
class UserContext:
    """Immutable identity of the current user flowing through the system.

    Set at the request boundary (web middleware) or passed explicitly
    to service methods. The SYSTEM sentinel represents unauthenticated
    or system-level operations.
    """

    user_id: str
    email: str
    display_name: str
    roles: frozenset[str] = field(default_factory=frozenset)
    provider: str = "local"
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    SYSTEM: ClassVar[UserContext]
    GUEST: ClassVar[UserContext]


# Sentinel for unauthenticated / system-level operations.
UserContext.SYSTEM = UserContext(
    user_id="system",
    email="system@localhost",
    display_name="System",
    roles=frozenset(),
    provider="system",
)

# Sentinel for unauthenticated local visitors — has "everyone" role.
UserContext.GUEST = UserContext(
    user_id="guest",
    email="",
    display_name="Guest",
    roles=frozenset({"everyone"}),
    provider="local",
)


@dataclass(frozen=True)
class AuthInfo:
    """Result returned by an AuthBackend after successful authentication.

    Contains provider-specific identity information that the AuthService
    uses to resolve or create a local user.
    """

    provider_type: str
    provider_user_id: str
    email: str
    display_name: str
    roles: frozenset[str] = field(default_factory=frozenset)
    raw: dict[str, Any] = field(default_factory=dict)


class AuthBackend(ABC):
    """Abstract authentication provider (backend).

    Concrete implementations handle a specific authentication mechanism
    (local passwords, Google OAuth, etc.). Multiple providers are
    aggregated by the AuthService.
    """

    _registry: dict[str, type["AuthBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            AuthBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["AuthBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """Unique identifier for this provider (e.g., 'local', 'google')."""

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize with provider-specific configuration."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""

    @abstractmethod
    async def authenticate(self, credentials: dict[str, Any]) -> AuthInfo | None:
        """Authenticate with provider-specific credentials.

        Returns AuthInfo on success, None on failure.
        """

    async def handle_callback(self, params: dict[str, Any]) -> AuthInfo | None:
        """Handle an OAuth/external callback. Default: not supported."""
        return None

    async def sync_users(self) -> list[AuthInfo]:
        """Pull users from an external system. Default: no-op (local providers)."""
        return []

    async def get_role_mappings(self) -> dict[str, str]:
        """Map external groups/roles to Gilbert roles. Default: empty."""
        return {}


@dataclass
class LoginMethod:
    """Describes a login option to render on the login page.

    Each AuthBackend provides one of these so the login page
    can display all available authentication options.
    """

    provider_type: str
    display_name: str
    # "form" = render an email/password form
    # "redirect" = render a button that redirects to an external auth URL
    method: str  # "form" or "redirect"
    # For "redirect" methods: the URL to redirect to
    redirect_url: str = ""
    # For "form" methods: the URL to POST the form to
    form_action: str = ""


@runtime_checkable
class AccessControlProvider(Protocol):
    """Protocol for role-based access control queries.

    Services and the web layer resolve this via
    ``get_capability("access_control")`` to check permissions without
    depending on the concrete AccessControlService.
    """

    def get_role_level(self, role_name: str) -> int:
        """Get the numeric level for a role name."""
        ...

    def get_effective_level(self, user_ctx: UserContext) -> int:
        """Get the user's effective permission level (lowest = most privileged)."""
        ...

    def resolve_rpc_level(self, frame_type: str) -> int:
        """Resolve the required role level for an RPC frame type."""
        ...
