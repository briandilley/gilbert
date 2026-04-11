"""Local filesystem document backend — serves documents from a directory."""

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from gilbert.interfaces.knowledge import (
    DocumentBackend,
    DocumentContent,
    DocumentMeta,
    DocumentType,
)

logger = logging.getLogger(__name__)

_STREAM_CHUNK_SIZE = 65536  # 64KB

_EXT_MAP: dict[str, DocumentType] = {
    ".txt": DocumentType.TEXT,
    ".md": DocumentType.MARKDOWN,
    ".csv": DocumentType.CSV,
    ".json": DocumentType.JSON,
    ".yaml": DocumentType.YAML,
    ".yml": DocumentType.YAML,
    ".pdf": DocumentType.PDF,
    ".docx": DocumentType.WORD,
    ".doc": DocumentType.WORD,
    ".xlsx": DocumentType.EXCEL,
    ".xls": DocumentType.EXCEL,
    ".pptx": DocumentType.POWERPOINT,
    ".ppt": DocumentType.POWERPOINT,
}


def _type_from_ext(path: Path) -> DocumentType:
    return _EXT_MAP.get(path.suffix.lower(), DocumentType.UNKNOWN)


def _mime_from_path(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


class LocalDocumentBackend(DocumentBackend):
    """Serves documents from a local filesystem directory."""

    backend_name = "local"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="path", type=ToolParameterType.STRING,
                description="Local filesystem directory path to index.",
                restart_required=True,
            ),
        ]

    def __init__(self, name: str = "local") -> None:
        self._name = name
        self._base_path = Path(".")

    @property
    def source_id(self) -> str:
        return f"local:{self._name}"

    @property
    def display_name(self) -> str:
        return f"Local: {self._name} ({self._base_path})"

    async def initialize(self, config: dict[str, object]) -> None:
        self._name = str(config.get("name", self._name))
        path = str(config.get("path", ""))
        if path:
            self._base_path = Path(path)
        if not self._base_path.exists():
            self._base_path.mkdir(parents=True, exist_ok=True)
            logger.info("Created document directory: %s", self._base_path)
        logger.info("Local document backend '%s' at %s", self._name, self._base_path)

    async def close(self) -> None:
        pass

    def _resolve(self, path: str) -> Path:
        """Resolve a relative path safely within base_path."""
        full = (self._base_path / path).resolve()
        if not full.is_relative_to(self._base_path.resolve()):
            raise PermissionError(f"Path escapes base directory: {path}")
        return full

    def _meta_for(self, file_path: Path) -> DocumentMeta:
        """Build metadata for a file."""
        stat = file_path.stat()
        rel = file_path.relative_to(self._base_path.resolve())
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return DocumentMeta(
            source_id=self.source_id,
            path=str(rel),
            name=file_path.name,
            document_type=_type_from_ext(file_path),
            size_bytes=stat.st_size,
            last_modified=modified,
            mime_type=_mime_from_path(file_path),
            checksum=f"{stat.st_size}:{int(stat.st_mtime)}",
        )

    async def list_documents(self, prefix: str = "") -> list[DocumentMeta]:
        base = self._base_path.resolve()
        search_dir = (base / prefix) if prefix else base
        if not search_dir.exists():
            return []

        results: list[DocumentMeta] = []
        for file_path in sorted(base.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in _EXT_MAP:
                continue
            if prefix:
                rel = file_path.relative_to(base)
                if not str(rel).startswith(prefix):
                    continue
            results.append(self._meta_for(file_path))
        return results

    async def get_document(self, path: str) -> DocumentContent | None:
        file_path = self._resolve(path)
        if not file_path.exists() or not file_path.is_file():
            return None
        meta = self._meta_for(file_path)
        data = file_path.read_bytes()
        return DocumentContent(meta=meta, data=data)

    async def get_metadata(self, path: str) -> DocumentMeta | None:
        file_path = self._resolve(path)
        if not file_path.exists() or not file_path.is_file():
            return None
        return self._meta_for(file_path)

    async def upload_document(
        self, path: str, data: bytes, mime_type: str = ""
    ) -> DocumentMeta:
        file_path = self._resolve(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        logger.info("Uploaded document: %s", file_path)
        return self._meta_for(file_path)

    async def delete_document(self, path: str) -> None:
        file_path = self._resolve(path)
        if not file_path.exists():
            raise KeyError(f"Document not found: {path}")
        file_path.unlink()
        logger.info("Deleted document: %s", file_path)

    async def stream_document(self, path: str) -> AsyncIterator[bytes]:
        file_path = self._resolve(path)
        if not file_path.exists():
            return
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
