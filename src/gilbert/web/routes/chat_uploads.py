"""HTTP upload + download endpoints for chat attachments.

Large file attachments (anything the AI can't read natively — zips,
videos, binaries, big PDFs, …) do NOT round-trip through the WebSocket.
Instead, the frontend uploads the bytes directly via
``POST /api/chat/upload``. This module streams the multipart body to
disk under the conversation's workspace ``uploads/`` directory, returns
a JSON descriptor that becomes a reference-mode ``FileAttachment``,
and the chat frame carries only the workspace coordinates — not the bytes.

On the read side, ``GET /api/chat/download/{conv_id}/{path}`` streams
the file back out from disk.

Both endpoints enforce conversation ownership via the shared
``check_conversation_access`` helper so users can't upload into, or
download from, someone else's chat.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from gilbert.core.chat import check_conversation_access
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.storage import StorageProvider
from gilbert.interfaces.workspace import WorkspaceProvider
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat")

# Hard cap mirrored from ``ai.py:_MAX_FILE_BYTES``. Kept as a local
# constant so this module doesn't pull in ai.py (which would create a
# cycle with its web-layer dependencies at import time). Update both
# if the cap changes.
_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB

# Chunk size for streaming the upload body to disk. 1 MiB balances
# syscall overhead against memory pressure; a 1 GB upload spends at
# most ~1 MB in RAM at any moment.
_CHUNK_SIZE = 1024 * 1024

# Conversation entity collection. Matches ``_COLLECTION`` in ai.py
# but kept local for the same cycle-avoidance reason.
_CONVERSATIONS_COLLECTION = "ai_conversations"

# Characters allowed in a sanitized filename. Everything else gets
# replaced with ``_``. Lets the common case (alphanumerics, spaces,
# dashes, dots, parentheses) through while blocking path traversal
# and shell-hostile characters.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 ._\-()\[\]+]")


def _sanitize_filename(name: str) -> str:
    """Turn a browser-supplied filename into something safe to write.

    Strips path components (``..``, ``/``, ``\\``) by running through
    ``Path.name``, then replaces anything outside the safe set with
    ``_``. Ensures the result is non-empty and capped at 200 chars so
    pathological inputs don't blow out the filesystem's name limit.
    """
    # ``Path.name`` drops directory components regardless of slash
    # direction, so the user can't smuggle ``..`` or ``some/dir/file``.
    base = Path(name).name
    base = _SAFE_FILENAME_RE.sub("_", base).strip()
    if not base or base in (".", ".."):
        base = "upload.bin"
    if len(base) > 200:
        stem = Path(base).stem[:180]
        suffix = Path(base).suffix[:20]
        base = stem + suffix
    return base


def _unique_filename(workspace: Path, name: str) -> str:
    """If ``name`` already exists in ``workspace``, append ``-1``,
    ``-2``, … to the stem until a free name is found.

    Users drag-drop files repeatedly and it's annoying when the second
    upload silently replaces the first. Collision avoidance is better
    than overwriting.
    """
    path = Path(name)
    stem = path.stem or "upload"
    suffix = path.suffix
    candidate = name
    counter = 1
    while (workspace / candidate).exists():
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _resolve_services(request: Request) -> tuple[StorageProvider, WorkspaceProvider]:
    """Resolve the storage and workspace capabilities from the app.

    Both are required for upload/download; if either is missing we
    return 503 so the frontend surfaces a clear error instead of
    falling through to a cryptic AttributeError.
    """
    gilbert = getattr(request.app.state, "gilbert", None)
    if gilbert is None:
        raise HTTPException(status_code=503, detail="Gilbert is not running")
    resolver = gilbert.service_manager
    storage = resolver.get_by_capability("entity_storage")
    if not isinstance(storage, StorageProvider):
        raise HTTPException(status_code=503, detail="Entity storage unavailable")
    workspace = resolver.get_by_capability("workspace")
    if not isinstance(workspace, WorkspaceProvider):
        raise HTTPException(status_code=503, detail="Workspace service unavailable")
    return storage, workspace


async def _authorize_conversation(
    request: Request,
    conversation_id: str,
    user: UserContext,
) -> tuple[StorageProvider, WorkspaceProvider]:
    """Look up the conversation, verify the caller has access, return
    the resolved services so the caller can use them.

    Raises 404 on unknown conversations and 403 when the user can't
    see it. Both codes are deliberate — we don't want to leak the
    existence of conversations a user can't see, but there's no
    realistic attack surface here since conversation ids are UUIDs.
    """
    storage, workspace = _resolve_services(request)
    backend = storage.backend
    data = await backend.get(_CONVERSATIONS_COLLECTION, conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    err = check_conversation_access(data, user)
    if err is not None:
        raise HTTPException(status_code=403, detail=err)
    return storage, workspace


@router.post("/upload")
async def upload_chat_file(
    request: Request,
    conversation_id: str = Form(...),
    file: UploadFile = File(...),
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Stream a file to the conversation's chat-uploads workspace.

    Form fields:

    - ``conversation_id``: the id of the conversation the file
      belongs to. Must exist and be accessible to the caller.
    - ``file``: the file payload (multipart/form-data).

    Returns a JSON object shaped like the reference-mode
    ``FileAttachment`` the frontend attaches to a chat message::

        {
            "kind": "file",
            "name": "archive.zip",
            "media_type": "application/zip",
            "workspace_skill": "chat-uploads",
            "workspace_path": "archive.zip",
            "workspace_conv": "<conv_id>",
            "size": 12345678
        }

    The bytes live on disk at
    ``.gilbert/skill-workspaces/users/<user>/conversations/<conv>/chat-uploads/<name>``
    and are cleaned up automatically when the conversation is deleted
    (via the existing ``chat.conversation.destroyed`` hook in
    SkillService).

    Errors:

    - 400 — missing filename, bad request shape.
    - 401 — not authenticated.
    - 403 — caller can't access the target conversation.
    - 404 — conversation id doesn't exist.
    - 413 — file exceeds ``_MAX_UPLOAD_BYTES``.
    - 503 — storage or skills service unavailable.
    """
    _, workspace_svc = await _authorize_conversation(request, conversation_id, user)

    raw_name = file.filename or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail="file has no filename")

    safe_name = _sanitize_filename(raw_name)

    upload_dir = workspace_svc.get_upload_dir(user.user_id, conversation_id)
    unique_name = _unique_filename(upload_dir, safe_name)
    dest = upload_dir / unique_name

    # Stream to disk in chunks, enforcing the size cap as we go. We
    # can't trust ``Content-Length`` by itself (clients lie / browsers
    # sometimes omit it for large uploads) so the check happens on
    # the running total.
    total = 0
    try:
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    # Stop the write immediately — don't let a rogue
                    # client exhaust the disk. The partial file is
                    # cleaned up in the exception handler below.
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File exceeds the "
                            f"{_MAX_UPLOAD_BYTES // (1024 * 1024 * 1024)} GB "
                            "upload cap."
                        ),
                    )
                f.write(chunk)
    except HTTPException:
        # Clean up the partial file so we don't leave debris on
        # rejected uploads. ``missing_ok=True`` covers the case where
        # the exception fired before any bytes hit disk.
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        logger.exception("chat upload failed for %s", unique_name)
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}") from exc

    # Resolve the media type. Prefer the browser's hint, fall back to
    # a guess from the filename, default to octet-stream.
    media_type = (
        file.content_type
        or mimetypes.guess_type(unique_name)[0]
        or "application/octet-stream"
    )

    logger.info(
        "chat upload: user=%s conv=%s name=%r size=%d mime=%s",
        user.user_id,
        conversation_id,
        unique_name,
        total,
        media_type,
    )

    # Register in the workspace file registry
    workspace_file_id = ""
    try:
        entity = await workspace_svc.register_file(
            conversation_id=conversation_id,
            user_id=user.user_id,
            category="upload",
            filename=unique_name,
            original_name=raw_name,
            rel_path=f"uploads/{unique_name}",
            media_type=media_type,
            size=total,
            created_by="user",
        )
        workspace_file_id = entity.get("_id", "")
    except Exception:
        logger.debug("failed to register uploaded file", exc_info=True)

    return {
        "kind": "file",
        "name": unique_name,
        "media_type": media_type,
        "workspace_skill": "workspace",
        "workspace_path": f"uploads/{unique_name}",
        "workspace_conv": conversation_id,
        "workspace_file_id": workspace_file_id,
        "size": total,
    }


