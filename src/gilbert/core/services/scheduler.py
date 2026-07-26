"""Scheduler service — manages system and user timers/alarms."""

import asyncio
import contextvars
import json
import logging
import random
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.context import get_current_user
from gilbert.interfaces.cron import CronKind, CronParseError
from gilbert.interfaces.scheduler import (
    ActionStep,
    CatchUpPolicy,
    JobCallback,
    JobInfo,
    JobState,
    OverlapPolicy,
    Schedule,
    ScheduledAction,
    ScheduledActionType,
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

# Last-fire history, keyed by job name. Separate from _JOBS_COLLECTION
# because system jobs are never persisted there but still need catch-up.
_JOB_STATE_COLLECTION = "scheduler_job_state"

#: Longest single sleep before re-reading the wall clock. Bounds how
#: late a fire can be after a host suspend or clock adjustment.
_SLEEP_SLICE_SECONDS = 60.0

#: Ceiling on BACKFILL catch-up, so a fast job plus long downtime can't
#: enumerate (or fire) an unbounded number of occurrences.
_MAX_BACKFILL_FIRES = 100

#: Ceiling on window-gating iterations, guarding against an expression
#: that can never fall inside its window.
_MAX_WINDOW_ADVANCES = 400


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


def _as_aware(dt: datetime | None, tz: Any) -> datetime | None:
    """Attach ``tz`` to a naive bound so it can be compared as an instant.

    Persisted bounds are naive-local by convention; the engine compares
    instants, so they have to be localised before use.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)


def _parse_catch_up(value: Any) -> CatchUpPolicy:
    """Read a persisted catch-up policy, defaulting to SKIP."""
    try:
        return CatchUpPolicy(str(value or "skip"))
    except ValueError:
        return CatchUpPolicy.SKIP


def _parse_overlap(value: Any) -> OverlapPolicy:
    """Read a persisted overlap policy, defaulting to SKIP."""
    try:
        return OverlapPolicy(str(value or "skip"))
    except ValueError:
        return OverlapPolicy.SKIP


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
        #: True while a fire is in flight — drives OverlapPolicy.SKIP.
        self.is_running = False
        #: Serialises fires under OverlapPolicy.QUEUE.
        self.lock = asyncio.Lock()
        #: In-flight fire tasks, so stop() can cancel them.
        self.fire_tasks: set[asyncio.Task[None]] = set()


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
            for fire in job.fire_tasks:
                fire.cancel()
        # Wait briefly for tasks to finish, then move on
        tasks: list[asyncio.Task[None]] = [
            j.task for j in self._jobs.values() if j.task is not None
        ]
        for j in self._jobs.values():
            tasks.extend(j.fire_tasks)
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
        replace_existing: bool = False,
    ) -> JobInfo:
        """Register a job. System jobs are not user-editable.

        The job starts running immediately if enabled. The optional
        ``action`` carries dispatch metadata (tool call, ai_prompt, or
        event fallback) so ``list_jobs()`` can report what each job
        will do when it fires.

        Pass ``replace_existing=True`` for system jobs registered from
        a service's ``start()`` so that a service-restart cycle
        (triggered by a config change) doesn't trip the "already
        registered" guard. ``stop()`` can't remove system jobs through
        the public ``remove_job`` API, so idempotent re-registration is
        the cleanest pattern. Without this flag, set-once behavior is
        preserved.
        """
        if name in self._jobs:
            if not replace_existing:
                raise ValueError(f"Job '{name}' already registered")
            existing = self._jobs[name]
            if existing.task is not None:
                existing.task.cancel()
            del self._jobs[name]

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

        job.info.description = schedule.describe()
        logger.info(
            "Job '%s' registered (%s, %s)",
            name,
            "system" if system else "user",
            schedule.expression,
        )
        return job.info

    def remove_job(
        self, name: str, requester_id: str = "", *, force: bool = False
    ) -> None:
        """Remove a job. System jobs cannot be removed by external callers.

        Non-admin users can only remove jobs they own. The owning
        service of a system job can pass ``force=True`` to replace it
        (e.g. AgentService re-arming a heartbeat).
        """
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(f"Job not found: {name}")
        if job.info.system and not force:
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
        job.info.next_run_at = ""
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

        The loop computes an absolute next-fire *instant* rather than a
        delay, then sleeps toward it in bounded slices. Working in
        instants is what makes the loop survive a host suspend or a
        clock adjustment: each slice re-reads the wall clock, so a
        machine that sleeps through a fire wakes up and notices,
        instead of firing late by exactly the lost interval.

        Fires are dispatched as separate tasks so a slow callback never
        stalls scheduling — which is also what makes
        :class:`OverlapPolicy` meaningful.
        """
        schedule = job.info.schedule
        tz = schedule.resolve_timezone()
        job.info.description = schedule.describe()

        last_fire_at = await self._load_last_fire_at(job.info.name)
        if last_fire_at is not None:
            await self._run_catch_up(job, last_fire_at)

        try:
            while True:
                now = datetime.now(tz)
                target = self._next_fire_at(schedule, last_fire_at, now)
                if target is None:
                    job.info.next_run_at = ""
                    job.info.state = JobState.DONE
                    return

                job.info.next_run_at = target.isoformat()
                await self._sleep_until(target)

                # Retire if this loop has been orphaned. ``remove_job`` /
                # a replacing ``add_job`` / ``disable_job`` all cancel the
                # task, but cancellation only stops the loop if it actually
                # propagates. A callback that swallows ``CancelledError`` —
                # as ``AIService.chat()`` does for the user stop button —
                # lets a cancelled fire return normally, after which this
                # loop would keep firing forever, orphaned out of
                # ``self._jobs`` and invisible to ``list_jobs`` (the "ghost
                # alarm" bug). Re-checking the registry after every sleep
                # kills the orphan on its next wake regardless of whether
                # the cancellation landed.
                if (
                    self._jobs.get(job.info.name) is not job
                    or job.task is not asyncio.current_task()
                ):
                    return

                if not job.info.enabled:
                    continue

                if schedule.is_one_shot:
                    # Awaited inline: there is no subsequent fire to
                    # protect, and the terminal state must not be
                    # overwritten by a still-running fire task.
                    await self._execute_job(job)
                    await self._record_fire(job.info.name, datetime.now(tz))
                    job.info.next_run_at = ""
                    if job.info.state is not JobState.FAILED:
                        job.info.state = JobState.DONE
                    return

                self._dispatch_fire(job)
                last_fire_at = datetime.now(tz)
                await self._record_fire(job.info.name, last_fire_at)
        except asyncio.CancelledError:
            job.info.next_run_at = ""
            return

    async def _sleep_until(self, target: datetime) -> None:
        """Sleep toward an absolute instant in bounded slices.

        A single ``asyncio.sleep(86400)`` for a daily job cannot notice
        that the host suspended for two hours. Re-reading the clock
        every slice bounds that error to one slice.
        """
        while True:
            remaining = (
                target.astimezone(UTC) - datetime.now(UTC)
            ).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, _SLEEP_SLICE_SECONDS))

    def _dispatch_fire(self, job: _Job) -> None:
        """Start a fire as its own task, honouring the overlap policy.

        ``context=contextvars.copy_context()`` keeps request-scoped
        context vars from leaking between concurrent fires.
        """
        policy = job.info.schedule.overlap

        if job.is_running and policy is OverlapPolicy.SKIP:
            logger.info(
                "Scheduler '%s': skipping fire — previous run still in "
                "flight (overlap=skip)",
                job.info.name,
            )
            return

        async def _run() -> None:
            if policy is OverlapPolicy.QUEUE:
                async with job.lock:
                    await self._execute_job(job)
            else:
                await self._execute_job(job)

        task = asyncio.create_task(_run(), context=contextvars.copy_context())
        job.fire_tasks.add(task)
        task.add_done_callback(job.fire_tasks.discard)

    async def _run_catch_up(self, job: _Job, last_fire_at: datetime) -> None:
        """Make up for fires missed while the process was down.

        Driven by the persisted ``last_fire_at`` — without it there is
        no way to tell a fresh install from a machine that was off for
        a week.
        """
        schedule = job.info.schedule
        policy = schedule.catch_up
        if policy is CatchUpPolicy.SKIP:
            return

        tz = schedule.resolve_timezone()
        now = datetime.now(tz)
        limit = 1 if policy is CatchUpPolicy.ONCE else _MAX_BACKFILL_FIRES
        missed = self._missed_fires(schedule, last_fire_at, now, limit=limit)
        if not missed:
            return

        logger.info(
            "Scheduler '%s': %d missed fire(s) since %s — catch_up=%s",
            job.info.name,
            len(missed),
            last_fire_at.isoformat(),
            policy.value,
        )
        for _ in missed:
            await self._execute_job(job)
        await self._record_fire(job.info.name, now)

    @staticmethod
    def _missed_fires(
        schedule: Schedule,
        last_fire_at: datetime,
        now: datetime,
        *,
        limit: int,
    ) -> list[datetime]:
        """Occurrences between the last recorded fire and now.

        Capped at ``limit`` — a 10-second interval job and a week of
        downtime would otherwise enumerate 60,000 occurrences.
        """
        tz = schedule.resolve_timezone()
        out: list[datetime] = []
        cursor = last_fire_at.astimezone(tz)
        for _ in range(limit):
            nxt = schedule.parsed.next_after(cursor, tz, anchor=cursor)
            if nxt is None:
                break
            if nxt.astimezone(UTC) > now.astimezone(UTC):
                break
            out.append(nxt)
            cursor = nxt
        return out

    async def _execute_job(self, job: _Job) -> None:
        """Execute a single job invocation."""
        job.is_running = True
        job.info.state = JobState.RUNNING
        start = time.monotonic()

        try:
            await job.callback()
            job.info.last_error = ""
        except asyncio.CancelledError:
            job.is_running = False
            raise
        except Exception as e:
            job.info.last_error = str(e)
            logger.exception("Job '%s' failed", job.info.name)
            if job.info.schedule.is_one_shot:
                job.info.state = JobState.FAILED
                job.is_running = False
                return

        elapsed = time.monotonic() - start
        job.info.run_count += 1
        job.info.last_run = datetime.now(UTC).isoformat()
        job.info.last_duration_seconds = round(elapsed, 3)
        job.info.state = JobState.IDLE
        job.is_running = False

    @staticmethod
    def _next_fire_at(
        schedule: Schedule,
        last_fire_at: datetime | None,
        now: datetime,
    ) -> datetime | None:
        """The next fire instant, or ``None`` when the job is retired.

        One path for every schedule kind:

        ``expression -> start_at floor -> window gate -> end_at -> jitter``

        ``last_fire_at`` anchors the interval and one-shot kinds so that
        drift does not accumulate; field expressions are absolute and
        ignore it.
        """
        tz = schedule.resolve_timezone()
        parsed = schedule.parsed
        start_at = _as_aware(schedule.start_at, tz)
        end_at = _as_aware(schedule.end_at, tz)

        # A one-shot fires exactly once; having a recorded fire retires it.
        if parsed.is_one_shot and last_fire_at is not None:
            return None

        # First fire may be held back by start_at.
        if last_fire_at is None and start_at is not None and (
            start_at.astimezone(UTC) > now.astimezone(UTC)
        ):
            if parsed.kind is CronKind.FIELDS:
                candidate = parsed.next_after(
                    start_at - timedelta(microseconds=1), tz
                )
            else:
                # An interval or one-shot job simply begins at start_at.
                candidate = start_at
        else:
            anchor = last_fire_at.astimezone(tz) if last_fire_at else None
            candidate = parsed.next_after(now, tz, anchor=anchor)

        if candidate is None:
            return None

        # Daily window, applied as a filter over candidate fires.
        if (
            schedule.window_start_time is not None
            and schedule.window_end_time is not None
        ):
            for _ in range(_MAX_WINDOW_ADVANCES):
                clamped = _clamp_to_daily_window(
                    candidate,
                    schedule.window_start_time,
                    schedule.window_end_time,
                )
                if clamped == candidate:
                    break
                if parsed.kind is CronKind.FIELDS:
                    # Land on a real expression match at or after the
                    # window opens, rather than on the window edge.
                    nxt = parsed.next_after(
                        clamped - timedelta(microseconds=1), tz
                    )
                    if nxt is None:
                        return None
                    candidate = nxt
                else:
                    candidate = clamped
                    break
            else:
                logger.warning(
                    "Scheduler: window gating did not converge for %r",
                    schedule.expression,
                )
                return None

        if end_at is not None and (
            candidate.astimezone(UTC) > end_at.astimezone(UTC)
        ):
            return None

        if schedule.jitter_seconds > 0:
            candidate = candidate + timedelta(
                seconds=random.uniform(0, schedule.jitter_seconds)
            )

        return candidate

    # --- Fire history (drives catch-up) ---

    async def _record_fire(self, name: str, when: datetime) -> None:
        """Persist the last fire time. Best-effort — never blocks a fire.

        Kept in its own collection rather than on the ``scheduler_jobs``
        row because **system jobs are never persisted there**; without a
        separate store they could never catch up.
        """
        if self._storage is None:
            return
        try:
            await self._storage.put(
                _JOB_STATE_COLLECTION,
                name,
                {
                    "id": name,
                    "name": name,
                    "last_fire_at": when.astimezone(UTC).isoformat(),
                },
            )
        except Exception:
            logger.debug("Scheduler: could not record fire for '%s'", name)

    async def _load_last_fire_at(self, name: str) -> datetime | None:
        """Read the last recorded fire time, if any."""
        if self._storage is None:
            return None
        try:
            row = await self._storage.get(_JOB_STATE_COLLECTION, name)
        except Exception:
            return None
        if not row:
            return None
        raw = str(row.get("last_fire_at") or "")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

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

        # Clear history from any earlier job of the same name, so a
        # recycled name doesn't start life looking like it already fired.
        await self._clear_fire_history(name)

        now_iso = datetime.now(UTC).isoformat()
        record: dict[str, Any] = {
            "id": name,
            "name": name,
            "expression": schedule.expression,
            "timezone": schedule.timezone,
            "start_at": _format_optional_datetime(schedule.start_at),
            "end_at": _format_optional_datetime(schedule.end_at),
            "window_start_time": _format_optional_time(schedule.window_start_time),
            "window_end_time": _format_optional_time(schedule.window_end_time),
            "catch_up": schedule.catch_up.value,
            "overlap": schedule.overlap.value,
            "jitter_seconds": schedule.jitter_seconds,
            "owner": owner,
            "action": action.to_dict(),
            "created_at": now_iso,
        }
        # One-shot timers need a fire_at so we can drop them on startup
        # if they were scheduled to fire while Gilbert was down.
        if schedule.is_one_shot:
            fire_at = datetime.now(UTC) + timedelta(
                seconds=schedule.parsed.once_delay_seconds
            )
            record["fire_at"] = fire_at.isoformat()

        try:
            await self._storage.put(_JOBS_COLLECTION, name, record)
        except Exception:
            logger.exception("Scheduler: failed to persist job '%s'", name)

    async def _unpersist_job(self, name: str) -> None:
        """Best-effort delete of a persisted job record and its history.

        The fire-history row has to go too. Job identity here is the
        name, so leaving history behind means a later job that reuses
        the name inherits it — and for a one-shot that reads as "already
        fired", retiring it before it ever runs.
        """
        if self._storage is None:
            return
        try:
            await self._storage.delete(_JOBS_COLLECTION, name)
        except Exception:
            logger.debug("Scheduler: unpersist of '%s' failed (may not exist)", name)
        await self._clear_fire_history(name)

    async def _clear_fire_history(self, name: str) -> None:
        """Drop any recorded fire history for a job name."""
        if self._storage is None:
            return
        try:
            await self._storage.delete(_JOB_STATE_COLLECTION, name)
        except Exception:
            logger.debug(
                "Scheduler: clearing fire history for '%s' failed", name
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
                expression = str(row.get("expression") or "").strip()
                if not expression:
                    logger.warning(
                        "Scheduler: job %r has no cron expression — skipping. "
                        "It predates migration 0007; run migrations to "
                        "convert it.",
                        name,
                    )
                    continue
                schedule = Schedule(
                    expression=expression,
                    timezone=str(row.get("timezone") or ""),
                    start_at=_parse_optional_iso_datetime(row.get("start_at")),
                    end_at=_parse_optional_iso_datetime(row.get("end_at")),
                    window_start_time=_parse_optional_time(
                        row.get("window_start_time")
                    ),
                    window_end_time=_parse_optional_time(
                        row.get("window_end_time")
                    ),
                    catch_up=_parse_catch_up(row.get("catch_up")),
                    overlap=_parse_overlap(row.get("overlap")),
                    jitter_seconds=float(row.get("jitter_seconds", 0) or 0),
                )

                # Drop one-shot timers that should have already fired
                if schedule.is_one_shot:
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
                # end_at is naive by convention and belongs to the job's
                # OWN timezone, not the host's — comparing it against a
                # bare datetime.now() drops still-valid jobs whenever the
                # two zones differ. This mirrors _next_fire_at.
                end_at_aware = _as_aware(
                    schedule.end_at, schedule.resolve_timezone()
                )
                if (
                    end_at_aware is not None
                    and end_at_aware.astimezone(UTC) <= now
                ):
                    logger.info(
                        "Scheduler: dropping expired recurring job '%s' "
                        "(end_at=%s)",
                        name,
                        end_at_aware.isoformat(),
                    )
                    await self._unpersist_job(name)
                    dropped_expired += 1
                    continue

                action = ScheduledAction.from_dict(row.get("action"))
                owner = str(row.get("owner") or "")
                event_type = (
                    "timer.fired" if schedule.is_one_shot else "alarm.fired"
                )
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
                    "[cron='0 9 * * MON-FRI' | hour=... minute=... | "
                    "interval_seconds=...] [timezone=... catch_up=... "
                    "message=... tool=... ai_prompt=...]"
                ),
                description=(
                    "Set a recurring user alarm. Use type='cron' with a "
                    "full cron expression for anything non-trivial "
                    "(weekdays only, nth weekday of the month, "
                    "sub-minute rates); 'interval', 'daily' and 'hourly' "
                    "remain as shorthands. By default publishes an "
                    "'alarm.fired' "
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
                        description=(
                            "Schedule type: 'cron' (full cron expression, "
                            "most flexible), 'interval', 'daily', or "
                            "'hourly'."
                        ),
                        enum=["cron", "interval", "daily", "hourly"],
                    ),
                    ToolParameter(
                        name="cron",
                        type=ToolParameterType.STRING,
                        description=(
                            "Cron expression, used with type='cron'. "
                            "Supports 5-field POSIX ('30 3 * * *'), "
                            "6-field with seconds ('*/30 * * * * *'), "
                            "ranges/lists/steps, JAN-DEC and SUN-SAT "
                            "names, the Quartz day forms L / W / # "
                            "('0 0 * * FRI#3' = third Friday), macros "
                            "(@daily @hourly @weekly @monthly @yearly), "
                            "'@every 90s' for arbitrary intervals, and "
                            "'@reboot' / '@once+45s' for one-shots. When "
                            "both day-of-month and day-of-week are "
                            "restricted a day matches if EITHER matches."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="timezone",
                        type=ToolParameterType.STRING,
                        description=(
                            "IANA timezone the schedule is evaluated in "
                            "(e.g. 'America/Los_Angeles'). Defaults to the "
                            "host's local zone. DST transitions are "
                            "handled: a job at a time that does not exist "
                            "on the spring-forward day fires once at the "
                            "transition; an ambiguous fall-back time fires "
                            "only on its first occurrence."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="catch_up",
                        type=ToolParameterType.STRING,
                        description=(
                            "What to do about fires missed while Gilbert "
                            "was down: 'skip' (real cron behaviour), "
                            "'once' (one make-up fire on startup), or "
                            "'backfill' (replay every missed occurrence, "
                            "capped at 100). Defaults to 'once' for daily "
                            "and cron alarms, 'skip' for interval alarms."
                        ),
                        enum=["skip", "once", "backfill"],
                        required=False,
                    ),
                    ToolParameter(
                        name="overlap",
                        type=ToolParameterType.STRING,
                        description=(
                            "What to do when a fire comes due while the "
                            "previous one is still running: 'skip' "
                            "(default), 'queue', or 'concurrent'."
                        ),
                        enum=["skip", "queue", "concurrent"],
                        required=False,
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
                    "schedule": j.schedule.expression,
                    "description": j.schedule.describe(),
                    "timezone": j.schedule.timezone,
                    "next_run_at": j.next_run_at,
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
        if window_start is not None and alarm_type in ("daily", "hourly"):
            return json.dumps(
                {
                    "error": (
                        "window_start_time / window_end_time do not apply to "
                        "'daily' or 'hourly' alarms — they already carry a "
                        "time anchor. Use type='cron' or type='interval'."
                    )
                }
            )

        timezone = str(arguments.get("timezone") or "").strip()
        catch_up = _parse_catch_up(arguments.get("catch_up") or None)
        overlap = _parse_overlap(arguments.get("overlap") or None)

        try:
            if alarm_type == "cron":
                expression = str(arguments.get("cron") or "").strip()
                if not expression:
                    return json.dumps(
                        {"error": "type='cron' requires a 'cron' expression."}
                    )
                schedule = Schedule.cron(
                    expression,
                    timezone=timezone,
                    start_at=start_at,
                    end_at=end_at,
                    window_start_time=window_start,
                    window_end_time=window_end,
                    catch_up=catch_up,
                    overlap=overlap,
                )
            elif alarm_type == "interval":
                schedule = Schedule.every(
                    float(arguments.get("interval_seconds", 60)),
                    start_at=start_at,
                    end_at=end_at,
                    window_start_time=window_start,
                    window_end_time=window_end,
                    timezone=timezone,
                    catch_up=catch_up,
                    overlap=overlap,
                )
            elif alarm_type == "daily":
                schedule = Schedule.daily_at(
                    hour=int(arguments.get("hour", 0)),
                    minute=int(arguments.get("minute", 0)),
                    start_at=start_at,
                    end_at=end_at,
                    timezone=timezone,
                    overlap=overlap,
                )
            elif alarm_type == "hourly":
                schedule = Schedule.hourly_at(
                    minute=int(arguments.get("minute", 0)),
                    start_at=start_at,
                    end_at=end_at,
                    timezone=timezone,
                    overlap=overlap,
                )
            else:
                return json.dumps({"error": f"Unknown schedule type: {alarm_type}"})
        except CronParseError as e:
            return json.dumps({"error": f"Invalid cron expression: {e}"})

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
                "expression": schedule.expression,
                "description": schedule.describe(),
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
            "next_run_at": info.next_run_at,
            "schedule": {
                "expression": info.schedule.expression,
                "description": info.schedule.describe(),
                "timezone": info.schedule.timezone,
                "catch_up": info.schedule.catch_up.value,
                "overlap": info.schedule.overlap.value,
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
