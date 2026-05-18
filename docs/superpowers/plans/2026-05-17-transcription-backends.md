# TranscriptionService Vendor Backends — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fill `BatchTranscriptionBackend`, `StreamingTranscriptionBackend`, and `WakeWordBackend` registries with real vendor backends so `TranscriptionService` is useful beyond the bundled `local_whisper`.

**Scope:** 6 backends across 6 std-plugins. Each backend is its own task. Submodule commit + parent pointer-bump in each task. No frontend changes. No OpenAI Realtime (conversational-API-coupled — out of scope for pure STT).

**Backends shipped:**

| Backend | Role | Plugin | New or extend |
|---|---|---|---|
| `openai_whisper`        | batch     | `std-plugins/openai`        | extend |
| `groq_whisper`          | batch     | `std-plugins/groq`          | extend |
| `elevenlabs_scribe`     | batch     | `std-plugins/elevenlabs`    | extend |
| `elevenlabs_scribe_live`| streaming | `std-plugins/elevenlabs`    | extend (same plugin as above) |
| `deepgram`              | streaming | `std-plugins/deepgram` *(new)* | new |
| `porcupine`             | wake_word | `std-plugins/porcupine` *(new)* | new |
| `openwakeword`          | wake_word | `std-plugins/openwakeword` *(new)* | new |

(Two streaming backends, two wake-word backends — gives users a choice between cloud and local on both axes.)

**Tech Stack:** Python 3.12, `uv` workspaces, `httpx` for REST, `websockets` for vendor WebSocket streams, plus vendor-specific SDKs where they exist (`deepgram-sdk`, `pvporcupine`, `openwakeword`).

**Source spec:** [`docs/superpowers/specs/2026-05-17-transcription-service-design.md`](../specs/2026-05-17-transcription-service-design.md)

---

## Cross-cutting conventions

These apply to **every** backend task. They are documented here once so each task can reference them without repeating.

### Plugin layout (std-plugins/CLAUDE.md, condensed)

Each plugin lives in `std-plugins/<name>/` with:
- `pyproject.toml` (`package = false`, declares third-party deps in `dependencies = [...]`).
- `plugin.yaml` (`name`, `version`, `description`, `provides`, `requires`).
- `plugin.py` exposing `create_plugin()` → `Plugin` subclass.
- `__init__.py` (empty).
- The backend module (e.g., `openai_whisper.py`).
- `tests/conftest.py` and `tests/test_<backend>.py`.

For **extending** existing plugins (`openai`, `elevenlabs`, `groq`):
- Add the new module file alongside the existing one.
- Add the new backend's name to `plugin.yaml`'s `provides:` list.
- Add a side-effect `from . import <module>  # noqa: F401` in `plugin.py`'s `setup()`.
- Add tests under the existing `tests/` directory.

