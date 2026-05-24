"""Tests for TTSService streaming capability checks and behavior."""

from collections.abc import AsyncIterator

import pytest

from gilbert.core.services.tts import TTSService
from gilbert.interfaces.tts import (
    AudioFormat,
    StreamingTTSCapability,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    TTSCapabilityError,
    Voice,
)


class _BatchOnlyBackend(TTSBackend):
    """Implements only the abstract TTSBackend surface.

    Deliberately NOT setting ``backend_name`` so ``__init_subclass__``
    doesn't pollute the global ``TTSBackend._registry`` for the rest
    of the test session. ``svc._backend_name`` is set on the fixture
    instead — that's what error messages reference."""

    async def initialize(self, config): pass
    async def close(self): pass
    async def synthesize(self, request):
        return SynthesisResult(audio=b"FULL", format=request.output_format)
    async def list_voices(self):
        return []
    async def get_voice(self, voice_id):
        return None


class _StreamingBackend(_BatchOnlyBackend):
    """Adds StreamingTTSCapability."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[bytes] = [b"AAA", b"BBB", b"CCC"]

    def synthesize_stream(self, request: SynthesisRequest) -> AsyncIterator[bytes]:
        chunks = self.chunks

        async def _gen() -> AsyncIterator[bytes]:
            for c in chunks:
                yield c

        return _gen()


@pytest.fixture
def svc_with_batch_only() -> TTSService:
    svc = TTSService()
    svc._backend = _BatchOnlyBackend()
    svc._backend_name = "_batch_only_test"
    svc._enabled = True
    svc._silence_padding = 0.5  # would normally pad
    return svc


@pytest.fixture
def svc_with_streaming() -> TTSService:
    svc = TTSService()
    svc._backend = _StreamingBackend()
    svc._backend_name = "_streaming_test"
    svc._enabled = True
    svc._silence_padding = 0.5  # must NOT be applied to streaming
    return svc


def test_synthesize_stream_raises_when_backend_lacks_capability(svc_with_batch_only):
    req = SynthesisRequest(text="hi", voice_id="v1", output_format=AudioFormat.MP3)
    with pytest.raises(TTSCapabilityError) as ei:
        svc_with_batch_only.synthesize_stream(req)
    assert "_batch_only_test" in str(ei.value)


def test_synthesize_stream_raises_synchronously_not_on_first_iter(svc_with_batch_only):
    # The check must happen at the call site, not when the consumer
    # starts iterating — otherwise consumers see the error mid-loop.
    req = SynthesisRequest(text="hi", voice_id="v1", output_format=AudioFormat.MP3)
    with pytest.raises(TTSCapabilityError):
        # If the implementation were ``async def`` with ``yield``,
        # the call itself would return a generator object without
        # raising, and this assertion would fail.
        svc_with_batch_only.synthesize_stream(req)


@pytest.mark.asyncio
async def test_synthesize_stream_yields_backend_chunks_without_padding(svc_with_streaming):
    req = SynthesisRequest(text="hi", voice_id="v1", output_format=AudioFormat.MP3)
    chunks: list[bytes] = []
    async for c in svc_with_streaming.synthesize_stream(req):
        chunks.append(c)
    # Exactly the backend's chunks — no silence padding appended.
    assert chunks == [b"AAA", b"BBB", b"CCC"]


@pytest.mark.asyncio
async def test_synthesize_stream_raises_when_backend_none():
    svc = TTSService()  # no backend set; _enabled stays False
    req = SynthesisRequest(text="hi", voice_id="v1", output_format=AudioFormat.MP3)
    with pytest.raises(RuntimeError, match="TTS service is not enabled"):
        svc.synthesize_stream(req)
