"""Tests for the Gilbert-proxied camera media HTTP endpoints.

The endpoints live at ``src/gilbert/web/routes/cameras.py`` and proxy
snapshot.jpg + clip.mp4 requests to the underlying camera backend
(typically Frigate). The browser hits the authenticated Gilbert URL
and Gilbert injects ``backend_auth_headers()`` server-side, so the
upstream bearer token / session cookie never reaches the browser.

These tests spin up a minimal FastAPI app with a fake camera service
+ a fake camera backend, hit the routes via Starlette's TestClient
(plus an httpx ``MockTransport`` for the upstream clip fetch), and
verify:

- Snapshot happy path returns JPEG bytes + ``image/jpeg`` content-type
  + private cache header.
- Snapshot 404 when the event isn't known.
- Snapshot 403 when the user role is below the camera's required role
  (default-role gating).
- Snapshot 403 when a per-camera role override gates the user out.
- Snapshot requests pass ``max_height=720`` to the service.
- Clip happy path returns video bytes from the upstream Frigate URL.
- Clip Range header forwards through to the upstream and the partial
  response is returned with status 206 + headers.
- Clip 404 when no upstream clip URL is available.
- ``backend_auth_headers()`` is injected into the upstream call.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.camera import (
    CameraEvent,
    CameraEventPhase,
    CameraInfo,
)
from gilbert.web.auth import require_authenticated
from gilbert.web.routes.cameras import router as cameras_router

# ── Test doubles ─────────────────────────────────────────────────────


class _FakeBackend:
    """Minimal stand-in for a CameraEventBackend.

    Exposes only the surface ``web/routes/cameras.py`` actually
    touches: ``get_clip_url`` and ``backend_auth_headers``.
    """

    def __init__(
        self,
        *,
        clip_url: str | None = "http://upstream.local/clip.mp4",
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        self._clip_url = clip_url
        self._auth_headers = auth_headers or {}

    async def get_clip_url(self, event_id: str) -> str | None:
        return self._clip_url

    def backend_auth_headers(self) -> dict[str, str]:
        return dict(self._auth_headers)


class _FakeCameraService:
    """Satisfies the CameraProvider Protocol for the routes.

    Holds a small in-memory event map + role-override map. Records
    ``max_height`` arguments passed to ``get_snapshot_bytes`` so the
    ``?h=720`` wiring assertion can verify the route.
    """

    def __init__(
        self,
        *,
        events: dict[str, CameraEvent],
        snapshots: dict[str, tuple[bytes, str]],
        backend: _FakeBackend | None,
        role_overrides: dict[str, str] | None = None,
        default_role: str = "everyone",
    ) -> None:
        self._events = events
        self._snapshots = snapshots
        self._backend = backend
        self._role_overrides = role_overrides or {}
        self._default_role = default_role
        self.snapshot_calls: list[tuple[str, int | None]] = []

    async def list_cameras(self) -> list[CameraInfo]:
        return []

    async def latest_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 20,
    ) -> list[CameraEvent]:
        return list(self._events.values())[:limit]

    async def get_event(self, event_id: str) -> CameraEvent | None:
        return self._events.get(event_id)

    async def get_snapshot_bytes(
        self,
        event_id: str,
        *,
        max_height: int | None = 720,
    ) -> tuple[bytes, str] | None:
        self.snapshot_calls.append((event_id, max_height))
        return self._snapshots.get(event_id)

    def _effective_role(self, camera: str) -> str:
        return self._role_overrides.get(camera, self._default_role)


class _FakeServiceManager:
    def __init__(self, camera_svc: _FakeCameraService | None) -> None:
        self._camera_svc = camera_svc

    def get_by_capability(self, capability: str) -> Any:
        if capability == "cameras":
            return self._camera_svc
        return None


class _FakeGilbert:
    def __init__(self, camera_svc: _FakeCameraService | None) -> None:
        self.service_manager = _FakeServiceManager(camera_svc)


def _make_event(
    event_id: str = "evt-1",
    camera: str = "front_door",
    label: str = "person",
) -> CameraEvent:
    return CameraEvent(
        event_id=event_id,
        camera=camera,
        label=label,
        sub_label="",
        phase=CameraEventPhase.ACTIVE,
        score=0.85,
        started_at=1_700_000_000_000,
        ended_at=0,
        zones=(),
        snapshot_url=f"/api/cameras/events/{event_id}/snapshot.jpg",
        clip_url=f"/api/cameras/events/{event_id}/clip.mp4",
        has_snapshot=True,
        has_clip=True,
        source_backend="fake",
    )


# ── Fixtures ─────────────────────────────────────────────────────────

_USER = UserContext(
    user_id="usr-user",
    email="user@example.com",
    display_name="User",
    roles=frozenset({"user"}),
    provider="local",
)

_ADMIN = UserContext(
    user_id="usr-admin",
    email="admin@example.com",
    display_name="Admin",
    roles=frozenset({"admin"}),
    provider="local",
)


def _build_app(
    camera_svc: _FakeCameraService | None,
    *,
    user: UserContext = _USER,
) -> FastAPI:
    app = FastAPI()
    app.state.gilbert = _FakeGilbert(camera_svc)
    app.include_router(cameras_router)

    def _fake_dep(request: Request) -> UserContext:
        return user

    app.dependency_overrides[require_authenticated] = _fake_dep
    return app


# ── Snapshot tests ───────────────────────────────────────────────────


def test_snapshot_returns_image_bytes_and_cache_header() -> None:
    ev = _make_event()
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={ev.event_id: (b"\xff\xd8\xff" + b"jpeg-bytes", "image/jpeg")},
        backend=_FakeBackend(),
    )
    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/snapshot.jpg")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.headers["cache-control"] == "private, max-age=3600"
    assert resp.content == b"\xff\xd8\xff" + b"jpeg-bytes"


def test_snapshot_passes_max_height_720_to_service() -> None:
    """The browser route always asks for the 720px downscale to keep
    payloads small; full-resolution lives behind the AI-tool path."""
    ev = _make_event()
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={ev.event_id: (b"jpeg", "image/jpeg")},
        backend=_FakeBackend(),
    )
    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/snapshot.jpg")
    assert resp.status_code == 200
    assert svc.snapshot_calls == [(ev.event_id, 720)]


def test_snapshot_returns_404_when_event_unknown() -> None:
    svc = _FakeCameraService(
        events={},
        snapshots={},
        backend=_FakeBackend(),
    )
    client = TestClient(_build_app(svc))
    resp = client.get("/api/cameras/events/no-such-event/snapshot.jpg")
    assert resp.status_code == 404


def test_snapshot_returns_404_when_snapshot_unavailable() -> None:
    """Event exists but the backend returned ``None`` (snapshot expired
    or the event never had one)."""
    ev = _make_event()
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},  # no snapshot bytes
        backend=_FakeBackend(),
    )
    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/snapshot.jpg")
    assert resp.status_code == 404


def test_snapshot_returns_403_when_default_role_gates_user_out() -> None:
    """``default_camera_role=admin`` should 403 the regular user."""
    ev = _make_event(camera="vault")
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={ev.event_id: (b"jpeg", "image/jpeg")},
        backend=_FakeBackend(),
        default_role="admin",
    )
    client = TestClient(_build_app(svc, user=_USER))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/snapshot.jpg")
    assert resp.status_code == 403


def test_snapshot_returns_403_when_per_camera_role_override_gates_user() -> None:
    """Default is ``everyone`` but the per-camera override pins to admin."""
    ev = _make_event(camera="vault")
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={ev.event_id: (b"jpeg", "image/jpeg")},
        backend=_FakeBackend(),
        default_role="everyone",
        role_overrides={"vault": "admin"},
    )
    client = TestClient(_build_app(svc, user=_USER))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/snapshot.jpg")
    assert resp.status_code == 403

    # Admin sees it.
    admin_client = TestClient(_build_app(svc, user=_ADMIN))
    resp = admin_client.get(f"/api/cameras/events/{ev.event_id}/snapshot.jpg")
    assert resp.status_code == 200


def test_snapshot_returns_503_when_camera_service_missing() -> None:
    client = TestClient(_build_app(None))
    resp = client.get("/api/cameras/events/anything/snapshot.jpg")
    assert resp.status_code == 503


# ── Clip tests ───────────────────────────────────────────────────────


def _patch_httpx_async_client(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Replace ``httpx.AsyncClient`` inside the cameras route with one
    bound to ``transport`` so requests go to the fake instead of the
    network."""
    import gilbert.web.routes.cameras as cameras_mod

    real_cls = httpx.AsyncClient

    class _AsyncClientWithTransport(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        cameras_mod.httpx, "AsyncClient", _AsyncClientWithTransport
    )


