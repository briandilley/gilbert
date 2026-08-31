"""Tests for SchedulerService — job lifecycle, timers, alarms."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.services.scheduler import (
    SchedulerService,
    _AICallRateLimiter,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.scheduler import (
    ActionStep,
    CatchUpPolicy,
    JobState,
    OverlapPolicy,
    Schedule,
    ScheduledAction,
    ScheduledActionType,
)
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType


@pytest.fixture
def resolver() -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)
    mock.get_capability.return_value = None
    return mock


@pytest.fixture
async def service(resolver: ServiceResolver) -> SchedulerService:
    svc = SchedulerService()
    await svc.start(resolver)
    yield svc  # type: ignore[misc]
    await svc.stop()


# --- Schedule factories ---


def test_schedule_every_compiles_to_an_interval_expression() -> None:
    s = Schedule.every(30)
    assert s.expression == "@every 30s"
    assert s.parsed.every_seconds == 30


def test_schedule_daily_compiles_to_a_cron_expression() -> None:
    s = Schedule.daily_at(8, 30)
    assert s.expression == "30 8 * * *"
    assert s.describe() == "daily at 08:30"


def test_schedule_hourly_compiles_to_a_cron_expression() -> None:
    assert Schedule.hourly_at(15).expression == "15 * * * *"


def test_schedule_once_compiles_to_a_one_shot_expression() -> None:
    s = Schedule.once_after(10)
    assert s.expression == "@once+10s"
    assert s.is_one_shot


def test_schedule_cron_accepts_a_raw_expression() -> None:
    s = Schedule.cron("0 9 * * MON-FRI", timezone="America/Los_Angeles")
    assert s.expression == "0 9 * * MON-FRI"
    assert s.timezone == "America/Los_Angeles"


def test_schedule_rejects_an_invalid_expression_at_construction() -> None:
    """A typo must fail where it is written, not at 3am on first fire."""
    from gilbert.interfaces.cron import CronParseError

    with pytest.raises(CronParseError):
        Schedule.cron("not a cron expression")


def test_catch_up_defaults_differ_by_constructor() -> None:
    """A missed daily digest earns a make-up run; a poll tick does not."""
    assert Schedule.daily_at(3).catch_up is CatchUpPolicy.ONCE
    assert Schedule.hourly_at(0).catch_up is CatchUpPolicy.ONCE
    assert Schedule.every(10).catch_up is CatchUpPolicy.SKIP
    assert Schedule.once_after(5).catch_up is CatchUpPolicy.SKIP


def test_overlap_defaults_to_skip() -> None:
    assert Schedule.every(10).overlap is OverlapPolicy.SKIP


def test_unknown_timezone_degrades_to_host_local() -> None:
    """A typo in a tz name must not take the scheduler down."""
    schedule = Schedule.cron("0 3 * * *", timezone="Mars/Olympus_Mons")
    assert schedule.resolve_timezone() is not None


# --- Schedule bounds & windows ---

from datetime import UTC, datetime, timedelta  # noqa: E402
from datetime import time as dtime  # noqa: E402

from gilbert.core.services.scheduler import (  # noqa: E402
    _clamp_to_daily_window,
    _parse_optional_iso_datetime,
    _parse_optional_time,
)


def _now_for(schedule: Schedule) -> datetime:
    """The current instant in the schedule's own timezone."""
    return datetime.now(schedule.resolve_timezone())


def _seconds_until(target: datetime, now: datetime) -> float:
    """Real elapsed seconds — via UTC, since a shared tzinfo compares
    as wall clock and would misread a DST boundary."""
    return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


def test_next_fire_waits_for_start_at_on_first_fire() -> None:
    """An interval job with a future start_at fires at start_at, not
    ``interval_seconds`` after registration. The "every minute starting
    at 1am" case."""
    schedule = Schedule.every(60)
    now = _now_for(schedule)
    schedule = Schedule.every(
        60, start_at=(now + timedelta(minutes=10)).replace(tzinfo=None)
    )
    nxt = SchedulerService._next_fire_at(schedule, None, now)
    assert nxt is not None
    assert 9 * 60 < _seconds_until(nxt, now) <= 10 * 60 + 5


def test_next_fire_does_not_wait_when_start_at_is_past() -> None:
    """A past start_at must not delay the first fire — the window has
    opened, so the normal cadence applies immediately."""
    schedule = Schedule.every(60)
    now = _now_for(schedule)
    schedule = Schedule.every(
        60, start_at=(now - timedelta(hours=1)).replace(tzinfo=None)
    )
    nxt = SchedulerService._next_fire_at(schedule, None, now)
    assert nxt is not None
    assert _seconds_until(nxt, now) <= 60.0


def test_next_fire_retires_job_past_end_at() -> None:
    """Once end_at is past, the next fire is None so the loop reaches
    DONE. Without this a "for today only" alarm ticks tomorrow."""
    schedule = Schedule.every(60)
    now = _now_for(schedule)
    schedule = Schedule.every(
        60, end_at=(now - timedelta(seconds=1)).replace(tzinfo=None)
    )
    assert SchedulerService._next_fire_at(schedule, None, now) is None


def test_next_fire_clamps_to_daily_window_when_outside() -> None:
    """Outside the window the next fire is pushed to the next window
    start. The "every minute from 1am to 2am daily" case: at noon the
    next fire is 1am tomorrow, not noon+60s."""
    schedule = Schedule.every(
        60,
        window_start_time=dtime(0, 0),
        window_end_time=dtime(0, 1),
    )
    now = _now_for(schedule)
    nxt = SchedulerService._next_fire_at(schedule, now, now)
    assert nxt is not None
    if not (now.hour == 0 and now.minute == 0):
        assert _seconds_until(nxt, now) > 60.0


def test_next_fire_honors_window_when_inside() -> None:
    """Inside the window the interval applies normally — an all-day
    window behaves exactly like no window at all."""
    schedule = Schedule.every(
        60,
        window_start_time=dtime(0, 0),
        window_end_time=dtime(23, 59, 59),
    )
    now = _now_for(schedule)
    last_fire = now - timedelta(seconds=10)
    nxt = SchedulerService._next_fire_at(schedule, last_fire, now)
    assert nxt is not None
    assert 45 <= _seconds_until(nxt, now) <= 60


def test_next_fire_once_retires_after_firing() -> None:
    """A one-shot yields its delay first, then None so the loop exits."""
    schedule = Schedule.once_after(30)
    now = _now_for(schedule)
    first = SchedulerService._next_fire_at(schedule, None, now)
    assert first is not None
    assert _seconds_until(first, now) == pytest.approx(30, abs=1)

    assert SchedulerService._next_fire_at(schedule, now, now) is None


def test_next_fire_cron_expression_lands_on_the_expression() -> None:
    schedule = Schedule.cron("0 3 * * *")
    now = _now_for(schedule)
    nxt = SchedulerService._next_fire_at(schedule, None, now)
    assert nxt is not None
    assert (nxt.hour, nxt.minute) == (3, 0)


def test_next_fire_applies_jitter_within_bounds() -> None:
    schedule = Schedule.cron("0 3 * * *", jitter_seconds=30)
    now = _now_for(schedule)
    plain = Schedule.cron("0 3 * * *")
    base = SchedulerService._next_fire_at(plain, None, now)
    jittered = SchedulerService._next_fire_at(schedule, None, now)
    assert base is not None and jittered is not None
    offset = _seconds_until(jittered, base)
    assert 0 <= offset <= 30


def test_clamp_to_daily_window_before_start_jumps_to_today() -> None:
    """Before the window's start-of-day, the next valid fire is today
    at the window start — not the previous day's start."""
    candidate = datetime(2026, 6, 1, 12, 30)  # noon
    result = _clamp_to_daily_window(
        candidate, dtime(13, 0), dtime(14, 0)
    )
    assert result == datetime(2026, 6, 1, 13, 0)


def test_clamp_to_daily_window_past_end_jumps_to_tomorrow() -> None:
    """After the window's end, the next valid fire is TOMORROW at the
    window start — preserves the daily-recurrence semantic."""
    candidate = datetime(2026, 6, 1, 15, 0)  # 3pm
    result = _clamp_to_daily_window(
        candidate, dtime(13, 0), dtime(14, 0)
    )
    assert result == datetime(2026, 6, 2, 13, 0)


def test_clamp_to_daily_window_inside_passes_through() -> None:
    """Inside the window, the candidate is returned unchanged — no
    artificial drift."""
    candidate = datetime(2026, 6, 1, 13, 30)
    result = _clamp_to_daily_window(
        candidate, dtime(13, 0), dtime(14, 0)
    )
    assert result == candidate


def test_parse_optional_iso_datetime_roundtrip() -> None:
    """Empty / None / malformed → None; valid ISO roundtrips as a
    naive datetime. Timezone-aware input is converted to local-naive
    because the scheduler does naive-local time arithmetic throughout."""
    assert _parse_optional_iso_datetime(None) is None
    assert _parse_optional_iso_datetime("") is None
    assert _parse_optional_iso_datetime("not a date") is None

    parsed = _parse_optional_iso_datetime("2026-04-19T01:30:00")
    assert parsed == datetime(2026, 4, 19, 1, 30, 0)
    assert parsed.tzinfo is None


