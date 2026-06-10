# Supervising Subagents Implementation Plan (Slice 6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user watch a running subagent live (read-only), stop it (keeping its partial result), receive the finished report as a workspace file attachment + notification anchored at its completion time, view `.md` with a Rendered/Raw toggle, ask the model for status, and run deep research on a richer prompt.

**Architecture:** A `_Run` registry in `SubagentService` (keyed by `subagent_id`) is the backbone: it holds the pre-allocated subagent conversation id (for watching), a `stop_flag` (for graceful stop via `chat()`'s `should_stop_callback`), and run status. Completion delivers a real message carrying a `FileAttachment` for the report (via an extended `append_assistant_message`) plus a notification. The frontend adds a read-only live viewer of the subagent conversation, Watch/Stop controls on the card, a Rendered/Raw tab in the markdown viewer, and an attachment-open hook.

**Tech Stack:** Backend Python 3.12, pytest, asyncio. Frontend React 19 + Vite, vitest + RTL, existing `Tabs`/`MessageList`/`TurnBubble`/`MarkdownContent`/`AttachmentChip`.

**Reference spec:** `docs/superpowers/specs/2026-06-09-supervising-subagents-design.md`. Builds on slices 1–5 (`SubagentService`, background `deep_research`, `<SubagentCard>`, `useActiveSubagents`, `<WorkspaceMarkdownViewer>`), all on `main`.

**Branch:** `feat/supervising-subagents` (already created; the spec is committed there).

---

## File Structure

- **Modify** `src/gilbert/core/services/subagent.py` — `_Run` registry; pre-allocate the subagent conv id; `spawn()` optional `conversation_id`/`subagent_id`/`should_stop`; `stop_subagent`; `WsHandlerProvider`/`get_ws_handlers` (`subagent.stop`); the `check_research` tool; deliver the report as an attachment + notification; enrich the started event.
- **Modify** `src/gilbert/interfaces/ai.py` — extend the `ConversationMessagePoster.append_assistant_message` protocol with `attachments`.
- **Modify** `src/gilbert/core/services/ai.py` — `append_assistant_message(attachments=…)` persists + emits attachments.
- **Modify** `src/gilbert/core/subagents/types.py` — deep-research prompt upgrade.
- **Modify** `tests/unit/test_subagent_service.py`, `tests/unit/test_ai_service.py`, `tests/unit/test_subagent_types.py`.
- **Create** `frontend/src/components/chat/SubagentLiveViewer.tsx` (+ `.test.tsx`) — read-only live view of a subagent conversation.
- **Modify** `frontend/src/components/chat/SubagentCard.tsx` (+ `.test.tsx`) — Watch + Stop controls.
- **Modify** `frontend/src/hooks/useActiveSubagents.ts` (+ test) — carry `conversation_id`/`query`; drop terminal runs.
- **Modify** `frontend/src/components/chat/WorkspaceMarkdownViewer.tsx` (+ test) — Rendered/Raw tab.
- **Modify** `frontend/src/components/chat/TurnBubble.tsx` — pass `onOpen` to `AttachmentChip` for `.md`.
- **Modify** `frontend/src/components/chat/ChatPage.tsx` — mount the viewers; the stop RPC; wire watch.
- **Modify** `frontend/src/hooks/useWsApi.ts` — a `stopSubagent` RPC.

Out of scope (spec §13): reply inside a watched run; resume; cross-restart persistence; a global tasks dashboard.

---

## Task 0: Branch

- [ ] **Step 1**
```bash
cd /home/assistant/gilbert
git rev-parse --abbrev-ref HEAD   # expect: feat/supervising-subagents
```
(Branch already exists; if not, `git checkout -b feat/supervising-subagents`.)

---

## Task 1: Run registry + pre-allocated conversation + enriched started event (backend)

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
@pytest.mark.asyncio
async def test_spawn_uses_preallocated_conversation_and_registers_run(tmp_path: Any) -> None:
    poster = _FakePoster(report="# R")
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    await svc._run_research_background("widgets?", "conv-parent", caller)

    # A run was registered with the subagent's own pre-allocated conversation id.
    runs = svc.list_runs("u1")
    assert len(runs) == 1
    run = runs[0]
    assert run["agent_type"] == "deep-research"
    assert run["query"] == "widgets?"
    assert run["conversation_id"]  # pre-allocated, non-empty
    # spawn passed that id to chat (fresh conv, but a known id we can watch).
    assert poster.calls[0]["conversation_id"] == run["conversation_id"]
    # The started event carried the subagent conversation id + the query.
    started = next(e for e in bus.events if e.event_type == "chat.stream.subagent_started")
    assert started.data["conversation_id"] == "conv-parent"  # routing = parent
    assert started.data["subagent_conversation_id"] == run["conversation_id"]
    assert started.data["query"] == "widgets?"
```

Note: this test relies on `_FakePoster.chat` recording `conversation_id` — update `_FakePoster.chat` to record `k` (it already stores `self.calls.append(k)`), so `poster.calls[0]["conversation_id"]` works.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k preallocated_conversation_and_registers_run -q`
Expected: FAIL — `list_runs` doesn't exist; no run registered; started event lacks `subagent_conversation_id`.

- [ ] **Step 3: Implement the registry + pre-alloc**

In `src/gilbert/core/services/subagent.py`:

(a) Add a dataclass import + the `_Run` type near the top (after imports):

```python
from dataclasses import dataclass, field


@dataclass
class _Run:
    subagent_id: str
    agent_type: str
    query: str
    conversation_id: str
    parent_conversation_id: str | None
    user_id: str
    status: str  # running | completed | stopped | failed
    started_at: str
    stop_flag: list[bool] = field(default_factory=lambda: [False])
    task: Any = None
```

(b) In `__init__`, replace the slice-5 `self._background_tasks` set with a registry:

```python
        self._runs: dict[str, _Run] = {}
```

(c) Add registry helpers (place before `_run_in_background`):

```python
    _RUN_CAP = 20

    def _register_run(self, run: _Run) -> None:
        self._runs[run.subagent_id] = run
        # Prune oldest finished runs beyond the cap.
        if len(self._runs) > self._RUN_CAP:
            finished = [r for r in self._runs.values() if r.status != "running"]
            finished.sort(key=lambda r: r.started_at)
            for r in finished[: len(self._runs) - self._RUN_CAP]:
                self._runs.pop(r.subagent_id, None)

    def list_runs(self, user_id: str) -> list[dict[str, Any]]:
        """Recent/active runs for a user — backs the check_research tool + UI."""
        return [
            {
                "subagent_id": r.subagent_id,
                "agent_type": r.agent_type,
                "query": r.query,
                "conversation_id": r.conversation_id,
                "status": r.status,
                "started_at": r.started_at,
            }
            for r in self._runs.values()
            if r.user_id == user_id
        ]
```

(d) Change `spawn()`'s signature + body to accept the pre-allocated ids + stop callback, and use a single captured `routing` (already does). Replace the `subagent_id = uuid.uuid4().hex` line and the chat call:

```python
    async def spawn(
        self,
        agent_type: str,
        prompt: str,
        user_ctx: UserContext | None = None,
        *,
        conversation_id: str | None = None,
        subagent_id: str | None = None,
        should_stop: Any = None,
    ) -> str:
```

Inside, replace `subagent_id = uuid.uuid4().hex` with:

```python
        subagent_id = subagent_id or uuid.uuid4().hex
```

Add `"subagent_conversation_id": conversation_id` and `"query": prompt` to the started event data, and pass `conversation_id` + `should_stop_callback=should_stop` to `self._ai.chat(...)`:

```python
        await self._publish_event(
            "chat.stream.subagent_started",
            {
                **routing,
                "subagent_id": subagent_id,
                "agent_type": agent.id,
                "subagent_conversation_id": conversation_id,
                "query": prompt,
            },
        )
        try:
            result = await self._ai.chat(
                user_message=prompt,
                conversation_id=conversation_id,   # pre-allocated (watchable) or None
                user_ctx=user_ctx,
                system_prompt=system_prompt,
                ai_call=f"subagent.{agent.id}",
                ai_profile=agent.profile_name,
                max_tool_rounds=agent.max_rounds,
                headless=True,
                source="subagent",
                should_stop_callback=should_stop,
            )
```

(e) In `_run_research_background`, pre-allocate the ids + register the run before calling `spawn`, and pass them in. Replace the `report = await self.spawn(...)` line region:

```python
        import uuid as _uuid

        subagent_id = _uuid.uuid4().hex
        sub_conv = _uuid.uuid4().hex
        run = _Run(
            subagent_id=subagent_id,
            agent_type="deep-research",
            query=query,
            conversation_id=sub_conv,
            parent_conversation_id=parent_conversation_id,
            user_id=user_ctx.user_id if user_ctx else "system",
            status="running",
            started_at=datetime.now(UTC).isoformat(),
        )
        self._register_run(run)
        try:
            report = await self.spawn(
                "deep-research",
                query,
                user_ctx=user_ctx,
                conversation_id=sub_conv,
                subagent_id=subagent_id,
                should_stop=lambda: run.stop_flag[0],
            )
            run.status = "stopped" if run.stop_flag[0] else "completed"
            # ... existing write-report + deliver ...
```

Add `from datetime import UTC, datetime` to the imports if not present.

Note: `spawn`'s `should_stop_callback` requires `AIService.chat` to honor it (it does). `_FakePoster.chat` ignores it — fine for these tests.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (all — new test + slices 1–5). The slice-5 `_run_in_background` GC-reference test still passes because the registry now holds the task ref (see Task 2 for the task-ref move; if the GC test references `_background_tasks`, update it to `_runs`).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: run registry + pre-allocated watchable conversation id

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Graceful stop (backend)

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
@pytest.mark.asyncio
async def test_stop_subagent_sets_flag_and_checks_owner(tmp_path: Any) -> None:
    svc = SubagentService()
    svc._enabled = True
    run = _Run(
        subagent_id="s1", agent_type="deep-research", query="q",
        conversation_id="c", parent_conversation_id="p", user_id="u1",
        status="running", started_at="t",
    )
    svc._runs["s1"] = run

    # Wrong user can't stop it.
    assert svc.stop_subagent("s1", "intruder") is False
    assert run.stop_flag[0] is False
    # Owner can.
    assert svc.stop_subagent("s1", "u1") is True
    assert run.stop_flag[0] is True
    # Unknown id is a harmless no-op.
    assert svc.stop_subagent("nope", "u1") is False


@pytest.mark.asyncio
async def test_stopped_run_delivers_partial(tmp_path: Any) -> None:
    # A poster whose chat returns once should_stop is requested.
    class _StopAwarePoster(_FakePoster):
        async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
            cb = k.get("should_stop_callback")
            # Simulate the engine seeing the stop and returning the partial.
            if cb:
                cb()
            return ChatTurnResult(
                response_text="# Partial findings", conversation_id=k.get("conversation_id") or "c",
                ui_blocks=[], tool_usage=[], attachments=[], rounds=[],
            )

    poster = _StopAwarePoster()
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    # Pre-set a run whose stop flag is already requested, then run.
    await svc._run_research_background("q", "conv-parent", caller)
    run = svc.list_runs("u1")[0]
    # The fake triggers stop via the callback; status reflects it.
    # (Delivery happened; we just assert a message was delivered with the partial.)
    assert poster.delivered, "delivered the partial"
    _, msg = poster.delivered[0]
    assert "Partial findings" in msg or "/api/chat/download/" in msg
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "stop_subagent or stopped_run_delivers_partial" -q`
Expected: FAIL — `stop_subagent` doesn't exist.

- [ ] **Step 3: Implement stop**

In `src/gilbert/core/services/subagent.py`:

```python
    def stop_subagent(self, subagent_id: str, requester_id: str) -> bool:
        """Request a graceful stop of a running subagent. Returns True if the
        stop was applied (the run exists, is running, and is owned by the
        requester). No-op (False) for unknown/finished/foreign runs."""
        run = self._runs.get(subagent_id)
        if run is None or run.status != "running" or run.user_id != requester_id:
            return False
        run.stop_flag[0] = True
        logger.info("Subagent %s stop requested by %s", subagent_id, requester_id)
        return True
```

In `_run_research_background`, when delivering on stop, label it. Change the success-path message build so a stopped run is announced as such — wrap the lead/message:

```python
            stopped = run.stop_flag[0]
            verb = "Research stopped early — here's what it found so far." if stopped else "**Research complete.**"
            if rel_path and parent_conversation_id:
                url = f"/api/chat/download/{parent_conversation_id}/{rel_path}"
                lead = report.strip().split("\n\n", 1)[0][:400]
                message = f"{verb} [Open the report]({url})\n\n{lead}"
            else:
                message = f"{verb}\n\n{report}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: graceful stop_subagent (deliver the partial report)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `subagent.stop` WS RPC + `check_research` tool (backend)

**Files:** Modify `src/gilbert/core/services/subagent.py`; Test `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
def test_service_provides_subagent_stop_ws_handler() -> None:
    from gilbert.interfaces.ws import WsHandlerProvider

    svc = SubagentService()
    assert isinstance(svc, WsHandlerProvider)
    assert "ws_handlers" in svc.service_info().capabilities
    assert "subagent.stop" in svc.get_ws_handlers()


@pytest.mark.asyncio
async def test_ws_stop_handler_stops_owned_run() -> None:
    svc = SubagentService()
    svc._enabled = True
    svc._runs["s1"] = _Run(
        subagent_id="s1", agent_type="deep-research", query="q",
        conversation_id="c", parent_conversation_id="p", user_id="u1",
        status="running", started_at="t",
    )

    class _Conn:
        user_id = "u1"
        user_ctx = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    res = await svc.get_ws_handlers()["subagent.stop"](_Conn(), {"id": "r1", "subagent_id": "s1"})
    assert res["ok"] is True
    assert svc._runs["s1"].stop_flag[0] is True


def test_get_tools_includes_check_research() -> None:
    tools = SubagentService().get_tools()
    assert any(t.name == "check_research" for t in tools)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "ws_handler or ws_stop or check_research" -q`
Expected: FAIL — no WsHandlerProvider, no handler, no tool.

- [ ] **Step 3: Implement the RPC + tool**

In `src/gilbert/core/services/subagent.py`:

(a) Imports + class bases:

```python
from gilbert.interfaces.ws import WsHandlerProvider
```
```python
class SubagentService(Service, WsHandlerProvider):
```

(b) Add `"ws_handlers"` to `service_info().capabilities`:

```python
            capabilities=frozenset({"subagent", "ai_tools", "ws_handlers"}),
```

(c) Add the handler + provider method (place near `execute_tool`):

```python
    def get_ws_handlers(self) -> dict[str, Any]:
        return {"subagent.stop": self._ws_stop_subagent}

    async def _ws_stop_subagent(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        subagent_id = str(frame.get("subagent_id") or "")
        ok = self.stop_subagent(subagent_id, getattr(conn, "user_id", ""))
        return {"type": "subagent.stop.result", "ref": frame.get("id"), "ok": ok}
```

(d) Add the `check_research` tool to `get_tools()` (after `deep_research`):

```python
            ToolDefinition(
                name="check_research",
                description=(
                    "List your recent and in-progress research runs and their "
                    "status (running/completed/stopped/failed) so you can report "
                    "progress or point at a finished report."
                ),
                parameters=[],
                slash_command="research-status",
                slash_help="Show running/recent research: /research-status",
                required_role="user",
                interactive=True,
            ),
```

(e) Dispatch it in `execute_tool` (add a branch before the final `raise KeyError`):

```python
        if name == "check_research":
            user = get_current_user()
            runs = self.list_runs(user.user_id)
            if not runs:
                return "No research runs found."
            lines = [
                f"- [{r['status']}] {r['agent_type']}: “{r['query']}” (started {r['started_at']})"
                for r in runs
            ]
            return "Research runs:\n" + "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: subagent.stop WS RPC + check_research status tool

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Deliver the report as an attachment + notification; prompt upgrade (backend)

**Files:** Modify `src/gilbert/interfaces/ai.py`, `src/gilbert/core/services/ai.py`, `src/gilbert/core/services/subagent.py`, `src/gilbert/core/subagents/types.py`; Tests in `tests/unit/test_subagent_service.py`, `tests/unit/test_ai_service.py`, `tests/unit/test_subagent_types.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_subagent_service.py`, update `_FakePoster.append_assistant_message` to accept attachments and record them, then add a test:

```python
# In _FakePoster: change the method to
#     async def append_assistant_message(self, conversation_id, content, attachments=None):
#         self.delivered.append((conversation_id, content, attachments or []))
# and update existing unpackings of self.delivered[0] from (conv, msg) to (conv, msg, atts).

@pytest.mark.asyncio
async def test_completed_run_delivers_report_attachment_and_notifies(tmp_path: Any) -> None:
    poster = _FakePoster(report="# Findings\n\nbody")
    notifs: list[dict[str, Any]] = []

    class _Notif:
        async def notify_user(self, **kwargs: Any) -> Any:
            notifs.append(kwargs)
            return object()

    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._notifications = _Notif()  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(_FakeBus()))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    await svc._run_research_background("widgets?", "conv-parent", caller)

    conv, _msg, atts = poster.delivered[0]
    assert conv == "conv-parent"
    assert len(atts) == 1
    att = atts[0]
    assert att.media_type == "text/markdown"
    assert att.workspace_path.startswith("outputs/research-")
    assert att.workspace_conv == "conv-parent"
    assert notifs and notifs[0]["user_id"] == "u1"
