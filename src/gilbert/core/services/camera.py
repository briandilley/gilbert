"""Camera event service — long-lived consumer for camera detection backends.

Subscribes to a single ``CameraEventBackend`` (Frigate today, multi-backend
tomorrow), republishes detection events on the bus with per-camera role
overrides applied, persists them into a ``camera_events`` collection with
a configurable retention sweep, optionally annotates snapshots through the
vision capability, and exposes AI tools + WebSocket RPCs for the UI.

Per the multi-user-isolation memo, this is a singleton — every per-event
state lives on the per-event-id annotation lock dict (gated by a guard
lock), and tasks spawned for the stream consumer / annotation pipeline
explicitly snapshot the context at spawn time so a later
``ContextVar.set`` can't leak across sibling tasks.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import re
import time
from collections.abc import AsyncIterator  # noqa: F401  — re-exported for downstream subclasses
from dataclasses import asdict  # noqa: F401  — re-exported for downstream subclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.core.services._ui_blocks import build_preview_output
from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.camera import (
    CameraBackendError,
    CameraEvent,
    CameraEventBackend,
    CameraEventPhase,
    CameraInfo,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput
from gilbert.interfaces.vision import VisionProvider
from gilbert.interfaces.ws import RpcHandler

logger = logging.getLogger(__name__)

_CAMERA_EVENTS_COLLECTION = "camera_events"
_CAMERA_MUTES_COLLECTION = "camera_mutes"

# Maximum raw JPEG bytes for an inline ``get_snapshot`` attachment.
# Frigate's ``?h=720`` server-side downscale should keep almost every
# JPEG under this; the cap exists for the pathological 4K + bad-quality
# case.
_MAX_INLINE_BYTES = 1_000_000

_DEFAULT_VISION_PROMPT = (
    "Describe in one terse, observational sentence what is visible in "
    "this security camera frame. Note: people (count and notable attire "
    "or carried objects), vehicles (count and color), packages or "
    "containers, animals. State only what you can see. Do not speculate "
    "about identity, intent, or activity (\"a delivery driver dropping "
    "off a package\" is wrong; \"a person in brown clothing setting a "
    "box near the door\" is right). No preamble, no emoji, no hedging "
    "qualifiers."
)

_DEFAULT_RECONNECT_MAX_SECONDS = 60.0
_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_VISION_CONCURRENCY = 4
# Default labels eligible for vision auto-annotation. Narrowed from
# ``["person", "package"]`` because person fires constantly on busy
# outdoor cameras (mail truck, neighbor walking dog, leaves in wind);
# add ``"person"`` opt-in.
_DEFAULT_VISION_ENABLED_LABELS: tuple[str, ...] = ("package",)
_DEFAULT_CAMERA_ROLE = "everyone"
_KNOWN_ROLES = frozenset({"admin", "user", "everyone"})

_GLOB_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _glob_safe(token: str) -> bool:
    """``True`` iff ``token`` is safe to embed in a dotted glob pattern.

    Reject anything containing ``.``, whitespace, or shell-meta — a
    Frigate label / camera that goes rogue (or a malformed payload)
    must NOT produce ambiguous ``camera.<X>.detected.<Y>`` events.
    """
    return bool(token) and bool(_GLOB_SAFE_RE.match(token))


class CameraEventService(Service):
    """Long-lived consumer for object-detection events from camera backends.

    Capabilities: cameras, ai_tools, ws_handlers
    """

    config_namespace = "cameras"
    config_category = "Monitoring"
    slash_namespace = "cameras"

    def __init__(self) -> None:
        self._backend: CameraEventBackend | None = None
        self._backend_name: str = "frigate"
        self._enabled: bool = False
        self._event_bus: EventBus | None = None
        self._storage: StorageBackend | None = None
        self._resolver: ServiceResolver | None = None

        # Live tasks
        self._stream_task: asyncio.Task[None] | None = None
        self._reconnect_attempt: int = 0

        # Cached static metadata
        self._cameras: list[CameraInfo] = []
        self._cameras_by_name: dict[str, CameraInfo] = {}

        # Config (cached, repopulated in on_config_changed)
        self._retention_days: int = _DEFAULT_RETENTION_DAYS
        self._vision_text_retention_days: int = 0
        self._selected_cameras: tuple[str, ...] = ()
        self._default_camera_role: str = _DEFAULT_CAMERA_ROLE
        self._role_overrides: dict[str, str] = {}
        self._vision_enabled_labels: frozenset[str] = frozenset(
            _DEFAULT_VISION_ENABLED_LABELS
        )
        self._vision_per_camera: dict[str, list[str]] = {}
        self._vision_prompt: str = _DEFAULT_VISION_PROMPT
        self._reconnect_max_seconds: float = _DEFAULT_RECONNECT_MAX_SECONDS

        # Per-event-id locks for parallel vision annotation.
        self._annotation_locks: dict[str, asyncio.Lock] = {}
        self._annotation_locks_guard = asyncio.Lock()
        # Bounded parallelism for vision describe_image calls.
        self._vision_semaphore: asyncio.Semaphore = asyncio.Semaphore(
            _DEFAULT_VISION_CONCURRENCY
        )
        self._vision_concurrency: int = _DEFAULT_VISION_CONCURRENCY

    # ── Capability exposure ─────────────────────────────────────────

    @property
    def available_cameras(self) -> list[str]:
        """Synchronous snapshot for the configuration dynamic-choices hook."""
        return [c.name for c in self._cameras]

    # ── Service lifecycle ───────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="cameras",
            capabilities=frozenset({"cameras", "ai_tools", "ws_handlers"}),
            requires=frozenset({"event_bus", "entity_storage"}),
            optional=frozenset({"configuration", "scheduler", "vision"}),
            events=frozenset(
                {
                    "camera.event.detected",
                    "camera.event.ended",
                    "camera.snapshot.annotated",
                    "camera.backend.connected",
                    "camera.backend.disconnected",
                    # ``camera.<label>.detected.<camera>`` is dynamic and
                    # cannot be enumerated up front; it's documented in
                    # ``memory-camera-service.md``.
                }
            ),
            toggleable=True,
            toggle_description="Camera object-detection events",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        bus_svc = resolver.require_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._event_bus = bus_svc.bus

        storage_svc = resolver.require_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend

        if self._storage is not None:
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_CAMERA_EVENTS_COLLECTION,
                    fields=["camera", "started_at"],
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_CAMERA_EVENTS_COLLECTION,
                    fields=["label", "started_at"],
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_CAMERA_EVENTS_COLLECTION,
                    fields=["started_at"],
                )
            )

        full_section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            full_section = config_svc.get_section_safe(self.config_namespace)
            self._apply_config(full_section)

        if not full_section.get("enabled", False):
            logger.info("Camera service disabled (enabled=false)")
            return

        self._enabled = True

        self._backend_name = full_section.get("backend", "frigate")
        backends = CameraEventBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is None:
            logger.warning(
                "Camera service NOT starting — unknown backend %r "
                "(registered: %s)",
                self._backend_name,
                sorted(backends),
            )
            self._enabled = False
            return
        self._backend = backend_cls()

        settings: dict[str, object] = dict(full_section.get("settings", {}))
        try:
            await self._backend.initialize(settings)
        except Exception:
            logger.exception(
                "Camera backend initialize() failed — service stays disabled"
            )
            self._enabled = False
            self._backend = None
            return

        try:
            self._cameras = await self._backend.list_cameras()
            self._cameras_by_name = {c.name: c for c in self._cameras}
        except Exception:
            logger.warning(
                "Could not enumerate cameras at startup", exc_info=True
            )

        scheduler = resolver.get_capability("scheduler")
        if isinstance(scheduler, SchedulerProvider):
            scheduler.add_job(
                name="cameras-refresh",
                schedule=Schedule.every(300.0),  # 5 min
                callback=self._refresh_camera_list,
                system=True,
            )
            scheduler.add_job(
                name="cameras-retention-sweep",
                schedule=Schedule.every(3600.0),  # 1 hour
                callback=self._sweep_old_camera_events,
                system=True,
            )

        # Spawn the stream consumer with an explicit context snapshot so
        # any ``ContextVar.set()`` inside the loop doesn't leak.
        self._stream_task = asyncio.create_task(
            self._run_stream_consumer(),
            name="cameras-stream-consumer",
            context=contextvars.copy_context(),
        )

        logger.info(
            "Camera service started — backend=%s, %d cameras",
            self._backend_name,
            len(self._cameras),
        )

    async def stop(self) -> None:
        was_enabled = self._enabled
        self._enabled = False
        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stream_task = None
        if self._backend is not None:
            try:
                await self._backend.disconnect()
            except Exception:
                logger.debug(
                    "disconnect() during stop raised", exc_info=True
                )
            try:
                await self._backend.close()
            except Exception:
                logger.debug("close() during stop raised", exc_info=True)
            self._backend = None
        if was_enabled:
            logger.info("Camera service stopped")

    async def _refresh_camera_list(self) -> None:
        if self._backend is None:
            return
        try:
            self._cameras = await self._backend.list_cameras()
            self._cameras_by_name = {c.name: c for c in self._cameras}
        except Exception:
            logger.debug("Camera list refresh failed", exc_info=True)

    # ── Configurable ────────────────────────────────────────────────

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the camera-event service.",
                default=False,
            ),
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Camera-event backend provider.",
                default="frigate",
                restart_required=True,
                choices=tuple(CameraEventBackend.registered_backends().keys())
                or ("frigate",),
            ),
            ConfigParam(
                key="retention_days",
                type=ToolParameterType.NUMBER,
                description=(
                    "How long to keep rows in the ``camera_events`` "
                    "collection (days). Older rows are swept hourly."
                ),
                default=_DEFAULT_RETENTION_DAYS,
            ),
            ConfigParam(
                key="vision_text_retention_days",
                type=ToolParameterType.NUMBER,
                description=(
                    "If > 0, scrub ``vision_text`` from rows older "
                    "than this many days while keeping the bare event "
                    "metadata. ``0`` (default) means vision text "
                    "expires with the row."
                ),
                default=0,
            ),
            ConfigParam(
                key="selected_cameras",
                type=ToolParameterType.ARRAY,
                description=(
                    "Subset of cameras to monitor; empty = all the "
                    "backend reports."
                ),
                default=[],
                choices_from="cameras",
            ),
            ConfigParam(
                key="default_camera_role",
                type=ToolParameterType.STRING,
                description=(
                    "Role required to see camera events for cameras "
                    "not explicitly listed in role_overrides."
                ),
                default=_DEFAULT_CAMERA_ROLE,
                choices=("everyone", "user", "admin"),
            ),
            ConfigParam(
                key="role_overrides",
                type=ToolParameterType.OBJECT,
                description=(
                    "Per-camera role override map: "
                    "``{camera_name: \"admin\"|\"user\"|\"everyone\"}``."
                ),
                default={},
            ),
            ConfigParam(
                key="vision_enabled_labels",
                type=ToolParameterType.ARRAY,
                description=(
                    "Labels to auto-annotate snapshots for via the "
                    "vision capability (default: ``[\"package\"]``)."
                ),
                default=list(_DEFAULT_VISION_ENABLED_LABELS),
            ),
            ConfigParam(
                key="vision_per_camera",
                type=ToolParameterType.OBJECT,
                description=(
                    "Per-camera override for vision-annotation labels: "
                    "missing key uses defaults; empty list ``[]`` "
                    "disables; non-empty overrides "
                    "``vision_enabled_labels``."
                ),
                default={},
            ),
            ConfigParam(
                key="vision_concurrency",
                type=ToolParameterType.INTEGER,
                description=(
                    "Max parallel describe_image calls across all "
                    "cameras (asyncio.Semaphore)."
                ),
                default=_DEFAULT_VISION_CONCURRENCY,
            ),
            ConfigParam(
                key="vision_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt fed to the vision capability when "
                    "annotating snapshots."
                ),
                default=_DEFAULT_VISION_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="reconnect_max_seconds",
                type=ToolParameterType.NUMBER,
                description=(
                    "Cap on the exponential reconnect backoff for the "
                    "backend stream."
                ),
                default=_DEFAULT_RECONNECT_MAX_SECONDS,
            ),
        ]
        backends = CameraEventBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    def _apply_config(self, section: dict[str, Any]) -> None:
        try:
            self._retention_days = int(
                section.get("retention_days", self._retention_days)
            )
        except (TypeError, ValueError):
            self._retention_days = _DEFAULT_RETENTION_DAYS
        try:
            self._vision_text_retention_days = int(
                section.get(
                    "vision_text_retention_days",
                    self._vision_text_retention_days,
                )
            )
        except (TypeError, ValueError):
            self._vision_text_retention_days = 0
        cams = section.get("selected_cameras")
        if isinstance(cams, list):
            self._selected_cameras = tuple(str(c) for c in cams)
        role = section.get("default_camera_role", self._default_camera_role)
        self._default_camera_role = (
            role if role in _KNOWN_ROLES else _DEFAULT_CAMERA_ROLE
        )
        overrides = section.get("role_overrides")
        if isinstance(overrides, dict):
            self._role_overrides = {
                str(k): str(v)
                for k, v in overrides.items()
                if isinstance(v, str) and v in _KNOWN_ROLES
            }
        labels = section.get("vision_enabled_labels")
        if isinstance(labels, list):
            self._vision_enabled_labels = frozenset(
                str(label) for label in labels
            )
        per_cam = section.get("vision_per_camera")
        if isinstance(per_cam, dict):
            self._vision_per_camera = {
                str(k): [str(label) for label in v]
                for k, v in per_cam.items()
                if isinstance(v, list)
            }
        prompt = section.get("vision_prompt")
        if isinstance(prompt, str) and prompt.strip():
            self._vision_prompt = prompt
        else:
            self._vision_prompt = _DEFAULT_VISION_PROMPT
        try:
            self._reconnect_max_seconds = float(
                section.get(
                    "reconnect_max_seconds", self._reconnect_max_seconds
                )
            )
        except (TypeError, ValueError):
            self._reconnect_max_seconds = _DEFAULT_RECONNECT_MAX_SECONDS
        try:
            new_concurrency = max(
                1, int(section.get("vision_concurrency", self._vision_concurrency))
            )
        except (TypeError, ValueError):
            new_concurrency = _DEFAULT_VISION_CONCURRENCY
        if new_concurrency != self._vision_concurrency:
            self._vision_concurrency = new_concurrency
            self._vision_semaphore = asyncio.Semaphore(new_concurrency)

    # ── ConfigActionProvider ────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=CameraEventBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    # ── Stream consumer loop ────────────────────────────────────────

    async def _run_stream_consumer(self) -> None:
        """Long-lived consumer of the backend's event stream.

        Reconnects on transport failures with exponential backoff capped
        at ``self._reconnect_max_seconds``. Publishes
        ``camera.backend.connected`` / ``camera.backend.disconnected``
        so the dashboard can show live status.
        """
        # Initial backoff cannot exceed the configured cap — production
        # uses 1.0s (cap 60s) for a generous floor, tests pin both to
        # tiny values to keep iteration tight.
        backoff = min(1.0, self._reconnect_max_seconds)
        while self._enabled and self._backend is not None:
            try:
                await self._backend.connect()
                self._reconnect_attempt = 0
                backoff = min(1.0, self._reconnect_max_seconds)
                await self._publish_status("connected")

                async for ev in self._backend.stream_events():
                    await self._handle_event(ev)

                # Stream ended cleanly — usually because disconnect()
                # was called by stop(); the outer while loop exits.
                await self._publish_status(
                    "disconnected", error="stream ended"
                )

            except CameraBackendError as exc:
                logger.warning(
                    "Camera backend stream error: %s — reconnecting in %.1fs",
                    exc,
                    backoff,
                )
                await self._publish_status("disconnected", error=str(exc))
                try:
                    if self._backend is not None:
                        await self._backend.disconnect()
                except Exception:
                    logger.debug(
                        "disconnect() during reconnect raised", exc_info=True
                    )
                if not self._enabled:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._reconnect_max_seconds)
                self._reconnect_attempt += 1
                continue

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Unexpected error in camera stream consumer"
                )
                if not self._enabled:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._reconnect_max_seconds)
                continue

        logger.info("Camera stream consumer exited")

    async def _publish_status(self, status: str, error: str = "") -> None:
        if self._event_bus is None:
            return
        if status == "connected":
            event_type = "camera.backend.connected"
        else:
            event_type = "camera.backend.disconnected"
        try:
            await self._event_bus.publish(
                Event(
                    event_type=event_type,
                    data={
                        "backend_name": self._backend_name,
                        "transport": "mqtt",
                        "error": error,
                    },
                    source="camera",
                )
            )
        except Exception:
            logger.debug("publish status %s failed", status, exc_info=True)

    # ── Per-event handling ──────────────────────────────────────────

    async def _handle_event(self, ev: CameraEvent) -> None:
        """Process a single backend event: persist, publish, optionally annotate."""
        if self._selected_cameras and ev.camera not in self._selected_cameras:
            return

        # 1. Stamp Gilbert-proxied URLs onto the event for downstream
        # consumers (the bus event payload, the persisted row).
        proxied = self._stamp_proxied_urls(ev)

        # 2. Persist FIRST so the annotation task spawned below can
        # read the row back without racing the persist write.
        try:
            await self._persist_event(proxied)
        except Exception:
            logger.warning(
                "Failed to persist camera event %s",
                proxied.event_id,
                exc_info=True,
            )

        # 3. Publish onto the bus, twice — static name + glob companion.
        required_role = self._effective_role(proxied.camera)
        payload = self._event_to_payload(proxied, required_role)

        detected_type = (
            "camera.event.detected"
            if proxied.phase is CameraEventPhase.ACTIVE
            else "camera.event.ended"
        )
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    event_type=detected_type,
                    data=payload,
                    source="camera",
                )
            )
            if proxied.phase is CameraEventPhase.ACTIVE and _glob_safe(
                proxied.label
            ) and _glob_safe(proxied.camera):
                await self._event_bus.publish(
                    Event(
                        event_type=(
                            f"camera.{proxied.label}.detected.{proxied.camera}"
                        ),
                        data=payload,
                        source="camera",
                    )
                )

        # 4. Vision annotation — async, off the hot path. Spawn with
        # an explicit context snapshot so a ContextVar.set() inside
        # the annotation task can't leak back to the consumer.
        if self._should_annotate(proxied):
            asyncio.create_task(
                self._annotate_event(proxied),
                name=f"camera-annotate-{proxied.event_id}",
                context=contextvars.copy_context(),
            )

    def _stamp_proxied_urls(self, ev: CameraEvent) -> CameraEvent:
        """Replace ``snapshot_url``/``clip_url`` with Gilbert-proxied paths.

        Backends MAY return raw URLs or pre-stamped proxied paths; this
        helper normalizes to the spec'd ``/api/cameras/events/<id>/...``
        form regardless. ``direct_*`` URLs from the backend are
        preserved unchanged on the dataclass for advanced LAN-only
        consumers.
        """
        snapshot_path = (
            f"/api/cameras/events/{ev.event_id}/snapshot.jpg"
            if ev.has_snapshot or ev.snapshot_url
            else ev.snapshot_url
        )
        clip_path = (
            f"/api/cameras/events/{ev.event_id}/clip.mp4"
            if ev.has_clip or ev.clip_url
            else ev.clip_url
        )
        # Preserve raw URLs the backend already supplied as direct_*.
        # If the backend left them empty but supplied snapshot_url /
        # clip_url, treat those as direct (they're not proxied yet).
        direct_snapshot = ev.direct_snapshot_url or (
            ev.snapshot_url if not ev.snapshot_url.startswith("/api/") else ""
        )
        direct_clip = ev.direct_clip_url or (
            ev.clip_url if not ev.clip_url.startswith("/api/") else ""
        )
        return CameraEvent(
            event_id=ev.event_id,
            camera=ev.camera,
            label=ev.label,
            sub_label=ev.sub_label,
            phase=ev.phase,
            score=ev.score,
            started_at=ev.started_at,
            ended_at=ev.ended_at,
            zones=ev.zones,
            snapshot_url=snapshot_path,
            clip_url=clip_path,
            has_snapshot=ev.has_snapshot,
            has_clip=ev.has_clip,
            source_backend=ev.source_backend,
            direct_snapshot_url=direct_snapshot,
            direct_clip_url=direct_clip,
            raw=ev.raw,
        )

    def _effective_role(self, camera: str) -> str:
        return self._role_overrides.get(camera, self._default_camera_role)

    def _event_to_payload(
        self,
        ev: CameraEvent,
        required_role: str,
    ) -> dict[str, Any]:
        return {
            "event_id": ev.event_id,
            "camera": ev.camera,
            "label": ev.label,
            "sub_label": ev.sub_label,
            "phase": ev.phase.value,
            "score": ev.score,
            "started_at": ev.started_at,
            "started_iso": ev.started_iso,
            "ended_at": ev.ended_at,
            "ended_iso": ev.ended_iso,
            "duration_seconds": ev.duration_seconds,
            "zones": list(ev.zones),
            "snapshot_url": ev.snapshot_url,
            "clip_url": ev.clip_url,
            "direct_snapshot_url": ev.direct_snapshot_url,
            "direct_clip_url": ev.direct_clip_url,
            "has_snapshot": ev.has_snapshot,
            "has_clip": ev.has_clip,
            "source_backend": ev.source_backend or self._backend_name,
            "vision_text": "",
            "required_role": required_role,
        }

    async def _persist_event(self, ev: CameraEvent) -> None:
        if self._storage is None:
            return
        existing = await self._storage.get(
            _CAMERA_EVENTS_COLLECTION, ev.event_id
        )
        # Preserve any vision_text that previous annotation already wrote.
        vision_text = (
            existing.get("vision_text", "") if existing else ""
        )
        row = {
            "event_id": ev.event_id,
            "camera": ev.camera,
            "label": ev.label,
            "sub_label": ev.sub_label,
            "score": ev.score,
            "phase": ev.phase.value,
            "started_at": ev.started_at,
            "ended_at": ev.ended_at,
            "duration_seconds": ev.duration_seconds,
            "zones": list(ev.zones),
            "snapshot_url": ev.snapshot_url,
            "clip_url": ev.clip_url,
            "has_snapshot": ev.has_snapshot,
            "has_clip": ev.has_clip,
            "source_backend": ev.source_backend or self._backend_name,
            "vision_text": vision_text,
            "required_role": self._effective_role(ev.camera),
        }
        await self._storage.put(
            _CAMERA_EVENTS_COLLECTION, ev.event_id, row
        )

    def _should_annotate(self, ev: CameraEvent) -> bool:
        if not ev.has_snapshot:
            return False
        labels = self._vision_per_camera.get(ev.camera)
        if labels is not None:
            # Empty list disables; non-empty overrides global defaults.
            if not labels:
                return False
            return ev.label in labels
        return ev.label in self._vision_enabled_labels

    async def _annotation_lock(self, event_id: str) -> asyncio.Lock:
        async with self._annotation_locks_guard:
            lock = self._annotation_locks.get(event_id)
            if lock is None:
                lock = asyncio.Lock()
                self._annotation_locks[event_id] = lock
            return lock

    async def _annotate_event(self, ev: CameraEvent) -> None:
        """Run the snapshot through the vision capability, persist the result."""
        if not ev.has_snapshot:
            return

        lock = await self._annotation_lock(ev.event_id)
        async with lock:
            existing = (
                await self._storage.get(
                    _CAMERA_EVENTS_COLLECTION, ev.event_id
                )
                if self._storage is not None
                else None
            )
            if existing is not None and existing.get("vision_text"):
                return

            if self._resolver is None:
                return
            vision_svc = self._resolver.get_capability("vision")
            if not isinstance(vision_svc, VisionProvider):
                return

            snap = await self._fetch_snapshot_bytes(ev, max_height=720)
            if snap is None:
                return
            bytes_, media_type = snap

            async with self._vision_semaphore:
                try:
                    text = await vision_svc.describe_image(bytes_, media_type)
                except Exception:
                    logger.warning(
                        "Vision describe_image failed for %s",
                        ev.event_id,
                        exc_info=True,
                    )
                    return

            if not text:
                return

            await self._update_event_vision_text(ev.event_id, text)

            if self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        event_type="camera.snapshot.annotated",
                        data={
                            "event_id": ev.event_id,
                            "camera": ev.camera,
                            "label": ev.label,
                            "vision_text": text,
                            "required_role": self._effective_role(ev.camera),
                        },
                        source="camera",
                    )
                )

    async def _update_event_vision_text(
        self,
        event_id: str,
        text: str,
    ) -> None:
        if self._storage is None:
            return
        row = await self._storage.get(_CAMERA_EVENTS_COLLECTION, event_id)
        if row is None:
            return
        # Drop the synthetic ``_id`` field returned by the storage layer.
        row.pop("_id", None)
        row["vision_text"] = text
        await self._storage.put(_CAMERA_EVENTS_COLLECTION, event_id, row)

    async def _fetch_snapshot_bytes(
        self,
        ev: CameraEvent,
        *,
        max_height: int | None = 720,
    ) -> tuple[bytes, str] | None:
        if self._backend is None:
            return None
        try:
            ref = await self._backend.get_snapshot(
                ev.camera, ev.event_id, max_height=max_height
            )
        except TypeError:
            # Backend doesn't accept ``max_height`` kwarg yet — fall
            # back to the legacy positional-only signature.
            ref = await self._backend.get_snapshot(ev.camera, ev.event_id)
        except Exception:
            logger.debug(
                "Backend get_snapshot failed for %s",
                ev.event_id,
                exc_info=True,
            )
            return None
        if ref is None:
            return None
        if ref.is_inline:
            return ref.data, (ref.media_type or "image/jpeg")
        # URL-based — fetch via httpx with backend auth headers.
        if not ref.url:
            return None
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    ref.url, headers=self._backend.backend_auth_headers()
                )
                if resp.status_code != 200:
                    return None
                media = resp.headers.get("content-type", "image/jpeg")
                return resp.content, media
        except Exception:
            logger.debug(
                "URL snapshot fetch failed for %s",
                ev.event_id,
                exc_info=True,
            )
            return None

    # ── Retention sweep ─────────────────────────────────────────────

    async def _sweep_old_camera_events(self) -> None:
        if self._storage is None:
            return
        if self._retention_days <= 0:
            return
        cutoff_ms = int(
            (
                datetime.now(UTC) - timedelta(days=self._retention_days)
            ).timestamp()
            * 1000
        )
        try:
            removed = await self._storage.delete_query(
                Query(
                    collection=_CAMERA_EVENTS_COLLECTION,
                    filters=[
                        Filter(
                            field="started_at",
                            op=FilterOp.LT,
                            value=cutoff_ms,
                        )
                    ],
                )
            )
            if removed:
                logger.info(
                    "Camera retention sweep removed %d row(s) older than %d days",
                    removed,
                    self._retention_days,
                )
        except Exception:
            logger.warning("Camera retention sweep failed", exc_info=True)
            return

        # Optional: scrub vision_text from older-but-still-present rows.
        if self._vision_text_retention_days > 0:
            scrub_cutoff_ms = int(
                (
                    datetime.now(UTC)
                    - timedelta(days=self._vision_text_retention_days)
                ).timestamp()
                * 1000
            )
            try:
                rows = await self._storage.query(
                    Query(
                        collection=_CAMERA_EVENTS_COLLECTION,
                        filters=[
                            Filter(
                                field="started_at",
                                op=FilterOp.LT,
                                value=scrub_cutoff_ms,
                            )
                        ],
                    )
                )
                for row in rows:
                    if not row.get("vision_text"):
                        continue
                    entity_id = row.get("_id") or row.get("event_id")
                    if not entity_id:
                        continue
                    row.pop("_id", None)
                    row["vision_text"] = ""
                    await self._storage.put(
                        _CAMERA_EVENTS_COLLECTION, str(entity_id), row
                    )
            except Exception:
                logger.debug(
                    "vision_text scrub failed", exc_info=True
                )

    # ── CameraProvider implementation ───────────────────────────────

    async def list_cameras(self) -> list[CameraInfo]:
        return list(self._cameras)

    async def latest_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 20,
    ) -> list[CameraEvent]:
        if self._storage is None:
            return []
        filters: list[Filter] = []
        if camera:
            filters.append(Filter(field="camera", op=FilterOp.EQ, value=camera))
        if label:
            filters.append(Filter(field="label", op=FilterOp.EQ, value=label))
        if since_ms is not None:
            filters.append(
                Filter(field="started_at", op=FilterOp.GTE, value=since_ms)
            )
        if until_ms is not None:
            filters.append(
                Filter(field="started_at", op=FilterOp.LTE, value=until_ms)
            )
        rows = await self._storage.query(
            Query(
                collection=_CAMERA_EVENTS_COLLECTION,
                filters=filters,
                sort=[SortField(field="started_at", descending=True)],
                limit=max(1, min(limit, 200)),
            )
        )
        return [_row_to_event(r) for r in rows]

    async def get_event(self, event_id: str) -> CameraEvent | None:
        if self._storage is None:
            return None
        row = await self._storage.get(_CAMERA_EVENTS_COLLECTION, event_id)
        if row is None:
            return None
        return _row_to_event(row)

    async def get_snapshot_bytes(
        self,
        event_id: str,
        *,
        max_height: int | None = 720,
    ) -> tuple[bytes, str] | None:
        ev = await self.get_event(event_id)
        if ev is None or self._backend is None:
            return None
        return await self._fetch_snapshot_bytes(ev, max_height=max_height)

    # ── Time-window grammar ─────────────────────────────────────────

    @staticmethod
    def _parse_time_window(
        value: str | None,
        *,
        default: str = "24h",
        user_tz: str | None = None,
    ) -> int | None:
        """Parse a relative / "today" / "yesterday" / ISO-8601 string.

        Returns epoch ms or ``None`` if value is empty/None.

        ``user_tz`` (an IANA name like ``"America/New_York"``) anchors
        ``today`` / ``yesterday`` to the caller's local midnight rather
        than UTC midnight — so a user in ``America/Los_Angeles`` asking
        for "today" at 23:00 local gets events from 00:00 local, not
        from 00:00 UTC (which would be 17:00 the previous day local).
        Falls back to UTC if ``user_tz`` is empty or unknown.
        """
        if value is None or value == "":
            value = default
        if not value:
            return None
        v = value.strip().lower()

        tz: ZoneInfo
        if user_tz:
            try:
                tz = ZoneInfo(user_tz)
            except (ZoneInfoNotFoundError, ValueError):
                tz = ZoneInfo("UTC")
        else:
            tz = ZoneInfo("UTC")
        now_local = datetime.now(tz)

        # Relative shorthand: 30m / 4h / 7d (always relative to "now",
        # so TZ doesn't matter — the elapsed delta is the same).
        match = re.fullmatch(r"(\d+)([mhd])", v)
        if match:
            count, unit = int(match.group(1)), match.group(2)
            seconds = {"m": 60, "h": 3600, "d": 86400}[unit]
            return int(
                (datetime.now(UTC) - timedelta(seconds=count * seconds)).timestamp()
                * 1000
            )

        if v == "today":
            # Anchor to local midnight, then convert back to epoch ms.
            start = datetime(
                now_local.year, now_local.month, now_local.day, tzinfo=tz
            )
            return int(start.timestamp() * 1000)
        if v == "yesterday":
            start = datetime(
                now_local.year, now_local.month, now_local.day, tzinfo=tz
            ) - timedelta(days=1)
            return int(start.timestamp() * 1000)

        # ISO 8601
        try:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None

    # ── Tool provider ───────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "cameras"

    def get_tools(
        self,
        user_ctx: UserContext | None = None,
    ) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="list_cameras",
                slash_command="list",
                slash_group="cameras",
                slash_help="/cameras list — list cameras visible to you",
                description=(
                    "Return the list of cameras the caller is allowed "
                    "to see, with their object labels and zones. "
                    "Cameras gated by per-camera role overrides are "
                    "redacted entirely from non-admin callers."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="latest_clips",
                slash_command="clips",
                slash_group="cameras",
                slash_help=(
                    "/cameras clips [camera] [label] — recent camera "
                    "events with snapshot/clip URLs"
                ),
                description=(
                    "Return the most-recent ended events from the "
                    "camera_events collection (descending order). "
                    "Time-window args accept ``{N}m`` / ``{N}h`` / "
                    "``{N}d`` (e.g. ``24h``, ``7d``), ``today``, "
                    "``yesterday`` (since only), or ISO 8601. Default "
                    "since=24h."
                ),
                parameters=[
                    ToolParameter(
                        name="camera",
                        type=ToolParameterType.STRING,
                        description="Filter by camera name (optional).",
                        required=False,
                    ),
                    ToolParameter(
                        name="label",
                        type=ToolParameterType.STRING,
                        description=(
                            "Filter by object label (e.g. ``person``, "
                            "``package``, ``car``). Optional."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="since",
                        type=ToolParameterType.STRING,
                        description=(
                            "Time window — ``{N}m``/``{N}h``/``{N}d``, "
                            "``today``, ``yesterday``, or ISO 8601."
                        ),
                        required=False,
                        default="24h",
                    ),
                    ToolParameter(
                        name="until",
                        type=ToolParameterType.STRING,
                        description="Upper-bound time. Optional.",
                        required=False,
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Max results (default 20, max 200).",
                        required=False,
                        default=20,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="get_snapshot",
                description=(
                    "Return the snapshot image for a camera event as "
                    "an inline attachment (preview-quality, 720px tall, "
                    "capped at 1MB raw). The text result describes "
                    "the event metadata. AI-only — humans use the "
                    "list/clips tools and click through to the proxied "
                    "URL instead."
                ),
                parameters=[
                    ToolParameter(
                        name="event_id",
                        type=ToolParameterType.STRING,
                        description=(
                            "Backend event id from a prior latest_clips "
                            "result."
                        ),
                    ),
                ],
                required_role="user",
                parallel_safe=True,
                ai_visible=True,
            ),
            ToolDefinition(
                name="who_was_seen",
                slash_command="seen",
                slash_group="cameras",
                slash_help=(
                    "/cameras seen <camera> [since] — face matches "
                    "within the window"
                ),
                description=(
                    "Deterministic — return face-recognition matches "
                    "(events with a populated ``sub_label``) for the "
                    "requested camera within the window, plus an "
                    "``unknown_count`` for unidentified person events. "
                    "No LLM call; the AI gets a clean signal it can "
                    "quote without hallucinating identity."
                ),
                parameters=[
                    ToolParameter(
                        name="camera",
                        type=ToolParameterType.STRING,
                        description="Camera name.",
                    ),
                    ToolParameter(
                        name="since",
                        type=ToolParameterType.STRING,
                        description=(
                            "Time window — same grammar as latest_clips. "
                            "Default ``today``."
                        ),
                        required=False,
                        default="today",
                    ),
                    ToolParameter(
                        name="until",
                        type=ToolParameterType.STRING,
                        description="Upper-bound. Optional.",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="count_detections",
                slash_command="count",
                slash_group="cameras",
                slash_help=(
                    "/cameras count [since] — structured counts of "
                    "detections by camera and label"
                ),
                description=(
                    "Return structured counts of detection events: "
                    "``{total, by_camera, by_label, by_camera_label}``. "
                    "Composes with ``latest_clips`` for follow-up "
                    "drill-down. Default since=24h."
                ),
                parameters=[
                    ToolParameter(
                        name="since",
                        type=ToolParameterType.STRING,
                        description=(
                            "Time window — same grammar as latest_clips."
                        ),
                        required=False,
                        default="24h",
                    ),
                    ToolParameter(
                        name="until",
                        type=ToolParameterType.STRING,
                        description="Upper-bound. Optional.",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str | ToolOutput:
        roles = _extract_roles(arguments)
        user_tz = _extract_user_tz(arguments)
        if name == "list_cameras":
            return await self._tool_list_cameras(roles)
        if name == "latest_clips":
            return await self._tool_latest_clips(
                arguments, roles, user_tz=user_tz
            )
        if name == "get_snapshot":
            return await self._tool_get_snapshot(arguments, roles)
        if name == "who_was_seen":
            return await self._tool_who_was_seen(
                arguments, roles, user_tz=user_tz
            )
        if name == "count_detections":
            return await self._tool_count_detections(
                arguments, roles, user_tz=user_tz
            )
        raise KeyError(f"Unknown tool: {name}")

    def _camera_visible_to(self, camera: str, roles: frozenset[str]) -> bool:
        """Return ``True`` iff a caller with ``roles`` can see this camera.

        Per-camera ``role_overrides`` win; otherwise
        ``default_camera_role`` applies.
        """
        required = self._effective_role(camera)
        if "admin" in roles:
            return True
        if required == "admin":
            return False
        if required == "user":
            return "user" in roles
        return True  # everyone

    async def _tool_list_cameras(self, roles: frozenset[str]) -> str:
        rows: list[dict[str, Any]] = []
        for cam in self._cameras:
            if not self._camera_visible_to(cam.name, roles):
                continue
            rows.append(
                {
                    "name": cam.name,
                    "labels": list(cam.labels),
                    "zones": list(cam.zones),
                    "role_visibility": self._effective_role(cam.name),
                    "has_audio": cam.has_audio,
                }
            )
        if not rows:
            return "No cameras visible."
        lines = [
            f"- {r['name']}: labels={r['labels']}, zones={r['zones']}, "
            f"required_role={r['role_visibility']}"
            for r in rows
        ]
        return "Cameras visible:\n" + "\n".join(lines)

    async def _tool_latest_clips(
        self,
        arguments: dict[str, Any],
        roles: frozenset[str],
        *,
        user_tz: str | None = None,
    ) -> str:
        camera = (arguments.get("camera") or "").strip() or None
        label = (arguments.get("label") or "").strip() or None
        since = self._parse_time_window(
            arguments.get("since"), default="24h", user_tz=user_tz
        )
        until = self._parse_time_window(
            arguments.get("until"), default="", user_tz=user_tz
        )
        try:
            limit = int(arguments.get("limit", 20) or 20)
        except (TypeError, ValueError):
            limit = 20

        if camera and not self._camera_visible_to(camera, roles):
            return "(no events visible for that camera)"

        events = await self.latest_events(
            camera=camera,
            label=label,
            since_ms=since,
            until_ms=until,
            limit=limit,
        )
        # Filter by per-camera role gate
        events = [
            e for e in events if self._camera_visible_to(e.camera, roles)
        ]
        if not events:
            return "(no events match)"

        lines: list[str] = []
        for e in events:
            lines.append(
                f"- {e.event_id} | {e.camera} | {e.label}"
                + (f"/{e.sub_label}" if e.sub_label else "")
                + f" | {e.started_iso} | score={e.score:.2f}"
                + (f" | clip={e.clip_url}" if e.clip_url else "")
            )
        return "Recent camera events:\n" + "\n".join(lines)

    async def _tool_get_snapshot(
        self,
        arguments: dict[str, Any],
        roles: frozenset[str],
    ) -> str | ToolOutput:
        event_id = str(arguments.get("event_id") or "").strip()
        if not event_id:
            return ToolOutput(
                text="event_id is required.", ui_blocks=[]
            )
        ev = await self.get_event(event_id)
        if ev is None:
            return ToolOutput(
                text=(
                    f"Snapshot no longer available for event "
                    f"{event_id}; try latest_clips for recent activity."
                ),
            )
        if not self._camera_visible_to(ev.camera, roles):
            return ToolOutput(
                text=(
                    "You are not authorized to view this camera's "
                    "snapshots."
                ),
            )
        snap = await self.get_snapshot_bytes(event_id, max_height=720)
        if snap is None:
            return ToolOutput(
                text=(
                    f"Snapshot no longer available for event "
                    f"{event_id}; try latest_clips for recent activity."
                ),
            )
        snap_bytes, media_type = snap
        if len(snap_bytes) > _MAX_INLINE_BYTES:
            return ToolOutput(
                text=(
                    f"Snapshot for event {event_id} is too large for "
                    f"inline display ({len(snap_bytes)} bytes). The "
                    f"clip URL {ev.clip_url} should still work."
                ),
            )

        return ToolOutput(
            text=(
                f"Snapshot for {ev.camera} at {ev.started_iso} "
                f"(label={ev.label}, score={ev.score:.2f}). "
                f"Image is preview-quality (720px tall)."
            ),
            attachments=(
                FileAttachment(
                    kind="image",
                    name=f"{ev.camera}_{ev.event_id}.jpg",
                    media_type=media_type or "image/jpeg",
                    data=base64.b64encode(snap_bytes).decode(),
                ),
            ),
        )

    async def _tool_who_was_seen(
        self,
        arguments: dict[str, Any],
        roles: frozenset[str],
        *,
        user_tz: str | None = None,
    ) -> str:
        camera = (arguments.get("camera") or "").strip()
        if not camera:
            return "camera is required."
        if not self._camera_visible_to(camera, roles):
            return "(no events visible for that camera)"
        since = self._parse_time_window(
            arguments.get("since"), default="today", user_tz=user_tz
        )
        until = self._parse_time_window(
            arguments.get("until"), default="", user_tz=user_tz
        )
        events = await self.latest_events(
            camera=camera, since_ms=since, until_ms=until, limit=200
        )

        per_name: dict[str, dict[str, Any]] = {}
        unknown_count = 0
        for e in events:
            if e.label != "person" and e.label != "face":
                continue
            if e.sub_label:
                entry = per_name.setdefault(
                    e.sub_label,
                    {
                        "name": e.sub_label,
                        "count": 0,
                        "first_seen": e.started_iso,
                        "last_seen": e.started_iso,
                    },
                )
                entry["count"] += 1
                # Iterate in time-desc order by default; first_seen
                # should be the earliest, last_seen the latest. Replace
                # only when the new value extends the range.
                if e.started_iso and e.started_iso < entry["first_seen"]:
                    entry["first_seen"] = e.started_iso
                if e.started_iso and e.started_iso > entry["last_seen"]:
                    entry["last_seen"] = e.started_iso
            elif e.label == "person":
                unknown_count += 1

        names = sorted(per_name.values(), key=lambda r: -r["count"])
        if not names and unknown_count == 0:
            return f"No person/face events on {camera} in the window."
        body = ""
        if names:
            body += "\n".join(
                f"- {n['name']}: {n['count']} (first {n['first_seen']}, last {n['last_seen']})"
                for n in names
            )
        body += f"\n- unknown_count: {unknown_count}"
        return f"Who was seen on {camera}:\n{body.strip()}"

    async def _tool_count_detections(
        self,
        arguments: dict[str, Any],
        roles: frozenset[str],
        *,
        user_tz: str | None = None,
    ) -> str:
        since = self._parse_time_window(
            arguments.get("since"), default="24h", user_tz=user_tz
        )
        until = self._parse_time_window(
            arguments.get("until"), default="", user_tz=user_tz
        )
        events = await self.latest_events(
            since_ms=since, until_ms=until, limit=2000
        )
        events = [
            e for e in events if self._camera_visible_to(e.camera, roles)
        ]
        by_camera: dict[str, int] = {}
        by_label: dict[str, int] = {}
        by_camera_label: dict[str, int] = {}
        for e in events:
            by_camera[e.camera] = by_camera.get(e.camera, 0) + 1
            by_label[e.label] = by_label.get(e.label, 0) + 1
            key = f"{e.camera}.{e.label}"
            by_camera_label[key] = by_camera_label.get(key, 0) + 1
        result = {
            "total": len(events),
            "by_camera": by_camera,
            "by_label": by_label,
            "by_camera_label": by_camera_label,
        }
        # Return parseable JSON — the AI consumes structured buckets
        # rather than parsing prose, and downstream tools can traverse
        # by_camera_label programmatically.
        return json.dumps(result)

    # ── WS handler provider ─────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, RpcHandler]:
        # Note: ``cameras.zones.update`` is intentionally NOT registered
        # here. Frigate exposes zone configuration as read-only from
        # Gilbert's perspective, so a "save zones" RPC would be a stub
        # that always 501s. Registering only the handlers we actually
        # implement keeps the public surface honest — the SPA can
        # branch on whether the frame type exists rather than parsing
        # error codes.
        return {
            "cameras.list": self._ws_cameras_list,
            "cameras.get": self._ws_cameras_get,
            "cameras.events.list": self._ws_events_list,
            "cameras.events.get": self._ws_events_get,
            "cameras.events.since": self._ws_events_since,
            "cameras.snapshots.get": self._ws_snapshot_get,
            "cameras.zones.list": self._ws_zones_list,
            "cameras.mutes.list": self._ws_mutes_list,
            "cameras.mutes.set": self._ws_mutes_set,
            "cameras.mutes.clear": self._ws_mutes_clear,
            "cameras.test_connection": self._ws_test_connection,
        }

    @staticmethod
    def _err(frame: dict[str, Any], msg: str, code: int) -> dict[str, Any]:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": msg,
            "code": code,
        }

    @staticmethod
    def _is_admin(conn: Any) -> bool:
        return getattr(conn, "user_level", 100) <= 0

    def _conn_roles(self, conn: Any) -> frozenset[str]:
        ctx = getattr(conn, "user_ctx", None)
        if ctx is not None:
            roles = getattr(ctx, "roles", None)
            if isinstance(roles, frozenset):
                return roles
            if isinstance(roles, set | list | tuple):
                return frozenset(str(r) for r in roles)
        return frozenset()

    async def _ws_cameras_list(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        roles = self._conn_roles(conn)
        rows: list[dict[str, Any]] = []
        for cam in self._cameras:
            if not self._camera_visible_to(cam.name, roles):
                continue
            rows.append(
                {
                    "name": cam.name,
                    "labels": list(cam.labels),
                    "zones": list(cam.zones),
                    "role_visibility": self._effective_role(cam.name),
                    "has_audio": cam.has_audio,
                }
            )
        return {
            "type": "cameras.list.result",
            "ref": frame.get("id"),
            "cameras": rows,
        }

    async def _ws_cameras_get(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(frame.get("name") or "")
        if not name:
            return self._err(frame, "name is required", 400)
        cam = self._cameras_by_name.get(name)
        if cam is None:
            return self._err(frame, "Camera not found", 404)
        roles = self._conn_roles(conn)
        if not self._camera_visible_to(name, roles):
            return self._err(frame, "Forbidden", 403)
        return {
            "type": "cameras.get.result",
            "ref": frame.get("id"),
            "camera": {
                "name": cam.name,
                "labels": list(cam.labels),
                "zones": list(cam.zones),
                "role_visibility": self._effective_role(cam.name),
                "has_audio": cam.has_audio,
            },
        }

    async def _ws_events_list(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if self._storage is None:
            return {
                "type": "cameras.events.list.result",
                "ref": frame.get("id"),
                "events": [],
            }
        roles = self._conn_roles(conn)
        camera = (frame.get("camera") or "").strip() or None
        label = (frame.get("label") or "").strip() or None
        since = self._parse_time_window(frame.get("since"), default="")
        until = self._parse_time_window(frame.get("until"), default="")
        try:
            limit = max(1, min(int(frame.get("limit", 50) or 50), 500))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(frame.get("offset", 0) or 0))
        except (TypeError, ValueError):
            offset = 0
        if camera and not self._camera_visible_to(camera, roles):
            return {
                "type": "cameras.events.list.result",
                "ref": frame.get("id"),
                "events": [],
            }

        filters: list[Filter] = []
        if camera:
            filters.append(Filter(field="camera", op=FilterOp.EQ, value=camera))
        if label:
            filters.append(Filter(field="label", op=FilterOp.EQ, value=label))
        if since is not None:
            filters.append(
                Filter(field="started_at", op=FilterOp.GTE, value=since)
            )
        if until is not None:
            filters.append(
                Filter(field="started_at", op=FilterOp.LTE, value=until)
            )
        rows = await self._storage.query(
            Query(
                collection=_CAMERA_EVENTS_COLLECTION,
                filters=filters,
                sort=[SortField(field="started_at", descending=True)],
                limit=limit,
                offset=offset,
            )
        )
        # Apply per-camera role filter
        rows = [r for r in rows if self._camera_visible_to(str(r.get("camera", "")), roles)]
        return {
            "type": "cameras.events.list.result",
            "ref": frame.get("id"),
            "events": rows,
        }

    async def _ws_events_get(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        event_id = str(frame.get("event_id") or "")
        if not event_id:
            return self._err(frame, "event_id is required", 400)
        if self._storage is None:
            return self._err(frame, "Storage unavailable", 503)
        row = await self._storage.get(_CAMERA_EVENTS_COLLECTION, event_id)
        if row is None:
            return self._err(frame, "Event not found", 404)
        roles = self._conn_roles(conn)
        if not self._camera_visible_to(str(row.get("camera", "")), roles):
            return self._err(frame, "Forbidden", 403)
        return {
            "type": "cameras.events.get.result",
            "ref": frame.get("id"),
            "event": row,
        }

    async def _ws_events_since(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        # Convenience handler: paginated tail since a given epoch ms.
        try:
            since_ms = int(frame.get("since_ms") or 0)
        except (TypeError, ValueError):
            since_ms = 0
        return await self._ws_events_list(
            conn,
            {
                **frame,
                "since": str(int(since_ms)),
                "type": "cameras.events.list",
            },
        )

    async def _ws_snapshot_get(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        event_id = str(frame.get("event_id") or "")
        if not event_id:
            return self._err(frame, "event_id is required", 400)
        ev = await self.get_event(event_id)
        if ev is None:
            return self._err(frame, "Event not found", 404)
        roles = self._conn_roles(conn)
        if not self._camera_visible_to(ev.camera, roles):
            return self._err(frame, "Forbidden", 403)
        snap = await self.get_snapshot_bytes(event_id, max_height=720)
        if snap is None:
            return self._err(frame, "Snapshot unavailable", 404)
        snap_bytes, media_type = snap
        return {
            "type": "cameras.snapshots.get.result",
            "ref": frame.get("id"),
            "data": base64.b64encode(snap_bytes).decode(),
            "media_type": media_type or "image/jpeg",
        }

    async def _ws_zones_list(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        roles = self._conn_roles(conn)
        rows: list[dict[str, Any]] = []
        for cam in self._cameras:
            if not self._camera_visible_to(cam.name, roles):
                continue
            rows.append({"camera": cam.name, "zones": list(cam.zones)})
        return {
            "type": "cameras.zones.list.result",
            "ref": frame.get("id"),
            "zones": rows,
        }

    async def _ws_mutes_list(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if self._storage is None:
            return {
                "type": "cameras.mutes.list.result",
                "ref": frame.get("id"),
                "mutes": [],
            }
        rows = await self._storage.query(
            Query(collection=_CAMERA_MUTES_COLLECTION)
        )
        # Drop expired entries so the UI doesn't show stale rows.
        now_ms = int(time.time() * 1000)
        live = [
            r
            for r in rows
            if int(r.get("until_ms") or 0) == 0 or int(r["until_ms"]) > now_ms
        ]
        return {
            "type": "cameras.mutes.list.result",
            "ref": frame.get("id"),
            "mutes": live,
        }

    async def _ws_mutes_set(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        # Admin-only: gating happens via DEFAULT_RPC_PERMISSIONS
        # ("cameras.": 100) plus the per-camera role filter; any user
        # with at least user-level access can mute their visible
        # cameras. Mute rows live in storage so the greeting service
        # can read them on the bus side.
        if self._storage is None:
            return self._err(frame, "Storage unavailable", 503)
        camera = str(frame.get("camera") or "")
        label = str(frame.get("label") or "")
        try:
            until_ms = int(frame.get("until_ms") or 0)
        except (TypeError, ValueError):
            until_ms = 0
        if camera and not self._camera_visible_to(
            camera, self._conn_roles(conn)
        ):
            return self._err(frame, "Forbidden", 403)
        entity_id = f"{camera or '*'}.{label or '*'}"
        await self._storage.put(
            _CAMERA_MUTES_COLLECTION,
            entity_id,
            {
                "camera": camera,
                "label": label,
                "until_ms": until_ms,
                "set_by": getattr(getattr(conn, "user_ctx", None), "user_id", ""),
                "set_at_ms": int(time.time() * 1000),
            },
        )
        return {
            "type": "cameras.mutes.set.result",
            "ref": frame.get("id"),
            "mute": {
                "camera": camera,
                "label": label,
                "until_ms": until_ms,
            },
        }

    async def _ws_mutes_clear(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if self._storage is None:
            return self._err(frame, "Storage unavailable", 503)
        camera = str(frame.get("camera") or "")
        label = str(frame.get("label") or "")
        entity_id = f"{camera or '*'}.{label or '*'}"
        await self._storage.delete(_CAMERA_MUTES_COLLECTION, entity_id)
        return {
            "type": "cameras.mutes.clear.result",
            "ref": frame.get("id"),
            "ok": True,
        }

    async def _ws_test_connection(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._is_admin(conn):
            return self._err(frame, "Admin access required", 403)
        result = await self.invoke_config_action("test_connection", {})
        return {
            "type": "cameras.test_connection.result",
            "ref": frame.get("id"),
            "status": result.status,
            "message": result.message,
            "data": result.data,
        }

    # ── Public mute query (used by greeting / other services) ───────

    async def is_camera_muted(
        self,
        camera: str,
        label: str,
        now_ms: int | None = None,
    ) -> bool:
        if self._storage is None:
            return False
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        # Match (camera, label), (camera, *), (*, label), (*, *).
        candidates = [
            f"{camera}.{label}",
            f"{camera}.*",
            f"*.{label}",
            "*.*",
        ]
        for entity_id in candidates:
            row = await self._storage.get(
                _CAMERA_MUTES_COLLECTION, entity_id
            )
            if row is None:
                continue
            until = int(row.get("until_ms") or 0)
            if until == 0 or until > ts:
                return True
        return False

    # ── Mute helper used by the AI tool on the greeting side ────────

    def build_mute_preview(
        self,
        *,
        camera: str | None,
        label: str | None,
        until_ms: int,
    ) -> ToolOutput:
        until_iso = (
            datetime.fromtimestamp(until_ms / 1000.0, tz=UTC).isoformat()
            if until_ms
            else "no end"
        )
        target = (
            f"{camera or '*all*'} / {label or '*all labels*'}"
        )
        return build_preview_output(
            tool_name="mute_camera_alerts",
            title="Mute camera alerts",
            summary=(
                f"Mute {target} until {until_iso}? Bus events still "
                f"flow; only the announcement is suppressed."
            ),
            summary_lines=[
                f"camera: {camera or 'all'}",
                f"label: {label or 'all'}",
                f"until: {until_iso}",
            ],
            arguments={
                "camera": camera or "",
                "label": label or "",
                "until_ms": until_ms,
            },
        )


# ── Helpers ─────────────────────────────────────────────────────────


def _row_to_event(row: dict[str, Any]) -> CameraEvent:
    phase_value = row.get("phase") or CameraEventPhase.ACTIVE.value
    try:
        phase = CameraEventPhase(phase_value)
    except ValueError:
        phase = CameraEventPhase.ACTIVE
    return CameraEvent(
        event_id=str(row.get("event_id") or row.get("_id") or ""),
        camera=str(row.get("camera") or ""),
        label=str(row.get("label") or ""),
        sub_label=str(row.get("sub_label") or ""),
        phase=phase,
        score=float(row.get("score") or 0.0),
        started_at=int(row.get("started_at") or 0),
        ended_at=int(row.get("ended_at") or 0),
        zones=tuple(row.get("zones") or ()),
        snapshot_url=str(row.get("snapshot_url") or ""),
        clip_url=str(row.get("clip_url") or ""),
        has_snapshot=bool(row.get("has_snapshot", False)),
        has_clip=bool(row.get("has_clip", False)),
        source_backend=str(row.get("source_backend") or ""),
        direct_snapshot_url=str(row.get("direct_snapshot_url") or ""),
        direct_clip_url=str(row.get("direct_clip_url") or ""),
    )


def _extract_roles(arguments: dict[str, Any]) -> frozenset[str]:
    """Pull the caller's roles out of the AI-injected ``_user_roles`` arg."""
    raw = arguments.get("_user_roles")
    if isinstance(raw, set | frozenset | list | tuple):
        return frozenset(str(r) for r in raw)
    return frozenset()


def _extract_user_tz(arguments: dict[str, Any]) -> str | None:
    """Pull the caller's IANA timezone from the AI-injected ``_user_tz``.

    AI service.py injects ``arguments["_user_tz"] = user_ctx.tz`` when
    the caller has a timezone configured. Returning ``None`` here means
    "fall back to UTC for date math" — the caller may have no TZ set.
    """
    raw = arguments.get("_user_tz")
    if isinstance(raw, str) and raw:
        return raw
    return None


__all__ = [
    "CameraEventService",
]
