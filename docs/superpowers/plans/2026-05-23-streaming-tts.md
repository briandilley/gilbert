# Streaming TTS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional streaming capabilities to TTS — one-shot streamed-out and bidirectional push-text/read-audio — without breaking any batch caller. STT streaming is already wired; this plan does not modify it.

**Architecture:** Two new `@runtime_checkable` capability protocols (`StreamingTTSCapability`, `BidirectionalTTSCapability`) opt-in on the existing `TTSBackend` class. `TTSService` gains capability-checked `synthesize_stream` / `open_stream` methods plus a `supported_capabilities()` query, and four new WS handlers (`tts.start_stream`, `tts.send_text`, `tts.flush`, `tts.close_stream`) mirroring transcription's WS pattern. ElevenLabs implements both protocols (HTTP chunked + websocket stream-input); Kokoro implements streaming via sentence-split.

**Tech Stack:** Python 3.12, asyncio, httpx (for HTTP streaming), websockets-via-httpx-or-aiohttp (for ElevenLabs bidirectional), pytest, uv workspace plugins.

**Spec:** `docs/superpowers/specs/2026-05-23-streaming-stt-tts-design.md`

---

## File Structure

**Core (new + modified):**
- `src/gilbert/interfaces/tts.py` — modified: new dataclasses, `TTSStream` ABC, two capability protocols, two consumer protocols, `TTSCapabilityError`.
- `src/gilbert/core/services/tts.py` — modified: three new methods (`synthesize_stream`, `open_stream`, `supported_capabilities`), `_ActiveTTSSession` dataclass, four new WS handlers, two pump methods, `_event_to_json` helper, `get_ws_handlers` override.

**Backends (modified):**
- `std-plugins/elevenlabs/elevenlabs_tts.py` — modified: `synthesize_stream` method, `open_stream` method, new `ElevenLabsTTSStream` class.
- `std-plugins/kokoro/kokoro_tts.py` — modified: `synthesize_stream` method with sentence-split.

**Tests (new):**
- `tests/unit/test_tts_streaming_interfaces.py` — protocol/dataclass shape tests.
- `tests/unit/test_tts_streaming_service.py` — service-level capability checks, padding, AI-injection retry.
- `tests/unit/test_tts_ws_streaming.py` — WS handler tests using the `_Conn` fake pattern from `test_transcription_service.py`.
- `std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py` — gated integration tests.
- `std-plugins/kokoro/tests/test_kokoro_streaming.py` — sentence-split unit test + gated integration test.

**Frontend (touched lightly):**
- `src/gilbert/web/spa/src/hooks/useTTSStream.ts` — new hook (exact path verified in Task 12).
- Existing TTS settings test button gains a "Stream" toggle.

---

## Task 1: Add interface dataclasses, ABC, and capability protocols

**Files:**
- Modify: `src/gilbert/interfaces/tts.py`
- Create: `tests/unit/test_tts_streaming_interfaces.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_streaming_interfaces.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_tts_streaming_interfaces.py -v
```

Expected: ImportError on `TTSStreamConfig` / `TTSAudioChunk` / etc.

- [ ] **Step 3: Add the types to `interfaces/tts.py`**

Append to `src/gilbert/interfaces/tts.py` (after the existing `AICapableTTSBackend` block):

```python
# ── Streaming TTS ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TTSStreamConfig:
    """Config for a bidirectional TTS session."""

    voice_id: str
    output_format: AudioFormat = AudioFormat.MP3
    speed: float = 1.0
    context: str = ""
    sample_rate: int = 44100   # PCM-only; phone-friendly preset is 8000


@dataclass(frozen=True)
class TTSAudioChunk:
    """Audio bytes emitted from a TTS stream."""
    audio: bytes


@dataclass(frozen=True)
class TTSWordTiming:
    """Word-level alignment metadata, if the backend reports it."""
    word: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class TTSFlushed:
    """Backend has finished synthesizing one flush boundary
    (i.e. all text sent before the last flush has been rendered)."""
    at_seconds: float


@dataclass(frozen=True)
class TTSStreamError:
    """Recoverable or fatal error mid-stream."""
    message: str
    recoverable: bool = False


TTSEvent = TTSAudioChunk | TTSWordTiming | TTSFlushed | TTSStreamError


class TTSStream(ABC):
    """A bidirectional TTS session opened by a backend.

    Producer pushes text via ``send_text``; consumer reads
    ``TTSEvent`` items from the ``events()`` async iterator.
    ``flush()`` tells the backend to start synthesizing the
    text buffered so far. ``close()`` signals end-of-input;
    ``events()`` still drains any final events the backend
    emits during shutdown.
    """

    @abstractmethod
    async def send_text(self, text: str) -> None: ...

    @abstractmethod
    async def flush(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[TTSEvent]: ...


@runtime_checkable
class StreamingTTSCapability(Protocol):
    """Optional capability on a ``TTSBackend``: one-shot text in,
    chunked audio out. Backends opt in by implementing
    ``synthesize_stream``."""

    def synthesize_stream(
        self, request: SynthesisRequest,
    ) -> AsyncIterator[bytes]: ...


@runtime_checkable
class BidirectionalTTSCapability(Protocol):
    """Optional capability on a ``TTSBackend``: push-text /
    read-audio session. Backends opt in by implementing
    ``open_stream``."""

    async def open_stream(self, config: TTSStreamConfig) -> "TTSStream": ...


# ── Consumer-facing capability protocols (mirror the above on the
#    service side so callers can depend on a Protocol, not the
#    concrete ``TTSService``).


@runtime_checkable
class StreamingTTSProvider(Protocol):
    def synthesize_stream(
        self, request: SynthesisRequest,
    ) -> AsyncIterator[bytes]: ...


@runtime_checkable
class BidirectionalTTSProvider(Protocol):
    async def open_stream(self, config: TTSStreamConfig) -> "TTSStream": ...


class TTSCapabilityError(RuntimeError):
    """Raised when a caller requests a TTS capability the active
    backend does not implement. Distinct from generic
    ``RuntimeError`` so callers can ``except TTSCapabilityError``
    and fall back to batch synthesis."""
```

Also add `AsyncIterator` to the existing import block at the top:

```python
from collections.abc import AsyncIterator
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_tts_streaming_interfaces.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Also run mypy and the full unit test suite to confirm nothing else broke**

```bash
uv run mypy src/gilbert/interfaces/tts.py
uv run pytest tests/unit/ -q
```

Expected: mypy clean for that file; existing unit tests still pass (additive change).

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/interfaces/tts.py tests/unit/test_tts_streaming_interfaces.py
git commit -m "feat(tts): add streaming TTS interface dataclasses and capability protocols

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Add `synthesize_stream` to `TTSService`

**Files:**
- Modify: `src/gilbert/core/services/tts.py`
- Create: `tests/unit/test_tts_streaming_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_streaming_service.py`:

```python
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

    chunks: list[bytes] = [b"AAA", b"BBB", b"CCC"]

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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_tts_streaming_service.py -v
```

Expected: AttributeError — `TTSService` has no `synthesize_stream`.

- [ ] **Step 3: Implement `synthesize_stream` on `TTSService`**

Add to `src/gilbert/core/services/tts.py`. Update the imports block:

```python
from gilbert.interfaces.tts import (
    AudioFormat,
    BidirectionalTTSCapability,
    StreamingTTSCapability,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    TTSCapabilityError,
    TTSStream,
    TTSStreamConfig,
    Voice,
    append_silence,
)
```

Also at the top of the file, add:

```python
from collections.abc import AsyncIterator
```

Then add a new method to `TTSService`, right after the existing `synthesize` method (around `core/services/tts.py:228`):

```python
def synthesize_stream(
    self, request: SynthesisRequest,
) -> AsyncIterator[bytes]:
    """Synthesize speech as a stream of audio chunks.

    Synchronous ``def`` (not ``async def``) so the capability check
    raises at the call site rather than on the consumer's first
    ``async for``. An async generator body wouldn't execute until
    first ``__anext__``; consumers would then see ``TTSCapabilityError``
    mid-iteration, which is confusing.

    Streaming bypasses the service's ``silence_padding`` — that's a
    finished-buffer concept and streaming consumers manage their
    own tail.
    """
    if self._backend is None:
        raise RuntimeError("TTS service is not enabled")
    if not isinstance(self._backend, StreamingTTSCapability):
        raise TTSCapabilityError(
            f"backend {self._backend_name!r} does not support streaming synthesis"
        )
    self._ensure_ai_injection()
    return self._backend.synthesize_stream(request)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_tts_streaming_service.py -v
uv run pytest tests/unit/test_tts_service.py -v  # existing tests must still pass
```

Expected: new tests pass; existing TTS service tests unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/tts.py tests/unit/test_tts_streaming_service.py
git commit -m "feat(tts): add synthesize_stream to TTSService

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Add `open_stream` and `supported_capabilities` to `TTSService`

**Files:**
- Modify: `src/gilbert/core/services/tts.py`
- Modify: `tests/unit/test_tts_streaming_service.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/unit/test_tts_streaming_service.py`:

```python
from gilbert.interfaces.tts import BidirectionalTTSCapability, TTSStream, TTSStreamConfig
from collections.abc import AsyncIterator


