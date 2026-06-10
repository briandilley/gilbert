# Configurable & Custom Subagent Types — Design Spec

- **Date:** 2026-06-09
- **Status:** Draft (for review)
- **Companion:** `2026-06-09-subagent-types-prompts.md` (the seeded built-in catalog + prompts).
- **Builds on:** the existing `SubagentService` engine (slices 1–7, on `main`).

## 1. Summary

Turn subagent **types** from hardcoded `AgentType` dataclasses into **entity-backed, admin-managed definitions** — exactly how AI profiles work today — so an admin can edit the built-ins and **create custom agent types** in a dedicated **Subagents** settings section, with no code changes.

A type becomes a **self-contained agent definition** that carries everything: model + temperature, tools, round/time budget, system prompt, and — crucially — its **execution mode** (`sync` | `background`) and **delivery** (`inline` | `report_file`). That last part **collapses the special-cased `deep_research` tool** into the engine: "background research that delivers a report" is just a *type configured that way*, not a separate tool/path.

Ship a curated roster of **10 well-prompted built-ins** (researched per persona — see companion). `spawn_agent(type, prompt, model?)` is the **only** tool; the standalone `deep_research` tool and `/research` command are **removed**. The parent AI **routes** to the right agent by matching the task to each type's `description`.

## 2. Goals / non-goals

**Goals:**
- Subagent types stored as entities (`subagent_types` collection), seeded with editable built-ins (protected from deletion), admin CRUD.
- A **Subagents** admin settings section (mirrors `/security/profiles`) to edit built-ins + create/edit/delete custom types.
- A type is self-contained: name, description, model+temperature, tools, max_rounds, max_wall_clock_s, system_prompt, execution_mode, deliver_as, enabled, built_in.
- The engine reads type config from storage; `execution_mode`/`deliver_as` generalize the current `_run_research_background` + report-file logic so *any* type can be background+report.
- `spawn_agent(type, prompt, model?)` is the sole tool; model override per spawn; description-based routing.
- Remove the `deep_research` tool and `/research`.
- Ship the 10-agent catalog with researched prompts + sane settings.

**Non-goals (this spec):**
- **Refactoring `AgentService`** to delegate individual-agent execution to this engine. The design *enables* it (the type system becomes the "run one agent" primitive), but the AgentService refactor is a deliberate **follow-up**, not in scope here.
- Per-user custom types (admin-only management; everyone *uses* enabled types).
- User-defined *tools* (types pick from existing registered tools).
- Versioning/history of type edits.

## 3. Architecture

### 3.1 The type entity (replaces the frozen `AgentType`)

A new mutable dataclass + storage collection `subagent_types`, mirroring `AIContextProfile`/`ai_profiles`:

```python
@dataclass
class SubagentType:
    id: str                       # stable slug (e.g. "software-engineer")
    name: str                     # display ("Software Engineer")
    description: str              # routing hint shown to the parent AI
    system_prompt: str
    backend: str = ""             # model selection (chat picker); "" = default
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    tool_mode: str = "all"        # all | include | exclude
    tools: list[str] = field(default_factory=list)
    max_rounds: int = 12
    max_wall_clock_s: float | None = 300.0
    execution_mode: str = "sync"  # sync | background
    deliver_as: str = "inline"    # inline | report_file
    enabled: bool = True
    built_in: bool = False        # protected: editable + resettable, not deletable
    icon: str = ""
```

The model/temperature/tools fields **reuse the same machinery as AI profiles** — at run time the engine builds a transient `AIContextProfile` from the type (or passes backend/model/temperature overrides + a tool filter into `chat()`), so we don't duplicate backend resolution or `_discover_tools` filtering. (A type is effectively "a profile + a prompt + a budget + a mode.")

### 3.2 Storage, seeding, protection (mirror `_load_profiles`)

