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
from typing import Any, Callable

from gilbert.interfaces.acl import (
    resolve_default_event_level,
    resolve_default_rpc_level,
)
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.ws import RpcHandler

logger = logging.getLogger(__name__)


def get_rpc_permission_level(frame_type: str) -> int:
    """Resolve the minimum role level for an RPC frame type (longest prefix match)."""
    return resolve_default_rpc_level(frame_type)


# Peer role level
_PEER_LEVEL = 50

# Heartbeat timeout (seconds)
_PING_TIMEOUT = 90


def get_event_visibility_level(event_type: str) -> int:
    """Resolve the minimum role level for an event type (longest prefix match)."""
    return resolve_default_event_level(event_type)


def can_see_event(user_level: int, event_type: str) -> bool:
    """Check if a user at the given level can see this event type."""
    if user_level < 0:  # system user
        return True
    return user_level <= get_event_visibility_level(event_type)


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

        # Invite events are targeted to specific users
        if event.event_type.startswith("chat.invite."):
            return event.data.get("user_id") == self.user_id

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

        data = event.data

        # Filter ui_blocks by for_user / exclude_user for this connection
        ui_blocks = data.get("ui_blocks")
        if ui_blocks and isinstance(ui_blocks, list):
            filtered = [
                b for b in ui_blocks
                if (not b.get("for_user") or b.get("for_user") == self.user_id)
                and b.get("exclude_user") != self.user_id
            ]
            if len(filtered) != len(ui_blocks):
                data = {**data, "ui_blocks": filtered}

        self.enqueue({
            "type": "gilbert.event",
            "event_type": event.event_type,
            "data": data,
            "source": event.source,
            "timestamp": event.timestamp.isoformat() if event.timestamp else "",
        })


class WsConnectionManager:
    """Manages all WebSocket connections and dispatches events.

    Service-provided handlers are discovered via the ``ws_handlers``
    capability (services implementing ``WsHandlerProvider``).  Core
    ``gilbert.*`` handlers are always registered from this module.
    """

    def __init__(self) -> None:
        self._connections: set[WsConnection] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self.gilbert: Any = None
        # Combined handler registry: core + service-provided
        self._handlers: dict[str, RpcHandler] = {}

    def subscribe_to_bus(self, gilbert: Any) -> None:
        """Subscribe to the event bus and discover service handlers."""
        self.gilbert = gilbert

        # Start with core handlers (gilbert.*)
        self._handlers = dict(_rpc_handlers)

        # Discover service-provided handlers
        from gilbert.interfaces.ws import WsHandlerProvider
        for svc in gilbert.service_manager.get_all_by_capability("ws_handlers"):
            if isinstance(svc, WsHandlerProvider):
                service_handlers = svc.get_ws_handlers()
                for frame_type, handler in service_handlers.items():
                    if frame_type in self._handlers:
                        logger.warning(
                            "WS handler conflict: %s already registered, skipping from %s",
                            frame_type, svc.service_info().name,
                        )
                    else:
                        self._handlers[frame_type] = handler
                logger.info(
                    "Registered %d WS handlers from %s",
                    len(service_handlers), svc.service_info().name,
                )

        logger.info("WebSocket manager ready: %d handlers registered", len(self._handlers))

        event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
        if event_bus_svc is None:
            return
        if isinstance(event_bus_svc, EventBusProvider):
            self._unsubscribe = event_bus_svc.bus.subscribe_pattern("*", self._dispatch_event)

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
    gilbert = conn.manager.gilbert
    if gilbert is not None:
        event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
        if event_bus_svc is not None:
            if isinstance(event_bus_svc, EventBusProvider):
                await event_bus_svc.bus.publish(Event(
                    event_type=event_type,
                    data=data,
                    source=source,
                ))

    return {"type": "gilbert.peer.publish.result", "ref": frame.get("id"), "ok": True}


# ── Frame dispatch ────────────────────────────────────────────────────


async def dispatch_frame(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Route an incoming frame to the appropriate handler.

    Checks permissions (hardcoded defaults + entity store overrides),
    then dispatches to the handler from the combined registry.
    """
    frame_type = frame.get("type", "")

    # Look up handler
    handler = conn.manager._handlers.get(frame_type)
    if handler is None:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": f"Unknown frame type: {frame_type}",
            "code": 400,
        }

    # Check RPC permissions — system user bypasses
    if conn.user_level >= 0:
        # Check overrides first (via AccessControlService)
        required_level = _resolve_rpc_level(conn, frame_type)
        if conn.user_level > required_level:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Access denied",
                "code": 403,
            }

    return await handler(conn, frame)


def _resolve_rpc_level(conn: WsConnection, frame_type: str) -> int:
    """Resolve the required level for an RPC frame type.

    Delegates to AccessControlService if available, otherwise falls back
    to hardcoded defaults.
    """
    gilbert = conn.manager.gilbert
    if gilbert is not None:
        acl_svc = gilbert.service_manager.get_by_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            return acl_svc.resolve_rpc_level(frame_type)

    # Fall back to hardcoded defaults
    return get_rpc_permission_level(frame_type)
