"""Rewrite persisted scheduler jobs onto single cron expressions.

Before this migration a ``scheduler_jobs`` row described its schedule
with a ``schedule_type`` discriminator plus whichever of
``interval_seconds`` / ``hour`` / ``minute`` that type happened to use.
Afterwards every row carries one ``expression`` string in the dialect
documented in :mod:`gilbert.interfaces.cron`:

============================================  ===========================
Old row                                       New ``expression``
============================================  ===========================
``schedule_type=interval``, ``interval_seconds=N``  ``@every Ns``
``schedule_type=daily``, ``hour=H``, ``minute=M``   ``M H * * *``
``schedule_type=hourly``, ``minute=M``              ``M * * * *``
``schedule_type=once``, ``interval_seconds=N``      ``@once+Ns``
============================================  ===========================

``start_at``, ``end_at``, ``window_start_time``, ``window_end_time``,
``owner``, ``action`` and (for one-shots) ``fire_at`` carry over
untouched. The new ``timezone`` / ``catch_up`` / ``overlap`` keys are
seeded with the same defaults the constructors use, so a migrated job
behaves exactly as it did before.

Idempotent: a row that already has a non-empty ``expression`` is left
alone. The runner records success only after ``up()`` returns, so a
crash mid-run re-executes this script — rows converted on the first
pass are simply skipped on the second.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.storage import Query
from gilbert.migrations.runner import MigrationContext

logger = logging.getLogger(__name__)

description = "Rewrite scheduler_jobs schedules as cron expressions"

_JOBS_COLLECTION = "scheduler_jobs"

#: Legacy per-type catch-up defaults, matching the Schedule constructors:
#: a missed daily digest earns one make-up run, a poll tick does not.
_CATCH_UP_BY_TYPE = {
    "daily": "once",
    "hourly": "once",
    "interval": "skip",
    "once": "skip",
}


async def up(ctx: MigrationContext) -> None:
    """Convert every legacy scheduler_jobs row to an expression."""
    storage = ctx.storage

    try:
        rows = await storage.query(Query(collection=_JOBS_COLLECTION))
    except Exception:
        logger.info(
            "migration 0007: no %s collection yet — nothing to convert",
            _JOBS_COLLECTION,
        )
        return

    converted = 0
    skipped = 0

    for row in rows:
        name = row.get("name") or row.get("id")
        if not name:
            continue

        if str(row.get("expression") or "").strip():
            skipped += 1
            continue

        legacy_type = str(row.get("schedule_type") or "interval")
        expression = _expression_for(row, legacy_type)
        if expression is None:
            logger.warning(
                "migration 0007: job %r has unrecognised schedule_type %r "
                "— leaving it for the loader to drop",
                name,
                legacy_type,
            )
            continue

        row["expression"] = expression
        row.setdefault("timezone", "")
        row.setdefault("catch_up", _CATCH_UP_BY_TYPE.get(legacy_type, "skip"))
        row.setdefault("overlap", "skip")
        row.setdefault("jitter_seconds", 0)

        # The discriminator and its per-type rate fields are now derived
        # from the expression; drop them so nothing reads them again.
        for stale in ("schedule_type", "interval_seconds", "hour", "minute"):
            row.pop(stale, None)

        await storage.put(_JOBS_COLLECTION, str(name), row)
        converted += 1

    if converted or skipped:
        logger.info(
            "migration 0007: converted %d scheduler job(s), skipped %d "
            "already-converted",
            converted,
            skipped,
        )


def _expression_for(row: dict[str, Any], legacy_type: str) -> str | None:
    """Build the cron expression matching a legacy row's schedule."""
    if legacy_type == "interval":
        return f"@every {_seconds(row.get('interval_seconds'), 60)}s"
    if legacy_type == "once":
        return f"@once+{_seconds(row.get('interval_seconds'), 0)}s"
    if legacy_type == "daily":
        return f"{_clamp(row.get('minute'), 0, 59)} {_clamp(row.get('hour'), 0, 23)} * * *"
    if legacy_type == "hourly":
        return f"{_clamp(row.get('minute'), 0, 59)} * * * *"
    return None


def _seconds(value: Any, default: float) -> str:
    """Render a stored seconds value, dropping a pointless trailing .0."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = default
    if seconds < 0:
        seconds = default
    return str(int(seconds)) if seconds.is_integer() else str(seconds)


def _clamp(value: Any, lo: int, hi: int) -> int:
    """Coerce a stored hour/minute into range so the result always parses."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, int(number)))