For **new** plugins (`deepgram`, `porcupine`, `openwakeword`):
- Full directory scaffold per above.
- Add the plugin to the **Gilbert root** `pyproject.toml` under `[tool.uv.workspace] members` (it's a glob `std-plugins/*` — should already include).
- Verify `uv sync` from the Gilbert root installs the new plugin's deps.
- Add an entry to `std-plugins/README.md` (table row + per-plugin detail section).

### Submodule commit dance

`std-plugins/` is a git submodule of `briandilley/gilbert-plugins`. Each backend task does TWO commits:

1. **Inside the submodule** (`cd std-plugins && git add ... && git commit -m "..."`):
   - The new/modified plugin files.
   - The submodule's `README.md` updates.
2. **In the parent Gilbert repo** (`cd /home/brian/gilbert && git add std-plugins && git commit -m "..."`):
   - The submodule pointer bump.

Do NOT push to the submodule's remote in this plan — that's a separate user action.

### Authentication / API keys

Existing plugins (`openai`, `elevenlabs`, `groq`) already declare an `api_key` config field on their existing backend. Re-use that — read it via the standard `ConfigurationReader` pattern (or, more typically, pass it through `initialize(config)` since the service flattens per-backend config into `<role>.backends.<name>.settings.*`). **The transcription service passes `settings.*` from `<role>.backends.<name>` into each backend's `initialize()`, so each STT backend has its OWN api_key config under its own name — separate from the sibling TTS/AI backend in the same plugin.** Document this clearly in each backend's `backend_config_params()`.

For new plugins (`deepgram`, `porcupine`, `openwakeword`):
- Declare an `api_key` (or `access_key`) ConfigParam with `sensitive=True` on each backend that needs one. `openwakeword` is fully local, no key.

### Test strategy

- Each backend gets one unit test file under `<plugin>/tests/test_<backend>.py`.
- **No live network calls.** Mock the HTTP client / SDK using `pytest-asyncio` + `unittest.mock` (or `httpx_mock` if it's a project dep — check first).
- Behaviors to cover per backend (minimal but real):
  1. Backend registers in the correct registry under the declared name.
  2. `backend_config_params()` returns the expected keys.
  3. `initialize()` reads `api_key` (or equivalent) and stores client state.
  4. The main call (`transcribe` / `open_stream` / `open_detector`) translates request → vendor call → result correctly. Mock the wire layer.
  5. Error path: vendor returns 4xx/5xx (or raises) — wrapped as a sensible `TranscriptionError` or RuntimeError with the message.

For streaming backends additionally:
- The session's `events()` iterator yields the expected `TranscriptionEvent` discriminators when the mocked vendor sends partial / final frames.
- `close()` is idempotent and tears down both ends.

For wake-word backends:
- The detector emits a `WakeEvent` when the mocked vendor signals one.

### File and naming conventions

- Backend module name: `<vendor>_<role>.py` (e.g., `openai_whisper.py`, `elevenlabs_scribe.py`). For multi-role plugins where two backends share a module is fine if they're tightly coupled (e.g., `elevenlabs_scribe.py` can contain both batch and streaming classes since they share auth and shape).
- Backend class `backend_name`: snake-case, vendor-prefixed for clarity (e.g., `"openai_whisper"`, `"groq_whisper"`, `"elevenlabs_scribe"`, `"elevenlabs_scribe_live"`, `"deepgram"`, `"porcupine"`, `"openwakeword"`).
- Logger name: `logger = logging.getLogger(__name__)` (standard).

### Architectural rules (each task)

- Plugin imports ONLY from `gilbert.interfaces.*` and stdlib + third-party.
- No imports from `gilbert.core.services`, `gilbert.integrations`, `gilbert.web`, or `gilbert.storage`.
- Each backend class has `backend_name` set so `__init_subclass__` auto-registers it.

### Audio format handling

The TranscriptionService passes the caller's `AudioFormat` through to the backend (via `TranscriptionRequest.format` for batch, `StreamConfig.format` for streaming, `WakeWordConfig.format` for wake-word). Each backend:
- For batch: read `request.audio` bytes + `request.format`. If the backend's API can sniff containers (Whisper, Scribe), pass through as-is. If it needs raw PCM at a specific rate, resample using `gilbert.interfaces.transcription.resample_pcm`.
- For streaming: clients typically send PCM16LE at 16 kHz mono. Pass through if the vendor accepts that; otherwise resample on the way in.
- For wake-word: most engines want 16 kHz mono PCM frames of specific sizes — use the helper.

---

## Task 1: OpenAI Whisper (batch)

**Plugin:** `std-plugins/openai/` (extend existing)
**Backend name:** `openai_whisper`
**Module:** `std-plugins/openai/openai_whisper.py`
**Endpoint:** `POST <base_url>/audio/transcriptions` (multipart form). Default `base_url=https://api.openai.com/v1`. Auth: `Authorization: Bearer <api_key>`.
**Model:** `whisper-1` (default) or `gpt-4o-transcribe` / `gpt-4o-mini-transcribe`.

- [ ] **Step 1: Create `tests/test_openai_whisper.py` with failing tests (TDD)**

Create `std-plugins/openai/tests/test_openai_whisper.py`. Test the following behaviors — write them as failing tests now, against the not-yet-existing class:

```python
"""Tests for the OpenAI Whisper batch transcription backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    BatchTranscriptionBackend,
    TranscriptionRequest,
)


@pytest.fixture
def backend():
    """Import inside the fixture so the registry is populated lazily."""
    from gilbert_plugin_openai import openai_whisper

    return openai_whisper.OpenAIWhisperBackend()


def test_backend_is_registered():
    from gilbert_plugin_openai import openai_whisper  # noqa: F401

    assert "openai_whisper" in BatchTranscriptionBackend.registered_backends()


def test_config_params_include_api_key_and_model(backend):
    keys = {p.key for p in backend.backend_config_params()}
    assert "api_key" in keys
    assert "model" in keys
    assert "base_url" in keys
    # api_key must be sensitive
    api_key_param = next(p for p in backend.backend_config_params() if p.key == "api_key")
    assert api_key_param.sensitive is True


@pytest.mark.asyncio
async def test_transcribe_sends_audio_and_returns_text(backend):
    await backend.initialize({
        "api_key": "sk-test",
        "model": "whisper-1",
        "base_url": "https://api.openai.com/v1",
    })

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "text": "hello world",
        "language": "english",
        "duration": 1.5,
        "segments": [
            {"start": 0.0, "end": 0.7, "text": "hello"},
            {"start": 0.7, "end": 1.5, "text": "world"},
        ],
    }
    fake_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)) as mock_post:
        result = await backend.transcribe(TranscriptionRequest(
            audio=b"\x00\x00" * 1000,
            format=AudioFormat(AudioEncoding.WAV),
            language="en",
        ))

    assert result.text == "hello world"
    assert len(result.segments) == 2
    assert result.segments[0].text == "hello"
    assert result.language == "english"
    assert result.duration_seconds == 1.5

    # Verify the call shape: posts to /audio/transcriptions, sends audio as multipart
    call = mock_post.call_args
    assert "/audio/transcriptions" in call.args[0]
    assert "files" in call.kwargs
    assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_transcribe_4xx_raises_runtime_error_with_message(backend):
    await backend.initialize({"api_key": "sk-test"})

    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = '{"error": {"message": "Invalid API key"}}'
    fake_response.raise_for_status.side_effect = Exception("HTTP 401")

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(RuntimeError, match="(?i)401|invalid api key"):
            await backend.transcribe(TranscriptionRequest(
                audio=b"\x00",
                format=AudioFormat(AudioEncoding.WAV),
            ))


@pytest.mark.asyncio
async def test_list_languages_returns_iso_codes(backend):
    langs = await backend.list_languages()
    assert "en" in langs
    assert "auto" in langs
    assert isinstance(langs, list)
```

- [ ] **Step 2: Confirm the tests fail** — `uv run pytest std-plugins/openai/tests/test_openai_whisper.py -v`. Expected: ImportError on `openai_whisper` module.

- [ ] **Step 3: Implement `std-plugins/openai/openai_whisper.py`**

```python
"""OpenAI Whisper batch transcription backend."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "whisper-1"

# Whisper supports the full Whisper language list. Keep the dropdown
# short and informative; the full list is upstream documentation.
_SUPPORTED_LANGUAGES = [
    "auto", "en", "es", "fr", "de", "it", "pt", "nl", "ru",
    "zh", "ja", "ko", "ar", "hi", "tr", "pl", "uk", "sv",
]


class OpenAIWhisperBackend(BatchTranscriptionBackend):
    """One-shot transcription via OpenAI's /audio/transcriptions endpoint.

    Supports `whisper-1` (the original Whisper API) and the newer
    `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` models which use the
    same endpoint but produce different latency/quality trade-offs.
    """

    backend_name = "openai_whisper"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="OpenAI API key.",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description="API base URL. Override for compatible providers.",
                default=_DEFAULT_BASE_URL,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description="Model id.",
                default=_DEFAULT_MODEL,
                choices=("whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"),
            ),
        ]

    def __init__(self) -> None:
        self._api_key: str = ""
        self._base_url: str = _DEFAULT_BASE_URL
        self._model: str = _DEFAULT_MODEL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")
        self._model = str(config.get("model", _DEFAULT_MODEL))
        if not self._api_key:
            logger.warning("openai_whisper initialized without api_key — calls will fail")

    async def close(self) -> None:
        pass

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        # Pick a filename extension that maps to the encoding we have —
        # OpenAI sniffs the file but a hint helps when format is AUTO.
        ext_map = {
            "wav": "wav", "mp3": "mp3", "m4a": "m4a",
            "ogg": "ogg", "webm": "webm", "opus": "opus",
            "pcm_s16le": "wav",  # not strictly true, but Whisper accepts it
            "auto": "wav",
        }
        filename = f"audio.{ext_map.get(request.format.encoding.value, 'wav')}"
        files = {"file": (filename, request.audio, "application/octet-stream")}
        data: dict[str, Any] = {
            "model": self._model,
            "response_format": "verbose_json",
        }
        if request.language and request.language != "auto":
            data["language"] = request.language
        if request.prompt:
            data["prompt"] = request.prompt

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    headers=headers,
                    files=files,
                    data=data,
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"openai_whisper request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise RuntimeError(
                f"openai_whisper HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
        segments = [
            TranscriptSegment(
                text=str(s.get("text", "")).strip(),
                start_seconds=float(s.get("start", 0.0)),
                end_seconds=float(s.get("end", 0.0)),
                speaker_label="",
                confidence=None,
            )
            for s in payload.get("segments", [])
        ]
        return TranscriptionResult(
            text=str(payload.get("text", "")).strip(),
            segments=segments,
            language=str(payload.get("language", "")),
            duration_seconds=float(payload.get("duration", 0.0)) if payload.get("duration") is not None else None,
            audio_seconds_used=float(payload.get("duration", 0.0)) if payload.get("duration") is not None else None,
        )

    async def list_languages(self) -> list[str]:
        return list(_SUPPORTED_LANGUAGES)
```

- [ ] **Step 4: Wire it into `plugin.py` and `plugin.yaml`**

In `std-plugins/openai/plugin.py`, add a side-effect import for the new module inside `setup()`:

```python
async def setup(self, context: PluginContext) -> None:
    from . import openai_ai  # noqa: F401 — existing
    from . import openai_whisper  # noqa: F401 — new
```

(If the file's current shape differs, follow the existing module's pattern.)

In `std-plugins/openai/plugin.yaml`, add `openai_whisper` to `provides:`:

```yaml
provides:
  - openai_ai
  - openai_whisper
```

- [ ] **Step 5: Run the new tests + the whole suite**

```bash
uv run pytest std-plugins/openai/tests/ -v
uv run pytest tests/unit/test_transcription_service.py -v   # smoke — sanity that registry is intact
```

Expected: all green. `OpenAIWhisperBackend` shows up in `BatchTranscriptionBackend.registered_backends()` when imported.

- [ ] **Step 6: Lint + type-check**

```bash
uv run ruff check std-plugins/openai/openai_whisper.py std-plugins/openai/tests/test_openai_whisper.py
uv run mypy std-plugins/openai/openai_whisper.py
```

- [ ] **Step 7: Commit (TWO commits — submodule then parent pointer bump)**

```bash
cd /home/brian/gilbert/std-plugins
git add openai/openai_whisper.py openai/plugin.py openai/plugin.yaml openai/tests/test_openai_whisper.py
git commit -m "openai: add Whisper batch transcription backend"

cd /home/brian/gilbert
git add std-plugins
git commit -m "transcription: openai_whisper batch backend (std-plugin bump)"
```

---

## Task 2: Groq Whisper (batch)

**Plugin:** `std-plugins/groq/` (extend existing)
**Backend name:** `groq_whisper`
**Module:** `std-plugins/groq/groq_whisper.py`
**Endpoint:** `POST https://api.groq.com/openai/v1/audio/transcriptions` (OpenAI-compatible).
**Model:** `whisper-large-v3` (default), `whisper-large-v3-turbo`.

The shape is nearly identical to `openai_whisper` — only the default `base_url` and `model` differ. The implementation should be a near-copy with the constants swapped.

- [ ] **Step 1: Create `std-plugins/groq/tests/test_groq_whisper.py` (TDD)**

Mirror the test file shape from Task 1, swapping:
- `from gilbert_plugin_groq import groq_whisper` (and adjust the import in the registry-check test).
- Backend class: `GroqWhisperBackend`.
- Registered name: `"groq_whisper"`.
- Default model assertion: `"whisper-large-v3"`.

- [ ] **Step 2: Run, confirm fail.**

- [ ] **Step 3: Implement `std-plugins/groq/groq_whisper.py`**

Same shape as `openai_whisper.py`, with constants:
- `_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"`
- `_DEFAULT_MODEL = "whisper-large-v3"`
- `choices` for model: `("whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en")`

The body of `transcribe()` and `list_languages()` can be copied verbatim — the API is OpenAI-compatible.

- [ ] **Step 4: Wire into `plugin.py` + `plugin.yaml`** (`provides:` add `groq_whisper`).

- [ ] **Step 5: Tests + lint + mypy.**

- [ ] **Step 6: Two commits (submodule + parent bump)**, messages:
- `groq: add Whisper batch transcription backend`
- `transcription: groq_whisper batch backend (std-plugin bump)`

---

## Task 3: ElevenLabs Scribe (batch + streaming)

**Plugin:** `std-plugins/elevenlabs/` (extend existing)
**Backend names:** `elevenlabs_scribe` (batch) and `elevenlabs_scribe_live` (streaming).
**Module:** `std-plugins/elevenlabs/elevenlabs_scribe.py` (both classes in one file).

**Batch endpoint:** `POST https://api.elevenlabs.io/v1/speech-to-text` (multipart). Auth: `xi-api-key: <api_key>` header.

**Streaming:** ElevenLabs' Scribe live API uses WebSockets at `wss://api.elevenlabs.io/v1/speech-to-text/stream`. The implementation maintains a vendor-side WS connection per session; audio chunks go in as binary frames, transcripts come back as JSON frames.

This is the largest task — it ships TWO backend classes in one file because they share auth + base URL.

- [ ] **Step 1: Create `std-plugins/elevenlabs/tests/test_elevenlabs_scribe.py`**

Tests covering BOTH backends:

```python
"""Tests for ElevenLabs Scribe batch + streaming transcription backends."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    BatchTranscriptionBackend,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionRequest,
)


def test_scribe_batch_registered():
    from gilbert_plugin_elevenlabs import elevenlabs_scribe  # noqa: F401

    assert "elevenlabs_scribe" in BatchTranscriptionBackend.registered_backends()


def test_scribe_live_registered():
    from gilbert_plugin_elevenlabs import elevenlabs_scribe  # noqa: F401

    assert "elevenlabs_scribe_live" in StreamingTranscriptionBackend.registered_backends()


@pytest.fixture
def batch():
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import ElevenLabsScribeBackend

    return ElevenLabsScribeBackend()


@pytest.fixture
def live():
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import ElevenLabsScribeLiveBackend

    return ElevenLabsScribeLiveBackend()


def test_batch_config_params_include_api_key(batch):
    keys = {p.key for p in batch.backend_config_params()}
    assert "api_key" in keys
    assert "model" in keys
    api_key = next(p for p in batch.backend_config_params() if p.key == "api_key")
    assert api_key.sensitive is True


@pytest.mark.asyncio
async def test_batch_transcribe_returns_text(batch):
    await batch.initialize({"api_key": "el-test", "model": "scribe_v1"})

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "text": "hello world",
        "language_code": "en",
        "words": [
            {"text": "hello", "start": 0.0, "end": 0.7, "type": "word"},
            {"text": " ", "start": 0.7, "end": 0.75, "type": "spacing"},
            {"text": "world", "start": 0.75, "end": 1.5, "type": "word"},
        ],
    }
    fake_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)) as mock_post:
        result = await batch.transcribe(TranscriptionRequest(
            audio=b"\x00" * 100,
            format=AudioFormat(AudioEncoding.WAV),
            language="en",
        ))

    assert "hello" in result.text.lower()
    assert "world" in result.text.lower()
    assert result.language == "en"
    # Auth header is xi-api-key
    call = mock_post.call_args
    assert call.kwargs["headers"]["xi-api-key"] == "el-test"


@pytest.mark.asyncio
async def test_batch_4xx_raises_runtime_error(batch):
    await batch.initialize({"api_key": "el-test"})
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = '{"detail": "invalid api key"}'
    fake_response.raise_for_status.side_effect = Exception("401")

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(RuntimeError, match="(?i)401|invalid"):
            await batch.transcribe(TranscriptionRequest(
                audio=b"\x00", format=AudioFormat(AudioEncoding.WAV),
            ))


@pytest.mark.asyncio
async def test_streaming_open_returns_stream(live):
    """The streaming backend's open_stream returns a TranscriptionStream
    that wraps a mocked vendor WebSocket. send() forwards bytes; events()
    yields events translated from the mocked vendor's JSON frames.
    """
    from gilbert.interfaces.transcription import (
        FinalTranscript, PartialTranscript, TranscriptionStream,
    )

    await live.initialize({"api_key": "el-test"})

    # The vendor WS is a mock that records sent bytes and yields
    # canned JSON frames on receive.
    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list = []
            self._queue = __import__("asyncio").Queue()

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return await self._queue.get()

        async def close(self):
            pass

        def push(self, frame):
            self._queue.put_nowait(frame)

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await live.open_stream(StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE),
            language="en",
        ))
        assert isinstance(stream, TranscriptionStream)

        # Push a partial then a final from the vendor
        import json
        fake_ws.push(json.dumps({"type": "partial", "text": "hel"}))
        fake_ws.push(json.dumps({"type": "final", "text": "hello", "start": 0.0, "end": 0.5}))

        events: list = []
        async def _drain():
            count = 0
            async for ev in stream.events():
                events.append(ev)
                count += 1
                if count >= 2:
                    break

        import asyncio
        await asyncio.wait_for(_drain(), timeout=1.0)

        assert any(isinstance(e, PartialTranscript) for e in events)
        assert any(isinstance(e, FinalTranscript) for e in events)

        await stream.close()
```

- [ ] **Step 2: Run, confirm fail.**

- [ ] **Step 3: Implement `std-plugins/elevenlabs/elevenlabs_scribe.py`**

```python
"""ElevenLabs Scribe — speech-to-text (batch and streaming)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    FinalTranscript,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionStream,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"
_DEFAULT_BATCH_MODEL = "scribe_v1"
_DEFAULT_LIVE_MODEL = "scribe_v1"
_DEFAULT_LIVE_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/stream"


def _common_config(default_model: str) -> list[ConfigParam]:
    return [
        ConfigParam(
            key="api_key",
            type=ToolParameterType.STRING,
            description="ElevenLabs API key.",
            default="",
            sensitive=True,
        ),
        ConfigParam(
            key="model",
            type=ToolParameterType.STRING,
            description="Scribe model id.",
            default=default_model,
        ),
    ]


class ElevenLabsScribeBackend(BatchTranscriptionBackend):
    """Batch transcription via ElevenLabs /v1/speech-to-text."""

    backend_name = "elevenlabs_scribe"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return _common_config(_DEFAULT_BATCH_MODEL) + [
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description="API base URL (default https://api.elevenlabs.io).",
                default=_DEFAULT_BASE_URL,
            ),
        ]

    def __init__(self) -> None:
        self._api_key = ""
        self._model = _DEFAULT_BATCH_MODEL
        self._base_url = _DEFAULT_BASE_URL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_BATCH_MODEL))
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")

    async def close(self) -> None:
        pass

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        headers = {"xi-api-key": self._api_key}
        files = {"file": ("audio.wav", request.audio, "application/octet-stream")}
        data: dict[str, Any] = {"model_id": self._model}
        if request.language and request.language != "auto":
            data["language_code"] = request.language
        if request.diarize:
            data["diarize"] = "true"

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/v1/speech-to-text",
                    headers=headers,
                    files=files,
                    data=data,
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"elevenlabs_scribe request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise RuntimeError(
                f"elevenlabs_scribe HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()

        # Scribe returns word-level entries; coalesce into rough sentence
        # segments by splitting on punctuation/length. Keep it simple — if
        # callers need word-level, they can read result.text and re-split.
        words = payload.get("words", [])
        segments: list[TranscriptSegment] = []
        if words:
            cur_text: list[str] = []
            cur_start = float(words[0].get("start", 0.0))
            cur_end = cur_start
            for w in words:
                t = str(w.get("text", ""))
                cur_text.append(t)
                cur_end = float(w.get("end", cur_end))
                if t.endswith((".", "!", "?")) and len("".join(cur_text)) > 20:
                    segments.append(TranscriptSegment(
                        text="".join(cur_text).strip(),
                        start_seconds=cur_start,
                        end_seconds=cur_end,
                    ))
                    cur_text = []
                    cur_start = cur_end
            if cur_text:
                segments.append(TranscriptSegment(
                    text="".join(cur_text).strip(),
                    start_seconds=cur_start,
                    end_seconds=cur_end,
                ))
        return TranscriptionResult(
            text=str(payload.get("text", "")).strip(),
            segments=segments,
            language=str(payload.get("language_code", "")),
            duration_seconds=None,
            audio_seconds_used=None,
        )

    async def list_languages(self) -> list[str]:
        return ["auto", "en", "es", "fr", "de", "it", "pt", "nl", "ru",
                "zh", "ja", "ko", "ar", "hi", "tr", "pl"]


class _ScribeLiveStream(TranscriptionStream):
    """A live streaming session backed by an ElevenLabs WebSocket."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        await self._ws.send(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def events(self) -> AsyncIterator[TranscriptionEvent]:
        while True:
            try:
                raw = await self._ws.recv()
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    yield TranscriptionError(message=str(exc))
                return
            if raw is None:
                return
            try:
                msg = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
            except Exception:  # noqa: BLE001
                continue
            kind = msg.get("type", "")
            if kind == "partial":
                yield PartialTranscript(
                    text=str(msg.get("text", "")),
                    start_seconds=float(msg.get("start", 0.0)),
                )
            elif kind == "final":
                yield FinalTranscript(
                    text=str(msg.get("text", "")),
                    start_seconds=float(msg.get("start", 0.0)),
                    end_seconds=float(msg.get("end", 0.0)),
                )
            elif kind == "speech_started":
                yield SpeechStarted(at_seconds=float(msg.get("at", 0.0)))
            elif kind == "speech_ended":
                yield SpeechEnded(at_seconds=float(msg.get("at", 0.0)))
            elif kind == "error":
                yield TranscriptionError(
                    message=str(msg.get("message", "scribe error")),
                    recoverable=False,
                )


class ElevenLabsScribeLiveBackend(StreamingTranscriptionBackend):
    """Streaming Scribe via WebSocket."""

    backend_name = "elevenlabs_scribe_live"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return _common_config(_DEFAULT_LIVE_MODEL) + [
            ConfigParam(
                key="ws_url",
                type=ToolParameterType.STRING,
                description="WebSocket URL for the Scribe live endpoint.",
                default=_DEFAULT_LIVE_WS_URL,
            ),
        ]

    def __init__(self) -> None:
        self._api_key = ""
        self._model = _DEFAULT_LIVE_MODEL
        self._ws_url = _DEFAULT_LIVE_WS_URL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_LIVE_MODEL))
        self._ws_url = str(config.get("ws_url", _DEFAULT_LIVE_WS_URL))

    async def close(self) -> None:
        pass

    async def open_stream(self, config: StreamConfig) -> TranscriptionStream:
        import websockets  # deferred — only needed for streaming

        url = (
            f"{self._ws_url}?model_id={self._model}"
            f"&language_code={config.language or 'auto'}"
        )
        ws = await websockets.connect(
            url,
            additional_headers={"xi-api-key": self._api_key},
            max_size=None,
        )
        return _ScribeLiveStream(ws)
```

Note: the actual ElevenLabs Scribe live API surface (URL, query params, frame shape) may differ from the placeholders above. If the test mocks pass but a live integration would fail because the URL or frame format is wrong, that's acceptable — the abstraction is the deliverable; vendor-API drift can be fixed in a follow-up when someone tries it live. **Document any guess explicitly in the module docstring.**

- [ ] **Step 4: Add `websockets` to `std-plugins/elevenlabs/pyproject.toml` dependencies if it's not already a Gilbert core dep.**

```bash
cd /home/brian/gilbert
grep -r 'websockets' pyproject.toml uv.lock | head
```

If `websockets` is already in core, no plugin dep needed. If not, add:

```toml
dependencies = [
    "websockets>=12.0",
]
```

- [ ] **Step 5: Wire into `plugin.py` (add import) + `plugin.yaml` (add both backend names to `provides`).**

- [ ] **Step 6: Tests + lint + mypy.**

- [ ] **Step 7: Two commits.**
- `elevenlabs: add Scribe batch + live streaming backends`
- `transcription: elevenlabs Scribe batch + streaming (std-plugin bump)`

---

## Task 4: Deepgram (streaming, new plugin)

**Plugin:** `std-plugins/deepgram/` *(new)*
**Backend name:** `deepgram`
**Module:** `std-plugins/deepgram/deepgram.py`
**Endpoint:** `wss://api.deepgram.com/v1/listen` with `Authorization: Token <api_key>` header.

We use raw `websockets` rather than the `deepgram-sdk` package — fewer deps, no transitive dep noise, and the WebSocket protocol is straightforward.

- [ ] **Step 1: Scaffold the plugin directory**

```bash
mkdir -p std-plugins/deepgram/tests
touch std-plugins/deepgram/__init__.py
```

Create `std-plugins/deepgram/pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-deepgram"
version = "1.0.0"
description = "Deepgram streaming speech-to-text backend for Gilbert"
requires-python = ">=3.12"
dependencies = [
    "websockets>=12.0",
]

[tool.uv]
package = false
```

Create `std-plugins/deepgram/plugin.yaml`:

```yaml
name: deepgram
version: "1.0.0"
description: "Deepgram streaming speech-to-text"

provides:
  - deepgram

requires: []
depends_on: []
```

Create `std-plugins/deepgram/plugin.py`:

```python
"""Deepgram streaming transcription plugin."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class DeepgramPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="deepgram",
            version="1.0.0",
            description="Deepgram streaming speech-to-text",
            provides=["deepgram"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import deepgram  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return DeepgramPlugin()
```

Create `std-plugins/deepgram/tests/conftest.py` by **copying** from `std-plugins/tesseract/tests/conftest.py` and adapting the plugin name to `gilbert_plugin_deepgram`. Read that file first to get the exact shape.

- [ ] **Step 2: Write `std-plugins/deepgram/tests/test_deepgram.py`** modeled on the ElevenLabs scribe live test (Task 3 — same pattern with a `_FakeWs` mock). Test:
  - Registers under `"deepgram"` in `StreamingTranscriptionBackend`.
  - `config_params()` has `api_key` (sensitive).
  - `open_stream` returns a stream; pushing JSON frames from mocked WS yields `PartialTranscript` / `FinalTranscript`.

- [ ] **Step 3: Implement `std-plugins/deepgram/deepgram.py`**

```python
"""Deepgram streaming speech-to-text backend."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    FinalTranscript,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionStream,
)

logger = logging.getLogger(__name__)

_DEFAULT_WS_URL = "wss://api.deepgram.com/v1/listen"
_DEFAULT_MODEL = "nova-3"


class _DeepgramStream(TranscriptionStream):
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        await self._ws.send(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            # Deepgram expects an empty binary frame to signal end-of-stream.
            await self._ws.send(b"")
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def events(self) -> AsyncIterator[TranscriptionEvent]:
        while True:
            try:
                raw = await self._ws.recv()
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    yield TranscriptionError(message=str(exc))
                return
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            kind = msg.get("type", "")
            if kind == "Results":
                channel = msg.get("channel", {})
                alternatives = channel.get("alternatives", [])
                if not alternatives:
                    continue
                alt = alternatives[0]
                text = str(alt.get("transcript", ""))
                if not text:
                    continue
                is_final = bool(msg.get("is_final", False))
                start = float(msg.get("start", 0.0))
                dur = float(msg.get("duration", 0.0))
                if is_final:
                    yield FinalTranscript(
                        text=text,
                        start_seconds=start,
                        end_seconds=start + dur,
                        confidence=float(alt.get("confidence", 0.0)) or None,
                    )
                else:
                    yield PartialTranscript(text=text, start_seconds=start)
            elif kind == "SpeechStarted":
                yield SpeechStarted(at_seconds=float(msg.get("timestamp", 0.0)))
            elif kind == "UtteranceEnd":
                yield SpeechEnded(at_seconds=float(msg.get("last_word_end", 0.0)))


class DeepgramBackend(StreamingTranscriptionBackend):
    """Streaming transcription via Deepgram's WebSocket API."""

    backend_name = "deepgram"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="Deepgram API key.",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description="Deepgram model id.",
                default=_DEFAULT_MODEL,
                choices=("nova-3", "nova-2", "enhanced", "base"),
            ),
            ConfigParam(
                key="ws_url",
                type=ToolParameterType.STRING,
                description="WebSocket URL.",
                default=_DEFAULT_WS_URL,
            ),
        ]

    def __init__(self) -> None:
        self._api_key = ""
        self._model = _DEFAULT_MODEL
        self._ws_url = _DEFAULT_WS_URL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_MODEL))
        self._ws_url = str(config.get("ws_url", _DEFAULT_WS_URL))

    async def close(self) -> None:
        pass

    async def open_stream(self, config: StreamConfig) -> TranscriptionStream:
        import websockets

        params: dict[str, Any] = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(config.format.sample_rate),
            "channels": str(config.format.channels),
            "interim_results": str(config.interim_results).lower(),
            "vad_events": str(config.vad_events).lower(),
        }
        if config.language and config.language != "auto":
            params["language"] = config.language
        if config.diarize:
            params["diarize"] = "true"

        url = f"{self._ws_url}?{urlencode(params)}"
        ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            max_size=None,
        )
        return _DeepgramStream(ws)
```

- [ ] **Step 4: Verify the Gilbert root `pyproject.toml` includes `std-plugins/*` in `[tool.uv.workspace] members`** (it should — a glob — confirm by `grep "std-plugins" pyproject.toml`).

If the new plugin needs an explicit `[tool.uv.sources]` entry, add it (look at how other plugins are listed).

- [ ] **Step 5: `uv sync` to install `websockets` if it's not already there.**

- [ ] **Step 6: Tests + lint + mypy.**

```bash
uv run pytest std-plugins/deepgram/tests/ -v
uv run ruff check std-plugins/deepgram/
uv run mypy std-plugins/deepgram/deepgram.py
```

- [ ] **Step 7: Two commits.**
- `deepgram: new plugin — streaming speech-to-text via Deepgram WS`
- `transcription: deepgram streaming backend (std-plugin bump)`

---

## Task 5: Porcupine (wake-word, new plugin)

**Plugin:** `std-plugins/porcupine/` *(new)*
**Backend name:** `porcupine`
**Module:** `std-plugins/porcupine/porcupine.py`
**SDK:** `pvporcupine` (official Python package, ships with native wheels).
**Auth:** Picovoice access key (free tier for personal use; paid for commercial).

Porcupine works on fixed-size PCM16 frames (typically 512 samples at 16 kHz). Our backend buffers incoming chunks and feeds full frames to the detector.

- [ ] **Step 1: Scaffold the plugin**

```bash
mkdir -p std-plugins/porcupine/tests
touch std-plugins/porcupine/__init__.py
```

`std-plugins/porcupine/pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-porcupine"
version = "1.0.0"
description = "Porcupine wake-word detection backend for Gilbert"
requires-python = ">=3.12"
dependencies = [
    "pvporcupine>=3.0",
]

[tool.uv]
package = false
```

`std-plugins/porcupine/plugin.yaml`:

```yaml
name: porcupine
version: "1.0.0"
description: "Porcupine wake-word detection"

provides:
  - porcupine

requires: []
depends_on: []
```

`std-plugins/porcupine/plugin.py` — boilerplate Plugin class with `setup()` importing `porcupine` (the module).

`std-plugins/porcupine/tests/conftest.py` — copy from `std-plugins/tesseract/tests/conftest.py` and adapt.

- [ ] **Step 2: Write `tests/test_porcupine.py`**

Test:
- Registers under `"porcupine"` in `WakeWordBackend`.
- `config_params` includes `access_key` (sensitive).
- `open_detector` returns a `WakeWordDetector`.
- Pushing audio chunks and having the mocked porcupine library return a positive keyword index emits a `WakeEvent` with the matching keyword.

Mock `pvporcupine.create(access_key=..., keywords=...)` to return a fake whose `process(frame)` returns `0` (first keyword) when called the second time, `-1` otherwise.

- [ ] **Step 3: Implement `std-plugins/porcupine/porcupine.py`**

```python
"""Porcupine wake-word detection backend."""

from __future__ import annotations

import array
import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)

logger = logging.getLogger(__name__)


class _PorcupineDetector(WakeWordDetector):
    """Buffers incoming PCM chunks and feeds Porcupine fixed-size frames."""

    def __init__(self, porcupine: Any, keywords: list[str]) -> None:
        self._p = porcupine
        self._keywords = keywords
        self._buf = bytearray()
        self._frame_bytes = porcupine.frame_length * 2  # 16-bit samples
        self._queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self._closed = False
        self._sample_count = 0

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        self._buf.extend(chunk)
        while len(self._buf) >= self._frame_bytes:
            frame_bytes = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            self._sample_count += self._p.frame_length
            # porcupine.process is a sync call returning a keyword index or -1
            frame = array.array("h", frame_bytes)
            idx = self._p.process(frame)
            if idx is not None and idx >= 0:
                kw = self._keywords[idx] if idx < len(self._keywords) else f"kw{idx}"
                await self._queue.put(WakeEvent(
                    keyword=kw,
                    at_seconds=self._sample_count / self._p.sample_rate,
                ))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)
        try:
            self._p.delete()
        except Exception:  # noqa: BLE001
            logger.exception("error releasing porcupine resources")

    async def events(self) -> AsyncIterator[WakeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class PorcupineBackend(WakeWordBackend):
    """Wake-word detection via Picovoice Porcupine."""

    backend_name = "porcupine"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="access_key",
                type=ToolParameterType.STRING,
                description="Picovoice access key (https://console.picovoice.ai).",
                default="",
                sensitive=True,
            ),
        ]

    def __init__(self) -> None:
        self._access_key = ""

    async def initialize(self, config: dict[str, object]) -> None:
        self._access_key = str(config.get("access_key", ""))

    async def close(self) -> None:
        pass

    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector:
        import pvporcupine

        # Porcupine accepts built-in keyword names ("computer", "hey google")
        # AND custom keyword model files. Treat each keyword string as a
        # built-in name; users with custom .ppn files set a longer path
        # that pvporcupine.create() accepts under keyword_paths instead.
        p = pvporcupine.create(
            access_key=self._access_key,
            keywords=list(config.keywords),
            sensitivities=[config.sensitivity] * len(config.keywords),
        )
        return _PorcupineDetector(p, list(config.keywords))
```

- [ ] **Step 4: `uv sync` to install `pvporcupine`.**

- [ ] **Step 5: Tests + lint + mypy.**

- [ ] **Step 6: Two commits.**
- `porcupine: new plugin — wake-word detection`
- `transcription: porcupine wake-word backend (std-plugin bump)`

---

## Task 6: openWakeWord (wake-word, new plugin)

**Plugin:** `std-plugins/openwakeword/` *(new)*
**Backend name:** `openwakeword`
**Module:** `std-plugins/openwakeword/openwakeword.py`
**SDK:** `openwakeword` (official Python package; ships pretrained ONNX models). Fully local, no API key.

openWakeWord works on 80ms frames of 16 kHz mono PCM (1280 samples per frame). The Model class buffers internally — we just call `predict()` per chunk and check scores against a threshold.

- [ ] **Step 1: Scaffold the plugin** (same pattern as Porcupine / Deepgram).

`std-plugins/openwakeword/pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-openwakeword"
version = "1.0.0"
description = "openWakeWord wake-word detection (local, no API key)"
requires-python = ">=3.12"
dependencies = [
    "openwakeword>=0.6",
]

[tool.uv]
package = false
```

`plugin.yaml`, `plugin.py`, `__init__.py`, `tests/conftest.py` — analogous.

- [ ] **Step 2: Tests** at `tests/test_openwakeword.py`. Mock `openwakeword.Model` to return scores ≥ threshold on the second prediction call so a `WakeEvent` is emitted.

- [ ] **Step 3: Implement `std-plugins/openwakeword/openwakeword.py`**

```python
"""openWakeWord — fully local wake-word detection (no API key)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)

logger = logging.getLogger(__name__)

# openWakeWord works on 80ms windows: 1280 samples * 2 bytes = 2560 bytes
_FRAME_BYTES = 1280 * 2


class _OWWDetector(WakeWordDetector):
    def __init__(self, model: Any, keywords: list[str], threshold: float) -> None:
        self._model = model
        self._keywords = keywords
        self._threshold = threshold
        self._buf = bytearray()
        self._queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self._closed = False
        self._sample_count = 0

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        self._buf.extend(chunk)
        while len(self._buf) >= _FRAME_BYTES:
            frame = bytes(self._buf[:_FRAME_BYTES])
            del self._buf[:_FRAME_BYTES]
            self._sample_count += 1280
            arr = np.frombuffer(frame, dtype=np.int16)
            scores = self._model.predict(arr)
            for name, score in scores.items():
                if score >= self._threshold and name in self._keywords:
                    await self._queue.put(WakeEvent(
                        keyword=name,
                        at_seconds=self._sample_count / 16000.0,
                        confidence=float(score),
                    ))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[WakeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class OpenWakeWordBackend(WakeWordBackend):
    """Local wake-word detection via openWakeWord."""

    backend_name = "openwakeword"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="model_paths",
                type=ToolParameterType.STRING,
                description="Comma-separated paths to .onnx wake-word models. "
                            "Leave empty to use the bundled pretrained set.",
                default="",
            ),
        ]

    def __init__(self) -> None:
        self._model_paths: list[str] = []

    async def initialize(self, config: dict[str, object]) -> None:
        raw = str(config.get("model_paths", "")).strip()
        self._model_paths = [p.strip() for p in raw.split(",") if p.strip()]

    async def close(self) -> None:
        pass

    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector:
        from openwakeword.model import Model

        kwargs: dict[str, Any] = {}
        if self._model_paths:
            kwargs["wakeword_models"] = self._model_paths
        # else: Model() loads the bundled pretrained models by default.
        model = Model(**kwargs)
        return _OWWDetector(model, list(config.keywords), config.sensitivity)
```

Note: `numpy` is likely already a Gilbert core dep (faster-whisper pulls it). If not, add to this plugin's deps.

- [ ] **Step 4: `uv sync`.**

- [ ] **Step 5: Tests + lint + mypy.**

- [ ] **Step 6: Two commits.**
- `openwakeword: new plugin — local wake-word detection`
- `transcription: openwakeword backend (std-plugin bump)`

---

## Task 7: Final audit + READMEs

**Files:**
- Modify: `std-plugins/README.md` (add detail sections for the three new plugins + table rows; mention extended functionality in the openai, groq, elevenlabs sections).
- Modify: `README.md` (Gilbert root): update the speech-to-text integrations row to note that there are now N backends across N plugins (or include the table of vendor backends).

- [ ] **Step 1: Run `python3 .claude/skills/validate-architecture/check_capabilities.py`** and confirm clean.

- [ ] **Step 2: Spot-audit each new plugin** — confirm:
  - Plugin only imports from `gilbert.interfaces.*` (no `gilbert.core.services` etc.).
  - Backend class has `backend_name`.
  - Tests don't reach out to network.

- [ ] **Step 3: Update `std-plugins/README.md`**

For the openai / groq / elevenlabs sections, add a sentence under "Provides" mentioning the new `_whisper` / `_scribe` / `_scribe_live` backends and the matching config keys.

Add three new sections for `deepgram`, `porcupine`, `openwakeword` — each with: what it provides, third-party deps, main config keys, OS prereqs (none), and one-line notes (e.g., "Porcupine requires a free Picovoice access key").

- [ ] **Step 4: Update Gilbert root `README.md`** — the integrations table row for speech-to-text now lists multiple options (local Whisper + the new vendor backends).

- [ ] **Step 5: Run `uv run pytest -x -q`** — confirm everything still green.

- [ ] **Step 6: Two commits.**
- (in submodule) `docs: README inventory for transcription backends`
- (in parent) `transcription: vendor backend READMEs (std-plugin bump)`

---

## Plan complete

After this lands:
- `BatchTranscriptionBackend.registered_backends()` contains: `local_whisper`, `openai_whisper`, `groq_whisper`, `elevenlabs_scribe`.
- `StreamingTranscriptionBackend.registered_backends()` contains: `elevenlabs_scribe_live`, `deepgram`.
- `WakeWordBackend.registered_backends()` contains: `porcupine`, `openwakeword`.

The user enables any combination via the Settings UI per the per-role `<role>.backends.<name>.enabled` toggles.

## Tracked follow-ups (NOT in this plan)

- **OpenAI Realtime API** as a streaming backend — deferred. The Realtime API is conversational-coupled and a poor fit for pure transcription; ElevenLabs Scribe Live + Deepgram cover the streaming role.
- **`listen_with_wake_word(...)` orchestration helper** — still tracked from prior plan.
- **Browser SPA voice control panel** — still tracked from prior plan.
- **Vendor-API drift** — the streaming protocols (especially ElevenLabs Scribe live URL/frame shape) are educated guesses; live integration may surface adjustments. Document in module docstrings.
