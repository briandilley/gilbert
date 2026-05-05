# Agent Messaging — Phase 3: Mid-Stream Interrupt Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Per-priority signal delivery. `agent_send_message` and `agent_delegate` accept an optional `priority="urgent"` argument. Urgent signals interrupt the recipient at the next safe boundary — between rounds (already exists, becomes lower-latency) OR between tool-call groups within a round (new). Non-urgent stays queue mode (Phase 2 behavior). When no urgent signal arrives, the chat path is bit-identical to before.

**Architecture:** `AIService.chat` gains a single new optional kwarg, `mid_round_interrupt: Callable[[], bool] | None = None`. It's passed through to `_execute_tool_calls`, which checks it BETWEEN groups of tool calls. If the callback returns `True`, the remaining groups are skipped, stub `ToolResult` rows are produced for unexecuted calls (so the model history stays coherent), and `_execute_tool_calls` returns early. The round loop then proceeds to its existing `between_rounds_callback` step, which already drains the inbox (Phase 2 wiring). `AgentService` provides the callback and tracks `_urgent_pending: dict[agent_id, bool]`, set by `_signal_agent` whenever it persists a signal with `priority="urgent"`. The flag is cleared at drain time.

**Tech Stack:** Same as prior phases — Python 3.12+, `uv run`, pytest, no new deps.

**Out of scope:**
- Mid-token-streaming cancellation. The interrupt boundary is between tool calls, not inside a single LLM API call.
- Cancelling parallel-batch tool calls already in flight — interrupt only fires AFTER the current group completes.
- Frontend visual indicator for urgent signals (a follow-up; SPA can read `InboxSignal.priority` from existing rows).
- Cross-user — Phase 6.

---

## File Structure

**Modify:**
- `src/gilbert/core/services/ai.py` — add `mid_round_interrupt` kwarg to `chat()`; thread through to `_execute_tool_calls`; check between groups.
- `src/gilbert/core/services/agent.py`:
  - `_urgent_pending: dict[str, bool]` set in `_signal_agent` when `priority == "urgent"`; cleared in `_drain_inbox`.
  - `_run_agent_internal` passes `mid_round_interrupt=lambda: self._urgent_pending.get(a.id, False)` to `AIService.chat`.
  - `agent_send_message` and `agent_delegate` ToolDefinitions gain an optional `priority` parameter (default `"normal"`); handlers forward it to `_signal_agent`.
- `tests/unit/test_agent_peer_messaging.py` — extend with priority-based tests.

---

## Tasks

### Task 1: `AIService.chat` interrupt point

**Files:**
- Modify: `src/gilbert/core/services/ai.py`.

- [ ] **Step 1: Plumb `mid_round_interrupt` through.**

`chat()` already takes `between_rounds_callback`. Add a sibling kwarg:

```python
mid_round_interrupt: Callable[[], bool] | None = None,
```

Pass it into `_execute_tool_calls` at the call site (around line 2386).

- [ ] **Step 2: Check between groups in `_execute_tool_calls`.**

The function iterates `for group in groups`. Before each iteration (after the first), check the callback. If it returns `True`:
1. For every remaining `ToolCall` in *unprocessed* groups, append a stub `ToolResult(tool_call_id=tc.tool_call_id, content="(skipped due to urgent interrupt)", is_error=False)` to `results`. The order must match `tool_calls` so the assistant's tool_calls list aligns.
2. Break out of the loop.
3. Return `(results, ui_blocks)` as normal — caller can't tell from the signature.

```python
for group_idx, group in enumerate(groups):
    if (
        group_idx > 0
        and mid_round_interrupt is not None
        and mid_round_interrupt()
    ):
        # Synthesize stub results for the un-run remainder so the
        # assistant's tool_calls / tool_results count matches.
        for remaining_group in groups[group_idx:]:
            for tc in remaining_group:
                results.append(ToolResult(
                    tool_call_id=tc.tool_call_id,
                    content="(skipped — urgent interrupt; the message is in the next round's inbox)",
                    is_error=False,
                ))
        break
    # ... existing per-group logic ...
```

(Confirm the actual `for group in groups` loop at line 3217 and graft the check in. Use enumerate.)

- [ ] **Step 3: Tests.**

In `tests/unit/test_ai_service.py` (or a new sibling file if the test file is huge), add:
- `test_mid_round_interrupt_skips_remaining_groups` — set up a chat call where the AI emits 3 sequential tool calls. The first call's handler sets a flag. `mid_round_interrupt` checks the flag. Assert tools 2 and 3 produce stub results.
- `test_no_interrupt_when_callback_absent` — verify behavior is unchanged when `mid_round_interrupt=None`.
- `test_no_interrupt_when_callback_returns_false` — same; callback returns `False`; everything runs normally.

Actually let me reread the existing test fixtures — the test file may be very large. If too disruptive, add a small new file `tests/unit/test_ai_service_interrupt.py`.

`uv run pytest tests/unit/test_ai_service*.py -x`

Commit message: `ai: mid_round_interrupt callback for AIService.chat`

---

### Task 2: AgentService priority + interrupt wiring

**Files:**
- Modify: `src/gilbert/core/services/agent.py`.
- Modify: `tests/unit/test_agent_peer_messaging.py`.

- [ ] **Step 1: `_urgent_pending` flag.**

In `__init__`:
```python
self._urgent_pending: dict[str, bool] = {}
```

In `_signal_agent`, after persisting:
```python
if priority == "urgent":
    self._urgent_pending[agent_id] = True
```

