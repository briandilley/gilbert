"""Migration 0005: seed durable-default and link existing agents to it.

Idempotent — re-running yields the same end state. Loaded by path because the
filename starts with a digit (same pattern the runner uses).
"""

import importlib.util
import logging
from pathlib import Path

from gilbert.migrations.runner import MigrationContext


def _load_migration():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "src/gilbert/migrations/0005_seed_durable_default_and_link_agents.py"
    spec = importlib.util.spec_from_file_location("m0005", path)
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


async def test_seeds_type_and_links_unlinked_agents(sqlite_storage) -> None:
    mod = _load_migration()
    # One agent with no type id, one already linked to a custom type.
    await sqlite_storage.put("agents", "ag_1", {"_id": "ag_1", "name": "a"})
    await sqlite_storage.put(
        "agents", "ag_2", {"_id": "ag_2", "name": "b", "agent_type_id": "software-engineer"}
    )

    await mod.up(_ctx(sqlite_storage))

    # durable-default type seeded.
    t = await sqlite_storage.get("subagent_types", "durable-default")
    assert t is not None
    assert t["ai_profile"] == "standard"

    # Unlinked agent now points at durable-default; the linked one is untouched.
    a1 = await sqlite_storage.get("agents", "ag_1")
    a2 = await sqlite_storage.get("agents", "ag_2")
    assert a1["agent_type_id"] == "durable-default"
    assert a2["agent_type_id"] == "software-engineer"


async def test_idempotent_rerun(sqlite_storage) -> None:
    mod = _load_migration()
    await sqlite_storage.put("agents", "ag_1", {"_id": "ag_1", "name": "a"})
    await mod.up(_ctx(sqlite_storage))
    await mod.up(_ctx(sqlite_storage))  # no crash, no change
    a1 = await sqlite_storage.get("agents", "ag_1")
    assert a1["agent_type_id"] == "durable-default"
