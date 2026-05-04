# Autonomous Agent — Phase 4b: Automatic Triggers Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Add automatic TIME and EVENT triggers to `AutonomousAgentService`. Goals can specify a `trigger_type` (`time` or `event`) and `trigger_config`; the service arms a `SchedulerService` job (TIME) or an `EventBus` subscription (EVENT) at create/update/enable, and disarms at disable/delete/complete. Trigger callbacks spawn background tasks that call the existing `run_goal_now` method. Skip-while-running concurrency. Restart safety re-arms enabled goals on `start()` and marks stale `RUNNING` runs as `FAILED`.

**Architecture:** Additive changes to `Goal` entity (`trigger_type: str | None`, `trigger_config: dict | None`). Two new methods: `_arm_trigger(goal)` and `_disarm_trigger(goal)`. `_spawn_run(goal_id, triggered_by, trigger_context)` creates a tracked background task running an internal `_run_goal_internal` that's like `run_goal_now` but takes an explicit `triggered_by` field. CRUD methods call arm/disarm. `start()` re-arms enabled goals.

**Tech Stack:** Same as Phase 4a.

**Out of scope for this plan:**
- Cross-run memory (Phase 4c)
- Materialized per-goal conversation (Phase 4c)
- Frontend (Phase 5)

---

## File Structure

**Modify:**
- `src/gilbert/interfaces/agent.py` — add `trigger_type` and `trigger_config` fields to `Goal`
- `src/gilbert/core/services/agent.py` — arm/disarm/spawn methods, restart-safety in `start()`, trigger fields in serialization
- `tests/unit/core/test_agent_service.py` — append trigger tests

---

## Tasks

### Task 1: Add trigger fields to Goal entity

**Files:**
- Modify: `src/gilbert/interfaces/agent.py`
- Modify: `src/gilbert/core/services/agent.py`

- [ ] **Step 1: Add fields to `Goal` dataclass**

In `src/gilbert/interfaces/agent.py`, append two new optional fields to `Goal` (after the existing fields, before `last_run_at`). The final field order:

```python
@dataclass
class Goal:
    """A persistent autonomous-agent task description."""

    id: str
    owner_user_id: str
    name: str
    instruction: str
    profile_id: str
    status: GoalStatus
    created_at: datetime
    updated_at: datetime
    trigger_type: str | None = None
    """``"time"`` or ``"event"`` or None for manual-only goals."""

    trigger_config: dict[str, Any] | None = None
    """Shape depends on trigger_type:
    - TIME: ``{"kind": "interval"|"daily_at"|"hourly_at", "seconds"?: int, "hour"?: int, "minute"?: int}``
    - EVENT: ``{"event_type": str, "filter"?: {"field": str, "op": str, "value": Any}}``
    """

    last_run_at: datetime | None = None
    last_run_status: RunStatus | None = None
    run_count: int = 0
    completed_at: datetime | None = None
    completed_reason: str | None = None
```

- [ ] **Step 2: Update `_goal_to_dict` and `_goal_from_dict`**

In `src/gilbert/core/services/agent.py`, update the helpers to include the new fields:

```python
def _goal_to_dict(g: Goal) -> dict[str, Any]:
    return {
        "id": g.id,
        "owner_user_id": g.owner_user_id,
        "name": g.name,
        "instruction": g.instruction,
        "profile_id": g.profile_id,
        "status": g.status.value,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
        "trigger_type": g.trigger_type,
        "trigger_config": g.trigger_config,
        "last_run_at": g.last_run_at.isoformat() if g.last_run_at else None,
        "last_run_status": g.last_run_status.value if g.last_run_status else None,
        "run_count": g.run_count,
        "completed_at": g.completed_at.isoformat() if g.completed_at else None,
        "completed_reason": g.completed_reason,
    }


def _goal_from_dict(d: dict[str, Any]) -> Goal:
    last_run_status_raw = d.get("last_run_status")
    completed_at_raw = d.get("completed_at")
    last_run_at_raw = d.get("last_run_at")
    return Goal(
        id=d["id"],
        owner_user_id=d["owner_user_id"],
        name=d["name"],
        instruction=d["instruction"],
        profile_id=d["profile_id"],
        status=GoalStatus(d["status"]),
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        trigger_type=d.get("trigger_type"),
        trigger_config=d.get("trigger_config"),
        last_run_at=datetime.fromisoformat(last_run_at_raw) if last_run_at_raw else None,
        last_run_status=RunStatus(last_run_status_raw) if last_run_status_raw else None,
        run_count=int(d.get("run_count", 0)),
        completed_at=datetime.fromisoformat(completed_at_raw) if completed_at_raw else None,
        completed_reason=d.get("completed_reason"),
    )
```

