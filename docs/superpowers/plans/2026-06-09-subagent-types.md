# Configurable & Custom Subagent Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use `- [ ]` checkboxes.

**Goal:** Make subagent types entity-backed, admin-managed, self-contained agent definitions (model, temperature, tools, budget, prompt, execution mode, delivery) — mirroring AI profiles — with a curated catalog of 10 built-ins; collapse the special-cased `deep_research` into a type configured as background+report; `spawn_agent(type, prompt, model?)` becomes the only tool.

**Architecture:** A new `SubagentType` dataclass + `subagent_types` storage collection, seeded with editable-but-protected built-ins (exactly like `AIContextProfile`/`ai_profiles`). The `SubagentService` loads types from storage, exposes admin CRUD WS RPCs, and drives `spawn()` from the type (model/temp/tools via a transient profile + `chat()` overrides). `execution_mode`/`deliver_as` generalize today's `_run_research_background`; the `deep_research` tool and `/research` are removed. A `/security/subagents` admin page mirrors `AIProfiles.tsx`.

**Tech Stack:** Backend Python 3.12 + pytest; frontend React 19 + Vite + vitest.

**Reference spec:** `docs/superpowers/specs/2026-06-09-subagent-types-design.md` + the catalog `docs/superpowers/specs/2026-06-09-subagent-types-prompts.md`.

**Branch:** `feat/subagent-types` (already created; spec committed there).

---

## Precedent to mirror (AI profiles) — read these first

- `src/gilbert/interfaces/ai.py:303` — `AIContextProfile` dataclass.
- `src/gilbert/core/services/ai.py`: `_PROFILES_COLLECTION = "ai_profiles"` (78); `_BUILTIN_PROFILES` (1179); `_UNDELETABLE_PROFILES` (1209); `_load_profiles` (1803); `_refresh_profiles` (1875); `get_profile` (1907); `list_profiles` (1914); `set_profile` (1926); `delete_profile` (1952). `_resolve_backend_and_model` (2153). `_discover_tools` profile filter (3620–3701).
- `src/gilbert/core/services/access_control.py:~870` — `roles.profile.{list,save,delete}` WS handlers; `_ws_profile_list` builds `all_tool_names` via `get_all_by_capability("ai_tools")` (≈998).
- `frontend/src/components/roles/AIProfiles.tsx` — the management UI (list cards + form dialog with backend/model dropdowns, tool_mode, tool checkbox list). `frontend/src/components/roles/RolesPage.tsx:24` — `<Route path="profiles" …>`. `frontend/src/hooks/useWsApi.ts:205` — `listModels: () => rpc({type:"chat.models.list"})`.
- Current subagent code being replaced: `src/gilbert/core/subagents/types.py` (frozen `AgentType` + built-ins); `src/gilbert/core/services/subagent.py` — `__init__` (`_type_prompts`), config params per-type prompt (`_prompt_key`, ~69/164/177), `get_tools` (`spawn_agent` ~197, `deep_research` ~231), `execute_tool` (~300), `spawn()` (~609), `_run_research_background` (~415), the `_Run` registry, `get_ws_handlers` (`subagent.stop`, `subagent.list`).

---

## Task 1: `SubagentType` dataclass + built-in catalog seed

**Files:** Rewrite `src/gilbert/core/subagents/types.py`; Test `tests/unit/test_subagent_types.py`

- [ ] **Step 1: Write the failing test**

Replace `tests/unit/test_subagent_types.py` content with:

```python
from gilbert.core.subagents.types import (
    SubagentType,
    BUILTIN_SUBAGENT_TYPES,
    builtin_seed_list,
)


def test_subagent_type_has_self_contained_fields() -> None:
    t = SubagentType(id="x", name="X", description="d", system_prompt="p")
    # Defaults
    assert t.tool_mode == "all"
    assert t.execution_mode == "sync"
    assert t.deliver_as == "inline"
    assert t.max_rounds == 12
    assert t.enabled is True
    assert t.built_in is False


def test_catalog_ships_ten_builtins_with_expected_ids() -> None:
    ids = {t.id for t in builtin_seed_list()}
    assert ids == {
        "general-purpose", "deep-research", "quick-answer", "software-engineer",
        "code-reviewer", "qa-engineer", "product-manager", "market-analyst",
        "fact-checker", "summarizer",
    }
    assert all(t.built_in for t in builtin_seed_list())


def test_deep_research_and_market_analyst_are_background_report() -> None:
    by_id = {t.id: t for t in builtin_seed_list()}
    for tid in ("deep-research", "market-analyst"):
        assert by_id[tid].execution_mode == "background"
        assert by_id[tid].deliver_as == "report_file"
    # A sync/inline one for contrast
    assert by_id["software-engineer"].execution_mode == "sync"
    assert by_id["software-engineer"].temperature == 0.1


def test_builtin_prompts_are_substantial() -> None:
    for t in builtin_seed_list():
        assert len(t.system_prompt) > 120, t.id
```

