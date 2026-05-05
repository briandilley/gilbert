"""Unit tests for AgentService — skeleton (Task 3) + CRUD (Task 5).

Covers:
- service_info() declares required capabilities.
- AgentService satisfies the AgentProvider runtime-checkable protocol.
- CRUD: create / get / list / update / delete.
- Uniqueness enforcement (same-owner, same-name).
- _load_agent_for_caller ownership check.
"""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.agent import AgentProvider, AgentStatus
from gilbert.interfaces.service import ServiceInfo

# ── Task 3 tests ─────────────────────────────────────────────────────


def test_service_info_declares_capabilities() -> None:
    """service_info() returns correct name, capabilities, requires, and ai_calls."""
    from gilbert.core.services.agent import AgentService

    svc = AgentService()
    info = svc.service_info()

    assert isinstance(info, ServiceInfo)
    assert info.name == "agent"

    # Declared capabilities
    assert "agent" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert "ws_handlers" in info.capabilities

    # Declared dependencies
    assert "entity_storage" in info.requires
    assert "event_bus" in info.requires
    assert "ai_chat" in info.requires
    assert "scheduler" in info.requires

    # AI call budget declarations
    assert "agent.run" in info.ai_calls


def test_agent_service_satisfies_agent_provider() -> None:
    """AgentService structurally satisfies the AgentProvider runtime-checkable Protocol.

    The Protocol verifies method *presence*, not behavior, so NotImplementedError
    stubs are sufficient.
    """
    from gilbert.core.services.agent import AgentService

    svc = AgentService()
    assert isinstance(svc, AgentProvider)


# ── Task 5 tests ─────────────────────────────────────────────────────


