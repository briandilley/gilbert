"""Unit tests for CameraEventService — lifecycle, persistence, role gates,
vision annotation, retention sweep, AI tools, WS RPCs.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.camera import CameraEventService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.camera import (
    CameraBackendError,
    CameraEvent,
    CameraEventBackend,
    CameraEventPhase,
    CameraInfo,
    SnapshotRef,
)
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.scheduler import Schedule
from gilbert.interfaces.storage import (
    Query,
    StorageBackend,
)
from gilbert.storage.sqlite import SQLiteStorage

# ── Test backend ────────────────────────────────────────────────────


class _FakeBackend(CameraEventBackend):
    """Configurable fake backend for service tests.

    Set ``connect_errors`` to a list of ``CameraBackendError`` instances
    that should be raised on each successive ``connect()`` attempt; an
    empty list (or exhausting the list) means "succeed."
    Set ``events`` to the sequence of ``CameraEvent`` to yield from
    ``stream_events``. The iterator blocks (returning to the queue) once
    the events are exhausted until ``disconnect()`` is called.
    """

    backend_name = ""  # don't register globally — test fakes

    def __init__(
        self,
        cameras: list[CameraInfo] | None = None,
        events: list[CameraEvent] | None = None,
    ) -> None:
        self._cameras = cameras or []
        self._events = list(events or [])
        self.snapshot_ref: SnapshotRef | None = SnapshotRef(
            data=b"fakejpeg", media_type="image/jpeg"
        )
        self.connect_errors: list[Exception] = []
        self.connect_calls: int = 0
        self.disconnect_calls: int = 0
        self.close_calls: int = 0
        self._stop = asyncio.Event()
        self._connected = False

    async def initialize(self, config: dict[str, object]) -> None:
        pass

    async def close(self) -> None:
        self.close_calls += 1
        self._stop.set()

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        self._stop.clear()
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._stop.set()
        self._connected = False

    async def stream_events(self) -> AsyncIterator[CameraEvent]:
        for ev in self._events:
            yield ev
        # Once buffered events are exhausted, wait for disconnect()
        # before exiting the iterator.
        await self._stop.wait()

    async def list_cameras(self) -> list[CameraInfo]:
        return list(self._cameras)

    async def get_snapshot(
        self,
        camera: str,
        event_id: str | None = None,
        *,
        max_height: int | None = None,
    ) -> SnapshotRef | None:
        return self.snapshot_ref

    async def get_clip_url(self, event_id: str) -> str | None:
        return f"http://fake/clip/{event_id}"


# ── Fixtures ────────────────────────────────────────────────────────


class _StorageProvider:
    """Satisfies ``StorageProvider`` Protocol for the fake resolver."""

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        raise NotImplementedError


class _BusProvider:
    """Satisfies ``EventBusProvider`` Protocol."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> EventBus:
        return self._bus


class _SchedulerProvider:
    """Satisfies ``SchedulerProvider`` Protocol with synchronous bookkeeping."""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}

    def add_job(
        self,
        name: str,
        schedule: Schedule,
        callback: Any,
        system: bool = False,
        enabled: bool = True,
        owner: str = "",
    ) -> Any:
        self.jobs[name] = callback
        return None

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return []

    def get_job(self, name: str) -> Any:
        return None

    async def run_now(self, name: str) -> None:
        cb = self.jobs.get(name)
        if cb is not None:
            await cb()


class _Resolver:
    """Duck-typed resolver — services get whatever we drop into caps."""

    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, capability: str) -> Any:
        return self.caps.get(capability)

    def require_capability(self, capability: str) -> Any:
        svc = self.caps.get(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Any]:
        svc = self.caps.get(capability)
        return [svc] if svc is not None else []


class _ConfigReader:
    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self._sections = sections

    def get(self, path: str) -> Any:
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return dict(self._sections.get(namespace, {}))

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        return dict(self._sections.get(namespace, {}))

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}


