# Deep Research — Background Run + Workspace Report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `deep_research` non-blocking — it returns immediately, runs detached, and delivers a workspace `.md` report into the conversation when done, openable in a viewer that renders embedded workspace media.

**Architecture:** The `deep_research` tool handler captures the parent conversation + caller, kicks off a detached `asyncio` task (`copy_context()`), and returns an acknowledgement so the parent turn completes. The detached task scopes workspace writes to the parent conversation, runs the existing `spawn("deep-research")` engine, writes the report to `outputs/research-<id>.md`, and delivers a linked message via the existing `append_assistant_message` (`ConversationMessagePoster`) path — emitting the slice-3 lifecycle events. A new `<WorkspaceMarkdownViewer>` opens the `.md` and rewrites relative embeds to `/api/chat/download/<conv>/<path>` so images render.

**Tech Stack:** Backend: Python 3.12, pytest, `asyncio`/`contextvars`. Frontend: React 19 + Vite, vitest + RTL, the existing `MarkdownContent` + `Dialog` + `/api/chat/download` route.

**Reference spec:** `docs/superpowers/specs/2026-06-09-deep-research-background-report-design.md`. Builds on slices 1–4 (`SubagentService`, the `deep-research` type + `deep_research` tool, the `chat.stream.subagent_*` events + `<SubagentCard>`), all on `main`.

**Branch:** `feat/deep-research-background` (already created; the spec is committed there).

---

## File Structure

- **Modify** `src/gilbert/core/services/subagent.py` — resolve `workspace` capability in `start()`; detach `deep_research` in `execute_tool`; add `_run_in_background`, `_run_research_background`, `_write_report`, `_deliver`.
- **Modify** `tests/unit/test_subagent_service.py` — tests for the background flow (immediate return, report write, delivery, events, failure, degradation).
- **Modify** `src/gilbert/core/services/ai.py` — add `write_workspace_file` to the seeded `deep-research` profile's tools.
- **Modify** `src/gilbert/core/subagents/types.py` — extend the deep-research prompt (save a markdown report; embed saved workspace media).
- **Modify** `tests/unit/test_ai_service.py` — assert the profile includes `write_workspace_file`.
- **Create** `frontend/src/components/chat/WorkspaceMarkdownViewer.tsx` + `.test.tsx` — the viewer + embed rewriting.
- **Modify** `frontend/src/components/ui/MarkdownContent.tsx` (or a small wrapper in chat) — intercept clicks on workspace `.md` download links to open the viewer.

Out of scope (spec §12): background for `spawn_agent`; an image-download tool; a running-tasks panel; restart persistence of detached runs.

---

## Task 1: Detach `deep_research` + the background flow (backend)

**Files:**
- Modify: `src/gilbert/core/services/subagent.py`
- Test: `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test for the background flow**

Append to `tests/unit/test_subagent_service.py`:

```python
class _FakePoster:
    """ConversationMessagePoster + AIProvider in one (mirrors AIService)."""

    def __init__(self, report: str = "THE REPORT") -> None:
        self.calls: list[dict[str, Any]] = []
        self.delivered: list[tuple[str, str]] = []
        self._report = report

    async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
        self.calls.append(k)
        return ChatTurnResult(
            response_text=self._report,
            conversation_id="ephemeral",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )

    async def append_assistant_message(self, conversation_id: str, content: str) -> None:
        self.delivered.append((conversation_id, content))


class _FakeWorkspace:
    def __init__(self, tmp_path: Any) -> None:
        self.registered: list[dict[str, Any]] = []
        self._root = tmp_path

    def get_output_dir(self, user_id: str, conversation_id: str) -> Any:
        d = self._root / user_id / conversation_id / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def register_file(self, **kwargs: Any) -> dict[str, Any]:
        self.registered.append(kwargs)
        return {"_id": "f1", **kwargs}


@pytest.mark.asyncio
async def test_run_research_background_writes_report_and_delivers(tmp_path: Any) -> None:
    poster = _FakePoster(report="# Findings\n\nWidgets are good.")
    ws = _FakeWorkspace(tmp_path)
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = ws  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    await svc._run_research_background("widgets?", "conv-parent", caller)

    # Wrote the report to the PARENT conversation's outputs/.
    assert ws.registered, "report file was registered"
    reg = ws.registered[0]
    assert reg["conversation_id"] == "conv-parent"
    assert reg["media_type"] == "text/markdown"
    assert reg["rel_path"].startswith("outputs/research-")
    # The subagent ran as the caller.
    assert poster.calls[0]["user_ctx"] is caller
    # Delivered a message into the parent conversation with a download link.
    assert poster.delivered, "delivered a message"
    conv, msg = poster.delivered[0]
    assert conv == "conv-parent"
    assert "/api/chat/download/conv-parent/outputs/research-" in msg
    # Lifecycle events fired.
    types = [e.event_type for e in bus.events]
    assert "chat.stream.subagent_started" in types
    assert "chat.stream.subagent_completed" in types


