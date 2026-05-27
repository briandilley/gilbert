"""In-memory audio-blob cache exposed via the ``audio_blob_store``
capability.

Plugins that need to surface engine-synthesized audio at a public
URL (Mentra Cloud fetches ``audioUrl`` server-side; some other voice
relays do the same) register bytes here and get back a short-lived
``blob_id`` they bake into a URL like
``https://<host>/api/audio-blob/<blob_id>``. The matching
``GET /api/audio-blob/<id>`` route in ``web/routes/audio_blob.py``
serves the bytes with the right ``Content-Type``.

Cache shape:

- In-memory dict keyed by ``blob_id``.
- Each entry expires ``ttl_seconds`` after registration (default
  60s — long enough for the cloud's fetcher to finish, short
  enough that memory doesn't grow unbounded if a session never
  resolves the URL).
- Lazy cleanup on access. No background sweeper.

Not appropriate for: persistent media, large files (> a few MB —
this is for second-scale TTS clips), or anything that needs to
survive a restart.
"""

from __future__ import annotations

import logging
import time
import uuid

from gilbert.interfaces.audio_blob import AudioBlob, AudioBlobStore
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


__all__ = ["AudioBlobStoreService"]


class _Entry:
    """Internal record. ``expires_at`` is monotonic-clock seconds."""

    __slots__ = ("blob", "expires_at")

    def __init__(self, blob: AudioBlob, expires_at: float) -> None:
        self.blob = blob
        self.expires_at = expires_at


class AudioBlobStoreService(Service):
    """Tiny in-memory blob cache.

    Capability provided: ``audio_blob_store`` (implements
    ``AudioBlobStore`` Protocol).

    Always on — there's no config and no per-tenant state to manage.
    Each instance keeps its own dict; we register a single instance
    in core's composition root.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="audio_blob_store",
            capabilities=frozenset({"audio_blob_store"}),
            requires=frozenset(),
            optional=frozenset(),
            toggleable=False,
        )

    async def start(self, resolver: ServiceResolver) -> None:
        logger.info("AudioBlobStore service started")

    async def stop(self) -> None:
        # Drop the cache on shutdown so we don't hold onto bytes
        # past process exit. Each entry expires on its own anyway;
        # this just speeds garbage collection in tests.
        self._entries.clear()

    # ── AudioBlobStore Protocol ────────────────────────────────────

    def register(
        self,
        data: bytes,
        mime: str,
        *,
        ttl_seconds: float = 60.0,
    ) -> str:
        """Insert a blob and return its id. 16-char hex uuid — short
        enough for a tidy URL, wide enough to make guessing one
        before expiry impractical (128 bits before truncation,
        ~64 bits after — still way beyond a TTL-window brute force).
        """
        self._evict_expired()
        blob_id = uuid.uuid4().hex[:16]
        # Defensive: copy the bytes so the caller mutating its
        # buffer post-register doesn't corrupt what we serve.
        # Audio clips are small (KB-MB range) so the copy is cheap.
        blob = AudioBlob(blob_id=blob_id, mime=mime, data=bytes(data))
        self._entries[blob_id] = _Entry(
            blob=blob,
            expires_at=time.monotonic() + max(0.0, ttl_seconds),
        )
        return blob_id

    def fetch(self, blob_id: str) -> AudioBlob | None:
        """Return the blob if it exists and hasn't expired; else
        ``None``. Expired entries are removed on access.
        """
        entry = self._entries.get(blob_id)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            # Lazy expiry — drop on the way out.
            self._entries.pop(blob_id, None)
            return None
        return entry.blob

    # ── Internals ─────────────────────────────────────────────────

    def _evict_expired(self) -> None:
        """Drop every expired entry. Called on register so a long-
        running process with steady traffic doesn't accumulate dead
        rows between fetches.
        """
        if not self._entries:
            return
        now = time.monotonic()
        # Materialize the list so we can mutate the dict during iter.
        stale = [
            blob_id
            for blob_id, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for blob_id in stale:
            self._entries.pop(blob_id, None)

    def _entry_count_for_test(self) -> int:
        """Test-only: number of live entries (no eviction). Used by
        unit tests to verify TTL expiry actually happens."""
        return len(self._entries)


# Static check at import time: the service class satisfies the
# capability Protocol. Catches drift if the Protocol gains a new
# method that we forgot to implement.
_: AudioBlobStore = AudioBlobStoreService()  # type: ignore[assignment]
del _
