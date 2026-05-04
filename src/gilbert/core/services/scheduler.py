"""Scheduler service — manages system and user timers/alarms."""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.scheduler import (
    ActionStep,
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


def _parse_optional_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 datetime string into a naive local datetime.

    Accepts ``None`` and empty strings (both return ``None``). Strips
    trailing ``Z`` for Python < 3.11 compatibility. Any timezone-aware
    input is converted to local-naive — the scheduler's time arithmetic
    is naive-local throughout, matching how ``hour``/``minute`` are
    already interpreted for DAILY/HOURLY schedules.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _parse_optional_time(value: Any) -> dtime | None:
    """Parse a ``HH:MM`` (or ``HH:MM:SS``) string into a ``time``.

    ``None`` / empty returns ``None``; unparseable input returns
    ``None`` rather than raising so that a bad config value just
    disables the window rather than crashing the scheduler.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return dtime.fromisoformat(s)
    except ValueError:
        return None


def _format_optional_datetime(dt: datetime | None) -> str:
    """Inverse of ``_parse_optional_iso_datetime`` for persistence."""
    return dt.isoformat() if dt is not None else ""


def _format_optional_time(t: dtime | None) -> str:
    """Inverse of ``_parse_optional_time`` for persistence."""
    return t.isoformat() if t is not None else ""


def _clamp_to_daily_window(
    candidate: datetime, window_start: dtime, window_end: dtime
) -> datetime:
    """Push ``candidate`` forward to the next point inside [start, end].

    If ``candidate``'s time-of-day is already inside the window, returns
    it unchanged. If it's before the start, jumps to today's start. If
    it's past the end, jumps to tomorrow's start. Overnight windows
    (end < start) are not supported — the caller validates that.
    """
    today_start = candidate.replace(
        hour=window_start.hour,
        minute=window_start.minute,
        second=window_start.second,
        microsecond=0,
    )
    today_end = candidate.replace(
        hour=window_end.hour,
        minute=window_end.minute,
        second=window_end.second,
        microsecond=0,
    )
    if candidate < today_start:
        return today_start
    if candidate <= today_end:
        return candidate
    return today_start + timedelta(days=1)


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
        self._ai_profile: str = "standard"
        self._scheduled_action_prompt: str = _SCHEDULED_ACTION_SYSTEM_PROMPT

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="scheduler",
            capabilities=frozenset({"scheduler", "ai_tools", "ws_handlers"}),
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
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile for scheduled AI actions.",
                default="standard",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="scheduled_action_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for AI-driven scheduled actions. The "
                    "stored instruction goes in as the user message; this "
                    "controls the AI's framing and confirmation style. "
                    "Leave blank to use the bundled default."
                ),
                default=_SCHEDULED_ACTION_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        max_calls = int(config.get("alarm_ai_max_calls", self._DEFAULT_AI_MAX_CALLS))
        window = float(config.get("alarm_ai_window_seconds", self._DEFAULT_AI_WINDOW_SECONDS))
        self._ai_rate_limiter.update_config(max_calls, window)
        self._ai_profile = config.get("ai_profile", self._ai_profile)
        self._scheduled_action_prompt = (
            str(config.get("scheduled_action_prompt", "") or "")
            or _SCHEDULED_ACTION_SYSTEM_PROMPT
        )
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
            except TimeoutError:
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
        return [j.info for j in self._jobs.values() if include_system or not j.info.system]

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
        """Run a job on its schedule until cancelled or retired.

        Tracks ``last_fire_at`` locally so interval-with-window jobs can
        anchor the next-fire calculation off the previous fire (not off
        the loop iteration time, which drifts when sleeps are long).
        The loop exits cleanly when ``_next_delay`` returns ``None`` —
        that means the schedule has nothing more to do (one-shot done,
        past ``end_at``, etc.) and the job transitions to ``DONE``.
        """
        last_fire_at: datetime | None = None
        try:
            while True:
                delay = self._next_delay(job.info.schedule, last_fire_at)
                if delay is None:
                    job.info.state = JobState.DONE
                    return
                await asyncio.sleep(delay)

                if not job.info.enabled:
                    continue

                await self._execute_job(job)
                last_fire_at = datetime.now()

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
    def _next_delay(
        schedule: Schedule,
        last_fire_at: datetime | None = None,
    ) -> float | None:
        """Seconds until the next fire, or ``None`` if the job is retired.

        ``last_fire_at`` lets interval jobs anchor off the previous fire
        so drift doesn't accumulate across long bounds/window sleeps.
        For non-interval schedules the parameter is ignored — DAILY and
        HOURLY compute against ``datetime.now()`` directly.

        Returns ``None`` in three cases:
        - A ONCE job has already fired.
        - The next computed fire would land after ``end_at``.
        - The schedule type is unrecognised (shouldn't happen, but
          returning ``None`` retires the loop cleanly rather than
          looping forever at the 60s fallback).
        """
        now = datetime.now()

        if schedule.type == ScheduleType.ONCE:
            # ONCE is one-shot: if we've already fired, we're done.
            if last_fire_at is not None:
                return None
            return schedule.interval_seconds

        # Candidate next-fire, ignoring bounds/window.
        natural: datetime
        if schedule.type == ScheduleType.INTERVAL:
            if last_fire_at is None:
                # First fire: honour start_at if set, else fire ASAP.
                natural = schedule.start_at or now
            else:
                natural = last_fire_at + timedelta(
                    seconds=schedule.interval_seconds
                )
        elif schedule.type == ScheduleType.DAILY:
            target = now.replace(
                hour=schedule.hour,
                minute=schedule.minute,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)
            natural = target
        elif schedule.type == ScheduleType.HOURLY:
            target = now.replace(minute=schedule.minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(hours=1)
            natural = target
        else:
            return None

        # ``start_at`` only delays the first fire for non-interval
        # schedules (INTERVAL handles it in the natural calc above).
        # For DAILY/HOURLY we still need to push past ``start_at``.
        if schedule.start_at is not None and natural < schedule.start_at:
            natural = schedule.start_at
            # DAILY/HOURLY anchor natural to a specific time-of-day;
            # if start_at itself doesn't match that anchor we have to
            # advance to the next valid anchor after start_at.
            if schedule.type == ScheduleType.DAILY:
                anchor = natural.replace(
                    hour=schedule.hour,
                    minute=schedule.minute,
                    second=0,
                    microsecond=0,
                )
                if anchor < natural:
                    anchor += timedelta(days=1)
                natural = anchor
            elif schedule.type == ScheduleType.HOURLY:
                anchor = natural.replace(
                    minute=schedule.minute, second=0, microsecond=0
                )
                if anchor < natural:
                    anchor += timedelta(hours=1)
                natural = anchor

        # Daily recurring window — only meaningful for INTERVAL.
        if (
            schedule.type == ScheduleType.INTERVAL
            and schedule.window_start_time is not None
            and schedule.window_end_time is not None
        ):
            natural = _clamp_to_daily_window(
                natural,
                schedule.window_start_time,
                schedule.window_end_time,
            )

        # Absolute deadline — if we've rolled past it, retire the job.
        if schedule.end_at is not None and natural > schedule.end_at:
            return None

        delay = (natural - now).total_seconds()
        return max(0.0, delay)

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
            elif action.type == ScheduledActionType.SEQUENCE:
                await self._dispatch_sequence_action(job_name, action, owner)
            else:
                await self._dispatch_event_action(job_name, action, event_type)
        except Exception:
            # Never let a dispatch failure crash the scheduler loop —
            # the next scheduled fire must still happen.
            logger.exception("Scheduler action dispatch failed for job '%s'", job_name)

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
        if not action.tool:
            logger.warning("Scheduler: job '%s' has tool action but no tool name", job_name)
            return
        await self._invoke_tool_by_name(job_name, action.tool, action.tool_arguments)

    async def _dispatch_sequence_action(
        self,
        job_name: str,
        action: ScheduledAction,
        owner: str,
    ) -> None:
        """Run an ordered sequence of tool calls with per-step delays.

        Each step's ``delay_before_seconds`` is awaited (via
        ``asyncio.sleep``) before the tool is invoked. A failing step
        is logged but does NOT abort the remaining steps — this matches
        the forgiving behavior of the single-tool dispatcher so a
        recurring sequence can self-heal across fires. Inter-step
        sleeps only hold THIS fire's coroutine; other scheduled jobs
        continue to tick normally.
        """
        if not action.steps:
            logger.warning(
                "Scheduler: job '%s' has sequence action with no steps",
                job_name,
            )
            return

        for idx, step in enumerate(action.steps):
            if step.delay_before_seconds > 0:
                logger.debug(
                    "Scheduler '%s' sequence: sleeping %.1fs before step %d/%d",
                    job_name,
                    step.delay_before_seconds,
                    idx + 1,
                    len(action.steps),
                )
                try:
                    await asyncio.sleep(step.delay_before_seconds)
                except asyncio.CancelledError:
                    # Scheduler shutting down — abandon the remaining steps
                    logger.info(
                        "Scheduler '%s' sequence cancelled mid-run at step %d",
                        job_name,
                        idx + 1,
                    )
                    raise

            if not step.tool:
                logger.warning(
                    "Scheduler '%s' sequence step %d has no tool — skipping",
                    job_name,
                    idx + 1,
                )
                continue

            logger.debug(
                "Scheduler '%s' sequence step %d/%d → %s",
                job_name,
                idx + 1,
                len(action.steps),
                step.tool,
            )
            # Per-step exceptions are logged inside the helper and do
            # NOT propagate — the next step still runs.
            await self._invoke_tool_by_name(job_name, step.tool, step.tool_arguments)

    async def _invoke_tool_by_name(
        self,
        job_name: str,
        tool_name: str,
        tool_arguments: dict[str, Any],
    ) -> None:
        """Look up and invoke a tool by name. Logs + swallows failures.

        Shared between ``_dispatch_tool_action`` (single-tool) and
        ``_dispatch_sequence_action`` (multi-step) so both paths apply
        the same discovery, error handling, and result logging.
        """
        if self._resolver is None:
            logger.warning("Scheduler: job '%s' cannot fire — no resolver", job_name)
            return

        from gilbert.interfaces.tools import ToolProvider

        for svc in self._resolver.get_all("ai_tools"):
            if not isinstance(svc, ToolProvider):
                continue
            for tdef in svc.get_tools():
                if tdef.name != tool_name:
                    continue
                try:
                    result = await svc.execute_tool(tool_name, dict(tool_arguments))
                    logger.info(
                        "Scheduler '%s' → %s: %s",
                        job_name,
                        tool_name,
                        (result or "")[:200] if isinstance(result, str) else "(non-string result)",
                    )
                except Exception:
                    logger.exception("Scheduler '%s' → %s raised", job_name, tool_name)
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
                "Scheduler '%s' AI fire skipped — rate limit (%d/%d in last %ds window)",
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
            response_text, *_ = await ai_svc.chat(
                user_message=action.ai_prompt,
                user_ctx=UserContext.SYSTEM,
                system_prompt=self._scheduled_action_prompt,
                ai_profile=self._ai_profile,
            )
            logger.info(
                "Scheduler '%s' AI fire: %s",
                job_name,
                (response_text or "")[:200],
            )
        except Exception:
            logger.exception("Scheduler '%s' AI fire raised", job_name)

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
        raw_steps = arguments.get("steps")
        # Explicit presence check — an empty list still counts as
        # "caller is using the steps path" so we error on empty.
        has_steps = raw_steps is not None

        # Mutual exclusion: at most one of {tool, ai_prompt, steps}
        mode_count = sum(1 for x in (bool(tool_name), bool(ai_prompt), has_steps) if x)
        if mode_count > 1:
            return (
                ScheduledAction(),
                "Specify only one of 'tool', 'ai_prompt', or 'steps'.",
            )

        if has_steps:
            if not isinstance(raw_steps, list):
                return (
                    ScheduledAction(),
                    "'steps' must be a list of step objects.",
                )
            if len(raw_steps) == 0:
                return (
                    ScheduledAction(),
                    "'steps' must contain at least one step.",
                )

            steps: list[ActionStep] = []
            for idx, raw in enumerate(raw_steps):
                if not isinstance(raw, dict):
                    return (
                        ScheduledAction(),
                        f"Step {idx + 1}: must be an object.",
                    )
                step_tool = (raw.get("tool") or "").strip()
                if not step_tool:
                    return (
                        ScheduledAction(),
                        f"Step {idx + 1}: missing 'tool'.",
                    )
                step_args_raw = raw.get("tool_arguments") or {}
                if not isinstance(step_args_raw, dict):
                    return (
                        ScheduledAction(),
                        f"Step {idx + 1}: 'tool_arguments' must be an object.",
                    )
                # RBAC check per step — the caller must be allowed to
                # invoke each tool in the chain.
                err = self._validate_tool_exists_and_allowed(step_tool)
                if err:
                    return (
                        ScheduledAction(),
                        f"Step {idx + 1} ('{step_tool}'): {err}",
                    )
                try:
                    delay = float(raw.get("delay_before_seconds") or 0)
                except (TypeError, ValueError):
                    return (
                        ScheduledAction(),
                        f"Step {idx + 1}: 'delay_before_seconds' must be numeric.",
                    )
                steps.append(
                    ActionStep(
                        tool=step_tool,
                        tool_arguments=dict(step_args_raw),
                        delay_before_seconds=max(0.0, delay),
                    )
                )
            return (
                ScheduledAction(
                    type=ScheduledActionType.SEQUENCE,
                    steps=steps,
                    message=message,
                ),
                None,
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

    def _validate_tool_exists_and_allowed(self, tool_name: str) -> str | None:
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
            "start_at": _format_optional_datetime(schedule.start_at),
            "end_at": _format_optional_datetime(schedule.end_at),
            "window_start_time": _format_optional_time(schedule.window_start_time),
            "window_end_time": _format_optional_time(schedule.window_end_time),
            "owner": owner,
            "action": action.to_dict(),
            "created_at": now_iso,
        }
        # One-shot timers need a fire_at so we can drop them on startup
        # if they were scheduled to fire while Gilbert was down.
        if schedule.type == ScheduleType.ONCE:
            fire_at = datetime.now(UTC) + timedelta(seconds=schedule.interval_seconds)
            record["fire_at"] = fire_at.isoformat()

        try:
            await self._storage.put(_JOBS_COLLECTION, name, record)
        except Exception:
            logger.exception("Scheduler: failed to persist job '%s'", name)

    async def _unpersist_job(self, name: str) -> None:
        """Best-effort delete of a persisted job record."""
        if self._storage is None:
            return
        try:
            await self._storage.delete(_JOBS_COLLECTION, name)
        except Exception:
            logger.debug("Scheduler: unpersist of '%s' failed (may not exist)", name)

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
                    start_at=_parse_optional_iso_datetime(row.get("start_at")),
                    end_at=_parse_optional_iso_datetime(row.get("end_at")),
                    window_start_time=_parse_optional_time(
                        row.get("window_start_time")
                    ),
                    window_end_time=_parse_optional_time(
                        row.get("window_end_time")
                    ),
                )

                # Drop one-shot timers that should have already fired
                if sched_type == ScheduleType.ONCE:
                    fire_at_str = row.get("fire_at") or ""
                    if fire_at_str:
                        try:
                            fire_at = datetime.fromisoformat(fire_at_str.replace("Z", "+00:00"))
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

                # Drop recurring jobs whose end_at is already past — they
                # would just re-register, tick once, and retire to DONE.
                # Dropping at load time keeps the scheduler clean across
                # restarts.
                if (
                    schedule.end_at is not None
                    and schedule.end_at <= datetime.now()
                ):
                    logger.info(
                        "Scheduler: dropping expired recurring job '%s' "
                        "(end_at=%s)",
                        name,
                        schedule.end_at.isoformat(),
                    )
                    await self._unpersist_job(name)
                    dropped_expired += 1
                    continue

                action = ScheduledAction.from_dict(row.get("action"))
                owner = str(row.get("owner") or "")
                event_type = "timer.fired" if sched_type == ScheduleType.ONCE else "alarm.fired"
                callback = self._make_fire_callback(name, action, owner, event_type)
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

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="list_timers",
                slash_group="timer",
                slash_command="list",
                slash_help="List all active timers and alarms: /timer list",
                description="List all active timers and alarms (both system and user).",
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="set_timer",
                slash_group="timer",
                slash_command="set",
                slash_help=(
                    "One-shot timer: /timer set <name> <seconds> "
                    "message='...' — or tool=... / ai_prompt=... for "
                    "actions on fire."
                ),
                description=(
                    "Set a user timer that fires ONCE after a delay. By "
                    "default publishes a 'timer.fired' event; optionally "
                    "invoke a specific tool (tool + tool_arguments), run "
                    "an AI instruction (ai_prompt), or chain multiple "
                    "tool calls with optional delays (steps). Persisted "
                    "across restarts. Prefer 'tool' or 'steps' for "
                    "deterministic, frequent, or cheap actions. Use "
                    "'ai_prompt' for complex or conditional actions — "
                    "AI fires are globally rate-limited to avoid "
                    "runaway cost."
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
                            "Optional free-text message. If no tool / "
                            "ai_prompt / steps is given, this message "
                            "is published with the 'timer.fired' event."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="tool",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional tool name to invoke when the timer "
                            "fires. Mutually exclusive with ai_prompt "
                            "and steps. The caller must have permission "
                            "to use the target tool at setup time."
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
                            "Mutually exclusive with tool and steps. "
                            "AI fires are globally rate-limited."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="steps",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "Optional ordered list of tool calls to run "
                            "in sequence on each fire. Each step is an "
                            "object with 'tool' (string, required), "
                            "'tool_arguments' (object, optional), and "
                            "'delay_before_seconds' (number, optional) "
                            "which waits that many seconds before "
                            "running the step. Example for 'play music "
                            "for 5 seconds then announce': [{tool: "
                            "'music_play', tool_arguments: {...}}, "
                            "{tool: 'music_stop', tool_arguments: {...}, "
                            "delay_before_seconds: 5}, {tool: "
                            "'audio_output', tool_arguments: {...}}]. "
                            "Mutually exclusive with tool and ai_prompt."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="set_alarm",
                slash_group="timer",
                slash_command="alarm",
                slash_help=(
                    "Recurring alarm: /timer alarm <name> <type> "
                    "[hour=... minute=... interval_seconds=...] "
                    "[message=... tool=... ai_prompt=...]"
                ),
                description=(
                    "Set a recurring user alarm (interval, daily, or "
                    "hourly). By default publishes an 'alarm.fired' "
                    "event on each fire; optionally invoke a single "
                    "tool (tool + tool_arguments), run an AI "
                    "instruction (ai_prompt), or chain multiple tool "
                    "calls with delays (steps). Persisted across "
                    "restarts. Prefer 'tool' or 'steps' for frequent "
                    "alarms — AI fires are globally rate-limited."
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
                            "Optional free-text message. If no tool / "
                            "ai_prompt / steps is given, published with "
                            "the 'alarm.fired' event on each fire."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="tool",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional tool name to invoke on each fire. "
                            "Mutually exclusive with ai_prompt and "
                            "steps. Caller must have permission for "
                            "the target tool at setup time."
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
                            "exclusive with tool and steps. AI fires "
                            "are globally rate-limited (default 1 per "
                            "15 minutes) — don't use this for alarms "
                            "that fire more often than the rate limit "
                            "allows, or most fires will be skipped."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="steps",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "Optional ordered list of tool calls to run "
                            "in sequence on each fire. Each step is an "
                            "object with 'tool' (string, required), "
                            "'tool_arguments' (object, optional), and "
                            "'delay_before_seconds' (number, optional) "
                            "which waits that many seconds before "
                            "running the step. Example for 'play music "
                            "for 5 seconds then announce': [{tool: "
                            "'music_play', tool_arguments: {...}}, "
                            "{tool: 'music_stop', tool_arguments: {...}, "
                            "delay_before_seconds: 5}, {tool: "
                            "'audio_output', tool_arguments: {...}}]. "
                            "Mutually exclusive with tool and ai_prompt. "
                            "Make sure the total sequence duration fits "
                            "within the alarm interval to avoid "
                            "overlapping fires."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="start_at",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional ISO-8601 datetime (local time, e.g. "
                            "'2026-04-20T01:00:00') — the first fire cannot "
                            "happen before this. Use for 'every minute "
                            "starting at 1am': type='interval', "
                            "interval_seconds=60, start_at='<today>T01:00:00'."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="end_at",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional ISO-8601 datetime — the alarm retires "
                            "after this. Combine with start_at for a bounded "
                            "run (e.g. 'from 1am to 2am today only'). Must be "
                            "after start_at."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="window_start_time",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional daily recurring time-of-day window "
                            "start, 'HH:MM' (24h). Only applies to "
                            "type='interval'. Must be paired with "
                            "window_end_time. Use for 'every minute from "
                            "1am to 2am every day': type='interval', "
                            "interval_seconds=60, window_start_time='01:00', "
                            "window_end_time='02:00'. Overnight windows "
                            "(end before start) are not supported."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="window_end_time",
                        type=ToolParameterType.STRING,
                        description=(
                            "Daily window end time, 'HH:MM' (24h). Paired "
                            "with window_start_time."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="cancel_timer",
                slash_group="timer",
                slash_command="cancel",
                slash_help="Cancel a timer/alarm: /timer cancel <name>",
                description="Cancel a user timer or alarm by name. Cannot cancel system timers.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of the timer/alarm to cancel.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="pause_timer",
                slash_group="timer",
                slash_command="pause",
                slash_help="Pause a timer/alarm: /timer pause <name>",
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
                slash_group="timer",
                slash_command="resume",
                slash_help="Resume a paused timer/alarm: /timer resume <name>",
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
        return json.dumps(
            [
                {
                    "name": j.name,
                    "type": "system" if j.system else "user",
                    "schedule": j.schedule.type.value,
                    "interval_seconds": j.schedule.interval_seconds,
                    "hour": j.schedule.hour,
                    "minute": j.schedule.minute,
                    "start_at": _format_optional_datetime(j.schedule.start_at),
                    "end_at": _format_optional_datetime(j.schedule.end_at),
                    "window_start_time": _format_optional_time(
                        j.schedule.window_start_time
                    ),
                    "window_end_time": _format_optional_time(
                        j.schedule.window_end_time
                    ),
                    "state": j.state.value,
                    "enabled": j.enabled,
                    "owner": j.owner,
                    "run_count": j.run_count,
                    "last_run": j.last_run,
                    "last_error": j.last_error,
                    "action": j.action.to_dict(),
                }
                for j in jobs
            ]
        )

    async def _tool_set_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        seconds = float(arguments["seconds"])

        action, err = self._build_action_from_args(arguments)
        if err is not None:
            return json.dumps({"error": err})

        schedule = Schedule.once_after(seconds)
        user = get_current_user()
        owner = user.user_id if user else ""
        callback = self._make_fire_callback(timer_name, action, owner, "timer.fired")

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

        return json.dumps(
            {
                "status": "set",
                "name": timer_name,
                "seconds": seconds,
                "action_type": action.type.value,
            }
        )

    async def _tool_set_alarm(self, arguments: dict[str, Any]) -> str:
        alarm_name = arguments["name"]
        alarm_type = arguments["type"]

        start_at = _parse_optional_iso_datetime(arguments.get("start_at"))
        end_at = _parse_optional_iso_datetime(arguments.get("end_at"))
        window_start = _parse_optional_time(arguments.get("window_start_time"))
        window_end = _parse_optional_time(arguments.get("window_end_time"))

        # Validate input: a bounded end must come after the bounded
        # start, and a window must be same-day with start < end.
        if start_at is not None and end_at is not None and end_at <= start_at:
            return json.dumps({"error": "end_at must be after start_at."})
        if (window_start is None) != (window_end is None):
            return json.dumps(
                {
                    "error": (
                        "window_start_time and window_end_time must be set "
                        "together."
                    )
                }
            )
        if (
            window_start is not None
            and window_end is not None
            and window_end <= window_start
        ):
            return json.dumps(
                {
                    "error": (
                        "window_end_time must be after window_start_time "
                        "(overnight windows are not supported)."
                    )
                }
            )
        if window_start is not None and alarm_type != "interval":
            return json.dumps(
                {
                    "error": (
                        "window_start_time / window_end_time only apply to "
                        "'interval' alarms."
                    )
                }
            )

        if alarm_type == "interval":
            schedule = Schedule.every(
                float(arguments.get("interval_seconds", 60)),
                start_at=start_at,
                end_at=end_at,
                window_start_time=window_start,
                window_end_time=window_end,
            )
        elif alarm_type == "daily":
            schedule = Schedule.daily_at(
                hour=int(arguments.get("hour", 0)),
                minute=int(arguments.get("minute", 0)),
                start_at=start_at,
                end_at=end_at,
            )
        elif alarm_type == "hourly":
            schedule = Schedule.hourly_at(
                minute=int(arguments.get("minute", 0)),
                start_at=start_at,
                end_at=end_at,
            )
        else:
            return json.dumps({"error": f"Unknown schedule type: {alarm_type}"})

        action, err = self._build_action_from_args(arguments)
        if err is not None:
            return json.dumps({"error": err})

        user = get_current_user()
        owner = user.user_id if user else ""
        callback = self._make_fire_callback(alarm_name, action, owner, "alarm.fired")

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

        return json.dumps(
            {
                "status": "set",
                "name": alarm_name,
                "type": alarm_type,
                "action_type": action.type.value,
            }
        )

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

    # --- WebSocket RPC handlers ---

    def get_ws_handlers(self) -> dict[str, Any]:
        """Expose scheduler operations to the web UI over WebSocket.

        Frame type namespace: ``scheduler.job.*``. Default permission
        levels are declared in ``gilbert.interfaces.acl`` — listing and
        deletion are user-level (ownership is enforced per-handler for
        non-admins), while enable/disable/run_now are admin-only.
        """
        return {
            "scheduler.job.list": self._ws_job_list,
            "scheduler.job.get": self._ws_job_get,
            "scheduler.job.enable": self._ws_job_enable,
            "scheduler.job.disable": self._ws_job_disable,
            "scheduler.job.remove": self._ws_job_remove,
            "scheduler.job.run_now": self._ws_job_run_now,
        }

    @staticmethod
    def _serialize_job(info: JobInfo) -> dict[str, Any]:
        """Convert a JobInfo to a plain dict for JSON transmission."""
        return {
            "name": info.name,
            "type": "system" if info.system else "user",
            "state": info.state.value,
            "enabled": info.enabled,
            "owner": info.owner,
            "run_count": info.run_count,
            "last_run": info.last_run,
            "last_duration_seconds": info.last_duration_seconds,
            "last_error": info.last_error,
            "schedule": {
                "type": info.schedule.type.value,
                "interval_seconds": info.schedule.interval_seconds,
                "hour": info.schedule.hour,
                "minute": info.schedule.minute,
                "start_at": _format_optional_datetime(info.schedule.start_at),
                "end_at": _format_optional_datetime(info.schedule.end_at),
                "window_start_time": _format_optional_time(
                    info.schedule.window_start_time
                ),
                "window_end_time": _format_optional_time(
                    info.schedule.window_end_time
                ),
            },
            "action": info.action.to_dict(),
        }

    @staticmethod
    def _ws_error(
        frame: dict[str, Any],
        *,
        error: str,
        code: int = 400,
    ) -> dict[str, Any]:
        """Build a standard error response frame."""
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": error,
            "code": code,
        }

    async def _ws_job_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """List all jobs (system + user) with their current state and action."""
        include_system = bool(frame.get("include_system", True))
        jobs = self.list_jobs(include_system=include_system)
        return {
            "type": "scheduler.job.list.result",
            "ref": frame.get("id"),
            "jobs": [self._serialize_job(j) for j in jobs],
        }

    async def _ws_job_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Get detailed info about a single job by name."""
        name = str(frame.get("name") or "").strip()
        if not name:
            return self._ws_error(frame, error="Missing 'name'")
        info = self.get_job(name)
        if info is None:
            return self._ws_error(frame, error=f"Job not found: {name}", code=404)
        return {
            "type": "scheduler.job.get.result",
            "ref": frame.get("id"),
            "job": self._serialize_job(info),
        }

    async def _ws_job_enable(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Enable a disabled job. Admin-level via RPC permissions."""
        name = str(frame.get("name") or "").strip()
        if not name:
            return self._ws_error(frame, error="Missing 'name'")
        try:
            self.enable_job(name)
        except KeyError:
            return self._ws_error(frame, error=f"Job not found: {name}", code=404)
        return {
            "type": "scheduler.job.enable.result",
            "ref": frame.get("id"),
            "name": name,
            "status": "enabled",
        }

    async def _ws_job_disable(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Disable (pause) a running job. Admin-level via RPC permissions."""
        name = str(frame.get("name") or "").strip()
        if not name:
            return self._ws_error(frame, error="Missing 'name'")
        try:
            self.disable_job(name)
        except KeyError:
            return self._ws_error(frame, error=f"Job not found: {name}", code=404)
        return {
            "type": "scheduler.job.disable.result",
            "ref": frame.get("id"),
            "name": name,
            "status": "disabled",
        }

    async def _ws_job_remove(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Cancel and delete a job.

        Non-admins can only remove jobs they own. System jobs cannot be
        removed by anyone (the service layer enforces this and raises
        ValueError).
        """
        name = str(frame.get("name") or "").strip()
        if not name:
            return self._ws_error(frame, error="Missing 'name'")

        # Ownership check: non-admins can only cancel their own jobs.
        user = getattr(conn, "user_ctx", None)
        user_id = getattr(user, "user_id", "") if user else ""
        roles: frozenset[str] = getattr(user, "roles", frozenset()) if user else frozenset()
        is_admin = "admin" in roles or getattr(conn, "user_level", 999) < 0
        requester_id = "" if is_admin else user_id

        try:
            self.remove_job(name, requester_id=requester_id)
        except KeyError:
            return self._ws_error(frame, error=f"Job not found: {name}", code=404)
        except ValueError as e:
            return self._ws_error(frame, error=str(e), code=400)
        except PermissionError as e:
            return self._ws_error(frame, error=str(e), code=403)

        # Best-effort persistence cleanup. The in-memory job is already
        # gone, so a stale storage record will be dropped on next start.
        import contextlib

        with contextlib.suppress(RuntimeError):
            asyncio.create_task(self._unpersist_job(name))

        return {
            "type": "scheduler.job.remove.result",
            "ref": frame.get("id"),
            "name": name,
            "status": "removed",
        }

    async def _ws_job_run_now(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Fire a job immediately, outside its schedule. Admin-level."""
        name = str(frame.get("name") or "").strip()
        if not name:
            return self._ws_error(frame, error="Missing 'name'")
        try:
            await self.run_now(name)
        except KeyError:
            return self._ws_error(frame, error=f"Job not found: {name}", code=404)
        return {
            "type": "scheduler.job.run_now.result",
            "ref": frame.get("id"),
            "name": name,
            "status": "fired",
        }