@pytest.mark.asyncio
async def test_run_research_background_delivers_failure(tmp_path: Any) -> None:
    class _BoomPoster(_FakePoster):
        async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
            raise RuntimeError("research boom")

    poster = _BoomPoster()
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True

    # Must NOT raise out of the detached task.
    await svc._run_research_background("q", "conv-parent", UserContext.SYSTEM)

    assert poster.delivered, "delivered a failure message"
    _, msg = poster.delivered[0]
    assert "fail" in msg.lower() or "boom" in msg.lower()
    assert "chat.stream.subagent_failed" in [e.event_type for e in bus.events]


@pytest.mark.asyncio
async def test_deep_research_tool_returns_immediately_and_schedules() -> None:
    scheduled: list[Any] = []
    fake = _FakeAI("ignored")
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake, websearch=object()))
    # Capture the background coro instead of really detaching it.
    svc._run_in_background = lambda coro: scheduled.append(coro) or coro.close()  # type: ignore[assignment]

    out = await svc.execute_tool("deep_research", {"query": "what is X?"})

    assert "research" in out.lower()  # an acknowledgement, not the report
    assert len(scheduled) == 1  # the run was scheduled in the background
    assert fake.calls == []  # the engine was NOT awaited inline
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "research_background or returns_immediately_and_schedules" -q`
Expected: FAIL — `_run_research_background` / `_run_in_background` / `_workspace` don't exist; `execute_tool` still awaits `spawn` inline.

- [ ] **Step 3: Implement the background flow**

In `src/gilbert/core/services/subagent.py`:

(a) Imports — add at the top:

```python
import asyncio
import contextvars

from gilbert.interfaces.ai import ConversationMessagePoster
from gilbert.interfaces.context import set_workspace_conversation_id
from gilbert.interfaces.workspace import WorkspaceProvider
```

(b) In `__init__`, add a workspace slot:

```python
        self._workspace: WorkspaceProvider | None = None
```

(c) In `start()`, after binding `ai_chat` and setting `self._enabled = True`, resolve the optional workspace capability:

```python
        ws = resolver.get_capability("workspace")
        self._workspace = ws if isinstance(ws, WorkspaceProvider) else None
```

(d) Replace the `deep_research` branch in `execute_tool` (which currently does `return await self.spawn("deep-research", query, ...)`) with a detached launch:

```python
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
            parent_conv = get_current_conversation_id()
            caller = get_current_user()
            self._run_in_background(
                self._run_research_background(query, parent_conv, caller)
            )
            return (
                f"🔍 Researching “{query}” in the background — I'll post the "
                "report here when it's ready. You can keep chatting."
            )
```

(e) Add the background helpers (place after `execute_tool`, before `spawn`):

```python
    def _run_in_background(self, coro: Any) -> None:
        """Detach a coroutine as a tracked task, preserving request context."""
        asyncio.create_task(coro, context=contextvars.copy_context())

    async def _run_research_background(
        self,
        query: str,
        parent_conversation_id: str | None,
        user_ctx: UserContext | None,
    ) -> None:
        """Run a deep-research subagent off the parent turn and deliver the
        result into the parent conversation. Never raises — a detached task's
        failure must be delivered, not lost."""
        # Scope workspace writes to the PARENT conversation so the report (and
        # any media the agent saves) is linkable from the user's chat.
        if parent_conversation_id:
            set_workspace_conversation_id(parent_conversation_id)
        try:
            report = await self.spawn("deep-research", query, user_ctx=user_ctx)
            rel_path = await self._write_report(
                parent_conversation_id,
                user_ctx.user_id if user_ctx else "system",
                report,
            )
            if rel_path and parent_conversation_id:
                url = f"/api/chat/download/{parent_conversation_id}/{rel_path}"
                lead = report.strip().split("\n\n", 1)[0][:400]
                message = f"**Research complete.** [Open the report]({url})\n\n{lead}"
            else:
                # No workspace — degrade to delivering the report inline.
                message = f"**Research complete.**\n\n{report}"
            await self._deliver(parent_conversation_id, message)
        except Exception as exc:  # noqa: BLE001 — deliver, don't crash
            logger.exception("Deep research background run failed")
            await self._publish_event(
                "chat.stream.subagent_failed",
                {
                    "conversation_id": parent_conversation_id,
                    "subagent_id": "",
                    "agent_type": "deep-research",
                    "reason": str(exc),
                    "visible_to": [user_ctx.user_id] if user_ctx and user_ctx.user_id else None,
                },
            )
            await self._deliver(
                parent_conversation_id, f"Deep research failed: {exc}"
            )

    async def _write_report(
        self, conversation_id: str | None, user_id: str, content: str
    ) -> str | None:
        """Write the report markdown to outputs/ in the conversation workspace.
        Returns the rel_path, or None when no workspace is available."""
        if self._workspace is None or not conversation_id:
            return None
        filename = f"research-{uuid.uuid4().hex[:8]}.md"
        rel_path = f"outputs/{filename}"
        out_dir = self._workspace.get_output_dir(user_id, conversation_id)
        target = out_dir / filename
        target.write_text(content, encoding="utf-8")
        await self._workspace.register_file(
            conversation_id=conversation_id,
            user_id=user_id,
            category="output",
            filename=filename,
            rel_path=rel_path,
            media_type="text/markdown",
            size=len(content.encode("utf-8")),
            created_by="ai",
            description="Deep research report",
        )
        return rel_path

    async def _deliver(self, conversation_id: str | None, content: str) -> None:
        """Post the result into the parent conversation (best-effort)."""
        if not conversation_id or not isinstance(self._ai, ConversationMessagePoster):
            return
        await self._ai.append_assistant_message(conversation_id, content)
