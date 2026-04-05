"""Tests for SchedulerService — job lifecycle, timers, alarms."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.services.scheduler import SchedulerService
from gilbert.interfaces.scheduler import JobState, Schedule, ScheduleType
from gilbert.interfaces.service import ServiceResolver


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
    assert config.doorbell.doorbell_names == {}
