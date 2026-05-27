"""Screen display routes — setup page, SSE push, temp file serving, and API.

Routes:
- ``GET /screens``              — renders the screen setup/display page
- ``GET /screens/info``         — public ``{enabled, allow_guest_screens}`` probe
- ``GET /screens/stream``       — SSE endpoint (requires ``?name=`` query param)
- ``GET /screens/api``          — list connected screens as JSON
- ``GET /screens/tmp/{token}``  — serve a temp file (PDF or image) by token
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.screens import GuestScreenPolicy
from gilbert.web.auth import require_role

router = APIRouter(prefix="/screens")


def _get_screen_service(request: Request) -> Any:
    """Get the ScreenService from the app, or raise 503."""
    gilbert = request.app.state.gilbert
    svc = gilbert.service_manager.get_by_capability("screen_display")
    if svc is None:
        raise HTTPException(status_code=503, detail="Screen service not available")
    return svc


@router.get("/info")
async def screens_info(request: Request) -> JSONResponse:
    """Public probe used (unauthenticated) by the login and screens pages to
    decide UI: whether the service is enabled and whether guests may set up a
    screen without logging in."""
    gilbert = request.app.state.gilbert
    svc = gilbert.service_manager.get_by_capability("screen_display")
    enabled = svc is not None and svc.enabled
    allow_guest = bool(
        enabled and isinstance(svc, GuestScreenPolicy) and svc.allow_guest_screens
    )
    return JSONResponse(content={"enabled": enabled, "allow_guest_screens": allow_guest})


@router.get("/stream", response_model=None)
async def screens_stream(
    request: Request,
    name: str = Query(..., min_length=1),
) -> StreamingResponse:
    """SSE endpoint — browser connects here to receive push events.

    Unauthenticated visitors (``UserContext.SYSTEM``, no roles) are rejected
    with a clean 403 unless ``allow_guest_screens`` is on. The endpoint stays
    in the middleware allowlist so the rejection is JSON, not a 302→login HTML
    redirect that would break ``EventSource``. Logged-in users and local
    guests (role ``everyone``) always pass.
    """
    screen_svc = _get_screen_service(request)
    user: UserContext = getattr(request.state, "user", UserContext.SYSTEM)
    if not user.roles and not screen_svc.allow_guest_screens:
        raise HTTPException(status_code=403, detail="Screen setup requires signing in.")
    screen = screen_svc.connect(name)
    return StreamingResponse(
        screen_svc.event_stream(screen),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api")
async def screens_api(
    request: Request,
    user: UserContext = Depends(require_role("user")),
) -> JSONResponse:
    """List all connected screens."""
    screen_svc = _get_screen_service(request)
    return JSONResponse(content={"screens": screen_svc.list_screens()})


@router.get("/tmp/{token}", response_model=None)
async def screen_tmp_file(
    request: Request,
    token: str,
) -> FileResponse:
    """Serve a temporary file (PDF or image) by token."""
    screen_svc = _get_screen_service(request)
    path = screen_svc.get_temp_path(token)
    if not path:
        raise HTTPException(status_code=404, detail="File not found or expired")

    media_type = screen_svc.get_temp_mime_type(token)
    return FileResponse(path, media_type=media_type)
