"""Scheduler interface — recurring and one-shot timed tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ScheduledActionType(StrEnum):
    """What a scheduled job does when it fires."""

    #: Publish an ``alarm.fired`` / ``timer.fired`` event carrying
    #: ``{name, message}`` — the legacy pub/sub behavior. This is the
    #: default when no ``tool``/``ai_prompt``/``steps`` is provided.
    EVENT = "event"

    #: Directly invoke a named tool with a fully-specified argument dict.
    #: Deterministic and cheap — no AI roundtrip at fire time. Use for
    #: high-frequency or well-defined single-tool actions.
    TOOL = "tool"

    #: Run an ``ai_prompt`` through the AI service with full tool access.
    #: Flexible but rate-limited globally to prevent runaway cost on
    #: frequent alarms. Use for complex, conditional, or natural-language
    #: instructions that a structured tool call can't express.
    AI_PROMPT = "ai_prompt"

    #: Invoke an ordered sequence of tool calls, optionally with
    #: per-step delays. Each step is a deterministic tool call — no AI
    #: cost per fire. Use for chained actions like "play music, wait 5
    #: seconds, then announce a message."
    SEQUENCE = "sequence"


@dataclass
class ActionStep:
    """A single tool invocation inside a ``SEQUENCE`` action.

    Steps run in order. Before each step, the dispatcher awaits
    ``delay_before_seconds`` seconds (if positive) — this is how you
    express "play music, wait 5 seconds, then announce". Step failures
    are logged but do not abort the remaining steps, so a recurring
    sequence can self-heal the same way single-tool actions do.
    """

    #: Name of the tool to invoke.
    tool: str
    #: Argument dict passed to the tool's ``execute_tool()``.
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    #: Seconds to await before running this step. Runs after the
    #: previous step returns (or after the fire starts, for the first
    #: step). Use to sequence "start music" → wait → "stop music".
    delay_before_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "tool_arguments": dict(self.tool_arguments),
            "delay_before_seconds": float(self.delay_before_seconds),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ActionStep:
        if not data:
            return cls(tool="")
        try:
            delay = float(data.get("delay_before_seconds") or 0)
        except (TypeError, ValueError):
            delay = 0.0
        return cls(
            tool=str(data.get("tool") or ""),
            tool_arguments=dict(data.get("tool_arguments") or {}),
            delay_before_seconds=max(0.0, delay),
        )


@dataclass
class ScheduledAction:
    """Describes what a timer or alarm does when it fires.

    Exactly one of (``tool`` + ``tool_arguments``), ``ai_prompt``, or
    ``steps`` may be set. If none is set, the job falls back to
    publishing a ``timer.fired`` / ``alarm.fired`` event carrying
    ``message``.
    """

    type: ScheduledActionType = ScheduledActionType.EVENT
    #: Name of the tool to invoke when ``type == TOOL``.
    tool: str = ""
    #: Argument dict passed to the tool's ``execute_tool()``.
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    #: Free-form instruction fed to the AI service when ``type == AI_PROMPT``.
    ai_prompt: str = ""
    #: Human-readable message published on event fires. Also included
    #: in tool/AI dispatch logs for debugging.
    message: str = ""
    #: Ordered sequence of tool calls when ``type == SEQUENCE``. Each
    #: step runs after the previous one returns, with an optional
    #: ``delay_before_seconds`` wait inserted before the step.
    steps: list[ActionStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for persistence."""
        return {
            "type": self.type.value,
            "tool": self.tool,
            "tool_arguments": dict(self.tool_arguments),
            "ai_prompt": self.ai_prompt,
            "message": self.message,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ScheduledAction:
        """Deserialize from a persisted dict."""
        if not data:
            return cls()
        try:
            action_type = ScheduledActionType(data.get("type") or "event")
        except ValueError:
            action_type = ScheduledActionType.EVENT
        raw_steps = data.get("steps") or []
        steps: list[ActionStep] = []
        if isinstance(raw_steps, list):
            for raw in raw_steps:
                if isinstance(raw, dict):
                    steps.append(ActionStep.from_dict(raw))
        return cls(
            type=action_type,
            tool=str(data.get("tool") or ""),
            tool_arguments=dict(data.get("tool_arguments") or {}),
            ai_prompt=str(data.get("ai_prompt") or ""),
            message=str(data.get("message") or ""),
            steps=steps,
        )


class JobState(StrEnum):
    """Lifecycle state of a scheduled job."""

    PENDING = "pending"
    RUNNING = "running"
    IDLE = "idle"
    DONE = "done"
    FAILED = "failed"


class ScheduleType(StrEnum):
    """How a job is scheduled."""

    INTERVAL = "interval"
    DAILY = "daily"
    HOURLY = "hourly"
    ONCE = "once"


@dataclass
class Schedule:
    """Describes when and how often a job runs.

    Beyond the base schedule (``type`` + rate fields), four optional
    bounds can be layered on:

    - ``start_at`` / ``end_at`` — absolute naive-local datetimes that
      delay the first fire and retire the job after a deadline. A job
      won't fire before ``start_at``; once ``end_at`` is past, the
      loop transitions to ``DONE``. Useful for "every minute starting
      at 1am" or "every minute from 1am to 2am today only".
    - ``window_start_time`` / ``window_end_time`` — a time-of-day
      window that recurs every day. Fires only happen inside the
      window; outside, the loop sleeps until the next window start.
      Useful for "every minute from 1am to 2am every day". Windows
      apply only to INTERVAL jobs — DAILY/HOURLY already carry their
      own time anchor, and ONCE is a single shot.

    All four are orthogonal: e.g. window=01:00-02:00 plus
    ``end_at=next Sunday`` gives "every minute between 1am and 2am
    daily until Sunday." Overnight windows (end < start) are not
    supported — keep window_end after window_start on the same day.
    """

    type: ScheduleType
    interval_seconds: float = 0
    hour: int = 0
    minute: int = 0
    #: First fire cannot happen before this time. ``None`` = no lower
    #: bound (first fire is scheduled per the type's normal rules).
    start_at: datetime | None = None
    #: Job retires to ``DONE`` once the next computed fire would land
    #: after this time. ``None`` = no deadline.
    end_at: datetime | None = None
    #: Start of a daily time-of-day window for INTERVAL jobs. If set,
    #: ``window_end_time`` must also be set. Ignored for
    #: DAILY/HOURLY/ONCE.
    window_start_time: time | None = None
    #: End of the daily window. Must be on the same clock day as
    #: ``window_start_time`` (i.e. strictly after it).
    window_end_time: time | None = None

    @classmethod
    def every(
        cls,
        seconds: float,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        window_start_time: time | None = None,
        window_end_time: time | None = None,
    ) -> Schedule:
        """Run every N seconds, optionally bounded by start/end/window."""
        return cls(
            type=ScheduleType.INTERVAL,
            interval_seconds=seconds,
            start_at=start_at,
            end_at=end_at,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
        )

    @classmethod
    def daily_at(
        cls,
        hour: int,
        minute: int = 0,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> Schedule:
        """Run daily at a specific time, optionally bounded by start/end."""
        return cls(
            type=ScheduleType.DAILY,
            hour=hour,
            minute=minute,
            start_at=start_at,
            end_at=end_at,
        )

    @classmethod
    def hourly_at(
        cls,
        minute: int = 0,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> Schedule:
        """Run hourly at a specific minute, optionally bounded."""
        return cls(
            type=ScheduleType.HOURLY,
            minute=minute,
            start_at=start_at,
            end_at=end_at,
        )

    @classmethod
    def once_after(cls, seconds: float) -> Schedule:
        """Run once after a delay. Bounds don't apply — the delay is the schedule."""
        return cls(type=ScheduleType.ONCE, interval_seconds=seconds)


@dataclass
class JobInfo:
    """Runtime info about a scheduled job."""

    name: str
    schedule: Schedule
    state: JobState = JobState.PENDING
    system: bool = False
    owner: str = ""  # user_id of creator (empty for system jobs)
    enabled: bool = True
    run_count: int = 0
    last_run: str = ""
    last_duration_seconds: float = 0.0
    last_error: str = ""
    #: What the job does when it fires. Default is a pure event
    #: publication for backward compatibility with existing alarms.
    action: ScheduledAction = field(default_factory=ScheduledAction)


# Callback type for scheduled jobs
JobCallback = Callable[[], Awaitable[Any]]


@runtime_checkable
class SchedulerProvider(Protocol):
    """Protocol for scheduling and managing timed jobs.

    Services resolve this via ``get_capability("scheduler")`` to register
    jobs without depending on the concrete SchedulerService.
    """

    def add_job(
        self,
        name: str,
        schedule: Schedule,
        callback: JobCallback,
        system: bool = False,
        enabled: bool = True,
        owner: str = "",
    ) -> JobInfo:
        """Register a job. System jobs are not user-editable."""
        ...

    def remove_job(self, name: str, requester_id: str = "") -> None:
        """Remove a job."""
        ...

    def enable_job(self, name: str) -> None:
        """Enable a disabled job."""
        ...

    def disable_job(self, name: str) -> None:
        """Disable a running job."""
        ...

    def list_jobs(self, include_system: bool = True) -> list[JobInfo]:
        """List all registered jobs."""
        ...

    def get_job(self, name: str) -> JobInfo | None:
        """Get info about a specific job."""
        ...

    async def run_now(self, name: str) -> None:
        """Execute a job immediately, outside its schedule."""
        ...
