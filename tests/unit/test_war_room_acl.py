"""Phase 4 — War-room ACL: assignee-only post + driver-only status +
assignee summary."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gilbert.core.services.agent import _AI_CONVERSATIONS_COLLECTION
from gilbert.interfaces.agent import AssignmentRole, GoalStatus


@pytest.mark.asyncio
async def test_non_assignee_post_blocked(started_agent_service: Any) -> None:
    svc = started_agent_service
    driver = await svc.create_agent(owner_user_id="usr_1", name="driver")
    intruder = await svc.create_agent(owner_user_id="usr_1", name="intruder")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="locked",
        assign_to=[(driver.name, AssignmentRole.DRIVER)],
    )
    res = await svc._exec_goal_post({
        "_agent_id": intruder.id,
        "goal_id": g.id,
        "body": "shouldn't be here",
    })
    assert res.startswith("error:")
    conv = await svc._storage.get(_AI_CONVERSATIONS_COLLECTION, g.war_room_conversation_id)
    assert conv["messages"] == []


@pytest.mark.asyncio
async def test_non_driver_status_blocked(started_agent_service: Any) -> None:
    svc = started_agent_service
    driver = await svc.create_agent(owner_user_id="usr_1", name="driver")
    collab = await svc.create_agent(owner_user_id="usr_1", name="collab")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="g",
        assign_to=[
            (driver.name, AssignmentRole.DRIVER),
            (collab.name, AssignmentRole.COLLABORATOR),
        ],
    )
    res = await svc._exec_goal_status({
        "_agent_id": collab.id,
        "goal_id": g.id,
        "new_status": "complete",
    })
    assert res.startswith("error:")
    fresh = await svc.get_goal(g.id)
    assert fresh.status is GoalStatus.NEW


@pytest.mark.asyncio
async def test_assignee_can_summary(started_agent_service: Any) -> None:
    svc = started_agent_service
    driver = await svc.create_agent(owner_user_id="usr_1", name="driver")
    collab = await svc.create_agent(owner_user_id="usr_1", name="collab")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="g",
        description="desc",
        assign_to=[
            (driver.name, AssignmentRole.DRIVER),
            (collab.name, AssignmentRole.COLLABORATOR),
        ],
    )
    raw = await svc._exec_goal_summary({
        "_agent_id": collab.id,
        "goal_id": g.id,
    })
    summary = json.loads(raw)
    assert summary["name"] == "g"
    assert {asg["agent_name"] for asg in summary["assignees"]} == {"driver", "collab"}


@pytest.mark.asyncio
async def test_cross_owner_blocked_on_post(started_agent_service: Any) -> None:
    """An agent owned by user A can't post into user B's goal."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_a", name="a1")
    g = await svc.create_goal(
        owner_user_id="usr_b",
        name="other-user",
    )
    res = await svc._exec_goal_post({
        "_agent_id": a.id,
        "goal_id": g.id,
        "body": "across the fence",
    })
    assert res.startswith("error:")
