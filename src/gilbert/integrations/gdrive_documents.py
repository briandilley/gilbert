"""Google Drive document backend — serves documents from Drive folders via service account."""

import asyncio
import io
import logging
import mimetypes
from typing import Any, AsyncIterator

from gilbert.interfaces.knowledge import (
    DocumentBackend,
    DocumentContent,
    DocumentMeta,
    DocumentType,
)

logger = logging.getLogger(__name__)

_STREAM_CHUNK_SIZE = 65536

# Map Google MIME types to our DocumentType
_GOOGLE_MIME_MAP: dict[str, DocumentType] = {
    "application/vnd.google-apps.document": DocumentType.WORD,
    "application/vnd.google-apps.spreadsheet": DocumentType.EXCEL,
    "application/vnd.google-apps.presentation": DocumentType.POWERPOINT,
}

# Export MIME types for Google-native formats
_EXPORT_MAP: dict[str, str] = {
    "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

_EXT_TYPE_MAP: dict[str, DocumentType] = {
    "text/plain": DocumentType.TEXT,
    "text/markdown": DocumentType.MARKDOWN,
    "text/csv": DocumentType.CSV,
    "application/json": DocumentType.JSON,
    "application/x-yaml": DocumentType.YAML,
    "application/pdf": DocumentType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocumentType.WORD,
    "application/msword": DocumentType.WORD,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentType.EXCEL,
    "application/vnd.ms-excel": DocumentType.EXCEL,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": DocumentType.POWERPOINT,
    "application/vnd.ms-powerpoint": DocumentType.POWERPOINT,
}


def _type_from_mime(mime: str, name: str) -> DocumentType:
    """Determine document type from MIME type and filename."""
    # Check Google-native types first
    if mime in _GOOGLE_MIME_MAP:
        return _GOOGLE_MIME_MAP[mime]
    # Check standard MIME types
    if mime in _EXT_TYPE_MAP:
        return _EXT_TYPE_MAP[mime]
    # Fallback to extension
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    from gilbert.integrations.local_documents import _EXT_MAP

    return _EXT_MAP.get(ext, DocumentType.UNKNOWN)


class GoogleDriveDocumentBackend(DocumentBackend):
    """Serves documents from a Google Drive folder or shared drive."""

    backend_name = "gdrive"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="service_account_json", type=ToolParameterType.STRING,
                description="Google service account key (paste JSON content).",
                sensitive=True, restart_required=True, multiline=True,
            ),
            ConfigParam(
                key="delegated_user", type=ToolParameterType.STRING,
                description="Email of the user to impersonate via domain-wide delegation.",
                default="", restart_required=True,
            ),
            ConfigParam(
                key="folder_id", type=ToolParameterType.STRING,
                description="Google Drive folder or Shared Drive ID to index.",
                restart_required=True,
            ),
        ]

    def __init__(self, name: str = "gdrive") -> None:
        self._name = name
        self._folder_id: str = ""
        self._drive: Any = None
        self._file_cache: dict[str, dict[str, Any]] = {}
        # path → folder_id cache for directory navigation
        self._folder_id_cache: dict[str, str] = {}
        # Lock to serialize Drive API calls — httplib2 is not thread-safe
        self._api_lock = asyncio.Lock()

    @property
    def source_id(self) -> str:
        return f"gdrive:{self._name}"

    @property
    def display_name(self) -> str:
        return f"Google Drive: {self._name}"

    async def initialize(self, config: dict[str, object]) -> None:
        import json as _json

        self._folder_id = str(config.get("folder_id", "") or config.get("shared_drive_id", ""))
        self._name = str(config.get("name", self._name))

        sa_json = config.get("service_account_json", "")
        delegated_user = str(config.get("delegated_user", ""))

        if sa_json:
            try:
                sa_info = _json.loads(sa_json) if isinstance(sa_json, str) else sa_json
                from google.oauth2 import service_account
                from googleapiclient.discovery import build

                scopes = ["https://www.googleapis.com/auth/drive.readonly"]
                creds = service_account.Credentials.from_service_account_info(
                    sa_info, scopes=scopes,
                )
                if delegated_user:
                    creds = creds.with_subject(delegated_user)

                self._drive = await asyncio.to_thread(
                    build, "drive", "v3", credentials=creds,
                )
            except Exception:
                logger.exception("Failed to initialize Google Drive backend '%s'", self._name)
                return
        else:
            logger.warning("Google Drive backend '%s': no credentials configured", self._name)
            return

        logger.info(
            "Google Drive backend '%s' initialized (folder=%s)",
            self._name,
            self._folder_id or "(root)",
        )

    async def close(self) -> None:
        self._drive = None
        self._file_cache.clear()

    def _is_google_native(self, mime: str) -> bool:
        return mime in _EXPORT_MAP

    async def _list_files(self, prefix: str = "") -> list[dict[str, Any]]:
        """List files from Drive recursively, handling pagination and subfolders."""
        if self._drive is None:
            return []

        root = self._folder_id or "root"
        files: list[dict[str, Any]] = []
        await self._list_files_recursive(root, "", prefix, files)
        return files

    async def _list_files_recursive(
        self, folder_id: str, path_prefix: str, filter_prefix: str,
        out: list[dict[str, Any]],
    ) -> None:
        """Recursively list files in a folder and its subfolders."""
        if self._drive is None:
            return

        query_parts = [f"'{folder_id}' in parents", "trashed = false"]
        q = " and ".join(query_parts)
        kwargs: dict[str, Any] = {
            "q": q,
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum, webViewLink)",
            "pageSize": 100,
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
        }

        page_token: str | None = None
        subfolders: list[tuple[str, str]] = []  # (folder_id, path)

        while True:
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                async with self._api_lock:
                    result = await asyncio.to_thread(
                        self._drive.files().list(**kwargs).execute
                    )
            except Exception:
                logger.warning("Drive API error listing folder %s", folder_id, exc_info=True)
                return
            for f in result.get("files", []):
                name = f.get("name", "")
                full_path = f"{path_prefix}{name}" if not path_prefix else f"{path_prefix}/{name}"

                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    subfolders.append((f["id"], full_path))
                    continue

                if filter_prefix and not full_path.startswith(filter_prefix):
                    continue

                # Skip files we can't extract text from
                doc_type = _type_from_mime(f.get("mimeType", ""), name)
                if doc_type == DocumentType.UNKNOWN:
                    continue

                f["_path"] = full_path
                out.append(f)
                self._file_cache[full_path] = f

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        # Recurse into subfolders
        for sub_id, sub_path in subfolders:
            await self._list_files_recursive(sub_id, sub_path, filter_prefix, out)

    def _file_to_meta(self, f: dict[str, Any]) -> DocumentMeta:
        """Convert a Drive file to DocumentMeta."""
        mime = f.get("mimeType", "")
        name = f.get("name", "")
        path = f.get("_path", name)  # full path including subfolders
        return DocumentMeta(
            source_id=self.source_id,
            path=path,
            name=name,
            document_type=_type_from_mime(mime, name),
            size_bytes=int(f.get("size", 0)),
            last_modified=f.get("modifiedTime", ""),
            mime_type=mime,
            checksum=f.get("md5Checksum", f.get("modifiedTime", "")),
            external_url=f.get("webViewLink", ""),
            metadata={"file_id": f.get("id", "")},
        )

    async def list_children(self, path: str = "") -> list[dict[str, Any]]:
        """List immediate children (folders + files) at a directory path.

        Unlike list_documents(), this does NOT recurse — it makes a single
        Drive API call for the folder's direct children.
        """
        if self._drive is None:
            return []

        # Resolve path to folder ID
        if not path:
            parent_id = self._folder_id or "root"
        else:
            parent_id = await self._resolve_folder_id(path)
            if not parent_id:
                return []

        query_parts = [f"'{parent_id}' in parents", "trashed = false"]
        q = " and ".join(query_parts)
        kwargs: dict[str, Any] = {
            "q": q,
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum, webViewLink)",
            "pageSize": 200,
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
            "orderBy": "folder,name",
        }

        children: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                async with self._api_lock:
                    result = await asyncio.to_thread(
                        self._drive.files().list(**kwargs).execute
                    )
            except Exception:
                logger.warning("Drive API error listing children at %s", path, exc_info=True)
                return []

            for f in result.get("files", []):
                name = f.get("name", "")
                child_path = f"{path}/{name}" if path else name
                is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"

                if is_folder:
                    # Cache folder ID for future navigation
                    self._folder_id_cache[child_path] = f["id"]
                    children.append({
                        "name": name,
                        "path": child_path,
                        "is_folder": True,
                    })
                else:
                    doc_type = _type_from_mime(f.get("mimeType", ""), name)
                    if doc_type == DocumentType.UNKNOWN:
                        continue
                    children.append({
                        "name": name,
                        "path": child_path,
                        "is_folder": False,
                        "size": int(f.get("size", 0)),
                        "modified": f.get("modifiedTime", ""),
                        "type": doc_type.value,
                        "external_url": f.get("webViewLink", ""),
                    })
                    # Cache file for metadata lookups
                    f["_path"] = child_path
                    self._file_cache[child_path] = f

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return children

    async def _resolve_folder_id(self, path: str) -> str | None:
        """Resolve a path like 'Foo/Bar' to a Drive folder ID.

        Walks the path segments, using the cache when possible and
        falling back to Drive API lookups.
        """
        if path in self._folder_id_cache:
            return self._folder_id_cache[path]

        # Walk segments from root
        parts = path.strip("/").split("/")
        current_id = self._folder_id or "root"

        built_path = ""
        for part in parts:
            built_path = f"{built_path}/{part}" if built_path else part

            if built_path in self._folder_id_cache:
                current_id = self._folder_id_cache[built_path]
                continue

            # Look up this folder in the current parent
            q = f"'{current_id}' in parents and name = '{part}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            try:
                async with self._api_lock:
                    result = await asyncio.to_thread(
                        self._drive.files().list(
                            q=q, fields="files(id)", pageSize=1,
                            includeItemsFromAllDrives=True, supportsAllDrives=True,
                        ).execute
                    )
                files = result.get("files", [])
                if not files:
                    return None
                current_id = files[0]["id"]
                self._folder_id_cache[built_path] = current_id
            except Exception:
                logger.warning("Failed to resolve folder path: %s", built_path, exc_info=True)
                return None

        return current_id

    async def list_documents(self, prefix: str = "") -> list[DocumentMeta]:
        files = await self._list_files(prefix)
        return [self._file_to_meta(f) for f in files]

    async def get_metadata(self, path: str) -> DocumentMeta | None:
        cached = self._file_cache.get(path)
        if cached:
            return self._file_to_meta(cached)
        # Fetch fresh
        files = await self._list_files()
        for f in files:
            if f.get("_path", f.get("name", "")) == path:
                return self._file_to_meta(f)
        return None

    async def get_document(self, path: str) -> DocumentContent | None:
        meta = await self.get_metadata(path)
        if meta is None:
            return None

        file_id = meta.metadata.get("file_id", "")
        if not file_id:
            return None

        data = await self._download_file(file_id, meta.mime_type)
        if data is None:
            return None

        return DocumentContent(meta=meta, data=data)

    async def _download_file(self, file_id: str, mime_type: str) -> bytes | None:
        """Download a file from Drive. Exports Google-native formats."""
        if self._drive is None:
            return None

        try:
            if self._is_google_native(mime_type):
                export_mime = _EXPORT_MAP[mime_type]
                request = self._drive.files().export_media(
                    fileId=file_id, mimeType=export_mime
                )
            else:
                request = self._drive.files().get_media(fileId=file_id)

            from googleapiclient.http import MediaIoBaseDownload

            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)

            def _do_download() -> bytes:
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                return buf.getvalue()

            async with self._api_lock:
                return await asyncio.to_thread(_do_download)
        except Exception:
            logger.warning("Failed to download file %s", file_id, exc_info=True)
            return None

    async def upload_document(
        self, path: str, data: bytes, mime_type: str = ""
    ) -> DocumentMeta:
        if self._drive is None:
            raise RuntimeError("Drive not initialized")

        from googleapiclient.http import MediaIoBaseUpload

        file_metadata: dict[str, Any] = {"name": path}
        if self._folder_id:
            file_metadata["parents"] = [self._folder_id]

        if not mime_type:
            import mimetypes as mt
            mime_type = mt.guess_type(path)[0] or "application/octet-stream"

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type)

        kwargs: dict[str, Any] = {
            "body": file_metadata,
            "media_body": media,
            "fields": "id, name, mimeType, size, modifiedTime, md5Checksum",
        }
        kwargs["supportsAllDrives"] = True

        async with self._api_lock:
            result = await asyncio.to_thread(
                self._drive.files().create(**kwargs).execute
            )
        self._file_cache[result["name"]] = result
        logger.info("Uploaded to Drive: %s", path)
        return self._file_to_meta(result)

    async def delete_document(self, path: str) -> None:
        cached = self._file_cache.get(path)
        if cached is None:
            raise KeyError(f"Document not found: {path}")
        file_id = cached.get("id", "")
        if self._drive is None:
            raise RuntimeError("Drive not initialized")

        kwargs: dict[str, Any] = {"fileId": file_id}
        kwargs["supportsAllDrives"] = True

        async with self._api_lock:
            await asyncio.to_thread(self._drive.files().delete(**kwargs).execute)
        self._file_cache.pop(path, None)

    async def stream_document(self, path: str) -> AsyncIterator[bytes]:
        content = await self.get_document(path)
        if content is None:
            return
        # Yield in chunks
        data = content.data
        offset = 0
        while offset < len(data):
            yield data[offset:offset + _STREAM_CHUNK_SIZE]
            offset += _STREAM_CHUNK_SIZE