- [ ] **Step 3: Update `create_goal` to accept trigger params**

In `src/gilbert/core/services/agent.py`, update the `create_goal` signature:

```python
    async def create_goal(
        self,
        *,
        owner_user_id: str,
        name: str,
        instruction: str,
        profile_id: str,
        trigger_type: str | None = None,
        trigger_config: dict[str, Any] | None = None,
    ) -> Goal:
```

And in the body, set `trigger_type=trigger_type` and `trigger_config=trigger_config` when constructing the Goal.

Update `update_goal` to also accept and apply trigger fields:

```python
    async def update_goal(
        self,
        goal_id: str,
        *,
        name: str | None = None,
        instruction: str | None = None,
        profile_id: str | None = None,
        status: GoalStatus | None = None,
        trigger_type: str | None = None,
        trigger_config: dict[str, Any] | None = None,
    ) -> Goal | None:
        ...
        # Inside the existing body, after profile_id update:
        if trigger_type is not None:
            goal.trigger_type = trigger_type
        if trigger_config is not None:
            goal.trigger_config = trigger_config
```

Note: passing `trigger_type=None` doesn't clear the trigger; that's intentional for now — to clear, pass `trigger_type=""` and have the service treat empty-string as "no trigger". But for v1, we don't expose a "clear trigger" path through update — disable the goal instead.

- [ ] **Step 4: Run existing tests — should still pass**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 18 passed (same as Phase 4a).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/agent.py src/gilbert/core/services/agent.py
git commit -m "agent: add trigger_type and trigger_config fields to Goal"
```

---

### Task 2: Add `_arm_trigger` / `_disarm_trigger` skeletons

**Files:**
- Modify: `src/gilbert/core/services/agent.py`

- [ ] **Step 1: Add scheduler import and instance state**

At the top of `src/gilbert/core/services/agent.py`, add to the imports:

```python
from gilbert.interfaces.scheduler import (
    JobCallback,
    Schedule,
    SchedulerProvider,
)
```

In the `__init__`:

```python
        self._scheduler: SchedulerProvider | None = None
        self._event_bus_unsubscribers: dict[str, Any] = {}
        """goal_id → unsubscribe callable for EVENT triggers."""

        self._running_goals: set[str] = set()
        """In-progress goal IDs to skip duplicate trigger fires."""
```

In `start()`, resolve the scheduler capability:

```python
        sched_svc = resolver.require_capability("scheduler")
        if not isinstance(sched_svc, SchedulerProvider):
            raise RuntimeError("scheduler missing or wrong type")
        self._scheduler = sched_svc
```

Place this after the existing `event_bus` resolution.

- [ ] **Step 2: Add the arm/disarm method skeletons (no body yet)**

Append to the class:

```python


    # ── Trigger plumbing ──────────────────────────────────────────

    def _scheduler_job_name(self, goal_id: str) -> str:
        return f"agent_goal_{goal_id}"

    async def _arm_trigger(self, goal: Goal) -> None:
        """Arm a goal's trigger if it has one and is enabled."""
        if goal.status != GoalStatus.ENABLED:
            return
        if goal.trigger_type == "time":
            self._arm_time_trigger(goal)
        elif goal.trigger_type == "event":
            self._arm_event_trigger(goal)
        # else: no trigger — manual-only goal

    async def _disarm_trigger(self, goal: Goal) -> None:
        """Remove any active trigger for this goal."""
        if goal.trigger_type == "time":
            self._disarm_time_trigger(goal.id)
        elif goal.trigger_type == "event":
            self._disarm_event_trigger(goal.id)

    def _arm_time_trigger(self, goal: Goal) -> None:
        raise NotImplementedError  # Task 3

    def _disarm_time_trigger(self, goal_id: str) -> None:
        raise NotImplementedError  # Task 3

    def _arm_event_trigger(self, goal: Goal) -> None:
        raise NotImplementedError  # Task 4

    def _disarm_event_trigger(self, goal_id: str) -> None:
        raise NotImplementedError  # Task 4
