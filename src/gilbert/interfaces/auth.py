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

    _registry: dict[str, type[AuthBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            AuthBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[AuthBackend]]:
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

    def get_login_method(self) -> LoginMethod:
        """Return the login method this backend advertises to the UI.

        Concrete backends override this with their own
        ``provider_type``, display name, form action, etc. The base
        implementation provides a generic fallback so the codebase
        can typecheck against ``AuthBackend`` instead of narrowing to
        concrete classes at every call site.
        """
        return LoginMethod(
            provider_type=getattr(self, "provider_type", "unknown"),
            display_name=getattr(self, "backend_name", "unknown"),
            method="form",
        )


@runtime_checkable
class UserBackendAware(Protocol):
    """Protocol for auth backends that need the users backend injected.

    Local auth verifies credentials against the user store, so the
    AuthService injects its UserBackend after ``initialize()``. OAuth
    backends don't need this. Implement this protocol to opt in.
    """

    def set_user_backend(self, user_backend: Any) -> None:
        """Receive the user storage backend for password verification etc."""
        ...


@runtime_checkable
class TunnelAwareAuthBackend(Protocol):
    """Protocol for auth backends that need the tunnel service injected.

    OAuth backends need the public tunnel URL to build valid redirect
    URIs. The AuthService injects a ``TunnelProvider`` after
    ``initialize()`` on any backend that satisfies this protocol.
    """

    def set_tunnel(self, tunnel: Any) -> None:
        """Receive the tunnel provider for building public callback URLs."""
        ...


@runtime_checkable
class OAuthLoginBackend(Protocol):
    """Protocol for auth backends that drive a redirect-based external
    login flow (OAuth2 authorization code grant, or anything shaped
    the same way: an authorization URL, a callback URL, and a code
    that the backend exchanges for user identity).

    The generic ``/auth/login/<provider_type>/start`` and
    ``/auth/login/<provider_type>/callback`` routes in
    ``web/routes/auth.py`` use this protocol to dispatch — core knows
    nothing about any specific provider. Plugins that implement OAuth
    auth (Google, GitHub, Okta, …) just satisfy this protocol and
    their flow lights up automatically.
    """

    def get_callback_url(self, request_base_url: str) -> str:
        """Return the URL the external service should redirect back to.

        Implementations typically prefer the public tunnel URL (for
        reachability from the OAuth provider's servers) and fall back
        to ``request_base_url`` — the origin of the current browser
        request — when no tunnel is available.
        """
        ...

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Return the external service's authorization URL for the
        browser to be redirected to. ``state`` is an opaque token
        that must round-trip back unchanged."""
        ...


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
class LoginMethodProvider(Protocol):
    """Protocol for the auth service's login-method enumeration.

    The unauthenticated ``/api/auth/methods`` route uses this to render
    the login page without importing the concrete auth service.
    """

    def get_login_methods(self) -> list[LoginMethod]:
        ...


@runtime_checkable
class SessionValidator(Protocol):
    """Protocol for resolving a session id into a ``UserContext``.

    Used by the WebSocket route to authenticate inbound connections
    without importing the concrete auth service.
    """

    async def validate_session(self, session_id: str) -> UserContext | None:
        ...


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

    def check_collection_read(self, user_ctx: UserContext, collection: str) -> bool:
        """Return True if the user can read from the entity collection."""
        ...

    def check_collection_write(self, user_ctx: UserContext, collection: str) -> bool:
        """Return True if the user can write to the entity collection."""
        ...
