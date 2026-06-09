# Deep Research — Background Run + Workspace Report — Design Spec

- **Date:** 2026-06-09
- **Status:** Draft (for review)
- **Scope:** "Slice 5" — a follow-up to the subagent engine (slices 1–4, on `main`). Makes the user-facing `deep_research` run **in the background** (so the user keeps chatting) and deliver its result as a **workspace markdown report** that opens in a viewer and renders embedded workspace media.

## 1. Summary

Two paired improvements to the `deep_research` capability:

1. **Background execution + delivery.** `deep_research` returns immediately ("researching… I'll post it when ready"), runs detached so the parent turn completes and the user can keep chatting, and **delivers the finished report into the conversation** when done (live, via the existing message-created path). The live `<SubagentCard>` (slice 3) flips running → done.
2. **Report as a workspace markdown file.** The full report is written as a `.md` file in the conversation's workspace and surfaced as a link that opens a **markdown viewer**; the viewer renders **embedded workspace files** (images, etc.) so a self-contained report can be opened and read separately from the chat.

This addresses two pieces of feedback: "I should be able to keep talking to the parent while a subagent runs," and "attach the report as a markdown file… support embedding images/files from the workspace."

## 2. Goals / non-goals

**Goals:**
- `deep_research` is non-blocking: the parent turn completes immediately; the user can keep chatting.
- The finished report is delivered into the *parent* conversation live (no reload), with a link to open the full report.
- The report is a `.md` file in the parent conversation's workspace, openable in a viewer that renders embedded workspace media (`![alt](…workspace file…)`).
- Failures are delivered into the conversation (never a silent background crash).
- The live subagent card continues to reflect running → done/failed.

**Non-goals (v1 boundaries):**
- **`spawn_agent` (general-purpose) stays synchronous.** Only `deep_research` goes background — when the AI spawns a general subtask mid-reasoning it needs the result in-turn. (Decision; confirm at review.)
- **No dedicated image-download-to-workspace tool.** The agent gets `write_workspace_file` (text/markdown + small files); systematically downloading binary images into the workspace is a follow-up. v1 *renders* embedded workspace media and lets the agent save/reference files it produces.
- **No streamed per-round progress** into the chat — just started → delivered/failed (the card already shows "running").
- **No new background-job UI** (no "running tasks" panel) — the live card + the delivered message suffice for v1.

## 3. Architecture

```
/research <q>  OR  AI calls deep_research(query)
  └─ SubagentService.execute_tool("deep_research")
       ├─ capture parent_conv_id (get_current_conversation_id) + caller (get_current_user)
       ├─ spawn a DETACHED task: asyncio.create_task(_run_research_bg(...),
       │      context=contextvars.copy_context())
       └─ RETURN IMMEDIATELY: "🔍 Researching '<q>' — I'll post the report here when it's ready."
            (parent turn completes; user keeps chatting)

_run_research_bg(query, parent_conv_id, caller):   # detached
   set _workspace_conversation_id = parent_conv_id   # writes land in the PARENT workspace
   emit chat.stream.subagent_started (already exists; card shows running)
   try:
     report_md = await self.spawn("deep-research", query, user_ctx=caller)   # the existing engine
     path = workspace.write(parent_conv_id, "outputs/research-<id>.md", report_md)  # the .md file
     url  = f"/api/chat/download/{parent_conv_id}/{path}"
     await poster.append_assistant_message(parent_conv_id,
              f"**Research complete.** [Open the report]({url})\n\n{lead_in(report_md)}")
     emit chat.stream.subagent_completed
   except Exception as e:
     await poster.append_assistant_message(parent_conv_id, f"Deep research failed: {e}")
     emit chat.stream.subagent_failed
```

The engine (`spawn()`) is unchanged and stays synchronous; only the `deep_research` **tool handler** detaches it. `spawn_agent` keeps calling `spawn()` inline.

## 4. Components

### 4.1 Background `deep_research` (backend — `SubagentService`)