class _FakeBidirectionalStream(TTSStream):
    def __init__(self):
        self.sent: list[str] = []
        self.flushed = 0
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def flush(self) -> None:
        self.flushed += 1

    async def close(self) -> None:
        self.closed = True

    def events(self) -> AsyncIterator:
        async def _gen():
            if False:
                yield  # pragma: no cover — empty iterator
        return _gen()


class _BidirectionalBackend(_BatchOnlyBackend):
    last_config: TTSStreamConfig | None = None

    async def open_stream(self, config: TTSStreamConfig) -> TTSStream:
        type(self).last_config = config
        return _FakeBidirectionalStream()


@pytest.fixture
def svc_with_bidi() -> TTSService:
    svc = TTSService()
    svc._backend = _BidirectionalBackend()
    svc._backend_name = "_bidi_test"
    svc._enabled = True
    return svc


@pytest.mark.asyncio
async def test_open_stream_raises_when_backend_lacks_capability(svc_with_streaming):
    # _StreamingBackend implements streaming but NOT bidirectional.
    cfg = TTSStreamConfig(voice_id="v1")
    with pytest.raises(TTSCapabilityError) as ei:
        await svc_with_streaming.open_stream(cfg)
    assert "bidirectional" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_open_stream_returns_backend_stream(svc_with_bidi):
    cfg = TTSStreamConfig(voice_id="v1", output_format=AudioFormat.PCM, sample_rate=8000)
    stream = await svc_with_bidi.open_stream(cfg)
    assert isinstance(stream, TTSStream)
    assert _BidirectionalBackend.last_config == cfg


@pytest.mark.asyncio
async def test_open_stream_raises_when_backend_none():
    svc = TTSService()
    cfg = TTSStreamConfig(voice_id="v1")
    with pytest.raises(RuntimeError, match="TTS service is not enabled"):
        await svc.open_stream(cfg)


def test_supported_capabilities_batch_only(svc_with_batch_only):
    assert svc_with_batch_only.supported_capabilities() == frozenset({"batch"})


def test_supported_capabilities_streaming(svc_with_streaming):
    assert svc_with_streaming.supported_capabilities() == frozenset({"batch", "streaming"})


def test_supported_capabilities_bidirectional(svc_with_bidi):
    # _BidirectionalBackend inherits from _BatchOnlyBackend, so "streaming"
    # is NOT present unless that class also adds synthesize_stream.
    assert svc_with_bidi.supported_capabilities() == frozenset({"batch", "bidirectional"})


def test_supported_capabilities_with_no_backend():
    svc = TTSService()
    # No backend loaded → only batch reported, since batch is intrinsic
    # to TTSBackend.synthesize() being abstract. Document the chosen
    # behavior explicitly: no backend → empty set.
    assert svc.supported_capabilities() == frozenset()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_tts_streaming_service.py -v
```

Expected: AttributeError on `open_stream` and `supported_capabilities`.

- [ ] **Step 3: Implement on `TTSService`**

Add to `src/gilbert/core/services/tts.py` directly after the `synthesize_stream` method:

```python
async def open_stream(self, config: TTSStreamConfig) -> TTSStream:
    """Open a bidirectional TTS session. Raises ``TTSCapabilityError``
    if the active backend doesn't implement ``BidirectionalTTSCapability``."""
    if self._backend is None:
        raise RuntimeError("TTS service is not enabled")
    if not isinstance(self._backend, BidirectionalTTSCapability):
        raise TTSCapabilityError(
            f"backend {self._backend_name!r} does not support bidirectional streaming"
        )
    self._ensure_ai_injection()
    return await self._backend.open_stream(config)


def supported_capabilities(self) -> frozenset[str]:
    """Report which TTS capabilities the active backend supports.

    Returns ``frozenset()`` when no backend is loaded. Otherwise
    always includes ``"batch"`` (every TTSBackend implements
    ``synthesize``), plus ``"streaming"`` and/or ``"bidirectional"``
    if the backend opts into the matching protocol.
    """
    if self._backend is None:
        return frozenset()
    caps = {"batch"}
    if isinstance(self._backend, StreamingTTSCapability):
        caps.add("streaming")
    if isinstance(self._backend, BidirectionalTTSCapability):
        caps.add("bidirectional")
    return frozenset(caps)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_tts_streaming_service.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/tts.py tests/unit/test_tts_streaming_service.py
git commit -m "feat(tts): add open_stream and supported_capabilities to TTSService

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add `_event_to_json` helper and `_ActiveTTSSession` model

**Files:**
- Modify: `src/gilbert/core/services/tts.py`
- Create: `tests/unit/test_tts_ws_streaming.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_ws_streaming.py`:

```python
"""Tests for TTSService WebSocket streaming handlers and helpers."""

import asyncio
import base64

import pytest

from gilbert.core.services.tts import TTSService, _event_to_json
from gilbert.interfaces.tts import (
    AudioFormat,
    TTSAudioChunk,
    TTSFlushed,
    TTSStreamError,
    TTSWordTiming,
)


def test_event_to_json_audio_chunk_uses_base64():
    ev = TTSAudioChunk(audio=b"\x00\x01\x02")
    j = _event_to_json(ev, AudioFormat.MP3)
    assert j == {
        "type": "audio",
        "audio_b64": base64.b64encode(b"\x00\x01\x02").decode(),
        "format": "mp3",
    }


def test_event_to_json_word_timing():
    ev = TTSWordTiming(word="hi", start_seconds=0.10, end_seconds=0.30)
    assert _event_to_json(ev, AudioFormat.MP3) == {
        "type": "word",
        "word": "hi",
        "start_seconds": 0.10,
        "end_seconds": 0.30,
    }


def test_event_to_json_flushed():
    assert _event_to_json(TTSFlushed(at_seconds=2.5), AudioFormat.MP3) == {
        "type": "flushed",
        "at_seconds": 2.5,
    }


def test_event_to_json_error():
    assert _event_to_json(TTSStreamError("oops", recoverable=True), AudioFormat.MP3) == {
        "type": "error",
        "message": "oops",
        "recoverable": True,
    }


def test_event_to_json_unknown_returns_unknown_type():
    # Defensive: pass an arbitrary object that's not a TTSEvent variant.
    class _Other:
        pass
    assert _event_to_json(_Other(), AudioFormat.MP3) == {"type": "unknown"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v
```

Expected: ImportError on `_event_to_json`.

- [ ] **Step 3: Add the helper and session dataclass**

In `src/gilbert/core/services/tts.py`, add imports near the existing imports:

```python
import asyncio
import base64
from dataclasses import dataclass
```

Add a module-level helper above the `TTSService` class (after the imports block, before `class TTSService`):

```python
def _event_to_json(ev: object, fmt: AudioFormat) -> dict[str, Any]:
    """Encode a ``TTSEvent`` for the WS wire.

    Audio bytes are base64-encoded so the JSON frame stays text-safe.
    The ``fmt`` argument is the session's output format, embedded on
    audio frames so the SPA player knows how to decode."""
    from gilbert.interfaces.tts import (
        TTSAudioChunk,
        TTSFlushed,
        TTSStreamError,
        TTSWordTiming,
    )

    if isinstance(ev, TTSAudioChunk):
        return {
            "type": "audio",
            "audio_b64": base64.b64encode(ev.audio).decode(),
            "format": fmt.value,
        }
    if isinstance(ev, TTSWordTiming):
        return {
            "type": "word",
            "word": ev.word,
            "start_seconds": ev.start_seconds,
            "end_seconds": ev.end_seconds,
        }
    if isinstance(ev, TTSFlushed):
        return {"type": "flushed", "at_seconds": ev.at_seconds}
    if isinstance(ev, TTSStreamError):
        return {"type": "error", "message": ev.message, "recoverable": ev.recoverable}
    return {"type": "unknown"}


@dataclass
class _ActiveTTSSession:
    """Per-WS-connection TTS session state. Held only on
    ``TTSService._sessions``, never as request-scoped attrs on ``self``."""

    session_id: str
    conn_id: str
    user_id: str
    mode: str                  # "oneshot" | "bidirectional"
    fmt: AudioFormat           # session's output format (used by _event_to_json)
    primitive: TTSStream | None     # None for oneshot
    pump_task: asyncio.Task[None] | None = None
```

Add `_sessions` and `_sessions_guard` to `TTSService.__init__`:

```python
def __init__(self) -> None:
    self._backend: TTSBackend | None = None
    self._backend_name: str = "elevenlabs"
    self._enabled: bool = False
    self._config: dict[str, object] = {}
    self._silence_padding: float = 3.0
    self._output_ttl_seconds: int = 3600
    self._resolver: ServiceResolver | None = None
    self._ai_injected: bool = False
    # WS streaming sessions, keyed by session_id (UUID hex).
    self._sessions: dict[str, _ActiveTTSSession] = {}
    self._sessions_guard = asyncio.Lock()
```

