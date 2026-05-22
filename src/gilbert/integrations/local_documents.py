"""Local filesystem document backend — serves documents from a directory."""

import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.knowledge import (
    EXT_TO_DOCUMENT_TYPE,
    DocumentBackend,
    DocumentContent,
    DocumentMeta,
    DocumentType,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_STREAM_CHUNK_SIZE = 65536  # 64KB

_EXT_MAP = EXT_TO_DOCUMENT_TYPE

# Directories we never walk into during indexing. Catches the common
# culprits: VCS metadata, Python / Node build artifacts and caches,
# Gilbert's own data dir (so a user pointing the backend at their repo
# root doesn't recursively index ``.gilbert/`` and feed it back on
# itself), and IDE state. Names are matched as path components, not
# globs.
_DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        # VCS
        ".git",
        ".hg",
        ".svn",
        # Python
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        ".venv",
        "venv",
        # Node / frontend build
        "node_modules",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".cache",
        ".turbo",
        "coverage",
        "out",
        # Gilbert
        ".gilbert",
        # IDEs
        ".idea",
        ".vscode",
    }
)


def _type_from_ext(path: Path) -> DocumentType:
    return _EXT_MAP.get(path.suffix.lower(), DocumentType.UNKNOWN)


def _mime_from_path(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


class LocalDocumentBackend(DocumentBackend):
    """Serves documents from a local filesystem directory."""

    backend_name = "local"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="path",
                type=ToolParameterType.STRING,
                description="Local filesystem directory path to index.",
                restart_required=True,
            ),
            ConfigParam(
                key="exclude_dirs",
                type=ToolParameterType.STRING,
                description=(
                    "Additional directory names to skip during indexing, "
                    "one per line. Matches path components anywhere in "
                    "the tree (e.g. ``my-cache`` skips every ``my-cache`` "
                    "subdir). Common build/cache/VCS dirs "
                    "(``node_modules``, ``dist``, ``.git``, ``__pycache__``, "
                    "``.gilbert``, ``.venv``, etc.) are always excluded."
                ),
                default="",
                multiline=True,
            ),
        ]

    def __init__(self, name: str = "local") -> None:
        self._name = name
        self._base_path = Path(".")
        self._extra_exclude: frozenset[str] = frozenset()

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
        raw_extra = str(config.get("exclude_dirs", "") or "")
        self._extra_exclude = frozenset(
            line.strip()
            for line in raw_extra.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
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
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
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

    def _excluded_names(self) -> frozenset[str]:
        return _DEFAULT_EXCLUDE_DIRS | self._extra_exclude

    async def list_documents(self, prefix: str = "") -> list[DocumentMeta]:
        base = self._base_path.resolve()
        search_dir = (base / prefix).resolve() if prefix else base
        if not search_dir.exists():
            return []

        excluded = self._excluded_names()
        results: list[DocumentMeta] = []
        # ``os.walk`` (not ``rglob``) so we can prune subtrees in-place
        # via ``dirnames[:] = ...`` and never descend into noise like
        # ``node_modules`` / ``.git`` / ``dist`` / ``__pycache__``. Also
        # respect symlink-loop safety by leaving ``followlinks=False``
        # (the default).
        for dirpath, dirnames, filenames in os.walk(search_dir):
            dirnames[:] = [d for d in dirnames if d not in excluded]
            for filename in filenames:
                ext = Path(filename).suffix.lower()
                if ext not in _EXT_MAP:
                    continue
                file_path = Path(dirpath) / filename
                results.append(self._meta_for(file_path))
        results.sort(key=lambda m: m.path)
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

    async def upload_document(self, path: str, data: bytes, mime_type: str = "") -> DocumentMeta:
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
