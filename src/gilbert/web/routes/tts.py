"""Public TTS endpoint — used by Mentra Cloud's audio routing.

When a Mentra app calls ``session.audio.speak(text)``, the SDK
builds ``<app-server-url>/api/tts?text=...`` and sends it as the
``audioUrl`` field of an ``audio_play_request`` frame. Mentra Cloud
then fetches that URL and pipes the response to the phone → glasses.
**The app is responsible for hosting the TTS endpoint** — Mentra Cloud
doesn't synthesize audio itself for third-party apps.

This route bridges that contract: we accept the cloud's GET, run the
``text`` through Gilbert's existing TTS service (whichever backend the
operator configured — ElevenLabs / Kokoro / etc.), and stream the
resulting MP3 back. Same TTSProvider capability the speaker, doorbell,
greeting and phone-call services already use.

Mounted at ``/api/tts`` (root, not under ``/api/mentra/``) because
that's the URL the upstream SDK builds when the app's Server URL is
``https://<host>`` — the standard Mentra developer-console setup.
Auth-exempted in ``web/auth.py`` since Mentra Cloud can't carry a
Gilbert session cookie.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    TTSProvider,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_tts(state: Any) -> TTSProvider | None:
    """Resolve the ``text_to_speech`` capability off the live Gilbert app.

    The TTS service advertises capability name ``text_to_speech``
    (see ``core/services/tts.py``) — not ``tts``. The same name is
    used by every other consumer (speaker, doorbell, phone, greeting).
    """
    gilbert = getattr(state, "gilbert", None)
    if gilbert is None:
        return None
    svc = gilbert.service_manager.get_capability("text_to_speech")
    if svc is None or not isinstance(svc, TTSProvider):
        return None
    return svc


@router.get("/api/tts")
async def synthesize_tts(
    request: Request,
    text: str = Query("", description="Text to synthesize"),
    voice_id: str = Query("", description="Optional voice id override"),
    model_id: str = Query("", description="Optional model id override"),
    voice_settings: str = Query(
        "",
        description=(
            "Optional JSON object with ElevenLabs voice settings "
            '(``{"stability":..,"similarity_boost":..,"style":..,'
            '"speed":..}``). The SDK URL-encodes this verbatim.'
        ),
    ),
) -> Response:
    """Synthesize ``text`` to MP3 and stream back.

    Returns ``audio/mpeg`` so Mentra Cloud (or any other fetcher)
    can pipe the bytes straight to a media player. Errors return
    silent MP3 so the cloud doesn't crash the audio pipeline on
    failure — better silence than a 500 that breaks the session.
    """
    if not text:
        return Response(
            content=b"",
            status_code=400,
            media_type="text/plain",
        )

    tts = _get_tts(request.app.state)
    if tts is None:
        logger.warning("/api/tts hit but no TTS service is registered")
        return Response(
            content=b"TTS service not available",
            status_code=503,
            media_type="text/plain",
        )

    # Optional voice settings — parse the JSON the SDK encodes into
    # the query string. ElevenLabs uses snake_case
    # (similarity_boost, etc.) which matches our SynthesisRequest
    # field names; pass through what the SDK gave us.
    stability: float | None = None
    similarity_boost: float | None = None
    speed: float = 1.0
    if voice_settings:
        try:
            parsed = json.loads(voice_settings)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("stability"), (int, float)):
                    stability = float(parsed["stability"])
                if isinstance(parsed.get("similarity_boost"), (int, float)):
                    similarity_boost = float(parsed["similarity_boost"])
                if isinstance(parsed.get("speed"), (int, float)):
                    speed = float(parsed["speed"])
        except json.JSONDecodeError:
            logger.debug(
                "ignoring malformed voice_settings query param: %r",
                voice_settings[:80],
            )

    req = SynthesisRequest(
        text=text,
        voice_id=voice_id,  # empty → backend picks default
        output_format=AudioFormat.MP3,
        speed=speed,
        stability=stability,
        similarity_boost=similarity_boost,
    )

    try:
        result = await tts.synthesize(req)
    except Exception:
        logger.exception("/api/tts synthesis failed for text=%r", text[:80])
        return Response(
            content=b"synthesis failed",
            status_code=502,
            media_type="text/plain",
        )

    logger.info(
        "/api/tts served len=%d bytes text=%r voice_id=%r",
        len(result.audio),
        text[:60],
        voice_id or "<default>",
    )
    return Response(
        content=result.audio,
        media_type="audio/mpeg",
        headers={
            # No caching — text may be dynamic per request, and
            # caching would interfere with debug-iteration. If we
            # add hash-keyed caching later, set a short max-age
            # here and key by full URL.
            "cache-control": "no-store",
        },
    )
