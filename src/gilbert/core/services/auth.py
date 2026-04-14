"""Auth service — session management and authentication aggregator.

Discovers all ``authentication_provider`` services and delegates
authentication to them. Manages sessions centrally.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.config import AuthConfig
from gilbert.core.services._backend_actions import (
    invoke_backend_action,
    merge_backend_actions,
)
from gilbert.interfaces.auth import (
    AuthBackend,
    AuthInfo,
    LoginMethod,
    UserContext,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import ToolParameterType

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

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="auth",
            capabilities=frozenset({"authentication"}),
            requires=frozenset({"users", "entity_storage"}),
            optional=frozenset({"tunnel", "configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.storage import StorageProvider

        self._user_service = resolver.require_capability("users")
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("entity_storage capability does not provide StorageProvider")
        self._storage = storage_svc.backend
        self._resolver = resolver

        # Load config for auth backends
        google_oauth_config: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section("auth")
                google_oauth_config = section.get("google_oauth", {})
                if not isinstance(google_oauth_config, dict):
                    google_oauth_config = {}

        # Register local auth backend (bundled with core).
        # Google/other auth backends register themselves via plugins.
        try:
            import gilbert.integrations.local_auth  # noqa: F401
        except ImportError:
            pass

        backends = AuthBackend.registered_backends()

        # Local auth backend — always enabled
        local_cls = backends.get("local")
        if local_cls:
            local = local_cls()
            await local.initialize({})
            local.set_user_backend(self._user_service.backend)
            self._backends["local"] = local

        # Google OAuth backend — if enabled and configured
        if google_oauth_config.get("enabled") and google_oauth_config.get("client_id"):
            google_cls = backends.get("google")
            if google_cls:
                google = google_cls()
                await google.initialize(google_oauth_config)
                tunnel = resolver.get_capability("tunnel")
                if tunnel:
                    google.set_tunnel(tunnel)
                self._backends["google"] = google

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
        return [
            ConfigParam(
                key="session_ttl_seconds", type=ToolParameterType.INTEGER,
                description="Session time-to-live in seconds.",
                default=86400,
            ),
            ConfigParam(
                key="default_roles", type=ToolParameterType.ARRAY,
                description="Default roles assigned to new users.",
                default=["user"],
            ),
            ConfigParam(
                key="allow_user_creation", type=ToolParameterType.BOOLEAN,
                description="Whether new users can be created on first login.",
                default=True,
            ),
            ConfigParam(
                key="root_password", type=ToolParameterType.STRING,
                description="Root admin password.",
                default="", restart_required=True, sensitive=True,
            ),
            # Google OAuth backend
            ConfigParam(
                key="google_oauth.enabled", type=ToolParameterType.BOOLEAN,
                description="Enable Google OAuth sign-in.",
                default=False, restart_required=True, backend_param=True,
            ),
            ConfigParam(
                key="google_oauth.client_id", type=ToolParameterType.STRING,
                description="Google OAuth client ID.",
                restart_required=True, sensitive=True, backend_param=True,
            ),
            ConfigParam(
                key="google_oauth.client_secret", type=ToolParameterType.STRING,
                description="Google OAuth client secret.",
                restart_required=True, sensitive=True, backend_param=True,
            ),
            ConfigParam(
                key="google_oauth.domain", type=ToolParameterType.STRING,
                description="Restrict Google login to this domain (empty = any).",
                default="", restart_required=True, backend_param=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        ttl = config.get("session_ttl_seconds")
        if ttl is not None:
            self._config.session_ttl_seconds = int(ttl)

    # --- ConfigActionProvider ---
    #
    # Auth service hosts multiple backends (e.g. local + google). Expose
    # the google backend's actions since local auth has nothing upstream
    # to test. If no google backend is live, fall back to the registry
    # class so the action still appears on the settings page.

    def config_actions(self) -> list[ConfigAction]:
        google_backend = self._backends.get("google")
        fallback_cls = AuthBackend.registered_backends().get("google")
        return merge_backend_actions(google_backend, fallback_cls)

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backends.get("google"), key, payload)

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

    async def _finalize_auth(
        self, auth_info: AuthInfo, provider_type: str
    ) -> UserContext | None:
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
        user = await backend.get_user_by_provider_link(
            auth_info.provider_type, auth_info.provider_user_id
        )
        if user is not None:
            return user

        # 2. Try email lookup (link if found).
        user = await backend.get_user_by_email(auth_info.email)
        if user is not None:
            if not user.get("is_root", False):
                await self._user_service.add_provider_link(
                    user["_id"], auth_info.provider_type, auth_info.provider_user_id
                )
            return user

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
        return user

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
