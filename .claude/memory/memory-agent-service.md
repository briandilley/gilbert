# AgentService

## Summary
Replaces AutonomousAgentService with the multi-agent design from
docs/superpowers/specs/2026-05-04-agent-messaging-design.md. `Agent`
is a durable identity (persona + system_prompt + procedural_rules +
heartbeat + memory + commitments + tool allowlist + avatar). Lives in
src/gilbert/core/services/agent.py. Phase 1A is the backend foundation;
Phase 1B adds the SPA management UI; Phases 2-5 add peer messaging,
mid-stream interrupt, multi-agent goals, and deliverables.

## Details

**Capabilities declared:** ``agent`` (satisfies `AgentProvider`),
``ai_tools``, ``ws_handlers``.

**Requires:** ``entity_storage``, ``event_bus``, ``ai_chat``, ``scheduler``.

**AI call name:** ``agent.run`` (via ``ai_calls`` in `ServiceInfo`).
Operators can route to a distinct profile via the AI profile assignment
table.

**Slash namespace:** `slash_namespace = "agents"` on the class.

**Entities** (one collection per type):
- `agents` — Agent rows (the durable identities).
- `agent_memories` — `AgentMemory` rows. Two-tier `state` field:
  `SHORT_TERM` (default; recent observations) vs `LONG_TERM`
  (durable, top-K loaded into prompt). `kind` field discriminates
  fact / preference / decision / daily / dream.
- `agent_triggers` — Phase 1A registers heartbeat triggers via the
  scheduler directly; the entity is defined for future use by time/event
  triggers in later phases.
- `agent_commitments` — opt-in short-lived follow-ups. Surfaced in
  heartbeat prompts when `due_at <= now` and `completed_at` is null.
- `agent_inbox_signals` — durable wake-up tracking. Message *content*
  lives in chat conversation rows; this entity tracks lifecycle
  (`processed_at` is null until the loop drains it).
- `agent_runs` — `Run` rows keyed by `agent_id`.

**Loop model:** `run_agent_now(agent_id, user_message=...)` is the
synchronous entry. Loops fire under `_running_agents` guard, wrapped in
`asyncio.shield` so a WS disconnect doesn't cancel the run.
`_run_agent_internal` builds the system prompt (persona +
system_prompt + procedural_rules + trigger-specific block + LONG_TERM
memory), synthesizes a user message from trigger context if not
provided, calls `AIService.chat(ai_call="agent.run")`, captures the
result fields (`response_text`, `conversation_id`, `turn_usage`'s
`input_tokens`/`output_tokens`/`cost_usd`/`rounds`) onto the Run row.

**Heartbeat:** when `Agent.heartbeat_enabled=True` (default), creating
or updating the agent registers a SchedulerService job named
`heartbeat_<agent_id>` at `Schedule.every(heartbeat_interval_s)`. Jobs
are marked `system=True` so users can't accidentally remove them.
Firing the job invokes `_on_heartbeat_fired(agent_id)` which spawns a
run with `triggered_by="heartbeat"`. Heartbeat re-armed in `start()`;
disarmed on delete and on stop. Note: `add_job` / `remove_job` are
synchronous on the real `SchedulerProvider`.

**InboxSignal dispatch:** `_signal_agent` is the single dispatch
point. Idle agents get a fresh run spawned (`asyncio.create_task` with
named task `agent-run-<id>`); busy agents have the signal enqueued to
in-memory cache and persisted to `agent_inbox_signals`. `_drain_inbox`
between rounds marks signals processed. `_rehydrate_inboxes` on
service start restores the cache from rows where
`processed_at IS NULL` (queried via `Filter(field="processed_at",
op=FilterOp.EXISTS, value=False)`). Phase 2 will add the producers
(peer DMs, mentions, delegations); Phase 5 the deliverable_ready
producer.

