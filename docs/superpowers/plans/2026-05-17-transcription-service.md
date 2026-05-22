# TranscriptionService Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-backend `TranscriptionService` to Gilbert, modeled on the new multi-backend `SpeakerService`, covering batch file transcription, streaming (browser voice + live meeting), and wake-word detection — with a bundled local Whisper backend so it works out of the box.

**Architecture:** Three sibling ABC registries (`BatchTranscriptionBackend`, `StreamingTranscriptionBackend`, `WakeWordBackend`) in `interfaces/transcription.py`. One aggregator service in `core/services/transcription.py` that loads N backends per role, exposes a default-per-role + per-call override routing API, registers WS RPC handlers for browser-mic sessions (`transcription.start_session` / `send_chunk` / `close_session` / `event`), and exposes batch transcription as slash commands / AI tools. Bundled `LocalWhisperBackend` in `integrations/local_whisper.py` using `faster-whisper`.

**Tech Stack:** Python 3.12, `uv`, `pytest`, `faster-whisper` (CPU-OK), existing Gilbert service framework (`Service`, `ServiceInfo`, `Configurable`, `ToolProvider`, `WsHandlerProvider`, `Backend` registries).

**Source spec:** [`docs/superpowers/specs/2026-05-17-transcription-service-design.md`](../specs/2026-05-17-transcription-service-design.md)

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/gilbert/interfaces/transcription.py` | **new** | All 3 ABCs, dataclasses, capability protocols, audio helpers. Imports nothing outside `interfaces/`. |
| `src/gilbert/core/services/transcription.py` | **new** | `TranscriptionService` aggregator + WS RPC plumbing. Side-effect imports the bundled backend inside its `start()`. |
| `src/gilbert/integrations/local_whisper.py` | **new** | Bundled `LocalWhisperBackend` (faster-whisper). |
| `src/gilbert/core/app.py` | modify | Register `TranscriptionService()` next to `TTSService()` / `SpeakerService()` (single line). |
| `pyproject.toml` | modify | Add `faster-whisper` dependency. |
| `tests/unit/test_transcription_interfaces.py` | **new** | Dataclass shapes, helper functions, ABC registry behavior. |
| `tests/unit/test_transcription_service.py` | **new** | Service-level: routing, WS RPCs, multi-user isolation, config wiring, lifecycle. Uses fake backends. |
| `tests/integration/test_local_whisper.py` | **new** | Real-backend smoke test (skipped if faster-whisper model can't load). |
| `README.md` | modify | Add speech-to-text row to integrations table. |
| `CLAUDE.md` | modify | Add `transcription.py` to interfaces list. |
| `docs/architecture/transcription-system.md` | **new** | Walkthrough analogous to `speaker-system.md`. |

---

## Task 1: Interface dataclasses & enums

**Files:**
- Create: `src/gilbert/interfaces/transcription.py`
- Test: `tests/unit/test_transcription_interfaces.py`

- [ ] **Step 1: Write failing test for dataclass roundtrips**

Create `tests/unit/test_transcription_interfaces.py`:

```python
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
    # SpeechStarted/Ended carry only a timestamp
    assert s.at_seconds == 0.0 and e.at_seconds == 0.5


def test_wake_word_config_and_event():
    cfg = WakeWordConfig(keywords=["hey gilbert"], format=AudioFormat(AudioEncoding.PCM_S16LE))
    assert cfg.sensitivity == 0.5
    ev = WakeEvent(keyword="hey gilbert", at_seconds=1.23)
    assert ev.confidence is None
```

- [ ] **Step 2: Verify the test fails (module does not yet exist)**

Run: `uv run pytest tests/unit/test_transcription_interfaces.py -v`
Expected: collection error / ImportError for `gilbert.interfaces.transcription`.

- [ ] **Step 3: Create `interfaces/transcription.py` with the dataclasses**

Create `src/gilbert/interfaces/transcription.py` (only the parts needed for this task — ABCs and helpers come in later tasks):

```python
"""Speech-to-text interface — convert audio into text.

Three sibling backend ABCs live here:
  - ``BatchTranscriptionBackend``   — one-shot bytes-in/text-out
  - ``StreamingTranscriptionBackend`` — push chunks, read transcript events
  - ``WakeWordBackend``             — push chunks, read wake events

A single backend class may inherit from more than one (e.g. a vendor
that does both batch and streaming). ``TranscriptionService`` is the
aggregator that loads backends from all three registries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AudioEncoding(StrEnum):
    """Audio encoding for transcription input."""

    PCM_S16LE = "pcm_s16le"   # raw 16-bit little-endian PCM
    OPUS      = "opus"        # browser-friendly streaming codec
    MP3       = "mp3"
    WAV       = "wav"
    M4A       = "m4a"
    OGG       = "ogg"
    WEBM      = "webm"
    AUTO      = "auto"        # batch only — backend sniffs the container


@dataclass(frozen=True)
class AudioFormat:
    """Describes the shape of the audio bytes being handed to a backend."""

    encoding: AudioEncoding
    sample_rate: int = 16000
    channels: int = 1


# --- Batch -----------------------------------------------------------

@dataclass(frozen=True)
class TranscriptionRequest:
    """One-shot transcription request."""

    audio: bytes
    format: AudioFormat = field(default_factory=lambda: AudioFormat(AudioEncoding.AUTO))
    language: str | None = None     # BCP-47 hint; None = auto-detect
    prompt: str = ""                # optional vocabulary/style bias
    diarize: bool = False
    word_timestamps: bool = False
    context: str = ""               # free-form caller hint (mirrors TTS)


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start_seconds: float
    end_seconds: float
    speaker_label: str = ""         # "" when diarization off / unsupported
    confidence: float | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = ""              # detected or echoed
    duration_seconds: float | None = None
    audio_seconds_used: float | None = None


# --- Streaming -------------------------------------------------------

@dataclass(frozen=True)
class StreamConfig:
    format: AudioFormat
    language: str | None = None
    prompt: str = ""
    diarize: bool = False
    interim_results: bool = True    # emit PartialTranscript events
    vad_events: bool = True         # emit SpeechStarted / SpeechEnded


@dataclass(frozen=True)
class PartialTranscript:
    text: str
    speaker_label: str = ""
    start_seconds: float = 0.0


@dataclass(frozen=True)
class FinalTranscript:
    text: str
    start_seconds: float
    end_seconds: float
    speaker_label: str = ""
    confidence: float | None = None


@dataclass(frozen=True)
class SpeechStarted:
    at_seconds: float


@dataclass(frozen=True)
class SpeechEnded:
    at_seconds: float


@dataclass(frozen=True)
class TranscriptionError:
    message: str
    recoverable: bool = False


TranscriptionEvent = (
    PartialTranscript
    | FinalTranscript
    | SpeechStarted
    | SpeechEnded
    | TranscriptionError
)


# --- Wake word -------------------------------------------------------

@dataclass(frozen=True)
class WakeWordConfig:
    keywords: list[str]             # e.g. ["hey gilbert", "computer"]
    format: AudioFormat             # most engines want 16kHz mono PCM
    sensitivity: float = 0.5        # 0..1


@dataclass(frozen=True)
class WakeEvent:
    keyword: str
    at_seconds: float
    confidence: float | None = None
```

- [ ] **Step 4: Verify the test passes**

Run: `uv run pytest tests/unit/test_transcription_interfaces.py -v`
Expected: all 6 tests pass.

- [ ] **Step 5: Lint & type-check the new file**

Run:
```bash
uv run ruff check src/gilbert/interfaces/transcription.py tests/unit/test_transcription_interfaces.py
uv run mypy src/gilbert/interfaces/transcription.py
```
Expected: no warnings / errors.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/interfaces/transcription.py tests/unit/test_transcription_interfaces.py
git commit -m "transcription: interface dataclasses and event types"
```

---

## Task 2: Audio helpers (`pcm_silence`, `resample_pcm`)

**Files:**
- Modify: `src/gilbert/interfaces/transcription.py`
- Modify: `tests/unit/test_transcription_interfaces.py`

- [ ] **Step 1: Add failing tests for helpers**

Append to `tests/unit/test_transcription_interfaces.py`:

```python
from gilbert.interfaces.transcription import pcm_silence, resample_pcm


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
```

- [ ] **Step 2: Verify the tests fail (helpers don't exist yet)**

Run: `uv run pytest tests/unit/test_transcription_interfaces.py::test_pcm_silence_length_matches_rate -v`
Expected: ImportError on `pcm_silence` / `resample_pcm`.

- [ ] **Step 3: Implement the helpers**

Append to `src/gilbert/interfaces/transcription.py` (above the `# --- Batch ---` section if you prefer; either spot works):

```python
# --- Audio helpers ---------------------------------------------------
#
# Pure, vendor-free. Live in ``interfaces/`` so both the core service
# and plugin tests can use them without depending on any backend.
# Mirrors how ``interfaces/tts.py`` ships ``append_silence``.

import audioop


def pcm_silence(seconds: float, sample_rate: int) -> bytes:
    """Generate ``seconds`` of 16-bit little-endian PCM silence at ``sample_rate``.

    Returns ``b""`` for non-positive ``seconds``. Always mono (1 channel).
    """
    if seconds <= 0:
        return b""
    samples = int(seconds * sample_rate)
    return b"\x00\x00" * samples


def resample_pcm(audio: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit little-endian mono PCM from ``src_rate`` → ``dst_rate``.

    Pass-through when the rates already match. Uses ``audioop.ratecv``
    which is shipped with stdlib in 3.12 (deprecated in 3.13 but still
    functional; swap to ``soxr`` later if/when that becomes a problem).
    """
    if src_rate == dst_rate:
        return audio
    converted, _ = audioop.ratecv(audio, 2, 1, src_rate, dst_rate, None)
    return converted
