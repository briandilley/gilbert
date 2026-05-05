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

from gilbert.core.events import InMemoryEventBus
from gilbert.interfaces.agent import AgentProvider, AgentStatus
from gilbert.interfaces.service import ServiceInfo
from gilbert.storage.sqlite import SQLiteStorage

# ── Minimal fakes that satisfy Protocol isinstance checks ────────────


class _FakeStorageProvider:
    """Satisfies StorageProvider (has .backend)."""

    def __init__(self, backend: SQLiteStorage) -> None:
        self._backend = backend

    @property
    def backend(self) -> SQLiteStorage:
        return self._backend

    @property
    def raw_backend(self) -> SQLiteStorage:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:  # noqa: ANN401
        return self._backend


class _FakeEventBusProvider:
    """Satisfies EventBusProvider (has .bus)."""

    def __init__(self) -> None:
        self.bus = InMemoryEventBus()


class _FakeAIProvider:
    """Satisfies AIProvider (has .chat)."""

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError("not used in CRUD tests")


class _FakeSchedulerProvider:
    """Satisfies SchedulerProvider (all required methods present)."""

    def add_job(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        pass

    def remove_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def enable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def disable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_jobs(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def get_job(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return None

    async def run_now(self, *args: Any, **kwargs: Any) -> None:
        pass


def _make_resolver(**caps: Any) -> Any:
    """Build a minimal ServiceResolver that returns the given capabilities."""

    class _Resolver:
        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def get_all(self, name: str) -> list[Any]:
            return []

    return _Resolver()


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def started_agent_service(sqlite_storage: SQLiteStorage) -> Any:
    """Start an AgentService backed by a real SQLite database."""
    from gilbert.core.services.agent import AgentService

    storage_provider = _FakeStorageProvider(sqlite_storage)
    event_bus_provider = _FakeEventBusProvider()
    ai_provider = _FakeAIProvider()
    scheduler_provider = _FakeSchedulerProvider()

    resolver = _make_resolver(
        entity_storage=storage_provider,
        event_bus=event_bus_provider,
        ai_chat=ai_provider,
        scheduler=scheduler_provider,
    )

    svc = AgentService()
    await svc.start(resolver)
    yield svc
    await svc.stop()


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