def test_parse_optional_time_roundtrip() -> None:
    assert _parse_optional_time(None) is None
    assert _parse_optional_time("") is None
    assert _parse_optional_time("bogus") is None
    assert _parse_optional_time("01:00") == dtime(1, 0)
    assert _parse_optional_time("01:00:30") == dtime(1, 0, 30)


# --- Job management ---


async def test_add_job(service: SchedulerService) -> None:
    callback = AsyncMock()
    info = service.add_job("test-job", Schedule.every(60), callback, system=True)
    assert info.name == "test-job"
    assert info.system is True
    assert info.enabled is True


async def test_add_duplicate_raises(service: SchedulerService) -> None:
    service.add_job("dup", Schedule.every(60), AsyncMock())
    with pytest.raises(ValueError, match="already registered"):
        service.add_job("dup", Schedule.every(60), AsyncMock())


async def test_remove_user_job(service: SchedulerService) -> None:
    service.add_job("removable", Schedule.every(60), AsyncMock(), system=False)
    service.remove_job("removable")
    assert service.get_job("removable") is None


async def test_remove_system_job_raises(service: SchedulerService) -> None:
    service.add_job("sys", Schedule.every(60), AsyncMock(), system=True)
    with pytest.raises(ValueError, match="Cannot remove system job"):
        service.remove_job("sys")


async def test_remove_system_job_with_force_succeeds(
    service: SchedulerService,
) -> None:
    """``force=True`` lets the owning service replace its own system job
    (e.g. AgentService re-arming an agent heartbeat after a config change).
    External callers never pass ``force``.
    """
    service.add_job("sys", Schedule.every(60), AsyncMock(), system=True)
    service.remove_job("sys", force=True)
    assert service.get_job("sys") is None
    # Re-adding under the same name now works — this is the re-arm path.
    service.add_job("sys", Schedule.every(120), AsyncMock(), system=True)


async def test_list_jobs(service: SchedulerService) -> None:
    service.add_job("j1", Schedule.every(60), AsyncMock(), system=True)
    service.add_job("j2", Schedule.every(60), AsyncMock(), system=False)
    all_jobs = service.list_jobs()
    assert len(all_jobs) == 2
    user_jobs = service.list_jobs(include_system=False)
    assert len(user_jobs) == 1
    assert user_jobs[0].name == "j2"


async def test_disable_enable_job(service: SchedulerService) -> None:
    service.add_job("toggle", Schedule.every(60), AsyncMock())
    service.disable_job("toggle")
    assert service.get_job("toggle").enabled is False  # type: ignore[union-attr]
    service.enable_job("toggle")
    assert service.get_job("toggle").enabled is True  # type: ignore[union-attr]


# --- Job execution ---


async def test_run_now(service: SchedulerService) -> None:
    callback = AsyncMock()
    service.add_job("manual", Schedule.every(9999), callback, enabled=False)
    await service.run_now("manual")
    callback.assert_awaited_once()


async def test_zero_delay_one_shot_fires() -> None:
    """``Schedule.once_after(0)`` — every service's boot job — must run.

    Regression: the cron engine treated a zero delay as "already
    past" and retired the job without ever firing it, silently
    stranding inbox/tasks/calendar/feeds boot work.
    """
    fired = asyncio.Event()

    async def _cb() -> None:
        fired.set()

    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)

    svc.add_job("boot", Schedule.once_after(0), _cb, system=True)
    await asyncio.wait_for(fired.wait(), timeout=2.0)


async def test_one_shot_timer_fires() -> None:
    """A once-after timer should execute and reach DONE state."""
    fired = asyncio.Event()

    async def _cb() -> None:
        fired.set()

    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)

    svc.add_job("quick", Schedule.once_after(0.05), _cb)
    await asyncio.wait_for(fired.wait(), timeout=2.0)

    info = svc.get_job("quick")
    # Give the loop a moment to update state
    await asyncio.sleep(0.1)
    info = svc.get_job("quick")
    assert info is not None
    assert info.state == JobState.DONE
    assert info.run_count == 1
    await svc.stop()


async def test_removed_job_with_cancel_swallowing_callback_stops_firing() -> None:
    """Regression: the "ghost alarm" bug.

    ``remove_job`` cancels the job's task, but cancellation only stops
    the loop if it actually propagates. ``AIService.chat()`` deliberately
    catches ``CancelledError`` (for the user stop button) and returns
    normally, so an interval alarm whose fire is mid-AI-call when it's
    cancelled would swallow the cancellation and keep firing forever —
    orphaned out of ``self._jobs`` and therefore invisible to the timer
    list. The scheduler must retire such an orphaned loop anyway.
    """
    fire_count = 0
    entered = asyncio.Event()
    # Simulate AIService.chat()'s stop-button behavior: swallow the
    # cancellation instead of re-raising. Flipped off in teardown so a
    # regression fails on the assertion below rather than hanging on an
    # unkillable orphan.
    swallow_cancel = True

    async def swallowing_cb() -> None:
        nonlocal fire_count
        fire_count += 1
        entered.set()
        try:
            # Stand in for a long in-flight AI call. When the task is
            # cancelled, the error is thrown in here.
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if swallow_cancel:
                return  # swallow, exactly like AIService.chat()
            raise

    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)
    tasks_before = asyncio.all_tasks()
    try:
        svc.add_job("ghost", Schedule.every(0.01), swallowing_cb, system=False)
        # Wait until the first fire is in-flight inside the callback.
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert fire_count == 1

        # Cancel while the callback is mid-flight: the cancellation is
        # thrown into the callback's await and swallowed there.
        svc.remove_job("ghost")
        assert svc.get_job("ghost") is None  # gone from the registry
        count_at_removal = fire_count

        # Give the (formerly orphaned) loop many intervals to misbehave.
        await asyncio.sleep(0.2)
        assert fire_count == count_at_removal, (
            f"orphaned job kept firing after removal: "
            f"{fire_count - count_at_removal} extra fire(s)"
        )
    finally:
        # Stop swallowing so any orphan the bug left behind can actually
        # be cancelled, then reap leftover tasks to keep teardown clean.
        swallow_cancel = False
        await svc.stop()
        for task in asyncio.all_tasks() - tasks_before:
            task.cancel()


# --- Tool: set_timer ---


async def test_tool_set_timer(service: SchedulerService) -> None:
    result = await service.execute_tool(
        "set_timer",
        {
            "name": "pizza",
            "seconds": 300,
            "message": "Pizza is ready!",
        },
    )
    parsed = json.loads(result)
    assert parsed["status"] == "set"
    assert parsed["name"] == "pizza"
    assert service.get_job("pizza") is not None


# --- Tool: set_alarm ---


async def test_tool_set_alarm_interval(service: SchedulerService) -> None:
    result = await service.execute_tool(
        "set_alarm",
        {
            "name": "check-mail",
            "type": "interval",
            "interval_seconds": 60,
        },
    )
    parsed = json.loads(result)
    assert parsed["status"] == "set"


async def test_tool_set_alarm_daily(service: SchedulerService) -> None:
    result = await service.execute_tool(
        "set_alarm",
        {
            "name": "standup",
            "type": "daily",
            "hour": 9,
            "minute": 0,
        },
    )
    parsed = json.loads(result)
    assert parsed["status"] == "set"


# --- Tool: cancel_timer ---


async def test_tool_cancel_timer(service: SchedulerService) -> None:
    await service.execute_tool("set_timer", {"name": "temp", "seconds": 999})
    result = await service.execute_tool("cancel_timer", {"name": "temp"})
    parsed = json.loads(result)
    assert parsed["status"] == "cancelled"


async def test_tool_cancel_nonexistent(service: SchedulerService) -> None:
    result = await service.execute_tool("cancel_timer", {"name": "nope"})
    parsed = json.loads(result)
    assert "error" in parsed


# --- Tool: list_timers ---


async def test_tool_list_timers(service: SchedulerService) -> None:
    service.add_job("sys-poll", Schedule.every(5), AsyncMock(), system=True)
    result = await service.execute_tool("list_timers", {})
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "sys-poll"
    assert parsed[0]["type"] == "system"


# --- Config ---


def test_config_doorbell_defaults() -> None:
    config = GilbertConfig.model_validate({})
    assert config.doorbell.enabled is False
    assert config.doorbell.poll_interval_seconds == 5.0
    assert config.doorbell.speakers == []


# --- Dynamic action tests ---


class _FakeTool:
    """Minimal ToolProvider + fake-service stand-in.

    Implements the handful of methods SchedulerService looks for when
    walking tool providers: service_info() is irrelevant because the
    resolver filters by capability, but get_tools() and execute_tool()
    are both called.
    """

    def __init__(
        self,
        tool_name: str = "test_tool",
        required_role: str = "user",
    ) -> None:
        self.tool_name = tool_name
        self.required_role = required_role
        self.calls: list[dict[str, Any]] = []
        self.raise_exc: Exception | None = None
        self.return_value: str = "OK"

    @property
    def tool_provider_name(self) -> str:
        return "fake_tool_provider"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=self.tool_name,
                description="Fake test tool",
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="Text",
                        required=False,
                    ),
                ],
                required_role=self.required_role,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append({"name": name, "arguments": arguments})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


class _FakeAIChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_exc: Exception | None = None

    async def chat(self, **kwargs: Any) -> tuple[str, str, list[Any], list[Any]]:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ("AI did the thing", "conv-id", [], [])