- [ ] **Step 2: Run → fail**

Run: `uv run pytest tests/unit/test_subagent_types.py -q`
Expected: FAIL — `SubagentType`/`builtin_seed_list` don't exist.

- [ ] **Step 3: Implement `types.py`**

Rewrite `src/gilbert/core/subagents/types.py`:

```python
"""Built-in subagent type seed definitions.

A subagent *type* is a self-contained agent definition: model + generation
params, tool gating, round/time budget, a system prompt, and an execution mode
(sync vs background) + delivery (inline vs report file). Types are stored as
entities (``subagent_types``) and managed by admins; this module only provides
the dataclass and the editable built-in *seed* values (mirrors
``_BUILTIN_PROFILES``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubagentType:
    id: str
    name: str
    description: str
    system_prompt: str
    backend: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    tool_mode: str = "all"  # all | include | exclude
    tools: list[str] = field(default_factory=list)
    max_rounds: int = 12
    max_wall_clock_s: float | None = 300.0
    execution_mode: str = "sync"  # sync | background
    deliver_as: str = "inline"  # inline | report_file
    enabled: bool = True
    built_in: bool = False
    icon: str = ""


# Prompts live verbatim in the catalog doc
# (docs/superpowers/specs/2026-06-09-subagent-types-prompts.md). Keep these in
# sync with that doc.
_GENERAL_PURPOSE_PROMPT = (
    "You are a capable autonomous agent inside Gilbert. You handle focused, "
    "multi-step tasks that don't fit a specialist. You run in a fresh context "
    "and cannot ask questions — make reasonable assumptions and state them "
    "explicitly in your output.\n\n"
    "Work this way:\n"
    "1. Restate the goal in one line, then sketch a brief plan.\n"
    "2. Execute the plan. Use web_search and fetch_url to gather facts, "
    "write_workspace_file to save artifacts, and read workspace files you're "
    "given. Prefer primary sources; verify anything load-bearing with a second "
    "source.\n"
    "3. Stop when the task is genuinely done, not when it looks done.\n\n"
    "Be thorough but not wasteful. Never fabricate facts, file contents, or "
    "citations; if something is unknown, say so. Your FINAL message is the "
    "deliverable, in Markdown: the result first, then a short 'How I got there' "
    "(steps, sources, and every assumption you made)."
)
# ... (the engineer copies the remaining 9 prompts VERBATIM from the catalog doc
# `2026-06-09-subagent-types-prompts.md` into named constants below:
#   _DEEP_RESEARCH_PROMPT, _QUICK_ANSWER_PROMPT, _SOFTWARE_ENGINEER_PROMPT,
#   _CODE_REVIEWER_PROMPT, _QA_ENGINEER_PROMPT, _PRODUCT_MANAGER_PROMPT,
#   _MARKET_ANALYST_PROMPT, _FACT_CHECKER_PROMPT, _SUMMARIZER_PROMPT.
#   Use the exact prompt text from each catalog section. _DEEP_RESEARCH_PROMPT is
#   identical to the one currently in git history for this file.)


def builtin_seed_list() -> list[SubagentType]:
    """The shipped built-in types (all ``built_in=True``). Settings come from the
    catalog doc's per-agent 'Settings' lines."""
    return [
        SubagentType(
            id="general-purpose", name="General Purpose",
            description=(
                "Use this agent when a task needs several autonomous steps "
                "(research, gather, produce an artifact) and no specialist "
                "agent fits."
            ),
            system_prompt=_GENERAL_PURPOSE_PROMPT,
            temperature=0.4, tool_mode="all",
            max_rounds=30, max_wall_clock_s=600.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="deep-research", name="Research Analyst",
            description=(
                "Use when the user wants thorough, source-cited research "
                "synthesized across many sources, delivered as a written report."
            ),
            system_prompt=_DEEP_RESEARCH_PROMPT,
            temperature=0.4, tool_mode="include",
            tools=["web_search", "fetch_url", "write_workspace_file"],
            max_rounds=40, max_wall_clock_s=900.0,
            execution_mode="background", deliver_as="report_file", built_in=True,
        ),
        SubagentType(
            id="quick-answer", name="Quick Answer",
            description="Use when you need a single fact or short factual answer from the web, fast and cited.",
            system_prompt=_QUICK_ANSWER_PROMPT,
            temperature=0.1, tool_mode="include", tools=["web_search", "fetch_url"],
            max_rounds=6, max_wall_clock_s=90.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="software-engineer", name="Software Engineer",
            description=(
                "Use when you have a concrete code spec or well-defined change "
                "and need production-quality, convention-matching code written "
                "autonomously in one pass."
            ),
            system_prompt=_SOFTWARE_ENGINEER_PROMPT,
            temperature=0.1, tool_mode="include",
            tools=["write_workspace_file", "web_search", "fetch_url"],
            max_rounds=12, max_wall_clock_s=300.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="code-reviewer", name="Code Reviewer",
            description=(
                "Use when you need a rigorous, severity-classified review of a "
                "diff or changed files — real bugs, security, correctness — with "
                "concrete fixes."
            ),
            system_prompt=_CODE_REVIEWER_PROMPT,
            temperature=0.1, tool_mode="include", tools=["web_search", "fetch_url"],
            max_rounds=12, max_wall_clock_s=300.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="qa-engineer", name="QA Engineer",
            description=(
                "Use when you need a rigorous test plan or defect hunt for a "
                "feature, spec, or code change."
            ),
            system_prompt=_QA_ENGINEER_PROMPT,
            temperature=0.3, tool_mode="include",
            tools=["web_search", "fetch_url", "write_workspace_file"],
            max_rounds=12, max_wall_clock_s=600.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="product-manager", name="Product Manager",
            description=(
                "Use when you need a build-ready product spec/PRD from a problem "
                "or feature idea (goals/non-goals, metrics, RICE, user stories)."
            ),
            system_prompt=_PRODUCT_MANAGER_PROMPT,
            temperature=0.4, tool_mode="include",
            tools=["web_search", "fetch_url", "write_workspace_file"],
            max_rounds=12, max_wall_clock_s=300.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="market-analyst", name="Market Analyst",
            description=(
                "Use when the user wants a thorough, source-cited market or "
                "competitive analysis delivered as a written report."
            ),
            system_prompt=_MARKET_ANALYST_PROMPT,
            temperature=0.4, tool_mode="include",
            tools=["web_search", "fetch_url", "write_workspace_file"],
            max_rounds=40, max_wall_clock_s=900.0,
            execution_mode="background", deliver_as="report_file", built_in=True,
        ),
        SubagentType(
            id="fact-checker", name="Fact Checker",
            description=(
                "Use when you need to verify one or more factual claims against "
                "authoritative, corroborated sources with a sourced verdict."
            ),
            system_prompt=_FACT_CHECKER_PROMPT,
            temperature=0.1, tool_mode="include", tools=["web_search", "fetch_url"],
            max_rounds=12, max_wall_clock_s=240.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
        SubagentType(
            id="summarizer", name="Summarizer",
            description="Use when you have a block of text or a URL and want a faithful summary of its key points.",
            system_prompt=_SUMMARIZER_PROMPT,
            temperature=0.2, tool_mode="include", tools=["fetch_url"],
            max_rounds=4, max_wall_clock_s=120.0,
            execution_mode="sync", deliver_as="inline", built_in=True,
        ),
    ]


BUILTIN_SUBAGENT_TYPES: dict[str, SubagentType] = {
    t.id: t for t in builtin_seed_list()
}
```

