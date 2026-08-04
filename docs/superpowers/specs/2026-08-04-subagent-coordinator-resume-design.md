# Intent-gated coordinator resume on background-agent completion

**Date:** 2026-08-04
**Status:** Design — approved shape, pending spec review

## Problem

When the coordinator (main chat AI) dispatches a **background** subagent, the multi-step
task stalls: the agent does its work and delivers a report, but the coordinator is never
re-driven, so any follow-up steps the coordinator intended to perform never run.

Reproduced example: user asked the main chat to "research lowrider oldies, find the top 50,
create a playlist, queue it up and play it." The `deep-research` agent
(`execution_mode="background"`) ran, wrote a research markdown file to the workspace, and
delivered a summary message — but the coordinator never resumed, so the playlist was never
created, queued, or played. It would only continue if the user sent another chat message.

### Root cause

`SubagentService` (`src/gilbert/core/services/subagent.py`) dispatches two ways:

- `execution_mode="sync"` — `execute_tool` blocks on `spawn()` and returns the agent's
  result as the tool result (`subagent.py:471`); the coordinator continues in the same turn.
  No gap.
- `execution_mode="background"` — `_run_agent_background` (`subagent.py:560`) detaches an
  asyncio task and immediately returns a placeholder string; the coordinator's turn ends.

On completion, `_run_agent_background` is **passive**: it writes the report file
(`_write_report`, `subagent.py:694`), appends a synthetic assistant message via
`append_assistant_message` (`_deliver`, `subagent.py:719`), fires a user notification, and
publishes `chat.stream.subagent_completed`. But:

- `chat.message.created` and the `chat.stream.subagent_*` events have **only frontend
  consumers** — no server-side subscriber re-drives the coordinator.
- `append_assistant_message` (`ai.py:4904`) documents that it relies on "the next AI turn"
  to see the result, but nothing triggers that turn.

There is no server-side hook that resumes the coordinator's turn when a background agent it
was waiting on completes.

## Goals

- When a background agent completes and the coordinator **declared** it had follow-up work,
  automatically drive one coordinator turn on the parent conversation to do that work.
- Standalone reports (no declared follow-up) stay quiet — no extra turn, no chatty summary.
- Support an opt-in **join**: when the coordinator dispatched a cohort it intends to wait on
  as a whole, resume fires once, after all members settle, seeded with all their results.
- Surface failures (solo and partial-cohort) to the coordinator so it can recover rather
  than silently stall.

## Non-goals

- Durability across process restart. Background runs today are in-memory/non-durable — a
  restart already loses the run itself, so durable continuations buy nothing yet. (Approach
  C, a scheduler-polled durable continuation queue, was considered and rejected as premature.)
- An implicit "wait for all in-flight agents" join. The join is **only** when the coordinator
  explicitly requested it via `join_group`; otherwise resume is per-agent.
- Changing the `sync` path (it already works).

## Design decisions (settled)

1. **Intent-gated, not always-on.** Resume only when the coordinator declared follow-up work
   at spawn time. Standalone reports stay passive.
2. **Explicit follow-up instruction**, captured verbatim per run (`on_complete`), not a bare
   boolean. The instruction is what makes the resume reliable and self-documents intent.
3. **Join is opt-in and explicit** via `join_group`. Per-agent resume is the default; the
   join only exists when the coordinator asked for it.
4. **Resume on failure** too — the coordinator gets a chance to recover.
5. **Approach A** for wiring: `SubagentService` drives the resume through a new AI-provider
   interface method; turn-concurrency control stays inside `AIService`.

## Architecture

The clean turn entry point already exists: `AIService.chat(...)` (`ai.py:2429`) is exactly
what the WebSocket personal-chat path calls (`ai.py:7129`). Its streaming is published to the
EventBus with a `visible_to` audience (`ai.py:4738`, `4782`), so a resumed turn renders live
in an open browser with no socket handle needed. Two constraints:

- `chat()` persists its `user_message` as a real `USER`-role row (visible in the transcript).
- A direct `chat()` call bypasses the `_in_flight_chats` concurrency guard
  (`ai.py:6931`), so a resume must not interleave with a live user turn on the same
  conversation (last-write-wins on the single `messages` doc otherwise).

### Component 1 — `spawn_agent` tool schema

Two new optional params (`get_tools`, `subagent.py:266`; `execute_tool`, `subagent.py:449`):

```
spawn_agent(agent_type, prompt, model?,
            on_complete?: str,   # follow-up instruction; empty = passive (today's behavior)
            join_group?: str)    # opt-in cohort id; resume fires once all members settle
```

- `on_complete` absent/empty → unchanged: report delivered, no resume.
- `on_complete` set, no `join_group` → **solo resume**: one coordinator turn when this run
  settles.
- `on_complete` set **with** `join_group` → **cohort resume**: the `on_complete` belongs to
  the group and fires once, when the last member settles. A per-member `on_complete` is
  ignored when `join_group` is present — the group's instruction wins.

Both values are threaded from `execute_tool` into `_run_agent_background` and stored on the
`_Run` record (new fields `on_complete: str`, `join_group: str`).

### Component 2 — `resume_turn` on the AI-provider protocol

Add to the AI provider protocol in `interfaces/ai.py`, implemented by `AIService`:

```python
async def resume_turn(
    conversation_id: str,
    user_ctx: UserContext,
    instruction: str,
    attachments: list[FileAttachment] | None = None,
    source: str = "subagent-resume",
) -> None
```

