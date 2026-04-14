"""Tests for KnowledgeService — document indexing, search, and multi-backend aggregation."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.documents.chunking import chunk_text
from gilbert.core.documents.extractors import extract_text
from gilbert.core.services.knowledge import KnowledgeService
from gilbert.interfaces.knowledge import (
    DocumentBackend,
    DocumentContent,
    DocumentMeta,
    DocumentType,
    SearchResponse,
    SearchResult,
)


# --- Document type and metadata ---


class TestDocumentMeta:
    def test_document_id(self) -> None:
        meta = DocumentMeta(source_id="local:docs", path="report.pdf", name="report.pdf",
                           document_type=DocumentType.PDF)
        assert meta.document_id == "local:docs:report.pdf"

    def test_document_id_with_subpath(self) -> None:
        meta = DocumentMeta(source_id="gdrive:lib", path="folder/doc.txt", name="doc.txt",
                           document_type=DocumentType.TEXT)
        assert meta.document_id == "gdrive:lib:folder/doc.txt"


# --- Text extraction ---


class TestExtractors:
    def test_text_extraction(self) -> None:
        meta = DocumentMeta(source_id="test", path="f.txt", name="f.txt",
                           document_type=DocumentType.TEXT)
        content = DocumentContent(meta=meta, data=b"Hello world")
        text, stats = extract_text(content)
        assert text == "Hello world"

    def test_markdown_extraction(self) -> None:
        meta = DocumentMeta(source_id="test", path="f.md", name="f.md",
                           document_type=DocumentType.MARKDOWN)
        content = DocumentContent(meta=meta, data=b"# Title\n\nBody text")
        text, stats = extract_text(content)
        assert "Title" in text
        assert "Body text" in text

    def test_json_extraction(self) -> None:
        meta = DocumentMeta(source_id="test", path="f.json", name="f.json",
                           document_type=DocumentType.JSON)
        content = DocumentContent(meta=meta, data=b'{"key": "value"}')
        text, stats = extract_text(content)
        assert "key" in text
        assert "value" in text

    def test_csv_extraction(self) -> None:
        meta = DocumentMeta(source_id="test", path="f.csv", name="f.csv",
                           document_type=DocumentType.CSV)
        content = DocumentContent(meta=meta, data=b"name,age\nAlice,30\nBob,25")
        text, stats = extract_text(content)
        assert "Alice" in text

    def test_unknown_falls_back_to_text(self) -> None:
        meta = DocumentMeta(source_id="test", path="f.xyz", name="f.xyz",
                           document_type=DocumentType.UNKNOWN)
        content = DocumentContent(meta=meta, data=b"some text")
        text, stats = extract_text(content)
        assert text == "some text"


# --- Chunking ---


class TestChunking:
    def test_basic_chunking(self) -> None:
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunk_text(text, "doc1", chunk_size=50, chunk_overlap=10)
        assert len(chunks) >= 1
        assert all(c.document_id == "doc1" for c in chunks)

    def test_respects_chunk_size(self) -> None:
        # Create text with many paragraphs
        text = "\n\n".join(f"Paragraph {i} with some content." for i in range(20))
        chunks = chunk_text(text, "doc1", chunk_size=100, chunk_overlap=20)
        for c in chunks:
            # Allow some tolerance for overlap
            assert len(c.text) <= 200  # chunk_size + reasonable overlap

    def test_empty_text_returns_empty(self) -> None:
        assert chunk_text("", "doc1") == []
        assert chunk_text("   ", "doc1") == []

    def test_single_paragraph(self) -> None:
        chunks = chunk_text("Hello world.", "doc1")
        assert len(chunks) == 1
        assert chunks[0].text == "Hello world."
        assert chunks[0].chunk_index == 0

    def test_page_number_detection(self) -> None:
        text = "[Page 1]\nContent on page 1.\n\n[Page 2]\nContent on page 2."
        chunks = chunk_text(text, "doc1", chunk_size=5000)
        assert chunks[0].page_number == 1

    def test_chunks_have_sequential_indices(self) -> None:
        text = "\n\n".join(f"Para {i}." for i in range(10))
        chunks = chunk_text(text, "doc1", chunk_size=30, chunk_overlap=5)
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))


# --- Config ---


class TestConfig:
    def test_knowledge_defaults(self) -> None:
        config = GilbertConfig.model_validate({})
        assert config.knowledge.enabled is False
        assert config.knowledge.chunk_size == 800
        assert config.knowledge.local.enabled is False
        assert config.knowledge.gdrive.enabled is False

    def test_knowledge_full(self) -> None:
        raw = {
            "knowledge": {
                "enabled": True,
                "sync_interval_seconds": 120,
                "local": {"enabled": True, "name": "docs", "path": "/tmp/docs"},
                "gdrive": {"enabled": True, "name": "lib", "folder_id": "abc123"},
            }
        }
        config = GilbertConfig.model_validate(raw)
        assert config.knowledge.enabled is True
        assert config.knowledge.local.enabled is True
        assert config.knowledge.local.path == "/tmp/docs"
        assert config.knowledge.gdrive.folder_id == "abc123"


# --- SearchResult / SearchResponse ---


class TestSearchModels:
    def test_search_response(self) -> None:
        results = [
            SearchResult(
                document_id="local:docs:report.pdf",
                source_id="local:docs",
                path="report.pdf",
                name="report.pdf",
                chunk_text="Revenue increased by 15%.",
                relevance_score=0.92,
                chunk_index=3,
                page_number=5,
                document_type=DocumentType.PDF,
            )
        ]
        response = SearchResponse(query="revenue growth", results=results, total_documents_searched=50)
        assert response.query == "revenue growth"
        assert len(response.results) == 1
        assert response.results[0].relevance_score == 0.92


# --- render_document_page tool ---


def _make_pdf_bytes() -> bytes:
    """Create a minimal single-page PDF using PyMuPDF."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((10, 50), "Test page content")
    data = doc.tobytes()
    doc.close()
    return data


