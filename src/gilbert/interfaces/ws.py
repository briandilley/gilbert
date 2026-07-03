"""WebSocket handler provider interface.

Services implement this protocol to register WS RPC frame handlers,
similar to how ``ToolProvider`` exposes AI tools. Declare the
``ws_handlers`` capability in ``ServiceInfo`` to be discovered.

Type aliases (``RpcHandler``, ``WsConnectionBase``) are defined here so
that both ``core/services/`` and ``web/`` can reference them without
creating import cycles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext

# ── Type aliases ──────────────────────────────────────────────────────

RpcHandler = Callable[[Any, dict[str, Any]], Coroutine[Any, Any, dict[str, Any] | None]]
"""Signature for a WS RPC frame handler.

The connection parameter is typed as ``Any`` so handlers can declare
their own narrower type (``WsConnection`` in ``web/ws_protocol.py``
or the ``WsConnectionBase`` protocol). Python's type system doesn't
let a function with a narrower parameter stand in for one with a
wider parameter (contravariance), so using ``Any`` here is the only
way to let both coexist in a single handler registry. Handlers
still get static checking inside their own bodies via their
parameter annotation."""


@runtime_checkable
class WsConnectionBase(Protocol):
    """Minimal protocol for a WebSocket connection.

    Defines the attributes and methods that core services may rely on.
    The concrete ``WsConnection`` in ``web/ws_protocol.py`` satisfies
    this protocol.
    """

    user_ctx: UserContext
    user_level: int
    shared_conv_ids: set[str]
    queue: asyncio.Queue[dict[str, Any]]
    manager: Any
    """The WebSocket connection manager instance. Typed as ``Any``
    to keep the interfaces layer free of ``web/`` imports — the
    concrete type lives in ``web/ws_protocol.py`` and carries a
    back-reference to the owning ``Gilbert`` app instance. Handlers
    that need the app reach it as ``conn.manager.gilbert``."""

    @property
    def user_id(self) -> str: ...

    def enqueue(self, msg: dict[str, Any]) -> None: ...

    async def call_client(
        self,
        frame: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a server-initiated RPC to the browser and await its reply.

        The implementation stamps a unique id onto the frame, enqueues
        it, and waits for a frame whose ``ref`` matches. Raises
        ``asyncio.TimeoutError`` on timeout or ``ConnectionError`` if
        the connection closes while waiting.
        """
        ...

    def cancel_pending_outbound(self) -> None:
        """Cancel all pending server-initiated RPCs on this connection."""
        ...

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Register a sync callback invoked when the connection closes.

        Services use this to tear down per-connection state (e.g.
        ephemeral registries tied to a browser tab). Callbacks must be
        synchronous; if they need async work, schedule a task.
        """
        ...


# ── Protocols ─────────────────────────────────────────────────────────


@runtime_checkable
class WsHandlerProvider(Protocol):
    """Protocol for services that expose WebSocket RPC handlers."""

    def get_ws_handlers(self) -> dict[str, RpcHandler]:
        """Return a mapping of frame type → async handler function.

        Each handler receives ``(conn: WsConnectionBase, frame: dict)`` and
        returns an optional response dict (or None for no response).

        Frame types use ``namespace.resource.verb`` naming, e.g.
        ``chat.message.send``, ``roles.role.create``.
        """
        ...


@runtime_checkable
class WsRpcRoleProvider(Protocol):
    """Optional companion to ``WsHandlerProvider``.

    A service may declare the minimum role required to call its OWN WS
    frames — e.g. a game plugin whose players are unauthenticated guests
    declares ``{"mafia.": "everyone"}``. Keys are exact frame types or
    dot-terminated prefixes; values are role names resolved through the
    access-control service. A declared entry is honored only for frame
    types whose handler the same service registered — it can never
    change the level of another service's frames. Admin runtime
    overrides still take precedence; hardcoded defaults remain the
    fallback.
    """

    def get_ws_rpc_roles(self) -> dict[str, str]:
        """Map frame types / prefixes to minimum role names."""
        ...


def require_admin(conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error frame if the connection is not admin-level, else None.

    Shared helper for WS handlers that require admin access.
    """
    if conn.user_level > 0:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Admin access required",
            "code": 403,
        }
    return None
