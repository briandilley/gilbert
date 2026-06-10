# Supervising Subagents — Design Spec (Slice 6)

- **Date:** 2026-06-09
- **Status:** Draft (for review)
- **Scope:** Make a running subagent *supervisable* — watch it live (read-only), stop it (keeping its partial work), deliver the finished report as a file attachment + notification anchored at its completion time, render `.md` in the viewer with a Rendered/Raw toggle, and upgrade the deep-research prompt. Builds on slices 1–5 (`SubagentService`, background `deep_research`, `<SubagentCard>`, `<WorkspaceMarkdownViewer>`), all on `main`.

## 1. Summary

Six related improvements, backed by a small **run registry** in `SubagentService`:

1. **Watch (read-only, live).** Click a running card to open the subagent's own (hidden) conversation in a read-only live view — you see its searches, page reads, and reasoning stream as they happen.
2. **Stop.** A Stop control gracefully halts the run and **delivers whatever it found so far**.
3. **Stop-time ordering.** While running, the card sits at the bottom (active); on finish/stop it **resolves into an inline message anchored at its completion time** and stays put.
4. **Attachment + notification delivery.** On completion, deliver a message that carries the report as a **workspace file attachment** (clickable chip) plus an in-conversation **link to open it**, and fire a **notification**.
5. **Viewer Rendered/Raw tab.** The markdown viewer gets a **Rendered ⇄ Raw** toggle and reliably renders `.md`.
6. **Prompt upgrade.** Fold the useful framing from Tongyi-DeepResearch's prompt into our (model-agnostic) deep-research prompt.

## 2. Background

From testing the background `deep_research`: the parent model couldn't report on a running agent ("I don't have a way to check"), there was no way to watch or stop it, the completion arrived as a plain link rather than an attachment, and the `.md` wasn't openable as a rendered document with a raw view. This slice closes those gaps.

## 3. Goals / non-goals

**Goals:** watch a run read-only/live; stop a run and deliver its partial result; completion lands by stop-time and persists; report delivered as an attachment + notification + link; viewer renders `.md` with a raw tab; richer deep-research prompt. Applies to **user-facing `deep_research`** runs (the background path); `spawn_agent` (synchronous internal subtasks) is unaffected.

