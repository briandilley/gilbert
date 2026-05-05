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
(force-include) constant: `_CORE_AGENT_TOOLS`. Phase 1A core set:
`complete_run`, `request_user_input`, `notify_user`,
`commitment_create`, `commitment_complete`, `commitment_list`,
`agent_memory_save`, `agent_memory_search`,
`agent_memory_review_and_promote`. Phase 2/4 add to it.

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
`_load_agent_for_caller`; admin sees-all on list.
`agents.tools.list_available` and `agents.tools.list_groups` ship in
Phase 1B alongside the SPA `<ToolPicker>`. Permission entry in
`acl.py`: single broad `"agents.": 100` matching the file's existing
prefix-style convention.

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

## Related
- `src/gilbert/interfaces/agent.py`
- `src/gilbert/core/services/agent.py`
- `tests/unit/test_agent_service.py`
- `tests/unit/test_agent_memory.py`
- `tests/unit/test_commitments.py`
- `tests/unit/test_heartbeat.py`
- `tests/unit/test_agent_inbox.py`
- `tests/unit/test_tool_gating.py`
- `tests/unit/test_agent_entities.py`
- `docs/superpowers/specs/2026-05-04-agent-messaging-design.md`
- `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md`
- `.claude/memory/memory-agent-loop.md` (run_loop primitive)
- `.claude/memory/memory-multi-user-isolation.md`
- `.claude/memory/memory-ai-prompts-configurable.md`
