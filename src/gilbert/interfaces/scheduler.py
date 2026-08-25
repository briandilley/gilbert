"""Scheduler interface — recurring and one-shot timed tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, tzinfo
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.interfaces.cron import CronExpression
from gilbert.interfaces.cron import parse as cron_parse


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


class CatchUpPolicy(StrEnum):
    """What to do about fires that were missed while Gilbert was down."""

    #: Real cron behaviour — the missed fire is simply lost.
    SKIP = "skip"
    #: Fire once on startup to make up for the gap, then resume the
    #: normal schedule. Four missed hourly fires produce ONE catch-up
    #: run, not four.
    ONCE = "once"
    #: Replay every missed occurrence in order. Correct for
    #: accounting-style jobs, dangerous for anything that announces or
    #: notifies.
    BACKFILL = "backfill"


class OverlapPolicy(StrEnum):
    """What to do when a fire is due while the previous one still runs."""

    #: Drop the new fire. The default: a slow callback should not
    #: accumulate a backlog of concurrent copies of itself.
    SKIP = "skip"
    #: Wait for the in-flight fire to finish, then run.
    QUEUE = "queue"
    #: Run anyway, concurrently.
    CONCURRENT = "concurrent"


@dataclass
class Schedule:
    """When a job runs, expressed as a single cron expression.

    Every schedule in Gilbert — sub-minute polling, one-shot startup
    work, and genuine calendar recurrences alike — is one expression in
    the dialect documented in :mod:`gilbert.interfaces.cron`. There is
    one evaluation engine, so a fix to DST handling or drift correction
    lands for every job at once.

    The four legacy constructors (:meth:`every`, :meth:`daily_at`,
    :meth:`hourly_at`, :meth:`once_after`) are retained as compilers to
    that dialect, so existing call sites are unaffected.

    Four optional bounds layer on top of the expression:

    - ``start_at`` / ``end_at`` — absolute datetimes that delay the
      first fire and retire the job after a deadline.
    - ``window_start_time`` / ``window_end_time`` — a time-of-day window
      that recurs daily. Applied as a *filter* on candidate fires rather
      than as a generator, so it composes with every expression kind —
      including ``@every``, whose sub-minute rates cron fields cannot
      express. Overnight windows (end before start) are not supported.
    """

    #: The cron expression. See :mod:`gilbert.interfaces.cron`.
    expression: str
    #: IANA timezone name (``America/Los_Angeles``). Empty = host local.
    timezone: str = ""
    #: First fire cannot happen before this time.
    start_at: datetime | None = None
    #: Job retires to ``DONE`` once the next fire would land after this.
    end_at: datetime | None = None
    #: Start of a daily time-of-day window gating fires.
    window_start_time: time | None = None
    #: End of the daily window. Must be after ``window_start_time``.
    window_end_time: time | None = None
    #: How to treat fires missed while the process was down.
    catch_up: CatchUpPolicy = CatchUpPolicy.SKIP
    #: How to treat a fire that comes due while one is still running.
    overlap: OverlapPolicy = OverlapPolicy.SKIP
    #: Random spread applied to each fire, to avoid thundering herds
    #: when many jobs share a schedule.
    jitter_seconds: float = 0.0

    def __post_init__(self) -> None:
        # Parse once, at construction, so an invalid expression fails
        # where it is written rather than at 3am on the first fire.
        self._parsed: CronExpression = cron_parse(self.expression)

    @property
    def parsed(self) -> CronExpression:
        """The parsed expression. Cached at construction."""
        return self._parsed

    @property
    def is_one_shot(self) -> bool:
        """True for ``@once``/``@reboot`` schedules."""
        return self._parsed.is_one_shot

    def resolve_timezone(self) -> tzinfo:
        """The job's timezone, falling back to the host's local zone.

        An unknown zone name degrades to host-local rather than raising,
        matching how the rest of the codebase treats bad tz config —
        a typo should not take the scheduler down.
        """
        if not self.timezone:
            return datetime.now().astimezone().tzinfo or UTC
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return datetime.now().astimezone().tzinfo or UTC

    def describe(self) -> str:
        """Human-readable summary, including any bounds."""
        base = self._parsed.describe()
        if self.timezone:
            base = f"{base} ({self.timezone})"
        return base

    # --- Constructors -------------------------------------------------

    @classmethod
    def every(
        cls,
        seconds: float,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        window_start_time: time | None = None,
        window_end_time: time | None = None,
        timezone: str = "",
        catch_up: CatchUpPolicy = CatchUpPolicy.SKIP,
        overlap: OverlapPolicy = OverlapPolicy.SKIP,
    ) -> Schedule:
        """Run every N seconds. Compiles to ``@every <N>s``.

        Catch-up defaults to ``SKIP``: a high-frequency poll tick has
        nothing meaningful to make up for after downtime.
        """
        return cls(
            expression=f"@every {_trim_seconds(seconds)}s",
            timezone=timezone,
            start_at=start_at,
            end_at=end_at,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            catch_up=catch_up,
            overlap=overlap,
        )

    @classmethod
    def daily_at(
        cls,
        hour: int,
        minute: int = 0,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        timezone: str = "",
        catch_up: CatchUpPolicy = CatchUpPolicy.ONCE,
        overlap: OverlapPolicy = OverlapPolicy.SKIP,
    ) -> Schedule:
        """Run daily at a specific time. Compiles to ``<M> <H> * * *``.

        Catch-up defaults to ``ONCE`` — a missed daily digest is worth
        one make-up run on the next startup.
        """
        return cls(
            expression=f"{int(minute)} {int(hour)} * * *",
            timezone=timezone,
            start_at=start_at,
            end_at=end_at,
            catch_up=catch_up,
            overlap=overlap,
        )

    @classmethod
    def hourly_at(
        cls,
        minute: int = 0,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        timezone: str = "",
        catch_up: CatchUpPolicy = CatchUpPolicy.ONCE,
        overlap: OverlapPolicy = OverlapPolicy.SKIP,
    ) -> Schedule:
        """Run hourly at a specific minute. Compiles to ``<M> * * * *``."""
        return cls(
            expression=f"{int(minute)} * * * *",
            timezone=timezone,
            start_at=start_at,
            end_at=end_at,
            catch_up=catch_up,
            overlap=overlap,
        )

    @classmethod
    def once_after(cls, seconds: float) -> Schedule:
        """Run once after a delay. Compiles to ``@once+<N>s``.

        Bounds don't apply — the delay is the whole schedule.
        """
        return cls(expression=f"@once+{_trim_seconds(seconds)}s")

    @classmethod
    def cron(
        cls,
        expression: str,
        *,
        timezone: str = "",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        window_start_time: time | None = None,
        window_end_time: time | None = None,
        catch_up: CatchUpPolicy = CatchUpPolicy.ONCE,
        overlap: OverlapPolicy = OverlapPolicy.SKIP,
        jitter_seconds: float = 0.0,
    ) -> Schedule:
        """Run on an arbitrary cron expression.

        Raises :class:`~gilbert.interfaces.cron.CronParseError` if the
        expression is invalid.
        """
        return cls(
            expression=expression,
            timezone=timezone,
            start_at=start_at,
            end_at=end_at,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            catch_up=catch_up,
            overlap=overlap,
            jitter_seconds=jitter_seconds,
        )


def _trim_seconds(seconds: float) -> str:
    """Render a seconds value without a pointless trailing ``.0``."""
    return str(int(seconds)) if float(seconds).is_integer() else str(seconds)


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
    #: ISO-8601 timestamp of the next scheduled fire, or "" when the job
    #: is retired/disabled. Recomputed each loop iteration so the UI can
    #: answer "when does this actually run next?".
    next_run_at: str = ""
    #: Human-readable schedule summary, derived from the expression.
    description: str = ""
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

    def remove_job(
        self, name: str, requester_id: str = "", *, force: bool = False
    ) -> None:
        """Remove a job.

        ``force=True`` bypasses the system-job protection so the
        service that owns a system job can replace it (e.g. heartbeat
        re-arm). External callers never pass ``force``.
        """
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
