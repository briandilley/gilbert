# Subagent Engine — Slice 1 (Engine Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational subagent engine — a core, default-on `SubagentService` that runs an agent *type* in a fresh, headless, bounded context and returns its final message — without the user-facing tool, UI, or deep-research type (those are slices 2–4).

**Architecture:** A new core service `SubagentService` composes the existing AI capability: it resolves the `ai_chat` capability (`AIProvider`) and drives `AIProvider.chat(...)` on a fresh ephemeral conversation with a per-type system prompt (shared preamble + type prompt), the type's AI profile (for model + tool gating), and a per-type round budget. Agent *types* are code-registered immutable definitions; slice 1 ships `general-purpose` (using the existing `standard` profile). No backend/model names appear in the engine — the profile owns those.

**Tech Stack:** Python 3.12, `uv`, pytest (`asyncio_mode=auto`), Gilbert's `Service`/`Configurable`/`AIProvider` interfaces.

**Reference spec:** `docs/superpowers/specs/2026-06-08-subagent-engine-design.md`

---

## File Structure

- **Create** `src/gilbert/core/subagents/__init__.py` — empty package marker.
- **Create** `src/gilbert/core/subagents/types.py` — the `AgentType` dataclass + the built-in type registry (`get_agent_type`, `list_agent_types`) + the `general-purpose` built-in. One responsibility: define and look up agent types.
- **Create** `src/gilbert/core/services/subagent.py` — `SubagentService` (`Service` + `Configurable`): lifecycle, config (enabled + configurable prompts), and the `spawn(...)` engine method. One responsibility: orchestrate a single ephemeral subagent run.
- **Create** `tests/unit/test_subagent_types.py` — tests for the type registry.
- **Create** `tests/unit/test_subagent_service.py` — tests for the service (config, lifecycle, `spawn`).
- **Modify** `src/gilbert/core/app.py` — register `SubagentService()` (composition root).
- **Modify** `src/gilbert/CONTEXT.md` — add the "subagent" glossary entry.

Out of scope for this slice (later slices): the `spawn_agent`/`deep_research` tools + `ToolDefinition.interactive` flag (slice 2), `subagent.*` lifecycle events + UI card (slice 3), the `deep-research` type + Tongyi profile + openrouter catalog entry (slice 4).

---

## Task 1: Agent type registry

**Files:**
- Create: `src/gilbert/core/subagents/__init__.py`
- Create: `src/gilbert/core/subagents/types.py`
- Test: `tests/unit/test_subagent_types.py`

- [ ] **Step 1: Create the empty package marker**

Create `src/gilbert/core/subagents/__init__.py` with no content (empty file).

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_subagent_types.py`:

```python
"""Tests for the built-in subagent type registry."""

from __future__ import annotations

from gilbert.core.subagents.types import (
    AgentType,
    get_agent_type,
    list_agent_types,
)


def test_general_purpose_type_is_registered() -> None:
    t = get_agent_type("general-purpose")
    assert t is not None
    assert isinstance(t, AgentType)
    assert t.id == "general-purpose"
    # Description is the routing hint the parent LLM will see — must be non-empty.
    assert t.description.strip()
    assert t.system_prompt.strip()
    # References an AI profile for model + tools; never names a backend itself.
    assert t.profile_name == "standard"
    assert t.max_rounds > 0


def test_get_unknown_type_returns_none() -> None:
    assert get_agent_type("does-not-exist") is None


def test_list_agent_types_includes_general_purpose() -> None:
    ids = {t.id for t in list_agent_types()}
    assert "general-purpose" in ids
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_types.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gilbert.core.subagents.types'`.

- [ ] **Step 4: Write the implementation**

Create `src/gilbert/core/subagents/types.py`:

```python
"""Built-in subagent types.

A subagent *type* is an immutable definition the engine runs: a description
(the routing hint the spawning model sees), a type-specific system prompt
(prepended with a shared headless preamble at run time), and a reference to an
AI profile that supplies the model + tool gating. The type never names a
backend/model itself — that lives in the profile (per the AI-backend-visibility
rule). v1 ships built-ins only; user-defined types are a later slice.
"""

from __future__ import annotations

from dataclasses import dataclass

_GENERAL_PURPOSE_PROMPT = (
    "You are a general-purpose subagent. Complete the assigned task thoroughly "
    "and autonomously using the tools available to you. Gather what you need, "
    "reason it through, and report your findings and conclusions as your final "
    "message."
)


@dataclass(frozen=True)
class AgentType:
    """An immutable subagent type definition."""

    id: str
    description: str
    system_prompt: str
    profile_name: str = "standard"
    max_rounds: int = 12
    max_wall_clock_s: float | None = 300.0


_GENERAL_PURPOSE = AgentType(
    id="general-purpose",
    description=(
        "General-purpose agent for researching complex questions and executing "
        "multi-step tasks autonomously in a fresh context. Use when a task "
        "benefits from focused, independent work and you don't need a more "
        "specialized agent."
    ),
    system_prompt=_GENERAL_PURPOSE_PROMPT,
    profile_name="standard",
    max_rounds=12,
)

BUILTIN_AGENT_TYPES: dict[str, AgentType] = {t.id: t for t in (_GENERAL_PURPOSE,)}


def get_agent_type(type_id: str) -> AgentType | None:
    """Return the built-in agent type with this id, or ``None``."""
    return BUILTIN_AGENT_TYPES.get(type_id)


def list_agent_types() -> list[AgentType]:
    """Return all built-in agent types."""
    return list(BUILTIN_AGENT_TYPES.values())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_types.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/subagents/__init__.py src/gilbert/core/subagents/types.py tests/unit/test_subagent_types.py
git commit -m "subagent: add built-in agent type registry"
```

---

## Task 2: SubagentService — lifecycle + config

**Files:**
- Create: `src/gilbert/core/services/subagent.py`
- Test: `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_subagent_service.py`:

```python
"""Tests for the core SubagentService (engine)."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.services.subagent import SubagentService
from gilbert.interfaces.ai import AIProvider, ChatTurnResult
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.tools import ToolParameterType


class _FakeAI:
    """Minimal AIProvider stand-in: records the chat() call, returns canned text."""

    def __init__(self, text: str = "subagent result") -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = text

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        user_ctx: UserContext | None = None,
        system_prompt: str | None = None,
        ai_call: str | None = None,
        attachments: Any = None,
        model: str = "",
        backend_override: str = "",
        ai_profile: str = "",
        max_tool_rounds: int | None = None,
        between_rounds_callback: Any = None,
        mid_round_interrupt: Any = None,
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
            }
        )
        return ChatTurnResult(
            response_text=self._text,
            conversation_id="conv-1",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )


def _resolver(**caps: Any) -> Any:
    class _R:
        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_all(self, name: str) -> list[Any]:
            return []

    return _R()


def test_fake_ai_satisfies_aiprovider() -> None:
    # runtime_checkable: presence of chat() is enough; guards the rest of the suite.
    assert isinstance(_FakeAI(), AIProvider)


def test_service_info_declares_subagent_capability() -> None:
    info = SubagentService().service_info()
    assert info.name == "subagent"
    assert "subagent" in info.capabilities
    assert "ai_chat" in info.requires
    assert info.toggleable is True


def test_config_params_include_enabled_and_ai_prompts() -> None:
    params = SubagentService().config_params()
    by_key = {p.key: p for p in params}
    assert by_key["enabled"].type == ToolParameterType.BOOLEAN
    # Shared preamble + the general-purpose type prompt are both AI-authorable.
    assert by_key["preamble"].ai_prompt is True
    assert by_key["preamble"].multiline is True
    assert by_key["general_purpose_system_prompt"].ai_prompt is True


@pytest.mark.asyncio
async def test_on_config_changed_caches_prompt_overrides() -> None:
    svc = SubagentService()
    await svc.on_config_changed(
        {
            "enabled": True,
            "preamble": "CUSTOM PREAMBLE",
            "general_purpose_system_prompt": "CUSTOM GP PROMPT",
        }
    )
    assert svc._preamble == "CUSTOM PREAMBLE"
    assert svc._type_prompts["general-purpose"] == "CUSTOM GP PROMPT"


@pytest.mark.asyncio
async def test_start_binds_ai_chat_capability() -> None:
    svc = SubagentService()
    fake = _FakeAI()
    await svc.start(_resolver(ai_chat=fake))
    assert svc._ai is fake


@pytest.mark.asyncio
async def test_start_without_ai_chat_raises() -> None:
    svc = SubagentService()
    with pytest.raises(LookupError):
        await svc.start(_resolver())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gilbert.core.services.subagent'`.

