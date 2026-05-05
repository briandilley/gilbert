"""Tests for the HTTP agent-avatar upload/download endpoints.

The route lives at ``src/gilbert/web/routes/agent_avatar.py`` and
streams avatar bytes into ``<DATA_DIR>/agent-avatars/<agent_id>/``.
These tests spin up a minimal FastAPI app with a fake AgentService,
hit the endpoints via Starlette's TestClient, and verify:

- Authenticated upload writes the file under the right directory and
  flips ``Agent.avatar_kind`` to ``"image"``.
- The GET endpoint streams the bytes back with a content-length and
  the correct ``image/png`` content type.
- Unauthenticated callers receive 401.
- Callers attempting to upload to another user's agent receive 403.
- Missing-filename uploads are rejected with 400.
- Oversized payloads are rejected with 413 and leave nothing on disk.
- Disallowed mime types are rejected with 415.
- The service-level ``_remove_avatar_dir`` helper deletes a real
  directory tree (covers the cleanup path used by ``delete_agent``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gilbert.interfaces.agent import Agent, AgentStatus
from gilbert.interfaces.auth import UserContext
from gilbert.web.auth import require_authenticated
from gilbert.web.routes.agent_avatar import (
    router as agent_avatar_router,
)

# ── Fakes ────────────────────────────────────────────────────────────


def _make_agent(agent_id: str, owner_user_id: str) -> Agent:
    """Build a real Agent with sensible defaults so the route can call
    ``_agent_to_dict`` on it without AttributeError."""
    now = datetime.now(UTC)
    return Agent(
        id=agent_id,
        owner_user_id=owner_user_id,
        name=agent_id,
        display_name=agent_id,
        role_label="",
        persona="",
        system_prompt="",
        procedural_rules="",
        profile_id="standard",
        conversation_id="",
        status=AgentStatus.ENABLED,
        avatar_kind="emoji",
        avatar_value="🤖",
        lifetime_cost_usd=0.0,
        cost_cap_usd=None,
        tools_include=None,
        tools_exclude=None,
        heartbeat_enabled=True,
        heartbeat_interval_s=1800,
        heartbeat_checklist="",
        dream_enabled=False,
        dream_quiet_hours="22:00-06:00",
        dream_probability=0.1,
        dream_max_per_night=3,
        created_at=now,
        updated_at=now,
    )


class _FakeAgentService:
    """Stand-in for AgentService.

    Satisfies the ``AgentProvider`` runtime-checkable protocol so the
    route's ``isinstance(agent_svc, AgentProvider)`` gate passes. Only
    the two methods the avatar route actually invokes are wired up;
    the rest raise ``NotImplementedError`` to make accidental use
    obvious.
    """

    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = agents

    async def load_agent_for_caller(
        self,
        agent_id: str,
        *,
        caller_user_id: str,
        admin: bool = False,
    ) -> Agent:
        a = self._agents.get(agent_id)
        if a is None:
            raise KeyError(agent_id)
        if not admin and a.owner_user_id != caller_user_id:
            raise PermissionError(
                f"agent {agent_id} not accessible to user {caller_user_id}"
            )
        return a

    async def set_agent_avatar(
        self, agent_id: str, *, filename: str
    ) -> Agent:
        a = self._agents[agent_id]
        a.avatar_kind = "image"
        a.avatar_value = filename
        return a

    # — Remaining ``AgentProvider`` surface — stubbed so the runtime
    # protocol check accepts the fake. The avatar route never calls
    # these.
    async def create_agent(self, **kwargs: Any) -> Agent:
        raise NotImplementedError

    async def get_agent(self, agent_id: str) -> Agent | None:
        raise NotImplementedError

    async def list_agents(self, **kwargs: Any) -> list[Agent]:
        raise NotImplementedError

    async def run_agent_now(self, agent_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    # Phase 4 additions to ``AgentProvider`` — goal CRUD + assignments.
    # Stubbed; the avatar route never calls these.
    async def create_goal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def get_goal(self, goal_id: str) -> Any:
        raise NotImplementedError

    async def list_goals(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def update_goal_status(self, goal_id: str, status: Any) -> Any:
        raise NotImplementedError

    async def delete_goal(self, goal_id: str) -> bool:
        raise NotImplementedError

    async def list_assignments(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def assign_agent_to_goal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def unassign_agent_from_goal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def handoff_goal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    # Phase 5 additions to ``AgentProvider`` — deliverables + dependencies.
    async def create_deliverable(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def get_deliverable(self, deliverable_id: str) -> Any:
        return None

    async def list_deliverables(self, **kwargs: Any) -> Any:
        return []

    async def finalize_deliverable(self, deliverable_id: str) -> Any:
        raise NotImplementedError

    async def supersede_deliverable(self, deliverable_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def add_goal_dependency(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def remove_goal_dependency(self, dependency_id: str) -> None:
        return None

    async def list_goal_dependencies(self, **kwargs: Any) -> Any:
        return []


class _FakeServiceManager:
    def __init__(self, agent_svc: _FakeAgentService) -> None:
        self._agent = agent_svc

    def get_by_capability(self, capability: str) -> Any:
        if capability == "agent":
            return self._agent
        return None


class _FakeGilbert:
    def __init__(self, agent_svc: _FakeAgentService) -> None:
        self.service_manager = _FakeServiceManager(agent_svc)


# ── Fixtures ─────────────────────────────────────────────────────────


_OWNER = UserContext(
    user_id="usr_owner",
    display_name="Owner",
    email="owner@example.com",
    roles=frozenset({"user"}),
    provider="local",
)

_OTHER = UserContext(
    user_id="usr_other",
    display_name="Other",
    email="other@example.com",
    roles=frozenset({"user"}),
    provider="local",
)


# A 1×1 transparent PNG. Tiny but real enough for content-type sniffing.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "89000000017352474200aece1ce90000000c49444154789c63000100000005"
    "0001a5f645400000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _isolate_avatar_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect ``_AVATAR_ROOT`` at the test's tmp_path so we never
    touch the real ``.gilbert/agent-avatars/`` from a unit test."""
    target = tmp_path / "agent-avatars"
    monkeypatch.setattr(
        "gilbert.web.routes.agent_avatar._AVATAR_ROOT", target
    )
    return target


