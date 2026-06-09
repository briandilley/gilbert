# Subagent Engine — Slice 4 (deep-research type) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `deep-research` agent type — a long-horizon web-research subagent that returns a cited report — plus a `deep_research` / `/research` sugar tool. The capstone of "add support for DeepResearch."

**Architecture:** Core-only, **model-agnostic**. Seed a model-agnostic "Deep Research" AI profile (default model + web tools only). Add a `deep-research` built-in agent type pointing at that profile with a research preamble. Add a thin `deep_research` tool on `SubagentService` that presets `spawn("deep-research", query)` and errors clearly when no web-search backend is enabled. **No OpenRouter, no submodule change, no hardcoded model** — Tongyi-DeepResearch-30B is recommended only as a text hint in the profile description; the user configures it themselves (e.g. via Ollama).

**Tech Stack:** Python 3.12, `uv`, pytest. Builds on slices 1–3 (on `main`): the `SubagentService` (engine + `spawn_agent` tool + lifecycle events), the `AgentType` registry, the built-in AI-profile seed.

**Reference spec:** `docs/superpowers/specs/2026-06-08-subagent-engine-design.md` §6.6.

**Branch:** create `feat/subagent-slice4` off `main` before starting.

---

## File Structure

- **Modify** `src/gilbert/core/services/ai.py` — add the `deep-research` entry to `_BUILTIN_PROFILES` + `_UNDELETABLE_PROFILES`.
- **Modify** `tests/unit/test_ai_service.py` — assert the seeded profile.
- **Modify** `src/gilbert/core/subagents/types.py` — add the `deep-research` `AgentType`.
- **Modify** `tests/unit/test_subagent_types.py` — assert the type.
- **Modify** `src/gilbert/core/services/subagent.py` — add the `deep_research` tool to `get_tools`, its `execute_tool` dispatch, and a web-search availability check.
- **Modify** `tests/unit/test_subagent_service.py` — tool + dispatch + degradation tests.

Out of scope: anything with OpenRouter/Tongyi wiring (intentionally omitted); scholar/sandbox tools; per-turn spawn cap; the Phase-2 autonomous-agents work.

---

## Task 0: Branch

- [ ] **Step 1**

```bash
cd /home/assistant/gilbert
git checkout main
git checkout -b feat/subagent-slice4
git rev-parse --abbrev-ref HEAD
```
Expected: `feat/subagent-slice4`.

---

## Task 1: Seed the model-agnostic "Deep Research" profile

**Files:**
- Modify: `src/gilbert/core/services/ai.py`
- Test: `tests/unit/test_ai_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_ai_service.py`:

```python
def test_deep_research_builtin_profile_is_model_agnostic() -> None:
    from gilbert.core.services.ai import _BUILTIN_PROFILES, _UNDELETABLE_PROFILES

    p = next((x for x in _BUILTIN_PROFILES if x.name == "deep-research"), None)
    assert p is not None
    # Web tools only.
    assert p.tool_mode == "include"
    assert set(p.tools) == {"web_search", "fetch_url"}
    # Model-agnostic: no hardcoded backend/model (uses the default model).
    assert p.backend == ""
    assert p.model == ""
    # Recommends a research-tuned model via a text hint, doesn't wire it.
    assert "tongyi" in p.description.lower()
    assert "deep-research" in _UNDELETABLE_PROFILES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ai_service.py -k deep_research_builtin_profile -q`
Expected: FAIL — no `deep-research` profile in `_BUILTIN_PROFILES`.

- [ ] **Step 3: Add the profile**

In `src/gilbert/core/services/ai.py`, add an entry to `_BUILTIN_PROFILES` (after the `advanced` entry, before the closing `]`):

```python
    AIContextProfile(
        name="deep-research",
        description=(
            "Deep research agent — iteratively searches the web, reads pages, "
            "cross-checks sources, and returns a cited report. Runs on your "
            "default model. Tip: a research-tuned model such as "
            "Tongyi-DeepResearch-30B-A3B works especially well here — configure "
            "one yourself (e.g. via Ollama) and point this profile at it."
        ),
        tool_mode="include",
        tools=["web_search", "fetch_url"],
    ),
```

And add `"deep-research"` to `_UNDELETABLE_PROFILES`:

```python
_UNDELETABLE_PROFILES = frozenset({"light", "standard", "advanced", "deep-research"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_ai_service.py -k deep_research_builtin_profile -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/ai.py tests/unit/test_ai_service.py
git commit -m "ai: seed model-agnostic 'deep-research' profile (web tools, default model)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: The `deep-research` agent type

**Files:**
- Modify: `src/gilbert/core/subagents/types.py`
- Test: `tests/unit/test_subagent_types.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_types.py`:

```python
def test_deep_research_type_registered() -> None:
    t = get_agent_type("deep-research")
    assert t is not None
    assert t.profile_name == "deep-research"
    # A longer budget than general-purpose — research is long-horizon.
    assert t.max_rounds >= 16
    # The prompt asks for a cited report.
    assert "report" in t.system_prompt.lower()
    assert "cit" in t.system_prompt.lower()  # "cite"/"citation"


def test_list_agent_types_includes_both_builtins() -> None:
    ids = {t.id for t in list_agent_types()}
    assert {"general-purpose", "deep-research"} <= ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_types.py -k "deep_research_type or both_builtins" -q`
Expected: FAIL — `get_agent_type("deep-research")` returns `None`.

- [ ] **Step 3: Add the type**

In `src/gilbert/core/subagents/types.py`, add the prompt + type, and register it. After the `_GENERAL_PURPOSE` definition, add:

```python
_DEEP_RESEARCH_PROMPT = (
    "You are a deep-research subagent. Investigate the question thoroughly and "
    "autonomously: plan what you need to find, search the web, read the most "
    "relevant pages in full, and cross-check claims across multiple independent "
    "sources. Iterate — search again to fill gaps — until you can answer with "
    "confidence. Then write a clear, well-structured answer in Markdown that "
    "directly addresses the question, with inline citations (page title + URL) "
    "for every non-obvious claim and a 'Sources' list at the end. Prefer primary "
    "sources; surface uncertainty and disagreements between sources rather than "
    "smoothing them over."
)

_DEEP_RESEARCH = AgentType(
    id="deep-research",
    description=(
        "Deep web research: a long-horizon agent that searches, reads pages, "
        "cross-checks sources, and returns a cited Markdown report. Use for "
        "questions needing current information or synthesis across many sources."
    ),
    system_prompt=_DEEP_RESEARCH_PROMPT,
    profile_name="deep-research",
    max_rounds=24,
    max_wall_clock_s=900.0,
)
```

Then update the registry dict to include it:

```python
BUILTIN_AGENT_TYPES: dict[str, AgentType] = {
    t.id: t for t in (_GENERAL_PURPOSE, _DEEP_RESEARCH)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_types.py -q`
Expected: PASS (all — the new tests plus the slice-1 ones).

- [ ] **Step 5: Run the subagent service tests (config_params now covers the new type)**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS — `SubagentService.config_params()` iterates `list_agent_types()` and now also emits a `deep_research_system_prompt` AI-prompt param; no existing assertion breaks. (If any test asserted an exact param count, update it — none should.)

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/subagents/types.py tests/unit/test_subagent_types.py
git commit -m "subagent: add deep-research agent type (cited-report research prompt)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: The `deep_research` / `/research` sugar tool

**Files:**
- Modify: `src/gilbert/core/services/subagent.py`
- Test: `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
def test_get_tools_includes_deep_research_sugar() -> None:
    tools = SubagentService().get_tools()
    dr = next((t for t in tools if t.name == "deep_research"), None)
    assert dr is not None
    assert dr.slash_command == "research"
    assert dr.required_role == "user"
    # Sugar is also an orchestration tool: keep it out of headless subagents.
    assert dr.interactive is True
    assert any(p.name == "query" for p in dr.parameters)


@pytest.mark.asyncio
async def test_deep_research_tool_spawns_deep_research_type() -> None:
    fake = _FakeAI("the report")
    svc = SubagentService()
    # websearch capability present -> deep research is allowed.
    await svc.start(_resolver(ai_chat=fake, websearch=object()))
    out = await svc.execute_tool("deep_research", {"query": "what is X?"})
    assert out == "the report"
    assert fake.calls[0]["ai_profile"] == "deep-research"
    assert fake.calls[0]["ai_call"] == "subagent.deep-research"


@pytest.mark.asyncio
async def test_deep_research_tool_errors_without_web_search() -> None:
    fake = _FakeAI()
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake))  # no websearch capability
    out = await svc.execute_tool("deep_research", {"query": "x"})
    assert "web" in out.lower() and "search" in out.lower()
    assert fake.calls == []  # never spawned the subagent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "deep_research" -q`
Expected: FAIL — no `deep_research` tool; `execute_tool("deep_research", ...)` raises `KeyError`.

- [ ] **Step 3: Implement the tool**

In `src/gilbert/core/services/subagent.py`:

(a) In `get_tools()`, add a second `ToolDefinition` to the returned list (after the `spawn_agent` one, still inside the `return [ ... ]`):

