"""Tests for the Deliverable + GoalDependency entity model and the
AgentService CRUD / lifecycle / propagation logic that owns them.

Covers:

- Round-trip dataclasses + enum coverage (Task 1).
- ``create_deliverable`` / ``finalize_deliverable`` /
  ``supersede_deliverable`` lifecycle including the single-READY
  invariant (Task 2).
- Tool dispatchers exposed via ``execute_tool`` (Task 4).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gilbert.interfaces.agent import (
    AssignmentRole,
    Deliverable,
    DeliverableState,
    GoalDependency,
)

# ── Task 1 — entity round-trip + enum coverage ───────────────────────


def test_deliverable_dataclass_round_trip() -> None:
    d = Deliverable(
        id="dlv_1",
        goal_id="goal_1",
        name="spec",
        kind="spec",
        state=DeliverableState.DRAFT,
        produced_by_agent_id="ag_1",
        content_ref="workspace_file:wsf_abc",
        created_at=datetime.now(UTC),
        finalized_at=None,
    )
    assert d.id == "dlv_1"
    assert d.state is DeliverableState.DRAFT
    assert d.finalized_at is None


def test_dependency_dataclass_round_trip() -> None:
    dep = GoalDependency(
        id="dep_1",
        dependent_goal_id="goal_2",
        source_goal_id="goal_1",
        required_deliverable_name="spec",
        satisfied_at=None,
    )
    assert dep.dependent_goal_id == "goal_2"
    assert dep.satisfied_at is None


def test_deliverable_state_enum_values() -> None:
    assert DeliverableState.DRAFT.value == "draft"
    assert DeliverableState.READY.value == "ready"
    assert DeliverableState.OBSOLETE.value == "obsolete"
    # All three are members; nothing extra.
    assert {s.value for s in DeliverableState} == {"draft", "ready", "obsolete"}


# ── Helpers shared with Task 2 / 4 tests ─────────────────────────────


async def _make_two_agents(svc):
    a = await svc.create_agent(owner_user_id="usr_1", name="alpha")
    b = await svc.create_agent(owner_user_id="usr_1", name="bravo")
    return a, b


async def _make_goal_with_driver(svc, *, name="g1", driver_name="alpha"):
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name=name,
        assign_to=[(driver_name, AssignmentRole.DRIVER)],
        assigned_by="user:usr_1",
    )
    return g


# ── Task 2 — lifecycle ───────────────────────────────────────────────


async def test_create_deliverable_starts_draft(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)
    d = await svc.create_deliverable(
        goal_id=g.id,
        name="spec",
        kind="spec",
        produced_by_agent_id=a.id,
        content_ref="inline:hello",
    )
    assert d.state is DeliverableState.DRAFT
    assert d.finalized_at is None
    fetched = await svc.get_deliverable(d.id)
    assert fetched is not None
    assert fetched.state is DeliverableState.DRAFT


async def test_finalize_sets_ready_and_supersedes_prior_ready(
    started_agent_service,
) -> None:
    """Single-READY invariant: finalizing a 2nd same-name deliverable
    flips the prior READY one OBSOLETE in the same operation."""
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)

    d1 = await svc.create_deliverable(
        goal_id=g.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    d1_ready = await svc.finalize_deliverable(d1.id)
    assert d1_ready.state is DeliverableState.READY
    assert d1_ready.finalized_at is not None

    d2 = await svc.create_deliverable(
        goal_id=g.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r2",
    )
    d2_ready = await svc.finalize_deliverable(d2.id)
    assert d2_ready.state is DeliverableState.READY

    # d1 should now be OBSOLETE.
    refreshed = await svc.get_deliverable(d1.id)
    assert refreshed is not None
    assert refreshed.state is DeliverableState.OBSOLETE

    # And listing READY by goal+name returns only d2.
    ready = await svc.list_deliverables(goal_id=g.id, state=DeliverableState.READY)
    assert [r.id for r in ready] == [d2.id]


async def test_supersede_creates_new_draft(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)

    d1 = await svc.create_deliverable(
        goal_id=g.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    obs, new = await svc.supersede_deliverable(
        d1.id, new_content_ref="r2",
    )
    assert obs.state is DeliverableState.OBSOLETE
    assert new.state is DeliverableState.DRAFT
    assert new.name == d1.name
    assert new.goal_id == d1.goal_id
    assert new.content_ref == "r2"


async def test_supersede_with_finalize_creates_ready(
    started_agent_service,
) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)

    d1 = await svc.create_deliverable(
        goal_id=g.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    obs, new = await svc.supersede_deliverable(
        d1.id, new_content_ref="r2", finalize=True,
    )
    assert obs.state is DeliverableState.OBSOLETE
    assert new.state is DeliverableState.READY
    assert new.finalized_at is not None


async def test_finalize_obsolete_raises(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)

    d1 = await svc.create_deliverable(
        goal_id=g.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    obs, _new = await svc.supersede_deliverable(d1.id, new_content_ref="r2")
    assert obs.state is DeliverableState.OBSOLETE
    with pytest.raises(ValueError):
        await svc.finalize_deliverable(obs.id)


# ── Task 4 — tools ───────────────────────────────────────────────────


async def test_deliverable_create_via_tool(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)

    out = await svc.execute_tool(
        "deliverable_create",
        {
            "_agent_id": a.id,
            "goal_id": g.id,
            "name": "spec",
            "kind": "spec",
            "content_ref": "inline:body",
        },
    )
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["state"] == "draft"
    assert "deliverable_id" in payload
    fetched = await svc.get_deliverable(payload["deliverable_id"])
    assert fetched is not None
    assert fetched.produced_by_agent_id == a.id


async def test_deliverable_create_blocks_non_assignee(
    started_agent_service,
) -> None:
    svc = started_agent_service
    a, b = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc, driver_name="alpha")

    out = await svc.execute_tool(
        "deliverable_create",
        {
            "_agent_id": b.id,  # bravo isn't on the goal
            "goal_id": g.id,
            "name": "spec",
            "kind": "spec",
        },
    )
    assert out.startswith("error:")


async def test_deliverable_finalize_producer_or_driver(
    started_agent_service,
) -> None:
    svc = started_agent_service
    a, b = await _make_two_agents(svc)
    g = await svc.create_goal(
        owner_user_id="usr_1",
        name="g",
        assign_to=[
            ("alpha", AssignmentRole.DRIVER),
            ("bravo", AssignmentRole.COLLABORATOR),
        ],
        assigned_by="user:usr_1",
    )

    # bravo (collab) creates the deliverable...
    out = await svc.execute_tool(
        "deliverable_create",
        {
            "_agent_id": b.id,
            "goal_id": g.id,
            "name": "spec",
            "kind": "spec",
            "content_ref": "inline:body",
        },
    )
    payload = json.loads(out)
    did = payload["deliverable_id"]

    # ... and finalizes it (producer path).
    fout = await svc.execute_tool(
        "deliverable_finalize",
        {"_agent_id": b.id, "deliverable_id": did},
    )
    assert "READY" in fout or "ready" in fout
    fetched = await svc.get_deliverable(did)
    assert fetched is not None
    assert fetched.state is DeliverableState.READY

    # Make a second draft, owned by bravo, then finalize via DRIVER (alpha).
    out2 = await svc.execute_tool(
        "deliverable_create",
        {
            "_agent_id": b.id, "goal_id": g.id,
            "name": "spec2", "kind": "report",
        },
    )
    did2 = json.loads(out2)["deliverable_id"]
    fout2 = await svc.execute_tool(
        "deliverable_finalize",
        {"_agent_id": a.id, "deliverable_id": did2},
    )
    assert "READY" in fout2 or "ready" in fout2

    # And a non-producer non-driver is blocked.
    c = await svc.create_agent(owner_user_id="usr_1", name="charlie")
    out3 = await svc.execute_tool(
        "deliverable_create",
        {"_agent_id": b.id, "goal_id": g.id, "name": "spec3", "kind": "spec"},
    )
    did3 = json.loads(out3)["deliverable_id"]
    fout3 = await svc.execute_tool(
        "deliverable_finalize",
        {"_agent_id": c.id, "deliverable_id": did3},
    )
    assert fout3.startswith("error:")


async def test_deliverable_supersede_via_tool(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g = await _make_goal_with_driver(svc)

    d = await svc.create_deliverable(
        goal_id=g.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    out = await svc.execute_tool(
        "deliverable_supersede",
        {
            "_agent_id": a.id,
            "deliverable_id": d.id,
            "new_content_ref": "r2",
            "finalize": True,
        },
    )
    payload = json.loads(out)
    assert payload["obsoleted_id"] == d.id
    new = await svc.get_deliverable(payload["new_id"])
    assert new is not None
    assert new.state is DeliverableState.READY
    assert new.content_ref == "r2"


async def test_goal_add_dependency_via_tool(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g_src = await _make_goal_with_driver(svc, name="src")
    g_dep = await _make_goal_with_driver(svc, name="dep")

    out = await svc.execute_tool(
        "goal_add_dependency",
        {
            "_agent_id": a.id,
            "goal_id": g_dep.id,
            "source_goal_id": g_src.id,
            "required_deliverable_name": "spec",
        },
    )
    payload = json.loads(out)
    assert payload["dependent_goal_id"] == g_dep.id
    deps = await svc.list_goal_dependencies(dependent_goal_id=g_dep.id)
    assert len(deps) == 1


async def test_goal_remove_dependency_via_tool(started_agent_service) -> None:
    svc = started_agent_service
    a, _ = await _make_two_agents(svc)
    g_src = await _make_goal_with_driver(svc, name="src")
    g_dep = await _make_goal_with_driver(svc, name="dep")

    dep = await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    out = await svc.execute_tool(
        "goal_remove_dependency",
        {"_agent_id": a.id, "dependency_id": dep.id},
    )
    assert "removed" in out
    deps = await svc.list_goal_dependencies(dependent_goal_id=g_dep.id)
    assert deps == []
