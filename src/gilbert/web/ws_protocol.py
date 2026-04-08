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


# ── Helper: admin check ───────────────────────────────────────────────


def _require_admin(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error frame if the connection is not admin-level, else None."""
    if conn.user_level > 0:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Admin access required", "code": 403}
    return None


def _get_gilbert(conn: WsConnection) -> Any:
    """Get the Gilbert instance from the connection manager."""
    return conn.manager._gilbert


def _get_service(conn: WsConnection, capability: str) -> Any:
    """Get a service by capability, or None."""
    gilbert = _get_gilbert(conn)
    if gilbert is None:
        return None
    return gilbert.service_manager.get_by_capability(capability)


# ── Chat conversation management ─────────────────────────────────────


@rpc_handler("chat.conversation.list")
async def _handle_conversation_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    ai_svc = _get_service(conn, "ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not running", "code": 503}

    from gilbert.web.chat_helpers import conv_summary

    personal = await ai_svc.list_conversations(user_id=conn.user_id, limit=30)
    shared = await ai_svc.list_shared_conversations(user_id=conn.user_id, limit=30)

    conversations = [conv_summary(c, shared=True) for c in shared]
    conversations += [conv_summary(c, shared=False) for c in personal]

    return {"type": "chat.conversation.list.result", "ref": frame.get("id"), "conversations": conversations}


@rpc_handler("chat.conversation.rename")
async def _handle_conversation_rename(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    title = (frame.get("title") or "").strip()
    if not conversation_id or not title:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and title required", "code": 400}

    gilbert = _get_gilbert(conn)
    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}
    storage = getattr(storage_svc, "backend", None)
    if storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Conversation not found", "code": 404}

    from gilbert.web.chat_helpers import check_conversation_access, publish_event
    err = check_conversation_access(data, conn.user_ctx)
    if err:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": err, "code": 403}

    data["title"] = title
    await storage.put("ai_conversations", conversation_id, data)
    await publish_event(gilbert, "chat.conversation.renamed", {"conversation_id": conversation_id, "title": title})

    return {"type": "chat.conversation.rename.result", "ref": frame.get("id"), "status": "ok", "title": title}


@rpc_handler("chat.conversation.delete")
async def _handle_conversation_delete(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    if not conversation_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}
    storage = getattr(storage_svc, "backend", None)

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Conversation not found", "code": 404}
    if data.get("shared"):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Use room destroy for shared conversations", "code": 400}
    conv_owner = data.get("user_id", "")
    if conv_owner and conn.user_id != "system" and conv_owner != conn.user_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Access denied", "code": 403}

    await storage.delete("ai_conversations", conversation_id)
    return {"type": "chat.conversation.delete.result", "ref": frame.get("id"), "status": "ok"}


# ── Chat room management ─────────────────────────────────────────────


@rpc_handler("chat.room.create")
async def _handle_room_create(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    title = (frame.get("title") or "").strip()
    if not title:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "title required", "code": 400}
    visibility = frame.get("visibility", "public")

    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}
    storage = getattr(storage_svc, "backend", None)

    import uuid as _uuid
    conv_id = str(_uuid.uuid4())
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc).isoformat()

    members = [{"user_id": conn.user_id, "display_name": conn.user_ctx.display_name, "role": "owner", "joined_at": now}]
    data = {
        "shared": True,
        "visibility": visibility,
        "title": title,
        "user_id": conn.user_id,
        "members": members,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }
    await storage.put("ai_conversations", conv_id, data)

    gilbert = _get_gilbert(conn)
    from gilbert.web.chat_helpers import publish_event
    await publish_event(gilbert, "chat.conversation.created", {
        "conversation_id": conv_id, "title": title, "shared": True,
        "members": members, "visibility": visibility,
    })

    return {
        "type": "chat.room.create.result", "ref": frame.get("id"),
        "conversation_id": conv_id, "title": title,
        "members": [{"user_id": m["user_id"], "display_name": m["display_name"]} for m in members],
    }


@rpc_handler("chat.room.join")
async def _handle_room_join(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    if not conversation_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    storage = getattr(storage_svc, "backend", None) if storage_svc else None
    if storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    data = await storage.get("ai_conversations", conversation_id)
    if data is None or not data.get("shared"):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

    members = data.get("members", [])
    if any(m.get("user_id") == conn.user_id for m in members):
        return {"type": "chat.room.join.result", "ref": frame.get("id"), "status": "already_member"}

    from datetime import datetime, timezone as tz
    members.append({"user_id": conn.user_id, "display_name": conn.user_ctx.display_name, "role": "member", "joined_at": datetime.now(tz.utc).isoformat()})
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    gilbert = _get_gilbert(conn)
    from gilbert.web.chat_helpers import publish_event
    await publish_event(gilbert, "chat.member.joined", {
        "conversation_id": conversation_id, "user_id": conn.user_id,
        "display_name": conn.user_ctx.display_name,
    })

    return {"type": "chat.room.join.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("chat.room.leave")
async def _handle_room_leave(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    if not conversation_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    storage = getattr(storage_svc, "backend", None) if storage_svc else None
    if storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    data = await storage.get("ai_conversations", conversation_id)
    if data is None or not data.get("shared"):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

    gilbert = _get_gilbert(conn)
    from gilbert.web.chat_helpers import publish_event

    # Owner leaving destroys the room
    if data.get("user_id") == conn.user_id:
        await storage.delete("ai_conversations", conversation_id)
        await publish_event(gilbert, "chat.conversation.destroyed", {"conversation_id": conversation_id})
        return {"type": "chat.room.leave.result", "ref": frame.get("id"), "status": "destroyed"}

    members = [m for m in data.get("members", []) if m.get("user_id") != conn.user_id]
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)
    await publish_event(gilbert, "chat.member.left", {"conversation_id": conversation_id, "user_id": conn.user_id})

    return {"type": "chat.room.leave.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("chat.room.kick")
async def _handle_room_kick(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    target_user = frame.get("user_id")
    if not conversation_id or not target_user:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and user_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    storage = getattr(storage_svc, "backend", None) if storage_svc else None
    if storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    data = await storage.get("ai_conversations", conversation_id)
    if data is None or not data.get("shared"):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}
    if data.get("user_id") != conn.user_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Only the room owner can kick members", "code": 403}

    members = [m for m in data.get("members", []) if m.get("user_id") != target_user]
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    gilbert = _get_gilbert(conn)
    from gilbert.web.chat_helpers import publish_event
    await publish_event(gilbert, "chat.member.kicked", {"conversation_id": conversation_id, "user_id": target_user})

    return {"type": "chat.room.kick.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("chat.room.invite")
async def _handle_room_invite(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = frame.get("conversation_id")
    target_user = frame.get("user_id")
    display_name = frame.get("display_name", "")
    if not conversation_id or not target_user:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "conversation_id and user_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    storage = getattr(storage_svc, "backend", None) if storage_svc else None
    if storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    data = await storage.get("ai_conversations", conversation_id)
    if data is None or not data.get("shared"):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Room not found", "code": 404}

    members = data.get("members", [])
    if any(m.get("user_id") == target_user for m in members):
        return {"type": "chat.room.invite.result", "ref": frame.get("id"), "status": "already_member"}

    from datetime import datetime, timezone as tz
    members.append({"user_id": target_user, "display_name": display_name, "role": "member", "joined_at": datetime.now(tz.utc).isoformat()})
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    gilbert = _get_gilbert(conn)
    from gilbert.web.chat_helpers import publish_event
    await publish_event(gilbert, "chat.member.joined", {
        "conversation_id": conversation_id, "user_id": target_user, "display_name": display_name,
    })

    return {"type": "chat.room.invite.result", "ref": frame.get("id"), "status": "ok"}


# ── Roles & Admin ─────────────────────────────────────────────────────


@rpc_handler("roles.role.list")
async def _handle_role_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    roles = await acl.list_roles()
    for r in roles:
        r.pop("_id", None)
    return {"type": "roles.role.list.result", "ref": frame.get("id"), "roles": roles}


@rpc_handler("roles.role.create")
async def _handle_role_create(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.create_role(frame.get("name", ""), frame.get("level", 100), frame.get("description", ""))
    return {"type": "roles.role.create.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.role.update")
async def _handle_role_update(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.update_role(frame.get("name", ""), level=frame.get("level"), description=frame.get("description"))
    return {"type": "roles.role.update.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.role.delete")
async def _handle_role_delete(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.delete_role(frame.get("name", ""))
    return {"type": "roles.role.delete.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.tool.list")
async def _handle_tool_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    gilbert = _get_gilbert(conn)
    acl = _get_service(conn, "access_control")
    if acl is None or gilbert is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Service not available", "code": 503}

    from gilbert.interfaces.tools import ToolProvider
    tools = []
    for svc in gilbert.service_manager.get_all_by_capability("ai_tools"):
        if isinstance(svc, ToolProvider):
            for t in svc.get_tools():
                effective = acl._tool_overrides.get(t.name, t.required_role)
                tools.append({
                    "provider": svc.tool_provider_name,
                    "tool_name": t.name,
                    "default_role": t.required_role,
                    "effective_role": effective,
                    "has_override": t.name in acl._tool_overrides,
                })
    role_names = sorted(acl._role_levels.keys())
    return {"type": "roles.tool.list.result", "ref": frame.get("id"), "tools": tools, "role_names": role_names}


@rpc_handler("roles.tool.set")
async def _handle_tool_set(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.set_tool_override(frame.get("tool_name", ""), frame.get("role", ""))
    return {"type": "roles.tool.set.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.tool.clear")
async def _handle_tool_clear(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.clear_tool_override(frame.get("tool_name", ""))
    return {"type": "roles.tool.clear.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.profile.list")
async def _handle_profile_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    ai_svc = _get_service(conn, "ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not available", "code": 503}

    gilbert = _get_gilbert(conn)
    profiles_raw = await ai_svc.list_profiles()
    assignments = ai_svc._assignments

    profiles = []
    for p in profiles_raw:
        assigned = [call for call, prof in assignments.items() if prof == p.name]
        profiles.append({
            "name": p.name, "description": p.description, "tool_mode": p.tool_mode,
            "tools": list(p.tools), "tool_roles": dict(p.tool_roles),
            "assigned_calls": assigned,
        })

    declared_calls: set[str] = set()
    for svc in gilbert.service_manager.get_all_by_capability("ai_tools"):
        info = svc.service_info()
        declared_calls.update(info.ai_calls)

    from gilbert.interfaces.tools import ToolProvider
    all_tools: set[str] = set()
    for svc in gilbert.service_manager.get_all_by_capability("ai_tools"):
        if isinstance(svc, ToolProvider):
            for t in svc.get_tools():
                all_tools.add(t.name)

    return {
        "type": "roles.profile.list.result", "ref": frame.get("id"),
        "profiles": profiles, "declared_calls": sorted(declared_calls),
        "profile_names": [p["name"] for p in profiles],
        "all_tool_names": sorted(all_tools),
    }


@rpc_handler("roles.profile.save")
async def _handle_profile_save(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    ai_svc = _get_service(conn, "ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not available", "code": 503}

    from gilbert.core.services.ai import AIContextProfile
    profile = AIContextProfile(
        name=frame.get("name", ""),
        description=frame.get("description", ""),
        tool_mode=frame.get("tool_mode", "all"),
        tools=frame.get("tools", []),
        tool_roles=frame.get("tool_roles", {}),
    )
    await ai_svc.set_profile(profile)
    return {"type": "roles.profile.save.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.profile.delete")
async def _handle_profile_delete(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    ai_svc = _get_service(conn, "ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not available", "code": 503}
    await ai_svc.delete_profile(frame.get("name", ""))
    return {"type": "roles.profile.delete.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.profile.assign")
async def _handle_profile_assign(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    ai_svc = _get_service(conn, "ai_chat")
    if ai_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "AI service not available", "code": 503}
    await ai_svc.set_assignment(frame.get("ai_call", ""), frame.get("profile_name", ""))
    return {"type": "roles.profile.assign.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.user.list")
async def _handle_user_role_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    gilbert = _get_gilbert(conn)
    user_svc = _get_service(conn, "users")
    acl = _get_service(conn, "access_control")
    if user_svc is None or acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Service not available", "code": 503}

    users = await user_svc.list_users()
    result = []
    for u in users:
        result.append({
            "user_id": u.get("user_id", u.get("_id", "")),
            "email": u.get("email", ""),
            "display_name": u.get("display_name", ""),
            "roles": u.get("roles", []),
        })
    role_names = sorted(acl._role_levels.keys())
    return {"type": "roles.user.list.result", "ref": frame.get("id"), "users": result, "role_names": role_names}


@rpc_handler("roles.user.set")
async def _handle_user_role_set(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    user_svc = _get_service(conn, "users")
    if user_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "User service not available", "code": 503}
    user_id = frame.get("user_id", "")
    roles = frame.get("roles", [])
    await user_svc.backend.update_user(user_id, {"roles": roles})
    return {"type": "roles.user.set.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.collection.list")
async def _handle_collection_acl_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    storage_svc = _get_service(conn, "entity_storage")
    if acl is None or storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Service not available", "code": 503}

    collections = await storage_svc.backend.list_collections()
    acl_entries = []
    for col in sorted(collections):
        entry = acl._collection_acl.get(col)
        acl_entries.append({
            "collection": col,
            "read_role": entry["read_role"] if entry else "user",
            "write_role": entry["write_role"] if entry else "admin",
            "has_custom": entry is not None,
        })
    roles = await acl.list_roles()
    return {
        "type": "roles.collection.list.result", "ref": frame.get("id"),
        "collections": acl_entries, "role_names": [r["name"] for r in roles],
    }


@rpc_handler("roles.collection.set")
async def _handle_collection_acl_set(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.set_collection_acl(frame.get("collection", ""), read_role=frame.get("read_role", "user"), write_role=frame.get("write_role", "admin"))
    return {"type": "roles.collection.set.result", "ref": frame.get("id"), "status": "ok"}


@rpc_handler("roles.collection.clear")
async def _handle_collection_acl_clear(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    acl = _get_service(conn, "access_control")
    if acl is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "ACL service not available", "code": 503}
    await acl.clear_collection_acl(frame.get("collection", ""))
    return {"type": "roles.collection.clear.result", "ref": frame.get("id"), "status": "ok"}


# ── Inbox ─────────────────────────────────────────────────────────────


@rpc_handler("inbox.stats.get")
async def _handle_inbox_stats(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    from gilbert.core.services.inbox import InboxService
    svc = _get_service(conn, "email")
    if svc is None or not isinstance(svc, InboxService):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Inbox not available", "code": 503}
    stats = await svc.get_stats()
    return {"type": "inbox.stats.get.result", "ref": frame.get("id"), **stats}


@rpc_handler("inbox.message.list")
async def _handle_inbox_messages(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    from gilbert.core.services.inbox import InboxService
    svc = _get_service(conn, "email")
    if svc is None or not isinstance(svc, InboxService):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Inbox not available", "code": 503}
    messages = await svc.search_messages(
        sender=frame.get("sender", ""), subject=frame.get("subject", ""),
        limit=frame.get("limit", 50), include_body=False,
    )
    summaries = []
    for m in messages:
        snippet = m.get("snippet", "")
        if not snippet:
            body = m.get("body_text", "")
            snippet = body[:120] + ("..." if len(body) > 120 else "")
        summaries.append({
            "message_id": m.get("_id", ""), "thread_id": m.get("thread_id", ""),
            "subject": m.get("subject", ""), "sender_email": m.get("sender_email", ""),
            "sender_name": m.get("sender_name", ""), "date": m.get("date", ""),
            "is_inbound": m.get("is_inbound", True), "snippet": snippet,
        })
    return {"type": "inbox.message.list.result", "ref": frame.get("id"), "messages": summaries, "total": len(summaries)}


@rpc_handler("inbox.message.get")
async def _handle_inbox_message_detail(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    from gilbert.core.services.inbox import InboxService
    svc = _get_service(conn, "email")
    if svc is None or not isinstance(svc, InboxService):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Inbox not available", "code": 503}
    record = await svc.get_message(frame.get("message_id", ""))
    if not record:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Message not found", "code": 404}
    return {
        "type": "inbox.message.get.result", "ref": frame.get("id"),
        "message_id": record.get("_id", ""), "thread_id": record.get("thread_id", ""),
        "subject": record.get("subject", ""), "sender_email": record.get("sender_email", ""),
        "sender_name": record.get("sender_name", ""), "date": record.get("date", ""),
        "to": record.get("to", []), "cc": record.get("cc", []),
        "body_text": record.get("body_text", ""), "body_html": record.get("body_html", ""),
        "is_inbound": record.get("is_inbound", True),
    }


@rpc_handler("inbox.thread.get")
async def _handle_inbox_thread(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    from gilbert.core.services.inbox import InboxService
    svc = _get_service(conn, "email")
    if svc is None or not isinstance(svc, InboxService):
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Inbox not available", "code": 503}
    messages = await svc.get_thread(frame.get("thread_id", ""))
    result = []
    for m in messages:
        result.append({
            "message_id": m.get("_id", ""), "thread_id": m.get("thread_id", ""),
            "subject": m.get("subject", ""), "sender_email": m.get("sender_email", ""),
            "sender_name": m.get("sender_name", ""), "date": m.get("date", ""),
            "to": m.get("to", []), "cc": m.get("cc", []),
            "body_text": m.get("body_text", ""), "body_html": m.get("body_html", ""),
            "is_inbound": m.get("is_inbound", True),
        })
    return {"type": "inbox.thread.get.result", "ref": frame.get("id"), "messages": result}


@rpc_handler("inbox.pending.list")
async def _handle_inbox_pending(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    gilbert = _get_gilbert(conn)
    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "inbox.pending.list.result", "ref": frame.get("id"), "pending": []}
    raw_storage = getattr(storage_svc, "raw_backend", None)
    if raw_storage is None:
        return {"type": "inbox.pending.list.result", "ref": frame.get("id"), "pending": []}

    from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField
    from datetime import datetime, timedelta, timezone as tz
    cutoff = (datetime.now(tz.utc) - timedelta(days=1)).isoformat()
    _PENDING_COLLECTIONS = ["gilbert.plugin.current-sales-assistant.pending_replies"]

    pending: list[dict[str, Any]] = []
    for collection in _PENDING_COLLECTIONS:
        try:
            pending_results = await raw_storage.query(Query(
                collection=collection,
                filters=[Filter(field="status", op=FilterOp.EQ, value="pending")],
                sort=[SortField(field="send_at", descending=False)],
            ))
            failed_results = await raw_storage.query(Query(
                collection=collection,
                filters=[Filter(field="status", op=FilterOp.EQ, value="failed"), Filter(field="send_at", op=FilterOp.GTE, value=cutoff)],
                sort=[SortField(field="send_at", descending=False)],
            ))
            for r in pending_results + failed_results:
                pending.append({
                    "id": r.get("_id", ""), "collection": collection,
                    "customer_email": r.get("customer_email", ""), "subject": r.get("subject", ""),
                    "status": r.get("status", ""), "send_at": r.get("send_at", ""),
                })
        except Exception:
            pass
    return {"type": "inbox.pending.list.result", "ref": frame.get("id"), "pending": pending}


@rpc_handler("inbox.pending.cancel")
async def _handle_inbox_cancel(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    reply_id = frame.get("reply_id", "")
    if not reply_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "reply_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    raw_storage = getattr(storage_svc, "raw_backend", None) if storage_svc else None
    if raw_storage is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    _PENDING_COLLECTIONS = ["gilbert.plugin.current-sales-assistant.pending_replies"]
    for collection in _PENDING_COLLECTIONS:
        try:
            existing = await raw_storage.get(collection, reply_id)
            if existing and existing.get("status") in ("pending", "failed"):
                existing["status"] = "cancelled"
                await raw_storage.put(collection, reply_id, existing)
                return {"type": "inbox.pending.cancel.result", "ref": frame.get("id"), "status": "cancelled"}
        except Exception:
            pass
    return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Pending reply not found", "code": 404}


# ── Documents ─────────────────────────────────────────────────────────


@rpc_handler("documents.list")
async def _handle_documents_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    from gilbert.core.services.knowledge import KnowledgeService
    svc = _get_service(conn, "knowledge")
    if svc is None or not isinstance(svc, KnowledgeService):
        return {"type": "documents.list.result", "ref": frame.get("id"), "sources": []}
    sources = await svc.list_sources_with_trees()
    return {"type": "documents.list.result", "ref": frame.get("id"), "sources": sources}


@rpc_handler("documents.search")
async def _handle_documents_search(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    query = frame.get("query", "").strip()
    if not query:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "query required", "code": 400}

    from gilbert.core.services.knowledge import KnowledgeService
    svc = _get_service(conn, "knowledge")
    if svc is None or not isinstance(svc, KnowledgeService):
        return {"type": "documents.search.result", "ref": frame.get("id"), "results": [], "query": query}

    results = await svc.search(query, source_id=frame.get("source_id"), max_results=frame.get("max_results", 20))
    return {"type": "documents.search.result", "ref": frame.get("id"), "results": results, "query": query}


# ── Dashboard ─────────────────────────────────────────────────────────


@rpc_handler("dashboard.get")
async def _handle_dashboard(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    gilbert = _get_gilbert(conn)
    if gilbert is None:
        return {"type": "dashboard.get.result", "ref": frame.get("id"), "cards": []}

    sm = gilbert.service_manager
    cards = []
    _DASHBOARD_CARDS = [
        {"title": "Chat", "description": "Talk with Gilbert", "url": "/chat", "icon": "message-square", "required_role": "everyone"},
        {"title": "Documents", "description": "Knowledge base", "url": "/documents", "icon": "file-text", "required_role": "user"},
        {"title": "Inbox", "description": "Email management", "url": "/inbox", "icon": "inbox", "required_role": "admin"},
        {"title": "Roles", "description": "Roles & access control", "url": "/roles", "icon": "shield", "required_role": "admin"},
        {"title": "Entities", "description": "Entity browser", "url": "/entities", "icon": "database", "required_role": "admin"},
        {"title": "System", "description": "Service inspector", "url": "/system", "icon": "settings", "required_role": "admin"},
    ]

    acl = _get_service(conn, "access_control")
    for card in _DASHBOARD_CARDS:
        if acl is not None:
            required_level = acl.get_role_level(card["required_role"])
            if conn.user_level > required_level:
                continue
        cards.append(card)

    return {"type": "dashboard.get.result", "ref": frame.get("id"), "cards": cards}


# ── System ────────────────────────────────────────────────────────────


@rpc_handler("system.services.list")
async def _handle_system_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err

    gilbert = _get_gilbert(conn)
    if gilbert is None:
        return {"type": "system.services.list.result", "ref": frame.get("id"), "services": []}

    from gilbert.interfaces.configuration import Configurable
    from gilbert.interfaces.tools import ToolProvider
    from gilbert.core.services.configuration import ConfigurationService

    sm = gilbert.service_manager
    config_svc = sm.get_by_capability("configuration")
    services = []

    for name in list(sm._registered.keys()):
        svc = sm._registered[name]
        info = svc.service_info()
        started = name in sm.started_services
        failed = name in sm.failed_services

        entry: dict[str, Any] = {
            "name": info.name,
            "capabilities": sorted(info.capabilities),
            "requires": sorted(info.requires),
            "optional": sorted(info.optional),
            "ai_calls": sorted(info.ai_calls),
            "events": sorted(info.events),
            "started": started,
            "failed": failed,
            "config_params": [],
            "config_values": {},
            "tools": [],
        }

        if isinstance(svc, Configurable):
            entry["config_namespace"] = svc.config_namespace
            entry["config_params"] = [
                {"key": p.key, "type": p.type.value, "description": p.description, "default": p.default, "restart_required": p.restart_required}
                for p in svc.config_params()
            ]
            if isinstance(config_svc, ConfigurationService):
                entry["config_values"] = config_svc.get_section(svc.config_namespace)

        if isinstance(svc, ToolProvider):
            entry["tools"] = [
                {"name": t.name, "description": t.description, "required_role": t.required_role,
                 "parameters": [{"name": p.name, "type": p.type.value, "description": p.description, "required": p.required} for p in t.parameters]}
                for t in svc.get_tools()
            ]

        services.append(entry)

    return {"type": "system.services.list.result", "ref": frame.get("id"), "services": services}


# ── Entities ──────────────────────────────────────────────────────────


@rpc_handler("entities.collection.list")
async def _handle_entities_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err
    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "entities.collection.list.result", "ref": frame.get("id"), "groups": []}

    collections = await storage_svc.backend.list_collections()
    groups: dict[str, list[dict[str, Any]]] = {}
    for col in sorted(collections):
        parts = col.rsplit(".", 1)
        ns = parts[0] if len(parts) > 1 else "(default)"
        short = parts[-1]
        count = await storage_svc.backend.count(col) if hasattr(storage_svc.backend, "count") else 0
        groups.setdefault(ns, []).append({"name": col, "short_name": short, "count": count})

    result = [{"namespace": ns, "collections": cols} for ns, cols in groups.items()]
    return {"type": "entities.collection.list.result", "ref": frame.get("id"), "groups": result}


@rpc_handler("entities.collection.query")
async def _handle_entities_query(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err

    collection = frame.get("collection", "")
    if not collection:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "collection required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    from gilbert.interfaces.storage import Query, SortField
    page = int(frame.get("page", 1))
    sort_field = frame.get("sort", "_id")
    order = frame.get("order", "asc")
    page_size = 50
    offset = (page - 1) * page_size

    sort = [SortField(field=sort_field, descending=(order == "desc"))]
    entities = await storage_svc.backend.query(Query(
        collection=collection, sort=sort, limit=page_size, offset=offset,
    ))
    total = await storage_svc.backend.count(collection) if hasattr(storage_svc.backend, "count") else len(entities)
    total_pages = max(1, (total + page_size - 1) // page_size)

    # Derive sortable fields from first entity
    sortable_fields = []
    if entities:
        sortable_fields = sorted(entities[0].keys())

    # FK map
    fk_map = {}
    if hasattr(storage_svc.backend, "get_foreign_keys"):
        fk_map = await storage_svc.backend.get_foreign_keys(collection) or {}

    return {
        "type": "entities.collection.query.result", "ref": frame.get("id"),
        "collection": collection, "entities": entities, "total": total,
        "page": page, "total_pages": total_pages,
        "sortable_fields": sortable_fields, "fk_map": fk_map,
    }


@rpc_handler("entities.entity.get")
async def _handle_entity_get(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    err = _require_admin(conn, frame)
    if err:
        return err

    collection = frame.get("collection", "")
    entity_id = frame.get("entity_id", "")
    if not collection or not entity_id:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "collection and entity_id required", "code": 400}

    storage_svc = _get_service(conn, "entity_storage")
    if storage_svc is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

    entity = await storage_svc.backend.get(collection, entity_id)
    if entity is None:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Entity not found", "code": 404}

    fk_map = {}
    if hasattr(storage_svc.backend, "get_foreign_keys"):
        fk_map = await storage_svc.backend.get_foreign_keys(collection) or {}

    return {
        "type": "entities.entity.get.result", "ref": frame.get("id"),
        "collection": collection, "entity_id": entity_id,
        "entity": entity, "fk_map": fk_map,
    }


# ── Screens ───────────────────────────────────────────────────────────


@rpc_handler("screens.list")
async def _handle_screens_list(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    from gilbert.core.services.screens import ScreenService
    svc = _get_service(conn, "screen_display")
    if svc is None or not isinstance(svc, ScreenService):
        return {"type": "screens.list.result", "ref": frame.get("id"), "screens": []}
    screens = svc.list_screens()
    return {"type": "screens.list.result", "ref": frame.get("id"), "screens": screens}


# ── Frame dispatch ────────────────────────────────────────────────────


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