class TestRenderDocumentPage:
    @pytest.fixture
    def knowledge_service(self) -> KnowledgeService:
        svc = KnowledgeService()
        svc._enabled = True
        return svc

    @pytest.fixture
    def pdf_content(self) -> DocumentContent:
        meta = DocumentMeta(
            source_id="local:docs", path="manual.pdf", name="manual.pdf",
            document_type=DocumentType.PDF,
        )
        return DocumentContent(meta=meta, data=_make_pdf_bytes())

    @pytest.fixture
    def stub_backend(self, pdf_content: DocumentContent) -> AsyncMock:
        backend = AsyncMock(spec=DocumentBackend)
        backend.get_document.return_value = pdf_content
        return backend

    @pytest.mark.asyncio
    async def test_renders_pdf_page(
        self, knowledge_service: KnowledgeService,
        stub_backend: AsyncMock, tmp_path: Path,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}

        with patch("gilbert.core.services.knowledge.get_output_dir", return_value=tmp_path):
            result = await knowledge_service._tool_render_page({
                "document_id": "local:docs:manual.pdf",
                "page": 1,
            })

        data = json.loads(result)
        assert data["page"] == 1
        assert "/output/knowledge/" in data["image_url"]
        assert "![manual.pdf - Page 1]" in data["markdown"]
        # Verify image file was written
        png_files = list(tmp_path.glob("*.png"))
        assert len(png_files) == 1
        assert png_files[0].stat().st_size > 0

    @pytest.mark.asyncio
    async def test_page_out_of_range(
        self, knowledge_service: KnowledgeService,
        stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}

        result = await knowledge_service._tool_render_page({
            "document_id": "local:docs:manual.pdf",
            "page": 999,
        })
        data = json.loads(result)
        assert "error" in data
        assert "out of range" in data["error"]

    @pytest.mark.asyncio
    async def test_non_pdf_rejected(
        self, knowledge_service: KnowledgeService,
    ) -> None:
        meta = DocumentMeta(
            source_id="local:docs", path="notes.txt", name="notes.txt",
            document_type=DocumentType.TEXT,
        )
        content = DocumentContent(meta=meta, data=b"hello")
        backend = AsyncMock(spec=DocumentBackend)
        backend.get_document.return_value = content
        knowledge_service._backends = {"local:docs": backend}

        result = await knowledge_service._tool_render_page({
            "document_id": "local:docs:notes.txt",
            "page": 1,
        })
        data = json.loads(result)
        assert "error" in data
        assert "PDF" in data["error"]

    @pytest.mark.asyncio
    async def test_negative_page_number(
        self, knowledge_service: KnowledgeService,
    ) -> None:
        result = await knowledge_service._tool_render_page({
            "document_id": "local:docs:manual.pdf",
            "page": 0,
        })
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_document_not_found(
        self, knowledge_service: KnowledgeService,
    ) -> None:
        backend = AsyncMock(spec=DocumentBackend)
        backend.get_document.return_value = None
        knowledge_service._backends = {"local:docs": backend}

        result = await knowledge_service._tool_render_page({
            "document_id": "local:docs:missing.pdf",
            "page": 1,
        })
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower()


# --- find_files tool ---


