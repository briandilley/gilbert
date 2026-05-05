"""Tests for ``GoalDependency`` lifecycle, propagation on
``deliverable_finalize``, and the ``goals.dependencies.*`` /
``deliverables.*`` WS RPC handlers.

Most tests use the shared ``started_agent_service`` fixture and the
sqlite-backed storage from ``conftest.py``. WS RPC tests build a minimal
fake connection that supplies ``user_id`` / ``user_level``.
"""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.agent import AssignmentRole
from gilbert.interfaces.storage import Filter, FilterOp, Query


async def _list_signals(svc: Any, agent_id: str) -> list[str]:
    """Return signal_kinds for all persisted inbox rows for an agent."""
    rows = await svc._storage.query(
        Query(
            collection="agent_inbox_signals",
            filters=[
                Filter(field="agent_id", op=FilterOp.EQ, value=agent_id),
            ],
        )
    )
    return [r.get("signal_kind", "") for r in rows]


class _FakeConn:
    """Minimal stand-in for the WS connection object expected by the
    handlers — provides ``user_id`` / ``user_level`` attributes only."""

    def __init__(self, *, user_id: str, user_level: int = 100) -> None:
        self.user_id = user_id
        self.user_level = user_level


# ── Helpers ──────────────────────────────────────────────────────────


async def _bootstrap_two_goals(svc, *, owner: str = "usr_1"):
    a = await svc.create_agent(owner_user_id=owner, name="alpha")
    b = await svc.create_agent(owner_user_id=owner, name="bravo")
    g_src = await svc.create_goal(
        owner_user_id=owner, name="src",
        assign_to=[("alpha", AssignmentRole.DRIVER)],
        assigned_by=f"user:{owner}",
    )
    g_dep = await svc.create_goal(
        owner_user_id=owner, name="dep",
        assign_to=[
            ("bravo", AssignmentRole.DRIVER),
            ("alpha", AssignmentRole.COLLABORATOR),
        ],
        assigned_by=f"user:{owner}",
    )
    return a, b, g_src, g_dep


# ── Dependency basics ────────────────────────────────────────────────


async def test_add_dependency_idempotent(started_agent_service: Any) -> None:
    svc = started_agent_service
    _, _, g_src, g_dep = await _bootstrap_two_goals(svc)

    d1 = await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    d2 = await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    assert d1.id == d2.id
    deps = await svc.list_goal_dependencies(dependent_goal_id=g_dep.id)
    assert len(deps) == 1


