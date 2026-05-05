# Agent Messaging — Phase 2: Peer Messaging (Queue Mode) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three new agent tools — `agent_list`, `agent_send_message`, `agent_delegate` — wired through the existing `_signal_agent` dispatch and `InboxSignal` storage that Phase 1A already shipped. Queue-mode delivery: signals are drained between rounds and formatted as user-role messages prepended to the next round's chat input. Delegation adds a per-call `asyncio.Future`, cycle detection (rejects A→B→A), depth cap (5), and a `max_wait_s` timeout (default 600 s). Cross-user blocked: targets must share `owner_user_id` with the caller. **Acceptance:** A→B `agent_send_message` lands in B's next loop. A→B `agent_delegate` returns B's final assistant text. A→B→A delegation rejected before fire. Delegation timeout returns an error string to the caller without cancelling the target's run.

**Architecture:** Phase 1A established the persistence layer (`agent_inbox_signals` collection, `InboxSignal` dataclass), the dispatch primitive (`_signal_agent`), the in-memory cache (`_inboxes: dict[agent_id, list[InboxSignal]]`), the cache rehydrate-on-start (`_rehydrate_inboxes`), and the drain helper (`_drain_inbox`). What Phase 1A *did not* do is integrate the drain into `_run_agent_internal` — drained signals are not yet formatted into the prompt, and `between_rounds_callback` is not passed to `AIService.chat`. Phase 2 closes that loop, then adds the three tools that produce signals.

**Tech Stack:** Python 3.12+, `uv run` for everything; pytest with the existing `started_agent_service` fixture; React Query + the `frontend/src/api/agents.ts` client from Phase 1B.

**Out of scope for Phase 2:**
- Mid-stream interrupt — Phase 3. Queue mode only: signals delivered at round boundaries, never inside a round.
- Goals, war rooms, deliverables — Phases 4 and 5. `agent_list` only returns peer agents, no goal context.
- `goal_post`, `goal_*` tools — Phase 4.
- Cross-user — Phase 6. Same-owner only.
- Persisting peer-message *content* into the target's chat conversation. The InboxSignal's `body` field carries the prose; the agent reads it from the drained signal directly. Writing user-role rows into the target's chat history (so the user sees peer DMs in the conversation thread) is a polish pass, deferred to a follow-up. Open question recorded below.
- Sender-attribution badges in chat UI — depends on the chat-conv persistence above; deferred together.
- Dreaming — Phase 7.
- Frontend Inbox tab on the agent detail page (showing pending signals before drain). Inbox visibility is implicit through the Runs tab's `triggered_by="inbox"` rows; a dedicated tab is a follow-up.

**Out of scope rationale (no chat-conv persistence):** doing it right requires a service method on `AIProvider` to inject a USER-role message into another user/agent's conversation, including conv-ACL safety, dedupe, and an `author_id`/`author_name` write path that respects existing `Message` shape. That's its own design effort; in Phase 2 the body lives in `InboxSignal.body` and gets formatted into the next-round prompt directly. The agent loop sees exactly the same prose either way; only the user-facing chat-history surface diverges. We'll wire the chat-conv path in a polish phase once peer messaging is stable.

---

## File Structure

**Create:**
- `tests/unit/test_agent_peer_messaging.py` — covers all three new tools end-to-end + cycle / depth / timeout for delegation.

**Modify:**
- `src/gilbert/core/services/agent.py`:
  - Add three `ToolDefinition`s + their `_exec_*` handlers (`agent_list`, `agent_send_message`, `agent_delegate`).
  - Extend `_CORE_AGENT_TOOLS` with the three new names.
  - Wire inbox drain into `_run_agent_internal`: drain at round 0; pass `between_rounds_callback` to `AIService.chat`.
  - Add `_pending_delegations: dict[str, asyncio.Future[str]]` to `__init__`.
  - Resolve the matching `Future` in `_run_agent_internal` when a delegation-triggered run finishes.
  - Add a small helper `_format_inbox_signal(sig: InboxSignal) -> Message` so the round-0 drain and the between-rounds callback share the formatting.
  - Update `_synthesize_trigger_message` for `triggered_by="inbox"` and `"delegation"`.
