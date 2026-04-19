"""MCP server HTTP endpoint — raw ASGI app mounted at ``/api/mcp``.

The endpoint has to bypass FastAPI's normal request/response cycle
because the SDK's ``StreamableHTTPSessionManager`` owns the ASGI
protocol directly (streaming responses, session state machine).
Returning a ``Response`` from a FastAPI handler after the session
manager has written to ``send`` causes a double-response error, so
we export a raw ASGI callable and mount it in ``web/__init__.py``
with ``app.add_route("/api/mcp", ..., include_in_schema=False)``.

The path is ``/api/mcp`` (not ``/mcp``) so it doesn't collide with
the SPA's ``/mcp/*`` admin pages — a browser refresh on any SPA
MCP page falls through to the SPA fallback cleanly.

Authenticates each request via ``MCPServerService.authenticate``
(bearer token → client + user), binds the resolved client and
``UserContext`` to async-local state so the SDK handlers can see
them, and delegates to the streamable HTTP session manager.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request

from gilbert.core.app import Gilbert
from gilbert.interfaces.mcp import MCPServerEndpoint
from gilbert.interfaces.service import Service, ServiceResolver

logger = logging.getLogger(__name__)


class _ResolverShim(ServiceResolver):
    """Adapts ``ServiceManager.get_by_capability`` → ``ServiceResolver.get_capability``.

    The MCP HTTP app's constructor wants a ``ServiceResolver`` so it
    can call ``get_capability("ai")`` etc. during request handling.
    The service manager provides the same lookup under a slightly
    different name; this shim is the thinnest possible adapter."""

    def __init__(self, gilbert: Gilbert) -> None:
        self._gilbert = gilbert

    def get_capability(self, capability: str) -> Service | None:
        svc = self._gilbert.service_manager.get_by_capability(capability)
        return svc if isinstance(svc, Service) else None

    def require_capability(self, capability: str) -> Service:
        svc = self.get_capability(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Service]:
        # The MCP HTTP app uses ``ai.discover_tools`` which internally
        # calls the real resolver's ``get_all``, so this shim's
        # ``get_all`` is never called directly. Return an empty list
        # as a safe default if a future change ever does call it.
        return []


class _McpAsgiEndpoint:
    """Callable ASGI app for ``/mcp``.

    Exists as a class rather than a bare coroutine so starlette's
    ``Route`` constructor treats it as a pure ASGI app (via
    ``inspect.isfunction`` check) rather than wrapping it in the
    request/response helper. Without this, starlette would assume
    the handler takes a ``Request`` argument and call it as
    ``handler(request)``, which fails because our handler needs the
    raw ``scope``/``receive``/``send`` triple to hand off to the
    SDK's streamable HTTP session manager.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return

        method = scope.get("method", "").upper()
        if method not in ("GET", "POST", "DELETE", "OPTIONS"):
            await _send_json(send, 405, {"error": "method not allowed"})
            return

        app_state = scope["app"].state
        gilbert: Gilbert | None = getattr(app_state, "gilbert", None)
        if gilbert is None:
            await _send_json(send, 503, {"error": "gilbert not ready"})
            return

        mcp_server_svc = gilbert.service_manager.get_by_capability("mcp_server")
        if not isinstance(mcp_server_svc, MCPServerEndpoint):
            await _send_json(send, 503, {"error": "MCP server is not enabled"})
            return
        if not mcp_server_svc.enabled:
            await _send_json(send, 503, {"error": "MCP server is not enabled"})
            return

        from gilbert.core.context import _current_user
        from gilbert.core.services.mcp_server_http import (
            authenticate_mcp_request,
            reset_current_client,
            set_current_client,
        )

        # Wrap the raw scope in a Request just so we can reuse the
        # authenticate helper's header-parsing logic without
        # duplicating the bearer-extraction code.
        request = Request(scope, receive=receive)
        auth_result = await authenticate_mcp_request(mcp_server_svc, request)
        if auth_result is None:
            await _send_json(
                send,
                401,
                {"error": "unauthorized"},
                headers=[
                    (b"www-authenticate", b'Bearer realm="gilbert-mcp"'),
                ],
            )
            return
        client, user_ctx = auth_result

        http_app = await _get_http_app(app_state, gilbert)
        client_token = set_current_client(client)
        user_token = _current_user.set(user_ctx)
        try:
            await http_app.handle_request(scope, receive, send)
        except Exception:  # noqa: BLE001
            logger.exception("MCP request failed for client %s", client.id)
        finally:
            reset_current_client(client_token)
            try:
                _current_user.reset(user_token)
            except (ValueError, LookupError):
                pass


mcp_asgi_endpoint = _McpAsgiEndpoint()
"""Module-level ASGI app instance. Callers register it via
``starlette.routing.Route(...).endpoint=mcp_asgi_endpoint``."""


async def _get_http_app(app_state: Any, gilbert: Gilbert) -> Any:
    """Lazy-build the ``MCPServerHttpApp`` and cache it on ``app.state``
    so the SDK ``Server`` construction cost is paid once per process
    but the instance's lifecycle is tied to the FastAPI app rather
    than a module-level singleton (which GC would clean up from the
    wrong task on shutdown)."""
    existing = getattr(app_state, "mcp_http_app", None)
    if existing is not None:
        return existing
    from gilbert.core.services.mcp_server_http import MCPServerHttpApp

    http_app = MCPServerHttpApp(_ResolverShim(gilbert))
    app_state.mcp_http_app = http_app
    return http_app


async def _send_json(
    send: Any,
    status: int,
    body: dict[str, Any],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Minimal ASGI response writer for the pre-delegation error
    paths. The session manager handles its own responses after we
    hand off."""
    import json

    payload = json.dumps(body).encode("utf-8")
    response_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    if headers:
        response_headers.extend(headers)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": response_headers,
        },
    )
    await send({"type": "http.response.body", "body": payload})
