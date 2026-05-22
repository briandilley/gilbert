"""Integration tests for the 0001 speaker migration.

Uses a real SQLite storage backend (per CLAUDE.md: 'database tests use a
real test SQLite database; no mocking the DB').

The migration's ``up(ctx)`` takes a ``MigrationContext``, so we build one
from the real storage fixture and inject fake backends by monkey-patching
the ``SpeakerBackend`` registry for the duration of each test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gilbert.interfaces.speaker import PlaybackState, SpeakerBackend, SpeakerInfo
from gilbert.migrations.runner import MigrationContext

# Migration files are named with a numeric prefix (e.g. ``0001_...``), which is
# not a valid Python identifier.  The runner loads them via ``importlib.util``
# just like we do here — this is intentional so normal import machinery doesn't
# pick them up by accident.
_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "src/gilbert/migrations/0001_namespace_speaker_aliases_and_config.py"
)
_spec = importlib.util.spec_from_file_location(
    "gilbert.migrations.__test__.0001_namespace_speaker_aliases_and_config",
    _MIGRATION_PATH,
)
assert _spec is not None and _spec.loader is not None, f"Could not load migration from {_MIGRATION_PATH}"
_migration_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration_module)  # type: ignore[union-attr]
up = _migration_module.up


# ---------------------------------------------------------------------------
# Minimal fake backends used across tests
# ---------------------------------------------------------------------------


class _FakeA(SpeakerBackend):
    """Fake backend 'fake_a' with one speaker: uid-1."""

    backend_name = "fake_a"

    async def initialize(self, config: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_speakers(self) -> list[SpeakerInfo]:
        return [SpeakerInfo(speaker_id="uid-1", name="A1", ip_address="")]

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        return None

    async def play_uri(self, request: Any) -> None:
        pass

    async def stop(self, speaker_ids: Any = None) -> None:
        pass

    async def get_volume(self, speaker_id: str) -> int:
        return 50

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        pass


class _FakeB(_FakeA):
    """Fake backend 'fake_b' with one speaker: uid-2."""

    backend_name = "fake_b"

    async def list_speakers(self) -> list[SpeakerInfo]:
        return [SpeakerInfo(speaker_id="uid-2", name="B2", ip_address="")]


# We never want these test-only classes persisting in the global registry
# between tests, so each test patches the registry directly.


def _make_fake_registry() -> dict[str, type[SpeakerBackend]]:
    """Return a registry dict with only the two fake backends."""
    return {"fake_a": _FakeA, "fake_b": _FakeB}


def _make_ctx(storage: Any) -> MigrationContext:
    import logging

    return MigrationContext(
        storage=storage,
        repo_root=Path("."),
        log=logging.getLogger("test.migration"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_namespaces_bare_alias_rows(sqlite_storage: Any) -> None:
    """A bare alias ``speaker_id`` is prefixed with the backend name."""
    await sqlite_storage.put(
        "speaker_aliases",
        "k1",
        {"speaker_id": "uid-1", "alias": "k1", "display_alias": "Kitchen"},
    )
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", _make_fake_registry()):
        await up(ctx)
    row = await sqlite_storage.get("speaker_aliases", "k1")
    assert row is not None
    assert row["speaker_id"] == "fake_a:uid-1"


@pytest.mark.asyncio
async def test_migration_is_idempotent(sqlite_storage: Any) -> None:
    """Running the migration twice on an already-namespaced row is a no-op."""
    await sqlite_storage.put(
        "speaker_aliases",
        "k1",
        {"speaker_id": "fake_a:uid-1", "alias": "k1", "display_alias": "Kitchen"},
    )
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", _make_fake_registry()):
        await up(ctx)
        await up(ctx)
    row = await sqlite_storage.get("speaker_aliases", "k1")
    assert row is not None
    assert row["speaker_id"] == "fake_a:uid-1"


@pytest.mark.asyncio
async def test_migration_leaves_unrecognized_alias_bare(sqlite_storage: Any) -> None:
    """A bare alias whose id no backend recognises is left unchanged."""
    await sqlite_storage.put(
        "speaker_aliases",
        "k1",
        {"speaker_id": "lost-uid-999", "alias": "k1", "display_alias": "Ghost"},
    )
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", _make_fake_registry()):
        await up(ctx)
    row = await sqlite_storage.get("speaker_aliases", "k1")
    assert row is not None
    assert row["speaker_id"] == "lost-uid-999"  # unchanged


@pytest.mark.asyncio
async def test_migration_rewrites_legacy_speaker_config(sqlite_storage: Any) -> None:
    """Legacy ``backend`` key is promoted to ``primary_backend`` + nested backends dict."""
    await sqlite_storage.put(
        "gilbert.config",
        "speaker",
        {"enabled": True, "backend": "fake_a"},
    )
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", _make_fake_registry()):
        await up(ctx)
    row = await sqlite_storage.get("gilbert.config", "speaker")
    assert row is not None
    assert row["primary_backend"] == "fake_a"
    assert row["backends"]["fake_a"]["enabled"] is True
    assert "backend" not in row


@pytest.mark.asyncio
async def test_migration_legacy_config_idempotent(sqlite_storage: Any) -> None:
    """Running the migration twice on an already-migrated config row is a no-op."""
    await sqlite_storage.put(
        "gilbert.config",
        "speaker",
        {
            "enabled": True,
            "primary_backend": "fake_a",
            "backends": {"fake_a": {"enabled": True}},
        },
    )
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", _make_fake_registry()):
        await up(ctx)
    row = await sqlite_storage.get("gilbert.config", "speaker")
    assert row is not None
    assert row["primary_backend"] == "fake_a"
    assert "backend" not in row


@pytest.mark.asyncio
async def test_migration_multi_backend_prefers_sonos(sqlite_storage: Any) -> None:
    """When a bare id matches multiple backends, 'sonos' is preferred if present."""

    class _FakeSonos(_FakeA):
        backend_name = "sonos"

        async def list_speakers(self) -> list[SpeakerInfo]:
            return [SpeakerInfo(speaker_id="uid-1", name="Sonos1", ip_address="")]

    registry = {"fake_a": _FakeA, "sonos": _FakeSonos}
    await sqlite_storage.put(
        "speaker_aliases",
        "k1",
        {"speaker_id": "uid-1", "alias": "k1", "display_alias": "Living Room"},
    )
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", registry):
        await up(ctx)
    row = await sqlite_storage.get("speaker_aliases", "k1")
    assert row is not None
    assert row["speaker_id"] == "sonos:uid-1"


@pytest.mark.asyncio
async def test_migration_empty_collections_is_noop(sqlite_storage: Any) -> None:
    """Running against empty collections raises no error and is a clean no-op."""
    ctx = _make_ctx(sqlite_storage)
    with patch.object(SpeakerBackend, "_registry", _make_fake_registry()):
        await up(ctx)  # should not raise