In `_drain_inbox`, after popping signals:
```python
self._urgent_pending.pop(agent_id, None)
```

(Clear unconditionally on drain — even if no urgent signal was in the drained batch, the flag is stale.)

- [ ] **Step 2: Pass `mid_round_interrupt` to `AIService.chat`.**

In `_run_agent_internal`:
```python
def _interrupt_check() -> bool:
    return self._urgent_pending.get(a.id, False)

result = await self._ai.chat(
    user_message=user_msg,
    conversation_id=a.conversation_id or None,
    user_ctx=user_ctx,
    system_prompt=system_prompt,
    ai_call=_AI_CALL_NAME,
    ai_profile=a.profile_id,
    between_rounds_callback=_between_rounds,
    mid_round_interrupt=_interrupt_check,
)
```

- [ ] **Step 3: `priority` param on `agent_send_message` and `agent_delegate`.**

Add a parameter to each ToolDefinition:

```python
ToolParameter(
    name="priority",
    type=ToolParameterType.STRING,
    description='"urgent" interrupts the recipient between tool calls; "normal" (default) waits for round boundaries.',
    required=False,
),
```

Handlers parse `priority = str(args.get("priority", "normal")).lower().strip() or "normal"`. Validate it's one of `{"urgent", "normal"}` else return error string. Pass to `_signal_agent(..., priority=priority)`.

For `agent_delegate`, default delegations to `priority="urgent"` — delegations are by definition synchronous and the caller is awaiting; the recipient should drop everything. Actually, re-reading the spec: "Delegation specifics: ... target idle → fire, busy → enqueue. Caller's tool returns when target's loop ends and produces END_TURN." The delegation signal kind is already `"delegation"`, distinct from `"inbox"`. For Phase 3, just apply `priority="urgent"` by default to delegation signals (so a busy target gets interrupted and processes the delegation). Mention in the tool description: "delegations default to urgent priority".

`agent_send_message` defaults to `priority="normal"`. Caller can override.

- [ ] **Step 4: Tests.**

In `tests/unit/test_agent_peer_messaging.py`:
- `test_agent_send_message_urgent_sets_pending_flag` — A1 sends to A2 with priority="urgent"; assert `svc._urgent_pending[A2.id] is True` after the call.
- `test_agent_send_message_normal_does_not_set_pending` — flag stays absent.
- `test_agent_delegate_defaults_to_urgent` — delegate without explicit priority; `_urgent_pending` is set on the target.
- `test_drain_inbox_clears_urgent_pending` — pre-set flag + signals; call `_drain_inbox`; flag is cleared.

Commit message: `agents: priority on send_message + delegate; wire mid_round_interrupt`

---

### Task 3: End-to-end interrupt test

**Files:**
- Modify: `tests/unit/test_agent_peer_messaging.py`.

- [ ] **Step 1: End-to-end test.**

A1's run is mid-round, executing 3 sequential tool calls. A2 sends an urgent message during call 1's execution. The remaining 2 tool calls produce stub results, the round ends, the inbox drain picks up the urgent signal, the next round sees it. The acceptance criterion: total tool-result count matches tool-call count, the message is visible to A1 in the next round.

This needs the `_FakeAIProvider` to be smart enough to:
- Emit a multi-tool-call response on round 0.
- Run a side-effect during call 1 that simulates the urgent send.
- Return END_TURN on round 1 with text confirming the urgent message was visible.

If the fake is too inflexible, fall back to a unit-level test that exercises `_execute_tool_calls` directly with a controllable interrupt callback.

`uv run pytest tests/unit/test_agent_peer_messaging.py::test_urgent_interrupts_mid_round -x`

Commit message: `agents: end-to-end urgent-interrupts-mid-round test`

---

### Task 4: Verification

- [ ] `uv run pytest -x`
- [ ] `uv run ruff check src/gilbert/core/services/ai.py src/gilbert/core/services/agent.py tests/unit/test_agent_peer_messaging.py`
- [ ] `uv run mypy src/gilbert/core/services/ai.py src/gilbert/core/services/agent.py`
- [ ] tsc clean (no frontend changes expected, but verify).

Commit message: any cleanup commits.

---

### Task 5: Memory file update

**Files:**
- Modify: `.claude/memory/memory-agent-service.md`.

Append a "Phase 3 — Mid-stream interrupt" subsection covering:
- `priority` arg on `agent_send_message` / `agent_delegate`; delegations default urgent.
- `_urgent_pending` flag flow.
- `mid_round_interrupt` kwarg on `AIService.chat`; check between tool-call groups; remaining calls get stub results.
- Behavior unchanged when `mid_round_interrupt=None` or returns False.

---

## Test Strategy

| Category | Coverage |
|---|---|
| Unit — AIService | `mid_round_interrupt` skips remaining groups; absent / False callback unchanged. |
| Unit — AgentService | `priority` param parsing; flag set in `_signal_agent`; flag cleared in `_drain_inbox`. |
| Integration | End-to-end: urgent peer DM during multi-tool round → remaining tools stubbed → next round sees the message. |
| Backwards-compat | Existing chat tests pass unchanged (no implicit interrupt unless callback wired). |

---

## Open Questions / Future

- **Cooperative cancel of in-flight parallel batch tools.** Phase 3 lets the current group finish. A future phase could send `asyncio.CancelledError` to in-flight tasks, but tools are not necessarily cancel-safe.
- **Mid-stream LLM cancellation.** Out of scope; would require streaming-API support per backend.
- **Frontend "urgent" badge** in MemoryBrowser / Runs trigger filter.