(That is the existing `__init__` plus the last two lines.)

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v
uv run pytest tests/unit/test_tts_service.py -v
```

Expected: new tests pass, existing tests unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/tts.py tests/unit/test_tts_ws_streaming.py
git commit -m "feat(tts): add _event_to_json helper and _ActiveTTSSession model

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Add `tts.start_stream` WS handler — oneshot mode

**Files:**
- Modify: `src/gilbert/core/services/tts.py`
- Modify: `tests/unit/test_tts_ws_streaming.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tts_ws_streaming.py`:

```python
from collections.abc import AsyncIterator
from contextlib import suppress

from gilbert.core.services.tts import TTSService
from gilbert.interfaces.tts import StreamingTTSCapability, SynthesisRequest, SynthesisResult, TTSBackend, Voice


class _OneshotBackend(TTSBackend):
    """Backend that yields three audio chunks on synthesize_stream.

    No ``backend_name`` — keeps the global registry clean across tests."""

    chunks_to_emit = [b"AAA", b"BBB", b"CCC"]

    async def initialize(self, config): pass
    async def close(self): pass
    async def synthesize(self, request):
        return SynthesisResult(audio=b"".join(self.chunks_to_emit), format=request.output_format)
    async def list_voices(self):
        return []
    async def get_voice(self, voice_id):
        return None

    def synthesize_stream(self, request: SynthesisRequest) -> AsyncIterator[bytes]:
        chunks = list(self.chunks_to_emit)

        async def _gen():
            for c in chunks:
                yield c

        return _gen()


def _make_svc_with_oneshot_backend() -> TTSService:
    svc = TTSService()
    svc._backend = _OneshotBackend()
    svc._backend_name = "_oneshot_ws_test"
    svc._enabled = True
    svc._silence_padding = 0.0
    return svc


class _FakeConn:
    def __init__(self, conn_id: str = "c1", user_id: str = "u1"):
        self.connection_id = conn_id
        self._user_id = user_id
        self.enqueued: list[dict] = []
        self.close_cbs: list = []

    @property
    def user_id(self) -> str:
        return self._user_id

    def enqueue(self, msg):
        self.enqueued.append(msg)

    def add_close_callback(self, cb):
        self.close_cbs.append(cb)


@pytest.mark.asyncio
async def test_start_stream_oneshot_pumps_audio_and_end():
    svc = _make_svc_with_oneshot_backend()
    conn = _FakeConn()

    res = await svc._handle_start_stream(conn, {
        "type": "tts.start_stream",
        "mode": "oneshot",
        "format": "mp3",
        "voice_id": "v1",
        "text": "hello world",
    })
    assert "session_id" in res
    session_id = res["session_id"]

    # Drain the pump task.
    sess = svc._sessions[session_id]
    assert sess.pump_task is not None
    await sess.pump_task

    events = [m for m in conn.enqueued if m.get("type") == "tts.event"]
    assert len(events) == 4  # 3 audio + 1 end
    assert all(m["session_id"] == session_id for m in events)
    assert [e["event"]["type"] for e in events] == ["audio", "audio", "audio", "end"]
    audio_b64s = [base64.b64decode(e["event"]["audio_b64"]) for e in events[:3]]
    assert audio_b64s == [b"AAA", b"BBB", b"CCC"]
    # Session is cleaned up after pump finishes.
    assert session_id not in svc._sessions


@pytest.mark.asyncio
async def test_start_stream_oneshot_capability_error_emits_error_event_and_cleans_up():
    svc = TTSService()
    # Batch-only backend: lacks StreamingTTSCapability.
    class _BatchOnly(TTSBackend):
        async def initialize(self, config): pass
        async def close(self): pass
        async def synthesize(self, request):
            return SynthesisResult(audio=b"", format=request.output_format)
        async def list_voices(self): return []
        async def get_voice(self, vid): return None

    svc._backend = _BatchOnly()
    svc._backend_name = "_batch_only_ws_test"
    svc._enabled = True

    conn = _FakeConn()
    res = await svc._handle_start_stream(conn, {
        "type": "tts.start_stream",
        "mode": "oneshot",
        "format": "mp3",
        "voice_id": "v1",
        "text": "hello",
    })
    # Pre-pump capability check happens *inside* the pump, so the
    # handler still returns a session_id; the error surfaces as a
    # tts.event with type=error.
    session_id = res["session_id"]
    await svc._sessions[session_id].pump_task
    error_events = [
        m for m in conn.enqueued
        if m.get("type") == "tts.event" and m["event"]["type"] == "error"
    ]
    assert len(error_events) == 1
    assert "_batch_only_ws_test" in error_events[0]["event"]["message"]
    assert session_id not in svc._sessions
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v
```

Expected: AttributeError on `_handle_start_stream`.

- [ ] **Step 3: Implement `_handle_start_stream` and `_pump_oneshot`**

Add to `src/gilbert/core/services/tts.py` near the bottom of `TTSService` (before the `# --- ToolProvider protocol ---` comment block):

```python
# --- WsHandlerProvider --------------------------------------------

def get_ws_handlers(self) -> dict[str, Any]:
    return {
        "tts.start_stream":  self._handle_start_stream,
        "tts.send_text":     self._handle_send_text,
        "tts.flush":         self._handle_flush,
        "tts.close_stream":  self._handle_close_stream,
    }

async def _handle_start_stream(
    self, conn: Any, frame: dict[str, Any],
) -> dict[str, Any]:
    """Open a TTS stream session. Returns ``{"session_id": "..."}``.

    Pump runs in the background; audio events arrive as server-pushed
    ``tts.event`` frames. Capability errors are reported via a
    server-pushed ``error`` event after the session is opened so the
    SPA's event-handling code path stays uniform.
    """
    import uuid

    mode = frame.get("mode", "oneshot")
    fmt = AudioFormat(frame.get("format", "mp3"))
    voice_id = str(frame.get("voice_id", ""))
    speed = float(frame.get("speed", 1.0))
    context = str(frame.get("context", ""))

    session_id = uuid.uuid4().hex

    if mode == "oneshot":
        text = str(frame.get("text", ""))
        request = SynthesisRequest(
            text=text,
            voice_id=voice_id,
            output_format=fmt,
            speed=speed,
            context=context,
        )
        record = _ActiveTTSSession(
            session_id=session_id,
            conn_id=conn.connection_id,
            user_id=conn.user_id or "",
            mode=mode,
            fmt=fmt,
            primitive=None,
        )
        async with self._sessions_guard:
            self._sessions[session_id] = record

        def _on_close(sid: str = session_id) -> None:
            asyncio.create_task(self._close_session(sid))

        conn.add_close_callback(_on_close)

        import contextvars
        ctx = contextvars.copy_context()
        record.pump_task = asyncio.create_task(
            self._pump_oneshot(conn, record, request),
            name=f"tts-pump-oneshot-{session_id}",
            context=ctx,
        )
        return {"session_id": session_id}

    # "bidirectional" branch added in Task 6.
    return {"ok": False, "error": f"unknown stream mode {mode!r}"}


async def _pump_oneshot(
    self,
    conn: Any,
    rec: _ActiveTTSSession,
    request: SynthesisRequest,
) -> None:
    """Drain the backend's chunk iterator, emit ``tts.event`` frames,
    then a single ``end`` event. Capability errors become a single
    ``error`` event. Always cleans up the session record."""
    try:
        async for chunk in self.synthesize_stream(request):
            conn.enqueue({
                "type": "tts.event",
                "session_id": rec.session_id,
                "event": _event_to_json(
                    __import__("gilbert.interfaces.tts", fromlist=["TTSAudioChunk"]).TTSAudioChunk(audio=chunk),
                    rec.fmt,
                ),
            })
        conn.enqueue({
            "type": "tts.event",
            "session_id": rec.session_id,
            "event": {"type": "end"},
        })
    except TTSCapabilityError as e:
        conn.enqueue({
            "type": "tts.event",
            "session_id": rec.session_id,
            "event": {"type": "error", "message": str(e), "recoverable": False},
        })
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("tts oneshot pump error for session %s", rec.session_id)
        conn.enqueue({
            "type": "tts.event",
            "session_id": rec.session_id,
            "event": {"type": "error", "message": str(e), "recoverable": False},
        })
    finally:
        async with self._sessions_guard:
            self._sessions.pop(rec.session_id, None)


async def _close_session(self, session_id: str) -> None:
    """Tear down a session: cancel pump, close primitive, drop record."""
    async with self._sessions_guard:
        rec = self._sessions.pop(session_id, None)
    if rec is None:
        return
    if rec.pump_task is not None and not rec.pump_task.done():
        rec.pump_task.cancel()
    if rec.primitive is not None:
        try:
            await rec.primitive.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing TTS primitive for session %s", session_id)


# Stubs filled in by later tasks; provide them now so get_ws_handlers
# can return a complete map.

async def _handle_send_text(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "error": "bidirectional mode not yet implemented"}

async def _handle_flush(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "error": "bidirectional mode not yet implemented"}

async def _handle_close_stream(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    sid = frame.get("session_id")
    if not isinstance(sid, str):
        return {"ok": False, "error": "missing session_id"}
    await self._close_session(sid)
    return {"ok": True}
```

Also update `service_info()` to advertise the new `ws_handlers` capability — add it to the capabilities set:

