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
import uuid
from collections.abc import Callable
from typing import Any

from gilbert.interfaces.acl import (
    BUILTIN_ROLE_LEVELS,
    DEFAULT_RPC_LEVEL,
    resolve_default_event_level,
    resolve_default_rpc_level,
    resolve_event_visibility,
)
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.service import Service
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
    """Resolve the minimum role level for an event type (longest prefix match).

    Pure-prefix resolution. Per-event overrides via
    ``event.data["required_role"]`` go through
    :func:`resolve_event_visibility` instead — call that one when an
    ``Event`` is in hand.
    """
    return resolve_default_event_level(event_type)


def can_see_event(
    user_level: int,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> bool:
    """Check if a user at the given level can see this event.

    When ``data`` is provided, honors the per-event
    ``data["required_role"]`` override; otherwise falls back to pure
    prefix-based resolution (the default behaviour for tests / call
    sites that don't have the full event payload).
    """
    if user_level < 0:  # system user
        return True
    required_level = resolve_event_visibility(event_type, data)
    return user_level <= required_level


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
        self.connection_id: str = uuid.uuid4().hex
        self.subscriptions: set[str] = {"*"}  # auto-subscribe to all
        self.shared_conv_ids: set[str] = set()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.last_ping: float = time.monotonic()
        self._pending_outbound: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._outbound_counter: int = 0
        self._close_callbacks: list[Callable[[], None]] = []

    @property
    def user_id(self) -> str:
        return self.user_ctx.user_id

    @property
    def display_name(self) -> str:
        return self.user_ctx.display_name or self.user_id

    @property
    def roles(self) -> frozenset[str]:
        return self.user_ctx.roles

    def matches_subscription(self, event_type: str) -> bool:
        """Check if the event matches any of this connection's subscriptions."""
        return any(fnmatch.fnmatch(event_type, pat) for pat in self.subscriptions)

    def can_see_event(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Check role-based visibility for an event.

        ``data`` is passed through so the per-event
        ``data["required_role"]`` override is honored — admin-gated
        cameras whose detection events ride a prefix-everyone topic
        (``camera.event.detected``) get correctly filtered out for
        non-admin connections.
        """
        return can_see_event(self.user_level, event_type, data)

    def can_see_auth_event(self, event: Event) -> bool:
        """Content-level filter for auth events.

        ``auth.user.roles.changed`` is user-level in the prefix map,
        but we additionally restrict delivery so a non-admin only sees
        events for their own user_id. Admins see all.
        """
        if event.event_type != "auth.user.roles.changed":
            return True
        # Admin (level 0 or below — negative levels are system) sees all.
        if self.user_level <= 0:
            return True
        return event.data.get("user_id") == self.user_id

    def can_see_workspace_event(self, event: Event) -> bool:
        """Content-level filter for workspace events.

        Workspace events carry ``visible_to`` to scope delivery to
        the conversation owner. Users only see workspace file events
        for their own conversations.
        """
        if not event.event_type.startswith("workspace."):
            return True
        visible_to = event.data.get("visible_to")
        if visible_to is not None and self.user_id not in visible_to:
            return False
        return True

    def can_see_notification_event(self, event: Event) -> bool:
        """Content-level filter for notification events.

        Notifications are 1:1 — addressed to a specific user via the
        ``user_id`` field on ``event.data``. Connections only see
        notification events for their own user.
        """
        if not event.event_type.startswith("notification."):
            return True
        return event.data.get("user_id") == self.user_id

    def can_see_health_event(self, event: Event) -> bool:
        """Content-level filter for health events.

        Health metrics + daily summaries: owner-only — connections only
        see their own user_id. Audit events: actor + target + admins.
        Link events: owner-only. Mirrors the inbox / notification
        pattern with the audit-row twist (target sees what touched
        their data, actor sees their own activity, admins see all).
        """
        if not event.event_type.startswith("health."):
            return True
        # Admin sees all (level 0 or below — system is negative).
        if self.user_level <= 0:
            return True

        if event.event_type == "health.access.audit":
            actor = event.data.get("actor_user_id")
            target = event.data.get("target_user_id")
            return actor == self.user_id or target == self.user_id

        # Everything else (metric.received, metric.deleted,
        # daily.summary, link.connected, link.disconnected) is
        # owner-only — the per-user filter narrows delivery to the
        # one connection whose user_id matches.
        return event.data.get("user_id") == self.user_id

    def can_see_feed_event(self, event: Event) -> bool:
        """Content-level filter for feed events that target a user.

        ``feed.briefing.ready`` carries a ``user_id`` (the recipient of
        the briefing) and is fanned out only to that user (admins see
        all). Per spec §12: "fanned out only to the recipient ``user_id``
        via a dedicated filter, not to every user — analogous to how
        notification events work."

        ``feed.ingest.throttled`` is also user-targeted (carries the
        owner's ``user_id``). Other ``feed.*`` events (item / subscription
        / shares) are feed-scoped, not user-scoped, and pass this filter
        — the SPA / consumer applies a per-feed ACL check on top by
        keeping a cache of accessible feed_ids.
        """
        if not event.event_type.startswith("feed."):
            return True
        # Admins (level 0 or below) see all feed events.
        if self.user_level <= 0:
            return True
        if event.event_type in ("feed.briefing.ready", "feed.ingest.throttled"):
            return event.data.get("user_id") == self.user_id
        return True

    def can_see_speaker_browser_event(self, event: Event) -> bool:
        """Content-level filter for browser-speaker playback frames.

        ``speaker.browser.*`` events are addressed to a specific user
        (their browser tab is the playback device) via the ``user_id``
        field on ``event.data``. Other users' connections never see
        them — playback in a private chat must stay private even when
        admins are subscribed broadly.
        """
        if not event.event_type.startswith("speaker.browser."):
            return True
        return event.data.get("user_id") == self.user_id

    def can_see_voice_agent_event(self, event: Event) -> bool:
        """Content-level filter for voice-agent events.

        ``voice_agent.transcript_turn`` (and any future ``voice_agent.*``
        server-initiated frame) carries a ``user_id`` for the user
        whose voice session it belongs to. Other users' connections
        don't see these — even admins broadly subscribed shouldn't
        see another user's conversation transcript live.
        """
        if not event.event_type.startswith("voice_agent."):
            return True
        return event.data.get("user_id") == self.user_id

    def can_see_chat_read_aloud_event(self, event: Event) -> bool:
        """Deliver chat.read_aloud.* events only to the matching user's
        own connections (so other tabs of that user stay in sync without
        leaking the preference to other users in a shared room)."""
        if not str(event.event_type).startswith("chat.read_aloud."):
            return True  # not our event type — let other filters decide
        target_user_id = (event.data or {}).get("user_id", "")
        return bool(target_user_id) and target_user_id == self.user_id

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

        # Filter by membership. ``chat.typing.*`` is gated the same way
        # as messages — a non-member of a shared room shouldn't see
        # someone typing in there, and personal-chat typing events
        # shouldn't leak to other users either. We also suppress the
        # typer's own typing events from their own connections (no need
        # to render your own "you are typing").
        if event.event_type.startswith(
            ("chat.message.", "chat.member.", "chat.typing.")
        ):
            if event.event_type.startswith("chat.typing.") and (
                event.data.get("user_id") == self.user_id
            ):
                return False
            if conv_id and conv_id not in self.shared_conv_ids:
                if not (
                    event.event_type == "chat.member.joined"
                    and event.data.get("user_id") == self.user_id
                ):
                    return False
            visible_to = event.data.get("visible_to")
            if visible_to is not None and self.user_id not in visible_to:
                return False

        # Live-streaming events (chat.stream.*) — e.g. ``chat.stream.text_delta``
        # for incremental assistant-text rendering. These don't go through
        # shared_conv_ids membership tracking because personal chats aren't
        # rooms; instead, the publisher sets ``visible_to`` to the explicit
        # list of user_ids that should see the event (conversation owner
        # for personal chats, all members for shared rooms). If no
        # ``visible_to`` is set, the event is treated as broadcast.
        if event.event_type.startswith("chat.stream."):
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
                b
                for b in ui_blocks
                if (not b.get("for_user") or b.get("for_user") == self.user_id)
                and b.get("exclude_user") != self.user_id
            ]
            if len(filtered) != len(ui_blocks):
                data = {**data, "ui_blocks": filtered}

        self.enqueue(
            {
                "type": "gilbert.event",
                "event_type": event.event_type,
                "data": data,
                "source": event.source,
                "timestamp": event.timestamp.isoformat() if event.timestamp else "",
            }
        )

    async def call_client(
        self,
        frame: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a server-initiated RPC to the browser and await its reply.

        The server stamps a unique ``id`` onto ``frame``, enqueues it, and
        waits for a frame from the client whose ``ref`` matches that id.
        ``dispatch_frame`` intercepts replies before handler lookup and
        resolves the pending future.

        Raises ``asyncio.TimeoutError`` if no reply arrives within
        ``timeout`` seconds, or ``ConnectionError`` if the connection is
        closed while waiting.
        """
        self._outbound_counter += 1
        outbound_id = f"s{self._outbound_counter}"
        stamped = {**frame, "id": outbound_id}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_outbound[outbound_id] = future
        self.enqueue(stamped)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_outbound.pop(outbound_id, None)

    def cancel_pending_outbound(self) -> None:
        """Cancel all pending outbound RPCs, e.g. on disconnect."""
        pending = list(self._pending_outbound.values())
        self._pending_outbound.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ConnectionError("WebSocket closed"))

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked once when the connection closes.

        Services use this to tear down per-connection state (e.g. an
        MCP session registry tied to a browser tab). Callbacks run
        synchronously from ``WsConnectionManager.unregister``; if the
        handler needs async work it should schedule a task itself
        (``asyncio.create_task(...)``) — ``unregister`` cannot await.
        Exceptions raised by a callback are logged and do not block
        the others from running.
        """
        self._close_callbacks.append(callback)

    def run_close_callbacks(self) -> None:
        """Invoke all registered close callbacks, swallowing errors."""
        for cb in self._close_callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.warning("WS close callback failed", exc_info=True)
        self._close_callbacks.clear()


class WsConnectionManager:
    """Manages all WebSocket connections and dispatches events.

    Service-provided handlers are discovered via the ``ws_handlers``
    capability (services implementing ``WsHandlerProvider``).  Core
    ``gilbert.*`` handlers are always registered from this module.

    Also tracks **session presence** — which user_ids currently have at
    least one WebSocket connection open. Distinct from ``PresenceService``
    (that one tracks physical occupancy via wifi/MAC observations); this
    one is the cheap "is someone using Gilbert right now" signal that
    powers the green online dot in shared rooms.

    The map is rebuilt incrementally by ``register()``/``unregister()`` —
    on the first connection for a user we emit ``chat.user.online`` to
    the bus, and on the last disconnect we emit ``chat.user.offline``.
    Multiple tabs / clients per user are fine — only edge transitions
    fire events.
    """

    def __init__(self) -> None:
        self._connections: set[WsConnection] = set()
        # user_id → set of active WsConnections. Map is the source of
        # truth for online-now; we recompute counts off it on every
        # register/unregister so a stale connection in ``_connections``
        # can't desync the online set.
        self._user_connections: dict[str, set[WsConnection]] = {}
        self._unsubscribe: Callable[[], None] | None = None
        self.gilbert: Any = None
        # Combined handler registry: core + service-provided
        self._handlers: dict[str, RpcHandler] = {}
        # frame_type → owning service name (for declared-role scoping)
        self._handler_owner: dict[str, str] = {}
        # declared prefix/frame → (role_name, owning service name)
        self._declared_rpc_roles: dict[str, tuple[str, str]] = {}

    def subscribe_to_bus(self, gilbert: Any) -> None:
        """Subscribe to the event bus and discover service handlers."""
        self.gilbert = gilbert

        # Start with core handlers (gilbert.*)
        self._handlers = dict(_rpc_handlers)

        # Discover service-provided handlers
        from gilbert.interfaces.ws import WsHandlerProvider, WsRpcRoleProvider

        for svc in gilbert.service_manager.get_all_by_capability("ws_handlers"):
            # Services exposing ws_handlers always inherit from
            # ``Service`` *and* implement the ``WsHandlerProvider``
            # protocol. Double-narrowing lets mypy see both sides
            # without making the protocol itself extend ``Service``
            # (which would create an import cycle across layers).
            if isinstance(svc, WsHandlerProvider) and isinstance(svc, Service):
                service_handlers = svc.get_ws_handlers()
                for frame_type, handler in service_handlers.items():
                    if frame_type in self._handlers:
                        logger.warning(
                            "WS handler conflict: %s already registered, skipping from %s",
                            frame_type,
                            svc.service_info().name,
                        )
                    else:
                        self._handlers[frame_type] = handler
                        self._handler_owner[frame_type] = svc.service_info().name
                logger.info(
                    "Registered %d WS handlers from %s",
                    len(service_handlers),
                    svc.service_info().name,
                )

                if isinstance(svc, WsRpcRoleProvider):
                    svc_name = svc.service_info().name
                    for key, role in svc.get_ws_rpc_roles().items():
                        owns_match = any(
                            ft == key or ft.startswith(key)
                            for ft, owner in self._handler_owner.items()
                            if owner == svc_name
                        )
                        if not owns_match:
                            logger.warning(
                                "Ignoring declared RPC role %r=%r from %s: matches none of its own handlers",
                                key,
                                role,
                                svc_name,
                            )
                            continue
                        if key in self._declared_rpc_roles:
                            logger.warning(
                                "Declared RPC role conflict on %r: keeping %s, skipping %s",
                                key,
                                self._declared_rpc_roles[key][1],
                                svc_name,
                            )
                            continue
                        self._declared_rpc_roles[key] = (role, svc_name)

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

    def resolve_declared_rpc_role(self, frame_type: str) -> str | None:
        """Longest-prefix declared role for a frame the declarer owns."""
        owner = self._handler_owner.get(frame_type)
        if owner is None:
            return None
        best = ""
        best_role: str | None = None
        for key, (role, svc_name) in self._declared_rpc_roles.items():
            if svc_name != owner:
                continue
            if (frame_type == key or frame_type.startswith(key)) and len(key) > len(best):
                best, best_role = key, role
        return best_role

    def register(self, conn: WsConnection) -> None:
        self._connections.add(conn)
        # Online-presence tracking. ``user_id`` is empty for unauth'd
        # peer connections; skip those — they don't represent a person.
        uid = conn.user_id
        if not uid:
            return
        bucket = self._user_connections.setdefault(uid, set())
        was_empty = not bucket
        bucket.add(conn)
        if was_empty:
            self._publish_online_event(conn, "chat.user.online")

    def unregister(self, conn: WsConnection) -> None:
        self._connections.discard(conn)
        conn.cancel_pending_outbound()
        conn.run_close_callbacks()
        uid = conn.user_id
        if not uid:
            return
        bucket = self._user_connections.get(uid)
        if bucket is None:
            return
        bucket.discard(conn)
        if not bucket:
            # Last connection for this user closed — they're offline now.
            del self._user_connections[uid]
            self._publish_online_event(conn, "chat.user.offline")

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def online_user_ids(self) -> set[str]:
        """Snapshot of user_ids that currently have ≥1 WS connection."""
        return set(self._user_connections.keys())

    def _publish_online_event(self, conn: WsConnection, event_type: str) -> None:
        """Emit a chat.user.online / chat.user.offline event to the bus.

        Best-effort: if there's no event bus wired up yet (early in
        startup, or in tests) this silently no-ops. The local
        ``_user_connections`` map is still accurate — clients that hit
        the snapshot RPC will see correct state.
        """
        if self.gilbert is None:
            return
        try:
            event_bus_svc = self.gilbert.service_manager.get_by_capability("event_bus")
        except Exception:  # noqa: BLE001 — service_manager unavailable
            return
        if event_bus_svc is None or not isinstance(event_bus_svc, EventBusProvider):
            return
        # Fire-and-forget on the running loop. ``EventBus.publish`` is
        # async; from a sync caller we schedule it and don't wait. If
        # we're not in a loop (unlikely from the WS lifecycle), give up.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        event = Event(
            event_type=event_type,
            source="ws_manager",
            data={
                "user_id": conn.user_id,
                "display_name": conn.display_name,
            },
        )
        loop.create_task(event_bus_svc.bus.publish(event))

    async def _dispatch_event(self, event: Event) -> None:
        """Dispatch a bus event to all eligible connections."""
        for conn in self._connections:
            if not conn.matches_subscription(event.event_type):
                continue
            if not conn.can_see_event(event.event_type, event.data):
                continue
            if not conn.can_see_chat_event(event):
                continue
            if not conn.can_see_auth_event(event):
                continue
            if not conn.can_see_workspace_event(event):
                continue
            if not conn.can_see_notification_event(event):
                continue
            if not conn.can_see_feed_event(event):
                continue
            if not conn.can_see_speaker_browser_event(event):
                continue
            if not conn.can_see_voice_agent_event(event):
                continue
            if not conn.can_see_chat_read_aloud_event(event):
                continue
            if not conn.can_see_health_event(event):
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


@rpc_handler("chat.presence.online_users")
async def _handle_online_users(
    conn: WsConnection, frame: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the set of user_ids that currently have ≥1 WS connection.

    The bus emits ``chat.user.online`` / ``chat.user.offline`` events
    for transitions, but a freshly-loaded UI needs the snapshot to render
    the green dot on members who were already online before its WS opened.
    """
    return {
        "type": "chat.presence.online_users.result",
        "ref": frame.get("id"),
        "user_ids": sorted(conn.manager.online_user_ids()),
    }