- `execute_tool("deep_research")`: validate query + web-search availability (as today), then **kick off a detached task** (`asyncio.create_task(self._run_research_background(...), context=contextvars.copy_context())`) and return a short acknowledgement string (the tool result). It no longer awaits `spawn()`.
- `_run_research_background(query, parent_conversation_id, user_ctx)`:
  - Sets the workspace-conversation override (`set_workspace_conversation_id(parent_conversation_id)`) so any workspace writes during the run (the agent's media + the report file) land in the **parent** conversation's workspace.
  - Runs `await self.spawn("deep-research", query, user_ctx=user_ctx)` (emits the started/completed events as today, scoped to the parent conversation).
  - Writes the returned report markdown to `outputs/research-<subagent_id>.md` in the parent workspace via the **`workspace`** capability (`WorkspaceProvider` — resolved at `start()`, optional).
  - Delivers the result into the parent conversation via the **`ConversationMessagePoster`** capability (`append_assistant_message`) — a concise message with a link to the report (`/api/chat/download/<parent_conv>/outputs/research-<id>.md`) plus a short lead-in.
  - **Catches all exceptions** and delivers a failure message + `subagent_failed` event; a detached research run must never crash silently.
- New optional capabilities resolved at `start()`: `workspace` (write the file) and the AI service as `ConversationMessagePoster` (deliver the message). Both degrade gracefully: if `workspace` is absent, deliver the report inline in the message; if the poster is absent, log + emit the completed event only.

### 4.2 The `deep-research` profile gains `write_workspace_file`

- Add `write_workspace_file` to the seeded "Deep Research" profile's include-list: `tools=["web_search", "fetch_url", "write_workspace_file"]`. This lets the agent save intermediate artifacts/media it wants to embed.
- Update the research system prompt to: produce a thorough cited report **in Markdown**, and when it references an image/file it has saved to the workspace, embed it with a relative markdown link (`![caption](outputs/<file>)`); the final message is the report markdown itself. (Prompt stays a configurable `ConfigParam(ai_prompt=True)` — slice 1's per-type prompt.)

### 4.3 Report file + delivery message

- The report file: `outputs/research-<subagent_id>.md` in the parent conversation's workspace, registered via `register_file` (media type `text/markdown`).
- Embedded media: markdown image/file links are rewritten by the **viewer** (4.4) to `/api/chat/download/<parent_conv>/<path>` so they resolve against the parent workspace.
- Delivery: `append_assistant_message(parent_conv_id, message)` where `message` = a one-line "Research complete — [Open the report](url)" + a short lead-in (first ~1–2 sentences/paragraph of the report). The full report lives in the file, not dumped inline.

### 4.4 Markdown viewer with workspace embeds (frontend)

- A **`<WorkspaceMarkdownViewer>`** (modal or route) that, given a conversation id + a workspace `.md` path: fetches the file (`/api/chat/download/<conv>/<path>`), renders it with the existing `MarkdownContent`, and **resolves embedded workspace references** — rewrites relative image/link URLs (`outputs/chart.png`) to `/api/chat/download/<conv>/<path>` before rendering, so `<img>`/`<a>` resolve against the workspace (DOMPurify already permits these and keeps same-origin links in-tab).
- Entry point: the delivered chat message's "Open the report" link (and/or the report file appearing as a workspace attachment) opens the viewer. The link is same-origin, so clicking opens the viewer route/modal rather than navigating away.
- Reuse the existing image-fetch precedent (`AttachmentChip` downloads workspace files as `data:` URLs) where a direct `<img src="/api/chat/download/…">` isn't sufficient (auth) — i.e. the viewer resolves embedded images by fetching them through the authenticated download path, same as attachments do today.

## 5. Data flow (one background `/research`)

1. User types `/research best home battery 2026`. The `deep_research` tool fires.
2. Handler captures the parent conversation + user, spawns the detached task, returns "🔍 Researching… I'll post it here when it's ready." The parent turn ends — the user can send more messages.
3. The detached task sets the workspace override to the parent conversation, emits `subagent_started` (card shows "Running"), and runs the deep-research subagent (web_search → fetch_url → … producing a cited markdown report; saving any media to the parent workspace).
4. On completion: write `outputs/research-<id>.md`; deliver "**Research complete.** [Open the report](…)\n\n<lead-in>" via `append_assistant_message` (appears live in the chat); emit `subagent_completed` (card → "Done").
5. User clicks "Open the report" → `<WorkspaceMarkdownViewer>` renders the full `.md`, with embedded workspace images resolved and displayed.

## 6. Configuration

- The deep-research system prompt (existing `deep_research_system_prompt` ai-prompt ConfigParam) gains the "save/embed media + produce a markdown report" guidance.
- `SubagentService` config: optionally a `deep_research_background` boolean (default **true**) so an operator could force synchronous behavior; and the report filename/category are conventions (not configurable in v1).

## 7. Error handling

- The detached task wraps the whole run in try/except; any failure → a delivered failure message + `subagent_failed`. No unhandled task exceptions.
- Budget: the subagent's existing round/wall-clock budget applies; a budget stop delivers the partial report.
- If `workspace` capability is missing: deliver the report inline in the chat message (degrade, don't fail).
- If the message-poster is missing: log + emit `subagent_completed` (the card still resolves); the report file still exists.
- The parent turn never blocks on the research, regardless of outcome.

## 8. Security / RBAC / isolation

- The detached task captures and uses the caller's `UserContext` (via `copy_context()` + explicit `user_ctx`) — RBAC unchanged; the research can't exceed the caller's permissions.
- Workspace writes are scoped to the parent conversation the caller owns; the download route already enforces conversation access.
- Per-run state (subagent id, conversation id) stays local to the task — no singleton state (isolation rules).
- The `write_workspace_file` size cap (512 KiB) and path-traversal checks already in the workspace service apply.

## 9. Testing

Backend (pytest, fakes):
- `deep_research` returns immediately and **schedules** a background task (don't await it); assert the tool result is the acknowledgement and that `spawn` is invoked off-thread (inject a fake task-runner / await the created task in the test).
- The background flow: with a fake AI (returns report text), a fake workspace, and a fake `ConversationMessagePoster`, assert it writes `outputs/research-*.md` to the **parent** conversation, calls `append_assistant_message(parent_conv, …)` with a link, and emits `subagent_completed`.
- Failure path: a raising `spawn` → `append_assistant_message` with an error + `subagent_failed`; the task never raises out.
- Degradation: no workspace → report delivered inline; no poster → completed event only.
- The deep-research profile includes `write_workspace_file`.

Frontend (vitest + RTL):
- `<WorkspaceMarkdownViewer>` renders markdown and rewrites a relative embed (`outputs/x.png`) to the `/api/chat/download/<conv>/outputs/x.png` URL; an embedded image element appears.
- The delivered message's report link opens the viewer.

## 10. Build order

1. **Backend background + delivery** — detach `deep_research`; `_run_research_background` (workspace scoping, report write, `append_assistant_message`, events, error handling). Resolve `workspace` + poster capabilities. Tests with fakes.
2. **Profile + prompt** — add `write_workspace_file` to the deep-research profile; update the research prompt for markdown-report + embed guidance.
3. **Frontend viewer** — `<WorkspaceMarkdownViewer>` + embed URL rewriting + open-from-link; vitest tests.
4. **Wire the report link** in the delivered message / attachment to open the viewer.

## 11. Open questions / decisions (for review)

1. **Background scope:** `deep_research` only (background), `spawn_agent` stays sync. **Default: yes.** Confirm.
2. **Lead-in content:** the delivered chat message includes a short lead-in (first paragraph) vs. just the link. **Default: link + first paragraph.**
3. **Report ownership:** the handler writes the report file from the agent's final message (reliable), and the agent additionally saves *media* via `write_workspace_file`. **Default: yes** (handler owns the report file; agent owns embedded media).
4. **Viewer form:** modal overlay vs. dedicated route. **Default: modal** (stays in chat context); revisit if reports want their own URL.

## 12. Out of scope / future

- Background execution for `spawn_agent` / a general "run this subagent in the background" affordance.
- A dedicated image/file-download-to-workspace tool for the agent (richer embedded media).
- A "running tasks" panel; cancel/resume of background runs; persistence of background runs across restarts (a detached `asyncio.task` dies on restart — acceptable for v1; the spec notes it).
- Scholar/sandbox research tools.

## 13. Architecture-rules compliance

- Background task uses `contextvars.copy_context()` (isolation rules) and inherits identity; no singleton request state.
- Capabilities resolved via the resolver (`workspace`, `ai_chat`/`ConversationMessagePoster`, `event_bus`) — no concrete cross-imports.
- Prompts remain `ConfigParam(ai_prompt=True)`.
- Frontend viewer is core chat UI (lives in core `frontend/src/`), reuses `MarkdownContent`; embeds resolved through the existing authenticated download route.
- Docs: update the deep-research notes; the report-as-file behavior is user-visible — note in the relevant docs.
