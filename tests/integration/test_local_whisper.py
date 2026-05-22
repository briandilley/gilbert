"""Integration test for the bundled local Whisper backend.

Skipped automatically if the model can't be loaded (network restricted,
disk full, etc.) so CI without model cache doesn't fail.
"""

from pathlib import Path

import pytest

faster_whisper = pytest.importorskip("faster_whisper")

from gilbert.integrations.local_whisper import LocalWhisperBackend  # noqa: E402
from gilbert.interfaces.transcription import (  # noqa: E402
    AudioEncoding,
    AudioFormat,
    TranscriptionRequest,
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
