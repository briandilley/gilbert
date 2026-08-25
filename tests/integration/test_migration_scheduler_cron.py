"""Integration tests for migration 0007 (scheduler cron schedules).

Runs against a real SQLite backend, per the project rule that database
tests do not mock the database. The migration is loaded via importlib
because the 4-digit filename prefix isn't a valid module name.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

from gilbert.interfaces.cron import parse
from gilbert.interfaces.scheduler import Schedule
from gilbert.interfaces.storage import Query
from gilbert.migrations.runner import MigrationContext
from gilbert.storage.sqlite import SQLiteStorage

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "src/gilbert/migrations/0007_scheduler_cron_schedules.py"
)
_spec = importlib.util.spec_from_file_location(
    "gilbert.migrations.__test__.0007_scheduler_cron_schedules",
    _MIGRATION_PATH,
)
assert _spec is not None and _spec.loader is not None
_migration_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration_module)  # type: ignore[union-attr]
_up = _migration_module.up

_JOBS = "scheduler_jobs"


def _ctx(storage: SQLiteStorage, tmp_path: Path) -> MigrationContext:
    return MigrationContext(
        storage=storage,
        repo_root=tmp_path,
        log=logging.getLogger("test"),
    )


async def _seed(storage: SQLiteStorage, name: str, **fields: Any) -> None:
    await storage.put(
        _JOBS,
        name,
        {
            "id": name,
            "name": name,
            "owner": "u1",
            "action": {"type": "event", "message": name},
            **fields,
        },
    )


async def _row(storage: SQLiteStorage, name: str) -> dict[str, Any]:
    row = await storage.get(_JOBS, name)
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_converts_interval_job(sqlite_storage: SQLiteStorage, tmp_path: Path) -> None:
    await _seed(sqlite_storage, "poll", schedule_type="interval", interval_seconds=90)
    await _up(_ctx(sqlite_storage, tmp_path))
    assert (await _row(sqlite_storage, "poll"))["expression"] == "@every 90s"


@pytest.mark.asyncio
async def test_converts_daily_job(sqlite_storage: SQLiteStorage, tmp_path: Path) -> None:
    await _seed(sqlite_storage, "digest", schedule_type="daily", hour=3, minute=30)
    await _up(_ctx(sqlite_storage, tmp_path))
    assert (await _row(sqlite_storage, "digest"))["expression"] == "30 3 * * *"


@pytest.mark.asyncio
async def test_converts_hourly_job(sqlite_storage: SQLiteStorage, tmp_path: Path) -> None:
    await _seed(sqlite_storage, "tick", schedule_type="hourly", minute=15)
    await _up(_ctx(sqlite_storage, tmp_path))
    assert (await _row(sqlite_storage, "tick"))["expression"] == "15 * * * *"


@pytest.mark.asyncio
async def test_converts_one_shot_job_and_keeps_fire_at(
    sqlite_storage: SQLiteStorage, tmp_path: Path
) -> None:
    await _seed(
        sqlite_storage,
        "egg",
        schedule_type="once",
        interval_seconds=45,
        fire_at="2026-08-25T12:00:00+00:00",
    )
    await _up(_ctx(sqlite_storage, tmp_path))
    row = await _row(sqlite_storage, "egg")
    assert row["expression"] == "@once+45s"
    assert row["fire_at"] == "2026-08-25T12:00:00+00:00"


@pytest.mark.asyncio
async def test_preserves_bounds_and_owner(sqlite_storage: SQLiteStorage, tmp_path: Path) -> None:
    await _seed(
        sqlite_storage,
        "bounded",
        schedule_type="interval",
        interval_seconds=60,
        start_at="2026-04-20T01:00:00",
        end_at="2026-04-20T02:00:00",
        window_start_time="01:00",
        window_end_time="02:00",
    )
    await _up(_ctx(sqlite_storage, tmp_path))
    row = await _row(sqlite_storage, "bounded")
    assert row["start_at"] == "2026-04-20T01:00:00"
    assert row["end_at"] == "2026-04-20T02:00:00"
    assert row["window_start_time"] == "01:00"
    assert row["window_end_time"] == "02:00"
    assert row["owner"] == "u1"
    assert row["action"]["message"] == "bounded"


@pytest.mark.asyncio
async def test_seeds_policy_defaults_per_legacy_type(
    sqlite_storage: SQLiteStorage, tmp_path: Path
) -> None:
    """A migrated job must behave exactly as it did before: a daily
    digest earns a make-up run, a poll tick does not."""
    await _seed(sqlite_storage, "digest", schedule_type="daily", hour=3, minute=0)
    await _seed(sqlite_storage, "poll", schedule_type="interval", interval_seconds=30)
    await _up(_ctx(sqlite_storage, tmp_path))
    assert (await _row(sqlite_storage, "digest"))["catch_up"] == "once"
    assert (await _row(sqlite_storage, "poll"))["catch_up"] == "skip"
    assert (await _row(sqlite_storage, "poll"))["overlap"] == "skip"
    assert (await _row(sqlite_storage, "poll"))["timezone"] == ""


@pytest.mark.asyncio
async def test_drops_legacy_discriminator_fields(
    sqlite_storage: SQLiteStorage, tmp_path: Path
) -> None:
    await _seed(sqlite_storage, "poll", schedule_type="interval", interval_seconds=30)
    await _up(_ctx(sqlite_storage, tmp_path))
    row = await _row(sqlite_storage, "poll")
    for stale in ("schedule_type", "interval_seconds", "hour", "minute"):
        assert stale not in row


@pytest.mark.asyncio
async def test_is_idempotent(sqlite_storage: SQLiteStorage, tmp_path: Path) -> None:
    """The runner records success only after up() returns, so a crash
    mid-run re-executes the script. Re-running must be a no-op."""
    await _seed(sqlite_storage, "digest", schedule_type="daily", hour=3, minute=30)
    await _up(_ctx(sqlite_storage, tmp_path))
    first = await _row(sqlite_storage, "digest")
    await _up(_ctx(sqlite_storage, tmp_path))
    await _up(_ctx(sqlite_storage, tmp_path))
    assert await _row(sqlite_storage, "digest") == first


@pytest.mark.asyncio
async def test_does_not_touch_already_converted_rows(
    sqlite_storage: SQLiteStorage, tmp_path: Path
) -> None:
    """A hand-written cron job must survive the migration unchanged."""
    await _seed(
        sqlite_storage,
        "standup",
        expression="0 9 * * MON-FRI",
        timezone="America/Los_Angeles",
        catch_up="backfill",
    )
    await _up(_ctx(sqlite_storage, tmp_path))
    row = await _row(sqlite_storage, "standup")
    assert row["expression"] == "0 9 * * MON-FRI"
    assert row["timezone"] == "America/Los_Angeles"
    assert row["catch_up"] == "backfill"


@pytest.mark.asyncio
async def test_every_converted_row_parses_and_loads(
    sqlite_storage: SQLiteStorage, tmp_path: Path
) -> None:
    """End to end: whatever the migration writes must build a Schedule."""
    await _seed(sqlite_storage, "a", schedule_type="interval", interval_seconds=90)
    await _seed(sqlite_storage, "b", schedule_type="daily", hour=23, minute=59)
    await _seed(sqlite_storage, "c", schedule_type="hourly", minute=0)
    await _seed(sqlite_storage, "d", schedule_type="once", interval_seconds=0)
    await _up(_ctx(sqlite_storage, tmp_path))

    rows = await sqlite_storage.query(Query(collection=_JOBS))
    assert len(rows) == 4
    for row in rows:
        parse(row["expression"])  # must not raise
        Schedule(expression=row["expression"])  # must not raise


@pytest.mark.asyncio
async def test_empty_collection_is_a_no_op(sqlite_storage: SQLiteStorage, tmp_path: Path) -> None:
    await _up(_ctx(sqlite_storage, tmp_path))
    assert await sqlite_storage.query(Query(collection=_JOBS)) == []