```

In `tests/unit/test_ai_service.py`:

```python
def test_append_assistant_message_accepts_attachments_param() -> None:
    import inspect

    from gilbert.core.services.ai import AIService
    from gilbert.interfaces.ai import ConversationMessagePoster

    assert "attachments" in inspect.signature(AIService.append_assistant_message).parameters
    assert "attachments" in inspect.signature(ConversationMessagePoster.append_assistant_message).parameters
```

In `tests/unit/test_subagent_types.py`:

```python
def test_deep_research_prompt_mentions_credible_sources_and_evidence() -> None:
    t = get_agent_type("deep-research")
    p = t.system_prompt.lower()
    assert "credible" in p
    assert "evidence" in p or "preserv" in p  # the page-reading discipline
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_subagent_service.py tests/unit/test_ai_service.py tests/unit/test_subagent_types.py -k "delivers_report_attachment or append_assistant_message_accepts_attachments or credible_sources" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

(a) `src/gilbert/interfaces/ai.py` — extend the protocol:

```python
    async def append_assistant_message(
        self,
        conversation_id: str,
        content: str,
        attachments: list[FileAttachment] | None = None,
    ) -> None: ...
```
(Confirm `FileAttachment` is imported in `interfaces/ai.py`; if not, `from gilbert.interfaces.attachments import FileAttachment`.)

