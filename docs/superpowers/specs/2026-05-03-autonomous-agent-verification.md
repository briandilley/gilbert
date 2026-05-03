# Autonomous Agent — Verification Findings

Companion to `2026-05-03-autonomous-agent-design.md`. Each section captures the answer to one open verification item from the spec, with file/line citations and (if applicable) a follow-up task assigned to a downstream phase plan.

## 1. push_to_user capability in web layer

**Status:** Missing
**Findings:** No per-user push helper exists. Connections are tracked in `src/gilbert/web/ws_protocol.py:286` as `_connections: set[WsConnection]` keyed only by the connection object itself. Adding `push_to_user(user_id, frame)` requires: (1) maintain `dict[user_id, set[WsConnection]]` updated on `register`/`unregister` in `WsConnectionManager`, (2) expose a `push_to_user` method on the `WsConnectionManager`, (3) declare a `user_ws_pusher` capability so other services can resolve it via `service_manager.get_by_capability("user_ws_pusher")`.
**Follow-up:** Phase 3 plan must include a task to add `push_to_user`. Tag this verification finding in the Phase 3 plan's pre-flight section.

## 2. SchedulerService.add_job idempotency on name

**Status:** Not idempotent
**Findings:** `src/gilbert/core/services/scheduler.py:379-380` — `add_job` raises `ValueError(f"Job '{name}' already registered")` if a job with the same name exists. Re-arming on goal update must call `remove_job(name)` first (best-effort, swallow not-found) then `add_job`.
**Follow-up:** Phase 4 plan: `_arm_trigger` does `remove_job` (best-effort, swallow KeyError) then `add_job`.

## 3. event_types registry / dynamic choices

