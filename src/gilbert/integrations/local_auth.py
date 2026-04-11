"""Local authentication backend — password-based auth via argon2."""

import logging
from typing import Any

from gilbert.interfaces.auth import (
    AuthBackend,
    AuthInfo,
    LoginMethod,
)

logger = logging.getLogger(__name__)


class LocalAuthBackend(AuthBackend):
    """Authenticates users against locally stored password hashes."""

    backend_name = "local"

    def __init__(self) -> None:
        self._users: Any = None  # UserBackend
        self._hasher: Any = None

    @property
    def provider_type(self) -> str:
        return "local"

    async def initialize(self, config: dict[str, Any]) -> None:
        from argon2 import PasswordHasher

        self._hasher = PasswordHasher()
        # _users is set by AuthService after initialization
        logger.info("Local auth backend initialized")

    async def close(self) -> None:
        pass

    def set_user_backend(self, user_backend: Any) -> None:
        """Set the user backend for password verification."""
        self._users = user_backend

    def get_login_method(self) -> LoginMethod:
        return LoginMethod(
            provider_type="local",
            display_name="Sign In",
            method="form",
            form_action="/auth/login/local",
        )

    async def authenticate(self, credentials: dict[str, Any]) -> AuthInfo | None:
        if self._users is None:
            return None

        identifier = credentials.get("email", "")
        password = credentials.get("password", "")
        if not identifier or not password:
            return None

        user = await self._users.get_user_by_username(identifier)
        if user is None:
            user = await self._users.get_user_by_email(identifier)
        if user is None:
            return None

        stored_hash = user.get("password_hash", "")
        if not stored_hash:
            return None

        if not self._verify_password(stored_hash, password):
            return None

        return AuthInfo(
            provider_type="local",
            provider_user_id=user["_id"],
            email=user.get("email", ""),
            display_name=user.get("display_name", ""),
            roles=frozenset(user.get("roles", [])),
        )

    def hash_password(self, password: str) -> str:
        if self._hasher is None:
            from argon2 import PasswordHasher

            self._hasher = PasswordHasher()
        return self._hasher.hash(password)

    def _verify_password(self, stored_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(stored_hash, password)
        except Exception:
            logger.debug("Password verification failed")
            return False
