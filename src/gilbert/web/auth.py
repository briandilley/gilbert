"""Web authentication — middleware and FastAPI dependencies."""

from typing import Any

from fastapi import Depends, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse, Response

from gilbert.core.context import set_current_user
from gilbert.interfaces.auth import GuestPolicy, UserContext

# Paths that bypass authentication when the visitor is unauthenticated.
# Used both for tunnel access (always login-required) and for local
# access when ``auth.allow_guests`` is off.
#
# These must include everything the login page itself needs to render:
# the SPA bundle under ``/assets/``, ``/auth/me`` (so the auth provider
# can determine the user is logged out), and ``/api/auth/methods`` (so
# the login form knows which providers to display). Without these the
# browser receives 302→HTML for what should be JS/JSON and the SPA
# never mounts.
#
# Share tokens are bearer-like (the token *is* the auth), so
# ``/api/share/`` is public too — otherwise
# ``share_workspace_file(via_tunnel=True)`` would produce URLs that
# immediately redirect to /auth/login.
_PUBLIC_EXACT = (
    "/auth/login",
    "/auth/logout",
    "/auth/session",
    "/auth/me",
    "/api/auth/methods",
    "/screens",
)
_PUBLIC_PREFIXES = (
    "/auth/login/",
    "/assets/",
    "/static/",
    "/screens/stream",
    "/screens/tmp/",
    "/api/share/",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Sets the current user on every request.

    Checks for a ``gilbert_session`` cookie or ``Authorization: Bearer``
    header.  If the auth service is not running (auth disabled), all
    requests proceed as ``UserContext.SYSTEM``.

    Local requests: unauthenticated users can access public paths (dashboard, etc).
    Tunnel requests: unauthenticated users are redirected to login for everything
    except the auth flow itself.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        user = UserContext.SYSTEM

        gilbert = getattr(request.app.state, "gilbert", None)
        auth_enabled = False
        is_tunnel = False
        allow_guests = True

        if gilbert is not None:
            is_tunnel = self._is_tunnel_request(request, gilbert)
            auth_svc = gilbert.service_manager.get_by_capability("authentication")
            if auth_svc is not None:
                auth_enabled = True
                if isinstance(auth_svc, GuestPolicy):
                    allow_guests = auth_svc.is_guest_allowed()
                session_id = _extract_session(request)
                if session_id:
                    ctx = await auth_svc.validate_session(session_id)
                    if ctx is not None:
                        user = ctx

        # For unauthenticated visitors:
        # - Tunnel, OR local with guests disabled: redirect to login
        #   (the public path list still lets the auth flow + static
        #   assets through so the login page itself is reachable).
        # - Local with guests enabled: treat as GUEST.
        if auth_enabled and user.user_id == "system":
            if is_tunnel or not allow_guests:
                is_public = path in _PUBLIC_EXACT or any(
                    path.startswith(p) for p in _PUBLIC_PREFIXES
                )
                if not is_public:
                    return RedirectResponse(url="/auth/login", status_code=302)
            else:
                # Local visitors get guest access
                user = UserContext.GUEST

        request.state.user = user
        request.state.is_tunnel = is_tunnel
        set_current_user(user)

        return await call_next(request)

    @staticmethod
    def _is_tunnel_request(request: Request, gilbert: Any) -> bool:
        """Check if the request came through the public tunnel (ngrok)."""
        from gilbert.interfaces.tunnel import TunnelProvider

        tunnel_svc = gilbert.service_manager.get_by_capability("tunnel")
        if not isinstance(tunnel_svc, TunnelProvider):
            return False
        public_url = tunnel_svc.public_url
        if not public_url:
            return False
        from urllib.parse import urlparse

        tunnel_host = urlparse(public_url).hostname or ""
        request_host = request.headers.get("host", "").split(":")[0]
        return bool(tunnel_host) and request_host == tunnel_host


def _extract_session(request: Request) -> str | None:
    """Pull a session token from cookie or Authorization header."""
    # Cookie first.
    session_id = request.cookies.get("gilbert_session")
    if session_id:
        return session_id

    # Bearer token fallback.
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    return None


# ---- FastAPI dependencies ----


async def get_user_context(request: Request) -> UserContext:
    """Dependency that returns the current user (may be SYSTEM)."""
    return getattr(request.state, "user", UserContext.SYSTEM)


async def require_authenticated(request: Request) -> UserContext:
    """Dependency that requires any user with roles (logged-in or local guest)."""
    user: UserContext = getattr(request.state, "user", UserContext.SYSTEM)
    if user.user_id == "system":
        raise HTTPException(status_code=401, detail="Authentication required")
    # GUEST and authenticated users both pass — they have roles
    return user


def require_role(role: str) -> Any:
    """Factory returning a dependency that checks for a role using the hierarchy.

    Uses AccessControlService if available, otherwise falls back to a
    hardcoded built-in hierarchy.
    """
    from gilbert.interfaces.acl import BUILTIN_ROLE_LEVELS as _BUILTIN_LEVELS

    async def _check(
        request: Request,
        user: UserContext = Depends(require_authenticated),  # noqa: B008
    ) -> UserContext:
        gilbert = getattr(request.app.state, "gilbert", None)
        if gilbert is not None:
            acl_svc = gilbert.service_manager.get_by_capability("access_control")
            if acl_svc is not None:
                required_level = acl_svc.get_role_level(role)
                effective_level = acl_svc.get_effective_level(user)
                if effective_level <= required_level:
                    return user
                raise HTTPException(status_code=403, detail=f"Requires role: {role}")

        # Fallback: hardcoded levels
        required_level = _BUILTIN_LEVELS.get(role, 100)
        user_level = min((_BUILTIN_LEVELS.get(r, 100) for r in user.roles), default=200)
        if user_level <= required_level:
            return user
        raise HTTPException(status_code=403, detail=f"Requires role: {role}")

    return _check
