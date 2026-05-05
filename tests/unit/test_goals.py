"""Phase 4 — Goals tests: entity round-trip + AgentService CRUD + assignments.

Service-level tests use the shared ``started_agent_service`` fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gilbert.core.services.agent import (
    _GOAL_ASSIGNMENTS_COLLECTION,
    _GOALS_COLLECTION,
)
from gilbert.interfaces.agent import (
    AssignmentRole,
    Goal,
    GoalAssignment,
    GoalStatus,
)
from gilbert.interfaces.events import Event
from gilbert.interfaces.storage import Filter, FilterOp, Query


# ── Task 1: entity round-trip + enum coverage ────────────────────────


def test_goal_status_enum_values() -> None:
    assert GoalStatus.NEW.value == "new"
    assert GoalStatus.IN_PROGRESS.value == "in_progress"
    assert GoalStatus.BLOCKED.value == "blocked"
    assert GoalStatus.COMPLETE.value == "complete"
    assert GoalStatus.CANCELLED.value == "cancelled"


def test_assignment_role_enum_values() -> None:
    assert AssignmentRole.DRIVER.value == "driver"
    assert AssignmentRole.COLLABORATOR.value == "collaborator"
    assert AssignmentRole.REVIEWER.value == "reviewer"


def test_goal_dataclass_round_trip() -> None:
    now = datetime.now(UTC)
    g = Goal(
        id="goal_1",
        owner_user_id="usr_1",
        name="ship phase 4",
        description="multi-agent goals",
        status=GoalStatus.NEW,
        war_room_conversation_id="conv_1",
        cost_cap_usd=None,
        lifetime_cost_usd=0.0,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    assert g.status is GoalStatus.NEW
    assert g.completed_at is None


def test_goal_assignment_dataclass_round_trip() -> None:
    now = datetime.now(UTC)
    ga = GoalAssignment(
        id="ga_1",
        goal_id="goal_1",
        agent_id="ag_1",
        role=AssignmentRole.DRIVER,
        assigned_at=now,
        assigned_by="user:usr_1",
        removed_at=None,
        handoff_note="",
    )
    assert ga.role is AssignmentRole.DRIVER
    assert ga.removed_at is None


# ── Task 2: AgentService CRUD + assignments + war-room conv ─────────


@pytest.mark.asyncio
async def test_create_goal_creates_war_room(started_agent_service: Any) -> None:
    """create_goal writes a war-room ai_conversations row keyed by uuid,
    stamps it onto goal.war_room_conversation_id."""
    svc = started_agent_service
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="ship-it",
        description="phase 4",
        assigned_by="user:usr_1",
    )
    assert g.war_room_conversation_id
    row = await svc._storage.get("ai_conversations", g.war_room_conversation_id)
    assert row is not None
    assert row["title"] == "ship-it"
    assert row["user_id"] == "usr_1"
    assert row["messages"] == []
    assert row["metadata"]["goal_id"] == g.id
    assert row["metadata"]["kind"] == "war_room"


@pytest.mark.asyncio
async def test_create_goal_with_assignees(started_agent_service: Any) -> None:
    """First assignee → DRIVER (when none specified); rest as specified."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_1", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_1", name="a2")
    a3 = await svc.create_agent(owner_user_id="usr_1", name="a3")

    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="grp-goal",
        assign_to=[
            (a1.name, AssignmentRole.COLLABORATOR),  # will be promoted to DRIVER
            (a2.name, AssignmentRole.COLLABORATOR),
            (a3.name, AssignmentRole.REVIEWER),
        ],
    )
    asgns = await svc.list_assignments(goal_id=g.id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[a1.id].role is AssignmentRole.DRIVER
    assert by_agent[a2.id].role is AssignmentRole.COLLABORATOR
    assert by_agent[a3.id].role is AssignmentRole.REVIEWER


@pytest.mark.asyncio
async def test_create_goal_with_assignees_explicit_driver(
    started_agent_service: Any,
) -> None:
    """If a DRIVER is specified, the first assignee is NOT promoted."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_1", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_1", name="a2")

    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="explicit",
        assign_to=[
            (a1.name, AssignmentRole.COLLABORATOR),
            (a2.name, AssignmentRole.DRIVER),
        ],
    )
    asgns = await svc.list_assignments(goal_id=g.id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[a1.id].role is AssignmentRole.COLLABORATOR
    assert by_agent[a2.id].role is AssignmentRole.DRIVER


@pytest.mark.asyncio
async def test_assign_agent_idempotent(started_agent_service: Any) -> None:
    """Same agent + same role → returns existing row, no second row added."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_1", name="a1")
    g = await svc.create_goal(owner_user_id="usr_1", name="g1")

    first = await svc.assign_agent_to_goal(
        goal_id=g.id,
        agent_id=a1.id,
        role=AssignmentRole.DRIVER,
        assigned_by="user:usr_1",
    )
    second = await svc.assign_agent_to_goal(
        goal_id=g.id,
        agent_id=a1.id,
        role=AssignmentRole.DRIVER,
        assigned_by="user:usr_1",
    )
    assert first.id == second.id

    rows = await svc._storage.query(
        Query(
            collection=_GOAL_ASSIGNMENTS_COLLECTION,
            filters=[
                Filter(field="goal_id", op=FilterOp.EQ, value=g.id),
                Filter(field="agent_id", op=FilterOp.EQ, value=a1.id),
            ],
        )
    )
    active = [r for r in rows if not r.get("removed_at")]
    assert len(active) == 1