**Non-goals (v1):** editing/replying inside the watched subagent conversation (read-only); resuming a stopped run; persisting runs across a Gilbert restart (a detached run still dies on restart — a stop on a now-dead run is a no-op); a global "all running tasks" dashboard (the per-conversation cards + the model's awareness suffice); pause/resume.

## 4. Architecture — the run registry

`SubagentService` keeps an in-memory registry of background runs, keyed by `subagent_id`:

```python
@dataclass
class _Run:
    subagent_id: str
    agent_type: str
    query: str
    conversation_id: str        # the subagent's own (pre-allocated) ephemeral conv
    parent_conversation_id: str | None
    user_id: str
    status: str                 # "running" | "completed" | "stopped" | "failed"
    started_at: str
    stop_flag: list[bool]       # [False]; set [0]=True to request a graceful stop
    task: asyncio.Task | None   # the detached run (also the GC ref, replaces _background_tasks)
```

The registry backs every feature: **watch** uses `conversation_id`; **stop** sets `stop_flag` (and is a no-op if the run is gone); **ordering/delivery** uses `parent_conversation_id`; and the model's status awareness (a `list_subagents`/`check_research` tool + a system-prompt line) reads it. Entries are kept briefly after completion (for status/late watch), pruned on a cap (e.g. last 20).

## 5. Components

### 5.1 Pre-allocated conversation + lifecycle events (backend)

- The background flow **pre-allocates** the subagent's `conversation_id` (`uuid4`) and `subagent_id`, registers a `_Run`, and passes both into `spawn()`.
- `spawn()` gains optional `conversation_id`, `subagent_id`, and `should_stop` params (defaults preserve `spawn_agent`'s behavior). It passes `conversation_id` to `AIService.chat` (which uses a provided id directly — `ai.py` `if conversation_id: … else: uuid4()`), and `should_stop_callback=should_stop` so a stop returns the partial `response_text`.
- `chat.stream.subagent_started` now includes `conversation_id` (the subagent's) and `query`, so the card can open the watch view. `subagent_completed`/`subagent_failed` gain a `status` and the report attachment ref.

### 5.2 Watch — read-only live view (frontend)

- A new **`<SubagentLiveViewer>`** modal: given the subagent's `conversation_id`, it `loadConversation(id)` (read-only — no input), renders turns via the existing `<MessageList>`/`<TurnBubble>` (the thinking-card already shows tool calls + reasoning), and **subscribes to that conversation's `chat.stream.*` events filtered by the watched id** (its own ref, independent of `activeConvId`) so it streams live without changing the user's active chat.
- The `<SubagentCard>` becomes clickable while running (and after) → opens the viewer for `subagent.conversation_id`.

### 5.3 Stop — graceful, partial-preserving (backend + frontend)

- A `subagent.stop` WS RPC (and a small `stop_subagent(subagent_id)` service method): looks up the run, sets `stop_flag[0] = True`. `chat()` breaks at the next round boundary and returns the accumulated partial in `response_text`. (No hard `task.cancel()` — graceful, so we keep the partial.)
- The background flow detects the stopped state (the run's `stop_flag`), marks `status="stopped"`, writes the **partial** report to the workspace, and delivers it labelled as stopped-early ("Research stopped — here's what it found so far").
- Frontend: a **Stop** button on the running `<SubagentCard>` calls the RPC; the card → "Stopping…" → "Stopped".

### 5.4 Stop-time ordering + delivery as attachment + notification (backend + frontend)

- **Delivery** uses an extended `AIService.append_assistant_message(conversation_id, content, attachments=None)`: it persists the attachments on the message row and includes them in the `chat.message.created` event's `attachments`. The report is delivered as a `FileAttachment(kind="text", workspace_skill="workspace", workspace_path="outputs/research-<id>.md", workspace_conv=parent_conv, media_type="text/markdown", name=…)` plus a short text body with an "Open the report" link.
- Because delivery is a real message created at completion time, it **anchors at stop-time** in the chat and stays there. On `subagent_completed`/`_failed`/`_stopped`, the frontend **removes the run from the active-cards list** (`useActiveSubagents`), so the floating bottom card cleanly gives way to the in-place message (no duplicate/flicker).
- **Notification:** resolve `NotificationProvider` (capability `notifications`) and `notify_user(user_id=…, message="Deep research complete: <query>", source="subagent", source_ref={conversation_id, subagent_id, report_path})`.
- **Frontend attachment open:** the message's `<AttachmentChip>` is given an `onOpen` that, for a `text/markdown` workspace attachment, opens `<WorkspaceMarkdownViewer>` (same as clicking the link).

### 5.5 Viewer Rendered/Raw tab (frontend)

- `<WorkspaceMarkdownViewer>` wraps its body in the existing `Tabs` (`variant="line"`): **Rendered** (`<MarkdownContent>` + the slice-5 embed rewriting) and **Raw Markdown** (`<pre>` with the raw text). `.md` files open through this viewer from links and attachment chips alike.

### 5.6 Model status awareness + the prompt upgrade (backend)

- A **`check_research` tool** (ai-visible, also `/research-status`) lists the caller's recent/active runs from the registry (type, query, status, elapsed, and the report path when done) so the model can answer "how's that agent doing?" accurately. (This is the smaller, on-demand half of the earlier status-awareness idea; a system-prompt line is optional follow-up.)
- The **deep-research prompt** (`deep_research_system_prompt` ConfigParam) gains: handle both broad/open-domain and specialized/academic questions; rely on credible, diverse sources and stay objective; and a page-reading discipline borrowed from Tongyi's extractor — *"when you read a page, extract the most relevant evidence while preserving its full original context, and weigh how much it actually answers the question before moving on."* The `<answer>`-tag / `<tool_call>` wire format from their repo is **not** adopted (model-specific; we use native tools + a final markdown message).

## 6. Data flow (watch + stop)

1. `/research …` → background flow pre-allocates `subagent_id` + ephemeral `conv_id`, registers the run, emits `subagent_started{conversation_id, query}`. Card shows "Running" + a **Watch** and **Stop** control.
2. **Watch:** click the card → `<SubagentLiveViewer>` loads `conv_id` and streams its rounds/tools live, read-only.
3. **Stop:** click Stop → `subagent.stop` sets `stop_flag`. `chat()` halts next round, returns the partial. Flow writes the partial report, delivers "Research stopped — partial findings" + the `.md` attachment, notifies, marks `status="stopped"`; the card resolves into the in-place message.
4. **Normal completion:** flow writes the report, delivers a message with the report **attachment** + link + notification; the active card is removed; the message stays anchored at completion time.

## 7. Configuration

- `deep_research_system_prompt` (existing ai-prompt ConfigParam) updated with the §5.6 additions.
- No new config required; the registry cap (e.g. 20) is a constant.

## 8. Error handling

- Stop on a missing/finished run → no-op (idempotent). Stop on a run whose task already died (restart) → no-op; the card may show stale "running" until pruned — acceptable.
- Delivery/notification failures are swallowed (slice-5 `_deliver` already try/excepts; notify wrapped likewise) — never escape the detached task.
- Watch on a conversation that 404s (pruned/deleted) → the viewer shows a friendly "this run is no longer available."
- The detached run still never raises out (slice-5 contract preserved); a stopped run is a normal, delivered outcome, not a failure.

## 9. Security / RBAC / isolation

- The watched conversation is the subagent's, owned by the caller; `loadConversation`/the download route enforce ownership — a user can only watch/stop/open their own runs. The `subagent.stop` RPC checks the run's `user_id` against the caller.
- Registry entries are keyed per run and carry `user_id`; `check_research` filters to the caller. No cross-user leakage. Per-run state lives in the registry, not as shared singleton fields beyond the registry dict (which is keyed/owned).

## 10. Testing

Backend (pytest, fakes):
- Pre-alloc: `spawn` uses a provided `conversation_id`; `subagent_started` carries it + `query`.
- Stop: a `stop_flag` set true makes `chat` (fake honoring `should_stop_callback`) return partial; the flow delivers a "stopped" message + attachment and marks `status="stopped"`; stop on unknown id is a no-op; stop checks `user_id`.
- Delivery: `append_assistant_message` carries attachments into the event; the report attachment has the right `workspace_path`/`media_type`; notification fired with the right `source_ref`.
- `check_research` lists the caller's runs with status; filters by user.
- Registry pruning at the cap.

Frontend (vitest + RTL):
- `<SubagentLiveViewer>` loads a conv (mocked) and renders turns; live event for the watched id updates it; events for other ids don't.
- Rendered/Raw tab toggles between `MarkdownContent` and the raw `<pre>`.
- `AttachmentChip` `onOpen` for a `.md` reference opens the viewer.
- `useActiveSubagents` removes a run on `completed`/`stopped`/`failed`.

## 11. Build order

1. **Backend: registry + pre-alloc + lifecycle events** (the backbone) — `_Run`, pre-allocated conv/id, `spawn()` optional params, enriched events. (Replaces the slice-5 `_background_tasks` set with the registry's task refs.)
2. **Backend: stop** — `stop_subagent` + `subagent.stop` RPC + `should_stop` wiring; the flow's stopped-path delivery.
3. **Backend: delivery as attachment + notification + `check_research`** — extend `append_assistant_message(attachments=…)`; deliver the report attachment; notify; the status tool; the prompt upgrade.
4. **Frontend: `<SubagentLiveViewer>` + Watch/Stop on the card** + active-card removal on terminal status.
5. **Frontend: viewer Rendered/Raw tab + AttachmentChip `onOpen` → viewer.**
6. **Verification.**

## 12. Open questions (defaults set; confirm at review)

1. **Watch view form:** modal overlay (default) vs. a side sheet.
2. **Stop label on the partial report:** "Research stopped early — here's what it found so far" (default).
3. **`check_research` exposure:** an AI tool the model calls on demand (default) now; a proactive system-prompt line is a small follow-up.
4. **Registry persistence:** in-memory only (default; lost on restart, consistent with detached-task v1).

## 13. Out of scope / future

- Replying/branching inside a watched run; resume of a stopped run; cross-restart persistence of runs; a global running-tasks dashboard; background execution + supervision for `spawn_agent`; an image-download-to-workspace tool.

## 14. Architecture-rules compliance

- Capabilities resolved via the resolver (`ai_chat`/`ConversationMessagePoster`, `workspace`, `event_bus`, `notifications`) — no concrete cross-imports.
- The new tool declares `required_role`; prompts stay `ConfigParam(ai_prompt=True)`.
- Multi-user isolation: the registry is keyed per run with `user_id`; RBAC checks on stop/watch.
- Frontend is core chat UI; reuses `MessageList`/`TurnBubble`/`MarkdownContent`/`Tabs`/`AttachmentChip`; new tests use the slice-3 vitest harness.
- Docs: update the deep-research/subagent notes; the watch/stop/attachment behavior is user-visible.
