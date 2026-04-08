"""WebSocket protocol — bidirectional typed message frames.

Frame format: JSON with ``type`` field as discriminator.
Naming: ``namespace.resource.verb`` (e.g., ``gilbert.sub.add``, ``chat.message.send``).

Core frames (``gilbert.*``) handle subscriptions, heartbeat, events, and peer publishing.
Service frames (``chat.*``, etc.) handle RPC-style request/response operations.
"""

import asyncio
import fnmatch
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event

logger = logging.getLogger(__name__)

# ── Event visibility defaults ──────────────────────────────────────────
# Maps event_type prefix → minimum role level required.
# Longest prefix match wins. System user (level -1) bypasses all.

_EVENT_VISIBILITY: dict[str, int] = {
    # everyone (200)
    "presence.": 200,
    "doorbell.": 200,
    "greeting.": 200,
    "timer.": 200,
    "alarm.": 200,
    "screen.": 200,
    # user (100)
    "chat.": 100,
    "radio_dj.": 100,
    "inbox.": 100,
    "knowledge.": 100,
    # admin (0)
    "service.": 0,
    "config.": 0,
    "acl.": 0,
}
_DEFAULT_VISIBILITY_LEVEL = 100  # unlisted events → user role

# Peer role level
_PEER_LEVEL = 50

# Heartbeat timeout (seconds)
_PING_TIMEOUT = 90


def get_event_visibility_level(event_type: str) -> int:
    """Resolve the minimum role level for an event type (longest prefix match)."""
    best_match = ""
    best_level = _DEFAULT_VISIBILITY_LEVEL
    for prefix, level in _EVENT_VISIBILITY.items():
        if event_type.startswith(prefix) and len(prefix) > len(best_match):
            best_match = prefix
            best_level = level
    return best_level


def can_see_event(user_level: int, event_type: str) -> bool:
    """Check if a user at the given level can see this event type."""
    if user_level < 0:  # system user
        return True
    return user_level <= get_event_visibility_level(event_type)


# Type alias for RPC handler functions
RpcHandler = Callable[["WsConnection", dict[str, Any]], Coroutine[Any, Any, dict[str, Any] | None]]

# Registry of RPC handlers: frame type → handler function
_rpc_handlers: dict[str, RpcHandler] = {}


def rpc_handler(frame_type: str) -> Callable[[RpcHandler], RpcHandler]:
    """Decorator to register an RPC handler for a frame type."""
    def decorator(fn: RpcHandler) -> RpcHandler:
        _rpc_handlers[frame_type] = fn
        return fn
    return decorator


class WsConnection:
    """A single WebSocket connection with its state."""

    def __init__(
        self,
        user_ctx: UserContext,
        user_level: int,
        manager: "WsConnectionManager",
    ) -> None:
        self.user_ctx = user_ctx
        self.user_level = user_level
        self.manager = manager
        self.subscriptions: set[str] = {"*"}  # auto-subscribe to all
        self.shared_conv_ids: set[str] = set()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.last_ping: float = time.monotonic()

    @property
    def user_id(self) -> str:
        return self.user_ctx.user_id

    @property
    def roles(self) -> frozenset[str]:
        return self.user_ctx.roles

    def matches_subscription(self, event_type: str) -> bool:
        """Check if the event matches any of this connection's subscriptions."""
        return any(fnmatch.fnmatch(event_type, pat) for pat in self.subscriptions)

    def can_see_event(self, event_type: str) -> bool:
        """Check role-based visibility for an event type."""
        return can_see_event(self.user_level, event_type)

    def can_see_chat_event(self, event: Event) -> bool:
        """Content-level filter for chat events (membership + visible_to)."""
        if not event.event_type.startswith("chat."):
            return True

        conv_id = event.data.get("conversation_id", "")

        # Update membership tracking
        if event.event_type == "chat.member.joined" and event.data.get("user_id") == self.user_id:
            self.shared_conv_ids.add(conv_id)
        elif event.event_type in ("chat.member.left", "chat.member.kicked"):
            if event.data.get("user_id") == self.user_id:
                self.shared_conv_ids.discard(conv_id)
        elif event.event_type in ("chat.conversation.abandoned", "chat.conversation.destroyed"):
            self.shared_conv_ids.discard(conv_id)
        elif event.event_type == "chat.conversation.created":
            members = event.data.get("members", [])
            if any(m.get("user_id") == self.user_id for m in members):
                self.shared_conv_ids.add(conv_id)

        # Filter by membership
        if event.event_type.startswith(("chat.message.", "chat.member.")):
            if conv_id and conv_id not in self.shared_conv_ids:
                if not (event.event_type == "chat.member.joined"
                        and event.data.get("user_id") == self.user_id):
                    return False
            visible_to = event.data.get("visible_to")
            if visible_to is not None and self.user_id not in visible_to:
                return False

        return True

    def enqueue(self, frame: dict[str, Any]) -> None:
        """Add a frame to the send queue, dropping if full."""
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    def send_event(self, event: Event) -> None:
        """Wrap a bus event as a gilbert.event frame and enqueue it."""
        # Skip peer-originated events for peer connections (loop prevention)
        if event.data.get("_from_peer") and self.user_level <= _PEER_LEVEL:
            return

        self.enqueue({
            "type": "gilbert.event",
            "event_type": event.event_type,
            "data": event.data,
            "source": event.source,
            "timestamp": event.timestamp.isoformat() if event.timestamp else "",
        })


