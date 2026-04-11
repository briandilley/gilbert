"""Tests for TTSService and TTS config parsing."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.services.tts import TTSService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    Voice,
)


class StubTTSBackend(TTSBackend):
    """In-memory TTS backend for testing."""

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.init_config: dict[str, object] = {}
        self.last_request: SynthesisRequest | None = None
        self._voices: list[Voice] = [
            Voice(voice_id="v1", name="Alice", language="en"),
            Voice(voice_id="v2", name="Bob", language="en", description="Deep voice"),
        ]

    async def initialize(self, config: dict[str, object]) -> None:
        self.init_config = config
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.last_request = request
        return SynthesisResult(
            audio=b"fake-audio-data",
            format=request.output_format,
            characters_used=len(request.text),
        )

    async def list_voices(self) -> list[Voice]:
        return list(self._voices)

    async def get_voice(self, voice_id: str) -> Voice | None:
        for v in self._voices:
            if v.voice_id == voice_id:
                return v
        return None


@pytest.fixture
def stub_backend() -> StubTTSBackend:
    return StubTTSBackend()


@pytest.fixture
def resolver() -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)
    mock.require_capability.side_effect = LookupError("not available")
    mock.get_capability.return_value = None
    return mock


@pytest.fixture
def service(stub_backend: StubTTSBackend) -> TTSService:
    svc = TTSService(stub_backend)
    svc._config = {"api_key": "sk-test-key"}
    svc._silence_padding = 0.0  # disable for tests
    return svc


# --- Service info ---


def test_service_info(service: TTSService) -> None:
    info = service.service_info()
    assert info.name == "tts"
    assert "text_to_speech" in info.capabilities
    assert "ai_tools" in info.capabilities


# --- Lifecycle ---


async def test_start_initializes_backend(
    stub_backend: StubTTSBackend, resolver: ServiceResolver
) -> None:
    svc = TTSService(stub_backend)
    svc._config = {"api_key": "sk-test-key", "model_id": "v2"}
    await svc.start(resolver)

    assert stub_backend.initialized
    assert stub_backend.init_config["api_key"] == "sk-test-key"
    assert stub_backend.init_config["model_id"] == "v2"


async def test_stop_closes_backend(
    service: TTSService, stub_backend: StubTTSBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.stop()
    assert stub_backend.closed


# --- Synthesis ---


async def test_synthesize(service: TTSService, resolver: ServiceResolver) -> None:
    await service.start(resolver)

    request = SynthesisRequest(text="Hello world", voice_id="v1")
    result = await service.synthesize(request)

    assert result.audio == b"fake-audio-data"
    assert result.format == AudioFormat.MP3
    assert result.characters_used == 11


async def test_synthesize_explicit_voice_id_used(
    service: TTSService, stub_backend: StubTTSBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)

    request = SynthesisRequest(text="Hello", voice_id="explicit-id")
    await service.synthesize(request)

    assert stub_backend.last_request is not None
    assert stub_backend.last_request.voice_id == "explicit-id"


async def test_synthesize_with_options(service: TTSService, resolver: ServiceResolver) -> None:
    await service.start(resolver)

    request = SynthesisRequest(
        text="Test",
        voice_id="v1",
        output_format=AudioFormat.WAV,
        stability=0.5,
        similarity_boost=0.8,
    )
    result = await service.synthesize(request)
    assert result.format == AudioFormat.WAV


# --- Voice listing ---


async def test_list_voices(service: TTSService, resolver: ServiceResolver) -> None:
    await service.start(resolver)

    voices = await service.list_voices()
    assert len(voices) == 2
    assert voices[0].voice_id == "v1"
    assert voices[1].name == "Bob"



# --- Config parsing ---


def test_config_parses_tts_full() -> None:
    raw = {
        "tts": {
            "enabled": True,
            "backend": "elevenlabs",
            "settings": {"api_key": "sk-xxx", "voice_id": "abc123", "model_id": "eleven_turbo_v2_5"},
        }
    }
    config = GilbertConfig.model_validate(raw)
    assert config.tts.enabled is True
    assert config.tts.backend == "elevenlabs"
    assert config.tts.settings["api_key"] == "sk-xxx"
    assert config.tts.settings["voice_id"] == "abc123"


def test_config_tts_defaults() -> None:
    config = GilbertConfig.model_validate({})
    assert config.tts.enabled is False
    assert config.tts.backend == "elevenlabs"
    assert config.tts.settings == {}


# --- Tool provider ---


def test_tool_provider_name(service: TTSService) -> None:
    assert service.tool_provider_name == "tts"


def test_get_tools(service: TTSService) -> None:
    tools = service.get_tools()
    names = [t.name for t in tools]
    assert "synthesize" in names
    assert "list_voices" in names


async def test_tool_synthesize(
    service: TTSService, resolver: ServiceResolver, tmp_path: Path, monkeypatch: object
) -> None:
    import gilbert.core.output as output_mod

    monkeypatch.setattr(output_mod, "OUTPUT_DIR", tmp_path / "output")  # type: ignore[attr-defined]
    await service.start(resolver)

    result = await service.execute_tool("synthesize", {"text": "Hello world"})
    parsed = json.loads(result)

    assert parsed["format"] == "mp3"
    assert parsed["characters_used"] == 11
    assert parsed["file_path"].endswith(".mp3")
    assert Path(parsed["file_path"]).exists()


async def test_tool_list_voices(service: TTSService, resolver: ServiceResolver) -> None:
    await service.start(resolver)

    result = await service.execute_tool("list_voices", {})
    parsed = json.loads(result)

    assert len(parsed) == 2
    assert parsed[0]["voice_id"] == "v1"
    assert parsed[0]["name"] == "Alice"
    assert parsed[1]["name"] == "Bob"


async def test_tool_unknown_raises(service: TTSService) -> None:
    with pytest.raises(KeyError, match="Unknown tool"):
        await service.execute_tool("nonexistent", {})
