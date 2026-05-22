"""``gilbert.cli.migrate`` — discover, list, and apply migrations.

Sub-commands:
- ``list`` — print every pending migration (scope, name, description).
  Exit 0 if there are no pending migrations, 1 if there are. Useful
  for ``./gilbert.sh start`` to detect "you have unapplied work."
- ``status`` — print everything: applied + pending. Exit 0.
- ``up`` — apply every pending migration in order. Exit 0 on success,
  1 on the first failure (with the offending migration logged).

The CLI boots only as much of Gilbert as it takes to read the
bootstrap config, open storage, and discover plugin directories. No
service startup, no network. The storage open/close handshake mirrors
``Gilbert._init_storage``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from gilbert.config import (
    DEFAULT_CONFIG_PATH,
    OVERRIDE_CONFIG_PATH,
    _deep_merge,
    _load_yaml,
)
from gilbert.interfaces.storage import StorageBackend
from gilbert.migrations.runner import (
    Migration,
    MigrationRunner,
    discover_migrations,
)
from gilbert.plugins.loader import PluginLoader

_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _c(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{_RESET}"


async def _open_storage() -> StorageBackend:
    """Open the storage backend configured by gilbert.yaml + overrides.

    Mirrors `Gilbert._init_storage` but stays standalone so the CLI
    doesn't spin up services. Today only sqlite is wired; extend in
    lockstep with `_init_storage` if other backends land."""
    base: dict[str, Any] = (
        _load_yaml(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else {}
    )
    overrides: dict[str, Any] = (
        _load_yaml(OVERRIDE_CONFIG_PATH) if OVERRIDE_CONFIG_PATH.exists() else {}
    )
    merged = _deep_merge(base, overrides)
    storage_cfg = merged.get("storage") or {}
    backend = str(storage_cfg.get("backend", "sqlite"))
    connection = str(storage_cfg.get("connection", ".gilbert/gilbert.db"))

    if backend == "sqlite":
        from gilbert.storage.sqlite import SQLiteStorage

        db_path = Path(connection).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        storage = SQLiteStorage(str(db_path))
        await storage.initialize()
        return storage
    raise SystemExit(f"Unsupported storage backend in config: {backend!r}")


def _discover_plugin_dirs() -> list[Path]:
    """Return every plugin root dir discovered on disk.

    Uses `PluginLoader.scan_directories` so we honor the same config
    surface (``plugins.directories``) and precedence rules the runtime
    does. Each manifest's `path` is the plugin dir; the runner looks
    for a `migrations/` subdir under each.
    """
    base: dict[str, Any] = (
        _load_yaml(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else {}
    )
    overrides: dict[str, Any] = (
        _load_yaml(OVERRIDE_CONFIG_PATH) if OVERRIDE_CONFIG_PATH.exists() else {}
    )
    merged = _deep_merge(base, overrides)
    plugins_raw = merged.get("plugins", {}) or {}
    directories = (
        plugins_raw.get("directories", []) if isinstance(plugins_raw, dict) else []
    )
    loader = PluginLoader(
        cache_dir=plugins_raw.get("cache_dir", ".gilbert/plugin-cache")
    )
    manifests = loader.scan_directories(directories)
    return [Path(m.path) for m in manifests]


def _print_pending(pending: list[Migration]) -> None:
    if not pending:
        print(_c("No pending migrations.", _GREEN))
        return
    print(_c(f"{len(pending)} pending migration(s):", _BOLD))
    for m in pending:
        scope_label = _c(f"[{m.scope}]", _DIM)
        print(f"  {scope_label} {m.name} — {m.description or '(no description)'}")


def _print_status(
    applied: list[Migration],
    pending: list[Migration],
) -> None:
    print(_c(f"Applied: {len(applied)}", _BOLD))
    for m in applied:
        print(f"  {_c('✓', _GREEN)}  [{m.scope}] {m.name}")
    print()
    print(_c(f"Pending: {len(pending)}", _BOLD))
    for m in pending:
        print(f"  {_c('·', _DIM)}  [{m.scope}] {m.name} — {m.description}")


async def _cmd_list(_args: argparse.Namespace) -> int:
    storage = await _open_storage()
    try:
        runner = MigrationRunner(storage, Path.cwd())
        migrations = discover_migrations(_discover_plugin_dirs())
        pending = await runner.pending(migrations)
        _print_pending(pending)
        return 1 if pending else 0
    finally:
        await storage.close()


async def _cmd_status(_args: argparse.Namespace) -> int:
    storage = await _open_storage()
    try:
        runner = MigrationRunner(storage, Path.cwd())
        migrations = discover_migrations(_discover_plugin_dirs())
        pending = await runner.pending(migrations)
        pending_keys = {m.key for m in pending}
        applied = [m for m in migrations if m.key not in pending_keys]
        _print_status(applied, pending)
        return 0
    finally:
        await storage.close()


async def _cmd_up(_args: argparse.Namespace) -> int:
    storage = await _open_storage()
    try:
        runner = MigrationRunner(storage, Path.cwd())
        migrations = discover_migrations(_discover_plugin_dirs())
        pending = await runner.pending(migrations)
        if not pending:
            print(_c("No pending migrations — nothing to do.", _GREEN))
            return 0
        print(_c(f"Running {len(pending)} migration(s)...", _BOLD))
        try:
            ran = await runner.run(migrations)
        except Exception as exc:
            # Whatever already committed stayed committed; surface the
            # offending migration so the user can investigate.
            print(_c(f"\nMIGRATION FAILED: {exc}", _RED), file=sys.stderr)
            print(
                _c(
                    "Earlier migrations that succeeded have been recorded; "
                    "re-run ``./gilbert.sh migrate up`` after fixing.",
                    _YELLOW,
                ),
                file=sys.stderr,
            )
            return 1
        for m in ran:
            print(f"  {_c('✓', _GREEN)}  [{m.scope}] {m.name}")
        print(_c(f"\nApplied {len(ran)} migration(s).", _GREEN))
        return 0
    finally:
        await storage.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="gilbert migrate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Print pending migrations (exit 1 if any).")
    sub.add_parser("status", help="Print applied + pending migrations.")
    sub.add_parser("up", help="Apply every pending migration.")
    args = parser.parse_args()

    if args.cmd == "list":
        return asyncio.run(_cmd_list(args))
    if args.cmd == "status":
        return asyncio.run(_cmd_status(args))
    if args.cmd == "up":
        return asyncio.run(_cmd_up(args))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
