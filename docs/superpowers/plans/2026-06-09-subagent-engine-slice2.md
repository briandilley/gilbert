# Subagent Engine — Slice 2 (Spawn Tool + Headless Gating) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the slice-1 subagent engine to the chat AI as a `spawn_agent` tool, and add the "headless" tool-gating mechanism so a subagent can't call user-interaction tools or spawn further subagents (no nesting).

**Architecture:** Add a declarative `ToolDefinition.interactive` flag. Thread a `headless: bool` flag from `AIService.chat` into `AIService._discover_tools`, which drops `interactive=True` tools when headless. `SubagentService` becomes a `ToolProvider` exposing `spawn_agent(agent_type, prompt)` (marked `interactive=True` so subagents never receive it → no nesting); its handler inherits the caller's identity via `get_current_user()` and calls the existing `spawn()`. `spawn()` drives `chat(headless=True)`.

**Tech Stack:** Python 3.12, `uv`, pytest (`asyncio_mode=auto`). Builds on slice 1 (`SubagentService`, `AgentType` registry), already merged to `main`.

**Reference spec:** `docs/superpowers/specs/2026-06-08-subagent-engine-design.md` (§6.2, §6.4). Slice-1 plan: `docs/superpowers/plans/2026-06-08-subagent-engine-slice1.md`.

**Branch:** create `feat/subagent-slice2` off `main` before starting.

---

## File Structure

- **Modify** `src/gilbert/interfaces/tools.py` — add `ToolDefinition.interactive: bool = False`.
- **Modify** `src/gilbert/interfaces/ai.py` — add `headless: bool = False` to the `AIProvider.chat` protocol signature (keep in sync with the concrete service).
- **Modify** `src/gilbert/core/services/ai.py` — add `headless` to `_discover_tools` (filter) and to `chat` (thread to the main-loop discovery call site).
- **Modify** `src/gilbert/core/services/subagent.py` — implement `ToolProvider` (`tool_provider_name`, `get_tools`, `execute_tool`), advertise the `ai_tools` capability, and pass `headless=True` from `spawn()`.
- **Modify** `tests/unit/test_subagent_service.py` — tool-provider tests + headless-pass-through test (update the `_FakeAI` to accept/record `headless`).
- **Modify** `tests/unit/test_ai_service.py` — `_discover_tools(headless=...)` filtering test.
- **Create** `tests/unit/test_tooldefinition_interactive.py` — the new field's default/behavior.

Out of scope (later slices): `subagent.*` lifecycle events + UI card (slice 3); the `deep-research` type + Tongyi profile + `deep_research`/`/research` sugar + openrouter catalog entry (slice 4); a per-turn aggregate spawn/cost cap and parallel fan-out (follow-up — `spawn_agent` ships `parallel_safe=False` for now).

---

## Task 0: Branch

- [ ] **Step 1: Create the feature branch off main**

Run:
```bash
cd /home/assistant/gilbert
git checkout main
git checkout -b feat/subagent-slice2
git rev-parse --abbrev-ref HEAD
```
Expected: `feat/subagent-slice2`.

---

## Task 1: `ToolDefinition.interactive` flag

**Files:**
- Modify: `src/gilbert/interfaces/tools.py`
- Test: `tests/unit/test_tooldefinition_interactive.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tooldefinition_interactive.py`:

```python
"""The ToolDefinition.interactive flag (headless-subagent gating)."""

from __future__ import annotations

from gilbert.interfaces.tools import ToolDefinition


def test_interactive_defaults_false() -> None:
    # Existing tools are unaffected — interactive is opt-in.
    assert ToolDefinition(name="x", description="d").interactive is False


def test_interactive_can_be_set() -> None:
    t = ToolDefinition(name="spawn_agent", description="d", interactive=True)
    assert t.interactive is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tooldefinition_interactive.py -q`
Expected: FAIL — `TypeError: ToolDefinition.__init__() got an unexpected keyword argument 'interactive'`.

- [ ] **Step 3: Add the field**

In `src/gilbert/interfaces/tools.py`, in the `ToolDefinition` dataclass, add the field immediately after `ai_visible: bool = True` (and before `def to_json_schema`):