```python
def service_info(self) -> ServiceInfo:
    return ServiceInfo(
        name="tts",
        capabilities=frozenset({"text_to_speech", "ai_tools", "ws_handlers"}),
        optional=frozenset({"configuration", "ai_chat"}),
        toggleable=True,
        toggle_description="Text-to-speech synthesis",
    )
```

Also add a small refinement: the `_pump_oneshot` body uses `__import__` to dodge the top-of-module import — replace that with a direct import added to the file's import block. Edit the existing import:

```python
from gilbert.interfaces.tts import (
    AudioFormat,
    BidirectionalTTSCapability,
    StreamingTTSCapability,
    SynthesisRequest,
    SynthesisResult,
    TTSAudioChunk,
    TTSBackend,
    TTSCapabilityError,
    TTSStream,
    TTSStreamConfig,
    Voice,
    append_silence,
)
```

And simplify the pump body:

```python
async for chunk in self.synthesize_stream(request):
    conn.enqueue({
        "type": "tts.event",
        "session_id": rec.session_id,
        "event": _event_to_json(TTSAudioChunk(audio=chunk), rec.fmt),
    })
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v
uv run pytest tests/unit/test_tts_service.py -v
```

Expected: new tests pass; existing TTS service tests unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/tts.py tests/unit/test_tts_ws_streaming.py
git commit -m "feat(tts): add tts.start_stream oneshot WS handler

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Add `tts.start_stream` bidirectional mode + `tts.send_text` / `tts.flush`

**Files:**
- Modify: `src/gilbert/core/services/tts.py`
- Modify: `tests/unit/test_tts_ws_streaming.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_tts_ws_streaming.py`:

```python
import asyncio as _asyncio  # alias to avoid name clash in tests
from collections.abc import AsyncIterator as _AsyncIter

from gilbert.interfaces.tts import (
    BidirectionalTTSCapability,
    TTSAudioChunk,
    TTSFlushed,
    TTSStream,
    TTSStreamConfig,
)


class _ScriptedTTSStream(TTSStream):
    """Records calls; emits a scripted event sequence on flush()."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = False
        self._event_queue: _asyncio.Queue = _asyncio.Queue()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def flush(self) -> None:
        # On each flush, push a TTSAudioChunk and a TTSFlushed.
        await self._event_queue.put(TTSAudioChunk(audio=b"AUDIO" + str(len(self.sent)).encode()))
        await self._event_queue.put(TTSFlushed(at_seconds=float(len(self.sent))))

    async def close(self) -> None:
        self.closed = True
        await self._event_queue.put(None)  # sentinel

    def events(self) -> _AsyncIter:
        q = self._event_queue

        async def _gen():
            while True:
                ev = await q.get()
                if ev is None:
                    return
                yield ev

        return _gen()


class _BidiBackend(TTSBackend):
    last_stream: _ScriptedTTSStream | None = None

    async def initialize(self, config): pass
    async def close(self): pass
    async def synthesize(self, request):
        return SynthesisResult(audio=b"", format=request.output_format)
    async def list_voices(self): return []
    async def get_voice(self, vid): return None

    async def open_stream(self, config: TTSStreamConfig) -> TTSStream:
        s = _ScriptedTTSStream()
        type(self).last_stream = s
        return s


def _make_svc_with_bidi() -> TTSService:
    svc = TTSService()
    svc._backend = _BidiBackend()
    svc._backend_name = "_bidi_ws_test"
    svc._enabled = True
    return svc


@pytest.mark.asyncio
async def test_start_stream_bidirectional_opens_session_and_pumps_events():
    svc = _make_svc_with_bidi()
    conn = _FakeConn()
    res = await svc._handle_start_stream(conn, {
        "type": "tts.start_stream",
        "mode": "bidirectional",
        "format": "mp3",
        "voice_id": "v1",
    })
    sid = res["session_id"]
    # send_text then flush — backend will push audio + flushed events.
    await svc._handle_send_text(conn, {"session_id": sid, "text": "hello"})
    await svc._handle_flush(conn, {"session_id": sid})
    # Drain at most 50 ms — enough for the pump to copy queued events.
    for _ in range(20):
        await _asyncio.sleep(0.005)
        if len([m for m in conn.enqueued if m.get("type") == "tts.event"]) >= 2:
            break
    events = [m for m in conn.enqueued if m.get("type") == "tts.event"]
    types = [m["event"]["type"] for m in events]
    assert "audio" in types
    assert "flushed" in types
    # Session is still open until close_stream.
    assert sid in svc._sessions
    await svc._handle_close_stream(conn, {"session_id": sid})
    assert sid not in svc._sessions
    assert _BidiBackend.last_stream.closed is True
    assert _BidiBackend.last_stream.sent == ["hello"]


@pytest.mark.asyncio
async def test_send_text_unknown_session_returns_error():
    svc = _make_svc_with_bidi()
    conn = _FakeConn()
    res = await svc._handle_send_text(conn, {"session_id": "nope", "text": "x"})
    assert res == {"ok": False, "error": "unknown session"}


@pytest.mark.asyncio
async def test_send_text_wrong_connection_rejected():
    svc = _make_svc_with_bidi()
    conn_a = _FakeConn(conn_id="A")
    conn_b = _FakeConn(conn_id="B")
    res = await svc._handle_start_stream(conn_a, {
        "type": "tts.start_stream", "mode": "bidirectional",
        "format": "mp3", "voice_id": "v1",
    })
    sid = res["session_id"]
    # Different connection tries to send on A's session → rejected.
    bad = await svc._handle_send_text(conn_b, {"session_id": sid, "text": "x"})
    assert bad == {"ok": False, "error": "unknown session"}


@pytest.mark.asyncio
async def test_close_session_on_socket_drop_cleans_up():
    svc = _make_svc_with_bidi()
    conn = _FakeConn()
    res = await svc._handle_start_stream(conn, {
        "type": "tts.start_stream", "mode": "bidirectional",
        "format": "mp3", "voice_id": "v1",
    })
    sid = res["session_id"]
    assert len(conn.close_cbs) == 1
    # Fire the close callback (simulating socket drop).
    conn.close_cbs[0]()
    # Callback schedules an async cleanup; await it.
    await _asyncio.sleep(0)
    await _asyncio.sleep(0)
    assert sid not in svc._sessions


@pytest.mark.asyncio
async def test_start_stream_bidirectional_no_capability_returns_error_response():
    svc = TTSService()
    # Streaming-only backend (no BidirectionalTTSCapability).
    svc._backend = _OneshotBackend()
    svc._backend_name = "_oneshot_ws_test"
    svc._enabled = True
    conn = _FakeConn()
    res = await svc._handle_start_stream(conn, {
        "type": "tts.start_stream", "mode": "bidirectional",
        "format": "mp3", "voice_id": "v1",
    })
    assert res.get("ok") is False
    assert "bidirectional" in res.get("error", "").lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v -k "bidirectional or send_text or close_session"
```

Expected: the stub `_handle_send_text` / `_handle_flush` returning "not yet implemented" causes the assertions to fail.

- [ ] **Step 3: Implement bidirectional mode**

Replace the `_handle_start_stream` body in `src/gilbert/core/services/tts.py` to handle both modes. Replace the placeholder branch at `# "bidirectional" branch added in Task 6.` with a real implementation, and replace the stub `_handle_send_text` / `_handle_flush` methods. Here's the full final shape (use as the replacement):