class WsConnectionManager:
    """Manages all WebSocket connections and dispatches events."""

    def __init__(self) -> None:
        self._connections: set[WsConnection] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._gilbert: Any = None

    def subscribe_to_bus(self, gilbert: Any) -> None:
        """Subscribe to the event bus (call once at app startup)."""
        self._gilbert = gilbert
        event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
        if event_bus_svc is None:
            return
        from gilbert.core.services.event_bus import EventBusService
        if isinstance(event_bus_svc, EventBusService):
            self._unsubscribe = event_bus_svc.bus.subscribe_pattern("*", self._dispatch_event)
            logger.info("WebSocket manager subscribed to event bus")

    def shutdown(self) -> None:
        """Unsubscribe from the bus."""
        if self._unsubscribe:
            self._unsubscribe()

    def register(self, conn: WsConnection) -> None:
        self._connections.add(conn)

    def unregister(self, conn: WsConnection) -> None:
        self._connections.discard(conn)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def _dispatch_event(self, event: Event) -> None:
        """Dispatch a bus event to all eligible connections."""
        for conn in self._connections:
            if not conn.matches_subscription(event.event_type):
                continue
            if not conn.can_see_event(event.event_type):
                continue
            if not conn.can_see_chat_event(event):
                continue
            conn.send_event(event)


# ── Core frame handlers (gilbert.*) ───────────────────────────────────


