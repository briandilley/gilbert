"""Phase 4 — Goal tools (goal_create, goal_post, goal_status, goal_handoff,
goal_summary, goal_assign, goal_unassign)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gilbert.core.services.agent import (
    _AGENT_INBOX_SIGNALS_COLLECTION,
    _AI_CONVERSATIONS_COLLECTION,
)
from gilbert.interfaces.agent import AssignmentRole, GoalStatus
from gilbert.interfaces.storage import Filter, FilterOp, Query

# ── goal_create ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_create_via_tool(started_agent_service: Any) -> None:
    """goal_create returns a goal_id and the war-room conv exists."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")

    raw = await svc._exec_goal_create({
        "_agent_id": a.id,
        "name": "ship-it",
        "description": "phase 4 final push",
    })
    out = json.loads(raw)
    goal_id = out["goal_id"]
    conv_id = out["war_room_conversation_id"]
    assert goal_id and conv_id

    g = await svc.get_goal(goal_id)
    assert g is not None
    assert g.owner_user_id == "usr_1"
    assert g.name == "ship-it"
    assert g.status is GoalStatus.NEW

    # War room conv exists.
    conv = await svc._storage.get(_AI_CONVERSATIONS_COLLECTION, conv_id)
    assert conv is not None
    assert conv["metadata"]["goal_id"] == goal_id


@pytest.mark.asyncio
async def test_goal_create_with_assignees_via_tool(
    started_agent_service: Any,
) -> None:
    """goal_create with assign_to resolves peer names; first → DRIVER."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b2")

    raw = await svc._exec_goal_create({
        "_agent_id": a.id,
        "name": "team-goal",
        "assign_to": [
            {"agent_name": "b2", "role": "collaborator"},
        ],
    })
    out = json.loads(raw)
    goal_id = out["goal_id"]

    asgns = await svc.list_assignments(goal_id=goal_id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[b.id].role is AssignmentRole.DRIVER  # first → DRIVER


@pytest.mark.asyncio
async def test_goal_create_assign_to_cross_owner_blocked(
    started_agent_service: Any,
) -> None:
    """goal_create rejects assign_to entries that aren't peers of the caller."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    await svc.create_agent(owner_user_id="usr_b", name="other")

    raw = await svc._exec_goal_create({
        "_agent_id": a.id,
        "name": "x",
        "assign_to": ["other"],
    })
    assert raw.startswith("error:")


# ── goal_post ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_post_writes_to_war_room(started_agent_service: Any) -> None:
    """An assignee can post; the message lands in the war-room conv."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="warroom-test",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )
    res = await svc._exec_goal_post({
        "_agent_id": a.id,
        "goal_id": g.id,
        "body": "hello team",
    })
    assert "posted to war room" in res

    conv = await svc._storage.get(_AI_CONVERSATIONS_COLLECTION, g.war_room_conversation_id)
    assert conv is not None
    msgs = conv["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello team"
    assert msgs[0]["metadata"]["sender"]["kind"] == "agent"
    assert msgs[0]["metadata"]["sender"]["id"] == a.id
    assert msgs[0]["metadata"]["sender"]["name"] == a.name


@pytest.mark.asyncio
async def test_goal_post_non_assignee_blocked(started_agent_service: Any) -> None:
    """A non-assignee gets an error and the conv stays empty."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    other = await svc.create_agent(owner_user_id="usr_1", name="other")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="closed",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )

    res = await svc._exec_goal_post({
        "_agent_id": other.id,
        "goal_id": g.id,
        "body": "let me in",
    })
    assert res.startswith("error:")
    conv = await svc._storage.get(_AI_CONVERSATIONS_COLLECTION, g.war_room_conversation_id)
    assert conv["messages"] == []


