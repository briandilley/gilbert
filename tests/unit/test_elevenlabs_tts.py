"""Tests for ElevenLabs TTS backend."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.core.services.tts import _generate_mp3_silence, _generate_pcm_silence
from gilbert.integrations.elevenlabs_tts import ElevenLabsTTS
from gilbert.interfaces.tts import AudioFormat, SynthesisRequest


@pytest.fixture
def backend() -> ElevenLabsTTS:
    return ElevenLabsTTS()


# --- Initialization ---


async def test_initialize_sets_api_key(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._api_key == "sk-test"
    assert backend._client is not None
    await backend.close()


async def test_initialize_default_model(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._model_id == "eleven_turbo_v2_5"
    await backend.close()


async def test_initialize_custom_model(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test", "model_id": "eleven_multilingual_v2"})
    assert backend._model_id == "eleven_multilingual_v2"
    await backend.close()


async def test_initialize_requires_api_key(backend: ElevenLabsTTS) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_initialize_rejects_empty_api_key(backend: ElevenLabsTTS) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({"api_key": ""})


# --- Close ---


async def test_close_clears_client(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})
    await backend.close()
    assert backend._client is None


async def test_close_idempotent(backend: ElevenLabsTTS) -> None:
    await backend.close()  # no-op when not initialized


# --- Client guard ---


def test_require_client_raises_before_init(backend: ElevenLabsTTS) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        backend._require_client()


# --- Silence generation ---


def test_generate_pcm_silence() -> None:
    silence = _generate_pcm_silence(1.0)
    # 44100 samples * 2 bytes per sample = 88200 bytes
    assert len(silence) == 88200
    assert silence == b"\x00" * 88200


def test_generate_pcm_silence_zero() -> None:
    silence = _generate_pcm_silence(0)
    assert silence == b""


def test_generate_mp3_silence_produces_bytes() -> None:
    silence = _generate_mp3_silence(1.0)
    assert len(silence) > 0
    # Should start with MP3 sync word
    assert silence[:2] == b"\xff\xfb"


def test_generate_mp3_silence_zero() -> None:
    # Even 0 seconds produces 1 frame due to rounding up
    silence = _generate_mp3_silence(0)
    assert len(silence) > 0


# --- Synthesize ---


async def test_synthesize_calls_api(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test", "silence_padding": 0})

    mock_response = AsyncMock()
    mock_response.content = b"audio-bytes"
    mock_response.raise_for_status = lambda: None

    with patch.object(backend._client, "post", return_value=mock_response) as mock_post:  # type: ignore[union-attr]
        request = SynthesisRequest(text="Hello", voice_id="voice123")
        result = await backend.synthesize(request)

        mock_post.assert_called_once()
        call_args = mock_post.call_args

        assert "/text-to-speech/voice123" in call_args.args[0]
        assert call_args.kwargs["json"]["text"] == "Hello"
        assert call_args.kwargs["json"]["model_id"] == "eleven_turbo_v2_5"
        assert call_args.kwargs["params"]["output_format"] == "mp3_44100_128"

    assert result.audio == b"audio-bytes"
    assert result.format == AudioFormat.MP3
    assert result.characters_used == 5
    await backend.close()


async def test_synthesize_passes_voice_settings(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = AsyncMock()
    mock_response.content = b"audio"
    mock_response.raise_for_status = lambda: None

    with patch.object(backend._client, "post", return_value=mock_response) as mock_post:  # type: ignore[union-attr]
        request = SynthesisRequest(
            text="Hi",
            voice_id="v1",
            stability=0.7,
            similarity_boost=0.9,
        )
        await backend.synthesize(request)

        body = mock_post.call_args.kwargs["json"]
        assert body["voice_settings"]["stability"] == 0.7
        assert body["voice_settings"]["similarity_boost"] == 0.9

    await backend.close()


# --- List voices ---


async def test_list_voices_parses_response(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "voices": [
            {
                "voice_id": "abc",
                "name": "Rachel",
                "description": "Calm voice",
                "labels": {"accent": "american"},
                "fine_tuning": {"language": "en"},
            },
            {
                "voice_id": "def",
                "name": "Domi",
                "labels": {},
            },
        ]
    }

    with patch.object(backend._client, "get", return_value=mock_response):  # type: ignore[union-attr]
        voices = await backend.list_voices()

    assert len(voices) == 2
    assert voices[0].voice_id == "abc"
    assert voices[0].name == "Rachel"
    assert voices[0].language == "en"
    assert voices[0].labels == {"accent": "american"}
    assert voices[1].voice_id == "def"
    assert voices[1].language is None
    await backend.close()


# --- Get voice ---


async def test_get_voice_found(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "voice_id": "abc",
        "name": "Rachel",
        "labels": {},
    }

    with patch.object(backend._client, "get", return_value=mock_response):  # type: ignore[union-attr]
        voice = await backend.get_voice("abc")

    assert voice is not None
    assert voice.voice_id == "abc"
    await backend.close()


async def test_get_voice_not_found(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch.object(backend._client, "get", return_value=mock_response):  # type: ignore[union-attr]
        voice = await backend.get_voice("nonexistent")

    assert voice is None
    await backend.close()


# --- Synthesis cache ---


def _make_mock_response(content: bytes = b"audio-bytes") -> AsyncMock:
    r = AsyncMock()
    r.content = content
    r.raise_for_status = lambda: None
    return r


async def test_cache_hit_skips_api_call(backend: ElevenLabsTTS) -> None:
    """A second identical request is served from the cache."""
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(b"cached-audio"),
    ) as mock_post:
        request = SynthesisRequest(text="Hello", voice_id="v1")
        first = await backend.synthesize(request)
        second = await backend.synthesize(request)

        assert mock_post.call_count == 1
        assert first.audio == b"cached-audio"
        assert second.audio == b"cached-audio"

    stats = backend.cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["size"] == 1
    await backend.close()


async def test_cache_keys_differ_by_text(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="One", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="Two", voice_id="v1"))
        assert mock_post.call_count == 2

    assert backend.cache_stats()["size"] == 2
    await backend.close()


async def test_cache_keys_differ_by_voice(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v2"))
        assert mock_post.call_count == 2

    await backend.close()


async def test_cache_keys_differ_by_format(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(
            SynthesisRequest(
                text="Hi", voice_id="v1", output_format=AudioFormat.MP3,
            )
        )
        await backend.synthesize(
            SynthesisRequest(
                text="Hi", voice_id="v1", output_format=AudioFormat.WAV,
            )
        )
        assert mock_post.call_count == 2

    await backend.close()


async def test_cache_keys_differ_by_voice_settings(
    backend: ElevenLabsTTS,
) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(
            SynthesisRequest(text="Hi", voice_id="v1", stability=0.5)
        )
        await backend.synthesize(
            SynthesisRequest(text="Hi", voice_id="v1", stability=0.9)
        )
        assert mock_post.call_count == 2

    await backend.close()


async def test_cache_lru_eviction(backend: ElevenLabsTTS) -> None:
    """Inserting past cache_max_entries evicts the least-recently-used."""
    await backend.initialize({"api_key": "sk-test", "cache_max_entries": 2})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(SynthesisRequest(text="one", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="two", voice_id="v1"))
        # Touch "one" so it's most-recently-used
        await backend.synthesize(SynthesisRequest(text="one", voice_id="v1"))
        # Inserting a third entry evicts the LRU ("two")
        await backend.synthesize(SynthesisRequest(text="three", voice_id="v1"))

    stats = backend.cache_stats()
    assert stats["size"] == 2
    assert stats["evictions"] >= 1
    # "two" should be gone — synthesizing it again triggers a fresh miss
    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="two", voice_id="v1"))
        assert mock_post.call_count == 1

    await backend.close()


async def test_cache_disabled_by_zero_max_entries(
    backend: ElevenLabsTTS,
) -> None:
    await backend.initialize({"api_key": "sk-test", "cache_max_entries": 0})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))
        assert mock_post.call_count == 2

    assert backend.cache_stats()["size"] == 0
    await backend.close()


async def test_cache_ttl_expires_old_entries(backend: ElevenLabsTTS) -> None:
    """Entries older than ttl_seconds are evicted on access."""
    import time as time_mod

    await backend.initialize(
        {"api_key": "sk-test", "cache_ttl_seconds": 0.05}
    )

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        request = SynthesisRequest(text="Hi", voice_id="v1")
        await backend.synthesize(request)
        # Wait past the TTL
        time_mod.sleep(0.1)
        await backend.synthesize(request)
        # Second call was NOT a cache hit — the entry expired
        assert mock_post.call_count == 2

    stats = backend.cache_stats()
    assert stats["evictions"] >= 1
    assert stats["misses"] == 2
    await backend.close()


async def test_cache_ttl_zero_disables_expiry(backend: ElevenLabsTTS) -> None:
    """ttl=0 means entries live until LRU evicts them."""
    await backend.initialize(
        {"api_key": "sk-test", "cache_ttl_seconds": 0}
    )

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        request = SynthesisRequest(text="Hi", voice_id="v1")
        await backend.synthesize(request)
        await backend.synthesize(request)
        assert mock_post.call_count == 1

    assert backend.cache_stats()["hits"] == 1
    await backend.close()


async def test_close_clears_cache(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))

    assert backend.cache_stats()["size"] == 1
    await backend.close()
    assert backend.cache_stats()["size"] == 0


async def test_config_cache_defaults() -> None:
    """Missing cache config keys fall back to the documented defaults."""
    backend = ElevenLabsTTS()
    await backend.initialize({"api_key": "sk-test"})
    stats = backend.cache_stats()
    assert stats["max_entries"] == 256
    assert stats["ttl_seconds"] == 1800.0
    await backend.close()
