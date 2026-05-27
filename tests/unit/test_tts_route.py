"""Tests for ``GET /api/tts``.

The route bridges Mentra Cloud's audio-fetch contract to Gilbert's
internal TTS service. Mentra Cloud GETs this URL when a Mentra app
calls ``session.audio.speak(text)`` — the SDK builds it against the
app's registered Server URL and the cloud fetches the response to
pipe to the phone → glasses.

These tests exercise the route in isolation: stub TTS service,
assert the route reads query params correctly, returns the right
content-type, surfaces failure modes as audio-compatible status
codes (silent failure beats a 500 that breaks the cloud's audio
pipeline).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
)
from gilbert.web.routes.tts import router as tts_router


class _FakeTTS:
    """Stub satisfying ``TTSProvider`` Protocol (one method)."""

    def __init__(self, audio: bytes = b"FAKE_MP3_BYTES") -> None:
        self.audio = audio
        self.calls: list[SynthesisRequest] = []
        self.raise_on_synthesize = False

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(request)
        if self.raise_on_synthesize:
            raise RuntimeError("simulated TTS failure")
        return SynthesisResult(
            audio=self.audio,
            format=AudioFormat.MP3,
            duration_seconds=2.5,
        )


def _make_app(tts: Any | None) -> FastAPI:
    """Build a FastAPI test app with the TTS router mounted against a
    stub service manager."""
    app = FastAPI()

    class _SM:
        def get_capability(self, name: str) -> Any:
            # TTSService advertises capability ``text_to_speech``;
            # see ``src/gilbert/core/services/tts.py``.
            if name == "text_to_speech":
                return tts
            return None

    app.state.gilbert = SimpleNamespace(service_manager=_SM())
    app.include_router(tts_router)
    return app


# ── Tests ───────────────────────────────────────────────────────────


def test_tts_synthesizes_and_returns_audio_mpeg() -> None:
    """The happy path Mentra Cloud expects — GET ``/api/tts?text=hi``
    returns ``audio/mpeg`` bytes the TTS service produced."""
    tts = _FakeTTS(audio=b"REAL_MP3_HERE")
    client = TestClient(_make_app(tts))

    response = client.get("/api/tts?text=hello+world")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"REAL_MP3_HERE"
    # ``cache-control: no-store`` — the SDK passes dynamic text, so
    # CDN caching by URL is wrong.
    assert response.headers["cache-control"] == "no-store"

    assert len(tts.calls) == 1
    req = tts.calls[0]
    assert req.text == "hello world"
    assert req.output_format == AudioFormat.MP3
    assert req.voice_id == ""  # backend default
    assert req.speed == 1.0


def test_tts_forwards_voice_id_and_settings() -> None:
    """The SDK URL-encodes a JSON ``voice_settings`` object — the
    route parses it and forwards to ``SynthesisRequest``."""
    tts = _FakeTTS()
    client = TestClient(_make_app(tts))

    settings = json.dumps(
        {"stability": 0.7, "similarity_boost": 0.8, "speed": 1.2}
    )
    response = client.get(
        "/api/tts",
        params={
            "text": "hi",
            "voice_id": "voice_abc",
            "voice_settings": settings,
        },
    )
    assert response.status_code == 200

    req = tts.calls[-1]
    assert req.voice_id == "voice_abc"
    assert req.stability == pytest.approx(0.7)
    assert req.similarity_boost == pytest.approx(0.8)
    assert req.speed == pytest.approx(1.2)


def test_tts_ignores_malformed_voice_settings() -> None:
    """Bad JSON in ``voice_settings`` shouldn't fail the request —
    fall back to defaults so the cloud's audio pipeline keeps moving."""
    tts = _FakeTTS()
    client = TestClient(_make_app(tts))

    response = client.get(
        "/api/tts",
        params={"text": "hi", "voice_settings": "not-json"},
    )
    assert response.status_code == 200
    req = tts.calls[-1]
    assert req.stability is None
    assert req.similarity_boost is None
    assert req.speed == 1.0


def test_tts_returns_400_on_missing_text() -> None:
    """No text → 400. Mentra Cloud should never hit this, but if it
    does we don't want to charge for synthesizing nothing."""
    tts = _FakeTTS()
    client = TestClient(_make_app(tts))

    response = client.get("/api/tts?text=")
    assert response.status_code == 400
    assert tts.calls == []


def test_tts_returns_503_when_service_not_running() -> None:
    """Plugin / service not loaded → return 503 quickly rather than
    blocking the cloud's audio pipeline waiting."""
    client = TestClient(_make_app(None))

    response = client.get("/api/tts?text=hi")
    assert response.status_code == 503


def test_tts_returns_502_on_synthesis_failure() -> None:
    """Backend raises → return 502. Keeps the route's contract
    consistent (4xx for bad input, 5xx for backend trouble)."""
    tts = _FakeTTS()
    tts.raise_on_synthesize = True
    client = TestClient(_make_app(tts))

    response = client.get("/api/tts?text=hi")
    assert response.status_code == 502