**Per-agent tool gating:** `_compute_allowed_tool_names` returns the
final tool name set: if `tools_allowed=None` → all available; if a
list → core ∪ allowlist intersected with available. Core
(force-include) constant: `_CORE_AGENT_TOOLS`. Phase 1A added:
`complete_run`, `request_user_input`, `notify_user`,
`commitment_create`, `commitment_complete`, `commitment_list`,
`agent_memory_save`, `agent_memory_search`,
`agent_memory_review_and_promote`. Phase 2 adds: `agent_list`,
`agent_send_message`, `agent_delegate`. Phase 4 will add `goal_post`.

**Tool argument injection:** `_inject_agent_id(agent_id, tools_dict)`
wraps each `(ToolDefinition, handler)` tuple so every tool call's
`arguments` dict has `_agent_id` set. Tools read identity from injected
args only — never from caller-supplied arg shapes. Phase 2 will plumb
this into the per-run tools dict before `AIService.chat()`.

**Tools (Phase 1A):** `complete_run`, `commitment_create`,
`commitment_complete`, `commitment_list`, `agent_memory_save`,
`agent_memory_search`, `agent_memory_review_and_promote`. Future phases
add `agent_send_message`, `agent_delegate`, `agent_list` (Phase 2),
`goal_*` (Phase 4), `deliverable_*` (Phase 5).

**WS RPCs (Phase 1A):** `agents.create / get / list / update / delete /
set_status / run_now / get_defaults`. Per-user RBAC enforced via
`load_agent_for_caller` (public on `AgentProvider`); admin sees-all on
list. Permission entry in `acl.py`: single broad `"agents.": 100`
matching the file's existing prefix-style convention.

**WS RPCs (Phase 1B):** the SPA reads runs / commitments / memories /
the tool catalog through:
- `agents.runs.list(agent_id, limit?)` — owner-scoped, clamped `limit ≥ 1`.
- `agents.commitments.{list, create, complete}` — owner-scoped; create
  rejects empty content and resolves `due_at` from either `due_at` or
  `due_in_seconds`; complete authorizes via the commitment's owning agent.
- `agents.memories.{list, set_state}` — owner-scoped. `list` supports
  `state`, `kind`, `tags` (any-match), `q` (substring), `limit` filters
  (handled inline in `search_memory`). `set_state` flips between
  `short_term` ↔ `long_term` via `promote_memory`.
- `agents.tools.list_available` — backed by `AIToolDiscoveryProvider.discover_tools(user_ctx=...)`
  bound as a hard requirement in `start()`. Returns `[{name, description, provider, required_role?}]`
  shape; tuple-unpacks the `(provider, ToolDefinition)` returned by `AIService`.
- `agents.tools.list_groups` — returns `_defaults["tool_groups"]`.

**HTTP routes (Phase 1B):** `web/routes/agent_avatar.py` mounts:
- `POST /api/agents/{agent_id}/avatar` — multipart `file=<image>`. MIME
  gate (png/jpeg/webp/gif → 415), 4 MiB cap (`_MAX_AVATAR_BYTES`),
  filename sanitizer (`_sanitize_filename`), streamed write. Calls
  `AgentService.set_agent_avatar(agent_id, filename=...)` (a thin
  wrapper that delegates to `update_agent` so `agent.updated` fires).
- `GET /api/agents/{agent_id}/avatar` — streams the avatar back; 404
  when `avatar_kind != "image"`. Cache headers: `Cache-Control: private,
  max-age=3600` (filenames are content-hashed).

Both routes require auth (`Depends(require_authenticated)`); admin
bypass goes through `AccessControlProvider.get_effective_level(user) ≤ 0`,
matching the WS-handler discipline. Avatars live at
`<DATA_DIR>/agent-avatars/<agent_id>/<sha-suffixed filename>`;
`_remove_avatar_dir(agent_id)` is best-effort and called from `delete_agent`.

**Public AgentProvider (Phase 1B):** Phase 1B added two methods to the
`AgentProvider` protocol — `load_agent_for_caller(agent_id, *,
caller_user_id, admin=False)` and `set_agent_avatar(agent_id, *,
filename)`. The HTTP routes use `isinstance(svc, AgentProvider)` rather
than reaching into private internals.