```

- [ ] **Step 4: Verify all interface tests pass**

Run: `uv run pytest tests/unit/test_transcription_interfaces.py -v`
Expected: 12 tests pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/gilbert/interfaces/transcription.py tests/unit/test_transcription_interfaces.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/interfaces/transcription.py tests/unit/test_transcription_interfaces.py
git commit -m "transcription: pcm_silence and resample_pcm helpers"
```

---

## Task 3: Backend ABCs + capability protocols

**Files:**
- Modify: `src/gilbert/interfaces/transcription.py`
- Modify: `tests/unit/test_transcription_interfaces.py`

- [ ] **Step 1: Add failing tests for ABCs and protocols**

Append to `tests/unit/test_transcription_interfaces.py`:

```python
from collections.abc import AsyncIterator

import pytest

from gilbert.interfaces.transcription import (
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
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/unit/test_transcription_interfaces.py -v -k "registry or protocol or abc"`
Expected: ImportError on the new symbols.

- [ ] **Step 3: Add ABCs and protocols**

Append to `src/gilbert/interfaces/transcription.py`:

```python
# --- Streaming / detector primitive ABCs -----------------------------

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


class TranscriptionStream(ABC):
    """A live streaming-transcription session opened by a backend.

    Producer pushes audio chunks via ``send``; consumer reads
    ``TranscriptionEvent``s from the ``events()`` async iterator.
    ``close()`` signals end-of-audio — ``events()`` should still drain
    any final events the backend emits during shutdown.
    """

    @abstractmethod
    async def send(self, chunk: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[TranscriptionEvent]: ...


class WakeWordDetector(ABC):
    """A live wake-word-detection session opened by a backend."""

    @abstractmethod
    async def send(self, chunk: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[WakeEvent]: ...


# --- Backend ABCs with registries -----------------------------------

class BatchTranscriptionBackend(ABC):
    """One-shot bytes-in / text-out transcription."""

    _registry: dict[str, type["BatchTranscriptionBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            BatchTranscriptionBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["BatchTranscriptionBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...

    async def list_languages(self) -> list[str]:
        """Optional: best-effort list of supported language codes. Default empty."""
        return []


class StreamingTranscriptionBackend(ABC):
    """Streaming transcription — push chunks, read transcript events."""

    _registry: dict[str, type["StreamingTranscriptionBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            StreamingTranscriptionBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["StreamingTranscriptionBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def open_stream(self, config: StreamConfig) -> TranscriptionStream: ...


class WakeWordBackend(ABC):
    """Continuous wake-word detection — push chunks, read wake events."""

    _registry: dict[str, type["WakeWordBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            WakeWordBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["WakeWordBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector: ...


# --- Consumer-facing capability protocols ----------------------------

@runtime_checkable
class BatchTranscriber(Protocol):
    """Service-level protocol for any object that can batch-transcribe."""

    async def transcribe(
        self,
        request: TranscriptionRequest,
        backend: str | None = None,
    ) -> TranscriptionResult: ...


@runtime_checkable
class StreamingTranscriber(Protocol):
    async def open_stream(
        self,
        config: StreamConfig,
        backend: str | None = None,
    ) -> TranscriptionStream: ...


@runtime_checkable
class WakeWordListener(Protocol):
    async def open_detector(
        self,
        config: WakeWordConfig,
        backend: str | None = None,
    ) -> WakeWordDetector: ...
```

- [ ] **Step 4: Verify the new tests pass and the whole file is green**

Run: `uv run pytest tests/unit/test_transcription_interfaces.py -v`
Expected: ~18 tests pass.

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run ruff check src/gilbert/interfaces/transcription.py tests/unit/test_transcription_interfaces.py
uv run mypy src/gilbert/interfaces/transcription.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/interfaces/transcription.py tests/unit/test_transcription_interfaces.py
git commit -m "transcription: backend ABCs and capability protocols"
```

---

## Task 4: Service skeleton, ServiceInfo, and `config_params`

**Files:**
- Create: `src/gilbert/core/services/transcription.py`
- Create: `tests/unit/test_transcription_service.py`

- [ ] **Step 1: Write failing test for service shape and config**

Create `tests/unit/test_transcription_service.py`:

```python
"""Unit tests for TranscriptionService."""

from gilbert.core.services.transcription import TranscriptionService
from gilbert.interfaces.service import ServiceInfo


def test_service_info_shape():
    svc = TranscriptionService()
    info = svc.service_info()
    assert isinstance(info, ServiceInfo)
    assert info.name == "transcription"
    assert "speech_to_text" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert "ws_handlers" in info.capabilities
    assert "configuration" in info.optional
    assert "event_bus" in info.optional
    assert "access_control" in info.optional
    assert info.toggleable is True


def test_service_config_namespace_and_category():
    svc = TranscriptionService()
    assert svc.config_namespace == "transcription"
    assert svc.config_category == "Media"


def test_config_params_includes_role_defaults_and_global_keys():
    svc = TranscriptionService()
    params = svc.config_params()
    keys = {p.key for p in params}
    assert "batch.default" in keys
    assert "streaming.default" in keys
    assert "wake_word.default" in keys
    assert "output_ttl_seconds" in keys