Copy the 9 remaining prompt constants verbatim from the catalog doc.

- [ ] **Step 4: Run → pass**

Run: `uv run pytest tests/unit/test_subagent_types.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/subagents/types.py tests/unit/test_subagent_types.py
git commit -m "subagent: SubagentType dataclass + 10-agent built-in seed catalog

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Type store (storage, seeding, CRUD, protection) in SubagentService

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

Mirror `_load_profiles`/`_refresh_profiles`/`set_profile`/`delete_profile`. Use a serialize/deserialize for `SubagentType` (dataclass ↔ dict).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py` (uses an in-memory storage like `test_ai_service.py`'s `_inmemory_storage_service`; import it or build a minimal dict-backed `StorageService` the same way):

```python
@pytest.mark.asyncio
async def test_type_store_seeds_builtins_and_crud(tmp_path: Any) -> None:
    from gilbert.core.subagents.types import builtin_seed_list, SubagentType
    storage, _store = _inmemory_storage_service()  # reuse the helper
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage))
    # Seeded all built-ins.
    ids = {t.id for t in svc.list_types()}
    assert {"general-purpose", "deep-research", "software-engineer"} <= ids
    assert len(svc.list_types()) >= len(builtin_seed_list())
    # Create a custom type.
    await svc.save_type(SubagentType(id="my-agent", name="Mine", description="d", system_prompt="do it"))
    assert svc.get_type("my-agent") is not None
    # Built-ins can't be deleted; custom can.
    assert await svc.delete_type("deep-research") is False
    assert await svc.delete_type("my-agent") is True
    # Edits to a built-in persist across reload (not overwritten by seeding).
    edited = svc.get_type("deep-research")
    edited.max_rounds = 99
    await svc.save_type(edited)
    svc2 = SubagentService()
    await svc2.start(_resolver(entity_storage=storage))
    assert svc2.get_type("deep-research").max_rounds == 99
    # Reset restores shipped default.
    await svc2.reset_type("deep-research")
    assert svc2.get_type("deep-research").max_rounds == 40
```

(If `_resolver` in this test file doesn't yet support `entity_storage`, extend it to return the storage for `"entity_storage"` like `test_ai_service.py`'s `_resolver_for`.)

- [ ] **Step 2: Run → fail**

Run: `uv run pytest tests/unit/test_subagent_service.py -k type_store_seeds_builtins -q`
Expected: FAIL — no `list_types`/`save_type`/etc.

- [ ] **Step 3: Implement the store**

In `subagent.py`:
- Module constant `_TYPES_COLLECTION = "subagent_types"`.
- In `__init__`: `self._types: dict[str, SubagentType] = {}` and `self._storage = None`.
- In `start()`: resolve storage (`resolver.require_capability("entity_storage")` / `get_capability`), then `await self._load_types()`.
- Add serialize/deserialize + store methods:

```python
    @staticmethod
    def _type_to_dict(t: SubagentType) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(t)

    @staticmethod
    def _type_from_dict(d: dict[str, Any]) -> SubagentType:
        fields = {f.name for f in dataclasses.fields(SubagentType)}
        return SubagentType(**{k: v for k, v in d.items() if k in fields})

    async def _load_types(self) -> None:
        """Seed missing built-ins (preserving edits), then load all into memory."""
        if self._storage is None:
            self._types = {t.id: t for t in builtin_seed_list()}
            return
        for seed in builtin_seed_list():
            existing = await self._storage.get(_TYPES_COLLECTION, seed.id)
            if existing is None:
                await self._storage.put(_TYPES_COLLECTION, seed.id, self._type_to_dict(seed))
        await self._refresh_types()

    async def _refresh_types(self) -> None:
        from gilbert.interfaces.storage import Query
        rows = await self._storage.query(Query(collection=_TYPES_COLLECTION))
        self._types = {}
        for r in rows:
            tid = r.get("id") or r.get("_id")
            if tid:
                self._types[tid] = self._type_from_dict({**r, "id": tid})

    def list_types(self) -> list[SubagentType]:
        return sorted(self._types.values(), key=lambda t: t.name)

    def get_type(self, type_id: str) -> SubagentType | None:
        return self._types.get(type_id)

    async def save_type(self, t: SubagentType) -> None:
        if self._storage is not None:
            await self._storage.put(_TYPES_COLLECTION, t.id, self._type_to_dict(t))
        self._types[t.id] = t

    async def delete_type(self, type_id: str) -> bool:
        t = self._types.get(type_id)
        if t is None or t.built_in:
            return False
        if self._storage is not None:
            await self._storage.delete(_TYPES_COLLECTION, type_id)
        self._types.pop(type_id, None)
        return True

    async def reset_type(self, type_id: str) -> bool:
        seed = next((s for s in builtin_seed_list() if s.id == type_id), None)
        if seed is None:
            return False
        await self.save_type(seed)
        return True
```

Add `import dataclasses` + `from gilbert.core.subagents.types import SubagentType, builtin_seed_list` at the top. **Remove** the old per-type prompt ConfigParams (`_prompt_key`, the `config_params` prompt entries, the `on_config_changed` `_type_prompts` loop) and the `self._type_prompts` field — prompts now live on the type entity. Keep the service's other config (enabled toggle) intact.

- [ ] **Step 4: Run → pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (update any test referencing `_type_prompts`/`get_agent_type` — see Task 3 for the engine swap).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: entity-backed type store (seed builtins, CRUD, reset, protect)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `spawn()` drives from the type (model/temp/tools + model override)

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_spawn_uses_type_model_temperature_tools_and_override() -> None:
    storage, _ = _inmemory_storage_service()
    poster = _FakePoster(report="# Done\n\n" + "x" * 100)
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage, event_bus=_FakeEventBusProvider(_FakeBus())))
    svc._ai = poster  # type: ignore[assignment]
    # Configure a custom sync type with a model + temperature + include tools.
    await svc.save_type(SubagentType(
        id="t1", name="T1", description="d", system_prompt="go",
        backend="ollama", model="llama3.3", temperature=0.2,
        tool_mode="include", tools=["web_search"], max_rounds=7,
    ))
    await svc.spawn("t1", "task")
    call = poster.calls[0]
    assert call["max_tool_rounds"] == 7
    # Type model/temperature flow through (exact kwarg names per chat()).
    assert call.get("model") == "llama3.3" or call.get("backend_override") == "ollama"
    # A per-spawn override beats the type's model.
    await svc.spawn("t1", "task", model_override="qwen2.5")
    assert poster.calls[1].get("model") == "qwen2.5"
