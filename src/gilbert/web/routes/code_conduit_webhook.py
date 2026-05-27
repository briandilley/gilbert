"""Code-conduit inbound webhook.

Push-style coding-agent integrations (Claude Code's ``Stop`` /
``Notification`` hooks, GitHub Action callbacks, custom CI
scripts — anything that can POST JSON when something happens)
deliver events to Gilbert here. The route normalizes the payload
into a ``CodingAgentEvent`` and feeds it through the
``CodingConduitInboundEndpoint`` capability on the code-conduit
service — same fan-out path (ring buffer + bus publish + user
notification) the pull-style OpenCode SSE consumer uses.

Auth: shared secret in the ``X-Code-Conduit-Secret`` header,
validated by the service via ``hmac.compare_digest``. No secret
configured = endpoint returns 503; wrong secret = 401. Both are
quiet (no per-attempt log noise) so brute-force probing doesn't
flood journalctl, but the WARNING on auth failure does surface
the request id so an operator debugging a misconfigured stop
hook can see why their POST got rejected.

Mounted at ``/api/code-conduit/`` and added to the auth-exempt
prefix list — the calling hook can't carry a Gilbert session
cookie. The shared secret is the trust anchor instead.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from gilbert.interfaces.coding_agent import (
    EVENT_KIND_DONE,
    EVENT_KIND_ERROR,
    EVENT_KIND_INFO,
    CodingAgentEvent,
    CodingConduitInboundEndpoint,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/code-conduit")


# Kinds we accept from external callers. Anything else gets
# coerced to ``info`` rather than rejected — better to surface a
# weird event in the activity feed than silently drop it.
_ACCEPTED_KINDS = {
    EVENT_KIND_DONE,
    EVENT_KIND_ERROR,
    EVENT_KIND_INFO,
    "attention",  # EVENT_KIND_ATTENTION
}


def _get_endpoint(state: Any) -> Any:
    """Resolve the code-conduit service from the FastAPI app's
    Gilbert handle. Returns ``None`` when the plugin isn't
    loaded so the route can 503 cleanly instead of 500-ing on
    AttributeError."""
    gilbert = getattr(state, "gilbert", None)
    if gilbert is None:
        return None
    svc = gilbert.service_manager.get_capability("code_conduit")
    if svc is None or not isinstance(svc, CodingConduitInboundEndpoint):
        return None
    return svc


@router.post("/inbound")
async def code_conduit_inbound(
    request: Request,
    x_code_conduit_secret: str = Header(default=""),
) -> JSONResponse:
    """Accept a single coding-agent event.

    Payload (JSON):
        kind:         "done" | "error" | "attention" | "info" (default "info")
        summary:      short, voice-friendly one-liner (default "")
        detail:       longer prose for the SPA feed (default "")
        session_id:   backend's session id (default "")
        project_path: working directory (default "")
        timestamp:    ISO-8601 string (default "")
        raw_type:     caller's native event-type name; defaults to "webhook"

    Caller is anonymous to the framework; the shared secret in
    ``X-Code-Conduit-Secret`` is the only credential.

    Response codes:
        200 — accepted + ingested.
        400 — body wasn't a JSON object.
        401 — secret missing or didn't match.
        503 — plugin not loaded OR no secret configured (endpoint
              not provisioned).
    """
    endpoint = _get_endpoint(request.app.state)
    if endpoint is None:
        return JSONResponse(
            {"error": "code-conduit plugin not loaded"},
            status_code=503,
        )

    # ``webhook_enabled`` returns False when the operator hasn't
    # set ``webhook_secret`` in plugin settings — the endpoint is
    # off until they do. 503 is the right code here ("service
    # exists but isn't provisioned"), not 401 (which would imply
    # auth could in principle succeed).
    if not getattr(endpoint, "webhook_enabled", False):
        return JSONResponse(
            {"error": "code-conduit webhook secret not configured"},
            status_code=503,
        )

    if not endpoint.verify_webhook_secret(x_code_conduit_secret):
        # Don't echo the presented secret; do log enough that an
        # operator debugging a misconfigured hook can correlate
        # against their hook script's output.
        logger.warning(
            "code-conduit webhook rejected — bad or missing "
            "X-Code-Conduit-Secret header (client=%s)",
            request.client.host if request.client else "?",
        )
        return JSONResponse(
            {"error": "invalid secret"},
            status_code=401,
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "body must be JSON"},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "body must be a JSON object"},
            status_code=400,
        )

    event = _payload_to_event(payload)
    try:
        await endpoint.deliver_inbound_event(event=event)
    except Exception:
        # Defensive — the service's deliver_inbound_event should
        # swallow its own errors. If we somehow get here, 500 the
        # request so the caller knows to retry rather than 200ing
        # a silent drop.
        logger.exception(
            "code-conduit webhook: deliver_inbound_event raised"
        )
        return JSONResponse(
            {"error": "ingest failed"},
            status_code=500,
        )

    return JSONResponse(
        {
            "status": "ok",
            "kind": event.kind,
            "raw_type": event.raw_type,
        }
    )


def _payload_to_event(payload: dict[str, Any]) -> CodingAgentEvent:
    """Coerce a webhook JSON payload into a ``CodingAgentEvent``.
    Unknown ``kind`` values fall back to ``info`` rather than
    erroring — preserves whatever the caller sent in ``raw_type``
    so the SPA feed can still render the original type name."""
    kind = str(payload.get("kind", EVENT_KIND_INFO) or EVENT_KIND_INFO).strip().lower()
    if kind not in _ACCEPTED_KINDS:
        kind = EVENT_KIND_INFO
    return CodingAgentEvent(
        kind=kind,
        summary=str(payload.get("summary", "") or "")[:500],
        detail=str(payload.get("detail", "") or "")[:4000],
        session_id=str(payload.get("session_id", "") or ""),
        project_path=str(payload.get("project_path", "") or ""),
        timestamp=str(payload.get("timestamp", "") or ""),
        raw_type=str(payload.get("raw_type", "webhook") or "webhook"),
    )