@pytest.fixture
async def storage(tmp_path: Path) -> SQLiteStorage:
    db = SQLiteStorage(str(tmp_path / "test.db"))
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def event_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture(autouse=True)
def _reset_camera_backend_registry():
    """Snapshot/restore the registry so test fakes don't leak."""
    snapshot = dict(CameraEventBackend._registry)
    yield
    CameraEventBackend._registry.clear()
    CameraEventBackend._registry.update(snapshot)


def _make_resolver(
    *,
    storage: StorageBackend,
    bus: EventBus,
    cameras_section: dict[str, Any] | None = None,
    scheduler: _SchedulerProvider | None = None,
    vision: Any = None,
) -> _Resolver:
    r = _Resolver()
    r.caps["entity_storage"] = _StorageProvider(storage)
    r.caps["event_bus"] = _BusProvider(bus)
    r.caps["configuration"] = _ConfigReader({"cameras": cameras_section or {}})
    if scheduler is not None:
        r.caps["scheduler"] = scheduler
    if vision is not None:
        r.caps["vision"] = vision
    return r


def _register_fake(name: str, factory: Any) -> type[CameraEventBackend]:
    """Build & register a backend class with a known name + factory."""

    class _Registered(_FakeBackend):
        backend_name = name

        def __init__(self) -> None:
            super().__init__(**factory())

    # Force registration (the autouse fixture restores it)
    CameraEventBackend._registry[name] = _Registered
    return _Registered


# ── Lifecycle ───────────────────────────────────────────────────────


async def test_starts_disabled_when_config_off(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": False},
    )
    await svc.start(resolver)
    assert svc._enabled is False
    assert svc._stream_task is None
    assert svc._backend is None


async def test_starts_with_unknown_backend_logs_and_no_op(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "no-such-backend"},
    )
    await svc.start(resolver)
    assert svc._enabled is False
    assert svc._stream_task is None