- [ ] **Step 3: Write the implementation**

Create `src/gilbert/core/services/subagent.py`:

```python
"""Subagent engine — spawn ephemeral, headless agents in a fresh context.

A subagent is a one-shot, autonomous run: a fresh conversation seeded with a
shared headless preamble + an agent type's system prompt, driven on the type's
AI profile (model + tool gating) with a bounded round budget, returning its
final message. It cannot ask the user anything. This service is the engine;
the user-facing ``spawn_agent`` tool, the live UI, and the ``deep-research``
type are added in later slices.

First-party orchestration of the AI capability — lives in core, resolves
``ai_chat`` (``AIProvider``) via the resolver, and never names a backend/model
(the type's profile owns those).
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.core.subagents.types import get_agent_type, list_agent_types
from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_DEFAULT_PREAMBLE = (
    "You are a subagent launched to complete a single task autonomously. You "
    "cannot ask the user questions or wait for input — make reasonable "
    "assumptions and proceed. Your final message is returned verbatim as the "
    "result to the agent that launched you; it is not shown to the user "
    "directly. Be thorough, then stop."
)


def _prompt_key(type_id: str) -> str:
    """Config key for a type's system-prompt override (``general-purpose`` ->
    ``general_purpose_system_prompt``)."""
    return f"{type_id.replace('-', '_')}_system_prompt"


class SubagentService(Service):
    """Engine that runs a single ephemeral subagent and returns its result."""

    def __init__(self) -> None:
        self._enabled = True
        self._ai: AIProvider | None = None
        self._preamble = _DEFAULT_PREAMBLE
        self._type_prompts: dict[str, str] = {
            t.id: t.system_prompt for t in list_agent_types()
        }

    # --- Service ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="subagent",
            capabilities=frozenset({"subagent"}),
            requires=frozenset({"ai_chat"}),
            toggleable=True,
            toggle_description=(
                "Let the AI spawn ephemeral subagents to work on focused tasks "
                "in a fresh context."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        ai = resolver.require_capability("ai_chat")
        if not isinstance(ai, AIProvider):
            raise RuntimeError("ai_chat capability does not implement AIProvider")
        self._ai = ai
        logger.info("Subagent service started")

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "subagent"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Allow the AI to spawn subagents.",
                default=True,
            ),
            ConfigParam(
                key="preamble",
                type=ToolParameterType.STRING,
                description=(
                    "Shared headless preamble prepended to every subagent's "
                    "system prompt. Encodes the autonomy / no-user-feedback "
                    "contract."
                ),
                default=_DEFAULT_PREAMBLE,
                multiline=True,
                ai_prompt=True,
            ),
        ]
        for t in list_agent_types():
            params.append(
                ConfigParam(
                    key=_prompt_key(t.id),
                    type=ToolParameterType.STRING,
                    description=f"System prompt for the '{t.id}' subagent type.",
                    default=t.system_prompt,
                    multiline=True,
                    ai_prompt=True,
                )
            )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", True))
        self._preamble = str(config.get("preamble") or _DEFAULT_PREAMBLE)
        for t in list_agent_types():
            value = config.get(_prompt_key(t.id))
            self._type_prompts[t.id] = str(value) if value else t.system_prompt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: add SubagentService lifecycle + configurable prompts"
```

---

## Task 3: The `spawn()` engine method

