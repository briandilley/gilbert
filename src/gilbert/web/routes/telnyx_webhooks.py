"""HTTP + WebSocket endpoints Telnyx talks to.

Two pieces:

- ``POST /api/telnyx/webhook`` — call-control event webhook. Telnyx
  POSTs JSON for every status transition (initiated → answered →
  hangup, plus DTMF + streaming.* events). Routed into the matching
  ``_TelnyxCallSession`` via the plugin's registry.

- ``WS   /api/telnyx/media``   — bidirectional media stream. Inbound
  frames carry base64-encoded mulaw audio + ``start`` / ``stop`` /
  ``mark`` control events; outbound frames are written by the call
  brain through the session's ``AudioSink``.

This module deliberately lives in core ``web/routes/`` rather than the
telnyx plugin because:

- FastAPI route registration happens in ``web/__init__.py`` and we want
  the routes mounted at fixed paths even if the plugin's not loaded yet
  (Telnyx might POST to the webhook before plugin init finishes during
  a restart).
- The plugin's module is imported via the loader, but FastAPI's `app`
  is constructed earlier in startup — easier to mount routes from the
  composition root than to plumb the app handle into the plugin.

The functions still do all their real work through the plugin's
registry helpers (``find_session_by_*``, ``deliver_webhook_event``),
keeping the carrier semantics on the plugin side.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telnyx")


# Importing the plugin module at module-load time would create a hard
# dependency from core ``web/`` on a std-plugin, which is exactly the
# layer rule the codebase forbids. Instead we import lazily inside the
# handlers — when the plugin isn't loaded (Telnyx disabled, fresh
# install, …) we 503 with a helpful message instead of import-erroring.


def _import_plugin() -> Any:
    """Best-effort import of the Telnyx plugin. Returns the module or
    ``None`` if the plugin isn't loaded.

    The plugin loader registers the package as ``gilbert_plugin_telnyx``
    so we look there first. Returns ``None`` (handler 503s) if the
    plugin hasn't been loaded yet, rather than import-erroring core.
    """
    try:
        import importlib

        return importlib.import_module("gilbert_plugin_telnyx.telnyx_telephony")
    except ImportError:
        return None


@router.post("/webhook")
async def telnyx_webhook(request: Request) -> dict[str, str]:
    """Receive a Telnyx call-control webhook.

    We accept the JSON unconditionally (Telnyx retries on non-2xx for
    a few minutes — far better to ack a duplicate than miss a state
    transition because the plugin happened to be reloading).
    """
    plugin = _import_plugin()
    if plugin is None:
        logger.warning("Telnyx webhook arrived but plugin isn't loaded")
        return {"status": "no_plugin"}

    try:
        payload = await request.json()
    except Exception:
        logger.warning("Telnyx webhook with non-JSON body")
        return {"status": "bad_request"}

    try:
        await plugin.deliver_webhook_event(payload)
    except Exception:
        logger.exception("Telnyx webhook dispatch failed")
        # Still return 200 — Telnyx will keep retrying on failure and
        # we don't want a transient bug to back up their queue.
        return {"status": "error"}

    return {"status": "ok"}


@router.websocket("/media")
async def telnyx_media(ws: WebSocket) -> None:
    """Bidirectional media stream.

    Telnyx frame shapes (see https://developers.telnyx.com/docs/voice
    /webhooks/streaming-callbacks):

    Inbound from Telnyx:
      {"event": "start",  "start": {"call_control_id": "...",
                                     "stream_id": "...",
                                     "custom_parameters": {...}}}
      {"event": "media",  "media": {"payload": "<base64>"}}
      {"event": "stop"}

    We send outbound:
      {"event": "media", "stream_id": "...", "media": {"payload": "<base64>"}}
      {"event": "clear", "stream_id": "..."}

    On connect we authenticate by reading the ``start`` frame's
    ``custom_parameters`` for the ``token`` we stamped at call-place
    time. Anything that doesn't match a live session is dropped.
    """
    await ws.accept()
    plugin = _import_plugin()
    if plugin is None:
        await ws.close(code=1011, reason="telnyx plugin not loaded")
        return

    session = None
    try:
        # First frame must be ``start``. Telnyx sends it within
        # ~milliseconds of accepting our upgrade.
        first_raw = await ws.receive_text()
        first = _safe_loads(first_raw)
        if first.get("event") != "start":
            await ws.close(code=1008, reason="expected start frame")
            return

        start = first.get("start", {}) or {}
        params = start.get("custom_parameters") or {}
        token = str(params.get("token") or "")
        call_id = str(params.get("call_id") or "")
        session = (
            plugin.find_session_by_token(token)
            if token
            else plugin.find_session_by_gilbert_id(call_id)
        )
        if session is None:
            logger.warning(
                "Telnyx media WS connected for unknown call (token=%r, "
                "call_id=%r) — dropping",
                token,
                call_id,
            )
            await ws.close(code=1008, reason="unknown call")
            return

        session.media_ws = ws
        session.stream_id = str(start.get("stream_id") or "")

        # Main read loop: shovel inbound media into the session queue
        # and surface stop frames cleanly.
        while True:
            raw = await ws.receive_text()
            frame = _safe_loads(raw)
            ev = frame.get("event")
            if ev == "media":
                payload = ((frame.get("media") or {}).get("payload") or "")
                if payload:
                    try:
                        chunk = base64.b64decode(payload)
                    except Exception:
                        continue
                    await session.push_audio_in(chunk)
            elif ev == "stop":
                # Carrier closing the media side. The webhook will
                # follow up with the hangup event; we just let the
                # socket close.
                return
            # Other events (mark, dtmf) come through the webhook path
            # instead — Telnyx mirrors them there too.
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Telnyx media WS crashed")
    finally:
        if session is not None and session.media_ws is ws:
            session.media_ws = None


def _safe_loads(raw: str) -> dict[str, Any]:
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}
