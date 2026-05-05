"""Tests for heartbeat trigger registration (Task 10).

Verifies that AgentService arms/disarms scheduler jobs for agents with
heartbeat_enabled=True, and that the fired callback invokes
_run_agent_internal with triggered_by='heartbeat'.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_heartbeat_trigger_registered_on_agent_create(
    started_agent_service: Any,
) -> None:
    """Creating an agent with heartbeat_enabled=True registers a scheduler job."""
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="hb-on",
        heartbeat_enabled=True,
        heartbeat_interval_s=600,
    )
    assert any(name == f"heartbeat_{a.id}" for name in svc._scheduler.added_jobs)


@pytest.mark.asyncio
async def test_heartbeat_disabled_skips_registration(
    started_agent_service: Any,
) -> None:
    """Creating an agent with heartbeat_enabled=False does not register a job."""
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="hb-off",
        heartbeat_enabled=False,
    )
    assert all(name != f"heartbeat_{a.id}" for name in svc._scheduler.added_jobs)


@pytest.mark.asyncio
async def test_heartbeat_unregistered_on_delete(
    started_agent_service: Any,
) -> None:
    """Deleting an agent with heartbeat_enabled=True removes its scheduler job."""
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="hb-del",
        heartbeat_enabled=True,
    )
    await svc.delete_agent(a.id)
    assert f"heartbeat_{a.id}" in svc._scheduler.removed_jobs


@pytest.mark.asyncio
async def test_heartbeat_triggered_run_uses_checklist_in_prompt(
    started_agent_service: Any,
) -> None:
    """The heartbeat callback passes the agent's checklist into the system prompt."""
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="hb-checklist",
        heartbeat_enabled=True,
        heartbeat_checklist="check the news",
    )
    # Manually invoke the heartbeat handler the way the scheduler would.
    await svc._on_heartbeat_fired(a.id)

    last_call = svc._ai.last_call_kwargs
    assert "check the news" in last_call.get("system_prompt", "")