class _FakeACL:
    """AccessControlProvider stand-in — simple level table."""

    def __init__(self, user_level: int = 100, role_levels: dict[str, int] | None = None) -> None:
        self._user_level = user_level
        self._role_levels = role_levels or {
            "everyone": 200,
            "user": 100,
            "admin": 0,
        }

    def get_role_level(self, role: str) -> int:
        return self._role_levels.get(role, 999)

    def get_effective_level(self, user_ctx: Any) -> int:
        return self._user_level

    def resolve_rpc_level(self, *args: Any, **kwargs: Any) -> int:
        return self._user_level

    def get_rpc_override_level(self, frame_type: str) -> int | None:
        return None

    def check_collection_read(self, user_ctx: Any, collection: str) -> bool:
        return True

    def check_collection_write(self, user_ctx: Any, collection: str) -> bool:
        return True


def _resolver_with(
    *,
    tools: list[_FakeTool] | None = None,
    ai: _FakeAIChat | None = None,
    acl: _FakeACL | None = None,
    storage: Any = None,
    event_bus: Any = None,
    config: Any = None,
) -> Any:
    """Build a fake ServiceResolver that returns configured capabilities."""

    class _FakeResolver:
        def __init__(self) -> None:
            self._tools = tools or []
            self._caps: dict[str, Any] = {}
            if ai is not None:
                self._caps["ai_chat"] = ai
            if acl is not None:
                self._caps["access_control"] = acl
            if storage is not None:
                self._caps["entity_storage"] = storage
            if event_bus is not None:
                self._caps["event_bus"] = event_bus
            if config is not None:
                self._caps["configuration"] = config

        def get_capability(self, name: str) -> Any:
            return self._caps.get(name)

        def require_capability(self, name: str) -> Any:
            cap = self._caps.get(name)
            if cap is None:
                raise LookupError(name)
            return cap

        def get_all(self, cap: str) -> list[Any]:
            if cap == "ai_tools":
                return list(self._tools)
            svc = self._caps.get(cap)
            return [svc] if svc is not None else []

    return _FakeResolver()


# --- Rate limiter unit tests ---


def test_rate_limiter_basic() -> None:
    rl = _AICallRateLimiter(max_calls=3, window_seconds=60)
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False  # limit hit
    assert rl.try_acquire() is False


def test_rate_limiter_disabled_by_zero_calls() -> None:
    rl = _AICallRateLimiter(max_calls=0, window_seconds=60)
    assert rl.try_acquire() is False


def test_rate_limiter_disabled_by_zero_window() -> None:
    rl = _AICallRateLimiter(max_calls=10, window_seconds=0)
    assert rl.try_acquire() is False


def test_rate_limiter_config_update() -> None:
    rl = _AICallRateLimiter(max_calls=1, window_seconds=60)
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False
    rl.update_config(max_calls=5, window_seconds=60)
    # The existing timestamp counts; there's now 4 slots available
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False


def test_rate_limiter_status_snapshot() -> None:
    rl = _AICallRateLimiter(max_calls=5, window_seconds=120)
    rl.try_acquire()
    rl.try_acquire()
    status = rl.status()
    assert status["max_calls"] == 5
    assert status["window_seconds"] == 120
    assert status["recent_calls"] == 2
    assert status["available"] == 3


def test_rate_limiter_window_eviction() -> None:
    """Old timestamps are evicted so slots free up over time."""
    import time as time_module

    rl = _AICallRateLimiter(max_calls=2, window_seconds=0.05)
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False
    # Wait longer than the window so old timestamps fall off
    time_module.sleep(0.1)
    assert rl.try_acquire() is True


# --- _build_action_from_args / validation ---


@pytest.mark.asyncio
async def test_build_action_event_default(service: SchedulerService) -> None:
    action, err = service._build_action_from_args({"message": "pizza ready"})
    assert err is None
    assert action.type == ScheduledActionType.EVENT
    assert action.message == "pizza ready"


@pytest.mark.asyncio
async def test_build_action_mutual_exclusion() -> None:
    svc = SchedulerService()
    # Reach into the builder directly without a resolver — the mutual-
    # exclusion check runs before tool validation, so no resolver needed.
    action, err = svc._build_action_from_args({"tool": "x", "ai_prompt": "y"})
    assert err is not None
    assert "only one of 'tool', 'ai_prompt', or 'steps'" in err


@pytest.mark.asyncio
async def test_build_action_unknown_tool_errors() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[], acl=_FakeACL())
    action, err = svc._build_action_from_args({"tool": "does_not_exist"})
    assert err is not None
    assert "Unknown tool" in err


@pytest.mark.asyncio
async def test_build_action_validates_rbac_at_setup() -> None:
    svc = SchedulerService()
    # User level 100 (user), but tool requires admin (level 0) — denied
    admin_tool = _FakeTool(tool_name="admin_only", required_role="admin")
    svc._resolver = _resolver_with(tools=[admin_tool], acl=_FakeACL(user_level=100))
    action, err = svc._build_action_from_args({"tool": "admin_only"})
    assert err is not None
    assert "permission" in err.lower()


@pytest.mark.asyncio
async def test_build_action_tool_arguments_must_be_dict() -> None:
    svc = SchedulerService()
    fake = _FakeTool()
    svc._resolver = _resolver_with(tools=[fake], acl=_FakeACL())
    action, err = svc._build_action_from_args({"tool": "test_tool", "tool_arguments": "not a dict"})
    assert err is not None
    assert "tool_arguments" in err


@pytest.mark.asyncio
async def test_build_action_tool_ok() -> None:
    svc = SchedulerService()
    fake = _FakeTool()
    svc._resolver = _resolver_with(tools=[fake], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {"tool": "test_tool", "tool_arguments": {"text": "hi"}}
    )
    assert err is None
    assert action.type == ScheduledActionType.TOOL
    assert action.tool == "test_tool"
    assert action.tool_arguments == {"text": "hi"}


@pytest.mark.asyncio
async def test_build_action_ai_prompt_ok() -> None:
    svc = SchedulerService()
    action, err = svc._build_action_from_args({"ai_prompt": "Announce at 6pm"})
    assert err is None
    assert action.type == ScheduledActionType.AI_PROMPT
    assert action.ai_prompt == "Announce at 6pm"


# --- Dispatch tests ---


@pytest.mark.asyncio
async def test_dispatch_tool_action_calls_tool() -> None:
    svc = SchedulerService()
    fake = _FakeTool()
    svc._resolver = _resolver_with(tools=[fake])
    action = ScheduledAction(
        type=ScheduledActionType.TOOL,
        tool="test_tool",
        tool_arguments={"text": "fire!"},
    )
    await svc._dispatch_action("test-job", action, owner="u1", event_type="timer.fired")
    assert len(fake.calls) == 1
    assert fake.calls[0] == {"name": "test_tool", "arguments": {"text": "fire!"}}


@pytest.mark.asyncio
async def test_dispatch_tool_action_unknown_tool_logs_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[])
    action = ScheduledAction(
        type=ScheduledActionType.TOOL,
        tool="missing_tool",
        tool_arguments={},
    )
    # Should not raise
    await svc._dispatch_action("j", action, owner="u1", event_type="timer.fired")


@pytest.mark.asyncio
async def test_dispatch_tool_action_swallows_tool_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = SchedulerService()
    fake = _FakeTool()
    fake.raise_exc = RuntimeError("tool went boom")
    svc._resolver = _resolver_with(tools=[fake])
    action = ScheduledAction(
        type=ScheduledActionType.TOOL,
        tool="test_tool",
        tool_arguments={},
    )
    # Should not raise — the scheduler loop must survive
    await svc._dispatch_action("j", action, owner="u1", event_type="timer.fired")
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_ai_action_calls_ai() -> None:
    svc = SchedulerService()
    ai = _FakeAIChat()
    svc._resolver = _resolver_with(ai=ai)
    action = ScheduledAction(
        type=ScheduledActionType.AI_PROMPT,
        ai_prompt="Do the thing",
    )
    await svc._dispatch_action("j", action, owner="u1", event_type="alarm.fired")
    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["user_message"] == "Do the thing"
    assert call["ai_profile"] == "standard"
    assert call["system_prompt"]  # non-empty


@pytest.mark.asyncio
async def test_dispatch_ai_action_respects_rate_limit() -> None:
    svc = SchedulerService()
    ai = _FakeAIChat()
    svc._resolver = _resolver_with(ai=ai)
    # Only 1 AI call allowed per very-long window → second fire is denied
    svc._ai_rate_limiter.update_config(max_calls=1, window_seconds=3600)
    action = ScheduledAction(
        type=ScheduledActionType.AI_PROMPT,
        ai_prompt="Fire!",
    )
    await svc._dispatch_action("j", action, owner="u1", event_type="alarm.fired")
    await svc._dispatch_action("j", action, owner="u1", event_type="alarm.fired")
    await svc._dispatch_action("j", action, owner="u1", event_type="alarm.fired")
    # Only the first one made it through to the AI
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_ai_action_disabled_by_zero_limit() -> None:
    svc = SchedulerService()
    ai = _FakeAIChat()
    svc._resolver = _resolver_with(ai=ai)
    svc._ai_rate_limiter.update_config(max_calls=0, window_seconds=60)
    action = ScheduledAction(
        type=ScheduledActionType.AI_PROMPT,
        ai_prompt="Fire!",
    )
    await svc._dispatch_action("j", action, owner="u1", event_type="alarm.fired")
    assert len(ai.calls) == 0


