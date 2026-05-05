"""Per-agent tool gating — force-include core + tools_allowed allowlist."""

from __future__ import annotations

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


async def test_tools_allowed_none_means_all(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x", tools_allowed=None)
    keep = svc._compute_allowed_tool_names(
        a, available={"complete_run", "search_knowledge", "lights.set"},
    )
    assert keep == {"complete_run", "search_knowledge", "lights.set"}


async def test_tools_allowed_empty_list_keeps_only_core(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x", tools_allowed=[])
    keep = svc._compute_allowed_tool_names(
        a, available={"complete_run", "search_knowledge", "lights.set"},
    )
    assert keep == {"complete_run"}  # only core that's available


async def test_tools_allowed_extra_unioned_with_core(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1", name="x", tools_allowed=["search_knowledge"],
    )
    available = {"complete_run", "search_knowledge", "lights.set"}
    keep = svc._compute_allowed_tool_names(a, available=available)
    assert keep == {"complete_run", "search_knowledge"}


def test_core_tools_constant_matches_spec():
    """If _CORE_AGENT_TOOLS drifts from the spec, fail loudly."""
    from gilbert.core.services.agent import _CORE_AGENT_TOOLS
    assert _CORE == _CORE_AGENT_TOOLS