```

(Update `_FakePoster.chat` to also record `model`/`backend_override`/`temperature` kwargs.)

- [ ] **Step 2: Run → fail.** `uv run pytest tests/unit/test_subagent_service.py -k uses_type_model -q` → FAIL.

- [ ] **Step 3: Implement.** Replace `get_agent_type(agent_type)` usage in `spawn()` with `self.get_type(agent_type)`. Build a transient profile (or pass overrides) so the type's tool gating + model + temperature reach `chat()`. The cleanest: pass a transient `AIContextProfile` is not available here (layer rules) — instead pass `chat()` the explicit overrides it already accepts: `model=...`, `backend_override=...`, plus a tool filter. Since `chat()` resolves tools via `ai_profile`, register a runtime profile is heavy; simplest path that exists today: `chat()` accepts `ai_profile` (a name) — so for tool gating, pass the type's `tools` by **constructing an ephemeral profile via AIService** is out of scope. Instead, **extend `chat()` minimally** is also heavy.

  Decision for this task: drive model + temperature + rounds + prompt directly (they're already chat() params), and gate tools by passing the type's tool list through a new `chat()` parameter `tool_filter: tuple[str, list[str]] | None = None` (mode, names) that `_discover_tools` honors when no profile is set. Add that param to `chat()` + the `AIProvider` protocol (mirroring how `source`/`headless` were added), and apply it in `_discover_tools` after the profile filter. Then `spawn()`:

```python
        t = self.get_type(agent_type)
        if t is None:
            raise ValueError(f"Unknown agent type: {agent_type}")
        system_prompt = f"{self._preamble}\n\n{t.system_prompt}"
        ...
        result = await self._ai.chat(
            user_message=prompt,
            conversation_id=conversation_id,
            user_ctx=user_ctx,
            system_prompt=system_prompt,
            ai_call=f"subagent.{t.id}",
            model=model_override or t.model,
            backend_override=backend_override or t.backend,
            max_tool_rounds=t.max_rounds,
            headless=True,
            source="subagent",
            should_stop_callback=should_stop,
            tool_filter=(t.tool_mode, list(t.tools)),
            conversation_parent_id=conversation_parent_id,
            conversation_title=conversation_title,
        )
