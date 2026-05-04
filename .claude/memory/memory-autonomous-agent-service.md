# AutonomousAgentService

## Summary
Persists Goal/Run entities and executes goals via ``AIService.chat(ai_call="agent.run")``.
Lives in ``src/gilbert/core/services/agent.py``.

## Details
**Capabilities declared:** ``agent`` (satisfies ``AgentProvider``),
``ai_tools`` (exposes the ``complete_goal`` tool), ``ws_handlers``.

**Ai call name:** ``agent.run`` — registered via ``ai_calls`` in
``ServiceInfo``. Operators can route this call to a distinct profile
via the AI profile assignment table.

**Execution model:** ``run_goal_now(goal_id)`` synthesizes a user
message from the goal's instruction, calls ``AIService.chat()`` with
``ai_call="agent.run"`` and ``ai_profile=goal.profile_id``, captures the
result into a ``Run`` entity. Each run gets its own chat conversation
(``Run.conversation_id`` from ``ChatTurnResult.conversation_id``). The
existing chat machinery handles tool dispatch, streaming, persistence,
and usage recording.

**Agent built-in tools (v1):**
- ``complete_goal(goal_id, reason)`` — exposed as a ``ToolProvider`` tool
  by AutonomousAgentService itself. Marks the goal as ``COMPLETED`` and
  prevents future runs.
- ``notify_user(user_id, message, urgency)`` — exposed by
  ``NotificationService`` (not ``AutonomousAgentService``); the agent
  discovers it through the normal AI tool-discovery flow.

**Goal lifecycle:** ``ENABLED`` → ``DISABLED`` (manual pause) → ``ENABLED``
or ``COMPLETED`` (terminal — no more runs).

**RBAC:** all ``agent.*`` WS RPCs are user-level. Handlers enforce
per-user ownership: a user can only see/run/edit/delete their own goals.
Set in ``DEFAULT_RPC_PERMISSIONS``.

**Triggers:** Three trigger sources funnel into one ``_run_goal_internal``
entry point.
- **Manual** (``agent.goal.run_now`` RPC): synchronous; returns the run
  to the caller.
- **TIME** (``trigger_type="time"``): scheduler job named
  ``agent_goal_<id>``. Schedule kinds: ``interval`` (seconds), ``daily_at``
  (hour, minute), ``hourly_at`` (minute). ``add_job`` is not idempotent
  on name; service does ``remove_job`` then ``add_job`` for re-arm.
- **EVENT** (``trigger_type="event"``): subscribes to one event_type
  with optional simple field/op/value filter (ops: eq, neq, in, contains).

Triggers are re-armed on ``start()`` from persisted goal state. Runs
left in RUNNING state across a process restart are marked FAILED with
``error="process_restarted"``. Concurrency: in-memory
``_running_goals: set[str]`` causes a duplicate trigger-fire to skip
silently while the previous run is still in flight.

**Cross-run memory & materialized conversations:** v1 does not implement
notes, digests, or per-goal conversation materialization. Each run
creates its own fresh conversation. Phase 4c will materialize a single
conversation per goal and add a notes scratchpad + auto-digest.

## Related
- ``src/gilbert/interfaces/agent.py``
- ``src/gilbert/core/services/agent.py``
- ``tests/unit/core/test_agent_service.py``
- ``docs/superpowers/specs/2026-05-03-autonomous-agent-design.md``
- ``docs/superpowers/plans/2026-05-03-autonomous-agent-phase-4a-agent-service.md``
- ``.claude/memory/memory-notification-service.md`` (notify_user tool)
- ``.claude/memory/memory-agent-loop.md`` (run_loop primitive — currently
  unused by AgentService; available for future direct callers)
