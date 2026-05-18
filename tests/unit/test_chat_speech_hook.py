"""Tests for AIService._speak_response — the chat-turn TTS hook."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from gilbert.core.services.ai import AIService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.speaker import SpeakerInfo


def _make_user_ctx(user_id: str) -> UserContext:
    """Construct a UserContext compatible with this repo's signature."""
    return UserContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id.title(),
        roles=frozenset(),
    )


@dataclass
class _TTSResult:
    audio: bytes
    format: str = "mp3"


class _FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[Any] = []

    async def synthesize(self, request: Any) -> Any:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("tts boom")
        return _TTSResult(audio=b"FAKEMP3")


class _FakeSpeaker:
    def __init__(self, browser_users: list[str] | None = None) -> None:
        self.list_speakers_calls = 0
        self.play_calls: list[dict] = []
        self._browser_users = browser_users or []

    async def list_speakers(self) -> list[SpeakerInfo]:
        self.list_speakers_calls += 1
        return [
            SpeakerInfo(speaker_id=f"browser:{u}", name=f"{u}'s Browser", ip_address="")
            for u in self._browser_users
        ]

    async def play_on_speakers(self, **kwargs: Any) -> None:
        self.play_calls.append(kwargs)

    def audio_url(self, file_path: str) -> str:
        return f"http://test/output/{file_path.rsplit('/', 1)[-1]}"


class _FakeResolver:
    def __init__(self, **caps: Any) -> None:
        self._caps = caps

    def get_capability(self, name: str) -> Any:
        return self._caps.get(name)


def _make_service(*, tts: Any = None, speaker: Any = None) -> AIService:
    svc = AIService()
    svc._resolver = _FakeResolver(text_to_speech=tts, speaker_control=speaker)
    return svc


@pytest.mark.asyncio
async def test_speak_response_routes_audio_to_browser_speaker() -> None:
    tts = _FakeTTS()
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=tts, speaker=speaker)
    user = _make_user_ctx("alice")
    await svc._speak_response(user, "conv-1", "Hello world.")
    assert len(tts.calls) == 1
    assert len(speaker.play_calls) == 1
    call = speaker.play_calls[0]
    assert call["speaker_ids"] == ["browser:alice"]
    assert call["kind"] == "chat_speech"


@pytest.mark.asyncio
async def test_speak_response_skips_when_no_browser_speaker() -> None:
    tts = _FakeTTS()
    speaker = _FakeSpeaker(browser_users=[])
    svc = _make_service(tts=tts, speaker=speaker)
    user = _make_user_ctx("alice")
    await svc._speak_response(user, "conv-1", "Hello world.")
    assert tts.calls == []
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_speak_response_skips_when_text_is_only_code() -> None:
    tts = _FakeTTS()
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=tts, speaker=speaker)
    user = _make_user_ctx("alice")
    await svc._speak_response(user, "conv-1", "```py\nx = 1\n```")
    assert tts.calls == []
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_speak_response_swallows_tts_errors() -> None:
    tts = _FakeTTS(fail=True)
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=tts, speaker=speaker)
    user = _make_user_ctx("alice")
    # Should not raise.
    await svc._speak_response(user, "conv-1", "Hello world.")
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_speak_response_noops_without_tts_capability() -> None:
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=None, speaker=speaker)
    user = _make_user_ctx("alice")
    await svc._speak_response(user, "conv-1", "Hello world.")
    assert speaker.play_calls == []
