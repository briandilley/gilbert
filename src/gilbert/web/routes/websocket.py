"""WebSocket route — bidirectional protocol for events, RPC, and peer communication."""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.web.ws_protocol import WsConnection, WsConnectionManager, dispatch_frame

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/events")
async def event_stream(websocket: WebSocket) -> None:
    """Bidirectional WebSocket endpoint.

    Authentication: session cookie (web UI) or ``?token=`` query param (peers).
    On connect, sends a ``gilbert.welcome`` frame with the user's identity.
    Auto-subscribes to ``*`` — client can narrow via ``gilbert.sub.*`` frames.
    """
    await websocket.accept()

    gilbert: Gilbert | None = getattr(websocket.app.state, "gilbert", None)
    manager: WsConnectionManager | None = getattr(websocket.app.state, "ws_manager", None)
    if gilbert is None or manager is None:
        await websocket.close(code=1011, reason="Gilbert not available")
        return

    # Authenticate — cookie or token query param
    user_ctx = await _authenticate(websocket, gilbert)

    # Resolve user's effective role level
    user_level = 200  # everyone
    acl_svc = gilbert.service_manager.get_by_capability("access_control")
    if acl_svc is not None:
        if isinstance(acl_svc, AccessControlProvider):
            user_level = acl_svc.get_effective_level(user_ctx)

    # Create connection
    conn = WsConnection(user_ctx, user_level, manager)

    # Seed shared conversation membership
    ai_svc = gilbert.service_manager.get_by_capability("ai_chat")
    if ai_svc is not None:
        try:
            convos = await ai_svc.list_shared_conversations(user_id=user_ctx.user_id, limit=200)
            for c in convos:
                cid = c.get("_id", "")
                if cid and c.get("_is_member", False):
                    conn.shared_conv_ids.add(cid)
        except Exception:
            logger.debug("Failed to seed shared memberships", exc_info=True)

    manager.register(conn)

    # Send welcome frame
    conn.enqueue({
        "type": "gilbert.welcome",
        "user_id": user_ctx.user_id,
        "roles": sorted(user_ctx.roles),
        "subscriptions": sorted(conn.subscriptions),
    })

    logger.info("WebSocket connected: user=%s, level=%d, roles=%s", user_ctx.user_id, user_level, sorted(user_ctx.roles))

    try:
        send_task = asyncio.create_task(_send_loop(websocket, conn))
        recv_task = asyncio.create_task(_recv_loop(websocket, conn))

        done, pending = await asyncio.wait(
            {send_task, recv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket error", exc_info=True)
    finally:
        manager.unregister(conn)
        logger.debug("WebSocket disconnected: user=%s", user_ctx.user_id)


async def _authenticate(websocket: WebSocket, gilbert: Gilbert) -> UserContext:
    """Extract user context from cookie or token query param."""
    # Try session cookie first
    session_id = websocket.cookies.get("gilbert_session")
    # Fall back to token query param
    if not session_id:
        session_id = websocket.query_params.get("token")

    if session_id:
        auth_svc = gilbert.service_manager.get_by_capability("authentication")
        if auth_svc is not None:
            ctx = await auth_svc.validate_session(session_id)
            if ctx is not None:
                return ctx

    return UserContext.GUEST


async def _send_loop(websocket: WebSocket, conn: WsConnection) -> None:
    """Send queued frames to the WebSocket client."""
    while True:
        frame = await conn.queue.get()
        try:
            await websocket.send_json(frame)
        except (TypeError, ValueError) as exc:
            # JSON serialization error — skip this frame, don't kill the connection
            logger.warning("Failed to serialize WS frame type=%s: %s", frame.get("type"), exc)
            continue
        except Exception:
            # Connection error — stop sending
            return


async def _recv_loop(websocket: WebSocket, conn: WsConnection) -> None:
    """Receive and dispatch incoming frames from the client."""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:
            return

        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            conn.enqueue({
                "type": "gilbert.error",
                "error": "Invalid JSON",
                "code": 400,
            })
            continue

        if not isinstance(frame, dict) or "type" not in frame:
            conn.enqueue({
                "type": "gilbert.error",
                "error": "Frame must be a JSON object with a 'type' field",
                "code": 400,
            })
            continue

        try:
            response = await dispatch_frame(conn, frame)
            if response is not None:
                conn.enqueue(response)
        except Exception:
            logger.warning("Frame dispatch error for type=%s", frame.get("type"), exc_info=True)
            conn.enqueue({
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Internal server error",
                "code": 500,
            })