class TestFindFiles:
    @pytest.fixture
    def knowledge_service(self) -> KnowledgeService:
        svc = KnowledgeService()
        svc._enabled = True
        return svc

    @pytest.fixture
    def mixed_files(self) -> list[DocumentMeta]:
        return [
            DocumentMeta(
                source_id="local:docs", path="photo.jpg", name="photo.jpg",
                document_type=DocumentType.IMAGE, size_bytes=50000,
                mime_type="image/jpeg",
            ),
            DocumentMeta(
                source_id="local:docs", path="logo.png", name="logo.png",
                document_type=DocumentType.IMAGE, size_bytes=12000,
                mime_type="image/png",
            ),
            DocumentMeta(
                source_id="local:docs", path="report.pdf", name="report.pdf",
                document_type=DocumentType.PDF, size_bytes=200000,
                mime_type="application/pdf",
            ),
            DocumentMeta(
                source_id="local:docs", path="clip.mp4", name="clip.mp4",
                document_type=DocumentType.VIDEO, size_bytes=5000000,
                mime_type="video/mp4",
            ),
            DocumentMeta(
                source_id="local:docs", path="notes.txt", name="notes.txt",
                document_type=DocumentType.TEXT, size_bytes=500,
                mime_type="text/plain",
            ),
        ]

    @pytest.fixture
    def stub_backend(self, mixed_files: list[DocumentMeta]) -> AsyncMock:
        backend = AsyncMock(spec=DocumentBackend)
        backend.list_documents.return_value = mixed_files
        return backend

    @pytest.mark.asyncio
    async def test_find_all_files(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files({})
        data = json.loads(result)
        assert data["total_found"] == 5

    @pytest.mark.asyncio
    async def test_find_by_type_image(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files({"type": "image"})
        data = json.loads(result)
        assert data["total_found"] == 2
        for f in data["files"]:
            assert f["type"] == "image"
            assert "markdown" in f  # Images get markdown tags
            assert f["url"].startswith("/documents/serve/")

    @pytest.mark.asyncio
    async def test_find_by_type_video(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files({"type": "video"})
        data = json.loads(result)
        assert data["total_found"] == 1
        assert data["files"][0]["name"] == "clip.mp4"

    @pytest.mark.asyncio
    async def test_find_by_name(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files({"name": "logo"})
        data = json.loads(result)
        assert data["total_found"] == 1
        assert data["files"][0]["name"] == "logo.png"

    @pytest.mark.asyncio
    async def test_find_by_type_and_name(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files(
            {"type": "image", "name": "photo"},
        )
        data = json.loads(result)
        assert data["total_found"] == 1
        assert data["files"][0]["name"] == "photo.jpg"

    @pytest.mark.asyncio
    async def test_find_no_matches(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files(
            {"type": "audio"},
        )
        data = json.loads(result)
        assert data["total_found"] == 0

    @pytest.mark.asyncio
    async def test_find_invalid_type(
        self, knowledge_service: KnowledgeService,
    ) -> None:
        knowledge_service._backends = {}
        result = await knowledge_service._tool_find_files(
            {"type": "spreadsheet"},
        )
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_find_respects_max_results(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files({"max_results": 2})
        data = json.loads(result)
        assert data["total_found"] == 2

    @pytest.mark.asyncio
    async def test_find_filters_by_source(
        self, knowledge_service: KnowledgeService,
    ) -> None:
        backend1 = AsyncMock(spec=DocumentBackend)
        backend1.list_documents.return_value = [
            DocumentMeta(
                source_id="local:a", path="a.png", name="a.png",
                document_type=DocumentType.IMAGE,
            ),
        ]
        backend2 = AsyncMock(spec=DocumentBackend)
        backend2.list_documents.return_value = [
            DocumentMeta(
                source_id="local:b", path="b.png", name="b.png",
                document_type=DocumentType.IMAGE,
            ),
        ]
        knowledge_service._backends = {"local:a": backend1, "local:b": backend2}

        result = await knowledge_service._tool_find_files({"source": "local:a"})
        data = json.loads(result)
        assert data["total_found"] == 1
        assert data["files"][0]["source_id"] == "local:a"
        # backend2 should not have been queried
        backend2.list_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_markdown_contains_serve_url(
        self, knowledge_service: KnowledgeService, stub_backend: AsyncMock,
    ) -> None:
        knowledge_service._backends = {"local:docs": stub_backend}
        result = await knowledge_service._tool_find_files({"type": "image"})
        data = json.loads(result)
        for f in data["files"]:
            assert f["markdown"] == f"![{f['name']}]({f['url']})"
