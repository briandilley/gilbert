# Autonomous Agent — Phase 1: `run_loop` Primitive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tested, pure async `run_loop` primitive in `src/gilbert/core/agent_loop.py` that drives one AI tool-use loop with budgets and a structured result. Plus complete the verification items from the spec so subsequent phases (chat refactor, NotificationService, AutonomousAgentService, UI) start from confirmed ground.

**Architecture:** A single async function with no service dependencies — takes an `AIBackend`, system prompt, message list, tool dict, and budgets; iterates `backend.generate_stream()` consuming events to find `MESSAGE_COMPLETE`; on `TOOL_USE`, executes tools (parallel if backend supports it) and loops; terminates on `END_TURN`, `MAX_TOKENS`, max-rounds, wall-clock deadline, token budget, or unrecoverable error. Returns a `LoopResult` with the final message, full message history, stop reason, and token/round counters.

**Tech Stack:** Python 3.12+, `asyncio`, `uv` for dependency/test management, pytest with `pytest-asyncio`, no new external deps.

**Out of scope for this plan:** Phase 2 (chat refactor), Phase 3 (NotificationService), Phase 4 (AutonomousAgentService), Phase 5 (UI). Each gets its own plan after the prior phase ships. Verification items that are not blockers for Phase 1 are *researched* here but any *patches* are deferred to the relevant phase's plan.

---

## File Structure

**Create:**
- `src/gilbert/core/agent_loop.py` — the primitive (one file, ~200 lines)
- `tests/unit/core/test_agent_loop.py` — tests against a fake `AIBackend`
- `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` — findings doc for the pre-flight verification tasks

**Modify:** None. `run_loop` has no callers in Phase 1.

---

## Pre-flight: Verification Tasks

These tasks verify open items from the spec. They produce findings written into a single verification doc that subsequent phase plans reference. **None of them block Phase 1 implementation** — they exist so Phase 2/3/4/5 plans can proceed without re-discovering the same questions.

### Task 0: Initialize the verification findings doc

**Files:**
- Create: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md`

- [ ] **Step 1: Create the findings doc with section headers**

Write the file:

```markdown
# Autonomous Agent — Verification Findings

Companion to `2026-05-03-autonomous-agent-design.md`. Each section captures the answer to one open verification item from the spec, with file/line citations and (if applicable) a follow-up task assigned to a downstream phase plan.

## 1. push_to_user capability in web layer

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 2. SchedulerService.add_job idempotency on name

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 3. event_types registry / dynamic choices

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 4. Conversation auto-archive policy

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 5. Workspace cleanup hooks via chat.conversation.archiving

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 6. Conversation auth model — shared read access

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 7. AI-call log named-call mechanism

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD
```

- [ ] **Step 2: Commit the scaffold**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs: scaffold verification findings doc for autonomous agent spec"
```

---

### Task 0.1: Verify push_to_user capability in web layer

**Files:**
- Read: `src/gilbert/web/ws_protocol.py`, `src/gilbert/web/auth.py`, anything else reachable from a `ws_handlers` capability touchpoint
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 1)

- [ ] **Step 1: Search the web layer for an existing per-user push helper**

Run:
```bash
grep -rn "push_to_user\|broadcast.*user\|user_ws\|user_id.*connections\|connections.*user_id" src/gilbert/web/ src/gilbert/core/services/ 2>/dev/null | head -30
```

Expected: either a clear hit (path/line for the helper) or nothing (capability is missing).

- [ ] **Step 2: Read the most likely candidate file**

If the grep surfaces something, read that file. If nothing surfaces, read `src/gilbert/web/ws_protocol.py` to confirm the absence and understand what registry of connections exists.

- [ ] **Step 3: Update the findings doc, section 1**

Replace the section's three TBD lines with the actual finding. Pick one of:

```markdown
**Status:** Exists
**Findings:** `<path>:<line>` exposes `<helper_name>(user_id, frame)` that broadcasts to all of a user's active connections. Used by `<example_caller>`.
**Follow-up:** None — Phase 3 NotificationService subscriber wires directly to this helper.
```

or:

```markdown
**Status:** Missing
**Findings:** No per-user push helper exists. Connections are tracked in `<path>:<line>` as `<shape>` keyed by `<key>`. Adding `push_to_user(user_id, frame)` requires: (1) maintain `dict[user_id, set[WsConnection]]` updated on connect/disconnect, (2) expose a `push_to_user` method on the WS service, (3) declare a `user_ws_pusher` capability so other services can resolve it.
**Follow-up:** Phase 3 plan must include a task to add `push_to_user`. Tag this verification finding in the Phase 3 plan's pre-flight section.
```