def test_clip_happy_path_returns_video_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev = _make_event()
    backend = _FakeBackend(clip_url="http://upstream.local/clip.mp4")
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},
        backend=backend,
    )

    received: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            content=b"video-mp4-bytes",
            headers={
                "content-type": "video/mp4",
                "content-length": "15",
                "accept-ranges": "bytes",
                "etag": "abc123",
            },
        )

    transport = httpx.MockTransport(_handler)
    _patch_httpx_async_client(monkeypatch, transport)

    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/clip.mp4")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"video-mp4-bytes"
    assert resp.headers["content-type"].startswith("video/mp4")
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["etag"] == "abc123"
    assert received["url"] == "http://upstream.local/clip.mp4"


def test_clip_forwards_range_header_and_returns_206(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev = _make_event()
    backend = _FakeBackend(clip_url="http://upstream.local/clip.mp4")
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},
        backend=backend,
    )

    received: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        received["range"] = request.headers.get("range")
        # Simulate an upstream 206 partial-content response.
        return httpx.Response(
            206,
            content=b"partial",
            headers={
                "content-type": "video/mp4",
                "content-range": "bytes 0-6/15",
                "content-length": "7",
                "accept-ranges": "bytes",
            },
        )

    transport = httpx.MockTransport(_handler)
    _patch_httpx_async_client(monkeypatch, transport)

    client = TestClient(_build_app(svc))
    resp = client.get(
        f"/api/cameras/events/{ev.event_id}/clip.mp4",
        headers={"Range": "bytes=0-6"},
    )
    assert resp.status_code == 206
    assert resp.content == b"partial"
    assert received["range"] == "bytes=0-6"
    assert resp.headers.get("content-range") == "bytes 0-6/15"
    assert resp.headers.get("accept-ranges") == "bytes"