- `frontend/src/components/agent/ToolPicker.tsx` — extend `CORE_TOOLS` Set so the three new tools render checked-and-disabled.
- `.claude/memory/memory-agent-service.md` — append "Phase 2 — peer messaging" subsection.

**Delete:** none.

**Out-of-pocket changes that may surface during implementation:**
- The existing `_drain_inbox` returns `list[InboxSignal]`. Phase 2's between-rounds callback needs `list[Message]`. Either map at call site or change `_drain_inbox` to return `list[Message]`. Recommended: keep `_drain_inbox` unchanged (still returns signals — useful for tests / future inspection) and add `_format_inbox_signal` separately.
- `_run_agent_internal` currently builds `user_message` from either the explicit caller arg or `_synthesize_trigger_message`. After Phase 2, drained signals live as additional `Message(role=USER, ...)` entries that need to be threaded into the conversation history alongside the synthesized trigger message. The cleanest path: let `_synthesize_trigger_message` produce the lead user message, and pass drained signals as a `messages_prefix` (or similar) that the AI service prepends. Read `AIService.chat` carefully — it accepts `user_message: str`, not a list. So drained signals will be concatenated into the lead user message text, with a clear separator. Keep it simple.
- Tools that return JSON to the agent: every existing `_exec_*` returns a string (the AI tool runtime stringifies whatever the tool returns). `agent_list` should return JSON-as-string for parseability.

---

## Tasks

### Task 1: Inbox drain wired into `_run_agent_internal`

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_inbox.py` — extend with drain-into-run tests.

The current `_run_agent_internal` ignores `_inboxes`. Phase 2 fix:

- [ ] **Step 1: Add `_format_inbox_signal` helper.**

```python
def _format_inbox_signal(self, sig: InboxSignal) -> str:
    """Convert an InboxSignal into a one-line user-role prose snippet.

    Peer messages format as ``[from {sender_name}]: {body}``;
    system signals format as ``[system: {body}]``.
    """
    if sig.sender_kind == "agent" or sig.sender_kind == "user":
        prefix = f"[from {sig.sender_name}]"
    else:
        prefix = "[system]"
    return f"{prefix}: {sig.body}"
```

The format follows the spec verbatim ("Conversation Routing" → "Inbox queue mechanic" subsection).

- [ ] **Step 2: Drain inbox at round 0 of `_run_agent_internal`.**

After the existing `user_msg = user_message or self._synthesize_trigger_message(...)` line, drain pending signals:

```python
drained = await self._drain_inbox(a.id)
if drained:
    inbox_block = "\n".join(self._format_inbox_signal(s) for s in drained)
    user_msg = f"{user_msg}\n\nINBOX:\n{inbox_block}"
```

This appends the drained content to the synthesized user message. The agent sees its trigger reason + a clearly-marked `INBOX:` block in one user-role chat turn.

- [ ] **Step 3: Pass `between_rounds_callback` to `AIService.chat`.**

```python
async def _between_rounds() -> list[Message]:
    sigs = await self._drain_inbox(a.id)
    if not sigs:
        return []
    return [
        Message(
            role=MessageRole.USER,
            content=self._format_inbox_signal(s),
        )
        for s in sigs
    ]

result = await self._ai.chat(
    user_message=user_msg,
    conversation_id=a.conversation_id or None,
    user_ctx=user_ctx,
    system_prompt=system_prompt,
    ai_call=_AI_CALL_NAME,
    ai_profile=a.profile_id,
    between_rounds_callback=_between_rounds,
)
```

Imports: `from gilbert.interfaces.ai import Message, MessageRole` — confirm at top of file (likely already present from existing tooling).

- [ ] **Step 4: Update `_synthesize_trigger_message`.**

Add cases:

```python
if triggered_by == "inbox":
    return "You have new inbox messages. Read them below and respond as appropriate."
if triggered_by == "delegation":
    sender = ctx.get("sender_id", "?")
    return (
        f"You are handling a delegation request. Read the instruction below "
        f"and end your turn with a clear conclusion — your final assistant "
        f"message becomes the reply to {sender}."
    )
