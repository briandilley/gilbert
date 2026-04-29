"""Auth routes — login page, provider-specific login, OAuth callbacks, logout."""

import base64
import logging
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import JSONResponse, RedirectResponse

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import OAuthLoginBackend, UserContext
from gilbert.web.auth import get_user_context, require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _get_auth_service(request: Request) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    svc = gilbert.service_manager.get_by_capability("authentication")
    if svc is None:
        raise HTTPException(status_code=503, detail="Authentication is not enabled")
    return svc


# ---- Local (email/password) login ----


@router.post("/login/local")
async def login_local(request: Request) -> Any:
    """Handle local email/password authentication."""
    auth_svc = _get_auth_service(request)

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        is_form = False
    else:
        form = await request.form()
        body = dict(form)
        is_form = True

    user_ctx = await auth_svc.authenticate("local", body)

    if user_ctx is None:
        if is_form:
            return RedirectResponse(
                url="/auth/login?error=Invalid+credentials",
                status_code=303,
            )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return _build_auth_response(user_ctx, is_form)


# ---- Generic external (OAuth-shaped) login flow ----
#
# Any AuthBackend that satisfies the ``OAuthLoginBackend`` protocol
# lights up these routes automatically — the path's ``<provider_type>``
# segment is the backend's registered name. Core never mentions any
# specific provider; plugins bring their own.


def _get_oauth_backend(request: Request, provider_type: str) -> Any:
    """Resolve an OAuth-shaped auth backend by name, or raise 503."""
    auth_svc = _get_auth_service(request)
    backend = auth_svc.get_backend(provider_type)
    if backend is None or not isinstance(backend, OAuthLoginBackend):
        raise HTTPException(
            status_code=503,
            detail=f"{provider_type} login is not available",
        )
    return backend


def _login_error_redirect(local_origin: str, message: str) -> RedirectResponse:
    """Redirect back to the login page with an error query string."""
    quoted = urllib.parse.quote(message)
    base = local_origin if local_origin else ""
    return RedirectResponse(
        url=f"{base}/auth/login?error={quoted}",
        status_code=303,
    )


@router.get("/login/{provider_type}/start")
async def external_login_start(provider_type: str, request: Request) -> Any:
    """Kick off a redirect-based external login for ``provider_type``.

    Looks up the auth backend by name, asks it for the authorization
    URL, and redirects the browser there. The current browser origin
    is stashed in the ``state`` parameter so the callback can hand
    the session back to the local domain if the OAuth round-trip
    went through a tunnel.
    """
    backend = _get_oauth_backend(request, provider_type)
    local_origin = str(request.base_url).rstrip("/")
    redirect_uri = backend.get_callback_url(local_origin)
    state = base64.urlsafe_b64encode(local_origin.encode()).decode()
    url = backend.get_authorization_url(redirect_uri, state)
    return RedirectResponse(url=url)


@router.get("/login/{provider_type}/callback")
async def external_login_callback(provider_type: str, request: Request) -> Any:
    """Handle the callback from a redirect-based external login."""
    auth_svc = _get_auth_service(request)
    backend = _get_oauth_backend(request, provider_type)

    # Decode the local origin from ``state`` — set by ``/start``.
    raw_state = request.query_params.get("state", "")
    try:
        local_origin = base64.urlsafe_b64decode(raw_state.encode()).decode() if raw_state else ""
    except Exception:
        local_origin = ""

    error = request.query_params.get("error")
    if error:
        return _login_error_redirect(local_origin, error)

    code = request.query_params.get("code", "")
    if not code:
        return _login_error_redirect(local_origin, "Missing authorization code")

    redirect_uri = backend.get_callback_url(str(request.base_url).rstrip("/"))

    user_ctx = await auth_svc.handle_callback(
        provider_type,
        {"code": code, "redirect_uri": redirect_uri},
    )

    if user_ctx is None:
        return _login_error_redirect(
            local_origin,
            f"{provider_type} authentication failed",
        )

    if local_origin and user_ctx.session_id:
        # Redirect to local origin with session token so we can set the
        # cookie on the correct domain.
        return RedirectResponse(
            url=f"{local_origin}/auth/session?token={user_ctx.session_id}",
            status_code=303,
        )

    resp = RedirectResponse(url="/", status_code=303)
    if user_ctx.session_id:
        resp.set_cookie(
            key="gilbert_session",
            value=user_ctx.session_id,
            httponly=True,
            samesite="lax",
        )
    return resp


