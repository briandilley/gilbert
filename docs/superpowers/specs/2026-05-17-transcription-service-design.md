# TranscriptionService — Design

**Status:** approved (brainstorm) — ready for implementation plan
**Date:** 2026-05-17
**Owner:** brian

## Goal

Add a first-class speech-to-text capability to Gilbert with the same shape and rigor as `TTSService` / `SpeakerService`. The service must cover four use cases on day one (in scope) and leave room for a fifth (wake-word orchestration / voice UI) without rework.

### In-scope use cases

1. **Browser voice control + AI tool** — user holds a mic in the SPA → transcript flows into chat or fires a slash command; the AI can also call a `transcribe` tool itself.
2. **Batch file transcription** — drop an mp3/wav/m4a/mp4 into chat or knowledge → transcript.
3. **Service-to-service** — voicemail, doorbell, future phone integration hand bytes in, get text back.
4. **Live meeting / continuous-stream** — long-running stream from a tab or room mic with diarization metadata.

Plus: a `WakeWordBackend` primitive so wake-word detection can be wired in without revisiting the interface.

## Architecture

Three backend ABCs in `interfaces/transcription.py`, each a separate registry following the universal backend pattern (`__init_subclass__` registry + `backend_config_params()`):

- **`BatchTranscriptionBackend`** — `transcribe(request) → result`. One-shot bytes-in/text-out. Whisper local, OpenAI Whisper API, ElevenLabs Scribe batch mode.
- **`StreamingTranscriptionBackend`** — `open_stream(config) → TranscriptionStream`. Caller pushes chunks via `session.send(chunk)`, reads events via `async for ev in session.events()`. Deepgram, ElevenLabs Scribe live, OpenAI Realtime.
- **`WakeWordBackend`** — `open_detector(config) → WakeWordDetector`. Continuous-listen: caller pushes chunks, reads `WakeEvent`s. Porcupine, openWakeWord.

A single class may inherit from multiple ABCs (e.g., Deepgram = Batch + Streaming).

`TranscriptionService` (in `core/services/transcription.py`) is the **aggregator** — loads any number of backends from each registry based on per-role `enabled: [...]` config, holds a configurable default backend per role, and exposes the union as a discoverable service.

Capabilities published: `frozenset({"speech_to_text", "ai_tools", "ws_handlers"})`.
Optional deps: `frozenset({"configuration", "event_bus", "access_control"})`.
Toggleable: yes (`toggle_description="Speech-to-text transcription"`).

## Interface (`interfaces/transcription.py`)

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


class AudioEncoding(StrEnum):
    PCM_S16LE = "pcm_s16le"   # raw 16-bit little-endian PCM
    OPUS      = "opus"        # browser-friendly
    MP3       = "mp3"
    WAV       = "wav"
    M4A       = "m4a"
    OGG       = "ogg"
    WEBM      = "webm"
    AUTO      = "auto"        # batch only — backend sniffs container


@dataclass(frozen=True)
class AudioFormat:
    encoding: AudioEncoding
    sample_rate: int = 16000
    channels: int = 1


# --- Batch ---

@dataclass(frozen=True)
class TranscriptionRequest:
    audio: bytes
    format: AudioFormat = AudioFormat(AudioEncoding.AUTO)
    language: str | None = None        # BCP-47 ("en", "es-MX"); None = auto-detect
    prompt: str = ""                   # optional bias text (vocabulary / style)
    diarize: bool = False
    word_timestamps: bool = False
    context: str = ""                  # free-form caller hint, mirrors TTS


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start_seconds: float
    end_seconds: float
    speaker_label: str = ""            # "" when diarization off / unsupported
    confidence: float | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str                          # full concatenated transcript
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = ""                 # detected or echoed back
    duration_seconds: float | None = None
    audio_seconds_used: float | None = None   # for future usage tracking


# --- Streaming ---