```python
async def _handle_start_stream(
    self, conn: Any, frame: dict[str, Any],
) -> dict[str, Any]:
    """Open a TTS stream session. Mode is ``oneshot`` (text in frame,
    audio out via tts.event) or ``bidirectional`` (push text via
    tts.send_text and tts.flush, audio out via tts.event)."""
    import contextvars
    import uuid

    mode = frame.get("mode", "oneshot")
    fmt = AudioFormat(frame.get("format", "mp3"))
    voice_id = str(frame.get("voice_id", ""))
    speed = float(frame.get("speed", 1.0))
    context = str(frame.get("context", ""))
    sample_rate = int(frame.get("sample_rate", 44100))
    session_id = uuid.uuid4().hex

    if mode == "oneshot":
        text = str(frame.get("text", ""))
        request = SynthesisRequest(
            text=text, voice_id=voice_id, output_format=fmt,
            speed=speed, context=context,
        )
        record = _ActiveTTSSession(
            session_id=session_id, conn_id=conn.connection_id,
            user_id=conn.user_id or "", mode=mode, fmt=fmt, primitive=None,
        )
        async with self._sessions_guard:
            self._sessions[session_id] = record
        conn.add_close_callback(
            lambda sid=session_id: asyncio.create_task(self._close_session(sid))
        )
        ctx = contextvars.copy_context()
        record.pump_task = asyncio.create_task(
            self._pump_oneshot(conn, record, request),
            name=f"tts-pump-oneshot-{session_id}",
            context=ctx,
        )
        return {"session_id": session_id}

    if mode == "bidirectional":
        cfg = TTSStreamConfig(
            voice_id=voice_id, output_format=fmt, speed=speed,
            context=context, sample_rate=sample_rate,
        )
        try:
            primitive = await self.open_stream(cfg)
        except TTSCapabilityError as e:
            return {"ok": False, "error": str(e)}
        record = _ActiveTTSSession(
            session_id=session_id, conn_id=conn.connection_id,
            user_id=conn.user_id or "", mode=mode, fmt=fmt,
            primitive=primitive,
        )
        async with self._sessions_guard:
            self._sessions[session_id] = record
        conn.add_close_callback(
            lambda sid=session_id: asyncio.create_task(self._close_session(sid))
        )
        ctx = contextvars.copy_context()
        record.pump_task = asyncio.create_task(
            self._pump_bidirectional(conn, record),
            name=f"tts-pump-bidi-{session_id}",
            context=ctx,
        )
        return {"session_id": session_id}

    return {"ok": False, "error": f"unknown stream mode {mode!r}"}


async def _pump_bidirectional(self, conn: Any, rec: _ActiveTTSSession) -> None:
    """Drain ``primitive.events()`` and push ``tts.event`` frames.
    Cleanup happens via ``_close_session`` (on socket drop or explicit
    close), not here — the pump just relays events."""
    assert rec.primitive is not None
    try:
        async for ev in rec.primitive.events():
            conn.enqueue({
                "type": "tts.event",
                "session_id": rec.session_id,
                "event": _event_to_json(ev, rec.fmt),
            })
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("tts bidi pump error for session %s", rec.session_id)
        conn.enqueue({
            "type": "tts.event",
            "session_id": rec.session_id,
            "event": {"type": "error", "message": str(e), "recoverable": False},
        })


async def _handle_send_text(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    sid = frame.get("session_id")
    text = frame.get("text")
    if not isinstance(sid, str) or not isinstance(text, str):
        return {"ok": False, "error": "missing session_id or text"}
    rec = self._sessions.get(sid)
    if rec is None or rec.conn_id != conn.connection_id or rec.primitive is None:
        return {"ok": False, "error": "unknown session"}
    await rec.primitive.send_text(text)
    return {"ok": True}


async def _handle_flush(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    sid = frame.get("session_id")
    if not isinstance(sid, str):
        return {"ok": False, "error": "missing session_id"}
    rec = self._sessions.get(sid)
    if rec is None or rec.conn_id != conn.connection_id or rec.primitive is None:
        return {"ok": False, "error": "unknown session"}
    await rec.primitive.flush()
    return {"ok": True}


async def _handle_close_stream(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    sid = frame.get("session_id")
    if not isinstance(sid, str):
        return {"ok": False, "error": "missing session_id"}
    rec = self._sessions.get(sid)
    if rec is not None and rec.conn_id != conn.connection_id:
        return {"ok": False, "error": "unknown session"}
    await self._close_session(sid)
    return {"ok": True}
```

(Delete the old stubs from Task 5 — these are their final versions.)

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v
uv run pytest tests/unit/test_tts_service.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/tts.py tests/unit/test_tts_ws_streaming.py
git commit -m "feat(tts): add bidirectional WS streaming handlers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Update `TTSService.stop()` to clean up sessions

**Files:**
- Modify: `src/gilbert/core/services/tts.py`
- Modify: `tests/unit/test_tts_ws_streaming.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_tts_ws_streaming.py`:

```python
@pytest.mark.asyncio
async def test_stop_cancels_pending_sessions():
    svc = _make_svc_with_bidi()
    conn = _FakeConn()
    res = await svc._handle_start_stream(conn, {
        "type": "tts.start_stream", "mode": "bidirectional",
        "format": "mp3", "voice_id": "v1",
    })
    sid = res["session_id"]
    assert sid in svc._sessions
    await svc.stop()
    # All sessions gone after stop().
    assert svc._sessions == {}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py::test_stop_cancels_pending_sessions -v
```

Expected: assertion failure — current `stop()` only closes the backend, leaves sessions in place.

- [ ] **Step 3: Update `stop()`**

In `src/gilbert/core/services/tts.py`, replace the existing `stop` method:

```python
async def stop(self) -> None:
    # Tear down any open WS streaming sessions before closing the
    # backend — otherwise pump tasks would call into a closed backend
    # and the close callbacks would race with backend.close().
    async with self._sessions_guard:
        session_ids = list(self._sessions.keys())
    for sid in session_ids:
        await self._close_session(sid)
    if self._backend is not None:
        await self._backend.close()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_tts_ws_streaming.py -v
uv run pytest tests/unit/test_tts_service.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/tts.py tests/unit/test_tts_ws_streaming.py
git commit -m "feat(tts): clean up streaming sessions on service stop

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Kokoro `synthesize_stream` (sentence-split, simplest backend)

**Files:**
- Modify: `std-plugins/kokoro/kokoro_tts.py`
- Create: `std-plugins/kokoro/tests/test_kokoro_streaming.py`

> **Note:** `std-plugins/` is a git submodule — commits inside it live on the plugin repo's branch. Run all commands from the Gilbert root; commits in this task happen from inside `std-plugins/kokoro/` after you `cd` into the submodule. After the submodule commit, also bump the submodule pointer in the Gilbert repo (covered in Step 6).

- [ ] **Step 1: Write the failing test**

Create `std-plugins/kokoro/tests/test_kokoro_streaming.py`:

```python
"""Streaming TTS tests for the Kokoro backend.

Unit tests patch the pipeline; the integration test (gated) actually
loads the model and synthesizes audio. Run gated tests with:

    RUN_SLOW=1 uv run pytest std-plugins/kokoro/tests/test_kokoro_streaming.py -m slow
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import patch

import numpy as np
import pytest

from gilbert.interfaces.tts import (
    AudioFormat,
    StreamingTTSCapability,
    SynthesisRequest,
)
from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend


def test_kokoro_implements_streaming_capability():
    backend = KokoroTTSBackend()
    assert isinstance(backend, StreamingTTSCapability)


@pytest.mark.asyncio
async def test_synthesize_stream_yields_one_chunk_per_sentence():
    """Three sentences in → three chunks out."""
    backend = KokoroTTSBackend()
    await backend.initialize({"device": "cpu", "default_voice": "af_heart"})

    # Patch the pipeline builder to return a stub.
    class _StubPipeline:
        def __call__(self, text, voice, speed):
            # Yield one fake audio sample per call; the count differs
            # per text so we can assert ordering.
            yield (None, None, np.full(int(len(text) * 10), 0.5, dtype=np.float32))

    with patch("gilbert_plugin_kokoro.kokoro_tts._build_pipeline", return_value=_StubPipeline()):
        req = SynthesisRequest(
            text="First sentence. Second sentence. Third!",
            voice_id="af_heart",
            output_format=AudioFormat.PCM,
        )
        chunks: list[bytes] = []
        async for c in backend.synthesize_stream(req):
            chunks.append(c)
    # Sentence-splitter must produce exactly three non-empty chunks.
    assert len(chunks) == 3
    assert all(len(c) > 0 for c in chunks)


@pytest.mark.asyncio
async def test_synthesize_stream_handles_single_sentence():
    backend = KokoroTTSBackend()
    await backend.initialize({"device": "cpu", "default_voice": "af_heart"})

    class _StubPipeline:
        def __call__(self, text, voice, speed):
            yield (None, None, np.full(100, 0.5, dtype=np.float32))

    with patch("gilbert_plugin_kokoro.kokoro_tts._build_pipeline", return_value=_StubPipeline()):
        req = SynthesisRequest(
            text="Only one sentence",  # no terminal punctuation
            voice_id="af_heart",
            output_format=AudioFormat.PCM,
        )
        chunks: list[bytes] = []
        async for c in backend.synthesize_stream(req):
            chunks.append(c)
    assert len(chunks) == 1


@pytest.mark.slow
@pytest.mark.asyncio
async def test_synthesize_stream_real_model():
    if not os.environ.get("RUN_SLOW"):
        pytest.skip("RUN_SLOW=1 required")
    backend = KokoroTTSBackend()
    await backend.initialize({"device": "cpu", "default_voice": "af_heart"})
    req = SynthesisRequest(
        text="Hello. How are you? I am fine.",
        voice_id="af_heart",
        output_format=AudioFormat.MP3,
    )
    chunks: list[bytes] = []
    async for c in backend.synthesize_stream(req):
        chunks.append(c)
    assert len(chunks) == 3
    assert all(len(c) > 100 for c in chunks)  # each MP3-encoded chunk has a header at minimum
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest std-plugins/kokoro/tests/test_kokoro_streaming.py -v -k "not slow"
```

Expected: AttributeError — backend has no `synthesize_stream`.

- [ ] **Step 3: Add `synthesize_stream` to `KokoroTTSBackend`**

In `std-plugins/kokoro/kokoro_tts.py`, add `re` to the imports at the top:

```python
import re
```

And `AsyncIterator`:

```python
from collections.abc import AsyncIterator
```

Add a module-level helper near the other helpers:

```python
# Sentence-splitter: terminal . ! ? optionally followed by quote, then
# whitespace, OR end-of-string. Trailing fragments without terminal
# punctuation are kept as a final chunk. Tuned for English; non-English
# voices still produce sensible-enough boundaries since the regex
# matches the same Latin punctuation other Kokoro languages use.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])["\')\]]?\s+')


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into a list of non-empty sentence-ish chunks."""
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]
```

Then add the method to `KokoroTTSBackend` (immediately after `synthesize`):

```python
def synthesize_stream(
    self, request: SynthesisRequest,
) -> AsyncIterator[bytes]:
    """Stream audio sentence-by-sentence.

    Splits the input text on sentence boundaries and yields each
    sentence's encoded audio as a separate chunk. The speaker hears
    sentence 1 while sentence 2 renders, which materially improves
    perceived latency on long replies — even though kokoro is local
    CPU inference."""
    if request.voice_id not in _VOICES_BY_ID:
        raise ValueError(f"Unknown Kokoro voice: {request.voice_id!r}")
    lang = _lang_code_for_voice(request.voice_id)
    speed = float(request.speed) if request.speed else self._speed
    sentences = _split_sentences(request.text) or [request.text]

    async def _gen() -> AsyncIterator[bytes]:
        pipeline = self._get_pipeline(lang)
        loop = asyncio.get_running_loop()
        for sentence in sentences:
            def _run_sync(s: str = sentence) -> np.ndarray:
                chunks: list[np.ndarray] = []
                for _g, _p, audio in pipeline(s, voice=request.voice_id, speed=speed):
                    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
                    chunks.append(arr)
                if not chunks:
                    return np.zeros(0, dtype=np.float32)
                return np.concatenate(chunks)
            samples = await loop.run_in_executor(None, _run_sync)
            yield _encode(samples, request.output_format)

    return _gen()