```

  Add `model_override: str = ""`, `backend_override: str = ""` params to `spawn()`. (Temperature: `chat()` doesn't take a per-call temperature today; pass it via the same `tool_filter`-style minimal extension OR defer temperature to Task 3b. If `chat()` has no temperature param, add `temperature: float | None = None` to `chat()` + protocol and thread it into the generation params layering — mirror the existing profile temperature path.)

  Apply `tool_filter` in `_discover_tools`: after the profile filter block, if `tool_filter` is set and no profile narrowed already, filter `tools_by_name` by mode/names the same way the profile `include`/`exclude` does.

- [ ] **Step 4: Run → pass.** `uv run pytest tests/unit/test_subagent_service.py tests/unit/test_ai_service.py -q` → PASS (update `_FakeAI`/`_FakePoster.chat` + the `AIProvider` protocol kwargs).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py src/gilbert/core/services/ai.py src/gilbert/interfaces/ai.py tests/unit/
git commit -m "subagent: spawn() drives model/temperature/tools/budget from the type + override

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: execution_mode/deliver_as; remove deep_research + /research; spawn_agent model param

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_spawn_agent_is_only_tool_with_model_param_and_dynamic_types() -> None:
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    import asyncio
    asyncio.get_event_loop().run_until_complete(svc.start(_resolver(entity_storage=storage)))
    tools = svc.get_tools()
    names = {t.name for t in tools}
    assert "spawn_agent" in names
    assert "deep_research" not in names  # collapsed away
    spawn = next(t for t in tools if t.name == "spawn_agent")
    pnames = {p.name for p in spawn.parameters}
    assert {"agent_type", "prompt", "model"} <= pnames
    # The agent_type enum lists enabled type ids; the description name-drops them.
    enum = next(p for p in spawn.parameters if p.name == "agent_type").enum
    assert "software-engineer" in enum and "deep-research" in enum


@pytest.mark.asyncio
async def test_background_type_detaches_and_delivers_report(tmp_path: Any) -> None:
    # market-analyst is background/report_file.
    poster = _FakePoster(report="# Market\n\n" + "y" * 120)
    ws = _FakeWorkspace(tmp_path)
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage, event_bus=_FakeEventBusProvider(_FakeBus())))
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = ws  # type: ignore[assignment]
    captured: list[Any] = []
    svc._run_in_background = lambda coro: captured.append(coro)  # type: ignore[assignment]
    out = await svc.execute_tool("spawn_agent", {
        "agent_type": "market-analyst", "prompt": "EV chargers",
        "_user_id": "u1",
    })
    assert "background" in out.lower() or "i'll" in out.lower()
    assert len(captured) == 1  # detached
    await captured[0]  # run it
    # Delivered a report attachment (deliver_as=report_file).
    _conv, _msg, atts = poster.delivered[0]
    assert atts and atts[0].media_type == "text/markdown"


@pytest.mark.asyncio
async def test_sync_type_returns_inline(tmp_path: Any) -> None:
    poster = _FakePoster(report="# Answer\n\n" + "z" * 120)
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage, event_bus=_FakeEventBusProvider(_FakeBus())))
    svc._ai = poster  # type: ignore[assignment]
    out = await svc.execute_tool("spawn_agent", {
        "agent_type": "software-engineer", "prompt": "write fizzbuzz", "_user_id": "u1",
    })
    assert "Answer" in out  # returned inline, not an ack
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement.**
  - `get_tools()`: remove the `deep_research` `ToolDefinition` entirely. Keep only `spawn_agent` (+ `check_research`). Add a `model` `ToolParameter` (string, optional) to `spawn_agent`. Build the enum from `[t.id for t in self.list_types() if t.enabled]` and the description from per-type `name (id): description` lines. Remove `slash_command="research"` anywhere; ensure no `/research`.
  - `execute_tool("spawn_agent")`: resolve the type; read `model = arguments.get("model","")`. If `t.execution_mode == "background"`: capture parent conv + caller, `self._run_in_background(self._run_agent_background(t, prompt, parent_conv, caller, model))`, return the ack. Else (`sync`): `return await self.spawn(t.id, prompt, user_ctx=caller, model_override=model)`.
  - Rename `_run_research_background` → `_run_agent_background(self, t: SubagentType, query, parent_conversation_id, user_ctx, model_override="")`. It now: registers the run with `agent_type=t.id`; ensures the child conversation (title `f"{t.name}: {query}"[:80]`); spawns with `model_override`; then **delivers per `t.deliver_as`**: if `report_file` → write report + attachment + notification (today's path); if `inline` → `append_assistant_message(parent, report)` with no file. Keep the stop/registry/events.
  - Remove the old `deep_research` branch in `execute_tool`.

- [ ] **Step 4: Run → pass.** Full `uv run pytest tests/unit/test_subagent_service.py -q`. Also grep: `grep -rn '"research"\|deep_research\|/research' src/gilbert/core/services/subagent.py` returns nothing meaningful. Update/remove slice-5/6 tests that referenced the `deep_research` tool or `_run_research_background` by name → use `spawn_agent` + `_run_agent_background`.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: type-driven execution mode/delivery; remove deep_research tool + /research

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Admin CRUD WS RPCs

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

Mirror `roles.profile.*`. Admin-gate via the caller's roles on `conn`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_type_crud_ws_handlers_admin_gated() -> None:
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage))
    h = svc.get_ws_handlers()
    assert {"subagent.types.list", "subagent.types.save", "subagent.types.delete", "subagent.types.reset"} <= set(h)

    class _Admin:
        user_id = "a"; roles = ("admin",)
    class _User:
        user_id = "u"; roles = ("user",)

    listed = await h["subagent.types.list"](_Admin(), {"id": "1"})
    assert any(t["id"] == "deep-research" for t in listed["types"])
    assert "all_tool_names" in listed
    # Non-admin save rejected.
    res = await h["subagent.types.save"](_User(), {"id": "2", "type": {"id": "x", "name": "X", "description": "d", "system_prompt": "p"}})
    assert res.get("code") == 403 or res.get("error")
    # Admin save accepted.
    ok = await h["subagent.types.save"](_Admin(), {"id": "3", "type": {"id": "x", "name": "X", "description": "d", "system_prompt": "p"}})
    assert ok.get("ok") or ok.get("status") == "ok"
    assert svc.get_type("x") is not None
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement.** Add to `get_ws_handlers()` the four entries. Implement handlers mirroring `access_control.py`'s `_ws_profile_*`: an admin check helper (`_is_admin(conn)` — `"admin" in getattr(conn, "roles", ())`), 403 dict if not admin (except `list` which can be admin-only too — match profiles). `_ws_types_list` returns `{"type": "subagent.types.list.result", "ref": id, "types": [asdict…], "all_tool_names": sorted(<from ai_tools providers>), }`. Build `all_tool_names` like `_ws_profile_list` (iterate `get_all_by_capability("ai_tools")` ToolProviders). `_ws_types_save` validates + `save_type(_type_from_dict)`. `_ws_types_delete` → `delete_type`. `_ws_types_reset` → `reset_type`. Resolve the service-manager/providers via `self._resolver`.

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: admin CRUD WS RPCs for types (list/save/delete/reset)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — Subagents admin settings page

**Files:** Create `frontend/src/components/roles/Subagents.tsx` (+ test); Modify `frontend/src/components/roles/RolesPage.tsx`, `frontend/src/hooks/useWsApi.ts`, the roles nav.

Mirror `AIProfiles.tsx` exactly (list cards + edit/reset/delete + a form dialog).

- [ ] **Step 1: API hook.** In `useWsApi.ts`, add:
```ts
    listSubagentTypes: () => rpc<{ types: SubagentTypeDTO[]; all_tool_names: string[] }>({ type: "subagent.types.list" }),
    saveSubagentType: (t: SubagentTypeDTO) => rpc<{ status: string }>({ type: "subagent.types.save", type: t }),
    deleteSubagentType: (id: string) => rpc<{ status: string }>({ type: "subagent.types.delete", type_id: id }),
    resetSubagentType: (id: string) => rpc<{ status: string }>({ type: "subagent.types.reset", type_id: id }),