@router.get("/download/{conversation_id}/{path:path}")
async def download_chat_file(
    conversation_id: str,
    path: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> StreamingResponse:
    """Stream a previously-uploaded chat file back to the browser.

    The URL carries ``conversation_id`` and the ``path`` portion is
    the workspace path (as returned by ``POST /api/chat/upload``).
    Access is gated by the same ``check_conversation_access`` helper
    the upload endpoint uses, so users can't download files out of
    conversations they can't see.

    This endpoint only serves files from the ``chat-uploads`` skill
    subdirectory. Tool-produced files (PDFs generated by skills,
    etc.) still go through ``skills.workspace.download`` — they're
    usually small and have their own access model.

    Falls back to the legacy per-user workspace path when the
    per-conversation path isn't found, matching the existing
    ``skills.workspace.download`` behavior so old attachments
    persisted before per-conversation workspaces still resolve.
    """
    _, workspace_svc = await _authorize_conversation(request, conversation_id, user)

    # Sanitize the requested path to block traversal. ``path:path``
    # matches anything including slashes, but we only want a single
    # filename — uploads is a flat directory, not a tree. If a
    # client sends a nested path we reject it outright.
    if "/" in path or "\\" in path or ".." in path:
        raise HTTPException(status_code=400, detail="invalid path")

    safe_name = _sanitize_filename(path)
    upload_dir = workspace_svc.get_upload_dir(user.user_id, conversation_id)
    full = upload_dir / safe_name

    if not full.is_file():
        # Fall back to the legacy chat-uploads skill workspace for
        # attachments persisted before the workspace refactor.
        legacy = Path(".gilbert/skill-workspaces/users") / user.user_id / "conversations" / conversation_id / "chat-uploads" / safe_name
        if legacy.is_file():
            full = legacy
        else:
            # Try legacy per-user workspace
            legacy2 = Path(".gilbert/skill-workspaces") / user.user_id / "chat-uploads" / safe_name
            if legacy2.is_file():
                full = legacy2
            else:
                raise HTTPException(status_code=404, detail="file not found")

    # Resolve the real path and make sure it's still inside a
    # workspace root — belt and suspenders against symlink tricks.
    resolved = full.resolve()
    workspace_root = workspace_svc.get_workspace_root(user.user_id, conversation_id).resolve()
    legacy_root = Path(".gilbert/skill-workspaces").resolve()
    if not (
        str(resolved).startswith(str(workspace_root))
        or str(resolved).startswith(str(legacy_root))
    ):
        raise HTTPException(status_code=400, detail="path escapes workspace")

    media_type = (
        mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    )

    def _iter_file() -> Any:
        with resolved.open("rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _iter_file(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(resolved.stat().st_size),
            "Cache-Control": "private, max-age=0, no-cache",
        },
    )


