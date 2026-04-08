"""Knowledge service — document indexing, vector search, and multi-backend aggregation."""

import json
import logging
from pathlib import Path
from typing import Any

from gilbert.core.documents.chunking import chunk_text
from gilbert.core.documents.extractors import extract_text
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.knowledge import (
    DocumentBackend,
    DocumentMeta,
    DocumentType,
    SearchResponse,
    SearchResult,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


class KnowledgeService(Service):
    """Aggregates multiple DocumentBackend instances and provides
    ChromaDB-based vector search across all of them.

    Capabilities: knowledge, ai_tools
    """

    def __init__(self, has_gdrive: bool = False) -> None:
        self._backends: dict[str, DocumentBackend] = {}
        self._chroma_client: Any = None
        self._collection: Any = None
        self._chunk_size: int = 800
        self._chunk_overlap: int = 200
        self._max_results: int = 20
        self._sync_interval: int = 300
        self._event_bus: EventBus | None = None
        self._storage: Any = None
        self._vision: Any = None  # VisionService
        self._ocr: Any = None  # OCRService
        self._has_gdrive = has_gdrive

    def service_info(self) -> ServiceInfo:
        # If gdrive sources are configured, require google_api so we start after it
        required: set[str] = {"entity_storage"}
        if self._has_gdrive:
            required.add("google_api")
        optional = frozenset({"scheduler", "google_api", "configuration", "credentials", "event_bus", "vision", "ocr"}) - required
        return ServiceInfo(
            name="knowledge",
            capabilities=frozenset({"knowledge", "ai_tools", "ws_handlers"}),
            requires=required,
            optional=optional,
            events=frozenset({"knowledge.document.indexed", "knowledge.document.removed", "knowledge.document.discovered"}),
        )

    @property
    def backends(self) -> dict[str, DocumentBackend]:
        return dict(self._backends)

    def get_backend(self, source_id: str) -> DocumentBackend | None:
        return self._backends.get(source_id)

    async def start(self, resolver: ServiceResolver) -> None:
        # Load config
        config_svc = resolver.get_capability("configuration")
        sources: list[dict[str, Any]] = []
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("knowledge")
                self._chunk_size = int(section.get("chunk_size", 1000))
                self._chunk_overlap = int(section.get("chunk_overlap", 200))
                self._max_results = int(section.get("max_search_results", 10))
                self._sync_interval = int(section.get("sync_interval_seconds", 300))
                sources = section.get("sources", [])
                chromadb_path = section.get("chromadb_path", ".gilbert/chromadb")

        # Initialize ChromaDB
        try:
            import chromadb

            persist_dir = str(Path(chromadb_path if 'chromadb_path' in dir() else ".gilbert/chromadb"))
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(path=persist_dir)
            self._collection = self._chroma_client.get_or_create_collection(
                name="documents",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("ChromaDB initialized at %s", persist_dir)
        except ImportError:
            logger.error("chromadb not installed — knowledge search disabled")
        except Exception:
            logger.exception("Failed to initialize ChromaDB")

        # Resolve vision and OCR services
        self._vision = resolver.get_capability("vision")
        self._ocr = resolver.get_capability("ocr")

        # Event bus
        # Entity storage for document tracking metadata (required for change detection)
        storage_svc = resolver.require_capability("entity_storage")
        self._storage = getattr(storage_svc, "backend", storage_svc)

        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            from gilbert.core.services.event_bus import EventBusService

            if isinstance(event_bus_svc, EventBusService):
                self._event_bus = event_bus_svc.bus

        google_svc = resolver.get_capability("google_api")

        for src in sources:
            if not isinstance(src, dict):
                continue
            if not src.get("enabled", True):
                continue
            src_type = src.get("type", "")
            src_name = src.get("name", "")

            try:
                if src_type == "local":
                    from gilbert.integrations.local_documents import LocalDocumentBackend

                    backend = LocalDocumentBackend(name=src_name, base_path=src.get("path", ""))
                    await backend.initialize({})
                    self._backends[backend.source_id] = backend
                    logger.info("Registered document source: %s", backend.source_id)

                elif src_type == "gdrive":
                    if google_svc is None:
                        logger.warning(
                            "Cannot initialize gdrive source '%s': Google service not available. "
                            "Ensure google.enabled is true in config.",
                            src_name,
                        )
                        continue

                    from gilbert.integrations.gdrive_documents import GoogleDriveDocumentBackend

                    backend = GoogleDriveDocumentBackend(
                        name=src_name,
                        account=src.get("account", ""),
                        folder_id=src.get("folder_id", ""),
                        shared_drive_id=src.get("shared_drive_id", ""),
                    )
                    await backend.initialize({"_google_service": google_svc})
                    self._backends[backend.source_id] = backend
                    logger.info("Registered document source: %s", backend.source_id)

                else:
                    logger.warning("Unknown document source type: %s", src_type)
            except Exception:
                logger.exception("Failed to initialize document source: %s:%s", src_type, src_name)

        # Register sync job with scheduler
        scheduler = resolver.get_capability("scheduler")
        if scheduler is not None and self._backends:
            from gilbert.core.services.scheduler import SchedulerService
            from gilbert.interfaces.scheduler import Schedule

            if isinstance(scheduler, SchedulerService):
                scheduler.add_job(
                    name="knowledge-sync",
                    schedule=Schedule.every(self._sync_interval),
                    callback=self._sync_all,
                    system=True,
                )

        # Schedule initial sync as a one-shot background job so it doesn't block startup.
        # ChromaDB may need to download the embedding model on first use.
        if scheduler is not None and self._backends and self._collection is not None:
            from gilbert.core.services.scheduler import SchedulerService
            from gilbert.interfaces.scheduler import Schedule

            if isinstance(scheduler, SchedulerService):
                scheduler.add_job(
                    name="knowledge-initial-sync",
                    schedule=Schedule.once_after(2),
                    callback=self._sync_all,
                    system=True,
                )

        logger.info(
            "Knowledge service started — %d sources, ChromaDB %s",
            len(self._backends),
            "ready" if self._collection else "unavailable",
        )

    async def stop(self) -> None:
        for backend in self._backends.values():
            await backend.close()
        self._backends.clear()

    # --- Indexing ---

    async def index_document(self, backend: DocumentBackend, meta: DocumentMeta) -> int:
        """Index a single document into ChromaDB. Returns number of chunks created."""
        if self._collection is None:
            return 0

        content = await backend.get_document(meta.path)
        if content is None:
            logger.warning("Failed to download document: %s (get_document returned None)", meta.document_id)
            return 0

        import asyncio

        text, stats = await asyncio.to_thread(
            extract_text,
            content,
            vision=self._vision,
            ocr=self._ocr,
        )
        if not text.strip():
            logger.warning(
                "No text extracted from %s (%s, %d bytes) — may be scanned/image-only",
                meta.document_id, meta.document_type.value, meta.size_bytes,
            )
            return 0

        # Log extraction stats
        if stats.ocr_pages or stats.vision_pages:
            logger.info(
                "Extraction stats for %s: %d pages, %d OCR pages (%d chars), "
                "%d Vision pages (%d chars), %d total chars",
                meta.name, stats.pages, stats.ocr_pages, stats.ocr_chars,
                stats.vision_pages, stats.vision_chars, stats.total_chars,
            )

        chunks = chunk_text(
            text, meta.document_id,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        )
        if not chunks:
            logger.warning("Chunking produced 0 chunks for %s (%d chars of text)", meta.document_id, len(text))
            return 0

        doc_id = meta.document_id

        # Remove old chunks for this document
        try:
            await asyncio.to_thread(
                self._collection.delete, where={"document_id": doc_id}
            )
        except Exception:
            pass  # May not exist yet

        # Upsert new chunks (runs embedding model — can be slow on CPU)
        chunk_ids = [f"{doc_id}#chunk{c.chunk_index}" for c in chunks]
        chunk_docs = [c.text for c in chunks]
        chunk_metas = [
            {
                "document_id": doc_id,
                "source_id": meta.source_id,
                "path": meta.path,
                "name": meta.name,
                "document_type": meta.document_type.value,
                "last_modified": meta.last_modified,
                "chunk_index": c.chunk_index,
                "page_number": c.page_number or -1,
            }
            for c in chunks
        ]
        await asyncio.to_thread(
            self._collection.upsert,
            ids=chunk_ids,
            documents=chunk_docs,
            metadatas=chunk_metas,
        )

        logger.info("Indexed %s: %d chunks", doc_id, len(chunks))

        # Cache extracted text for fast keyword search at query time
        await self._cache_text(doc_id, text)

        await self._track_document(meta, indexed_chunks=len(chunks))
        await self._emit("knowledge.document.indexed", {
            "document_id": doc_id,
            "source_id": meta.source_id,
            "name": meta.name,
            "path": meta.path,
            "type": meta.document_type.value,
            "chunks": len(chunks),
        })

        return len(chunks)

    async def _cache_text(self, document_id: str, text: str) -> None:
        """Cache extracted text in entity store for fast keyword search."""
        if self._storage is None:
            return
        try:
            await self._storage.put("knowledge_text", document_id, {
                "document_id": document_id,
                "text": text,
            })
        except Exception:
            logger.warning("Failed to cache extracted text for %s", document_id)

    async def get_cached_text(self, document_id: str) -> str | None:
        """Retrieve cached extracted text for a document."""
        if self._storage is None:
            return None
        try:
            record = await self._storage.get("knowledge_text", document_id)
            if record:
                return record.get("text")
        except Exception:
            pass
        return None

    async def _sync_backend(self, backend: DocumentBackend) -> int:
        """Sync a single backend. Returns number of documents indexed."""
        logger.info("Syncing document source: %s", backend.source_id)
        try:
            docs = await backend.list_documents()
        except Exception:
            logger.warning("Failed to list documents from %s", backend.source_id, exc_info=True)
            return 0

        logger.info("Found %d documents in %s", len(docs), backend.source_id)

        # Track current document IDs for removal detection
        current_doc_ids = {meta.document_id for meta in docs}

        # Detect removed documents (were indexed but no longer in backend)
        if self._collection is not None:
            try:
                stored = self._collection.get(
                    where={"source_id": backend.source_id},
                    include=["metadatas"],
                )
                stored_ids = {
                    m["document_id"]
                    for m in (stored.get("metadatas") or [])
                    if "document_id" in m
                }
                removed_ids = stored_ids - current_doc_ids
                for removed_id in removed_ids:
                    try:
                        self._collection.delete(where={"document_id": removed_id})
                    except Exception:
                        pass
                    await self._untrack_document(removed_id)
                    await self._emit("knowledge.document.removed", {
                        "document_id": removed_id,
                        "source_id": backend.source_id,
                    })
                    logger.info("Document removed from index: %s", removed_id)
            except Exception:
                pass

        # Filter to documents that need indexing
        to_index: list[DocumentMeta] = []
        for meta in docs:
            if meta.document_type == DocumentType.UNKNOWN:
                continue

            is_new = True
            try:
                tracked = await self._storage.get("knowledge_documents", meta.document_id)
                if tracked:
                    is_new = False
                    stored_modified = tracked.get("last_modified", "")
                    stored_checksum = tracked.get("checksum", "")
                    has_been_indexed = bool(tracked.get("indexed_at"))

                    if has_been_indexed:
                        if meta.checksum and stored_checksum:
                            if meta.checksum == stored_checksum:
                                continue
                        elif stored_modified == meta.last_modified:
                            continue
                        logger.info(
                            "Re-indexing changed document: %s (modified: %s -> %s)",
                            meta.name, stored_modified, meta.last_modified,
                        )
            except Exception:
                logger.warning("Failed to check tracking for %s", meta.document_id, exc_info=True)

            if is_new:
                await self._track_document(meta)
                await self._emit("knowledge.document.discovered", {
                    "document_id": meta.document_id,
                    "source_id": meta.source_id,
                    "name": meta.name,
                    "path": meta.path,
                    "type": meta.document_type.value,
                })

            to_index.append(meta)

        if not to_index:
            logger.info("Sync complete for %s: 0 documents indexed (all up to date)", backend.source_id)
            return 0

        # Index documents concurrently (up to 4 at a time)
        import asyncio

        semaphore = asyncio.Semaphore(4)
        indexed = 0

        async def _index_one(meta: DocumentMeta) -> bool:
            nonlocal indexed
            async with semaphore:
                try:
                    logger.info(
                        "Indexing: %s (%s, %d bytes)",
                        meta.name, meta.document_type.value, meta.size_bytes,
                    )
                    chunks = await self.index_document(backend, meta)
                    if chunks > 0:
                        indexed += 1
                        return True
                    logger.warning("Indexing produced 0 chunks: %s", meta.name)
                except Exception:
                    logger.warning("Failed to index %s", meta.document_id, exc_info=True)
            return False

        await asyncio.gather(*[_index_one(m) for m in to_index])

        logger.info("Sync complete for %s: %d documents indexed", backend.source_id, indexed)
        return indexed

    async def _sync_all(self) -> None:
        """Sync all backends."""
        logger.info("Starting knowledge sync across %d sources", len(self._backends))
        total = 0
        for backend in self._backends.values():
            count = await self._sync_backend(backend)
            total += count
        logger.info("Knowledge sync complete: %d documents indexed total", total)

    # --- Document tracking in entity store ---

    async def _track_document(self, meta: DocumentMeta, indexed_chunks: int = 0) -> None:
        """Store/update document tracking info in the entity store."""
        if self._storage is None:
            return
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        doc_id = meta.document_id

        # Get existing record to preserve added_at
        existing = await self._storage.get("knowledge_documents", doc_id)
        added_at = existing.get("added_at", now) if existing else now

        await self._storage.put("knowledge_documents", doc_id, {
            "document_id": doc_id,
            "source_id": meta.source_id,
            "path": meta.path,
            "name": meta.name,
            "type": meta.document_type.value,
            "size_bytes": meta.size_bytes,
            "last_modified": meta.last_modified,
            "checksum": meta.checksum,
            "external_url": meta.external_url,
            "added_at": added_at,
            "indexed_at": now if indexed_chunks > 0 else (existing or {}).get("indexed_at", ""),
            "chunks": indexed_chunks or (existing or {}).get("chunks", 0),
        })

    async def _untrack_document(self, document_id: str) -> None:
        """Remove document tracking info from entity store."""
        if self._storage is None:
            return
        try:
            await self._storage.delete("knowledge_documents", document_id)
        except Exception:
            pass

    # --- Entity store queries ---

    async def _list_from_entity_store(
        self, source_filter: str | None = None, prefix: str = ""
    ) -> list[dict[str, Any]]:
        """List documents from the entity store (fast, no backend calls)."""
        if self._storage is None:
            return []
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        filters: list[Filter] = []
        if source_filter:
            filters.append(Filter(field="source_id", op=FilterOp.EQ, value=source_filter))

        docs = await self._storage.query(Query(
            collection="knowledge_documents",
            filters=filters,
        ))

        if prefix:
            docs = [d for d in docs if d.get("path", "").startswith(prefix)]

        return docs

    # --- Events ---

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event if the event bus is available."""
        if self._event_bus is not None:
            await self._event_bus.publish(Event(
                event_type=event_type,
                data=data,
                source="knowledge",
            ))

    # --- Search ---

    async def search(
        self, query: str, n_results: int = 10, source_filter: str | None = None
    ) -> SearchResponse:
        """Search documents using hybrid name + vector approach.

        1. First, find documents whose names match query terms (fast, precise).
        2. If a name match is found, search within that document for best chunks.
        3. Also do a broad vector search and merge results (name-matched first).
        """
        if self._collection is None:
            return SearchResponse(query=query)

        effective_n = min(n_results, self._max_results)

        # Phase 1: Find documents by name match
        name_matched_doc_id = await self._find_document_by_name(query)

        # Phase 2: If we found a name match, search within it for best chunks
        name_results: list[SearchResult] = []
        if name_matched_doc_id:
            name_results = self._vector_search(
                query, effective_n,
                where_filter={"document_id": name_matched_doc_id},
            )
            if name_results:
                logger.debug(
                    "Name-matched document %s: %d chunks found",
                    name_matched_doc_id, len(name_results),
                )

        # Phase 3: Broad vector search (may find different documents)
        broad_filter: dict[str, Any] | None = None
        if source_filter:
            broad_filter = {"source_id": source_filter}
        broad_results = self._vector_search(query, effective_n, where_filter=broad_filter)

        # Merge: name-matched results first, then broad results (deduplicated)
        seen_ids: set[str] = set()
        merged: list[SearchResult] = []
        for r in name_results + broad_results:
            chunk_id = f"{r.document_id}#chunk{r.chunk_index}"
            if chunk_id not in seen_ids:
                seen_ids.add(chunk_id)
                merged.append(r)

        total = self._collection.count() if self._collection else 0
        return SearchResponse(
            query=query,
            results=merged[:effective_n],
            total_documents_searched=total,
        )

    async def _find_document_by_name(self, query: str) -> str | None:
        """Find the best document whose name matches the query terms.

        Searches tracked documents by name using substring matching.
        Returns the document_id of the best match, or None.
        """
        if self._storage is None:
            return None

        from gilbert.interfaces.storage import Query as StoreQuery

        try:
            tracked = await self._storage.query(StoreQuery(collection="knowledge_documents"))
        except Exception:
            return None

        if not tracked:
            return None

        # Score each document by how many query terms appear in its name
        terms = [t.lower() for t in query.split() if len(t) >= 3]
        if not terms:
            return None

        best_doc_id: str | None = None
        best_score = 0
        for doc in tracked:
            name = (doc.get("name") or "").lower()
            path = (doc.get("path") or "").lower()
            searchable = f"{name} {path}"
            score = sum(1 for t in terms if t in searchable)
            if score > best_score:
                best_score = score
                best_doc_id = doc.get("document_id")

        # Require at least 2 term matches, or 1 if query is short
        min_matches = 1 if len(terms) <= 2 else 2
        if best_score >= min_matches:
            return best_doc_id
        return None

    def _vector_search(
        self, query: str, n_results: int,
        where_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Run a vector search on ChromaDB. Returns SearchResult list."""
        if self._collection is None:
            return []

        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            logger.warning("ChromaDB search failed", exc_info=True)
            return []

        search_results: list[SearchResult] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for i, doc_text in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            distance = distances[i] if i < len(distances) else 1.0
            page = meta.get("page_number", -1)

            search_results.append(SearchResult(
                document_id=meta.get("document_id", ""),
                source_id=meta.get("source_id", ""),
                path=meta.get("path", ""),
                name=meta.get("name", ""),
                chunk_text=doc_text,
                relevance_score=round(1.0 - distance, 4),
                chunk_index=meta.get("chunk_index", 0),
                page_number=page if page != -1 else None,
                document_type=DocumentType(meta.get("document_type", "unknown")),
            ))

        return search_results

    # --- Backend routing ---

    def _resolve_backend(self, document_id: str) -> tuple[DocumentBackend, str]:
        """Parse 'source_id:path' and return (backend, path)."""
        for sid, backend in self._backends.items():
            prefix = sid + ":"
            if document_id.startswith(prefix):
                return backend, document_id[len(prefix):]
        raise KeyError(f"No backend found for document: {document_id}")

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "knowledge"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="search_documents",
                description=(
                    "Search the document knowledge base using natural language. "
                    "Returns relevant passages with source references."
                ),
                parameters=[
                    ToolParameter(
                        name="query", type=ToolParameterType.STRING,
                        description="Natural language search query.",
                    ),
                    ToolParameter(
                        name="max_results", type=ToolParameterType.INTEGER,
                        description="Maximum results (default 5).",
                        required=False,
                    ),
                    ToolParameter(
                        name="source", type=ToolParameterType.STRING,
                        description="Filter by source_id. Omit to search all.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="get_document",
                description="Retrieve the full text content of a document by its ID.",
                parameters=[
                    ToolParameter(
                        name="document_id", type=ToolParameterType.STRING,
                        description="Document ID (source_id:path).",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="list_documents",
                description="List documents available in the knowledge store.",
                parameters=[
                    ToolParameter(
                        name="source", type=ToolParameterType.STRING,
                        description="Filter by source_id. Omit to list all.",
                        required=False,
                    ),
                    ToolParameter(
                        name="prefix", type=ToolParameterType.STRING,
                        description="Filter by path prefix.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="list_document_sources",
                description="List all registered document sources/backends.",
                required_role="user",
            ),
            ToolDefinition(
                name="upload_document",
                description="Upload a new document to a writable source and index it.",
                parameters=[
                    ToolParameter(
                        name="source", type=ToolParameterType.STRING,
                        description="Target source_id for upload.",
                    ),
                    ToolParameter(
                        name="path", type=ToolParameterType.STRING,
                        description="File path within the source.",
                    ),
                    ToolParameter(
                        name="content", type=ToolParameterType.STRING,
                        description="Text content to store.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="index_document",
                description="Manually trigger re-indexing of a specific document.",
                parameters=[
                    ToolParameter(
                        name="document_id", type=ToolParameterType.STRING,
                        description="Document ID to re-index.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="reindex_all",
                description=(
                    "Force a full re-index of all documents. Clears tracking data "
                    "so every document is treated as new. Runs in the background."
                ),
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "search_documents":
                return await self._tool_search(arguments)
            case "get_document":
                return await self._tool_get_document(arguments)
            case "list_documents":
                return await self._tool_list_documents(arguments)
            case "list_document_sources":
                return self._tool_list_sources()
            case "upload_document":
                return await self._tool_upload(arguments)
            case "index_document":
                return await self._tool_index(arguments)
            case "reindex_all":
                return await self._tool_reindex_all()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_search(self, arguments: dict[str, Any]) -> str:
        query = arguments["query"]
        max_results = int(arguments.get("max_results", 5))
        source = arguments.get("source")

        response = await self.search(query, n_results=max_results, source_filter=source)
        return json.dumps({
            "query": response.query,
            "total_searched": response.total_documents_searched,
            "results": [
                {
                    "document_id": r.document_id,
                    "name": r.name,
                    "source_id": r.source_id,
                    "relevance": r.relevance_score,
                    "text": r.chunk_text,
                    "page": r.page_number,
                    "type": r.document_type.value,
                }
                for r in response.results
            ],
        })

    async def _tool_get_document(self, arguments: dict[str, Any]) -> str:
        document_id = arguments["document_id"]
        try:
            backend, path = self._resolve_backend(document_id)
        except KeyError as e:
            return json.dumps({"error": str(e)})

        content = await backend.get_document(path)
        if content is None:
            return json.dumps({"error": f"Document not found: {document_id}"})

        text = extract_text(content)
        return json.dumps({
            "document_id": document_id,
            "name": content.meta.name,
            "type": content.meta.document_type.value,
            "text": text[:50000],  # Cap at 50K chars for AI context
        })

    async def _tool_list_documents(self, arguments: dict[str, Any]) -> str:
        source = arguments.get("source")
        prefix = arguments.get("prefix", "")

        docs = await self._list_from_entity_store(source_filter=source, prefix=prefix)
        return json.dumps([
            {
                "document_id": d.get("document_id", ""),
                "name": d.get("name", ""),
                "source_id": d.get("source_id", ""),
                "type": d.get("type", ""),
                "size": d.get("size_bytes", 0),
                "modified": d.get("last_modified", ""),
                "indexed": bool(d.get("indexed_at")),
            }
            for d in docs
        ])

    def _tool_list_sources(self) -> str:
        sources = [
            {
                "source_id": b.source_id,
                "display_name": b.display_name,
                "read_only": b.read_only,
            }
            for b in self._backends.values()
        ]
        return json.dumps(sources)

    async def _tool_upload(self, arguments: dict[str, Any]) -> str:
        source = arguments["source"]
        path = arguments["path"]
        content = arguments["content"]

        backend = self._backends.get(source)
        if backend is None:
            return json.dumps({"error": f"Source not found: {source}"})
        if backend.read_only:
            return json.dumps({"error": f"Source is read-only: {source}"})

        try:
            meta = await backend.upload_document(path, content.encode("utf-8"))
            # Auto-index
            chunks = await self.index_document(backend, meta)
            return json.dumps({
                "status": "uploaded",
                "document_id": meta.document_id,
                "chunks_indexed": chunks,
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_index(self, arguments: dict[str, Any]) -> str:
        document_id = arguments["document_id"]
        try:
            backend, path = self._resolve_backend(document_id)
        except KeyError as e:
            return json.dumps({"error": str(e)})

        meta = await backend.get_metadata(path)
        if meta is None:
            return json.dumps({"error": f"Document not found: {document_id}"})

        chunks = await self.index_document(backend, meta)
        return json.dumps({"status": "indexed", "document_id": document_id, "chunks": chunks})

    async def _tool_reindex_all(self) -> str:
        """Clear all tracking data and trigger a full re-index."""
        # Clear tracking records so every document is treated as new
        cleared = 0
        if self._storage is not None:
            from gilbert.interfaces.storage import Query

            tracked = await self._storage.query(Query(collection="knowledge_documents"))
            for doc in tracked:
                doc_id = doc.get("_id", "")
                if doc_id:
                    await self._storage.delete("knowledge_documents", doc_id)
                    cleared += 1

        logger.info("Cleared %d tracking records — triggering full re-index", cleared)

        # Trigger sync in background
        import asyncio
        asyncio.ensure_future(self._sync_all())

        return json.dumps({
            "status": "reindex_started",
            "tracking_records_cleared": cleared,
            "message": f"Cleared {cleared} tracking records. Full re-index running in background.",
        })

    # --- WebSocket RPC handlers ---

    async def list_sources(self) -> list[dict[str, Any]]:
        """List source IDs and names (no document listing)."""
        return [
            {"source_id": b.source_id, "source_name": b.display_name}
            for b in self._backends.values()
        ]

    async def browse(self, source_id: str, path: str = "") -> list[dict[str, Any]]:
        """Browse a source at a directory path. Returns immediate children (folders + files).

        Builds a virtual directory structure from flat document paths.
        """
        backend = self._backends.get(source_id)
        if backend is None:
            return []

        try:
            docs = await backend.list_documents(prefix=path)
        except Exception:
            logger.warning("Failed to list documents from %s", source_id, exc_info=True)
            return []

        # Normalize the prefix for stripping
        prefix = path.rstrip("/") + "/" if path else ""
        prefix_len = len(prefix)

        seen_folders: set[str] = set()
        children: list[dict[str, Any]] = []

        for d in docs:
            # Get the relative path after the prefix
            rel = d.path
            if prefix and rel.startswith(prefix):
                rel = rel[prefix_len:]
            elif prefix:
                continue  # not under this path

            # If there's a "/" in the relative path, the first segment is a folder
            if "/" in rel:
                folder_name = rel.split("/", 1)[0]
                folder_path = f"{prefix}{folder_name}" if prefix else folder_name
                if folder_path not in seen_folders:
                    seen_folders.add(folder_path)
                    children.append({
                        "name": folder_name,
                        "path": folder_path,
                        "is_folder": True,
                    })
            else:
                # Direct child file
                modified = d.last_modified
                if hasattr(modified, "isoformat"):
                    modified = modified.isoformat()
                children.append({
                    "name": d.name,
                    "path": d.path,
                    "is_folder": False,
                    "size": d.size_bytes,
                    "modified": modified or "",
                    "type": d.document_type.value,
                    "external_url": d.external_url or "",
                })

        # Sort: folders first, then files, alphabetically
        children.sort(key=lambda c: (not c["is_folder"], c["name"].lower()))
        return children

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "documents.sources.list": self._ws_sources_list,
            "documents.browse": self._ws_documents_browse,
            "documents.search": self._ws_documents_search,
        }

    async def _ws_sources_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        sources = await self.list_sources()
        return {"type": "documents.sources.list.result", "ref": frame.get("id"), "sources": sources}

    async def _ws_documents_browse(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        source_id = frame.get("source_id", "")
        path = frame.get("path", "")
        if not source_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "source_id required", "code": 400}
        children = await self.browse(source_id, path)
        return {"type": "documents.browse.result", "ref": frame.get("id"), "source_id": source_id, "path": path, "children": children}

    async def _ws_documents_search(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        query = frame.get("query", "").strip()
        if not query:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "query required", "code": 400}

        results = await self.search(query, source_filter=frame.get("source_id"), n_results=frame.get("max_results", 20))
        return {"type": "documents.search.result", "ref": frame.get("id"), "results": results, "query": query}
