"""User backend interface — ABC for user CRUD, provider links, and roles."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
    def backend_config_params(cls) -> list["ConfigParam"]:
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
    async def get_external_user(
        self, provider_user_id: str
    ) -> ExternalUser | None:
        """Fetch a single user by their external ID."""

    async def get_external_user_by_email(
        self, email: str
    ) -> ExternalUser | None:
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
    async def list_users(
        self, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
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

    @property
    def backend(self) -> UserBackend:
        """Access the user storage backend."""
        ...
