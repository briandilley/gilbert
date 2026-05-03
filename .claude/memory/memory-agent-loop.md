# `core/agent_loop.run_loop`

## Summary
Pure async primitive that drives one AI tool-use loop. Used (eventually) by
both `AIService.chat()` and `AutonomousAgentService.run_goal()`. Lives in
`src/gilbert/core/agent_loop.py`.

## Details
Signature is keyword-only — `backend`, `system_prompt`, `messages`, `tools`
(`dict[str, tuple[ToolDefinition, ToolHandler]]`), `max_rounds`, optional
`max_wall_clock_s`, `max_tokens`, `model`. Returns `LoopResult` with the
final `Message`, full message history, `LoopStopReason`, round/token
counters, and an optional `error`.

Loop body: build `AIRequest`, call `backend.generate_stream()`, find
`MESSAGE_COMPLETE`, append assistant message, then:
- `END_TURN` → return `LoopStopReason.END_TURN`.
- `MAX_TOKENS` (backend-side) → return `LoopStopReason.MAX_TOKENS`. The
  primitive does NOT implement chat-style continuation; callers wrap if
  they need it.
- `TOOL_USE` → execute tools (parallel via `asyncio.gather` if
  `backend.capabilities().parallel_tool_calls` and >1 call), append a
  `TOOL_RESULT` message, continue.
- Anything else → break, fall through to MAX_ROUNDS.

Pre-iteration checks at the top of every round, in order: wall-clock
deadline, cumulative token budget. Tool-handler exceptions are caught and
formatted as error `ToolResult`s (`is_error=True`, `content="tool failed:
<repr>"`); the loop continues so the model can decide to recover.
Backend exceptions during the stream are caught and returned as
`LoopStopReason.ERROR` with the exception in `LoopResult.error`.

The loop is service-free — no event bus, no scheduler, no storage. Streaming
text deltas to UI clients, conversation persistence, and per-round usage
recording belong to the caller. The chat refactor in Phase 2 will keep
those concerns inside `AIService.chat()` and let `run_loop` stay pure.

## Related
- `src/gilbert/core/agent_loop.py`
- `tests/unit/core/test_agent_loop.py`
- `docs/superpowers/specs/2026-05-03-autonomous-agent-design.md`
- `docs/superpowers/plans/2026-05-03-autonomous-agent-phase-1-run-loop.md`