```

- [ ] **Step 3: Verify imports + existing tests still pass**

```bash
cd /home/assistant/gilbert && uv run python -c "from gilbert.core.services.agent import AutonomousAgentService; print('ok')"
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Now the test fixture `service` will need to provide a `scheduler` capability. Look at the existing `_FakeResolver` and add a `_FakeScheduler` shim. Add to `tests/unit/core/test_agent_service.py`, near the other fakes:

```python
class _FakeScheduler:
    """Minimal SchedulerProvider stub for tests.

    Records add_job/remove_job calls so trigger tests can assert on them.
    """

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.jobs: dict[str, Any] = {}

    def add_job(
        self,
        name: str,
        schedule: Any,
        callback: Any,
        system: bool = False,
        enabled: bool = True,
        owner: str = "",
    ) -> Any:
        if name in self.jobs:
            raise ValueError(f"Job '{name}' already registered")
        self.added.append(
            {
                "name": name,
                "schedule": schedule,
                "callback": callback,
                "system": system,
                "enabled": enabled,
                "owner": owner,
            }
        )
        self.jobs[name] = callback
        # Return something JobInfo-shaped (only what callers actually inspect)
        from types import SimpleNamespace
        return SimpleNamespace(name=name, schedule=schedule, owner=owner)

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.removed.append(name)
        self.jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        return None

    def disable_job(self, name: str) -> None:
        return None

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.jobs.values())

    def get_job(self, name: str) -> Any:
        return self.jobs.get(name)

    async def run_now(self, name: str) -> None:
        cb = self.jobs.get(name)
        if cb is not None:
            await cb()
```

Update the `service` fixture to include a `_FakeScheduler` in the resolver:

```python
@pytest.fixture
async def service(sqlite_storage: StorageBackend) -> tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler]:
    ai = _FakeAIService()
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    svc = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler,
        }
    )
    await svc.start(resolver)
    return svc, ai, bus, scheduler
```

Update existing tests that destructure the fixture: `svc, ai, bus = service` becomes `svc, ai, bus, _scheduler = service`.

Run:
```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 18 passed.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: add scheduler dep and trigger arm/disarm skeletons"
```

---

### Task 3: Implement TIME trigger arm/disarm + spawn (TDD)

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing tests**

```python


# ── Trigger tests ─────────────────────────────────────────────────


async def test_create_time_trigger_goal_arms_scheduler_job(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="Hourly check",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 3600},
    )

    assert len(scheduler.added) == 1
    job = scheduler.added[0]
    assert job["name"] == f"agent_goal_{g.id}"
    assert job["owner"] == "u_alice"


async def test_create_daily_at_time_trigger_arms_scheduler_job(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="Morning brief",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "daily_at", "hour": 7, "minute": 0},
    )

    assert len(scheduler.added) == 1
    assert scheduler.added[0]["name"] == f"agent_goal_{g.id}"


async def test_disable_goal_disarms_scheduler_job(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 600},
    )
    assert len(scheduler.added) == 1

    await svc.update_goal(g.id, status=GoalStatus.DISABLED)

    assert scheduler.removed == [f"agent_goal_{g.id}"]


async def test_delete_goal_disarms_trigger(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 600},
    )

    await svc.delete_goal(g.id)

    assert scheduler.removed == [f"agent_goal_{g.id}"]


async def test_time_trigger_callback_spawns_a_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 60},
    )

    # Invoke the registered callback to simulate the scheduler firing
    await scheduler.run_now(f"agent_goal_{g.id}")

    # Allow the spawned task to complete
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1
    assert fetched.last_run_status == RunStatus.COMPLETED


async def test_skip_while_running_drops_concurrent_trigger(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    """If a trigger fires while a previous run is still in flight,
    the second tick is silently skipped (no duplicate Run entity).
    """
    svc, ai, _bus, scheduler = service

    # Make chat() block until we release it
    proceed = asyncio.Event()
    original_chat = ai.chat

    async def slow_chat(*args: Any, **kwargs: Any) -> Any:
        await proceed.wait()
        return await original_chat(*args, **kwargs)

    ai.chat = slow_chat  # type: ignore[method-assign]

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 60},
    )

    job_name = f"agent_goal_{g.id}"

    # Fire twice in quick succession
    task1 = asyncio.create_task(scheduler.run_now(job_name))
    await asyncio.sleep(0)  # let task1 enter _spawn_run and add to running set
    task2 = asyncio.create_task(scheduler.run_now(job_name))

    # Release the slow chat so task1 finishes
    proceed.set()
    await asyncio.gather(task1, task2)
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1  # second trigger was skipped
```