```
Add a `SubagentTypeDTO` type in `frontend/src/types/` matching the dataclass fields.

- [ ] **Step 2: Component (failing test first).** Create `Subagents.test.tsx` asserting: it renders type cards from a mocked `listSubagentTypes`; built-ins show a Reset button + no Delete; saving calls `saveSubagentType`. Then create `Subagents.tsx` modeled on `AIProfiles.tsx`:
  - List of cards (name, description, mode/model badges; Edit; Reset for built-ins, Delete for custom).
  - Form dialog fields: name, description (textarea), backend+model dropdowns (copy the exact `modelsData` select from `AIProfiles.tsx` 215–265 via `listModels`), temperature (number), tool_mode select + tool checkbox list (copy from AIProfiles), max_rounds + max_wall_clock_s (numbers), execution_mode select (sync/background), deliver_as select (inline/report_file), enabled toggle, system_prompt (large textarea). Disable `id` on edit.

- [ ] **Step 3: Route + nav.** In `RolesPage.tsx` add `<Route path="subagents" element={<Subagents />} />`; add the nav/tab entry next to "AI Profiles" (mirror how profiles is added).

- [ ] **Step 4: Verify.** `cd frontend && npm run test -- Subagents && npm run typecheck && npm run build` → green.

- [ ] **Step 5: Commit**

```bash
cd /home/assistant/gilbert
git add frontend/src/components/roles/Subagents.tsx frontend/src/components/roles/Subagents.test.tsx frontend/src/components/roles/RolesPage.tsx frontend/src/hooks/useWsApi.ts frontend/src/types/
git commit -m "frontend: Subagents admin settings page (manage agent types)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Migration, docs, verification

