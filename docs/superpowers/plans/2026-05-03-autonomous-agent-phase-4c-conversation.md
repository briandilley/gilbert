# Autonomous Agent — Phase 4c: Materialized Conversation per Goal Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Move from "fresh conversation per run" to "one conversation per goal — runs append to it." This gives users one chat thread per goal where they can scroll the entire history, see live runs streaming via the existing chat WS infrastructure, and (in a future phase) chat into the goal between runs. Lazy-create the conversation on the first run, store its id on `Goal.conversation_id`, reuse it forever after.

**Out of scope for this plan:** notes scratchpad, auto-digest summarizer, user-input-into-goal consumption (those are future Phase 4c+ work). This plan only handles the conversation materialization itself.

---

## Tasks

### Task 1: Add `conversation_id` field to Goal entity

**Files:**
- Modify: `src/gilbert/interfaces/agent.py`
- Modify: `src/gilbert/core/services/agent.py`

- [ ] **Step 1: Add field to `Goal` dataclass**

In `src/gilbert/interfaces/agent.py`, add a new field to `Goal` (after `trigger_config`):

```python
    conversation_id: str = ""
    """Per-goal materialized chat conversation. Lazy-created on the
    first run; subsequent runs append to it. Empty string before the
    first run."""
```

- [ ] **Step 2: Update serialization**

In `src/gilbert/core/services/agent.py`, update `_goal_to_dict` and `_goal_from_dict` to include the new field. In `_goal_to_dict`:

```python
        "conversation_id": g.conversation_id,
```

In `_goal_from_dict`:

```python
        conversation_id=d.get("conversation_id", ""),
```

- [ ] **Step 3: Run tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 30 passed (still — additive field with default).

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/interfaces/agent.py src/gilbert/core/services/agent.py
git commit -m "agent: add conversation_id field to Goal for materialized conversations"
```

---

### Task 2: Use Goal.conversation_id when running, capture from first run

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing tests**

```python


# ── Materialized conversation tests ───────────────────────────────


async def test_first_run_captures_new_conversation_id_on_goal(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )
    assert g.conversation_id == ""

    # First run: chat() returns conversation_id "conv-fake"
    await svc.run_goal_now(g.id)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.conversation_id == "conv-fake"


async def test_subsequent_runs_reuse_goal_conversation_id(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, ai, _bus, _scheduler = service
    g = await svc.create_goal(
        owner_user_id="u_alice", name="x", instruction="i", profile_id="default"
    )

    await svc.run_goal_now(g.id)
    # First call passed conversation_id=None
    assert ai.calls[-1]["conversation_id"] is None

    await svc.run_goal_now(g.id)
    # Second call passes the captured conversation_id
    assert ai.calls[-1]["conversation_id"] == "conv-fake"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py::test_first_run_captures_new_conversation_id_on_goal tests/unit/core/test_agent_service.py::test_subsequent_runs_reuse_goal_conversation_id -v
```

- [ ] **Step 3: Update `_run_goal_internal` to use goal.conversation_id**

Find the `_run_goal_internal` method's `chat()` call. Replace:

```python
            result = await self._ai.chat(
                user_message=user_message,
                conversation_id=None,
                user_ctx=None,
                ai_call=_AI_CALL_NAME,
                ai_profile=goal.profile_id,
            )
```

with:

```python
            # Pass the goal's conversation_id (or None if it's the first run
            # — chat() will create one and we capture it onto the goal below).
            existing_conv = goal.conversation_id or None
            result = await self._ai.chat(
                user_message=user_message,
                conversation_id=existing_conv,
                user_ctx=None,
                ai_call=_AI_CALL_NAME,
                ai_profile=goal.profile_id,
            )
```

Then, in the same method's success path (where the run result is captured), add the conversation_id capture:

```python
            run.status = RunStatus.COMPLETED
            run.final_message_text = result.response_text
            run.conversation_id = result.conversation_id
            if result.turn_usage:
                run.tokens_in = int(result.turn_usage.get("input_tokens", 0))
                run.tokens_out = int(result.turn_usage.get("output_tokens", 0))
            run.rounds_used = len(result.rounds) + 1
            # Capture the conversation_id on the goal if this was the first run
            if not goal.conversation_id and result.conversation_id:
                goal.conversation_id = result.conversation_id
```

The goal save at the end of the method already persists this update.

- [ ] **Step 4: Run tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 32 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: materialize conversation per goal — runs append to a single conversation"
```

---

### Task 3: WS goal create response includes conversation_id

**Files:** Already covered — `_goal_to_dict` includes the field, so `_ws_goal_create.result.goal.conversation_id` is included automatically. No change needed.

Skip this task; verify by reading existing tests.

---

### Task 4: Update memory + quality gate

**Files:**
- Modify: `.claude/memory/memory-autonomous-agent-service.md`

- [ ] **Step 1: Update memory file**

In `.claude/memory/memory-autonomous-agent-service.md`, change the "**Cross-run memory & materialized conversations:**" paragraph from "v1 does not implement..." to:

```markdown
**Materialized conversation per goal:** ``Goal.conversation_id`` is
lazy-created on the first run by ``AIService.chat()`` (called with
``conversation_id=None``); the returned id is captured on the goal and
reused for every subsequent run. The Activity tab in the UI is just a
view of this conversation. Cross-run notes and auto-digest summarization
are still future work.
```

- [ ] **Step 2: mypy + ruff**

```bash
cd /home/assistant/gilbert && uv run mypy src/gilbert/core/services/agent.py
cd /home/assistant/gilbert && uv run ruff format src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
cd /home/assistant/gilbert && uv run ruff check src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
```

Fix any issues.

- [ ] **Step 3: Final test run**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
cd /home/assistant/gilbert && uv run pytest -q 2>&1 | tail -5
```

Expected: 32 agent tests; full repo only the 2 pre-existing failures.

- [ ] **Step 4: Commit**

```bash
git add .claude/memory/memory-autonomous-agent-service.md src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git diff --cached --quiet || git commit -m "agent: update memory for materialized conversation; ruff pass"
```

---

## Phase 4c Complete

- Each goal has one conversation across all its runs.
- Activity history visible by viewing `Goal.conversation_id` in the chat UI.
- Notes + digest deferred (future work).
- 32 agent tests passing.