- [ ] **Step 2: Run — expect FAIL** (NotImplementedError on `_arm_time_trigger`)

- [ ] **Step 3: Implement TIME trigger arm/disarm**

In `src/gilbert/core/services/agent.py`, replace the TIME trigger stubs:

```python
    def _arm_time_trigger(self, goal: Goal) -> None:
        if self._scheduler is None or goal.trigger_config is None:
            return
        cfg = goal.trigger_config
        kind = cfg.get("kind", "interval")
        if kind == "interval":
            schedule = Schedule.every(seconds=float(cfg.get("seconds", 3600)))
        elif kind == "daily_at":
            schedule = Schedule.daily_at(
                hour=int(cfg.get("hour", 0)),
                minute=int(cfg.get("minute", 0)),
            )
        elif kind == "hourly_at":
            schedule = Schedule.hourly_at(minute=int(cfg.get("minute", 0)))
        else:
            logger.warning("unknown TIME trigger kind: %s", kind)
            return
        name = self._scheduler_job_name(goal.id)
        # add_job is not idempotent on name — remove first if present.
        if self._scheduler.get_job(name) is not None:
            self._scheduler.remove_job(name)
        callback = self._make_trigger_callback(goal.id, "time", {})
        self._scheduler.add_job(
            name=name,
            schedule=schedule,
            callback=callback,
            owner=goal.owner_user_id,
        )

    def _disarm_time_trigger(self, goal_id: str) -> None:
        if self._scheduler is None:
            return
        name = self._scheduler_job_name(goal_id)
        if self._scheduler.get_job(name) is not None:
            self._scheduler.remove_job(name)

    def _make_trigger_callback(
        self,
        goal_id: str,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> JobCallback:
        async def _fire() -> None:
            await self._spawn_run(goal_id, triggered_by, trigger_context)
        return _fire

    async def _spawn_run(
        self,
        goal_id: str,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> None:
        """Spawn a tracked background task that runs the goal once.

        Skip-while-running: if the goal is already running, log and
        return without creating a new Run.
        """
        if goal_id in self._running_goals:
            logger.info(
                "skipping %s trigger for goal %s; previous run still active",
                triggered_by,
                goal_id,
            )
            return
        self._running_goals.add(goal_id)

        async def _do_run() -> None:
            try:
                await self._run_goal_internal(goal_id, triggered_by, trigger_context)
            finally:
                self._running_goals.discard(goal_id)

        asyncio.create_task(_do_run())

    async def _run_goal_internal(
        self,
        goal_id: str,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> Run:
        """Internal run path used by triggers; mirrors run_goal_now but
        respects the trigger context.
        """
        # The trigger_context is currently unused beyond logging; future
        # phases may include it in the prompt for EVENT-triggered runs.
        if trigger_context:
            logger.info("agent run for goal %s triggered by %s with context %s",
                        goal_id, triggered_by, trigger_context)
        # Adapt run_goal_now's body, but tag triggered_by:
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")
        goal = await self.get_goal(goal_id)
        if goal is None or goal.status != GoalStatus.ENABLED:
            return Run(
                id="",
                goal_id=goal_id,
                triggered_by=triggered_by,
                started_at=datetime.now(UTC),
                status=RunStatus.FAILED,
                error="goal not in ENABLED state at run-start time",
            )
        run = Run(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            triggered_by=triggered_by,
            started_at=datetime.now(UTC),
            status=RunStatus.RUNNING,
        )
        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))
        try:
            user_message = self._build_initial_user_message(goal)
            result = await self._ai.chat(
                user_message=user_message,
                conversation_id=None,
                user_ctx=None,
                ai_call=_AI_CALL_NAME,
                ai_profile=goal.profile_id,
            )
            run.status = RunStatus.COMPLETED
            run.final_message_text = result.response_text
            run.conversation_id = result.conversation_id
            if result.turn_usage:
                run.tokens_in = int(result.turn_usage.get("input_tokens", 0))
                run.tokens_out = int(result.turn_usage.get("output_tokens", 0))
            run.rounds_used = len(result.rounds) + 1
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = repr(exc)
            logger.exception("agent run failed: goal=%s run=%s", goal_id, run.id)

        run.ended_at = datetime.now(UTC)
        await self._storage.put(_RUN_COLLECTION, run.id, _run_to_dict(run))

        goal.run_count += 1
        goal.last_run_at = run.ended_at
        goal.last_run_status = run.status
        goal.updated_at = datetime.now(UTC)
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        return run
```

