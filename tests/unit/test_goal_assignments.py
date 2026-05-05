"""Phase 4 — Goals WS RPCs."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.agent import AssignmentRole, GoalStatus
from gilbert.interfaces.auth import UserContext


class _FakeConn:
    def __init__(self, user_id: str, user_level: int = 100) -> None:
        self.user_id = user_id
        self.user_level = user_level
        self.user_ctx = UserContext(
            user_id=user_id,
            email=f"{user_id}@test.local",
            display_name=user_id,
            roles=frozenset(),
            provider="local",
        )


# ── goals.create ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_goals_create_owner_only(started_agent_service: Any) -> None:
    """The created goal is owned by the caller."""
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["goals.create"](
        _FakeConn("usr_1"), {"name": "g1", "description": "d"},
    )
    g = res["goal"]
    assert g["owner_user_id"] == "usr_1"
    assert g["name"] == "g1"
    assert g["status"] == "new"
    assert g["war_room_conversation_id"]


@pytest.mark.asyncio
async def test_ws_goals_create_with_assignees(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="alpha")
    b = await svc.create_agent(owner_user_id="usr_1", name="beta")

    res = await h["goals.create"](_FakeConn("usr_1"), {
        "name": "team-g",
        "assign_to": [
            {"agent_name": "alpha", "role": "driver"},
            {"agent_name": "beta", "role": "collaborator"},
        ],
    })
    g_id = res["goal"]["_id"]
    asgns = await svc.list_assignments(goal_id=g_id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[a.id].role is AssignmentRole.DRIVER
    assert by_agent[b.id].role is AssignmentRole.COLLABORATOR


# ── goals.list / goals.get ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_goals_list_owner_filtered(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    await svc.create_goal(owner_user_id="usr_1", name="ga")
    await svc.create_goal(owner_user_id="usr_2", name="gb")
    res = await h["goals.list"](_FakeConn("usr_1"), {})
    names = {row["name"] for row in res["goals"]}
    assert names == {"ga"}


@pytest.mark.asyncio
async def test_ws_goals_get_cross_owner_blocked(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    g = await svc.create_goal(owner_user_id="usr_1", name="x")
    with pytest.raises(PermissionError):
        await h["goals.get"](_FakeConn("usr_2"), {"goal_id": g.id})


# ── goals.update_status ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_goals_update_status(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    g = await svc.create_goal(owner_user_id="usr_1", name="s")
    res = await h["goals.update_status"](
        _FakeConn("usr_1"), {"goal_id": g.id, "status": "in_progress"},
    )
    assert res["goal"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_ws_goals_update_status_cross_owner_blocked(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    g = await svc.create_goal(owner_user_id="usr_1", name="s")
    with pytest.raises(PermissionError):
        await h["goals.update_status"](
            _FakeConn("usr_2"), {"goal_id": g.id, "status": "in_progress"},
        )


# ── goals.assignments.* ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_goals_assignments_add_remove_handoff(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    b = await svc.create_agent(owner_user_id="usr_1", name="b1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="add-rem-handoff",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )

    # add B as collaborator
    add_res = await h["goals.assignments.add"](_FakeConn("usr_1"), {
        "goal_id": g.id,
        "agent_id": b.id,
        "role": "collaborator",
    })
    assert add_res["assignment"]["role"] == "collaborator"

    # list with goal_id
    list_res = await h["goals.assignments.list"](_FakeConn("usr_1"), {
        "goal_id": g.id,
    })
    agent_ids = {a["agent_id"] for a in list_res["assignments"]}
    assert agent_ids == {a.id, b.id}

    # handoff a -> b
    handoff_res = await h["goals.assignments.handoff"](_FakeConn("usr_1"), {
        "goal_id": g.id,
        "from_agent_id": a.id,
        "to_agent_id": b.id,
        "note": "you got it",
    })
    assert handoff_res["from_assignment"]["role"] == "collaborator"
    assert handoff_res["to_assignment"]["role"] == "driver"

    # remove a
    remove_res = await h["goals.assignments.remove"](_FakeConn("usr_1"), {
        "goal_id": g.id,
        "agent_id": a.id,
    })
    assert remove_res["assignment"]["removed_at"] is not None


@pytest.mark.asyncio
async def test_ws_goals_assignments_list_by_agent(
    started_agent_service: Any,
) -> None:
    """List by agent_id authorizes via the agent (not the goal)."""
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="byagent",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )
    res = await h["goals.assignments.list"](_FakeConn("usr_1"), {
        "agent_id": a.id,
    })
    goal_ids = {row["goal_id"] for row in res["assignments"]}
    assert g.id in goal_ids


# ── goals.summary ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_goals_summary_returns_recent_posts(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="brief",
        description="brief desc",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )
    # Drop a war-room post.
    await svc._exec_goal_post({
        "_agent_id": a.id,
        "goal_id": g.id,
        "body": "kick off",
    })

    res = await h["goals.summary"](_FakeConn("usr_1"), {"goal_id": g.id})
    assert res["goal"]["name"] == "brief"
    assert res["is_dependency_blocked"] is False
    assert len(res["recent_posts"]) == 1
    assert res["recent_posts"][0]["body"] == "kick off"
    assert {x["agent_name"] for x in res["assignees"]} == {"a1"}


@pytest.mark.asyncio
async def test_ws_goals_posts_list(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="postslist",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )
    for body in ("first", "second"):
        await svc._exec_goal_post({
            "_agent_id": a.id,
            "goal_id": g.id,
            "body": body,
        })
    res = await h["goals.posts.list"](_FakeConn("usr_1"), {
        "goal_id": g.id,
        "limit": 50,
    })
    bodies = [p["body"] for p in res["posts"]]
    assert bodies == ["first", "second"]


# ── Cross-owner blocked across the board ─────────────────────────────


@pytest.mark.asyncio
async def test_ws_cross_owner_blocked(started_agent_service: Any) -> None:
    """Every per-goal handler rejects callers not owning the goal."""
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="a1")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="closed",
        assign_to=[(a.name, AssignmentRole.DRIVER)],
    )
    intruder = _FakeConn("usr_2")

    with pytest.raises(PermissionError):
        await h["goals.get"](intruder, {"goal_id": g.id})
    with pytest.raises(PermissionError):
        await h["goals.update_status"](intruder, {"goal_id": g.id, "status": "complete"})
    with pytest.raises(PermissionError):
        await h["goals.assignments.list"](intruder, {"goal_id": g.id})
    with pytest.raises(PermissionError):
        await h["goals.assignments.add"](intruder, {
            "goal_id": g.id, "agent_id": a.id, "role": "collaborator",
        })
    with pytest.raises(PermissionError):
        await h["goals.assignments.remove"](intruder, {
            "goal_id": g.id, "agent_id": a.id,
        })
    with pytest.raises(PermissionError):
        await h["goals.assignments.handoff"](intruder, {
            "goal_id": g.id, "from_agent_id": a.id, "to_agent_id": a.id,
        })
    with pytest.raises(PermissionError):
        await h["goals.summary"](intruder, {"goal_id": g.id})
    with pytest.raises(PermissionError):
        await h["goals.posts.list"](intruder, {"goal_id": g.id})

    # Goal status untouched.
    fresh = await svc.get_goal(g.id)
    assert fresh.status is GoalStatus.NEW
