"""Migration: carry legacy per-type subagent prompt overrides onto the
seeded ``subagent_types`` rows. Idempotent — re-running yields the same end
state.

Migration modules whose filename starts with a digit can't be imported via
dotted syntax, so we load by path with ``importlib.util`` — the same pattern
the migration runner uses.
"""

import importlib.util
import logging
from pathlib import Path

from gilbert.migrations.runner import MigrationContext


def _load_migration():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "src/gilbert/migrations/0004_seed_subagent_types.py"
    spec = importlib.util.spec_from_file_location("m0004", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(storage) -> MigrationContext:
    return MigrationContext(
        storage=storage,
        log=logging.getLogger("test"),
        repo_root=Path("."),
    )


async def test_migration_carries_over_legacy_prompt_override(sqlite_storage) -> None:
    mod = _load_migration()
    # Legacy: per-type prompt override lived in the subagent config section as
    # ``<type_id_with_underscores>_system_prompt``.
    await sqlite_storage.put(
        "gilbert.config",
        "subagent",
        {"enabled": True, "deep_research_system_prompt": "CUSTOM"},
    )
    await mod.up(_ctx(sqlite_storage))

    row = await sqlite_storage.get("subagent_types", "deep-research")
    assert row is not None
    assert row["system_prompt"] == "CUSTOM"
    # The carried-over key is removed from the config section.
    cfg = await sqlite_storage.get("gilbert.config", "subagent")
    assert "deep_research_system_prompt" not in cfg

    # Idempotent: re-running doesn't blow up or re-apply (key already gone).
    await mod.up(_ctx(sqlite_storage))
    row2 = await sqlite_storage.get("subagent_types", "deep-research")
    assert row2["system_prompt"] == "CUSTOM"


async def test_migration_noop_without_legacy_config(sqlite_storage) -> None:
    mod = _load_migration()
    # No subagent config at all → nothing to carry; no crash.
    await mod.up(_ctx(sqlite_storage))
    # No legacy prompt keys → it shouldn't fabricate a custom override row.
    row = await sqlite_storage.get("subagent_types", "deep-research")
    assert row is None or row.get("system_prompt")
