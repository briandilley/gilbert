"""Scheduler interface — recurring and one-shot timed tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


class ScheduledActionType(StrEnum):
    """What a scheduled job does when it fires."""

    #: Publish an ``alarm.fired`` / ``timer.fired`` event carrying
    #: ``{name, message}`` — the legacy pub/sub behavior. This is the
    #: default when no ``tool`` or ``ai_prompt`` is provided.
    EVENT = "event"

    #: Directly invoke a named tool with a fully-specified argument dict.
    #: Deterministic and cheap — no AI roundtrip at fire time. Use for
    #: high-frequency or well-defined actions.
    TOOL = "tool"

    #: Run an ``ai_prompt`` through the AI service with full tool access.
    #: Flexible but rate-limited globally to prevent runaway cost on
    #: frequent alarms. Use for complex, conditional, or natural-language
    #: instructions that a structured tool call can't express.
    AI_PROMPT = "ai_prompt"


@dataclass
class ScheduledAction:
    """Describes what a timer or alarm does when it fires.

    Exactly one of (``tool`` + ``tool_arguments``) or ``ai_prompt`` may
    be set. If neither is set, the job falls back to publishing a
    ``timer.fired`` / ``alarm.fired`` event carrying ``message``.
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for persistence."""
        return {
            "type": self.type.value,
            "tool": self.tool,
            "tool_arguments": dict(self.tool_arguments),
            "ai_prompt": self.ai_prompt,
            "message": self.message,
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
        return cls(
            type=action_type,
            tool=str(data.get("tool") or ""),
            tool_arguments=dict(data.get("tool_arguments") or {}),
            ai_prompt=str(data.get("ai_prompt") or ""),
            message=str(data.get("message") or ""),
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
    """Describes when and how often a job runs."""

    type: ScheduleType
    interval_seconds: float = 0
    hour: int = 0
    minute: int = 0

    @classmethod
    def every(cls, seconds: float) -> Schedule:
        """Run every N seconds."""
        return cls(type=ScheduleType.INTERVAL, interval_seconds=seconds)

    @classmethod
    def daily_at(cls, hour: int, minute: int = 0) -> Schedule:
        """Run daily at a specific time."""
        return cls(type=ScheduleType.DAILY, hour=hour, minute=minute)

    @classmethod
    def hourly_at(cls, minute: int = 0) -> Schedule:
        """Run hourly at a specific minute."""
        return cls(type=ScheduleType.HOURLY, minute=minute)

    @classmethod
    def once_after(cls, seconds: float) -> Schedule:
        """Run once after a delay."""
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