**Files:** Create `src/gilbert/migrations/NNNN_seed_subagent_types.py`; docs.

- [ ] **Step 1: Migration.** Add an idempotent migration that, on first run, migrates any existing per-type prompt overrides from the old `subagent` config section onto the seeded `subagent_types` rows' `system_prompt` (best-effort; skip if the config keys are absent). Seeding itself is handled by `_load_types`, so the migration only needs to carry over old prompt customizations. Test: a config with `deep_research_system_prompt="CUSTOM"` results in the `deep-research` row's `system_prompt == "CUSTOM"`.

- [ ] **Step 2: Docs.** Update `README.md` / `src/gilbert/CONTEXT.md` (subagent vocabulary), remove `/research` references, add a short ADR `docs/adr/NNNN-subagent-types-are-entities.md` ("subagent types are entity-backed self-contained agent definitions; deep research is a type, not a service"). Run the `validate-architecture` skill audit and fix anything it flags (esp. capability wiring for `subagent.types.*`, AI-backend-visibility — model is admin-selected data, document the exception).

- [ ] **Step 3: Full verification.**
```bash
cd /home/assistant/gilbert
uv run ruff check src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py
uv run mypy src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py src/gilbert/interfaces/ai.py
uv run pytest tests/unit/ -q
cd frontend && npm run typecheck && npm run test && npm run build
```
Expected: green (pre-existing unrelated `ai.py` lint out of scope).