- [ ] **Step 4: Commit the finding**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): push_to_user capability finding"
```

---

### Task 0.2: Verify SchedulerService.add_job idempotency on name

**Files:**
- Read: `src/gilbert/core/services/scheduler.py` (or the equivalent path)
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 2)

- [ ] **Step 1: Locate the SchedulerService implementation**

Run:
```bash
grep -rln "class.*Scheduler.*Service\|SchedulerProvider\|add_job\b" src/gilbert/core/services/ 2>/dev/null | head -5
```

- [ ] **Step 2: Read `add_job` and check idempotency on name**

Open the file from step 1. Find `add_job`. Determine:
- Does it raise on duplicate name?
- Does it overwrite the existing job silently?
- Does it return the existing job?

- [ ] **Step 3: Update the findings doc, section 2**

Replace TBD with one of:

```markdown
**Status:** Idempotent on name
**Findings:** `<path>:<line>` — `add_job` overwrites/replaces an existing job with the same name. Re-arming on goal update is a single call.
**Follow-up:** None — Phase 4 `_arm_trigger` calls `add_job` directly.
```

or:

```markdown
**Status:** Not idempotent
**Findings:** `<path>:<line>` — `add_job` raises/duplicates if a job with the same name exists. Re-arming on goal update must call `remove_job(name)` first.
**Follow-up:** Phase 4 plan: `_arm_trigger` does `remove_job` (best-effort, swallow not-found) then `add_job`.
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): scheduler add_job idempotency finding"
```

---

### Task 0.3: Verify event_types registry / dynamic choices

**Files:**
- Read: `src/gilbert/interfaces/configuration.py`, `src/gilbert/core/services/configuration.py`, anything that handles `choices_from`
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 3)

- [ ] **Step 1: Search for existing event-type discovery**

Run:
```bash
grep -rn "choices_from\|event_types\|registered_event\|known_events\|event_type_registry" src/gilbert/ 2>/dev/null | head -30
```

- [ ] **Step 2: Determine whether `choices_from="event_types"` is feasible today**

- If a registry exists, note where event types are registered and how `choices_from` resolves.
- If not, decide on the fallback (free-text input with autocomplete from event types observed at runtime via the EventBus).

- [ ] **Step 3: Update findings doc, section 3**

Pick one:

```markdown
**Status:** Registry exists
**Findings:** `<path>:<line>` — events register their type via `<mechanism>`. `choices_from="event_types"` resolves through `<resolver>`.
**Follow-up:** None — Phase 5 goal-create form uses `choices_from="event_types"` as designed.
```

or:

```markdown
**Status:** No registry
**Findings:** Event types are string literals scattered across publishers. No central registry. ConfigurationService's `choices_from` resolver `<path>:<line>` does not have an `event_types` source.
**Follow-up:** Phase 5 plan: trigger config UI uses a free-text input with an autocomplete suggester populated from a runtime-observed set in EventBus (a `set[str]` of event_types seen since process start). Add a small `EventBus.observed_event_types` accessor in Phase 4 if it doesn't already exist.
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): event_types dynamic choices finding"
```

---

### Task 0.4: Verify conversation auto-archive policy

**Files:**
- Read: `src/gilbert/core/services/ai.py` around the `chat.conversation.archiving` publish points (lines ~6302 and ~6499 per earlier survey), plus any scheduler jobs the AIService registers
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 4)

- [ ] **Step 1: Find every place `chat.conversation.archiving` is published**

Run:
```bash
grep -rn "chat\.conversation\.archiving" src/gilbert/ 2>/dev/null
```

- [ ] **Step 2: For each publish site, determine the trigger**

Open each call site and trace upward to determine whether archive is:
- User-explicit (a delete RPC handler)
- Time-based (a scheduler job firing after N days idle)
- Both

- [ ] **Step 3: Update findings doc, section 4**

```markdown
**Status:** <Explicit-only | Auto-archive after N days | Both>
**Findings:** `<path>:<line>` publishes archiving on <trigger>. <If auto-archive: include the threshold and the scheduler job that drives it.>
**Follow-up:** <If explicit-only: None — agent goal conversations stay forever unless explicitly deleted.>
                <If auto-archive: Phase 4 plan adds a `pinned: bool = False` (or `do_not_auto_archive`) flag on the conversation entity, set to True when AgentService creates a goal's conversation. Auto-archive scheduler must skip pinned conversations — Phase 4 includes the SkipFilter wiring.>
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): conversation auto-archive finding"
```

---

### Task 0.5: Verify workspace cleanup hooks via `chat.conversation.archiving`

**Files:**
- Read: `src/gilbert/core/services/workspace.py`
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 5)

- [ ] **Step 1: Determine whether the workspace service subscribes to archiving**

Run:
```bash
grep -n "chat\.conversation\.archiving\|subscribe.*archiv" src/gilbert/core/services/workspace.py
```

- [ ] **Step 2: Inspect the subscriber (if present) and confirm it deletes workspace files for the archived conversation**

- [ ] **Step 3: Update findings doc, section 5**

```markdown
**Status:** <Subscribed and cleans | Not subscribed>
**Findings:** <If subscribed: `<path>:<line>` — handler deletes the workspace dir for the conversation_id and purges entries from `_WORKSPACE_FILES_COLLECTION`. Confirmed on lines `<X-Y>`.>
                <If not subscribed: workspace files persist after conversation archive. Cleanup must be wired in the workspace service subscribing to `chat.conversation.archiving`.>