@rpc_handler("gilbert.sub.add")
async def _handle_sub_add(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    patterns = frame.get("patterns", [])
    if isinstance(patterns, list):
        conn.subscriptions.update(patterns)
    return {"type": "gilbert.sub.add.result", "ref": frame.get("id"), "ok": True}


@rpc_handler("gilbert.sub.remove")
async def _handle_sub_remove(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    patterns = frame.get("patterns", [])
    if isinstance(patterns, list):
        conn.subscriptions -= set(patterns)
    return {"type": "gilbert.sub.remove.result", "ref": frame.get("id"), "ok": True}


@rpc_handler("gilbert.sub.list")
async def _handle_sub_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    return {
        "type": "gilbert.sub.list.result",
        "ref": frame.get("id"),
        "subscriptions": sorted(conn.subscriptions),
    }


@rpc_handler("gilbert.ping")
async def _handle_ping(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conn.last_ping = time.monotonic()
    return {"type": "gilbert.pong"}


@rpc_handler("gilbert.peer.publish")
async def _handle_peer_publish(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    if conn.user_level > _PEER_LEVEL:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Peer publishing requires peer or admin role", "code": 403}

    event_type = frame.get("event_type", "")
    data = frame.get("data", {})
    source = f"peer:{frame.get('source', conn.user_id)}"

    if not event_type:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "event_type is required", "code": 400}

    # Tag to prevent loops
    data = {**data, "_from_peer": True}

    # Publish to local bus
    gilbert = conn.manager._gilbert
    if gilbert is not None:
        event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
        if event_bus_svc is not None:
            from gilbert.core.services.event_bus import EventBusService
            if isinstance(event_bus_svc, EventBusService):
                await event_bus_svc.bus.publish(Event(
                    event_type=event_type,
                    data=data,
                    source=source,
                ))

    return {"type": "gilbert.peer.publish.result", "ref": frame.get("id"), "ok": True}


# ── Chat frame handlers (chat.*) ──────────────────────────────────────


@rpc_handler("chat.message.send")
async def _handle_chat_send(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    message = frame.get("message", "").strip()
    if not message:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "message is required", "code": 400}

    conversation_id = frame.get("conversation_id") or None
    gilbert = conn.manager._gilbert
    if gilbert is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Gilbert not available", "code": 503}

    ai_svc = gilbert.service_manager.get_by_capability("ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not running", "code": 503}

    try:
        response_text, conv_id, ui_blocks = await ai_svc.chat(
            user_message=message,
            conversation_id=conversation_id,
            user_ctx=conn.user_ctx,
            ai_call="human_chat",
        )
    except Exception as exc:
        logger.warning("chat.message.send failed", exc_info=True)
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": str(exc), "code": 500}

    return {
        "type": "chat.message.send.result",
        "ref": frame.get("id"),
        "response": response_text,
        "conversation_id": conv_id,
        "ui_blocks": ui_blocks,
    }


@rpc_handler("chat.form.submit")
async def _handle_form_submit(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    block_id = frame.get("block_id")
    values = frame.get("values", {})

    if not conversation_id or not block_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and block_id required", "code": 400}

    gilbert = conn.manager._gilbert
    if gilbert is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Gilbert not available", "code": 503}

    ai_svc = gilbert.service_manager.get_by_capability("ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not running", "code": 503}

    # Mark block as submitted in storage
    storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
    block_title = "Form"
    if storage_svc is not None:
        storage = getattr(storage_svc, "backend", None)
        if storage:
            conv_data = await storage.get("ai_conversations", conversation_id)
            if conv_data:
                for block in conv_data.get("ui_blocks", []):
                    if block.get("block_id") == block_id:
                        block["submitted"] = True
                        block["submission"] = values
                        block_title = block.get("title") or "Form"
                        break
                await storage.put("ai_conversations", conversation_id, conv_data)

    # Build text message for AI
    form_message = f"[Form submitted: {block_title}]\n"
    for k, v in values.items():
        form_message += f"- {k}: {v}\n"

    try:
        response_text, conv_id, ui_blocks = await ai_svc.chat(
            user_message=form_message,
            conversation_id=conversation_id,
            user_ctx=conn.user_ctx,
            ai_call="human_chat",
        )
    except Exception as exc:
        logger.warning("chat.form.submit failed", exc_info=True)
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": str(exc), "code": 500}

    return {
        "type": "chat.form.submit.result",
        "ref": frame.get("id"),
        "response": response_text,
        "conversation_id": conv_id,
        "ui_blocks": ui_blocks,
    }


@rpc_handler("chat.history.load")
async def _handle_chat_history(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    if not conversation_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

    gilbert = conn.manager._gilbert
    if gilbert is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Gilbert not available", "code": 503}

    storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
    if storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    storage = getattr(storage_svc, "backend", None)
    if storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Conversation not found", "code": 404}

    is_shared = data.get("shared", False)
    display_messages = []
    for m in data.get("messages", []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        visible_to = m.get("visible_to")
        if visible_to is not None and conn.user_id not in visible_to:
            continue
        msg: dict[str, Any] = {"role": role, "content": m.get("content", "")}
        if is_shared:
            msg["author_id"] = m.get("author_id", "")
            msg["author_name"] = m.get("author_name", "")
        display_messages.append(msg)

    ui_blocks = [b for b in data.get("ui_blocks", [])
                 if not b.get("for_user") or b.get("for_user") == conn.user_id]

    result: dict[str, Any] = {
        "type": "chat.history.load.result",
        "ref": frame.get("id"),
        "messages": display_messages,
        "ui_blocks": ui_blocks,
        "shared": is_shared,
        "title": data.get("title", ""),
    }
    if is_shared:
        result["members"] = data.get("members", [])
    return result


async def dispatch_frame(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Route an incoming frame to the appropriate handler."""
    frame_type = frame.get("type", "")
    handler = _rpc_handlers.get(frame_type)
    if handler is not None:
        return await handler(conn, frame)

    return {
        "type": "gilbert.error",
        "ref": frame.get("id"),
        "error": f"Unknown frame type: {frame_type}",
        "code": 400,
    }
