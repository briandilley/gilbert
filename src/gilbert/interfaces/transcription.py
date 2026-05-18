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
