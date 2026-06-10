# AgentService Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `AgentService` a coordinator that delegates individual-agent execution to a shared engine reused by `SubagentService`, and lets durable agents reference a `SubagentType` for execution config.

**Architecture:** Extract the per-run shaping under `SubagentService.spawn()` into a shared `core/agent_run/` engine (`RunSpec` + `AgentRunEngine`), parameterized on `headless`. Profile-ify `SubagentType`. Route both `spawn()` and `AgentService._run_agent_internal()` through the engine. Add `Agent.agent_type_id` referencing a `SubagentType` via a `SubagentCatalog` capability protocol, type-provides/Agent-overrides.

**Tech Stack:** Python 3.12, uv, pytest, SQLite entity store, React/TS SPA.

---

## File Structure

- Create: `src/gilbert/interfaces/subagent.py` — `SubagentType` dataclass (moved here as shared data) + `SubagentCatalog` runtime_checkable protocol.
- Create: `src/gilbert/core/agent_run/__init__.py` — exports `RunSpec`, `RunResult`, `AgentRunEngine`.
- Create: `src/gilbert/core/agent_run/engine.py` — the engine + dataclasses.
- Modify: `src/gilbert/core/subagents/types.py` — re-export `SubagentType` from interfaces; add `ai_profile` field; `durable-default` built-in.
- Modify: `src/gilbert/core/services/subagent.py` — `spawn()` builds `RunSpec` → engine; profile passthrough; protocol conformance.
- Modify: `src/gilbert/core/services/agent.py` — `_run_agent_internal()` builds durable `RunSpec` → engine; `agent_type_id` resolution; base-layer prompt.
- Modify: `src/gilbert/interfaces/agent.py` — add `agent_type_id` to `Agent` + (de)serialize.
- Create: `src/gilbert/migrations/0005_seed_durable_default_and_link_agents.py`.
- Modify: `frontend/src/types/subagent.ts`, `frontend/src/components/roles/Subagents.tsx` — `ai_profile` dropdown.
- Modify: `frontend/src/types/agent.ts`, `frontend/src/components/agent/AgentEditForm.tsx` — type picker.
- Tests: `tests/unit/core/test_agent_run_engine.py`, plus additions to existing subagent/agent test modules.

---

## Slice 1 — Extract `AgentRunEngine`

### Task 1: `RunSpec` / `RunResult` / `AgentRunEngine` skeleton with passthrough

**Files:**
- Create: `src/gilbert/core/agent_run/engine.py`, `src/gilbert/core/agent_run/__init__.py`
- Test: `tests/unit/core/test_agent_run_engine.py`

- [ ] **Step 1: Failing test** — engine calls `ai.chat` with the spec fields and returns its text.

```python
import pytest
from gilbert.core.agent_run import RunSpec, AgentRunEngine

class _FakeAI:
    def __init__(self): self.calls = []
    async def chat(self, **kw):
        self.calls.append(kw)
        from gilbert.interfaces.ai import ChatTurnResult
        return ChatTurnResult(response_text="hello", conversation_id=kw.get("conversation_id") or "c1")

@pytest.mark.asyncio
async def test_engine_passes_spec_to_chat():
    ai = _FakeAI()
    eng = AgentRunEngine()
    spec = RunSpec(system_prompt="sys", ai_profile="standard", tool_filter=("all", []),
                   max_rounds=7, headless=True, ai_call="subagent.x")
    res = await eng.run(spec, ai=ai, user_ctx=None, conversation_id="c1")
    assert res.text == "hello"
    call = ai.calls[0]
    assert call["system_prompt"] == "sys"
    assert call["ai_profile"] == "standard"
    assert call["max_tool_rounds"] == 7
    assert call["headless"] is True
    assert call["tool_filter"] == ("all", [])
```

- [ ] **Step 2: Run, verify import/attr failure.** `uv run pytest tests/unit/core/test_agent_run_engine.py -v`
- [ ] **Step 3: Implement** `RunSpec` (fields per spec §1), `RunResult(text: str, chat_result: ChatTurnResult|None, was_stopped: bool)`, and `AgentRunEngine.run()` that maps `spec.max_rounds`→`max_tool_rounds` and passes every field through to `ai.chat`. `on_event` defaults to `None` (no events emitted).
- [ ] **Step 4: Run, pass.**
- [ ] **Step 5: Commit** `feat(agent_run): RunSpec + AgentRunEngine passthrough`.

### Task 2: wall-clock deadline folding

- [ ] **Step 1: Failing test** — with `max_wall_clock_s=0`, the `should_stop_callback` passed to `chat` returns True immediately; with `None`, the caller's stop is passed through unchanged.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3: Implement** the deadline closure exactly as `spawn()` lines 795-802, combining `spec.should_stop_callback`.
- [ ] **Step 4/5: pass + commit.**

