"""Scheduler service — manages system and user timers/alarms."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.scheduler import (
    JobCallback,
    JobInfo,
    JobState,
    Schedule,
    ScheduleType,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


class _Job:
    """Internal tracked job with its asyncio task."""

    def __init__(
        self,
        name: str,
        schedule: Schedule,
        callback: JobCallback,
        system: bool = False,
        enabled: bool = True,
    ) -> None:
        self.info = JobInfo(
            name=name,
            schedule=schedule,
            system=system,
            enabled=enabled,
        )
        self.callback = callback
        self.task: asyncio.Task[None] | None = None


class SchedulerService(Service):
    """Manages recurring and one-shot timed tasks.

    System jobs are registered by other services (e.g., doorbell polling).
    User jobs can be created/managed via AI tools (timers, alarms).
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._storage: Any = None
        self._event_bus: Any = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="scheduler",
            capabilities=frozenset({"scheduler", "ai_tools"}),
            optional=frozenset({"entity_storage", "event_bus"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None:
            from gilbert.core.services.storage import StorageService

            if isinstance(storage_svc, StorageService):
                self._storage = storage_svc.backend

        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            from gilbert.core.services.event_bus import EventBusService

            if isinstance(event_bus_svc, EventBusService):
                self._event_bus = event_bus_svc.bus

        logger.info("Scheduler service started")

    async def stop(self) -> None:
        """Cancel all running job tasks."""
        for job in self._jobs.values():
            if job.task is not None:
                job.task.cancel()
        # Wait for all tasks to finish
        tasks = [j.task for j in self._jobs.values() if j.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        logger.info("Scheduler stopped — all jobs cancelled")

    # --- Job management ---

    def add_job(
        self,
        name: str,
        schedule: Schedule,
        callback: JobCallback,
        system: bool = False,
        enabled: bool = True,
    ) -> JobInfo:
        """Register a job. System jobs are not user-editable.

        The job starts running immediately if enabled.
        """
        if name in self._jobs:
            raise ValueError(f"Job '{name}' already registered")

        job = _Job(name=name, schedule=schedule, callback=callback, system=system, enabled=enabled)
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

    def remove_job(self, name: str) -> None:
        """Remove a job. System jobs cannot be removed."""
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(f"Job not found: {name}")
        if job.info.system:
            raise ValueError(f"Cannot remove system job: {name}")
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
        job.info.last_run = datetime.now(timezone.utc).isoformat()
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

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "scheduler"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="list_timers",
                description="List all active timers and alarms (both system and user).",
            ),
            ToolDefinition(
                name="set_timer",
                description="Set a user timer that fires after a delay. Publishes a 'timer.fired' event when done.",
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
                        description="Message to include when the timer fires.",
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="set_alarm",
                description="Set a recurring user alarm (e.g., every N seconds, daily at a time, hourly at a minute).",
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
                        description="Message to include when the alarm fires.",
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="cancel_timer",
                description="Cancel a user timer or alarm by name.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Name of the timer/alarm to cancel.",
                    ),
                ],
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
                "state": j.state.value,
                "enabled": j.enabled,
                "run_count": j.run_count,
                "last_run": j.last_run,
                "last_error": j.last_error,
            }
            for j in jobs
        ])

    async def _tool_set_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        seconds = float(arguments["seconds"])
        message = arguments.get("message", "")

        async def _fire() -> None:
            if self._event_bus:
                from gilbert.interfaces.events import Event
                await self._event_bus.publish(Event(
                    event_type="timer.fired",
                    data={"name": timer_name, "message": message},
                    source="scheduler",
                ))
            logger.info("Timer '%s' fired: %s", timer_name, message or "(no message)")

        try:
            self.add_job(
                name=timer_name,
                schedule=Schedule.once_after(seconds),
                callback=_fire,
                system=False,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})

        return json.dumps({"status": "set", "name": timer_name, "seconds": seconds})

    async def _tool_set_alarm(self, arguments: dict[str, Any]) -> str:
        alarm_name = arguments["name"]
        alarm_type = arguments["type"]
        message = arguments.get("message", "")

        if alarm_type == "interval":
            sched = Schedule.every(float(arguments.get("interval_seconds", 60)))
        elif alarm_type == "daily":
            sched = Schedule.daily_at(
                hour=int(arguments.get("hour", 0)),
                minute=int(arguments.get("minute", 0)),
            )
        elif alarm_type == "hourly":
            sched = Schedule.hourly_at(minute=int(arguments.get("minute", 0)))
        else:
            return json.dumps({"error": f"Unknown schedule type: {alarm_type}"})

        async def _fire() -> None:
            if self._event_bus:
                from gilbert.interfaces.events import Event
                await self._event_bus.publish(Event(
                    event_type="alarm.fired",
                    data={"name": alarm_name, "message": message},
                    source="scheduler",
                ))
            logger.info("Alarm '%s' fired: %s", alarm_name, message or "(no message)")

        try:
            self.add_job(
                name=alarm_name,
                schedule=sched,
                callback=_fire,
                system=False,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})

        return json.dumps({"status": "set", "name": alarm_name, "type": alarm_type})

    def _tool_cancel_timer(self, arguments: dict[str, Any]) -> str:
        timer_name = arguments["name"]
        try:
            self.remove_job(timer_name)
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"status": "cancelled", "name": timer_name})