```

Confirm the existing `triggered_by="manual" / "heartbeat" / "time" / "event"` branches still match the function's current behavior; only ADD cases.

- [ ] **Step 5: Tests.**

In `tests/unit/test_agent_inbox.py`, add:
- `test_run_drains_inbox_at_round_zero` — pre-populate `_inboxes[agent_id]` with two signals; run the agent; assert the AI provider received `user_message` containing `[from {sender}]: {body}` for each.
- `test_run_drains_inbox_between_rounds` — wire the FakeAIProvider to fire `between_rounds_callback` once (mock it returning a multi-round response); pre-stage a signal during round 0 → 1 transition; assert the callback returned a USER `Message` with the expected content.

The existing `_FakeAIProvider` likely doesn't exercise `between_rounds_callback`. Either (a) extend it to optionally invoke the callback once, or (b) write a test-only fake that does. Lean toward (a) — add an `invoke_between_rounds: bool` flag.

`uv run pytest tests/unit/test_agent_inbox.py tests/unit/test_agent_service.py -x`

---

### Task 2: `agent_list` tool

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_peer_messaging.py` (new file).

- [ ] **Step 1: ToolDefinition.**

```python
_TOOL_AGENT_LIST = ToolDefinition(
    name="agent_list",
    description=(
        "List your peer agents (other agents owned by the same user). "
        "Returns name, role_label, status, conversation_id."
    ),
    parameters=[],
    slash_command="agent_list",
    slash_help="List your peer agents.",
)
```

- [ ] **Step 2: Handler.**

```python
async def _exec_agent_list(self, args: dict[str, Any]) -> str:
    agent_id = args.get("_agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return "error: agent_list requires _agent_id"
    me = await self.get_agent(agent_id)
    if me is None:
        return "error: caller agent not found"
    peers = await self.list_agents(owner_user_id=me.owner_user_id)
    out = [
        {
            "name": p.name,
            "role_label": p.role_label,
            "status": p.status.value,
            "conversation_id": p.conversation_id,
        }
        for p in peers
        if p.id != me.id  # exclude self
    ]
    return json.dumps(out)
```

Add `import json` at the top if missing.

- [ ] **Step 3: Register in `get_tools` + `execute_tool` + `_CORE_AGENT_TOOLS`.**

- [ ] **Step 4: Tests.**

In the new `tests/unit/test_agent_peer_messaging.py`:
- `test_agent_list_returns_peers_owner_scoped` — create three agents under user A, one under user B; agent A1 calls list; assert returns A2, A3, NOT A1 (self-excluded), NOT B1.

---

### Task 3: `agent_send_message` tool

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_peer_messaging.py`.

- [ ] **Step 1: ToolDefinition.**

```python
_TOOL_AGENT_SEND_MESSAGE = ToolDefinition(
    name="agent_send_message",
    description=(
        "Send a fire-and-forget direct message to a peer agent. The peer's "
        "loop wakes (or, if running, picks up the message between rounds). "
        "No reply is awaited — use agent_delegate if you need a response."
    ),
    parameters=[
        ToolParameter(name="target_name", type=ToolParameterType.STRING,
                      description="The peer agent's name.", required=True),
        ToolParameter(name="body", type=ToolParameterType.STRING,
                      description="Message body.", required=True),
    ],
    slash_command="agent_send_message",
    slash_help="DM another agent.",
)
```

- [ ] **Step 2: Helper to resolve target by name within owner.**

```python
async def _load_peer_by_name(
    self,
    *,
    caller_agent_id: str,
    target_name: str,
) -> Agent:
    """Resolve a peer agent by name within the caller's owner. Raises
    ``PermissionError`` if no agent matches in the same owner namespace
    (cross-user reach is treated as a permission failure, not a missing
    record, to avoid leaking that the name exists)."""
    me = await self.get_agent(caller_agent_id)
    if me is None:
        raise PermissionError("caller agent not found")
    rows = await self._storage.query(  # type: ignore[union-attr]
        Query(
            collection=_AGENTS_COLLECTION,
            filters=[
                Filter(field="owner_user_id", op=FilterOp.EQ, value=me.owner_user_id),
                Filter(field="name", op=FilterOp.EQ, value=target_name),
            ],
        )
    )
    if not rows:
        raise PermissionError(f"no peer named {target_name!r}")
    return _agent_from_dict(rows[0])