async def test_create_agent_round_trip(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(
        owner_user_id="usr_1",
        name="research-bot",
        role_label="Research",
        persona="curious",
        system_prompt="follow up",
        procedural_rules="cite sources",
        profile_id="standard",
    )
    assert a.id
    assert a.owner_user_id == "usr_1"
    assert a.name == "research-bot"
    assert a.status is AgentStatus.ENABLED
    fetched = await svc.get_agent(a.id)
    assert fetched is not None
    assert fetched.name == "research-bot"


async def test_list_agents_filters_by_owner(started_agent_service: Any) -> None:
    svc = started_agent_service
    await svc.create_agent(owner_user_id="usr_1", name="a1")
    await svc.create_agent(owner_user_id="usr_1", name="a2")
    await svc.create_agent(owner_user_id="usr_2", name="b1")

    only_usr_1 = await svc.list_agents(owner_user_id="usr_1")
    assert {a.name for a in only_usr_1} == {"a1", "a2"}

    only_usr_2 = await svc.list_agents(owner_user_id="usr_2")
    assert {a.name for a in only_usr_2} == {"b1"}

    everyone = await svc.list_agents()
    assert len(everyone) == 3


async def test_create_agent_unique_name_per_owner(started_agent_service: Any) -> None:
    svc = started_agent_service
    await svc.create_agent(owner_user_id="usr_1", name="dup")
    with pytest.raises(ValueError, match="name already in use"):
        await svc.create_agent(owner_user_id="usr_1", name="dup")
    # Different owner — same name OK.
    await svc.create_agent(owner_user_id="usr_2", name="dup")


async def test_update_agent_patches_fields(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    updated = await svc.update_agent(a.id, {"role_label": "New Label", "persona": "new persona"})
    assert updated.role_label == "New Label"
    assert updated.persona == "new persona"
    assert updated.name == "x"  # unchanged


async def test_delete_agent_removes_row(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    deleted = await svc.delete_agent(a.id)
    assert deleted is True
    assert await svc.get_agent(a.id) is None


async def test_load_agent_for_caller_owner_match(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    found = await svc._load_agent_for_caller(a.id, caller_user_id="usr_1")
    assert found.id == a.id


async def test_load_agent_for_caller_owner_mismatch(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    with pytest.raises(PermissionError):
        await svc._load_agent_for_caller(a.id, caller_user_id="usr_2")


# ── Task 6 tests — WS RPC handlers ───────────────────────────────────


class _FakeConn:
    def __init__(self, user_id: str, user_level: int = 100):
        self.user_id = user_id
        self.user_level = user_level
        self.user_ctx = type("U", (), {"user_id": user_id, "roles": frozenset()})()


async def test_ws_rpc_create_agent_returns_id(started_agent_service: Any) -> None:
    svc = started_agent_service
    handlers = svc.get_ws_handlers()
    assert "agents.create" in handlers

    conn = _FakeConn("usr_1")
    result = await handlers["agents.create"](
        conn, {"name": "x", "role_label": "Tester"},
    )
    assert "agent" in result
    assert result["agent"]["name"] == "x"
    assert result["agent"]["owner_user_id"] == "usr_1"


async def test_ws_rpc_list_filters_by_caller_unless_admin(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()

    # User 1 creates 2.
    await h["agents.create"](_FakeConn("usr_1"), {"name": "a1"})
    await h["agents.create"](_FakeConn("usr_1"), {"name": "a2"})
    # User 2 creates 1.
    await h["agents.create"](_FakeConn("usr_2"), {"name": "b1"})

    # User 1 sees their own only.
    res = await h["agents.list"](_FakeConn("usr_1"), {})
    assert {a["name"] for a in res["agents"]} == {"a1", "a2"}

    # Admin sees all.
    admin = _FakeConn("usr_admin", user_level=0)
    res = await h["agents.list"](admin, {})
    assert {a["name"] for a in res["agents"]} == {"a1", "a2", "b1"}


async def test_ws_rpc_update_rejects_cross_user(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["agents.create"](_FakeConn("usr_1"), {"name": "x"})
    agent_id = res["agent"]["_id"]
    with pytest.raises(PermissionError):
        await h["agents.update"](_FakeConn("usr_2"), {"agent_id": agent_id, "patch": {"role_label": "X"}})


async def test_ws_rpc_set_status_toggles(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["agents.create"](_FakeConn("usr_1"), {"name": "x"})
    agent_id = res["agent"]["_id"]
    out = await h["agents.set_status"](_FakeConn("usr_1"), {"agent_id": agent_id, "status": "disabled"})
    assert out["agent"]["status"] == "disabled"


async def test_ws_rpc_delete_cascades(started_agent_service: Any) -> None:
    svc = started_agent_service
    h = svc.get_ws_handlers()
    res = await h["agents.create"](_FakeConn("usr_1"), {"name": "x"})
    agent_id = res["agent"]["_id"]
    out = await h["agents.delete"](_FakeConn("usr_1"), {"agent_id": agent_id})
    assert out["deleted"] is True
    assert await svc.get_agent(agent_id) is None


# ── Task 8 tests — Run lifecycle ──────────────────────────────────────


async def test_run_agent_now_creates_run_row(started_agent_service: Any) -> None:
    """run_agent_now spawns a run, calls AIService.chat, persists a Run."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    run = await svc.run_agent_now(a.id, user_message="hello")
    assert run.agent_id == a.id
    assert run.triggered_by == "manual"

    runs = await svc.list_runs(agent_id=a.id)
    assert len(runs) == 1
    assert runs[0].id == run.id


# ── Task 12 tests — ConfigParam defaults + on_config_changed ──────────


async def test_config_params_includes_defaults(started_agent_service: Any) -> None:
    svc = started_agent_service
    params = svc.config_params()
    keys = {p.key for p in params}
    expected = {
        "default_persona", "default_system_prompt", "default_procedural_rules",
        "default_heartbeat_interval_s", "default_heartbeat_checklist",
        "default_dream_enabled", "default_dream_quiet_hours",
        "default_dream_probability", "default_dream_max_per_night",
        "default_profile_id", "default_avatar_kind", "default_avatar_value",
        "default_tools_allowed", "tool_groups",
    }
    assert expected.issubset(keys)


async def test_default_persona_is_ai_prompt_flagged(started_agent_service: Any) -> None:
    svc = started_agent_service
    params = {p.key: p for p in svc.config_params()}
    assert params["default_persona"].ai_prompt is True
    assert params["default_persona"].multiline is True
    assert params["default_system_prompt"].ai_prompt is True
    assert params["default_procedural_rules"].ai_prompt is True
    assert params["default_heartbeat_checklist"].ai_prompt is True


async def test_on_config_changed_caches_defaults(started_agent_service: Any) -> None:
    svc = started_agent_service
    await svc.on_config_changed({"default_persona": "I am helpful."})
    assert svc._defaults["default_persona"] == "I am helpful."


async def test_agents_get_defaults_rpc_returns_current(started_agent_service: Any) -> None:
    svc = started_agent_service
    await svc.on_config_changed({"default_persona": "X"})
    h = svc.get_ws_handlers()
    res = await h["agents.get_defaults"](_FakeConn("usr_1"), {})
    assert res["defaults"]["default_persona"] == "X"