(b) `src/gilbert/core/services/ai.py` — `append_assistant_message`: add the param, persist serialized attachments on the message row, and include them in the event:

```python
    async def append_assistant_message(
        self,
        conversation_id: str,
        content: str,
        attachments: list[FileAttachment] | None = None,
    ) -> None:
```
In the row build, add `"attachments": [self._serialize_attachment(a) for a in (attachments or [])]` (use the existing attachment-serialization helper the chat path uses; if the helper is named differently, mirror how `chat()` serializes `Message.attachments`). In the `chat.message.created` event `data`, replace `"attachments": []` with `"attachments": [self._serialize_attachment(a) for a in (attachments or [])]`.

(c) `src/gilbert/core/services/subagent.py`:
- Resolve notifications in `start()`: `self._notifications = resolver.get_capability("notifications")` (store as-is; isinstance-check on use). Add `self._notifications = None` in `__init__`.
- In `_run_research_background` success path, build the attachment + deliver + notify:

```python
            from gilbert.interfaces.attachments import FileAttachment
            from gilbert.interfaces.notifications import NotificationProvider, NotificationUrgency

            attachments: list[FileAttachment] = []
            if rel_path and parent_conversation_id:
                attachments = [
                    FileAttachment(
                        kind="text",
                        name=rel_path.split("/")[-1],
                        media_type="text/markdown",
                        workspace_skill="workspace",
                        workspace_path=rel_path,
                        workspace_conv=parent_conversation_id,
                    )
                ]
            await self._deliver(parent_conversation_id, message, attachments)
            if isinstance(self._notifications, NotificationProvider) and user_ctx:
                try:
                    await self._notifications.notify_user(
                        user_id=user_ctx.user_id,
                        message=f"Deep research {run.status}: {query}",
                        urgency=NotificationUrgency.NORMAL,
                        source="subagent",
                        source_ref={
                            "conversation_id": parent_conversation_id,
                            "subagent_id": run.subagent_id,
                            "report_path": rel_path or "",
                        },
                    )
                except Exception:
                    logger.exception("subagent notification failed")
```