```

This goes in `AgentService` near `_load_agent_for_caller`.

- [ ] **Step 3: Handler.**

```python
async def _exec_agent_send_message(self, args: dict[str, Any]) -> str:
    agent_id = args.get("_agent_id")
    target_name = str(args.get("target_name", "")).strip()
    body = str(args.get("body", "")).strip()
    if not isinstance(agent_id, str) or not agent_id:
        return "error: missing _agent_id"
    if not target_name:
        return "error: target_name is required"
    if not body:
        return "error: body is required"

    me = await self.get_agent(agent_id)
    if me is None:
        return "error: caller agent not found"

    try:
        target = await self._load_peer_by_name(
            caller_agent_id=agent_id, target_name=target_name,
        )
    except PermissionError as exc:
        return f"error: {exc}"

    if target.id == me.id:
        return "error: cannot message yourself"

    await self._signal_agent(
        agent_id=target.id,
        signal_kind="inbox",
        body=body,
        sender_kind="agent",
        sender_id=me.id,
        sender_name=me.name,
    )
    return f"sent to {target_name}"
```

- [ ] **Step 4: Register + tests.**

Tests in `tests/unit/test_agent_peer_messaging.py`:
- `test_agent_send_message_signals_target` — A1 sends to A2; assert an `InboxSignal` row exists for A2 with `sender_id=A1.id`, `body="hello"`.
- `test_agent_send_message_blocks_cross_owner` — A1 (user A) targets B1 (user B) → tool returns `error: ...` and no signal row created.
- `test_agent_send_message_self_rejected` — A1 → A1 returns error.
- `test_agent_send_message_idle_peer_fires_run` — A2 idle → after the call, eventually A2's run task is scheduled (verify `_running_agents` ever contained A2 OR check that the new run row was created within a short timeout).

---

### Task 4: `agent_delegate` tool — cycle detection + Future-based timeout

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_peer_messaging.py`.

The mechanic: caller awaits an `asyncio.Future` that the target's run resolves on completion. Target's run is signaled with `signal_kind="delegation"` carrying the chain.

- [ ] **Step 1: ToolDefinition.**

```python
_TOOL_AGENT_DELEGATE = ToolDefinition(
    name="agent_delegate",
    description=(
        "Send a message to a peer and await its END_TURN reply. The peer "
        "gets a system-prompt note saying it is handling a delegation; its "
        "final assistant message becomes your tool result. Errors on "
        "circular delegations or when the delegation chain depth would "
        "exceed 5. Default timeout is 600 seconds."
    ),
    parameters=[
        ToolParameter(name="target_name", type=ToolParameterType.STRING,
                      description="The peer agent's name.", required=True),
        ToolParameter(name="instruction", type=ToolParameterType.STRING,
                      description="What you want the peer to do.", required=True),
        ToolParameter(name="max_wait_s", type=ToolParameterType.NUMBER,
                      description="Timeout in seconds (default 600).", required=False),
    ],
    slash_command="agent_delegate",
    slash_help="Delegate work to another agent and await its reply.",
)
```

- [ ] **Step 2: Pending-delegations dict.**

In `AgentService.__init__`:
```python
self._pending_delegations: dict[str, asyncio.Future[str]] = {}
```

`_DELEGATION_DEPTH_CAP = 5` near other module-level constants.

- [ ] **Step 3: Handler.**

