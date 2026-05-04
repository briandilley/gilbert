# AutonomousAgentService

## Summary
Persists Goal/Run entities and executes goals via ``AIService.chat(ai_call="agent.run")``. Lives in ``src/gilbert/core/services/agent.py``. Supports manual + scheduled + event-driven triggers, mid-run user-message injection (live conversation while a run is in flight), an explicit ``request_user_input`` tool with urgent notifications, and a materialized per-goal conversation that drives the chat-style /agents UI.

## Details

**Capabilities declared:** ``agent`` (satisfies ``AgentProvider``), ``ai_tools`` (exposes ``complete_goal`` and ``request_user_input``), ``ws_handlers``.

**``requires`` / ``optional``:** ``requires=frozenset({"entity_storage", "event_bus", "ai_chat", "scheduler"})`` — the service is hard-dependent on these. Optional: ``notifications`` (enables urgent toasts/sounds), ``user_memory``.

**AI call name:** ``agent.run`` — registered via ``ai_calls`` in ``ServiceInfo``. Operators can route to a distinct profile via the AI profile assignment table.

**Execution model:**
- ``run_goal_now(goal_id)`` is the synchronous entry; ``start_goal_run(goal_id)`` is the async/fire-and-forget entry the WS RPC uses to avoid blocking on long agent runs.
- The actual loop runs inside ``_run_goal_internal`` wrapped by ``asyncio.shield`` so a WS disconnect doesn't cascade-cancel the in-flight ``AIService.chat()`` call.
- Synthesizes a user message from the goal's instruction, calls ``AIService.chat()`` with ``ai_call="agent.run"`` and ``ai_profile=goal.profile_id``, captures the result into a ``Run`` entity.
- ``goal.conversation_id`` is the materialized chat — first run creates it, subsequent runs reuse. ``run.conversation_id`` falls back to ``goal.conversation_id`` for old runs.
- End-of-run writeback re-reads ``fresh_goal`` from storage before persisting state to avoid clobbering deletes / status changes / awaiting-input flags that landed during the run.

**Agent built-in tools:**
- ``complete_goal(goal_id, reason)`` — flags the *run* as complete, never the goal. Re-running a goal that completed once is supported (the goal stays ``ENABLED``).
- ``request_user_input(question)`` — flags ``goal.awaiting_user_input=True`` + sets ``pending_question``, then publishes a notification at ``urgency="urgent"``. Heuristic backstop: if the final assistant message ends with ``?`` and the agent didn't explicitly call ``request_user_input``, the service auto-flags it.
- ``notify_user(user_id, message, urgency)`` — exposed by ``NotificationService``; the agent discovers it via normal tool-discovery.

**Mid-run user-message injection:** ``AIService.chat()`` accepts a ``between_rounds_callback`` that the service uses to drain pending user messages from ``self._pending_user_messages[goal_id]`` between AI rounds. Lets the user converse with a running agent without having to wait for the run to finish.

**Goal lifecycle:** ``ENABLED`` → ``DISABLED`` (manual pause) → ``ENABLED`` or ``COMPLETED`` (the latter is admin-set; runs themselves no longer auto-complete the goal).

**RBAC:** all ``agent.*`` WS RPCs are user-level (set in ``DEFAULT_RPC_PERMISSIONS``). Handlers enforce per-user ownership: a user can only see / run / edit / delete their own goals.

**Triggers:** Three sources funnel into ``_run_goal_internal``.
- **Manual** (``agent.goal.run_now`` RPC): fire-and-forget background task; returns once the run is registered, not when it completes.
- **TIME** (``trigger_type="time"``): scheduler job named ``agent_goal_<id>``. Kinds: ``interval``, ``daily_at`` (hour+minute), ``hourly_at`` (minute). ``add_job`` is not idempotent on name; the service does ``remove_job`` then ``add_job`` to re-arm.
- **EVENT** (``trigger_type="event"``): subscribes to one or more ``event_type``s with optional simple ``field/op/value`` filter (ops: ``eq``, ``neq``, ``in``, ``contains``). Multi-event triggers honour ``trigger_config.event_types`` (legacy single-event ``event_type`` still accepted).

Triggers are re-armed on ``start()`` from persisted goal state. Runs left in RUNNING across a process restart are marked FAILED with ``error="process_restarted"``. In-memory ``_running_goals: set[str]`` causes duplicate trigger fires to skip silently while the previous run is in flight.

**Materialized conversation per goal:** ``goal.conversation_id`` is lazy-created on the first run by ``AIService.chat()`` (called with ``conversation_id=None``); the returned id is captured on the goal and reused for every subsequent run. The /agents UI is just a chat view of this conversation. Conversations created by the agent get ``source="agent"`` so they don't show in /chat's regular list. Cascade delete on goal removal.

**Streaming UI:** The /agents page subscribes to ``chat.stream.*`` events for the goal's conversation, mirroring the regular ChatPage's text-delta / round-complete handlers. Real-time tokens appear as the agent thinks.

**Urgent notifications:** ``request_user_input`` and the heuristic backstop publish notifications at ``urgency="urgent"``. NotificationBell.tsx shows a persistent red toast top-right, plays a synthesised WebAudio "ding", and pulses the bell — independent of whether the user is on /agents at the time.

## Related
- ``src/gilbert/interfaces/agent.py``
- ``src/gilbert/core/services/agent.py``
- ``tests/unit/core/test_agent_service.py``
- ``docs/superpowers/specs/2026-05-03-autonomous-agent-design.md``
- ``docs/superpowers/plans/2026-05-03-autonomous-agent-phase-4a-agent-service.md``
- ``.claude/memory/memory-notification-service.md`` (notify_user tool, urgent flow)
- ``.claude/memory/memory-agent-loop.md`` (run_loop primitive — available for future direct callers)