@pytest.fixture
def agents() -> dict[str, Agent]:
    return {
        "ag_owned": _make_agent("ag_owned", "usr_owner"),
    }


@pytest.fixture
def agent_service(agents: dict[str, Agent]) -> _FakeAgentService:
    return _FakeAgentService(agents)


@pytest.fixture
def app(agent_service: _FakeAgentService) -> FastAPI:
    app = FastAPI()
    app.state.gilbert = _FakeGilbert(agent_service)
    app.include_router(agent_avatar_router)
    return app


def _override_auth(app: FastAPI, user: UserContext | None) -> None:
    from fastapi import HTTPException

    def _fake_dep(request: Request) -> UserContext:
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return user

    app.dependency_overrides[require_authenticated] = _fake_dep


# ── Upload tests ─────────────────────────────────────────────────────


def test_upload_writes_avatar_and_marks_agent_image(
    app: FastAPI,
    agents: dict[str, Agent],
    _isolate_avatar_root: Path,
) -> None:
    _override_auth(app, _OWNER)
    client = TestClient(app)

    resp = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("portrait.png", _TINY_PNG, "image/png")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent"]["avatar_kind"] == "image"
    filename = body["agent"]["avatar_value"]
    assert filename.endswith(".png")
    assert filename.startswith("portrait-")

    # Bytes hit the right per-agent subdirectory.
    on_disk = _isolate_avatar_root / "ag_owned" / filename
    assert on_disk.is_file()
    assert on_disk.read_bytes() == _TINY_PNG

    # Service state mutated.
    assert agents["ag_owned"].avatar_kind == "image"
    assert agents["ag_owned"].avatar_value == filename


def test_upload_then_download_roundtrips(
    app: FastAPI,
) -> None:
    _override_auth(app, _OWNER)
    client = TestClient(app)

    upload = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("face.png", _TINY_PNG, "image/png")},
    )
    assert upload.status_code == 200, upload.text

    resp = client.get("/api/agents/ag_owned/avatar")
    assert resp.status_code == 200
    assert resp.content == _TINY_PNG
    # mimetypes.guess_type for ``.png`` is image/png on every platform
    # we run on. If a downstream env disagrees we'd see this fail.
    assert resp.headers["content-type"].startswith("image/png")


def test_upload_requires_authentication(app: FastAPI) -> None:
    _override_auth(app, None)
    client = TestClient(app)
    resp = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("x.png", _TINY_PNG, "image/png")},
    )
    assert resp.status_code == 401


def test_upload_rejects_other_users_agent(app: FastAPI) -> None:
    _override_auth(app, _OTHER)
    client = TestClient(app)
    resp = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("x.png", _TINY_PNG, "image/png")},
    )
    assert resp.status_code == 403


def test_upload_unknown_agent_returns_404(app: FastAPI) -> None:
    _override_auth(app, _OWNER)
    client = TestClient(app)
    resp = client.post(
        "/api/agents/ag_missing/avatar",
        files={"file": ("x.png", _TINY_PNG, "image/png")},
    )
    assert resp.status_code == 404