@pytest.mark.asyncio
async def test_dispatch_ai_action_no_ai_capability_logs() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with()  # no AI registered
    action = ScheduledAction(
        type=ScheduledActionType.AI_PROMPT,
        ai_prompt="Fire!",
    )
    # Should not raise
    await svc._dispatch_action("j", action, owner="u1", event_type="alarm.fired")


@pytest.mark.asyncio
async def test_dispatch_event_action_publishes_event() -> None:
    svc = SchedulerService()
    published: list[Any] = []

    class _Bus:
        async def publish(self, event: Any) -> None:
            published.append(event)

    svc._event_bus = _Bus()
    action = ScheduledAction(type=ScheduledActionType.EVENT, message="pizza done")
    await svc._dispatch_action("pizza-timer", action, owner="u1", event_type="timer.fired")
    assert len(published) == 1
    assert published[0].event_type == "timer.fired"
    assert published[0].data == {"name": "pizza-timer", "message": "pizza done"}


# --- SEQUENCE action: _build_action_from_args validation ---


@pytest.mark.asyncio
async def test_build_action_steps_ok() -> None:
    svc = SchedulerService()
    music = _FakeTool(tool_name="music_play")
    stopper = _FakeTool(tool_name="music_stop")
    announcer = _FakeTool(tool_name="audio_output")
    svc._resolver = _resolver_with(tools=[music, stopper, announcer], acl=_FakeACL())

    action, err = svc._build_action_from_args(
        {
            "steps": [
                {"tool": "music_play", "tool_arguments": {"q": "random"}},
                {"tool": "music_stop", "tool_arguments": {}, "delay_before_seconds": 5},
                {"tool": "audio_output", "tool_arguments": {"text": "hi"}},
            ],
        }
    )
    assert err is None
    assert action.type == ScheduledActionType.SEQUENCE
    assert len(action.steps) == 3
    assert action.steps[0].tool == "music_play"
    assert action.steps[0].tool_arguments == {"q": "random"}
    assert action.steps[0].delay_before_seconds == 0.0
    assert action.steps[1].delay_before_seconds == 5.0
    assert action.steps[2].tool == "audio_output"


@pytest.mark.asyncio
async def test_build_action_steps_rejects_empty_list() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[], acl=_FakeACL())
    action, err = svc._build_action_from_args({"steps": []})
    assert err is not None
    assert "at least one step" in err


@pytest.mark.asyncio
async def test_build_action_steps_rejects_non_list() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[], acl=_FakeACL())
    action, err = svc._build_action_from_args({"steps": "not a list"})
    assert err is not None
    assert "list" in err.lower()


@pytest.mark.asyncio
async def test_build_action_steps_missing_tool_rejected() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "steps": [{"tool_arguments": {"x": 1}}],
        }
    )
    assert err is not None
    assert "missing 'tool'" in err


@pytest.mark.asyncio
async def test_build_action_steps_unknown_tool_rejected() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "steps": [{"tool": "does_not_exist"}],
        }
    )
    assert err is not None
    assert "does_not_exist" in err
    assert "Unknown tool" in err


@pytest.mark.asyncio
async def test_build_action_steps_rbac_enforced_per_step() -> None:
    """Each step is RBAC-checked individually — a non-admin user can't
    schedule a sequence that includes an admin-only tool."""
    svc = SchedulerService()
    user_tool = _FakeTool(tool_name="user_tool", required_role="user")
    admin_tool = _FakeTool(tool_name="admin_only", required_role="admin")
    svc._resolver = _resolver_with(
        tools=[user_tool, admin_tool],
        acl=_FakeACL(user_level=100),
    )
    action, err = svc._build_action_from_args(
        {
            "steps": [
                {"tool": "user_tool"},
                {"tool": "admin_only"},
            ],
        }
    )
    assert err is not None
    assert "admin_only" in err
    assert "permission" in err.lower()


@pytest.mark.asyncio
async def test_build_action_steps_invalid_tool_arguments() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "steps": [{"tool": "test_tool", "tool_arguments": "not a dict"}],
        }
    )
    assert err is not None
    assert "tool_arguments" in err


@pytest.mark.asyncio
async def test_build_action_steps_invalid_delay_rejected() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "steps": [{"tool": "test_tool", "delay_before_seconds": "soon"}],
        }
    )
    assert err is not None
    assert "delay_before_seconds" in err


@pytest.mark.asyncio
async def test_build_action_steps_negative_delay_clamped_to_zero() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "steps": [{"tool": "test_tool", "delay_before_seconds": -3}],
        }
    )
    assert err is None
    assert action.steps[0].delay_before_seconds == 0.0


@pytest.mark.asyncio
async def test_build_action_steps_mutually_exclusive_with_tool() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "tool": "test_tool",
            "steps": [{"tool": "test_tool"}],
        }
    )
    assert err is not None
    assert "only one of" in err


@pytest.mark.asyncio
async def test_build_action_steps_mutually_exclusive_with_ai_prompt() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    action, err = svc._build_action_from_args(
        {
            "ai_prompt": "do stuff",
            "steps": [{"tool": "test_tool"}],
        }
    )
    assert err is not None
    assert "only one of" in err


# --- SEQUENCE action: _dispatch_sequence_action ---


@pytest.mark.asyncio
async def test_dispatch_sequence_runs_steps_in_order() -> None:
    svc = SchedulerService()
    tool_a = _FakeTool(tool_name="tool_a")
    tool_b = _FakeTool(tool_name="tool_b")
    tool_c = _FakeTool(tool_name="tool_c")
    svc._resolver = _resolver_with(tools=[tool_a, tool_b, tool_c])

    action = ScheduledAction(
        type=ScheduledActionType.SEQUENCE,
        steps=[
            ActionStep(tool="tool_a", tool_arguments={"i": 1}),
            ActionStep(tool="tool_b", tool_arguments={"i": 2}),
            ActionStep(tool="tool_c", tool_arguments={"i": 3}),
        ],
    )
    await svc._dispatch_action("seq-test", action, owner="u1", event_type="alarm.fired")
    assert len(tool_a.calls) == 1
    assert tool_a.calls[0]["arguments"] == {"i": 1}
    assert len(tool_b.calls) == 1
    assert tool_b.calls[0]["arguments"] == {"i": 2}
    assert len(tool_c.calls) == 1
    assert tool_c.calls[0]["arguments"] == {"i": 3}


@pytest.mark.asyncio
async def test_dispatch_sequence_respects_delays() -> None:
    """Steps with delay_before_seconds actually wait via asyncio.sleep."""
    svc = SchedulerService()
    tool_a = _FakeTool(tool_name="tool_a")
    tool_b = _FakeTool(tool_name="tool_b")
    svc._resolver = _resolver_with(tools=[tool_a, tool_b])

    sleep_calls: list[float] = []
    original_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await original_sleep(0)

    action = ScheduledAction(
        type=ScheduledActionType.SEQUENCE,
        steps=[
            ActionStep(tool="tool_a"),
            ActionStep(tool="tool_b", delay_before_seconds=5.0),
        ],
    )

    from unittest.mock import patch

    with patch(
        "gilbert.core.services.scheduler.asyncio.sleep",
        side_effect=fake_sleep,
    ):
        await svc._dispatch_action("delay-test", action, owner="u1", event_type="alarm.fired")

    # Only the second step had a delay — only one sleep call with 5s
    assert sleep_calls == [5.0]
    assert len(tool_a.calls) == 1
    assert len(tool_b.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_sequence_continues_after_failed_step() -> None:
    """A failing step is logged but subsequent steps still run."""
    svc = SchedulerService()
    good_a = _FakeTool(tool_name="good_a")
    bad = _FakeTool(tool_name="bad")
    bad.raise_exc = RuntimeError("middle step exploded")
    good_c = _FakeTool(tool_name="good_c")
    svc._resolver = _resolver_with(tools=[good_a, bad, good_c])

    action = ScheduledAction(
        type=ScheduledActionType.SEQUENCE,
        steps=[
            ActionStep(tool="good_a"),
            ActionStep(tool="bad"),
            ActionStep(tool="good_c"),
        ],
    )
    await svc._dispatch_action("resilient", action, owner="u1", event_type="alarm.fired")

    assert len(good_a.calls) == 1
    assert len(bad.calls) == 1
    assert len(good_c.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_sequence_unknown_tool_skipped_not_fatal() -> None:
    svc = SchedulerService()
    good = _FakeTool(tool_name="good")
    svc._resolver = _resolver_with(tools=[good])

    action = ScheduledAction(
        type=ScheduledActionType.SEQUENCE,
        steps=[
            ActionStep(tool="missing"),
            ActionStep(tool="good"),
        ],
    )
    await svc._dispatch_action("unknown", action, owner="u1", event_type="alarm.fired")
    assert len(good.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_sequence_empty_steps_no_crash() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[])
    action = ScheduledAction(type=ScheduledActionType.SEQUENCE, steps=[])
    await svc._dispatch_action("empty", action, owner="u1", event_type="alarm.fired")


@pytest.mark.asyncio
async def test_dispatch_sequence_step_with_empty_tool_skipped() -> None:
    svc = SchedulerService()
    good = _FakeTool(tool_name="good")
    svc._resolver = _resolver_with(tools=[good])

    action = ScheduledAction(
        type=ScheduledActionType.SEQUENCE,
        steps=[
            ActionStep(tool=""),
            ActionStep(tool="good"),
        ],
    )
    await svc._dispatch_action("malformed", action, owner="u1", event_type="alarm.fired")
    assert len(good.calls) == 1


@pytest.mark.asyncio
async def test_set_alarm_with_steps_end_to_end() -> None:
    """Full path: set_alarm → _build_action_from_args → registered job
    with SEQUENCE action → manually triggered callback runs all steps."""
    svc = SchedulerService()
    music = _FakeTool(tool_name="music_play")
    stopper = _FakeTool(tool_name="music_stop")
    announcer = _FakeTool(tool_name="audio_output")
    svc._resolver = _resolver_with(tools=[music, stopper, announcer], acl=_FakeACL())

    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "wake-up-chain",
            "type": "interval",
            "interval_seconds": 99999,
            "steps": [
                {
                    "tool": "music_play",
                    "tool_arguments": {"query": "random", "speakers": ["Bedroom"]},
                },
                {"tool": "music_stop", "tool_arguments": {"speakers": ["Bedroom"]}},
                {
                    "tool": "audio_output",
                    "tool_arguments": {
                        "text": "wake up",
                        "destination": "speakers",
                        "speaker_names": ["Bedroom"],
                    },
                },
            ],
        },
    )
    parsed = json.loads(result)
    assert parsed["status"] == "set"
    assert parsed["action_type"] == "sequence"

    job = svc._jobs.get("wake-up-chain")
    assert job is not None
    assert job.info.action.type == ScheduledActionType.SEQUENCE
    assert len(job.info.action.steps) == 3

    await job.callback()
    assert len(music.calls) == 1
    assert len(stopper.calls) == 1
    assert len(announcer.calls) == 1

    await svc.stop()


