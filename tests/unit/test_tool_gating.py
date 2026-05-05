"""Per-agent tool gating — tools_include / tools_exclude (mutex)."""

from __future__ import annotations

import pytest

_CORE = {
    # Phase 1A — agent self-management
    "complete_run", "request_user_input", "notify_user",
    "commitment_create", "commitment_complete", "commitment_list",
    "agent_memory_save", "agent_memory_search",
    "agent_memory_review_and_promote",
    # Phase 2 — peer messaging
    "agent_list", "agent_send_message", "agent_delegate",
    # Phase 4 — multi-agent goals (war-room post)
    "goal_post",
}


async def test_tools_include_keeps_only_listed_plus_core(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x", tools_include=["search_knowledge"],
    )
    available = {"complete_run", "search_knowledge", "lights.set"}
    keep = svc._compute_allowed_tool_names(a, available=available)
    # Core (intersected with available) ∪ include (intersected with available).
    assert keep == {"complete_run", "search_knowledge"}


async def test_tools_exclude_drops_listed_keeps_core(started_agent_service):
    svc = started_agent_service
    # Exclude the non-core tool ``lights.set``; ``complete_run`` is core
    # so even if it were in the exclude list it would still be kept.
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x",
        tools_exclude=["lights.set", "complete_run"],
    )
    available = {"complete_run", "search_knowledge", "lights.set"}
    keep = svc._compute_allowed_tool_names(a, available=available)
    # Core kept regardless; non-core excluded.
    assert "complete_run" in keep
    assert "search_knowledge" in keep
    assert "lights.set" not in keep


async def test_no_gating_returns_all_available(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    available = {"complete_run", "search_knowledge", "lights.set"}
    keep = svc._compute_allowed_tool_names(a, available=available)
    assert keep == available


async def test_owner_loses_tool_propagates(started_agent_service):
    """Include mode is intersected with available — if the owner loses
    access to a listed tool the agent loses it too."""
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x", tools_include=["search_knowledge"],
    )
    # Owner no longer has search_knowledge available.
    available = {"complete_run"}
    keep = svc._compute_allowed_tool_names(a, available=available)
    assert keep == {"complete_run"}


async def test_create_rejects_both_include_and_exclude(started_agent_service):
    svc = started_agent_service
    with pytest.raises(ValueError, match="mutually exclusive"):
        await svc.create_agent(
            owner_user_id="usr_1",
            name="x",
            tools_include=["search_knowledge"],
            tools_exclude=["lights.set"],
        )


def test_core_tools_constant_matches_spec():
    """If _CORE_AGENT_TOOLS drifts from the spec, fail loudly."""
    from gilbert.core.services.agent import _CORE_AGENT_TOOLS
    assert _CORE == _CORE_AGENT_TOOLS
