"""HTTP routes Mentra Cloud and the in-glasses-app companion talk to.

Three routes:

- ``POST /api/mentra/webhook`` — Mentra Cloud POSTs lifecycle events
  here (``session_request``, ``stop_request``). Dispatched to the
  ``mentra_webhook`` capability.
- ``GET /api/mentra/webview`` — HTML page Mentra opens in a webview
  on the user's phone when they tap the app's tile. Serves a
  live debug view of the user's session state + recent events.
- ``GET /api/mentra/debug/events`` — JSON event log the webview polls
  for live updates. Reads from the ``mentra_debug`` capability.

All carrier-specific parsing (the websocket URL aliases, the
StopRequestReason enum, etc.) lives in the plugin so these routes
stay plugin-agnostic.

Important: Mentra retries non-2xx webhook responses, so the webhook
endpoint returns 200 unconditionally with a status field.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from gilbert.interfaces.mentra import (
    MentraDebugProvider,
    MentraWebhookEndpoint,
    WebhookResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mentra")


def _get_endpoint(state: Any) -> MentraWebhookEndpoint | None:
    """Resolve the ``mentra_webhook`` capability off the live Gilbert
    app. Returns ``None`` if the Mentra plugin isn't registered.
    """
    gilbert = getattr(state, "gilbert", None)
    if gilbert is None:
        return None
    svc = gilbert.service_manager.get_capability("mentra_webhook")
    if svc is None or not isinstance(svc, MentraWebhookEndpoint):
        return None
    return svc


@router.post("/photo-upload")
async def mentra_photo_upload(request: Request) -> JSONResponse:
    """Receive a photo upload from Mentra Cloud.

    Mentra Live's photo-capture flow is push-based: the cloud POSTs
    multipart form-data here (URL pattern documented in the upstream
    SDK's ``MiniAppServer.setupPhotoUploadEndpoint`` at
    ``${publicUrl}/photo-upload``; our routes are prefixed
    ``/api/mentra``). Fields: ``requestId`` (matches our pending
    ``photo_request``), ``type`` (``photo_error`` on failure),
    ``errorCode`` / ``errorMessage`` (on error), and ``photo``
    (the actual file).

    Returns the upstream contract shape: 200 with
    ``{success, requestId, message}`` on match, 404 with
    ``{success: false, error}`` when no pending request is found
    (request timed out, session ended, etc.). The plugin treats
    both as "stop retrying" so Mentra Cloud doesn't queue.
    """
    endpoint = _get_endpoint(request.app.state)
    if endpoint is None:
        logger.warning(
            "Mentra photo-upload arrived but plugin isn't loaded"
        )
        return JSONResponse(
            {"success": False, "error": "mentra plugin not loaded"},
            status_code=503,
        )

    try:
        form = await request.form()
    except Exception:
        logger.warning(
            "Mentra photo-upload with unparseable multipart body"
        )
        return JSONResponse(
            {"success": False, "error": "unparseable multipart body"},
            status_code=400,
        )

    request_id = str(form.get("requestId") or "")
    if not request_id:
        logger.warning("Mentra photo-upload missing requestId field")
        return JSONResponse(
            {"success": False, "error": "missing requestId"},
            status_code=400,
        )

    type_field = str(form.get("type") or "")
    error_code = str(form.get("errorCode") or "")
    error_message = str(form.get("errorMessage") or "")
    success_field = str(form.get("success") or "")
    is_explicit_error = (
        type_field == "photo_error" or success_field == "false"
    )

    photo_bytes = b""
    mime_type = ""
    photo_file = form.get("photo")
    # Starlette returns UploadFile for file fields. The presence of
    # ``read`` + ``filename`` is the canonical duck-type check.
    if hasattr(photo_file, "read"):
        try:
            photo_bytes = await photo_file.read()  # type: ignore[union-attr]
        except Exception:
            logger.exception(
                "Mentra photo-upload failed to read file field "
                "request_id=%s",
                request_id,
            )
            return JSONResponse(
                {
                    "success": False,
                    "error": "failed to read photo file",
                    "requestId": request_id,
                },
                status_code=500,
            )
        mime_type = getattr(photo_file, "content_type", "") or ""

    has_photo = bool(photo_bytes)
    logger.info(
        "Mentra photo-upload: request_id=%s type=%r has_photo=%s "
        "bytes=%d mime=%s explicit_error=%s",
        request_id,
        type_field,
        has_photo,
        len(photo_bytes),
        mime_type or "<none>",
        is_explicit_error,
    )

    # Upstream convention: error iff explicit_error AND no photo
    # file present. Don't reject the success path just because the
    # ``success`` field is missing — some clients omit it.
    is_error_response = is_explicit_error and not has_photo

    try:
        result = await endpoint.deliver_photo_upload(
            request_id=request_id,
            photo_bytes=b"" if is_error_response else photo_bytes,
            mime_type=mime_type or "image/jpeg",
            error_code=error_code if is_error_response else "",
            error_message=error_message if is_error_response else "",
        )
    except Exception:
        logger.exception(
            "Mentra photo-upload dispatch raised request_id=%s",
            request_id,
        )
        return JSONResponse(
            {
                "success": False,
                "error": "dispatch raised",
                "requestId": request_id,
            },
            status_code=500,
        )

    if isinstance(result, WebhookResponse) and result.status == "success":
        return JSONResponse(
            {
                "success": True,
                "requestId": request_id,
                "message": result.message or "photo received",
            },
            status_code=200,
        )
    # No pending request matched → 404 per upstream contract so the
    # cloud doesn't retry. Also covers session-ended-mid-capture.
    return JSONResponse(
        {
            "success": False,
            "error": (
                result.message
                if isinstance(result, WebhookResponse)
                else "no pending request found"
            ),
            "requestId": request_id,
        },
        status_code=404,
    )


@router.post("/webhook")
async def mentra_webhook(request: Request) -> dict[str, str]:
    """Receive a Mentra Cloud webhook (session_request /
    stop_request).

    Always returns 200 with a status field — the cloud retries on
    non-2xx and we'd rather log a transient bug than back up
    Mentra's queue.
    """
    endpoint = _get_endpoint(request.app.state)
    if endpoint is None:
        logger.warning(
            "Mentra webhook arrived but plugin isn't loaded"
        )
        return WebhookResponse(
            status="error",
            message="mentra plugin not loaded",
        ).to_dict()

    try:
        payload = await request.json()
    except Exception:
        logger.warning("Mentra webhook with non-JSON body")
        return WebhookResponse(
            status="error", message="non-JSON body"
        ).to_dict()

    if not isinstance(payload, dict):
        logger.warning(
            "Mentra webhook with non-object JSON body: type=%s",
            type(payload).__name__,
        )
        return WebhookResponse(
            status="error", message="JSON body must be an object"
        ).to_dict()

    try:
        result = await endpoint.deliver_webhook_event(payload)
    except Exception:
        logger.exception("Mentra webhook dispatch raised")
        return WebhookResponse(
            status="error", message="dispatch raised"
        ).to_dict()

    # Normalize the response — the endpoint contract says
    # ``WebhookResponse``, but defend against a plugin returning
    # something else.
    if isinstance(result, WebhookResponse):
        return result.to_dict()
    return WebhookResponse(status="success").to_dict()


# ── Webview + debug routes ───────────────────────────────────────────


def _get_debug(state: Any) -> MentraDebugProvider | None:
    gilbert = getattr(state, "gilbert", None)
    if gilbert is None:
        return None
    svc = gilbert.service_manager.get_capability("mentra")
    if svc is None or not isinstance(svc, MentraDebugProvider):
        return None
    return svc


def _decode_jwt_sub_unsafe(token: str) -> str:
    """Decode the ``sub`` claim from a JWT WITHOUT verifying the
    signature. Used only for identifying which Mentra user is
    requesting their own debug data — not for trust decisions.

    Mentra signs ``aos_signed_user_token`` with an RS256 key whose
    public counterpart we don't have on hand; the webview path
    already accepts that we trust the cloud's URL routing (Mentra
    only opens its webview against URLs registered to this app).
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload = parts[1]
        # Re-pad base64 — JWT strips trailing ``=`` chars.
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return str(data.get("sub") or "")
    except Exception:
        return ""