```python
    # Marks a tool that must NOT be exposed to a *headless* subagent run
    # (``AIService.chat(headless=True)``). Two cases set this: tools that
    # need the user (a future ``request_user_input``), and orchestration
    # tools whose use by an autonomous leaf would be unsafe — notably
    # ``spawn_agent`` itself, so subagents can't spawn further subagents
    # (no nesting). ``AIService._discover_tools`` drops ``interactive=True``
    # tools when called with ``headless=True``; interactive chat is
    # unaffected. Default ``False`` — opt-in, like ``parallel_safe``.
    interactive: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tooldefinition_interactive.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/tools.py tests/unit/test_tooldefinition_interactive.py
git commit -m "tools: add ToolDefinition.interactive flag for headless-subagent gating"
```

---

## Task 2: Headless filtering in `_discover_tools`

**Files:**
- Modify: `src/gilbert/core/services/ai.py`
- Test: `tests/unit/test_ai_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_ai_service.py` (it already imports `AIService`, `Service`, `ToolDefinition`, `ToolParameter` via `StubToolProviderService` at the top — reuse those imports; add any missing import at the top of the file):

```python
def _ai_with_tools(*tool_defs):
    """An AIService whose resolver exposes one ToolProvider with the given tools."""
    from gilbert.interfaces.tools import ToolProvider

    class _P(Service):
        def service_info(self):  # type: ignore[no-untyped-def]
            from gilbert.interfaces.service import ServiceInfo

            return ServiceInfo(name="p", capabilities=frozenset({"ai_tools"}))

        @property
        def tool_provider_name(self) -> str:
            return "p"

        def get_tools(self, user_ctx=None):  # type: ignore[no-untyped-def]
            return list(tool_defs)

        async def execute_tool(self, name, arguments):  # type: ignore[no-untyped-def]
            return "ok"

    provider = _P()
    assert isinstance(provider, ToolProvider)

    class _R:
        def get_all(self, cap):  # type: ignore[no-untyped-def]
            return [provider] if cap == "ai_tools" else []

        def get_capability(self, cap):  # type: ignore[no-untyped-def]
            return None

        def require_capability(self, cap):  # type: ignore[no-untyped-def]
            raise LookupError(cap)

    svc = AIService()
    svc._resolver = _R()  # type: ignore[assignment]
    return svc


def test_discover_tools_headless_drops_interactive() -> None:
    normal = ToolDefinition(name="web_search", description="d")
    interactive = ToolDefinition(name="ask_user", description="d", interactive=True)
    svc = _ai_with_tools(normal, interactive)

    headless = svc._discover_tools(headless=True)
    assert "web_search" in headless
    assert "ask_user" not in headless  # interactive tool excluded when headless

    full = svc._discover_tools(headless=False)
    assert "web_search" in full
    assert "ask_user" in full  # interactive chat keeps it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ai_service.py -k discover_tools_headless -q`
Expected: FAIL — `TypeError: _discover_tools() got an unexpected keyword argument 'headless'`.

- [ ] **Step 3: Implement the filter**

In `src/gilbert/core/services/ai.py`, change the `_discover_tools` signature (currently lines ~3577–3581) to add the `headless` parameter:

```python
    def _discover_tools(
        self,
        user_ctx: UserContext | None = None,
        profile: AIContextProfile | None = None,
        headless: bool = False,
    ) -> dict[str, tuple[ToolProvider, ToolDefinition]]:
```

Then, inside the `for tool_def in svc.get_tools(user_ctx):` loop, immediately after the existing `ai_visible` skip:

```python
                if not tool_def.ai_visible:
                    continue
```

add:

```python
                # Headless subagent runs can't use tools that need the user
                # or that would let an autonomous agent spawn more agents.
                if headless and tool_def.interactive:
                    continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_ai_service.py -k discover_tools_headless -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/ai.py tests/unit/test_ai_service.py
git commit -m "ai: _discover_tools drops interactive tools in headless mode"
```

---

## Task 3: Thread `headless` through `chat` + the `AIProvider` protocol

**Files:**
- Modify: `src/gilbert/core/services/ai.py` (chat signature + main-loop call site)
- Modify: `src/gilbert/interfaces/ai.py` (protocol signature)
- Test: `tests/unit/test_ai_service.py`

- [ ] **Step 1: Write the failing test (signature wiring)**

Append to `tests/unit/test_ai_service.py`:

```python
def test_chat_signature_accepts_headless() -> None:
    import inspect

    from gilbert.interfaces.ai import AIProvider

    assert "headless" in inspect.signature(AIService.chat).parameters
    assert "headless" in inspect.signature(AIProvider.chat).parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ai_service.py -k chat_signature_accepts_headless -q`
Expected: FAIL — assertion error (`headless` not in the parameters).

- [ ] **Step 3: Add `headless` to `AIService.chat` and thread it to the main-loop discovery**

In `src/gilbert/core/services/ai.py`, in the `chat` signature (lines ~2291–2307), add the parameter immediately before `should_stop_callback`:

```python
        mid_round_interrupt: Callable[[], bool] | None = None,
        headless: bool = False,
        should_stop_callback: Callable[[], bool] | None = None,
```

Then update the main agentic-loop tool-discovery call site. Find this unique block (the comment makes it unambiguous):

```python
        # Discover and filter tools based on profile
        tools_by_name = self._discover_tools(user_ctx=user_ctx, profile=profile)
```

Change the call to thread `headless`:

```python
        # Discover and filter tools based on profile
        tools_by_name = self._discover_tools(
            user_ctx=user_ctx, profile=profile, headless=headless
        )
```

(Leave the other `_discover_tools` call sites unchanged — they default to `headless=False`. The main-loop site is the one that builds the per-round tool list the model actually receives, so gating it fully enforces headlessness for the subagent's reasoning loop.)

- [ ] **Step 4: Add `headless` to the `AIProvider` protocol**

In `src/gilbert/interfaces/ai.py`, in the `AIProvider.chat` protocol signature (lines ~510–524), add the parameter before the closing `) -> ChatTurnResult:` (after `mid_round_interrupt: Any = None,`):

```python
        mid_round_interrupt: Any = None,
        headless: bool = False,
    ) -> ChatTurnResult:
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_ai_service.py -k chat_signature_accepts_headless -q`
Expected: PASS (1 passed).

- [ ] **Step 6: Verify no chat callers broke + types**

Run: `uv run pytest tests/unit/test_ai_service.py -q`
Expected: PASS (existing chat tests still green — the new param is optional/defaulted).

Run: `uv run mypy src/gilbert/core/services/ai.py src/gilbert/interfaces/ai.py 2>&1 | tail -5`
Expected: no NEW errors referencing `headless` (pre-existing unrelated mypy output, if any, is acceptable — confirm none mention `headless` or the lines you changed).

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/ai.py src/gilbert/interfaces/ai.py tests/unit/test_ai_service.py
git commit -m "ai: thread headless flag through chat() and the AIProvider protocol"
```

---

## Task 4: `SubagentService` exposes the `spawn_agent` tool

**Files:**
- Modify: `src/gilbert/core/services/subagent.py`
- Test: `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
from gilbert.interfaces.context import set_current_user  # add near the top imports
from gilbert.interfaces.tools import ToolProvider  # add near the top imports


def test_subagent_service_is_a_tool_provider() -> None:
    svc = SubagentService()
    assert isinstance(svc, ToolProvider)
    assert svc.tool_provider_name == "subagent"
    # Advertises ai_tools so AIService._discover_tools picks it up.
    assert "ai_tools" in svc.service_info().capabilities


def test_get_tools_exposes_spawn_agent_with_type_enum() -> None:
    tools = SubagentService().get_tools()
    spawn = next(t for t in tools if t.name == "spawn_agent")
    # Must be excluded from headless subagents (no nesting).
    assert spawn.interactive is True
    assert spawn.ai_visible is True
    agent_type_param = next(p for p in spawn.parameters if p.name == "agent_type")
    assert "general-purpose" in (agent_type_param.enum or [])
    assert any(p.name == "prompt" for p in spawn.parameters)


def test_get_tools_empty_when_disabled() -> None:
    svc = SubagentService()
    svc._enabled = False
    assert svc.get_tools() == []


@pytest.mark.asyncio
async def test_execute_spawn_agent_inherits_current_user() -> None:
    svc, fake = await _started("done")
    caller = UserContext(user_id="u9", email="u9@x.com", display_name="U9")
    token = set_current_user(caller)
    try:
        out = await svc.execute_tool(
            "spawn_agent",
            {"agent_type": "general-purpose", "prompt": "go", "_user_id": "u9"},
        )
    finally:
        from gilbert.interfaces.context import _current_user

        _current_user.reset(token)
    assert out == "done"
    # The spawned chat inherited the caller identity + ran headless.
    assert fake.calls[0]["user_ctx"] is caller
    assert fake.calls[0]["headless"] is True


@pytest.mark.asyncio
async def test_execute_unknown_tool_raises_keyerror() -> None:
    svc, _ = await _started()
    with pytest.raises(KeyError):
        await svc.execute_tool("nope", {})
```

Note: this test relies on `_FakeAI.chat` recording a `headless` key and on `set_current_user` returning a reset token — both handled in Task 5 Step 1 (update `_FakeAI`) and by `gilbert.interfaces.context`. If `set_current_user` does not return a token, use the documented direct-ContextVar reset shown in `gilbert/interfaces/context.py` instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "tool_provider or spawn_agent or execute_" -q`
Expected: FAIL — `SubagentService` has no `tool_provider_name`/`get_tools`/`execute_tool`, and `ai_tools` not in capabilities.

- [ ] **Step 3: Implement ToolProvider on SubagentService**

In `src/gilbert/core/services/subagent.py`:

(a) Update imports at the top to add:

```python
from gilbert.interfaces.context import get_current_user
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
```

(remove the now-redundant standalone `from gilbert.interfaces.tools import ToolParameterType` if present — consolidate into the single import above).

(b) Add `"ai_tools"` to the advertised capabilities in `service_info()`:

```python
            capabilities=frozenset({"subagent", "ai_tools"}),
```

(c) Add the ToolProvider surface (place after `on_config_changed`, before `spawn`):

```python
    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "subagent"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        types = list_agent_types()
        type_lines = "\n".join(f"- {t.id}: {t.description}" for t in types)
        return [
            ToolDefinition(
                name="spawn_agent",
                description=(
                    "Launch a subagent to work on a focused task autonomously in "
                    "a fresh context, then return its final report. The subagent "
                    "cannot ask you or the user questions — give it a complete, "
                    "self-contained task. Available agent types:\n" + type_lines
                ),
                parameters=[
                    ToolParameter(
                        name="agent_type",
                        type=ToolParameterType.STRING,
                        description="Which agent type to launch.",
                        enum=[t.id for t in types],
                    ),
                    ToolParameter(
                        name="prompt",
                        type=ToolParameterType.STRING,
                        description=(
                            "The complete task for the subagent. Include all "
                            "context it needs; it has a fresh context and cannot "
                            "ask follow-up questions."
                        ),
                    ),
                ],
                # interactive=True keeps spawn_agent out of headless subagent
                # runs, so subagents can't spawn more subagents (no nesting).
                interactive=True,
                # Conservative for v1: no parallel fan-out of (expensive)
                # sub-chats until a per-turn spawn/cost cap exists.
                parallel_safe=False,
            )
        ]

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

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "tool_provider or spawn_agent or execute_" -q`
Expected: the tool-provider/get_tools/keyerror tests PASS; `test_execute_spawn_agent_inherits_current_user` will still FAIL until Task 5 updates `_FakeAI` to accept/record `headless` (its `chat` currently has no `headless` param, so `spawn()` passing `headless=True` in Task 5 is what makes the `headless is True` assertion meaningful). That's expected — proceed to Task 5.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: expose spawn_agent tool (ToolProvider), inherit caller identity"
```

---

## Task 5: `spawn()` runs headless

**Files:**
- Modify: `src/gilbert/core/services/subagent.py` (spawn → headless=True)
- Test: `tests/unit/test_subagent_service.py` (update `_FakeAI`)

- [ ] **Step 1: Update `_FakeAI` to accept + record `headless`**

In `tests/unit/test_subagent_service.py`, update the `_FakeAI.chat` method signature to add `headless: bool = False` (before the final closing paren) and record it in the captured call dict:

```python
        max_tool_rounds: int | None = None,
        between_rounds_callback: Any = None,
        mid_round_interrupt: Any = None,
        headless: bool = False,
    ) -> ChatTurnResult:
        self.calls.append(
            {
                "user_message": user_message,
                "conversation_id": conversation_id,
                "user_ctx": user_ctx,
                "system_prompt": system_prompt,
                "ai_call": ai_call,
                "ai_profile": ai_profile,
                "max_tool_rounds": max_tool_rounds,
                "headless": headless,
            }
        )