```python
async def _exec_agent_delegate(self, args: dict[str, Any]) -> str:
    agent_id = args.get("_agent_id")
    target_name = str(args.get("target_name", "")).strip()
    instruction = str(args.get("instruction", "")).strip()
    max_wait_s_raw = args.get("max_wait_s", 600)
    try:
        max_wait_s = max(1, int(max_wait_s_raw))
    except (TypeError, ValueError):
        return "error: max_wait_s must be a number"
    if not isinstance(agent_id, str) or not agent_id:
        return "error: missing _agent_id"
    if not target_name or not instruction:
        return "error: target_name and instruction are required"

    me = await self.get_agent(agent_id)
    if me is None:
        return "error: caller agent not found"

    try:
        target = await self._load_peer_by_name(
            caller_agent_id=agent_id, target_name=target_name,
        )
    except PermissionError as exc:
        return f"error: {exc}"

    if target.id == me.id:
        return "error: cannot delegate to yourself"

    # Cycle + depth check.
    chain: list[str] = list(args.get("_delegation_chain", []))
    chain.append(me.id)
    if target.id in chain:
        return f"error: delegation cycle — {target.name} already in chain"
    if len(chain) >= _DELEGATION_DEPTH_CAP:
        return (
            f"error: delegation depth cap reached "
            f"({_DELEGATION_DEPTH_CAP})"
        )

    delegation_id = f"del_{uuid.uuid4().hex[:12]}"
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    self._pending_delegations[delegation_id] = future

    try:
        await self._signal_agent(
            agent_id=target.id,
            signal_kind="delegation",
            body=instruction,
            sender_kind="agent",
            sender_id=me.id,
            sender_name=me.name,
            delegation_id=delegation_id,
            metadata={"chain": chain},
        )
        try:
            reply = await asyncio.wait_for(future, timeout=max_wait_s)
        except asyncio.TimeoutError:
            return f"error: delegation to {target_name} timed out after {max_wait_s}s"
        return reply
    finally:
        self._pending_delegations.pop(delegation_id, None)
```

`_delegation_chain` is read from injected tool args — meaning a delegated-to run that *itself* delegates is responsible for forwarding the chain. To make that automatic, modify the tool argument injection (Phase 1A) to ALSO inject `_delegation_chain` when the active Run carries a `delegation_id`. See Step 4.

- [ ] **Step 4: Plumb the delegation chain through the run context.**

In `_run_agent_internal`, when a run is delegation-triggered (`triggered_by == "delegation"`), capture the chain from `trigger_context.get("chain", [])` onto the run AND propagate it to tool argument injection.

The existing argument injection wrapper (search for `_inject_agent_id` in agent.py) currently only injects `_agent_id`. Extend it: if the active run has a non-empty chain, also inject `_delegation_chain`.

If `_inject_agent_id` doesn't currently support extra fields, change its signature to accept a `chain: list[str] | None` and add the field conditionally.

The trigger_context for delegation runs needs to be enriched. Look at `_signal_agent` → `_run_with_signal` → `_run_agent_internal` flow. The `_run_with_signal` currently passes `trigger_context={"signal_id": sig.id, "sender_id": sig.sender_id}`. Phase 2: also include `"chain": sig.metadata.get("chain", [])` and `"delegation_id": sig.delegation_id`. Then `_run_agent_internal` reads chain and forwards into the tool injection.

- [ ] **Step 5: Resolve the Future on delegation-triggered run completion.**

At the end of `_run_agent_internal`, after the run's `final_message_text` is set and the row persisted:

```python
delegation_id = trigger_context.get("delegation_id", "")
if delegation_id:
    fut = self._pending_delegations.get(delegation_id)
    if fut is not None and not fut.done():
        if run.status is RunStatus.COMPLETED:
            fut.set_result(run.final_message_text or "")
        else:
            fut.set_exception(
                RuntimeError(f"delegation target run {run.status.value}")
            )
```

The caller's `wait_for` then returns the result. Tools that already failed (`run.status == FAILED`) propagate as an exception to the caller — the caller's `_exec_agent_delegate` catches `asyncio.TimeoutError` but not `RuntimeError`; let it bubble up so the AI sees the error in its tool result. (`execute_tool` callers in the AI tooling layer wrap exceptions into error strings — confirm by reading the call site.)

- [ ] **Step 6: Inject a system note for delegation-triggered runs.**

`_build_system_prompt` currently appends a HEARTBEAT block when `triggered_by == "heartbeat"`. Add a delegation block:

```python
if triggered_by == "delegation":
    parts.append(
        "You are handling a delegation from a peer. Read the request "
        "(in the inbox) and respond. Your final assistant message is "
        "returned as the reply — end your turn cleanly with a complete "
        "answer, no follow-up actions."
    )
```

- [ ] **Step 7: Register + tests.**