@pytest.mark.asyncio
async def test_set_alarm_rejects_tool_plus_steps() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "bad",
            "type": "interval",
            "interval_seconds": 60,
            "tool": "test_tool",
            "steps": [{"tool": "test_tool"}],
        },
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "only one of" in parsed["error"].lower()
    assert "bad" not in svc._jobs


@pytest.mark.asyncio
async def test_list_timers_exposes_sequence_action() -> None:
    """list_timers serializes the full sequence so the web UI can show
    what each step will do."""
    svc = SchedulerService()
    svc._resolver = _resolver_with(
        tools=[_FakeTool(tool_name="tool_a"), _FakeTool(tool_name="tool_b")],
        acl=_FakeACL(),
    )
    await svc.execute_tool(
        "set_alarm",
        {
            "name": "seq-list",
            "type": "interval",
            "interval_seconds": 99999,
            "steps": [
                {"tool": "tool_a", "tool_arguments": {"x": 1}},
                {"tool": "tool_b", "delay_before_seconds": 2},
            ],
        },
    )
    result = await svc.execute_tool("list_timers", {})
    parsed = json.loads(result)
    entry = next(j for j in parsed if j["name"] == "seq-list")
    assert entry["action"]["type"] == "sequence"
    assert len(entry["action"]["steps"]) == 2
    assert entry["action"]["steps"][0]["tool"] == "tool_a"
    assert entry["action"]["steps"][1]["delay_before_seconds"] == 2.0

    await svc.stop()


@pytest.mark.asyncio
async def test_sequence_persistence_round_trip() -> None:
    """A SEQUENCE alarm survives a scheduler restart with all steps intact."""
    stored: dict[str, dict[str, Any]] = {}

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data  # type: ignore[assignment]

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)  # type: ignore[call-overload]

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    fake_storage = _FakeStorage()

    svc1 = SchedulerService()
    svc1._resolver = _resolver_with(
        tools=[_FakeTool(tool_name="tool_a"), _FakeTool(tool_name="tool_b")],
        acl=_FakeACL(),
    )
    svc1._storage = fake_storage  # type: ignore[assignment]
    await svc1.execute_tool(
        "set_alarm",
        {
            "name": "persisted-seq",
            "type": "interval",
            "interval_seconds": 99999,
            "steps": [
                {"tool": "tool_a", "tool_arguments": {"n": 1}},
                {"tool": "tool_b", "delay_before_seconds": 7},
            ],
        },
    )
    await svc1.stop()
    assert "persisted-seq" in stored["scheduler_jobs"]

    svc2 = SchedulerService()
    svc2._storage = fake_storage  # type: ignore[assignment]
    await svc2._load_persisted_jobs()
    restored = svc2._jobs.get("persisted-seq")
    assert restored is not None
    assert restored.info.action.type == ScheduledActionType.SEQUENCE
    assert len(restored.info.action.steps) == 2
    assert restored.info.action.steps[0].tool == "tool_a"
    assert restored.info.action.steps[0].tool_arguments == {"n": 1}
    assert restored.info.action.steps[1].tool == "tool_b"
    assert restored.info.action.steps[1].delay_before_seconds == 7.0
    await svc2.stop()


# --- set_timer / set_alarm integration with actions ---


@pytest.mark.asyncio
async def test_set_alarm_with_tool_action_registers_and_persists() -> None:
    svc = SchedulerService()
    # In-memory fake storage for persistence
    stored: dict[str, dict[str, Any]] = {}

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data  # type: ignore[assignment]

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)  # type: ignore[call-overload]

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    fake_storage = _FakeStorage()
    fake_tool = _FakeTool()

    class _FakeStorageSvc:
        backend = fake_storage
        raw_backend = fake_storage

        def create_namespaced(self, ns: str) -> Any:
            return fake_storage

    svc._resolver = _resolver_with(tools=[fake_tool], acl=_FakeACL(), storage=_FakeStorageSvc())
    svc._storage = fake_storage  # type: ignore[assignment]

    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "test-alarm",
            "type": "interval",
            "interval_seconds": 99999,  # far future so it never fires
            "tool": "test_tool",
            "tool_arguments": {"text": "hi"},
        },
    )
    parsed = json.loads(result)
    assert parsed["status"] == "set"
    assert parsed["action_type"] == "tool"

    # Registered in memory
    job = svc._jobs.get("test-alarm")
    assert job is not None
    assert job.info.action.type == ScheduledActionType.TOOL
    assert job.info.action.tool == "test_tool"
    assert job.info.action.tool_arguments == {"text": "hi"}

    # Persisted
    persisted = stored.get("scheduler_jobs", {})
    assert "test-alarm" in persisted
    persisted_action = persisted["test-alarm"]["action"]
    assert persisted_action["type"] == "tool"
    assert persisted_action["tool"] == "test_tool"

    # Cleanup
    await svc.stop()


@pytest.mark.asyncio
async def test_set_alarm_rejects_both_tool_and_ai_prompt() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "bad",
            "type": "interval",
            "interval_seconds": 60,
            "tool": "test_tool",
            "ai_prompt": "Also do this",
        },
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "only one of" in parsed["error"].lower()
    # Nothing got registered
    assert "bad" not in svc._jobs


# --- set_alarm bounds + window validation ---


@pytest.mark.asyncio
async def test_set_alarm_accepts_start_at_and_end_at() -> None:
    """Happy path: 'every minute from 1am to 2am today only' should
    register, persist the bounds, and expose them in list_timers."""
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "bounded",
            "type": "interval",
            "interval_seconds": 60,
            "start_at": "2099-01-01T01:00:00",
            "end_at": "2099-01-01T02:00:00",
            "message": "tick",
        },
    )
    parsed = json.loads(result)
    assert parsed["status"] == "set"
    info = svc.get_job("bounded")
    assert info is not None
    assert info.schedule.start_at == datetime(2099, 1, 1, 1, 0)
    assert info.schedule.end_at == datetime(2099, 1, 1, 2, 0)


@pytest.mark.asyncio
async def test_set_alarm_rejects_end_before_start() -> None:
    """Bounded runs must go forward in time. Inverted bounds are almost
    always user error (or a typo) and would silently retire the job on
    first tick — better to fail loudly at setup."""
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "bad",
            "type": "interval",
            "interval_seconds": 60,
            "start_at": "2099-01-01T02:00:00",
            "end_at": "2099-01-01T01:00:00",
        },
    )
    assert "after start_at" in json.loads(result)["error"]
    assert "bad" not in svc._jobs


@pytest.mark.asyncio
async def test_set_alarm_rejects_half_specified_window() -> None:
    """window_start_time and window_end_time must be set together; a
    single-sided window is ambiguous and rejecting it early prevents
    silent misbehaviour."""
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "halfwindow",
            "type": "interval",
            "interval_seconds": 60,
            "window_start_time": "01:00",
        },
    )
    assert "together" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_set_alarm_rejects_overnight_window() -> None:
    """Overnight windows (end before start, e.g. 22:00-02:00) aren't
    supported — they'd require second-day wrapping in the clamp
    logic that isn't implemented. Reject with a clear message rather
    than half-working."""
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "overnight",
            "type": "interval",
            "interval_seconds": 60,
            "window_start_time": "22:00",
            "window_end_time": "02:00",
        },
    )
    assert "overnight windows" in json.loads(result)["error"].lower()


