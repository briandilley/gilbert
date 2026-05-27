"""Audio-blob store — short-lived in-memory cache of audio bytes
exposed via an HTTPS URL.

Some integrations (Mentra smart-glasses being the first) push audio
through an external cloud whose audio router does a server-side
HTTPS fetch of whatever URL the app supplies. That means the
Conversation engine's locally-synthesized TTS bytes need a temporary
public URL the third-party cloud can resolve.

Rather than reinvent the same blob-cache + route pattern per
plugin, the engine's wrapper registers the synthesized clip via
this capability and gets back a short-lived ``blob_id``. A single
core route (``GET /api/audio-blob/{blob_id}``) serves the bytes
back, scoped by the configured TTL.

Properties:

- **In-memory only.** Blobs do not survive a restart and are not
  persisted. A bounce wipes the cache; the engine will re-synthesize
  on the next turn.
- **One-shot oriented.** The cache is sized to hold a handful of
  concurrent clips for the duration of a conversation turn (TTL ~60
  seconds). The TTS audio for an 8-second utterance lives for one
  fetch cycle; consumers don't try to cache for replay.
- **No auth.** The route is exempted from session-cookie auth
  precisely because external cloud fetchers can't carry our
  cookies. ``blob_id`` is a 128-bit random UUID — guessing one
  before TTL expiry is the only practical defense.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["AudioBlob", "AudioBlobStore"]


class AudioBlob:
    """An audio clip registered with the store, addressable by id.

    Carries the raw bytes plus a MIME type so the serving route can
    set ``Content-Type`` correctly (Mentra Cloud cares — MP3 vs WAV
    routes through different decode paths). The store hands these
    out from ``fetch`` and consumers should not mutate them.
    """

    __slots__ = ("blob_id", "mime", "data")

    def __init__(self, blob_id: str, mime: str, data: bytes) -> None:
        self.blob_id = blob_id
        self.mime = mime
        self.data = data


@runtime_checkable
class AudioBlobStore(Protocol):
    """Capability protocol for the in-memory audio-blob cache.

    Implemented by the core ``AudioBlobStoreService``; consumed by:

    - Plugin sinks that need to expose engine-synthesized audio via
      a fetchable URL (Mentra plugin's ``_MentraAudioSink``).
    - The ``GET /api/audio-blob/{blob_id}`` route in core/web.

    Resolution is via ``resolver.get_capability("audio_blob_store")``
    plus an ``isinstance`` check.
    """

    def register(
        self,
        data: bytes,
        mime: str,
        *,
        ttl_seconds: float = 60.0,
    ) -> str:
        """Register an audio clip; return a short-lived ``blob_id``.

        The id is opaque — currently a UUIDv4 hex. Callers must use
        the returned string verbatim when building the URL.

        The clip auto-expires after ``ttl_seconds``. Default 60s is
        tuned for the conversation-engine path: synthesize → register
        → hand URL to cloud → cloud fetches almost immediately → play
        through speaker. A 60-second TTL gives the cloud ample time
        on a slow link while keeping memory bounded.
        """
        ...

    def fetch(self, blob_id: str) -> AudioBlob | None:
        """Return the blob for ``blob_id`` if it exists and hasn't
        expired; otherwise ``None``. Idempotent — fetching does NOT
        consume the blob (Mentra Cloud sometimes retries on a slow
        connection)."""
        ...
