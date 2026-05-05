"""HTTP upload + download endpoints for agent avatar images.

Avatars are images attached to an Agent that the SPA renders next to
the agent's name in lists, headers, and message attribution. The
backing storage is intentionally **not** the per-conversation skill
workspace: an agent may not yet have a conversation when the user
first uploads an avatar (avatars are part of the Agent identity, not
of any one run). Instead we drop the bytes into a service-owned bucket
under the per-installation data directory:

    .gilbert/agent-avatars/<agent_id>/<sha-suffixed-name>

``Agent.avatar_kind`` is set to ``"image"`` and ``Agent.avatar_value``
holds the SHA-suffixed filename, e.g. ``portrait-ab12cd34.png``. The
GET endpoint reconstructs the full disk path from
``agent_id + agent.avatar_value`` and streams the bytes back.

Both endpoints require authentication and load the agent through
``AgentService._load_agent_for_caller`` so a user can only upload to /
download from agents they own (admins bypass the ownership check, same
as the agents.* WS RPCs).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from gilbert.config import DATA_DIR
from gilbert.interfaces.auth import UserContext
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents")

# Avatars live alongside the rest of the per-installation data under
# ``.gilbert/``. ``DATA_DIR`` is gitignored so user-uploaded image
# bytes never accidentally land in the tracked tree.
_AVATAR_ROOT = DATA_DIR / "agent-avatars"

# 4 MiB is plenty for an avatar — anything bigger is almost certainly
# someone trying to use the avatar slot as general file storage.
_MAX_AVATAR_BYTES = 4 * 1024 * 1024

# Chunk size for streaming uploads to disk. Avatars are small, so the
# loop will usually terminate after one chunk; we still cap memory at
# 1 MiB for the (uncommon) larger-than-1MiB legitimate case.
_CHUNK_SIZE = 1024 * 1024

# Image content types we accept. Anything else is rejected with a 415.
_ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

# Mapping of accepted MIME → canonical extension. Used to choose a
# stable suffix for the on-disk filename when the browser-supplied
# filename has no useful extension.
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Same safe-set as chat_uploads — duplicated rather than imported to
# avoid pulling chat_uploads' transitive deps into this route.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 ._\-()\[\]+]")


def _sanitize_filename(name: str) -> str:
    """Strip path components and unsafe characters from *name*.

    Mirrors ``chat_uploads._sanitize_filename`` so behavior is consistent
    across upload routes. ``Path.name`` discards directories regardless
    of slash direction, which blocks ``../`` traversal even before the
    regex filter runs.
    """
    base = Path(name).name
    base = _SAFE_FILENAME_RE.sub("_", base).strip()
    if not base or base in (".", ".."):
        base = "avatar.bin"
    if len(base) > 200:
        stem = Path(base).stem[:180]
        suffix = Path(base).suffix[:20]
        base = stem + suffix
    return base


def _avatar_dir(agent_id: str) -> Path:
    """Return the on-disk directory holding *agent_id*'s avatar files.

    Uses ``Path.name`` on the supplied id as a defense-in-depth measure
    against a caller smuggling path components — the route always loads
    the agent via storage by id, so this should be unreachable, but we
    don't want a slip elsewhere to compromise the filesystem layout.
    """
    safe_id = Path(agent_id).name
    return _AVATAR_ROOT / safe_id


def _resolve_agent_service(request: Request) -> Any:
    """Return the running ``AgentService`` (capability ``agent``).

    Returns 503 when Gilbert isn't running or the capability isn't
    registered, matching the error model used by ``chat_uploads``.
    """
    gilbert = getattr(request.app.state, "gilbert", None)
    if gilbert is None:
        raise HTTPException(status_code=503, detail="Gilbert is not running")
    resolver = gilbert.service_manager
    agent_svc = resolver.get_by_capability("agent")
    if agent_svc is None:
        raise HTTPException(status_code=503, detail="Agent service unavailable")
    return agent_svc


def _is_admin(user: UserContext) -> bool:
    """True if *user* has the admin role (or root).

    The HTTP route can't reach ``conn.user_level`` the way the WS RPC
    handlers can, so we approximate the same check via the role
    membership. ``admin`` is the canonical admin role; ``root`` is the
    bootstrap superuser. Either grants cross-owner access.
    """
    return "admin" in user.roles or "root" in user.roles


async def _authorize_agent(
    request: Request,
    agent_id: str,
    user: UserContext,
) -> tuple[Any, Any]:
    """Look up the agent and enforce ownership.

    Returns ``(agent_service, agent)`` so the caller can act on either
    without a second round-trip. Maps the service's exceptions to the
    HTTP status codes the SPA expects:

    - ``KeyError`` → 404 (agent doesn't exist).
    - ``PermissionError`` → 403 (agent exists but isn't yours).
    """
    agent_svc = _resolve_agent_service(request)
    try:
        agent = await agent_svc._load_agent_for_caller(
            agent_id,
            caller_user_id=user.user_id,
            admin=_is_admin(user),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return agent_svc, agent


def _make_unique_filename(
    raw_name: str,
    content_type: str,
    payload_for_hash: bytes,
) -> str:
    """Build a stable, unique on-disk filename for an avatar upload.

    The browser-supplied name is sanitized for the human-readable
    portion, then a short sha256 prefix of the bytes is appended so
    re-uploading the same file produces the same name (idempotent
    cache key) and different files never collide. The extension is
    forced to match the (already-validated) content type so a user
    who renames ``cat.exe`` to ``cat.png`` still gets a ``.png`` on
    disk for downstream MIME sniffing.
    """
    sanitized = _sanitize_filename(raw_name) if raw_name else "avatar"
    stem = Path(sanitized).stem or "avatar"
    ext = _MIME_TO_EXT.get(content_type, "")
    if not ext:
        # Fall back to whatever extension the sanitized name has; the
        # MIME validator already rejected unknown types so this is a
        # safety net, not a security check.
        ext = Path(sanitized).suffix or ".bin"
    digest = hashlib.sha256(payload_for_hash).hexdigest()[:8]
    return f"{stem}-{digest}{ext}"


@router.post("/{agent_id}/avatar")
async def upload_agent_avatar(
    agent_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Persist an uploaded image as the agent's avatar.

    On success returns the updated agent dict (same shape the WS RPCs
    return) so the SPA can refresh its cached state without a follow-up
    fetch.

    Errors:

    - 400 — missing filename / empty file.
    - 401 — not authenticated.
    - 403 — caller doesn't own the agent.
    - 404 — agent doesn't exist.
    - 413 — payload exceeds ``_MAX_AVATAR_BYTES``.
    - 415 — content type not in the allowed image set.
    - 503 — agent service not running.
    """
    agent_svc, _agent = await _authorize_agent(request, agent_id, user)

    raw_name = file.filename or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail="file has no filename")

    # Cheap MIME-type gate. We trust ``content_type`` here — it's a
    # browser-supplied hint and not a security boundary, but agents
    # are owner-scoped and the bytes never get executed; the cap +
    # extension forcing is more than enough defense in depth.
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported image type: {content_type or 'unknown'}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_MIME_TYPES))}."
            ),
        )

    # Ensure the per-agent directory exists. ``parents=True`` covers
    # the very first avatar upload for any agent on a fresh install
    # (when ``.gilbert/agent-avatars/`` itself may not exist yet).
    target_dir = _avatar_dir(agent_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Stream to a temp path inside the same dir, then rename. We have
    # to read the full payload anyway to compute the sha for the final
    # filename, so accumulate as we go and write once at the end. With
    # the 4 MiB cap the memory pressure is bounded and this avoids a
    # double-write.
    buf = bytearray()
    total = 0
    try:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_AVATAR_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Avatar exceeds the "
                        f"{_MAX_AVATAR_BYTES // (1024 * 1024)} MiB cap."
                    ),
                )
            buf.extend(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("avatar upload read failed for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}") from exc

    if total == 0:
        raise HTTPException(status_code=400, detail="empty file")

    payload = bytes(buf)
    filename = _make_unique_filename(raw_name, content_type, payload)
    dest = target_dir / filename

    try:
        dest.write_bytes(payload)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        logger.exception("avatar write failed for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc

    # Persist via the service so ``agent.updated`` fires for the WS
    # subscribers — the route never hand-rolls an update_agent patch.
    try:
        updated = await agent_svc.set_agent_avatar(agent_id, filename)
    except Exception:
        # Roll back the on-disk file so we don't leave orphans behind.
        dest.unlink(missing_ok=True)
        raise

    logger.info(
        "agent avatar uploaded: agent=%s user=%s name=%r size=%d mime=%s",
        agent_id,
        user.user_id,
        filename,
        total,
        content_type,
    )

    # Re-import locally to avoid a top-level cycle (agent service module
    # imports a lot of interfaces). The function lives in the service
    # module since it's the canonical Agent → dict serializer.
    from gilbert.core.services.agent import _agent_to_dict

    return {"agent": _agent_to_dict(updated)}


@router.get("/{agent_id}/avatar")
async def download_agent_avatar(
    agent_id: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> StreamingResponse:
    """Stream the agent's current avatar image bytes back to the caller.

    404 when the agent has no image avatar set (``avatar_kind != "image"``)
    or when the recorded filename can't be located on disk. Auth and
    ownership are enforced the same way the upload endpoint does.
    """
    _agent_svc, agent = await _authorize_agent(request, agent_id, user)

    if agent.avatar_kind != "image" or not agent.avatar_value:
        raise HTTPException(status_code=404, detail="no image avatar set")

    # ``avatar_value`` is the bare filename — protect against legacy
    # rows that might have stored a path.
    safe_name = Path(agent.avatar_value).name
    full = _avatar_dir(agent_id) / safe_name

    if not full.is_file():
        raise HTTPException(status_code=404, detail="avatar file missing on disk")

    # Belt and suspenders: refuse to serve anything that resolved
    # outside the avatar root (symlink trickery / weird unicode).
    resolved = full.resolve()
    avatar_root = _AVATAR_ROOT.resolve()
    try:
        resolved.relative_to(avatar_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="path escapes avatar root") from exc

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
            "Content-Length": str(resolved.stat().st_size),
            # Avatars rarely change but the value is keyed off the
            # filename hash, so a long cache is fine — when the user
            # uploads a new avatar the filename changes and the SPA
            # will fetch the new URL automatically.
            "Cache-Control": "private, max-age=3600",
        },
    )