### Task 3: lifecycle events via injected `on_event`

- [ ] **Step 1: Failing test** — pass an `on_event` recorder; assert `subagent_started` then `subagent_completed`; on `chat` raising, assert `subagent_failed` and re-raise; when `was_stopped`, assert `subagent_stopped`.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3: Implement** event emission (engine calls `await on_event(event_type, payload)` if provided). Payload keys: `subagent_id`, `agent_type` (from `spec.ai_call`-derived or a `spec.agent_type` field — add `agent_type: str = ""` to RunSpec), plus caller-supplied routing merged by the caller's `on_event` closure.
- [ ] **Step 4/5: pass + commit.**

### Task 4: synthesis fallback

- [ ] **Step 1: Failing test** — `synthesize_on_empty=True`, fake `chat` returns text `"x"` (<80 chars) first call then `"FULL"` on synthesis; with a `conversation_id`, result text == `"FULL"` and a second `chat` call with `max_tool_rounds=2` happened. With `synthesize_on_empty=False`, no second call.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3: Implement** the synthesis block from `spawn()` lines 844-875, gated on `spec.synthesize_on_empty`.
- [ ] **Step 4/5: pass + commit.**

### Task 5: route `spawn()` through the engine

**Files:** Modify `src/gilbert/core/services/subagent.py`

- [ ] **Step 1:** Run existing subagent suite to capture green baseline: `uv run pytest tests/unit/test_subagent_service.py -q`.
- [ ] **Step 2: Refactor** `spawn()` body (lines 780-882) to build a `RunSpec(system_prompt=f"{self._preamble}\n\n{t.system_prompt}", model=model_override or t.model, backend_override=backend_override or t.backend, temperature=t.temperature, tool_filter=(t.tool_mode, list(t.tools)), max_rounds=t.max_rounds, max_wall_clock_s=t.max_wall_clock_s, headless=True, ai_call=f"subagent.{t.id}", source="subagent", should_stop_callback=should_stop, conversation_parent_id=..., conversation_title=..., synthesize_on_empty=True, agent_type=t.id)` and call `self._engine.run(spec, ai=self._ai, user_ctx=user_ctx, conversation_id=conversation_id, subagent_id=subagent_id, on_event=self._make_on_event())`. `_make_on_event` closes over `self._event_routing()` and `self._publish_event`. Instantiate `self._engine = AgentRunEngine()` in `start()`.
- [ ] **Step 3:** Run full subagent suite — must stay green with zero behavioral change.
- [ ] **Step 4: Commit** `refactor(subagent): route spawn() through AgentRunEngine`.

---

## Slice 2 — Profile-ify `SubagentType`

### Task 6: move `SubagentType` to interfaces + add `ai_profile`

**Files:** Create `src/gilbert/interfaces/subagent.py`; modify `src/gilbert/core/subagents/types.py`.

- [ ] **Step 1: Failing test** in `tests/unit/test_subagent_types.py` — `SubagentType` importable from `gilbert.interfaces.subagent`; default `ai_profile == ""`; `core.subagents.types.SubagentType is interfaces.subagent.SubagentType`.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3: Implement** — move the dataclass to `interfaces/subagent.py`, add `ai_profile: str = ""` (after `system_prompt`), re-export from `core/subagents/types.py` (`from gilbert.interfaces.subagent import SubagentType`). Update `_type_to_dict`/`_type_from_dict` in `subagent.py` to round-trip `ai_profile`.
- [ ] **Step 4/5: pass + commit.**

### Task 7: `spawn()` passes `ai_profile`

- [ ] **Step 1: Failing test** — a type with `ai_profile="fast"` and `model=""` causes `chat` to receive `ai_profile="fast"`; a type with `ai_profile=""` and `model="m"` receives `ai_profile=""`, `model="m"`; `model_override` still wins.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3: Implement** — add `ai_profile=t.ai_profile` to the `RunSpec` in `spawn()`; engine passes it to `chat`. (Raw `model`/`backend`/`temperature` already layer above the profile in `chat`.)
- [ ] **Step 4/5: pass + commit.**

### Task 8: admin form profile dropdown

**Files:** modify `subagent.py` `_ws_types_list` to also return `all_profiles`; `frontend/src/types/subagent.ts` (+`ai_profile`); `frontend/src/components/roles/Subagents.tsx`.

