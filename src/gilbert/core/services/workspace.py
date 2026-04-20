"""Workspace service — manages per-conversation file workspaces for AI chats."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import mimetypes
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gilbert.core.file_analysis import analyze_file
from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Filter, FilterOp, IndexDefinition, Query
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
    ToolResult,
)
from gilbert.interfaces.ws import WsHandlerProvider

logger = logging.getLogger(__name__)

_READ_FILE_CAP = 1 * 1024 * 1024  # 1 MiB
_WORKSPACE_FILES_COLLECTION = "workspace_files"
_WORKSPACE_SHARES_COLLECTION = "workspace_file_shares"

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}

# Share defaults (per brian@2026-04-19 product call — "24h TTL / 10 accesses
# is generous enough for media links handed out in chat").
_DEFAULT_SHARE_MAX_ACCESSES = 10
_DEFAULT_SHARE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_MAX_SHARE_MAX_ACCESSES = 1000
_MAX_SHARE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# UPnP consumers (Sonos especially) whitelist canonical audio MIME types
# and trip error 714 "Illegal MIME-Type" on the ``audio/x-*`` experimental
# prefix Python's ``mimetypes`` returns by default. Normalize to the
# widely-accepted forms at both share-create and share-serve time.
_MEDIA_TYPE_ALIASES = {
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac",
    "audio/x-m4a": "audio/mp4",
    "audio/x-ogg": "audio/ogg",
}


def _normalize_media_type(media_type: str) -> str:
    """Fold experimental / vendor-prefix audio MIME types into the
    canonical forms HTTP consumers (Sonos, DLNA devices, browsers'
    native <audio>) actually whitelist."""
    if not media_type:
        return "application/octet-stream"
    mt = media_type.strip().lower()
    return _MEDIA_TYPE_ALIASES.get(mt, media_type)


async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
    return await loop.run_in_executor(None, func, *args)


class WorkspaceService(Service, ToolProvider, WsHandlerProvider):
    """Manages per-conversation file workspaces with purpose-based directories."""

    slash_namespace = "workspace"

    def __init__(self) -> None:
        self._enabled: bool = False
        self._resolver: ServiceResolver | None = None
        self._storage: Any = None
        self._event_bus: Any = None
        # Captured from the ``web`` YAML section so share URLs can route
        # back to this Gilbert instance over the LAN. ``0.0.0.0`` falls
        # back to the auto-detected LAN IP when a URL actually gets
        # built (same pattern as SpeakerService uses for TTS audio).
        self._web_host: str = "0.0.0.0"
        self._web_port: int = 8000
        # Per-path locks for the write tools. The AI loop fan-out pattern
        # is typically "write N *different* files in parallel", which is
        # safe; the rare "two calls to the same path in one batch" case
        # would race on disk write + registry bookkeeping, so we gate
        # each tool by path to make same-path collisions serialize
        # transparently. Key is ``"<conv_id>:<rel_path>"`` so two
        # conversations writing the same rel_path don't contend. Dict
        # grows unbounded with workspace path cardinality; in practice
        # that's bounded by real files on disk, and per-path locks are
        # cheap (~100 bytes each). ``_path_locks_guard`` serializes the
        # dict insert so we don't race on get-or-create.
        self._path_locks: dict[str, asyncio.Lock] = {}
        self._path_locks_guard = asyncio.Lock()

    # ── Service interface ────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="workspace",
            capabilities=frozenset({"workspace", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            # ``configuration`` is read for the web host/port used when
            # building share URLs; ``scheduler`` hosts the hourly cleanup
            # of exhausted/expired share tokens; ``tunnel`` is consulted
            # when a caller asks for a publicly-reachable share URL.
            optional=frozenset({"event_bus", "configuration", "scheduler", "tunnel"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        self._enabled = True

        from gilbert.interfaces.storage import StorageProvider

        storage_svc = resolver.get_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_WORKSPACE_FILES_COLLECTION,
                    fields=["conversation_id"],
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_WORKSPACE_FILES_COLLECTION,
                    fields=["conversation_id", "category"],
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_WORKSPACE_FILES_COLLECTION,
                    fields=["derived_from"],
                )
            )
            # Indexes for share-token lookup + expiry sweeps. ``token``
            # is the hot lookup path (every share URL fetch hits it);
            # ``expires_at`` powers the periodic cleanup query.
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_WORKSPACE_SHARES_COLLECTION,
                    fields=["token"],
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_WORKSPACE_SHARES_COLLECTION,
                    fields=["expires_at"],
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_WORKSPACE_SHARES_COLLECTION,
                    fields=["conversation_id"],
                )
            )

        # Capture web host/port for building share URLs. The share endpoint
        # lives on this same FastAPI app, so the URL is just
        # ``http://<host>:<port>/api/share/<token>``.
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                web_section = config_svc.get_section("web")
                self._web_host = str(web_section.get("host", "0.0.0.0"))
                self._web_port = int(web_section.get("port", 8000))

        # Hourly cleanup of exhausted/expired share tokens. Optional —
        # works without the scheduler (shares just pile up until the
        # 30-day-maximum TTL ages them out behaviourally).
        scheduler = resolver.get_capability("scheduler")
        if scheduler is not None and self._storage is not None:
            from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

            if isinstance(scheduler, SchedulerProvider):
                scheduler.add_job(
                    name="workspace-share-cleanup",
                    schedule=Schedule.every(60 * 60),
                    callback=self._cleanup_file_shares,
                    system=True,
                )

        self._unsubscribe_conv_destroyed: Any = None
        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            from gilbert.interfaces.events import EventBusProvider

            if isinstance(event_bus_svc, EventBusProvider):
                self._event_bus = event_bus_svc.bus
                self._unsubscribe_conv_destroyed = event_bus_svc.bus.subscribe(
                    "chat.conversation.destroyed",
                    self._on_conversation_destroyed,
                )

        logger.info("Workspace service started")

    async def stop(self) -> None:
        if getattr(self, "_unsubscribe_conv_destroyed", None) is not None:
            try:
                self._unsubscribe_conv_destroyed()
            except Exception:
                pass
            self._unsubscribe_conv_destroyed = None

    # ── Directory layout ─────────────────────────────────────────────

    @staticmethod
    def _workspace_top() -> Path:
        return Path(".gilbert/workspaces").resolve()

    @staticmethod
    def _legacy_workspace_top() -> Path:
        return Path(".gilbert/skill-workspaces").resolve()

    def get_workspace_root(self, user_id: str, conversation_id: str) -> Path:
        root = (
            self._workspace_top()
            / "users"
            / user_id
            / "conversations"
            / conversation_id
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    def get_upload_dir(self, user_id: str, conversation_id: str) -> Path:
        d = self.get_workspace_root(user_id, conversation_id) / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_output_dir(self, user_id: str, conversation_id: str) -> Path:
        d = self.get_workspace_root(user_id, conversation_id) / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_scratch_dir(self, user_id: str, conversation_id: str) -> Path:
        d = self.get_workspace_root(user_id, conversation_id) / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # Legacy path resolution for old conversations
    def _legacy_workspace_dir(
        self,
        user_id: str,
        skill_name: str,
    ) -> Path:
        return self._legacy_workspace_top() / user_id / skill_name

    def _legacy_conversation_workspace(
        self,
        user_id: str,
        conversation_id: str,
        skill_name: str,
    ) -> Path:
        return (
            self._legacy_workspace_top()
            / "users"
            / user_id
            / "conversations"
            / conversation_id
            / skill_name
        )

    # ── Conversation cleanup ─────────────────────────────────────────

    async def _on_conversation_destroyed(self, event: Any) -> None:
        data = getattr(event, "data", {}) or {}
        conv_id = str(data.get("conversation_id") or "").strip()
        if not conv_id:
            return
        owner_id = str(data.get("owner_id") or "").strip()

        # Delete file registry entries for this conversation
        if self._storage is not None:
            try:
                docs = await self._storage.query(
                    Query(
                        collection=_WORKSPACE_FILES_COLLECTION,
                        filters=[
                            Filter(
                                field="conversation_id",
                                op=FilterOp.EQ,
                                value=conv_id,
                            )
                        ],
                    )
                )
                for doc in docs:
                    file_id = doc.get("_id", "")
                    if file_id:
                        await self._storage.delete(
                            _WORKSPACE_FILES_COLLECTION, file_id
                        )
            except Exception:
                logger.exception(
                    "Failed to delete workspace_files for conv %s", conv_id
                )

        targets: list[Path] = []

        # New layout
        if owner_id:
            new_root = (
                self._workspace_top()
                / "users"
                / owner_id
                / "conversations"
                / conv_id
            )
            targets.append(new_root)
        else:
            users_root = self._workspace_top() / "users"
            if users_root.is_dir():
                for user_dir in users_root.iterdir():
                    candidate = user_dir / "conversations" / conv_id
                    if candidate.is_dir():
                        targets.append(candidate)

        # Legacy layout
        if owner_id:
            legacy_root = (
                self._legacy_workspace_top()
                / "users"
                / owner_id
                / "conversations"
                / conv_id
            )
            targets.append(legacy_root)
        else:
            legacy_users = self._legacy_workspace_top() / "users"
            if legacy_users.is_dir():
                for user_dir in legacy_users.iterdir():
                    candidate = user_dir / "conversations" / conv_id
                    if candidate.is_dir():
                        targets.append(candidate)

        for target in targets:
            try:
                resolved = target.resolve()
                # Defense in depth: refuse to rm outside workspace roots.
                ws_top = self._workspace_top().resolve()
                legacy_top = self._legacy_workspace_top().resolve()
                if not (
                    str(resolved).startswith(str(ws_top))
                    or str(resolved).startswith(str(legacy_top))
                ):
                    continue
            except (OSError, ValueError):
                continue
            try:
                await _to_thread(shutil.rmtree, resolved, ignore_errors=True)
                logger.info("Removed conversation workspace: %s", resolved)
            except Exception:
                logger.exception(
                    "Failed to remove conversation workspace: %s", resolved
                )

    # ── Share tokens ─────────────────────────────────────────────────

    async def create_file_share(
        self,
        *,
        user_id: str,
        conversation_id: str,
        rel_path: str,
        max_accesses: int = _DEFAULT_SHARE_MAX_ACCESSES,
        ttl_seconds: int = _DEFAULT_SHARE_TTL_SECONDS,
        via_tunnel: bool = False,
    ) -> dict[str, Any]:
        """Mint a temporary URL that serves a workspace file over HTTP.

        External consumers (speakers, SMS/MMS bridges, email attachments,
        anything that needs a URL rather than a file path) fetch the URL;
        the web layer streams bytes and decrements the access counter.
        The token dies when ``max_accesses`` is exhausted or
        ``ttl_seconds`` elapses, whichever comes first.

        Returns ``{"url", "token", "expires_at", "max_accesses",
        "remaining_uses", "via_tunnel", "file_id", "rel_path",
        "media_type", "size"}``.

        Raises ``ValueError`` when inputs are invalid, storage isn't
        available, the file can't be resolved, or ``via_tunnel=True`` is
        requested without a live tunnel.
        """
        import secrets
        import uuid
        from datetime import timedelta

        if self._storage is None:
            raise ValueError("storage is not available")
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not rel_path:
            raise ValueError("rel_path is required")

        max_accesses = max(1, min(int(max_accesses), _MAX_SHARE_MAX_ACCESSES))
        ttl_seconds = max(60, min(int(ttl_seconds), _MAX_SHARE_TTL_SECONDS))

        # Resolve the path through the same safety path the other tools
        # use — catches path traversal + missing-file before we mint.
        target, err = self._resolve_file_path(user_id, rel_path, conversation_id)
        if err is not None:
            raise ValueError(err)
        if target is None or not target.is_file():
            raise ValueError(f"File not found: {rel_path}")

        # Look up the registered file entity (for media_type + file_id).
        # If the file isn't registered, fall back to mimetypes + a
        # synthesized uuid so this still works for freshly-created
        # scratch files that haven't been register_file'd yet.
        file_entity = await self._find_registered_file(conversation_id, rel_path)
        if file_entity:
            file_id = str(file_entity.get("_id") or "")
            media_type = str(
                file_entity.get("media_type")
                or mimetypes.guess_type(target.name)[0]
                or "application/octet-stream"
            )
            size = int(file_entity.get("size") or target.stat().st_size)
        else:
            file_id = ""
            media_type = (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            size = target.stat().st_size

        # Canonicalize the MIME type so downstream UPnP consumers don't
        # reject the stream with error 714 "Illegal MIME-Type".
        media_type = _normalize_media_type(media_type)

        # Resolve the outbound URL. Local (default) builds a LAN URL;
        # tunnel builds a tunnel URL and errors if the tunnel isn't up.
        if via_tunnel:
            tunnel_url = self._tunnel_base_url()
            if not tunnel_url:
                raise ValueError(
                    "via_tunnel=true requires a running tunnel service with a "
                    "public_url — none is available. Start the tunnel plugin "
                    "(e.g. ngrok) or call with via_tunnel=false."
                )
            base = tunnel_url.rstrip("/")
        else:
            base = self._local_base_url()

        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)

        record: dict[str, Any] = {
            "token": token,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "file_id": file_id,
            "rel_path": rel_path,
            "media_type": media_type,
            "size": size,
            "max_accesses": max_accesses,
            "remaining_uses": max_accesses,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "via_tunnel": bool(via_tunnel),
        }
        share_id = str(uuid.uuid4())
        await self._storage.put(_WORKSPACE_SHARES_COLLECTION, share_id, record)

        url = f"{base}/api/share/{token}"
        return {
            "url": url,
            "token": token,
            "expires_at": record["expires_at"],
            "max_accesses": max_accesses,
            "remaining_uses": max_accesses,
            "via_tunnel": bool(via_tunnel),
            "file_id": file_id,
            "rel_path": rel_path,
            "media_type": media_type,
            "size": size,
        }

    async def consume_file_share(
        self,
        token: str,
    ) -> tuple[Path, str, str] | None:
        """Validate + consume a share token.

        Returns ``(resolved_path, media_type, filename)`` on a successful
        hit and decrements ``remaining_uses`` in storage. Returns
        ``None`` when the token is unknown, expired, or exhausted — the
        caller should 404 without differentiating (don't leak "exists
        but exhausted" vs "never existed").
        """
        if self._storage is None or not token:
            return None

        from gilbert.interfaces.storage import Filter, FilterOp, Query

        docs = await self._storage.query(
            Query(
                collection=_WORKSPACE_SHARES_COLLECTION,
                filters=[Filter(field="token", op=FilterOp.EQ, value=token)],
                limit=1,
            )
        )
        if not docs:
            return None
        record = docs[0]
        share_id = str(record.get("_id") or "")
        if not share_id:
            return None

        # Expiry check — compare ISO strings after parsing.
        expires_raw = str(record.get("expires_at") or "")
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            expires_at = None
        if expires_at is not None and expires_at <= datetime.now(UTC):
            # Fire-and-forget cleanup — caller still 404s.
            await self._storage.delete(_WORKSPACE_SHARES_COLLECTION, share_id)
            return None

        remaining = int(record.get("remaining_uses") or 0)
        if remaining <= 0:
            await self._storage.delete(_WORKSPACE_SHARES_COLLECTION, share_id)
            return None

        user_id = str(record.get("user_id") or "")
        conv_id = str(record.get("conversation_id") or "")
        rel_path = str(record.get("rel_path") or "")
        # Re-normalize on the way out too — records written before the
        # normalizer existed would otherwise keep serving ``audio/x-wav``
        # until they expire.
        media_type = _normalize_media_type(
            str(record.get("media_type") or "application/octet-stream")
        )

        target, err = self._resolve_file_path(user_id, rel_path, conv_id)
        if err is not None or target is None or not target.is_file():
            await self._storage.delete(_WORKSPACE_SHARES_COLLECTION, share_id)
            return None

        # Atomically decrement. SQLite JSON storage is serialised per-
        # connection so racing is unlikely, but we still read-modify-write
        # through a single put so the "remaining reaches zero, delete"
        # branch stays simple.
        record["remaining_uses"] = remaining - 1
        if record["remaining_uses"] <= 0:
            await self._storage.delete(_WORKSPACE_SHARES_COLLECTION, share_id)
        else:
            await self._storage.put(
                _WORKSPACE_SHARES_COLLECTION, share_id, record
            )

        return target, media_type, Path(rel_path).name

    async def _cleanup_file_shares(self) -> None:
        """Delete share records that have expired or run out of uses.

        Runs hourly; the same check also happens lazily at consume time
        so the cleanup is a floor on storage growth, not a safety rail."""
        if self._storage is None:
            return

        from gilbert.interfaces.storage import Query

        now_iso = datetime.now(UTC).isoformat()
        try:
            docs = await self._storage.query(
                Query(collection=_WORKSPACE_SHARES_COLLECTION)
            )
        except Exception:
            logger.exception("Failed to list workspace file shares for cleanup")
            return

        deleted = 0
        for doc in docs:
            share_id = str(doc.get("_id") or "")
            if not share_id:
                continue
            expires = str(doc.get("expires_at") or "")
            remaining = int(doc.get("remaining_uses") or 0)
            if remaining <= 0 or (expires and expires <= now_iso):
                try:
                    await self._storage.delete(
                        _WORKSPACE_SHARES_COLLECTION, share_id
                    )
                    deleted += 1
                except Exception:
                    logger.exception(
                        "Failed to delete expired share %s", share_id
                    )
        if deleted:
            logger.info(
                "Cleaned up %d expired/exhausted workspace file share(s)", deleted
            )

    async def _find_registered_file(
        self,
        conversation_id: str,
        rel_path: str,
    ) -> dict[str, Any] | None:
        """Best-effort lookup of the workspace_files entity for a rel_path."""
        if self._storage is None:
            return None
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        try:
            docs = await self._storage.query(
                Query(
                    collection=_WORKSPACE_FILES_COLLECTION,
                    filters=[
                        Filter(
                            field="conversation_id",
                            op=FilterOp.EQ,
                            value=conversation_id,
                        ),
                        Filter(field="rel_path", op=FilterOp.EQ, value=rel_path),
                    ],
                    limit=1,
                )
            )
        except Exception:
            return None
        return docs[0] if docs else None

    def _local_base_url(self) -> str:
        """Build the http://host:port prefix for local share URLs.

        Mirrors SpeakerService's audio-URL logic — ``0.0.0.0`` / loopback
        binds get replaced with the machine's LAN IP so external devices
        (speakers, phones on the same Wi-Fi) can actually reach the
        server."""
        host = self._web_host
        if host in ("0.0.0.0", "127.0.0.1", "localhost"):
            host = self._get_lan_ip()
        return f"http://{host}:{self._web_port}"

    def _tunnel_base_url(self) -> str:
        """Return the tunnel's public URL, or ``""`` if no tunnel is live."""
        if self._resolver is None:
            return ""
        tunnel_svc = self._resolver.get_capability("tunnel")
        if tunnel_svc is None:
            return ""
        from gilbert.interfaces.tunnel import TunnelProvider

        if not isinstance(tunnel_svc, TunnelProvider):
            return ""
        return tunnel_svc.public_url or ""

    @staticmethod
    def _get_lan_ip() -> str:
        """Discover the machine's LAN-facing IP.

        Connects a UDP socket to 8.8.8.8 to force the OS to pick an
        outbound interface; no packets are actually sent. Same trick
        SpeakerService uses for TTS audio URLs."""
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return str(s.getsockname()[0])
        except OSError:
            return "127.0.0.1"

    # ── File Registry ────────────────────────────────────────────────

    async def register_file(
        self,
        *,
        conversation_id: str,
        user_id: str,
        category: str,
        filename: str,
        rel_path: str,
        media_type: str,
        size: int,
        created_by: str = "ai",
        original_name: str = "",
        skill_name: str = "",
        description: str = "",
        derived_from: str | None = None,
        derivation_method: str | None = None,
        derivation_script: str | None = None,
        derivation_notes: str | None = None,
        reusable: bool = False,
    ) -> dict[str, Any]:
        """Register a file in the workspace_files entity collection.

        Runs file metadata analysis and stores the result. Returns the
        created entity dict (including ``_id``).
        """
        if self._storage is None:
            return {}

        import uuid

        file_id = str(uuid.uuid4())

        # Run metadata analysis
        workspace_root = self.get_workspace_root(user_id, conversation_id)
        file_path = workspace_root / rel_path
        metadata: dict[str, Any] = {}
        if file_path.is_file():
            metadata = await _to_thread(analyze_file, file_path, media_type)

        entity: dict[str, Any] = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "category": category,
            "filename": filename,
            "original_name": original_name or filename,
            "rel_path": rel_path,
            "media_type": media_type,
            "size": size,
            "skill_name": skill_name,
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": created_by,
            "description": description,
            "pinned": False,
            "derived_from": derived_from,
            "derivation_method": derivation_method,
            "derivation_script": derivation_script,
            "derivation_notes": derivation_notes,
            "reusable": reusable,
            "metadata": metadata,
        }

        await self._storage.put(_WORKSPACE_FILES_COLLECTION, file_id, entity)
        entity["_id"] = file_id

        await self._emit_file_event(
            "workspace.file.created", entity, user_id
        )

        return entity

    async def _emit_file_event(
        self, event_type: str, entity: dict[str, Any], user_id: str
    ) -> None:
        if self._event_bus is None:
            return
        from gilbert.interfaces.events import Event

        await self._event_bus.publish(
            Event(
                event_type=event_type,
                data={
                    "file": entity,
                    "conversation_id": entity.get("conversation_id", ""),
                    "visible_to": [user_id],
                },
                source="workspace",
            )
        )

    async def list_files(
        self, conversation_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """List registered files for a conversation, optionally filtered by category."""
        if self._storage is None:
            return []

        filters = [
            Filter(
                field="conversation_id",
                op=FilterOp.EQ,
                value=conversation_id,
            )
        ]
        if category:
            filters.append(
                Filter(field="category", op=FilterOp.EQ, value=category)
            )

        docs = await self._storage.query(
            Query(collection=_WORKSPACE_FILES_COLLECTION, filters=filters)
        )
        return list(docs)

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        """Get a single file entity by ID."""
        if self._storage is None:
            return None
        return await self._storage.get(_WORKSPACE_FILES_COLLECTION, file_id)

    async def update_file(
        self, file_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update fields on a file entity."""
        if self._storage is None:
            return None
        existing = await self._storage.get(_WORKSPACE_FILES_COLLECTION, file_id)
        if existing is None:
            return None
        existing.update(updates)
        await self._storage.put(_WORKSPACE_FILES_COLLECTION, file_id, existing)
        return existing

    async def delete_file(self, file_id: str) -> bool:
        """Delete a file entity and its on-disk file."""
        if self._storage is None:
            return False
        entity = await self._storage.get(_WORKSPACE_FILES_COLLECTION, file_id)
        if entity is None:
            return False

        # Delete from disk
        conv_id = entity.get("conversation_id", "")
        user_id = entity.get("user_id", "")
        rel_path = entity.get("rel_path", "")
        if conv_id and user_id and rel_path:
            workspace_root = self.get_workspace_root(user_id, conv_id)
            target = (workspace_root / rel_path).resolve()
            try:
                target.relative_to(workspace_root.resolve())
                if target.is_file():
                    target.unlink()
            except (ValueError, OSError):
                pass

        await self._storage.delete(_WORKSPACE_FILES_COLLECTION, file_id)

        entity["_id"] = file_id
        await self._emit_file_event(
            "workspace.file.deleted", entity, user_id
        )

        return True

    async def find_file_by_path(
        self, conversation_id: str, rel_path: str
    ) -> dict[str, Any] | None:
        """Find a registered file by its relative path within a conversation."""
        if self._storage is None:
            return None
        docs = await self._storage.query(
            Query(
                collection=_WORKSPACE_FILES_COLLECTION,
                filters=[
                    Filter(
                        field="conversation_id",
                        op=FilterOp.EQ,
                        value=conversation_id,
                    ),
                    Filter(
                        field="rel_path",
                        op=FilterOp.EQ,
                        value=rel_path,
                    ),
                ],
            )
        )
        return docs[0] if docs else None

    async def build_workspace_manifest(self, conversation_id: str) -> str:
        """Build a system prompt fragment listing the conversation's files."""
        from gilbert.core.file_analysis import format_metadata_summary

        files = await self.list_files(conversation_id)
        if not files:
            return ""

        uploads = [f for f in files if f.get("category") == "upload"]
        outputs = [f for f in files if f.get("category") == "output"]
        scratch = [f for f in files if f.get("category") == "scratch"]

        # Build an ID map for lineage references
        id_map: dict[str, str] = {}
        for i, f in enumerate(uploads):
            id_map[f.get("_id", "")] = f"U{i + 1}"
        for i, f in enumerate(outputs):
            id_map[f.get("_id", "")] = f"O{i + 1}"
        for i, f in enumerate(scratch):
            id_map[f.get("_id", "")] = f"S{i + 1}"

        parts: list[str] = ["## Workspace Files"]

        def _format_size(size: int) -> str:
            if size >= 1_000_000:
                return f"{size / 1_000_000:.1f} MB"
            if size >= 1_000:
                return f"{size / 1_000:.1f} KB"
            return f"{size} B"

        def _format_file(f: dict[str, Any], short_id: str) -> str:
            name = f.get("filename", "")
            mt = f.get("media_type", "")
            size = f.get("size", 0)
            meta = f.get("metadata", {}) or {}
            desc = f.get("description", "")
            pinned = f.get("pinned", False)
            reusable = f.get("reusable", False)
            derived_from = f.get("derived_from")
            derivation_script = f.get("derivation_script")
            derivation_notes = f.get("derivation_notes")

            line = f"- [{short_id}] {name} ({mt}, {_format_size(size)})"

            meta_summary = format_metadata_summary(meta, mt)
            if meta_summary:
                line += f" — {meta_summary}"

            flags: list[str] = []
            if pinned:
                flags.append("pinned")
            if reusable:
                flags.append("reusable")
            if flags:
                line += " [" + ", ".join(flags) + "]"

            if desc:
                line += f'\n  "{desc}"'

            if derived_from and derived_from in id_map:
                parent_id = id_map[derived_from]
                lineage = f"  ← derived from [{parent_id}]"
                if derivation_script:
                    lineage += f" via {derivation_script}"
                line += f"\n{lineage}"

            if derivation_notes and not desc:
                line += f'\n  "{derivation_notes}"'

            return line

        if uploads:
            parts.append("\n### Uploads")
            for i, f in enumerate(uploads):
                parts.append(_format_file(f, f"U{i + 1}"))

        if outputs:
            parts.append("\n### Outputs")
            for i, f in enumerate(outputs):
                parts.append(_format_file(f, f"O{i + 1}"))

        if scratch:
            parts.append("\n### Working Files")
            for i, f in enumerate(scratch):
                parts.append(_format_file(f, f"S{i + 1}"))

        # One-line proactive hint: external consumers (speakers, SMS,
        # email attachments) can't read workspace paths. Without this
        # nudge, the AI tries to pass ``uploads/foo.mp3`` to ``play_audio``
        # and fails. Kept terse so it survives context-window pressure.
        parts.append(
            "\n_To hand any of these to an HTTP-only consumer (speakers, "
            "SMS/MMS, email attachments, webhooks), first call "
            "``share_workspace_file`` to mint a URL — don't pass raw "
            "workspace paths as URIs._"
        )

        return "\n".join(parts)

    # ── ToolProvider interface ───────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "workspace"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="browse_workspace",
                slash_command="browse",
                slash_help="List files in the conversation workspace: /workspace browse",
                description=(
                    "List all files in the current conversation's workspace, "
                    "organised by category (uploads, outputs, scratch). Use this "
                    "to see what files are available — user uploads, AI-generated "
                    "outputs, and working files."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="read_workspace_file",
                slash_command="read",
                slash_help="Read a workspace file: /workspace read <path>",
                description="Read a text file from the conversation workspace.",
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Relative path within the workspace "
                            "(e.g. 'uploads/data.csv' or 'scratch/analyze.py')."
                        ),
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="write_workspace_file",
                slash_command="write",
                slash_help=(
                    "Write a text file to the workspace: "
                    "/workspace write <path> <content>"
                ),
                description=(
                    "Write a small text file to the conversation workspace. "
                    "Files are written to scratch/ by default. Use "
                    "category='output' to write directly to outputs/.\n\n"
                    "**Best for:** small scripts (<100 lines), config files, "
                    "templates. **NOT for:** large data files, CSVs with many "
                    "rows, or generated reports — use run_workspace_script "
                    "to generate those (the script writes them to disk "
                    "directly, avoiding token limits)."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Simple filename within the category directory "
                            "(e.g. 'analyze.py', 'config.json'). "
                            "Keep paths flat — avoid nested subdirectories."
                        ),
                    ),
                    ToolParameter(
                        name="content",
                        type=ToolParameterType.STRING,
                        description="UTF-8 text content of the file.",
                    ),
                    ToolParameter(
                        name="category",
                        type=ToolParameterType.STRING,
                        description=(
                            "Target category: 'scratch' (default) for working "
                            "files, 'output' for user deliverables."
                        ),
                        required=False,
                        enum=["scratch", "output"],
                    ),
                ],
                required_role="user",
                # Per-path locks inside the handler serialize same-path
                # concurrent writes; different paths fan out.
                parallel_safe=True,
            ),
            ToolDefinition(
                name="run_workspace_script",
                slash_command="run",
                slash_help=(
                    "Run a script from the workspace: "
                    "/workspace run <path> [args...]"
                ),
                description=(
                    "Execute a script from the conversation workspace. "
                    "Python (``.py``) runs via the workspace's own virtual "
                    "environment (auto-created on first run that needs "
                    "packages), shell (``.sh``) via ``bash``, Node "
                    "(``.ts``/``.js``) via ``node``. Scripts run with the "
                    "workspace root as their working directory, so they "
                    "can access uploaded files at ``uploads/<filename>`` "
                    "and write output files. Use ``packages`` to declare "
                    "Python libraries the script needs — they're installed "
                    "into the workspace venv via ``uv pip`` and cached "
                    "across runs. Script timeout is 120 seconds.\n\n"
                    "THIS IS THE PRIMARY TOOL FOR ANALYZING USER-UPLOADED "
                    "FILES. When the user attaches a file, it lands in "
                    "``uploads/``. Write a Python script to ``scratch/``, "
                    "then run it here. The script can open uploaded files "
                    "via ``'uploads/<filename>'`` relative paths. Request "
                    "parsers via ``packages`` (e.g. ``['pandas']`` for "
                    "CSVs, ``['PyPDF2']`` for PDFs)."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Relative path to the script within the workspace "
                            "(e.g. 'scratch/analyze.py')."
                        ),
                    ),
                    ToolParameter(
                        name="arguments",
                        type=ToolParameterType.ARRAY,
                        description="Command-line arguments to pass to the script.",
                        required=False,
                    ),
                    ToolParameter(
                        name="packages",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "Python packages the script needs (only "
                            "meaningful for .py scripts). When provided, "
                            "the workspace gets a virtual environment at "
                            "``scratch/.venv/`` (via ``uv venv``) and the "
                            "packages are installed via ``uv pip install`` "
                            "before the script runs. The venv is cached "
                            "across runs. Example: ['pandas', 'numpy']."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="attach_workspace_file",
                slash_command="attach",
                slash_help=(
                    "Attach a workspace file to your reply: "
                    "/workspace attach <path> [display_name]"
                ),
                description=(
                    "Attach a file from the workspace to your reply so the "
                    "user sees a downloadable chip. Use this after a script "
                    "has produced a file (PDF, image, spreadsheet, etc.). "
                    "The file is copied to outputs/ and a reference "
                    "attachment is created — the frontend fetches bytes on "
                    "click."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Relative path to the file within the workspace "
                            "(e.g. 'scratch/report.pdf' or 'outputs/chart.png')."
                        ),
                    ),
                    ToolParameter(
                        name="display_name",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional user-visible filename. Defaults to "
                            "the basename of ``path``."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
                # Per-path locks serialize same-source concurrent
                # attaches (preventing the scratch→outputs copy + unique-
                # filename dance from racing). Different source paths
                # fan out.
                parallel_safe=True,
            ),
            ToolDefinition(
                name="annotate_workspace_file",
                slash_command="annotate",
                slash_help=(
                    "Annotate a workspace file: "
                    "/workspace annotate <path> [description=...] [reusable=...]"
                ),
                description=(
                    "Set metadata on a workspace file: description, "
                    "reusable flag, derivation notes, and lineage. "
                    "Call this after generating a file to help future "
                    "turns understand what it contains and how it was "
                    "produced."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Relative path to the file in the workspace.",
                    ),
                    ToolParameter(
                        name="description",
                        type=ToolParameterType.STRING,
                        description="What this file contains or is for.",
                        required=False,
                    ),
                    ToolParameter(
                        name="reusable",
                        type=ToolParameterType.BOOLEAN,
                        description="Mark as reusable for future analysis.",
                        required=False,
                    ),
                    ToolParameter(
                        name="derivation_notes",
                        type=ToolParameterType.STRING,
                        description="How the file was derived.",
                        required=False,
                    ),
                    ToolParameter(
                        name="derived_from",
                        type=ToolParameterType.STRING,
                        description="Path of the parent file this was derived from.",
                        required=False,
                    ),
                ],
                required_role="user",
                # Per-path locks serialize concurrent metadata updates
                # on the same file; different files fan out.
                parallel_safe=True,
            ),
            ToolDefinition(
                name="delete_workspace_file",
                slash_command="delete",
                slash_help="Delete a workspace file: /workspace delete <path>",
                description=(
                    "Delete a file from the conversation workspace. "
                    "Removes both the file from disk and its registry entry."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Relative path to the file (e.g. 'scratch/temp.py').",
                    ),
                ],
                required_role="user",
                # Per-path locks serialize concurrent deletes/writes on
                # the same file; different files fan out.
                parallel_safe=True,
            ),
            ToolDefinition(
                name="share_workspace_file",
                slash_command="share",
                slash_help=(
                    "Mint a temporary HTTP URL for a workspace file: "
                    "/workspace share <path> [max_accesses] [ttl_seconds]"
                ),
                description=(
                    "Create a temporary HTTP URL that serves a workspace "
                    "file to external consumers (speakers, SMS/MMS, email "
                    "attachments, anything that needs a URL rather than a "
                    "local path). The URL works until ``max_accesses`` is "
                    "exhausted or ``ttl_seconds`` elapses, whichever comes "
                    "first. Returns JSON with the URL and its metadata — "
                    "pass the ``url`` field along to the service that "
                    "needs it. USE THIS before asking the speaker service "
                    "(or any HTTP-only consumer) to play a workspace file "
                    "— they can't read local paths."
                ),
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description=(
                            "Workspace-relative path to share "
                            "(e.g. 'uploads/song.mp3', 'outputs/greeting.wav')."
                        ),
                    ),
                    ToolParameter(
                        name="max_accesses",
                        type=ToolParameterType.INTEGER,
                        description=(
                            "Number of times the URL can be fetched before "
                            "the token dies. Default 10."
                        ),
                        required=False,
                        default=_DEFAULT_SHARE_MAX_ACCESSES,
                    ),
                    ToolParameter(
                        name="ttl_seconds",
                        type=ToolParameterType.INTEGER,
                        description=(
                            "Seconds until the URL expires even if it hasn't "
                            "been fully consumed. Default 86400 (24h)."
                        ),
                        required=False,
                        default=_DEFAULT_SHARE_TTL_SECONDS,
                    ),
                    ToolParameter(
                        name="via_tunnel",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "If true, build the URL against the public "
                            "tunnel (ngrok) so consumers outside the LAN "
                            "can reach it. Requires the tunnel service "
                            "to be running. Default false — the URL uses "
                            "the LAN address and only works for clients "
                            "on the same network as the Gilbert host."
                        ),
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        match name:
            case "browse_workspace":
                return await self._tool_browse_workspace(arguments)
            case "read_workspace_file":
                return await self._tool_read_workspace_file(arguments)
            case "write_workspace_file":
                return await self._tool_write_workspace_file(arguments)
            case "run_workspace_script":
                return await self._tool_run_workspace_script(arguments)
            case "attach_workspace_file":
                return await self._tool_attach_workspace_file(arguments)
            case "annotate_workspace_file":
                return await self._tool_annotate_workspace_file(arguments)
            case "delete_workspace_file":
                return await self._tool_delete_workspace_file(arguments)
            case "share_workspace_file":
                return await self._tool_share_workspace_file(arguments)
            # Legacy tool names — aliases for backward compat
            case "browse_skill_workspace":
                return await self._tool_browse_workspace(arguments)
            case "read_skill_workspace_file":
                return await self._tool_read_workspace_file(
                    self._migrate_legacy_args(arguments)
                )
            case "write_skill_workspace_file":
                return await self._tool_write_workspace_file(
                    self._migrate_legacy_args(arguments)
                )
            case _:
                raise KeyError(f"Unknown tool: {name}")

    # ── WsHandlerProvider interface ──────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "workspace.browse": self._ws_workspace_browse,
            "workspace.download": self._ws_workspace_download,
            "workspace.files.list": self._ws_files_list,
            "workspace.files.pin": self._ws_files_pin,
            "workspace.files.delete": self._ws_files_delete,
            # Legacy handler names for backward compat
            "skills.workspace.browse": self._ws_workspace_browse,
            "skills.workspace.download": self._ws_workspace_download,
        }

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _conv_id_from_args(arguments: dict[str, Any]) -> str | None:
        conv_id = arguments.get("_conversation_id")
        if isinstance(conv_id, str) and conv_id:
            return conv_id
        return None

    async def _get_path_lock(self, conv_id: str, rel_path: str) -> asyncio.Lock:
        """Return the lock guarding mutations on ``rel_path`` within
        conversation ``conv_id``. Lazily created on first use and cached
        for the service lifetime. Two concurrent tool calls against the
        same ``(conv_id, rel_path)`` acquire the same lock and serialize;
        different paths get different locks and fan out freely.
        """
        key = f"{conv_id}:{rel_path}"
        async with self._path_locks_guard:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._path_locks[key] = lock
            return lock

    @staticmethod
    def _migrate_legacy_args(arguments: dict[str, Any]) -> dict[str, Any]:
        """Translate old skill_name-based arguments to the new layout.

        Old tools had ``skill_name`` + ``path`` where ``path`` was
        relative to ``<workspace>/<skill_name>/``. New tools just have
        ``path`` relative to the workspace root with category prefixes.
        For legacy calls, we map:

        - skill_name='chat-uploads' + path='file.csv' → path='uploads/file.csv'
        - skill_name=<other> + path='script.py' → path='scratch/script.py'
        """
        result = dict(arguments)
        skill_name = str(result.pop("skill_name", "")).strip()
        rel_path = str(result.get("path", "")).strip()

        if skill_name == "chat-uploads":
            result["path"] = f"uploads/{rel_path}"
        elif rel_path and not (
            rel_path.startswith("uploads/")
            or rel_path.startswith("outputs/")
            or rel_path.startswith("scratch/")
        ):
            result["path"] = f"scratch/{rel_path}"
        return result

    def _resolve_workspace_root(
        self,
        user_id: str,
        conversation_id: str | None,
    ) -> Path:
        """Get the workspace root, creating it if needed.

        Without a conversation_id, there's no workspace root — return a
        temporary path that shouldn't be used for writes.
        """
        if conversation_id:
            return self.get_workspace_root(user_id, conversation_id)
        return self._legacy_workspace_top() / user_id

    def _resolve_file_path(
        self,
        user_id: str,
        rel_path: str,
        conversation_id: str | None,
    ) -> tuple[Path | None, str | None]:
        """Resolve a workspace-relative path, trying new layout then legacy.

        Returns ``(resolved_path, error_message)``.
        """
        candidates: list[Path] = []

        if conversation_id:
            new_root = self.get_workspace_root(user_id, conversation_id)
            candidates.append(new_root)

            # Legacy: try the old skill-based paths
            # If path starts with uploads/, check chat-uploads skill dir
            if rel_path.startswith("uploads/"):
                bare = rel_path[len("uploads/"):]
                candidates.append(
                    self._legacy_conversation_workspace(
                        user_id, conversation_id, "chat-uploads"
                    )
                )
                # For legacy, the file is at the bare name, not under uploads/
                for ws in candidates[1:]:
                    target = (ws / bare).resolve()
                    try:
                        target.relative_to(ws.resolve())
                    except ValueError:
                        return None, "Path traversal not allowed"
                    if target.is_file():
                        return target, None

            # Legacy: try all skill dirs under old conversation workspace
            legacy_conv_root = (
                self._legacy_workspace_top()
                / "users"
                / user_id
                / "conversations"
                / conversation_id
            )
            if legacy_conv_root.is_dir():
                for skill_dir in legacy_conv_root.iterdir():
                    if skill_dir.is_dir():
                        candidates.append(skill_dir)

        # Also try legacy per-user workspaces
        legacy_user_root = self._legacy_workspace_top() / user_id
        if legacy_user_root.is_dir():
            for skill_dir in legacy_user_root.iterdir():
                if skill_dir.is_dir() and skill_dir.name != "conversations":
                    candidates.append(skill_dir)

        # Check new workspace root first
        for workspace in candidates:
            if not workspace.is_dir():
                continue
            # For the new-layout root, use the path as-is
            target = (workspace / rel_path).resolve()
            try:
                target.relative_to(workspace.resolve())
            except ValueError:
                return None, "Path traversal not allowed"
            if target.is_file():
                return target, None

            # For legacy skill dirs, try the bare filename (strip category prefix)
            if workspace != candidates[0] if candidates else None:
                for prefix in ("uploads/", "outputs/", "scratch/"):
                    if rel_path.startswith(prefix):
                        bare = rel_path[len(prefix):]
                        bare_target = (workspace / bare).resolve()
                        try:
                            bare_target.relative_to(workspace.resolve())
                        except ValueError:
                            continue
                        if bare_target.is_file():
                            return bare_target, None

        return None, f"File not found: {rel_path}"

    @staticmethod
    def _list_files(directory: Path) -> list[dict[str, Any]]:
        """List files in a directory recursively. Blocking — run in executor."""
        files: list[dict[str, Any]] = []
        if not directory.is_dir():
            return files
        for f in sorted(directory.rglob("*")):
            if f.is_file() and not any(
                p.name in _SKIP_DIRS for p in f.relative_to(directory).parents
            ):
                stat = f.stat()
                files.append(
                    {
                        "path": str(f.relative_to(directory)),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(
                            stat.st_mtime,
                            tz=UTC,
                        ).isoformat(),
                    }
                )
        return files

    # ── Tool implementations ─────────────────────────────────────────

    async def _tool_browse_workspace(self, arguments: dict[str, Any]) -> str:
        user_id = arguments.get("_user_id", "system")
        conv_id = self._conv_id_from_args(arguments)

        if not conv_id:
            return json.dumps({"error": "No conversation context"})

        root = self.get_workspace_root(user_id, conv_id)

        uploads = await _to_thread(self._list_files, root / "uploads")
        outputs = await _to_thread(self._list_files, root / "outputs")
        scratch = await _to_thread(self._list_files, root / "scratch")

        # Check legacy workspace for fallback
        legacy_files: list[dict[str, Any]] = []
        if not uploads and not outputs and not scratch:
            legacy_conv = (
                self._legacy_workspace_top()
                / "users"
                / user_id
                / "conversations"
                / conv_id
            )
            if legacy_conv.is_dir():
                for skill_dir in legacy_conv.iterdir():
                    if skill_dir.is_dir():
                        legacy_files.extend(
                            await _to_thread(self._list_files, skill_dir)
                        )

        return json.dumps(
            {
                "workspace": str(root),
                "uploads": uploads,
                "outputs": outputs,
                "scratch": scratch,
                "legacy_files": legacy_files,
            }
        )

    async def _tool_read_workspace_file(self, arguments: dict[str, Any]) -> str:
        rel_path = str(arguments.get("path", "")).strip()
        user_id = arguments.get("_user_id", "system")

        if not rel_path:
            return json.dumps({"error": "path is required"})

        conv_id = self._conv_id_from_args(arguments)
        target, err = self._resolve_file_path(user_id, rel_path, conv_id)
        if err is not None:
            return json.dumps({"error": err})
        assert target is not None

        try:
            size = target.stat().st_size
        except OSError as exc:
            return json.dumps({"error": f"Cannot stat file: {exc}"})

        if size > _READ_FILE_CAP:
            return json.dumps(
                {
                    "error": (
                        f"File is too large to read directly ({size} bytes "
                        f"> {_READ_FILE_CAP} byte cap). Use "
                        "run_workspace_script to write and execute a Python "
                        "script that extracts what you need — the script "
                        "runs with the workspace as its current directory."
                    ),
                    "size": size,
                    "path": rel_path,
                }
            )

        try:
            content = str(await _to_thread(target.read_text, "utf-8"))
            if len(content) > 50_000:
                content = content[:50_000] + "\n\n[... truncated at 50,000 characters]"
            return content
        except (OSError, UnicodeDecodeError) as exc:
            return json.dumps({"error": f"Cannot read file: {exc}"})

    async def _tool_write_workspace_file(
        self,
        arguments: dict[str, Any],
    ) -> str:
        rel_path = str(arguments.get("path", "")).strip()
        content = arguments.get("content", "")
        category = str(arguments.get("category", "scratch")).strip()
        user_id = arguments.get("_user_id", "system")
        conv_id = self._conv_id_from_args(arguments)

        if not rel_path:
            return json.dumps({"error": "path is required"})
        if not isinstance(content, str):
            return json.dumps({"error": "content must be a string"})
        if not conv_id:
            return json.dumps({"error": "No conversation context"})

        max_bytes = 512 * 1024
        byte_len = len(content.encode("utf-8"))
        if byte_len > max_bytes:
            return json.dumps(
                {"error": f"content too large ({byte_len} bytes > {max_bytes} max)"}
            )

        if category == "output":
            target_dir = self.get_output_dir(user_id, conv_id)
        else:
            target_dir = self.get_scratch_dir(user_id, conv_id)

        # If path already includes the category prefix, strip it
        for prefix in ("scratch/", "outputs/", "uploads/"):
            if rel_path.startswith(prefix):
                rel_path = rel_path[len(prefix):]
                break

        target = (target_dir / rel_path).resolve()

        try:
            target.relative_to(target_dir.resolve())
        except ValueError:
            return json.dumps({"error": "Path traversal not allowed"})

        root = self.get_workspace_root(user_id, conv_id)
        try:
            stored = target.relative_to(root.resolve()).as_posix()
        except ValueError:
            stored = rel_path

        # Serialize any other tool call targeting the same file in this
        # conversation so concurrent writes/annotate/delete on one path
        # don't race on disk or registry bookkeeping. Different paths
        # acquire different locks and fan out freely.
        path_lock = await self._get_path_lock(conv_id, stored)
        async with path_lock:
            try:
                await _to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await _to_thread(target.write_text, content, encoding="utf-8")
            except OSError as exc:
                return json.dumps({"error": f"Cannot write file: {exc}"})

            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            await self.register_file(
                conversation_id=conv_id,
                user_id=user_id,
                category=category,
                filename=target.name,
                rel_path=stored,
                media_type=media_type,
                size=byte_len,
                created_by="ai",
            )

        return json.dumps(
            {
                "status": "written",
                "path": stored,
                "category": category,
                "bytes": byte_len,
            }
        )

    async def _tool_run_workspace_script(
        self,
        arguments: dict[str, Any],
    ) -> str:
        rel_path = str(arguments.get("path", "")).strip()
        script_args = arguments.get("arguments", []) or []
        raw_packages = arguments.get("packages") or []
        user_id = arguments.get("_user_id", "system")
        conv_id = self._conv_id_from_args(arguments)

        # Legacy support: if skill_name is present, migrate args
        if "skill_name" in arguments and arguments["skill_name"]:
            arguments = self._migrate_legacy_args(arguments)
            rel_path = str(arguments.get("path", "")).strip()

        if not rel_path:
            return json.dumps({"error": "path is required"})
        if not conv_id:
            return json.dumps({"error": "No conversation context"})

        packages: list[str]
        if isinstance(raw_packages, str):
            packages = [p.strip() for p in re.split(r"[,\s]+", raw_packages) if p.strip()]
        elif isinstance(raw_packages, list):
            packages = [str(p).strip() for p in raw_packages if str(p).strip()]
        else:
            return json.dumps({"error": "packages must be a list of strings"})

        workspace = self.get_workspace_root(user_id, conv_id)

        # Snapshot existing files before script runs so we can detect new ones
        existing_files = set()
        for d in (workspace / "scratch", workspace / "uploads", workspace / "outputs"):
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file() and ".venv" not in f.parts:
                        existing_files.add(str(f.resolve()))

        result = str(
            await _to_thread(
                self._do_run_workspace_script,
                workspace,
                rel_path,
                script_args,
                packages,
            )
        )

        # Auto-register new files created by the script
        for d_name in ("scratch", "uploads", "outputs"):
            d = workspace / d_name
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if (
                    f.is_file()
                    and ".venv" not in f.parts
                    and str(f.resolve()) not in existing_files
                ):
                    f_rel = f.relative_to(workspace.resolve()).as_posix()
                    mt = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                    await self.register_file(
                        conversation_id=conv_id,
                        user_id=user_id,
                        category=d_name if d_name != "outputs" else "output",
                        filename=f.name,
                        rel_path=f_rel,
                        media_type=mt,
                        size=f.stat().st_size,
                        created_by="ai",
                        derivation_script=rel_path,
                        derivation_method="script",
                    )

        return result

    @staticmethod
    def _ensure_workspace_venv(scratch_dir: Path) -> tuple[Path, str]:
        """Create (or reuse) a venv inside the scratch directory."""
        venv_dir = scratch_dir / ".venv"
        python_bin = venv_dir / "bin" / "python"
        if python_bin.is_file():
            return python_bin, str(venv_dir)

        scratch_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["uv", "venv", str(venv_dir)],
            cwd=str(scratch_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        if not python_bin.is_file():
            raise RuntimeError(f"uv venv ran but {python_bin} wasn't created")
        return python_bin, str(venv_dir)

    def _do_run_workspace_script(
        self,
        workspace: Path,
        script_path: str,
        script_args: list[Any],
        packages: list[str],
    ) -> str:
        """Blocking workspace-script execution. Must run in executor."""
        target = (workspace / script_path).resolve()

        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            return json.dumps({"error": "Path traversal not allowed"})

        if not target.is_file():
            return json.dumps({"error": f"Script not found: {script_path}"})

        suffix = target.suffix.lower()
        scratch_dir = workspace / "scratch"

        py_bin: Path | None = None
        venv_setup_log = ""
        if suffix == ".py" and packages:
            try:
                py_bin, venv_path = self._ensure_workspace_venv(scratch_dir)
            except subprocess.TimeoutExpired:
                return json.dumps(
                    {"error": "uv venv timed out after 60 seconds"}
                )
            except subprocess.CalledProcessError as exc:
                return json.dumps(
                    {"error": "uv venv failed", "stderr": (exc.stderr or "")[:2000]}
                )
            except OSError as exc:
                return json.dumps(
                    {"error": f"Cannot create venv (is uv installed?): {exc}"}
                )

            try:
                install = subprocess.run(
                    ["uv", "pip", "install", "--python", str(py_bin), *packages],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                return json.dumps(
                    {"error": "uv pip install timed out after 5 minutes", "packages": packages}
                )
            except OSError as exc:
                return json.dumps({"error": f"Cannot run uv pip install: {exc}"})
            if install.returncode != 0:
                return json.dumps(
                    {
                        "error": "uv pip install failed",
                        "packages": packages,
                        "stderr": (install.stderr or "")[:4000],
                    }
                )
            venv_setup_log = f"[workspace venv: installed {', '.join(packages)}]\n"

        if suffix == ".py":
            if py_bin is None:
                existing = scratch_dir / ".venv" / "bin" / "python"
                if existing.is_file():
                    py_bin = existing
            python_cmd = str(py_bin) if py_bin else "python3"
            cmd = [python_cmd, str(target)] + [str(a) for a in script_args]
        elif suffix == ".sh":
            cmd = ["bash", str(target)] + [str(a) for a in script_args]
        elif suffix in (".ts", ".js"):
            cmd = ["node", str(target)] + [str(a) for a in script_args]
        else:
            cmd = [str(target)] + [str(a) for a in script_args]

        try:
            result = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = venv_setup_log + result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            if len(output) > 30_000:
                output = output[:30_000] + "\n\n[... truncated at 30,000 characters]"
            return output if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Script timed out after 120 seconds"})
        except OSError as exc:
            return json.dumps({"error": f"Cannot execute script: {exc}"})

    async def _tool_attach_workspace_file(
        self, arguments: dict[str, Any]
    ) -> ToolResult:
        rel_path = str(arguments.get("path", "")).strip()
        display_name = str(arguments.get("display_name", "")).strip()
        user_id = arguments.get("_user_id", "system")
        conv_id = self._conv_id_from_args(arguments)

        # Legacy support
        if "skill_name" in arguments and arguments["skill_name"]:
            arguments = self._migrate_legacy_args(arguments)
            rel_path = str(arguments.get("path", "")).strip()

        if not rel_path:
            return ToolResult(
                tool_call_id="",
                content=json.dumps({"error": "path is required"}),
                is_error=True,
            )
        if not conv_id:
            return ToolResult(
                tool_call_id="",
                content=json.dumps({"error": "No conversation context"}),
                is_error=True,
            )

        # Serialize concurrent attaches of the same source path so the
        # scratch→outputs copy + registry lookup/create below can't race
        # (two callers both seeing the unique filename dance at the same
        # time could end up writing to the same dest file).
        path_lock = await self._get_path_lock(conv_id, rel_path)
        async with path_lock:
            # Resolve the file
            target, err = self._resolve_file_path(user_id, rel_path, conv_id)
            if err is not None:
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps({"error": err}),
                    is_error=True,
                )
            assert target is not None

            # If the file is in scratch/, copy it to outputs/
            root = self.get_workspace_root(user_id, conv_id)
            output_dir = self.get_output_dir(user_id, conv_id)

            try:
                relative = target.relative_to(root.resolve())
            except ValueError:
                # Legacy file — copy it to outputs
                relative = Path(target.name)

            if str(relative).startswith("scratch/"):
                dest = output_dir / target.name
                if dest.exists():
                    stem = dest.stem
                    suffix = dest.suffix
                    counter = 1
                    while dest.exists():
                        dest = output_dir / f"{stem}-{counter}{suffix}"
                        counter += 1
                await _to_thread(shutil.copy2, target, dest)
                target = dest
                stored_path = f"outputs/{dest.name}"
            elif str(relative).startswith("outputs/"):
                stored_path = str(relative.as_posix())
            else:
                # uploads/ or other — reference in place
                stored_path = str(relative.as_posix()) if relative else target.name

            name = display_name or target.name
            media_type, _enc = mimetypes.guess_type(target.name)
            media_type = media_type or "application/octet-stream"

            if media_type.startswith("image/"):
                kind = "image"
            elif media_type.startswith("text/") or media_type in (
                "application/json",
                "application/xml",
            ):
                kind = "text"
            else:
                kind = "document"

            size_bytes = target.stat().st_size

            # Register the output file (or update if already registered)
            file_id = ""
            existing = await self.find_file_by_path(conv_id, stored_path)
            if existing is None:
                entity = await self.register_file(
                    conversation_id=conv_id,
                    user_id=user_id,
                    category="output",
                    filename=target.name,
                    rel_path=stored_path,
                    media_type=media_type,
                    size=size_bytes,
                    created_by="ai",
                )
                file_id = entity.get("_id", "")
            else:
                file_id = existing.get("_id", "")
                if existing.get("category") != "output":
                    await self.update_file(
                        file_id,
                        {"category": "output", "rel_path": stored_path},
                    )

        attachment = FileAttachment(
            kind=kind,
            name=name,
            media_type=media_type,
            workspace_skill="workspace",
            workspace_path=stored_path,
            workspace_conv=conv_id or "",
            workspace_file_id=file_id,
            size=size_bytes,
        )

        summary = (
            f"Attached {name} ({media_type}, {size_bytes} bytes). "
            f"The user will see a downloadable chip on your reply."
        )
        return ToolResult(
            tool_call_id="",
            content=summary,
            attachments=(attachment,),
        )

    async def _tool_annotate_workspace_file(
        self, arguments: dict[str, Any]
    ) -> str:
        rel_path = str(arguments.get("path", "")).strip()
        conv_id = self._conv_id_from_args(arguments)

        if not rel_path or not conv_id:
            return json.dumps({"error": "path and conversation context required"})

        # Serialize concurrent metadata updates on the same file so the
        # read-modify-write sequence below can't clobber itself under
        # fan-out. Different paths run in parallel.
        path_lock = await self._get_path_lock(conv_id, rel_path)
        async with path_lock:
            entity = await self.find_file_by_path(conv_id, rel_path)
            if entity is None:
                return json.dumps(
                    {"error": f"File not registered: {rel_path}. It may not have been created through workspace tools."}
                )

            file_id = entity.get("_id", "")
            updates: dict[str, Any] = {}

            description = arguments.get("description")
            if description is not None:
                updates["description"] = str(description)

            reusable = arguments.get("reusable")
            if reusable is not None:
                updates["reusable"] = bool(reusable)

            derivation_notes = arguments.get("derivation_notes")
            if derivation_notes is not None:
                updates["derivation_notes"] = str(derivation_notes)

            derived_from_path = arguments.get("derived_from")
            if derived_from_path is not None:
                parent = await self.find_file_by_path(conv_id, str(derived_from_path))
                if parent:
                    updates["derived_from"] = parent.get("_id", "")
                    updates["derivation_method"] = "script"
                else:
                    updates["derived_from"] = None

            if not updates:
                return json.dumps({"status": "no changes", "path": rel_path})

            await self.update_file(file_id, updates)
            return json.dumps(
                {"status": "annotated", "path": rel_path, "updated": list(updates.keys())}
            )

    async def _tool_delete_workspace_file(
        self, arguments: dict[str, Any]
    ) -> str:
        rel_path = str(arguments.get("path", "")).strip()
        user_id = arguments.get("_user_id", "system")
        conv_id = self._conv_id_from_args(arguments)

        if not rel_path or not conv_id:
            return json.dumps({"error": "path and conversation context required"})

        # Delete from disk
        root = self.get_workspace_root(user_id, conv_id)
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return json.dumps({"error": "Path traversal not allowed"})

        # Serialize concurrent operations on this path so an in-flight
        # write or annotate can finish before the delete lands.
        path_lock = await self._get_path_lock(conv_id, rel_path)
        async with path_lock:
            deleted_disk = False
            if target.is_file():
                target.unlink()
                deleted_disk = True

            # Delete registry entry
            deleted_registry = False
            entity = await self.find_file_by_path(conv_id, rel_path)
            if entity:
                await self.delete_file(entity.get("_id", ""))
                deleted_registry = True

            if not deleted_disk and not deleted_registry:
                return json.dumps({"error": f"File not found: {rel_path}"})

            return json.dumps(
                {"status": "deleted", "path": rel_path}
            )

    async def _tool_share_workspace_file(
        self, arguments: dict[str, Any]
    ) -> str:
        rel_path = str(arguments.get("path", "")).strip()
        user_id = arguments.get("_user_id", "system")
        conv_id = self._conv_id_from_args(arguments)

        if not rel_path:
            return json.dumps({"error": "path is required"})
        if not conv_id:
            return json.dumps({"error": "No conversation context"})

        # Tool parameters arrive as their declared types after coercion,
        # but defend against strings-coming-in-as-JSON too.
        try:
            max_accesses = int(
                arguments.get("max_accesses") or _DEFAULT_SHARE_MAX_ACCESSES
            )
        except (TypeError, ValueError):
            max_accesses = _DEFAULT_SHARE_MAX_ACCESSES
        try:
            ttl_seconds = int(
                arguments.get("ttl_seconds") or _DEFAULT_SHARE_TTL_SECONDS
            )
        except (TypeError, ValueError):
            ttl_seconds = _DEFAULT_SHARE_TTL_SECONDS
        via_tunnel_raw = arguments.get("via_tunnel")
        via_tunnel = bool(via_tunnel_raw) and str(via_tunnel_raw).lower() not in (
            "false",
            "0",
            "no",
            "off",
        )

        try:
            share = await self.create_file_share(
                user_id=user_id,
                conversation_id=conv_id,
                rel_path=rel_path,
                max_accesses=max_accesses,
                ttl_seconds=ttl_seconds,
                via_tunnel=via_tunnel,
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            logger.exception("Failed to create workspace file share")
            return json.dumps({"error": f"Unexpected error: {exc}"})

        return json.dumps(share)

    # ── WebSocket Handlers ───────────────────────────────────────────

    async def _ws_workspace_browse(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        user_id = getattr(conn, "user_id", "system")
        conv_id = frame.get("conversation_id") or None

        # Legacy support: accept skill_name for old frontend code
        skill_name = frame.get("skill_name", "")

        if conv_id:
            root = self.get_workspace_root(user_id, conv_id)
            files = await _to_thread(self._list_files, root)
        elif skill_name:
            # Legacy path
            legacy = self._legacy_workspace_dir(user_id, skill_name)
            files = await _to_thread(self._list_files, legacy)
        else:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "conversation_id is required",
            }

        # Return both new and legacy frame types for compat
        return {
            "type": "workspace.browse.result",
            "ref": frame.get("id"),
            "conversation_id": conv_id,
            "skill_name": skill_name,
            "files": files,
        }

    async def _ws_workspace_download(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        user_id = getattr(conn, "user_id", "system")
        rel_path = frame.get("path", "")
        conv_id = frame.get("conversation_id") or None
        skill_name = frame.get("skill_name", "")

        if not rel_path:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "path is required",
            }

        # Try to find the file across all possible locations
        candidates: list[Path] = []

        if conv_id:
            # New layout
            new_root = self.get_workspace_root(user_id, conv_id)
            candidates.append(new_root)

            # Legacy conversation workspace with skill name
            if skill_name:
                candidates.append(
                    self._legacy_conversation_workspace(user_id, conv_id, skill_name)
                )

            # Legacy: scan all skill dirs under old conversation workspace
            legacy_conv = (
                self._legacy_workspace_top()
                / "users"
                / user_id
                / "conversations"
                / conv_id
            )
            if legacy_conv.is_dir():
                for skill_dir in legacy_conv.iterdir():
                    if skill_dir.is_dir() and skill_dir not in candidates:
                        candidates.append(skill_dir)

        # Legacy per-user workspace
        if skill_name:
            candidates.append(self._legacy_workspace_dir(user_id, skill_name))

        target: Path | None = None
        for workspace in candidates:
            if not workspace.is_dir():
                continue
            candidate = (workspace / rel_path).resolve()
            try:
                candidate.relative_to(workspace.resolve())
            except ValueError:
                return {
                    "type": "gilbert.error",
                    "ref": frame.get("id"),
                    "code": 403,
                    "error": "Path traversal not allowed",
                }
            if candidate.is_file():
                target = candidate
                break

        if target is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 404,
                "error": f"File not found: {rel_path}",
            }

        try:
            data = await _to_thread(target.read_bytes)
            media_type, _enc = mimetypes.guess_type(target.name)
            return {
                "type": "workspace.download.result",
                "ref": frame.get("id"),
                "skill_name": skill_name,
                "path": rel_path,
                "filename": target.name,
                "media_type": media_type or "application/octet-stream",
                "size": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        except OSError as exc:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 500,
                "error": f"Cannot read file: {exc}",
            }

    async def _ws_files_list(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        conv_id = frame.get("conversation_id", "")
        if not conv_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "conversation_id is required",
            }

        files = await self.list_files(conv_id)

        # Reconcile: remove registry entries for files deleted from disk
        user_id = getattr(conn, "user_id", "system")
        live: list[dict[str, Any]] = []
        for f in files:
            rel_path = f.get("rel_path", "")
            f_user = f.get("user_id", user_id)
            if rel_path and f.get("conversation_id"):
                root = self.get_workspace_root(f_user, f["conversation_id"])
                if not (root / rel_path).is_file():
                    fid = f.get("_id", "")
                    if fid and self._storage:
                        await self._storage.delete(
                            _WORKSPACE_FILES_COLLECTION, fid
                        )
                    continue
            live.append(f)

        uploads = [f for f in live if f.get("category") == "upload"]
        outputs = [f for f in live if f.get("category") == "output"]
        scratch = [f for f in live if f.get("category") == "scratch"]

        return {
            "type": "workspace.files.list.result",
            "ref": frame.get("id"),
            "conversation_id": conv_id,
            "uploads": uploads,
            "outputs": outputs,
            "scratch": scratch,
        }

    async def _ws_files_pin(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        file_id = frame.get("file_id", "")
        pinned = frame.get("pinned", True)
        if not file_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "file_id is required",
            }

        updated = await self.update_file(file_id, {"pinned": bool(pinned)})
        if updated is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 404,
                "error": "File not found",
            }

        return {
            "type": "workspace.files.pin.result",
            "ref": frame.get("id"),
            "file_id": file_id,
            "pinned": bool(pinned),
        }

    async def _ws_files_delete(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        file_id = frame.get("file_id", "")
        if not file_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 400,
                "error": "file_id is required",
            }

        deleted = await self.delete_file(file_id)
        if not deleted:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "code": 404,
                "error": "File not found",
            }

        return {
            "type": "workspace.files.delete.result",
            "ref": frame.get("id"),
            "file_id": file_id,
        }
