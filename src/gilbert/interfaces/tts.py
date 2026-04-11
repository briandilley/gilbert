"""Text-to-speech interface — convert text into audio."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class AudioFormat(StrEnum):
    """Supported audio output formats."""

    MP3 = "mp3"
    WAV = "wav"
    OGG = "ogg"
    PCM = "pcm"


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
    def backend_config_params(cls) -> list["ConfigParam"]:
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