```

Note: `_run_research_background` emits `subagent_started`/`subagent_completed` through the existing `spawn()` (which already publishes them). It adds only the `subagent_failed` emission on the outer failure path (e.g. a failure before/around `spawn`). Keep `spawn`'s own events as-is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (all — the new background tests plus the slice-1–4 ones). The slice-4 `test_deep_research_tool_spawns_deep_research_type` and `..._inherits_current_user` tests assumed `deep_research` returned the report synchronously — UPDATE them: now `execute_tool("deep_research", ...)` returns the acknowledgement and schedules the run, so those two tests should assert the run is scheduled (mirror `test_deep_research_tool_returns_immediately_and_schedules`) rather than awaiting a report. Update them in this step so the suite is green.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: run deep_research in the background and deliver a workspace report

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: deep-research profile + prompt for the markdown report

**Files:**
- Modify: `src/gilbert/core/services/ai.py`
- Modify: `src/gilbert/core/subagents/types.py`
- Test: `tests/unit/test_ai_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_ai_service.py`:

```python
def test_deep_research_profile_includes_workspace_write() -> None:
    from gilbert.core.services.ai import _BUILTIN_PROFILES

    p = next(x for x in _BUILTIN_PROFILES if x.name == "deep-research")
    assert "write_workspace_file" in p.tools
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ai_service.py -k deep_research_profile_includes_workspace_write -q`
Expected: FAIL — `write_workspace_file` not in the profile's tools.

- [ ] **Step 3: Add the tool + extend the prompt**

In `src/gilbert/core/services/ai.py`, in the seeded `deep-research` profile, change the tools list:

```python
        tools=["web_search", "fetch_url", "write_workspace_file"],