**Defaults (ConfigParam):** `default_persona`, `default_system_prompt`,
`default_procedural_rules`, `default_heartbeat_checklist` are flagged
`multiline=True, ai_prompt=True` for the prompt-author UI.
`tool_groups` is `ToolParameterType.OBJECT` for operator-editable JSON.
`default_tools_allowed` is a comma-separated string normalized in
`on_config_changed` (empty → `None`).

**RBAC:** all `agents.*` WS RPCs are user-level (100 in
`DEFAULT_RPC_PERMISSIONS`). Handlers enforce per-user ownership via
`_load_agent_for_caller(agent_id, *, caller_user_id, admin=False)`.
KeyError if missing; PermissionError if cross-user without admin.

**Multi-user isolation:** `_running_agents` and `_inboxes` are keyed
by agent_id (owner-scoped). `asyncio.create_task` for spawned loops
inherits the current contextvars by default in Python 3.12+.

**Cost cap:** `_accumulate_cost(agent_id, delta)` adds to
`Agent.lifetime_cost_usd` after every run; if `cost_cap_usd` is set
and exceeded, the agent is auto-flipped to DISABLED and a warning is
logged.

**Events published:** the service publishes on every state change via
`_publish(event_type, data)` (no-op when the bus isn't bound).
- `agent.created` — `{agent_id, owner_user_id}` after `create_agent`.
- `agent.updated` — `{agent_id}` after `update_agent`.
- `agent.deleted` — `{agent_id}` after `delete_agent`.
- `agent.run.started` — `{agent_id, run_id, triggered_by}` after the
  initial RUNNING row is persisted in `_run_agent_internal`.
- `agent.run.completed` — `{agent_id, run_id, status, cost_usd}` right
  before `_run_agent_internal` returns. `source="agent"` on every event.
The SPA subscribes to these for real-time refresh of the agents UI.

**Storage API:** the backend uses `Query(collection=..., filters=[...])`
with `Filter(field=..., op=FilterOp.EQ, value=...)`. Don't use
dict-shaped filters — they don't match the real API. For
`processed_at IS NULL`-style queries, use
`FilterOp.EXISTS` with `value=False`.

## Phase 2 — Peer messaging (queue mode)

Three new core tools, all force-included via `_CORE_AGENT_TOOLS`. They
share the existing `_signal_agent` dispatch primitive — Phase 2 added
*producers*; the inbox-drain consumer was already in place from
Phase 1A storage and gets activated in Phase 2 by wiring it into the
run loop.

**Tools added:**
- **`agent_list`** — no parameters. Returns a JSON-encoded list of
  peer agents under the same owner (self excluded). Each entry is
  `{name, role_label, status, conversation_id}`. Slash:
  `/agents agent_list`.
- **`agent_send_message(target_name, body)`** — fire-and-forget DM.
  Resolves the target via `_load_peer_by_name` (same-owner namespace),
  dispatches an `inbox`-kind `InboxSignal` via `_signal_agent`. No
  reply awaited. Slash: `/agents agent_send_message`.
- **`agent_delegate(target_name, instruction, max_wait_s=600)`** —
  send-and-await. Caller awaits an `asyncio.Future` that the target's
  run resolves on completion. Slash: `/agents agent_delegate`.

**Inbox drain into the run loop:** `_run_agent_internal` now drains
pending signals at round 0 and appends them (one line each, formatted
by `_format_inbox_signal`) onto the lead user message under an
`INBOX:` block. It also passes a `_between_rounds` async closure to
`AIService.chat` as `between_rounds_callback` — the closure drains the
inbox and returns a list of `Message(role=USER, …)` so signals
arriving mid-run are visible at the next round. `_format_inbox_signal`
formats peer/user signals as `[from {sender_name}]: {body}` and
system signals as `[system]: {body}`.

**Delegation mechanics:**
- `_pending_delegations: dict[str, asyncio.Future[str]]` lives on the
  service. Keyed by `delegation_id`. The handler creates the future,
  fires the signal, awaits with `wait_for(timeout=max_wait_s)`, and
  always pops the entry in a finally.
- `_run_with_signal` reads `metadata['chain']` and `sig.delegation_id`
  off the InboxSignal and forwards them in `trigger_context` so
  `_run_agent_internal` can stamp `run.delegation_id` and resolve the
  future at end-of-run (success → `set_result(final_message_text)`;
  FAILED/etc. → `set_exception(RuntimeError(...))`).
- `_inject_agent_id` grew an optional `delegation_chain` kwarg. When
  set, wrapped tool handlers also see `_delegation_chain` in their
  args, so a delegated-to agent that itself calls `agent_delegate`
  forwards the chain automatically.
- `_build_system_prompt` appends a "you are handling a delegation"
  block when `triggered_by == "delegation"`. `_synthesize_trigger_message`
  produces the matching user-message text.

**Cycle + depth + timeout policies:**
- **Cycle**: handler appends caller to chain, then rejects if
  `target.id ∈ chain`. So A→B→A is rejected before any signal fires.
- **Depth cap**: `_DELEGATION_DEPTH_CAP = 5`. After append, if
  `len(chain) >= cap`, reject.
- **Timeout**: `max_wait_s` (default 600). On `TimeoutError` the tool
  returns an error string. The target's run is NOT cancelled — only
  the caller's await is abandoned. The future still in
  `_pending_delegations` is popped by the finally; if the target
  finishes after the timeout, `set_result` no-ops because the entry
  is already gone.
- **Target failure**: target run lands in FAILED → end-of-run sets
  the future's exception → handler catches it as `Exception` and
  returns `f"error: target run {exc}"` so the AI tool runtime
  receives a clean string, not a raised exception.

**Same-owner enforcement:** both `agent_send_message` and
`agent_delegate` go through `_load_peer_by_name(caller_agent_id, target_name)`
which queries the agents collection filtered by the caller's
`owner_user_id`. Cross-owner reach raises `PermissionError(
'no peer named …')` — same wording as a missing name, so existence
of names in another owner's namespace doesn't leak.

**Out of scope for Phase 2 (deferred):** chat-conversation persistence
of peer-message bodies. The body lives in `InboxSignal.body` and gets
formatted into the next round's prompt; it does NOT get inserted as a
USER row into the target's chat conversation. A polish phase will add
that path (along with sender-attribution UI in chat). Mid-stream
interrupt is also deferred (Phase 3).

## Phase 3 — Mid-stream interrupt

A busy agent can now be interrupted between tool-call groups inside a
single round when an `urgent` signal arrives, instead of waiting for
the round to complete.

**Tool surface:**
- `agent_send_message` and `agent_delegate` accept an optional
  `priority` parameter — `"urgent"` or `"normal"`.
- `agent_send_message` defaults to `"normal"` (queue mode, Phase 2
  behavior).
- `agent_delegate` defaults to `"urgent"` because the caller is awaiting
  an END_TURN reply and a busy target should drop everything.
- Invalid values return `"error: priority must be one of: ..."` from
  the tool — they never reach `_signal_agent`. Validation is via
  `_parse_priority(raw, default)`.

**Service-level state:**
- `AgentService._urgent_pending: dict[str, bool]` — keyed by agent_id.
  Set by `_signal_agent` AFTER the inbox row is persisted, only when
  `priority == "urgent"`. Cleared unconditionally at the END of
  `_drain_inbox` (after `processed_at` writes succeed) so a stale
  flag never trips the next round's interrupt check.

**Run-loop wiring:**
- `_run_agent_internal` defines `_interrupt_check() -> bool: return
  self._urgent_pending.get(a.id, False)` and passes it as
  `mid_round_interrupt=` to `AIService.chat`. Exists alongside the
  existing `between_rounds_callback` (Phase 2) which drains the
  signals into the next round once the interrupt fires.

**AIService.chat boundary mechanic:**
- `AIService.chat(..., mid_round_interrupt: Callable[[], bool] | None
  = None)` threads the callback into `_execute_tool_calls`.
- `_execute_tool_calls` checks the callback BEFORE each tool-call
  group iteration (skipping `group_idx == 0` so at least one group
  always runs). On True: every remaining un-run `ToolCall` receives a
  stub `ToolResult(content="(skipped — urgent interrupt; the message
  is in the next round's inbox)", is_error=False)` so the assistant
  message's tool_calls list and the next tool_results message stay
  aligned. Loop returns early.
- The check happens BETWEEN groups, never inside a parallel batch —
  in-flight `asyncio.gather`'d tools run to completion.

**Backwards-compat guarantee:** when `mid_round_interrupt is None` or
the callback always returns False, `_execute_tool_calls` is
bit-identical to its prior behavior. No existing tests changed
semantics.

## Phase 4 — Multi-agent goals

First-class `Goal` entity with one or more agent assignees. A Goal
owns a war-room conversation; assignments come in three roles —
DRIVER (the responsible owner), COLLABORATOR (peer worker), REVIEWER
(read-mostly). Same-owner only in Phase 4; cross-user is Phase 6.

**Entities** (`src/gilbert/interfaces/agent.py`):
- `Goal` — id, owner_user_id, name, description, status (NEW /
  IN_PROGRESS / BLOCKED / COMPLETE / CANCELLED),
  war_room_conversation_id, cost_cap_usd, lifetime_cost_usd,
  created_at, updated_at, completed_at.
- `GoalAssignment` — id, goal_id, agent_id, role (driver /
  collaborator / reviewer), assigned_at, assigned_by ("user:<uid>"
  or "<agent_id>"), removed_at, handoff_note.

**Collections:** `goals`, `goal_assignments`. War-room conversations
reuse the existing `ai_conversations` collection with
`metadata={"goal_id": …, "kind": "war_room"}` so consumers can find
them.

**`AgentService` methods:** `create_goal` (creates row + war-room
conv + initial assignments — first listed assignee defaults DRIVER
when none specified), `get_goal`, `list_goals`,
`update_goal_status`, `list_assignments` (filterable by goal_id,
agent_id, active_only), `assign_agent_to_goal` (idempotent on
same-role), `unassign_agent_from_goal` (marks `removed_at`, doesn't
delete), `handoff_goal` (atomically demotes DRIVER →
`new_role_for_from` (default COLLABORATOR) and promotes target →
DRIVER). `_recent_war_room_posts(goal_id, limit)` reads the conv's
last N user-role messages for prompt assembly.

**Tools (seven new):**
- `goal_create` — creates goal + assignments. Caller becomes the
  owner. Resolves `assign_to` agent names via `_load_peer_by_name`.
- `goal_assign` — DRIVER-only.
- `goal_unassign` — DRIVER-only OR self-unassign.
- `goal_handoff` — current DRIVER only. Defaults `new_role_for_from`
  to COLLABORATOR.
- `goal_post(goal_id, body, mention=[])` — assignee-only. Appends
  USER-role row to the war-room conv with
  `metadata.sender = {kind, id, name}`. Mentioned peers receive an
  `inbox` signal with body `[mentioned in war room <name>]: <body>`.
  Joins `_CORE_AGENT_TOOLS`.
- `goal_status(goal_id, new_status)` — DRIVER-only.
- `goal_summary(goal_id)` — assignee-only. Returns JSON
  `{name, description, status, assignees, recent_posts (last 10),
  lifetime_cost_usd, is_dependency_blocked: false}`.
  `is_dependency_blocked` is hardwired False in Phase 4; Phase 5
  computes it from real GoalDependency rows.

**WS RPCs** (all `goals.*`, owner-scoped or admin):
`goals.create / list / get / update_status` for goal CRUD,
`goals.assignments.list / add / remove / handoff` for assignment
management, `goals.summary` and `goals.posts.list` for war-room
reads. ACL: `"goals.": 100` in `interfaces/acl.py`.

**System prompt — ACTIVE ASSIGNMENTS block:** every run, after the
LONG_TERM memory block, the agent's active assignments are appended:

```
ACTIVE ASSIGNMENTS:
- Goal 'name' (id=goal_id) [role=driver, status=in_progress]
  alice: hi everyone
  bob: kicking off the spec
- Goal 'other-name' …
```

Recent posts default to last 10. The block is omitted entirely when
the agent has no active assignments.

**Events:** `goal.created`, `goal.updated`, `goal.status.changed`,
`goal.assignment.changed` published by the relevant methods. The
SPA subscribes for live refresh of the kanban + war-room.

**Frontend (Phase 4):**
- New types in `frontend/src/types/agent.ts` (Goal /
  GoalAssignment / WarRoomPost / GoalSummary / GoalStatus /
  AssignmentRole).
- React Query client at `frontend/src/api/goals.ts` mirroring
  `agents.ts` patterns.
- `/goals` route — `GoalsListPage` + `GoalKanban` + `GoalCard`. Five
  columns (NEW / IN_PROGRESS / BLOCKED / COMPLETE / CANCELLED).
  Native HTML5 drag-and-drop between columns calls
  `useUpdateGoalStatus`. "New goal" Dialog with name, description,
  multi-select assignee picker.
- `/goals/<id>` route — `WarRoomPage` with header (name, status,
  cost, Status/Handoff/Add-assignee actions), `AssigneesStrip`,
  scrollable read-only post list, right-rail Phase 5 placeholders
  (Deliverables / Dependencies). No composer in Phase 4.
- "Goals" nav group registered in `web_api.py` (icon: `target`,
  required_role: `user`).

**Out of scope (Phase 4):**
- War-room composer for human posting. Posts come via `goal_post`
  from agents only. Polish follow-up.
- Goal deletion / purge. CANCELLED is the closure path.
- Run-cost rollup onto `Goal.lifetime_cost_usd` (field exists; not
  populated automatically — needs a run→goal linkage).
- Direct conv-row write contract — `goal_post` mutates
  `ai_conversations.<id>.messages` directly. A capability-protocol
  `append_message_to_conversation` would be cleaner; deferred.

## Related
- `src/gilbert/interfaces/agent.py`
- `src/gilbert/core/services/agent.py`
- `src/gilbert/web/routes/agent_avatar.py` (Phase 1B HTTP routes)
- `frontend/src/api/agents.ts` (Phase 1B SPA client)
- `frontend/src/components/agent/` (Phase 1B SPA components)
- `tests/unit/test_agent_service.py`
- `tests/unit/test_agent_memory.py`
- `tests/unit/test_commitments.py`
- `tests/unit/test_heartbeat.py`
- `tests/unit/test_agent_inbox.py`
- `tests/unit/test_tool_gating.py`
- `tests/unit/test_agent_entities.py`
- `tests/unit/test_agents_ws_rpcs.py` (Phase 1B WS RPC coverage)
- `tests/unit/test_agent_avatar_route.py` (Phase 1B HTTP route coverage)
- `tests/unit/test_agent_peer_messaging.py` (Phase 2 + Phase 3 coverage)
- `tests/unit/test_ai_service_interrupt.py` (Phase 3 boundary mechanic)
- `tests/unit/test_goals.py` (Phase 4 entities + service)
- `tests/unit/test_goal_tools.py` (Phase 4 agent tools)
- `tests/unit/test_goal_assignments.py` (Phase 4 WS RPCs)
- `tests/unit/test_war_room_acl.py` (Phase 4 RBAC)
- `frontend/src/api/goals.ts` (Phase 4 SPA client)
- `frontend/src/components/goals/` (Phase 4 SPA components)
- `docs/superpowers/specs/2026-05-04-agent-messaging-design.md`
- `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md`
- `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1b-ui.md`
- `docs/superpowers/plans/2026-05-04-agent-messaging-phase-2-peer-messaging.md`
- `docs/superpowers/plans/2026-05-04-agent-messaging-phase-3-mid-stream-interrupt.md`
- `docs/superpowers/plans/2026-05-04-agent-messaging-phase-4-goals.md`
- `.claude/memory/memory-agent-loop.md` (run_loop primitive)
- `.claude/memory/memory-multi-user-isolation.md`
- `.claude/memory/memory-ai-prompts-configurable.md`
