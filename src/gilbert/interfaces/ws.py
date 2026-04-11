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
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext


# ── Type aliases ──────────────────────────────────────────────────────

RpcHandler = Callable[["WsConnectionBase", dict[str, Any]], Coroutine[Any, Any, dict[str, Any] | None]]
"""Signature for a WS RPC frame handler."""


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

    @property
    def user_id(self) -> str: ...

    def enqueue(self, msg: dict[str, Any]) -> None: ...


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


def require_admin(conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error frame if the connection is not admin-level, else None.

    Shared helper for WS handlers that require admin access.
    """
    if conn.user_level > 0:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Admin access required", "code": 403}
    return None