```python
            ToolDefinition(
                name="deep_research",
                description=(
                    "Run a deep web-research task: an autonomous agent searches "
                    "the web, reads pages, cross-checks sources, and returns a "
                    "cited Markdown report. Use for questions that need current "
                    "information or synthesis across multiple sources. (Sugar "
                    "over spawn_agent with the 'deep-research' type.)"
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description=(
                            "The research question or task, stated completely "
                            "and self-contained — the agent cannot ask follow-ups."
                        ),
                    ),
                ],
                slash_command="research",
                slash_help="Deep web research: /research <question>",
                required_role="user",
                # Orchestration tool: keep it out of headless subagent runs
                # (a subagent calling deep_research would nest).
                interactive=True,
                parallel_safe=False,
            ),
```

(b) Add a web-search availability helper (place near `_event_routing`):

```python
    def _web_search_available(self) -> bool:
        """Whether a web-search backend is enabled (deep research needs one)."""
        if self._resolver is None:
            return False
        return self._resolver.get_capability("websearch") is not None
```

(c) Extend `execute_tool` to handle `deep_research`. Replace the current body:

```python
    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "spawn_agent":
            raise KeyError(f"Unknown tool: {name}")
        agent_type = str(arguments.get("agent_type") or "")
        prompt = str(arguments.get("prompt") or "")
        if not agent_type or not prompt:
            raise ValueError("spawn_agent requires 'agent_type' and 'prompt'")
        # Inherit the caller's full identity for the subagent's RBAC.
        return await self.spawn(agent_type, prompt, user_ctx=get_current_user())
```

with:

```python
    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "spawn_agent":
            agent_type = str(arguments.get("agent_type") or "")
            prompt = str(arguments.get("prompt") or "")
            if not agent_type or not prompt:
                raise ValueError("spawn_agent requires 'agent_type' and 'prompt'")
            # Inherit the caller's full identity for the subagent's RBAC.
            return await self.spawn(agent_type, prompt, user_ctx=get_current_user())
        if name == "deep_research":
            query = str(arguments.get("query") or "")
            if not query:
                raise ValueError("deep_research requires 'query'")
            if not self._web_search_available():
                return (
                    "Deep research needs a web-search backend, but none is "
                    "enabled. Enable a web-search provider (for example the "
                    "Tavily plugin) under Settings → Intelligence, then try again."
                )
            return await self.spawn("deep-research", query, user_ctx=get_current_user())
        raise KeyError(f"Unknown tool: {name}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (all, including the three new `deep_research` tests).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: add deep_research / /research sugar tool (web-search gated)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Verification

**Files:** none (verification only)

- [ ] **Step 1: Lint + type-check the changed files**

Run:
```bash
cd /home/assistant/gilbert
uv run ruff check src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py tests/unit/test_subagent_service.py tests/unit/test_subagent_types.py
uv run ruff format --check src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py
uv run mypy src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py
```
Expected: ruff clean on these files; format clean; mypy `Success`. (Pre-existing unrelated issues in `ai.py` are out of scope — only fix files/lines you changed.)

- [ ] **Step 2: Full unit suite**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (all green).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A
git commit -m "subagent slice4: lint/format fixups" || echo "nothing to commit"
```

---

## Self-review notes (author check)

- **Spec coverage (§6.6, per the 2026-06-09 decision):** model-agnostic "Deep Research" profile seeded (Task 1, `backend=""`/`model=""`, web tools only, Tongyi-via-Ollama hint in the description); `deep-research` agent type with a cited-report research prompt + larger budget (Task 2); `deep_research`/`/research` sugar tool that presets `spawn("deep-research")` and errors clearly with no web-search backend (Task 3). No OpenRouter/submodule/hardcoded model. ✓
- **Reuse:** the type's web-tools-only restriction comes entirely from the seeded profile's `tool_mode="include"` (existing `_discover_tools` behavior); the sugar tool reuses the existing `spawn()` (events, headless, RBAC from slices 1–3). `interactive=True` on `deep_research` reuses slice-2 headless gating (no nesting).
- **Type/name consistency:** profile name, type `profile_name`, and `ai_call`/`ai_profile` are all the literal `"deep-research"`; the per-type config prompt key is `deep_research_system_prompt` (from slice-1's `_prompt_key`). Web tool names `web_search`/`fetch_url` match the WebSearchService tools and the profile's include-list. The web-search guard checks the `"websearch"` capability (WebSearchService advertises it).
- **Degradation:** no web-search backend → `deep_research` returns a clear, actionable message and never spawns (tested). A subagent can't call `deep_research`/`spawn_agent` (both `interactive=True`, excluded from headless runs).
- **No placeholders:** every code step has complete code; every run step has an exact command + expected result.
