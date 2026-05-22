"""Tests for BrowserSpeakerBackend activation tracking."""

from __future__ import annotations

import pytest

from gilbert.integrations.browser_speaker import BrowserSpeakerBackend


@pytest.fixture
async def backend() -> BrowserSpeakerBackend:
    b = BrowserSpeakerBackend()
    await b.initialize({})
    return b


def test_activate_registers_connection_for_user(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    assert "alice" in backend._active_connections
    assert "c1" in backend._active_connections["alice"]
    assert backend._active_connections["alice"]["c1"] == "Alice"
    assert backend._conn_to_user["c1"] == "alice"


def test_deactivate_removes_connection(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.deactivate(conn_id="c1")
    assert "alice" not in backend._active_connections
    assert "c1" not in backend._conn_to_user


def test_deactivate_unknown_conn_is_noop(backend: BrowserSpeakerBackend) -> None:
    backend.deactivate(conn_id="never-registered")
    assert backend._active_connections == {}


def test_activate_idempotent_on_repeated_calls(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    assert len(backend._active_connections["alice"]) == 1


def test_multiple_conns_per_user(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="alice", display_name="Alice")
    backend.deactivate(conn_id="c1")
    assert "alice" in backend._active_connections
    assert set(backend._active_connections["alice"]) == {"c2"}


@pytest.mark.asyncio
async def test_list_speakers_empty_when_no_active_connections(backend: BrowserSpeakerBackend) -> None:
    assert await backend.list_speakers() == []


@pytest.mark.asyncio
async def test_list_speakers_returns_entry_per_active_user(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="bob", display_name="Bob")
    result = await backend.list_speakers()
    by_id = {s.speaker_id: s for s in result}
    assert set(by_id) == {"alice", "bob"}
    assert by_id["alice"].name == "Alice's Browser"
    assert by_id["bob"].name == "Bob's Browser"


@pytest.mark.asyncio
async def test_list_speakers_drops_user_when_last_conn_deactivates(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.deactivate(conn_id="c1")
    assert await backend.list_speakers() == []
