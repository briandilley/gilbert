"""Gilbert migration system.

Migrations are arbitrary one-shot upgrade scripts — *not* schema
migrations. A migration can rename a stored field, backfill defaults,
recompute a derived index, move files in the data directory, or
anything else that needs to happen exactly once after an update. The
runner tracks which migrations have been applied and never re-runs
them. See ``runner.py`` for the discovery + execution logic and
``gilbert.cli.migrate`` for the CLI entrypoint.

Core migrations live in this directory as files named
``NNNN_short_name.py``. Each plugin can ship its own under
``<plugin>/migrations/`` — the runner discovers them per-plugin and
tracks applied state separately so an enabled / disabled plugin
doesn't leak migration state into core.

A migration file must define:

- ``description: str`` (module-level) — short human-readable label.
- ``async def up(ctx: MigrationContext) -> None`` — the work itself.

Make ``up`` idempotent. Migrations are tracked in storage, but the
runner is best-effort: if the process is killed mid-migration the
applied-marker may not get written, and the migration will be re-run.
Idempotent shape ("if entity already has field X, skip") keeps that
safe.
"""

from gilbert.migrations.runner import (
    Migration,
    MigrationContext,
    MigrationRunner,
    discover_migrations,
)

__all__ = [
    "Migration",
    "MigrationContext",
    "MigrationRunner",
    "discover_migrations",
]
