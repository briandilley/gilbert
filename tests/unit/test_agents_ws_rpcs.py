"""Unit tests for AgentService WS RPC handlers (Phase 1B Tasks 2–5).

Covers the SPA-facing handlers added in Phase 1B:

- ``agents.runs.list``
- ``agents.commitments.{list,create,complete}``
- ``agents.memories.{list,set_state}``
- ``agents.tools.{list_available,list_groups}``

All tests use the shared ``started_agent_service`` fixture from
``tests/unit/conftest.py`` and a small ``_FakeConn`` mirroring the
existing ``test_agent_service.py`` style.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


class _FakeConn:
    def __init__(self, user_id: str, user_level: int = 100) -> None:
        self.user_id = user_id
        self.user_level = user_level
        self.user_ctx = type(
            "U", (), {"user_id": user_id, "roles": frozenset()},
        )()


# ── Task 2: agents.runs.list ─────────────────────────────────────────


async def test_ws_runs_list_returns_owned_runs(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="r1")
    run = await svc.run_agent_now(a.id, user_message="hi")

    res = await h["agents.runs.list"](_FakeConn("usr_1"), {"agent_id": a.id})
    assert "runs" in res
    ids = [row["_id"] for row in res["runs"]]
    assert run.id in ids


async def test_ws_runs_list_blocks_non_owner(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="r2")
    await svc.run_agent_now(a.id, user_message="hi")

    with pytest.raises(PermissionError):
        await h["agents.runs.list"](_FakeConn("usr_2"), {"agent_id": a.id})


async def test_ws_runs_list_honors_limit(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="r3")

    # Insert several run rows directly so we can control count without
    # racing the AIService stub.
    base = datetime.now(UTC)
    for i in range(5):
        rid = f"run_lim_{i}"
        await svc._storage.put("agent_runs", rid, {
            "_id": rid,
            "agent_id": a.id,
            "triggered_by": "manual",
            "trigger_context": {},
            "started_at": (base - timedelta(seconds=i)).isoformat(),
            "status": "completed",
            "conversation_id": "",
            "delegation_id": "",
            "ended_at": base.isoformat(),
            "final_message_text": None,
            "rounds_used": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "error": None,
            "awaiting_user_input": False,
            "pending_question": None,
            "pending_actions": [],
        })

    res = await h["agents.runs.list"](
        _FakeConn("usr_1"), {"agent_id": a.id, "limit": 3},
    )
    assert len(res["runs"]) == 3


# ── Task 3: agents.commitments.{list,create,complete} ────────────────


async def test_ws_commitments_list(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c1")
    due = datetime.now(UTC) + timedelta(hours=1)
    await svc.create_commitment(agent_id=a.id, content="ping me", due_at=due)

    res = await h["agents.commitments.list"](
        _FakeConn("usr_1"), {"agent_id": a.id},
    )
    assert len(res["commitments"]) == 1
    assert res["commitments"][0]["content"] == "ping me"


async def test_ws_commitments_list_include_completed(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c1b")
    due = datetime.now(UTC) + timedelta(hours=1)
    c = await svc.create_commitment(agent_id=a.id, content="x", due_at=due)
    await svc.complete_commitment(c.id, note="ok")

    res = await h["agents.commitments.list"](
        _FakeConn("usr_1"), {"agent_id": a.id},
    )
    assert res["commitments"] == []

    res2 = await h["agents.commitments.list"](
        _FakeConn("usr_1"), {"agent_id": a.id, "include_completed": True},
    )
    assert len(res2["commitments"]) == 1


async def test_ws_commitments_create_due_at(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c2")
    due = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    res = await h["agents.commitments.create"](
        _FakeConn("usr_1"),
        {"agent_id": a.id, "content": "do thing", "due_at": due},
    )
    assert res["commitment"]["content"] == "do thing"
    assert res["commitment"]["due_at"].startswith(due[:10])


async def test_ws_commitments_create_due_in_seconds(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c3")

    res = await h["agents.commitments.create"](
        _FakeConn("usr_1"),
        {"agent_id": a.id, "content": "soon", "due_in_seconds": 600},
    )
    assert res["commitment"]["content"] == "soon"
    assert res["commitment"]["due_at"]


async def test_ws_commitments_create_rejects_empty_content(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c4")

    with pytest.raises(ValueError, match="content"):
        await h["agents.commitments.create"](
            _FakeConn("usr_1"),
            {"agent_id": a.id, "content": "   ", "due_in_seconds": 60},
        )


async def test_ws_commitments_create_requires_due(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c4b")

    with pytest.raises(ValueError, match="due_at"):
        await h["agents.commitments.create"](
            _FakeConn("usr_1"),
            {"agent_id": a.id, "content": "needs due"},
        )


async def test_ws_commitments_complete(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c5")
    due = datetime.now(UTC) + timedelta(hours=1)
    c = await svc.create_commitment(agent_id=a.id, content="x", due_at=due)

    res = await h["agents.commitments.complete"](
        _FakeConn("usr_1"),
        {"commitment_id": c.id, "note": "done"},
    )
    assert res["commitment"]["completed_at"] is not None
    assert res["commitment"]["completion_note"] == "done"


async def test_ws_commitments_blocks_non_owner(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="c6")
    due = datetime.now(UTC) + timedelta(hours=1)
    c = await svc.create_commitment(agent_id=a.id, content="x", due_at=due)

    with pytest.raises(PermissionError):
        await h["agents.commitments.list"](
            _FakeConn("usr_2"), {"agent_id": a.id},
        )

    with pytest.raises(PermissionError):
        await h["agents.commitments.create"](
            _FakeConn("usr_2"),
            {"agent_id": a.id, "content": "x", "due_in_seconds": 60},
        )

    with pytest.raises(PermissionError):
        await h["agents.commitments.complete"](
            _FakeConn("usr_2"), {"commitment_id": c.id},
        )


# ── Task 4: agents.memories.{list,set_state} ─────────────────────────


async def test_ws_memories_list_filters(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    from gilbert.interfaces.agent import MemoryState

    a = await svc.create_agent(owner_user_id="usr_1", name="m1")

    m1 = await svc.save_memory(
        agent_id=a.id, content="user prefers dark mode",
        kind="preference", tags=frozenset({"ui", "user"}),
    )
    await svc.save_memory(
        agent_id=a.id, content="capital of france is paris",
        kind="fact", tags=frozenset({"geo"}),
    )
    m3 = await svc.save_memory(
        agent_id=a.id, content="legacy long term thing",
        kind="fact", tags=frozenset({"old"}),
    )
    await svc.promote_memory(memory_id=m3.id, score=0.9, state=MemoryState.LONG_TERM)

    # No filter — all three.
    res = await h["agents.memories.list"](
        _FakeConn("usr_1"), {"agent_id": a.id},
    )
    assert len(res["memories"]) == 3

    # state=long_term → only m3.
    res_lt = await h["agents.memories.list"](
        _FakeConn("usr_1"), {"agent_id": a.id, "state": "long_term"},
    )
    assert {m["_id"] for m in res_lt["memories"]} == {m3.id}

    # kind=preference → only m1.
    res_kind = await h["agents.memories.list"](
        _FakeConn("usr_1"), {"agent_id": a.id, "kind": "preference"},
    )
    assert {m["_id"] for m in res_kind["memories"]} == {m1.id}

    # tags any-match: tag "ui" → m1.
    res_tag = await h["agents.memories.list"](
        _FakeConn("usr_1"), {"agent_id": a.id, "tags": ["ui"]},
    )
    assert {m["_id"] for m in res_tag["memories"]} == {m1.id}

    # q=dark substring → m1.
    res_q = await h["agents.memories.list"](
        _FakeConn("usr_1"), {"agent_id": a.id, "q": "dark"},
    )
    assert {m["_id"] for m in res_q["memories"]} == {m1.id}

    # Combined: state=short_term + kind=fact → only the unpromoted "paris" row.
    res_combo = await h["agents.memories.list"](
        _FakeConn("usr_1"),
        {"agent_id": a.id, "state": "short_term", "kind": "fact"},
    )
    assert len(res_combo["memories"]) == 1
    assert "paris" in res_combo["memories"][0]["content"]


async def test_ws_memories_set_state(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="m2")
    m = await svc.save_memory(agent_id=a.id, content="promote me")
    assert m.state.value == "short_term"

    res = await h["agents.memories.set_state"](
        _FakeConn("usr_1"),
        {"memory_id": m.id, "state": "long_term"},
    )
    assert res["memory"]["state"] == "long_term"


async def test_ws_memories_blocks_non_owner(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    a = await svc.create_agent(owner_user_id="usr_1", name="m3")
    m = await svc.save_memory(agent_id=a.id, content="hidden")

    with pytest.raises(PermissionError):
        await h["agents.memories.list"](
            _FakeConn("usr_2"), {"agent_id": a.id},
        )

    with pytest.raises(PermissionError):
        await h["agents.memories.set_state"](
            _FakeConn("usr_2"),
            {"memory_id": m.id, "state": "long_term"},
        )


# ── Task 5: agents.tools.{list_available,list_groups} ────────────────


async def test_ws_tools_list_groups_returns_defaults(
    started_agent_service: Any,
) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    svc._defaults["tool_groups"] = {
        "communication": ["notify_user"],
        "self": ["agent_memory_save"],
    }

    res = await h["agents.tools.list_groups"](_FakeConn("usr_1"), {})
    assert res["groups"] == {
        "communication": ["notify_user"],
        "self": ["agent_memory_save"],
    }


async def test_ws_tools_list_available_returns_discovered_shape(
    started_agent_service: Any,
) -> None:
    """The handler should call into the bound AIToolDiscoveryProvider and
    flatten its ``dict[name, (provider, ToolDefinition)]`` result into
    a list of ``{name, description, required_role}`` objects."""
    svc = started_agent_service
    h = svc.get_ws_handlers()

    # Stub the discovery provider with a known dict.
    from gilbert.interfaces.tools import ToolDefinition

    fake_tool = ToolDefinition(
        name="search_knowledge",
        description="search the docs",
        required_role="user",
    )

    class _Provider:
        tool_provider_name = "knowledge"

    class _StubDiscovery:
        def discover_tools(
            self, *, user_ctx: Any = None, profile_name: str | None = None,
        ) -> dict[str, Any]:
            return {fake_tool.name: (_Provider(), fake_tool)}

    svc._tool_discovery = _StubDiscovery()

    res = await h["agents.tools.list_available"](_FakeConn("usr_1"), {})
    assert "tools" in res
    assert len(res["tools"]) == 1
    t = res["tools"][0]
    assert t["name"] == "search_knowledge"
    assert t["description"] == "search the docs"
    assert t["required_role"] == "user"
    assert t["provider"] == "knowledge"
