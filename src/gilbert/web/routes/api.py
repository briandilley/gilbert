"""JSON API routes — minimal HTTP endpoints that must remain.

Most operations have moved to WebSocket RPC (``ws_protocol.py`` +
service-owned handlers). Only pre-auth endpoints, callbacks from
external services, and protocol endpoints that can't speak WebSocket
stay here.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import LoginMethodProvider
from gilbert.interfaces.mcp import MCPOAuthCallbackHandler

router = APIRouter(prefix="/api", tags=["api"])


def _gilbert(request: Request) -> Gilbert:
    result: Gilbert = request.app.state.gilbert
    return result


@router.get("/auth/methods")
async def auth_methods(request: Request) -> list[dict[str, Any]]:
    """Return available login methods as JSON (pre-auth, no session needed)."""
    gilbert = _gilbert(request)
    auth_svc = gilbert.service_manager.get_by_capability("authentication")
    if not isinstance(auth_svc, LoginMethodProvider):
        return []
    methods = auth_svc.get_login_methods()
    return [
        {
            "provider_type": m.provider_type,
            "display_name": m.display_name,
            "method": m.method,
            "redirect_url": m.redirect_url,
            "form_action": m.form_action,
        }
        for m in methods
    ]


@router.get("/mcp/oauth/callback", response_class=HTMLResponse)
async def mcp_oauth_callback(request: Request) -> HTMLResponse:
    """OAuth 2.1 redirect target for MCP server sign-in flows.

    The user's browser hits this route after completing authentication
    at an external MCP server's auth portal. We pull the ``code`` and
    ``state`` out of the query string, hand them to ``MCPService``'s
    flow manager (which resolves the blocked connect task waiting on
    its ``callback_handler``), and render a tiny success page telling
    the user they can close the tab and return to Gilbert. Errors from
    the auth portal (``error=access_denied`` etc.) are surfaced too so
    the user understands why the tab won't auto-close.
    """
    gilbert = _gilbert(request)
    mcp_svc = gilbert.service_manager.get_by_capability("mcp")

    params = dict(request.query_params)
    error = params.get("error")
    if error:
        description = params.get("error_description") or ""
        return HTMLResponse(
            _callback_page(
                title="Sign-in failed",
                message=f"The MCP server rejected the sign-in: {error} {description}",
                ok=False,
            ),
            status_code=400,
        )

    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return HTMLResponse(
            _callback_page(
                title="Malformed callback",
                message="Missing ``code`` or ``state`` in the OAuth redirect.",
                ok=False,
            ),
            status_code=400,
        )

    if not isinstance(mcp_svc, MCPOAuthCallbackHandler):
        return HTMLResponse(
            _callback_page(
                title="MCP not running",
                message="Gilbert's MCP client isn't currently enabled.",
                ok=False,
            ),
            status_code=503,
        )

    resolved = await mcp_svc.complete_oauth_callback(state, code, state)
    if not resolved:
        return HTMLResponse(
            _callback_page(
                title="No flow in progress",
                message=(
                    "This sign-in link has already been used or expired. "
                    "Start a new sign-in from Gilbert and try again."
                ),
                ok=False,
            ),
            status_code=404,
        )
    return HTMLResponse(
        _callback_page(
            title="Sign-in complete",
            message="You can close this tab and return to Gilbert.",
            ok=True,
        ),
    )


def _callback_page(*, title: str, message: str, ok: bool) -> str:
    """Tiny inline HTML response — no external assets, no JS beyond the
    auto-close attempt for the success case."""
    color = "#10b981" if ok else "#ef4444"
    auto_close = "<script>setTimeout(() => window.close(), 1500);</script>" if ok else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      max-width: 32rem;
      margin: 4rem auto;
      padding: 0 1rem;
      color: #1f2937;
      text-align: center;
    }}
    h1 {{ color: {color}; }}
    p {{ color: #4b5563; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p>{message}</p>
  {auto_close}
</body>
</html>"""