```

In `src/gilbert/core/subagents/types.py`, extend `_DEEP_RESEARCH_PROMPT` by appending this sentence to the existing prompt string (before the closing quote):

```
" When you have media (an image, chart, or file) you saved to the workspace, embed it in the report with a relative Markdown link like ![caption](outputs/<file>). Produce the full report as your final message in Markdown — it will be saved as a file and linked into the chat."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ai_service.py -k deep_research_profile_includes_workspace_write -q`
Expected: PASS.

Run: `uv run pytest tests/unit/test_subagent_types.py -q`
Expected: PASS (the prompt still contains "report"/"cit" assertions from slice 4).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/ai.py src/gilbert/core/subagents/types.py tests/unit/test_ai_service.py
git commit -m "deep-research: give the agent write_workspace_file + markdown-report/embed prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `<WorkspaceMarkdownViewer>` (frontend)

**Files:**
- Create: `frontend/src/components/chat/WorkspaceMarkdownViewer.tsx`
- Test: `frontend/src/components/chat/WorkspaceMarkdownViewer.test.tsx`

The viewer fetches a workspace `.md` via the authenticated `/api/chat/download/<conv>/<path>` route, rewrites **relative** embed URLs to absolute `/api/chat/download/<conv>/<dir>/<rel>` so images/links resolve, and renders with `MarkdownContent` inside a `Dialog`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/chat/WorkspaceMarkdownViewer.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { rewriteWorkspaceEmbeds } from "./WorkspaceMarkdownViewer";

describe("rewriteWorkspaceEmbeds", () => {
  const base = "conv-1/outputs/report.md";
  it("rewrites a relative image to the download route", () => {
    const out = rewriteWorkspaceEmbeds("![c](chart.png)", "conv-1", base);
    expect(out).toContain("/api/chat/download/conv-1/outputs/chart.png");
  });
  it("rewrites a relative outputs/ path", () => {
    const out = rewriteWorkspaceEmbeds("![c](outputs/a.png)", "conv-1", base);
    expect(out).toContain("/api/chat/download/conv-1/outputs/a.png");
  });
  it("leaves absolute and http urls untouched", () => {
    const md = "![x](https://e.com/i.png) and ![y](/api/chat/download/conv-1/outputs/z.png)";
    const out = rewriteWorkspaceEmbeds(md, "conv-1", base);
    expect(out).toContain("https://e.com/i.png");
    expect(out).toContain("/api/chat/download/conv-1/outputs/z.png");
    expect(out).not.toContain("/api/chat/download/conv-1/outputs/https");
  });
});

describe("WorkspaceMarkdownViewer", () => {
  it("fetches and renders the report markdown", async () => {
    const { WorkspaceMarkdownViewer } = await import("./WorkspaceMarkdownViewer");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, text: async () => "# Hello report" })),
    );
    render(
      <WorkspaceMarkdownViewer
        open
        conversationId="conv-1"
        path="outputs/report.md"
        onClose={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Hello report/i)).toBeInTheDocument(),
    );
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/assistant/gilbert/frontend && npm run test -- WorkspaceMarkdownViewer`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the viewer**

Create `frontend/src/components/chat/WorkspaceMarkdownViewer.tsx`:

```tsx
import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { MarkdownContent } from "@/components/ui/MarkdownContent";

/**
 * Rewrite relative image/link targets in `md` to the authenticated workspace
 * download route, so embedded workspace media resolves. Absolute URLs (http(s)
 * or already-/api/...) are left untouched. `basePath` is the report's own
 * workspace path (e.g. "conv-1/outputs/report.md"); relatives resolve against
 * its directory.
 */
export function rewriteWorkspaceEmbeds(
  md: string,
  conversationId: string,
  basePath: string,
): string {
  const dir = basePath.includes("/") ? basePath.slice(0, basePath.lastIndexOf("/")) : "";
  // strip the leading "<conv>/" if present so dir is workspace-relative
  const relDir = dir.startsWith(conversationId + "/")
    ? dir.slice(conversationId.length + 1)
    : dir;
  const toUrl = (target: string): string => {
    if (/^(https?:)?\/\//i.test(target) || target.startsWith("/")) return target;
    const cleaned = target.replace(/^\.\//, "");
    const full = cleaned.startsWith("outputs/") || cleaned.startsWith("scratch/") || cleaned.startsWith("uploads/")
      ? cleaned
      : relDir
        ? `${relDir}/${cleaned}`
        : cleaned;
    return `/api/chat/download/${conversationId}/${full}`;
  };
  // ![alt](target) and [text](target)
  return md.replace(/(!?\[[^\]]*\])\(([^)\s]+)([^)]*)\)/g, (_m, label, target, rest) => {
    return `${label}(${toUrl(target)}${rest})`;
  });
}

export function WorkspaceMarkdownViewer({
  open,
  conversationId,
  path,
  onClose,
}: {
  open: boolean;
  conversationId: string;
  path: string;
  onClose: () => void;
}) {
  const [content, setContent] = useState<string>("");
  const [error, setError] = useState<string>("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setContent("");
    setError("");
    const url = `/api/chat/download/${encodeURIComponent(conversationId)}/${path
      .split("/")
      .map(encodeURIComponent)
      .join("/")}`;
    fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((text) => {
        if (!cancelled) setContent(rewriteWorkspaceEmbeds(text, conversationId, `${conversationId}/${path}`));
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [open, conversationId, path]);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{path.split("/").pop()}</DialogTitle>
        </DialogHeader>
        {error ? (
          <p className="text-sm text-rose-400">Couldn't load report: {error}</p>
        ) : (
          <MarkdownContent content={content} />
        )}
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/assistant/gilbert/frontend && npm run test -- WorkspaceMarkdownViewer`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/assistant/gilbert
git add frontend/src/components/chat/WorkspaceMarkdownViewer.tsx frontend/src/components/chat/WorkspaceMarkdownViewer.test.tsx
git commit -m "frontend: WorkspaceMarkdownViewer — renders a workspace .md with embedded media

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Open the viewer from a report link in chat

