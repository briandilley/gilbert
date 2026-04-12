"""Scheduler service — manages system and user timers/alarms."""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.scheduler import (
    JobCallback,
    JobInfo,
    JobState,
    Schedule,
    ScheduledAction,
    ScheduledActionType,
    ScheduleType,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Query
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Collection holding persisted USER alarms/timers. System jobs are
# registered in-memory on each startup by their owning services and
# are NOT persisted here.
_JOBS_COLLECTION = "scheduler_jobs"


# Minimal system prompt for AI-driven scheduled actions. Kept small to
# stay within the rate-limit budget — the AI has full tool access and
# can do whatever the stored instruction asks.
_SCHEDULED_ACTION_SYSTEM_PROMPT = (
    "You are executing a pre-scheduled instruction. The user set this up "
    "ahead of time and wants you to carry it out NOW using your available "
    "tools. Execute the instruction directly. Do not ask for confirmation. "
    "Do not describe what you are about to do — just do it, then give a "
    "one-sentence confirmation of what you did."
)


class _AICallRateLimiter:
    """Sliding-window rate limiter for AI-driven scheduled fires.

    Applies globally across all alarms/timers that use ``ai_prompt`` —
    not per-job — so a single spammy alarm can't blow through the
    entire budget. Cheap O(1) amortized check; the deque only ever
    holds timestamps within the active window.

    A ``max_calls`` or ``window_seconds`` of 0 disables the AI path
    entirely.
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max_calls = max(0, int(max_calls))
        self._window_seconds = max(0.0, float(window_seconds))
        self._timestamps: deque[float] = deque()

    def update_config(self, max_calls: int, window_seconds: float) -> None:
        """Re-tune limits at runtime. Existing timestamps stay valid."""
        self._max_calls = max(0, int(max_calls))
        self._window_seconds = max(0.0, float(window_seconds))

    def try_acquire(self) -> bool:
        """Attempt to reserve a slot. Returns True on success."""
        if self._max_calls == 0 or self._window_seconds == 0:
            return False
        now = time.monotonic()
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max_calls:
            return False
        self._timestamps.append(now)
        return True

    def status(self) -> dict[str, Any]:
        """Snapshot of current usage for logging / list_timers output."""
        now = time.monotonic()
        cutoff = now - self._window_seconds
        recent = sum(1 for t in self._timestamps if t >= cutoff)
        return {
            "max_calls": self._max_calls,
            "window_seconds": self._window_seconds,
            "recent_calls": recent,
            "available": max(0, self._max_calls - recent),
        }


class _Job:
    """Internal tracked job with its asyncio task."""

    def __init__(
        self,
        name: str,
        schedule: Schedule,
        callback: JobCallback,
        system: bool = False,
        enabled: bool = True,
        action: ScheduledAction | None = None,
    ) -> None:
        self.info = JobInfo(
            name=name,
            schedule=schedule,
            system=system,
            enabled=enabled,
            action=action or ScheduledAction(),
        )
        self.callback = callback
        self.task: asyncio.Task[None] | None = None


class SchedulerService(Service):
    """Manages recurring and one-shot timed tasks.

    System jobs are registered by other services (e.g., doorbell polling).
    User jobs can be created/managed via AI tools (timers, alarms).
    """

    # Default AI rate limits — tunable via config. These are the
    # fall-through values used until the configuration service provides
    # an override in on_config_changed().
    _DEFAULT_AI_MAX_CALLS = 1
    _DEFAULT_AI_WINDOW_SECONDS = 900  # 15 minutes

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._storage: Any = None
        self._event_bus: Any = None
        self._resolver: ServiceResolver | None = None
        self._ai_rate_limiter = _AICallRateLimiter(
            max_calls=self._DEFAULT_AI_MAX_CALLS,
            window_seconds=self._DEFAULT_AI_WINDOW_SECONDS,
        )

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="scheduler",
            capabilities=frozenset({"scheduler", "ai_tools"}),
            optional=frozenset(
                {"entity_storage", "event_bus", "configuration", "ai_chat", "access_control"}
            ),
            events=frozenset({"timer.fired", "alarm.fired"}),
            ai_calls=frozenset({"scheduled_action"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None:
            from gilbert.interfaces.storage import StorageProvider

            if isinstance(storage_svc, StorageProvider):
                self._storage = storage_svc.backend

        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            from gilbert.interfaces.events import EventBusProvider

            if isinstance(event_bus_svc, EventBusProvider):
                self._event_bus = event_bus_svc.bus

        # Apply persisted configuration (live tunable via on_config_changed)
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section_safe(self.config_namespace)
                if section:
                    await self.on_config_changed(section)

        # Rebuild persisted user alarms/timers from storage. System jobs
        # are registered fresh on each startup by their owning services.
        await self._load_persisted_jobs()

        logger.info("Scheduler service started")

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "scheduler"

    @property
    def config_category(self) -> str:
        return "System"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="alarm_ai_max_calls",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum AI-driven alarm/timer fires allowed within "
                    "the rolling window. Protects against runaway AI "
                    "spend from frequent alarms. Set to 0 to disable "
                    "AI-driven alarm actions entirely."
                ),
                default=self._DEFAULT_AI_MAX_CALLS,
            ),
            ConfigParam(
                key="alarm_ai_window_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Rolling window (in seconds) over which "
                    "alarm_ai_max_calls is enforced. Default 900 = "
                    "15 minutes."
                ),
                default=self._DEFAULT_AI_WINDOW_SECONDS,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        max_calls = int(
            config.get("alarm_ai_max_calls", self._DEFAULT_AI_MAX_CALLS)
        )
        window = float(
            config.get("alarm_ai_window_seconds", self._DEFAULT_AI_WINDOW_SECONDS)
        )
        self._ai_rate_limiter.update_config(max_calls, window)
        logger.info(
            "Scheduler AI rate limit set to %d per %.0fs window",
            max_calls,
            window,
        )

    async def stop(self) -> None:
        """Cancel all running job tasks with a timeout."""
        for job in self._jobs.values():
            if job.task is not None:
                job.task.cancel()
        # Wait briefly for tasks to finish, then move on
        tasks = [j.task for j in self._jobs.values() if j.task is not None]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Scheduler stop timed out — some jobs may still be running")
        self._jobs.clear()
        logger.info("Scheduler stopped")

    # --- Job management ---

    def add_job(
        self,
        name: str,
        schedule: Schedule,
        callback: JobCallback,
        system: bool = False,
        enabled: bool = True,
        owner: str = "",
        action: ScheduledAction | None = None,
    ) -> JobInfo:
        """Register a job. System jobs are not user-editable.

        The job starts running immediately if enabled. The optional
        ``action`` carries dispatch metadata (tool call, ai_prompt, or
        event fallback) so ``list_jobs()`` can report what each job
        will do when it fires.
        """
        if name in self._jobs:
            raise ValueError(f"Job '{name}' already registered")

        job = _Job(
            name=name,
            schedule=schedule,
            callback=callback,
            system=system,
            enabled=enabled,
            action=action,
        )
        job.info.owner = owner
        self._jobs[name] = job

        if enabled:
            job.task = asyncio.create_task(self._run_job_loop(job))

        logger.info(
            "Job '%s' registered (%s, %s, interval=%.1fs)",
            name,
            "system" if system else "user",
            schedule.type.value,
            schedule.interval_seconds,
        )
        return job.info

    def remove_job(self, name: str, requester_id: str = "") -> None:
        """Remove a job. System jobs cannot be removed.

        Non-admin users can only remove jobs they own.
        """
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(f"Job not found: {name}")
        if job.info.system:
            raise ValueError(f"Cannot remove system job: {name}")
        # Ownership check: if requester is set and doesn't match owner, deny
        if requester_id and job.info.owner and requester_id != job.info.owner:
            raise PermissionError(f"Job '{name}' is owned by '{job.info.owner}'")
        if job.task is not None:
            job.task.cancel()
        del self._jobs[name]

    def enable_job(self, name: str) -> None:
        """Enable a disabled job."""
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(f"Job not found: {name}")
        if job.info.enabled:
            return
        job.info.enabled = True
        job.task = asyncio.create_task(self._run_job_loop(job))

    def disable_job(self, name: str) -> None:
        """Disable a running job (keeps registration, stops execution)."""
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(f"Job not found: {name}")
        job.info.enabled = False
        if job.task is not None:
            job.task.cancel()
            job.task = None

    def list_jobs(self, include_system: bool = True) -> list[JobInfo]:
        """List all registered jobs."""
        return [
            j.info for j in self._jobs.values()
            if include_system or not j.info.system
        ]

    def get_job(self, name: str) -> JobInfo | None:
        """Get info about a specific job."""
        job = self._jobs.get(name)
        return job.info if job else None

    async def run_now(self, name: str) -> None:
        """Execute a job immediately, outside its schedule."""
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(f"Job not found: {name}")
        await self._execute_job(job)

    # --- Job execution loop ---

    async def _run_job_loop(self, job: _Job) -> None:
        """Run a job on its schedule until cancelled."""
        try:
            while True:
                delay = self._next_delay(job.info.schedule)
                await asyncio.sleep(delay)

                if not job.info.enabled:
                    continue

                await self._execute_job(job)

                if job.info.schedule.type == ScheduleType.ONCE:
                    job.info.state = JobState.DONE
                    return
        except asyncio.CancelledError:
            return

    async def _execute_job(self, job: _Job) -> None:
        """Execute a single job invocation."""
        job.info.state = JobState.RUNNING
        start = time.monotonic()

        try:
            await job.callback()
            job.info.last_error = ""
        except Exception as e:
            job.info.last_error = str(e)
            logger.exception("Job '%s' failed", job.info.name)
            if job.info.schedule.type == ScheduleType.ONCE:
                job.info.state = JobState.FAILED
                return

        elapsed = time.monotonic() - start
        job.info.run_count += 1
        job.info.last_run = datetime.now(UTC).isoformat()
        job.info.last_duration_seconds = round(elapsed, 3)
        job.info.state = JobState.IDLE

    @staticmethod
    def _next_delay(schedule: Schedule) -> float:
        """Calculate seconds until the next run."""
        if schedule.type in (ScheduleType.INTERVAL, ScheduleType.ONCE):
            return schedule.interval_seconds

        now = datetime.now()
        if schedule.type == ScheduleType.DAILY:
            target = now.replace(
                hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0,
            )
            if target <= now:
                target = target.replace(day=target.day + 1)
            return (target - now).total_seconds()

        if schedule.type == ScheduleType.HOURLY:
            target = now.replace(minute=schedule.minute, second=0, microsecond=0)
            if target <= now:
                from datetime import timedelta
                target += timedelta(hours=1)
            return (target - now).total_seconds()

        return 60.0  # fallback

    # --- Action dispatch ---

    def _make_fire_callback(
        self,
        job_name: str,
        action: ScheduledAction,
        owner: str,
        event_type: str,
    ) -> JobCallback:
        """Build the fire callback for a scheduled job.

        The callback captures the action payload so that restarts, config
        changes, and the rate limiter can all take effect on the next fire
        without re-registering the job.
        """

        async def _fire() -> None:
            await self._dispatch_action(job_name, action, owner, event_type)

        return _fire

    async def _dispatch_action(
        self,
        job_name: str,
        action: ScheduledAction,
        owner: str,
        event_type: str,
    ) -> None:
        """Route a fire to the right dispatch path based on action type."""
        try:
            if action.type == ScheduledActionType.TOOL:
                await self._dispatch_tool_action(job_name, action, owner)
            elif action.type == ScheduledActionType.AI_PROMPT:
                await self._dispatch_ai_action(job_name, action, owner)
            else:
                await self._dispatch_event_action(job_name, action, event_type)
        except Exception:
            # Never let a dispatch failure crash the scheduler loop —
            # the next scheduled fire must still happen.
            logger.exception(
                "Scheduler action dispatch failed for job '%s'", job_name
            )

    async def _dispatch_event_action(
        self,
        job_name: str,
        action: ScheduledAction,
        event_type: str,
    ) -> None:
        """Legacy pub/sub behavior — publish the fire as an event."""
        if self._event_bus is not None:
            from gilbert.interfaces.events import Event

            await self._event_bus.publish(
                Event(
                    event_type=event_type,
                    data={"name": job_name, "message": action.message},
                    source="scheduler",
                )
            )
        logger.info(
            "Scheduler fired '%s' as event: %s",
            job_name,
            action.message or "(no message)",
        )

    async def _dispatch_tool_action(
        self,
        job_name: str,
        action: ScheduledAction,
        owner: str,
    ) -> None:
        """Invoke the target tool by name with the stored arguments.

        RBAC is enforced at setup time (``_validate_tool_permission``),
        so fire time uses ``UserContext.SYSTEM``. If the owner's role
        changes after setup, the stale check stays in effect — acceptable
        for v1; a future enhancement could re-validate on each fire.
        """
        if self._resolver is None:
            logger.warning(
                "Scheduler: job '%s' cannot fire — no resolver", job_name
            )
            return

        tool_name = action.tool
        if not tool_name:
            logger.warning(
                "Scheduler: job '%s' has tool action but no tool name", job_name
            )
            return

        from gilbert.interfaces.tools import ToolProvider

        for svc in self._resolver.get_all("ai_tools"):
            if not isinstance(svc, ToolProvider):
                continue
            for tdef in svc.get_tools():
                if tdef.name != tool_name:
                    continue
                try:
                    result = await svc.execute_tool(tool_name, dict(action.tool_arguments))
                    logger.info(
                        "Scheduler '%s' → %s: %s",
                        job_name,
                        tool_name,
                        (result or "")[:200] if isinstance(result, str) else "(non-string result)",
                    )
                except Exception:
                    logger.exception(
                        "Scheduler '%s' → %s raised", job_name, tool_name
                    )
                return

        logger.warning(
            "Scheduler: job '%s' references unknown tool '%s' — skipping",
            job_name,
            tool_name,
        )

    async def _dispatch_ai_action(
        self,
        job_name: str,
        action: ScheduledAction,
        owner: str,
    ) -> None:
        """Run the stored ai_prompt through the AI service.

        Rate-limited globally via ``_ai_rate_limiter`` to cap cost on
        frequent alarms. If the limiter denies the fire, the job simply
        skips this cycle (no retry, no fallback). The next cycle will
        try again and may succeed once older timestamps age out of the
        window.
        """
        if not self._ai_rate_limiter.try_acquire():
            status = self._ai_rate_limiter.status()
            logger.info(
                "Scheduler '%s' AI fire skipped — rate limit "
                "(%d/%d in last %ds window)",
                job_name,
                status["recent_calls"],
                status["max_calls"],
                int(status["window_seconds"]),
            )
            return

        if self._resolver is None:
            return

        # AIService exposes chat() but there is no AIChatProvider
        # protocol in interfaces/, so we duck-type here (matching the
        # existing pattern in plugins/current-data-sync/data_sync_service.py
        # and speaker.py's announce path). Typed as Any to suppress the
        # attr-defined check.
        ai_svc: Any = self._resolver.get_capability("ai_chat")
        if ai_svc is None:
            logger.warning(
                "Scheduler '%s': AI service not available — cannot fire prompt",
                job_name,
            )
            return

        try:
            response_text, _, _, _ = await ai_svc.chat(
                user_message=action.ai_prompt,
                user_ctx=UserContext.SYSTEM,
                system_prompt=_SCHEDULED_ACTION_SYSTEM_PROMPT,
                ai_call="scheduled_action",
            )
            logger.info(
                "Scheduler '%s' AI fire: %s",
                job_name,
                (response_text or "")[:200],
            )
        except Exception:
            logger.exception(
                "Scheduler '%s' AI fire raised", job_name
            )

    # --- Action validation (at setup time) ---

    def _build_action_from_args(
        self, arguments: dict[str, Any]
    ) -> tuple[ScheduledAction, str | None]:
        """Construct a ScheduledAction from set_timer/set_alarm arguments.

        Returns (action, error_message). If error_message is not None, the
        arguments are invalid and the tool should return an error instead
        of creating the job.
        """
        tool_name = (arguments.get("tool") or "").strip()
        ai_prompt = (arguments.get("ai_prompt") or "").strip()
        message = arguments.get("message", "") or ""

        if tool_name and ai_prompt:
            return (
                ScheduledAction(),
                "Specify either 'tool' or 'ai_prompt', not both.",
            )

        if tool_name:
            tool_args_raw = arguments.get("tool_arguments") or {}
            if not isinstance(tool_args_raw, dict):
                return (
                    ScheduledAction(),
                    "'tool_arguments' must be an object (dict).",
                )
            err = self._validate_tool_exists_and_allowed(tool_name)
            if err:
                return ScheduledAction(), err
            return (
                ScheduledAction(
                    type=ScheduledActionType.TOOL,
                    tool=tool_name,
                    tool_arguments=dict(tool_args_raw),
                    message=message,
                ),
                None,
            )

        if ai_prompt:
            return (
                ScheduledAction(
                    type=ScheduledActionType.AI_PROMPT,
                    ai_prompt=ai_prompt,
                    message=message,
                ),
                None,
            )

        # Legacy event-only behavior
        return (
            ScheduledAction(type=ScheduledActionType.EVENT, message=message),
            None,
        )

    def _validate_tool_exists_and_allowed(
        self, tool_name: str
    ) -> str | None:
        """Check that the tool exists and the current user may call it.

        Returns an error string if the tool is missing or the caller lacks
        the required role; ``None`` if the setup is allowed. RBAC is
        checked here at setup time; fire-time dispatch trusts the result.
        """
        if self._resolver is None:
            return "Scheduler is not ready to validate tools."

        from gilbert.interfaces.auth import AccessControlProvider
        from gilbert.interfaces.tools import ToolProvider

        user = get_current_user()
        acl_svc = self._resolver.get_capability("access_control")
        acl: AccessControlProvider | None = (
            acl_svc if isinstance(acl_svc, AccessControlProvider) else None
        )

        for svc in self._resolver.get_all("ai_tools"):
            if not isinstance(svc, ToolProvider):
                continue
            for tdef in svc.get_tools():
                if tdef.name != tool_name:
                    continue
                if acl is not None and user is not None:
                    required_level = acl.get_role_level(tdef.required_role)
                    user_level = acl.get_effective_level(user)
                    if user_level > required_level:
                        return (
                            f"You do not have permission to schedule "
                            f"tool '{tool_name}' (requires role "
                            f"'{tdef.required_role}')."
                        )
                return None  # found + permitted

        return f"Unknown tool: '{tool_name}'."

    # --- Persistence (user jobs only) ---

    async def _persist_job(
        self,
        name: str,
        schedule: Schedule,
        action: ScheduledAction,
        owner: str,
    ) -> None:
        """Write a user job to entity storage so it survives restarts.

        System jobs are NEVER persisted — they are registered fresh on
        each startup by their owning services, so persisting them would
        just create duplicates on restart.
        """
        if self._storage is None:
            return

        now_iso = datetime.now(UTC).isoformat()
        record: dict[str, Any] = {
            "id": name,
            "name": name,
            "schedule_type": schedule.type.value,
            "interval_seconds": schedule.interval_seconds,
            "hour": schedule.hour,
            "minute": schedule.minute,
            "owner": owner,
            "action": action.to_dict(),
            "created_at": now_iso,
        }
        # One-shot timers need a fire_at so we can drop them on startup
        # if they were scheduled to fire while Gilbert was down.
        if schedule.type == ScheduleType.ONCE:
            fire_at = datetime.now(UTC) + timedelta(
                seconds=schedule.interval_seconds
            )
            record["fire_at"] = fire_at.isoformat()

        try:
            await self._storage.put(_JOBS_COLLECTION, name, record)
        except Exception:
            logger.exception(
                "Scheduler: failed to persist job '%s'", name
            )

    async def _unpersist_job(self, name: str) -> None:
        """Best-effort delete of a persisted job record."""
        if self._storage is None:
            return
        try:
            await self._storage.delete(_JOBS_COLLECTION, name)
        except Exception:
            logger.debug(
                "Scheduler: unpersist of '%s' failed (may not exist)", name
            )

    async def _load_persisted_jobs(self) -> None:
        """Rebuild user jobs from storage on startup."""
        if self._storage is None:
            return

        try:
            rows = await self._storage.query(Query(collection=_JOBS_COLLECTION))
        except Exception:
            logger.exception("Scheduler: failed to load persisted jobs")
            return

        now = datetime.now(UTC)
        restored = 0
        dropped_expired = 0

        for row in rows:
            try:
                name = row["name"]
                sched_type = ScheduleType(row.get("schedule_type") or "interval")
                schedule = Schedule(
                    type=sched_type,
                    interval_seconds=float(row.get("interval_seconds", 0) or 0),
                    hour=int(row.get("hour", 0) or 0),
                    minute=int(row.get("minute", 0) or 0),
                )

                # Drop one-shot timers that should have already fired
                if sched_type == ScheduleType.ONCE:
                    fire_at_str = row.get("fire_at") or ""
                    if fire_at_str:
                        try:
                            fire_at = datetime.fromisoformat(
                                fire_at_str.replace("Z", "+00:00")
                            )
                            if fire_at <= now:
                                logger.info(
                                    "Scheduler: dropping expired one-shot timer '%s'",
                                    name,
                                )
                                await self._unpersist_job(name)
                                dropped_expired += 1
                                continue
                        except ValueError:
                            pass

                action = ScheduledAction.from_dict(row.get("action"))
                owner = str(row.get("owner") or "")
                event_type = (
                    "timer.fired"
                    if sched_type == ScheduleType.ONCE
                    else "alarm.fired"
                )
                callback = self._make_fire_callback(
                    name, action, owner, event_type
                )
                self.add_job(
                    name=name,
                    schedule=schedule,
                    callback=callback,
                    system=False,
                    owner=owner,
                )
                # Stamp the action on the in-memory JobInfo so list_timers
                # can report what each job will do.
                job = self._jobs.get(name)
                if job is not None:
                    job.info.action = action
                restored += 1
            except Exception:
                logger.exception(
                    "Scheduler: failed to restore persisted job %r",
                    row.get("name"),
                )

        if restored or dropped_expired:
            logger.info(
                "Scheduler: restored %d persisted user jobs, dropped %d expired",
                restored,
                dropped_expired,
            )

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "scheduler"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="list_timers",
                description="List all active timers and alarms (both system and user).",
                required_role="everyone",
            ),
            ToolDefinition(
                name="set_timer",
                description=(
                    "Set a user timer that fires ONCE after a delay. By "
                    "default publishes a 'timer.fired' event; optionally "
                    "invoke a specific tool with arguments (tool + "
                    "tool_arguments) or run a natural-language "
                    "instruction through the AI (ai_prompt). Persisted "
                    "across restarts. Use 'tool' for deterministic, "
                    "frequent, or cheap actions. Use 'ai_prompt' for "
                    "complex or conditional actions — AI fires are "
                    "globally rate-limited to avoid runaway cost."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name for this timer (e.g., 'pizza-timer').",
                    ),
                    ToolParameter(
                        name="seconds",
                        type=ToolParameterType.NUMBER,
                        description="Seconds until the timer fires.",
                    ),
                    ToolParameter(
                        name="message",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional free-text message. If no tool or "
                            "ai_prompt is given, this message is "
                            "published with the 'timer.fired' event."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="tool",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional tool name to invoke when the timer "
                            "fires. Mutually exclusive with ai_prompt. "
                            "The caller must have permission to use the "
                            "target tool at setup time."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="tool_arguments",
                        type=ToolParameterType.OBJECT,
                        description=(
                            "Arguments object to pass to the target "
                            "tool when it fires (used with 'tool')."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="ai_prompt",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional natural-language instruction to "
                            "run through the AI when the timer fires. "
                            "Mutually exclusive with tool. AI fires are "
                            "globally rate-limited."
                        ),
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="set_alarm",
                description=(
                    "Set a recurring user alarm (interval, daily, or "
                    "hourly). By default publishes an 'alarm.fired' "
                    "event on each fire; optionally invoke a specific "
                    "tool with arguments (tool + tool_arguments) or "
                    "run an instruction through the AI (ai_prompt). "
                    "Persisted across restarts. Prefer 'tool' for "
                    "frequent alarms (every few seconds/minutes) — AI "
                    "fires are globally rate-limited."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name for this alarm.",
                    ),
                    ToolParameter(
                        name="type",
                        type=ToolParameterType.STRING,
                        description="Schedule type: 'interval', 'daily', or 'hourly'.",
                        enum=["interval", "daily", "hourly"],
                    ),
                    ToolParameter(
                        name="interval_seconds",
                        type=ToolParameterType.NUMBER,
                        description="Seconds between runs (for 'interval' type).",
                        required=False,
                    ),
                    ToolParameter(
                        name="hour",
                        type=ToolParameterType.INTEGER,
                        description="Hour of day 0-23 (for 'daily' type).",
                        required=False,
                    ),
                    ToolParameter(
                        name="minute",
                        type=ToolParameterType.INTEGER,
                        description="Minute of hour 0-59 (for 'daily' or 'hourly' type).",
                        required=False,
                    ),
                    ToolParameter(
                        name="message",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional free-text message. If no tool or "
                            "ai_prompt is given, published with the "
                            "'alarm.fired' event on each fire."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="tool",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional tool name to invoke on each fire. "
                            "Mutually exclusive with ai_prompt. Caller "
                            "must have permission for the target tool "
                            "at setup time."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="tool_arguments",
                        type=ToolParameterType.OBJECT,
                        description=(
                            "Arguments object to pass to the target "
                            "tool on each fire (used with 'tool')."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="ai_prompt",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional natural-language instruction to "
                            "run through the AI on each fire. Mutually "
                            "exclusive with tool. AI fires are globally "
                            "rate-limited (default 1 per 15 minutes) — "
                            "don't use this for alarms that fire more "
                            "often than the rate limit allows, or most "
                            "fires will be skipped."
                        ),
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="cancel_timer",
                description="Cancel a user timer or alarm by name. Cannot cancel system timers.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of the timer/alarm to cancel.",
                    ),
                ],
            ),
            ToolDefinition(
                name="pause_timer",
                description="Pause a timer or alarm (admin only). System timers can only be paused, not cancelled.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of the timer/alarm to pause.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="resume_timer",
                description="Resume a paused timer or alarm (admin only).",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of the timer/alarm to resume.",
                    ),
                ],
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "list_timers":
                return self._tool_list_timers()
            case "set_timer":
                return await self._tool_set_timer(arguments)
            case "set_alarm":
                return await self._tool_set_alarm(arguments)
            case "cancel_timer":
                return self._tool_cancel_timer(arguments)
            case "pause_timer":
                return self._tool_pause_timer(arguments)
            case "resume_timer":
                return self._tool_resume_timer(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    def _tool_list_timers(self) -> str:
        jobs = self.list_jobs()
        return json.dumps([
            {
                "name": j.name,
                "type": "system" if j.system else "user",
                "schedule": j.schedule.type.value,
                "interval_seconds": j.schedule.interval_seconds,
                "hour": j.schedule.hour,
                "minute": j.schedule.minute,
                "state": j.state.value,
                "enabled": j.enabled,
                "owner": j.owner,
                "run_count": j.run_count,
                "last_run": j.last_run,
                "last_error": j.last_error,
                "action": j.action.to_dict(),
            }
            for j in jobs
        ])

    async def _tool_set_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        seconds = float(arguments["seconds"])

        action, err = self._build_action_from_args(arguments)
        if err is not None:
            return json.dumps({"error": err})

        schedule = Schedule.once_after(seconds)
        user = get_current_user()
        owner = user.user_id if user else ""
        callback = self._make_fire_callback(
            timer_name, action, owner, "timer.fired"
        )

        try:
            self.add_job(
                name=timer_name,
                schedule=schedule,
                callback=callback,
                system=False,
                owner=owner,
                action=action,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})

        # Persist after successful registration so we never leave a
        # storage record pointing at a job that doesn't exist.
        await self._persist_job(timer_name, schedule, action, owner)

        return json.dumps({
            "status": "set",
            "name": timer_name,
            "seconds": seconds,
            "action_type": action.type.value,
        })

    async def _tool_set_alarm(self, arguments: dict[str, Any]) -> str:
        alarm_name = arguments["name"]
        alarm_type = arguments["type"]

        if alarm_type == "interval":
            schedule = Schedule.every(
                float(arguments.get("interval_seconds", 60))
            )
        elif alarm_type == "daily":
            schedule = Schedule.daily_at(
                hour=int(arguments.get("hour", 0)),
                minute=int(arguments.get("minute", 0)),
            )
        elif alarm_type == "hourly":
            schedule = Schedule.hourly_at(
                minute=int(arguments.get("minute", 0))
            )
        else:
            return json.dumps({"error": f"Unknown schedule type: {alarm_type}"})

        action, err = self._build_action_from_args(arguments)
        if err is not None:
            return json.dumps({"error": err})

        user = get_current_user()
        owner = user.user_id if user else ""
        callback = self._make_fire_callback(
            alarm_name, action, owner, "alarm.fired"
        )

        try:
            self.add_job(
                name=alarm_name,
                schedule=schedule,
                callback=callback,
                system=False,
                owner=owner,
                action=action,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})

        await self._persist_job(alarm_name, schedule, action, owner)

        return json.dumps({
            "status": "set",
            "name": alarm_name,
            "type": alarm_type,
            "action_type": action.type.value,
        })

    def _tool_cancel_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        user = get_current_user()
        # Admin can cancel any timer; others can only cancel their own
        is_admin = "admin" in user.roles or user.user_id == "system"
        requester_id = "" if is_admin else user.user_id
        try:
            self.remove_job(timer_name, requester_id=requester_id)
        except (KeyError, ValueError, PermissionError) as e:
            return json.dumps({"error": str(e)})
        # Best-effort persistence cleanup after successful removal. Fire
        # and forget — the in-memory job is already gone, so a stale
        # storage record will be dropped on the next startup load.
        import contextlib

        with contextlib.suppress(RuntimeError):
            # No running event loop shouldn't happen in an async
            # context, but defend against test edge cases.
            asyncio.create_task(self._unpersist_job(timer_name))
        return json.dumps({"status": "cancelled", "name": timer_name})

    def _tool_pause_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        try:
            self.disable_job(timer_name)
        except KeyError as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"status": "paused", "name": timer_name})

    def _tool_resume_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        try:
            self.enable_job(timer_name)
        except KeyError as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"status": "resumed", "name": timer_name})
