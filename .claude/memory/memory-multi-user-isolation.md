# Multi-User Isolation

## Summary
Gilbert services are **singletons** — one `AIService`, one `SpeakerService`, etc. instantiated at boot and shared across every user, WebSocket connection, and in-flight chat turn. Any per-request state (active conversation id, active user, request-scoped locks) must live in `ContextVar`s or travel as function parameters — **never** as instance attributes — or two overlapping operations will trample each other. This is a cross-cutting correctness requirement, not a performance concern: the symptom of getting it wrong is "events from conversation A appear in conversation B" bugs that are hard to reproduce but easy to ship.

## Details

### Why this matters

The singleton + multi-user + async combination is the footgun. Every service gets instantiated once in `app.py`. Every WebSocket handler, chat RPC, or scheduled job calls into those singletons concurrently. Python's asyncio runs everything in one event loop, so two `chat()` calls overlap in time — the second one executes some code while the first is awaiting I/O.

If a service stores per-request state on `self`, the second caller's write overwrites the first caller's value. When the first caller's coroutine resumes and reads `self._current_conversation_id`, it gets the second caller's value. Events go to the wrong user, database rows get written under the wrong id, tool calls show up in the wrong UI.

This happened in `AIService._current_conversation_id` (fixed — now a `ContextVar` in `gilbert.core.context`). The symptom was "I'm seeing tool calls from other workspaces in my workspace." Users observed it immediately once the system had more than one active chat at a time.

### Required patterns

**For per-request identity (active user, active conversation, request ID, correlation ID, trace context):** use a `ContextVar` in `gilbert.core.context`. See `_current_user` and `_current_conversation_id` as the two examples. Entry points (WS handler, HTTP route, scheduler job) call `set_*(...)` once at the start of the request, and every downstream call reads via `get_*()`. Parallel `asyncio.Task`s created inside that request inherit the context automatically (or explicitly via `asyncio.Task(..., context=contextvars.copy_context())`).

**For request-scoped locks / queues / caches:** keyed by the request identity (`user_id`, `conversation_id`, target resource id). See `SpeakerService._speaker_locks: dict[speaker_id, asyncio.Lock]` and `WorkspaceService._path_locks: dict[str, asyncio.Lock]`. The dict itself lives on the singleton, but entries are keyed so per-request operations don't collide.

**For caller identity in tool arguments:** inject `_user_id`, `_user_name`, `_user_roles`, `_conversation_id`, `_invocation_source`, `_room_members` into `tc.arguments` as the AI service does (`_run_one_tool` in `core/services/ai.py`). Tools then read the *call's* identity from arguments, not from `self`.

**For parallel tool execution:** each task gets its own `contextvars.copy_context()` when spawned as an `asyncio.Task` so `ContextVar.set()` inside one task stays local. See `_execute_tool_calls` in `core/services/ai.py` — this is how `set_current_user` inside one parallel tool doesn't leak to its siblings.

### Forbidden patterns

1. **Instance attributes for per-request state.** Things like `self._current_conversation_id`, `self._current_user`, `self._active_request`, `self._pending_reply` on a singleton service are almost always bugs waiting to be triggered. If the attribute is set inside a request handler and read later in the same handler, it's suspect.
2. **Module-level mutables keyed by nothing.** `_current_session: dict = {}` at module scope, written to during one request and read during another. Same failure mode as instance attributes.
3. **Global locks for per-resource operations.** `self._announce_lock = asyncio.Lock()` serializes every announce across every speaker across every user. Gate by resource (per-speaker, per-path, per-conversation) instead so unrelated operations fan out.
4. **Reading `self._current_*` after an `await`.** Even if the attribute is set correctly at entry, an `await` can suspend the coroutine long enough for another request to overwrite it. If you need a value that persists across `await` boundaries in the same request, capture it into a local variable at the start AND pass it as a parameter to helper methods — don't re-read from `self`.

### Audit questions to ask per service

When reviewing a service for multi-user safety, walk through every instance attribute in `__init__` and ask:

- **Is this value the same for every user, every request, for the service's lifetime?** (Config, backend handles, storage refs — fine to keep on `self`.)
- **Or is this value request-scoped — different per user, per conversation, per turn?** (Must be a `ContextVar`, passed as a parameter, or keyed dict.)

Any attribute with a name like `_current_*`, `_active_*`, `_pending_*`, `_last_*` is a red flag worth a second look. `_last_*` is often just a cache of "most recent thing seen by any caller" — occasionally legitimate (e.g., `_last_speaker_ids` as a lazy default when no speakers are specified), but needs a conscious decision.

For any `asyncio.Lock` or `asyncio.Semaphore`, ask: does this lock protect a resource that's *shared globally* (e.g., a single hardware device that can only do one thing) or a resource that's *per-user / per-target* (e.g., one of many speakers, one of many workspace paths)? Global locks that actually protect per-target resources serialize unrelated work.

### Entry points where contexts get set

- **WebSocket handlers** (`web/ws_protocol.py`) — set `_current_user` at connection / frame dispatch; the chat RPC handler sets `_current_conversation_id` via `AIService.chat()`.
- **HTTP routes** (`web/routes/*`) — same: set user context from the authenticated session before calling services.
- **Scheduler jobs** (`core/services/scheduler.py`) — run in the background without a human user. Set `_current_user` to `UserContext.SYSTEM` explicitly (or leave unset — `get_current_user()` defaults to SYSTEM). Set `_current_conversation_id` if the job operates on a specific conversation.
- **MCP / external tool callbacks** — any time a tool call is routed from an external source, treat it like a new request: set the identity ContextVars at the edge.

### How to fix a violation

1. Identify the per-request attribute on `self`.
2. If it matches an existing `ContextVar` concept (user id, conversation id), migrate to that — usually `get_current_*()` for reads, `set_current_*()` at the entry point for writes.
3. If it's a new concept, add a `ContextVar` to `gilbert.core.context` with a matching getter/setter and a docstring explaining why it's there.
4. Remove the instance attribute. The absence is a feature — it forces future code to go through the ContextVar.
5. Update tests that directly assigned to the instance attribute to call the setter instead (see `test_ai_service.py` for the pattern: `set_current_conversation_id("conv-x")` before the code under test runs).

### Related architecture-checklist section

The "Multi-User Isolation" section of `memory-architecture-checklist.md` lists the specific grep patterns to run when auditing. Keep these two memories in sync — the principle lives here, the mechanical audit lives in the checklist.

## Related
- [AI Service](memory-ai-service.md) — example of per-request state migrated to ContextVars (`_current_conversation_id`)
- [Speaker System](memory-speaker-system.md) — per-target locks (per-speaker) example
- [Architecture Violation Checklist](memory-architecture-checklist.md) — the auditable version of this
- `src/gilbert/core/context.py` — where `_current_user` and `_current_conversation_id` live