- Extend `_deliver` to accept + forward attachments:

```python
    async def _deliver(
        self,
        conversation_id: str | None,
        content: str,
        attachments: Any = None,
    ) -> None:
        if not conversation_id or not isinstance(self._ai, ConversationMessagePoster):
            return
        try:
            await self._ai.append_assistant_message(conversation_id, content, attachments)
        except Exception:
            logger.exception("Failed to deliver research message to %s", conversation_id)
```

(d) `src/gilbert/core/subagents/types.py` — extend `_DEEP_RESEARCH_PROMPT` with the §5.6 additions (append before the existing slice-5 sentence):

```
" Handle both broad, open-domain questions and specialized or academic ones. Rely on credible, diverse sources and stay objective. When you read a page, extract the most relevant evidence while preserving its full original context, and weigh how much it actually answers the question before moving on."
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py tests/unit/test_ai_service.py tests/unit/test_subagent_types.py -q`
Expected: PASS (update any earlier test that unpacked `poster.delivered` as a 2-tuple to the new 3-tuple).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/ai.py src/gilbert/core/services/ai.py src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py tests/unit/
git commit -m "subagent: deliver report as a file attachment + notification; richer prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend — live viewer, Watch/Stop on the card, active-card removal

**Files:** Create `frontend/src/components/chat/SubagentLiveViewer.tsx` (+ test); Modify `frontend/src/hooks/useActiveSubagents.ts` (+ test), `frontend/src/components/chat/SubagentCard.tsx` (+ test), `frontend/src/hooks/useWsApi.ts`, `frontend/src/components/chat/ChatPage.tsx`