```

- [ ] **Step 2: Verify the test fails (module missing)**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: ImportError.

- [ ] **Step 3: Create the service skeleton**

Create `src/gilbert/core/services/transcription.py`:

```python
"""Transcription service — aggregates batch / streaming / wake-word backends.

Mirrors the multi-backend SpeakerService template: one ``Service``
instance owns multiple backend instances (one per role), exposes a
default-per-role + per-call override routing API, and provides WS RPC
handlers for browser-mic sessions.

Side-effect imports for bundled vendor-free backends live inside
``start()`` / ``config_params()`` (see SpeakerService for the pattern).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    StreamingTranscriptionBackend,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionStream,
    WakeWordBackend,
    WakeWordDetector,
    WakeWordConfig,
    StreamConfig,
)

logger = logging.getLogger(__name__)


@dataclass
class _ActiveSession:
    """Per-WS-connection transcription session.

    Held only on the service singleton in ``self._sessions[conn_id]`` —
    never as request-scoped state on ``self``.
    """

    session_id: str
    conn_id: str
    user_id: str
    mode: str                       # "stream" | "wake_word"
    primitive: TranscriptionStream | WakeWordDetector
    pump_task: asyncio.Task[None] | None = None


class TranscriptionService(Service):
    """Aggregator over Batch/Streaming/WakeWord backends plus browser-mic plumbing."""

    def __init__(self) -> None:
        # Loaded backends, keyed by backend_name within each role.
        self._batch_backends: dict[str, BatchTranscriptionBackend] = {}
        self._streaming_backends: dict[str, StreamingTranscriptionBackend] = {}
        self._wake_word_backends: dict[str, WakeWordBackend] = {}
        self._default_batch: str = ""
        self._default_streaming: str = ""
        self._default_wake_word: str = ""
        self._enabled: bool = False
        self._output_ttl_seconds: int = 3600
        # Per-WS-connection active sessions. Keyed by session_id (UUID),
        # which a single conn_id may hold several of.
        self._sessions: dict[str, _ActiveSession] = {}
        self._sessions_guard = asyncio.Lock()
        # Per-role startup failures so the settings UI can show them.
        self._startup_failures: dict[str, dict[str, str]] = {
            "batch": {}, "streaming": {}, "wake_word": {},
        }
        self._resolver: ServiceResolver | None = None
        self._event_bus_provider: Any = None
        self._access_control: AccessControlProvider | None = None

    # --- Service ----------------------------------------------------

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="transcription",
            capabilities=frozenset({"speech_to_text", "ai_tools", "ws_handlers"}),
            optional=frozenset({"configuration", "event_bus", "access_control"}),
            toggleable=True,
            toggle_description="Speech-to-text transcription",
        )

    # --- Configurable ----------------------------------------------

    @property
    def config_namespace(self) -> str:
        return "transcription"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        # Side-effect import so bundled backends are registered before
        # we enumerate them. Guarded so the service is still importable
        # while LocalWhisperBackend is being implemented (Task 12) and
        # so it stays resilient if the bundled module is ever removed.
        try:
            import gilbert.integrations.local_whisper  # noqa: F401
        except ImportError:
            pass

        batch_choices = tuple(BatchTranscriptionBackend.registered_backends().keys())
        streaming_choices = tuple(StreamingTranscriptionBackend.registered_backends().keys())
        wake_choices = tuple(WakeWordBackend.registered_backends().keys())

        params: list[ConfigParam] = [
            ConfigParam(
                key="output_ttl_seconds",
                type=ToolParameterType.NUMBER,
                description="Seconds before transient transcript files are cleaned up.",
                default=3600,
            ),
            ConfigParam(
                key="batch.default",
                type=ToolParameterType.STRING,
                description="Default backend for batch (file) transcription.",
                default=batch_choices[0] if batch_choices else "",
                choices=batch_choices,
            ),
            ConfigParam(
                key="streaming.default",
                type=ToolParameterType.STRING,
                description="Default backend for streaming transcription.",
                default=streaming_choices[0] if streaming_choices else "",
                choices=streaming_choices,
            ),
            ConfigParam(
                key="wake_word.default",
                type=ToolParameterType.STRING,
                description="Default wake-word backend.",
                default=wake_choices[0] if wake_choices else "",
                choices=wake_choices,
            ),
        ]

        # Per-backend settings flattened into dotted keys, one block per role.
        for role, registry in (
            ("batch", BatchTranscriptionBackend.registered_backends()),
            ("streaming", StreamingTranscriptionBackend.registered_backends()),
            ("wake_word", WakeWordBackend.registered_backends()),
        ):
            for name, cls in registry.items():
                # Per-backend enabled toggle (off by default for everything
                # except local_whisper — which we ship enabled so the
                # service is useful out of the box).
                params.append(
                    ConfigParam(
                        key=f"{role}.backends.{name}.enabled",
                        type=ToolParameterType.BOOLEAN,
                        description=f"Enable the {name!r} {role} backend.",
                        default=(role == "batch" and name == "local_whisper"),
                        restart_required=True,
                    )
                )
                for bp in cls.backend_config_params():
                    params.append(
                        ConfigParam(
                            key=f"{role}.backends.{name}.settings.{bp.key}",
                            type=bp.type,
                            description=bp.description,
                            default=bp.default,
                            restart_required=bp.restart_required,
                            sensitive=bp.sensitive,
                            choices=bp.choices,
                            choices_from=bp.choices_from,
                            multiline=bp.multiline,
                            ai_prompt=bp.ai_prompt,
                            backend_param=True,
                        )
                    )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Apply config updates without a full service restart.

        Defers actual backend reinit until Task 5 wires ``_reinit_backends``.
        """
        out_ttl = config.get("output_ttl_seconds")
        if out_ttl is not None:
            self._output_ttl_seconds = int(out_ttl)
        for role in ("batch", "streaming", "wake_word"):
            section = config.get(role, {})
            if not isinstance(section, dict):
                continue
            default = section.get("default")
            if isinstance(default, str):
                setattr(self, f"_default_{role}", default)

    # --- Backends -------------------------------------------------

    @property
    def batch_backends(self) -> Mapping[str, BatchTranscriptionBackend]:
        return self._batch_backends

    @property
    def streaming_backends(self) -> Mapping[str, StreamingTranscriptionBackend]:
        return self._streaming_backends

    @property
    def wake_word_backends(self) -> Mapping[str, WakeWordBackend]:
        return self._wake_word_backends

    # --- Lifecycle (stubs — filled in in later tasks) -------------

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

    async def stop(self) -> None:
        for b in (
            *self._batch_backends.values(),
            *self._streaming_backends.values(),
            *self._wake_word_backends.values(),
        ):
            try:
                await b.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing transcription backend %r", b)

    # --- Config actions (stub — filled in in Task 8) --------------

    def config_actions(self) -> list[ConfigAction]:
        return []

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        return ConfigActionResult(ok=False, message=f"unknown action {key!r}")
```

- [ ] **Step 4: Verify the test passes**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 3 tests pass. (`config_params` includes the role defaults even when no backends are registered — choices will be empty tuples and defaults empty strings.)

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run ruff check src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
uv run mypy src/gilbert/core/services/transcription.py
```
Expected: ruff clean. The side-effect import of `local_whisper` is wrapped in `try/except ImportError`, so neither tests nor mypy should fail because the module doesn't exist yet — Task 12 creates it.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: service skeleton, ServiceInfo, config_params"
```

---

## Task 5: Backend loading via `_reinit_backends_for_role`

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

This task wires up real backend loading inside `start()` and a per-role reinit helper that `on_config_changed()` calls. Fake backends declared in the test file exercise it.

- [ ] **Step 1: Add a shared test helpers block (fake backends + a fake resolver)**

Append to `tests/unit/test_transcription_service.py`:

```python
"""Fake backends and a minimal resolver for service tests."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    PartialTranscript,
    FinalTranscript,
    StreamingTranscriptionBackend,
    StreamConfig,
    TranscriptionEvent,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionStream,
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)


# --- Fake batch backend -----------------------------------------------

class _FakeBatch(BatchTranscriptionBackend):
    backend_name = "_fake_batch"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="API key.",
                default="",
                sensitive=True,
            ),
        ]

    def __init__(self) -> None:
        self.initialized_with: dict[str, object] | None = None
        self.closed = False
        self.calls: list[TranscriptionRequest] = []

    async def initialize(self, config):
        self.initialized_with = dict(config)

    async def close(self):
        self.closed = True

    async def transcribe(self, request):
        self.calls.append(request)
        return TranscriptionResult(text="fake", language="en")


# --- Fake streaming backend ------------------------------------------

class _FakeStream(TranscriptionStream):
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue[TranscriptionEvent | None] = asyncio.Queue()

    async def send(self, chunk):
        self.sent.append(chunk)
        # Each chunk produces a final transcript for testing.
        await self._queue.put(FinalTranscript(
            text=f"chunk{len(self.sent)}", start_seconds=0.0, end_seconds=0.1,
        ))

    async def close(self):
        self.closed = True
        await self._queue.put(None)

    def events(self):
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[TranscriptionEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _FakeStreaming(StreamingTranscriptionBackend):
    backend_name = "_fake_streaming"

    def __init__(self) -> None:
        self.opened: list[StreamConfig] = []
        self.streams: list[_FakeStream] = []

    async def initialize(self, config):
        pass

    async def close(self):
        pass

    async def open_stream(self, config):
        self.opened.append(config)
        s = _FakeStream()
        self.streams.append(s)
        return s


# --- Fake wake-word backend ------------------------------------------

class _FakeDetector(WakeWordDetector):
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self.closed = False

    async def send(self, chunk):
        self.sent.append(chunk)

    async def close(self):
        self.closed = True
        await self._queue.put(None)

    def events(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _FakeWake(WakeWordBackend):
    backend_name = "_fake_wake"

    def __init__(self) -> None:
        self.detectors: list[_FakeDetector] = []

    async def initialize(self, config):
        pass

    async def close(self):
        pass

    async def open_detector(self, config):
        d = _FakeDetector()
        self.detectors.append(d)
        return d


```

(All tests in this file drive the service through its internal helpers
— `_apply_config_section` and `_reinit_backends_for_role` — so they
don't need a fake `ServiceResolver`. If a future test wants to exercise
`start()` end-to-end, add a minimal `ServiceResolver` stub at that
point.)

- [ ] **Step 2: Add failing test for backend loading**

Append:

```python
@pytest.mark.asyncio
async def test_start_loads_enabled_batch_backend():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    # Pretend the configuration service exposes our test section.
    svc._apply_config_section({
        "batch": {
            "default": "_fake_batch",
            "backends": {"_fake_batch": {"enabled": True, "settings": {"api_key": "k"}}},
        },
    })
    await svc._reinit_backends_for_role("batch")
    assert "_fake_batch" in svc.batch_backends
    backend = svc.batch_backends["_fake_batch"]
    assert isinstance(backend, _FakeBatch)
    assert backend.initialized_with == {"api_key": "k"}


@pytest.mark.asyncio
async def test_disabling_backend_closes_and_drops_it():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "batch": {"backends": {"_fake_batch": {"enabled": True, "settings": {}}}},
    })
    await svc._reinit_backends_for_role("batch")
    instance = svc.batch_backends["_fake_batch"]

    # Now flip it off and reinit.
    svc._apply_config_section({
        "batch": {"backends": {"_fake_batch": {"enabled": False}}},
    })
    await svc._reinit_backends_for_role("batch")
    assert "_fake_batch" not in svc.batch_backends
    assert instance.closed is True


@pytest.mark.asyncio
async def test_startup_failure_is_recorded_not_raised():
    """A backend that raises during initialize is recorded; service keeps running."""
    from gilbert.core.services.transcription import TranscriptionService

    class _Boom(BatchTranscriptionBackend):
        backend_name = "_boom_batch"

        async def initialize(self, config):
            raise RuntimeError("boom")

        async def close(self):
            pass

        async def transcribe(self, request):
            raise NotImplementedError

    try:
        svc = TranscriptionService()
        svc._apply_config_section({
            "batch": {"backends": {"_boom_batch": {"enabled": True}}},
        })
        await svc._reinit_backends_for_role("batch")
        assert "_boom_batch" not in svc.batch_backends
        assert "_boom_batch" in svc._startup_failures["batch"]
        assert "boom" in svc._startup_failures["batch"]["_boom_batch"]
    finally:
        BatchTranscriptionBackend._registry.pop("_boom_batch", None)
```

- [ ] **Step 3: Verify the tests fail**

Run: `uv run pytest tests/unit/test_transcription_service.py -v -k "loads or disable or startup"`
Expected: AttributeError on `_apply_config_section` / `_reinit_backends_for_role`.

- [ ] **Step 4: Implement the helpers**

Add to `TranscriptionService` (replace the placeholder `start()` and add the helpers):

```python
    # --- Internal config plumbing ---------------------------------

    def _apply_config_section(self, section: dict[str, Any]) -> None:
        """Cache the resolved transcription config section."""
        self._config_section = section
        if not isinstance(section, dict):
            return
        out_ttl = section.get("output_ttl_seconds")
        if out_ttl is not None:
            self._output_ttl_seconds = int(out_ttl)
        for role in ("batch", "streaming", "wake_word"):
            sub = section.get(role, {})
            if isinstance(sub, dict):
                default = sub.get("default")
                if isinstance(default, str):
                    setattr(self, f"_default_{role}", default)

    def _role_registry(self, role: str) -> dict[str, type]:
        if role == "batch":
            return BatchTranscriptionBackend.registered_backends()
        if role == "streaming":
            return StreamingTranscriptionBackend.registered_backends()
        if role == "wake_word":
            return WakeWordBackend.registered_backends()
        raise ValueError(f"unknown role {role!r}")

    def _role_loaded(self, role: str) -> dict[str, Any]:
        if role == "batch":
            return self._batch_backends            # type: ignore[return-value]
        if role == "streaming":
            return self._streaming_backends        # type: ignore[return-value]
        if role == "wake_word":
            return self._wake_word_backends        # type: ignore[return-value]
        raise ValueError(f"unknown role {role!r}")

    async def _reinit_backends_for_role(self, role: str) -> None:
        """Reconcile loaded backends for ``role`` against the latest config."""
        section = getattr(self, "_config_section", {}) or {}
        sub = section.get(role, {})
        backends_cfg = sub.get("backends", {}) if isinstance(sub, dict) else {}
        if not isinstance(backends_cfg, dict):
            backends_cfg = {}
        loaded = self._role_loaded(role)
        registry = self._role_registry(role)
        for name, cls in registry.items():
            cfg = backends_cfg.get(name, {})
            if not isinstance(cfg, dict):
                cfg = {}
            enabled = cfg.get("enabled", False) is True
            existing = loaded.get(name)
            if not enabled:
                if existing is not None:
                    try:
                        await existing.close()
                    except Exception:  # noqa: BLE001
                        logger.exception("error closing %s backend %r", role, name)
                    loaded.pop(name, None)
                self._startup_failures[role].pop(name, None)
                continue
            settings = cfg.get("settings", {}) if isinstance(cfg, dict) else {}
            if not isinstance(settings, dict):
                settings = {}
            if existing is None:
                try:
                    inst = cls()
                    await inst.initialize(settings)
                except Exception as exc:  # noqa: BLE001
                    self._startup_failures[role][name] = repr(exc)
                    logger.exception("failed to initialize %s backend %r", role, name)
                    continue
                loaded[name] = inst
                self._startup_failures[role].pop(name, None)
            # Already loaded: leave as-is. on_config_changed should
            # close+reopen if settings change in a way the backend can't
            # apply live — backends that support hot-reconfig override
            # initialize with idempotent behavior.
```

Replace the placeholder `start()`:

```python
    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Side-effect imports: register bundled vendor-free backends
        # before we ask the registries which to load. Guarded so the
        # service is still functional while LocalWhisperBackend is
        # being implemented in Task 12.
        try:
            import gilbert.integrations.local_whisper  # noqa: F401
        except ImportError:
            pass

        section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Transcription service disabled")
            return

        self._enabled = True
        self._apply_config_section(section)

        bus_svc = resolver.get_capability("event_bus")
        if bus_svc is not None:
            self._event_bus_provider = bus_svc
        acl_svc = resolver.get_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            self._access_control = acl_svc

        for role in ("batch", "streaming", "wake_word"):
            await self._reinit_backends_for_role(role)
        logger.info(
            "Transcription service started (batch=%s streaming=%s wake_word=%s)",
            sorted(self._batch_backends), sorted(self._streaming_backends),
            sorted(self._wake_word_backends),
        )
```

Update `on_config_changed`:

```python
    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config_section(config)
        for role in ("batch", "streaming", "wake_word"):
            await self._reinit_backends_for_role(role)
```

- [ ] **Step 5: Verify the new tests pass**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 6 tests pass (3 original + 3 new). `pytest-asyncio` should already be configured.

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: per-role backend loading and reinit"
```

---

## Task 6: `transcribe()` routing (batch)

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

- [ ] **Step 1: Add failing tests for batch routing**

Append:

```python
@pytest.mark.asyncio
async def test_transcribe_routes_to_default():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "batch": {
            "default": "_fake_batch",
            "backends": {"_fake_batch": {"enabled": True, "settings": {}}},
        },
    })
    await svc._reinit_backends_for_role("batch")
    result = await svc.transcribe(TranscriptionRequest(audio=b"\x00" * 10))
    assert result.text == "fake"
    assert svc.batch_backends["_fake_batch"].calls[0].audio == b"\x00" * 10


@pytest.mark.asyncio
async def test_transcribe_explicit_backend_overrides_default():
    from gilbert.core.services.transcription import TranscriptionService

    class _OtherBatch(BatchTranscriptionBackend):
        backend_name = "_other_batch"

        def __init__(self):
            self.calls = []

        async def initialize(self, config):
            pass

        async def close(self):
            pass

        async def transcribe(self, request):
            self.calls.append(request)
            return TranscriptionResult(text="other")

    try:
        svc = TranscriptionService()
        svc._apply_config_section({
            "batch": {
                "default": "_fake_batch",
                "backends": {
                    "_fake_batch": {"enabled": True},
                    "_other_batch": {"enabled": True},
                },
            },
        })
        await svc._reinit_backends_for_role("batch")
        out = await svc.transcribe(TranscriptionRequest(audio=b"\x00"), backend="_other_batch")
        assert out.text == "other"
        # Default backend was NOT used
        assert svc.batch_backends["_fake_batch"].calls == []
    finally:
        BatchTranscriptionBackend._registry.pop("_other_batch", None)


@pytest.mark.asyncio
async def test_transcribe_raises_when_no_backend_available():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()  # nothing loaded, no default
    with pytest.raises(RuntimeError, match="no transcription backend available"):
        await svc.transcribe(TranscriptionRequest(audio=b""))
```

- [ ] **Step 2: Verify they fail**

Run: `uv run pytest tests/unit/test_transcription_service.py -v -k transcribe`
Expected: AttributeError (`transcribe` not implemented yet).

- [ ] **Step 3: Implement `transcribe`**

Add to `TranscriptionService`:

```python
    # --- Public API: BatchTranscriber -----------------------------

    async def transcribe(
        self,
        request: TranscriptionRequest,
        backend: str | None = None,
    ) -> TranscriptionResult:
        name = backend or self._default_batch
        if not name or name not in self._batch_backends:
            # If no default but only one is loaded, use it.
            if len(self._batch_backends) == 1:
                name = next(iter(self._batch_backends))
            else:
                raise RuntimeError(
                    f"no transcription backend available for batch "
                    f"(asked for {backend!r}, default={self._default_batch!r}, "
                    f"loaded={sorted(self._batch_backends)})"
                )
        return await self._batch_backends[name].transcribe(request)
```

- [ ] **Step 4: Verify the tests pass**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 9 tests pass (6 prior + 3 new).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: batch transcribe routing"
```

---

## Task 7: `open_stream`, `open_detector`, `list_backends`

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

- [ ] **Step 1: Add failing tests**

Append:

```python
@pytest.mark.asyncio
async def test_open_stream_returns_backend_primitive():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")
    cfg = StreamConfig(format=__import__("gilbert.interfaces.transcription", fromlist=["AudioFormat", "AudioEncoding"]).AudioFormat(
        __import__("gilbert.interfaces.transcription", fromlist=["AudioEncoding"]).AudioEncoding.PCM_S16LE
    ))
    stream = await svc.open_stream(cfg)
    assert isinstance(stream, TranscriptionStream)
    await stream.send(b"\x00\x00")
    await stream.close()


@pytest.mark.asyncio
async def test_open_detector_returns_backend_primitive():
    from gilbert.core.services.transcription import TranscriptionService
    from gilbert.interfaces.transcription import AudioEncoding, AudioFormat

    svc = TranscriptionService()
    svc._apply_config_section({
        "wake_word": {
            "default": "_fake_wake",
            "backends": {"_fake_wake": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("wake_word")
    det = await svc.open_detector(WakeWordConfig(
        keywords=["hey"], format=AudioFormat(AudioEncoding.PCM_S16LE)
    ))
    assert isinstance(det, WakeWordDetector)
    await det.close()


def test_list_backends_returns_loaded_per_role():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._batch_backends["a"] = _FakeBatch()
    svc._streaming_backends["b"] = _FakeStreaming()
    out = svc.list_backends()
    assert out == {"batch": ["a"], "streaming": ["b"], "wake_word": []}
    assert svc.list_backends("batch") == {"batch": ["a"]}
```

- [ ] **Step 2: Verify they fail**

Run: `uv run pytest tests/unit/test_transcription_service.py -v -k "open_stream or open_detector or list_backends"`
Expected: AttributeError.

- [ ] **Step 3: Implement the methods**

Add to `TranscriptionService`:

```python
    # --- Public API: StreamingTranscriber + WakeWordListener -----

    async def open_stream(
        self,
        config: StreamConfig,
        backend: str | None = None,
    ) -> TranscriptionStream:
        name = backend or self._default_streaming
        if not name or name not in self._streaming_backends:
            if len(self._streaming_backends) == 1:
                name = next(iter(self._streaming_backends))
            else:
                raise RuntimeError(
                    f"no transcription backend available for streaming "
                    f"(asked for {backend!r}, default={self._default_streaming!r})"
                )
        return await self._streaming_backends[name].open_stream(config)

    async def open_detector(
        self,
        config: WakeWordConfig,
        backend: str | None = None,
    ) -> WakeWordDetector:
        name = backend or self._default_wake_word
        if not name or name not in self._wake_word_backends:
            if len(self._wake_word_backends) == 1:
                name = next(iter(self._wake_word_backends))
            else:
                raise RuntimeError(
                    f"no transcription backend available for wake_word "
                    f"(asked for {backend!r}, default={self._default_wake_word!r})"
                )
        return await self._wake_word_backends[name].open_detector(config)

    def list_backends(self, role: str | None = None) -> dict[str, list[str]]:
        """Return loaded backend names per role.

        With ``role=None`` returns all three roles. With ``role`` set,
        returns only that role's entry.
        """
        all_roles = {
            "batch": sorted(self._batch_backends),
            "streaming": sorted(self._streaming_backends),
            "wake_word": sorted(self._wake_word_backends),
        }
        if role is None:
            return all_roles
        return {role: all_roles[role]}
```

- [ ] **Step 4: Verify the tests pass**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 12 tests pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: streaming and wake-word routing + list_backends"
```

---

## Task 8: Config-action delegation

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

- [ ] **Step 1: Add failing test**

Append:

```python
def test_config_actions_aggregate_from_backend_classes():
    """Service exposes backend-declared config actions, tagged by backend."""
    from gilbert.core.services.transcription import TranscriptionService
    from gilbert.interfaces.configuration import BackendActionProvider

    # _FakeBatch doesn't declare any actions in this plan; treat absence
    # as zero actions. Just assert the call shape works and returns a list.
    svc = TranscriptionService()
    svc._batch_backends["_fake_batch"] = _FakeBatch()
    actions = svc.config_actions()
    assert isinstance(actions, list)
```

- [ ] **Step 2: Verify it passes against current stub but fails the next step**

Run: `uv run pytest tests/unit/test_transcription_service.py::test_config_actions_aggregate_from_backend_classes -v`
Expected: passes (stub returns `[]`). The richer assertion happens once a backend that *does* expose actions is added — covered manually when LocalWhisperBackend lands.

- [ ] **Step 3: Wire through `all_backend_actions` for all three role registries**

Replace the stubs in `TranscriptionService`:

```python
    # --- Config actions -------------------------------------------

    def config_actions(self) -> list[ConfigAction]:
        from gilbert.core.services._backend_actions import all_backend_actions

        actions: list[ConfigAction] = []
        for role, registry, loaded in (
            ("batch", BatchTranscriptionBackend.registered_backends(), self._batch_backends),
            ("streaming", StreamingTranscriptionBackend.registered_backends(), self._streaming_backends),
            ("wake_word", WakeWordBackend.registered_backends(), self._wake_word_backends),
        ):
            # Picking *any* loaded instance is fine — actions are class-
            # level metadata. Loaded instance is used only for live invokes.
            current = next(iter(loaded.values()), None)
            actions.extend(all_backend_actions(registry=registry, current_backend=current))
        return actions

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        from gilbert.core.services._backend_actions import invoke_backend_action

        # Try each role's loaded backends in turn. The action key's
        # backend-name prefix disambiguates which one to invoke.
        for loaded in (self._batch_backends, self._streaming_backends, self._wake_word_backends):
            for inst in loaded.values():
                result = await invoke_backend_action(inst, key, payload)
                if result.ok or "unknown" not in (result.message or "").lower():
                    return result
        return ConfigActionResult(ok=False, message=f"unknown action {key!r}")
```

- [ ] **Step 4: Re-run all service tests**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 13 tests pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: config_action delegation to backends"
```

---

## Task 9: WS RPC handlers — `start_session` / `close_session`

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

The service exposes WS handlers via the `WsHandlerProvider` protocol: implement `get_ws_handlers() -> dict[str, RpcHandler]` mapping each frame type (`transcription.start_session`, etc.) to an async handler `(conn, frame) -> dict`. There is NO list-of-records shape — it's a flat dict.

Before this task, read:
- `src/gilbert/interfaces/ws.py` — the `RpcHandler` type alias and `WsHandlerProvider` Protocol.
- `src/gilbert/core/services/speaker.py:1377-1411` — `get_ws_handlers()` + `_ws_browser_speaker_activate` (canonical example, including `conn.add_close_callback(lambda: ...)` for connection-drop cleanup).

Key framework facts the plan relies on (confirmed from `interfaces/ws.py` + `web/ws_protocol.py`):
- `conn.connection_id: str` — unique per WS connection.
- `conn.user_ctx: UserContext` and `conn.user_id: str`.
- `conn.enqueue(msg: dict)` — push a server-initiated frame to the client (fire-and-forget). Use this for `transcription.event` outbound frames.
- `conn.add_close_callback(callback)` — register a **synchronous** callback called when the connection drops. We use this at session-open time to schedule session cleanup; the callback schedules the actual async close via `asyncio.create_task(...)`.

- [ ] **Step 1: Add failing test for `start_session` / `close_session` lifecycle**

Append:

```python
@pytest.mark.asyncio
async def test_start_session_creates_record_and_close_drops_it():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _Conn:
        def __init__(self) -> None:
            self.connection_id = "conn-A"
            self.user_id = "u-1"
            self.user_ctx = type("U", (), {
                "user_id": "u-1", "display_name": "Bri",
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = "Bri"
            self.enqueued: list[dict] = []
            self.close_callbacks: list = []
        def enqueue(self, msg):
            self.enqueued.append(msg)
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    conn = _Conn()
    frame = {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {"interim_results": True},
    }
    result = await svc._handle_start_session(conn, frame)
    sid = result["session_id"]
    assert sid in svc._sessions
    assert svc._sessions[sid].conn_id == "conn-A"
    assert svc._sessions[sid].user_id == "u-1"
    assert svc._sessions[sid].mode == "stream"
    # A close callback was registered so connection-drop cleans up.
    assert len(conn.close_callbacks) == 1

    await svc._handle_close_session(conn, {"session_id": sid})
    assert sid not in svc._sessions


@pytest.mark.asyncio
async def test_connection_close_callback_cleans_up_sessions():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _Conn:
        def __init__(self) -> None:
            self.connection_id = "conn-B"
            self.user_id = "u-2"
            self.user_ctx = type("U", (), {
                "user_id": "u-2", "display_name": "Eve",
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = "Eve"
            self.close_callbacks: list = []
        def enqueue(self, msg): pass
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    conn = _Conn()
    r1 = await svc._handle_start_session(conn, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    r2 = await svc._handle_start_session(conn, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    assert {r1["session_id"], r2["session_id"]} <= set(svc._sessions)

    # Simulate the WS connection closing: callbacks fire synchronously
    # in arbitrary order. The service's callbacks schedule async cleanup,
    # so we drain the loop after firing them.
    for cb in list(conn.close_callbacks):
        cb()
    # Drain pending tasks
    for _ in range(50):
        await asyncio.sleep(0)
        if r1["session_id"] not in svc._sessions and r2["session_id"] not in svc._sessions:
            break
    assert r1["session_id"] not in svc._sessions
    assert r2["session_id"] not in svc._sessions
```

- [ ] **Step 2: Verify they fail**

Run: `uv run pytest tests/unit/test_transcription_service.py -v -k "start_session or close_callback"`
Expected: AttributeError on `_handle_start_session` / `_handle_close_session`.

- [ ] **Step 3: Implement `get_ws_handlers()` and the session handlers**

Add to `TranscriptionService` (mirrors `SpeakerService.get_ws_handlers` / `_ws_browser_speaker_activate` at `core/services/speaker.py:1377-1411`):

```python
    # --- WsHandlerProvider ---------------------------------------

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "transcription.start_session": self._handle_start_session,
            "transcription.send_chunk":    self._handle_send_chunk,
            "transcription.close_session": self._handle_close_session,
        }

    def _parse_audio_format(self, raw: dict[str, Any]):
        from gilbert.interfaces.transcription import AudioEncoding, AudioFormat

        encoding = AudioEncoding(raw.get("encoding", "pcm_s16le"))
        return AudioFormat(
            encoding=encoding,
            sample_rate=int(raw.get("sample_rate", 16000)),
            channels=int(raw.get("channels", 1)),
        )

    async def _handle_start_session(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        """Open a browser-mic session and register a close-callback for cleanup."""
        import uuid

        from gilbert.interfaces.transcription import StreamConfig, WakeWordConfig

        mode = frame.get("mode", "stream")
        fmt = self._parse_audio_format(frame.get("format", {}))
        backend_name = frame.get("backend")
        sub = frame.get("config", {}) if isinstance(frame.get("config"), dict) else {}

        if mode == "stream":
            cfg = StreamConfig(
                format=fmt,
                language=sub.get("language"),
                prompt=sub.get("prompt", ""),
                diarize=bool(sub.get("diarize", False)),
                interim_results=bool(sub.get("interim_results", True)),
                vad_events=bool(sub.get("vad_events", True)),
            )
            primitive: TranscriptionStream | WakeWordDetector = await self.open_stream(
                cfg, backend=backend_name
            )
        elif mode == "wake_word":
            cfg = WakeWordConfig(
                keywords=list(sub.get("keywords", [])),
                format=fmt,
                sensitivity=float(sub.get("sensitivity", 0.5)),
            )
            primitive = await self.open_detector(cfg, backend=backend_name)
        else:
            return {"ok": False, "error": f"unknown session mode {mode!r}"}

        session_id = uuid.uuid4().hex
        record = _ActiveSession(
            session_id=session_id,
            conn_id=conn.connection_id,
            user_id=conn.user_id or "",
            mode=mode,
            primitive=primitive,
        )
        async with self._sessions_guard:
            self._sessions[session_id] = record

        # Connection-drop cleanup. add_close_callback expects a sync
        # callable; we schedule the async cleanup as a task. Matches
        # SpeakerService._ws_browser_speaker_activate pattern at
        # core/services/speaker.py:1399.
        def _on_close(sid: str = session_id) -> None:
            asyncio.create_task(self._close_session(sid))

        conn.add_close_callback(_on_close)
        # Pump task is created lazily on first chunk in Task 10.
        return {"session_id": session_id}

    async def _handle_close_session(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        sid = frame.get("session_id")
        if not isinstance(sid, str):
            return {"ok": False, "error": "missing session_id"}
        await self._close_session(sid)
        return {"ok": True}

    async def _close_session(self, session_id: str) -> None:
        async with self._sessions_guard:
            rec = self._sessions.pop(session_id, None)
        if rec is None:
            return
        if rec.pump_task is not None:
            rec.pump_task.cancel()
        try:
            await rec.primitive.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing primitive for session %s", session_id)

    async def _handle_send_chunk(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        # Filled in in Task 10.
        return {"ok": False, "error": "not yet implemented"}
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 15 tests pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: WS start_session / close_session and disconnect cleanup"
```

---

## Task 10: WS RPC `send_chunk` + event pump

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

- [ ] **Step 1: Add failing test**

Append:

```python
@pytest.mark.asyncio
async def test_send_chunk_forwards_to_primitive_and_events_get_enqueued():
    import base64

    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _Conn:
        def __init__(self) -> None:
            self.connection_id = "conn-Z"
            self.user_id = "u-9"
            self.user_ctx = type("U", (), {
                "user_id": "u-9", "display_name": "Z",
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = "Z"
            self.enqueued: list[dict] = []
            self.close_callbacks: list = []
        def enqueue(self, msg):
            self.enqueued.append(msg)
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    conn = _Conn()
    open_res = await svc._handle_start_session(conn, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    sid = open_res["session_id"]

    # Send two chunks; each produces a FinalTranscript on _FakeStream.
    payload_bytes = b"\x00\x00\x01\x00"
    chunk_b64 = base64.b64encode(payload_bytes).decode()
    await svc._handle_send_chunk(conn, {"session_id": sid, "audio_b64": chunk_b64})
    await svc._handle_send_chunk(conn, {"session_id": sid, "audio_b64": chunk_b64})

    # Let the pump task drain.
    for _ in range(50):
        await asyncio.sleep(0)
        events_so_far = [m for m in conn.enqueued if m.get("type") == "transcription.event"]
        if len(events_so_far) >= 2:
            break

    events = [m for m in conn.enqueued if m.get("type") == "transcription.event"]
    assert len(events) >= 2
    assert events[0]["session_id"] == sid
    assert events[0]["event"]["type"] == "final"
    assert events[0]["event"]["text"].startswith("chunk")

    await svc._handle_close_session(conn, {"session_id": sid})
```

- [ ] **Step 2: Verify it fails**

Run: `uv run pytest tests/unit/test_transcription_service.py -v -k send_chunk`
Expected: assertion failure (handler returns `not yet implemented`).

- [ ] **Step 3: Implement `_handle_send_chunk` and the event pump**

Replace `_handle_send_chunk` and add helpers in `TranscriptionService`. The pump pushes server-initiated frames via `conn.enqueue({"type": "transcription.event", ...})` — same fire-and-forget pattern other services use to push events to the SPA.

```python
    async def _handle_send_chunk(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        import base64

        sid = frame.get("session_id")
        b64 = frame.get("audio_b64")
        if not isinstance(sid, str) or not isinstance(b64, str):
            return {"ok": False, "error": "missing session_id or audio_b64"}
        rec = self._sessions.get(sid)
        if rec is None or rec.conn_id != conn.connection_id:
            return {"ok": False, "error": "unknown session"}
        try:
            chunk = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "invalid base64"}

        # Lazy-start the pump on first chunk. Spawn under a copied
        # context so logging / trace context follow the task.
        if rec.pump_task is None or rec.pump_task.done():
            import contextvars

            ctx = contextvars.copy_context()
            rec.pump_task = asyncio.create_task(
                self._pump_events(conn, rec),
                name=f"transcription-pump-{sid}",
                context=ctx,
            )
        await rec.primitive.send(chunk)
        return {"ok": True}

    async def _pump_events(self, conn: Any, rec: _ActiveSession) -> None:
        """Drain the primitive's event stream and push server-initiated frames."""
        try:
            async for ev in rec.primitive.events():
                conn.enqueue({
                    "type": "transcription.event",
                    "session_id": rec.session_id,
                    "event": _event_to_json(ev),
                })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("transcription pump error for session %s", rec.session_id)
