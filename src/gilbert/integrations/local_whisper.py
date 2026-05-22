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
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]

        self._model_size = str(config.get("model_size", "base"))
        compute_type = str(config.get("compute_type", "int8"))
        device = str(config.get("device", "cpu"))
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: WhisperModel(self._model_size, device=device, compute_type=compute_type),
        )
        logger.info(
            "LocalWhisperBackend initialized: model=%s device=%s compute=%s",
            self._model_size, device, compute_type,
        )

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
