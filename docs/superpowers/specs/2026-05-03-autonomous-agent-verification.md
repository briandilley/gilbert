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

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 5. Workspace cleanup hooks via chat.conversation.archiving

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 6. Conversation auth model — shared read access

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 7. AI-call log named-call mechanism

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD
