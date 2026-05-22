"""Migration discovery, tracking, and execution.

A migration is any Python module that defines:
    description: str        # short human-readable label
    async def up(ctx)       # the work; ctx is MigrationContext

Discovery sources:
1. ``src/gilbert/migrations/*.py`` — core migrations, scope ``core``.
2. ``<plugin-dir>/migrations/*.py`` — per-plugin migrations, scope
   ``<plugin-name>``. Discovered from every PluginManifest the
   `PluginLoader` finds on disk, regardless of whether that plugin is
   enabled. (Disabled plugins still have data in storage; ignoring
   their migrations would let that data drift.)

Files starting with ``_`` are skipped, so helpers can live alongside
migrations without being picked up. Files MUST start with a 4-digit
zero-padded ordering prefix; the runner sorts within each scope by
this prefix. Plugins' ordering is independent of core's.

Tracking:
- Applied migrations are stored as entities in collection
  ``_migrations``, with entity id ``<scope>:<basename>`` and
  ``{"scope", "name", "applied_at", "duration_ms"}``.
- The runner reads the collection once at startup, then on each
  successful ``up`` call writes a new row.
"""

from __future__ import annotations

import importlib.util
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from gilbert.interfaces.storage import StorageBackend

logger = logging.getLogger(__name__)

_MIGRATIONS_COLLECTION = "_migrations"

_UpFn = Callable[["MigrationContext"], Awaitable[None]]


@dataclass(frozen=True)
class MigrationContext:
    """What a migration's ``up`` function gets handed.

    Keep this surface deliberately small. Migrations have access to
    `storage` (the same `StorageBackend` core uses), a logger, and the
    repository root for any filesystem operations. Migrations that
    need richer context (e.g. an HTTP client) construct it themselves
    — they're one-shot scripts, not services.
    """

    storage: StorageBackend
    repo_root: Path
    log: logging.Logger


@dataclass(frozen=True)
class Migration:
    """A discovered migration ready to run.

    `scope` is ``"core"`` for migrations under ``src/gilbert/migrations``,
    or the plugin name for plugin-shipped migrations. `name` is the
    filename without the ``.py`` extension. The pair ``(scope, name)``
    is the tracking key.
    """

    scope: str
    name: str
    description: str
    up: _UpFn
    source_path: Path

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"


def _is_migration_filename(name: str) -> bool:
    """Migration files MUST be named ``NNNN_short_name.py`` (4-digit
    zero-padded ordering prefix). Anything else in the directory —
    ``runner.py``, ``__init__.py``, ``_helpers.py``, README — is
    skipped without complaint, so helpers can live alongside
    migrations.
    """
    if not name.endswith(".py"):
        return False
    if name.startswith("_") or name == "__init__.py":
        return False
    stem = name[:-3]
    if len(stem) < 5 or stem[4] != "_":
        return False
    return stem[:4].isdigit()


def _load_migration_module(scope: str, file_path: Path) -> Migration | None:
    """Load a migration file and pull out its required attributes.

    Returns ``None`` (with a warning) for files that don't conform —
    we don't want a half-broken file in someone's plugin dir to abort
    the whole run.
    """
    if not _is_migration_filename(file_path.name):
        return None
    mod_name = f"gilbert.migrations.__loaded__.{scope}.{file_path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        logger.warning("migration: could not load spec for %s", file_path)
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("migration: failed to import %s — %s", file_path, exc)
        return None

    up = getattr(module, "up", None)
    if not callable(up):
        logger.warning(
            "migration: %s has no ``up`` callable — skipping", file_path
        )
        return None

    description = getattr(module, "description", "") or ""
    if not isinstance(description, str):
        description = str(description)

    return Migration(
        scope=scope,
        name=file_path.stem,
        description=description,
        up=up,
        source_path=file_path,
    )


def _discover_in_dir(scope: str, directory: Path) -> list[Migration]:
    if not directory.is_dir():
        return []
    out: list[Migration] = []
    for child in sorted(directory.iterdir()):
        if not child.is_file():
            continue
        migration = _load_migration_module(scope, child)
        if migration is not None:
            out.append(migration)
    return out


def discover_migrations(plugin_dirs: list[Path]) -> list[Migration]:
    """Discover every migration on disk.

    `plugin_dirs` is the list of plugin root directories — typically
    obtained by walking each ``plugins.directories`` from config and
    collecting the immediate children. The runner doesn't load
    plugins; it just looks for a ``migrations/`` subdir under each.

    Ordering: core migrations first (sorted by filename), then each
    plugin's migrations (plugin name alphabetical, files within each
    plugin sorted by filename). Within a scope the filename's
    ``NNNN_`` prefix is what does the actual ordering; across scopes
    we run core first so plugin migrations can depend on core fields
    already being shaped.
    """
    core_dir = Path(__file__).parent
    out: list[Migration] = list(_discover_in_dir("core", core_dir))

    seen_plugins: set[str] = set()
    for plugin_dir in sorted(plugin_dirs):
        if not plugin_dir.is_dir():
            continue
        plugin_name = plugin_dir.name
        if plugin_name in seen_plugins:
            # Two plugin install locations claim the same name; the
            # first wins (matches PluginLoader's precedence).
            continue
        seen_plugins.add(plugin_name)
        out.extend(_discover_in_dir(plugin_name, plugin_dir / "migrations"))

    return out


class MigrationRunner:
    """Runs migrations once, tracks applied state in storage."""

    def __init__(self, storage: StorageBackend, repo_root: Path) -> None:
        self._storage = storage
        self._repo_root = repo_root
        self._applied: set[str] | None = None

    async def _load_applied(self) -> set[str]:
        if self._applied is not None:
            return self._applied
        try:
            rows = await self._storage.query(
                _build_all_query(),
            )
        except Exception as exc:
            # First run on a fresh store may not have the collection
            # registered yet — treat as "nothing applied".
            logger.debug("migration: applied-set query failed (%s)", exc)
            rows = []
        self._applied = {str(r.get("key", "")) for r in rows if r.get("key")}
        return self._applied

    async def pending(self, migrations: list[Migration]) -> list[Migration]:
        """Return migrations not yet recorded as applied."""
        applied = await self._load_applied()
        return [m for m in migrations if m.key not in applied]

    async def run(self, migrations: list[Migration]) -> list[Migration]:
        """Run every pending migration in `migrations`, in order.

        Returns the list of migrations that ran. Raises the first
        exception that comes out of a migration's ``up`` (so the
        caller can stop, log, and let the user investigate). Anything
        that already succeeded stays recorded.
        """
        pending = await self.pending(migrations)
        ran: list[Migration] = []
        ctx_log = logging.getLogger("gilbert.migrations")
        for m in pending:
            ctx = MigrationContext(
                storage=self._storage,
                repo_root=self._repo_root,
                log=ctx_log,
            )
            start = time.monotonic()
            logger.info("migration: running %s — %s", m.key, m.description)
            await m.up(ctx)
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._storage.put(
                _MIGRATIONS_COLLECTION,
                m.key,
                {
                    "key": m.key,
                    "scope": m.scope,
                    "name": m.name,
                    "description": m.description,
                    "applied_at": datetime.now(UTC).isoformat(),
                    "duration_ms": duration_ms,
                },
            )
            if self._applied is not None:
                self._applied.add(m.key)
            logger.info("migration: %s ok (%d ms)", m.key, duration_ms)
            ran.append(m)
        return ran


def _build_all_query() -> Any:
    """Construct a query that lists every entity in the migrations
    collection. Built lazily so importing this module doesn't pull
    storage Query types into circular range."""
    from gilbert.interfaces.storage import Query

    return Query(collection=_MIGRATIONS_COLLECTION, limit=10000)