def test_upload_missing_filename_rejected(app: FastAPI) -> None:
    _override_auth(app, _OWNER)
    client = TestClient(app)
    resp = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("", _TINY_PNG, "image/png")},
    )
    # FastAPI may surface this as 400 (route check) or 422 (validation).
    assert resp.status_code in (400, 422)


def test_upload_rejects_disallowed_mime_type(app: FastAPI) -> None:
    _override_auth(app, _OWNER)
    client = TestClient(app)
    resp = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("not-image.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 415


def test_upload_oversized_rejected(
    app: FastAPI,
    _isolate_avatar_root: Path,
    agents: dict[str, Agent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_auth(app, _OWNER)
    # Shrink the cap so we exercise the rejection path with a tiny
    # payload (allocating 4 MiB+ in a unit test is wasteful and slow).
    # ``_CHUNK_SIZE`` must stay <= the cap so the partial-read accounting
    # in the route's chunk loop is still exercised the same way.
    monkeypatch.setattr(
        "gilbert.web.routes.agent_avatar._MAX_AVATAR_BYTES", 100
    )
    monkeypatch.setattr(
        "gilbert.web.routes.agent_avatar._CHUNK_SIZE", 64
    )
    client = TestClient(app)
    payload = b"\x00" * 101
    resp = client.post(
        "/api/agents/ag_owned/avatar",
        files={"file": ("huge.png", payload, "image/png")},
    )
    assert resp.status_code == 413
    # Agent state untouched on rejection.
    assert agents["ag_owned"].avatar_kind == "emoji"
    # No partial file landed in the per-agent dir.
    target = _isolate_avatar_root / "ag_owned"
    if target.exists():
        assert list(target.iterdir()) == []


# ── Download tests ───────────────────────────────────────────────────


def test_download_404_when_no_image_avatar(app: FastAPI) -> None:
    _override_auth(app, _OWNER)
    client = TestClient(app)
    resp = client.get("/api/agents/ag_owned/avatar")
    assert resp.status_code == 404


def test_download_requires_authentication(app: FastAPI) -> None:
    _override_auth(app, None)
    client = TestClient(app)
    resp = client.get("/api/agents/ag_owned/avatar")
    assert resp.status_code == 401


def test_download_rejects_other_users_agent(
    app: FastAPI,
    agents: dict[str, Agent],
    _isolate_avatar_root: Path,
) -> None:
    # Pre-stage a file as the owner.
    agents["ag_owned"].avatar_kind = "image"
    agents["ag_owned"].avatar_value = "x-deadbeef.png"
    d = _isolate_avatar_root / "ag_owned"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x-deadbeef.png").write_bytes(_TINY_PNG)

    _override_auth(app, _OTHER)
    client = TestClient(app)
    resp = client.get("/api/agents/ag_owned/avatar")
    assert resp.status_code == 403


# ── Service helper test ──────────────────────────────────────────────


async def test_remove_avatar_dir_deletes_directory_tree(
    started_agent_service: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``_remove_avatar_dir`` helper used by ``delete_agent`` must
    recursively remove the avatar directory if it exists, and stay
    silent if it doesn't.

    We redirect ``DATA_DIR`` at a tmp directory, write a fake avatar
    file, then trigger the helper through ``delete_agent`` and assert
    the whole subtree is gone.
    """
    fake_data_dir = tmp_path / "data"
    # The helper imports ``DATA_DIR`` from ``gilbert.config`` at call
    # time, so patching the source module is what actually takes
    # effect. If the implementation later switches to a top-level
    # import, this patch will need to also cover
    # ``gilbert.core.services.agent.DATA_DIR`` — the test will fail
    # loudly when that happens.
    monkeypatch.setattr("gilbert.config.DATA_DIR", fake_data_dir)

    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")

    avatar_dir = fake_data_dir / "agent-avatars" / a.id
    avatar_dir.mkdir(parents=True, exist_ok=True)
    (avatar_dir / "fake.png").write_bytes(b"not-really-a-png")

    deleted = await svc.delete_agent(a.id)
    assert deleted is True
    assert not avatar_dir.exists()


async def test_remove_avatar_dir_silent_when_no_dir(
    started_agent_service: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent with no avatar directory must still delete cleanly."""
    fake_data_dir = tmp_path / "data"
    monkeypatch.setattr("gilbert.config.DATA_DIR", fake_data_dir)

    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="y")

    deleted = await svc.delete_agent(a.id)
    assert deleted is True