- [ ] **Step 1: Failing test** (`tests/unit/test_subagent_service.py`) — `subagent.types.list` response includes `all_profiles` (list of profile names) sourced from the AI service's profile catalog via the `configuration`/`ai_chat` capability.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3: Implement** backend: resolve profile names (reuse the same source `choices_from="ai_profiles"` resolves to). Frontend: add `ai_profile` to `SubagentTypeDTO`, render a `<select>` of `all_profiles` (blank option = use raw model fields).
- [ ] **Step 4:** `uv run pytest tests/unit/test_subagent_service.py -q` + targeted vitest if present.
- [ ] **Step 5: Commit** `feat(subagent): AI-profile selection on subagent types`.

---

## Slice 3 — Route `AgentService` through the engine

### Task 9: build durable `RunSpec` and call the engine

**Files:** modify `src/gilbert/core/services/agent.py` (`_run_agent_internal` lines ~2225-2277).

- [ ] **Step 1: Baseline** — `uv run pytest tests/unit/test_agent_service.py tests/unit/test_heartbeat.py tests/unit/test_agent_peer_messaging.py tests/unit/test_agent_inbox.py -q` green.
- [ ] **Step 2: Failing test** (`tests/unit/test_agent_service.py`) — after a run, the fake AI received the non-headless call (`headless` falsy) with the agent's assembled `system_prompt`, `ai_profile == agent.profile_id`, `max_tool_rounds == agent.max_tool_rounds`, and the between-rounds/interrupt/should-stop callbacks wired (assert they are the same callables the service built). Construct via the existing `started_agent_service` fixture.
- [ ] **Step 3:** Replace the direct `self._ai.chat(...)` (lines 2225-2236) with: build `RunSpec(system_prompt=system_prompt, ai_profile=a.profile_id, tool_filter=self._tool_filter_for(a), max_rounds=a.max_tool_rounds or None, max_wall_clock_s=None, headless=False, ai_call=_AI_CALL_NAME, between_rounds_callback=_between_rounds, mid_round_interrupt=_interrupt_check, should_stop_callback=_should_stop_check, synthesize_on_empty=False, agent_type=a.id)` then `result_obj = await self._engine.run(spec, ai=self._ai, user_ctx=user_ctx, conversation_id=a.conversation_id or None, subagent_id=run.id, on_event=self._make_on_event(a))`; `result = result_obj.chat_result`. Keep all surrounding durable logic (lines 2245-2311) unchanged. Add `self._engine = AgentRunEngine()` in `start()`. `_tool_filter_for(a)` derives `(mode, names)` from `tools_include`/`tools_exclude` (include→`("include", core∪include)`, exclude→`("exclude", exclude)`, none→`("all", [])`); core tools still force-added downstream by existing `_compute_allowed_tool_names` semantics — keep that path authoritative by passing `tool_filter=None` if computing it risks divergence. **Preferred:** pass `tool_filter=None` and leave tool gating exactly as today (the engine change is only about *where* `chat` is called), so this slice is a pure call-site move with zero tool-behavior risk.
- [ ] **Step 4:** Run the baseline suites — all green, identical behavior.
- [ ] **Step 5: Commit** `refactor(agent): route durable runs through AgentRunEngine`.

> Note: durable runs now also emit `chat.stream.subagent_*` via the engine's `on_event`. Add a test asserting `agent.run.started/completed` still fire (unchanged) and that the new events are additive.

---

## Slice 4 — `Agent` references a `SubagentType`

### Task 10: `SubagentCatalog` protocol

**Files:** `src/gilbert/interfaces/subagent.py`.

- [ ] **Step 1: Failing test** (`tests/unit/test_subagent_service.py`) — `isinstance(subagent_service, SubagentCatalog)` is True.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3:** Add `@runtime_checkable class SubagentCatalog(Protocol)` with `list_types`/`get_type`. (Service already implements them.)
- [ ] **Step 4/5: pass + commit.**

### Task 11: `Agent.agent_type_id`

**Files:** `src/gilbert/interfaces/agent.py`.

- [ ] **Step 1: Failing test** (`tests/unit/test_agent_entities.py`) — `Agent` default `agent_type_id == "durable-default"`; round-trips through `_agent_to_dict`/`_agent_from_dict`.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3:** Add field + (de)serialize (default `"durable-default"`).
- [ ] **Step 4/5: pass + commit.**

### Task 12: `durable-default` built-in type

**Files:** `src/gilbert/core/subagents/types.py`.

- [ ] **Step 1: Failing test** (`tests/unit/test_subagent_types.py`) — catalog includes `durable-default` with `ai_profile="standard"`, `tool_mode="all"`, `system_prompt==""`, `max_rounds==50`, `max_wall_clock_s is None`, `built_in=True`.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3:** Add the seed entry to `builtin_seed_list()` / `BUILTIN_SUBAGENT_TYPES`.
- [ ] **Step 4/5: pass + commit.**