async def _publish_typing_event(
    conn: WsConnection, conversation_id: str, is_typing: bool
) -> None:
    """Fan out a chat.typing.* event so the conversation's other members
    can render / clear the "X is typing…" footer.

    Ephemeral — never persisted. Goes through the regular event bus so
    the same ``can_see_chat_event`` membership filter that gates message
    events also gates typing events (a non-member can't see who's typing
    in a room they can't read).
    """
    gilbert = conn.manager.gilbert
    if gilbert is None:
        return
    try:
        event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
    except Exception:  # noqa: BLE001
        return
    if event_bus_svc is None or not isinstance(event_bus_svc, EventBusProvider):
        return
    event = Event(
        event_type="chat.typing.start" if is_typing else "chat.typing.stop",
        source="ws_manager",
        data={
            "conversation_id": conversation_id,
            "user_id": conn.user_id,
            "display_name": conn.display_name,
        },
    )
    await event_bus_svc.bus.publish(event)


@rpc_handler("chat.typing.start")
async def _handle_typing_start(
    conn: WsConnection, frame: dict[str, Any]
) -> dict[str, Any] | None:
    """Broadcast that the caller is typing in a conversation.

    Frontend fires this on the first keystroke after a quiet period, then
    re-fires every ~3 seconds while the user keeps typing (server doesn't
    track timeouts — clients auto-clear stale indicators on their side).
    Stopping is signalled explicitly via ``chat.typing.stop`` on blur /
    send / silence.

    No-op for personal conversations — typing indicators are only
    interesting in shared rooms. We don't enforce that here; the event
    just fans out and the receiving frontends ignore irrelevant
    conversation_ids.
    """
    conversation_id = (frame.get("conversation_id") or "").strip()
    if not conversation_id:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "conversation_id is required",
            "code": 400,
        }
    await _publish_typing_event(conn, conversation_id, is_typing=True)
    # No response — the event is the side effect. Returning ``None``
    # tells ``dispatch_frame`` not to enqueue a reply.
    return None