def test_clip_injects_backend_auth_headers_on_upstream_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev = _make_event()
    backend = _FakeBackend(
        clip_url="http://upstream.local/clip.mp4",
        auth_headers={"Authorization": "Bearer s3cr3t"},
    )
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},
        backend=backend,
    )

    received: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        received["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            content=b"v",
            headers={"content-type": "video/mp4"},
        )

    transport = httpx.MockTransport(_handler)
    _patch_httpx_async_client(monkeypatch, transport)

    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/clip.mp4")
    assert resp.status_code == 200
    assert received["auth"] == "Bearer s3cr3t"


def test_clip_returns_404_when_no_upstream_url() -> None:
    ev = _make_event()
    backend = _FakeBackend(clip_url=None)
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},
        backend=backend,
    )
    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/clip.mp4")
    assert resp.status_code == 404


def test_clip_returns_404_when_event_unknown() -> None:
    backend = _FakeBackend()
    svc = _FakeCameraService(
        events={},
        snapshots={},
        backend=backend,
    )
    client = TestClient(_build_app(svc))
    resp = client.get("/api/cameras/events/missing/clip.mp4")
    assert resp.status_code == 404


def test_clip_returns_403_when_per_camera_role_gates_user() -> None:
    ev = _make_event(camera="vault")
    backend = _FakeBackend(clip_url="http://upstream.local/clip.mp4")
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},
        backend=backend,
        role_overrides={"vault": "admin"},
    )
    client = TestClient(_build_app(svc, user=_USER))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/clip.mp4")
    assert resp.status_code == 403


def test_clip_returns_503_when_backend_missing() -> None:
    """Service exists but backend wasn't initialized — service is in
    error/disabled state."""
    ev = _make_event()
    svc = _FakeCameraService(
        events={ev.event_id: ev},
        snapshots={},
        backend=None,
    )
    client = TestClient(_build_app(svc))
    resp = client.get(f"/api/cameras/events/{ev.event_id}/clip.mp4")
    assert resp.status_code == 503

