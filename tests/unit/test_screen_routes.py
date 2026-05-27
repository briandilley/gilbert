"""Tests for the screen HTTP routes — ``/screens/info`` payload and the
``/screens/stream`` guest-access gate.

The gate is the security-relevant bit: unauthenticated visitors
(``UserContext.SYSTEM``, no roles) may only open the SSE stream when
``allow_guest_screens`` is on. Logged-in users and local guests (role
``everyone``) always pass.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from gilbert.core.services.screens import ConnectedScreen, ScreenService
from gilbert.interfaces.auth import UserContext
from gilbert.web.routes.screens import router as screens_router


def _make_app(svc: Any, user: UserContext = UserContext.SYSTEM) -> FastAPI:
    """Mount the screens router with a stub service manager (returns ``svc``
    for the ``screen_display`` capability) and a middleware that pins
    ``request.state.user`` to ``user`` (the real AuthMiddleware does this)."""
    app = FastAPI()

    class _SM:
        def get_by_capability(self, name: str) -> Any:
            return svc if name == "screen_display" else None

    app.state.gilbert = SimpleNamespace(service_manager=_SM())

    @app.middleware("http")
    async def _set_user(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.user = user
        return await call_next(request)

    app.include_router(screens_router)
    return app


def _enabled_service(allow_guest: bool = False) -> ScreenService:
    svc = ScreenService()
    svc._enabled = True
    svc._allow_guest_screens = allow_guest
    # The real SSE generator blocks forever waiting on the queue, which would
    # hang TestClient. The gate (what these tests exercise) runs before the
    # stream is opened, so swap in a finite generator and let the body drain.
    async def _finite_stream(screen: ConnectedScreen) -> AsyncGenerator[str, None]:
        yield ": ok\n\n"

    svc.event_stream = _finite_stream  # type: ignore[method-assign]
    return svc


# ── /screens/info ────────────────────────────────────────────


class TestScreensInfo:
    def test_service_absent(self) -> None:
        client = TestClient(_make_app(None))
        body = client.get("/screens/info").json()
        assert body == {"enabled": False, "allow_guest_screens": False}

    def test_disabled_service(self) -> None:
        svc = ScreenService()  # _enabled defaults to False
        body = TestClient(_make_app(svc)).get("/screens/info").json()
        assert body == {"enabled": False, "allow_guest_screens": False}

    def test_enabled_guest_off(self) -> None:
        body = TestClient(_make_app(_enabled_service(allow_guest=False))).get(
            "/screens/info"
        ).json()
        assert body == {"enabled": True, "allow_guest_screens": False}

    def test_enabled_guest_on(self) -> None:
        body = TestClient(_make_app(_enabled_service(allow_guest=True))).get(
            "/screens/info"
        ).json()
        assert body == {"enabled": True, "allow_guest_screens": True}


# ── /screens/stream gate ─────────────────────────────────────


class TestScreensStreamGate:
    def test_unauthenticated_rejected_when_guest_off(self) -> None:
        client = TestClient(_make_app(_enabled_service(allow_guest=False)))
        resp = client.get("/screens/stream?name=shop")
        assert resp.status_code == 403

    def test_unauthenticated_allowed_when_guest_on(self) -> None:
        svc = _enabled_service(allow_guest=True)
        resp = TestClient(_make_app(svc)).get("/screens/stream?name=shop")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert svc.get_screen("shop") is not None

    def test_authenticated_user_allowed_when_guest_off(self) -> None:
        user = UserContext(
            user_id="u1", email="u1@x", display_name="U", roles=frozenset({"user"})
        )
        svc = _enabled_service(allow_guest=False)
        resp = TestClient(_make_app(svc, user=user)).get("/screens/stream?name=bench")
        assert resp.status_code == 200
        assert svc.get_screen("bench") is not None

    def test_local_guest_allowed_when_guest_off(self) -> None:
        svc = _enabled_service(allow_guest=False)
        resp = TestClient(_make_app(svc, user=UserContext.GUEST)).get(
            "/screens/stream?name=bench"
        )
        assert resp.status_code == 200

    def test_missing_service_returns_503(self) -> None:
        client = TestClient(_make_app(None, user=UserContext.GUEST))
        resp = client.get("/screens/stream?name=shop")
        assert resp.status_code == 503
