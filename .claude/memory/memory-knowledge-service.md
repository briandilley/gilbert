# Knowledge Service (Document Store)

## Summary
Multi-backend document knowledge store with ChromaDB vector search. Indexes documents from local filesystem and Google Drive, supports semantic search via AI tools and web UI.

## Details

### Interface
- `src/gilbert/interfaces/knowledge.py` — `DocumentBackend` ABC, `DocumentMeta`, `DocumentContent`, `DocumentChunk`, `SearchResult`, `SearchResponse`, `DocumentType` enum
- Documents identified by `source_id:path` (document_id)

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
- `src/gilbert/integrations/gdrive_documents.py` — `GoogleDriveDocumentBackend`: service account via GoogleService, exports Google-native docs as Office formats

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

### Dependencies (heavy)
- chromadb (pulls sentence-transformers + torch ~2GB)
- pymupdf (PyMuPDF for PDF rendering + text extraction)
- pypdf (used by screen service for page extraction)
- python-docx, openpyxl, python-pptx
- pytesseract + Pillow (OCR, optional — needs tesseract-ocr system package)
- anthropic (Vision API, shared with AI service)

## Related
- `src/gilbert/core/services/scheduler.py` — runs periodic sync job
- `src/gilbert/core/services/google.py` — provides Drive API clients
- `tests/unit/test_knowledge_service.py` — 16 tests
