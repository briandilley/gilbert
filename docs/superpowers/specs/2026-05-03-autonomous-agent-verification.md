# Autonomous Agent — Verification Findings

Companion to `2026-05-03-autonomous-agent-design.md`. Each section captures the answer to one open verification item from the spec, with file/line citations and (if applicable) a follow-up task assigned to a downstream phase plan.

## 1. push_to_user capability in web layer

**Status:** Missing
**Findings:** No per-user push helper exists. Connections are tracked in `src/gilbert/web/ws_protocol.py:286` as `_connections: set[WsConnection]` keyed only by the connection object itself. Adding `push_to_user(user_id, frame)` requires: (1) maintain `dict[user_id, set[WsConnection]]` updated on `register`/`unregister` in `WsConnectionManager`, (2) expose a `push_to_user` method on the `WsConnectionManager`, (3) declare a `user_ws_pusher` capability so other services can resolve it via `service_manager.get_by_capability("user_ws_pusher")`.
**Follow-up:** Phase 3 plan must include a task to add `push_to_user`. Tag this verification finding in the Phase 3 plan's pre-flight section.

## 2. SchedulerService.add_job idempotency on name

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

## 3. event_types registry / dynamic choices

**Status:** TBD
**Findings:** TBD
**Follow-up:** TBD

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
