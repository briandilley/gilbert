"""WebSocket route — bidirectional protocol for events, RPC, and peer communication."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from gilbert.core.app import Gilbert
from gilbert.interfaces.ai import SharedConversationProvider
from gilbert.interfaces.auth import (
    AccessControlProvider,
    GuestPolicy,
    SessionValidator,
    UserContext,
)
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
    if user_ctx is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

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
    if isinstance(ai_svc, SharedConversationProvider):
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
    conn.enqueue(
        {
            "type": "gilbert.welcome",
            "user_id": user_ctx.user_id,
            "roles": sorted(user_ctx.roles),
            "subscriptions": sorted(conn.subscriptions),
        }
    )

    logger.info(
        "WebSocket connected: user=%s, level=%d, roles=%s",
        user_ctx.user_id,
        user_level,
        sorted(user_ctx.roles),
    )

    # Tasks spawned per-frame by ``_recv_loop`` so a handler that
    # awaits for a client reply (e.g. the MCP browser bridge) doesn't
    # block the recv loop from reading the reply frame it's waiting
    # on. Tracked here so the connection cleanup can cancel any that
    # are still pending on disconnect instead of leaking.
    dispatch_tasks: set[asyncio.Task[None]] = set()

    try:
        send_task = asyncio.create_task(_send_loop(websocket, conn))
        recv_task = asyncio.create_task(_recv_loop(websocket, conn, dispatch_tasks))

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
        for task in dispatch_tasks:
            task.cancel()
        manager.unregister(conn)
        logger.debug("WebSocket disconnected: user=%s", user_ctx.user_id)


async def _authenticate(websocket: WebSocket, gilbert: Gilbert) -> UserContext | None:
    """Extract user context from cookie or token query param.

    Returns ``None`` if no valid session is provided and the auth
    service has guests disabled — the caller should close the
    connection. Returns ``UserContext.GUEST`` when guests are
    permitted (or when the auth service isn't running at all).
    """
    # Try session cookie first
    session_id = websocket.cookies.get("gilbert_session")
    # Fall back to token query param
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


async def _recv_loop(
    websocket: WebSocket,
    conn: WsConnection,
    dispatch_tasks: set[asyncio.Task[None]],
) -> None:
    """Receive and dispatch incoming frames from the client.

    Each frame is handled in a dedicated background task so a handler
    that ``await``s for a subsequent frame on the same connection
    (e.g. ``call_client``'s outbound-RPC future, used by the MCP
    browser bridge) can't block the recv loop from reading the reply
    it's waiting on. Without this split, a handler that calls
    ``await conn.call_client(...)`` would wedge the entire socket:
    the reply frame the future needs is sitting in the WebSocket
    receive buffer, but the loop that would read it is blocked on
    the outer ``await dispatch_frame``.

    Tasks are tracked in ``dispatch_tasks`` so the connection
    cleanup can cancel any still-running handlers on disconnect
    instead of leaking them.
    """
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
            conn.enqueue(
                {
                    "type": "gilbert.error",
                    "error": "Invalid JSON",
                    "code": 400,
                }
            )
            continue

        if not isinstance(frame, dict) or "type" not in frame:
            conn.enqueue(
                {
                    "type": "gilbert.error",
                    "error": "Frame must be a JSON object with a 'type' field",
                    "code": 400,
                }
            )
            continue

        task = asyncio.create_task(_dispatch_and_respond(conn, frame))
        dispatch_tasks.add(task)
        task.add_done_callback(dispatch_tasks.discard)


async def _dispatch_and_respond(conn: WsConnection, frame: dict[str, Any]) -> None:
    """Run a single frame through ``dispatch_frame`` and enqueue
    the response (or a server-side error frame on exception).

    Separated out of ``_recv_loop`` so each frame runs in its own
    task and can ``await`` for other frames on the same connection
    without blocking new frames from being read.
    """
    try:
        response = await dispatch_frame(conn, frame)
    except Exception:
        logger.warning(
            "Frame dispatch error for type=%s",
            frame.get("type"),
            exc_info=True,
        )
        conn.enqueue(
            {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Internal server error",
                "code": 500,
            }
        )
        return
    if response is not None:
        conn.enqueue(response)
