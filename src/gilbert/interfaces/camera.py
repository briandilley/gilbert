"""Camera event backend interface — object detection + snapshot/clip retrieval.

Mirrors ``DoorbellBackend`` in shape but is built around a long-lived
event stream (push) rather than polling. Backends that can't push (e.g.
Reolink AI events via HTTP polling) implement ``stream_events`` as an
adapter over ``asyncio.Queue`` fed from a polling task.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam

_logger = logging.getLogger(__name__)


class CameraEventPhase(StrEnum):
    """Lifecycle phase of a detection event.

    Frigate emits ``new`` (object enters frame), ``update`` (score /
    snapshot updated mid-event), and ``end`` (object left frame /
    timeout). Backends collapse ``new`` + ``update`` into ``ACTIVE``
    (with internal dedup so the bus isn't hammered on every score
    tick); ``end`` becomes ``ENDED``.
    """

    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class CameraInfo:
    """Static metadata about a single camera known to a backend."""

    name: str
    """Camera identifier (matches the topic prefix and URL slug)."""
    labels: tuple[str, ...] = ()
    """Object labels this camera is configured to detect (e.g.
    ``("person", "car", "package")``). Empty when the backend can't
    report this — callers should treat empty as "unknown, accept any
    label."
    """
    zones: tuple[str, ...] = ()
    """Named zones configured on this camera (e.g. ``("porch",
    "driveway")``)."""
    has_audio: bool = False
    has_ptz: bool = False
    snapshot_supported: bool = True
    clip_supported: bool = True


@dataclass(frozen=True)
class CameraEvent:
    """A single detection event from a camera.

    Backends produce these; the service publishes them on the bus and
    persists them. All timestamps are epoch milliseconds (UTC) for
    consistency with ``RingEvent`` and direct comparability via ``int``
    arithmetic — service callers that want ISO format use the
    ``started_iso`` / ``ended_iso`` helper properties below.
    """

    event_id: str
    camera: str = ""
    label: str = ""
    sub_label: str = ""
    """Optional sub-classification — for ``"face"`` events this carries
    the recognized identity. Empty for unknown / generic detections."""
    phase: CameraEventPhase = CameraEventPhase.ACTIVE
    score: float = 0.0
    """Confidence 0..1. For ``ENDED`` events, the *top* score over the
    event's lifetime."""
    started_at: int = 0
    """Epoch ms at first frame in event."""
    ended_at: int = 0
    """Epoch ms at last frame in event (only set when ``phase == ENDED``)."""
    zones: tuple[str, ...] = ()
    snapshot_url: str = ""
    """HTTP(S) URL pointing at the event's best snapshot. The service
    sets this to a Gilbert-proxied path (``/api/cameras/events/<id>/
    snapshot.jpg``); raw backend URLs live on ``direct_snapshot_url``.
    Empty when no snapshot is available."""
    clip_url: str = ""
    """HTTP(S) URL pointing at the recorded clip. Gilbert-proxied path
    once the service stamps it; empty until the backend has finalized
    the clip (typically only on ``ENDED``)."""
    has_snapshot: bool = False
    has_clip: bool = False
    source_backend: str = ""
    """``backend_name`` of the producing backend; set by the backend so
    the service can stamp the bus event with provenance."""
    direct_snapshot_url: str = ""
    """Raw backend snapshot URL (LAN-only). Operators who know what
    they're doing can use this; bus consumers prefer
    ``snapshot_url`` (the Gilbert-proxied path)."""
    direct_clip_url: str = ""
    """Raw backend clip URL (LAN-only). Same caveat as
    ``direct_snapshot_url``."""
    raw: Mapping[str, object] = field(default_factory=dict)
    """Original backend payload as a read-only mapping (typing-only;
    backends pass a plain dict and consumers SHOULD treat it as
    immutable). Debug-only and forward-compatible — every legitimate
    consumer reads typed attributes. Not serialized into the bus event
    or persisted form by default."""

    @property
    def started_iso(self) -> str:
        return _epoch_ms_to_iso(self.started_at)

    @property
    def ended_iso(self) -> str:
        return _epoch_ms_to_iso(self.ended_at)

    @property
    def duration_seconds(self) -> float:
        if self.ended_at and self.started_at:
            return max(0.0, (self.ended_at - self.started_at) / 1000.0)
        return 0.0