- [ ] **Step 1: `useActiveSubagents` carries conv id/query + drops terminal runs (failing test first)**

In `frontend/src/hooks/useActiveSubagents.test.tsx`, add cases asserting: a started event with `subagent_conversation_id` + `query` populates those fields; a `completed`/`stopped`/`failed` event REMOVES the run from the returned list. Then update `frontend/src/types/events.ts` `ActiveSubagent` to add `conversationId?: string` and `query?: string`, and update `useActiveSubagents.ts`:
- on started: store `conversationId: String(d.subagent_conversation_id||"")`, `query: String(d.query||"")`.
- on completed/stopped/failed: instead of marking status, **delete** the entry (`const {[id]:_, ...rest} = prev; return rest;`), so the floating card disappears and the delivered message stands in.

Add a `chat.stream.subagent_stopped` subscription (status terminal too) alongside completed/failed.

- [ ] **Step 2: `SubagentLiveViewer` (failing test first)**

Create `frontend/src/components/chat/SubagentLiveViewer.test.tsx` mirroring the `WorkspaceMarkdownViewer` test style: mock `useWsApi().loadConversation` to return `{ turns: [{ user_message:{content:"go",attachments:[]}, rounds:[], final_content:"working…", streaming:false }] }`, render `<SubagentLiveViewer open conversationId="sub-1" onClose={()=>{}} />` wrapped in `<AuthProvider>`, and assert the turn content appears.

