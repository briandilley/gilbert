"""Auth service — session management and authentication aggregator.

Discovers all ``authentication_provider`` services and delegates
authentication to them. Manages sessions centrally.
"""

import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.config import AuthConfig
from gilbert.interfaces.auth import (
    AuthBackend,
    AuthInfo,
    LoginMethod,
    TunnelAwareAuthBackend,
    UserBackendAware,
    UserContext,
)
from gilbert.interfaces.configuration import (
    BackendActionProvider,
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
)
from gilbert.interfaces.tools import ToolParameterType

# Name of the built-in local auth backend — always enabled, no toggle,
# no per-backend config section. Every other registered AuthBackend is
# treated generically: its params come from ``backend_config_params()``
# and it's gated by an ``<backend_name>.enabled`` flag.
_LOCAL_BACKEND = "local"

logger = logging.getLogger(__name__)

_SESSIONS = "auth_sessions"


class AuthService(Service):
    """Central authentication and session management.

    Discovers all services with the ``authentication_provider`` capability,
    provides a unified authentication API, and manages session lifecycle.

    Capabilities: ``authentication``.
    Requires: ``users``, ``entity_storage``.
    """

    def __init__(self, config: AuthConfig) -> None:
        self._config = config
        self._storage: StorageBackend | None = None
        self._user_service: Any = None
        self._resolver: ServiceResolver | None = None
        self._backends: dict[str, AuthBackend] = {}
        # When False, the web layer redirects local unauthenticated
        # visitors to /auth/login instead of granting GUEST access, and
        # WebSocket connections without a valid session are refused.
        # Tunnel access is unaffected (always requires login).
        self._allow_guests: bool = True

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="auth",
            capabilities=frozenset({"authentication"}),
            requires=frozenset({"users", "entity_storage"}),
            optional=frozenset({"tunnel", "configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.configuration import ConfigurationReader
        from gilbert.interfaces.storage import StorageProvider

        self._user_service = resolver.require_capability("users")
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("entity_storage capability does not provide StorageProvider")
        self._storage = storage_svc.backend
        self._resolver = resolver

        # ``revoke_user_sessions`` and "list this user's sessions" both
        # filter on ``user_id`` — without an index that's a full scan
        # of every active session.
        await self._storage.ensure_index(
            IndexDefinition(collection=_SESSIONS, fields=["user_id"])
        )

        # Register local auth backend (bundled with core).
        # Other auth backends register themselves via plugins.
        try:
            import gilbert.integrations.local_auth  # noqa: F401
        except ImportError:
            pass

        # Load the auth config section once; each non-local backend
        # gets a subsection keyed on its ``backend_name``.
        auth_section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            auth_section = config_svc.get_section("auth")

        self._allow_guests = bool(auth_section.get("allow_guests", True))

        tunnel = resolver.get_capability("tunnel")
        registry = AuthBackend.registered_backends()

        for name, cls in registry.items():
            if name == _LOCAL_BACKEND:
                # Always enabled; no config section, but needs the user
                # backend injected for password verification.
                instance = cls()
                await instance.initialize({})
                if isinstance(instance, UserBackendAware):
                    instance.set_user_backend(self._user_service.backend)
                self._backends[name] = instance
                continue

            sub = auth_section.get(name, {})
            if not isinstance(sub, dict) or not sub.get("enabled"):
                continue

            instance = cls()
            await instance.initialize(sub)
            if tunnel is not None and isinstance(instance, TunnelAwareAuthBackend):
                instance.set_tunnel(tunnel)
            self._backends[name] = instance

        logger.info(
            "Auth service started — %d backend(s): %s",
            len(self._backends),
            ", ".join(self._backends.keys()),
        )

    async def stop(self) -> None:
        pass

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "auth"

    @property
    def config_category(self) -> str:
        return "Security"

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="session_ttl_seconds",
                type=ToolParameterType.INTEGER,
                description="Session time-to-live in seconds.",
                default=86400,
            ),
            ConfigParam(
                key="default_roles",
                type=ToolParameterType.ARRAY,
                description="Default roles assigned to new users.",
                default=["user"],
            ),
            ConfigParam(
                key="allow_user_creation",
                type=ToolParameterType.BOOLEAN,
                description="Whether new users can be created on first login.",
                default=True,
            ),
            ConfigParam(
                key="allow_guests",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Allow unauthenticated visitors on the local network to use "
                    "Gilbert as a guest. When off, every request requires a login. "
                    "Tunnel access always requires login regardless of this setting."
                ),
                default=True,
            ),
            ConfigParam(
                key="root_password",
                type=ToolParameterType.STRING,
                description="Root admin password.",
                default="",
                restart_required=True,
                sensitive=True,
            ),
        ]
        # Per-backend params: discovered from the registry so adding a
        # new auth backend (local, google, github, ldap, …) needs zero
        # changes in core. Local is always on and gets no enabled
        # toggle or config section.
        for name, cls in AuthBackend.registered_backends().items():
            if name == _LOCAL_BACKEND:
                continue
            params.append(
                ConfigParam(
                    key=f"{name}.enabled",
                    type=ToolParameterType.BOOLEAN,
                    description=f"Enable the {name} auth backend.",
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
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        ttl = config.get("session_ttl_seconds")
        if ttl is not None:
            self._config.session_ttl_seconds = int(ttl)
        if "allow_guests" in config:
            self._allow_guests = bool(config["allow_guests"])

    # --- GuestPolicy protocol ---

    def is_guest_allowed(self) -> bool:
        return self._allow_guests

    # --- ConfigActionProvider ---
    #
    # Auth hosts multiple live backends at once (local + any number of
    # plugin-provided ones). Each backend's actions are surfaced with
    # the key prefixed by ``<backend_name>.`` so they're routable back
    # on invoke and so two backends can legitimately declare the same
    # leaf name (e.g. ``test_connection``) without colliding.

    def config_actions(self) -> list[ConfigAction]:
        actions: list[ConfigAction] = []
        for name, cls in AuthBackend.registered_backends().items():
            source: BackendActionProvider | None = None
            live = self._backends.get(name)
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
                message=f"Malformed auth action key '{key}' — expected '<backend>.<action>'",
            )
        backend = self._backends.get(backend_name)
        if backend is None:
            return ConfigActionResult(
                status="error",
                message=f"Auth backend '{backend_name}' is not running — enable it first.",
            )
        if not isinstance(backend, BackendActionProvider):
            return ConfigActionResult(
                status="error",
                message=f"Auth backend '{backend_name}' does not support actions.",
            )
        return await backend.invoke_backend_action(action_key, payload)

    # ---- Backend access ----

    def get_login_methods(self) -> list[LoginMethod]:
        """Return login methods from all auth backends."""
        return [b.get_login_method() for b in self._backends.values()]

    def get_backend(self, provider_type: str) -> AuthBackend | None:
        """Get a specific auth backend by type."""
        return self._backends.get(provider_type)

    # Legacy alias
    def get_provider(self, provider_type: str) -> AuthBackend | None:
        return self.get_backend(provider_type)

    # ---- Authentication ----

    async def authenticate(
        self, provider_type: str, credentials: dict[str, Any]
    ) -> UserContext | None:
        """Authenticate via a specific provider.

        On success: resolves/creates local user, creates session, returns UserContext.
        """
        provider = self.get_provider(provider_type)
        if provider is None:
            logger.warning("Auth attempt with unknown provider: %s", provider_type)
            return None

        auth_info = await provider.authenticate(credentials)
        if auth_info is None:
            return None

        return await self._finalize_auth(auth_info, provider_type)

    async def handle_callback(
        self, provider_type: str, params: dict[str, Any]
    ) -> UserContext | None:
        """Handle an external auth callback (e.g., OAuth redirect)."""
        provider = self.get_provider(provider_type)
        if provider is None:
            logger.warning("Callback for unknown provider: %s", provider_type)
            return None

        auth_info = await provider.handle_callback(params)
        if auth_info is None:
            return None

        return await self._finalize_auth(auth_info, provider_type)

    async def _finalize_auth(self, auth_info: AuthInfo, provider_type: str) -> UserContext | None:
        """Resolve local user, update last_login, create session."""
        user = await self._resolve_local_user(auth_info)
        if user is None:
            return None

        user_id = user["_id"]
        await self._user_service.backend.update_user(
            user_id, {"last_login": datetime.now(UTC).isoformat()}
        )

        session_id = await self._create_session(user_id, provider_type)

        return UserContext(
            user_id=user_id,
            email=user["email"],
            display_name=user.get("display_name", ""),
            roles=frozenset(user.get("roles", [])),
            provider=provider_type,
            session_id=session_id,
        )

    async def _resolve_local_user(self, auth_info: AuthInfo) -> dict[str, Any] | None:
        """Find or create a local user from auth info."""
        backend = self._user_service.backend

        # 1. Try provider link lookup.
        user: dict[str, Any] | None = await backend.get_user_by_provider_link(
            auth_info.provider_type, auth_info.provider_user_id
        )
        if user is not None:
            return dict(user)

        # 2. Try email lookup (link if found).
        user = await backend.get_user_by_email(auth_info.email)
        if user is not None:
            if not user.get("is_root", False):
                await self._user_service.add_provider_link(
                    user["_id"], auth_info.provider_type, auth_info.provider_user_id
                )
            return dict(user)

        # 3. Create new local user.
        import uuid

        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        user = await self._user_service.create_user(
            user_id,
            {
                "email": auth_info.email,
                "display_name": auth_info.display_name,
                "provider_links": [
                    {
                        "provider_type": auth_info.provider_type,
                        "provider_user_id": auth_info.provider_user_id,
                    }
                ],
            },
        )
        logger.info(
            "Created local user %s from %s provider",
            user_id,
            auth_info.provider_type,
        )
        return dict(user) if user is not None else None

    # ---- Session management ----

    async def _create_session(self, user_id: str, provider: str) -> str:
        assert self._storage is not None
        session_id = f"sess_{secrets.token_urlsafe(32)}"
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._config.session_ttl_seconds)
        await self._storage.put(
            _SESSIONS,
            session_id,
            {
                "user_id": user_id,
                "provider": provider,
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            },
        )
        return session_id

    async def validate_session(self, session_id: str) -> UserContext | None:
        if not session_id or self._storage is None:
            return None

        session = await self._storage.get(_SESSIONS, session_id)
        if session is None:
            return None

        expires_str = session.get("expires_at", "")
        if expires_str:
            expires = datetime.fromisoformat(expires_str)
            if datetime.now(UTC) > expires:
                await self._storage.delete(_SESSIONS, session_id)
                return None

        user_id = session.get("user_id", "")
        user = await self._user_service.backend.get_user(user_id)
        if user is None:
            await self._storage.delete(_SESSIONS, session_id)
            return None

        return UserContext(
            user_id=user["_id"],
            email=user["email"],
            display_name=user.get("display_name", ""),
            roles=frozenset(user.get("roles", [])),
            provider=session.get("provider", "local"),
            session_id=session_id,
        )

    async def invalidate_session(self, session_id: str) -> None:
        if self._storage is not None:
            await self._storage.delete(_SESSIONS, session_id)

    async def revoke_user_sessions(
        self, user_id: str, except_session_id: str | None = None
    ) -> int:
        """Delete every active session for ``user_id``.

        Pass ``except_session_id`` to keep one session alive — used by
        ``change_password`` so the caller stays logged in on the
        device they just changed the password from.

        Returns the number of sessions revoked.
        """
        if self._storage is None or not user_id:
            return 0
        sessions = await self._storage.query(
            Query(
                collection=_SESSIONS,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        revoked = 0
        for s in sessions:
            sid = s.get("_id")
            if not sid or sid == except_session_id:
                continue
            await self._storage.delete(_SESSIONS, sid)
            revoked += 1
        return revoked

    async def change_password(
        self,
        user_id: str,
        old_password: str,
        new_password: str,
        keep_session_id: str | None = None,
    ) -> None:
        """Change a local user's password after verifying the old one.

        Only works for users who already have a ``password_hash`` —
        i.e., users created or seeded with local-auth credentials.
        Users authenticated solely through an external provider (e.g.
        Google) get a clear error and must set up a password through
        an admin reset first.

        On success, every session for the user is invalidated except
        ``keep_session_id`` (typically the caller's current session).

        Raises ``ValueError`` with a user-safe message on any failure.
        """
        from gilbert.interfaces.auth import PasswordHasher

        if not new_password or len(new_password) < 8:
            raise ValueError("New password must be at least 8 characters.")

        local = self._backends.get(_LOCAL_BACKEND)
        if not isinstance(local, PasswordHasher):
            raise ValueError("Local authentication is not available.")

        backend = self._user_service.backend
        user = await backend.get_user(user_id)
        if user is None:
            raise ValueError("User not found.")

        stored_hash = user.get("password_hash", "")
        if not stored_hash:
            raise ValueError(
                "This account has no password set — sign in with the "
                "external provider you originally used, or ask an admin "
                "to set an initial password."
            )

        if not local.verify_password(stored_hash, old_password):
            raise ValueError("Current password is incorrect.")

        new_hash = local.hash_password(new_password)
        await backend.update_user(user_id, {"password_hash": new_hash})

        await self.revoke_user_sessions(user_id, except_session_id=keep_session_id)
        logger.info("Password changed for user %s", user_id)

    async def user_has_password(self, user_id: str) -> bool:
        """True if ``user_id`` has a local password set.

        Used by ``/auth/me`` so the SPA can decide whether to surface
        a "Change password" form for the current user.
        """
        if not user_id or self._user_service is None:
            return False
        user = await self._user_service.backend.get_user(user_id)
        if user is None:
            return False
        return bool(user.get("password_hash"))