@rpc_handler("chat.typing.stop")
async def _handle_typing_stop(
    conn: WsConnection, frame: dict[str, Any]
) -> dict[str, Any] | None:
    """Broadcast that the caller stopped typing.

    Counterpart to ``chat.typing.start``. Clients should also use a
    short timeout to auto-clear an indicator if they don't see a fresh
    ``start`` (in case the typer's connection drops silently).
    """
    conversation_id = (frame.get("conversation_id") or "").strip()
    if not conversation_id:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "conversation_id is required",
            "code": 400,
        }
    await _publish_typing_event(conn, conversation_id, is_typing=False)
    return None


@rpc_handler("gilbert.peer.publish")
async def _handle_peer_publish(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    if conn.user_level > _PEER_LEVEL:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Peer publishing requires peer or admin role",
            "code": 403,
        }

    event_type = frame.get("event_type", "")
    data = frame.get("data", {})
    source = f"peer:{frame.get('source', conn.user_id)}"

    if not event_type:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "event_type is required",
            "code": 400,
        }

    # Tag to prevent loops
    data = {**data, "_from_peer": True}

    # Publish to local bus
    gilbert = conn.manager.gilbert
    if gilbert is not None:
        event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
        if event_bus_svc is not None:
            if isinstance(event_bus_svc, EventBusProvider):
                await event_bus_svc.bus.publish(
                    Event(
                        event_type=event_type,
                        data=data,
                        source=source,
                    )
                )

    return {"type": "gilbert.peer.publish.result", "ref": frame.get("id"), "ok": True}