# ---- Legacy POST /login (backward compat) ----


@router.post("/login")
async def login_legacy(request: Request) -> Any:
    """Legacy login endpoint — routes to the correct provider."""
    auth_svc = _get_auth_service(request)

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        is_form = False
    else:
        form = await request.form()
        body = dict(form)
        is_form = True

    provider_type = body.pop("provider", "local")
    user_ctx = await auth_svc.authenticate(provider_type, body)

    if user_ctx is None:
        if is_form:
            return RedirectResponse(
                url="/auth/login?error=Invalid+credentials",
                status_code=303,
            )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return _build_auth_response(user_ctx, is_form)


# ---- Session handoff (sets cookie on local domain after external OAuth) ----


@router.get("/session")
async def session_handoff(request: Request) -> Any:
    """Set session cookie on the local domain and redirect to home.

    Called after external OAuth to transfer the session from the tunnel
    domain to the local domain.
    """
    token = request.query_params.get("token", "")
    if not token:
        return RedirectResponse(url="/auth/login", status_code=303)

    # Validate the session before setting the cookie.
    auth_svc = _get_auth_service(request)
    user_ctx = await auth_svc.validate_session(token)
    if user_ctx is None:
        return RedirectResponse(url="/auth/login?error=Invalid+session", status_code=303)

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key="gilbert_session",
        value=token,
        httponly=True,
        samesite="lax",
    )
    return resp


# ---- Logout ----


@router.post("/logout")
async def logout(
    request: Request,
    user: UserContext = Depends(get_user_context),  # noqa: B008
) -> Any:
    """Invalidate the current session and redirect to home."""
    if user.session_id:
        auth_svc = _get_auth_service(request)
        await auth_svc.invalidate_session(user.session_id)
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("gilbert_session")
    return response


# ---- Current user ----


@router.get("/me")
async def me(
    request: Request,
    user: UserContext = Depends(get_user_context),  # noqa: B008
) -> dict[str, Any]:
    # ``has_password`` lets the account page decide whether to show a
    # "Change password" form. SYSTEM/GUEST never have one.
    has_password = False
    if user.user_id not in ("system", "guest"):
        try:
            auth_svc = _get_auth_service(request)
        except HTTPException:
            auth_svc = None
        if auth_svc is not None:
            has_password = await auth_svc.user_has_password(user.user_id)
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "roles": sorted(user.roles),
        "provider": user.provider,
        "has_password": has_password,
    }


# ---- Account self-service (require an authenticated user) ----


@router.post("/password")
async def change_password(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Response:
    """Change the calling user's password.

    Verifies the old password, writes a new argon2 hash to the user
    entity, and invalidates every other session for the user — the
    caller's current session stays alive so they don't get bounced
    to the login page mid-form.
    """
    auth_svc = _get_auth_service(request)
    body = await request.json()
    old_password = body.get("old_password", "")
    new_password = body.get("new_password", "")
    if not old_password or not new_password:
        raise HTTPException(
            status_code=400,
            detail="Both old_password and new_password are required.",
        )
    try:
        await auth_svc.change_password(
            user.user_id,
            old_password,
            new_password,
            keep_session_id=user.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Response:
    """Revoke every session for the calling user, including this one.

    The SPA should treat the 204 as "you've been signed out" and
    redirect to ``/auth/login``; the cookie is cleared here so the
    next request lands as an unauthenticated visitor.
    """
    auth_svc = _get_auth_service(request)
    await auth_svc.revoke_user_sessions(user.user_id)
    response = Response(status_code=204)
    response.delete_cookie("gilbert_session")
    return response


# ---- Helpers ----


def _build_auth_response(user_ctx: UserContext, is_form: bool) -> Response:
    """Build the response with session cookie for successful auth."""
    resp: Response
    if is_form:
        resp = RedirectResponse(url="/", status_code=303)
    else:
        resp = JSONResponse(
            {
                "user_id": user_ctx.user_id,
                "email": user_ctx.email,
                "display_name": user_ctx.display_name,
                "roles": sorted(user_ctx.roles),
                "session_id": user_ctx.session_id,
            }
        )

    if user_ctx.session_id:
        resp.set_cookie(
            key="gilbert_session",
            value=user_ctx.session_id,
            httponly=True,
            samesite="lax",
        )

    return resp