Create `frontend/src/components/chat/SubagentLiveViewer.tsx`: a `Dialog` that on open `loadConversation(conversationId)` into local `turns` state and renders `<MessageList turns={turns} uiBlocks={[]} isShared={false} onBlockSubmit={()=>{}} />` (read-only — no input). Subscribe via `useEventBus("chat.stream.text_delta"/"reasoning"/"round_complete"/"turn_complete", handler)` where the handler **only updates when `event.data.conversation_id === conversationId`** (its own id, not the active chat) — append/extend the streaming turn the same way ChatPage does, scoped locally. Title: "Subagent activity".

- [ ] **Step 3: `useWsApi` stop RPC + `SubagentCard` Watch/Stop (failing test first)**

In `frontend/src/hooks/useWsApi.ts` add:
```ts
    stopSubagent: (subagentId: string) =>
      rpc<{ ok: boolean }>({ type: "subagent.stop", subagent_id: subagentId }),
```

Update `SubagentCard.test.tsx`: assert that when `status==="running"` the card shows a **Watch** button (calls an `onWatch` prop) and a **Stop** button (calls an `onStop` prop). Update `SubagentCard.tsx` to take optional `onWatch?: () => void` and `onStop?: () => void` and render the two buttons while running (Watch always when a conversation id exists; Stop while running).

