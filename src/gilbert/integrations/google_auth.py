"""Google OAuth authentication backend.

Authenticates users via Google OAuth 2.0 ID token verification.
Self-contained — reads OAuth client credentials from auth config.
"""

import logging
from typing import Any

from gilbert.interfaces.auth import (
    AuthBackend,
    AuthInfo,
    LoginMethod,
)

logger = logging.getLogger(__name__)


class GoogleAuthBackend(AuthBackend):
    """Authenticates users via Google OAuth ID tokens."""

    backend_name = "google"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="client_id", type=ToolParameterType.STRING,
                description="Google OAuth client ID.",
                restart_required=True, sensitive=True,
            ),
            ConfigParam(
                key="client_secret", type=ToolParameterType.STRING,
                description="Google OAuth client secret.",
                restart_required=True, sensitive=True,
            ),
            ConfigParam(
                key="domain", type=ToolParameterType.STRING,
                description="Restrict Google login to this domain (empty = any).",
                default="",
            ),
        ]

    def __init__(self) -> None:
        self._oauth_client_id: str = ""
        self._client_secret: str = ""
        self._domain: str = ""
        self._tunnel: Any = None

    @property
    def provider_type(self) -> str:
        return "google"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._oauth_client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._domain = config.get("domain", "")

        if not self._oauth_client_id:
            logger.warning("Google OAuth client ID not configured")
        else:
            logger.info("Google auth backend initialized (domain=%s)", self._domain or "(any)")

    async def close(self) -> None:
        pass

    def set_tunnel(self, tunnel: Any) -> None:
        """Set the tunnel service for HTTPS callback URLs."""
        self._tunnel = tunnel

    def get_login_method(self) -> LoginMethod:
        return LoginMethod(
            provider_type="google",
            display_name="Sign in with Google",
            method="redirect",
            redirect_url="/auth/login/google/start",
        )

    def get_callback_url(self, request_base_url: str = "") -> str:
        if self._tunnel and self._tunnel.public_url:
            return self._tunnel.public_url_for("/auth/login/google/callback")
        base = request_base_url.rstrip("/") if request_base_url else ""
        return f"{base}/auth/login/google/callback"

    async def authenticate(self, credentials: dict[str, Any]) -> AuthInfo | None:
        id_token_str = credentials.get("id_token", "")
        if not id_token_str:
            return None

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            if not self._oauth_client_id:
                logger.error("Google OAuth client ID not configured")
                return None

            info = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                self._oauth_client_id,
            )

            email = info.get("email", "")
            if not email:
                logger.warning("Google ID token missing email claim")
                return None

            if self._domain and info.get("hd") != self._domain:
                logger.warning("Google auth rejected: domain %s != %s", info.get("hd"), self._domain)
                return None

            return AuthInfo(
                provider_type="google",
                provider_user_id=info.get("sub", ""),
                email=email,
                display_name=info.get("name", email.split("@")[0]),
                raw={"picture": info.get("picture", "")},
            )
        except Exception:
            logger.exception("Google OAuth token verification failed")
            return None

    async def handle_callback(self, params: dict[str, Any]) -> AuthInfo | None:
        """Exchange OAuth authorization code for ID token and authenticate."""
        code = params.get("code", "")
        redirect_uri = params.get("redirect_uri", "")
        if not code or not self._oauth_client_id:
            return None

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": code,
                        "client_id": self._oauth_client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                resp.raise_for_status()
                token_data = resp.json()

            id_token_str = token_data.get("id_token", "")
            if not id_token_str:
                logger.warning("Google OAuth token exchange returned no id_token")
                return None

            return await self.authenticate({"id_token": id_token_str})
        except Exception:
            logger.exception("Google OAuth callback failed")
            return None

    @property
    def oauth_client_id(self) -> str:
        return self._oauth_client_id

    @property
    def client_secret(self) -> str:
        return self._client_secret

    @property
    def domain(self) -> str:
        return self._domain
