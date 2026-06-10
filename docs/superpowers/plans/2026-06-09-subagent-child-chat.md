# Subagent Child-Chat + Card Persistence Implementation Plan (Slice 7)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Make a subagent run a navigable **read-only child conversation** of its parent — watchable in the main chat window (full tool-call/thinking rendering, no composer), nested under the parent in the sidebar, with its running card surviving navigation.

**Architecture:** The subagent's pre-allocated conversation gets a `parent_conversation_id` + a title stamped on its row, so it's a real child. `list_conversations` returns subagent children (no longer hidden) carrying `parent_conversation_id`; the sidebar nests them. The "Watch" action opens the child conversation as the active conversation in **read-only** mode (composer hidden, a breadcrumb/back bar shown), reusing all existing chat rendering. On opening any conversation, the frontend re-seeds running-subagent cards from a `subagent.list` RPC so they survive navigation. The slice-6 `SubagentLiveViewer` modal is retired.

**Tech Stack:** Backend Python 3.12, pytest. Frontend React 19 + Vite, vitest.

**Branch:** `feat/subagent-watch-persistence` (already created).

**Context — slices 1–6 (on main):** `SubagentService` with a `_Run` registry (`subagent_id → {conversation_id, parent_conversation_id, query, status, user_id, …}`), background `deep_research`, `chat.stream.subagent_*` events, `<SubagentCard>` with Watch/Stop, `useActiveSubagents` (event-driven), `<SubagentLiveViewer>` modal, `<WorkspaceMarkdownViewer>`. `AIService.chat(conversation_id=…, source="subagent", …)` creates/saves the subagent conversation; `list_conversations` excludes `source="subagent"` via `_EXCLUDED_SOURCES`. `_save_conversation(conv_id, messages, user_ctx, ui_blocks, source)` stamps the row.

---

## Task 1: Stamp parent + title on the subagent conversation row (backend)

**Files:** `src/gilbert/interfaces/ai.py`, `src/gilbert/core/services/ai.py`, `src/gilbert/core/services/subagent.py`; Test: `tests/unit/test_ai_service.py`, `tests/unit/test_subagent_service.py`

The subagent conversation must carry `parent_conversation_id` + a human title so it can be shown as a child. Thread these through `chat()` → `_save_conversation` so they're stamped on first save.

- [ ] **Step 1 (test):** In `tests/unit/test_ai_service.py`, assert `chat()` accepts `conversation_parent_id` + `conversation_title` and `_save_conversation` stamps them. Concretely, test `_save_conversation` directly:

```python
@pytest.mark.asyncio
async def test_save_conversation_stamps_parent_and_title(ai_service_with_storage) -> None:
    svc, storage = ai_service_with_storage
    await svc._save_conversation(
        "c1", [], source="subagent",
        parent_conversation_id="parent-1", title="Research: widgets",
    )
    row = await storage.get("conversations", "c1")
    assert row["parent_conversation_id"] == "parent-1"
    assert row["source"] == "subagent"
    assert row["title"] == "Research: widgets"
```
(Use the existing storage-backed AIService test fixture; if none exists, mirror how other `_save_conversation` tests construct the service.)

- [ ] **Step 2 (fail):** `uv run pytest tests/unit/test_ai_service.py -k save_conversation_stamps_parent -q` → FAIL (unexpected kwargs).

