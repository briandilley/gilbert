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

    On connect we authenticate by matching the start frame's
    ``call_control_id`` against our active-session registry. The
    optional ``custom_parameters.token`` we used to look for is only
    populated when ``stream_custom_parameters`` is set on place_call;
    Telnyx always includes ``call_control_id`` natively, so we lean
    on that as the primary identifier and treat the token as a
    secondary check.
    """
    await ws.accept()
    logger.info("Telnyx media WS: connection accepted")
    plugin = _import_plugin()
    if plugin is None:
        logger.warning("Telnyx media WS: plugin not loaded, closing")
        await ws.close(code=1011, reason="telnyx plugin not loaded")
        return

    session = None
    inbound_media_frames = 0
    try:
        # Telnyx's protocol sequence on the Media Stream WebSocket:
        #
        #   1. ``event: connected`` — handshake-complete handshake
        #      from the carrier. NOT the audio-stream start. Has to
        #      be accepted and ignored.
        #   2. ``event: start``     — the real stream beginning with
        #      ``call_control_id``, ``stream_id``, etc.
        #   3. ``event: media``     — audio frames
        #   4. ``event: stop``      — stream ended
        #
        # Earlier versions of this handler treated the first frame as
        # ``start`` unconditionally — receiving ``connected`` instead
        # made us close the socket immediately, which manifested in
        # carrier-side logs as ``streaming.failed:
        # disconnected_no_reconnect`` and on our side as hundreds of
        # dropped media-WS writes per call.
        #
        # Loop until we see ``start`` (or anything terminal). Cap the
        # iterations to keep a misbehaving carrier from holding the
        # connection open with junk preambles.
        start_frame: dict[str, Any] = {}
        for _ in range(5):
            raw = await ws.receive_text()
            frame = _safe_loads(raw)
            event_name = frame.get("event")
            logger.info(
                "Telnyx media WS: pre-start frame — event=%r",
                event_name,
            )
            if event_name == "start":
                start_frame = frame
                break
            if event_name == "connected":
                continue  # expected handshake; wait for start
            if event_name in (None, "stop"):
                await ws.close(code=1008, reason="protocol error before start")
                return
            # Unknown event — log and keep waiting; Telnyx may add
            # new preamble events over time and we don't want to drop
            # the connection on a forward-compat addition.
        else:
            await ws.close(code=1008, reason="no start frame after preamble")
            return

        start = start_frame.get("start", {}) or {}
        # Dump the entire start frame so we can see Telnyx's actual
        # field names — earlier guesswork had us using ``stream_id``
        # for the outbound media frames but the start frame yielded
        # an empty string, suggesting the key is named something else
        # (``stream_sid``? ``stream_uuid``? nested differently?).
        logger.info(
            "Telnyx media WS: start frame body: top-keys=%s start-keys=%s "
            "full=%s",
            sorted(start_frame.keys()),
            sorted(start.keys()),
            start_frame,
        )
        cc_id = str(start.get("call_control_id") or "")
        params = start.get("custom_parameters") or {}
        token = str(params.get("token") or "")
        call_id = str(params.get("call_id") or "")

        # Primary lookup: Telnyx's ``call_control_id`` is in every
        # start frame — match it against our sidecar map. Fall back to
        # the token / gilbert call_id from custom_parameters when set.
        session = None
        if cc_id:
            session = plugin.find_session_by_call_control_id(cc_id)
        if session is None and token:
            session = plugin.find_session_by_token(token)
        if session is None and call_id:
            session = plugin.find_session_by_gilbert_id(call_id)
        if session is None:
            logger.warning(
                "Telnyx media WS connected for unknown call "
                "(call_control_id=%r token=%r call_id=%r) — dropping",
                cc_id,
                token,
                call_id,
            )
            await ws.close(code=1008, reason="unknown call")
            return

        session.media_ws = ws
        # ``stream_id`` lives at the top level of the frame, NOT
        # nested inside ``start``. Diagnostic dump showed:
        #   top-keys: ['event', 'sequence_number', 'start', 'stream_id']
        #   start-keys: ['call_control_id', 'call_session_id', 'from', 'to', …]
        # Reading the wrong level here was the actual reason every
        # outbound media frame got silently dropped — we'd stamp an
        # empty ``stream_id`` into the JSON and Telnyx couldn't route
        # it back onto the call.
        session.stream_id = str(start_frame.get("stream_id") or "")
        logger.info(
            "Telnyx media WS: bound to session — call=%s stream_id=%s",
            session.call_id,
            session.stream_id,
        )

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
                    inbound_media_frames += 1
                    # First media frame is the smoking gun — Telnyx
                    # is actually forwarding remote audio to us.
                    if inbound_media_frames == 1:
                        logger.info(
                            "Telnyx media WS: first inbound media chunk — call=%s",
                            session.call_id,
                        )
            elif ev == "stop":
                # Carrier closing the media side. The webhook will
                # follow up with the hangup event; we just let the
                # socket close.
                logger.info(
                    "Telnyx media WS: stop frame received — call=%s "
                    "inbound_frames=%d",
                    session.call_id if session else "?",
                    inbound_media_frames,
                )
                return
            else:
                # Anything else (mark, error, ...) — log once so we
                # can see what Telnyx is actually sending without
                # spamming on media flood.
                logger.debug(
                    "Telnyx media WS: unhandled frame event=%r — call=%s",
                    ev,
                    session.call_id if session else "?",
                )
    except WebSocketDisconnect:
        logger.info(
            "Telnyx media WS: disconnected (Telnyx closed) — call=%s "
            "inbound_frames=%d",
            session.call_id if session else "?",
            inbound_media_frames,
        )
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