```

- [ ] **Step 2: Add the failing assertion**

Append to `tests/unit/test_subagent_service.py`:

```python
@pytest.mark.asyncio
async def test_spawn_runs_headless() -> None:
    svc, fake = await _started()
    await svc.spawn("general-purpose", "task")
    assert fake.calls[0]["headless"] is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k spawn_runs_headless -q`
Expected: FAIL — `assert None is True` / KeyError-style: `spawn()` does not yet pass `headless`, so the fake records the default `False`.

- [ ] **Step 4: Make `spawn()` pass `headless=True`**

In `src/gilbert/core/services/subagent.py`, in `spawn()`, add `headless=True` to the `self._ai.chat(...)` call:

```python
        result = await self._ai.chat(
            user_message=prompt,
            conversation_id=None,
            user_ctx=user_ctx,
            system_prompt=system_prompt,
            ai_call=f"subagent.{agent.id}",
            ai_profile=agent.profile_name,
            max_tool_rounds=agent.max_rounds,
            headless=True,
        )
```

- [ ] **Step 5: Run tests to verify all subagent tests pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (all, including `test_execute_spawn_agent_inherits_current_user` from Task 4 and `test_spawn_runs_headless`).

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: spawn() runs the sub-chat headless (no interactive tools, no nesting)"
```

