"""Tests for SpeakerService browser RPCs, role filter, and permissions."""
from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock

from gilbert.core.services.speaker import SpeakerService
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.auth import UserContext


def _make_admin() -> UserContext:
    return UserContext(user_id="admin1", display_name="Admin", email="", roles=frozenset({"admin"}))


def _make_user(uid: str = "alice") -> UserContext:
    return UserContext(user_id=uid, display_name=uid.title(), email="", roles=frozenset({"user"}))


@pytest.fixture
async def svc_with_browser_backend() -> SpeakerService:
    svc = SpeakerService()
    backend = BrowserSpeakerBackend()
    await backend.initialize({})
    svc._backends = {"browser": backend}
    return svc


# --- WS RPC handlers ---

@pytest.mark.asyncio
async def test_ws_activate_registers_connection_on_browser_backend(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    conn = MagicMock()
    conn.connection_id = "c1"
    conn.user_id = "alice"
    conn.display_name = "Alice"
    close_callbacks: list[Any] = []
    conn.add_close_callback.side_effect = close_callbacks.append

    result = await svc._ws_browser_speaker_activate(conn, {})

    assert result == {"status": "ok"}
    backend = svc._backends["browser"]
    assert "alice" in backend._active_connections
    assert "c1" in backend._active_connections["alice"]
    assert len(close_callbacks) == 1


@pytest.mark.asyncio
async def test_ws_deactivate_removes_connection(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    conn = MagicMock()
    conn.connection_id = "c1"
    conn.user_id = "alice"

    result = await svc._ws_browser_speaker_deactivate(conn, {})

    assert result == {"status": "ok"}
    assert "alice" not in backend._active_connections


@pytest.mark.asyncio
async def test_ws_activate_registers_close_callback_for_disconnect_cleanup(
    svc_with_browser_backend: SpeakerService,
) -> None:
    """When the WS connection drops, registration must vanish."""
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    captured: list[Any] = []
    conn = MagicMock()
    conn.connection_id = "c1"
    conn.user_id = "alice"
    conn.display_name = "Alice"
    conn.add_close_callback.side_effect = captured.append

    await svc._ws_browser_speaker_activate(conn, {})
    assert "alice" in backend._active_connections

    # Simulate the connection closing
    captured[0]()
    assert "alice" not in backend._active_connections


@pytest.mark.asyncio
async def test_get_ws_handlers_exposes_browser_speaker_rpcs(
    svc_with_browser_backend: SpeakerService,
) -> None:
    handlers = svc_with_browser_backend.get_ws_handlers()
    assert "browser_speaker.activate" in handlers
    assert "browser_speaker.deactivate" in handlers