@pytest.mark.asyncio
async def test_set_alarm_rejects_window_on_daily_alarm() -> None:
    """Daily/hourly alarms already carry their own time anchor; a
    time-of-day window on them is nonsensical. Reject so the user
    doesn't discover this via mysterious non-firing."""
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    result = await svc.execute_tool(
        "set_alarm",
        {
            "name": "daily-with-window",
            "type": "daily",
            "hour": 8,
            "minute": 30,
            "window_start_time": "01:00",
            "window_end_time": "02:00",
        },
    )
    assert "interval" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_set_alarm_bounds_round_trip_through_persistence() -> None:
    """Re-registration across a restart must preserve start_at, end_at,
    and the window — otherwise a bounded alarm created before a reboot
    would run unbounded after it."""
    stored: dict[str, dict[str, Any]] = {}

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    fake = _FakeStorage()

    svc1 = SchedulerService()
    svc1._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    svc1._storage = fake  # type: ignore[assignment]
    await svc1.execute_tool(
        "set_alarm",
        {
            "name": "windowed",
            "type": "interval",
            "interval_seconds": 60,
            "start_at": "2099-06-01T00:00:00",
            "end_at": "2099-06-30T00:00:00",
            "window_start_time": "01:00",
            "window_end_time": "02:00",
        },
    )
    await svc1.stop()

    svc2 = SchedulerService()
    svc2._storage = fake  # type: ignore[assignment]
    await svc2._load_persisted_jobs()
    info = svc2.get_job("windowed")
    assert info is not None
    assert info.schedule.start_at == datetime(2099, 6, 1, 0, 0)
    assert info.schedule.end_at == datetime(2099, 6, 30, 0, 0)
    assert info.schedule.window_start_time == dtime(1, 0)
    assert info.schedule.window_end_time == dtime(2, 0)
    await svc2.stop()


@pytest.mark.asyncio
async def test_load_persisted_jobs_drops_expired_recurring_bounds() -> None:
    """A recurring alarm whose end_at is in the past on startup should
    be dropped from storage — analogous to how expired one-shot
    timers are cleaned up. Otherwise stale records would linger and
    re-register-then-immediately-retire on every restart."""
    past = (datetime.now() - timedelta(days=1)).isoformat()
    stored: dict[str, dict[str, Any]] = {
        "scheduler_jobs": {
            "retired": {
                "id": "retired",
                "name": "retired",
                "expression": "@every 60s",
                "start_at": "",
                "end_at": past,
                "window_start_time": "",
                "window_end_time": "",
                "owner": "u1",
                "action": {"type": "event", "message": "stale"},
                "created_at": past,
            }
        }
    }

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    svc = SchedulerService()
    svc._storage = _FakeStorage()  # type: ignore[assignment]
    await svc._load_persisted_jobs()
    assert svc.get_job("retired") is None
    assert "retired" not in stored["scheduler_jobs"]
    await svc.stop()


@pytest.mark.asyncio
async def test_list_timers_includes_action() -> None:
    svc = SchedulerService()
    svc._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())

    await svc.execute_tool(
        "set_alarm",
        {
            "name": "audio-alarm",
            "type": "interval",
            "interval_seconds": 99999,
            "tool": "test_tool",
            "tool_arguments": {"text": "hi"},
        },
    )

    result = await svc.execute_tool("list_timers", {})
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "audio-alarm"
    action = parsed[0]["action"]
    assert action["type"] == "tool"
    assert action["tool"] == "test_tool"
    assert action["tool_arguments"] == {"text": "hi"}

    await svc.stop()


# --- Persistence round-trip ---


@pytest.mark.asyncio
async def test_persistence_round_trip() -> None:
    """A user alarm created in one service instance is restored on the next."""
    stored: dict[str, dict[str, Any]] = {}

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data  # type: ignore[assignment]

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)  # type: ignore[call-overload]

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    fake_storage = _FakeStorage()

    # Instance 1: create the alarm
    svc1 = SchedulerService()
    svc1._resolver = _resolver_with(tools=[_FakeTool()], acl=_FakeACL())
    svc1._storage = fake_storage  # type: ignore[assignment]
    await svc1.execute_tool(
        "set_alarm",
        {
            "name": "persist-test",
            "type": "interval",
            "interval_seconds": 99999,
            "tool": "test_tool",
            "tool_arguments": {"text": "from instance 1"},
        },
    )
    await svc1.stop()
    assert "persist-test" in stored["scheduler_jobs"]

    # Instance 2: load from storage
    svc2 = SchedulerService()
    svc2._storage = fake_storage  # type: ignore[assignment]
    await svc2._load_persisted_jobs()
    restored = svc2._jobs.get("persist-test")
    assert restored is not None
    assert restored.info.action.type == ScheduledActionType.TOOL
    assert restored.info.action.tool == "test_tool"
    assert restored.info.action.tool_arguments == {"text": "from instance 1"}
    await svc2.stop()


@pytest.mark.asyncio
async def test_persistence_drops_expired_one_shot_timers() -> None:
    """One-shot timers whose fire_at is in the past are deleted on startup."""
    from datetime import UTC, datetime, timedelta

    past_fire_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    stored: dict[str, dict[str, Any]] = {
        "scheduler_jobs": {
            "expired": {
                "id": "expired",
                "name": "expired",
                "expression": "@once+60s",
                "owner": "u1",
                "action": {"type": "event", "message": "stale"},
                "created_at": past_fire_at,
                "fire_at": past_fire_at,
            },
        },
    }

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data  # type: ignore[assignment]

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)  # type: ignore[call-overload]

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    svc = SchedulerService()
    svc._storage = _FakeStorage()  # type: ignore[assignment]
    await svc._load_persisted_jobs()
    assert "expired" not in svc._jobs
    assert "expired" not in stored.get("scheduler_jobs", {})


@pytest.mark.asyncio
async def test_persistence_skips_rows_predating_the_cron_migration() -> None:
    """A pre-0007 row has no expression. Guessing one could turn a
    one-shot into a job that fires forever, so it is skipped and left in
    storage for the migration to convert."""
    stored: dict[str, dict[str, Any]] = {
        "scheduler_jobs": {
            "legacy": {
                "id": "legacy",
                "name": "legacy",
                "schedule_type": "once",
                "interval_seconds": 60,
                "owner": "u1",
                "action": {"type": "event", "message": "stale"},
            },
        },
    }

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data  # type: ignore[assignment]

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)  # type: ignore[call-overload]

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    svc = SchedulerService()
    svc._storage = _FakeStorage()  # type: ignore[assignment]
    await svc._load_persisted_jobs()

    assert "legacy" not in svc._jobs
    # Not deleted — the migration still needs to convert it.
    assert "legacy" in stored["scheduler_jobs"]


# --- Config live reload ---


@pytest.mark.asyncio
async def test_on_config_changed_updates_rate_limiter() -> None:
    svc = SchedulerService()
    await svc.on_config_changed({"alarm_ai_max_calls": 7, "alarm_ai_window_seconds": 123})
    status = svc._ai_rate_limiter.status()
    assert status["max_calls"] == 7
    assert status["window_seconds"] == 123
    # Disable via zero
    await svc.on_config_changed({"alarm_ai_max_calls": 0, "alarm_ai_window_seconds": 60})
    assert svc._ai_rate_limiter.try_acquire() is False


# --- WebSocket RPC handler tests ---


class _FakeConn:
    """Minimal WsConnection stand-in for handler tests.

    Exposes the attributes scheduler handlers actually read: user_ctx,
    user_level, and roles (via user_ctx).
    """

    def __init__(
        self,
        user_id: str = "alice",
        roles: frozenset[str] = frozenset({"user"}),
        user_level: int = 100,
    ) -> None:
        class _Ctx:
            def __init__(self, uid: str, r: frozenset[str]) -> None:
                self.user_id = uid
                self.roles = r

        self.user_ctx = _Ctx(user_id, roles)
        self.user_level = user_level


@pytest.mark.asyncio
async def test_ws_service_info_includes_ws_handlers_capability() -> None:
    svc = SchedulerService()
    info = svc.service_info()
    assert "ws_handlers" in info.capabilities


@pytest.mark.asyncio
async def test_ws_get_ws_handlers_returns_expected_keys() -> None:
    svc = SchedulerService()
    handlers = svc.get_ws_handlers()
    expected = {
        "scheduler.job.list",
        "scheduler.job.get",
        "scheduler.job.enable",
        "scheduler.job.disable",
        "scheduler.job.remove",
        "scheduler.job.run_now",
    }
    assert set(handlers.keys()) == expected


@pytest.mark.asyncio
async def test_ws_job_list_returns_serialized_jobs(service: SchedulerService) -> None:
    service.add_job("sys-poll", Schedule.every(5), AsyncMock(), system=True)
    service.add_job("user-beep", Schedule.every(10), AsyncMock(), system=False, owner="alice")
    conn = _FakeConn()
    response = await service._ws_job_list(conn, {"id": "req-1"})
    assert response is not None
    assert response["type"] == "scheduler.job.list.result"
    assert response["ref"] == "req-1"
    assert len(response["jobs"]) == 2
    names = {j["name"] for j in response["jobs"]}
    assert names == {"sys-poll", "user-beep"}
    # Each serialized job has schedule + action + type
    for j in response["jobs"]:
        assert "schedule" in j
        assert "action" in j
        assert j["type"] in ("system", "user")
        assert "enabled" in j
        assert "state" in j


