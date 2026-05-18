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

import audioop
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


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


# --- Audio helpers ---------------------------------------------------
#
# Pure, vendor-free. Live in ``interfaces/`` so both the core service
# and plugin tests can use them without depending on any backend.
# Mirrors how ``interfaces/tts.py`` ships ``append_silence``.


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


# --- Streaming / detector primitive ABCs -----------------------------


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

    _registry: dict[str, type[BatchTranscriptionBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            BatchTranscriptionBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[BatchTranscriptionBackend]]:
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

    _registry: dict[str, type[StreamingTranscriptionBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            StreamingTranscriptionBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[StreamingTranscriptionBackend]]:
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

    _registry: dict[str, type[WakeWordBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            WakeWordBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[WakeWordBackend]]:
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