@dataclass(frozen=True)
class StreamConfig:
    format: AudioFormat                # what the caller will send
    language: str | None = None
    prompt: str = ""
    diarize: bool = False
    interim_results: bool = True       # emit PartialTranscript
    vad_events: bool = True            # emit SpeechStarted/SpeechEnded


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
    PartialTranscript | FinalTranscript | SpeechStarted | SpeechEnded | TranscriptionError
)


class TranscriptionStream(ABC):
    @abstractmethod
    async def send(self, chunk: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None:
        """Signal end-of-audio. After close, events() drains any final events."""

    @abstractmethod
    def events(self) -> AsyncIterator[TranscriptionEvent]: ...


# --- Wake word ---

@dataclass(frozen=True)
class WakeWordConfig:
    keywords: list[str]                # e.g. ["hey gilbert", "computer"]
    format: AudioFormat                # most engines want 16kHz mono PCM
    sensitivity: float = 0.5           # 0..1


@dataclass(frozen=True)
class WakeEvent:
    keyword: str
    at_seconds: float
    confidence: float | None = None


class WakeWordDetector(ABC):
    @abstractmethod
    async def send(self, chunk: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[WakeEvent]: ...


# --- Backend ABCs with registries ---

class BatchTranscriptionBackend(ABC):
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
        """Optional: best-effort list of supported languages. Default empty."""
        return []


class StreamingTranscriptionBackend(ABC):
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


# --- Capability protocols (consumers isinstance-check these) ---

@runtime_checkable
class BatchTranscriber(Protocol):
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

**Shared helpers** in the same module (vendor-free, like `tts.append_silence`):

- `resample_pcm(audio: bytes, src_rate: int, dst_rate: int) -> bytes`
- `pcm_silence(seconds: float, sample_rate: int) -> bytes`

## Service (`core/services/transcription.py`)

### Shape

```python
class TranscriptionService(Service):
    def __init__(self) -> None:
        self._batch_backends: dict[str, BatchTranscriptionBackend] = {}
        self._streaming_backends: dict[str, StreamingTranscriptionBackend] = {}
        self._wake_word_backends: dict[str, WakeWordBackend] = {}
        self._default_batch: str = ""
        self._default_streaming: str = ""
        self._default_wake_word: str = ""
        self._enabled: bool = False
        self._output_ttl_seconds: int = 3600
        # Active browser-mic sessions, keyed by WsConnection conn_id.
        # All per-session state lives here — never request-scoped on self.
        self._sessions: dict[str, _ActiveSession] = {}
        self._sessions_guard = asyncio.Lock()
        self._startup_failures: dict[str, str] = {}
        self._event_bus_provider: Any = None
        self._access_control: AccessControlProvider | None = None
```

`_ActiveSession` is a private dataclass holding `conn_id`, `user_id` (from `WsConnection.user_ctx`), `mode` (`"stream"` or `"wake_word"`), the underlying primitive (`TranscriptionStream` or `WakeWordDetector`), and the pump task that drains `events()` and emits `transcription.event` back to that connection.

### ServiceInfo

```python
ServiceInfo(
    name="transcription",
    capabilities=frozenset({"speech_to_text", "ai_tools", "ws_handlers"}),
    optional=frozenset({"configuration", "event_bus", "access_control"}),
    toggleable=True,
    toggle_description="Speech-to-text transcription",
)
```

### Configuration (`config_namespace = "transcription"`, `config_category = "Media"`)

| key | type | notes |
|---|---|---|
| `batch.enabled`         | LIST(string) | Multi-select dropdown from `BatchTranscriptionBackend.registered_backends()` |
| `batch.default`         | STRING       | Dropdown from loaded batch backends — used when caller omits `backend=` |
| `streaming.enabled`     | LIST(string) | Same shape for streaming |
| `streaming.default`     | STRING       | |
| `wake_word.enabled`     | LIST(string) | Same shape for wake-word |
| `wake_word.default`     | STRING       | |
| `output_ttl_seconds`    | NUMBER       | Cleanup window for transient transcript files |
| `settings.<backend>.<param>` | various | Wrapped from each loaded backend's `backend_config_params()`, forwarding `restart_required`, `sensitive`, `choices`, `multiline`, `ai_prompt`, `backend_param=True` — same pattern as `SpeakerService.config_params()`. |

Conditional blocks (per-backend `settings.*`) wrap inside bordered/tinted containers in the settings UI so the dependency on `<role>.enabled` is visually obvious (see the existing settings convention).

### Public API

Implements `BatchTranscriber`, `StreamingTranscriber`, `WakeWordListener`:

```python
async def transcribe(self, request: TranscriptionRequest,
                     backend: str | None = None) -> TranscriptionResult: ...

async def open_stream(self, config: StreamConfig,
                      backend: str | None = None) -> TranscriptionStream: ...

async def open_detector(self, config: WakeWordConfig,
                        backend: str | None = None) -> WakeWordDetector: ...

def list_backends(self, role: str | None = None) -> dict[str, list[str]]:
    """Returns {role: [backend_names]} for role in {"batch","streaming","wake_word"}
    or all roles if role is None."""
```

Each public method resolves `backend` → falls back to `self._default_<role>` → raises `RuntimeError("no transcription backend available for <role>")` if neither.

The streaming/wake-word methods return the backend's primitive directly (no service-level wrapper). The batch method does not wrap either — there is no padding-equivalent step like TTS's silence.

### Browser-mic WS RPCs

`TranscriptionService` implements `WsHandlerProvider`. RPCs:

| RPC | direction | payload |
|---|---|---|
| `transcription.start_session` | client→server | `{mode: "stream"|"wake_word", format: AudioFormat, config: StreamConfig|WakeWordConfig, backend?: str}` → returns `{session_id}` |
| `transcription.send_chunk`    | client→server | `{session_id, audio_b64: str}` (JSON-wrapped for v1; framed-binary optimization deferred) |
| `transcription.close_session` | client→server | `{session_id}` |
| `transcription.event`         | server→client | `{session_id, event: TranscriptionEvent|WakeEvent|Error}` |

`session_id` is server-minted (UUID), opaque to clients. Each session record is keyed primarily by `conn_id` (one user can hold N concurrent sessions on one WS connection; the session_id disambiguates).

On `WsDisconnect`, the service closes any sessions for that `conn_id` — mirrors `BrowserSpeakerBackend` cleanup.

`required_role="everyone"` on all RPCs in v1.

### Concurrency / multi-user

- All per-session state lives in `self._sessions[conn_id]`. **No** request-scoped data on `self`.
- Pump tasks spawned with `context=contextvars.copy_context()` so logging/trace context carries through.
- Per-session locks are not needed: each session has a single pump task and a single producer (the WS handler).

### Backend lifecycle

- `start()` walks `<role>.enabled` lists, instantiates each backend class from the registry, calls `initialize(self._config_for(backend))`, records failures in `self._startup_failures`.
- `stop()` calls `close()` on every loaded backend.
- `on_config_changed()`: when `<role>.enabled` changes or a backend's `settings.*` change, **reinitialize only that backend in place** (no full service restart) — same pattern as the recently-merged multi-backend `SpeakerService`.

### Config actions

`config_actions()` returns `all_backend_actions(registry, current_backend=…)` for each role (test-connection probes, etc.) — same shape as `TTSService.config_actions()`.

## Tools / slash commands

`ToolProvider`, `tool_provider_name = "transcription"`. Per-tool `slash_group="transcription"` (no service-level `slash_namespace` — that's plugin-only per the rulebook; core services scope via each tool's `slash_group`).

Every tool sets `slash_help` (one-line autocomplete hint), `required_role`, and `parallel_safe` explicitly.

| Tool / slash | Args (in shell-friendly order) | Notes |
|---|---|---|
| `/transcription transcribe <source>` | `source` (path or URL), `language?`, `diarize?`, `backend?` | Batch-transcribe a file. Writes transcript to `get_output_dir("transcription")`, returns `{file_path, text, segments, language, duration_seconds}`. `required_role="everyone"`, `parallel_safe=True`. |
| `/transcription backends` | `role?` | Lists loaded backends per role, indicating which is the default. |
| `/transcription languages` | `backend?` | Calls `backend.list_languages()` on the named (or default) batch backend. |

Live streaming and wake-word are **not** slash commands — they are WS-driven. Per the rulebook, WS handlers are not required to declare `slash_command`.

## Bundled backend

`src/gilbert/integrations/local_whisper.py` — concrete `BatchTranscriptionBackend` using `faster-whisper` (CPU-OK). Registered side-effect from `app.py`:

```python
import gilbert.integrations.local_whisper  # noqa: F401
```

Runtime model download is handled lazily on first use; binary deps (if any beyond pip-installable wheels) declared via `Plugin.runtime_dependencies()` *if* this ends up needing one — for `faster-whisper` on CPU, the wheel is self-contained, so no `runtime_dependencies()` entry is expected.

## Tests

- `tests/unit/test_transcription_interfaces.py` — dataclass roundtrips, `resample_pcm`, `pcm_silence` helpers.
- `tests/unit/test_transcription_service.py` — routing to defaults, per-call `backend=` override, "no backend available" error path, WS RPC session lifecycle (open → send chunks → events flow → close), cleanup on `WsDisconnect`, multi-user isolation (two concurrent sessions on different `conn_id`s don't cross-talk). Uses fake backends defined inline.
- `tests/integration/test_local_whisper.py` — small fixture WAV → asserts a known phrase appears in the result. Skipped if model can't be loaded in CI.

## Docs to update in the same PR

- `README.md` — integrations table gets a speech-to-text row.
- `CLAUDE.md` — interfaces list gets `transcription.py`.
- `docs/architecture/transcription-system.md` *(new)* — analogous to `speaker-system.md`: three-ABC topology, multi-backend aggregator, browser-mic session lifecycle, deliberate v1 omissions.

## Out of scope for v1 (deliberate cuts)

- **Wake-word orchestration helper** (`listen_with_wake_word(...)`) — primitives only; chain in consumer code. Will land once a real consumer (voice panel) exists, so the helper's shape can be informed by use.
- **Frontend / SPA voice panel** — WS RPCs work server-side; no UI consumes them yet. Voice UX deserves its own brainstorm.
- **Transcript persistence / search** — `transcribe` writes a transient file under `output_ttl_seconds` cleanup; no entity collection, no knowledge ingest. If transcripts should be searchable, knowledge ingest is the right boundary.
- **Streaming `transcribe` for huge files** — batch path loads whole file into memory. Fine for chat attachments and voicemails; revisit when a real caller hits the limit.
- **Speaker-name resolution** — `diarize=True` is plumbed; mapping `"speaker_0" → "Brian"` is out.
- **Live translation** — `language` is a source hint only; no target-language field.
- **Usage / cost dashboards** — `audio_seconds_used` is on the result for backends to report, but no aggregation.
- **Per-user concurrent-session caps** — first user to open many sessions can exhaust backend capacity; add when usage is real.
- **Non-browser live audio sources** (SIP/RTP, phone, room mic) — batch handles bytes-from-anywhere; live non-browser sources are speculative.

## Wiring summary

1. New file `src/gilbert/interfaces/transcription.py`.
2. New file `src/gilbert/core/services/transcription.py`.
3. New file `src/gilbert/integrations/local_whisper.py`.
4. `app.py`:
   - `import gilbert.integrations.local_whisper  # noqa: F401`
   - register `TranscriptionService()` in the service list.
5. `pyproject.toml` — add `faster-whisper` to core deps.
6. Tests as listed.
7. Doc updates as listed.

No frontend changes. No plugin changes (std-plugin extensions for OpenAI / ElevenLabs Scribe / Deepgram / Porcupine are follow-up PRs).