### Task 13: type-as-defaults resolution + base-layer prompt

**Files:** `src/gilbert/core/services/agent.py`.

- [ ] **Step 1: Failing test** (`tests/unit/test_agent_service.py`) — give the fixture a `SubagentCatalog` fake exposing a type `t1(ai_profile="fast", system_prompt="ROLE", max_rounds=9)`. An agent with `agent_type_id="t1"`, `profile_id=""`, `max_tool_rounds=0` runs → `chat` receives `ai_profile=="fast"`, `max_tool_rounds==9`, and `system_prompt` starts with `"ROLE\n\n---\n\n"`. With agent `profile_id="own"`, `chat` receives `ai_profile=="own"` (override wins).
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3:** Resolve `self._subagents = resolver.get_capability("subagent")` in `start()` (guard `isinstance(.., SubagentCatalog)`, else `None`). Add `_resolve_type(a) -> SubagentType | None`. In `_run_agent_internal`/`_build_system_prompt`: prepend `type.system_prompt` as the first `parts` entry when non-empty; compute `ai_profile = a.profile_id or (type.ai_profile if type else "")`; `max_rounds = a.max_tool_rounds or (type.max_rounds if type else None)`; `max_wall_clock_s = type.max_wall_clock_s if type else None`. All override rules per spec §2. When `self._subagents is None` or type missing → fall back to today's behavior (Agent fields only), so it degrades gracefully.
- [ ] **Step 4:** Run agent suites green.
- [ ] **Step 5: Commit** `feat(agent): durable agents reference a SubagentType for execution defaults`.

### Task 14: migration

**Files:** `src/gilbert/migrations/0005_seed_durable_default_and_link_agents.py`; test `tests/unit/test_migration_link_agents.py`.

- [ ] **Step 1: Failing test** — running `up(ctx)` with two existing agents (one already having a type id) seeds `durable-default` and sets `agent_type_id="durable-default"` only on the one lacking it; idempotent on re-run.
- [ ] **Step 2: Run/fail.**
- [ ] **Step 3:** Implement idempotent `up()` mirroring `0004`'s structure (seed type if absent via the types collection; query `agents`, patch rows missing/empty `agent_type_id`).
- [ ] **Step 4/5: pass + commit.**

### Task 15: agent edit form type picker

**Files:** `frontend/src/types/agent.ts`, `frontend/src/components/agent/AgentEditForm.tsx`, `frontend/src/api/agents.ts` (if a types list fetch is needed — reuse `agents.tools.list_available` pattern or a new `agents.types.list` that proxies `SubagentCatalog.list_types`).

- [ ] **Step 1:** Add `agent_type_id` to the `Agent` TS type + create/update payloads.
- [ ] **Step 2:** Add a `<select>` of subagent types to the form (label "Execution type"), defaulting to `durable-default`, with helper text "Supplies model/tools/budgets; your fields below override."
- [ ] **Step 3:** Backend WS handler `agents.create`/`agents.update` already pass through arbitrary fields — ensure `agent_type_id` is accepted/validated (must reference an existing type or `durable-default`).
- [ ] **Step 4:** `uv run pytest tests/unit/test_agents_ws_rpcs.py -q` + vitest.
- [ ] **Step 5: Commit** `feat(frontend): execution-type picker on agent edit form`.

---

## Finalization

- [ ] Run full suite: `uv run pytest -q`.
- [ ] `uv run ruff check src/ tests/` + `uv run mypy src/`.
- [ ] Invoke `validate-architecture` skill as an audit; fix findings (esp. layer rules around `interfaces/subagent.py`, capability-protocol use, README/glossary freshness — add `agent_type_id` + the engine to `docs/architecture/agent-service.md` and the Core glossary).
- [ ] Update `docs/architecture/agent-service.md` and `src/gilbert/CONTEXT.md` (vocabulary: AgentRunEngine, RunSpec, execution type).

## Self-Review notes

- Spec §1–§7 each map to slices: §1→Slice1+Task9; §2→Task13; §3→Tasks6-8; §4→Task13; §5→Task10; §6→Tasks11-12,14; §7→Finalization audit.
- Override naming consistent: `ai_profile`, `max_rounds`, `max_wall_clock_s`, `tool_filter`, `agent_type_id` used identically across tasks.
- Tool-gating risk in Task 9 explicitly defused by passing `tool_filter=None` (keep existing gating authoritative); type-derived tool defaults are deferred to Task 13 only for budgets/profile/prompt, leaving tool gating on the existing `_compute_allowed_tool_names` path. If type-driven tool defaults are desired later, that is a follow-up, not this plan.
