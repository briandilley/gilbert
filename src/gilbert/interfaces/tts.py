"""Text-to-speech interface — convert text into audio."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam

# ── Shared audio-silence helpers ─────────────────────────────────────
#
# These sit in ``interfaces/`` because both the core TTS service and
# plugin tests need them. Generating a block of MP3/PCM silence is
# pure and vendor-agnostic — no TTS backend or network call involved.

_PCM_SAMPLE_RATE = 44100


class AudioFormat(StrEnum):
    """Supported audio output formats."""

    MP3 = "mp3"
    WAV = "wav"
    OGG = "ogg"
    PCM = "pcm"


def generate_pcm_silence(seconds: float) -> bytes:
    """Generate raw 16-bit PCM silence at 44100 Hz."""
    return b"\x00\x00" * int(_PCM_SAMPLE_RATE * seconds)


def generate_mp3_silence(seconds: float) -> bytes:
    """Generate minimal valid MP3 silence frames (MPEG1 Layer 3, 128kbps, 44100 Hz)."""
    frame_samples = 1152
    frames_needed = int((_PCM_SAMPLE_RATE * seconds) / frame_samples) + 1
    header = b"\xff\xfb\x90\xc0"
    frame = header + b"\x00" * 413  # 417-byte frame: 4 header + 413 payload
    return frame * frames_needed


def append_silence(audio: bytes, fmt: "AudioFormat", seconds: float) -> bytes:
    """Append silence padding to audio data.

    Used by the TTS service so speakers don't cut off the last word.
    Lives in ``interfaces/`` so services and plugin-side tests can
    share the exact same implementation.
    """
    if seconds <= 0:
        return audio
    if fmt == AudioFormat.MP3:
        return audio + generate_mp3_silence(seconds)
    if fmt in (AudioFormat.PCM, AudioFormat.WAV):
        return audio + generate_pcm_silence(seconds)
    return audio


@dataclass(frozen=True)
class Voice:
    """A voice available for synthesis."""

    voice_id: str
    name: str
    language: str | None = None
    description: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisRequest:
    """Parameters for a text-to-speech synthesis call."""

    text: str
    voice_id: str
    output_format: AudioFormat = AudioFormat.MP3
    speed: float = 1.0
    stability: float | None = None
    similarity_boost: float | None = None
    # Optional caller-provided context describing the situation/mood
    # of the text — e.g. "celebratory end-of-day announcement",
    # "doorbell ring at the front door", "sarcastic reply to a
    # customer email". Backends may use it to inform delivery
    # decisions (the ElevenLabs backend feeds it to the audio-tag
    # director). Backends that don't tag should ignore it.
    context: str = ""


@dataclass(frozen=True)
class SynthesisResult:
    """Result of a text-to-speech synthesis call."""

    audio: bytes
    format: AudioFormat
    duration_seconds: float | None = None
    characters_used: int | None = None


class TTSBackend(ABC):
    """Abstract text-to-speech backend. Implementation-agnostic."""

    _registry: dict[str, type["TTSBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            TTSBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["TTSBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the backend with provider-specific configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    @abstractmethod
    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize speech from text."""
        ...

    @abstractmethod
    async def list_voices(self) -> list[Voice]:
        """List available voices."""
        ...

    @abstractmethod
    async def get_voice(self, voice_id: str) -> Voice | None:
        """Get a voice by ID, or None if not found."""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    """Protocol for text-to-speech synthesis from a service."""

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize speech from text."""
        ...


@runtime_checkable
class AICapableTTSBackend(Protocol):
    """Protocol for TTS backends that want an ``AISamplingProvider`` to
    use for text preprocessing — e.g. injecting ElevenLabs v3 audio
    tags via a small model. The TTS service injects the sampling
    provider after ``initialize()`` on backends that satisfy this
    protocol; backends that don't get nothing extra and their behavior
    is unchanged.
    """

    def set_ai_sampling(self, ai: object) -> None:
        """Receive the AI sampling provider for one-shot completions.

        Typed as ``object`` to keep ``interfaces/tts.py`` from importing
        ``interfaces/ai.py``; concrete implementations narrow at the
        boundary with ``isinstance(ai, AISamplingProvider)``.
        """
        ...


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