@pytest.mark.asyncio
async def test_ws_job_list_can_exclude_system(service: SchedulerService) -> None:
    service.add_job("sys1", Schedule.every(5), AsyncMock(), system=True)
    service.add_job("usr1", Schedule.every(5), AsyncMock(), system=False, owner="alice")
    conn = _FakeConn()
    response = await service._ws_job_list(conn, {"id": "r", "include_system": False})
    assert response is not None
    names = {j["name"] for j in response["jobs"]}
    assert names == {"usr1"}


@pytest.mark.asyncio
async def test_ws_job_list_surfaces_action(service: SchedulerService) -> None:
    action = ScheduledAction(
        type=ScheduledActionType.TOOL,
        tool="audio_output",
        tool_arguments={"text": "hi"},
    )
    service.add_job(
        "with-action",
        Schedule.every(99),
        AsyncMock(),
        system=False,
        owner="alice",
        action=action,
    )
    conn = _FakeConn()
    response = await service._ws_job_list(conn, {"id": "r"})
    assert response is not None
    job = next(j for j in response["jobs"] if j["name"] == "with-action")
    assert job["action"]["type"] == "tool"
    assert job["action"]["tool"] == "audio_output"
    assert job["action"]["tool_arguments"] == {"text": "hi"}


@pytest.mark.asyncio
async def test_ws_job_get_returns_single_job(service: SchedulerService) -> None:
    service.add_job("only-one", Schedule.every(5), AsyncMock(), system=False, owner="alice")
    conn = _FakeConn()
    response = await service._ws_job_get(conn, {"id": "r", "name": "only-one"})
    assert response is not None
    assert response["type"] == "scheduler.job.get.result"
    assert response["job"]["name"] == "only-one"


@pytest.mark.asyncio
async def test_ws_job_get_unknown_returns_404(service: SchedulerService) -> None:
    conn = _FakeConn()
    response = await service._ws_job_get(conn, {"id": "r", "name": "nope"})
    assert response is not None
    assert response["type"] == "gilbert.error"
    assert response["code"] == 404


@pytest.mark.asyncio
async def test_ws_job_get_missing_name_returns_400(service: SchedulerService) -> None:
    conn = _FakeConn()
    response = await service._ws_job_get(conn, {"id": "r"})
    assert response is not None
    assert response["type"] == "gilbert.error"
    assert response["code"] == 400


@pytest.mark.asyncio
async def test_ws_job_enable_disable_toggle(service: SchedulerService) -> None:
    service.add_job("toggleable", Schedule.every(5), AsyncMock(), system=False, owner="alice")
    conn = _FakeConn()

    # Disable
    response = await service._ws_job_disable(conn, {"id": "r", "name": "toggleable"})
    assert response is not None
    assert response["status"] == "disabled"
    assert service.get_job("toggleable").enabled is False

    # Enable
    response = await service._ws_job_enable(conn, {"id": "r", "name": "toggleable"})
    assert response is not None
    assert response["status"] == "enabled"
    assert service.get_job("toggleable").enabled is True


@pytest.mark.asyncio
async def test_ws_job_enable_unknown_returns_404(service: SchedulerService) -> None:
    conn = _FakeConn()
    response = await service._ws_job_enable(conn, {"id": "r", "name": "nope"})
    assert response is not None
    assert response["type"] == "gilbert.error"
    assert response["code"] == 404


@pytest.mark.asyncio
async def test_ws_job_remove_admin_can_remove_any(service: SchedulerService) -> None:
    service.add_job("others-job", Schedule.every(5), AsyncMock(), system=False, owner="bob")
    # Admin connection
    admin_conn = _FakeConn(user_id="alice", roles=frozenset({"admin"}), user_level=0)
    response = await service._ws_job_remove(admin_conn, {"id": "r", "name": "others-job"})
    assert response is not None
    assert response["status"] == "removed"
    assert service.get_job("others-job") is None


@pytest.mark.asyncio
async def test_ws_job_remove_user_blocked_from_others(service: SchedulerService) -> None:
    service.add_job("bobs-job", Schedule.every(5), AsyncMock(), system=False, owner="bob")
    user_conn = _FakeConn(user_id="alice", roles=frozenset({"user"}), user_level=100)
    response = await service._ws_job_remove(user_conn, {"id": "r", "name": "bobs-job"})
    assert response is not None
    assert response["type"] == "gilbert.error"
    assert response["code"] == 403
    # Job is still there
    assert service.get_job("bobs-job") is not None


@pytest.mark.asyncio
async def test_ws_job_remove_user_can_remove_own(service: SchedulerService) -> None:
    service.add_job("alices-job", Schedule.every(5), AsyncMock(), system=False, owner="alice")
    user_conn = _FakeConn(user_id="alice", roles=frozenset({"user"}), user_level=100)
    response = await service._ws_job_remove(user_conn, {"id": "r", "name": "alices-job"})
    assert response is not None
    assert response["status"] == "removed"


@pytest.mark.asyncio
async def test_ws_job_remove_system_job_blocked(service: SchedulerService) -> None:
    service.add_job("sys", Schedule.every(5), AsyncMock(), system=True)
    admin_conn = _FakeConn(user_id="alice", roles=frozenset({"admin"}), user_level=0)
    response = await service._ws_job_remove(admin_conn, {"id": "r", "name": "sys"})
    assert response is not None
    assert response["type"] == "gilbert.error"
    # System job → ValueError → 400
    assert response["code"] == 400
    assert "system" in response["error"].lower()
    # Still registered
    assert service.get_job("sys") is not None


@pytest.mark.asyncio
async def test_ws_job_run_now_fires_callback(service: SchedulerService) -> None:
    fired = asyncio.Event()

    async def _fire() -> None:
        fired.set()

    service.add_job("run-now-test", Schedule.every(99999), _fire, system=False, owner="alice")
    conn = _FakeConn()
    response = await service._ws_job_run_now(conn, {"id": "r", "name": "run-now-test"})
    assert response is not None
    assert response["status"] == "fired"
    assert fired.is_set()


@pytest.mark.asyncio
async def test_ws_job_run_now_unknown_returns_404(service: SchedulerService) -> None:
    conn = _FakeConn()
    response = await service._ws_job_run_now(conn, {"id": "r", "name": "nope"})
    assert response is not None
    assert response["type"] == "gilbert.error"
    assert response["code"] == 404


def test_acl_scheduler_rpc_defaults() -> None:
    """The scheduler frame types must resolve to the documented role levels."""
    from gilbert.interfaces.acl import resolve_default_rpc_level

    # User-level
    assert resolve_default_rpc_level("scheduler.job.list") == 100
    assert resolve_default_rpc_level("scheduler.job.get") == 100
    assert resolve_default_rpc_level("scheduler.job.remove") == 100
    # Admin-only state-changing operations
    assert resolve_default_rpc_level("scheduler.job.enable") == 0
    assert resolve_default_rpc_level("scheduler.job.disable") == 0
    assert resolve_default_rpc_level("scheduler.job.run_now") == 0


def _make_job(name: str, schedule: Schedule, callback: Any) -> Any:
    """Build an internal _Job directly, for engine-level tests."""
    from gilbert.core.services.scheduler import _Job

    return _Job(name=name, schedule=schedule, callback=callback)



# --- Catch-up, overlap, and suspend resilience ---


class _RecordingStorage:
    """Minimal in-memory storage that records fire history."""

    def __init__(self, seed: dict[str, dict[str, Any]] | None = None) -> None:
        self.data: dict[str, dict[str, Any]] = seed or {}

    async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
        self.data.setdefault(coll, {})[key] = data

    async def get(self, coll: str, key: str) -> dict[str, Any] | None:
        return self.data.get(coll, {}).get(key)

    async def delete(self, coll: str, key: str) -> None:
        self.data.get(coll, {}).pop(key, None)

    async def query(self, q: Any) -> list[dict[str, Any]]:
        return list(self.data.get(q.collection, {}).values())


def _fire_state(name: str, when: datetime) -> dict[str, dict[str, Any]]:
    return {
        "scheduler_job_state": {
            name: {
                "id": name,
                "name": name,
                "last_fire_at": when.astimezone(UTC).isoformat(),
            }
        }
    }


@pytest.mark.asyncio
async def test_catch_up_skip_does_not_fire_for_missed_occurrences() -> None:
    """Real cron behaviour: a fire missed during downtime is lost."""
    svc = SchedulerService()
    svc._storage = _RecordingStorage(  # type: ignore[assignment]
        _fire_state("j", datetime.now(UTC) - timedelta(days=2))
    )
    calls = 0

    async def _cb() -> None:
        nonlocal calls
        calls += 1

    schedule = Schedule.daily_at(3, catch_up=CatchUpPolicy.SKIP)
    job = _make_job("j", schedule, _cb)
    await svc._run_catch_up(job, datetime.now(UTC) - timedelta(days=2))
    assert calls == 0


@pytest.mark.asyncio
async def test_catch_up_once_fires_exactly_one_make_up_run() -> None:
    """Two days of missed daily fires produce ONE catch-up, not two."""
    svc = SchedulerService()
    svc._storage = _RecordingStorage()  # type: ignore[assignment]
    calls = 0

    async def _cb() -> None:
        nonlocal calls
        calls += 1

    schedule = Schedule.daily_at(3, catch_up=CatchUpPolicy.ONCE)
    job = _make_job("j", schedule, _cb)
    await svc._run_catch_up(job, datetime.now(UTC) - timedelta(days=2))
    assert calls == 1


