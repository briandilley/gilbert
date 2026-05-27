"""Tests for ``GET /api/audio-blob/{blob_id}``.

The route is the public face of the in-memory audio-blob cache —
external cloud audio routers (Mentra Cloud being first) fetch
this URL to retrieve engine-synthesized clips. Coverage targets
the contract: registered blob serves with right mime + bytes,
unknown id returns 404, expired id returns 404, missing service
returns 503, no caching headers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gilbert.core.services.audio_blob_store import AudioBlobStoreService
from gilbert.web.routes.audio_blob import router as audio_blob_router


def _make_app(store: Any | None) -> FastAPI:
    """Mount the route against a stub service manager that returns
    ``store`` for the ``audio_blob_store`` capability lookup. Passing
    ``None`` tests the "service unavailable" path."""
    app = FastAPI()

    class _SM:
        def get_capability(self, name: str) -> Any:
            if name == "audio_blob_store":
                return store
            return None

    app.state.gilbert = SimpleNamespace(service_manager=_SM())
    app.include_router(audio_blob_router)
    return app


def test_returns_registered_blob_bytes_with_correct_mime() -> None:
    """Happy path — register a blob, fetch via the route, get back
    exactly what we stored."""
    svc = AudioBlobStoreService()
    blob_id = svc.register(b"\x49\x44\x33\x04\x00\x00", "audio/mpeg")
    client = TestClient(_make_app(svc))

    response = client.get(f"/api/audio-blob/{blob_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"\x49\x44\x33\x04\x00\x00"
    # No-store keeps the cloud from caching across utterances; a
    # CDN that cached this URL would serve the wrong bytes the next
    # time the same blob_id was issued (improbable but possible).
    assert response.headers["cache-control"] == "no-store"


def test_unknown_blob_id_returns_404() -> None:
    """Unknown ids look identical to expired ones from the route's
    perspective — both 404. Good for not leaking "this id existed
    but is now gone" timing info."""
    svc = AudioBlobStoreService()
    client = TestClient(_make_app(svc))

    response = client.get("/api/audio-blob/nonexistent_xyz")
    assert response.status_code == 404


def test_returns_503_when_service_unavailable() -> None:
    """If the AudioBlobStoreService isn't registered (boot order
    issue, broken composition root), the route must fail loud
    rather than 500 — 503 + plain text tells the operator exactly
    what's wrong without leaking internals."""
    client = TestClient(_make_app(None))

    response = client.get("/api/audio-blob/anything")
    assert response.status_code == 503


def test_mime_type_passed_through_verbatim() -> None:
    """Storing as ``audio/wav`` must serve as ``audio/wav`` — the
    cloud's decoder branches on Content-Type, so MP3 vs WAV
    routing depends on this being faithful."""
    svc = AudioBlobStoreService()
    blob_id = svc.register(b"RIFF....WAVE", "audio/wav")
    client = TestClient(_make_app(svc))

    response = client.get(f"/api/audio-blob/{blob_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")


def test_content_length_header_matches_body() -> None:
    """External fetchers (Mentra Cloud) rely on Content-Length to
    know when the stream is complete. Mismatch would cause hung
    fetches or truncated playback."""
    svc = AudioBlobStoreService()
    payload = b"a" * 12345
    blob_id = svc.register(payload, "audio/mpeg")
    client = TestClient(_make_app(svc))

    response = client.get(f"/api/audio-blob/{blob_id}")
    assert response.status_code == 200
    assert response.headers["content-length"] == "12345"
    assert len(response.content) == 12345