Refactor `run_goal_now` to delegate to `_run_goal_internal`:

```python
    async def run_goal_now(self, goal_id: str) -> Run:
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")
        goal = await self.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"goal not found: {goal_id}")
        if goal.status == GoalStatus.COMPLETED:
            raise ValueError(f"goal {goal_id} is completed")
        if goal.status == GoalStatus.DISABLED:
            raise ValueError(f"goal {goal_id} is disabled")
        # run_goal_now runs synchronously and returns the completed run
        # (not via _spawn_run — manual triggers want the result back).
        return await self._run_goal_internal(goal_id, "manual", {})
```

Update `create_goal` to call `_arm_trigger`:

```python
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        await self._arm_trigger(goal)
        return goal
```

Update `update_goal` to disarm-then-arm if trigger fields changed OR if status changed:

```python
        # At the end, after persisting:
        await self._storage.put(_GOAL_COLLECTION, goal.id, _goal_to_dict(goal))
        # Re-arm: cheapest correct policy is always disarm then arm
        await self._disarm_trigger(goal)
        await self._arm_trigger(goal)
        return goal
```

Update `delete_goal` to disarm before deletion:

```python
        # Inside delete_goal, BEFORE deleting:
        existing_goal = _goal_from_dict(raw)
        await self._disarm_trigger(existing_goal)
        # then existing delete logic
```

Update `declare_goal_complete` to disarm:

```python
        # After setting goal.status = GoalStatus.COMPLETED and persisting:
        await self._disarm_trigger(goal)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 18 (prior) + 6 (new) = 24 passed.

If tests fail because the existing CRUD tests now have an extra `await scheduler.something` call interaction, check that those existing tests didn't depend on `scheduler.added` being empty.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: implement TIME triggers via scheduler with skip-while-running"
```

---

### Task 4: Implement EVENT trigger arm/disarm (TDD)

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing tests**

```python


async def test_event_trigger_subscribes_and_fires_run(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="React to leads",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_type": "lead.created"},
    )

    # Now publish a matching event — note: tests use _FakeEventBus which has
    # a subscribe method we need to implement
    from gilbert.interfaces.events import Event
    from datetime import UTC, datetime as _dt

    ev = Event(
        event_type="lead.created",
        data={"lead_id": "L42"},
        source="crm",
        timestamp=_dt.now(UTC),
    )
    # _FakeEventBus.dispatch() — added in Task 4 to deliver events to subscribers
    await bus.dispatch(ev)
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1


async def test_event_trigger_filter_skips_non_matching(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="Watch high-value leads",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={
            "event_type": "lead.created",
            "filter": {"field": "value", "op": "eq", "value": "high"},
        },
    )

    from gilbert.interfaces.events import Event
    from datetime import UTC, datetime as _dt

    # Event with no value — filter rejects
    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={"lead_id": "L42"},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.02)
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 0

    # Event with matching value — filter accepts
    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={"lead_id": "L43", "value": "high"},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 1


async def test_event_trigger_disarms_on_disable(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, bus, _scheduler = service

    g = await svc.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="event",
        trigger_config={"event_type": "lead.created"},
    )

    await svc.update_goal(g.id, status=GoalStatus.DISABLED)

    from gilbert.interfaces.events import Event
    from datetime import UTC, datetime as _dt

    await bus.dispatch(
        Event(
            event_type="lead.created",
            data={},
            source="crm",
            timestamp=_dt.now(UTC),
        )
    )
    await asyncio.sleep(0.05)

    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.run_count == 0
```

