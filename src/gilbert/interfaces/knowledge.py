"""Document knowledge store interface — backends, metadata, and search models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, AsyncIterator


class DocumentType(StrEnum):
    """Supported document file types."""

    TEXT = "text"
    MARKDOWN = "markdown"
    CSV = "csv"
    JSON = "json"
    YAML = "yaml"
    PDF = "pdf"
    WORD = "word"
    EXCEL = "excel"
    POWERPOINT = "powerpoint"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DocumentMeta:
    """Metadata for a document in a backend."""

    source_id: str
    path: str
    name: str
    document_type: DocumentType
    size_bytes: int = 0
    last_modified: str = ""
    mime_type: str = ""
    checksum: str = ""
    external_url: str = ""  # URL to access the document in its native system
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def document_id(self) -> str:
        """Globally unique identifier: source_id:path."""
        return f"{self.source_id}:{self.path}"


@dataclass(frozen=True)
class DocumentContent:
    """Raw content fetched from a backend."""

    meta: DocumentMeta
    data: bytes
    encoding: str = "utf-8"


@dataclass
class ExtractionStats:
    """Statistics from document text extraction."""

    pages: int = 0
    images_found: int = 0
    ocr_pages: int = 0
    ocr_chars: int = 0
    vision_pages: int = 0
    vision_chars: int = 0
    total_chars: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DocumentChunk:
    """A chunk of extracted text from a document, ready for embedding."""

    document_id: str
    chunk_index: int
    text: str
    start_offset: int = 0
    end_offset: int = 0
    page_number: int | None = None


@dataclass(frozen=True)
class SearchResult:
    """A single search result from the knowledge store."""

    document_id: str
    source_id: str
    path: str
    name: str
    chunk_text: str
    relevance_score: float
    chunk_index: int
    page_number: int | None = None
    document_type: DocumentType = DocumentType.UNKNOWN


@dataclass(frozen=True)
class SearchResponse:
    """Response from a knowledge search query."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    total_documents_searched: int = 0


class DocumentBackend(ABC):
    """Abstract document backend. Each instance represents one source."""

    _registry: dict[str, type["DocumentBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            DocumentBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["DocumentBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        """Describe backend-specific configuration parameters."""
        return []

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for this backend instance."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for this source."""
        ...

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the backend with configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...

    @abstractmethod
    async def list_documents(self, prefix: str = "") -> list[DocumentMeta]:
        """List all documents, optionally filtered by path prefix."""
        ...

    async def list_children(self, path: str = "") -> list[dict[str, Any]]:
        """List immediate children at a directory path (folders + files).

        Returns dicts with keys: name, path, is_folder, and optionally
        size, modified, type, external_url for files.

        Default implementation falls back to list_documents() and builds
        the directory structure client-side. Backends should override for
        efficient single-level listing.
        """
        docs = await self.list_documents(prefix=path)
        prefix_str = path.rstrip("/") + "/" if path else ""
        prefix_len = len(prefix_str)

        seen_folders: set[str] = set()
        children: list[dict[str, Any]] = []

        for d in docs:
            rel = d.path
            if prefix_str and rel.startswith(prefix_str):
                rel = rel[prefix_len:]
            elif prefix_str:
                continue

            if "/" in rel:
                folder_name = rel.split("/", 1)[0]
                folder_path = f"{prefix_str}{folder_name}" if prefix_str else folder_name
                if folder_path not in seen_folders:
                    seen_folders.add(folder_path)
                    children.append({"name": folder_name, "path": folder_path, "is_folder": True})
            else:
                modified = d.last_modified
                if hasattr(modified, "isoformat"):
                    modified = modified.isoformat()
                children.append({
                    "name": d.name, "path": d.path, "is_folder": False,
                    "size": d.size_bytes, "modified": modified or "",
                    "type": d.document_type.value, "external_url": d.external_url or "",
                })

        children.sort(key=lambda c: (not c["is_folder"], c["name"].lower()))
        return children

    @abstractmethod
    async def get_document(self, path: str) -> DocumentContent | None:
        """Fetch the full content of a document by path."""
        ...

    @abstractmethod
    async def get_metadata(self, path: str) -> DocumentMeta | None:
        """Get metadata for a single document without fetching content."""
        ...

    @abstractmethod
    async def upload_document(
        self, path: str, data: bytes, mime_type: str = ""
    ) -> DocumentMeta:
        """Upload/create a document. Raises PermissionError if read-only."""
        ...

    @abstractmethod
    async def delete_document(self, path: str) -> None:
        """Delete a document. Raises KeyError if not found."""
        ...

    @abstractmethod
    async def stream_document(self, path: str) -> AsyncIterator[bytes]:
        """Stream document content in chunks for web serving."""
        ...

    @property
    def read_only(self) -> bool:
        """Whether this backend supports uploads."""
        return False