```

- [ ] **Step 4: Run unit tests**

```bash
uv run pytest std-plugins/kokoro/tests/test_kokoro_streaming.py -v -k "not slow"
```

Expected: 3 pass, 1 skipped (`test_synthesize_stream_real_model`).

- [ ] **Step 5: (Optional) Run the slow integration test if you have the model cached**

```bash
RUN_SLOW=1 uv run pytest std-plugins/kokoro/tests/test_kokoro_streaming.py::test_synthesize_stream_real_model -v
```

Expected: passes if the model is available; skip otherwise.

- [ ] **Step 6: Commit in the submodule and bump the pointer**

```bash
# 6a — submodule commit
cd std-plugins
git add kokoro/kokoro_tts.py kokoro/tests/test_kokoro_streaming.py
git commit -m "feat(kokoro): add StreamingTTSCapability via sentence-split

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
cd ..

# 6b — bump submodule pointer in the Gilbert repo
git add std-plugins
git commit -m "kokoro: bump submodule for streaming TTS support

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: ElevenLabs `synthesize_stream` (HTTP chunked)

**Files:**
- Modify: `std-plugins/elevenlabs/elevenlabs_tts.py`
- Create: `std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py`

- [ ] **Step 1: Write the failing test**

Create `std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py`:

```python
"""Streaming TTS tests for the ElevenLabs backend."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.interfaces.tts import (
    AudioFormat,
    StreamingTTSCapability,
    SynthesisRequest,
)
from gilbert_plugin_elevenlabs.elevenlabs_tts import ElevenLabsTTS


def test_elevenlabs_implements_streaming_capability():
    backend = ElevenLabsTTS()
    assert isinstance(backend, StreamingTTSCapability)


@pytest.mark.asyncio
async def test_synthesize_stream_yields_chunks_via_http_streaming():
    backend = ElevenLabsTTS()

    # Skip the real initialize — fake the client directly.
    backend._voice_id = "v1"
    backend._model_id = "eleven_v3"
    backend._client = MagicMock()

    # Fake httpx streaming response.
    class _Resp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def raise_for_status(self): pass
        async def aiter_bytes(self, chunk_size=None):
            for c in (b"AAA", b"BBB", b"CCC"):
                yield c

    backend._client.stream = MagicMock(return_value=_Resp())

    req = SynthesisRequest(text="hello", voice_id="v1", output_format=AudioFormat.MP3)
    chunks: list[bytes] = []
    async for c in backend.synthesize_stream(req):
        chunks.append(c)
    assert chunks == [b"AAA", b"BBB", b"CCC"]
    # Streaming endpoint used — not the non-stream POST.
    backend._client.stream.assert_called_once()
    args, kwargs = backend._client.stream.call_args
    assert args[0] == "POST"
    assert "/text-to-speech/v1/stream" in args[1]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_synthesize_stream_real_api():
    if not os.environ.get("RUN_SLOW"):
        pytest.skip("RUN_SLOW=1 required")
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        pytest.skip("ELEVENLABS_API_KEY required")
    voice_id = os.environ.get("ELEVENLABS_TEST_VOICE_ID", "")
    if not voice_id:
        pytest.skip("ELEVENLABS_TEST_VOICE_ID required")
    backend = ElevenLabsTTS()
    await backend.initialize({"api_key": api_key, "voice_id": voice_id, "model_id": "eleven_v3"})
    try:
        req = SynthesisRequest(text="Hello world.", voice_id=voice_id, output_format=AudioFormat.MP3)
        chunks: list[bytes] = []
        async for c in backend.synthesize_stream(req):
            chunks.append(c)
        # At least 2 chunks and total non-trivial size.
        assert len(chunks) >= 2
        assert sum(len(c) for c in chunks) > 1000
    finally:
        await backend.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py -v -k "not slow"
```

Expected: AttributeError — backend has no `synthesize_stream`.

- [ ] **Step 3: Add `synthesize_stream` to `ElevenLabsTTS`**

In `std-plugins/elevenlabs/elevenlabs_tts.py`, add `AsyncIterator`:

```python
from collections.abc import AsyncIterator
```

Add the method on `ElevenLabsTTS` (after the existing `synthesize` method):

```python
def synthesize_stream(
    self, request: SynthesisRequest,
) -> AsyncIterator[bytes]:
    """Stream MP3/PCM audio chunks via the ElevenLabs streaming endpoint.

    Skips the local response cache — streaming is intended for
    long replies where the caller wants minimal first-byte latency
    anyway. Skips audio-tag injection too; the director model would
    block first-byte latency on its own round-trip. Callers that
    want tagged audio should use ``synthesize`` instead."""
    if not request.voice_id:
        if self._voice_id:
            request = SynthesisRequest(
                text=request.text, voice_id=self._voice_id,
                output_format=request.output_format, speed=request.speed,
                stability=request.stability, similarity_boost=request.similarity_boost,
                context=request.context,
            )
        else:
            raise ValueError("No voice_id configured — set voice_id in TTS backend settings")
    client = self._require_client()
    output_format = _FORMAT_MAP.get(request.output_format, "mp3_44100_128")
    body: dict[str, Any] = {"text": request.text, "model_id": self._model_id}
    voice_settings: dict[str, float] = {}
    if request.stability is not None:
        voice_settings["stability"] = request.stability
    if request.similarity_boost is not None:
        voice_settings["similarity_boost"] = request.similarity_boost
    if voice_settings:
        body["voice_settings"] = voice_settings

    async def _gen() -> AsyncIterator[bytes]:
        async with client.stream(
            "POST",
            f"/text-to-speech/{request.voice_id}/stream",
            json=body,
            params={"output_format": output_format},
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk

    return _gen()
```

- [ ] **Step 4: Run unit tests**

```bash
uv run pytest std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py -v -k "not slow"
```

Expected: 2 pass, 1 skipped.

- [ ] **Step 5: Commit in submodule and bump pointer**

```bash
cd std-plugins
git add elevenlabs/elevenlabs_tts.py elevenlabs/tests/test_elevenlabs_streaming.py
git commit -m "feat(elevenlabs): add StreamingTTSCapability via HTTP streaming endpoint

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
cd ..
git add std-plugins
git commit -m "elevenlabs: bump submodule for streaming TTS

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: ElevenLabs `BidirectionalTTSCapability` (websocket stream-input)

**Files:**
- Modify: `std-plugins/elevenlabs/elevenlabs_tts.py`
- Modify: `std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py`
- Modify: `std-plugins/elevenlabs/pyproject.toml` — add `websockets` dep if not present

- [ ] **Step 1: Check pyproject.toml for websocket dep**

```bash
grep -n 'websockets' std-plugins/elevenlabs/pyproject.toml
```

If not present, add the dep:

```toml
dependencies = [
    "httpx>=0.27",
    "websockets>=12.0",
    # ...existing deps
]
```

Then `uv sync`.

- [ ] **Step 2: Write the failing test**

Append to `std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py`:

```python
import asyncio
import json as _json
from unittest.mock import AsyncMock

from gilbert.interfaces.tts import (
    BidirectionalTTSCapability,
    TTSAudioChunk,
    TTSFlushed,
    TTSStream,
    TTSStreamConfig,
    TTSWordTiming,
)


def test_elevenlabs_implements_bidirectional_capability():
    backend = ElevenLabsTTS()
    assert isinstance(backend, BidirectionalTTSCapability)