- [ ] **Step 3 (impl):**
  - `_save_conversation` gains `parent_conversation_id: str = ""` and `title: str = ""`; when non-empty, set them in the `data` dict (only set `title` if non-empty so existing titles aren't clobbered: `if title and "title" not in existing: data["title"] = title`; always set `parent_conversation_id` when provided).
  - `chat()` gains `conversation_parent_id: str = ""` and `conversation_title: str = ""` and forwards them to every `_save_conversation(...)` call it makes. Add the same two params to the `AIProvider.chat` protocol in `interfaces/ai.py` (mirroring how `source`/`headless` were added).
  - In `subagent.py` `spawn()`, forward them to `self._ai.chat(...)`: add params `conversation_parent_id: str = ""`, `conversation_title: str = ""` to `spawn()` and pass through. In `_run_research_background`, call `spawn(..., conversation_parent_id=parent_conversation_id or "", conversation_title=f"Research: {query}"[:80])`.

- [ ] **Step 4 (pass):** `uv run pytest tests/unit/test_ai_service.py tests/unit/test_subagent_service.py -q` → PASS (update any `_FakeAI.chat`/`_FakePoster.chat` in `test_subagent_service.py` to accept the two new kwargs).

- [ ] **Step 5 (commit):**
```bash
git add src/gilbert/interfaces/ai.py src/gilbert/core/services/ai.py src/gilbert/core/services/subagent.py tests/unit/
git commit -m "subagent: stamp parent_conversation_id + title on the child conversation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Return subagent conversations as children in the list (backend)

**Files:** `src/gilbert/core/services/ai.py`; Test: `tests/unit/test_ai_service.py`

Stop hiding subagent conversations; instead include them carrying `parent_conversation_id` so the sidebar can nest them.

- [ ] **Step 1 (test):**
```python
@pytest.mark.asyncio
async def test_list_conversations_includes_subagent_children(ai_service_with_storage) -> None:
    svc, storage = ai_service_with_storage
    await storage.put("conversations", "p", {"id": "p", "user_id": "u1", "messages": [{"role":"user","content":"hi"}], "updated_at": "2026-01-01"})
    await storage.put("conversations", "s", {"id": "s", "user_id": "u1", "source": "subagent", "parent_conversation_id": "p", "title": "Research: x", "messages": [{"role":"user","content":"go"}], "updated_at": "2026-01-02"})
    convs = await svc.list_conversations("u1")
    by_id = {c["conversation_id"]: c for c in convs}
    assert "s" in by_id  # no longer hidden
    assert by_id["s"]["parent_conversation_id"] == "p"
```

- [ ] **Step 2 (fail):** FAIL — `s` excluded; no `parent_conversation_id` in the dict.

- [ ] **Step 3 (impl):** In `list_conversations`:
  - Remove `"subagent"` from `_EXCLUDED_SOURCES` (keep `agent`, `voice_agent`, `phone_call` hidden).
  - In the per-conversation dict the method builds, include `"parent_conversation_id": c.get("parent_conversation_id", "")`.

- [ ] **Step 4 (pass):** `uv run pytest tests/unit/test_ai_service.py -k list_conversations -q` → PASS.

- [ ] **Step 5 (commit):**
```bash
git add src/gilbert/core/services/ai.py tests/unit/test_ai_service.py
git commit -m "ai: surface subagent conversations as children (parent_conversation_id) in the list

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `subagent.list` RPC — active runs for a parent conversation (backend)

**Files:** `src/gilbert/core/services/subagent.py`; Test: `tests/unit/test_subagent_service.py`

Lets the frontend re-seed running-subagent cards when a conversation is (re)opened.

- [ ] **Step 1 (test):**
```python
def test_list_active_for_conversation_filters_by_parent_and_user() -> None:
    svc = SubagentService()
    svc._runs["a"] = _Run(subagent_id="a", agent_type="deep-research", query="q1",
        conversation_id="ca", parent_conversation_id="p1", user_id="u1", status="running", started_at="t")
    svc._runs["b"] = _Run(subagent_id="b", agent_type="deep-research", query="q2",
        conversation_id="cb", parent_conversation_id="p2", user_id="u1", status="running", started_at="t")
    svc._runs["c"] = _Run(subagent_id="c", agent_type="deep-research", query="q3",
        conversation_id="cc", parent_conversation_id="p1", user_id="u1", status="completed", started_at="t")
    out = svc.list_active_for_conversation("p1", "u1")
    ids = {r["subagent_id"] for r in out}
    assert ids == {"a"}  # only running + parent p1 + user u1
    assert out[0]["conversation_id"] == "ca"
    assert out[0]["query"] == "q1"


@pytest.mark.asyncio
async def test_ws_subagent_list_returns_active() -> None:
    svc = SubagentService()
    svc._runs["a"] = _Run(subagent_id="a", agent_type="deep-research", query="q",
        conversation_id="ca", parent_conversation_id="p1", user_id="u1", status="running", started_at="t")
    class _Conn:
        user_id = "u1"
    res = await svc.get_ws_handlers()["subagent.list"](_Conn(), {"id": "r", "conversation_id": "p1"})
    assert [r["subagent_id"] for r in res["runs"]] == ["a"]
```

- [ ] **Step 2 (fail):** FAIL — method/handler missing.

- [ ] **Step 3 (impl):**
```python
    def list_active_for_conversation(self, parent_conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        return [
            {"subagent_id": r.subagent_id, "agent_type": r.agent_type, "query": r.query,
             "conversation_id": r.conversation_id, "status": r.status}
            for r in self._runs.values()
            if r.user_id == user_id
            and r.status == "running"
            and r.parent_conversation_id == parent_conversation_id
        ]
```
Add to `get_ws_handlers`: `"subagent.list": self._ws_list_subagents`, and:
```python
    async def _ws_list_subagents(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        runs = self.list_active_for_conversation(
            str(frame.get("conversation_id") or ""), getattr(conn, "user_id", ""))
        return {"type": "subagent.list.result", "ref": frame.get("id"), "runs": runs}
```

- [ ] **Step 4 (pass):** `uv run pytest tests/unit/test_subagent_service.py -q` → PASS.

- [ ] **Step 5 (commit):**
```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: subagent.list RPC — running runs for a parent conversation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Re-seed running cards on conversation open (frontend)

**Files:** `frontend/src/hooks/useWsApi.ts`, `frontend/src/hooks/useActiveSubagents.ts` (+ test)

- [ ] **Step 1:** Add to `useWsApi.ts`:
```ts
    listSubagents: (conversationId: string) =>
      rpc<{ runs: Array<{ subagent_id: string; agent_type: string; query: string; conversation_id: string; status: string }> }>(
        { type: "subagent.list", conversation_id: conversationId },
      ),
```

- [ ] **Step 2 (test, then impl):** In `useActiveSubagents.test.tsx`, add a case: when `activeConversationId` changes, the hook calls `listSubagents` and seeds the returned running runs into its map (so they render even with no live event). Implement: in `useActiveSubagents`, add a `useEffect([activeConversationId])` that calls `api.listSubagents(activeConversationId)` and merges each run into `byId` (id → `{ subagent_id, conversationId: run.conversation_id, query: run.query, agentType: run.agent_type, status: "running" }`), guarding against races (ignore stale responses if `activeConversationId` changed). Import `useWsApi` in the hook.

- [ ] **Step 3:** `cd frontend && npm run test -- useActiveSubagents` → PASS. `npm run typecheck`.

- [ ] **Step 4 (commit):**
```bash
cd /home/assistant/gilbert
git add frontend/src/hooks/useWsApi.ts frontend/src/hooks/useActiveSubagents.ts frontend/src/hooks/useActiveSubagents.test.tsx
git commit -m "frontend: re-seed running subagent cards from subagent.list on conversation open

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Read-only watch in the main chat window; retire the modal (frontend)

**Files:** `frontend/src/components/chat/ChatPage.tsx`; delete `SubagentLiveViewer.tsx` + `.test.tsx`

- [ ] **Step 1:** Add `const [readOnly, setReadOnly] = useState(false)` and `const [returnToConv, setReturnToConv] = useState<string | null>(null)`.

- [ ] **Step 2:** Replace the `onWatchSubagent` handler (currently `setWatchConv(subagentConvId)`) with:
```ts
            onWatchSubagent={(subagentConvId) => {
              setReturnToConv(activeConvId);
              setReadOnly(true);
              loadConversation(subagentConvId);
            }}
```
Add an `exitWatch`:
```ts
  const exitWatch = useCallback(() => {
    setReadOnly(false);
    const back = returnToConv;
    setReturnToConv(null);
    if (back) loadConversation(back);
    else handleNewChat();
  }, [returnToConv, loadConversation, handleNewChat]);
```

- [ ] **Step 3:** Gate the composer: change the sticky-input block `{(activeConvId || turns.length > 0) && (<ChatInput .../>)}` to `{!readOnly && (activeConvId || turns.length > 0) && (<ChatInput .../>)}`, and directly after it render the read-only bar:
```tsx
        {readOnly && (
          <div className="px-3 sm:px-4 py-3 border-t border-border flex items-center gap-3 text-sm text-muted-foreground">
            <span aria-hidden>👁</span>
            <span>Watching a subagent — read-only.</span>
            <button className="ml-auto underline" onClick={exitWatch}>Back to chat</button>
          </div>
        )}
```

- [ ] **Step 4:** Remove the modal: delete the `SubagentLiveViewer` import (line ~30), the `watchConv` state, and the `{watchConv && <SubagentLiveViewer .../>}` block (~1555). Then `git rm frontend/src/components/chat/SubagentLiveViewer.tsx frontend/src/components/chat/SubagentLiveViewer.test.tsx`.

- [ ] **Step 5:** `cd frontend && npm run typecheck && npm run test && npm run build` → all green (the SubagentLiveViewer test is gone; nothing else should reference it — grep `SubagentLiveViewer` returns nothing).

- [ ] **Step 6 (commit):**
```bash
cd /home/assistant/gilbert
git add frontend/src/components/chat/ChatPage.tsx
git rm frontend/src/components/chat/SubagentLiveViewer.tsx frontend/src/components/chat/SubagentLiveViewer.test.tsx 2>/dev/null; git add -A
git commit -m "frontend: watch a subagent read-only in the main chat window; retire the modal viewer

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Nest subagent children under their parent in the sidebar (frontend)

**Files:** the conversation-list / sidebar component (find it — likely `frontend/src/components/chat/ConversationList.tsx` or a sidebar under `components/chat/` / `components/layout/`), plus the conversation type in `frontend/src/types/chat.ts`.

- [ ] **Step 1:** Add `parent_conversation_id?: string` to the conversation summary type used by the sidebar list (match the field returned by `list_conversations` in Task 2).

- [ ] **Step 2:** In the sidebar list rendering, group conversations: render top-level conversations (no `parent_conversation_id`) as today; for each, render its children (conversations whose `parent_conversation_id` equals this id) **indented** beneath it with a "🔍 " prefix and the child's title. Clicking a child loads it (which, via Task 5, the user reaches read-only when entered through Watch; a direct sidebar click opens it normally read-only too — gate the same `readOnly` when the loaded conv's `source === "subagent"`). Keep it simple: a single level of nesting (subagents don't nest).

- [ ] **Step 3:** `cd frontend && npm run typecheck && npm run build` → green. Add a focused vitest if the sidebar component is unit-harnessed; otherwise verify via build (note in the commit).

- [ ] **Step 4 (commit):**
```bash
cd /home/assistant/gilbert
git add frontend/src/
git commit -m "frontend: nest subagent child conversations under their parent in the sidebar

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Verification

- [ ] **Backend:** `uv run ruff check src/gilbert/core/services/subagent.py src/gilbert/core/services/ai.py src/gilbert/interfaces/ai.py tests/unit/test_subagent_service.py` ; `uv run mypy src/gilbert/core/services/subagent.py src/gilbert/interfaces/ai.py` ; `uv run pytest tests/unit/ -q`. Expected: clean + green (pre-existing unrelated `ai.py` lint out of scope).
- [ ] **Frontend:** `cd frontend && npm run typecheck && npm run test && npm run build` → all green.
- [ ] **Commit fixups** if any.

---

## Self-review notes

- **Coverage:** parent+title on the child row (T1); children surfaced in the list (T2); running-runs RPC (T3); card re-seed on open (T4); read-only in-window watch + modal retired (T5); sidebar nesting (T6).
- **Name consistency:** `parent_conversation_id` is the field name everywhere (row, `list_conversations` dict, frontend type, sidebar grouping); `list_active_for_conversation(parent_conversation_id, user_id)` ↔ `subagent.list` RPC; `conversation_parent_id`/`conversation_title` are the chat()/spawn() kwargs.
- **Known limits:** read-only is enforced by hiding the composer (no server-side block needed — the subagent conv is the user's own; they simply have no input affordance); sidebar nesting is one level; #4 (markdown) is a stale-bundle/cache issue, resolved by a hard reload after this build, not a code change.
- **Markdown (#4):** no code task — confirm after rebuild + hard refresh.
