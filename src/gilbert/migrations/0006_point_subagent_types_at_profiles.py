"""Point existing built-in subagent-type rows at their seeded AI profiles.

Subagent types no longer carry their own ``tool_mode``/``tools`` — tool gating
(and model selection) now comes from the type's referenced AI profile. Built-in
types seeded before this change have an empty ``ai_profile`` and a stale tool
list; on load the dropped fields are ignored, which would silently degrade them
to *all tools* until the profile reference is set.

For each shipped built-in type whose stored row has no ``ai_profile``, set it to
the seed's ``ai_profile`` (the same-named profile seeded by the AI service).
Admin-chosen profiles are left untouched. Idempotent: once set, re-running skips.
"""

from __future__ import annotations

from gilbert.core.subagents.types import BUILTIN_SUBAGENT_TYPES
from gilbert.migrations.runner import MigrationContext

description = "Point existing built-in subagent types at their seeded AI profiles"

_TYPES_COLLECTION = "subagent_types"


async def up(ctx: MigrationContext) -> None:
    fixed = 0
    for type_id, seed in BUILTIN_SUBAGENT_TYPES.items():
        if not seed.ai_profile:
            continue
        row = await ctx.storage.get(_TYPES_COLLECTION, type_id)
        if row is None or row.get("ai_profile"):
            continue
        row["ai_profile"] = seed.ai_profile
        # Drop the now-defunct tool fields from the stored row (cosmetic; the
        # loader already ignores unknown keys).
        row.pop("tool_mode", None)
        row.pop("tools", None)
        await ctx.storage.put(_TYPES_COLLECTION, type_id, row)
        fixed += 1
    if fixed:
        ctx.log.info("migration 0006: pointed %d subagent type(s) at their profiles", fixed)