class _FakeWS:
    """Minimal fake of a websockets connection."""

    def __init__(self, scripted_recv: list[str]):
        self.sent: list[str] = []
        self._recv = list(scripted_recv)
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if not self._recv:
            await asyncio.sleep(0.005)
            raise StopAsyncIteration
        return self._recv.pop(0)

    async def close(self, *args, **kwargs) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_open_stream_returns_tts_stream(monkeypatch):
    backend = ElevenLabsTTS()
    backend._api_key = "test-key"
    backend._model_id = "eleven_v3"

    # Fake out the websocket connection.
    fake_ws = _FakeWS(scripted_recv=[])
    async def _fake_connect(*args, **kwargs):
        return fake_ws

    import gilbert_plugin_elevenlabs.elevenlabs_tts as mod
    monkeypatch.setattr(mod, "_open_stream_input_ws", AsyncMock(return_value=fake_ws))

    cfg = TTSStreamConfig(voice_id="v1", output_format=AudioFormat.MP3)
    stream = await backend.open_stream(cfg)
    assert isinstance(stream, TTSStream)
    # The opening "initialize voice settings" frame was sent.
    assert any('"text"' in s for s in fake_ws.sent)
    await stream.close()


@pytest.mark.asyncio
async def test_stream_send_text_and_flush_send_correct_frames(monkeypatch):
    backend = ElevenLabsTTS()
    backend._api_key = "test-key"
    backend._model_id = "eleven_v3"

    fake_ws = _FakeWS(scripted_recv=[])
    import gilbert_plugin_elevenlabs.elevenlabs_tts as mod
    monkeypatch.setattr(mod, "_open_stream_input_ws", AsyncMock(return_value=fake_ws))

    cfg = TTSStreamConfig(voice_id="v1")
    stream = await backend.open_stream(cfg)
    await stream.send_text("hello")
    await stream.flush()
    # Last two frames: one with the text, one with the flush marker.
    payloads = [_json.loads(s) for s in fake_ws.sent[-2:]]
    assert payloads[0].get("text") == "hello"
    # ElevenLabs' stream-input WS treats an empty-string text as the flush marker.
    assert payloads[1].get("text") == ""
    assert payloads[1].get("flush") is True
    await stream.close()


@pytest.mark.asyncio
async def test_stream_events_decodes_audio_and_alignment_frames(monkeypatch):
    import base64 as _b64
    backend = ElevenLabsTTS()
    backend._api_key = "test-key"
    backend._model_id = "eleven_v3"

    # Scripted server frames: one audio, one alignment, then close marker.
    fake_ws = _FakeWS(scripted_recv=[
        _json.dumps({"audio": _b64.b64encode(b"BYTES1").decode()}),
        _json.dumps({"normalizedAlignment": {
            "chars": ["h", "i"],
            "charStartTimesMs": [0, 100],
            "charDurationsMs": [80, 200],
        }}),
        _json.dumps({"isFinal": True}),
    ])
    import gilbert_plugin_elevenlabs.elevenlabs_tts as mod
    monkeypatch.setattr(mod, "_open_stream_input_ws", AsyncMock(return_value=fake_ws))

    cfg = TTSStreamConfig(voice_id="v1")
    stream = await backend.open_stream(cfg)
    collected: list = []
    async for ev in stream.events():
        collected.append(ev)
    assert any(isinstance(e, TTSAudioChunk) and e.audio == b"BYTES1" for e in collected)
    assert any(isinstance(e, TTSWordTiming) for e in collected)
    await stream.close()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py -v -k "bidirectional or open_stream or stream_send_text or stream_events"
```

Expected: AttributeError on `open_stream` / `_open_stream_input_ws`.

- [ ] **Step 4: Add the `ElevenLabsTTSStream` class and `open_stream`**

In `std-plugins/elevenlabs/elevenlabs_tts.py`, add imports near the top:

```python
import asyncio
import base64 as _b64
import json as _json
from collections.abc import AsyncIterator
```

Add the WS-open helper near the top of the file:

```python
async def _open_stream_input_ws(
    *,
    voice_id: str,
    api_key: str,
    model_id: str,
    output_format: str,
):
    """Open the ElevenLabs stream-input WebSocket. Returns the
    connected websocket. Isolated for tests to patch."""
    import websockets

    url = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
        f"?model_id={model_id}&output_format={output_format}"
    )
    return await websockets.connect(
        url,
        additional_headers={"xi-api-key": api_key},
        max_size=None,
    )
