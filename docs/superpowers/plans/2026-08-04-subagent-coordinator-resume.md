# Subagent Coordinator Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a background subagent finishes, resume the coordinator's chat turn on the parent conversation — but only when the coordinator declared follow-up work — so multi-step tasks ("research, then build a playlist, then play it") no longer stall.

**Architecture:** `spawn_agent` gains two optional params (`on_complete`, `join_group`). On completion (or failure), `SubagentService` drives a fresh coordinator turn via a new `AIProvider.resume_turn(...)` method, seeded with the declared follow-up plus a pointer to the report. A per-conversation turn lock in `AIService` serializes the resume against any live user turn. Join cohorts (`join_group`) resume once, after all members settle, with membership frozen by synchronous registration at spawn time and sealed on the coordinator turn's `chat.stream.turn_complete`.

**Tech Stack:** Python 3.12, uv, pytest with `unittest.mock.AsyncMock`, asyncio, the in-house EventBus (`gilbert.interfaces.events`).

## Global Constraints

- Always use `uv run pytest ...` — never bare `pytest`. Never run `uv sync`/`uv add`; `uv run` rewrites `uv.lock` — if it does, `git checkout uv.lock` before committing (do not stage it).
- Type hints on every new function signature (project rule).
- Layer rules: `SubagentService` (core/services) accesses the AI capability only through `interfaces/ai.py` protocols (`AIProvider`, `ConversationMessagePoster`, and the new `ConversationResumer`) — never a concrete class. It reaches the event bus via `get_capability("event_bus")` + `EventBusProvider`.
- Best-effort delivery contract: `_run_agent_background` "never raises" — new code in the completion/failure paths must swallow its own errors (log, don't propagate).
- No new AI prompt *strings* are added as hardcoded persona/system prompts here; the resume seed is a runtime instruction assembled from the coordinator's own `on_complete`, not a configurable AI prompt.

---

### Task 1: `resume_turn` on the AI provider + per-conversation turn lock

Adds the mechanism that drives one coordinator turn on an existing conversation from outside the WS flow, serialized against live user turns.

**Files:**
- Modify: `src/gilbert/interfaces/ai.py` (add `ConversationResumer` protocol near `ConversationMessagePoster` ~line 692)
- Modify: `src/gilbert/core/services/ai.py` (add `_conv_turn_locks` init ~line 1404; add `_conv_turn_lock` + `resume_turn`; wrap the personal-path `chat()` call ~line 7128)
- Test: `tests/unit/test_ai_service_resume_turn.py` (new)

**Interfaces:**
- Produces:
  - `ConversationResumer.resume_turn(conversation_id: str, user_ctx: UserContext, instruction: str, attachments: list[FileAttachment] | None = None, source: str = "subagent-resume") -> None` (protocol, `interfaces/ai.py`)
  - `AIService.resume_turn(...)` — concrete impl with the same signature
  - `AIService._conv_turn_lock(conversation_id: str) -> asyncio.Lock`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_service_resume_turn.py`. It builds an `AIService`, stubs `chat()` to record its call and return a `ChatTurnResult`, seeds a conversation row in storage, and asserts `resume_turn` calls `chat()` once with the chat profile. (Mirror the in-memory storage helper from `tests/unit/test_subagent_service.py`.)

```python
"""Tests for AIService.resume_turn — server-initiated coordinator resume."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import AIService, _COLLECTION
from gilbert.interfaces.ai import ChatTurnResult, ConversationResumer
from gilbert.interfaces.auth import UserContext


def _user() -> UserContext:
    return UserContext(user_id="u1", display_name="U", roles=("user",))


@pytest.mark.asyncio
async def test_resume_turn_drives_one_chat_turn() -> None:
    svc = AIService()
    store: dict[str, dict[str, Any]] = {f"{_COLLECTION}:c1": {"messages": []}}
    backend = AsyncMock()
    backend.get = AsyncMock(side_effect=lambda c, k: store.get(f"{c}:{k}"))
    backend.put = AsyncMock()
    svc._storage = backend
    svc._chat_profile = "standard"

    calls: list[dict[str, Any]] = []

    async def _fake_chat(user_message: str, **kw: Any) -> ChatTurnResult:
        calls.append({"user_message": user_message, **kw})
        return ChatTurnResult(
            response_text="done", conversation_id="c1", ui_blocks=[],
            tool_usage=[], attachments=[], rounds=1,
        )

    svc.chat = _fake_chat  # type: ignore[method-assign]

    assert isinstance(svc, ConversationResumer)
    await svc.resume_turn("c1", _user(), "continue the task")

    assert len(calls) == 1
    assert calls[0]["user_message"] == "continue the task"
    assert calls[0]["conversation_id"] == "c1"
    assert calls[0]["ai_profile"] == "standard"


@pytest.mark.asyncio
async def test_resume_turn_noops_when_conversation_missing() -> None:
    svc = AIService()
    backend = AsyncMock()
    backend.get = AsyncMock(return_value=None)
    svc._storage = backend
    called = False

    async def _fake_chat(*a: Any, **k: Any) -> ChatTurnResult:
        nonlocal called
        called = True
        return ChatTurnResult("", "c1", [], [], [], 0)

    svc.chat = _fake_chat  # type: ignore[method-assign]
    await svc.resume_turn("gone", _user(), "x")
    assert called is False


@pytest.mark.asyncio
async def test_conv_turn_lock_is_stable_per_conversation() -> None:
    svc = AIService()
    assert svc._conv_turn_lock("c1") is svc._conv_turn_lock("c1")
    assert svc._conv_turn_lock("c1") is not svc._conv_turn_lock("c2")
```

> Note: `ChatTurnResult` is a `NamedTuple` (`interfaces/ai.py:441`) — construct positionally as above or with field names. Verify its exact fields when writing the test and match them.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ai_service_resume_turn.py -v`
Expected: FAIL — `AttributeError: 'AIService' object has no attribute 'resume_turn'` / `_conv_turn_lock`, and `ConversationResumer` import error.

- [ ] **Step 3: Add the `ConversationResumer` protocol**

In `src/gilbert/interfaces/ai.py`, immediately after the `ConversationMessagePoster` protocol (ends ~line 733), add:

```python
@runtime_checkable
class ConversationResumer(Protocol):
    """Protocol for driving one coordinator AI turn on an existing
    conversation from outside the normal WS-initiated chat flow.

    Used to resume the coordinator after a background subagent it was
    waiting on completes: the resume turn is seeded with the coordinator's
    own declared follow-up instruction so it can act on the subagent's
    result. Implementations MUST serialize the resume against any live
    user turn on the same conversation so their writes to the stored
    ``messages`` list can't interleave.
    """

    async def resume_turn(
        self,
        conversation_id: str,
        user_ctx: UserContext,
        instruction: str,
        attachments: list[FileAttachment] | None = None,
        source: str = "subagent-resume",
    ) -> None: ...
```

Confirm `runtime_checkable`, `Protocol`, `UserContext`, and `FileAttachment` are already imported in this module (they are — used by `ConversationMessagePoster`). If `AIProvider` (Protocol, ~line 499) is what callers type against and you want `resume_turn` visible there too, leave `AIProvider` unchanged — `SubagentService` will `isinstance`-check `ConversationResumer` exactly like it already does for `ConversationMessagePoster`.

- [ ] **Step 4: Add the lock registry + `_conv_turn_lock` + `resume_turn` to `AIService`**

In `src/gilbert/core/services/ai.py`, in `__init__` next to `self._in_flight_chats` (line 1404), add:

```python
        # Per-conversation turn lock: serializes a background-driven resume
        # turn (resume_turn) against a live user turn on the same conversation
        # so their appends to the single stored messages doc can't interleave.
        self._conv_turn_locks: dict[str, asyncio.Lock] = {}
```

Then add these methods (place near `append_assistant_message`, ~line 4904):

```python
    def _conv_turn_lock(self, conversation_id: str) -> asyncio.Lock:
        """Stable per-conversation lock guarding a full turn on one
        conversation. Created on first use; entries are cheap and bounded
        by the number of active conversations."""
        lock = self._conv_turn_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_turn_locks[conversation_id] = lock
        return lock

    async def resume_turn(
        self,
        conversation_id: str,
        user_ctx: UserContext,
        instruction: str,
        attachments: list[FileAttachment] | None = None,
        source: str = "subagent-resume",
    ) -> None:
        """Drive one coordinator turn on an existing conversation (see
        ConversationResumer). Serialized per-conversation against live user
        turns via the turn lock. Best-effort: no-ops if the conversation is
        gone (user deleted it mid-run), consistent with append_assistant_message.
        """
        if self._storage is None or not conversation_id:
            return
        try:
            if await self._storage.get(_COLLECTION, conversation_id) is None:
                return
        except Exception:
            logger.debug("resume_turn: storage.get failed for %s", conversation_id, exc_info=True)
            return
        async with self._conv_turn_lock(conversation_id):
            result = await self.chat(
                user_message=instruction,
                conversation_id=conversation_id,
                user_ctx=user_ctx,
                ai_profile=self._chat_profile,
                attachments=attachments,
                source=source,
            )
        # Mirror the WS path so an open SPA renders the resumed reply even if
        # it wasn't actively streaming when the turn started.
        if result.response_text:
            await self._publish_event(
                "chat.message.created",
                {
                    "conversation_id": result.conversation_id,
                    "author_id": "gilbert",
                    "author_name": "Gilbert",
                    "content": result.response_text,
                    "user_message": instruction,
                    "attachments": _serialize_attachments_for_wire(result.attachments),
                    "user_attachments": [],
                    "ui_blocks": result.ui_blocks,
                    "mentioned_user_ids": [],
                },
            )
```

Confirm `_serialize_attachments_for_wire` and `_COLLECTION` are module-level in `ai.py` (they are — used by `append_assistant_message`). `logger` exists module-level.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ai_service_resume_turn.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Serialize the personal user-turn path against resume_turn**

In `src/gilbert/core/services/ai.py`, the personal-chat branch (`else:` at ~line 7127) calls `self.chat(...)`. Wrap that single call in the per-conversation lock so a user turn and a resume turn can't run concurrently on the same conversation. At the top of the function add the import if not present:

```python
from contextlib import nullcontext
```

Change the personal branch from:

```python
            else:
                # Personal chat — normal AI flow
                turn_result = await self.chat(
                    user_message=message,
                    conversation_id=conversation_id,
                    user_ctx=conn.user_ctx,
                    ai_profile=self._chat_profile,
                    attachments=attachments,
                    model=frame_model,
                    backend_override=frame_backend,
                )
```

to:

```python
            else:
                # Personal chat — normal AI flow. Serialize against any
                # background-driven resume turn on the same conversation
                # (resume_turn) so their messages-doc writes can't interleave.
                # nullcontext for a brand-new conversation (no id yet → nothing
                # to collide with).
                turn_lock = (
                    self._conv_turn_lock(conversation_id)
                    if conversation_id
                    else nullcontext()
                )
                async with turn_lock:
                    turn_result = await self.chat(
                        user_message=message,
                        conversation_id=conversation_id,
                        user_ctx=conn.user_ctx,
                        ai_profile=self._chat_profile,
                        attachments=attachments,
                        model=frame_model,
                        backend_override=frame_backend,
                    )
```

(`contextlib.nullcontext` supports `async with` on Python 3.10+, so mixing it with the `asyncio.Lock` branch is fine.)

- [ ] **Step 7: Run the AI test suite to confirm no regression**

Run: `uv run pytest tests/unit/test_ai_service_resume_turn.py tests/unit/test_ai_service.py -q`
Expected: PASS. If `uv run` rewrote `uv.lock`, run `git checkout uv.lock`.

- [ ] **Step 8: Commit**

```bash
git add src/gilbert/interfaces/ai.py src/gilbert/core/services/ai.py tests/unit/test_ai_service_resume_turn.py
git commit -m "feat(ai): add resume_turn + per-conversation turn lock for server-initiated coordinator turns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Thread `on_complete` / `join_group` through `spawn_agent`

Plumbing only: expose the two params, carry them into the background path, store them on `_Run`. No resume behavior yet.

**Files:**
- Modify: `src/gilbert/core/services/subagent.py` (`_Run` dataclass ~line 60; `get_tools` `spawn_agent` params ~line 277; `execute_tool` background branch ~line 461; `_run_agent_background` signature ~line 560)
- Test: `tests/unit/test_subagent_service.py` (add cases)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `_Run` gains `on_complete: str = ""` and `join_group: str = ""`.
  - `_run_agent_background(..., on_complete: str = "", join_group: str = "", subagent_id: str | None = None)` — accepts a pre-generated `subagent_id` (needed by Task 4 synchronous registration).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_subagent_service.py`:

```python
def test_spawn_agent_exposes_on_complete_and_join_group() -> None:
    svc = SubagentService()
    tools = svc.get_tools()
    spawn = next(t for t in tools if t.name == "spawn_agent")
    names = {p.name for p in spawn.parameters}
    assert "on_complete" in names
    assert "join_group" in names
    for pname in ("on_complete", "join_group"):
        p = next(p for p in spawn.parameters if p.name == pname)
        assert p.required is False
        assert p.type == ToolParameterType.STRING


@pytest.mark.asyncio
async def test_run_agent_background_stores_intent_on_run(monkeypatch: Any) -> None:
    svc = SubagentService()
    svc._enabled = True
    svc._ai = _FakeAI("report body")

    async def _fake_spawn(*a: Any, **k: Any) -> str:
        return "report body"

    monkeypatch.setattr(svc, "spawn", _fake_spawn)

    await svc._run_agent_background(
        t=next(iter(svc._types.values())),
        query="q",
        parent_conversation_id="parent1",
        user_ctx=UserContext(user_id="u1", display_name="U", roles=("user",)),
        on_complete="build the playlist",
        join_group="cohort-a",
        subagent_id="sa1",
    )
    run = svc._runs["sa1"]
    assert run.on_complete == "build the playlist"
    assert run.join_group == "cohort-a"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "on_complete or intent_on_run" -v`
Expected: FAIL — params absent / `_run_agent_background` has no `on_complete` kwarg.

- [ ] **Step 3: Add fields to `_Run`**

In `src/gilbert/core/services/subagent.py`, extend the `_Run` dataclass (after `started_at`, before `stop_flag`):

```python
    on_complete: str = ""
    join_group: str = ""
```

- [ ] **Step 4: Add the two tool params**

In `get_tools`, inside the `spawn_agent` `parameters=[...]` list, after the existing `model` param, add:

```python
                    ToolParameter(
                        name="on_complete",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional. A follow-up instruction to run YOURSELF "
                            "once this agent finishes — use it when this spawn is "
                            "one step of a larger task (e.g. 'using the report, "
                            "create a playlist of the tracks, queue it, and play "
                            "it'). Leave empty for a standalone report that needs "
                            "no follow-up. When set, you are automatically resumed "
                            "with the report when the agent completes (or fails)."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="join_group",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional. To wait on a COHORT of agents before your "
                            "follow-up runs, give every spawn in the cohort the "
                            "same join_group id and put the combined follow-up in "
                            "on_complete. You are resumed once, after ALL members "
                            "finish, with all their reports. Omit for per-agent "
                            "resume."
                        ),
                        required=False,
                    ),
```

- [ ] **Step 5: Thread through `execute_tool` and `_run_agent_background`**

In `execute_tool`, the `spawn_agent` block, read the two args and pass them + a pre-generated `subagent_id` into the background path. Replace the `if t.execution_mode == "background":` block (~line 461) with:

```python
            on_complete = str(arguments.get("on_complete") or "")
            join_group = str(arguments.get("join_group") or "")
            if t.execution_mode == "background":
                parent_conv = get_current_conversation_id()
                subagent_id = uuid.uuid4().hex
                self._run_in_background(
                    self._run_agent_background(
                        t, prompt, parent_conv, caller, model,
                        on_complete=on_complete,
                        join_group=join_group,
                        subagent_id=subagent_id,
                    )
                )
                return (
                    f"\U0001f50d Running {t.name} on \"{prompt}\" in the background "
                    "— I'll post the report here when it's ready. You can keep "
                    "chatting."
                )
```

Update `_run_agent_background`'s signature and the `subagent_id`/`_Run` construction. Change the signature (~line 560) to:

```python
    async def _run_agent_background(
        self,
        t: SubagentType,
        query: str,
        parent_conversation_id: str | None,
        user_ctx: UserContext | None,
        model_override: str = "",
        *,
        on_complete: str = "",
        join_group: str = "",
        subagent_id: str | None = None,
    ) -> None:
```

Inside, replace `subagent_id = uuid.uuid4().hex` (~line 580) with:

```python
        subagent_id = subagent_id or uuid.uuid4().hex
```

and add `on_complete=on_complete, join_group=join_group,` to the `_Run(...)` constructor call (~line 583).

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "on_complete or intent_on_run" -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "feat(subagent): thread on_complete/join_group intent through spawn_agent

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Solo resume on completion and failure

Wire the actual resume for the non-join case: after delivering the report (or on failure), if `on_complete` is set and there's no `join_group`, drive the coordinator via `resume_turn`.

**Files:**
- Modify: `src/gilbert/core/services/subagent.py` (import `ConversationResumer`; add `_build_solo_seed` + `_finalize_run`; call `_finalize_run` from the success and failure paths of `_run_agent_background`)
- Test: `tests/unit/test_subagent_service.py`

**Interfaces:**
- Consumes: `ConversationResumer.resume_turn(...)` from Task 1.
- Produces:
  - `SubagentService._finalize_run(run, t, user_ctx, parent_conversation_id, *, report, rel_path, failure) -> None`
  - `SubagentService._build_solo_seed(on_complete, agent_name, rel_path, report, failure) -> str` (staticmethod)

- [ ] **Step 1: Extend the fake AI to record resume_turn, then write failing tests**

In `tests/unit/test_subagent_service.py`, add a `resume_turn` recorder to `_FakeAI` (so it satisfies `ConversationResumer`). Add to the `_FakeAI` class:

```python
    async def resume_turn(
        self,
        conversation_id: str,
        user_ctx: Any,
        instruction: str,
        attachments: Any = None,
        source: str = "subagent-resume",
    ) -> None:
        self.resumes.append(
            {"conversation_id": conversation_id, "instruction": instruction,
             "attachments": attachments, "source": source}
        )
```

and initialize `self.resumes: list[dict[str, Any]] = []` in `_FakeAI.__init__`. `_FakeAI` also needs `append_assistant_message` + `ensure_conversation` no-ops if not already present (so `_deliver` works) — add minimal async no-ops recording delivered messages:

```python
    async def append_assistant_message(self, conversation_id: str, content: str, attachments: Any = None) -> None:
        self.delivered.append({"conversation_id": conversation_id, "content": content})

    async def ensure_conversation(self, conversation_id: str, user_ctx: Any, **kw: Any) -> None:
        return None
```

(init `self.delivered: list[dict[str, Any]] = []`.)

Then add the tests:

```python
def _svc_with_fake_ai() -> tuple[SubagentService, "_FakeAI"]:
    svc = SubagentService()
    svc._enabled = True
    fake = _FakeAI("REPORT BODY")
    svc._ai = fake
    return svc, fake


@pytest.mark.asyncio
async def test_solo_resume_fires_with_on_complete(monkeypatch: Any) -> None:
    svc, fake = _svc_with_fake_ai()

    async def _fake_spawn(*a: Any, **k: Any) -> str:
        return "REPORT BODY"

    monkeypatch.setattr(svc, "spawn", _fake_spawn)
    # Force inline delivery (no workspace) so the report body is in the seed.
    t = next(iter(svc._types.values()))
    t = dataclasses.replace(t, deliver_as="inline")

    await svc._run_agent_background(
        t, "q", "parent1",
        UserContext(user_id="u1", display_name="U", roles=("user",)),
        on_complete="build the playlist", subagent_id="sa1",
    )
    assert len(fake.resumes) == 1
    r = fake.resumes[0]
    assert r["conversation_id"] == "parent1"
    assert "build the playlist" in r["instruction"]


@pytest.mark.asyncio
async def test_no_resume_without_on_complete(monkeypatch: Any) -> None:
    svc, fake = _svc_with_fake_ai()

    async def _fake_spawn(*a: Any, **k: Any) -> str:
        return "REPORT BODY"

    monkeypatch.setattr(svc, "spawn", _fake_spawn)
    t = dataclasses.replace(next(iter(svc._types.values())), deliver_as="inline")
    await svc._run_agent_background(
        t, "q", "parent1",
        UserContext(user_id="u1", display_name="U", roles=("user",)),
        subagent_id="sa1",
    )
    assert fake.resumes == []


@pytest.mark.asyncio
async def test_solo_resume_on_failure(monkeypatch: Any) -> None:
    svc, fake = _svc_with_fake_ai()

    async def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(svc, "spawn", _boom)
    t = dataclasses.replace(next(iter(svc._types.values())), deliver_as="inline")
    await svc._run_agent_background(
        t, "q", "parent1",
        UserContext(user_id="u1", display_name="U", roles=("user",)),
        on_complete="build the playlist", subagent_id="sa1",
    )
    assert len(fake.resumes) == 1
    assert "agent exploded" in fake.resumes[0]["instruction"]
    assert "build the playlist" in fake.resumes[0]["instruction"]
```

`dataclasses` is already imported in `subagent.py`; ensure the test module imports `dataclasses` (add `import dataclasses` at top of the test file if absent).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "solo_resume or no_resume_without" -v`
Expected: FAIL — no resume fired (`_finalize_run` not wired).

- [ ] **Step 3: Implement `_build_solo_seed` + `_finalize_run` and call them**

In `subagent.py`, update the import (line 36):

```python
from gilbert.interfaces.ai import AIProvider, ConversationMessagePoster, ConversationResumer
```

Add the two helpers (place them right after `_deliver`, ~line 736):

```python
    @staticmethod
    def _build_solo_seed(
        on_complete: str,
        agent_name: str,
        rel_path: str | None,
        report: str,
        failure: str | None,
    ) -> str:
        """Assemble the resume instruction handed back to the coordinator.
        Marked ``[automated]`` because it persists as a real user-role row."""
        if failure is not None:
            return (
                f"[automated] The background agent `{agent_name}` you dispatched "
                f"FAILED: {failure}. You intended to: {on_complete}. Decide how to "
                "proceed — retry, adjust the approach, or tell the user what "
                "happened."
            )
        if rel_path:
            where = (
                f" The full report is saved at `{rel_path}` — read it with your "
                "workspace tools before acting."
            )
        else:
            where = f"\n\nReport:\n{report}"
        return (
            f"[automated] The background agent `{agent_name}` you dispatched has "
            f"finished.{where}\n\nNow: {on_complete}"
        )

    async def _finalize_run(
        self,
        run: _Run,
        t: SubagentType,
        user_ctx: UserContext | None,
        parent_conversation_id: str | None,
        *,
        report: str | None,
        rel_path: str | None,
        failure: str | None,
    ) -> None:
        """Post-delivery hook: resume the coordinator if intent was declared.
        Join-group runs are routed to the cohort machinery (Task 4); solo runs
        with an on_complete resume immediately. Best-effort — never raises into
        the detached task."""
        try:
            if run.join_group:
                await self._settle_join_member(
                    run, t, user_ctx, parent_conversation_id,
                    report=report, rel_path=rel_path, failure=failure,
                )
                return
            if not run.on_complete:
                return
            if not (parent_conversation_id and user_ctx):
                return
            if not isinstance(self._ai, ConversationResumer):
                return
            seed = self._build_solo_seed(
                run.on_complete, t.name, rel_path, report or "", failure
            )
            await self._ai.resume_turn(parent_conversation_id, user_ctx, seed)
        except Exception:
            logger.exception("subagent _finalize_run failed for %s", run.subagent_id)
```

> `_settle_join_member` is added in Task 4. To keep this task independently green, add a temporary stub now and replace it in Task 4:
> ```python
>     async def _settle_join_member(self, run: _Run, t: SubagentType, user_ctx: UserContext | None, parent_conversation_id: str | None, *, report: str | None, rel_path: str | None, failure: str | None) -> None:
>         return None  # replaced in Task 4
> ```

Now call `_finalize_run` from both paths of `_run_agent_background`.

Success path — immediately after the notification block (`await self._deliver(...)` then the notify `try/except`, ~line 676), add:

```python
            await self._finalize_run(
                run, t, user_ctx, parent_conversation_id,
                report=report, rel_path=rel_path, failure=None,
            )
```

Failure path — in the `except Exception as exc:` block, after `await self._deliver(parent_conversation_id, f"{t.name} failed: {exc}")` (~line 692), add:

```python
            await self._finalize_run(
                run, t, user_ctx, parent_conversation_id,
                report=None, rel_path=None, failure=str(exc),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "solo_resume or no_resume_without" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "feat(subagent): resume coordinator on solo background-agent completion/failure

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Join cohort — synchronous registration, seal on turn_complete, fire once

Support `join_group`: members register synchronously at spawn (freezing membership before any settles), each records its result on settle, and the cohort resumes once — after the coordinator turn seals it via `chat.stream.turn_complete` and all members have settled.

**Files:**
- Modify: `src/gilbert/core/services/subagent.py` (`_JoinMember` + `_JoinGroup` dataclasses; `_join_groups` registry in `__init__`; register in `execute_tool`; replace the `_settle_join_member` stub; `_maybe_fire_join`; `_build_join_seed`; subscribe to `chat.stream.turn_complete` in `start()`; add `stop()` to unsubscribe)
- Test: `tests/unit/test_subagent_service.py`

**Interfaces:**
- Consumes: `ConversationResumer.resume_turn(...)`; the EventBus (`EventBusProvider`, `Event`).
- Produces:
  - `_JoinGroup` keyed `(parent_conversation_id, join_group)` in `self._join_groups`
  - `SubagentService._on_turn_complete(event: Event) -> None`
  - `SubagentService._settle_join_member(...)` (real impl replacing the Task 3 stub)
  - `SubagentService._maybe_fire_join(group: _JoinGroup) -> None`
  - `SubagentService._build_join_seed(group: _JoinGroup) -> str` (staticmethod)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_subagent_service.py`. These drive the cohort by hand: register two members (via `execute_tool`-style registration helper), settle them, seal via a synthetic `turn_complete` event, and assert exactly one resume with both reports.

```python
from gilbert.interfaces.events import Event


def _register_member(svc: SubagentService, parent: str, group: str, on_complete: str, uid: str, sa_id: str, name: str = "Research Analyst") -> None:
    svc._register_join_member(parent, group, on_complete,
                              UserContext(user_id=uid, display_name="U", roles=("user",)),
                              sa_id, name)


@pytest.mark.asyncio
async def test_join_fires_once_after_seal_and_all_settled() -> None:
    svc, fake = _svc_with_fake_ai()
    parent, group = "parent1", "cohort-a"
    _register_member(svc, parent, group, "merge into one playlist", "u1", "sa1")
    _register_member(svc, parent, group, "merge into one playlist", "u1", "sa2")

    t = dataclasses.replace(next(iter(svc._types.values())), deliver_as="inline", name="Research Analyst")

    # Settle both members BEFORE seal — must NOT fire yet.
    run1 = svc._runs_for_test("sa1", parent, group)
    run2 = svc._runs_for_test("sa2", parent, group)
    await svc._settle_join_member(run1, t, run1_user(), parent, report="R1", rel_path=None, failure=None)
    await svc._settle_join_member(run2, t, run1_user(), parent, report="R2", rel_path=None, failure=None)
    assert fake.resumes == []

    # Seal via the coordinator turn completing.
    await svc._on_turn_complete(Event(event_type="chat.stream.turn_complete", data={"conversation_id": parent}))

    assert len(fake.resumes) == 1
    instr = fake.resumes[0]["instruction"]
    assert "merge into one playlist" in instr
    assert "R1" in instr and "R2" in instr


@pytest.mark.asyncio
async def test_join_surfaces_partial_failure() -> None:
    svc, fake = _svc_with_fake_ai()
    parent, group = "parent1", "cohort-b"
    _register_member(svc, parent, group, "combine results", "u1", "sa1")
    _register_member(svc, parent, group, "combine results", "u1", "sa2")
    t = dataclasses.replace(next(iter(svc._types.values())), deliver_as="inline", name="Research Analyst")

    r1 = svc._runs_for_test("sa1", parent, group)
    r2 = svc._runs_for_test("sa2", parent, group)
    await svc._on_turn_complete(Event(event_type="chat.stream.turn_complete", data={"conversation_id": parent}))
    await svc._settle_join_member(r1, t, run1_user(), parent, report="GOOD", rel_path=None, failure=None)
    assert fake.resumes == []  # one still outstanding
    await svc._settle_join_member(r2, t, run1_user(), parent, report=None, rel_path=None, failure="boom")

    assert len(fake.resumes) == 1
    instr = fake.resumes[0]["instruction"]
    assert "GOOD" in instr
    assert "boom" in instr  # failure surfaced, not swallowed
```

Add small test helpers at the top of the module:

```python
def run1_user() -> UserContext:
    return UserContext(user_id="u1", display_name="U", roles=("user",))
```

And add a tiny accessor to `SubagentService` used only by the tests to fetch/construct the `_Run` for a settled member (or construct `_Run` inline in the test instead). Prefer constructing inline — replace `svc._runs_for_test(...)` calls with direct `_Run(...)` construction:

```python
    r = _Run(subagent_id="sa1", agent_type="deep-research", query="q",
             conversation_id="c", parent_conversation_id=parent, user_id="u1",
             status="completed", started_at="t", on_complete="merge into one playlist",
             join_group=group)
```

(Use this inline form in both tests; drop the `_runs_for_test` helper.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "join_fires or join_surfaces" -v`
Expected: FAIL — `_register_join_member` / `_on_turn_complete` / real `_settle_join_member` absent.

- [ ] **Step 3: Add the cohort dataclasses + registry**

In `subagent.py`, after the `_Run` dataclass (~line 71), add:

```python
@dataclass
class _JoinMember:
    subagent_id: str
    agent_name: str
    settled: bool = False
    report: str | None = None
    rel_path: str | None = None
    failure: str | None = None


@dataclass
class _JoinGroup:
    key: tuple[str, str]  # (parent_conversation_id, join_group)
    on_complete: str
    user_ctx: Any
    parent_conversation_id: str
    members: dict[str, _JoinMember] = field(default_factory=dict)
    sealed: bool = False
    fired: bool = False
```

In `__init__`, add (near `self._runs`):

```python
        # Join cohorts keyed by (parent_conversation_id, join_group).
        self._join_groups: dict[tuple[str, str], _JoinGroup] = {}
        self._event_bus: Any = None
        self._unsub_turn_complete: Any = None
```

- [ ] **Step 4: Register members synchronously in `execute_tool`**

Members MUST register during the coordinator turn (before any settles), so add registration in `execute_tool`'s background branch, right after `subagent_id = uuid.uuid4().hex`:

```python
                if join_group and parent_conv:
                    self._register_join_member(
                        parent_conv, join_group, on_complete, caller,
                        subagent_id, t.name,
                    )
```

Add the registration helper (near the other join helpers):

```python
    def _register_join_member(
        self,
        parent_conversation_id: str,
        join_group: str,
        on_complete: str,
        user_ctx: UserContext | None,
        subagent_id: str,
        agent_name: str,
    ) -> None:
        """Add a member to its cohort synchronously, at spawn time, so
        membership is frozen before any member can settle."""
        key = (parent_conversation_id, join_group)
        group = self._join_groups.get(key)
        if group is None:
            group = _JoinGroup(
                key=key,
                on_complete=on_complete,
                user_ctx=user_ctx,
                parent_conversation_id=parent_conversation_id,
            )
            self._join_groups[key] = group
        # First non-empty on_complete wins as the cohort's follow-up.
        if not group.on_complete and on_complete:
            group.on_complete = on_complete
        group.members[subagent_id] = _JoinMember(
            subagent_id=subagent_id, agent_name=agent_name
        )
```

- [ ] **Step 5: Replace the `_settle_join_member` stub with the real impl + fire logic + seed**

Replace the Task 3 stub with:

```python
    async def _settle_join_member(
        self,
        run: _Run,
        t: SubagentType,
        user_ctx: UserContext | None,
        parent_conversation_id: str | None,
        *,
        report: str | None,
        rel_path: str | None,
        failure: str | None,
    ) -> None:
        """Record a cohort member's result, then fire the cohort if it's
        sealed and every member has settled."""
        if not parent_conversation_id:
            return
        key = (parent_conversation_id, run.join_group)
        group = self._join_groups.get(key)
        if group is None:
            return
        member = group.members.get(run.subagent_id)
        if member is None:
            # Not pre-registered (shouldn't happen) — add it so the count is right.
            member = _JoinMember(subagent_id=run.subagent_id, agent_name=t.name)
            group.members[run.subagent_id] = member
        member.settled = True
        member.report = report
        member.rel_path = rel_path
        member.failure = failure
        await self._maybe_fire_join(group)

    async def _maybe_fire_join(self, group: _JoinGroup) -> None:
        """Fire the cohort's resume exactly once, when sealed and all settled."""
        if group.fired or not group.sealed:
            return
        if not group.members or not all(m.settled for m in group.members.values()):
            return
        group.fired = True
        self._join_groups.pop(group.key, None)
        if not (group.parent_conversation_id and group.user_ctx):
            return
        if not isinstance(self._ai, ConversationResumer):
            return
        seed = self._build_join_seed(group)
        try:
            await self._ai.resume_turn(group.parent_conversation_id, group.user_ctx, seed)
        except Exception:
            logger.exception("join-group resume failed for %s", group.key)

    @staticmethod
    def _build_join_seed(group: _JoinGroup) -> str:
        """Assemble the combined resume instruction for a settled cohort:
        every successful report (path or inline) plus an explicit list of any
        failures, so the coordinator can decide whether it has enough."""
        ok_lines: list[str] = []
        fail_lines: list[str] = []
        for m in group.members.values():
            if m.failure is not None:
                fail_lines.append(f"- `{m.agent_name}` FAILED: {m.failure}")
            elif m.rel_path:
                ok_lines.append(f"- `{m.agent_name}`: report at `{m.rel_path}`")
            else:
                ok_lines.append(f"- `{m.agent_name}`:\n{m.report or ''}")
        parts = [
            "[automated] The cohort of background agents you dispatched has "
            "finished."
        ]
        if ok_lines:
            parts.append("Reports:\n" + "\n".join(ok_lines))
        if fail_lines:
            parts.append("These failed:\n" + "\n".join(fail_lines))
        parts.append(f"Now: {group.on_complete}")
        return "\n\n".join(parts)
```

- [ ] **Step 6: Subscribe to `chat.stream.turn_complete` (seal) in `start()`, add `stop()`**

At the end of `SubagentService.start(...)`, after the existing wiring, add:

```python
        from gilbert.interfaces.events import EventBusProvider

        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._event_bus = bus_svc.bus
            self._unsub_turn_complete = self._event_bus.subscribe(
                "chat.stream.turn_complete", self._on_turn_complete
            )
```

Add the handler and `stop()`:

```python
    async def _on_turn_complete(self, event: Any) -> None:
        """Seal every cohort whose parent coordinator turn just completed, then
        fire any that are already fully settled. Membership was frozen at spawn
        time (synchronous registration), so sealing only enables firing."""
        conv = str(event.data.get("conversation_id") or "")
        if not conv:
            return
        for group in list(self._join_groups.values()):
            if group.parent_conversation_id == conv and not group.sealed:
                group.sealed = True
                await self._maybe_fire_join(group)

    async def stop(self) -> None:
        """Unsubscribe from the event bus on shutdown/restart."""
        if self._unsub_turn_complete is not None:
            try:
                self._unsub_turn_complete()
            except Exception:
                logger.debug("unsubscribe(turn_complete) raised (ignored)", exc_info=True)
            self._unsub_turn_complete = None
```

> Non-streaming-backend limitation: `chat.stream.turn_complete` is only published when the backend supports streaming (`ai.py:3314`). On a non-streaming backend a cohort never seals and never fires. The deep-research flow runs on streaming backends (Anthropic/Ollama), so this is an accepted v1 limitation — document it and leave a follow-up. Solo resume (Task 3) is unaffected.

- [ ] **Step 7: Run the join tests**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "join_fires or join_surfaces" -v`
Expected: PASS (2 tests).

- [ ] **Step 8: Run the full subagent + AI suites**

Run: `uv run pytest tests/unit/test_subagent_service.py tests/unit/test_ai_service_resume_turn.py tests/unit/test_ai_service.py -q`
Expected: PASS. `git checkout uv.lock` if it changed.

- [ ] **Step 9: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "feat(subagent): join-group cohort resume (seal on turn_complete, fire once)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full regression, type/lint, docs sync

**Files:**
- Modify: `docs/architecture/*` only if a subagent walkthrough exists that now drifts (check `ls docs/architecture/`); otherwise none.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -q`
Expected: PASS. Investigate any failure before proceeding. `git checkout uv.lock` if `uv run` rewrote it.

- [ ] **Step 2: Types + lint on touched files**

Run:
```bash
uv run mypy src/gilbert/core/services/ai.py src/gilbert/core/services/subagent.py src/gilbert/interfaces/ai.py
uv run ruff check src/gilbert tests/unit/test_subagent_service.py tests/unit/test_ai_service_resume_turn.py
uv run ruff format src/gilbert/core/services/subagent.py src/gilbert/core/services/ai.py src/gilbert/interfaces/ai.py
```
Expected: clean (fix anything reported).

- [ ] **Step 3: Docs check**

Run: `ls docs/architecture/` and grep for a subagent/deep-research walkthrough. If one describes the completion path as "passive / no coordinator resume," update it to describe intent-gated resume (`on_complete`/`join_group`). If none exists, no doc change is needed (design + this plan are the record).

- [ ] **Step 4: Final commit (if docs changed)**

```bash
git add docs/
git commit -m "docs(subagent): note intent-gated coordinator resume on background-agent completion

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Component 1 (`spawn_agent` schema) → Task 2. ✓
- Component 2 (`resume_turn`) → Task 1. ✓
- Component 3 (concurrency guard) → Task 1 (per-conversation turn lock, Steps 4 & 6). ✓ Realized as a shared per-conversation lock (deferral by awaiting the lock in the detached resume task) — matches the approved "defer, don't interleave" intent.
- Component 4 (solo resume) → Task 3. ✓
- Component 5 (join seal + settle) → Task 4. ✓ Synchronous registration added to remove the settle-before-register race the spec's async model implied.
- Component 6 (resume seed) → `_build_solo_seed` (Task 3) + `_build_join_seed` (Task 4), `USER`-role row with `[automated]` prefix. ✓
- Component 7 (failure handling) → Task 3 (solo failure path) + Task 4 (`_build_join_seed` failure lines). ✓
- Testing section → covered across Tasks 1,3,4 + Task 5 regression. ✓

**Placeholder scan:** The Task 3 `_settle_join_member` stub is intentional and explicitly replaced in Task 4 Step 5 — not a placeholder left dangling. No TBD/TODO remain.

**Type consistency:** `resume_turn`, `_finalize_run`, `_settle_join_member`, `_maybe_fire_join`, `_build_solo_seed`, `_build_join_seed`, `_register_join_member`, `_on_turn_complete` signatures are consistent between their definitions and call sites. `_JoinGroup.key` is `(parent_conversation_id, join_group)` everywhere it's constructed/looked up.

**Known limitation (documented in Task 4 Step 6):** join cohorts don't auto-fire on non-streaming backends (no `turn_complete`); solo resume is unaffected.
