"""Web routes for the health service.

Three groups:

1. ``POST /webhook/health/{token}`` — push-style ingestion. Path-isolated
   from ``/api`` because some Shortcut clients have trouble with auth-
   redirected ``/api/*`` paths if a tunnel is in front. The token is
   the only auth.
2. ``/api/health/me/*`` — per-user account routes. Authenticated.
3. ``/api/health/admin/*`` — admin-gated routes (``health-admin`` role
   required for cross-user reads, ``admin`` for the user-counts list).
4. ``GET /api/health/me/oauth/{backend}/callback`` — generic OAuth
   callback. One route handles every OAuth backend; future Garmin /
   Oura / Fitbit additions need zero new routes.

The route layer is **thin** — parses requests, calls the
HealthService methods, formats responses. Authorization, audit,
payload validation, OAuth state machine, and backend dispatch all
live in the service.

The route resolves the service via a route-local
``runtime_checkable`` Protocol so the layer rules don't get
violated by importing the concrete ``HealthService`` class — the
Protocol expresses the surface the route needs, the service
satisfies it structurally.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.health import (
    HEALTH_ADMIN_ROLE,
    DailySummary,
    HealthMetric,
    LinkCompleteResult,
    LinkStartResult,
    MetricType,
)
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)


# ── Route-local Protocol over HealthService ──────────────────────────


@runtime_checkable
class _HealthRouteSurface(Protocol):
    """Capability protocol expressing the surface this route needs.

    ``HealthService`` satisfies it structurally — the route layer
    doesn't import the concrete class so the layer rules stay clean.
    """

    @property
    def public_base_url(self) -> str: ...

    @property
    def webhook_max_body_bytes(self) -> int: ...

    async def ingest_webhook(
        self,
        token: str,
        body: bytes,
        headers: dict[str, str],
        *,
        remote_addr: str = "",
    ) -> Any: ...

    async def list_user_links(self, user_id: str) -> list[dict[str, Any]]: ...

    async def begin_link(
        self, user_id: str, backend_name: str
    ) -> LinkStartResult: ...

    async def complete_link(
        self,
        user_id: str,
        backend_name: str,
        payload: dict[str, Any],
    ) -> LinkCompleteResult: ...

    async def disconnect_backend(
        self, user_id: str, backend_name: str
    ) -> bool: ...

    async def rotate_webhook_token(
        self, user_id: str, backend_name: str
    ) -> dict[str, Any]: ...

    async def preview_delete_all(self, user_id: str) -> dict[str, Any]: ...

    async def delete_all_my_data(
        self,
        user_id: str,
        *,
        actor_kind: str = ...,
    ) -> dict[str, Any]: ...

    async def read_metrics(
        self,
        user_id: str,
        metric_types: list[MetricType],
        since: datetime,
        until: datetime,
    ) -> list[HealthMetric]: ...

    async def latest_daily_summary(
        self,
        user_id: str,
        on_or_before: datetime | None = None,
    ) -> DailySummary | None: ...

    async def consume_oauth_state(
        self,
        state: str,
        backend_name: str,
        caller_user_id: str,
    ) -> dict[str, Any]: ...

    async def record_oauth_error(
        self,
        user_id: str,
        backend_name: str,
        error: str,
    ) -> None: ...

    async def list_admin_user_counts(self) -> list[dict[str, Any]]: ...

    async def admin_read_metrics(
        self,
        actor_ctx: UserContext,
        target_user_id: str,
        metric_types: list[MetricType],
        since: datetime,
        until: datetime,
    ) -> list[HealthMetric]: ...

    async def list_admin_audit_log(
        self, limit: int = ...
    ) -> list[dict[str, Any]]: ...

    async def list_my_audit_log(
        self, user_id: str, limit: int = ...
    ) -> list[dict[str, Any]]: ...


# ── Webhook router (path-isolated from /api) ─────────────────────────

webhook_router = APIRouter(prefix="")


# ── Per-user + admin router ─────────────────────────────────────────

api_router = APIRouter(prefix="/api/health", tags=["health"])


# ── Helpers ──────────────────────────────────────────────────────────


def _gilbert(request: Request) -> Gilbert:
    return request.app.state.gilbert  # type: ignore[no-any-return]


def _health_service(request: Request) -> _HealthRouteSurface:
    gilbert = _gilbert(request)
    svc = gilbert.service_manager.get_by_capability("health")
    if svc is None or not isinstance(svc, _HealthRouteSurface):
        raise HTTPException(status_code=503, detail="health service unavailable")
    return svc


def _client_ip(request: Request) -> str:
    """Extract the client IP for the per-IP webhook rate-limit bucket.

    INTENTIONALLY ignores ``X-Forwarded-For``: Gilbert has no
    ``web.trusted_proxies`` allowlist, so any value in that header is
    attacker-controlled. Honoring it would let a single client spoof
    a fresh IP per request and bypass the 30/min cap on the 404
    enumeration-defense path. Until a trusted-proxy allowlist exists
    in core (open follow-up), only ``request.client.host`` is used.
    """
    if request.client is not None:
        return request.client.host
    return ""


# ── Webhook ──────────────────────────────────────────────────────────


@webhook_router.post("/webhook/health/{token}")
async def health_webhook(token: str, request: Request) -> Response:
    """Per-user push ingestion. The URL token is the only authorization.

    Returns:
    - 200 ``{"received": N, "dropped": M}`` on success
    - 400 if the body is malformed OR exceeds the metric-count cap
    - 404 ``{"received": 0}`` for unknown / disabled tokens (collapsed)
    - 413 if the body exceeds ``webhook_max_body_bytes``
    - 429 with ``Retry-After`` for rate-limited requests

    Body-cap enforcement is BEFORE ``await request.body()``: a POST
    advertising ``Content-Length: 5_000_000_000`` would otherwise
    make Starlette buffer 5 GB into the worker's memory before the
    1 MB cap is checked (memory-DoS vector).
    """
    svc = _health_service(request)
    cap = svc.webhook_max_body_bytes
    content_length_raw = request.headers.get("content-length") or ""
    try:
        content_length = int(content_length_raw) if content_length_raw else 0
    except ValueError:
        content_length = 0
    if content_length > cap:
        return JSONResponse(content={"received": 0}, status_code=413)
    body = await request.body()
    if len(body) > cap:
        # Defensive — covers chunked transfer encoding / missing
        # content-length where the pre-check above couldn't fire.
        return JSONResponse(content={"received": 0}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    remote_addr = _client_ip(request)
    result = await svc.ingest_webhook(
        token=token,
        body=body,
        headers=headers,
        remote_addr=remote_addr,
    )
    status_map = {
        "ok": 200,
        "bad_request": 400,
        "not_found": 404,
        "payload_too_large": 413,
        "rate_limited": 429,
    }
    status = status_map.get(result.status, 500)
    response_headers: dict[str, str] = {}
    if result.status == "rate_limited" and result.retry_after_seconds:
        response_headers["Retry-After"] = str(result.retry_after_seconds)
    if result.status == "ok":
        body_obj = {"received": result.received, "dropped": result.dropped}
    elif result.status == "not_found":
        # Identical shape to "disabled" by design — defeats enumeration.
        body_obj = {"received": 0}
    else:
        body_obj = {"received": 0, "error": result.message}
    return JSONResponse(content=body_obj, status_code=status, headers=response_headers)


# ── Per-user (/api/health/me/*) ──────────────────────────────────────


@api_router.get("/me/links")
async def list_my_links(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """List the current user's connected backends, secrets redacted."""
    svc = _health_service(request)
    items = await svc.list_user_links(user.user_id)
    return {"items": items}