**Status:** No registry
**Findings:** Event types are string literals scattered across publishers (e.g., `"config.changed"` in `src/gilbert/core/services/configuration.py:737`, `"doorbell.ring"` in `src/gilbert/core/services/doorbell.py:259`, `"presence.arrived"` / `"presence.departed"` in `src/gilbert/core/services/presence.py:291,305`). No central registry exists. ConfigurationService's `_resolve_dynamic_choices` at `src/gilbert/core/services/configuration.py:493-576` supports multiple dynamic sources (`"speakers"`, `"ai_profiles"`, `"inbox_mailboxes"`, etc.) but does not have an `"event_types"` case. The `InMemoryEventBus` implementation at `src/gilbert/core/events.py` tracks subscribers internally but does not expose an observable registry of event types.
**Follow-up:** Phase 4 plan (or Phase 5 if bootstrapping agent UI first): When the goal-create form's trigger-config UI is implemented, the `event_type` field should be a free-text input with an autocomplete suggester populated from a runtime-observed set. Add a small `EventBus.observed_event_types() -> set[str]` accessor in Phase 4 if it doesn't already exist (it doesn't), and have `InMemoryEventBus.publish()` accumulate each new event type in a set. ConfigurationService's goal-create endpoint can then query this set and pass it to the UI for autocomplete, or simply surface it as a `/api/event-types` endpoint.

## 4. Conversation auto-archive policy

**Status:** Explicit-only
**Findings:** `src/gilbert/core/services/ai.py:6302` publishes archiving in the `_ws_conversation_delete` RPC handler (user-initiated delete), and `src/gilbert/core/services/ai.py:6499` publishes archiving from `_ws_room_leave` when the conversation owner leaves a shared conversation room. No scheduler jobs or time-based archive logic exists in AIService. Conversations persist indefinitely unless explicitly deleted by the owner.
**Follow-up:** None — agent goal conversations stay forever unless explicitly deleted. Phase 4 plan adds a `pinned: bool = False` flag on conversations (set to True for goal conversations) to prevent accidental deletion via future bulk-cleanup features, but no such cleanup currently exists.

## 5. Workspace cleanup hooks via chat.conversation.destroyed

**Status:** Subscribed and cleans
**Findings:** `src/gilbert/core/services/workspace.py:209-211` — WorkspaceService subscribes to `chat.conversation.destroyed` with handler `_on_conversation_destroyed`. Handler confirmed on lines 285-376 to: (1) delete all entries from `_WORKSPACE_FILES_COLLECTION` for the conversation_id via storage.query/delete loop (lines 293-316), (2) delete the workspace directory under `.gilbert/workspaces/users/<user_id>/conversations/<conversation_id>/` by resolving both new and legacy layout paths (lines 318-375), with defense-in-depth path validation to prevent directory traversal (lines 359-366) and `shutil.rmtree` cleanup (line 370). Note: the design spec refers to `chat.conversation.archiving` but the workspace service actually subscribes to `chat.conversation.destroyed` (the destruction event published immediately after the archiving event in the same delete handler). Phase 4 goal-delete code must publish `chat.conversation.destroyed` after `chat.conversation.archiving` to trigger workspace cleanup, matching the existing chat delete pattern.
**Follow-up:** None — workspace cleanup is already wired and working.

## 6. Conversation auth model — shared read access

**Status:** Owner-only (no ACL extension for personal conversations)
**Findings:** Conversations have two access models: (1) **Personal conversations** (`shared=false`, the default) — owned by a single user via `user_id` field at `src/gilbert/core/services/ai.py:3938`. Access is NOT enforced at message load (`_load_conversation` at line 4002 returns unconditionally). (2) **Shared rooms** (`shared=true`) — room members listed in `members: list[dict]` array (line 3871) can read/write. Per-message visibility control exists (`Message.visible_to` at `src/gilbert/interfaces/ai.py:137`), but is for hiding messages *within* a shared room, not a conversation-level ACL for non-shared conversations. No per-conversation read-access list exists for personal conversations — the conversation is either owned or private.
**Follow-up:** Phase 4 plan must either: (a) Add per-conversation read-access ACL (new `read_access: list[str]` field on conversation entity + access-check refactor in `_load_conversation` and `_ws_chat_send` at line 5430-5439), OR (b) Scope agents to single-user goals only in v1 (`notify_user_ids` constrained to `[owner_user_id]` to avoid multi-user sharing). Flag as a scope decision for Phase 4 pre-flight review.

## 7. AI-call log named-call mechanism

**Status:** Mechanism documented
**Findings:** Callers pass `ai_call="<name>"` to `AIService.chat()` at `src/gilbert/core/services/ai.py:2000` to tag the interaction with a symbolic name. The AI service resolves the profile via `self.get_profile(ai_call)` at line 2113, looking up the name in an assignment table (`self._assignments` at line 1775) that maps call names to profiles. This allows each named call (like `"user_memory_synthesis"`, `"roast"`, `"greeting"`, etc.) to be routed to a distinct AI context profile with custom model/backend/tool settings. Log entries for all AI API calls are written to the file configured at `src/gilbert/config.py:83` (default: `.gilbert/ai_calls.log`), via the `gilbert.ai` logger at `src/gilbert/core/services/ai.py:73`. The logger is configured at `src/gilbert/core/logging.py:74-86` to write to a separate file with timestamp, level, logger name, and message. Existing named calls in use: `user_memory_synthesis` (line 59), `roast` (line 57 of roast.py), `greeting` (line 69 of greeting.py), `scheduled_action` (line 236 of scheduler.py), `inbox_ai_chat` (line 73 of inbox_ai_chat.py), `inbox_compose`, `inbox_reply` (line 159 of inbox.py), `record_observation` (line 426 of proposals.py). The `ServiceInfo.ai_calls` field (src/gilbert/interfaces/service.py:18) declares which call names a service will use, allowing tooling to discover available names at runtime.
**Follow-up:** Phase 4 plan: AgentService declares `ai_calls=frozenset({"agent.run", "agent.digest"})` in its `ServiceInfo`, and calls `AIService.chat(ai_call="agent.run")` for per-round generation and `ai_call="agent.digest"` for post-run summarization. The assignment table allows an operator to assign different profiles to each, enabling cost/latency trade-offs (e.g., cheaper model for digest, more capable for run). Phase 4 verifies names land correctly in the log by inspecting `.gilbert/ai_calls.log` and confirming both `"agent.run"` and `"agent.digest"` appear in log entries (or profile names if assignment table is used).
