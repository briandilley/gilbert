"""User service — manages local user accounts with external provider sync."""

import json
import logging
import time
import uuid
from dataclasses import replace
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    BackendActionProvider,
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend, StorageProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.users import ExternalUser, UserBackend, UserProviderBackend

logger = logging.getLogger(__name__)

_ROOT_USER_ID = "root"
_ROOT_USERNAME = "root"
_ROOT_EMAIL = ""


class UserService(Service):
    """Wraps a UserBackend as a discoverable service.

    Always registered (users are foundational). On startup ensures the
    root user exists. Discovers UserProviderBackend instances to sync
    external users on demand.
    """

    # Default: refresh from providers at most once per hour
    _DEFAULT_SYNC_TTL_SECONDS = 3600

    def __init__(
        self,
        root_password_hash: str = "",
        default_roles: list[str] | None = None,
        sync_ttl_seconds: int | None = None,
        allow_user_creation: bool = True,
    ) -> None:
        self._root_password_hash = root_password_hash
        self._default_roles = default_roles or ["user"]
        self._sync_ttl = (
            sync_ttl_seconds if sync_ttl_seconds is not None else self._DEFAULT_SYNC_TTL_SECONDS
        )
        self._allow_user_creation = allow_user_creation
        self._backend: UserBackend | None = None
        self._resolver: ServiceResolver | None = None
        self._last_sync: float = 0.0  # monotonic timestamp of last provider sync
        # Live provider backends, keyed on ``backend_name`` from the
        # registry (e.g. ``"google_directory"``). Populated in start()
        # by discovering every registered UserProviderBackend whose
        # per-backend ``enabled`` flag is set.
        self._provider_backends: dict[str, UserProviderBackend] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="users",
            capabilities=frozenset({"users", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"user_provider"}),
        )

    @property
    def allow_user_creation(self) -> bool:
        """Whether new user creation is allowed."""
        return self._allow_user_creation

    @property
    def backend(self) -> UserBackend:
        if self._backend is None:
            raise RuntimeError("UserService not started")
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.configuration import ConfigurationReader

        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("entity_storage capability does not provide StorageProvider")
        storage: StorageBackend = storage_svc.backend

        from gilbert.storage.user_storage import StorageUserBackend

        backend = StorageUserBackend(storage)
        await backend.ensure_indexes()
        self._backend = backend
        self._resolver = resolver

        # Load config — sync TTL, allow_user_creation, and each
        # registered provider backend's per-backend subsection.
        users_section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            users_section = config_svc.get_section("users")
            ttl = users_section.get("sync_ttl_seconds")
            if ttl is not None:
                self._sync_ttl = int(ttl)

            auth_section = config_svc.get_section("auth")
            allow = auth_section.get("allow_user_creation")
            if allow is not None:
                self._allow_user_creation = bool(allow)

        # Discover and initialize every user provider whose
        # ``<backend_name>.enabled`` flag is set. Adding a new provider
        # plugin (LDAP, Okta, …) works with zero core changes.
        for name, cls in UserProviderBackend.registered_backends().items():
            sub = users_section.get(name, {})
            if not isinstance(sub, dict) or not sub.get("enabled"):
                continue
            provider = cls()
            await provider.initialize(sub)
            self._provider_backends[name] = provider
            logger.info("User provider '%s' initialized", name)

        await self._ensure_root_user()

    async def _ensure_root_user(self) -> None:
        """Create the root user if missing, or sync its password hash."""
        assert self._backend is not None
        existing = await self._backend.get_user(_ROOT_USER_ID)
        if existing is None:
            logger.info("Creating root user")
            await self._backend.create_user(
                _ROOT_USER_ID,
                {
                    "username": _ROOT_USERNAME,
                    "email": _ROOT_EMAIL,
                    "display_name": "Root",
                    "password_hash": self._root_password_hash,
                    "is_root": True,
                    "roles": ["admin"],
                },
            )
            return

        # Keep the root password in sync with what's in config.
        if self._root_password_hash and existing.get("password_hash") != self._root_password_hash:
            logger.info("Updating root user password hash")
            await self._backend.update_user(
                _ROOT_USER_ID,
                {"password_hash": self._root_password_hash},
            )

    # ---- Provider discovery ----

    def _get_providers(self) -> list[UserProviderBackend]:
        """Return all initialized user provider backends."""
        return list(self._provider_backends.values())

    # ---- Sync from providers ----

    async def _ensure_local_user(self, ext: ExternalUser) -> dict[str, Any]:
        """Ensure an external user has a local equivalent. Returns local user."""
        backend = self.backend

        # 1. Try provider link lookup.
        user = await backend.get_user_by_provider_link(ext.provider_type, ext.provider_user_id)
        if user is not None:
            # Update display name and metadata if changed.
            updates: dict[str, Any] = {}
            if ext.display_name and user.get("display_name") != ext.display_name:
                updates["display_name"] = ext.display_name
            if ext.metadata:
                existing_meta = user.get("metadata", {})
                merged_meta = {**existing_meta, **ext.metadata}
                if merged_meta != existing_meta:
                    updates["metadata"] = merged_meta
            if ext.groups:
                updates.setdefault("metadata", user.get("metadata", {}))
                updates["metadata"]["groups"] = ext.groups
            if updates:
                await backend.update_user(user["_id"], updates)
                user.update(updates)
            return user

        # 2. Try email lookup (link if found).
        user = await backend.get_user_by_email(ext.email)
        if user is not None:
            if not user.get("is_root", False):
                await backend.add_provider_link(
                    user["_id"], ext.provider_type, ext.provider_user_id
                )
                # Cache in provider_users table.
                await backend.put_provider_user(
                    ext.provider_type,
                    ext.provider_user_id,
                    {
                        "local_user_id": user["_id"],
                        "email": ext.email,
                        "display_name": ext.display_name,
                    },
                )
            return user

        # 3. Create new local user.
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        roles = set(ext.roles) | set(self._default_roles)
        data: dict[str, Any] = {
            "email": ext.email,
            "display_name": ext.display_name,
            "roles": sorted(roles),
            "provider_links": [
                {
                    "provider_type": ext.provider_type,
                    "provider_user_id": ext.provider_user_id,
                }
            ],
            "metadata": {**ext.metadata, "groups": ext.groups} if ext.groups else ext.metadata,
        }
        user = await backend.create_user(user_id, data)

        # Cache in provider_users table.
        await backend.put_provider_user(
            ext.provider_type,
            ext.provider_user_id,
            {
                "local_user_id": user_id,
                "email": ext.email,
                "display_name": ext.display_name,
            },
        )

        logger.info(
            "Created local user %s from %s provider (%s)",
            user_id,
            ext.provider_type,
            ext.email,
        )
        return user

    async def sync_providers(self, force: bool = False) -> int:
        """Sync all external providers. Returns total users synced."""
        count = 0
        for provider in self._get_providers():
            try:
                external_users = await provider.list_external_users()
                for ext in external_users:
                    await self._ensure_local_user(ext)
                    count += 1
                logger.info(
                    "Synced %d users from %s provider",
                    len(external_users),
                    provider.provider_type,
                )
            except Exception:
                logger.exception("Failed to sync from %s provider", provider.provider_type)
        self._last_sync = time.monotonic()
        return count

    async def sync_if_stale(self) -> None:
        """Sync providers if the TTL has elapsed since the last sync."""
        elapsed = time.monotonic() - self._last_sync
        if elapsed >= self._sync_ttl:
            await self.sync_providers()

    # ---- Public API (delegates to backend with root-user guards) ----

    async def create_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new user with default roles applied."""
        roles = set(data.get("roles", []))
        roles.update(self._default_roles)
        data["roles"] = sorted(roles)
        return await self.backend.create_user(user_id, data)

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        user = await self.backend.get_user(user_id)
        if user is None:
            # Not found locally — maybe it exists in a provider we haven't synced recently
            await self.sync_if_stale()
            user = await self.backend.get_user(user_id)
        return user

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Get user by email. Checks providers if not found locally."""
        user = await self.backend.get_user_by_email(email)
        if user is not None:
            return user

        # Not found locally — check providers.
        for provider in self._get_providers():
            try:
                ext = await provider.get_external_user_by_email(email)
                if ext is not None:
                    return await self._ensure_local_user(ext)
            except Exception:
                logger.debug(
                    "Provider %s failed email lookup for %s",
                    provider.provider_type,
                    email,
                )
        return None

    async def list_users(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """List users, lazily syncing from providers if stale."""
        await self.sync_if_stale()
        return await self.backend.list_users(limit=limit, offset=offset)

    async def delete_user(self, user_id: str) -> None:
        if user_id == _ROOT_USER_ID:
            raise ValueError("Cannot delete the root user")
        await self.backend.delete_user(user_id)

    async def add_provider_link(
        self, user_id: str, provider_type: str, provider_user_id: str
    ) -> None:
        if user_id == _ROOT_USER_ID:
            raise ValueError("Cannot link external providers to the root user")
        await self.backend.add_provider_link(user_id, provider_type, provider_user_id)

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "users"

    @property
    def config_category(self) -> str:
        return "Security"

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="sync_ttl_seconds",
                type=ToolParameterType.INTEGER,
                description="How often to refresh users from external providers (seconds).",
                default=3600,
            ),
        ]
        # Per-provider params: discovered from the registry so adding
        # a new user-provider plugin (google_directory, ldap, okta, …)
        # needs zero changes in core.
        for name, cls in UserProviderBackend.registered_backends().items():
            params.append(
                ConfigParam(
                    key=f"{name}.enabled",
                    type=ToolParameterType.BOOLEAN,
                    description=f"Enable the {name} user provider.",
                    default=False,
                    restart_required=True,
                    backend_param=True,
                )
            )
            for bp in cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"{name}.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        multiline=bp.multiline,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        ttl = config.get("sync_ttl_seconds")
        if ttl is not None:
            self._sync_ttl = int(ttl)

    # --- ConfigActionProvider ---
    #
    # UserService can host multiple live provider backends at once,
    # so each backend's actions are surfaced with the key prefixed by
    # ``<backend_name>.`` — routable back on invoke, and two providers
    # can legitimately declare the same leaf name (e.g.
    # ``test_connection``) without colliding.

    def config_actions(self) -> list[ConfigAction]:
        actions: list[ConfigAction] = []
        for name, cls in UserProviderBackend.registered_backends().items():
            source: BackendActionProvider | None = None
            live = self._provider_backends.get(name)
            if isinstance(live, BackendActionProvider):
                source = live
            else:
                try:
                    probe = cls()
                except Exception:
                    continue
                if isinstance(probe, BackendActionProvider):
                    source = probe
            if source is None:
                continue
            try:
                raw = source.backend_actions()
            except Exception:
                continue
            for a in raw:
                actions.append(
                    replace(
                        a,
                        key=f"{name}.{a.key}",
                        backend_action=True,
                        backend=name,
                    )
                )
        return actions

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        backend_name, _, action_key = key.partition(".")
        if not backend_name or not action_key:
            return ConfigActionResult(
                status="error",
                message=f"Malformed provider action key '{key}' — expected '<backend>.<action>'",
            )
        provider = self._provider_backends.get(backend_name)
        if provider is None:
            return ConfigActionResult(
                status="error",
                message=f"User provider '{backend_name}' is not running — enable it first.",
            )
        if not isinstance(provider, BackendActionProvider):
            return ConfigActionResult(
                status="error",
                message=f"User provider '{backend_name}' does not support actions.",
            )
        return await provider.invoke_backend_action(action_key, payload)

    # --- WebSocket RPC handlers ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "users.user.create": self._ws_user_create,
            "users.user.delete": self._ws_user_delete,
            "users.user.reset_password": self._ws_user_reset_password,
        }

    async def _ws_user_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        if not self._allow_user_creation:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "User creation is disabled",
                "code": 403,
            }

        username = (frame.get("username") or "").strip().lower()
        if not username:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Username is required",
                "code": 400,
            }

        password = frame.get("password") or ""
        if not password:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Password is required",
                "code": 400,
            }

        # Check uniqueness
        existing = await self.backend.get_user_by_username(username)
        if existing is not None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"Username '{username}' already exists",
                "code": 409,
            }

        email = (frame.get("email") or "").strip()
        if email:
            existing = await self.backend.get_user_by_email(email)
            if existing is not None:
                return {
                    "type": "gilbert.error",
                    "ref": frame.get("id"),
                    "error": f"Email '{email}' already in use",
                    "code": 409,
                }

        password_hash = self._hash_password(password)
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        user = await self.create_user(
            user_id,
            {
                "username": username,
                "email": email,
                "display_name": (frame.get("display_name") or "").strip(),
                "password_hash": password_hash,
            },
        )
        user.pop("password_hash", None)
        return {
            "type": "users.user.create.result",
            "ref": frame.get("id"),
            "status": "ok",
            "user": user,
        }

    async def _ws_user_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        user_id = frame.get("user_id", "")
        if not user_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "user_id is required",
                "code": 400,
            }
        try:
            await self.delete_user(user_id)
        except ValueError as e:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": str(e),
                "code": 403,
            }
        return {"type": "users.user.delete.result", "ref": frame.get("id"), "status": "ok"}

    async def _ws_user_reset_password(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        user_id = frame.get("user_id", "")
        if not user_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "user_id is required",
                "code": 400,
            }
        password = frame.get("password", "")
        if not password:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "password is required",
                "code": 400,
            }
        password_hash = self._hash_password(password)
        assert self._backend is not None
        await self._backend.update_user(user_id, {"password_hash": password_hash})
        return {"type": "users.user.reset_password.result", "ref": frame.get("id"), "status": "ok"}

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "users"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="list_users",
                slash_group="user",
                slash_command="list",
                slash_help="List all users: /user list [limit]",
                description="List all users (syncs from external providers first).",
                parameters=[
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Maximum number of users to return.",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="get_user",
                slash_group="user",
                slash_command="get",
                slash_help="Look up a user: /user get <user_id-or-email>",
                description="Get a user by ID or email address.",
                parameters=[
                    ToolParameter(
                        name="user_id",
                        type=ToolParameterType.STRING,
                        description="The user ID or email to look up.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="create_user",
                slash_group="user",
                slash_command="create",
                slash_help=(
                    "Create a local user: /user create <username> "
                    "<display_name> <password> email=..."
                ),
                description="Create a new local user account.",
                parameters=[
                    ToolParameter(
                        name="username",
                        type=ToolParameterType.STRING,
                        description="Unique username for login.",
                    ),
                    ToolParameter(
                        name="email",
                        type=ToolParameterType.STRING,
                        description="User email address (optional).",
                        required=False,
                    ),
                    ToolParameter(
                        name="display_name",
                        type=ToolParameterType.STRING,
                        description="Display name for the user.",
                    ),
                    ToolParameter(
                        name="password",
                        type=ToolParameterType.STRING,
                        description="Password for the account.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="sync_users",
                slash_group="user",
                slash_command="sync",
                slash_help="Sync users from external providers: /user sync",
                description="Sync users from all external providers (e.g., Google Workspace).",
                parameters=[],
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "list_users":
                return await self._tool_list_users(arguments)
            case "get_user":
                return await self._tool_get_user(arguments)
            case "create_user":
                return await self._tool_create_user(arguments)
            case "sync_users":
                return await self._tool_sync_users()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_list_users(self, arguments: dict[str, Any]) -> str:
        limit = arguments.get("limit")
        users = await self.list_users(limit=limit)
        for u in users:
            u.pop("password_hash", None)
        return json.dumps(users)

    async def _tool_get_user(self, arguments: dict[str, Any]) -> str:
        identifier = arguments["user_id"]
        # Try by ID first, then username, then email.
        user = await self.backend.get_user(identifier)
        if user is None:
            user = await self.backend.get_user_by_username(identifier)
        if user is None:
            user = await self.get_user_by_email(identifier)
        if user is None:
            return json.dumps({"error": f"User not found: {identifier}"})
        user.pop("password_hash", None)
        return json.dumps(user)

    async def _tool_create_user(self, arguments: dict[str, Any]) -> str:
        username = arguments["username"].strip().lower()
        password = arguments.get("password", "")

        # Hash password if provided
        password_hash = ""
        if password:
            password_hash = self._hash_password(password)

        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        user = await self.create_user(
            user_id,
            {
                "username": username,
                "email": arguments.get("email", ""),
                "display_name": arguments.get("display_name", ""),
                "password_hash": password_hash,
            },
        )
        user.pop("password_hash", None)
        return json.dumps({"status": "ok", "user": user})

    @staticmethod
    def _hash_password(password: str) -> str:
        """Hash a password with argon2id."""
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)

    async def _tool_sync_users(self) -> str:
        count = await self.sync_providers()
        return json.dumps({"status": "ok", "synced": count})
