# Autonomous Agent Service — Design

**Status:** Draft for implementation planning
**Date:** 2026-05-03
**Inspired by:** [OpenClaw](https://openclaw.ai/) — adapted for Gilbert's architecture

## Overview

Add an autonomous agent capability to Gilbert: persistent goals that run AI tool-use loops on heartbeats, schedules, or events, with cross-run memory, configurable budgets, live-tailing, and a notification surface for getting the user's attention.

The design is intentionally a minimal first cut. Scope explicitly excludes self-authoring plugins, multi-chat-platform surfaces, and the broader "50+ integrations" footprint OpenClaw advertises — those are downstream questions once the core loop is real.

## Decisions Summary

The shape of the system is determined by these choices made during brainstorming:

1. **Unified goal model.** One `Goal` entity supports three trigger sources (collapsed to `TIME | EVENT` after the design pass).
2. **Profile-bound authorization.** Each goal binds to an existing AI profile; tools and RBAC inherit from there. An `approval_required` opt-in remains a future flag.
3. **Termination via `END_TURN` + budgets + `complete_goal` tool.** Per-run termination is the chat-loop pattern plus wall-clock and token budgets; per-goal completion is an agent-callable tool.
4. **Cross-run memory: notes + auto-digest.** Per-goal scratchpad notes (`note_add`/`note_list`/`note_delete`) plus an auto-summarized digest of recent runs included in the system prompt.
5. **Output: activity stream + `notify_user`.** Each run is visible in a per-goal activity stream; explicit attention-getting via a `notify_user` tool with badge + WebSocket ping/flash.
6. **Owner + `notify_user_ids` for fan-out.** Owner is auth context; notifications can fan out to multiple users.
7. **Approach 3 — shared `run_loop` primitive.** Refactor `AIService.chat()` to use a new `core/agent_loop.py` primitive; the new agent service uses the same primitive. One loop body, two consumers.
8. **Materialized per-goal conversation.** Each goal has a real chat conversation; runs append messages to it. Live-tailing reuses existing chat WS streaming; user input into the conversation is consumed by the next run.
9. **`<ConversationView>` extraction.** The message-rendering portion of the chat UI is lifted into a reusable component used by `/chat` and `/agents/:id`. Composer and header stay surface-specific.
10. **Workspace per goal.** Each goal inherits a workspace via its conversation_id, using existing `WorkspaceProvider` infrastructure.

## Architecture

Three new pieces plus one careful refactor:

```
src/gilbert/core/agent_loop.py             # NEW — pure async loop primitive
src/gilbert/core/services/agent.py         # NEW — AutonomousAgentService
src/gilbert/core/services/notifications.py # NEW — NotificationService
src/gilbert/core/services/ai.py            # REFACTOR — chat() delegates to agent_loop
```

**Layering** (per CLAUDE.md rules):
- `core/agent_loop.py` imports `interfaces/` only. Pure function, no service deps. Unit-testable against a fake `AIBackend`.
- `core/services/agent.py` imports `interfaces/` and `core/agent_loop`. Never imports `web/` or specific integrations.
- `core/services/notifications.py` imports `interfaces/` only.
- `core/services/ai.py` (refactored) gains a dep on `core/agent_loop`.
- A web-layer subscriber in `web/` listens for `notification.received` events and pushes WS frames to the recipient's connections.
- Agent built-in tools (`note_*`, `notify_user`, `complete_goal`) are constructed per-run as closures over `goal` and `run`. They are **not** registered with the global `ToolProvider` registry.

## The `run_loop` Primitive

A pure async function in `core/agent_loop.py` that drives one tool-use loop. Used by both `AIService.chat()` (after refactor) and `AutonomousAgentService.run_goal()`.

```python
async def run_loop(
    *,
    backend: AIBackend,
    system_prompt: str,
    messages: list[Message],
    tools: dict[str, tuple[ToolDefinition, ToolHandler]],
    max_rounds: int,
    max_wall_clock_s: float | None,
    max_tokens: int | None,
) -> LoopResult: ...

@dataclass
class LoopResult:
    final_message: Message
    full_message_history: list[Message]   # initial + assistant + tool results, for persistence
    stop_reason: LoopStopReason            # END_TURN | MAX_ROUNDS | WALL_CLOCK | TOKEN_BUDGET | ERROR
    rounds_used: int
    tokens_in: int
    tokens_out: int
    error: Exception | None
```

The loop streams from `backend.generate_stream()`; on `MESSAGE_COMPLETE`, if the stop reason is tool use, it executes tools (in parallel when `backend.capabilities().parallel_tool_calls` is true), appends results, and iterates. Termination conditions (checked between rounds, never mid-tool-call): END_TURN, max_rounds, wall-clock deadline, cumulative token budget, or unrecoverable error.

Streaming and persistence: rather than callbacks, the loop writes through the same conversation-persistence path that `chat()` uses today (this path is the artifact of the refactor; details land in the implementation plan). One streaming pipeline serves both consumers.

### Profile resolution (shared helper)

A shared helper, defined alongside the existing AI service code, materializes a profile into the args `run_loop` needs:

```python
async def resolve_profile(
    profile_id: str,
    user_ctx: UserContext,
    resolver: CapabilityResolver,
    *,
    extra_tools: dict[str, tuple[ToolDefinition, ToolHandler]] | None = None,
    system_prompt_overrides: list[str] | None = None,
) -> ResolvedProfile: ...

@dataclass
class ResolvedProfile:
    backend: AIBackend
    system_prompt: str
    tools: dict[str, tuple[ToolDefinition, ToolHandler]]
    max_rounds: int
```

The resolver looks up the backend by `profile.backend_name`, filters tools by RBAC + profile include/exclude lists, and assembles the system prompt. `AgentService` passes its built-ins via `extra_tools` and its goal-specific prompt sections via `system_prompt_overrides`. `run_loop` itself never touches the profile system.

## Entity Data Model

Four new collections.

### `agent_goals`

```python
@dataclass
class Goal:
    id: str
    owner_user_id: str          # auth context: RBAC + profile + user_memory inherit from this user
    notify_user_ids: list[str]  # fan-out for notify_user; defaults to [owner_user_id]
    name: str
    instruction: str            # "what to do, how you know you're done" — user-authored
    profile_id: str

    trigger_type: TriggerType   # TIME | EVENT
    trigger_config: dict        # shape depends on trigger_type

    status: GoalStatus          # ENABLED | DISABLED | COMPLETED
    recent_runs_digest: str     # rolling AI-generated summary of last N runs

    conversation_id: str        # materialized per-goal conversation in the chat store

    max_rounds_override: int | None
    max_wall_clock_s_override: float | None
    max_tokens_override: int | None

    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None
    last_run_status: RunStatus | None
    run_count: int
```

`trigger_config` shapes:
- **TIME**: `{"kind": "interval"|"daily_at"|"hourly_at", "seconds"?: int, "hour"?: int, "minute"?: int, "window_start"?: time, "window_end"?: time}` — wraps `Schedule.every()` / `Schedule.daily_at()` / `Schedule.hourly_at()`.
- **EVENT**: `{"event_type": str, "filter"?: {"field": str, "op": str, "value": Any}}` — subscribes to one event type with an optional simple field/op/value filter on `event.data`.

Indexes: `(status, owner_user_id)`, `trigger_type`.

### `agent_runs`

```python
@dataclass
class Run:
    id: str
    goal_id: str
    triggered_by: TriggerKind   # TIME | EVENT | MANUAL
    trigger_context: dict       # for EVENT runs, the source event data

    started_at: datetime
    ended_at: datetime | None
    status: RunStatus           # RUNNING | COMPLETED | FAILED | TIMED_OUT | TOKEN_BUDGET_EXCEEDED | MAX_ROUNDS

    start_message_id: str       # points into goal.conversation_id; demarcates this run's message range
    final_message_text: str | None

    rounds_used: int
    tokens_in: int
    tokens_out: int
    tool_calls_count: int

    complete_goal_called: bool
    complete_reason: str | None # captured from complete_goal(reason)

    error: str | None
```

`messages` are stored in the goal's conversation, not inline on the Run. The Run holds the `start_message_id` pointer to reconstruct "which messages belong to which run" on demand.

Indexes: `(goal_id, started_at desc)`, `status`.

### `agent_notes`

```python
@dataclass
class Note:
    id: str
    goal_id: str
    content: str
    tags: list[str]
    created_at: datetime
    updated_at: datetime
```

Indexes: `(goal_id, tags)`.

### `notifications`

Owned by `NotificationService`, not `AgentService` — usable by anything that wants to ping a user.

```python
@dataclass
class Notification:
    id: str
    user_id: str
    source: str                 # "agent" | "scheduler" | "ingest" | ...
    source_ref: dict | None     # e.g., {"goal_id": ..., "run_id": ...}
    message: str
    urgency: NotificationUrgency  # INFO | NORMAL | URGENT
    created_at: datetime
    read: bool
    read_at: datetime | None
```

Indexes: `(user_id, read, created_at desc)`.

## Triggers and Wiring

`AutonomousAgentService.start()` re-arms all enabled goals on startup. Goal CRUD calls `_disarm_trigger` then `_arm_trigger` on update; arms on enable; disarms on disable / delete / complete.

### TIME triggers — via `SchedulerService`

```python
match trigger_config["kind"]:
    case "interval":
        schedule = Schedule.every(seconds=trigger_config["seconds"])
    case "daily_at":
        schedule = Schedule.daily_at(trigger_config["hour"], trigger_config["minute"])
    case "hourly_at":
        schedule = Schedule.hourly_at(trigger_config["minute"])

self._scheduler.add_job(
    name=f"agent_goal_{goal.id}",
    schedule=schedule,
    callback=lambda g=goal.id: self._on_trigger_fired(g, TriggerKind.TIME, {}),
    system="agent",
    owner=goal.owner_user_id,
)
```

### EVENT triggers — via `EventBus.subscribe`

```python
async def _on_event(self, event: Event, goal_id: str) -> None:
    goal = self._get_goal(goal_id)
    if not goal or goal.status != GoalStatus.ENABLED:
        return
    if not self._event_matches_filter(event, goal.trigger_config.get("filter")):
        return
    self._spawn_run(goal, TriggerKind.EVENT, trigger_context=event.data)
```

The event handler **must not** await the run. `EventBus.publish` awaits all handlers; blocking handlers block the publisher (per CLAUDE.md feedback).

### MANUAL — via WS RPC `agent.goal.run_now`

UI button → `agent.goal.run_now({goal_id})` → `_on_trigger_fired(goal_id, TriggerKind.MANUAL, {})`. Same path as scheduled and event triggers.

### Concurrency: skip-while-running

If a trigger fires while a previous run for the same goal is in flight, the new tick is **skipped** (logged, no Run entity created). One `set[goal_id]` of currently-running goals; entry added before `asyncio.create_task(...)`, removed in the task's `finally`.

### Restart safety

In-flight runs are orphaned by a process restart. At startup, `AutonomousAgentService.start()` scans `agent_runs` for `status == RUNNING` with `started_at` older than `(max_wall_clock_s × 2)`, marks them `FAILED` with `error="process_restarted"`, and continues. Re-arming triggers is the same code path used at create-time.

## Run Lifecycle (`_run_goal`)

```python
async def _run_goal(
    self,
    goal: Goal,
    trigger_kind: TriggerKind,
    trigger_context: dict,
) -> Run:
    run = Run(
        id=uuid(), goal_id=goal.id,
        triggered_by=trigger_kind, trigger_context=trigger_context,
        started_at=now(), status=RunStatus.RUNNING,
        start_message_id="", rounds_used=0, tokens_in=0, tokens_out=0,
        tool_calls_count=0, complete_goal_called=False,
    )
    # Append a synthetic marker message to the goal's conversation
    run.start_message_id = await self._append_run_marker(goal, run, trigger_kind, trigger_context)
    await self._save_run(run)
    await self._event_bus.publish(Event("agent.run.started", {"goal_id": goal.id, "run_id": run.id}))

    try:
        owner_ctx = self._user_ctx_for(goal.owner_user_id)
        resolved = await resolve_profile(
            profile_id=goal.profile_id,
            user_ctx=owner_ctx,
            resolver=self._resolver,
            extra_tools=self._agent_tools(goal, run),
            system_prompt_overrides=await self._agent_prompt_parts(goal, trigger_kind, trigger_context),
        )

        # Initial user message: pulls goal instruction, recent user input from the conversation,
        # and trigger-specific framing
        initial_messages = [
            Message(role=USER, content=await self._initial_user_message(goal, trigger_kind, trigger_context)),
        ]

        result = await run_loop(
            backend=resolved.backend,
            system_prompt=resolved.system_prompt,
            messages=initial_messages,
            tools=resolved.tools,
            max_rounds=goal.max_rounds_override or self._default_max_rounds,
            max_wall_clock_s=goal.max_wall_clock_s_override or self._default_max_wall_clock_s,
            max_tokens=goal.max_tokens_override or self._default_max_tokens,
        )

        run.status = self._map_loop_stop_reason(result.stop_reason)
        run.final_message_text = result.final_message.content_text()
        run.rounds_used = result.rounds_used
        run.tokens_in, run.tokens_out = result.tokens_in, result.tokens_out
        run.tool_calls_count = sum(1 for m in result.full_message_history if m.tool_calls)
        if result.error:
            run.error = repr(result.error)

    except Exception as e:
        run.status = RunStatus.FAILED
        run.error = repr(e)
        log.exception("agent run failed: goal=%s run=%s", goal.id, run.id)

    finally:
        run.ended_at = now()
        await self._save_run(run)
        await self._post_run(goal, run)
        self._running.discard(goal.id)

    return run
```

### System prompt assembly

Three sections appended via `system_prompt_overrides` (after the profile's own system prompt):

1. **Identity & operating rules** — from the configurable `agent_identity_prompt` (see Configuration). Templates `{owner_user_name}`, `{max_rounds}`, `{max_wall_clock_s}`, `{max_tokens}`.
2. **Goal instruction** — verbatim from `goal.instruction`.
3. **Cross-run memory:**
   - **Workspace manifest** — from `ws_svc.build_workspace_manifest(goal.conversation_id)`.
   - **Recent runs digest** — from `goal.recent_runs_digest`.
   - **Curated notes** — all notes for the goal listed inline if total length ≤ `notes_inline_cap_chars` (default 4000); otherwise replaced with a hint to use `note_list`.

### Initial user message

Different content per trigger kind, all configurable as separate templates (per the "AI prompts are configurable" rule):

- **TIME**: `agent_time_message_template` — references `{timestamp}`.
- **EVENT**: `agent_event_message_template` — references `{timestamp}`, `{event_type}`, `{event_data}`.
- **MANUAL**: `agent_manual_message_template` — references `{timestamp}`, `{user_name}`.

In addition, before calling the loop, the run pulls any user messages posted into `goal.conversation_id` since `goal.last_run_at` and prepends them to the loop's initial message context as "the user said this since your last run." This is the consumption side of "chat into the goal."

### Post-run housekeeping (`_post_run`)

```python
async def _post_run(self, goal: Goal, run: Run) -> None:
    goal.last_run_at = run.ended_at
    goal.last_run_status = run.status
    goal.run_count += 1

    if run.complete_goal_called:
        goal.status = GoalStatus.COMPLETED
        await self._disarm_trigger(goal)
        await self._notify_owner(
            goal,
            self._format(self._goal_completion_notification_template, goal=goal, reason=run.complete_reason),
            urgency=NotificationUrgency.INFO,
        )

    # Asynchronous digest update
    asyncio.create_task(self._update_digest(goal))

    await self._save_goal(goal)
    await self._event_bus.publish(Event("agent.run.completed", {
        "goal_id": goal.id, "run_id": run.id, "status": run.status.name,
    }))
```

The digest update is a **separate AI call** that summarizes the last `digest_runs_window` runs into ~500 tokens using `agent_digest_prompt`. It uses `digest_profile_id` if set, otherwise the goal's profile. It runs as a tracked background task so a slow summarization can't extend the run's wall clock or block the next trigger.

## Agent Built-in Tools

Five tools auto-injected per run as closures over `goal` and `run`, passed via `extra_tools` to `resolve_profile`:

| Tool | Args | Returns | Behavior |
|---|---|---|---|
| `note_add` | `content, tags?` | `{id, created_at}` | Insert a Note scoped to `goal.id`. |
| `note_list` | `tags?` | `[{id, content, tags, created_at}, ...]` | All notes for this goal, optionally filtered by any-of-tags, sorted desc. |
| `note_delete` | `id` | `{deleted: bool}` | Delete a note. **Validates `note.goal_id == goal.id`** before deleting. |
| `notify_user` | `message, urgency, source_ref?` | `{notified_user_ids: [...]}` | Calls `NotificationService.notify_user` once per id in `goal.notify_user_ids`. `urgency` enumerated as `"info"`/`"normal"`/`"urgent"`. `source_ref` defaults to `{"goal_id": goal.id, "run_id": run.id}`. |
| `complete_goal` | `reason` | `{accepted: bool}` | Sets `run.complete_goal_called = True` and `run.complete_reason`. Side effects (status flip, disarm, notify owner) happen in `_post_run`, not in the tool. Subsequent calls in the same run return `{accepted: false}`. |

Tools that raise are caught by `run_loop`, formatted as a tool-result message ("tool failed: <repr>"), and the agent decides whether to retry. The same behavior `AIService.chat()` has today.

These built-ins bypass profile include/exclude lists and role checks — they're inherent to being an agent and don't depend on the goal's profile choice.

## Configuration

Per CLAUDE.md, every AI prompt is a `ConfigParam(multiline=True, ai_prompt=True)` on the owning service with the bundled string as `default`. Live values cached in `self._foo_prompt`-style attributes refreshed in `on_config_changed`.

### `AutonomousAgentService` config params

| Name | Type | `ai_prompt` | Default | Purpose |
|---|---|---|---|---|
| `default_max_rounds` | int | — | 25 | Per-run tool-round cap |
| `default_max_wall_clock_s` | int | — | 300 | Per-run wall-clock cap (seconds) |
| `default_max_tokens` | int | — | 200_000 | Per-run cumulative token cap |
| `default_time_interval_seconds` | int | — | 3600 | Default for new TIME-interval goals |
| `notes_inline_cap_chars` | int | — | 4000 | Inline-vs-tool threshold for notes in system prompt |
| `digest_runs_window` | int | — | 5 | How many recent runs the digest summarizes |
| `digest_after_each_run` | bool | — | true | If false, digest is updated lazily before each run |
| `digest_profile_id` | str | — | (none) | Profile for digest summarization; `choices_from="ai_profiles"`. Falls back to goal's profile if unset. |
| `agent_identity_prompt` | str | ✓ | (bundled) | "You are an autonomous agent…" — prepended to every run |
| `agent_time_message_template` | str | ✓ | (bundled) | Initial user message for TIME-triggered runs |
| `agent_event_message_template` | str | ✓ | (bundled) | Initial user message for EVENT-triggered runs |
| `agent_manual_message_template` | str | ✓ | (bundled) | Initial user message for MANUAL runs |
| `agent_digest_prompt` | str | ✓ | (bundled) | System prompt for the digest summarizer |
| `goal_completion_notification_template` | str | — | (bundled) | Plain text shown when `complete_goal` fires; multiline; references `{goal_name}`, `{reason}` |

Defaults marked **(bundled)** are short opinionated strings authored at implementation time. They live in source as `_DEFAULT_*` constants and are referenced from `ConfigParam(default=...)`. The service reads the active value via `self._foo_prompt`-style attributes refreshed in `on_config_changed`, never from the `_DEFAULT_*` constants directly (per CLAUDE.md's "AI prompts are configurable" rule).

### `NotificationService` config params

| Name | Type | Default | Purpose |
|---|---|---|---|
| `audible_urgencies` | list[str] | `["urgent"]` | Urgencies that trigger an audible signal in the UI (frontend honors this) |

The candidate per-user-preference for `audible_urgencies` is left as a follow-up; service-scoped is fine for v1.

## Notification Surface

### `NotificationService` interface

```python
class NotificationService:
    capabilities = ["notifications", "ai_tools", "ws_handlers"]

    async def notify_user(
        self,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict | None = None,
    ) -> Notification: ...
```

The service persists the notification, then publishes a `notification.received` event. It does not know about WebSockets.

A subscriber in `web/` listens for `notification.received`, looks up active connections for the recipient, and pushes a frame:

```json
{
  "frame": "notification.received",
  "data": {
    "id": "n_abc123",
    "user_id": "u_brian",
    "source": "agent",
    "source_ref": {"goal_id": "g_xyz", "run_id": "r_42"},
    "message": "Found 3 overdue invoices.",
    "urgency": "urgent",
    "created_at": "2026-05-03T14:21:09Z"
  }
}
```

### Client → server RPCs

| Frame | Args | Returns |
|---|---|---|
| `notification.list` | `{filter?: {read?, source?, since?}, limit?: int}` | `{items: [...], unread_count: int}` |
| `notification.mark_read` | `{id}` | `{ok: true}` |
| `notification.mark_all_read` | `{}` | `{count: int}` |
| `notification.delete` | `{id}` | `{ok: true}` |

All RBAC-checked against the connecting user; users can only see/manage their own notifications.

### UI behavior

- Top-bar bell icon with unread badge for the current user.
- Click → dropdown with latest ~10 unread + "View all" → `/notifications` page.
- Each item: source icon, urgency color (info=neutral, normal=blue, urgent=red), message, relative time, and a click-action that marks read and (if `source_ref` matches a known shape) deep-links.
- On WS receipt:
  - `urgent`: title-bar flash + sound (if `audible_urgencies` includes `"urgent"`) + bell pulse animation.
  - `normal`: silent badge bump.
  - `info`: silent badge bump (no animation).

## UI Placement

### Navigation

- **Top-level: `Agents`** — daily-use entry, not buried in settings.
- **Top-level: bell icon** — notifications.
- **Settings → AI → Autonomous Agent** — service-level config (above).
- **Settings → AI → Notifications** — `NotificationService` config.

### Pages

**`/agents`** — list of goals. Columns: name, owner, trigger summary, status badge, last run, enabled toggle, "Run Now". Filters: status, trigger type, owner. Visibility: `owner_user_id == self` OR `self in notify_user_ids`; admins see all. "+ Create Goal" button.

**`/agents/:id`** — goal detail with four tabs:

1. **Configure** — name, instruction (with "Author with AI" button), profile picker, trigger type and config, notify users multi-select, advanced overrides.
2. **Activity** — the materialized conversation, rendered with `<ConversationView>`, with a goal-specific composer (queues messages for the next run).
3. **Notes** — read-only list of `agent_notes`, sorted desc, tag filter, owner has a "Delete" button per note.
4. **Runs** — full history table; each row links to run detail.

**`/agents/:id/runs/:run_id`** — run detail: header (status, trigger kind, started/ended, rounds/tokens), message history (collapsible, anchored at `start_message_id`), error block if applicable.

**`/notifications`** — full notification list.

### Goal-create form

Single page (not multi-step). Defaults: `TIME` / interval / 3600s, user's default profile, `[self]` for notify users, `ENABLED`. Submit → POST creates goal and arms trigger; routes to goal detail on the Activity tab.

### Out of scope for v1

- Goal versioning / instruction history
- Goal templates
- Manual note authoring
- Goal sharing / cloning across users
- Re-run button on run detail

## Materialized Conversations and Live-Tailing

Each goal has a `Goal.conversation_id` pointing at a normal entry in Gilbert's existing chat conversation store, owned by `Goal.owner_user_id` with read access extended to `notify_user_ids` (and admins per RBAC).

Run lifecycle within the conversation:
1. On run start, append a synthetic marker message: *"Run #N — triggered by TIME at HH:MM:SS."* Capture its id as `run.start_message_id`.
2. Run the loop **inside the conversation** — messages are persisted into the goal's conversation as they're produced, via the same path `chat()` uses today.
3. On completion, append a status footer message with rounds/tokens/duration.

Live-tailing falls out for free: the existing chat WS infrastructure already provides per-conversation token-delta streaming, tool-call events, message-completion events, and a subscription model. The Activity tab on `/agents/:id` is just a normal conversation view subscribed to `goal.conversation_id`.

User input into `goal.conversation_id` between runs is consumed by the next run: `_initial_user_message` reads messages posted since `goal.last_run_at` and prepends them to the run's initial context as "the user said this since your last run."

## `<ConversationView>` Extraction

Frontend stack: React + TypeScript via Vite (`frontend/src/`). The message-rendering portion of the existing chat UI is lifted into a reusable React component:

**`<ConversationView>`** — props: `conversation_id`. Subscribes to the conversation's WS stream, renders messages chronologically (assistant bubbles, tool-call cards, tool-result cards), handles streaming token deltas, manages scroll-to-bottom. Headless of who's chatting. Exposes a render-prop slot for "render this synthetic marker between messages X and Y" so the goal view can inject run boundaries.

**Not extracted** (surface-specific):
- Headers (`/chat` shows conversation title and profile selector; `/agents/:id` shows goal name, status, run-in-progress indicator).
- Composers (`/chat` sends and immediately invokes; `/agents/:id` queues for the next run with explicit UX about delivery timing).
- Run boundary markers (goal-only; rendered via the slot exposed by `<ConversationView>`).

**Scope guardrails:**
- One focused sub-task. No "while we're at it" cleanup of unrelated chat features (slash commands, attachments, profile selector — those stay in the chat-page composer).
- Behavior of `/chat` must be byte-identical after extraction; verified against existing chat tests and a manual smoke before agent UI work begins.
- If extraction would exceed ~1 day of work, fall back to copying the message-list portion into a new component with a TODO to converge later. The feature isn't worth blocking on a heavier UI refactor.

## Workspace per Goal

Workspaces are inherited via the materialized conversation. `WorkspaceProvider` is keyed by `(user_id, conversation_id)`, so each goal gets:

- `.gilbert/workspaces/users/<owner>/conversations/<goal_conv>/uploads/`
- `.../outputs/`
- `.../scratch/`

### What we plug in

1. **Workspace manifest in the agent system prompt.** `_run_goal` calls `ws_svc.build_workspace_manifest(goal.conversation_id)` (the same call `AIService.chat()` makes at `ai.py:2939`) and appends the result to `system_prompt_overrides`.
2. **File-upload UX on the goal page.** The goal-detail composer wires drag/drop upload to the existing endpoint in `web/routes/chat_uploads.py`, posting with `goal.conversation_id`. The composer doesn't extract; the upload affordance is re-implemented inside the goal-specific composer (small).
3. **File tools.** Existing chat workspace tools (`read_file`, `write_file`, `list_files`, etc.) are already registered as `ToolProvider` tools. The agent gets them through normal profile resolution.

### Lifecycle

- **Goal delete** → cascade-delete the conversation → existing `chat.conversation.archiving` event → workspace cleanup as today.
- **Goal disable** → leave the workspace untouched. Re-enable resumes against existing files.
- **Goal complete** → leave the workspace; it's a record of what the agent did.

### Verification items (not new design — confirm-and-patch in implementation)

- Confirm Gilbert doesn't auto-archive idle conversations on a timer. If it does, agent-owned conversations need a `pinned` / `do_not_auto_archive` flag.
- Confirm workspace cleanup actually fires via `chat.conversation.archiving`. If not, add a subscriber in the workspace service.

## Error Handling

| Failure mode | Behavior |
|---|---|
| AI backend raises mid-stream | `run_loop` catches, sets `LoopResult.error`, returns. Run is `FAILED`. No retry in v1. |
| Tool raises | `run_loop` formats error as a tool-result message; the agent decides whether to retry. |
| Process restart mid-run | Startup scans `agent_runs` with status `RUNNING` and `started_at` older than `max_wall_clock_s × 2`, marks `FAILED` with `error="process_restarted"`. No resume. |
| Scheduler job fires for deleted goal | Handler re-fetches; if missing or not `ENABLED`, no-op and remove the stale job. |
| Event subscription fires for deleted goal | Same: handler checks goal state; if gone, unsubscribe. |
| `notify_user` to a user who no longer exists | Log warning, skip recipient, continue with others. |
| Profile resolution fails (deleted profile) | Run marked `FAILED` with clear error. Goal status unchanged. |

## Testing Strategy

**Unit tests** (`tests/unit/core/`):

- `test_agent_loop.py` — `run_loop` against a fake `AIBackend` that yields scripted streams. Covers all stop reasons, tool execution (sequential and parallel), tool exceptions, AI exceptions.
- `test_agent_tools.py` — built-in tools against a fake storage backend. Covers note CRUD, cross-goal note isolation, `complete_goal` once-per-run semantics, `notify_user` fan-out.
- `test_notification_service.py` — entity persistence, event publish, RBAC on RPCs.

**Integration tests** (`tests/integration/`, real SQLite):

- `test_agent_service_lifecycle.py` — create goal → trigger fires → run executes → run persisted → digest updated. Across all trigger types.
- `test_agent_service_concurrency.py` — heartbeat firing while previous run is still going → second tick is skipped.
- `test_agent_service_restart.py` — orphaned `RUNNING` run is marked `FAILED` and triggers re-arm correctly on startup.
- `test_chat_loop_byte_identical.py` — replay a recorded chat session against the new `agent_loop`-backed implementation; assert produced message history matches the pre-refactor baseline.

**Faking strategy:**
- Fake `AIBackend` that takes a scripted list of `(stream_events, stop_reason)` per call.
- Fake notification sink that captures published events.
- Real SQLite for entity tests (per CLAUDE.md).

## Implementation Phases

Five increments. Each phase ends in a working state; the feature is incremental but releasable per phase.

1. **`agent_loop.run_loop()` primitive + tests.** Pure function, no service, fake-backend tests. No production caller yet.
2. **Refactor `AIService.chat()` to use `agent_loop`.** Behavior-preserving. Verified by the byte-identical replay test. Ships before any new agent code is wired.
3. **`NotificationService` + WS push + UI bell.** Useful on its own (scheduler/ingest can call it). Ships independently.
4. **`AutonomousAgentService` core.** Entities, CRUD WS RPCs, trigger plumbing, `_run_goal`, materialized conversation per goal, digest updates. Goals creatable via raw RPC; runs persist and live-tail in the existing chat UI pointed at the goal's `conversation_id`.
5. **UI.** `<ConversationView>` extraction; `/agents` list; goal detail with all four tabs; goal-specific composer (with queued user input → next run consumption); `/agents/:id/runs/:run_id`; "Author with AI" on `Goal.instruction`.

## Open Verification Items (carry into implementation plan)

- Confirm the web layer has (or build) a `push_to_user(user_id, frame)` capability that pushes to all of a user's active connections. The notification subscriber needs it.
- Confirm `SchedulerService.add_job` is idempotent on `name`, or implement delete-then-add by name in `_arm_trigger`.
- Confirm `event_types` registry exists for `choices_from` on the EVENT trigger configuration form. If absent, fall back to free-text with autocomplete from observed event types.
- Confirm Gilbert doesn't auto-archive idle conversations on a timer; if it does, add `do_not_auto_archive` for agent-owned conversations.
- Confirm workspace cleanup fires via `chat.conversation.archiving`; if not, add a subscriber.
- Confirm the existing chat conversation auth model supports per-conversation read access for non-owner users (for `notify_user_ids` to read the goal conversation).
- Confirm the AI-call logging system's named-call mechanism (used by user_memory's `_AI_CALL_NAME`) — agent runs and the digest summarization should each have distinct call names so the AI API call log distinguishes them.

## Out of Scope for v1

- Self-authoring plugins (the agent writing new plugins on its own).
- Multi-platform chat surfaces (WhatsApp, Discord, Telegram, etc.).
- "50+ integrations" — agents inherit whatever integrations exist as plugins.
- Per-goal tool allowlist beyond profile filtering.
- Approval queue for write/destructive actions (kept as a future flag).
- Retry policy for failed runs.
- Goal templates, sharing, cloning, versioning.
- Live-tailing in `/chat` of an in-progress agent run from outside its goal page.
- Notification dedupe / grouping.