**Follow-up:** <If subscribed: None.>
                <If not subscribed: Phase 4 plan adds the subscription in WorkspaceService.start() with handler that calls `_remove_conversation_workspace(conversation_id)`. Tag this verification finding in Phase 4's pre-flight section.>
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): workspace cleanup on archiving finding"
```

---

### Task 0.6: Verify conversation auth model for shared read

**Files:**
- Read: the AIService conversation auth code — likely around `chat()` setup or a helper checking access; search for `visible_to`, `shared_with`, or `acl` patterns near conversations.
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 6)

- [ ] **Step 1: Search for conversation-level read-access mechanisms**

Run:
```bash
grep -rn "visible_to\|conversation.*acl\|shared_with\|conversation.*member" src/gilbert/core/services/ai.py src/gilbert/web/ 2>/dev/null | head -30
```

- [ ] **Step 2: Read the conversation entity definition + access-check code paths**

Determine whether a non-owner user can read a conversation, and how access is granted (per-conversation ACL, role-based, both, none).

- [ ] **Step 3: Update findings doc, section 6**

```markdown
**Status:** <Per-conversation ACL exists | Owner-only | Hybrid>
**Findings:** `<path>:<line>` — <describe mechanism>. Granting read access to a non-owner is done by <method/field>.
**Follow-up:** <If per-conversation ACL exists: None — Phase 4 sets the goal conversation's read-access list to `notify_user_ids` on creation, updates on goal update.>
                <If owner-only: Phase 4 plan must add per-conversation read access (potentially substantial). Either implement a small ACL extension OR scope agents to single-user goals only in v1 (notify_user_ids = [owner] hard constraint) — flag this as a scope question for the Phase 4 plan.>
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): conversation shared-read auth finding"
```

---

### Task 0.7: Verify AI-call log named-call mechanism

**Files:**
- Read: `src/gilbert/core/services/user_memory.py` for `_AI_CALL_NAME` usage, plus the AI logging implementation
- Modify: `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` (section 7)

- [ ] **Step 1: Find the named-call API**

Run:
```bash
grep -rn "_AI_CALL_NAME\|ai_call=\|ai_call_name" src/gilbert/ 2>/dev/null | head -20
```

- [ ] **Step 2: Read the AI logging code path**

Trace `ai_call` from a caller (user_memory) into the AI service's logging. Document how named calls are surfaced in the AI API call log.

- [ ] **Step 3: Update findings doc, section 7**

```markdown
**Status:** Mechanism documented
**Findings:** Callers pass `ai_call="<name>"` to `AIProvider.chat()` (or an analogous parameter on `complete_one_shot`). The AI service tags the API call log entry at `<path>:<line>` so the operator log distinguishes calls. Existing names in use: `<list>`.
**Follow-up:** Phase 4 plan: AgentService's run-loop calls and digest-summarization calls each pass distinct `ai_call` names — proposed `agent.run` and `agent.digest`. Phase 4 verifies the names land correctly in the log.
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md
git commit -m "docs(verification): AI-call log named-call mechanism finding"
```

---

## Phase 1: `run_loop` Primitive Implementation (TDD)

Strict TDD from here: write the failing test, run it red, implement minimal code, run it green, commit. One assertion area per task.

### Task 1: Define types — `LoopStopReason`, `LoopResult`, `ToolHandler`

**Files:**
- Create: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Create the file with imports and type definitions**

Write the file:

```python
"""Pure async loop primitive for AI tool-use loops.

Used by both ``AIService.chat()`` (after refactor) and the upcoming
``AutonomousAgentService.run_goal()``. The loop drives one
``AIBackend.generate_stream()`` per round, consumes events to find
``MESSAGE_COMPLETE``, and on ``TOOL_USE`` executes tools (in parallel
when the backend supports it) and iterates. Termination: ``END_TURN``,
backend ``MAX_TOKENS``, max-rounds, wall-clock budget, token budget,
or unrecoverable error.

This module is intentionally service-free — no event bus, no scheduler,
no storage. Streaming/persistence/UI concerns belong to the caller.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
    StreamEventType,
)
from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult


class LoopStopReason(StrEnum):
    """Why ``run_loop`` returned.

    Distinct from ``StopReason`` (which is per-round, backend-emitted)
    because the loop has its own termination conditions on top of what
    the model decides.
    """

    END_TURN = "end_turn"
    """Model emitted END_TURN — natural completion."""

    MAX_ROUNDS = "max_rounds"
    """Hit the loop's per-call round cap before END_TURN."""

    WALL_CLOCK = "wall_clock"
    """Wall-clock deadline elapsed between rounds."""

    TOKEN_BUDGET = "token_budget"
    """Cumulative tokens (input + output across all rounds) exceeded the cap."""

    MAX_TOKENS = "max_tokens"
    """Backend hit its per-round output cap on the last round. The loop does
    not implement continuation in this primitive; callers that need
    chat-style continuation can wrap ``run_loop`` and re-invoke it."""

    ERROR = "error"
    """Unrecoverable error during the loop. ``LoopResult.error`` holds it."""


ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]
"""Async callable that executes a tool. Takes the parsed argument dict,
returns the tool's textual result. Exceptions are caught by the loop
and formatted as error tool-result messages."""


@dataclass
class LoopResult:
    """The outcome of one ``run_loop`` call."""

    final_message: Message
    """The last assistant ``Message`` from the loop. For successful
    completion this is the END_TURN response; for budget/error
    terminations it's whatever the last round produced (may be empty)."""

    full_message_history: list[Message]
    """Initial messages + every assistant + every tool_result message
    accumulated by the loop. Caller persists this verbatim if it cares
    to record the run."""

    stop_reason: LoopStopReason

    rounds_used: int
    tokens_in: int
    tokens_out: int

    error: Exception | None = None
    """Set when ``stop_reason == LoopStopReason.ERROR``. Otherwise None."""
```

- [ ] **Step 2: Verify file compiles and imports resolve**

Run:
```bash
uv run python -c "from gilbert.core.agent_loop import LoopStopReason, LoopResult, ToolHandler; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/core/agent_loop.py
git commit -m "agent_loop: add LoopStopReason, LoopResult, ToolHandler types"
```

---

### Task 2: Add the empty `run_loop` signature

**Files:**
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Append the function signature and a `NotImplementedError` body**

At the end of `src/gilbert/core/agent_loop.py`, append:

```python


async def run_loop(
    *,
    backend: AIBackend,
    system_prompt: str,
    messages: list[Message],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
    max_rounds: int,
    max_wall_clock_s: float | None = None,
    max_tokens: int | None = None,
    model: str = "",
) -> LoopResult:
    """Drive one AI tool-use loop end-to-end.

    See module docstring for the contract. All arguments are keyword-only —
    every additional knob in the future should also be keyword-only so the
    call sites stay readable.
    """
    raise NotImplementedError
