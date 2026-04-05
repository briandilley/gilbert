"""Scheduler interface — recurring and one-shot timed tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable


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
    enabled: bool = True
    run_count: int = 0
    last_run: str = ""
    last_duration_seconds: float = 0.0
    last_error: str = ""


# Callback type for scheduled jobs
JobCallback = Callable[[], Awaitable[Any]]
