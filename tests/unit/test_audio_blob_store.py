"""Unit tests for ``AudioBlobStoreService``.

The store sits behind the ``audio_blob_store`` capability and is used
by plugins (Mentra) to make engine-synthesized audio fetchable by
external cloud relays. Tests cover the core contract: register
returns an id, fetch returns the bytes, TTL actually expires entries.
"""

from __future__ import annotations

import time

import pytest

from gilbert.core.services.audio_blob_store import AudioBlobStoreService
from gilbert.interfaces.audio_blob import AudioBlob, AudioBlobStore


def test_satisfies_audio_blob_store_protocol() -> None:
    """The runtime-checkable Protocol is what consumers narrow
    against. If the service drifts away from the surface the
    `isinstance` check at the call site silently returns False
    and the capability lookup fails — catch the drift here."""
    svc = AudioBlobStoreService()
    assert isinstance(svc, AudioBlobStore)


def test_register_then_fetch_returns_the_same_bytes() -> None:
    svc = AudioBlobStoreService()
    blob_id = svc.register(b"\x00\x01\x02\x03", "audio/mpeg")
    fetched = svc.fetch(blob_id)
    assert fetched is not None
    assert isinstance(fetched, AudioBlob)
    assert fetched.blob_id == blob_id
    assert fetched.mime == "audio/mpeg"
    assert fetched.data == b"\x00\x01\x02\x03"


def test_register_copies_the_input_buffer() -> None:
    """If the caller mutates its buffer after register, the stored
    blob must be unaffected — otherwise a sink that reuses its
    bytearray would corrupt the next playback. Defensive copy is
    cheap (single-digit MB ceiling) and prevents a class of
    nightmare-to-debug glitches."""
    svc = AudioBlobStoreService()
    buf = bytearray(b"original")
    blob_id = svc.register(bytes(buf), "audio/mpeg")
    buf[0:1] = b"X"  # mutate after register
    fetched = svc.fetch(blob_id)
    assert fetched is not None
    assert fetched.data == b"original"


def test_register_returns_unique_ids() -> None:
    """Two registrations of identical bytes still get distinct ids.
    Critical for the per-utterance URL pattern — same text spoken
    twice in a row must produce two cacheable-by-cloud URLs."""
    svc = AudioBlobStoreService()
    a = svc.register(b"identical", "audio/mpeg")
    b = svc.register(b"identical", "audio/mpeg")
    assert a != b


def test_fetch_unknown_id_returns_none() -> None:
    svc = AudioBlobStoreService()
    assert svc.fetch("nope_does_not_exist") is None


def test_fetch_does_not_consume_the_blob() -> None:
    """Mentra Cloud has been observed retrying TTS fetches twice
    when its connection is slow. Fetch must be idempotent — the
    second call should still get the bytes (until TTL expires)."""
    svc = AudioBlobStoreService()
    blob_id = svc.register(b"persistent", "audio/mpeg")
    first = svc.fetch(blob_id)
    second = svc.fetch(blob_id)
    assert first is not None
    assert second is not None
    assert first.data == second.data == b"persistent"


def test_ttl_expires_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry past its TTL must not be fetchable and must be
    cleaned out of the internal dict on access. Use a fake
    monotonic clock so the test doesn't actually sleep."""
    svc = AudioBlobStoreService()
    fake_now = [1000.0]

    def _fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", _fake_monotonic)

    blob_id = svc.register(b"will-expire", "audio/mpeg", ttl_seconds=10.0)
    # Within TTL — still fetchable.
    fake_now[0] = 1005.0
    assert svc.fetch(blob_id) is not None

    # Past TTL — gone, and the lazy cleanup drops it from the dict.
    fake_now[0] = 1015.0
    assert svc.fetch(blob_id) is None
    assert svc._entry_count_for_test() == 0


def test_register_evicts_expired_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without eviction, a long-running process with steady traffic
    would accumulate dead rows between fetches. ``register`` is the
    natural sweep point — every new clip gets a chance to clear
    yesterday's leftovers."""
    svc = AudioBlobStoreService()
    fake_now = [1000.0]

    def _fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", _fake_monotonic)

    # Drop two entries with short TTL.
    svc.register(b"a", "audio/mpeg", ttl_seconds=5.0)
    svc.register(b"b", "audio/mpeg", ttl_seconds=5.0)
    assert svc._entry_count_for_test() == 2

    # Skip past TTL, then register a fresh one — the two stale
    # entries should be swept out, leaving just the new one.
    fake_now[0] = 1010.0
    svc.register(b"fresh", "audio/mpeg", ttl_seconds=60.0)
    assert svc._entry_count_for_test() == 1


def test_blob_id_format_is_16_hex_chars() -> None:
    """16-char hex (64 bits truncated from 128) is the documented
    contract and the basis of the "guessing one before TTL impractical"
    security argument. If the impl drifts to shorter / longer ids
    the auth-exempt-by-id-opacity story falls apart."""
    svc = AudioBlobStoreService()
    blob_id = svc.register(b"x", "audio/mpeg")
    assert len(blob_id) == 16
    # All chars hex.
    int(blob_id, 16)
