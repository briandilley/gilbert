# Knowledge Service (Document Store)

## Summary
Multi-backend document knowledge store with ChromaDB vector search. Indexes documents from local filesystem and Google Drive, supports semantic search via AI tools and web UI.

## Details

### Interface
- `src/gilbert/interfaces/knowledge.py` — `DocumentBackend` ABC, `DocumentMeta`, `DocumentContent`, `DocumentChunk`, `SearchResult`, `SearchResponse`, `DocumentType` enum, **`KnowledgeProvider` capability protocol** (`index_document`, `remove_document`, `resolve_document`, `get_backend`, read-only `backends` property)
- Documents identified by `source_id:path` (document_id)
- Configuration uses per-type sub-sections (`local`, `gdrive`) instead of a sources array. Each sub-section has its own `enabled` flag and settings.

### KnowledgeProvider protocol
`@runtime_checkable Protocol` introduced alongside the feeds feature
so consumers (`InboxService`, `FeedsService`) can `isinstance`-check
against the protocol instead of duck-typing
(`getattr(svc, "backends", ...)`). `KnowledgeService` already
implemented `index_document`, `resolve_document`, `get_backend`, and
the `backends` property — the protocol wraps the existing public
surface. **`remove_document` is the new public method** added with
this PR; used by `FeedsService` for retention purges and unsubscribe
cascade. Removes the document's chunks from ChromaDB, drops the
tracking row, deletes cached text, emits
`knowledge.document.removed`.

### Service
- `src/gilbert/core/services/knowledge.py` — `KnowledgeService`
- Capabilities: `knowledge`, `ai_tools`
- Aggregates multiple backends in `dict[str, DocumentBackend]`
- ChromaDB `PersistentClient` at `.gilbert/chromadb/`, collection "documents"
- Background sync via scheduler system timer `knowledge-sync` (default 5min)
- Initial sync on startup before registering periodic timer
- Change detection: compares `last_modified` against ChromaDB metadata
- Removal detection: documents that disappear from backend are removed from index

### Document Processing
- `src/gilbert/core/documents/extractors.py` — text extraction per type with optional Vision + OCR enrichment. PDF uses PyMuPDF. Returns `(text, ExtractionStats)`. Page markers: `[Page N]` format.
- `src/gilbert/core/documents/chunking.py` — paragraph-based chunking with overlap, sentence sub-splitting, PDF page tracking via `[Page N]` markers
- Vision: Claude Vision describes image-heavy pages (sparse text + images) during indexing. VisionService capability: `vision`.
- OCR: Tesseract extracts text from images/scanned pages. OCRService capability: `ocr`. Gracefully degrades if tesseract not installed.
- Extracted text (including Vision/OCR content) cached in entity store (`knowledge_text` collection) for fast keyword search at query time.

### Backends
- `src/gilbert/integrations/local_documents.py` — `LocalDocumentBackend`: recursive dir scan, path traversal prevention, extension-to-type mapping
- `src/gilbert/integrations/gdrive_documents.py` — `GoogleDriveDocumentBackend`: self-contained with its own `service_account_json` config param. Builds its own Drive API client during `initialize()`. No external GoogleService dependency. Exports Google-native docs as Office formats.

### AI Tools (all default to "user" role)
- `search_documents` — semantic vector search
- `list_documents`, `list_document_sources` — browse
- `get_document` — retrieve full text
- `upload_document` (admin) — upload + auto-index
- `index_document` (admin) — manual re-indexing
- `reindex_all` (admin) — clear tracking, force full re-index

### Web UI
- `/documents` — browse by source with filter tabs
- `/documents/search` — search interface with relevance scores
- `/documents/serve/{source_id}/{path}` — stream documents from any backend
- Dashboard card: "Documents" (user role)

### Events Published
- `knowledge.document.discovered` — new document found during sync
- `knowledge.document.indexed` — document chunked and embedded in ChromaDB
- `knowledge.document.removed` — document disappeared from backend, removed from index

### Configuration
```yaml
knowledge:
  enabled: false
  local:
    enabled: false
    name: local
    path: ""
  gdrive:
    enabled: false
    name: gdrive
    folder_id: ""
    # service_account_json in backend settings
  sync_interval_seconds: 300
  chunk_size: 800
  chunk_overlap: 200
  max_search_results: 20
  chromadb_path: ".gilbert/chromadb"
  vision_enabled: true
  vision_model: "claude-sonnet-4-5-20250929"
```

### Dependencies (heavy)
- chromadb (pulls sentence-transformers + torch ~2GB)
- pymupdf (PyMuPDF for PDF rendering + text extraction)
- pypdf (used by screen service for page extraction)
- python-docx, openpyxl, python-pptx
- pytesseract + Pillow (OCR, optional — needs tesseract-ocr system package)
- anthropic (Vision API, shared with AI service)

## Related
- `src/gilbert/core/services/scheduler.py` — runs periodic sync job
- `src/gilbert/integrations/gdrive_documents.py` — GDrive backend (self-contained, owns service_account_json)
- `tests/unit/test_knowledge_service.py` — unit tests