@api_router.get("/me/config")
async def my_health_config(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Public-facing snapshot of the operator-set ``gilbert.public_base_url``.

    Returned as a boolean ``has_public_base_url`` so plugin SPA panels
    can disable their Connect button when the operator hasn't yet set
    the base URL (Withings OAuth, future Garmin/Oura/Fitbit). Never
    returns the URL itself — the panel doesn't need it; the server
    builds the callback URL on its own.
    """
    svc = _health_service(request)
    return {"has_public_base_url": bool(svc.public_base_url)}


@api_router.post("/me/connect/{backend}")
async def connect_backend(
    backend: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Begin a per-user link flow (OAuth or webhook-token rotate)."""
    svc = _health_service(request)
    result = await svc.begin_link(user.user_id, backend)
    if result.status == "error":
        # Distinguish "unknown backend" 404 vs other errors.
        if "unknown backend" in result.message:
            raise HTTPException(status_code=404, detail=result.message)
    return {
        "status": result.status,
        "message": result.message,
        "open_url": result.open_url,
        "webhook_url": result.webhook_url,
        "followup_action_key": result.followup_action_key,
    }


@api_router.post("/me/complete/{backend}")
async def complete_backend(
    backend: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Complete a started flow (e.g. exchange OAuth code)."""
    svc = _health_service(request)
    payload = await request.json() if (await request.body()) else {}
    result = await svc.complete_link(user.user_id, backend, payload)
    if result.status == "error" and "unknown backend" in result.message:
        raise HTTPException(status_code=404, detail=result.message)
    return {"status": result.status, "message": result.message}


@api_router.post("/me/disconnect/{backend}")
async def disconnect_backend(
    backend: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Disconnect a single backend (revokes upstream + drops local row,
    leaves historical metrics in place)."""
    svc = _health_service(request)
    ok = await svc.disconnect_backend(user.user_id, backend)
    return {"ok": ok}


@api_router.post("/me/rotate-token/{backend}")
async def rotate_webhook_token(
    backend: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Rotate a per-user webhook token. Returns the raw token ONCE so
    the SPA can show "copy this URL into your iOS Shortcut"; only the
    SHA-256 hash is persisted."""
    svc = _health_service(request)
    result = await svc.rotate_webhook_token(user.user_id, backend)
    if result.get("status") == "error":
        if "unknown backend" in result.get("message", ""):
            raise HTTPException(status_code=404, detail=result["message"])
    return result


@api_router.get("/me/delete-all/preview")
async def preview_delete_all(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    svc = _health_service(request)
    return await svc.preview_delete_all(user.user_id)


@api_router.post("/me/delete-all")
async def execute_delete_all(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Two-step cascade delete. Caller MUST send ``confirm: "DELETE"``.

    Returns ``{deleted_metrics, disconnected_backends,
    upstream_revoke_failures}`` per spec §6.6.
    """
    svc = _health_service(request)
    payload = await request.json() if (await request.body()) else {}
    if payload.get("confirm") != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="confirm must equal the literal string 'DELETE'",
        )
    return await svc.delete_all_my_data(user.user_id)


@api_router.get("/me/metrics")
async def list_my_metrics(
    request: Request,
    metric_type: str = "",
    since: str = "",
    until: str = "",
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    svc = _health_service(request)
    types: list[MetricType] = []
    if metric_type:
        try:
            types = [MetricType(metric_type)]
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown metric_type") from None
    until_dt = datetime.fromisoformat(until) if until else datetime.now(UTC)
    since_dt = datetime.fromisoformat(since) if since else (until_dt - timedelta(days=7))

    from gilbert.core.context import set_current_user

    set_current_user(user)
    rows = await svc.read_metrics(user.user_id, types, since_dt, until_dt)
    return {"items": [r.to_dict() for r in rows]}


@api_router.get("/me/summary")
async def my_latest_summary(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    svc = _health_service(request)
    from gilbert.core.context import set_current_user

    set_current_user(user)
    summary = await svc.latest_daily_summary(user.user_id)
    if summary is None:
        return {"summary": None}
    return {"summary": summary.to_dict()}


# ── Generic OAuth callback ───────────────────────────────────────────


@api_router.get("/me/oauth/{backend}/callback")
async def oauth_callback(
    backend: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Response:
    """Generic OAuth callback. Verifies state row, server-side-binds it
    to the calling session, one-shot-consumes it, and then asks the
    backend to exchange the code for tokens.

    Defeats the confused-deputy attack: an attacker who initiates an
    OAuth flow and tricks a victim into completing it on the victim's
    account. State.user_id MUST match the caller's user_id.
    """
    svc = _health_service(request)

    if error:
        # User clicked "Deny" on the provider's screen.
        await svc.record_oauth_error(user.user_id, backend, error)
        return RedirectResponse(
            url="/account?health_oauth_error=denied",
            status_code=302,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    consume = await svc.consume_oauth_state(state, backend, user.user_id)
    status = consume.get("status")
    if status == "missing":
        raise HTTPException(status_code=400, detail="unknown state")
    if status == "expired":
        raise HTTPException(status_code=400, detail="state expired")
    if status == "user_mismatch":
        raise HTTPException(status_code=400, detail="state user mismatch")
    if status == "backend_mismatch":
        raise HTTPException(status_code=400, detail="state backend mismatch")
    if status == "already":
        return RedirectResponse(url="/account?health_oauth=already", status_code=302)

    result = await svc.complete_link(user.user_id, backend, {"code": code})
    if result.status != "ok":
        return RedirectResponse(
            url=f"/account?health_oauth_error={result.message}",
            status_code=302,
        )
    return RedirectResponse(url="/account?health_oauth=ok", status_code=302)


# ── Admin routes ─────────────────────────────────────────────────────


def _require_admin(user: UserContext) -> None:
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="admin role required")


def _require_health_admin(user: UserContext) -> None:
    if HEALTH_ADMIN_ROLE not in user.roles:
        raise HTTPException(
            status_code=403,
            detail=f"{HEALTH_ADMIN_ROLE} role required",
        )


@api_router.get("/admin/users")
async def admin_user_counts(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Per-user counts (no values, no metric types) for admin oversight."""
    _require_admin(user)
    svc = _health_service(request)
    users = await svc.list_admin_user_counts()
    return {"users": users}


@api_router.get("/admin/users/{user_id}/metrics")
async def admin_read_user_metrics(
    user_id: str,
    request: Request,
    metric_type: str = "",
    since: str = "",
    until: str = "",
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Cross-user read — gated on the dedicated ``health-admin`` role.

    Audit-logged AND notifies the target user. ``admin`` membership
    alone is not sufficient — operators grant ``health-admin``
    explicitly via /roles/users.
    """
    _require_health_admin(user)
    svc = _health_service(request)
    types: list[MetricType] = []
    if metric_type:
        try:
            types = [MetricType(metric_type)]
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown metric_type") from None
    until_dt = datetime.fromisoformat(until) if until else datetime.now(UTC)
    since_dt = datetime.fromisoformat(since) if since else (until_dt - timedelta(days=7))
    rows = await svc.admin_read_metrics(user, user_id, types, since_dt, until_dt)
    return {"items": [r.to_dict() for r in rows]}


@api_router.get("/admin/audit-log")
async def admin_audit_log(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Read every ``health_audit`` row. Requires ``health-admin``."""
    _require_health_admin(user)
    svc = _health_service(request)
    items = await svc.list_admin_audit_log()
    return {"items": items}


@api_router.get("/me/audit-log")
async def my_audit_log(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Per-user audit log. The user sees every row where they were the
    target, sorted most recent first. Closes the loop opened by the
    cross-user-read notification."""
    svc = _health_service(request)
    items = await svc.list_my_audit_log(user.user_id)
    return {"items": items}

