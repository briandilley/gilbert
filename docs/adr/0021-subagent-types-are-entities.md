# ADR-0021: Subagent types are entity-backed, self-contained agent definitions

**Status:** Accepted  
**Date:** 2026-06-09

## Context

Gilbert previously used a frozen `AgentType` enum plus per-type config params (one `ConfigParam` per prompt per type). This made the catalog hard to extend, impossible to edit at runtime without a restart, and gave admins no control over model selection, tool gating, or execution parameters per type.

Deep research was special-cased as a separate `deep_research` AI tool with its own background execution path (`_run_research_background`). This made the engine logic branch on type identity rather than on data-driven execution modes.

## Decision

1. **Subagent types are entity-backed.** A `SubagentType` dataclass is stored in the `subagent_types` collection (one entity per type). Types are loaded at service start and cached in memory. Admins manage them via `subagent.types.{list,save,delete,reset}` WS RPCs, accessible at `/security/subagents`.

2. **Types are self-contained.** Every parameter that controls a subagent run lives on the type: `system_prompt`, `backend`, `model`, `temperature`, `max_tokens`, `tool_mode`, `tools`, `max_rounds`, `max_wall_clock_s`, `execution_mode` (`sync` | `background`), `deliver_as` (`inline` | `report_file`), `enabled`, `icon`.

3. **Deep research is a type, not a service.** The `deep-research` built-in type (`execution_mode=background`, `deliver_as=report_file`) encodes the same semantics as the old `deep_research` tool and `_run_research_background` path. The `deep_research` tool and `/research` slash command are removed; `spawn_agent` is the only subagent tool.

4. **Built-in types are seeded, not locked.** On first run, `_load_types` seeds the 10 built-in types. Subsequent starts preserve user edits (no overwrite). Admins can reset a built-in to its shipped defaults via `subagent.types.reset`. Built-ins cannot be deleted.

5. **Model is admin-selected data — AI-backend-visibility exception.** The type's `backend` and `model` fields are admin-managed configuration, not runtime user data. Exposing them in the `subagent.types.list` RPC (admin-only) and in the admin settings UI is intentional and does not violate the AI-backend-visibility principle, which applies to user-facing surfaces. The `spawn_agent` tool description does not name specific models; only the type's `name` and `description` are user-visible.

## Consequences

- The `SubagentService` stores types, seeds built-ins, and drives `spawn()` from the type's fields.
- `get_tools()` returns only `spawn_agent` (with a dynamic enum from enabled type IDs); the `deep_research` tool is gone.
- `execution_mode` / `deliver_as` replace the old `_run_research_background` special case.
- The `/security/subagents` admin page (mirrors `/security/profiles`) lets admins view, edit, reset, and create types.
- Plugin-contributed types can be added by seeding entities at plugin start.
