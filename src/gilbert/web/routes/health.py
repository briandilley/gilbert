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

The route layer is **thin** — parses requests, calls
``HealthService``, formats responses. Authorization, audit, payload
validation, and backend dispatch all live in the service.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from gilbert.core.app import Gilbert
from gilbert.core.services.health import HealthService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.health import (
    HEALTH_ADMIN_ROLE,
    MetricType,
)
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)


# ── Webhook router (path-isolated from /api) ─────────────────────────

webhook_router = APIRouter(prefix="")


# ── Per-user + admin router ─────────────────────────────────────────

api_router = APIRouter(prefix="/api/health", tags=["health"])


# ── Helpers ──────────────────────────────────────────────────────────


def _gilbert(request: Request) -> Gilbert:
    return request.app.state.gilbert  # type: ignore[no-any-return]


def _health_service(request: Request) -> HealthService:
    gilbert = _gilbert(request)
    svc = gilbert.service_manager.get_by_capability("health")
    if not isinstance(svc, HealthService):
        raise HTTPException(status_code=503, detail="health service unavailable")
    return svc


def _client_ip(request: Request) -> str:
    """Extract the client IP, honoring X-Forwarded-For when present."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
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
    """
    svc = _health_service(request)
    body = await request.body()
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
    if svc._storage is None:
        return {"items": []}
    from gilbert.interfaces.storage import Filter, FilterOp, Query as StorageQuery

    rows = await svc._storage.query(
        StorageQuery(
            collection="health_links",
            filters=[Filter(field="user_id", op=FilterOp.EQ, value=user.user_id)],
        )
    )
    from gilbert.core.services.health import _redact_link

    return {"items": [_redact_link(r) for r in rows]}


