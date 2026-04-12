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
from gilbert.interfaces.scheduler import (
    JobState,
    Schedule,
    ScheduledAction,
    ScheduledActionType,
    ScheduleType,
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


def test_schedule_every() -> None:
    s = Schedule.every(30)
    assert s.type == ScheduleType.INTERVAL
    assert s.interval_seconds == 30


def test_schedule_daily() -> None:
    s = Schedule.daily_at(8, 30)
    assert s.type == ScheduleType.DAILY
    assert s.hour == 8
    assert s.minute == 30


def test_schedule_once() -> None:
    s = Schedule.once_after(10)
    assert s.type == ScheduleType.ONCE
    assert s.interval_seconds == 10


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


# --- Tool: set_timer ---


async def test_tool_set_timer(service: SchedulerService) -> None:
    result = await service.execute_tool("set_timer", {
        "name": "pizza",
        "seconds": 300,
        "message": "Pizza is ready!",
    })
    parsed = json.loads(result)
    assert parsed["status"] == "set"
    assert parsed["name"] == "pizza"
    assert service.get_job("pizza") is not None


# --- Tool: set_alarm ---


async def test_tool_set_alarm_interval(service: SchedulerService) -> None:
    result = await service.execute_tool("set_alarm", {
        "name": "check-mail",
        "type": "interval",
        "interval_seconds": 60,
    })
    parsed = json.loads(result)
    assert parsed["status"] == "set"


async def test_tool_set_alarm_daily(service: SchedulerService) -> None:
    result = await service.execute_tool("set_alarm", {
        "name": "standup",
        "type": "daily",
        "hour": 9,
        "minute": 0,
    })
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

    def get_tools(self) -> list[ToolDefinition]:
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
    action, err = svc._build_action_from_args(
        {"tool": "x", "ai_prompt": "y"}
    )
    assert err is not None
    assert "either 'tool' or 'ai_prompt'" in err


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
    action, err = svc._build_action_from_args(
        {"tool": "test_tool", "tool_arguments": "not a dict"}
    )
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
    action, err = svc._build_action_from_args(
        {"ai_prompt": "Announce at 6pm"}
    )
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
    assert call["ai_call"] == "scheduled_action"
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
    action = ScheduledAction(
        type=ScheduledActionType.EVENT, message="pizza done"
    )
    await svc._dispatch_action("pizza-timer", action, owner="u1", event_type="timer.fired")
    assert len(published) == 1
    assert published[0].event_type == "timer.fired"
    assert published[0].data == {"name": "pizza-timer", "message": "pizza done"}


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

    svc._resolver = _resolver_with(
        tools=[fake_tool], acl=_FakeACL(), storage=_FakeStorageSvc()
    )
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
    assert "either" in parsed["error"].lower()
    # Nothing got registered
    assert "bad" not in svc._jobs


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
                "schedule_type": "once",
                "interval_seconds": 60,
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


# --- Config live reload ---


@pytest.mark.asyncio
async def test_on_config_changed_updates_rate_limiter() -> None:
    svc = SchedulerService()
    await svc.on_config_changed(
        {"alarm_ai_max_calls": 7, "alarm_ai_window_seconds": 123}
    )
    status = svc._ai_rate_limiter.status()
    assert status["max_calls"] == 7
    assert status["window_seconds"] == 123
    # Disable via zero
    await svc.on_config_changed(
        {"alarm_ai_max_calls": 0, "alarm_ai_window_seconds": 60}
    )
    assert svc._ai_rate_limiter.try_acquire() is False