@pytest.mark.asyncio
async def test_handoff_swaps_driver(started_agent_service: Any) -> None:
    """A=DRIVER, B=COLLABORATOR; handoff(A→B) → A=COLLABORATOR, B=DRIVER."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a")
    b = await svc.create_agent(owner_user_id="usr_1", name="b")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="handoff-test",
        assign_to=[
            (a.name, AssignmentRole.DRIVER),
            (b.name, AssignmentRole.COLLABORATOR),
        ],
    )

    from_a, to_b = await svc.handoff_goal(
        goal_id=g.id,
        from_agent_id=a.id,
        to_agent_id=b.id,
        note="passing the baton",
    )
    assert from_a.role is AssignmentRole.COLLABORATOR
    assert to_b.role is AssignmentRole.DRIVER
    assert from_a.handoff_note == "passing the baton"
    assert to_b.handoff_note == "passing the baton"

    asgns = await svc.list_assignments(goal_id=g.id, active_only=True)
    by_agent = {x.agent_id: x for x in asgns}
    assert by_agent[a.id].role is AssignmentRole.COLLABORATOR
    assert by_agent[b.id].role is AssignmentRole.DRIVER


@pytest.mark.asyncio
async def test_unassign_marks_removed_at(started_agent_service: Any) -> None:
    """Unassign sets removed_at — the row is preserved."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="a")
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="unassign-test",
        assign_to=[(a.name, AssignmentRole.COLLABORATOR)],
    )
    removed = await svc.unassign_agent_from_goal(goal_id=g.id, agent_id=a.id)
    assert removed.removed_at is not None

    # Row preserved, just inactive.
    rows = await svc._storage.query(
        Query(
            collection=_GOAL_ASSIGNMENTS_COLLECTION,
            filters=[Filter(field="goal_id", op=FilterOp.EQ, value=g.id)],
        )
    )
    assert len(rows) == 1

    # active_only=True excludes it.
    active = await svc.list_assignments(goal_id=g.id, active_only=True)
    assert active == []


@pytest.mark.asyncio
async def test_status_event_published(started_agent_service: Any) -> None:
    """update_goal_status fires goal.status.changed."""
    svc = started_agent_service
    received: list[Event] = []

    async def handler(ev: Event) -> None:
        received.append(ev)

    svc._event_bus.subscribe("goal.status.changed", handler)
    g = await svc.create_goal(owner_user_id="usr_1", name="status-event")
    await svc.update_goal_status(g.id, GoalStatus.IN_PROGRESS)

    assert any(
        ev.event_type == "goal.status.changed"
        and ev.data.get("goal_id") == g.id
        and ev.data.get("status") == "in_progress"
        for ev in received
    )


@pytest.mark.asyncio
async def test_assignment_event_published(started_agent_service: Any) -> None:
    """assign_agent_to_goal and unassign fire goal.assignment.changed."""
    svc = started_agent_service
    received: list[Event] = []

    async def handler(ev: Event) -> None:
        received.append(ev)

    svc._event_bus.subscribe("goal.assignment.changed", handler)
    a = await svc.create_agent(owner_user_id="usr_1", name="a")
    g = await svc.create_goal(owner_user_id="usr_1", name="asgn-event")
    await svc.assign_agent_to_goal(
        goal_id=g.id, agent_id=a.id, role=AssignmentRole.DRIVER,
        assigned_by="user:usr_1",
    )
    await svc.unassign_agent_from_goal(goal_id=g.id, agent_id=a.id)
    assert sum(1 for ev in received if ev.event_type == "goal.assignment.changed") >= 2


@pytest.mark.asyncio
async def test_list_goals_filtered_by_owner(started_agent_service: Any) -> None:
    svc = started_agent_service
    await svc.create_goal(owner_user_id="usr_a", name="ga1")
    await svc.create_goal(owner_user_id="usr_a", name="ga2")
    await svc.create_goal(owner_user_id="usr_b", name="gb1")

    aas = await svc.list_goals(owner_user_id="usr_a")
    assert {g.name for g in aas} == {"ga1", "ga2"}
    bs = await svc.list_goals(owner_user_id="usr_b")
    assert {g.name for g in bs} == {"gb1"}


@pytest.mark.asyncio
async def test_get_goal_round_trip(started_agent_service: Any) -> None:
    svc = started_agent_service
    g = await svc.create_goal(owner_user_id="usr_1", name="rt")
    fetched = await svc.get_goal(g.id)
    assert fetched is not None
    assert fetched.id == g.id
    assert fetched.name == "rt"
    assert fetched.status is GoalStatus.NEW

    # Storage row sanity.
    row = await svc._storage.get(_GOALS_COLLECTION, g.id)
    assert row is not None
    assert row["status"] == "new"


# ── Task 5: System prompt active-assignments block (added in Task 5)