@router.get("/download-all/{conversation_id}")
async def download_all_chat_files(
    conversation_id: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> StreamingResponse:
    """Stream every workspace file for a conversation back as a ZIP.

    Bundles ``uploads/``, ``outputs/``, and ``scratch/`` using the same
    directory layout as the on-disk workspace so the archive is a
    faithful snapshot of everything the user and AI have accumulated
    for this chat.

    The ZIP is built in a ``TemporaryFile`` (spills to disk, auto-deleted
    on close) which lets us compute ``Content-Length`` up front and gives
    the browser a proper download progress bar. Files that are registered
    but missing on disk are skipped rather than failing the whole archive.
    """
    _, workspace_svc = await _authorize_conversation(request, conversation_id, user)

    files = await workspace_svc.list_files(conversation_id)
    workspace_root = workspace_svc.get_workspace_root(
        user.user_id, conversation_id
    ).resolve()

    # Build the archive into an auto-deleting temp file. ``zipfile``
    # needs a seekable target to write its central directory at the
    # end, which rules out direct streaming. A temp file keeps memory
    # bounded regardless of archive size. It outlives this function
    # — the StreamingResponse's generator closes it after the last
    # chunk ships — so a ``with`` block isn't usable here.
    tmp = tempfile.TemporaryFile()  # noqa: SIM115
    included = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            seen: set[str] = set()
            for entry in files:
                rel_path = entry.get("rel_path") or ""
                if not rel_path or rel_path in seen:
                    continue
                seen.add(rel_path)
                full = (workspace_root / rel_path).resolve()
                # Belt and suspenders: skip anything that somehow
                # escapes the workspace root (bad registry data,
                # symlink tricks).
                try:
                    full.relative_to(workspace_root)
                except ValueError:
                    continue
                if not full.is_file():
                    continue
                zf.write(full, arcname=rel_path)
                included += 1
    except Exception:
        tmp.close()
        logger.exception(
            "zip build failed for conv=%s user=%s", conversation_id, user.user_id
        )
        raise HTTPException(status_code=500, detail="failed to build archive") from None

    tmp.seek(0, 2)
    size = tmp.tell()
    tmp.seek(0)

    logger.info(
        "workspace zip: user=%s conv=%s files=%d size=%d",
        user.user_id,
        conversation_id,
        included,
        size,
    )

    def _iter_zip() -> Any:
        try:
            while True:
                chunk = tmp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            tmp.close()

    filename = f"workspace-{conversation_id}.zip"
    return StreamingResponse(
        _iter_zip(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
            "Cache-Control": "private, max-age=0, no-cache",
        },
    )