```

Add a module-level helper above the class:

```python
def _event_to_json(ev: Any) -> dict[str, Any]:
    """Encode a TranscriptionEvent or WakeEvent for the wire."""
    from gilbert.interfaces.transcription import (
        FinalTranscript, PartialTranscript, SpeechEnded, SpeechStarted,
        TranscriptionError, WakeEvent,
    )

    if isinstance(ev, PartialTranscript):
        return {"type": "partial", "text": ev.text, "speaker_label": ev.speaker_label,
                "start_seconds": ev.start_seconds}
    if isinstance(ev, FinalTranscript):
        return {"type": "final", "text": ev.text, "start_seconds": ev.start_seconds,
                "end_seconds": ev.end_seconds, "speaker_label": ev.speaker_label,
                "confidence": ev.confidence}
    if isinstance(ev, SpeechStarted):
        return {"type": "speech_started", "at_seconds": ev.at_seconds}
    if isinstance(ev, SpeechEnded):
        return {"type": "speech_ended", "at_seconds": ev.at_seconds}
    if isinstance(ev, TranscriptionError):
        return {"type": "error", "message": ev.message, "recoverable": ev.recoverable}
    if isinstance(ev, WakeEvent):
        return {"type": "wake", "keyword": ev.keyword, "at_seconds": ev.at_seconds,
                "confidence": ev.confidence}
    return {"type": "unknown"}
