"""Carry legacy per-type subagent prompt overrides onto ``subagent_types`` rows.

Before subagent types became entity-backed, a per-type system-prompt override
lived in the ``subagent`` config section under
``<type_id_with_underscores>_system_prompt`` (e.g. ``deep_research_system_prompt``
for the ``deep-research`` type). Those config params are gone; prompts now live
on the ``subagent_types`` entity, seeded at service start by
``SubagentService._load_types``.

This migration only carries over an operator's *customized* prompts: for each
legacy ``*_system_prompt`` key present in the ``subagent`` config, it upserts the
corresponding built-in type row (full shipped seed) with the override applied,
then strips the key from the config section. Seeding of un-customized built-ins
is left to ``_load_types``.

Idempotent: after the keys are stripped, re-running finds nothing to carry. A
crash mid-run re-executes safely — an already-applied override is simply
re-written from the same input until the config key is removed (the last step).
"""

from __future__ import annotations

from dataclasses import asdict

from gilbert.core.subagents.types import builtin_seed_list
from gilbert.migrations.runner import MigrationContext

description = "Carry legacy per-type subagent prompt overrides onto subagent_types rows"

_TYPES_COLLECTION = "subagent_types"
_PROMPT_SUFFIX = "_system_prompt"


def _legacy_key(type_id: str) -> str:
    """``general-purpose`` -> ``general_purpose_system_prompt``."""
    return f"{type_id.replace('-', '_')}{_PROMPT_SUFFIX}"


async def up(ctx: MigrationContext) -> None:
    config = await ctx.storage.get("gilbert.config", "subagent")
    if not config:
        return

    seeds = {t.id: t for t in builtin_seed_list()}
    carried = False
    for type_id, seed in seeds.items():
        key = _legacy_key(type_id)
        if key not in config:
            continue
        override = config.pop(key)
        # Upsert the full shipped seed row with the operator's override applied.
        # If the row already exists (re-run after a partial apply), preserve any
        # other edits the operator may have made by patching just the prompt.
        existing = await ctx.storage.get(_TYPES_COLLECTION, type_id)
        row = existing if existing is not None else asdict(seed)
        row["id"] = type_id
        row["system_prompt"] = override
        await ctx.storage.put(_TYPES_COLLECTION, type_id, row)
        carried = True
        ctx.log.info(
            "migration 0004: carried legacy %s onto subagent type %r",
            key, type_id,
        )

    if carried:
        # Persist the stripped config so the (now-removed) keys don't linger.
        await ctx.storage.put("gilbert.config", "subagent", config)