---

## Task 6: Lint, type-check, full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Ensure dev tooling**

Run: `uv sync --extra dev >/dev/null 2>&1; echo done`

- [ ] **Step 2: Lint + format the changed files**

Run:
```bash
uv run ruff check src/gilbert/interfaces/tools.py src/gilbert/interfaces/ai.py src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py tests/unit/test_tooldefinition_interactive.py tests/unit/test_ai_service.py
uv run ruff format --check src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py tests/unit/test_tooldefinition_interactive.py
```
Expected: "All checks passed!" and no reformat needed. If `ai.py`/`test_ai_service.py` report PRE-EXISTING issues unrelated to your edits, leave them; only fix files/lines you changed. If format wants your new files, run `uv run ruff format <those files>` and amend.

- [ ] **Step 3: Type-check the changed core files**

Run: `uv run mypy src/gilbert/core/services/subagent.py src/gilbert/interfaces/tools.py`
Expected: `Success: no issues found`. (For `ai.py`/`interfaces/ai.py`, confirm your `headless` additions introduce no new errors; pre-existing unrelated errors are out of scope.)

- [ ] **Step 4: Full unit suite (regression check)**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (all green; the slice adds tests, breaks none).

- [ ] **Step 5: Commit any fixups**

```bash
git add -A
git commit -m "subagent slice2: lint/format fixups" || echo "nothing to commit"
```

---

## Self-review notes (author check)

- **Spec coverage (slice 2):** `spawn_agent` tool exposed to chat with a type enum + descriptions (Task 4) ✓; headless gating via `ToolDefinition.interactive` (Task 1) enforced in `_discover_tools` (Task 2) and threaded through `chat`/`AIProvider` (Task 3) ✓; no-nesting (spawn_agent marked `interactive=True`, excluded from headless runs) ✓; caller identity inherited for RBAC via `get_current_user()` (Task 4) ✓; `spawn()` runs headless (Task 5) ✓.
- **Deferred (called out, not gaps):** `subagent.*` events + UI (slice 3); `deep-research` type + Tongyi profile + `deep_research`/`/research` + openrouter (slice 4); per-turn spawn/cost cap + parallel fan-out (follow-up; `spawn_agent` is `parallel_safe=False`).
- **Type consistency:** the `headless` param is added identically (name + default `False`) to `_discover_tools`, `AIService.chat`, and the `AIProvider.chat` protocol, and is passed at exactly one call site (the main agentic loop). `_FakeAI.chat` mirrors the protocol so the subagent tests exercise the real contract. `ToolDefinition.interactive` is referenced consistently in the filter (Task 2) and on `spawn_agent` (Task 4).
- **Known small gap (acknowledged):** the one-line `headless=headless` wiring at the chat main-loop call site is verified by the signature test (Task 3) + `_discover_tools` filtering test (Task 2) + code review, not by a full end-to-end `chat()` behavior test (which needs the heavy backend/storage harness). End-to-end headless behavior is exercised when slice 4 integration-tests the deep-research path.
- **No placeholders:** every code step has complete code; every run step has an exact command + expected result.