**Files:**
- Modify: `src/gilbert/core/services/subagent.py` (add `spawn`)
- Test: `tests/unit/test_subagent_service.py` (add cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
async def _started(text: str = "subagent result") -> tuple[SubagentService, _FakeAI]:
    svc = SubagentService()
    fake = _FakeAI(text)
    await svc.start(_resolver(ai_chat=fake))
    return svc, fake


@pytest.mark.asyncio
async def test_spawn_drives_chat_with_fresh_context_and_type_config() -> None:
    svc, fake = await _started("the report")
    ctx = UserContext(user_id="u1", email="u@x.com", display_name="U")

    out = await svc.spawn("general-purpose", "Research widgets", user_ctx=ctx)

    assert out == "the report"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # Fresh context: no parent conversation is threaded in.
    assert call["conversation_id"] is None
    # Caller identity is inherited (RBAC applies as for the caller).
    assert call["user_ctx"] is ctx
    # System prompt = shared preamble + the type's prompt.
    assert call["system_prompt"].startswith(_DEFAULT_PREAMBLE)
    assert "general-purpose subagent" in call["system_prompt"]
    # Profile + budget + usage tag come from the type.
    assert call["ai_profile"] == "standard"
    assert call["ai_call"] == "subagent.general-purpose"
    assert call["max_tool_rounds"] == 12
    assert call["user_message"] == "Research widgets"


@pytest.mark.asyncio
async def test_spawn_unknown_type_raises() -> None:
    svc, _ = await _started()
    with pytest.raises(ValueError, match="Unknown agent type"):
        await svc.spawn("nope", "do a thing")


@pytest.mark.asyncio
async def test_spawn_before_start_raises() -> None:
    svc = SubagentService()
    with pytest.raises(RuntimeError, match="not started"):
        await svc.spawn("general-purpose", "do a thing")


@pytest.mark.asyncio
async def test_spawn_uses_configured_prompt_override() -> None:
    svc, fake = await _started()
    await svc.on_config_changed(
        {"preamble": "PRE", "general_purpose_system_prompt": "GP-OVERRIDE"}
    )
    await svc.spawn("general-purpose", "task")
    assert fake.calls[0]["system_prompt"] == "PRE\n\nGP-OVERRIDE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k spawn -q`
Expected: FAIL — `AttributeError: 'SubagentService' object has no attribute 'spawn'`.

- [ ] **Step 3: Write the implementation**

Add this method to `SubagentService` in `src/gilbert/core/services/subagent.py` (after `on_config_changed`):

```python
    # --- engine ---

    async def spawn(
        self,
        agent_type: str,
        prompt: str,
        user_ctx: UserContext | None = None,
    ) -> str:
        """Run one ephemeral subagent of ``agent_type`` on ``prompt``.

        Drives a fresh chat turn (no parent history) with the shared preamble +
        the type's prompt, on the type's AI profile and round budget, inheriting
        the caller's identity for RBAC. Returns the subagent's final message
        text. The subagent cannot ask the user anything (headless preamble; the
        spawn tool excludes interactive tools in a later slice).
        """
        if self._ai is None:
            raise RuntimeError("subagent service not started")
        agent = get_agent_type(agent_type)
        if agent is None:
            raise ValueError(f"Unknown agent type: {agent_type}")

        type_prompt = self._type_prompts.get(agent.id, agent.system_prompt)
        system_prompt = f"{self._preamble}\n\n{type_prompt}"

        result = await self._ai.chat(
            user_message=prompt,
            conversation_id=None,
            user_ctx=user_ctx,
            system_prompt=system_prompt,
            ai_call=f"subagent.{agent.id}",
            ai_profile=agent.profile_name,
            max_tool_rounds=agent.max_rounds,
        )
        return result.response_text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (10 passed — the 6 from Task 2 plus the 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: add spawn() engine method (fresh-context, headless, bounded)"
```

---

## Task 4: Register the service + glossary

**Files:**
- Modify: `src/gilbert/core/app.py`
- Modify: `src/gilbert/CONTEXT.md`

- [ ] **Step 1: Register the service in the composition root**

In `src/gilbert/core/app.py`, find the OCR service registration block:

```python
        # 6c. OCR service
        from gilbert.core.services.ocr import OCRService

        self.service_manager.register(OCRService())
```

Add immediately after it:

```python
        # 6c-bis2. Subagent engine — lets the AI spawn ephemeral, headless
        # agents in a fresh context. Default-on, toggleable; requires the
        # ``ai_chat`` capability (AIService), which is registered above.
        from gilbert.core.services.subagent import SubagentService

        self.service_manager.register(SubagentService())
```

- [ ] **Step 2: Verify the app imports and the service registers (smoke test)**

Run:

```bash
uv run python -c "from gilbert.core.services.subagent import SubagentService; s = SubagentService(); print(s.service_info().name, sorted(s.service_info().capabilities), s.service_info().toggleable)"
```

Expected output: `subagent ['subagent'] True`

- [ ] **Step 3: Add the glossary entry**

In `src/gilbert/CONTEXT.md`, add a glossary entry for **subagent** (place it alphabetically / near the "Agent" entry). Use this text:

```markdown
- **Subagent** — an *ephemeral, headless* agent run spawned within a chat turn
  (the `SubagentService` engine): a fresh context (shared preamble + an agent
  *type* prompt + the task), a scoped toolset + model (from the type's AI
  profile), and a bounded budget. It runs autonomously and **cannot ask the
  user** — its final message is returned as the spawning tool's result. Distinct
  from an **Autonomous agent** (the durable, goal-based agent with persona /
  memory / heartbeats). _Avoid_ calling a subagent an "agent" unqualified.
```

If `CONTEXT.md` has no glossary/term list to slot into, add a short `## Subagents` note with the same content near the agent-related section.

- [ ] **Step 4: Run the full new test set + a broad sanity check**

Run: `uv run pytest tests/unit/test_subagent_types.py tests/unit/test_subagent_service.py -q`
Expected: PASS (13 passed total).

Run: `uv run pytest tests/unit/ -q -k "service or agent" `
Expected: PASS — confirms the new core service didn't break service/agent tests.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/app.py src/gilbert/CONTEXT.md
git commit -m "subagent: register engine in app + add glossary entry"
```

---

## Task 5: Lint, type-check, and full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Lint the new/modified files**

Run:

```bash
uv run ruff check src/gilbert/core/subagents/ src/gilbert/core/services/subagent.py tests/unit/test_subagent_types.py tests/unit/test_subagent_service.py
uv run ruff format --check src/gilbert/core/subagents/ src/gilbert/core/services/subagent.py tests/unit/test_subagent_types.py tests/unit/test_subagent_service.py
```

Expected: "All checks passed!" and no files needing reformat. If format complains, run `uv run ruff format <those paths>` and re-commit.

- [ ] **Step 2: Type-check core**

Run: `uv run mypy src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py`
Expected: `Success: no issues found`. (If the AIProvider/ChatTurnResult import triggers a stub warning, fix the annotation; do not suppress with `# type: ignore`.)

- [ ] **Step 3: Run the full unit suite**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (all green; no regressions).

- [ ] **Step 4: Commit any lint/format fixups**

```bash
git add -A
git commit -m "subagent: lint/format fixups" || echo "nothing to commit"
```

---

## Self-review notes (author check)

- **Spec coverage (slice 1 scope):** core `SubagentService` default-on + toggleable (Task 2/4) ✓; agent-type registry + `general-purpose` (Task 1) ✓; fresh-context, headless-preamble, bounded `spawn` driving `AIProvider.chat` with the type's profile (Task 3) ✓; configurable preamble + per-type prompts as `ai_prompt` ConfigParams (Task 2) ✓; AI-backend-visibility respected — engine names no backend, profile does (Task 1/3) ✓; glossary "subagent" (Task 4) ✓.
- **Deferred to later slices (called out in spec, not gaps):** `spawn_agent`/`deep_research` tools + `ToolDefinition.interactive` + no-nesting gating (slice 2); `subagent.*` events + UI card (slice 3); `deep-research` type + Tongyi profile + openrouter catalog (slice 4).
- **Type consistency:** `AgentType` fields (`id/description/system_prompt/profile_name/max_rounds/max_wall_clock_s`) used identically in `types.py` and `subagent.py`; `_prompt_key()` is the single source for the config-key transform used by both `config_params` and `on_config_changed`; `ChatTurnResult.response_text` is the only return field read.
- **No placeholders:** every code step contains complete code; every run step has an exact command + expected result.
