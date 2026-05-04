"""Browser-plugin web routes.

The browser plugin lives outside core (under ``std-plugins/browser/``)
but its VNC live-login flow needs two HTTP-side affordances that don't
fit the WS-RPC model:

1. ``GET /api/browser/novnc/{filename:path}`` — serve the vendored
   noVNC client so the in-app dialog can iframe it without dragging
   the JS into the SPA bundle.
2. ``WS /api/browser/vnc/{session_id}/ws`` — authenticated proxy that
   tunnels bytes between the browser noVNC client and the local
   websockify port owned by the VNC session manager.

Authorization: every request must come from an authenticated
``UserContext`` (user level), and the websocket route additionally
verifies that the calling user owns the session via the browser
service's ``get_vnc_websockify_port`` capability.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser")


def _resolve_browser_service(request_or_ws: Any) -> Any | None:
    """Pull the BrowserService from the running Gilbert by capability."""
    app = (
        request_or_ws.app
        if hasattr(request_or_ws, "app")
        else request_or_ws.scope["app"]
    )
    gilbert = getattr(app.state, "gilbert", None)
    if gilbert is None:
        return None
    return gilbert.service_manager.get_by_capability("browser")


def _resolve_novnc_root() -> Path | None:
    """Locate the vendored noVNC dir on disk.

    Looks under each plugin search path for ``browser/static/novnc``
    until it finds an existing dir.
    """
    candidates = [
        Path("std-plugins/browser/static/novnc"),
        Path("local-plugins/browser/static/novnc"),
        Path("installed-plugins/browser/static/novnc"),
    ]
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    return None


@router.get("/novnc/{filename:path}", response_model=None)
async def serve_novnc(
    request: Request,
    filename: str,
) -> FileResponse:
    """Serve the noVNC client. Authenticated user-level access only.

    AuthMiddleware sets ``request.state.user`` (a UserContext); a
    ``user_id`` of "" / "guest" is treated as anonymous and rejected.
    """
    user_ctx = getattr(request.state, "user", None)
    if user_ctx is None or not getattr(user_ctx, "user_id", "") or user_ctx.user_id == "guest":
        raise HTTPException(status_code=401, detail="Authentication required")
    root = _resolve_novnc_root()
    if root is None:
        raise HTTPException(status_code=404, detail="noVNC client not installed")
    target = (root / filename).resolve()
    # Path-traversal guard: ensure target stays under root.
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(target))


async def _authenticate_ws(websocket: Any, gilbert: Any) -> Any | None:
    """Resolve UserContext on a WebSocket upgrade.

    AuthMiddleware (BaseHTTPMiddleware) does NOT run on websocket
    handshakes, so ``websocket.state.user`` would be unset — we have
    to extract the session cookie / token ourselves. Mirrors the
    logic in ``web/routes/websocket._authenticate`` but kept local
    to avoid a cross-module import.
    """
    from gilbert.interfaces.auth import (
        GuestPolicy,
        SessionValidator,
        UserContext,
    )

    session_id = websocket.cookies.get("gilbert_session")
    if not session_id:
        session_id = websocket.query_params.get("token")
    auth_svc = gilbert.service_manager.get_by_capability("authentication")

    if session_id and isinstance(auth_svc, SessionValidator):
        ctx = await auth_svc.validate_session(session_id)
        if isinstance(ctx, UserContext):
            return ctx
    if isinstance(auth_svc, GuestPolicy) and not auth_svc.is_guest_allowed():
        return None
    return UserContext.GUEST


@router.websocket("/vnc/{session_id}/ws")
async def vnc_proxy(websocket: WebSocket, session_id: str) -> None:
    """Authenticated WebSocket-to-TCP proxy.

    noVNC connects to us via WebSocket (binary subprotocol). We
    terminate the WS upgrade, then open a raw TCP socket to x11vnc
    on its localhost port and pipe RFB protocol bytes between the
    two endpoints. The previous design proxied to ``websockify``
    in the middle, but that's a websocket-server itself — raw TCP
    bytes into it failed the WS handshake. Going directly to
    x11vnc removes one redundant process and one layer of framing.
    """
    gilbert = getattr(websocket.app.state, "gilbert", None)
    if gilbert is None:
        await websocket.close(code=4503)
        return

    user_ctx = await _authenticate_ws(websocket, gilbert)
    if (
        user_ctx is None
        or not getattr(user_ctx, "user_id", "")
        or user_ctx.user_id == "guest"
    ):
        await websocket.close(code=4401)
        return

    svc = _resolve_browser_service(websocket)
    # Newer name; legacy alias still works for older builds.
    target_port = (
        getattr(svc, "get_vnc_target_port", None)
        or getattr(svc, "get_vnc_websockify_port", None)
    )
    if svc is None or target_port is None:
        await websocket.close(code=4503)
        return

    port = target_port(session_id, user_ctx.user_id)
    if port is None:
        await websocket.close(code=4404)
        return

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except Exception:
        logger.exception("failed to connect to x11vnc on %s", port)
        await websocket.close(code=4502)
        return

    await websocket.accept(subprotocol="binary")

    async def client_to_server() -> None:
        try:
            while True:
                data = await websocket.receive_bytes()
                writer.write(data)
                await writer.drain()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("client→server pipe failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def server_to_client() -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            logger.exception("server→client pipe failed")
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    await asyncio.gather(
        client_to_server(),
        server_to_client(),
        return_exceptions=True,
    )
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
