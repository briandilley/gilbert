# AgentService as a coordinator on the subagent engine

**Date:** 2026-06-09
**Status:** Approved design, staged implementation
**Related:** ADR-0021 (subagent types are entities), `docs/superpowers/specs/2026-06-09-subagent-types-design.md`, `docs/architecture/agent-service.md`

## Problem

Gilbert has two systems that each "run an individual agent":

- **`SubagentService`** (`core/services/subagent.py`) — the canonical primitive for an *ephemeral, headless* agent run, configured by a `SubagentType` entity (model/tools/budgets/prompt/execution_mode/deliver_as), admin-managed at `/security/subagents`. Entry point: `spawn()`.
- **`AgentService`** (`core/services/agent.py`) — the older system for *durable, goal-driven* agents (goals, runs, scheduling, heartbeats, peer messaging/delegation, multi-agent orchestration). Entry point: `run_agent_now()` → `_run_agent_internal()`.

Both are already coordinators on top of `AIService.chat()` (the real multi-round tool loop lives in `AIService.chat()` / `agent_loop.run_loop`). But they **duplicate the per-run shaping** around that call — assembling the system prompt, selecting model/profile, building the tool filter, applying round/wall-clock budgets, and (for subagents) the budget-exhaustion synthesis fallback and lifecycle events. They also carry **two parallel config vocabularies** for model/tools/budgets.

Goal: one "run an agent" primitive used by both, with `AgentService` reduced to a coordinator that owns durability, goals, scheduling, and orchestration, and **reuses the subagent type system** instead of duplicating execution config.

## Key constraint discovered

**Durable-agent runs cannot literally delegate to `spawn()`.** `spawn()` always calls `ai.chat(headless=True)`, and `headless=True` strips every `interactive=True` tool — the deliberate *no-nesting* gate (`spawn_agent` and the future `request_user_input` are interactive). Durable agents run **non-headless** on purpose: they spawn peers, `agent_delegate`, and ask the user. A durable agent calling `spawn()` would lose exactly the tools that make it durable.

Therefore "delegate to the subagent engine" means: **extract the execution primitive that sits *underneath* `spawn()`** and have both `spawn()` and `_run_agent_internal()` call it, parameterized on `headless` (and the full run spec). The engine never forces headless.

## Design

### 1. One execution engine, two coordinators

New module `core/agent_run/` (a core module, not a service; depends only on `interfaces/`). Two pieces:

**`RunSpec`** — a dumb, fully-resolved dataclass describing one run:

```
system_prompt: str
ai_profile: str = ""           # profile name; "" = none
model: str = ""                # raw override (layers over profile)
backend_override: str = ""
temperature: float | None = None
tool_filter: tuple[str, list[str]] | None = None
max_rounds: int | None = None
max_wall_clock_s: float | None = None
headless: bool = False
ai_call: str = ""
source: str = ""
# callbacks (durable runs use these; subagent runs mostly don't)
between_rounds_callback: Any = None
mid_round_interrupt: Callable[[], bool] | None = None
should_stop_callback: Callable[[], bool] | None = None
# delivery context
conversation_parent_id: str = ""
conversation_title: str = ""
# synthesis fallback toggle (subagent path uses it; durable path off)
synthesize_on_empty: bool = False
```

**`AgentRunEngine.run(spec, *, ai, user_ctx, conversation_id, subagent_id, on_event) -> RunResult`** — the shared runner:

1. Folds `spec.max_wall_clock_s` into a deadline-aware `should_stop` (combining with `spec.should_stop_callback`), exactly as `spawn()` does today.
2. Emits `chat.stream.subagent_started` via the injected `on_event` hook (the engine does not import the event bus; the caller passes a publish callback + routing).
3. Calls `ai.chat(...)` with every field of the spec passed through (the `chat()` API already accepts `ai_profile`, `model`, `backend_override`, `temperature`, `tool_filter`, `max_tool_rounds`, `headless`, the three callbacks, `source`, `conversation_parent_id`, `conversation_title`).
4. If `spec.synthesize_on_empty` and the run wasn't stopped and the result is empty/near-empty and a `conversation_id` exists → the one-shot synthesis turn (`max_tool_rounds=2`), unchanged from today.
5. Emits `chat.stream.subagent_completed` / `_stopped` / `_failed`.
6. Returns `RunResult` (final text + the underlying `ChatTurnResult` usage so durable callers get tokens/cost).

`SubagentService.spawn()` builds a **headless** `RunSpec` (with `synthesize_on_empty=True`) → engine. `AgentService._run_agent_internal()` builds a **non-headless** `RunSpec` (rich prompt, core forced tools, its callbacks, `synthesize_on_empty=False`) → engine, and keeps everything genuinely durable around the call: Run-row lifecycle, cost accounting + cap auto-disable, inbox drain, ContextVar setup (`_active_agent_id`, `_active_delegation_chain`, `_workspace_conversation_id`), delegation-future resolution, conversation-row patching.

The lifecycle events are emitted by the engine for both paths, so durable runs additionally start emitting `chat.stream.subagent_*` — acceptable and useful (live run cards), and additive (no existing consumer breaks). `AgentService`'s own `agent.run.started/completed` events remain, emitted by `AgentService` as today.

### 2. Config relationship: `Agent` → `SubagentType`, "type provides, Agent overrides"

`Agent` gains `agent_type_id: str` (references a `SubagentType`). One consistent rule everywhere: **the referenced type supplies execution defaults; an Agent field overrides when set.**

