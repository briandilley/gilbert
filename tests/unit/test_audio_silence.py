"""Tests for the audio-silence helpers in ``gilbert.interfaces.tts``."""

from gilbert.interfaces.tts import (
    AudioFormat,
    append_silence,
    generate_mp3_silence,
    generate_pcm_silence,
)


# --- PCM silence ---


def test_generate_pcm_silence() -> None:
    silence = generate_pcm_silence(1.0)
    # 44100 samples * 2 bytes per sample = 88200 bytes
    assert len(silence) == 88200
    assert silence == b"\x00" * 88200


def test_generate_pcm_silence_zero() -> None:
    silence = generate_pcm_silence(0)
    assert silence == b""


# --- MP3 silence ---


def test_generate_mp3_silence_produces_bytes() -> None:
    silence = generate_mp3_silence(1.0)
    assert len(silence) > 0
    # Should start with MP3 sync word
    assert silence[:2] == b"\xff\xfb"


def test_generate_mp3_silence_zero() -> None:
    # Even 0 seconds produces 1 frame due to rounding up
    silence = generate_mp3_silence(0)
    assert len(silence) > 0


# --- append_silence dispatch ---


def test_append_silence_mp3() -> None:
    audio = b"\xff\xfb\x00\x00" * 10
    padded = append_silence(audio, AudioFormat.MP3, 0.5)
    assert len(padded) > len(audio)
    assert padded.startswith(audio)


def test_append_silence_pcm() -> None:
    audio = b"\x01\x02" * 100
    padded = append_silence(audio, AudioFormat.PCM, 0.5)
    assert len(padded) > len(audio)
    assert padded.startswith(audio)


def test_append_silence_wav_uses_pcm_frames() -> None:
    audio = b"\x03\x04" * 100
    padded = append_silence(audio, AudioFormat.WAV, 0.5)
    assert len(padded) > len(audio)


def test_append_silence_zero_is_noop() -> None:
    audio = b"\x01\x02\x03"
    assert append_silence(audio, AudioFormat.MP3, 0) == audio
    assert append_silence(audio, AudioFormat.PCM, 0) == audio


def test_append_silence_unknown_format_is_noop() -> None:
    audio = b"\xde\xad\xbe\xef"
    assert append_silence(audio, AudioFormat.OGG, 0.5) == audio
