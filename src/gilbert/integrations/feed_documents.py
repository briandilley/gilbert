"""Synthetic ``feed_articles`` document backend — read-only over ``.gilbert/feed-cache/``.

Owned PRIVATELY by ``FeedsService`` for vector ingestion of feed
articles. **Not registered with ``KnowledgeService._backends``** —
``FeedsService`` calls ``KnowledgeProvider.index_document`` directly
with the synthetic instance it owns. The class declares
``backend_name = "feed_articles"`` so the registry knows about it
(future plugins can reference the source_id), but registration with
``KnowledgeService`` would trigger sync-storms — feed articles are
push-on-receive, not pull-on-sync.

Defensive: ``list_documents()`` returns ``[]`` so even if a future
contributor accidentally registers it, the periodic ``knowledge-sync``
loop is a no-op. See spec §9 for the ingestion flow and the
"never registered" rationale.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from gilbert.interfaces.knowledge import (
    DocumentBackend,
    DocumentContent,
    DocumentMeta,
    DocumentType,
)

logger = logging.getLogger(__name__)

_STREAM_CHUNK_SIZE = 65536  # 64 KiB

_SOURCE_ID = "feed_articles"


class FeedDocumentBackend(DocumentBackend):
    """Read-only ``DocumentBackend`` over the per-feed article cache.

    Files live under ``<base_dir>/<feed_id>/<safe_uid>.html``.
    ``FeedsService`` owns the single instance, writes the bytes
    itself (so this backend is read-only from the perspective of
    ``KnowledgeService.index_document``), and constructs ``path``
    as ``<feed_id>/<safe_uid>.html``.
    """

    backend_name = _SOURCE_ID

    def __init__(self, name: str = _SOURCE_ID) -> None:
        super().__init__(name)
        self._base_dir = Path(".gilbert/feed-cache")

    @property
    def source_id(self) -> str:
        return _SOURCE_ID

    @property
    def display_name(self) -> str:
        return "Feed articles"

    @property
    def read_only(self) -> bool:
        # Writes happen through ``FeedsService.cache_article`` not
        # through ``upload_document``; from the indexing surface this
        # is read-only.
        return True

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    async def initialize(self, config: dict[str, object]) -> None:
        base = str(config.get("base_dir") or self._base_dir)
        self._base_dir = Path(base)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass

    def _resolve(self, path: str) -> Path:
        full = (self._base_dir / path).resolve()
        if not full.is_relative_to(self._base_dir.resolve()):
            raise PermissionError(f"Path escapes feed cache: {path!r}")
        return full

    async def list_documents(self, prefix: str = "") -> list[DocumentMeta]:
        """Defensively returns ``[]`` — the synthetic backend is NOT
        meant to be walked by ``KnowledgeService._sync_backend``.

        Even if a future contributor accidentally registers this
        backend, the sync loop produces zero work."""
        return []

    async def get_document(self, path: str) -> DocumentContent | None:
        full = self._resolve(path)
        if not full.exists() or not full.is_file():
            return None
        meta = await self.get_metadata(path)
        if meta is None:
            return None
        return DocumentContent(meta=meta, data=full.read_bytes())

    async def get_metadata(self, path: str) -> DocumentMeta | None:
        full = self._resolve(path)
        if not full.exists() or not full.is_file():
            return None
        stat = full.stat()
        mime, _ = mimetypes.guess_type(str(full))
        return DocumentMeta(
            source_id=self.source_id,
            path=path,
            name=full.name,
            document_type=DocumentType.MARKDOWN
            if full.suffix.lower() in {".md", ".markdown"}
            else DocumentType.TEXT,
            size_bytes=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            mime_type=mime or "text/html",
            checksum=f"{stat.st_size}:{int(stat.st_mtime)}",
        )

    async def upload_document(
        self,
        path: str,
        data: bytes,
        mime_type: str = "",
    ) -> DocumentMeta:
        # Writes flow through ``FeedsService.cache_article`` directly.
        raise PermissionError(
            "feed_articles is read-only; use FeedsService.cache_article"
        )

    async def delete_document(self, path: str) -> None:
        full = self._resolve(path)
        if not full.exists():
            raise KeyError(f"Cached article not found: {path!r}")
        full.unlink()

    async def stream_document(self, path: str) -> AsyncIterator[bytes]:
        full = self._resolve(path)
        if not full.exists():
            return
        with full.open("rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