| Dimension | Type provides | Agent override |
|---|---|---|
| Model/sampling | `ai_profile` (new, §3) → raw `model`/`backend`/`temperature` fallback | `profile_id` when set (`effective_profile = agent.profile_id or type.ai_profile`) |
| Tools | `tool_mode` + `tools` → base `tool_filter` | `tools_include`/`tools_exclude` refine; `_CORE_AGENT_TOOLS` **always force-added** for durable runs |
| Round budget | `max_rounds` | `max_tool_rounds` when set |
| Wall-clock | `max_wall_clock_s` | Agent value; migrated agents get `None` (unlimited — preserves today) |
| Prompt | `system_prompt` as **base layer** (§4) | persona/system_prompt/procedural_rules + dynamic blocks layer on top |
| `execution_mode` / `deliver_as` | **ignored for durable runs** (durability is AgentService's job); only govern the ephemeral `spawn()` path | — |

`AgentService` reads the type catalog through a new capability protocol, never importing the concrete `SubagentService`.

### 3. `SubagentType` becomes profile-driven

Add `ai_profile: str = ""` to `SubagentType`. A type names an AI profile (model-agnostic, preferred) and falls back to its raw `backend`/`model`/`temperature` only when `ai_profile == ""` (back-compat for the 10 built-ins, which keep working unchanged). `spawn()`'s per-call `model_override`/`backend_override` still win for one-off runs (they layer at the call level above the profile). The admin form (`Subagents.tsx`) gains an AI-profile dropdown populated via `ConfigParam.choices_from` / the profile catalog (per the dropdowns-for-known-choices rule). Durable Agents inherit the type's profile and override with `profile_id`.

### 4. Prompt composition: type prompt is a base layer

Durable prompt assembly becomes:

```
[type.system_prompt]   ← role base (new, prepended)
persona + system_prompt + procedural_rules
+ long-term memory + active assignments + heartbeat/delegation blocks   ← unchanged
```

The migration gives every existing agent the neutral `durable-default` type whose `system_prompt` is **empty**, so the base layer contributes nothing until an admin deliberately points an agent at a richer type. Additive and opt-in — zero change to any existing agent's effective prompt.

### 5. New capability protocol

`interfaces/subagent.py` gains a `@runtime_checkable SubagentCatalog` protocol exposing the read surface `AgentService` needs:

```
def list_types(self) -> list[SubagentType]: ...
def get_type(self, type_id: str) -> SubagentType | None: ...
```

`SubagentService` already implements these methods; it declares the `subagent` capability and now satisfies the typed protocol. `AgentService` resolves it via `resolver.get_capability("subagent")` + `isinstance(svc, SubagentCatalog)` (degrading gracefully to its own override fields if the subagent service is disabled — i.e. a missing type falls back to neutral defaults). `SubagentType` moves/stays importable from `interfaces/` so the protocol can name it without a layer violation (the dataclass is shared data → belongs in `interfaces/`).

### 6. Migration & backward compatibility

- New built-in type **`durable-default`**: `ai_profile="standard"`, `tool_mode="all"`, empty `system_prompt`, `max_rounds=50` (today's `default_max_tool_rounds`), `max_wall_clock_s=None`, `execution_mode`/`deliver_as` irrelevant for durable use. Seeded alongside the existing 10 built-ins.
- Migration `0005_seed_durable_default_and_link_agents.py`: idempotent; seeds `durable-default` if absent, then sets `agent_type_id="durable-default"` on every existing `Agent` row that lacks one. Existing `profile_id`/`tools_*`/`max_tool_rounds` keep overriding → **provably identical behavior**.
- Entity store → no SQL/schema migration. Goals, runs, heartbeat, delegation, peer messaging, war-rooms, deliverables, dependencies, and all existing durable-agent UI are untouched.

### 7. Architecture compliance

- Engine lives in `core/agent_run/`, depends only on `interfaces/`; both services import it (core → core/interfaces is legal).
- No `integrations/` or `web/` imports in the engine.
- `AgentService` ↔ `SubagentService` decoupled via the `SubagentCatalog` protocol (capability protocol, not concrete class).
- `SubagentType` dataclass is shared data → `interfaces/`.
- All AI prompts remain `ConfigParam(ai_prompt=True)` on their owning service; the type system's prompts stay entity-managed (ADR-0021).
- Model-agnostic preserved and strengthened: both systems now select models via AI profiles.

## Staging (each slice ships working software; TDD throughout)

1. **Extract `AgentRunEngine`** from `spawn()`; route `SubagentService` through it. Pure refactor, no external change. New engine unit tests + existing subagent tests green.
2. **Profile-ify `SubagentType`** — add `ai_profile`, profile-based RunSpec with raw-model fallback, admin-UI profile dropdown, seed/no-op migration handling. Subagent runs become profile-driven; built-ins unchanged.
3. **Route `AgentService._run_agent_internal` through the engine** — build the durable non-headless `RunSpec`; durable agents behave identically. The big internal consolidation; no external change.
4. **`Agent` references a `SubagentType`** — add `agent_type_id` + `SubagentCatalog` protocol, `durable-default` built-in, migration, agent-edit-form type picker, type-as-defaults + Agent-overrides + base-layer prompt. New capability; behavior preserved for existing agents.

Slices 1–3 are externally invisible; slice 4 adds the feature with back-compat. Run the `validate-architecture` skill at the end of each slice.

## Out of scope / non-goals

- No change to `agent_loop.run_loop` or the core tool-use loop.
- No change to goals/war-room/deliverable/dependency semantics.
- No removal of `Agent.profile_id`/`tools_*`/`max_tool_rounds` — they remain as overrides (keeps behavior and the model-agnostic profile indirection).
- No new orchestration features; orchestration stays where it is, now sitting on the shared single-run primitive.
