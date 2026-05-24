"""Shape tests for the streaming TTS interfaces."""

from collections.abc import AsyncIterator

import pytest

from gilbert.interfaces.tts import (
    AudioFormat,
    BidirectionalTTSCapability,
    BidirectionalTTSProvider,
    StreamingTTSCapability,
    StreamingTTSProvider,
    SynthesisRequest,
    TTSAudioChunk,
    TTSCapabilityError,
    TTSEvent,
    TTSFlushed,
    TTSStream,
    TTSStreamConfig,
    TTSStreamError,
    TTSWordTiming,
)


def test_tts_stream_config_defaults():
    cfg = TTSStreamConfig(voice_id="v1")
    assert cfg.voice_id == "v1"
    assert cfg.output_format == AudioFormat.MP3
    assert cfg.speed == 1.0
    assert cfg.context == ""
    assert cfg.sample_rate == 44100


def test_event_dataclasses_are_frozen():
    chunk = TTSAudioChunk(audio=b"abc")
    word = TTSWordTiming(word="hi", start_seconds=0.0, end_seconds=0.1)
    flushed = TTSFlushed(at_seconds=1.5)
    err = TTSStreamError(message="oops")
    assert chunk.audio == b"abc"
    assert word.word == "hi"
    assert flushed.at_seconds == 1.5
    assert err.recoverable is False
    with pytest.raises(Exception):
        chunk.audio = b"new"  # frozen


def test_tts_event_union_includes_all_event_types():
    # Static assertion: union assignability via runtime values.
    ev: TTSEvent
    for ev in (TTSAudioChunk(b""), TTSWordTiming("w", 0.0, 0.0),
               TTSFlushed(0.0), TTSStreamError("e")):
        assert isinstance(ev, (TTSAudioChunk, TTSWordTiming, TTSFlushed, TTSStreamError))


def test_capability_protocols_are_runtime_checkable():
    class _BatchOnly:
        pass

    class _Streaming:
        def synthesize_stream(self, request):  # type: ignore[no-untyped-def]
            async def _gen() -> AsyncIterator[bytes]:
                yield b""
            return _gen()

    class _Bidirectional:
        async def open_stream(self, config):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    assert not isinstance(_BatchOnly(), StreamingTTSCapability)
    assert isinstance(_Streaming(), StreamingTTSCapability)
    assert isinstance(_Bidirectional(), BidirectionalTTSCapability)
    # Provider protocols mirror the capability shape on the service side.
    assert isinstance(_Streaming(), StreamingTTSProvider)
    assert isinstance(_Bidirectional(), BidirectionalTTSProvider)


def test_tts_capability_error_is_runtime_error():
    e = TTSCapabilityError("nope")
    assert isinstance(e, RuntimeError)


def test_tts_stream_abstract_methods():
    # Cannot instantiate the ABC directly.
    with pytest.raises(TypeError):
        TTSStream()  # type: ignore[abstract]