async def test_finalize_satisfies_unsatisfied_deps(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    a, _, g_src, g_dep = await _bootstrap_two_goals(svc)

    dep = await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    assert dep.satisfied_at is None

    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    await svc.finalize_deliverable(d.id)

    refreshed = await svc.list_goal_dependencies(
        dependent_goal_id=g_dep.id,
    )
    assert len(refreshed) == 1
    assert refreshed[0].satisfied_at is not None


async def test_finalize_signals_non_reviewer_assignees(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    a, b, g_src, g_dep = await _bootstrap_two_goals(svc)

    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    await svc.finalize_deliverable(d.id)

    # bravo (DRIVER on dep) and alpha (COLLABORATOR on dep) should both
    # have a deliverable_ready signal persisted.
    assert "deliverable_ready" in await _list_signals(svc, b.id)
    assert "deliverable_ready" in await _list_signals(svc, a.id)


async def test_finalize_does_not_signal_reviewers(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="alpha")
    rev = await svc.create_agent(owner_user_id="usr_1", name="revvy")

    g_src = await svc.create_goal(
        owner_user_id="usr_1", name="src",
        assign_to=[("alpha", AssignmentRole.DRIVER)],
        assigned_by="user:usr_1",
    )
    g_dep = await svc.create_goal(
        owner_user_id="usr_1", name="dep",
        assign_to=[
            ("alpha", AssignmentRole.DRIVER),
            ("revvy", AssignmentRole.REVIEWER),
        ],
        assigned_by="user:usr_1",
    )
    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    # Snapshot reviewer signals BEFORE finalizing.
    rev_pre = await _list_signals(svc, rev.id)

    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    await svc.finalize_deliverable(d.id)

    rev_post = await _list_signals(svc, rev.id)
    # Reviewer count of deliverable_ready signals didn't change.
    assert rev_post.count("deliverable_ready") == rev_pre.count(
        "deliverable_ready"
    )


async def test_add_dependency_immediately_satisfied_when_source_already_ready(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    a, b, g_src, g_dep = await _bootstrap_two_goals(svc)

    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    await svc.finalize_deliverable(d.id)

    # Snapshot bravo's signals BEFORE adding the dep.
    pre = await _list_signals(svc, b.id)

    dep = await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    assert dep.satisfied_at is not None

    post = await _list_signals(svc, b.id)
    # bravo got at least one new deliverable_ready signal.
    assert post.count("deliverable_ready") > pre.count("deliverable_ready")


async def test_obsolete_does_not_satisfy(started_agent_service: Any) -> None:
    """Marking a deliverable OBSOLETE (without finalizing a replacement)
    must NOT satisfy any dependencies."""
    svc = started_agent_service
    a, _, g_src, g_dep = await _bootstrap_two_goals(svc)

    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    # Supersede WITHOUT finalize → new is DRAFT, old is OBSOLETE,
    # nothing satisfies the dep.
    await svc.supersede_deliverable(d.id, new_content_ref="r2")
    deps = await svc.list_goal_dependencies(dependent_goal_id=g_dep.id)
    assert deps[0].satisfied_at is None


async def test_goal_summary_reflects_dependency_blocked(
    started_agent_service: Any,
) -> None:
    """Phase 4's ``goal_summary`` ``is_dependency_blocked`` flag must
    reflect unsatisfied dependencies on the goal."""
    import json as _json

    svc = started_agent_service
    a, b, g_src, g_dep = await _bootstrap_two_goals(svc)

    # No deps yet.
    out = await svc.execute_tool(
        "goal_summary", {"_agent_id": b.id, "goal_id": g_dep.id},
    )
    payload = _json.loads(out)
    assert payload["is_dependency_blocked"] is False

    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    out = await svc.execute_tool(
        "goal_summary", {"_agent_id": b.id, "goal_id": g_dep.id},
    )
    payload = _json.loads(out)
    assert payload["is_dependency_blocked"] is True

    # Finalize → satisfied → not blocked.
    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id, content_ref="r1",
    )
    await svc.finalize_deliverable(d.id)
    out = await svc.execute_tool(
        "goal_summary", {"_agent_id": b.id, "goal_id": g_dep.id},
    )
    payload = _json.loads(out)
    assert payload["is_dependency_blocked"] is False


# ── WS RPC handlers ──────────────────────────────────────────────────


async def test_ws_deliverables_owner_only(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="alpha")
    g = await svc.create_goal(
        owner_user_id="usr_1", name="g",
        assign_to=[("alpha", AssignmentRole.DRIVER)],
        assigned_by="user:usr_1",
    )

    handlers = svc.get_ws_handlers()
    list_h = handlers["deliverables.list"]
    create_h = handlers["deliverables.create"]

    # Owner can list.
    out = await list_h(_FakeConn(user_id="usr_1"), {"goal_id": g.id})
    assert "deliverables" in out

    # Other user blocked.
    with pytest.raises(PermissionError):
        await list_h(_FakeConn(user_id="usr_2"), {"goal_id": g.id})
    with pytest.raises(PermissionError):
        await create_h(
            _FakeConn(user_id="usr_2"),
            {
                "goal_id": g.id, "name": "spec", "kind": "spec",
                "produced_by_agent_id": a.id,
            },
        )


async def test_ws_deliverables_create_finalize_round_trip(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="alpha")
    g = await svc.create_goal(
        owner_user_id="usr_1", name="g",
        assign_to=[("alpha", AssignmentRole.DRIVER)],
        assigned_by="user:usr_1",
    )
    handlers = svc.get_ws_handlers()
    conn = _FakeConn(user_id="usr_1")

    created = await handlers["deliverables.create"](
        conn,
        {
            "goal_id": g.id, "name": "spec", "kind": "spec",
            "produced_by_agent_id": a.id, "content_ref": "r1",
        },
    )
    did = created["deliverable"]["_id"]
    assert created["deliverable"]["state"] == "draft"

    finalized = await handlers["deliverables.finalize"](
        conn, {"deliverable_id": did},
    )
    assert finalized["deliverable"]["state"] == "ready"

    listed = await handlers["deliverables.list"](
        conn, {"goal_id": g.id, "state": "ready"},
    )
    assert any(d["_id"] == did for d in listed["deliverables"])

    superseded = await handlers["deliverables.supersede"](
        conn,
        {
            "deliverable_id": did, "new_content_ref": "r2",
            "finalize": True,
        },
    )
    assert superseded["new"]["state"] == "ready"
    assert superseded["obsoleted"]["state"] == "obsolete"


async def test_ws_dependencies_owner_only(started_agent_service: Any) -> None:
    svc = started_agent_service
    _, _, g_src, g_dep = await _bootstrap_two_goals(svc)

    handlers = svc.get_ws_handlers()
    add_h = handlers["goals.dependencies.add"]
    list_h = handlers["goals.dependencies.list"]

    # Owner can add.
    out = await add_h(
        _FakeConn(user_id="usr_1"),
        {
            "dependent_goal_id": g_dep.id,
            "source_goal_id": g_src.id,
            "required_deliverable_name": "spec",
        },
    )
    assert "dependency" in out

    # Other user blocked.
    with pytest.raises(PermissionError):
        await add_h(
            _FakeConn(user_id="usr_2"),
            {
                "dependent_goal_id": g_dep.id,
                "source_goal_id": g_src.id,
                "required_deliverable_name": "other",
            },
        )
    with pytest.raises(PermissionError):
        await list_h(
            _FakeConn(user_id="usr_2"), {"dependent_goal_id": g_dep.id},
        )


async def test_ws_dependencies_add_list_remove(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    _, _, g_src, g_dep = await _bootstrap_two_goals(svc)

    handlers = svc.get_ws_handlers()
    conn = _FakeConn(user_id="usr_1")

    added = await handlers["goals.dependencies.add"](
        conn,
        {
            "dependent_goal_id": g_dep.id,
            "source_goal_id": g_src.id,
            "required_deliverable_name": "spec",
        },
    )
    dep_id = added["dependency"]["_id"]

    listed = await handlers["goals.dependencies.list"](
        conn, {"dependent_goal_id": g_dep.id},
    )
    assert any(d["_id"] == dep_id for d in listed["dependencies"])

    removed = await handlers["goals.dependencies.remove"](
        conn, {"dependency_id": dep_id},
    )
    assert removed["removed"] is True

    listed = await handlers["goals.dependencies.list"](
        conn, {"dependent_goal_id": g_dep.id},
    )
    assert listed["dependencies"] == []
