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
    """Serve the noVNC client. Authenticated user-level access only."""
    user_ctx = getattr(request.state, "user_ctx", None)
    if user_ctx is None or not user_ctx.user_id:
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


@router.websocket("/vnc/{session_id}/ws")
async def vnc_proxy(websocket: WebSocket, session_id: str) -> None:
    """Authenticated WS proxy → 127.0.0.1:<websockify_port>."""
    user_ctx = getattr(websocket.state, "user_ctx", None)
    if user_ctx is None or not user_ctx.user_id:
        await websocket.close(code=4401)
        return

    svc = _resolve_browser_service(websocket)
    if svc is None or not hasattr(svc, "get_vnc_websockify_port"):
        await websocket.close(code=4503)
        return

    port = svc.get_vnc_websockify_port(session_id, user_ctx.user_id)
    if port is None:
        await websocket.close(code=4404)
        return

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except Exception:
        logger.exception("failed to connect to websockify on %s", port)
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