@router.get("/debug/events")
async def mentra_debug_events(
    request: Request,
    user: str = Query("", description="Mentra user id (email)"),
    token: str = Query(
        "",
        alias="aos_signed_user_token",
        description="JWT — used to derive user when 'user' is not set",
    ),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """Return recent debug events + active-session summary for the
    Mentra user. The in-glasses-app webview polls this every few
    seconds for live state."""
    debug = _get_debug(request.app.state)
    if debug is None:
        return JSONResponse(
            {"error": "mentra plugin not loaded", "events": [], "session": None},
            status_code=200,
        )
    mentra_user = user or _decode_jwt_sub_unsafe(token)
    if not mentra_user:
        return JSONResponse(
            {"error": "no user resolvable from request", "events": [], "session": None},
            status_code=200,
        )
    events = debug.get_recent_events(mentra_user, limit=limit)
    session = debug.get_active_session_summary(mentra_user)
    return JSONResponse(
        {
            "user": mentra_user,
            "session": session,
            "events": events,
        }
    )


@router.get("/webview", response_class=HTMLResponse)
async def mentra_webview(request: Request) -> HTMLResponse:
    """Mobile-friendly debug page Mentra opens in a webview when the
    user taps the Gilbert app tile in the MentraOS phone app. Shows
    live session state + recent events. Pure HTML + inline JS — no
    bundler, no build step, designed to load fast on a phone."""
    return HTMLResponse(content=_WEBVIEW_HTML)


_WEBVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Gilbert · Mentra debug</title>
<style>
  :root {
    color-scheme: dark light;
    --bg: #0a0a0b;
    --fg: #e5e5e7;
    --muted: #8e8e93;
    --panel: #161618;
    --border: #2a2a2e;
    --ok: #34c759;
    --warn: #ff9f0a;
    --err: #ff3b30;
    --info: #64d2ff;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f7f7f8;
      --fg: #111;
      --muted: #6e6e73;
      --panel: #ffffff;
      --border: #e5e5ea;
    }
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--fg);
    font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    min-height: 100vh;
  }
  body { padding: 16px; padding-bottom: env(safe-area-inset-bottom, 16px); }
  header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  header h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .pulse {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted);
    box-shadow: 0 0 0 0 var(--muted);
  }
  .pulse.live { background: var(--ok); animation: pulse 2s infinite; }
  .pulse.stale { background: var(--warn); }
  .pulse.dead { background: var(--err); }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(52, 199, 89, 0.7); }
    70% { box-shadow: 0 0 0 8px rgba(52, 199, 89, 0); }
    100% { box-shadow: 0 0 0 0 rgba(52, 199, 89, 0); }
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    margin-bottom: 12px;
  }
  .panel h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); font-weight: 600; margin: 0 0 8px 0;
  }
  .session-grid {
    display: grid; grid-template-columns: auto 1fr; gap: 4px 12px;
    font-size: 13px;
  }
  .session-grid .label { color: var(--muted); }
  .session-grid .value { font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, monospace; word-break: break-all; }
  .caps { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .cap {
    font-size: 11px;
    padding: 2px 8px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--muted);
  }
  .cap.yes { color: var(--ok); border-color: var(--ok); }
  .cap.no { opacity: 0.4; }
  ul.events { list-style: none; padding: 0; margin: 0; }
  .events li {
    padding: 8px 0;
    border-top: 1px solid var(--border);
    display: grid;
    grid-template-columns: 60px 1fr;
    gap: 10px;
    align-items: start;
    font-size: 13px;
  }
  .events li:first-child { border-top: 0; }
  .events .time { color: var(--muted); font-size: 11px; font-family: ui-monospace, monospace; padding-top: 2px; }
  .events .msg { word-break: break-word; }
  .events .kind { display: block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 2px; }
  .events .lvl-info { }
  .events .lvl-warning .msg { color: var(--warn); }
  .events .lvl-error .msg { color: var(--err); }
  .empty { color: var(--muted); font-style: italic; padding: 8px 0; }
  .footer { color: var(--muted); font-size: 11px; text-align: center; margin-top: 16px; }