# ── Frame dispatch ────────────────────────────────────────────────────


async def dispatch_frame(conn: WsConnection, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Route an incoming frame to the appropriate handler.

    Checks permissions (hardcoded defaults + entity store overrides),
    then dispatches to the handler from the combined registry.
    """
    # Reply to a server-initiated RPC: resolve the pending future and stop.
    ref = frame.get("ref")
    if isinstance(ref, str) and ref in conn._pending_outbound:
        future = conn._pending_outbound.pop(ref)
        if not future.done():
            future.set_result(frame)
        return None

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

    # Propagate the current user through the async context so services
    # can read it via gilbert.interfaces.context.get_current_user() without
    # explicit threading on every read method.
    from gilbert.interfaces.context import set_current_user

    set_current_user(conn.user_ctx)
    return await handler(conn, frame)


def _resolve_rpc_level(conn: WsConnection, frame_type: str) -> int:
    """Resolve the required level for an RPC frame type.

    Precedence: admin runtime overrides, then roles the owning service
    declared for its own frames (WsRpcRoleProvider), then hardcoded
    defaults.
    """
    manager = conn.manager
    acl_svc: AccessControlProvider | None = None
    gilbert = manager.gilbert
    if gilbert is not None:
        svc = gilbert.service_manager.get_by_capability("access_control")
        if isinstance(svc, AccessControlProvider):
            acl_svc = svc

    if acl_svc is not None:
        override = acl_svc.get_rpc_override_level(frame_type)
        if override is not None:
            return override

    declared = manager.resolve_declared_rpc_role(frame_type)
    if declared is not None:
        if acl_svc is not None:
            return acl_svc.get_role_level(declared)
        return BUILTIN_ROLE_LEVELS.get(declared, DEFAULT_RPC_LEVEL)

    if acl_svc is not None:
        return acl_svc.resolve_rpc_level(frame_type)
    return get_rpc_permission_level(frame_type)