```

- [ ] **Step 4: Verify the test passes**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 16 tests pass.

- [ ] **Step 5: Add a multi-user-isolation test**

Append:

```python
@pytest.mark.asyncio
async def test_two_concurrent_sessions_do_not_cross_talk():
    import base64

    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _C:
        def __init__(self, cid: str, uid: str) -> None:
            self.connection_id = cid
            self.user_id = uid
            self.user_ctx = type("U", (), {
                "user_id": uid, "display_name": cid,
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = cid
            self.enqueued: list[dict] = []
            self.close_callbacks: list = []
        def enqueue(self, msg):
            if msg.get("type") == "transcription.event":
                self.enqueued.append(msg)
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    a, b = _C("conn-A", "u-A"), _C("conn-B", "u-B")
    ra = await svc._handle_start_session(a, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    rb = await svc._handle_start_session(b, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    payload = base64.b64encode(b"\x00\x00").decode()
    await svc._handle_send_chunk(a, {"session_id": ra["session_id"], "audio_b64": payload})
    await svc._handle_send_chunk(b, {"session_id": rb["session_id"], "audio_b64": payload})

    for _ in range(50):
        await asyncio.sleep(0)
        if a.enqueued and b.enqueued:
            break

    assert all(m["session_id"] == ra["session_id"] for m in a.enqueued)
    assert all(m["session_id"] == rb["session_id"] for m in b.enqueued)
    await svc._handle_close_session(a, {"session_id": ra["session_id"]})
    await svc._handle_close_session(b, {"session_id": rb["session_id"]})
```

- [ ] **Step 6: Run and confirm**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 17 tests pass.

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: WS send_chunk and event pump with multi-user isolation"
```

---

## Task 11: ToolProvider — `/transcription transcribe / backends / languages`

**Files:**
- Modify: `src/gilbert/core/services/transcription.py`
- Modify: `tests/unit/test_transcription_service.py`

Before this task, briefly read `src/gilbert/core/services/tts.py` lines 236–328 for the `ToolProvider` + tool-handler pattern.

- [ ] **Step 1: Add failing tests**

Append:

```python
def test_tool_provider_lists_three_tools_with_slash_group():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._enabled = True
    tools = svc.get_tools()
    names = [t.name for t in tools]
    assert names == ["transcribe", "backends", "languages"]
    for t in tools:
        assert t.slash_group == "transcription"
        assert t.slash_command
        assert t.slash_help
        assert t.required_role == "everyone"
        assert t.parallel_safe is True


def test_get_tools_returns_empty_when_service_disabled():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    # _enabled defaults to False; tools should be hidden.
    assert svc.get_tools() == []


@pytest.mark.asyncio
async def test_backends_tool_returns_loaded_per_role():
    import json

    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._enabled = True
    svc._batch_backends["_fake_batch"] = _FakeBatch()
    out = await svc.execute_tool("backends", {})
    data = json.loads(out)
    assert data["batch"] == ["_fake_batch"]
    assert data["streaming"] == []
    assert data["wake_word"] == []
```

- [ ] **Step 2: Verify they fail**

Run: `uv run pytest tests/unit/test_transcription_service.py -v -k "tool_provider or backends_tool or get_tools"`
Expected: AttributeError on `get_tools` / `execute_tool`.

- [ ] **Step 3: Implement `ToolProvider`**

Add to `TranscriptionService`:

```python
    # --- ToolProvider --------------------------------------------

    @property
    def tool_provider_name(self) -> str:
        return "transcription"

    def get_tools(self, user_ctx: UserContext | None = None):
        if not self._enabled:
            return []
        from gilbert.interfaces.tools import (
            ToolDefinition, ToolParameter, ToolParameterType,
        )

        return [
            ToolDefinition(
                name="transcribe",
                slash_group="transcription",
                slash_command="transcribe",
                slash_help='Transcribe an audio file: /transcription transcribe "<path or url>"',
                description=(
                    "Transcribe audio from a file path or URL. Writes the "
                    "transcript to an output file and returns the text plus "
                    "per-segment timings."
                ),
                parameters=[
                    ToolParameter(
                        name="source",
                        type=ToolParameterType.STRING,
                        description="Path to a local audio file, or an http(s) URL.",
                    ),
                    ToolParameter(
                        name="language",
                        type=ToolParameterType.STRING,
                        description="Optional language hint (BCP-47, e.g. 'en').",
                        required=False,
                    ),
                    ToolParameter(
                        name="diarize",
                        type=ToolParameterType.BOOLEAN,
                        description="Attempt speaker diarization if the backend supports it.",
                        required=False,
                    ),
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description="Override the default batch backend by name.",
                        required=False,
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="backends",
                slash_group="transcription",
                slash_command="backends",
                slash_help="List loaded transcription backends: /transcription backends [role]",
                description="List currently-loaded transcription backends per role.",
                parameters=[
                    ToolParameter(
                        name="role",
                        type=ToolParameterType.STRING,
                        description="Optional role filter: batch | streaming | wake_word.",
                        required=False,
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="languages",
                slash_group="transcription",
                slash_command="languages",
                slash_help="List supported languages: /transcription languages [backend]",
                description="Best-effort list of supported language codes for a batch backend.",
                parameters=[
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description="Backend name (defaults to the configured batch default).",
                        required=False,
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "transcribe":
                return await self._tool_transcribe(arguments)
            case "backends":
                return await self._tool_backends(arguments)
            case "languages":
                return await self._tool_languages(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_transcribe(self, arguments: dict[str, Any]) -> str:
        import json
        import uuid
        from pathlib import Path

        from gilbert.core.output import cleanup_old_files, get_output_dir
        from gilbert.interfaces.transcription import (
            AudioEncoding, AudioFormat, TranscriptionRequest,
        )

        source = arguments["source"]
        language = arguments.get("language")
        diarize = bool(arguments.get("diarize", False))
        backend = arguments.get("backend") or None

        # Fetch the bytes.
        if source.startswith("http://") or source.startswith("https://"):
            import httpx  # already a project dep

            async with httpx.AsyncClient() as client:
                resp = await client.get(source)
                resp.raise_for_status()
                audio = resp.content
        else:
            audio = Path(source).read_bytes()

        request = TranscriptionRequest(
            audio=audio,
            format=AudioFormat(AudioEncoding.AUTO),
            language=language,
            diarize=diarize,
        )
        result = await self.transcribe(request, backend=backend)

        out_dir = get_output_dir("transcription")
        cleanup_old_files(out_dir, self._output_ttl_seconds)
        out_path = out_dir / f"{uuid.uuid4().hex}.txt"
        out_path.write_text(result.text)

        return json.dumps({
            "file_path": str(out_path),
            "text": result.text,
            "segments": [
                {
                    "text": s.text, "start": s.start_seconds, "end": s.end_seconds,
                    "speaker_label": s.speaker_label, "confidence": s.confidence,
                }
                for s in result.segments
            ],
            "language": result.language,
            "duration_seconds": result.duration_seconds,
        })

    async def _tool_backends(self, arguments: dict[str, Any]) -> str:
        import json

        role = arguments.get("role")
        return json.dumps(self.list_backends(role))

    async def _tool_languages(self, arguments: dict[str, Any]) -> str:
        import json

        backend_name = arguments.get("backend") or self._default_batch
        if not backend_name or backend_name not in self._batch_backends:
            return json.dumps([])
        langs = await self._batch_backends[backend_name].list_languages()
        return json.dumps(langs)
```

- [ ] **Step 4: Verify the tests pass**

Run: `uv run pytest tests/unit/test_transcription_service.py -v`
Expected: 20 tests pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/gilbert/core/services/transcription.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/transcription.py tests/unit/test_transcription_service.py
git commit -m "transcription: ToolProvider — transcribe, backends, languages"
```

---

## Task 12: Bundled `LocalWhisperBackend`

**Files:**
- Create: `src/gilbert/integrations/local_whisper.py`
- Modify: `pyproject.toml`
- Create: `tests/integration/test_local_whisper.py`

- [ ] **Step 1: Add `faster-whisper` to deps**

Run: `uv add faster-whisper`
This edits `pyproject.toml` and `uv.lock`.

- [ ] **Step 2: Add failing integration test (skipped if model unavailable)**

Create `tests/integration/test_local_whisper.py`:

```python
"""Integration test for the bundled local Whisper backend.

Skipped automatically if the model can't be loaded (network restricted,
disk full, etc.) so CI without model cache doesn't fail.
"""

from pathlib import Path

import pytest

faster_whisper = pytest.importorskip("faster_whisper")

from gilbert.integrations.local_whisper import LocalWhisperBackend  # noqa: E402
from gilbert.interfaces.transcription import (  # noqa: E402
    AudioEncoding, AudioFormat, TranscriptionRequest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hello_world.wav"


@pytest.mark.asyncio
async def test_local_whisper_transcribes_known_phrase():
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing: {FIXTURE}")
    backend = LocalWhisperBackend()
    try:
        await backend.initialize({"model_size": "tiny", "compute_type": "int8"})
    except Exception as exc:
        pytest.skip(f"local-whisper model not available: {exc}")
    try:
        result = await backend.transcribe(
            TranscriptionRequest(
                audio=FIXTURE.read_bytes(),
                format=AudioFormat(AudioEncoding.WAV),
                language="en",
            )
        )
        assert "hello" in result.text.lower()
    finally:
        await backend.close()
```

(Provide a short `hello_world.wav` under `tests/integration/fixtures/` — a 1–2-second WAV clip with someone saying "hello world". If you don't have one to hand, generate one via `say` / `espeak` and commit. The test skips if the file is missing, so the rest of the suite stays green either way.)

- [ ] **Step 3: Verify the test fails (no module yet)**

Run: `uv run pytest tests/integration/test_local_whisper.py -v`
Expected: ImportError on `gilbert.integrations.local_whisper`.

- [ ] **Step 4: Implement `LocalWhisperBackend`**

Create `src/gilbert/integrations/local_whisper.py`:

```python
"""Bundled batch transcription backend using faster-whisper (CPU-OK).

Vendor-free in the sense that it requires no external API key —
the model is downloaded by faster-whisper on first use. Lives in
``integrations/`` (not a plugin) so the service has something to
register out of the box.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)


class LocalWhisperBackend(BatchTranscriptionBackend):
    """Batch transcription via faster-whisper running locally."""

    backend_name = "local_whisper"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="model_size",
                type=ToolParameterType.STRING,
                description="faster-whisper model size.",
                default="base",
                choices=("tiny", "base", "small", "medium", "large-v3"),
                restart_required=True,
            ),
            ConfigParam(
                key="compute_type",
                type=ToolParameterType.STRING,
                description="Precision: 'int8' is fastest on CPU; 'float16' on GPU.",
                default="int8",
                choices=("int8", "int8_float16", "float16", "float32"),
                restart_required=True,
            ),
            ConfigParam(
                key="device",
                type=ToolParameterType.STRING,
                description="Compute device.",
                default="cpu",
                choices=("cpu", "cuda", "auto"),
                restart_required=True,
            ),
        ]

    def __init__(self) -> None:
        self._model: Any = None
        self._model_size = "base"

    async def initialize(self, config: dict[str, object]) -> None:
        from faster_whisper import WhisperModel  # heavy import deferred to init

        self._model_size = str(config.get("model_size", "base"))
        compute_type = str(config.get("compute_type", "int8"))
        device = str(config.get("device", "cpu"))
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: WhisperModel(self._model_size, device=device, compute_type=compute_type),
        )
        logger.info("LocalWhisperBackend initialized: model=%s device=%s compute=%s",
                    self._model_size, device, compute_type)

    async def close(self) -> None:
        self._model = None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if self._model is None:
            raise RuntimeError("LocalWhisperBackend is not initialized")

        # faster-whisper wants a file or path. Spool to a temp file —
        # safer than passing bytes through audio-decoders that vary in
        # what they accept. AUTO encoding works because Whisper's
        # internal decoder sniffs the container.
        with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tf:
            tf.write(request.audio)
            tmp_path = Path(tf.name)
        try:
            loop = asyncio.get_running_loop()
            segments_iter, info = await loop.run_in_executor(
                None,
                lambda: self._model.transcribe(
                    str(tmp_path),
                    language=request.language,
                    initial_prompt=request.prompt or None,
                    word_timestamps=request.word_timestamps,
                ),
            )
            segments = [
                TranscriptSegment(
                    text=s.text.strip(),
                    start_seconds=float(s.start),
                    end_seconds=float(s.end),
                    speaker_label="",  # faster-whisper doesn't diarize
                    confidence=None,
                )
                for s in segments_iter
            ]
            full_text = " ".join(s.text for s in segments).strip()
            return TranscriptionResult(
                text=full_text,
                segments=segments,
                language=info.language or "",
                duration_seconds=float(info.duration) if getattr(info, "duration", None) else None,
                audio_seconds_used=float(info.duration) if getattr(info, "duration", None) else None,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    async def list_languages(self) -> list[str]:
        # faster-whisper supports the full Whisper language set. Keep this
        # short and informative; full list is upstream documentation.
        return [
            "auto", "en", "es", "fr", "de", "it", "pt", "nl", "ru",
            "zh", "ja", "ko", "ar", "hi", "tr", "pl", "uk", "sv",
        ]
```

- [ ] **Step 5: Verify the integration test passes (or skips cleanly)**

Run: `uv run pytest tests/integration/test_local_whisper.py -v`
Expected: PASS if a `hello_world.wav` fixture + model are available; otherwise SKIP. The test never fails as long as the import works.

- [ ] **Step 6: Re-run unit tests to make sure side-effect imports still work**

Run: `uv run pytest tests/unit/test_transcription_service.py tests/unit/test_transcription_interfaces.py -v`
Expected: all green; `LocalWhisperBackend` now appears in the registry.

- [ ] **Step 7: Lint and type-check**

Run:
```bash
uv run ruff check src/gilbert/integrations/local_whisper.py tests/integration/test_local_whisper.py
uv run mypy src/gilbert/integrations/local_whisper.py
```
Expected: clean (mypy may need `# type: ignore` on the `from faster_whisper import WhisperModel` line if no stubs ship).

- [ ] **Step 8: Commit**

```bash
git add src/gilbert/integrations/local_whisper.py tests/integration/test_local_whisper.py pyproject.toml uv.lock
git commit -m "transcription: bundled LocalWhisperBackend (faster-whisper)"
```

(If you generated a fixture WAV, `git add tests/integration/fixtures/hello_world.wav` too.)

---

## Task 13: Register the service in `app.py`

**Files:**
- Modify: `src/gilbert/core/app.py`

- [ ] **Step 1: Add the registration line**

In `src/gilbert/core/app.py` around line 207 (right next to `SpeakerService()`), add:

```python
        self.service_manager.register(TTSService())
        self.service_manager.register(SpeakerService())
        self.service_manager.register(MusicService())
+       from gilbert.core.services.transcription import TranscriptionService
+       self.service_manager.register(TranscriptionService())
        self.service_manager.register(LightsService())
```

(Place the import next to the registration line in the same local-import style other adjacent services already use — search for `from gilbert.core.services.scheduler import SchedulerService` for the pattern.)

- [ ] **Step 2: Find any get_service factory and add a parallel line**

Around line 575–579 (the section that returned `TTSService()` and `SpeakerService()` in factory form):

```python
+       if name == "transcription":
+           from gilbert.core.services.transcription import TranscriptionService
+           return TranscriptionService()
```

Match the surrounding return-style precisely — copy from the existing TTS / Speaker entries.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -x -q`
Expected: all tests pass.

- [ ] **Step 4: Smoke-start the app to confirm bootstrap works**

Run: `./gilbert.sh start --foreground` (or whatever the dev command is) for ~10 seconds, look for `Transcription service started` (when enabled in config) or `Transcription service disabled` (default). Kill it.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/app.py
git commit -m "transcription: register TranscriptionService in app bootstrap"
```

---

## Task 14: Architecture validation + docs

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Create: `docs/architecture/transcription-system.md`

- [ ] **Step 1: Run the architecture validator**

Use the validate-architecture skill in audit mode (or follow its categories manually). Fix any layer-import / concrete-class / capability-wiring / multi-user-isolation issues it surfaces. Expected: clean — the design was written to comply.

- [ ] **Step 2: Update `README.md` integrations table**

Add a row to the integrations table — find the row for "Text-to-speech (ElevenLabs)" and add an analogous "Speech-to-text (local Whisper, bundled)" row directly above or below it. Keep wording consistent with adjacent rows.

- [ ] **Step 3: Update `CLAUDE.md`**

In the "Key Directories" section's `interfaces/` enumeration, add `transcription.py` to the list (alphabetical order: between `tools.py` and `tts.py`).

- [ ] **Step 4: Write the architecture walkthrough**

Create `docs/architecture/transcription-system.md`:

```markdown
# Transcription System

Aggregator-style service that owns three sibling backend registries
(`BatchTranscriptionBackend`, `StreamingTranscriptionBackend`,
`WakeWordBackend`) and exposes a uniform routing API plus browser-mic
WS plumbing. Sibling-by-design to `SpeakerService` (output) and the
new multi-backend SpeakerService template.

## Why three ABCs

Batch and streaming providers have substantially different shapes
(one-shot bytes vs. open a session and stream events), and wake-word
engines (Porcupine, openWakeWord) are a different class of code from
transcription engines (Whisper, Deepgram). Treating them as three
sibling roles lets one class implement multiple roles (e.g., Deepgram
can subclass both `BatchTranscriptionBackend` and
`StreamingTranscriptionBackend`) while keeping the simple cases simple.

## Backend lifecycle

`TranscriptionService.start()` reads the `transcription` config section,
then calls `_reinit_backends_for_role(role)` for each role. The
reconciler:

- closes any backends that are no longer in `<role>.backends`,
- skips any whose `enabled` is `False`,
- instantiates and `initialize()`s any newly-enabled backends.

Startup errors are captured in `self._startup_failures[role][name]` so
the settings UI can surface them; one failing backend never prevents
others from coming up.

`on_config_changed()` calls the same reconciler — toggling a backend
on or off in settings reinitializes only that one backend, no service
restart.

## Routing

Each public method (`transcribe`, `open_stream`, `open_detector`)
takes an optional `backend=` kwarg. Resolution order:

1. The named backend, if loaded.
2. `self._default_<role>` from config, if loaded.
3. The single loaded backend, if exactly one.
4. `RuntimeError`.

This matches the AI-profile shape and keeps callers blissfully
ignorant of which provider is wired up.

## Browser-mic sessions

`TranscriptionService` is the `ws_handlers` provider for browser
voice control:

| RPC | Direction | Purpose |
|---|---|---|
| `transcription.start_session` | C→S | Opens a stream or wake-word session, returns `session_id` |
| `transcription.send_chunk`    | C→S | Base64-encoded audio chunk; pump task lazily started on first chunk |
| `transcription.close_session` | C→S | Closes the session early |
| `transcription.event`         | S→C | Forwarded transcript / wake events |

`session_id`s are server-minted UUIDs, opaque to clients. One WS
connection may hold N concurrent sessions; the session dictionary is
keyed by `session_id` and each record carries `conn_id` so disconnect
cleanup can find them by connection.

A connection drop fires `on_ws_disconnect`, which closes every session
attached to that `conn_id` — same cleanup pattern as
`BrowserSpeakerBackend`.

## Deliberate v1 omissions

These are listed in the spec for completeness — they are tracked, not
forgotten:

- **No wake-word orchestration helper** (no `listen_with_wake_word(...)`):
  primitives only. Will land once a real consumer exists so the
  helper's shape is informed by use.
- **No SPA voice panel:** WS RPCs work server-side; UI is a follow-up.
- **No transcript persistence:** `transcribe` writes a transient
  output file under `output_ttl_seconds` cleanup; no entity collection.
  If you want transcripts searchable, ingest them via the knowledge
  service instead.
- **No streaming `transcribe`:** the batch path loads the whole file.
  Fine for chat attachments and voicemails; revisit if a caller hits
  the limit.
- **No live translation, no per-user session caps, no usage dashboards.**
```

- [ ] **Step 5: Run the full suite one more time**

Run: `uv run pytest -x -q`
Expected: green.

- [ ] **Step 6: Commit docs**

```bash
git add README.md CLAUDE.md docs/architecture/transcription-system.md
git commit -m "transcription: README, CLAUDE.md, and architecture walkthrough"
```

---

## Plan complete

At this point:
- `transcription` is a registered, toggleable service with three role-aware backend registries.
- `LocalWhisperBackend` ships bundled so the service is usable out of the box.
- `/transcription transcribe` / `/transcription backends` / `/transcription languages` are wired in.
- WS RPCs are live for future browser voice UI work.
- Docs are current.

**Tracked follow-ups (not part of this PR):**
- `listen_with_wake_word(...)` orchestration helper (see memory entry `project-transcription-wakeword-followup`).
- SPA voice control panel.
- Std-plugin backends: OpenAI Whisper API, ElevenLabs Scribe (batch + streaming), Deepgram, Porcupine.
