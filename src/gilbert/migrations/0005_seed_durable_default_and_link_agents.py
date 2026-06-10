"""Seed the ``durable-default`` subagent type and link existing agents to it.

Durable agents now reference a ``SubagentType`` for execution defaults
(model/tools/budgets/role-prompt). This migration:

1. Seeds the ``durable-default`` built-in type if the catalog row is absent
   (service start also seeds it, but the migration makes the link below safe
   regardless of start ordering).
2. Sets ``agent_type_id="durable-default"`` on every existing ``agents`` row
   that lacks one. Their own ``profile_id``/``tools_*``/``max_tool_rounds``
   keep overriding, so behavior is identical to before the type system.

Idempotent: re-running finds the type already seeded and every agent already
linked, so it is a no-op. A crash mid-run re-executes safely — re-seeding the
same row and re-linking already-linked agents both converge.
"""

from __future__ import annotations

from dataclasses import asdict

from gilbert.core.subagents.types import BUILTIN_SUBAGENT_TYPES
from gilbert.interfaces.storage import Query
from gilbert.migrations.runner import MigrationContext

description = "Seed durable-default subagent type and link existing agents"

_TYPES_COLLECTION = "subagent_types"
_AGENTS_COLLECTION = "agents"
_DEFAULT_TYPE_ID = "durable-default"


async def up(ctx: MigrationContext) -> None:
    # 1. Seed the durable-default type row if missing.
    existing = await ctx.storage.get(_TYPES_COLLECTION, _DEFAULT_TYPE_ID)
    if existing is None:
        seed = BUILTIN_SUBAGENT_TYPES[_DEFAULT_TYPE_ID]
        row = asdict(seed)
        row["id"] = _DEFAULT_TYPE_ID
        await ctx.storage.put(_TYPES_COLLECTION, _DEFAULT_TYPE_ID, row)
        ctx.log.info("migration 0005: seeded %r subagent type", _DEFAULT_TYPE_ID)

    # 2. Link every existing agent that has no type reference yet.
    agents = await ctx.storage.query(Query(collection=_AGENTS_COLLECTION, filters=[]))
    linked = 0
    for row in agents:
        if row.get("agent_type_id"):
            continue
        row["agent_type_id"] = _DEFAULT_TYPE_ID
        await ctx.storage.put(_AGENTS_COLLECTION, row["_id"], row)
        linked += 1
    if linked:
        ctx.log.info("migration 0005: linked %d agent(s) to %r", linked, _DEFAULT_TYPE_ID)