**Files:**
- Modify: `frontend/src/components/chat/ChatPage.tsx`

The delivered message contains `[Open the report](/api/chat/download/<conv>/outputs/research-*.md)`. Intercept clicks on such links (rendered inside chat markdown) and open the viewer instead of navigating.

- [ ] **Step 1: Add viewer state + a delegated link handler**

In `ChatPage.tsx`, add state near the other `useState`s:

```tsx
  const [reportView, setReportView] = useState<{ conv: string; path: string } | null>(null);
```

Add a delegated click handler (a `useCallback`) that detects clicks on anchors whose href matches the workspace `.md` download route:

```tsx
  const handleChatClick = useCallback((e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") || "";
    const m = href.match(/^\/api\/chat\/download\/([^/]+)\/(.+\.md)$/);
    if (m) {
      e.preventDefault();
      setReportView({ conv: decodeURIComponent(m[1]), path: decodeURIComponent(m[2]) });
    }
  }, []);
```

- [ ] **Step 2: Wire the handler + render the viewer**

Wrap the message-list region with the click handler — add `onClick={handleChatClick}` to the existing scroll container that holds `<MessageList>` (the outer chat column `div`). Then render the viewer near the end of the returned JSX:

```tsx
        {reportView && (
          <WorkspaceMarkdownViewer
            open
            conversationId={reportView.conv}
            path={reportView.path}
            onClose={() => setReportView(null)}
          />
        )}
```

Add the import at the top:

```tsx
import { WorkspaceMarkdownViewer } from "@/components/chat/WorkspaceMarkdownViewer";
```

- [ ] **Step 3: Type-check + build**

Run:
```bash
cd /home/assistant/gilbert/frontend
npm run typecheck
npm run test
npm run build
```
Expected: typecheck clean; vitest green; `vite build` succeeds. (Fix any unused-import/type issues in the files you changed.)

- [ ] **Step 4: Commit**

```bash
cd /home/assistant/gilbert
git add frontend/src/components/chat/ChatPage.tsx
git commit -m "frontend: open the research report in the markdown viewer on link click

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Verification

- [ ] **Step 1: Backend lint/type/tests**

Run:
```bash
cd /home/assistant/gilbert
uv run ruff check src/gilbert/core/services/subagent.py src/gilbert/core/subagents/types.py tests/unit/test_subagent_service.py
uv run mypy src/gilbert/core/services/subagent.py
uv run pytest tests/unit/ -q
```
Expected: ruff clean on these files; mypy `Success`; full suite green. (Pre-existing unrelated `ai.py` lint is out of scope.)

- [ ] **Step 2: Frontend full check**

Run:
```bash
cd /home/assistant/gilbert/frontend
npm run typecheck && npm run test && npm run build
```
Expected: all green.

- [ ] **Step 3: Commit any fixups**

```bash
cd /home/assistant/gilbert
git add -A
git commit -m "deep-research background: lint/format fixups" || echo "nothing to commit"
```

---

## Self-review notes (author check)

- **Spec coverage:** background detach + immediate ack (Task 1); deliver-on-done via `append_assistant_message` + `subagent_*` events (Task 1); report written to the parent workspace, scoped via `set_workspace_conversation_id` (Task 1); failure delivered, never crashes (Task 1); `write_workspace_file` + markdown-report/embed prompt (Task 2); viewer rendering embedded workspace media via `/api/chat/download` rewriting (Task 3); open-from-link wiring (Task 4). `spawn_agent` stays synchronous (untouched). ✓
- **Degradation:** no workspace → report delivered inline; no poster → no delivery (events still fire) — covered in `_deliver`/`_write_report` guards.
- **Type/name consistency:** `_run_research_background(query, parent_conversation_id, user_ctx)`, `_write_report(...) -> str|None`, `_deliver(...)`, `_run_in_background(coro)` are used identically across Task 1 code and tests; the download URL format `/api/chat/download/<conv>/<rel_path>` is identical in backend delivery (Task 1), the viewer rewrite (Task 3), and the click matcher (Task 4); `write_workspace_file` matches the workspace tool name.
- **Known limits (acknowledged):** the click-interception wiring (Task 4) is verified by typecheck/build, not a vitest test (ChatPage isn't unit-harnessed); a detached run dies on restart (spec §12). The viewer's relative-embed rewrite handles the common `outputs/` and same-dir cases (tested); exotic `../` paths are out of scope.
- **No placeholders:** every code step has complete code; every run step has an exact command + expected result.
