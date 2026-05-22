"""Migration: namespace stored speaker ids and rewrite legacy speaker.backend config.

Idempotent — rows already namespaced are skipped; configs already containing
``primary_backend`` are skipped.

**Runner note:** The migration runner passes a ``MigrationContext`` (not a raw
``storage`` + ``backends`` pair), so this migration discovers backends by
importing the bundled integrations and calling
``SpeakerBackend.registered_backends()``.  For each registered backend it
instantiates a temporary instance, calls ``await inst.initialize({})``, queries
``list_speakers()``, then ``await inst.close()``.  Backends that don't
initialise cleanly with an empty config (e.g. a third-party plugin that requires
auth credentials) log a DEBUG message and are skipped — their speakers' alias
rows will be left bare until either (a) the migration is re-run after config is
in place, or (b) the ``resolve_speaker_name`` compat shim in SpeakerService
handles them at runtime.
"""

from __future__ import annotations

import logging

from gilbert.interfaces.speaker import SpeakerBackend, SpeakerInfo
from gilbert.interfaces.storage import Query
from gilbert.migrations.runner import MigrationContext

logger = logging.getLogger(__name__)

description = "Namespace stored speaker alias IDs and rewrite legacy speaker.backend config"


async def up(ctx: MigrationContext) -> None:
    """Run the migration.

    Builds a live ``{backend_name: [SpeakerInfo, ...]}`` map by temporarily
    instantiating every registered SpeakerBackend, then rewrites bare alias
    rows and the legacy ``gilbert.config/speaker`` entity.
    """
    # Trigger registration of the bundled vendor-free backends.
    import gilbert.integrations.browser_speaker  # noqa: F401
    import gilbert.integrations.local_speaker  # noqa: F401

    backends_map = await _load_backends(ctx)
    await _namespace_aliases(ctx, backends_map)
    await _rewrite_legacy_config(ctx)


async def _load_backends(
    ctx: MigrationContext,
) -> dict[str, list[SpeakerInfo]]:
    """Instantiate each registered SpeakerBackend with an empty config.

    Returns a mapping of ``{backend_name: [SpeakerInfo, ...]}``.  Backends
    that fail to initialise or list speakers are skipped with a DEBUG log.
    """
    result: dict[str, list[SpeakerInfo]] = {}
    for name, cls in SpeakerBackend.registered_backends().items():
        inst = None
        try:
            inst = cls()
            await inst.initialize({})
            speakers = await inst.list_speakers()
            result[name] = speakers
            ctx.log.debug("migration 0001: backend %r contributed %d speaker(s)", name, len(speakers))
        except Exception as exc:
            ctx.log.debug(
                "migration 0001: backend %r skipped during alias discovery (%s)",
                name, exc,
            )
        finally:
            if inst is not None:
                try:
                    await inst.close()
                except Exception:
                    pass
    return result


async def _namespace_aliases(
    ctx: MigrationContext,
    backends_map: dict[str, list[SpeakerInfo]],
) -> None:
    """Rewrite bare ``speaker_id`` values in the ``speaker_aliases`` collection."""
    rows = await ctx.storage.query(Query(collection="speaker_aliases"))
    for row in rows:
        sid = row.get("speaker_id", "")
        if ":" in str(sid):
            # Already namespaced — idempotent skip.
            continue

        matches: list[str] = [
            name
            for name, speakers in backends_map.items()
            if any(s.speaker_id == sid for s in speakers)
        ]

        if not matches:
            ctx.log.info(
                "migration 0001: no loaded backend recognises alias %r (id=%r) — left bare",
                row.get("alias"), sid,
            )
            continue

        if len(matches) > 1:
            chosen = "sonos" if "sonos" in matches else sorted(matches)[0]
            ctx.log.warning(
                "migration 0001: alias %r matches multiple backends %s — chose %r",
                row.get("alias"), matches, chosen,
            )
        else:
            chosen = matches[0]

        row["speaker_id"] = f"{chosen}:{sid}"
        key = row.get("alias") or row.get("_id") or str(sid)
        await ctx.storage.put("speaker_aliases", key, row)
        ctx.log.info(
            "migration 0001: namespaced alias %r: %r → %r",
            row.get("alias"), sid, row["speaker_id"],
        )


async def _rewrite_legacy_config(ctx: MigrationContext) -> None:
    """Rewrite a legacy ``gilbert.config/speaker`` row that uses ``backend: <name>``.

    The new schema uses ``primary_backend: <name>`` and a ``backends``
    sub-dict.  Rows that already contain ``primary_backend`` are left alone.
    """
    row = await ctx.storage.get("gilbert.config", "speaker")
    if not row:
        return
    if "backend" not in row or "primary_backend" in row:
        # Already migrated or uses new schema — idempotent skip.
        return

    backend_name = row.pop("backend")
    row.setdefault("backends", {}).setdefault(backend_name, {})["enabled"] = True
    row["primary_backend"] = backend_name
    await ctx.storage.put("gilbert.config", "speaker", row)
    ctx.log.info(
        "migration 0001: rewrote legacy speaker.backend=%r → primary_backend + backends.%s.enabled",
        backend_name, backend_name,
    )
