"""User backend interface — ABC for user CRUD, provider links, and roles."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


class NameMatch(NamedTuple):
    """Result of resolving a free-form name to a user.

    ``confidence`` is 0.0–1.0; higher = stronger match. The canonical
    rubric used by ``UserManagementProvider.resolve_user_id_by_name``:

    - ``1.0`` — exact match against ``display_name`` / ``username``
      (case-insensitive, trimmed).
    - ``0.8`` — match against the first token of the display name.
    - ``0.7`` — match against the email local part (before ``@``).

    Callers pick a threshold appropriate to how much risk they want
    to accept on a wrong match. Greeting / notification paths might
    accept ``>= 0.7``; a destructive action (delete, demote, etc.)
    might insist on ``1.0``.
    """

    user_id: str
    confidence: float


@dataclass
class ExternalUser:
    """A user record from an external provider.

    Used by UserProvider implementations to report users that should
    have local equivalents in Gilbert.
    """

    provider_type: str
    provider_user_id: str
    email: str
    display_name: str = ""
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class UserProviderBackend(ABC):
    """Abstract external user source (backend).

    Implementations (e.g., Google Directory, LDAP) are discovered by the
    UserService and queried to ensure external users have local equivalents.
    """

    _registry: dict[str, type["UserProviderBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            UserProviderBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["UserProviderBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize from config. Override in backends."""

    async def close(self) -> None:
        """Release resources. Override in backends."""

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """Unique identifier for this provider (e.g., ``"google"``)."""

    @abstractmethod
    async def list_external_users(self) -> list[ExternalUser]:
        """Fetch all users from the external source."""

    @abstractmethod
    async def get_external_user(self, provider_user_id: str) -> ExternalUser | None:
        """Fetch a single user by their external ID."""

    async def get_external_user_by_email(self, email: str) -> ExternalUser | None:
        """Fetch a single user by email. Default: linear scan."""
        for user in await self.list_external_users():
            if user.email == email:
                return user
        return None

    async def list_groups(self) -> list[dict[str, Any]]:
        """List groups/teams from the external source. Default: empty."""
        return []


class UserBackend(ABC):
    """Abstract user storage.

    Provides domain-specific operations on top of the generic entity store
    so that services never need to construct raw storage queries for users.
    """

    # ---- User CRUD ----

    @abstractmethod
    async def create_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new user. Returns the stored entity."""

    @abstractmethod
    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        """Get a user by ID, or None."""

    @abstractmethod
    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Look up a user by username (case-insensitive)."""

    @abstractmethod
    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Look up a user by email address."""

    @abstractmethod
    async def get_user_by_provider_link(
        self, provider_type: str, provider_user_id: str
    ) -> dict[str, Any] | None:
        """Find a user linked to an external provider identity."""

    @abstractmethod
    async def update_user(self, user_id: str, data: dict[str, Any]) -> None:
        """Merge *data* into an existing user entity."""

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """Delete a user by ID."""

    @abstractmethod
    async def list_users(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """List users with optional pagination."""

    # ---- Provider links ----

    @abstractmethod
    async def add_provider_link(
        self, user_id: str, provider_type: str, provider_user_id: str
    ) -> None:
        """Link an external provider identity to a local user."""

    @abstractmethod
    async def remove_provider_link(self, user_id: str, provider_type: str) -> None:
        """Remove an external provider link from a user."""

    # ---- Roles ----

    @abstractmethod
    async def set_roles(self, user_id: str, roles: set[str]) -> None:
        """Replace the user's roles with *roles*."""

    @abstractmethod
    async def get_roles(self, user_id: str) -> set[str]:
        """Return the user's current roles."""

    # ---- Provider users (remote user cache) ----

    @abstractmethod
    async def put_provider_user(
        self, provider_type: str, provider_user_id: str, data: dict[str, Any]
    ) -> None:
        """Store or update a remote user entity."""

    @abstractmethod
    async def get_provider_user(
        self, provider_type: str, provider_user_id: str
    ) -> dict[str, Any] | None:
        """Retrieve a cached remote user entity."""

    @abstractmethod
    async def list_provider_users(
        self, provider_type: str, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List cached remote users for a given provider."""


@runtime_checkable
class UserPrefReader(Protocol):
    """Read + write per-user preference values.

    Preferences live in the ``metadata`` dict on the user document
    (the generic per-user-settings escape hatch in ``UserService``).
    Consumers go through this protocol rather than poking at the
    storage shape directly so the storage layout can change without
    breaking every caller.

    Keys are flat strings — namespace by prefix if needed (e.g.
    ``"ui.theme"``). Values are JSON-serializable
    primitives; a service that wants structured prefs should put
    them under a single key as a dict.
    """

    async def get_user_pref(
        self, user_id: str, key: str, default: object = None
    ) -> object:
        """Return the user's value for ``key`` or ``default`` if unset."""
        ...

    async def set_user_pref(self, user_id: str, key: str, value: object) -> None:
        """Persist a pref value on the user's metadata.

        Raises ``KeyError`` if the user doesn't exist.
        """
        ...


@runtime_checkable
class UserManagementProvider(Protocol):
    """Protocol for services providing user management capabilities.

    Used by other services (e.g., access control) to query user data
    without depending on the concrete UserService class.
    """

    @property
    def allow_user_creation(self) -> bool:
        """Whether new user creation is allowed."""
        ...

    async def list_users(self) -> list[dict[str, Any]]:
        """List all users."""
        ...

    async def resolve_user_id_by_name(self, name: str) -> NameMatch | None:
        """Resolve a free-form name to a unique user_id with confidence.

        Matches the input (case-insensitive, trimmed) in priority order:
        full ``display_name`` / ``username`` (confidence ``1.0``), then
        first-name token (``0.8``), then email local part (``0.7``).
        Returns the ``NameMatch`` only when exactly one user matches at
        the highest priority level. ``None`` for empty input, no match,
        or ambiguous match — the caller decides what to do next (ask
        for clarification, fall back, etc.). Callers can also threshold
        on the confidence to ignore weaker matches.
        """
        ...

    @property
    def backend(self) -> UserBackend:
        """Access the user storage backend."""
        ...