- [ ] **Step 2: Extend `_FakeEventBus` to support subscribers and dispatch**

Update the `_FakeEventBus` class in the test file:

```python
class _FakeEventBus:
    """Captures published events and supports subscribe/dispatch for trigger tests."""

    def __init__(self) -> None:
        self.published: list[Event] = []
        self.subscribers: dict[str, list[Any]] = {}

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        # Don't auto-dispatch; tests call dispatch() explicitly when they want
        # to simulate a published event reaching subscribers.

    def subscribe(self, event_type: str, handler: Any) -> Any:
        self.subscribers.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            handlers = self.subscribers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    def subscribe_pattern(self, pattern: str, handler: Any) -> Any:
        return self.subscribe(pattern, handler)

    async def dispatch(self, event: Event) -> None:
        """Test-only: deliver an event to its subscribers."""
        for handler in list(self.subscribers.get(event.event_type, [])):
            await handler(event)
```

- [ ] **Step 3: Run tests — expect FAIL** (NotImplementedError on `_arm_event_trigger`)

- [ ] **Step 4: Implement EVENT trigger**

In `src/gilbert/core/services/agent.py`, replace the EVENT trigger stubs:

```python
    def _arm_event_trigger(self, goal: Goal) -> None:
        if self._event_bus is None or goal.trigger_config is None:
            return
        event_type = goal.trigger_config.get("event_type")
        if not event_type:
            logger.warning("EVENT trigger for goal %s missing event_type", goal.id)
            return
        filter_spec = goal.trigger_config.get("filter")

        async def _on_event(event: Any) -> None:
            if not self._event_matches_filter(event, filter_spec):
                return
            # Re-fetch goal at fire time in case it was disabled/deleted
            current = await self.get_goal(goal.id)
            if current is None or current.status != GoalStatus.ENABLED:
                return
            await self._spawn_run(
                goal.id,
                "event",
                {"event_type": event.event_type, "event_data": event.data},
            )

        unsubscribe = self._event_bus.subscribe(event_type, _on_event)
        # If a subscription already exists for this goal, drop the old one
        old = self._event_bus_unsubscribers.pop(goal.id, None)
        if old is not None:
            try:
                old()
            except Exception:
                logger.warning("failed to unsubscribe old EVENT handler for %s", goal.id)
        self._event_bus_unsubscribers[goal.id] = unsubscribe

    def _disarm_event_trigger(self, goal_id: str) -> None:
        unsubscribe = self._event_bus_unsubscribers.pop(goal_id, None)
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:
                logger.warning("failed to unsubscribe EVENT handler for %s", goal_id)

    def _event_matches_filter(
        self,
        event: Any,
        filter_spec: dict[str, Any] | None,
    ) -> bool:
        if not filter_spec:
            return True
        field = filter_spec.get("field")
        op = filter_spec.get("op", "eq")
        expected = filter_spec.get("value")
        if not field:
            return True
        actual = (event.data or {}).get(field)
        if op == "eq":
            return actual == expected
        elif op == "neq":
            return actual != expected
        elif op == "in":
            return actual in (expected or [])
        elif op == "contains":
            return expected in (actual or "")
        else:
            logger.warning("unknown filter op: %s", op)
            return False
```

