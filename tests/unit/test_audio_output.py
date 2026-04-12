"""Tests for AudioOutputService — chat and speaker audio delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gilbert.core.services.audio_output import AudioOutputService
from gilbert.interfaces.tts import AudioFormat, SynthesisRequest, SynthesisResult


class FakeTTS:
    """In-memory TTSProvider stand-in."""

    def __init__(
        self,
        audio: bytes = b"ID3FAKEMP3BYTES",
        fmt: AudioFormat = AudioFormat.MP3,
        duration: float | None = 12.5,
        raise_exc: Exception | None = None,
    ) -> None:
        self.audio = audio
        self.fmt = fmt
        self.duration = duration
        self.raise_exc = raise_exc
        self.calls: list[SynthesisRequest] = []

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return SynthesisResult(
            audio=self.audio,
            format=self.fmt,
            duration_seconds=self.duration,
        )


class FakeSpeaker:
    """SpeakerProvider stand-in with observable announce() calls."""

    def __init__(
        self,
        raise_exc: Exception | None = None,
        result: str = "/tmp/announce-xyz.mp3",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_exc = raise_exc
        self.result = result
        self.backend = None  # satisfies SpeakerProvider protocol

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
    ) -> str:
        self.calls.append(
            {"text": text, "speaker_names": speaker_names, "volume": volume}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


class FakeResolver:
    def __init__(
        self,
        tts: FakeTTS | None = None,
        speaker: FakeSpeaker | None = None,
    ) -> None:
        self._caps: dict[str, Any] = {}
        if tts is not None:
            self._caps["text_to_speech"] = tts
        if speaker is not None:
            self._caps["speaker_control"] = speaker

    def get_capability(self, cap: str) -> Any:
        return self._caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self._caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc


# --- Service metadata ---


def test_service_info_capabilities() -> None:
    svc = AudioOutputService()
    info = svc.service_info()
    assert info.name == "audio_output"
    assert "ai_tools" in info.capabilities
    # Both TTS and speaker are optional — the service still starts
    # cleanly even if one is missing, and fails gracefully at tool time
    assert "text_to_speech" in info.optional
    assert "speaker_control" in info.optional


def test_tool_provider_name() -> None:
    assert AudioOutputService().tool_provider_name == "audio_output"


def test_get_tools_has_audio_output_with_required_role_user() -> None:
    svc = AudioOutputService()
    tools = svc.get_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "audio_output"
    assert tool.required_role == "user"
    param_names = {p.name for p in tool.parameters}
    assert {"text", "destination", "volume", "speaker_names"} == param_names
    # text is the only required parameter
    text_param = next(p for p in tool.parameters if p.name == "text")
    assert text_param.required is True
    dest_param = next(p for p in tool.parameters if p.name == "destination")
    assert dest_param.required is False
    assert dest_param.enum == ["chat", "speakers"]


# --- Chat destination ---


@pytest.mark.asyncio
async def test_chat_destination_writes_file_and_returns_markdown_link(
    tmp_path: Path,
) -> None:
    svc = AudioOutputService()
    tts = FakeTTS(audio=b"mp3bytes", duration=8.3)
    svc._resolver = FakeResolver(tts=tts)  # type: ignore[assignment]

    with patch("gilbert.core.services.audio_output.get_output_dir") as m_get_dir:
        m_get_dir.return_value = tmp_path
        result = await svc.execute_tool(
            "audio_output", {"text": "Hello world", "destination": "chat"}
        )

    # TTS was called with voice_id="" (default voice) and MP3 format
    assert len(tts.calls) == 1
    req = tts.calls[0]
    assert req.text == "Hello world"
    assert req.voice_id == ""
    assert req.output_format == AudioFormat.MP3

    # File was written
    written = list(tmp_path.glob("audio-*.mp3"))
    assert len(written) == 1
    assert written[0].read_bytes() == b"mp3bytes"

    # Response contains a relative /output/audio/... URL and duration
    assert "[▶ Play or download](/output/audio/audio-" in result
    assert ".mp3)" in result
    assert "8s" in result  # duration, rounded


@pytest.mark.asyncio
async def test_chat_destination_is_default_when_destination_omitted(
    tmp_path: Path,
) -> None:
    svc = AudioOutputService()
    tts = FakeTTS()
    svc._resolver = FakeResolver(tts=tts)  # type: ignore[assignment]

    with patch("gilbert.core.services.audio_output.get_output_dir") as m_get_dir:
        m_get_dir.return_value = tmp_path
        result = await svc.execute_tool("audio_output", {"text": "default dest test"})

    assert len(tts.calls) == 1
    assert "[▶ Play or download](/output/audio/" in result


@pytest.mark.asyncio
async def test_chat_destination_no_tts_capability_fails_gracefully(
    tmp_path: Path,
) -> None:
    svc = AudioOutputService()
    svc._resolver = FakeResolver(tts=None)  # type: ignore[assignment]

    result = await svc.execute_tool(
        "audio_output", {"text": "hello", "destination": "chat"}
    )
    assert "Text-to-speech is not available" in result
    # No file should have been written
    audio_dir = tmp_path / "audio"
    if audio_dir.exists():
        assert list(audio_dir.glob("*.mp3")) == []


@pytest.mark.asyncio
async def test_chat_destination_tts_raises_returns_error_message(
    tmp_path: Path,
) -> None:
    svc = AudioOutputService()
    tts = FakeTTS(raise_exc=RuntimeError("TTS boom"))
    svc._resolver = FakeResolver(tts=tts)  # type: ignore[assignment]

    with patch("gilbert.core.services.audio_output.get_output_dir") as m_get_dir:
        m_get_dir.return_value = tmp_path
        result = await svc.execute_tool(
            "audio_output", {"text": "hello", "destination": "chat"}
        )
    assert "Failed to synthesize audio" in result
    assert list(tmp_path.glob("audio-*.mp3")) == []


@pytest.mark.asyncio
async def test_chat_destination_no_duration_still_works(tmp_path: Path) -> None:
    svc = AudioOutputService()
    tts = FakeTTS(duration=None)
    svc._resolver = FakeResolver(tts=tts)  # type: ignore[assignment]

    with patch("gilbert.core.services.audio_output.get_output_dir") as m_get_dir:
        m_get_dir.return_value = tmp_path
        result = await svc.execute_tool(
            "audio_output", {"text": "no duration"}
        )
    # Duration string omitted, but link still present
    assert "[▶ Play or download](/output/audio/audio-" in result
    assert "Audio ready." in result  # no parenthetical duration


# --- Speaker destination ---


@pytest.mark.asyncio
async def test_speaker_destination_calls_announce_with_params() -> None:
    svc = AudioOutputService()
    speaker = FakeSpeaker()
    svc._resolver = FakeResolver(speaker=speaker)  # type: ignore[assignment]

    result = await svc.execute_tool(
        "audio_output",
        {
            "text": "Big announcement",
            "destination": "speakers",
            "volume": 60,
            "speaker_names": ["Kitchen", "Shop Floor"],
        },
    )

    assert len(speaker.calls) == 1
    call = speaker.calls[0]
    assert call["text"] == "Big announcement"
    assert call["volume"] == 60
    assert call["speaker_names"] == ["Kitchen", "Shop Floor"]
    assert "Played on speakers" in result
    assert "Big announcement" in result


@pytest.mark.asyncio
async def test_speaker_destination_without_volume_or_speakers() -> None:
    svc = AudioOutputService()
    speaker = FakeSpeaker()
    svc._resolver = FakeResolver(speaker=speaker)  # type: ignore[assignment]

    await svc.execute_tool(
        "audio_output", {"text": "no extras", "destination": "speakers"}
    )

    call = speaker.calls[0]
    assert call["volume"] is None
    assert call["speaker_names"] is None  # falls through to service defaults


@pytest.mark.asyncio
async def test_speaker_destination_no_speaker_capability_fails_gracefully() -> None:
    svc = AudioOutputService()
    svc._resolver = FakeResolver(speaker=None)  # type: ignore[assignment]

    result = await svc.execute_tool(
        "audio_output", {"text": "hi", "destination": "speakers"}
    )
    assert "Speaker control is not available" in result


@pytest.mark.asyncio
async def test_speaker_destination_announce_raises_returns_error() -> None:
    svc = AudioOutputService()
    speaker = FakeSpeaker(raise_exc=RuntimeError("sonos dead"))
    svc._resolver = FakeResolver(speaker=speaker)  # type: ignore[assignment]

    result = await svc.execute_tool(
        "audio_output", {"text": "hi", "destination": "speakers"}
    )
    assert "Failed to play audio on speakers" in result


@pytest.mark.asyncio
async def test_speaker_destination_preview_truncates_long_text() -> None:
    svc = AudioOutputService()
    speaker = FakeSpeaker()
    svc._resolver = FakeResolver(speaker=speaker)  # type: ignore[assignment]

    long_text = ("This is a very long announcement " * 10).strip()
    result = await svc.execute_tool(
        "audio_output", {"text": long_text, "destination": "speakers"}
    )
    # Preview is truncated to 80 chars plus ellipsis
    assert "..." in result
    # But the full text was still sent to the speaker service
    assert speaker.calls[0]["text"] == long_text


@pytest.mark.asyncio
async def test_speaker_volume_coerces_string_to_int() -> None:
    """AI tool calls sometimes pass numeric args as strings."""
    svc = AudioOutputService()
    speaker = FakeSpeaker()
    svc._resolver = FakeResolver(speaker=speaker)  # type: ignore[assignment]

    await svc.execute_tool(
        "audio_output",
        {"text": "hi", "destination": "speakers", "volume": "75"},
    )
    assert speaker.calls[0]["volume"] == 75


@pytest.mark.asyncio
async def test_speaker_invalid_volume_falls_back_to_none() -> None:
    svc = AudioOutputService()
    speaker = FakeSpeaker()
    svc._resolver = FakeResolver(speaker=speaker)  # type: ignore[assignment]

    await svc.execute_tool(
        "audio_output",
        {"text": "hi", "destination": "speakers", "volume": "loud"},
    )
    assert speaker.calls[0]["volume"] is None


# --- Error and edge cases ---


@pytest.mark.asyncio
async def test_unknown_tool_raises_key_error() -> None:
    svc = AudioOutputService()
    with pytest.raises(KeyError):
        await svc.execute_tool("not_my_tool", {})


@pytest.mark.asyncio
async def test_empty_text_returns_friendly_message() -> None:
    svc = AudioOutputService()
    svc._resolver = FakeResolver(tts=FakeTTS())  # type: ignore[assignment]

    result = await svc.execute_tool("audio_output", {"text": "   "})
    assert "No text provided" in result


@pytest.mark.asyncio
async def test_missing_text_argument_returns_friendly_message() -> None:
    svc = AudioOutputService()
    svc._resolver = FakeResolver(tts=FakeTTS())  # type: ignore[assignment]

    result = await svc.execute_tool("audio_output", {})
    assert "No text provided" in result


@pytest.mark.asyncio
async def test_unknown_destination_returns_error() -> None:
    svc = AudioOutputService()
    svc._resolver = FakeResolver(tts=FakeTTS(), speaker=FakeSpeaker())  # type: ignore[assignment]

    result = await svc.execute_tool(
        "audio_output", {"text": "hi", "destination": "telegram"}
    )
    assert "Unknown destination" in result
    assert "telegram" in result


@pytest.mark.asyncio
async def test_resolver_none_returns_friendly_error() -> None:
    svc = AudioOutputService()
    # Never started, _resolver is None
    result = await svc.execute_tool("audio_output", {"text": "hi"})
    assert "not ready" in result
