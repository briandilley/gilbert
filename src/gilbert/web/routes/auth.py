"""Auth routes — login page, provider-specific login, OAuth callbacks, logout."""

import logging
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import JSONResponse, RedirectResponse

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.web.auth import get_user_context

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


# ---- Google OAuth ----


def _get_google_provider(request: Request) -> Any:
    """Get the Google auth backend or raise 503."""
    auth_svc = _get_auth_service(request)
    backend = auth_svc.get_backend("google")
    if backend is None:
        raise HTTPException(status_code=503, detail="Google auth not available")
    return backend


def _get_google_callback_url(request: Request, provider: Any) -> str:
    """Get the callback URL — tunnel if available, otherwise local."""
    local_url = str(request.base_url).rstrip("/")
    return provider.get_callback_url(local_url)


@router.get("/login/google/start")
async def google_oauth_start(request: Request) -> Any:
    """Redirect to Google's OAuth consent screen."""
    import base64

    provider = _get_google_provider(request)
    redirect_uri = _get_google_callback_url(request, provider)

    # Encode the local origin so the callback can redirect back to it.
    local_origin = str(request.base_url).rstrip("/")
    state = base64.urlsafe_b64encode(local_origin.encode()).decode()

    params = urllib.parse.urlencode({
        "client_id": provider.oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
        **({"hd": provider.domain} if provider.domain else {}),
    })

    return RedirectResponse(
        url=f"https://accounts.google.com/o/oauth2/v2/auth?{params}"
    )


@router.get("/login/google/callback")
async def google_oauth_callback(request: Request) -> Any:
    """Handle the Google OAuth callback."""
    import base64

    auth_svc = _get_auth_service(request)
    provider = _get_google_provider(request)

    # Decode the local origin from state.
    state = request.query_params.get("state", "")
    try:
        local_origin = base64.urlsafe_b64decode(state.encode()).decode() if state else ""
    except Exception:
        local_origin = ""

    error = request.query_params.get("error")
    if error:
        login_url = f"{local_origin}/auth/login?error={urllib.parse.quote(error)}" if local_origin else f"/auth/login?error={urllib.parse.quote(error)}"
        return RedirectResponse(url=login_url, status_code=303)

    code = request.query_params.get("code", "")
    if not code:
        login_url = f"{local_origin}/auth/login?error=Missing+authorization+code" if local_origin else "/auth/login?error=Missing+authorization+code"
        return RedirectResponse(url=login_url, status_code=303)

    redirect_uri = _get_google_callback_url(request, provider)

    user_ctx = await auth_svc.handle_callback(
        "google",
        {"code": code, "redirect_uri": redirect_uri},
    )

    if user_ctx is None:
        login_url = f"{local_origin}/auth/login?error=Google+authentication+failed" if local_origin else "/auth/login?error=Google+authentication+failed"
        return RedirectResponse(url=login_url, status_code=303)

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
        return RedirectResponse(
            url="/auth/login?error=Invalid+session", status_code=303
        )

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
async def me(user: UserContext = Depends(get_user_context)) -> dict:  # noqa: B008
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "roles": sorted(user.roles),
        "provider": user.provider,
    }


# ---- Helpers ----


def _build_auth_response(user_ctx: UserContext, is_form: bool) -> Response:
    """Build the response with session cookie for successful auth."""
    if is_form:
        resp = RedirectResponse(url="/", status_code=303)
    else:
        resp = JSONResponse({
            "user_id": user_ctx.user_id,
            "email": user_ctx.email,
            "display_name": user_ctx.display_name,
            "roles": sorted(user_ctx.roles),
            "session_id": user_ctx.session_id,
        })

    if user_ctx.session_id:
        resp.set_cookie(
            key="gilbert_session",
            value=user_ctx.session_id,
            httponly=True,
            samesite="lax",
        )

    return resp