- [ ] **Step 4: Wire into ChatPage**

In `ChatPage.tsx`: add `const [watchConv, setWatchConv] = useState<string|null>(null)`. Pass `onWatch`/`onStop` down to the cards (through `MessageList`'s `subagents` rendering — give `SubagentCard` the callbacks: `onWatch={() => sa.conversationId && setWatchConv(sa.conversationId)}`, `onStop={() => api.stopSubagent(sa.subagent_id)}`). Mount `{watchConv && <SubagentLiveViewer open conversationId={watchConv} onClose={() => setWatchConv(null)} />}`.

Since the cards render inside `MessageList`, thread the two callbacks as `MessageList` props (`onWatchSubagent?(id)`, `onStopSubagent?(id)`) and call them from the `subagents.map`.

- [ ] **Step 5: Run + verify**

Run:
```bash
cd /home/assistant/gilbert/frontend
npm run test
npm run typecheck
npm run build
```
Expected: vitest green (incl. the new viewer + card + hook tests); typecheck clean; build succeeds.

- [ ] **Step 6: Commit**
```bash
cd /home/assistant/gilbert
git add frontend/src/components/chat/SubagentLiveViewer.tsx frontend/src/components/chat/SubagentLiveViewer.test.tsx frontend/src/components/chat/SubagentCard.tsx frontend/src/components/chat/SubagentCard.test.tsx frontend/src/hooks/useActiveSubagents.ts frontend/src/hooks/useActiveSubagents.test.tsx frontend/src/types/events.ts frontend/src/hooks/useWsApi.ts frontend/src/components/chat/ChatPage.tsx
git commit -m "frontend: subagent live viewer + Watch/Stop on the card

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — viewer Rendered/Raw tab + open `.md` attachments in the viewer

**Files:** Modify `frontend/src/components/chat/WorkspaceMarkdownViewer.tsx` (+ test), `frontend/src/components/chat/TurnBubble.tsx`, `frontend/src/components/chat/ChatPage.tsx`

- [ ] **Step 1: Rendered/Raw tab (failing test first)**

In `WorkspaceMarkdownViewer.test.tsx`, add a test: after rendering with content "# Hello", clicking a "Raw" tab shows the raw `# Hello` text in a `<pre>` (and "Rendered" shows the heading). Implement in `WorkspaceMarkdownViewer.tsx`: wrap the body in:
```tsx
<Tabs defaultValue="rendered">
  <TabsList variant="line">
    <TabsTrigger value="rendered">Rendered</TabsTrigger>
    <TabsTrigger value="raw">Raw</TabsTrigger>
  </TabsList>
  <TabsContent value="rendered"><MarkdownContent content={content} /></TabsContent>
  <TabsContent value="raw"><pre className="text-xs whitespace-pre-wrap break-words">{rawContent}</pre></TabsContent>
</Tabs>
```
where `rawContent` is the fetched text BEFORE embed-rewriting (keep both: store raw, derive rewritten for the rendered tab). Import `Tabs, TabsList, TabsTrigger, TabsContent` from `@/components/ui/tabs`.

- [ ] **Step 2: Open `.md` attachments in the viewer**

In `TurnBubble.tsx`, where `<AttachmentChip>` is rendered for message attachments, pass an `onOpen` that for a `text/markdown` workspace-reference attachment calls up to ChatPage to open the viewer. Thread a `onOpenReport?(conv: string, path: string)` prop from ChatPage → MessageList → TurnBubble → AttachmentChip's `onOpen`:
```tsx
onOpen={(att) => {
  if ((att.media_type === "text/markdown") && att.workspace_path) {
    onOpenReport?.(att.workspace_conv || conversationId || "", att.workspace_path);
  }
}}
```
In ChatPage, `onOpenReport={(conv, path) => setReportView({ conv, path })}` (reuses the slice-5 `reportView` state + `<WorkspaceMarkdownViewer>`).

- [ ] **Step 3: Run + verify**
```bash
cd /home/assistant/gilbert/frontend && npm run test && npm run typecheck && npm run build
```
Expected: all green.

- [ ] **Step 4: Commit**
```bash
cd /home/assistant/gilbert
git add frontend/src/components/chat/WorkspaceMarkdownViewer.tsx frontend/src/components/chat/WorkspaceMarkdownViewer.test.tsx frontend/src/components/chat/TurnBubble.tsx frontend/src/components/chat/ChatPage.tsx
git commit -m "frontend: Rendered/Raw tab in the md viewer; open .md attachments in it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Verification

- [ ] **Step 1: Backend**
```bash
cd /home/assistant/gilbert
uv run ruff check src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py src/gilbert/interfaces/ai.py tests/unit/test_subagent_service.py
uv run mypy src/gilbert/core/services/subagent.py src/gilbert/interfaces/ai.py
uv run pytest tests/unit/ -q
```
Expected: ruff clean on these files; mypy Success; full suite green. (Pre-existing unrelated `ai.py` lint is out of scope.)

- [ ] **Step 2: Frontend**
```bash
cd /home/assistant/gilbert/frontend && npm run typecheck && npm run test && npm run build
```
Expected: all green.

- [ ] **Step 3: Commit fixups**
```bash
cd /home/assistant/gilbert
git add -A && git commit -m "supervising subagents: lint/format fixups" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** registry + pre-alloc conv + enriched events (T1); graceful stop + partial delivery (T2); `subagent.stop` RPC + `check_research` (T3); attachment + notification delivery + prompt upgrade (T4); live read-only viewer + Watch/Stop + active-card removal/ordering (T5); Rendered/Raw tab + open `.md` attachments (T6). `spawn_agent` untouched.
- **Type/name consistency:** `_Run` fields; `stop_subagent(subagent_id, requester_id)`; `list_runs(user_id)`; the started event keys `subagent_conversation_id`/`query`; `append_assistant_message(conversation_id, content, attachments)` identical in protocol (T4a) + impl (T4b) + `_deliver` (T4c) + `_FakePoster`; `FileAttachment(kind/name/media_type/workspace_skill/workspace_path/workspace_conv)` matches the dataclass; the download URL format is identical across slices.
- **Known limits (acknowledged in spec):** in-memory registry (stop on a restart-killed run is a no-op); the read-only viewer is a separate component (ChatPage wiring verified by typecheck/build, not a unit test); a couple of ChatPage prop-threading steps are verified by build, not vitest.
- **No placeholders:** code steps contain the code; run steps contain exact commands + expected results. The few prop-threading instructions name the exact components and props to add.