Implementation mirrors the WS personal path minus the frame plumbing: register the turn in
`_in_flight_chats` for the conversation, call
`self.chat(instruction, conversation_id=…, user_ctx=…, ai_profile=self._chat_profile)`, then
publish `chat.message.created` so an open browser renders the result. Keeping this in
`AIService` means the concurrency guard lives where it belongs; `SubagentService` never
touches `_in_flight_chats`.

### Component 3 — concurrency guard (defer-and-retry)

`resume_turn` checks `_in_flight_chats` for the conversation. If a live user turn is already
running there, it **defers** rather than interleaving two writers on the same `messages` doc:
the run/cohort is marked "resume pending" and retried when the in-flight turn completes
(hook the existing turn-completion point). Chosen over a blind serialized wait because a
research turn finishing mid-user-turn is a real race.

### Component 4 — solo resume flow (fixes the reported bug)

In `_run_agent_background`, after `_deliver(...)` posts the report (so history contains it
*before* the resume runs):

- if `run.on_complete` and no `run.join_group` → build the resume seed (Component 6) and call
  `self._ai.resume_turn(parent_conv, user_ctx, seed, attachments)`.

### Component 5 — join cohort: sealing and settling

Members register asynchronously across parallel `spawn_agent` calls, so "all members" is not
known up front. Design:

- A `_JoinGroup` registry keyed by `(parent_conversation_id, join_group)`, holding:
  `on_complete`, the set of member `subagent_id`s, each member's settled result
  (report or failure), and a `sealed` flag.
- **Sealing:** all `spawn_agent` calls for a group happen inside the one coordinator turn
  that spawned them. `SubagentService` subscribes to `chat.stream.turn_complete` for the
  parent conversation and **seals** the group when that turn ends — membership is then frozen
  to whoever registered. This is the first server-side consumer of a `chat.stream.*` event
  (today they are frontend-only).
- **Settling:** each member records its result as it finishes. Resume fires when
  `sealed AND all members settled` — seeded with all reports plus a note of any that failed
  (Component 7).

Rejected alternative: an explicit `join_size: int` on the first cohort spawn, firing when
`settled == join_size`. Simpler, but pushes correctness onto the model counting right. The
turn_complete seal is a small subscriber and removes that burden.

### Component 6 — the resume seed

A synthetic message giving the coordinator its instruction plus how to reach the results.
It persists as a real `USER`-role row (kept `USER` rather than expanding `chat()`'s contract),
so its provenance is made obvious with an `[automated]` prefix, e.g.:

> `[automated] The background agent(s) you dispatched have finished. Report(s): <path(s)>. Now: <on_complete>.`

- `deliver_as="report_file"`: the full report is a workspace file; the seed passes the
  path(s) and the coordinator reads them with its workspace tools.
- inline delivery: the report text is already in history; the seed just carries `on_complete`.

### Component 7 — failure handling

The failure path (`subagent.py:677`) settles into the same machinery:

- **Solo + `on_complete`:** still resume — seed says *"the agent failed: <reason>; you
  intended to: <on_complete>"* so the coordinator can retry, apologize, or reroute instead of
  stalling.
- **In a `join_group`:** record the failure, count it as settled; when the group fires,
  successful reports are included **plus** an explicit "these failed: …" list, so the
  coordinator decides whether it has enough to proceed.

## Data flow

```
coordinator turn
  └─ spawn_agent(deep-research, "...", on_complete="create playlist, queue, play")
       → _run_agent_background detaches; returns placeholder; coordinator turn ends
          └─ agent runs (AgentRunEngine → ai.chat headless on child conv)
             └─ on settle:
                 ├─ _write_report → outputs/research-*.md
                 ├─ _deliver → append_assistant_message (report in parent history)
                 ├─ notify_user
                 └─ if on_complete and not join_group:
                      resume_turn(parent_conv, user_ctx, seed, attachments)
                        └─ (defers if live user turn in-flight, else)
                           ai.chat(seed, parent_conv, chat_profile)  ← coordinator resumes
                              → creates playlist, queues, plays
```

Join variant: each member settles into `_JoinGroup`; `chat.stream.turn_complete` seals the
group; when sealed and all settled, one `resume_turn` fires with all reports.

## Testing

- **Solo:** background run with `on_complete` drives exactly one `resume_turn`; without it,
  zero.
- **Join:** three members; resume fires once, after the last settles and only after seal;
  partial-failure cohort surfaces the failures in the seed.
- **Concurrency:** resume defers while a live turn is in-flight, then runs after it completes.
- **Failure (solo):** failed run with `on_complete` resumes with a failure-framed seed.
- **Regression:** `on_complete` absent behaves exactly as today (report delivered, no resume).

Unit tests mock the AI provider (assert `resume_turn` call count/args) and the event bus
(drive `chat.stream.turn_complete` to exercise sealing). Follow existing subagent test
patterns.

## Files touched (anticipated)

- `src/gilbert/interfaces/ai.py` — add `resume_turn` to the AI-provider protocol.
- `src/gilbert/core/services/ai.py` — implement `resume_turn`; expose the turn-completion
  hook used for defer-and-retry.
- `src/gilbert/core/services/subagent.py` — `spawn_agent` schema + threading; `_Run` fields;
  `_JoinGroup` registry; `chat.stream.turn_complete` subscription; resume dispatch in the
  success and failure paths.
- Tests under the subagent test suite.

## Open items for implementation

- Exact shape of the turn-completion hook `AIService` exposes for defer-and-retry (reuse the
  existing `_in_flight_chats` completion point vs. a small callback registry).
- Whether `resume_turn` should short-circuit when the parent conversation no longer exists
  (user deleted it mid-run) — treat as best-effort no-op, consistent with `_deliver`.
