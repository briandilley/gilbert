"""Migration 0006: point existing built-in subagent types at their profiles.

Loaded by path because the filename starts with a digit.
"""

import importlib.util
import logging
from pathlib import Path

from gilbert.migrations.runner import MigrationContext


def _load_migration():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "src/gilbert/migrations/0006_point_subagent_types_at_profiles.py"
    spec = importlib.util.spec_from_file_location("m0006", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(storage) -> MigrationContext:
    return MigrationContext(storage=storage, log=logging.getLogger("test"), repo_root=Path("."))


async def test_points_unset_builtins_at_their_profiles(sqlite_storage) -> None:
    mod = _load_migration()
    # A legacy built-in row: empty ai_profile + stale tool fields.
    await sqlite_storage.put(
        "subagent_types",
        "deep-research",
        {"_id": "deep-research", "ai_profile": "", "tool_mode": "include",
         "tools": ["web_search"]},
    )
    # An admin-customized row already pointing at a chosen profile is untouched.
    await sqlite_storage.put(
        "subagent_types",
        "quick-answer",
        {"_id": "quick-answer", "ai_profile": "my-custom"},
    )

    await mod.up(_ctx(sqlite_storage))

    dr = await sqlite_storage.get("subagent_types", "deep-research")
    assert dr["ai_profile"] == "deep-research"
    assert "tool_mode" not in dr and "tools" not in dr

    qa = await sqlite_storage.get("subagent_types", "quick-answer")
    assert qa["ai_profile"] == "my-custom"  # left alone

    # Idempotent.
    await mod.up(_ctx(sqlite_storage))
    assert (await sqlite_storage.get("subagent_types", "deep-research"))["ai_profile"] == "deep-research"