- [ ] **Step 4: Commit fixups.**

---

## Self-review notes

- **Spec coverage:** type entity (T1); store/seed/CRUD/protect + migration (T2, T7); spawn drives from type + model override (T3); execution_mode/deliver_as + remove deep_research/`/research` + spawn_agent model param + dynamic routing description (T4); admin CRUD RPCs (T5); Subagents settings page (T6); 10-agent catalog (T1, prompts copied from the catalog doc); docs/ADR (T7). AgentService untouched (out of scope) ✓.
- **Name consistency:** `SubagentType`, `subagent_types`, `_TYPES_COLLECTION`, `list_types/get_type/save_type/delete_type/reset_type`, `_run_agent_background`, `execution_mode`/`deliver_as`, `tool_filter=(mode, names)`, `model_override`/`backend_override`, `subagent.types.{list,save,delete,reset}` are used identically across tasks.
- **Risk flagged for the implementer:** Task 3's tool-gating mechanism (`tool_filter` param on `chat()`/`AIProvider` + `_discover_tools`) and temperature passthrough touch `ai.py`/the protocol — verify the real `chat()` signature and the generation-params layering before wiring; if a profile-construction path already exists that's cleaner, use it and report the deviation. Keep the two background built-ins behaving exactly as the current deep_research did (report file + attachment + notification + watchable child).
- **No placeholders:** the only "copy verbatim" instruction is the 9 prompt constants, whose exact text is in the committed catalog doc — that's content transcription, not an undefined reference.