@pytest.mark.asyncio
async def test_catch_up_backfill_replays_every_missed_occurrence() -> None:
    svc = SchedulerService()
    svc._storage = _RecordingStorage()  # type: ignore[assignment]
    calls = 0

    async def _cb() -> None:
        nonlocal calls
        calls += 1

    schedule = Schedule.hourly_at(0, catch_up=CatchUpPolicy.BACKFILL)
    job = _make_job("j", schedule, _cb)
    await svc._run_catch_up(job, datetime.now(UTC) - timedelta(hours=5))
    # Five hours of downtime — between 4 and 6 hourly boundaries elapsed
    # depending on where "now" sits inside the hour.
    assert 4 <= calls <= 6


@pytest.mark.asyncio
async def test_catch_up_backfill_is_capped() -> None:
    """A fast job plus long downtime must not fire unboundedly."""
    schedule = Schedule.every(1, catch_up=CatchUpPolicy.BACKFILL)
    missed = SchedulerService._missed_fires(
        schedule,
        datetime.now(UTC) - timedelta(days=30),
        datetime.now(UTC),
        limit=100,
    )
    assert len(missed) == 100


@pytest.mark.asyncio
async def test_catch_up_does_nothing_without_recorded_history() -> None:
    """A fresh install has no fire history and must not fire on boot."""
    svc = SchedulerService()
    svc._storage = _RecordingStorage()  # type: ignore[assignment]
    assert await svc._load_last_fire_at("never-run") is None


@pytest.mark.asyncio
async def test_overlap_skip_drops_a_fire_while_one_is_in_flight() -> None:
    svc = SchedulerService()
    calls = 0

    async def _cb() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)

    job = _make_job("j", Schedule.every(1, overlap=OverlapPolicy.SKIP), _cb)
    svc._dispatch_fire(job)
    await asyncio.sleep(0.02)
    svc._dispatch_fire(job)  # previous still running -> dropped
    await asyncio.sleep(0.3)
    assert calls == 1


@pytest.mark.asyncio
async def test_overlap_queue_serialises_fires() -> None:
    svc = SchedulerService()
    concurrent = 0
    peak = 0

    async def _cb() -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.1)
        concurrent -= 1

    job = _make_job("j", Schedule.every(1, overlap=OverlapPolicy.QUEUE), _cb)
    svc._dispatch_fire(job)
    await asyncio.sleep(0.02)
    svc._dispatch_fire(job)
    await asyncio.sleep(0.4)
    assert peak == 1


@pytest.mark.asyncio
async def test_overlap_concurrent_allows_simultaneous_fires() -> None:
    svc = SchedulerService()
    concurrent = 0
    peak = 0

    async def _cb() -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.1)
        concurrent -= 1

    job = _make_job(
        "j", Schedule.every(1, overlap=OverlapPolicy.CONCURRENT), _cb
    )
    svc._dispatch_fire(job)
    await asyncio.sleep(0.02)
    svc._dispatch_fire(job)
    await asyncio.sleep(0.3)
    assert peak == 2


@pytest.mark.asyncio
async def test_sleep_until_returns_immediately_for_a_past_target() -> None:
    """The suspend case: waking to find the target already behind us."""
    svc = SchedulerService()
    started = asyncio.get_running_loop().time()
    await svc._sleep_until(datetime.now(UTC) - timedelta(hours=2))
    assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.asyncio
async def test_sleep_until_is_chunked_not_one_long_sleep() -> None:
    """A day-long wait must not become a single un-interruptible sleep,
    or a host suspend would go unnoticed until it elapsed."""
    svc = SchedulerService()
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _spy(delay: float, *a: Any, **kw: Any) -> None:
        slept.append(delay)
        raise asyncio.CancelledError

    asyncio.sleep = _spy  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await svc._sleep_until(datetime.now(UTC) + timedelta(days=1))
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert slept and slept[0] <= 60.0


@pytest.mark.asyncio
async def test_next_run_at_is_reported_for_a_running_job() -> None:
    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)

    async def _cb() -> None:
        return None

    svc.add_job("daily", Schedule.daily_at(3, 30), _cb)
    await asyncio.sleep(0.05)
    info = svc.get_job("daily")
    assert info is not None
    assert info.next_run_at
    assert info.description == "daily at 03:30"
    await svc.stop()


@pytest.mark.asyncio
async def test_serialize_job_exposes_expression_and_next_run() -> None:
    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)

    async def _cb() -> None:
        return None

    svc.add_job("cronjob", Schedule.cron("0 9 * * MON-FRI"), _cb)
    await asyncio.sleep(0.05)
    payload = svc._serialize_job(svc.get_job("cronjob"))  # type: ignore[arg-type]
    assert payload["schedule"]["expression"] == "0 9 * * MON-FRI"
    assert payload["schedule"]["catch_up"] == "once"
    assert payload["schedule"]["overlap"] == "skip"
    assert payload["next_run_at"]
    await svc.stop()


@pytest.mark.asyncio
async def test_set_alarm_accepts_a_cron_expression() -> None:
    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)

    result = json.loads(
        await svc.execute_tool(
            "set_alarm",
            {"name": "standup", "type": "cron", "cron": "0 9 * * MON-FRI"},
        )
    )
    assert result["status"] == "set"
    assert result["expression"] == "0 9 * * MON-FRI"
    await svc.stop()


@pytest.mark.asyncio
async def test_set_alarm_rejects_a_bad_cron_expression() -> None:
    svc = SchedulerService()
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.get_capability.return_value = None
    await svc.start(resolver)

    result = json.loads(
        await svc.execute_tool(
            "set_alarm",
            {"name": "bad", "type": "cron", "cron": "not a cron"},
        )
    )
    assert "error" in result
    assert "bad" not in svc._jobs
    await svc.stop()


@pytest.mark.asyncio
async def test_reusing_a_job_name_does_not_inherit_stale_fire_history() -> None:
    """A one-shot retires once it has fired. Since job identity is the
    name, a later timer reusing that name must not inherit the old
    history — it would retire before ever firing."""
    svc = SchedulerService()
    svc._storage = _RecordingStorage(  # type: ignore[assignment]
        _fire_state("pizza", datetime.now(UTC) - timedelta(days=1))
    )

    schedule = Schedule.once_after(30)
    await svc._persist_job("pizza", schedule, ScheduledAction(), "u1")

    assert await svc._load_last_fire_at("pizza") is None
    now = datetime.now(schedule.resolve_timezone())
    assert SchedulerService._next_fire_at(schedule, None, now) is not None


@pytest.mark.asyncio
async def test_cancelling_a_job_clears_its_fire_history() -> None:
    svc = SchedulerService()
    svc._storage = _RecordingStorage(  # type: ignore[assignment]
        _fire_state("gone", datetime.now(UTC))
    )
    await svc._unpersist_job("gone")
    assert await svc._load_last_fire_at("gone") is None


@pytest.mark.asyncio
async def test_load_does_not_drop_a_job_whose_end_at_is_future_in_its_own_tz() -> None:
    """`end_at` is naive and belongs to the JOB's timezone, not the
    host's. Comparing it against a bare datetime.now() drops still-valid
    jobs whenever the two zones differ."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    # An hour from now in Pacific, written naive the way it is persisted.
    end_at = (datetime.now(tz) + timedelta(hours=1)).replace(tzinfo=None)

    stored: dict[str, dict[str, Any]] = {
        "scheduler_jobs": {
            "pacific": {
                "id": "pacific",
                "name": "pacific",
                "expression": "@every 60s",
                "timezone": "America/Los_Angeles",
                "end_at": end_at.isoformat(),
                "owner": "u1",
                "action": {"type": "event", "message": "x"},
            }
        }
    }

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data

        async def get(self, coll: str, key: str) -> dict[str, Any] | None:
            return stored.get(coll, {}).get(key)

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    svc = SchedulerService()
    svc._storage = _FakeStorage()  # type: ignore[assignment]
    await svc._load_persisted_jobs()

    assert "pacific" in svc._jobs
    assert "pacific" in stored["scheduler_jobs"]
    await svc.stop()


@pytest.mark.asyncio
async def test_load_still_drops_a_job_whose_end_at_is_genuinely_past() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    end_at = (datetime.now(tz) - timedelta(hours=1)).replace(tzinfo=None)

    stored: dict[str, dict[str, Any]] = {
        "scheduler_jobs": {
            "done": {
                "id": "done",
                "name": "done",
                "expression": "@every 60s",
                "timezone": "America/Los_Angeles",
                "end_at": end_at.isoformat(),
                "owner": "u1",
                "action": {"type": "event", "message": "x"},
            }
        }
    }

    class _FakeStorage:
        async def put(self, coll: str, key: str, data: dict[str, Any]) -> None:
            stored.setdefault(coll, {})[key] = data

        async def get(self, coll: str, key: str) -> dict[str, Any] | None:
            return stored.get(coll, {}).get(key)

        async def delete(self, coll: str, key: str) -> None:
            stored.get(coll, {}).pop(key, None)

        async def query(self, q: Any) -> list[dict[str, Any]]:
            return list(stored.get(q.collection, {}).values())

    svc = SchedulerService()
    svc._storage = _FakeStorage()  # type: ignore[assignment]
    await svc._load_persisted_jobs()

    assert "done" not in svc._jobs
    assert "done" not in stored["scheduler_jobs"]