- [ ] **Step 5: Run tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 27 passed (24 prior + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: implement EVENT triggers via event bus subscription with simple filter"
```

---

### Task 5: Restart safety — re-arm enabled goals on `start()`

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py`

- [ ] **Step 1: Append failing test**

```python


async def test_start_rearms_existing_enabled_goals(
    sqlite_storage: StorageBackend,
) -> None:
    """Enabled goals with triggers must be re-armed on service startup so
    the system survives restarts.
    """
    # Seed the storage with a goal directly (simulating a previous process
    # that created the goal then exited)
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    ai = _FakeAIService()

    # First service instance creates a goal
    svc1 = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler,
        }
    )
    await svc1.start(resolver)
    g = await svc1.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
        trigger_type="time",
        trigger_config={"kind": "interval", "seconds": 600},
    )
    assert len(scheduler.added) == 1
    await svc1.stop()

    # Simulate a process restart with a fresh scheduler (jobs lost)
    scheduler2 = _FakeScheduler()
    svc2 = AutonomousAgentService()
    resolver2 = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler2,
        }
    )
    await svc2.start(resolver2)

    # The new scheduler should have the goal's trigger re-armed
    assert len(scheduler2.added) == 1
    assert scheduler2.added[0]["name"] == f"agent_goal_{g.id}"


async def test_start_marks_stale_running_runs_as_failed(
    sqlite_storage: StorageBackend,
) -> None:
    """A run left in RUNNING state across a process restart should be
    marked FAILED so the goal isn't permanently stuck.
    """
    import json
    from datetime import UTC, datetime as _dt

    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    ai = _FakeAIService()

    svc1 = AutonomousAgentService()
    resolver = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": scheduler,
        }
    )
    await svc1.start(resolver)

    g = await svc1.create_goal(
        owner_user_id="u_alice",
        name="x",
        instruction="i",
        profile_id="default",
    )

    # Insert a stale RUNNING run directly
    stale_run_id = "stale-run-1"
    started_at = _dt(2026, 5, 1, tzinfo=UTC)  # well in the past
    await sqlite_storage.put(
        "agent_runs",
        stale_run_id,
        {
            "id": stale_run_id,
            "goal_id": g.id,
            "triggered_by": "manual",
            "started_at": started_at.isoformat(),
            "status": "running",
            "conversation_id": "",
            "ended_at": None,
            "final_message_text": None,
            "rounds_used": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "error": None,
            "complete_goal_called": False,
            "complete_reason": None,
        },
    )

    await svc1.stop()

    # Restart
    svc2 = AutonomousAgentService()
    resolver2 = _FakeResolver(
        {
            "entity_storage": _FakeStorageProvider(sqlite_storage),
            "event_bus": _FakeEventBusProvider(bus),
            "ai_chat": ai,
            "scheduler": _FakeScheduler(),
        }
    )
    await svc2.start(resolver2)

    raw = await sqlite_storage.get("agent_runs", stale_run_id)
    assert raw is not None
    assert raw["status"] == "failed"
    assert "process_restarted" in (raw.get("error") or "")
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement restart safety in `start()`**

In `src/gilbert/core/services/agent.py`, append to `start()` (just before the `logger.info` line at the end):

```python
        await self._mark_orphaned_runs_failed()
        await self._rearm_enabled_goals()
```

And add the helper methods:

```python


    async def _mark_orphaned_runs_failed(self) -> None:
        """Find any runs left in RUNNING state from a previous process
        and mark them FAILED.
        """
        if self._storage is None:
            return
        running = await self._storage.query(
            Query(
                collection=_RUN_COLLECTION,
                filters=[
                    Filter(field="status", op=FilterOp.EQ, value="running"),
                ],
                limit=10_000,
            )
        )
        for raw in running:
            raw["status"] = "failed"
            raw["error"] = "process_restarted"
            raw["ended_at"] = datetime.now(UTC).isoformat()
            await self._storage.put(_RUN_COLLECTION, raw["id"], raw)
        if running:
            logger.info(
                "marked %d orphaned RUNNING runs as FAILED on startup",
                len(running),
            )

    async def _rearm_enabled_goals(self) -> None:
        """Re-arm triggers for every enabled goal on startup."""
        goals = await self.list_goals()
        for g in goals:
            if g.status != GoalStatus.ENABLED:
                continue
            if g.trigger_type:
                try:
                    await self._arm_trigger(g)
                except Exception:
                    logger.exception(
                        "failed to re-arm trigger for goal %s on startup",
                        g.id,
                    )
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 29 passed (27 prior + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
git commit -m "agent: re-arm enabled goals and clean up orphaned runs on startup"
```

---

### Task 6: WS RPC accepts trigger fields + quality gate + memory update

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/core/test_agent_service.py` (small test)
- Modify: `.claude/memory/memory-autonomous-agent-service.md`

- [ ] **Step 1: Update `_ws_goal_create` and `_ws_goal_update` to accept trigger fields**

In `_ws_goal_create`, after extracting name/instruction/profile_id, also pull trigger fields:

```python
        trigger_type = frame.get("trigger_type")
        trigger_config = frame.get("trigger_config")
        # Validate trigger_type
        if trigger_type not in (None, "", "time", "event"):
            return {
                "type": "agent.goal.create.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "trigger_type must be 'time' or 'event'",
            }
        # Empty string normalizes to None (manual-only)
        if trigger_type == "":
            trigger_type = None
        goal = await self.create_goal(
            owner_user_id=conn.user_ctx.user_id,
            name=name,
            instruction=instruction,
            profile_id=profile_id,
            trigger_type=trigger_type,
            trigger_config=trigger_config if isinstance(trigger_config, dict) else None,
        )
```

Similarly update `_ws_goal_update` to pass through trigger fields:

```python
        # Inside the existing handler, before calling self.update_goal:
        trigger_type = frame.get("trigger_type")
        trigger_config = frame.get("trigger_config")
        # ... pass to update_goal
        updated = await self.update_goal(
            goal_id,
            name=frame.get("name"),
            instruction=frame.get("instruction"),
            profile_id=frame.get("profile_id"),
            status=status_enum,
            trigger_type=trigger_type if trigger_type in (None, "time", "event") else None,
            trigger_config=trigger_config if isinstance(trigger_config, dict) else None,
        )
```

- [ ] **Step 2: Add a small test for the trigger field passthrough**

```python


async def test_ws_agent_goal_create_with_time_trigger(
    service: tuple[AutonomousAgentService, _FakeAIService, _FakeEventBus, _FakeScheduler],
) -> None:
    svc, _ai, _bus, scheduler = service
    handlers = svc.get_ws_handlers()
    handler = handlers["agent.goal.create"]

    result = await handler(
        _make_conn("u_alice"),
        {
            "id": "f1",
            "name": "Hourly check",
            "instruction": "i",
            "profile_id": "default",
            "trigger_type": "time",
            "trigger_config": {"kind": "interval", "seconds": 3600},
        },
    )

    assert result is not None
    assert result["ok"] is True
    assert result["goal"]["trigger_type"] == "time"
    assert len(scheduler.added) == 1
```

- [ ] **Step 3: Run all tests**

```bash
cd /home/assistant/gilbert && uv run pytest tests/unit/core/test_agent_service.py -v
```

Expected: 30 passed.

- [ ] **Step 4: Run mypy + ruff**

```bash
cd /home/assistant/gilbert && uv run mypy src/gilbert/core/services/agent.py
cd /home/assistant/gilbert && uv run ruff format src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
cd /home/assistant/gilbert && uv run ruff check src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py
```

Fix any issues.

- [ ] **Step 5: Update memory file**

In `.claude/memory/memory-autonomous-agent-service.md`, replace the "**Triggers:** v1 supports manual triggers only..." paragraph with:

```markdown
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
```

Run mypy/ruff one more time if you edited Python; commit:

```bash
git add src/gilbert/core/services/agent.py tests/unit/core/test_agent_service.py .claude/memory/memory-autonomous-agent-service.md
git commit -m "agent: WS RPCs accept trigger config + memory update for triggers"
```

---

## Phase 4b Complete

- TIME triggers (interval, daily_at, hourly_at) via SchedulerService.
- EVENT triggers via EventBus subscription with optional field/op/value filter.
- Re-arm on enable, disarm on disable/delete/complete.
- Skip-while-running concurrency.
- Restart safety: re-arm enabled goals, mark stale RUNNING runs FAILED.
- WS RPCs accept trigger config.
- 30 agent tests passing.