@api_router.post("/me/connect/{backend}")
async def connect_backend(
    backend: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Begin a per-user link flow (OAuth or webhook-token rotate)."""
    svc = _health_service(request)
    backend_obj = svc._backends.get(backend)
    if backend_obj is None:
        raise HTTPException(status_code=404, detail="unknown backend")
    result = await backend_obj.begin_link(user.user_id)
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
    backend_obj = svc._backends.get(backend)
    if backend_obj is None:
        raise HTTPException(status_code=404, detail="unknown backend")
    payload = await request.json() if (await request.body()) else {}
    result = await backend_obj.complete_link(user.user_id, payload)
    if result.status != "ok":
        return {"status": result.status, "message": result.message}
    await svc._publish_event(  # type: ignore[attr-defined]
        "health.link.connected",
        {"user_id": user.user_id, "backend": backend},
    )
    return {"status": "ok", "message": result.message}


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
    backend_obj = svc._backends.get(backend)
    if backend_obj is None:
        raise HTTPException(status_code=404, detail="unknown backend")
    if not backend_obj.supports_push:
        return {
            "status": "error",
            "message": f"Token rotation does not apply to backend '{backend}'",
        }
    if svc._storage is None:
        raise HTTPException(status_code=503, detail="storage unavailable")
    raw_token = secrets.token_urlsafe(48)
    import hashlib

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    last4 = raw_token[-4:]
    link_id = f"{user.user_id}/{backend}"
    existing = await svc._storage.get("health_links", link_id) or {
        "_id": link_id,
        "user_id": user.user_id,
        "backend_name": backend,
        "enabled": True,
        "created_at": datetime.now(UTC).isoformat(),
    }
    existing["webhook_token_hash"] = token_hash
    existing["webhook_token_last4"] = last4
    existing["enabled"] = True
    existing["updated_at"] = datetime.now(UTC).isoformat()
    await svc._storage.put("health_links", link_id, existing)

    base = svc.public_base_url.rstrip("/")
    webhook_url = f"{base}/webhook/health/{raw_token}" if base else ""

    # Notify the user — the rotation revokes the previous token, so
    # any device still posting with the old token silently fails until
    # the user updates it.
    if svc._notifications is not None:
        from gilbert.interfaces.notifications import NotificationUrgency

        try:
            await svc._notifications.notify_user(
                user_id=user.user_id,
                message=(
                    f"Health webhook token rotated for {backend}. Update "
                    "your iOS Shortcut / device with the new URL."
                ),
                urgency=NotificationUrgency.URGENT,
                source="health",
            )
        except Exception:
            logger.debug("Token-rotate notify failed", exc_info=True)
    return {
        "status": "ok",
        "raw_token": raw_token,
        "webhook_url": webhook_url,
    }


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
    if svc._storage is None:
        raise HTTPException(status_code=503, detail="storage unavailable")

    if error:
        # User clicked "Deny" on the provider's screen.
        link_id = f"{user.user_id}/{backend}"
        existing = await svc._storage.get("health_links", link_id) or {
            "_id": link_id,
            "user_id": user.user_id,
            "backend_name": backend,
            "enabled": False,
        }
        existing["last_sync_error"] = f"oauth denied: {error}"
        await svc._storage.put("health_links", link_id, existing)
        return RedirectResponse(
            url="/account?health_oauth_error=denied",
            status_code=302,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    state_row = await svc._storage.get("health_oauth_state", state)
    if state_row is None:
        raise HTTPException(status_code=400, detail="unknown state")
    # Expiry.
    expires_at_raw = state_row.get("expires_at") or ""
    try:
        expires_at = datetime.fromisoformat(str(expires_at_raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed state row") from None
    if datetime.now(UTC) > expires_at:
        await svc._storage.delete("health_oauth_state", state)
        raise HTTPException(status_code=400, detail="state expired")
    # Confused-deputy defense.
    if str(state_row.get("user_id") or "") != user.user_id:
        raise HTTPException(status_code=400, detail="state user mismatch")
    # Backend-namespaced.
    if str(state_row.get("backend_name") or "") != backend:
        raise HTTPException(status_code=400, detail="state backend mismatch")
    # One-shot consume.
    if state_row.get("consumed_at"):
        return RedirectResponse(url="/account?health_oauth=already", status_code=302)
    state_row["consumed_at"] = datetime.now(UTC).isoformat()
    await svc._storage.put("health_oauth_state", state, state_row)

    backend_obj = svc._backends.get(backend)
    if backend_obj is None:
        raise HTTPException(status_code=404, detail="unknown backend")
    result = await backend_obj.complete_link(user.user_id, {"code": code})
    if result.status != "ok":
        return RedirectResponse(
            url=f"/account?health_oauth_error={result.message}",
            status_code=302,
        )
    await svc._publish_event(
        "health.link.connected",
        {"user_id": user.user_id, "backend": backend},
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
    if svc._storage is None:
        return {"users": []}
    from gilbert.interfaces.storage import Query as StorageQuery, SortField

    rows = await svc._storage.query(
        StorageQuery(
            collection="health_links",
            sort=[SortField(field="user_id", descending=False)],
        )
    )
    by_user: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = str(r.get("user_id") or "")
        if not uid:
            continue
        info = by_user.setdefault(
            uid,
            {
                "user_id": uid,
                "has_data": False,
                "backends": [],
                "last_ingested_at": "",
            },
        )
        info["backends"].append(str(r.get("backend_name") or ""))
        if r.get("last_delivery_at"):
            info["has_data"] = True
            if str(r["last_delivery_at"]) > info["last_ingested_at"]:
                info["last_ingested_at"] = str(r["last_delivery_at"])
        if r.get("last_sync_at"):
            info["has_data"] = True
            if str(r["last_sync_at"]) > info["last_ingested_at"]:
                info["last_ingested_at"] = str(r["last_sync_at"])
    return {"users": list(by_user.values())}


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
    if svc._storage is None:
        return {"items": []}
    from gilbert.interfaces.storage import Query as StorageQuery, SortField

    rows = await svc._storage.query(
        StorageQuery(
            collection="health_audit",
            sort=[SortField(field="accessed_at", descending=True)],
            limit=500,
        )
    )
    return {"items": rows}


@api_router.get("/me/audit-log")
async def my_audit_log(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Per-user audit log. The user sees every row where they were the
    target, sorted most recent first. Closes the loop opened by the
    cross-user-read notification."""
    svc = _health_service(request)
    if svc._storage is None:
        return {"items": []}
    from gilbert.interfaces.storage import Filter, FilterOp, Query as StorageQuery, SortField

    rows = await svc._storage.query(
        StorageQuery(
            collection="health_audit",
            filters=[
                Filter(field="target_user_id", op=FilterOp.EQ, value=user.user_id)
            ],
            sort=[SortField(field="accessed_at", descending=True)],
            limit=500,
        )
    )
    return {"items": rows}

