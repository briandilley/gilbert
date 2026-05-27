"""Public audio-blob fetch route used by external cloud relays.

When a plugin synthesizes audio that needs to reach a third-party
cloud audio router (Mentra Cloud being the first consumer), it
registers the bytes with the ``audio_blob_store`` capability and
hands the cloud a URL like ``https://<host>/api/audio-blob/<id>``.
The cloud's server-side fetcher hits this route, gets the MP3,
and streams it to the device speaker.

Auth-exempted in ``web/auth.py`` (the cloud has no session cookie
to present). Security comes from the blob id being a 16-char
random uuid + a short TTL — a tradeoff that matches the route's
narrow purpose (a fresh url per spoken utterance, valid for ~60s).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from gilbert.interfaces.audio_blob import AudioBlobStore

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_store(state: Any) -> AudioBlobStore | None:
    """Resolve the ``audio_blob_store`` capability off the live
    Gilbert app. Returns None if the service isn't registered
    (e.g. during a misconfigured boot or in a test harness that
    skips core service registration).
    """
    gilbert = getattr(state, "gilbert", None)
    if gilbert is None:
        return None
    svc = gilbert.service_manager.get_capability("audio_blob_store")
    if svc is None or not isinstance(svc, AudioBlobStore):
        return None
    return svc


@router.get("/api/audio-blob/{blob_id}")
async def fetch_audio_blob(blob_id: str, request: Request) -> Response:
    """Stream the blob bytes back with the registered MIME type.

    404 on unknown / expired ids. ``Cache-Control: no-store``
    because blobs are per-utterance and short-lived; nothing
    downstream should cache them.
    """
    store = _get_store(request.app.state)
    if store is None:
        logger.warning(
            "/api/audio-blob hit but audio_blob_store capability is "
            "unavailable (boot order issue?)"
        )
        return Response(
            content=b"audio-blob store not available",
            status_code=503,
            media_type="text/plain",
        )

    blob = store.fetch(blob_id)
    if blob is None:
        return Response(
            content=b"audio blob not found or expired",
            status_code=404,
            media_type="text/plain",
        )

    return Response(
        content=blob.data,
        media_type=blob.mime,
        headers={
            "cache-control": "no-store",
            # No-op-ish but explicit so downstream proxies don't
            # try to be clever about identical-bytes responses.
            "content-length": str(len(blob.data)),
        },
    )