</style>
</head>
<body>

<header>
  <span class="pulse" id="pulse"></span>
  <h1>Gilbert · Mentra debug</h1>
</header>

<div class="panel">
  <h2>Session</h2>
  <div id="session-body" class="empty">No active session.</div>
</div>

<div class="panel">
  <h2>Events</h2>
  <ul class="events" id="events-list">
    <li class="empty">Loading…</li>
  </ul>
</div>

<div class="footer">refreshes every 2s · last update <span id="lastUpdate">—</span></div>

<script>
(function () {
  // Read the user token from the URL — Mentra passes both
  // ``aos_temp_token`` and ``aos_signed_user_token`` query params
  // on the webview open. We use the signed one's ``sub`` claim to
  // identify the user.
  const params = new URLSearchParams(location.search);
  const userToken = params.get("aos_signed_user_token") || "";

  // Helpers
  const fmtTime = (iso) => {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch { return "—"; }
  };
  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  let lastEventTs = 0;
  const STALE_MS = 60_000;

  async function poll() {
    const url = `/api/mentra/debug/events?aos_signed_user_token=${encodeURIComponent(userToken)}&limit=80`;
    let payload;
    try {
      const resp = await fetch(url, { cache: "no-store" });
      payload = await resp.json();
    } catch (e) {
      document.getElementById("pulse").className = "pulse dead";
      document.getElementById("lastUpdate").textContent = "(fetch failed)";
      return;
    }

    const session = payload.session;
    const events = payload.events || [];

    // Session panel
    const sBody = document.getElementById("session-body");
    if (!session) {
      sBody.className = "empty";
      sBody.textContent = "No active session.";
    } else {
      sBody.className = "";
      const caps = session.capabilities || {};
      const cap = (k, label) =>
        `<span class="cap ${caps[k] ? 'yes' : 'no'}">${label}</span>`;
      sBody.innerHTML = `
        <div class="session-grid">
          <div class="label">Device</div><div class="value">${esc(session.model || "(unknown)")}</div>
          <div class="label">Session</div><div class="value">${esc(session.session_id || "")}</div>
          <div class="label">Mentra user</div><div class="value">${esc(session.mentra_user_id || "")}</div>
          <div class="label">Gilbert user</div><div class="value">${esc(session.gilbert_user_id || "")}</div>
          <div class="label">Connected</div><div class="value">${esc(fmtTime(session.connected_at))}</div>
        </div>
        <div class="caps">
          ${cap("has_display", "display")}
          ${cap("has_camera", "camera")}
          ${cap("has_microphone", "mic")}
          ${cap("has_speaker", "speaker")}
        </div>
      `;
    }

    // Events list — newest at the top.
    const evList = document.getElementById("events-list");
    if (events.length === 0) {
      evList.innerHTML = '<li class="empty">No events yet.</li>';
    } else {
      const reversed = [...events].reverse();
      evList.innerHTML = reversed.map(ev => `
        <li class="lvl-${esc(ev.level || "info")}">
          <div class="time">${esc(fmtTime(ev.timestamp))}</div>
          <div>
            <span class="kind">${esc(ev.kind || "")}</span>
            <span class="msg">${esc(ev.message || "")}</span>
          </div>
        </li>
      `).join("");

      const newestTs = new Date(events[events.length - 1].timestamp).getTime();
      if (!isNaN(newestTs)) lastEventTs = newestTs;
    }

    // Pulse — green if recent activity, amber if stale, red if no
    // session at all + no events.
    const pulse = document.getElementById("pulse");
    const now = Date.now();
    if (session && (now - lastEventTs) < STALE_MS) {
      pulse.className = "pulse live";
    } else if (session) {
      pulse.className = "pulse stale";
    } else if (events.length === 0) {
      pulse.className = "pulse dead";
    } else {
      pulse.className = "pulse stale";
    }
    document.getElementById("lastUpdate").textContent = fmtTime(new Date().toISOString());
  }

  poll();
  setInterval(poll, 2000);
})();
</script>
</body>
</html>"""

