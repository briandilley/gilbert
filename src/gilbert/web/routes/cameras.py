"""Gilbert-proxied media routes for camera events.

Browsers (especially over a tunnel) can't reach the LAN-only Frigate
HTTP base URL — and even on the LAN, sending the bearer token in
``Authorization`` headers from a cross-origin ``<img>`` / ``<video>``
isn't viable. These routes wrap the backend's ``get_snapshot`` /
``get_clip_url`` calls, inject ``backend_auth_headers()`` server-side,
and stream the bytes back through the authenticated Gilbert session.

Both routes apply the per-camera role gate from ``CameraEventService``
on top of the session-cookie auth — admin-gated cameras 403 for
non-admin callers regardless of frame type.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.camera import CameraProvider
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cameras")

_CHUNK_SIZE = 64 * 1024
_USER_LEVEL = 100
_ADMIN_LEVEL = 0


def _gilbert(request: Request) -> Gilbert:
    return request.app.state.gilbert  # type: ignore[no-any-return]


def _camera_service(gilbert: Gilbert) -> Any:
    return gilbert.service_manager.get_by_capability("cameras")


def _user_visible_to(svc: Any, camera: str, user: UserContext) -> bool:
    """Re-implement the per-camera role gate against a UserContext.

    ``CameraEventService._camera_visible_to`` takes a roles frozenset;
    do the same shape here without coupling to the concrete service
    class.
    """
    roles = set(user.roles)
    if "admin" in roles:
        return True
    required = svc._effective_role(camera)  # noqa: SLF001 — internal-by-design
    if required == "admin":
        return False
    if required == "user":
        return "user" in roles
    return True


@router.get("/events/{event_id}/snapshot.jpg")
async def get_event_snapshot(
    event_id: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Response:
    gilbert = _gilbert(request)
    svc = _camera_service(gilbert)
    if svc is None or not isinstance(svc, CameraProvider):
        raise HTTPException(status_code=503, detail="camera service unavailable")
    ev = await svc.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    if not _user_visible_to(svc, ev.camera, user):
        raise HTTPException(status_code=403, detail="forbidden")
    snap = await svc.get_snapshot_bytes(event_id, max_height=720)
    if snap is None:
        raise HTTPException(status_code=404, detail="snapshot unavailable")
    snap_bytes, media_type = snap
    return Response(
        content=snap_bytes,
        media_type=media_type or "image/jpeg",
        headers={
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/events/{event_id}/clip.mp4")
async def get_event_clip(
    event_id: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> StreamingResponse:
    gilbert = _gilbert(request)
    svc = _camera_service(gilbert)
    if svc is None or not isinstance(svc, CameraProvider):
        raise HTTPException(status_code=503, detail="camera service unavailable")
    ev = await svc.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    if not _user_visible_to(svc, ev.camera, user):
        raise HTTPException(status_code=403, detail="forbidden")

    backend = svc._backend  # noqa: SLF001 — protected-by-design accessor
    if backend is None:
        raise HTTPException(status_code=503, detail="backend unavailable")

    upstream_url = await backend.get_clip_url(event_id)
    if not upstream_url:
        raise HTTPException(status_code=404, detail="clip unavailable")

    # Forward Range from the client through to Frigate so <video> seek
    # works. Frigate supports byte-range requests natively.
    headers: dict[str, str] = dict(backend.backend_auth_headers())
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    upstream_req = client.build_request("GET", upstream_url, headers=headers)
    upstream_resp = await client.send(upstream_req, stream=True)

    if upstream_resp.status_code >= 400:
        # Drain and close before raising.
        await upstream_resp.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=upstream_resp.status_code,
            detail="upstream clip fetch failed",
        )

    # Forward Range-related headers verbatim so the browser sees the
    # 206 partial-content semantics.
    forward_headers: dict[str, str] = {}
    for h in (
        "content-length",
        "content-range",
        "accept-ranges",
        "last-modified",
        "etag",
    ):
        v = upstream_resp.headers.get(h)
        if v:
            forward_headers[h] = v

    async def _iter() -> Any:
        try:
            async for chunk in upstream_resp.aiter_bytes(_CHUNK_SIZE):
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _iter(),
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type", "video/mp4"),
        headers=forward_headers,
    )