```

- [ ] **Step 2: Verify**

Run:
```bash
uv run python -c "from gilbert.core.agent_loop import run_loop; print(run_loop.__doc__[:40])"
```

Expected output: `Drive one AI tool-use loop end-to-end.`

- [ ] **Step 3: Commit**

```bash
git add src/gilbert/core/agent_loop.py
git commit -m "agent_loop: add run_loop signature stub"
```

---

### Task 3: Test scaffold — `FakeAIBackend` and pytest fixtures

**Files:**
- Create: `tests/unit/core/test_agent_loop.py`

- [ ] **Step 1: Write the test scaffold with a scriptable fake backend**

Write the file:

```python
"""Unit tests for ``gilbert.core.agent_loop.run_loop``.

The fake backend takes a scripted list of ``(events, capabilities_kwargs)``
tuples — one per ``generate_stream`` call. Tests assemble scripts that
exercise specific loop behaviors (END_TURN, tool calls, budget hits,
etc.) and assert against the returned ``LoopResult``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from gilbert.core.agent_loop import (
    LoopResult,
    LoopStopReason,
    ToolHandler,
    run_loop,
)
from gilbert.interfaces.ai import (
    AIBackend,
    AIBackendCapabilities,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    StreamEvent,
    StreamEventType,
    TokenUsage,
)
from gilbert.interfaces.tools import ToolCall, ToolDefinition, ToolResult


def _msg_complete(
    *,
    text: str = "",
    tool_calls: list[ToolCall] | None = None,
    stop_reason: StopReason = StopReason.END_TURN,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> StreamEvent:
    """Build a single MESSAGE_COMPLETE event for the fake backend script."""
    return StreamEvent(
        type=StreamEventType.MESSAGE_COMPLETE,
        response=AIResponse(
            message=Message(
                role=MessageRole.ASSISTANT,
                content=text,
                tool_calls=tool_calls or [],
            ),
            model="fake",
            stop_reason=stop_reason,
            usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
    )


class FakeAIBackend(AIBackend):
    """Backend that replays a pre-scripted list of stream events per round."""

    backend_name = ""  # don't register

    def __init__(
        self,
        scripts: list[list[StreamEvent]],
        *,
        parallel_tool_calls: bool = False,
        streaming: bool = True,
        raise_on_round: int | None = None,
    ) -> None:
        self._scripts = scripts
        self._round = 0
        self._caps = AIBackendCapabilities(
            streaming=streaming,
            parallel_tool_calls=parallel_tool_calls,
        )
        self._raise_on_round = raise_on_round
        self.requests_seen: list[AIRequest] = []

    async def initialize(self, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    def capabilities(self) -> AIBackendCapabilities:
        return self._caps

    async def generate(self, request: AIRequest) -> AIResponse:
        # Not used — run_loop calls generate_stream.
        raise NotImplementedError

    async def generate_stream(self, request: AIRequest) -> AsyncIterator[StreamEvent]:
        if self._raise_on_round is not None and self._round == self._raise_on_round:
            raise RuntimeError("scripted backend failure")
        self.requests_seen.append(request)
        if self._round >= len(self._scripts):
            raise AssertionError(
                f"FakeAIBackend out of script: round {self._round}, only "
                f"{len(self._scripts)} round(s) scripted"
            )
        events = self._scripts[self._round]
        self._round += 1
        for ev in events:
            yield ev


# pytest-asyncio convention used elsewhere in the repo
pytestmark = pytest.mark.asyncio
```

- [ ] **Step 2: Verify imports**

Run:
```bash
uv run python -c "import tests.unit.core.test_agent_loop; print('ok')" 2>&1 | tail -5
```

Expected output ends with: `ok`. (If any import fails, fix the import in the test file before continuing.)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_agent_loop.py
git commit -m "test(agent_loop): scaffold FakeAIBackend and event helpers"
```

---

### Task 4: Test + impl — single END_TURN round

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_single_end_turn_round_terminates_immediately() -> None:
    backend = FakeAIBackend(scripts=[[_msg_complete(text="hello")]])
    initial = [Message(role=MessageRole.USER, content="hi")]

    result = await run_loop(
        backend=backend,
        system_prompt="you are a test bot",
        messages=initial,
        tools={},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    assert result.final_message.content == "hello"
    assert result.rounds_used == 1
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.error is None
    # full_message_history = initial + assistant
    assert len(result.full_message_history) == 2
    assert result.full_message_history[0] is initial[0]
    assert result.full_message_history[1].role == MessageRole.ASSISTANT
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_single_end_turn_round_terminates_immediately -v
```

Expected: FAIL with `NotImplementedError` from `run_loop`.

- [ ] **Step 3: Implement minimal `run_loop` body**

Replace the `raise NotImplementedError` body of `run_loop` in `src/gilbert/core/agent_loop.py` with:

```python
    history = list(messages)
    tokens_in = 0
    tokens_out = 0
    final_message = Message(role=MessageRole.ASSISTANT, content="")
    rounds_used = 0

    for _ in range(max_rounds):
        rounds_used += 1
        request = AIRequest(
            messages=history,
            system_prompt=system_prompt,
            tools=[t[0] for t in tools.values()],
            model=model,
        )
        response = None
        async for ev in backend.generate_stream(request):
            if ev.type == StreamEventType.MESSAGE_COMPLETE:
                response = ev.response
        if response is None:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.ERROR,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=RuntimeError(
                    "backend stream ended without MESSAGE_COMPLETE"
                ),
            )

        if response.usage:
            tokens_in += response.usage.input_tokens
            tokens_out += response.usage.output_tokens
        final_message = response.message
        history.append(response.message)

        if response.stop_reason == StopReason.END_TURN:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.END_TURN,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        # Tool use, max-tokens, and budgets get added in subsequent tasks.
        break

    return LoopResult(
        final_message=final_message,
        full_message_history=history,
        stop_reason=LoopStopReason.MAX_ROUNDS,
        rounds_used=rounds_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_single_end_turn_round_terminates_immediately -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: implement single-round END_TURN path"
```

---

### Task 5: Test + impl — one tool-call round followed by END_TURN

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_tool_call_round_then_end_turn() -> None:
    tool_def = ToolDefinition(
        name="echo",
        description="Echo the input",
        parameters=[],
    )
    invocations: list[dict[str, Any]] = []

    async def echo_handler(args: dict[str, Any]) -> str:
        invocations.append(args)
        return f"echoed: {args.get('text', '')}"

    round0 = [
        _msg_complete(
            text="let me echo",
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="echo", arguments={"text": "hi"})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="done")]

    backend = FakeAIBackend(scripts=[round0, round1])

    result = await run_loop(
        backend=backend,
        system_prompt="you are a test bot",
        messages=[Message(role=MessageRole.USER, content="hi")],
        tools={"echo": (tool_def, echo_handler)},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    assert result.rounds_used == 2
    assert invocations == [{"text": "hi"}]

    # History: user, assistant(tool_call), tool_result, assistant(end_turn)
    assert len(result.full_message_history) == 4
    assert result.full_message_history[1].role == MessageRole.ASSISTANT
    assert result.full_message_history[1].tool_calls[0].tool_name == "echo"
    assert result.full_message_history[2].role == MessageRole.TOOL_RESULT
    tr = result.full_message_history[2].tool_results[0]
    assert tr.tool_call_id == "t1"
    assert tr.content == "echoed: hi"
    assert tr.is_error is False
    assert result.full_message_history[3].content == "done"
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_tool_call_round_then_end_turn -v
```

Expected: FAIL — `run_loop` currently `break`s on TOOL_USE without executing.

- [ ] **Step 3: Replace the `break` with tool execution + continue**

In `src/gilbert/core/agent_loop.py`, replace the comment `# Tool use, max-tokens, and budgets get added in subsequent tasks.` and the `break` with:

```python
        if response.stop_reason == StopReason.TOOL_USE and response.message.tool_calls:
            tool_results = await _execute_tool_calls_sequential(
                response.message.tool_calls, tools
            )
            history.append(
                Message(role=MessageRole.TOOL_RESULT, tool_results=tool_results)
            )
            continue

        # No other stop reasons handled yet — break out of the loop and
        # let the post-loop fallthrough mark this MAX_ROUNDS. Subsequent
        # tasks add MAX_TOKENS / budget handling.
        break
```

Then add the helper at module bottom (above existing `run_loop` if necessary, or below — Python resolves at call time):

```python


async def _execute_tool_calls_sequential(
    tool_calls: list[ToolCall],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
) -> list[ToolResult]:
    """Execute tool calls one at a time, in order. Errors are caught and
    formatted as error tool results so the loop continues with whatever
    the agent decides to do next.
    """
    results: list[ToolResult] = []
    for tc in tool_calls:
        result = await _invoke_one_tool(tc, tools)
        results.append(result)
    return results


async def _invoke_one_tool(
    tc: ToolCall,
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
) -> ToolResult:
    pair = tools.get(tc.tool_name)
    if pair is None:
        return ToolResult(
            tool_call_id=tc.tool_call_id,
            content=f"tool not found: {tc.tool_name}",
            is_error=True,
        )
    _, handler = pair
    try:
        content = await handler(tc.arguments)
    except Exception as exc:  # tools failing must not crash the loop
        return ToolResult(
            tool_call_id=tc.tool_call_id,
            content=f"tool failed: {exc!r}",
            is_error=True,
        )
    return ToolResult(
        tool_call_id=tc.tool_call_id,
        content=content,
        is_error=False,
    )
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_tool_call_round_then_end_turn -v
```

Expected: PASS.

- [ ] **Step 5: Run all tests in the module to confirm no regression**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: execute tool calls sequentially and continue loop"
```

---

### Task 6: Test + impl — `MAX_ROUNDS` termination

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_max_rounds_terminates_loop() -> None:
    tool_def = ToolDefinition(name="loop", description="loop forever", parameters=[])

    async def loop_handler(args: dict[str, Any]) -> str:
        return "ok"

    # Every round emits another tool call — never END_TURN — so the loop
    # must terminate via MAX_ROUNDS.
    round_with_tool = [
        _msg_complete(
            text="",
            tool_calls=[
                ToolCall(tool_call_id=f"t", tool_name="loop", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    backend = FakeAIBackend(scripts=[round_with_tool] * 3)

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"loop": (tool_def, loop_handler)},
        max_rounds=3,
    )

    assert result.stop_reason == LoopStopReason.MAX_ROUNDS
    assert result.rounds_used == 3
```

- [ ] **Step 2: Run the test**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_max_rounds_terminates_loop -v
```

Expected: PASS without code changes (the existing impl falls through to MAX_ROUNDS naturally).

If it fails, inspect why — the existing impl should already cover this. If it passes, no implementation change needed; just commit.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_agent_loop.py
git commit -m "test(agent_loop): cover MAX_ROUNDS termination"
```

---

### Task 7: Test + impl — parallel tool calls when backend supports it

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_parallel_tool_calls_dispatched_concurrently() -> None:
    tool_def = ToolDefinition(name="slow", description="slow", parameters=[])
    invocation_log: list[str] = []
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_handler(args: dict[str, Any]) -> str:
        invocation_log.append(f"start:{args['n']}")
        started.set()
        await proceed.wait()
        invocation_log.append(f"end:{args['n']}")
        return f"r{args['n']}"

    round0 = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="slow", arguments={"n": 1}),
                ToolCall(tool_call_id="t2", tool_name="slow", arguments={"n": 2}),
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="ok")]

    backend = FakeAIBackend(
        scripts=[round0, round1],
        parallel_tool_calls=True,
    )

    async def driver() -> LoopResult:
        return await run_loop(
            backend=backend,
            system_prompt="x",
            messages=[Message(role=MessageRole.USER, content="go")],
            tools={"slow": (tool_def, slow_handler)},
            max_rounds=10,
        )

    task = asyncio.create_task(driver())
    # Give run_loop a chance to enter the tool dispatch and start both
    await asyncio.wait_for(started.wait(), timeout=1.0)
    # Both should be started before either ends
    assert sorted(x for x in invocation_log if x.startswith("start:")) == [
        "start:1",
        "start:2",
    ]
    assert not any(x.startswith("end:") for x in invocation_log)
    proceed.set()
    result = await asyncio.wait_for(task, timeout=2.0)

    assert result.stop_reason == LoopStopReason.END_TURN
    # Both ends recorded; order between them is non-deterministic
    assert {x for x in invocation_log if x.startswith("end:")} == {"end:1", "end:2"}
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_parallel_tool_calls_dispatched_concurrently -v
```

Expected: FAIL — sequential execution means the `started.set()` events fire one-after-another, and the assertion that both start before either end fails.

- [ ] **Step 3: Add a parallel dispatch helper and select between strategies**

In `src/gilbert/core/agent_loop.py`, change the call site `tool_results = await _execute_tool_calls_sequential(...)` to:

```python
            if backend.capabilities().parallel_tool_calls and len(response.message.tool_calls) > 1:
                tool_results = await _execute_tool_calls_parallel(
                    response.message.tool_calls, tools
                )
            else:
                tool_results = await _execute_tool_calls_sequential(
                    response.message.tool_calls, tools
                )
```

And add the parallel helper alongside the sequential one:

```python


async def _execute_tool_calls_parallel(
    tool_calls: list[ToolCall],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
) -> list[ToolResult]:
    """Execute tool calls concurrently. Each invocation is independently
    wrapped in error handling so one failure doesn't poison the others.
    Result order matches the input order so ``zip(tool_calls, results)``
    is meaningful.
    """
    return await asyncio.gather(
        *(_invoke_one_tool(tc, tools) for tc in tool_calls)
    )
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_parallel_tool_calls_dispatched_concurrently -v
```

Expected: PASS.

- [ ] **Step 5: Verify no regressions**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: dispatch tool calls in parallel when backend supports it"
```

---

### Task 8: Test — tool exception is surfaced as an error tool result

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`

- [ ] **Step 1: Write the test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_tool_exception_becomes_error_tool_result_and_loop_continues() -> None:
    tool_def = ToolDefinition(name="boom", description="boom", parameters=[])

    async def boom_handler(args: dict[str, Any]) -> str:
        raise RuntimeError("kaboom")

    round0 = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="boom", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="recovered")]

    backend = FakeAIBackend(scripts=[round0, round1])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"boom": (tool_def, boom_handler)},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    assert result.error is None
    # The tool result message has is_error=True and a "tool failed" content
    tr_msg = result.full_message_history[2]
    assert tr_msg.role == MessageRole.TOOL_RESULT
    tr = tr_msg.tool_results[0]
    assert tr.is_error is True
    assert "tool failed" in tr.content
    assert "kaboom" in tr.content
```

- [ ] **Step 2: Run the test**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_tool_exception_becomes_error_tool_result_and_loop_continues -v
```

Expected: PASS without code changes (`_invoke_one_tool` already wraps `try/except`).

If it fails, inspect; otherwise commit.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_agent_loop.py
git commit -m "test(agent_loop): cover tool-exception error result handling"
```

---

### Task 9: Test + impl — AI backend exception during stream returns ERROR

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_backend_exception_returns_error_loop_result() -> None:
    backend = FakeAIBackend(scripts=[[]], raise_on_round=0)

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.ERROR
    assert isinstance(result.error, RuntimeError)
    assert "scripted backend failure" in str(result.error)
    # Loop ran one round (the failing one) and bailed
    assert result.rounds_used == 1
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_backend_exception_returns_error_loop_result -v
```

Expected: FAIL — the backend exception currently bubbles out of `run_loop`.

- [ ] **Step 3: Wrap the stream consumption in `try/except`**

In `src/gilbert/core/agent_loop.py`, find the block that iterates `backend.generate_stream(request)` and wrap it:

```python
        response = None
        try:
            async for ev in backend.generate_stream(request):
                if ev.type == StreamEventType.MESSAGE_COMPLETE:
                    response = ev.response
        except Exception as exc:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.ERROR,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=exc,
            )
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_backend_exception_returns_error_loop_result -v
```

Expected: PASS.

- [ ] **Step 5: Run all tests**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: catch backend exceptions and return ERROR result"
```

---

### Task 10: Test + impl — wall-clock budget exceeded between rounds

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_wall_clock_budget_exceeded_between_rounds() -> None:
    """The first round completes; the loop checks wall-clock before the
    next round starts and terminates with WALL_CLOCK.
    """
    tool_def = ToolDefinition(name="slow", description="slow", parameters=[])

    async def slow_handler(args: dict[str, Any]) -> str:
        # Burn at least 0.05s so the deadline check after this round trips
        await asyncio.sleep(0.05)
        return "ok"

    round_with_tool = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t", tool_name="slow", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    # Two scripted rounds — but wall-clock should kill us before round 2.
    backend = FakeAIBackend(scripts=[round_with_tool, round_with_tool])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"slow": (tool_def, slow_handler)},
        max_rounds=10,
        max_wall_clock_s=0.01,  # already exceeded after round 1's tool ran
    )

    assert result.stop_reason == LoopStopReason.WALL_CLOCK
    assert result.rounds_used == 1
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_wall_clock_budget_exceeded_between_rounds -v
```

Expected: FAIL — no wall-clock check yet.

- [ ] **Step 3: Add a deadline check before each round**

In `src/gilbert/core/agent_loop.py`, just inside the `for _ in range(max_rounds):` loop, capture the deadline once before the loop and check at the top of each iteration. Replace the loop scaffolding to look like this:

Change:
```python
    final_message = Message(role=MessageRole.ASSISTANT, content="")
    rounds_used = 0

    for _ in range(max_rounds):
        rounds_used += 1
```

To:
```python
    final_message = Message(role=MessageRole.ASSISTANT, content="")
    rounds_used = 0
    deadline: float | None = (
        time.monotonic() + max_wall_clock_s if max_wall_clock_s is not None else None
    )

    for _ in range(max_rounds):
        if deadline is not None and time.monotonic() >= deadline:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.WALL_CLOCK,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        rounds_used += 1
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_wall_clock_budget_exceeded_between_rounds -v
```

Expected: PASS.

- [ ] **Step 5: Run all tests**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: enforce wall-clock budget between rounds"
```

---

### Task 11: Test + impl — token budget exceeded between rounds

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_token_budget_exceeded_between_rounds() -> None:
    """Round 1 records 60 tokens (10+5 default usage from `_msg_complete` is
    overridden here); the loop checks the cumulative total before the next
    round and terminates with TOKEN_BUDGET.
    """
    tool_def = ToolDefinition(name="loop", description="loop", parameters=[])

    async def handler(args: dict[str, Any]) -> str:
        return "ok"

    round_with_tool = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t", tool_name="loop", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
            input_tokens=40,
            output_tokens=20,
        )
    ]
    backend = FakeAIBackend(scripts=[round_with_tool, round_with_tool])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"loop": (tool_def, handler)},
        max_rounds=10,
        max_tokens=50,  # 60 cumulative > 50 → bail before round 2
    )

    assert result.stop_reason == LoopStopReason.TOKEN_BUDGET
    assert result.rounds_used == 1
    assert result.tokens_in == 40
    assert result.tokens_out == 20
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_token_budget_exceeded_between_rounds -v
```

Expected: FAIL — no token check yet.

- [ ] **Step 3: Add a token-budget check at the top of each round (after the wall-clock check)**

In `src/gilbert/core/agent_loop.py`, augment the top-of-iteration block:

```python
    for _ in range(max_rounds):
        if deadline is not None and time.monotonic() >= deadline:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.WALL_CLOCK,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        if max_tokens is not None and (tokens_in + tokens_out) >= max_tokens:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.TOKEN_BUDGET,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        rounds_used += 1
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_token_budget_exceeded_between_rounds -v
```

Expected: PASS.

- [ ] **Step 5: Run all tests**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: enforce cumulative token budget between rounds"
```

---

### Task 12: Test + impl — backend `MAX_TOKENS` stop reason terminates with `LoopStopReason.MAX_TOKENS`

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`
- Modify: `src/gilbert/core/agent_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_backend_max_tokens_terminates_with_max_tokens() -> None:
    backend = FakeAIBackend(
        scripts=[
            [_msg_complete(text="cut off here", stop_reason=StopReason.MAX_TOKENS)]
        ]
    )

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.MAX_TOKENS
    assert result.rounds_used == 1
    assert result.final_message.content == "cut off here"
```

- [ ] **Step 2: Run the test — expect FAIL**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_backend_max_tokens_terminates_with_max_tokens -v
```

Expected: FAIL — currently MAX_TOKENS falls through and ends as MAX_ROUNDS (or hits the script-empty assertion if it tries another round).

- [ ] **Step 3: Map backend `MAX_TOKENS` to loop `MAX_TOKENS`**

In `src/gilbert/core/agent_loop.py`, after the existing `END_TURN` early return and before the tool-call branch, add:

```python
        if response.stop_reason == StopReason.MAX_TOKENS:
            return LoopResult(
                final_message=final_message,
                full_message_history=history,
                stop_reason=LoopStopReason.MAX_TOKENS,
                rounds_used=rounds_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_backend_max_tokens_terminates_with_max_tokens -v
```

Expected: PASS.

- [ ] **Step 5: Run all tests**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git commit -m "agent_loop: surface backend MAX_TOKENS as LoopStopReason.MAX_TOKENS"
```

---

### Task 13: Test — missing tool name yields error tool result, not crash

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`

- [ ] **Step 1: Write the test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_unknown_tool_name_becomes_error_tool_result() -> None:
    round0 = [
        _msg_complete(
            tool_calls=[
                ToolCall(tool_call_id="t1", tool_name="ghost", arguments={})
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    ]
    round1 = [_msg_complete(text="ok")]
    backend = FakeAIBackend(scripts=[round0, round1])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={},  # no tools registered, but the agent calls "ghost"
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.END_TURN
    tr = result.full_message_history[2].tool_results[0]
    assert tr.is_error is True
    assert "ghost" in tr.content
    assert "tool not found" in tr.content
```

- [ ] **Step 2: Run the test**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_unknown_tool_name_becomes_error_tool_result -v
```

Expected: PASS without code changes (`_invoke_one_tool` already handles missing tool).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_agent_loop.py
git commit -m "test(agent_loop): cover unknown-tool-name error path"
```

---

### Task 14: Test — backend stream that ends without `MESSAGE_COMPLETE` returns ERROR

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`

- [ ] **Step 1: Write the test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_stream_ends_without_message_complete_returns_error() -> None:
    # Only a TEXT_DELTA, no MESSAGE_COMPLETE — backend bug.
    bad_round = [
        StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial"),
    ]
    backend = FakeAIBackend(scripts=[bad_round])

    result = await run_loop(
        backend=backend,
        system_prompt="x",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={},
        max_rounds=10,
    )

    assert result.stop_reason == LoopStopReason.ERROR
    assert isinstance(result.error, RuntimeError)
    assert "MESSAGE_COMPLETE" in str(result.error)
```

- [ ] **Step 2: Run the test**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_stream_ends_without_message_complete_returns_error -v
```

Expected: PASS (existing impl already handles `response is None`).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_agent_loop.py
git commit -m "test(agent_loop): cover missing MESSAGE_COMPLETE error path"
```

---

### Task 15: Test — tools dict is correctly serialized into AIRequest

**Files:**
- Modify: `tests/unit/core/test_agent_loop.py`

- [ ] **Step 1: Write the test**

Append to `tests/unit/core/test_agent_loop.py`:

```python


async def test_tool_definitions_are_passed_to_backend_request() -> None:
    tool_def = ToolDefinition(name="echo", description="echo", parameters=[])

    async def handler(args: dict[str, Any]) -> str:
        return "x"

    backend = FakeAIBackend(scripts=[[_msg_complete(text="done")]])

    await run_loop(
        backend=backend,
        system_prompt="sp",
        messages=[Message(role=MessageRole.USER, content="go")],
        tools={"echo": (tool_def, handler)},
        max_rounds=10,
        model="some-model",
    )

    assert len(backend.requests_seen) == 1
    req = backend.requests_seen[0]
    assert req.system_prompt == "sp"
    assert req.model == "some-model"
    assert len(req.tools) == 1
    assert req.tools[0].name == "echo"
```

- [ ] **Step 2: Run the test**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py::test_tool_definitions_are_passed_to_backend_request -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_agent_loop.py
git commit -m "test(agent_loop): cover tool defs and model passed to backend"
```

---

### Task 16: Final pass — full suite + mypy + ruff

**Files:** All `agent_loop` files.

- [ ] **Step 1: Run the full agent_loop test suite**

Run:
```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

Expected: All tests pass (12 tests total).

- [ ] **Step 2: Run mypy on the new module**

Run:
```bash
uv run mypy src/gilbert/core/agent_loop.py
```

Expected: 0 errors. If mypy reports errors, fix them inline (likely missing `from __future__ import annotations` already in place; common issues are dataclass default factories or imports). Re-run until clean.

- [ ] **Step 3: Run ruff format and check on the new files**

Run:
```bash
uv run ruff format src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
uv run ruff check src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
```

Expected: format produces a clean diff (or no changes); check reports no errors. If formatter changed lines, re-run the full test suite to confirm nothing regressed:

```bash
uv run pytest tests/unit/core/test_agent_loop.py -v
```

- [ ] **Step 4: Run the full repo test suite to confirm no broader regression**

Run:
```bash
uv run pytest -q
```

Expected: existing tests still pass; only the new tests are added. (If any unrelated test fails, that's a pre-existing flake — note in the commit message but don't try to fix as part of this plan.)

- [ ] **Step 5: Commit any formatting changes (if ruff modified files)**

```bash
git add src/gilbert/core/agent_loop.py tests/unit/core/test_agent_loop.py
git diff --cached --quiet || git commit -m "agent_loop: ruff formatting pass"
```

(The `git diff --cached --quiet` short-circuits the commit if nothing was staged.)

---

### Task 17: Update agent memory + spec cross-reference

**Files:**
- Modify: `.claude/memory/MEMORIES.md` and a new `.claude/memory/memory-agent-loop.md`

- [ ] **Step 1: Create the agent-loop memory**

Write `.claude/memory/memory-agent-loop.md`:

```markdown
# `core/agent_loop.run_loop`

## Summary
Pure async primitive that drives one AI tool-use loop. Used (eventually) by
both `AIService.chat()` and `AutonomousAgentService.run_goal()`. Lives in
`src/gilbert/core/agent_loop.py`.

## Details
Signature is keyword-only — `backend`, `system_prompt`, `messages`, `tools`
(`dict[str, tuple[ToolDefinition, ToolHandler]]`), `max_rounds`, optional
`max_wall_clock_s`, `max_tokens`, `model`. Returns `LoopResult` with the
final `Message`, full message history, `LoopStopReason`, round/token
counters, and an optional `error`.

Loop body: build `AIRequest`, call `backend.generate_stream()`, find
`MESSAGE_COMPLETE`, append assistant message, then:
- `END_TURN` → return `LoopStopReason.END_TURN`.
- `MAX_TOKENS` (backend-side) → return `LoopStopReason.MAX_TOKENS`. The
  primitive does NOT implement chat-style continuation; callers wrap if
  they need it.
- `TOOL_USE` → execute tools (parallel via `asyncio.gather` if
  `backend.capabilities().parallel_tool_calls` and >1 call), append a
  `TOOL_RESULT` message, continue.
- Anything else → break, fall through to MAX_ROUNDS.

Pre-iteration checks at the top of every round, in order: wall-clock
deadline, cumulative token budget. Tool-handler exceptions are caught and
formatted as error `ToolResult`s (`is_error=True`, `content="tool failed:
<repr>"`); the loop continues so the model can decide to recover.
Backend exceptions during the stream are caught and returned as
`LoopStopReason.ERROR` with the exception in `LoopResult.error`.

The loop is service-free — no event bus, no scheduler, no storage. Streaming
text deltas to UI clients, conversation persistence, and per-round usage
recording belong to the caller. The chat refactor in Phase 2 will keep
those concerns inside `AIService.chat()` and let `run_loop` stay pure.

## Related
- `src/gilbert/core/agent_loop.py`
- `tests/unit/core/test_agent_loop.py`
- `docs/superpowers/specs/2026-05-03-autonomous-agent-design.md`
- `docs/superpowers/plans/2026-05-03-autonomous-agent-phase-1-run-loop.md`
```

- [ ] **Step 2: Add the memory to the index**

Open `.claude/memory/MEMORIES.md` and append (preserving existing entries):

```markdown
- [run_loop primitive](memory-agent-loop.md) — pure async loop primitive in `core/agent_loop.py`; budgets, tool dispatch, error handling
```

- [ ] **Step 3: Commit**

```bash
git add .claude/memory/MEMORIES.md .claude/memory/memory-agent-loop.md
git commit -m "memory: index run_loop primitive"
```

---

## Phase 1 Complete

At this point:
- `src/gilbert/core/agent_loop.py` exists, is tested (12 tests), mypy-clean, ruff-clean.
- `docs/superpowers/specs/2026-05-03-autonomous-agent-verification.md` records all seven verification findings.
- `.claude/memory/memory-agent-loop.md` is indexed.
- The repo's existing test suite still passes.

Phase 2 (chat refactor) gets its own plan, written after this one ships. That plan will reference the verification doc for the `chat.conversation.archiving` flow and will introduce the byte-identical replay test against the new `agent_loop`-backed `chat()` implementation.

---

## Self-Review Notes

Spec coverage check (Phase 1 only):
- [x] `run_loop` primitive signature matches spec §"The `run_loop` Primitive"
- [x] `LoopResult` shape matches spec
- [x] `LoopStopReason` enum covers END_TURN | MAX_ROUNDS | WALL_CLOCK | TOKEN_BUDGET | ERROR — plus an additional MAX_TOKENS case to cleanly map the backend's `StopReason.MAX_TOKENS` (the spec didn't enumerate this; documented in Task 1's docstring).
- [x] Budgets enforced between rounds, never mid-tool-call (spec §Run Lifecycle/Budget enforcement).
- [x] Parallel tool dispatch when backend supports it (spec §"The `run_loop` Primitive").
- [x] Tool exceptions become error tool results, loop continues (spec §"Error Handling").

Verification coverage:
- [x] All seven items from spec §"Open Verification Items" addressed in Tasks 0.1–0.7.

Placeholder scan: no TBD/TODO in plan tasks; pre-flight templates use TBD as placeholders inside the *findings doc* (those get filled in during the task), not in the plan steps themselves.

Type consistency: `ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]` is referenced consistently across Task 1 (definition), Task 5 (handler), and Task 17 (memory).
