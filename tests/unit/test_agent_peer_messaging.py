"""Phase 2 — Peer messaging tools (agent_list, agent_send_message, agent_delegate)."""

from __future__ import annotations

import json

import pytest

# ── agent_list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_list_returns_peers_owner_scoped(started_agent_service):
    """agent_list returns peers under the same owner, excludes self,
    excludes other owners' agents."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1", role_label="Researcher")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2", role_label="Writer")
    a3 = await svc.create_agent(owner_user_id="usr_a", name="a3", role_label="Editor")
    await svc.create_agent(owner_user_id="usr_b", name="b1", role_label="Other")

    raw = await svc._exec_agent_list({"_agent_id": a1.id})
    out = json.loads(raw)
    names = {p["name"] for p in out}
    assert names == {"a2", "a3"}
    # No B-owner leak.
    assert "b1" not in names
    # No self.
    assert "a1" not in names
    # Schema sanity.
    by_name = {p["name"]: p for p in out}
    assert by_name["a2"]["role_label"] == "Writer"
    assert by_name["a2"]["status"] == "enabled"
    assert "conversation_id" in by_name["a2"]

    # _agent_id presence is required.
    assert "error" in await svc._exec_agent_list({})
    # Bogus _agent_id returns an error too.
    assert "error" in await svc._exec_agent_list({"_agent_id": "ag_missing"})

    # Cross-owner sanity: B's agents only see their own bucket (which
    # is empty — no peers exist for usr_b).
    raw_b = await svc._exec_agent_list({
        "_agent_id": (await svc.list_agents(owner_user_id="usr_b"))[0].id
    })
    assert json.loads(raw_b) == []
    # Touch unused fixture so flake8 doesn't complain.
    assert a2.id and a3.id