- `SubagentTypeStore` (in the SubagentService): `_load_types()` seeds the 10 built-ins on first run if missing, then `_refresh_types()` loads all from `subagent_types` into memory. Built-ins are reconciled like profiles: **seed if missing, preserve user edits**, never overwrite an edited built-in on restart.
- `_UNDELETABLE` = the 10 built-in ids. Delete is rejected for built-ins; **reset-to-default** restores a built-in's shipped values.
- Built-in definitions (ids, names, descriptions, prompts, settings) live in `core/subagents/types.py` as the seed source (the companion doc's catalog), the way `_BUILTIN_PROFILES` does.

### 3.3 Engine: `spawn()` reads the type (and model override)

`spawn(agent_type, prompt, *, conversation_id=None, subagent_id=None, should_stop=None, model_override="", backend_override="")`:
- Look up the `SubagentType` from the store (not the frozen dict).
- Build the system prompt = preamble + `type.system_prompt`.
- Call `AIService.chat(...)` with: the type's tool filter (`tool_mode`/`tools` via a transient profile), `backend_override`/`model_override` (spawn override > type.backend/model), `temperature` from the type, `max_tool_rounds=type.max_rounds`, `headless=True`, `source="subagent"`, plus the watchable-conversation params.
- Wall-clock budget from `type.max_wall_clock_s` (enforced as today).
- The synthesis-fallback (never return empty) stays.

### 3.4 `execution_mode` + `deliver_as` generalize background research

`execute_tool("spawn_agent")`:
- Resolve the type. If `type.execution_mode == "sync"` → run `spawn()` inline and return the result text (as `spawn_agent` does today).
- If `type.execution_mode == "background"` → detach (today's `_run_research_background`, **renamed `_run_agent_background` and made type-driven**): register the run, ensure the child conversation, spawn, then deliver per `type.deliver_as`:
  - `inline` → `append_assistant_message(parent, result)`.
  - `report_file` → write `outputs/<id>-<slug>.md`, deliver an attachment + link + notification (today's report path).
- The standalone `deep_research` tool + `_run_research_background` **are removed**; `deep-research` and `market-analyst` are simply built-in types with `execution_mode=background, deliver_as=report_file`.

### 3.5 `spawn_agent` tool (the only tool)

```
spawn_agent(agent_type: enum[<enabled type ids>], prompt: string, model?: string)
```
- `description` is built dynamically: a line per **enabled** type — `"<name> (<id>): <description>"` — so the model routes by matching the task ("start an agent to do XYZ") to the right type.
- `model` is an optional per-spawn override (passed as `model_override`; backend inferred or `backend:model` form).
- Stays `interactive=True` (no-nesting in headless) and `required_role="user"`.
- Background types return their ack immediately; sync types return the result — same tool, behavior driven by the type.

### 3.6 Admin CRUD (mirror `roles.profile.*`)

WS handlers on the SubagentService (`ws_handlers` capability), admin-gated:
- `subagent.types.list` → all types + the available tool-name list (for the tool multiselect) + the backends/models list reference.
- `subagent.types.save` (create/update; validates id slug, that built-ins keep protected invariants).
- `subagent.types.delete` (rejects built-ins).
- `subagent.types.reset` (restore a built-in to its shipped default).
- All admin-only (`require_admin` on the handler, like profile management).

### 3.7 Frontend — Subagents settings section (mirror `AIProfiles.tsx`)

- A new admin page `frontend/src/components/.../Subagents.tsx` (route under the same area as `/security/profiles`, e.g. `/security/subagents`), listing type cards with edit/reset/delete; a form dialog with: name, description, model+temperature (the chat model picker), tools (tool_mode + checkbox list, like profiles), max rounds, max time, execution mode + deliver-as selects, system prompt textarea, enabled toggle. Built-ins show Reset (not Delete).
- Add the nav entry (mirrors how `/security/profiles` is registered).

## 4. Data flow (a user-launched agent)

1. User: "start an agent to write a PRD for X." → parent AI calls `spawn_agent("product-manager", "<task>")` (matched by description).
2. Type is `sync/inline` → `spawn()` runs it (its model, temp, tools, 12 rounds), returns the PRD text → the parent AI presents it in-turn.
3. "research the EV-charger market" → `spawn_agent("market-analyst", …)` → `background/report_file` → detaches, ack returned, runs, writes the report file, delivers attachment + link + notification, watchable as a child conversation (slice 6/7 behavior, now type-driven).

## 5. Configuration / migration

- The per-type `*_system_prompt` ConfigParams on the SubagentService are **removed** — prompts now live on the type entity (edited in the Subagents UI). A migration seeds the `subagent_types` collection; any existing prompt overrides in config are migrated onto the seeded built-ins' `system_prompt` (best-effort) so current customizations survive.
- `/research` and the `deep_research` tool removal: update docs; the slash registry no longer has `/research`.

## 6. RBAC / security

- Type management (list/save/delete/reset) is **admin-only**. Using an enabled type via `spawn_agent` stays `required_role="user"`.
- Tool selection on a type can only *narrow* what a caller could already use — `_discover_tools` still applies the **caller's** RBAC on top, so a type can't grant a user tools they lack.
- Model override on spawn flows through the same backend resolution; no new backend exposure beyond what the chat model picker already allows.

## 7. Testing

Backend:
- Type store: seeds 10 built-ins; preserves edits across reload; reset restores defaults; built-ins undeletable; custom create/update/delete.
- `spawn()` honors type model/temperature/tools/rounds; spawn `model_override` beats type model.
- `execution_mode`: sync returns inline; background detaches + delivers per `deliver_as` (inline vs report_file); the deep-research/market-analyst types produce a report file + attachment + notification (the migrated background path).
- `spawn_agent` description lists enabled types; disabled types excluded from the enum.
- CRUD RPCs admin-gated; non-admin rejected.
- `deep_research` tool + `/research` gone (no registration).

Frontend (vitest): the Subagents form round-trips a type; built-ins show Reset not Delete; the tool multiselect + model picker populate.

## 8. Build order

1. **Type entity + store + seeding** (`SubagentType`, `subagent_types`, seed the 10 from the catalog, reset/protection). Migration of existing prompt overrides.
2. **Engine reads types** — `spawn()` from the store + model/temp/tools wiring + `model_override`. Keep behavior identical for the two built-ins.
3. **`execution_mode`/`deliver_as`** — generalize `_run_research_background` → `_run_agent_background`; remove `deep_research` tool + `/research`; `spawn_agent` gains `model` + dynamic description.
4. **Admin CRUD RPCs** (list/save/delete/reset, admin-gated).
5. **Frontend Subagents settings** page + nav.
6. **Catalog polish + docs** (validate-architecture, README/CONTEXT, ADR for the type model).

## 9. Open questions (defaults set)

1. **Route location:** `/security/subagents` next to profiles (default) vs a top-level Settings section. Default: next to profiles.
2. **Reset scope:** reset restores all shipped fields of a built-in (default) vs prompt-only.
3. **Model override surface:** `spawn_agent(model=…)` only (default) vs also a per-conversation default. Default: spawn param only.
4. **AgentService:** left untouched this slice; the follow-up refactor will delegate to this engine. Confirmed out of scope.

## 10. Architecture-rules compliance

- Types reuse profile/model/tool machinery (no duplicate backend resolution or hardcoded models — model is admin-selected data, like a profile).
- Prompts are data on entities (no hardcoded prompts in call sites; the `ai_prompt` ConfigParam pattern is replaced by per-type entity fields edited in the UI).
- Capabilities via the resolver; CRUD via `ws_handlers`; admin RBAC enforced.
- Frontend is core admin UI mirroring `AIProfiles.tsx`; nav gated by capability.
- New ADR documenting "subagent types are entity-backed self-contained agent definitions; deep research is a type, not a service."
