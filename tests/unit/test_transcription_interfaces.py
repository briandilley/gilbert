"""Unit tests for transcription interface dataclasses, helpers, and ABCs."""

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    FinalTranscript,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    TranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptSegment,
    WakeEvent,
    WakeWordConfig,
    pcm_silence,
    resample_pcm,
)


def test_audio_format_defaults():
    fmt = AudioFormat(AudioEncoding.PCM_S16LE)
    assert fmt.sample_rate == 16000
    assert fmt.channels == 1
    assert fmt.encoding == AudioEncoding.PCM_S16LE


def test_transcription_request_defaults():
    req = TranscriptionRequest(audio=b"abc")
    assert req.format.encoding == AudioEncoding.AUTO
    assert req.language is None
    assert req.diarize is False
    assert req.word_timestamps is False
    assert req.context == ""
    assert req.prompt == ""


def test_transcription_result_default_segments():
    r = TranscriptionResult(text="hi")
    assert r.segments == []
    assert r.language == ""
    assert r.duration_seconds is None


def test_transcript_segment_round_trip():
    seg = TranscriptSegment(
        text="hello", start_seconds=0.0, end_seconds=1.5,
        speaker_label="speaker_0", confidence=0.97,
    )
    assert seg.text == "hello"
    assert seg.speaker_label == "speaker_0"


def test_streaming_event_shapes():
    p = PartialTranscript(text="hel", speaker_label="speaker_0")
    f = FinalTranscript(text="hello", start_seconds=0.0, end_seconds=0.5)
    s = SpeechStarted(at_seconds=0.0)
    e = SpeechEnded(at_seconds=0.5)
    err = TranscriptionError(message="boom")
    assert p.start_seconds == 0.0
    assert f.confidence is None
    assert err.recoverable is False
    assert s.at_seconds == 0.0 and e.at_seconds == 0.5


def test_wake_word_config_and_event():
    cfg = WakeWordConfig(keywords=["hey gilbert"], format=AudioFormat(AudioEncoding.PCM_S16LE))
    assert cfg.sensitivity == 0.5
    ev = WakeEvent(keyword="hey gilbert", at_seconds=1.23)
    assert ev.confidence is None


def test_pcm_silence_zero_seconds_is_empty():
    assert pcm_silence(0.0, 16000) == b""


def test_pcm_silence_length_matches_rate():
    # 1 second of 16kHz 16-bit PCM = 16000 samples * 2 bytes = 32000 bytes
    data = pcm_silence(1.0, 16000)
    assert len(data) == 32000
    assert data == b"\x00" * 32000


def test_pcm_silence_partial_second():
    # 0.5s @ 16kHz = 8000 samples * 2 bytes
    assert len(pcm_silence(0.5, 16000)) == 16000


def test_resample_pcm_identity_when_rates_match():
    src = b"\x01\x00" * 100
    assert resample_pcm(src, 16000, 16000) == src


def test_resample_pcm_downsample_halves_length():
    # 100 samples of 16-bit PCM downsampled from 32k → 16k = 50 samples
    src = b"\x01\x00" * 100  # 200 bytes
    out = resample_pcm(src, 32000, 16000)
    assert len(out) == 100  # 50 samples * 2 bytes


def test_resample_pcm_upsample_doubles_length():
    src = b"\x01\x00" * 100  # 200 bytes (100 samples)
    out = resample_pcm(src, 16000, 32000)
    # Upsample doubles the sample count → 400 bytes. audioop.ratecv may
    # round; allow ±2 samples.
    assert abs(len(out) - 400) <= 4


# ---------------------------------------------------------------------------
# Task 3: Backend ABCs and capability protocols
# ---------------------------------------------------------------------------


import pytest  # noqa: E402

from gilbert.interfaces.transcription import (  # noqa: E402
    BatchTranscriber,
    BatchTranscriptionBackend,
    StreamingTranscriber,
    StreamingTranscriptionBackend,
    TranscriptionStream,
    WakeWordBackend,
    WakeWordDetector,
    WakeWordListener,
)


def test_batch_backend_registry_records_subclasses():
    class _MyBatch(BatchTranscriptionBackend):
        backend_name = "_test_batch_registry"

        async def initialize(self, config):  # type: ignore[override]
            pass

        async def close(self):  # type: ignore[override]
            pass

        async def transcribe(self, request):  # type: ignore[override]
            raise NotImplementedError

    try:
        assert (
            BatchTranscriptionBackend.registered_backends().get("_test_batch_registry")
            is _MyBatch
        )
    finally:
        BatchTranscriptionBackend._registry.pop("_test_batch_registry", None)


def test_streaming_backend_registry_records_subclasses():
    class _MyStream(StreamingTranscriptionBackend):
        backend_name = "_test_stream_registry"

        async def initialize(self, config):  # type: ignore[override]
            pass

        async def close(self):  # type: ignore[override]
            pass

        async def open_stream(self, config):  # type: ignore[override]
            raise NotImplementedError

    try:
        assert (
            StreamingTranscriptionBackend.registered_backends().get("_test_stream_registry")
            is _MyStream
        )
    finally:
        StreamingTranscriptionBackend._registry.pop("_test_stream_registry", None)


def test_wake_word_backend_registry_records_subclasses():
    class _MyWake(WakeWordBackend):
        backend_name = "_test_wake_registry"

        async def initialize(self, config):  # type: ignore[override]
            pass

        async def close(self):  # type: ignore[override]
            pass

        async def open_detector(self, config):  # type: ignore[override]
            raise NotImplementedError

    try:
        assert (
            WakeWordBackend.registered_backends().get("_test_wake_registry")
            is _MyWake
        )
    finally:
        WakeWordBackend._registry.pop("_test_wake_registry", None)


def test_unnamed_subclass_is_not_registered():
    initial = dict(BatchTranscriptionBackend.registered_backends())

    class _Anon(BatchTranscriptionBackend):
        # no backend_name → must not register
        async def initialize(self, config):  # type: ignore[override]
            pass

        async def close(self):  # type: ignore[override]
            pass

        async def transcribe(self, request):  # type: ignore[override]
            raise NotImplementedError

    assert BatchTranscriptionBackend.registered_backends() == initial


def test_capability_protocols_runtime_checkable():
    class _BatchOnly:
        async def transcribe(self, request, backend=None):
            ...

    class _StreamingOnly:
        async def open_stream(self, config, backend=None):
            ...

    class _WakeOnly:
        async def open_detector(self, config, backend=None):
            ...

    assert isinstance(_BatchOnly(), BatchTranscriber)
    assert isinstance(_StreamingOnly(), StreamingTranscriber)
    assert isinstance(_WakeOnly(), WakeWordListener)
    assert not isinstance(_BatchOnly(), StreamingTranscriber)


def test_stream_and_detector_are_abcs():
    with pytest.raises(TypeError):
        TranscriptionStream()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        WakeWordDetector()  # type: ignore[abstract]