@dataclass(frozen=True)
class SnapshotRef:
    """Discriminated container for a snapshot — URL or bytes.

    Exactly one of ``url`` and ``data`` should be non-empty.
    ``media_type`` is required when ``data`` is set.
    """

    url: str = ""
    data: bytes = b""
    media_type: str = ""

    @property
    def is_inline(self) -> bool:
        return bool(self.data)


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    if not epoch_ms:
        return ""
    try:
        return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC).isoformat()
    except (ValueError, OSError):
        return ""


class CameraBackendError(Exception):
    """Raised by a backend when its event stream has terminally failed.

    Reconnect retries exhausted, auth permanently rejected, etc. The
    service catches this, publishes ``camera.backend.disconnected``,
    and may schedule a re-connect based on its own retry policy.
    """


class CameraEventBackend(ABC):
    """Abstract camera/object-detection backend.

    **Lifecycle variant — streaming.** This ABC departs from the
    standard ``initialize / close`` lifecycle that polling backends
    (``DoorbellBackend``, ``PresenceBackend``, ``TTSBackend``, …) use.
    Camera backends are *push*-driven and additionally implement
    ``connect / disconnect / stream_events``. The split exists so the
    service can probe config (``test_connection``) via ``initialize``
    without starting the firehose.

    1. ``initialize(config)`` — connect to the broker / API, do any
       handshake. Must NOT start streaming yet — ``connect()`` does that.
    2. ``connect()`` — begin streaming. Returns once the connection is
       established (or raises). The backend is now obligated to drive
       events into ``stream_events()`` until ``disconnect()`` is called.
    3. ``stream_events()`` — async iterator over ``CameraEvent``.
       Implemented as ``async def stream_events(self) ->
       AsyncIterator[CameraEvent]: yield ...`` — i.e., an *async
       generator function* (typed return is ``AsyncIterator``; the
       runtime object is an ``AsyncGenerator``). Yields on every state
       change. Iterator stops cleanly when ``disconnect()`` is called.
    4. ``disconnect()`` — stop streaming, close connections, but the
       instance remains reusable (call ``connect()`` again to resume).
    5. ``close()`` — full teardown, release HTTP clients, wipe state.
       Backend instance should not be used after this.

    Future polling-style camera backends SHOULD still subclass
    ``CameraEventBackend`` and implement ``stream_events`` as an adapter
    over an internal ``asyncio.Queue`` fed by a polling task — that way
    the service interface stays uniform.
    """

    _registry: dict[str, type[CameraEventBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Defensive: every concrete subclass must declare ``backend_name``
        # explicitly, otherwise an unnamed subclass silently registers
        # as the empty string and overwrites any prior unnamed
        # registration. Test fakes and intermediate abstract subclasses
        # can leave it empty (they don't end up in the registry).
        if cls.backend_name:
            existing = CameraEventBackend._registry.get(cls.backend_name)
            if existing is not None and existing is not cls:
                _logger.warning(
                    "CameraEventBackend %r already registered as %s; "
                    "overwriting with %s",
                    cls.backend_name,
                    existing.__name__,
                    cls.__name__,
                )
            CameraEventBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[CameraEventBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def connect(self) -> None:
        """Open the live event stream. Idempotent: if the backend is
        already connected, returns without reopening. After a transport
        error (where the service caught a ``CameraBackendError``), the
        backend is in a disconnected state and ``connect()`` MUST
        reopen the underlying broker/API connection."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the live event stream. Idempotent: a second call on
        an already-disconnected backend MUST NOT raise.

        Must terminate any in-flight ``stream_events()`` iterators
        cleanly (e.g. by closing the underlying queue / cancelling the
        consumer task).
        """
        ...

    @abstractmethod
    def stream_events(self) -> AsyncIterator[CameraEvent]:
        """Yield ``CameraEvent``s as the backend produces them.

        Implementations write this as an async generator:
        ``async def stream_events(self) -> AsyncIterator[CameraEvent]:
        while ...: yield event``. The typed return is
        ``AsyncIterator[CameraEvent]``; the runtime object is the
        ``AsyncGenerator`` produced by the generator function.

        MUST be safe to call exactly once per ``connect()`` cycle. The
        service will not subscribe twice. If the backend supports
        fanout, it can wrap a broadcast queue internally.

        Stops cleanly when ``disconnect()`` is called or when the
        underlying transport is permanently lost (and reconnect retries
        are exhausted) — in the latter case the backend SHOULD raise a
        ``CameraBackendError`` so the service can surface a
        ``camera.backend.disconnected`` event.
        """
        ...

    @abstractmethod
    async def list_cameras(self) -> list[CameraInfo]:
        """Static metadata about every camera the backend can see.

        Called on ``start()`` and on a periodic refresh timer. Backends
        that need to authenticate to enumerate cameras should cache the
        result internally and refresh on a slower cadence.
        """
        ...

    @abstractmethod
    async def get_snapshot(
        self,
        camera: str,
        event_id: str | None = None,
        *,
        max_height: int | None = None,
    ) -> SnapshotRef | None:
        """Fetch a snapshot.

        - ``event_id is None`` → live snapshot of the camera.
        - ``event_id`` → the historic snapshot for that event (must
          have been emitted by ``stream_events()`` previously, or
          retrievable from the backend's history).
        - ``max_height`` is an optional server-side downscale request;
          backends that don't support it can ignore the kwarg.

        Returns a ``SnapshotRef`` so callers can decide whether to
        embed the bytes or proxy via URL. ``None`` means the snapshot
        is no longer available (e.g. expired in the backend's
        retention).
        """
        ...

    @abstractmethod
    async def get_clip_url(self, event_id: str) -> str | None:
        """Return a URL to the event's clip, or ``None`` if unavailable.

        The URL may require auth headers exposed via
        ``backend_auth_headers()``. Service callers proxy via a Gilbert
        HTTP route that adds those headers transparently — the URL
        never gets handed to the browser raw if it needs auth.
        """
        ...

    def backend_auth_headers(self) -> dict[str, str]:
        """Optional auth headers for direct HTTP fetches of
        snapshot/clip URLs. Default: no auth. Override when the backend
        serves media behind a token."""
        return {}


@runtime_checkable
class CameraProvider(Protocol):
    """Capability protocol for the camera service.

    Other services (greeting, agent tools, future plugins) ``isinstance``-
    check against this protocol rather than the concrete
    ``CameraEventService``.
    """

    async def list_cameras(self) -> list[CameraInfo]: ...

    async def latest_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 20,
    ) -> list[CameraEvent]: ...

    async def get_event(self, event_id: str) -> CameraEvent | None: ...

    async def get_snapshot_bytes(
        self,
        event_id: str,
        *,
        max_height: int | None = 720,
    ) -> tuple[bytes, str] | None:
        """Return ``(bytes, media_type)`` for an event's snapshot, or
        ``None`` if not available. ``max_height`` requests a
        server-side downscale where the backend supports it (Frigate
        honors ``?h=<n>``); pass ``None`` for full resolution."""
        ...


@runtime_checkable
class AvailableCameraLister(Protocol):
    """Protocol for anything that can report the currently-known camera
    names. Used by ``ConfigurationService._resolve_dynamic_choices`` to
    populate the ``cameras`` dropdown on settings pages without
    duck-typing the service instance.
    """

    @property
    def available_cameras(self) -> list[str]:
        """Names of cameras currently known to the service."""
        ...