@pytest.mark.asyncio
async def test_goal_post_mentions_signal_targets(started_agent_service: Any) -> None:
    """Each mentioned peer name produces an InboxSignal row."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b1")
    c = await svc.create_agent(owner_user_id="usr_1", name="c1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="mentions",
        assign_to=[
            (a.name, AssignmentRole.DRIVER),
            (b.name, AssignmentRole.COLLABORATOR),
            (c.name, AssignmentRole.COLLABORATOR),
        ],
    )

    # Hold b/c busy so the mention signals queue rather than fire a run.
    svc._running_agents.add(b.id)
    svc._running_agents.add(c.id)
    try:
        res = await svc._exec_goal_post({
            "_agent_id": a.id,
            "goal_id": g.id,
            "body": "heads up team",
            "mention": ["b1", "c1"],
        })
    finally:
        svc._running_agents.discard(b.id)
        svc._running_agents.discard(c.id)
    assert "mentions=2" in res

    rows_b = await svc._storage.query(
        Query(
            collection=_AGENT_INBOX_SIGNALS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=b.id)],
        )
    )
    rows_c = await svc._storage.query(
        Query(
            collection=_AGENT_INBOX_SIGNALS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=c.id)],
        )
    )
    # B should have one war_room_mention signal.
    mention_b = [r for r in rows_b if r.get("metadata", {}).get("kind") == "war_room_mention"]
    mention_c = [r for r in rows_c if r.get("metadata", {}).get("kind") == "war_room_mention"]
    assert len(mention_b) == 1
    assert len(mention_c) == 1
    assert "mentions" in mention_b[0]["body"].lower() or "mentioned in war room" in mention_b[0]["body"]


# ── goal_status ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_status_driver_only(started_agent_service: Any) -> None:
    """A non-DRIVER assignee gets an error; DRIVER succeeds."""
    svc = started_agent_service
    driver = await svc.create_agent(owner_user_id="usr_1", name="driver")
    collab = await svc.create_agent(owner_user_id="usr_1", name="collab")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="status-test",
        assign_to=[
            (driver.name, AssignmentRole.DRIVER),
            (collab.name, AssignmentRole.COLLABORATOR),
        ],
    )

    # Non-driver blocked.
    bad = await svc._exec_goal_status({
        "_agent_id": collab.id,
        "goal_id": g.id,
        "new_status": "in_progress",
    })
    assert bad.startswith("error:")
    fresh = await svc.get_goal(g.id)
    assert fresh.status is GoalStatus.NEW

    # Driver succeeds.
    ok = await svc._exec_goal_status({
        "_agent_id": driver.id,
        "goal_id": g.id,
        "new_status": "in_progress",
    })
    assert "in_progress" in ok
    fresh = await svc.get_goal(g.id)
    assert fresh.status is GoalStatus.IN_PROGRESS


# ── goal_handoff ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_handoff_via_tool(started_agent_service: Any) -> None:
    """DRIVER A hands off to B; both rows reflect the new roles."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="handoff",
        assign_to=[
            (a.name, AssignmentRole.DRIVER),
            (b.name, AssignmentRole.COLLABORATOR),
        ],
    )

    res = await svc._exec_goal_handoff({
        "_agent_id": a.id,
        "goal_id": g.id,
        "target_name": "b1",
        "note": "switching",
    })
    assert "handed off" in res

    asgns = await svc.list_assignments(goal_id=g.id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[a.id].role is AssignmentRole.COLLABORATOR
    assert by_agent[b.id].role is AssignmentRole.DRIVER


@pytest.mark.asyncio
async def test_goal_handoff_non_driver_blocked(started_agent_service: Any) -> None:
    """A non-DRIVER cannot hand off."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b1")
    c = await svc.create_agent(owner_user_id="usr_1", name="c1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="x",
        assign_to=[
            (a.name, AssignmentRole.DRIVER),
            (b.name, AssignmentRole.COLLABORATOR),
            (c.name, AssignmentRole.COLLABORATOR),
        ],
    )
    res = await svc._exec_goal_handoff({
        "_agent_id": b.id,
        "goal_id": g.id,
        "target_name": "c1",
    })
    assert res.startswith("error:")
    asgns = await svc.list_assignments(goal_id=g.id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[a.id].role is AssignmentRole.DRIVER


# ── goal_summary ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_summary_assignee_only(started_agent_service: Any) -> None:
    """An assignee gets JSON; a non-assignee gets an error."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    other = await svc.create_agent(owner_user_id="usr_1", name="other")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="brief",
        description="test goal",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )
    # Add a war-room post so recent_posts is non-empty.
    await svc._exec_goal_post({
        "_agent_id": a.id,
        "goal_id": g.id,
        "body": "kicking off",
    })

    raw = await svc._exec_goal_summary({
        "_agent_id": a.id,
        "goal_id": g.id,
    })
    # Should be JSON, not an error.
    summary = json.loads(raw)
    assert summary["name"] == "brief"
    assert summary["status"] == "new"
    assert summary["is_dependency_blocked"] is False
    assert any(asg["agent_name"] == "a1" and asg["role"] == "driver"
               for asg in summary["assignees"])
    assert summary["recent_posts"]
    assert summary["recent_posts"][0]["author_name"] == "a1"

    # Non-assignee blocked.
    bad = await svc._exec_goal_summary({
        "_agent_id": other.id,
        "goal_id": g.id,
    })
    assert bad.startswith("error:")


# ── goal_assign / goal_unassign ──────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_assign_driver_only(started_agent_service: Any) -> None:
    """Only the DRIVER may assign new agents."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b1")
    c = await svc.create_agent(owner_user_id="usr_1", name="c1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="g",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )

    bad = await svc._exec_goal_assign({
        "_agent_id": b.id,  # not a driver (and not assigned)
        "goal_id": g.id,
        "agent_name": "c1",
        "role": "collaborator",
    })
    assert bad.startswith("error:")

    ok = await svc._exec_goal_assign({
        "_agent_id": a.id,
        "goal_id": g.id,
        "agent_name": "c1",
        "role": "collaborator",
    })
    assert "assigned" in ok
    asgns = await svc.list_assignments(goal_id=g.id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[c.id].role is AssignmentRole.COLLABORATOR


@pytest.mark.asyncio
async def test_goal_unassign_self_or_driver(started_agent_service: Any) -> None:
    """Self-unassign always works; non-DRIVER can't unassign others."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b1")
    c = await svc.create_agent(owner_user_id="usr_1", name="c1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="u",
        assign_to=[
            (a.name, AssignmentRole.DRIVER),
            (b.name, AssignmentRole.COLLABORATOR),
            (c.name, AssignmentRole.COLLABORATOR),
        ],
    )

    # B unassigning C → blocked (B is not driver).
    bad = await svc._exec_goal_unassign({
        "_agent_id": b.id,
        "goal_id": g.id,
        "agent_name": "c1",
    })
    assert bad.startswith("error:")

    # B unassigning self → ok.
    ok_self = await svc._exec_goal_unassign({
        "_agent_id": b.id,
        "goal_id": g.id,
        "agent_name": "b1",
    })
    assert "unassigned" in ok_self

    # Driver A unassigning C → ok.
    ok_drv = await svc._exec_goal_unassign({
        "_agent_id": a.id,
        "goal_id": g.id,
        "agent_name": "c1",
    })
    assert "unassigned" in ok_drv
