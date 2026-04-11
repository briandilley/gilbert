"""JSON API routes — minimal HTTP endpoints that must remain.

Most operations have moved to WebSocket RPC (``ws_protocol.py`` +
service-owned handlers). Only pre-auth endpoints stay here.
"""

from typing import Any

from fastapi import APIRouter, Request

from gilbert.core.app import Gilbert

router = APIRouter(prefix="/api", tags=["api"])


def _gilbert(request: Request) -> Gilbert:
    result: Gilbert = request.app.state.gilbert
    return result


@router.get("/auth/methods")
async def auth_methods(request: Request) -> list[dict[str, Any]]:
    """Return available login methods as JSON (pre-auth, no session needed)."""
    gilbert = _gilbert(request)
    auth_svc = gilbert.service_manager.get_by_capability("authentication")
    if auth_svc is None:
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