Tests:
- `test_agent_delegate_round_trip` — A1 calls delegate(target=A2, instruction="say hi"); FakeAIProvider's chat for A2 returns "hello back" as final message; assert delegate returns "hello back".
- `test_agent_delegate_cycle_rejected` — pre-populate `args["_delegation_chain"]=[A2.id]`; A2 calls delegate(A1) — assert error string mentioning cycle. (Simulates A1→A2→A1.)
- `test_agent_delegate_depth_cap` — chain of length 4; the 5th delegate returns the cap error.
- `test_agent_delegate_timeout` — set max_wait_s=1; FakeAIProvider's target chat hangs (or doesn't resolve fast enough); assert error string mentions timeout.
- `test_agent_delegate_target_failure` — target's run fails; the caller's tool result is an error (the RuntimeError exception path).
- `test_agent_delegate_resolves_future_after_run` — verify that on a normal delegation, `_pending_delegations[id]` is cleaned up after resolution.

---

### Task 5: Tool registration + core set + slash commands

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_slash_command_uniqueness.py` if it exists.

- [ ] **Step 1: Add the three tools to `_CORE_AGENT_TOOLS`.**

```python
_CORE_AGENT_TOOLS: frozenset[str] = frozenset({
    # Phase 1A — agent self-management
    "complete_run",
    "request_user_input",
    "notify_user",
    "commitment_create", "commitment_complete", "commitment_list",
    "agent_memory_save", "agent_memory_search",
    "agent_memory_review_and_promote",
    # Phase 2 — peer messaging
    "agent_list",
    "agent_send_message",
    "agent_delegate",
})
```

- [ ] **Step 2: `get_tools()` and `execute_tool()` updated.**

Add the three tool definitions to the list returned by `get_tools()`. Add the three name → handler dispatches in `execute_tool()`.

- [ ] **Step 3: Slash command uniqueness test.**

Run: `uv run pytest tests/unit/test_slash_command_uniqueness.py -x`

If it exists and fails because the new tool slash commands collide with anything, rename. (Spec already declares `slash_namespace="agents"` on the service so the full slash names will be `/agents agent_list`, etc., but the per-tool `slash_command` should still be unique within the service. The names listed above are unique to Phase 2.)

---

### Task 6: Frontend — extend `ToolPicker.CORE_TOOLS`

**Files:**
- Modify: `frontend/src/components/agent/ToolPicker.tsx`.

The component currently hard-codes the Phase 1A core set. Phase 2 adds three:

- [ ] **Step 1: Update the `CORE_TOOLS` Set.**

```ts
const CORE_TOOLS = new Set([
  // Phase 1A
  "complete_run", "request_user_input", "notify_user",
  "commitment_create", "commitment_complete", "commitment_list",
  "agent_memory_save", "agent_memory_search",
  "agent_memory_review_and_promote",
  // Phase 2
  "agent_list", "agent_send_message", "agent_delegate",
]);
```

- [ ] **Step 2: Verify tsc.**

```bash
/home/assistant/.nvm/versions/node/v22.22.2/bin/node /home/assistant/gilbert/node_modules/typescript/bin/tsc -b /home/assistant/gilbert/frontend/tsconfig.json
```

Exit 0.

This is the only frontend change in Phase 2. There is no chat-side sender attribution UI in this phase (see "Out of scope" rationale).

---

### Task 7: Verification — full suite + lint + mypy + tsc

- [ ] **Step 1.** `uv run pytest -x`
- [ ] **Step 2.** `uv run ruff check src/gilbert/core/services/agent.py tests/unit/test_agent_peer_messaging.py tests/unit/test_agent_inbox.py`
- [ ] **Step 3.** `uv run mypy src/gilbert/core/services/agent.py`
- [ ] **Step 4.** `npm run --workspace frontend tsc -b` (or the node-direct invocation).
- [ ] **Step 5.** Architecture audit pass per CLAUDE.md "Architecture Violation Checklist":
  - All new tools are core (force-included) — confirm `_CORE_AGENT_TOOLS` includes them.
  - `agent_send_message` and `agent_delegate` enforce same-owner via `_load_peer_by_name`.
  - No private-data leakage in the new tool descriptions.
  - Slash commands declared on every new tool; namespace inherited from `slash_namespace="agents"`.
  - Tests assert ownership rejection for cross-owner calls.

---

### Task 8: Memory file update

**Files:**
- Modify: `.claude/memory/memory-agent-service.md`.

Append a "Phase 2 — Peer messaging (queue mode)" subsection covering:
- Three new core tools and their semantics.
- Inbox drain at round 0 + `between_rounds_callback`.
- `_pending_delegations` dict + cycle/depth/timeout.
- The deferral note about chat-conv persistence.

Index in `MEMORIES.md` is already pointing at this file; no new index entry needed.

---

## Test Strategy

| Category | Coverage |
|---|---|
| **Unit — inbox drain** | Round-0 drain formats peer DMs as `[from {sender}]: {body}`. Between-rounds callback is invoked and returns `Message(role=USER, ...)` entries. Drain is idempotent (re-running drain after no new signals returns []). |
| **Unit — agent_list** | Owner-scoped peer enumeration; self-excluded; cross-owner agents not visible. |
| **Unit — agent_send_message** | Idle target → run scheduled. Busy target → signal queued, picked up next round. Cross-owner target rejected. Self-target rejected. |
| **Unit — agent_delegate happy path** | Caller awaits Future; target's `final_message_text` becomes the tool result. `_pending_delegations` cleaned up. |
| **Unit — agent_delegate cycle** | A→B→A rejected by chain check. |
| **Unit — agent_delegate depth** | Chain length 5 → reject. |
| **Unit — agent_delegate timeout** | `max_wait_s=1` with target run unresolved → tool returns timeout error string. Caller's run continues. Target run NOT cancelled. |
| **Unit — agent_delegate target failure** | Target run fails → caller's `await` raises; tool result contains the error. |
| **Unit — multi-user isolation** | Concurrent runs of two owners' agents don't see each other's signals. Future map keyed by delegation_id, not agent_id, so no cross-user leakage. |
| **Backend lint / type-check** | `ruff` and `mypy` on the modified files. |
| **Frontend** | `tsc -b` passes after `ToolPicker.CORE_TOOLS` extension. |
| **Slash uniqueness** | `tests/unit/test_slash_command_uniqueness.py` passes. |

---

## Open Questions / Future

- **Chat-conv persistence.** When `agent_send_message` is called, should the body also be inserted as a USER-role row in the target agent's chat conversation (with `metadata.sender = {kind: agent, ...}`)? Phase 2 says NO — the body lives in `InboxSignal.body`. The user can see it via the Runs tab once the target's run completes. A polish phase will write to chat history so the conversation thread shows peer DMs inline.
- **Sender-attribution UI.** Tied to the chat-conv question above. Once chat-conv writes are in, `frontend/src/components/chat/TurnBubble.tsx` (and the main chat conv renderer) will need branches for user-role messages whose `metadata.sender.kind === "agent"`.
- **Inbox tab on the agent detail page.** Today there's no SPA surface for unread signals. Phase 2 ships `triggered_by="inbox"` runs in the existing Runs tab; a dedicated Inbox tab is a follow-up.
- **Delegation timeout policy.** `max_wait_s=600` is the default. Should it be a per-agent config (`default_delegation_timeout_s`)? For Phase 2 we keep it as a literal default; later phases can hoist if operator-level customization is needed.
- **Delegation cancellation.** If the caller's run is cancelled mid-`await`, the target's run keeps going. Phase 2 accepts this — the design says delegation timeout doesn't cancel the target. A cooperative-cancel signal could be a Phase 3 add (alongside mid-stream interrupt).
- **Delegation concurrency.** A single agent can be delegated-to multiple times concurrently — each delegation gets its own `delegation_id` Future, but the same agent is `_running_agents`-guarded, so the second delegation's signal is queued until the first finishes. Acceptable for Phase 2; could revisit if it becomes a bottleneck.

---

## Related

- Spec: `docs/superpowers/specs/2026-05-04-agent-messaging-design.md` (Sections "Conversation Routing & Messaging", "Loop Model & Tool Surface")
- Phase 1A plan: `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md`
- Phase 1B plan: `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1b-ui.md`
- `.claude/memory/memory-agent-service.md`
- `.claude/memory/memory-agent-loop.md` (run_loop primitive)
- `.claude/memory/memory-multi-user-isolation.md`
- `src/gilbert/core/services/agent.py` (target file)
- `src/gilbert/core/services/ai.py` (`AIService.chat` `between_rounds_callback` integration point)
- `src/gilbert/interfaces/ai.py` (`Message`, `MessageRole`)