```

Add the `ElevenLabsTTSStream` class — place it just above the `ElevenLabsTTS` class definition:

```python
class ElevenLabsTTSStream(TTSStream):
    """Bidirectional TTS session wrapping the stream-input WebSocket.

    Frame mapping:
      - ``{"audio": "<base64>"}`` → ``TTSAudioChunk``
      - ``{"normalizedAlignment": {...}}`` → one ``TTSWordTiming`` per
        whitespace-delimited word reassembled from the character spans
      - ``{"isFinal": true}`` → terminates the events iterator
      - Anything else → ignored
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False
        # Cross-task queue so send_text/flush callers don't block on
        # the recv loop and vice versa. Pump task fills this from
        # ``self._ws.recv()`` and the consumer drains via ``events()``.
        self._events: asyncio.Queue = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._pump_recv())

    async def send_text(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("stream is closed")
        await self._ws.send(_json.dumps({"text": text}))

    async def flush(self) -> None:
        if self._closed:
            raise RuntimeError("stream is closed")
        # Per ElevenLabs docs: an empty-text frame with flush=true
        # triggers synthesis of buffered text.
        await self._ws.send(_json.dumps({"text": "", "flush": True}))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Polite close: signal end-of-input via an empty-string text.
        try:
            await self._ws.send(_json.dumps({"text": ""}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        if not self._pump_task.done():
            self._pump_task.cancel()
        await self._events.put(None)  # sentinel

    def events(self) -> AsyncIterator:
        q = self._events

        async def _gen():
            while True:
                ev = await q.get()
                if ev is None:
                    return
                yield ev

        return _gen()

    async def _pump_recv(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                msg = _json.loads(raw)
                if "audio" in msg and msg["audio"]:
                    await self._events.put(TTSAudioChunk(audio=_b64.b64decode(msg["audio"])))
                if "normalizedAlignment" in msg:
                    align = msg["normalizedAlignment"]
                    for word_ev in _alignment_to_word_events(align):
                        await self._events.put(word_ev)
                if msg.get("isFinal"):
                    await self._events.put(None)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await self._events.put(TTSStreamError(message=str(e), recoverable=False))
            await self._events.put(None)


def _alignment_to_word_events(alignment: dict) -> list[TTSWordTiming]:
    """Reassemble whole-word events from ElevenLabs' per-character
    alignment payload. Each whitespace-separated run of characters is
    one event; start = first char's start, end = last char's start +
    duration. Returns an empty list if the payload is malformed."""
    chars = alignment.get("chars") or []
    starts = alignment.get("charStartTimesMs") or []
    durs = alignment.get("charDurationsMs") or []
    if not (len(chars) == len(starts) == len(durs)) or not chars:
        return []
    events: list[TTSWordTiming] = []
    word_chars: list[str] = []
    word_start_ms: int | None = None
    word_end_ms: int = 0
    for ch, st, du in zip(chars, starts, durs):
        if ch.isspace():
            if word_chars:
                events.append(TTSWordTiming(
                    word="".join(word_chars),
                    start_seconds=(word_start_ms or 0) / 1000.0,
                    end_seconds=word_end_ms / 1000.0,
                ))
                word_chars, word_start_ms = [], None
            continue
        if word_start_ms is None:
            word_start_ms = st
        word_chars.append(ch)
        word_end_ms = st + du
    if word_chars:
        events.append(TTSWordTiming(
            word="".join(word_chars),
            start_seconds=(word_start_ms or 0) / 1000.0,
            end_seconds=word_end_ms / 1000.0,
        ))
    return events
```

Add the imports needed by the new code to the file's import block:

```python
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSAudioChunk,
    TTSBackend,
    TTSStream,
    TTSStreamConfig,
    TTSStreamError,
    TTSWordTiming,
    Voice,
)
```

Add the `open_stream` method on `ElevenLabsTTS` (after `synthesize_stream`):

```python
async def open_stream(self, config: TTSStreamConfig) -> TTSStream:
    """Open a bidirectional TTS session via the stream-input WS API."""
    voice_id = config.voice_id or self._voice_id
    if not voice_id:
        raise ValueError("No voice_id configured — set voice_id in TTS backend settings")
    output_format = _FORMAT_MAP.get(config.output_format, "mp3_44100_128")
    ws = await _open_stream_input_ws(
        voice_id=voice_id,
        api_key=self._api_key,
        model_id=self._model_id,
        output_format=output_format,
    )
    # ElevenLabs requires a "voice settings" priming frame as the first
    # send before any text. We send a minimal placeholder; subsequent
    # send_text frames are what actually produce audio.
    await ws.send(_json.dumps({"text": " ", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}))
    return ElevenLabsTTSStream(ws)
```

- [ ] **Step 5: Run unit tests**

```bash
uv run pytest std-plugins/elevenlabs/tests/test_elevenlabs_streaming.py -v -k "not slow"
```

Expected: all unit tests pass.

- [ ] **Step 6: Commit in submodule and bump pointer**

```bash
cd std-plugins
git add elevenlabs/elevenlabs_tts.py elevenlabs/tests/test_elevenlabs_streaming.py elevenlabs/pyproject.toml
git commit -m "feat(elevenlabs): add BidirectionalTTSCapability via stream-input WS

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
cd ..
git add std-plugins
git commit -m "elevenlabs: bump submodule for bidirectional TTS

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Confirm full suite green and update CLAUDE.md if needed

**Files:**
- Verify only.
- Maybe modify: `CLAUDE.md` or `docs/architecture/speaker-system.md` if streaming TTS is referenced anywhere.

- [ ] **Step 1: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all unit tests pass; slow integration tests skipped (default).

- [ ] **Step 2: Type-check**

```bash
uv run mypy src/
```

Expected: no errors.

- [ ] **Step 3: Lint**

```bash
uv run ruff check src/ tests/ std-plugins/
```

Expected: clean (or only pre-existing warnings; do not fix unrelated ones).

- [ ] **Step 4: Scan for stale doc references**

```bash
grep -nE 'TTS|text.to.speech|synthesize' CLAUDE.md README.md docs/architecture/*.md 2>/dev/null | head -40
```

Read each match. If anything claims "TTS is bytes-only" or "no streaming," update it to mention the optional `StreamingTTSCapability` / `BidirectionalTTSCapability` protocols. If nothing claims that, no doc edit needed in this task.

- [ ] **Step 5: Run the validate-architecture audit**

Use the `validate-architecture` skill (per the project's CLAUDE.md) to confirm no rules were broken — capability protocols, AI prompt configurability, layer imports, plugin isolation, README freshness. Fix anything it flags before merging.

- [ ] **Step 6: Commit any doc fixes**

If any docs were edited:

```bash
git add CLAUDE.md README.md docs/architecture/
git commit -m "docs: note optional streaming TTS capabilities

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

If nothing changed, skip the commit.

---

## Task 12: SPA "Stream" toggle in the TTS settings test button

**Files:**
- Create: `src/gilbert/web/spa/src/hooks/useTTSStream.ts` (exact path verified in Step 1)
- Modify: the existing TTS settings test-button component (located in Step 1)

> **Note:** This task is the only frontend touch in this design. It's a smoke test, not a covered surface — the goal is to prove the WS wire works in a browser, not to build a polished UX.

- [ ] **Step 1: Locate the existing TTS settings test button and hook directory**

```bash
ls src/gilbert/web/spa 2>/dev/null && find src/gilbert/web/spa/src -path '*/hooks/*' -name '*.ts' -o -name '*.tsx' | head -5
grep -rn "tts" src/gilbert/web/spa/src/ --include='*.tsx' --include='*.ts' | grep -i "synthesize\|tts.*test\|test.*tts" | head -10
```

Record the exact paths. If the SPA layout differs from the assumed `src/gilbert/web/spa/src/hooks/` location, use whatever the codebase actually uses. **Do not invent paths.**

- [ ] **Step 2: Add the `useTTSStream` hook**

Create at the path located in Step 1. The hook opens a WS oneshot session and accumulates audio chunks into a `Blob`:

```typescript
import { useCallback, useRef, useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";   // verify import path in Step 1

type StreamState = "idle" | "streaming" | "done" | "error";

export function useTTSStream() {
  const { rpc, on } = useWebSocket();
  const [state, setState] = useState<StreamState>("idle");
  const [error, setError] = useState<string>("");
  const chunksRef = useRef<Uint8Array[]>([]);
  const sessionIdRef = useRef<string>("");
  const formatRef = useRef<string>("mp3");

  const start = useCallback(async (text: string, voiceId = "", format = "mp3") => {
    setError("");
    setState("streaming");
    chunksRef.current = [];
    formatRef.current = format;
    try {
      const res = await rpc({
        type: "tts.start_stream",
        mode: "oneshot",
        format,
        voice_id: voiceId,
        text,
      });
      sessionIdRef.current = res.session_id;
    } catch (e: any) {
      setState("error");
      setError(String(e));
    }
  }, [rpc]);

  // Wire the server-pushed events.
  // (Implementation depends on the SPA's WS event API. The shape is:
  //  `{type: "tts.event", session_id, event: {...}}`. Forward `event.audio_b64`
  //  -> base64.decode -> push into chunksRef. On `event.type === "end"`
  //  set state="done"; on "error" set state="error" with event.message.)
  // ... use the SPA's existing event-subscription pattern here ...

  const getBlob = useCallback(() => {
    const total = chunksRef.current.reduce((n, c) => n + c.byteLength, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const c of chunksRef.current) {
      out.set(c, offset);
      offset += c.byteLength;
    }
    const mime = formatRef.current === "mp3" ? "audio/mpeg"
              : formatRef.current === "wav" ? "audio/wav"
              : formatRef.current === "ogg" ? "audio/ogg"
              : "application/octet-stream";
    return new Blob([out], { type: mime });
  }, []);

  return { state, error, start, getBlob };
}
```

The exact event-subscription wiring **must follow the existing SPA pattern** for `transcription.event` — find it and mirror it. Don't roll your own listener.

- [ ] **Step 3: Add the "Stream" toggle to the TTS test button**

Edit the component located in Step 1. Add a checkbox or button beside the existing "Synthesize" action that, when checked, calls `useTTSStream().start(text, voiceId, "mp3")` instead of the existing batch path. When `state === "done"`, create a blob URL from `getBlob()` and play it in an `<audio controls>` element.

Minimal user-visible affordance:

```tsx
const [streamMode, setStreamMode] = useState(false);
const tts = useTTSStream();
const handleClick = streamMode
  ? () => tts.start(text, voiceId, "mp3")
  : () => /* existing batch handler */;

return (
  <>
    <label>
      <input type="checkbox" checked={streamMode}
             onChange={e => setStreamMode(e.target.checked)} />
      Stream
    </label>
    <button onClick={handleClick}>Synthesize</button>
    {tts.state === "done" && (
      <audio controls src={URL.createObjectURL(tts.getBlob())} />
    )}
    {tts.state === "error" && <div className="error">{tts.error}</div>}
  </>
);
```

(Adapt to whatever component library / styling the existing settings page uses.)

- [ ] **Step 4: Manual smoke test**

Per the project CLAUDE.md ("For UI or frontend changes, start the dev server and use the feature in a browser"):

```bash
./gilbert.sh start   # or whatever the project uses to launch the SPA
```

Open the TTS settings page in a browser. Make sure the active backend is one that implements `StreamingTTSCapability` (ElevenLabs with valid key, or Kokoro). Type a sentence, check "Stream," click "Synthesize." Confirm the audio element renders and plays. Open DevTools → Network → WS to confirm `tts.event` frames are arriving with `audio_b64`.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/web/spa/
git commit -m "feat(tts): add streaming WS smoke test to TTS settings page

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementing engineer

- **Submodule discipline.** Tasks 8-10 touch `std-plugins/`, which is a git submodule. Each of those tasks has **two** commits: one inside the submodule (commits to the plugin repo), one in the Gilbert repo to bump the submodule pointer. Do not bundle plugin commits into the Gilbert repo's history.
- **TDD literally.** Write the failing test, watch it fail, then make it pass. Don't skip the "watch it fail" step — that's how you catch tests that pass for the wrong reason.
- **No silence padding on streaming.** This is asserted explicitly in `test_synthesize_stream_yields_backend_chunks_without_padding`. If you find yourself adding padding, you've broken the contract.
- **Capability checks fire at call time, not at first iter.** Task 2 documents the rationale for `synthesize_stream` being a synchronous `def`. If you "refactor" it to `async def` with `yield`, you'll silently regress this — the test for it relies on the call itself raising.
- **Keep batch unchanged.** Every existing TTS test, including `tests/unit/test_tts_service.py`, must still pass byte-for-byte. The streaming additions are purely additive.
- **Don't touch STT.** The streaming-STT pattern is already in place (`StreamingTranscriptionBackend` + Deepgram + ElevenLabsScribeLive). Leave it alone.
- **Frontend is a smoke test, not a feature.** Task 12's deliverable is "I can verify the WS frames flow end-to-end in a real browser." Chat-UI streaming, conversation player, phone-bridge are explicitly out of scope per the spec.

---

## Self-review checklist

- ✅ Spec coverage: every section of the spec maps to a task (interfaces → Task 1; service methods → Tasks 2-3; `_event_to_json` & session model → Task 4; oneshot WS → Task 5; bidirectional WS + send_text/flush → Task 6; `stop()` cleanup → Task 7; Kokoro backend → Task 8; ElevenLabs streaming → Task 9; ElevenLabs bidirectional → Task 10; full-suite & docs → Task 11; SPA smoke test → Task 12).
- ✅ No placeholders: every code step has actual code; no "TBD" inside steps.
- ✅ Type consistency: `synthesize_stream` signature `def → AsyncIterator[bytes]` is consistent across the protocol, the service method, and both backend implementations. `open_stream` signature `async def → TTSStream` is consistent. `TTSStreamConfig` field set matches everywhere it's instantiated.
- ✅ TDD ordering: each task has failing-test → implementation → passing-test → commit.
- ✅ Submodule commits called out for Tasks 8-10.
- ✅ Capability check `def` vs `async def` rationale and regression test in place.
- ⚠️ One spec deferral worth flagging to the engineer: Task 12 says "exact SPA paths verified in Step 1" because I couldn't find the SPA layout from the spec alone. That's a real verification step in the task, not a placeholder — but it's the one spot where the plan asks the engineer to look something up rather than handing it over.
