"""MCP server HTTP endpoint — Gilbert's tools exposed over MCP.

Builds a ``mcp.server.Server`` wired to Gilbert's existing tool
pipeline and fronts it with ``StreamableHTTPSessionManager``, which
is ASGI-compatible. The actual mount happens in
``web/routes/api.py`` so this module stays a self-contained bridge
between Gilbert's services and the MCP SDK.

The per-request auth / session flow:

1. Incoming HTTP request hits the ``mount_mcp_server`` handler.
2. We pull the ``Authorization: Bearer <token>`` header and hand it
   to ``MCPServerService.authenticate`` — that resolves the token
   to an ``MCPServerClient`` + ``UserContext`` (the Gilbert user
   whose identity this client is acting under).
3. We stash the context on the ASGI scope and run the MCP session
   handler under ``set_current_user`` so tool execution sees the
   right principal.
4. The session manager speaks MCP to the client. When the client
   calls ``tools/list`` or ``tools/call``, the handlers below
   dispatch through Gilbert's tool registry using the client's
   configured AI profile to filter the visible set.

Per-call audit logs go to the ``gilbert.mcp_server.audit`` logger
with one structured line per tool invocation (client id, user,
tool name, outcome, duration).
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextvars import ContextVar, Token
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request

from gilbert.core.context import _current_user
from gilbert.core.services.mcp_server import MCPServerClient
from gilbert.interfaces.ai import AIToolDiscoveryProvider
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.mcp import MCPServerEndpoint
from gilbert.interfaces.service import ServiceResolver

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("gilbert.mcp_server.audit")


# Per-request state stashed in a ContextVar so the SDK-facing handlers
# (``list_tools``, ``call_tool``) can see which client is talking without
# needing the MCP request context to carry arbitrary metadata.
_current_client: ContextVar[MCPServerClient | None] = ContextVar(
    "_mcp_server_current_client",
    default=None,
)


class MCPServerHttpApp:
    """Assembles the MCP ``Server`` + ASGI session manager.

    Constructed lazily the first time a request arrives at the HTTP
    mount so service resolution has settled. Reusable across
    requests — the ``handle_request`` method is an ASGI callable
    that routes each incoming connection through the shared session
    manager."""

    def __init__(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        self._server: Server[Any, Any] = self._build_server()
        self._manager = StreamableHTTPSessionManager(
            app=self._server,
            stateless=True,
        )
        self._manager_cm: Any = None
        self._started: bool = False

    async def start(self) -> None:
        """Enter the session manager's async context. Called once on
        first request."""
        if self._started:
            return
        self._manager_cm = self._manager.run()
        await self._manager_cm.__aenter__()
        self._started = True

    async def stop(self) -> None:
        if self._manager_cm is not None:
            with contextlib.suppress(Exception):
                await self._manager_cm.__aexit__(None, None, None)
            self._manager_cm = None
            self._started = False

    async def handle_request(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI entry point. Ensures the session manager is running
        and delegates to its handler."""
        if not self._started:
            await self.start()
        await self._manager.handle_request(scope, receive, send)

    # --- MCP server wiring --------------------------------------------------

    def _build_server(self) -> Server[Any, Any]:
        """Construct the underlying SDK ``Server`` and register
        handlers that dispatch through Gilbert's tool pipeline."""
        server: Server[Any, Any] = Server("gilbert")

        @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
        async def _list_tools() -> list[types.Tool]:
            client = _current_client.get()
            if client is None:
                return []
            ai_svc = self._resolver.get_capability("ai")
            if not isinstance(ai_svc, AIToolDiscoveryProvider):
                return []
            user_ctx = _user_ctx_for(client)
            try:
                discovered = ai_svc.discover_tools(
                    user_ctx=user_ctx,
                    profile_name=client.ai_profile,
                )
            except Exception:
                logger.exception(
                    "MCP server list_tools failed for client %s",
                    client.id,
                )
                return []
            tools: list[types.Tool] = []
            for _, tool_def in discovered.values():
                tools.append(
                    types.Tool(
                        name=tool_def.name,
                        description=tool_def.description,
                        inputSchema=tool_def.to_json_schema(),
                    ),
                )
            return tools

        @server.call_tool()  # type: ignore[untyped-decorator]
        async def _call_tool(
            name: str,
            arguments: dict[str, Any],
        ) -> list[types.TextContent]:
            client = _current_client.get()
            if client is None:
                return [_error_text("no client context")]
            ai_svc = self._resolver.get_capability("ai")
            if not isinstance(ai_svc, AIToolDiscoveryProvider):
                return [_error_text("AI service unavailable")]
            user_ctx = _user_ctx_for(client)
            try:
                discovered = ai_svc.discover_tools(
                    user_ctx=user_ctx,
                    profile_name=client.ai_profile,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "MCP server tool discovery failed for client %s",
                    client.id,
                )
                return [_error_text(f"tool discovery failed: {exc}")]

            tuple_entry = discovered.get(name)
            if tuple_entry is None:
                audit_logger.info(
                    "tool_rejected",
                    extra={
                        "mcp_client_id": client.id,
                        "user_id": user_ctx.user_id,
                        "tool": name,
                        "reason": "not_in_profile_or_rbac",
                    },
                )
                return [_error_text(f"Tool {name!r} not available to this client")]
            provider, _tool_def = tuple_entry

            # Run the tool under the owner's UserContext so the
            # tool's own ``get_current_user``-based gating sees the
            # right principal (e.g. the MCP client tool dispatches
            # hit the same ``mcp__<slug>__<tool>`` namespace filter
            # that chat-originated calls do). Call ``_current_user.set``
            # directly rather than the wrapper because we need the
            # token for reset.
            token = _current_user.set(user_ctx)
            start = time.monotonic()
            try:
                result = await provider.execute_tool(name, arguments)
                audit_logger.info(
                    "tool_ok",
                    extra={
                        "mcp_client_id": client.id,
                        "user_id": user_ctx.user_id,
                        "tool": name,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                )
                return [types.TextContent(type="text", text=str(result))]
            except PermissionError as exc:
                audit_logger.info(
                    "tool_denied",
                    extra={
                        "mcp_client_id": client.id,
                        "user_id": user_ctx.user_id,
                        "tool": name,
                        "error": str(exc),
                    },
                )
                return [_error_text(f"Permission denied: {exc}")]
            except Exception as exc:  # noqa: BLE001
                audit_logger.info(
                    "tool_error",
                    extra={
                        "mcp_client_id": client.id,
                        "user_id": user_ctx.user_id,
                        "tool": name,
                        "error": str(exc),
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                )
                logger.warning(
                    "MCP tool %s failed for client %s: %s",
                    name,
                    client.id,
                    exc,
                )
                return [_error_text(f"Tool execution failed: {exc}")]
            finally:
                # Reset the ContextVar so unrelated concurrent
                # requests don't see this user.
                _current_user_reset(token)

        return server


# ── Module-level helpers ───────────────────────────────────────────────


def _user_ctx_for(client: MCPServerClient) -> UserContext:
    """Build a minimal ``UserContext`` for tool dispatch. The real
    context (with email, display name, etc.) was created in
    ``MCPServerService.authenticate`` and stashed on the ASGI scope
    before the MCP session started, but this helper exists for the
    case where the SDK invokes list_tools/call_tool outside the
    per-request ContextVar scope (unlikely under streamable HTTP but
    defensive)."""
    return UserContext(
        user_id=client.owner_user_id,
        email="",
        display_name=client.name,
        roles=frozenset(),
        provider="mcp_server",
        session_id=f"mcp_client:{client.id}",
        metadata={"mcp_client_id": client.id},
    )


def _error_text(message: str) -> types.TextContent:
    return types.TextContent(type="text", text=f"[error] {message}")


def _current_user_reset(token: Token[UserContext]) -> None:
    """Safely reset the user contextvar after a tool call.

    Token reset can raise if the ContextVar was bound on a different
    task (when the SDK's anyio task group switches tasks mid-call).
    We absorb that since the value we set was request-scoped and
    the parent task will tear it down on exit anyway."""
    try:
        _current_user.reset(token)
    except (ValueError, LookupError):
        pass


async def authenticate_mcp_request(
    mcp_server_svc: MCPServerEndpoint,
    request: Request,
) -> tuple[MCPServerClient, UserContext] | None:
    """Pull the bearer out of the request and authenticate.

    Used by the FastAPI route before handing the scope off to the
    streamable session manager. Returns ``None`` on any failure so
    the route can respond with 401 without leaking which part of
    the token shape was wrong."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("Bearer ") :].strip()
    if not token:
        return None
    client_ip = request.client.host if request.client else ""
    return await mcp_server_svc.authenticate(token, client_ip=client_ip)


def set_current_client(client: MCPServerClient | None) -> Token[MCPServerClient | None]:
    """Bind the authenticated client to the current async context.

    Used by the HTTP route so ``list_tools`` / ``call_tool`` handlers
    (which run inside the session manager's task) can read back the
    client without passing it through every SDK callback."""
    return _current_client.set(client)


def reset_current_client(token: Token[MCPServerClient | None]) -> None:
    try:
        _current_client.reset(token)
    except (ValueError, LookupError):
        pass