async def test_publishes_detected_and_glob_event(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    received: list[Event] = []

    async def collector(e: Event) -> None:
        received.append(e)

    event_bus.subscribe_pattern("camera.*", collector)

    ev = CameraEvent(
        event_id="evt-1",
        camera="front_door",
        label="person",
        phase=CameraEventPhase.ACTIVE,
        score=0.9,
        started_at=int(time.time() * 1000),
        has_snapshot=True,
        source_backend="fake",
    )
    _register_fake("fake1", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake1"},
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()

    types = [e.event_type for e in received]
    assert "camera.event.detected" in types
    assert "camera.person.detected.front_door" in types


async def _async_noop() -> None:
    return None


async def test_does_not_publish_glob_on_ended(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    event_bus.subscribe_pattern("camera.*", handler)

    ev = CameraEvent(
        event_id="evt-2",
        camera="back_yard",
        label="dog",
        phase=CameraEventPhase.ENDED,
        score=0.7,
        started_at=int(time.time() * 1000),
        has_snapshot=False,
        source_backend="fake",
    )
    _register_fake("fake2", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake2"},
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()
    types = [e.event_type for e in received]
    assert "camera.event.ended" in types
    # No glob companion for ENDED
    assert not any(t.startswith("camera.dog.detected.") for t in types)


async def test_glob_emission_skipped_for_unsafe_label_or_camera(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    event_bus.subscribe_pattern("camera.*", handler)

    ev = CameraEvent(
        event_id="evt-3",
        camera="front door",  # whitespace — unsafe
        label="person",
        phase=CameraEventPhase.ACTIVE,
        started_at=int(time.time() * 1000),
        source_backend="fake",
    )
    _register_fake("fake3", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake3"},
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()
    types = [e.event_type for e in received]
    assert "camera.event.detected" in types
    assert not any(".detected." in t and t != "camera.event.detected" for t in types)


async def test_persists_event_to_camera_events_collection(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    ev = CameraEvent(
        event_id="evt-persist",
        camera="porch",
        label="package",
        phase=CameraEventPhase.ACTIVE,
        started_at=1700000000000,
        has_snapshot=True,
        source_backend="fake",
    )
    _register_fake("fake4", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake4"},
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()

    rows = await storage.query(Query(collection="camera_events"))
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "evt-persist"
    assert row["snapshot_url"] == "/api/cameras/events/evt-persist/snapshot.jpg"
    assert "started_iso" not in row
    assert "vision_model" not in row


async def test_persists_proxied_urls_not_raw_frigate_urls(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    ev = CameraEvent(
        event_id="evt-proxied",
        camera="driveway",
        label="car",
        phase=CameraEventPhase.ENDED,
        started_at=1700000000000,
        ended_at=1700000010000,
        snapshot_url="http://frigate.local:5000/api/events/evt-proxied/snapshot.jpg",
        clip_url="http://frigate.local:5000/api/events/evt-proxied/clip.mp4",
        has_snapshot=True,
        has_clip=True,
        source_backend="fake",
    )
    _register_fake("fake5", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake5"},
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()

    rows = await storage.query(Query(collection="camera_events"))
    row = rows[0]
    assert row["snapshot_url"].startswith("/api/cameras/events/")
    assert row["clip_url"].startswith("/api/cameras/events/")


async def test_retention_sweep_deletes_old_rows(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 30 * 86400 * 1000  # 30 days old
    await storage.put(
        "camera_events",
        "old-1",
        {"event_id": "old-1", "camera": "a", "started_at": old_ms},
    )
    await storage.put(
        "camera_events",
        "fresh-1",
        {"event_id": "fresh-1", "camera": "a", "started_at": now_ms},
    )
    _register_fake("fake6", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={
            "enabled": True,
            "backend": "fake6",
            "retention_days": 7,
        },
    )
    await svc.start(resolver)
    await svc._sweep_old_camera_events()
    rows = await storage.query(Query(collection="camera_events"))
    ids = {r["event_id"] for r in rows}
    assert ids == {"fresh-1"}
    await svc.stop()


async def test_annotation_off_path_when_label_not_in_vision_enabled_labels(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    ev = CameraEvent(
        event_id="evt-anno-off",
        camera="porch",
        label="person",  # not in default ["package"]
        phase=CameraEventPhase.ACTIVE,
        started_at=int(time.time() * 1000),
        has_snapshot=True,
        source_backend="fake",
    )
    vision_calls: list[bytes] = []

    class _Vision:
        async def describe_image(self, b: bytes, m: str) -> str:
            vision_calls.append(b)
            return "should not be called"

    _register_fake("fake7", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake7"},
        vision=_Vision(),
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()
    assert vision_calls == []


async def test_annotation_runs_with_vision_provider(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    annotated: list[Event] = []

    async def annotated_handler(e: Event) -> None:
        annotated.append(e)

    event_bus.subscribe("camera.snapshot.annotated", annotated_handler)

    ev = CameraEvent(
        event_id="evt-anno-on",
        camera="porch",
        label="package",
        phase=CameraEventPhase.ACTIVE,
        started_at=int(time.time() * 1000),
        has_snapshot=True,
        source_backend="fake",
    )

    class _Vision:
        async def describe_image(self, b: bytes, m: str) -> str:
            return "a brown box on the porch"

    _register_fake("fake8", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake8"},
        vision=_Vision(),
    )
    await svc.start(resolver)
    # Give the annotation task time to run.
    for _ in range(20):
        if annotated:
            break
        await asyncio.sleep(0.05)
    await svc.stop()
    assert annotated, "annotation event was not published"
    data = annotated[0].data
    assert "vision_model" not in data
    assert data["vision_text"] == "a brown box on the porch"

    rows = await storage.query(Query(collection="camera_events"))
    assert rows[0]["vision_text"] == "a brown box on the porch"


async def test_annotation_lock_prevents_duplicate(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    call_count = 0

    class _Vision:
        async def describe_image(self, b: bytes, m: str) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return "described"

    _register_fake("fake9", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake9"},
        vision=_Vision(),
    )
    await svc.start(resolver)

    ev = CameraEvent(
        event_id="evt-lock",
        camera="porch",
        label="package",
        phase=CameraEventPhase.ACTIVE,
        started_at=int(time.time() * 1000),
        has_snapshot=True,
        source_backend="fake",
    )
    # Persist first so the annotation task's existing-row check works.
    await svc._persist_event(svc._stamp_proxied_urls(ev))
    # Spawn two concurrent annotations for the same event id.
    await asyncio.gather(
        svc._annotate_event(ev),
        svc._annotate_event(ev),
    )
    await svc.stop()
    assert call_count == 1


async def test_camera_role_override_filters_user_tool_call(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    # Pre-seed events for both cameras
    now_ms = int(time.time() * 1000)
    await storage.put(
        "camera_events",
        "ev-a",
        {
            "event_id": "ev-a",
            "camera": "front_door",
            "label": "person",
            "started_at": now_ms,
            "score": 0.5,
            "phase": "active",
        },
    )
    await storage.put(
        "camera_events",
        "ev-b",
        {
            "event_id": "ev-b",
            "camera": "bedroom",
            "label": "person",
            "started_at": now_ms,
            "score": 0.5,
            "phase": "active",
        },
    )
    _register_fake("fake10", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={
            "enabled": True,
            "backend": "fake10",
            "role_overrides": {"bedroom": "admin"},
        },
    )
    await svc.start(resolver)

    user_result = await svc.execute_tool(
        "latest_clips", {"_user_roles": ["user"]}
    )
    admin_result = await svc.execute_tool(
        "latest_clips", {"_user_roles": ["admin"]}
    )
    assert "ev-a" in str(user_result)
    assert "ev-b" not in str(user_result)
    assert "ev-b" in str(admin_result)
    await svc.stop()


async def test_required_role_lands_in_event_data(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    received: list[Event] = []

    async def handler(e: Event) -> None:
        if e.event_type == "camera.event.detected":
            received.append(e)

    event_bus.subscribe("camera.event.detected", handler)
    ev = CameraEvent(
        event_id="evt-role",
        camera="bedroom",
        label="person",
        phase=CameraEventPhase.ACTIVE,
        started_at=int(time.time() * 1000),
        source_backend="fake",
    )
    _register_fake("fake11", lambda: {"events": [ev]})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={
            "enabled": True,
            "backend": "fake11",
            "role_overrides": {"bedroom": "admin"},
        },
    )
    await svc.start(resolver)
    await asyncio.sleep(0.05)
    await svc.stop()
    assert received
    assert received[0].data["required_role"] == "admin"


async def test_get_snapshot_tool_returns_attachment(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    await storage.put(
        "camera_events",
        "snap-evt",
        {
            "event_id": "snap-evt",
            "camera": "porch",
            "label": "package",
            "started_at": now_ms,
            "has_snapshot": True,
            "phase": "active",
            "source_backend": "fake",
        },
    )
    _register_fake("fake12", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake12"},
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "get_snapshot",
        {"event_id": "snap-evt", "_user_roles": ["user"]},
    )
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    assert out.attachments
    att = out.attachments[0]
    assert att.kind == "image"
    # base64-decoded fakejpeg
    assert base64.b64decode(att.data) == b"fakejpeg"
    await svc.stop()


async def test_get_snapshot_returns_error_when_backend_404(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    await storage.put(
        "camera_events",
        "missing-snap",
        {
            "event_id": "missing-snap",
            "camera": "porch",
            "label": "package",
            "started_at": now_ms,
            "has_snapshot": True,
            "phase": "active",
        },
    )
    _register_fake("fake13", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake13"},
    )
    await svc.start(resolver)
    # Tell the backend to return None
    assert svc._backend is not None
    svc._backend.snapshot_ref = None  # type: ignore[attr-defined]
    out = await svc.execute_tool(
        "get_snapshot",
        {"event_id": "missing-snap", "_user_roles": ["user"]},
    )
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    assert "no longer available" in out.text
    assert not out.attachments
    await svc.stop()


async def test_get_snapshot_caps_max_inline_bytes(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    await storage.put(
        "camera_events",
        "huge-snap",
        {
            "event_id": "huge-snap",
            "camera": "porch",
            "label": "package",
            "started_at": now_ms,
            "has_snapshot": True,
            "phase": "active",
        },
    )
    _register_fake("fake14", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake14"},
    )
    await svc.start(resolver)
    assert svc._backend is not None
    svc._backend.snapshot_ref = SnapshotRef(  # type: ignore[attr-defined]
        data=b"x" * 2_000_000, media_type="image/jpeg"
    )
    out = await svc.execute_tool(
        "get_snapshot",
        {"event_id": "huge-snap", "_user_roles": ["user"]},
    )
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    assert "too large" in out.text
    assert not out.attachments
    await svc.stop()


async def test_who_was_seen_returns_face_matches_and_unknown_count(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    for i, sub in enumerate(["jeff", "", "", "", ""]):
        await storage.put(
            "camera_events",
            f"who-{i}",
            {
                "event_id": f"who-{i}",
                "camera": "front",
                "label": "person",
                "sub_label": sub,
                "started_at": now_ms - i * 60_000,
                "phase": "active",
                "score": 0.9,
            },
        )
    _register_fake("fake15", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake15"},
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "who_was_seen",
        {"camera": "front", "since": "today", "_user_roles": ["user"]},
    )
    assert "jeff" in str(out)
    assert "unknown_count: 4" in str(out)
    await svc.stop()


async def test_count_detections_returns_structured_buckets(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    seed = [
        ("front_door", "person"),
        ("front_door", "person"),
        ("front_door", "package"),
        ("driveway", "car"),
        ("driveway", "person"),
    ]
    for i, (cam, label) in enumerate(seed):
        await storage.put(
            "camera_events",
            f"cnt-{i}",
            {
                "event_id": f"cnt-{i}",
                "camera": cam,
                "label": label,
                "started_at": now_ms - i * 60_000,
                "phase": "active",
                "score": 0.5,
            },
        )
    _register_fake("fake16", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake16"},
    )
    await svc.start(resolver)
    out = await svc.execute_tool(
        "count_detections",
        {"since": "24h", "_user_roles": ["user"]},
    )
    assert "total=5" in str(out)
    assert "front_door" in str(out)
    assert "package" in str(out)
    await svc.stop()


async def test_reconnect_calls_backend_connect_each_cycle(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    """Service retries by calling backend.connect() again after each error.

    Drive the loop directly (rather than relying on the
    ``asyncio.sleep`` backoff which can take seconds) by invoking the
    consumer's per-iteration helpers via small, controlled steps.
    """

    class _AlwaysFailing(_FakeBackend):
        backend_name = "fake17"

        def __init__(self) -> None:
            super().__init__(events=[])

        async def connect(self) -> None:
            self.connect_calls += 1
            raise CameraBackendError(
                f"fail {self.connect_calls}"
            )

    CameraEventBackend._registry["fake17"] = _AlwaysFailing
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={
            "enabled": True,
            "backend": "fake17",
            # Tiny cap on backoff so the loop spins fast enough for
            # the test deadline.
            "reconnect_max_seconds": 0.01,
        },
    )
    await svc.start(resolver)
    # Override the cached backoff cap on the service before the loop
    # ramps up — the loop reads ``self._reconnect_max_seconds`` each
    # cycle, but the initial backoff starts at 1.0s. Set it down.
    svc._reconnect_max_seconds = 0.01

    # Wait long enough for at least two connect attempts. The first
    # connect is at t=0 (failure -> sleep backoff -> reconnect). With
    # backoff capped at 0.01s the second attempt should happen well
    # within 1.5s.
    for _ in range(60):
        if svc._backend is not None and svc._backend.connect_calls >= 2:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.05)
    assert svc._backend is not None
    assert svc._backend.connect_calls >= 2  # type: ignore[attr-defined]
    await svc.stop()


async def test_stop_cancels_stream_task_promptly(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    _register_fake("fake18", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake18"},
    )
    await svc.start(resolver)
    await asyncio.sleep(0.02)
    await svc.stop()
    assert svc._stream_task is None
    assert svc._backend is None


async def test_concurrent_user_calls_isolate_roles(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    await storage.put(
        "camera_events",
        "iso-a",
        {
            "event_id": "iso-a",
            "camera": "lobby",
            "label": "person",
            "started_at": now_ms,
            "phase": "active",
            "score": 0.5,
        },
    )
    await storage.put(
        "camera_events",
        "iso-b",
        {
            "event_id": "iso-b",
            "camera": "vault",
            "label": "person",
            "started_at": now_ms,
            "phase": "active",
            "score": 0.5,
        },
    )
    _register_fake("fake19", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={
            "enabled": True,
            "backend": "fake19",
            "role_overrides": {"vault": "admin"},
        },
    )
    await svc.start(resolver)

    async def call_user() -> str:
        out = await svc.execute_tool(
            "latest_clips", {"_user_roles": ["user"]}
        )
        return str(out)

    async def call_admin() -> str:
        out = await svc.execute_tool(
            "latest_clips", {"_user_roles": ["admin"]}
        )
        return str(out)

    user_out, admin_out = await asyncio.gather(call_user(), call_admin())
    assert "iso-a" in user_out
    assert "iso-b" not in user_out
    assert "iso-b" in admin_out
    await svc.stop()


# ── WS RPCs ─────────────────────────────────────────────────────────


def _conn(level: int, roles: set[str]) -> Any:
    """Build a stand-in connection object for WS handler tests."""

    class _Conn:
        user_level = level
        user_ctx = UserContext(
            user_id="u", email="", display_name="", roles=frozenset(roles)
        )

    return _Conn()


async def test_cameras_events_list_rpc_filters_by_role(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    now_ms = int(time.time() * 1000)
    await storage.put(
        "camera_events",
        "rpc-a",
        {
            "event_id": "rpc-a",
            "camera": "front_door",
            "label": "person",
            "started_at": now_ms,
            "phase": "active",
            "score": 0.5,
        },
    )
    await storage.put(
        "camera_events",
        "rpc-b",
        {
            "event_id": "rpc-b",
            "camera": "vault",
            "label": "person",
            "started_at": now_ms,
            "phase": "active",
            "score": 0.5,
        },
    )
    _register_fake("fake20", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={
            "enabled": True,
            "backend": "fake20",
            "role_overrides": {"vault": "admin"},
        },
    )
    await svc.start(resolver)

    handler = svc.get_ws_handlers()["cameras.events.list"]
    out = await handler(_conn(100, {"user"}), {"id": "1"})
    assert isinstance(out, dict)
    ids = [e["event_id"] for e in out["events"]]
    assert "rpc-a" in ids
    assert "rpc-b" not in ids

    out = await handler(_conn(0, {"admin"}), {"id": "2"})
    ids = [e["event_id"] for e in out["events"]]
    assert "rpc-b" in ids
    await svc.stop()


async def test_cameras_test_connection_requires_admin(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    _register_fake("fake21", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake21"},
    )
    await svc.start(resolver)
    handler = svc.get_ws_handlers()["cameras.test_connection"]
    out = await handler(_conn(100, {"user"}), {"id": "1"})
    assert isinstance(out, dict)
    assert out.get("type") == "gilbert.error"
    assert out.get("code") == 403
    await svc.stop()


# ── Mute helpers ────────────────────────────────────────────────────


async def test_is_camera_muted_matches_wildcards(
    storage: SQLiteStorage, event_bus: InMemoryEventBus
) -> None:
    _register_fake("fake22", lambda: {"events": []})
    svc = CameraEventService()
    resolver = _make_resolver(
        storage=storage,
        bus=event_bus,
        cameras_section={"enabled": True, "backend": "fake22"},
    )
    await svc.start(resolver)
    handler = svc.get_ws_handlers()["cameras.mutes.set"]
    until = int((time.time() + 3600) * 1000)
    await handler(
        _conn(0, {"admin"}),
        {
            "id": "1",
            "camera": "porch",
            "label": "person",
            "until_ms": until,
        },
    )
    assert await svc.is_camera_muted("porch", "person")
    assert not await svc.is_camera_muted("porch", "package")
    # Wildcard label
    await handler(
        _conn(0, {"admin"}),
        {
            "id": "2",
            "camera": "porch",
            "label": "",  # wildcard
            "until_ms": until,
        },
    )
    assert await svc.is_camera_muted("porch", "package")
    await svc.stop()
