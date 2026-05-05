"""Tests for the cross-goal Deliverable file access resolver.

The ``WorkspaceProvider.resolve_deliverable_for_dependent`` resolver
returns the on-disk path of a workspace file iff:

- A Deliverable references the file (``content_ref`` of either shape
  ``"workspace_file:<id>"`` or bare ``<id>``).
- The Deliverable is currently READY.
- The viewing goal has a satisfied ``GoalDependency`` row pointing at
  the deliverable's goal with the matching name.

Tests build a minimal scaffolding: a started AgentService (sqlite-
backed) plus a started WorkspaceService bound to the same storage
backend with the agent capability injected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gilbert.core.services.workspace import WorkspaceService
from gilbert.interfaces.agent import AssignmentRole

# ── Fixtures / scaffolding ───────────────────────────────────────────


@pytest.fixture
async def started_workspace(
    started_agent_service: Any,
    sqlite_storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Start a WorkspaceService backed by the same storage as the agent
    service, with the agent capability bound.

    Redirects ``_workspace_top`` and ``_legacy_workspace_top`` at
    ``tmp_path`` so file resolution lands under the test sandbox.
    """
    monkeypatch.setattr(
        WorkspaceService, "_workspace_top",
        staticmethod(lambda: tmp_path / "workspaces"),
    )
    monkeypatch.setattr(
        WorkspaceService, "_legacy_workspace_top",
        staticmethod(lambda: tmp_path / "skill-workspaces"),
    )

    class _StorageProvider:
        @property
        def backend(self) -> Any:
            return sqlite_storage

        @property
        def raw_backend(self) -> Any:
            return sqlite_storage

        def create_namespaced(self, namespace: str) -> Any:
            return sqlite_storage

    class _Resolver:
        def __init__(self) -> None:
            self._caps = {
                "entity_storage": _StorageProvider(),
                "agent": started_agent_service,
            }

        def require_capability(self, name: str) -> Any:
            if name not in self._caps:
                raise LookupError(name)
            return self._caps[name]

        def get_capability(self, name: str) -> Any:
            return self._caps.get(name)

        def get_all(self, name: str) -> list[Any]:
            return []

    svc = WorkspaceService()
    await svc.start(_Resolver())
    yield svc
    await svc.stop()


async def _create_workspace_file(
    workspace: WorkspaceService,
    *,
    user_id: str,
    conversation_id: str,
    contents: bytes = b"hello",
) -> str:
    """Create a workspace file on disk + register it. Returns file_id."""
    out_dir = workspace.get_output_dir(user_id, conversation_id)
    target = out_dir / "deliverable.txt"
    target.write_bytes(contents)
    rel_path = "outputs/deliverable.txt"
    file_entity = await workspace.register_file(
        conversation_id=conversation_id,
        user_id=user_id,
        category="outputs",
        filename="deliverable.txt",
        rel_path=rel_path,
        media_type="text/plain",
        size=len(contents),
    )
    return file_entity["_id"]


async def _setup_two_goals(svc: Any):
    """Create two goals owned by usr_1, with alpha as DRIVER on each."""
    a = await svc.create_agent(owner_user_id="usr_1", name="alpha")
    g_src = await svc.create_goal(
        owner_user_id="usr_1", name="src",
        assign_to=[("alpha", AssignmentRole.DRIVER)],
        assigned_by="user:usr_1",
    )
    g_dep = await svc.create_goal(
        owner_user_id="usr_1", name="dep",
        assign_to=[("alpha", AssignmentRole.DRIVER)],
        assigned_by="user:usr_1",
    )
    return a, g_src, g_dep


# ── Tests ────────────────────────────────────────────────────────────


async def test_resolve_grants_access_when_dependency_satisfied(
    started_agent_service: Any,
    started_workspace: Any,
) -> None:
    svc = started_agent_service
    a, g_src, g_dep = await _setup_two_goals(svc)

    file_id = await _create_workspace_file(
        started_workspace,
        user_id="usr_1",
        conversation_id=g_src.war_room_conversation_id,
        contents=b"the spec",
    )
    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id,
        content_ref=f"workspace_file:{file_id}",
    )
    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    # Finalize → propagation marks dep satisfied.
    await svc.finalize_deliverable(d.id)

    path, err = await started_workspace.resolve_deliverable_for_dependent(
        file_id=file_id,
        viewing_agent_id=a.id,
        viewing_goal_id=g_dep.id,
    )
    assert err is None
    assert path is not None
    assert path.read_bytes() == b"the spec"


async def test_resolve_blocks_when_dependency_missing(
    started_agent_service: Any,
    started_workspace: Any,
) -> None:
    svc = started_agent_service
    a, g_src, g_dep = await _setup_two_goals(svc)

    file_id = await _create_workspace_file(
        started_workspace,
        user_id="usr_1",
        conversation_id=g_src.war_room_conversation_id,
    )
    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id,
        content_ref=f"workspace_file:{file_id}",
    )
    await svc.finalize_deliverable(d.id)
    # NO dependency added.

    path, err = await started_workspace.resolve_deliverable_for_dependent(
        file_id=file_id,
        viewing_agent_id=a.id,
        viewing_goal_id=g_dep.id,
    )
    assert path is None
    assert err is not None
    assert "depend" in err.lower()


async def test_resolve_blocks_when_obsolete(
    started_agent_service: Any,
    started_workspace: Any,
) -> None:
    svc = started_agent_service
    a, g_src, g_dep = await _setup_two_goals(svc)

    file_id = await _create_workspace_file(
        started_workspace,
        user_id="usr_1",
        conversation_id=g_src.war_room_conversation_id,
    )
    d = await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id,
        content_ref=f"workspace_file:{file_id}",
    )
    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    await svc.finalize_deliverable(d.id)
    # Now supersede → first deliverable becomes OBSOLETE; new is DRAFT
    # (no finalize). The OBSOLETE row still references the same file.
    obs, _new = await svc.supersede_deliverable(
        d.id, new_content_ref="r2",
    )
    # The OBSOLETE deliverable still exists with the file_id ref.
    path, err = await started_workspace.resolve_deliverable_for_dependent(
        file_id=file_id,
        viewing_agent_id=a.id,
        viewing_goal_id=g_dep.id,
    )
    assert path is None
    assert err is not None
    assert "OBSOLETE" in err


async def test_resolve_blocks_unrelated_file(
    started_agent_service: Any,
    started_workspace: Any,
) -> None:
    """A file that isn't a deliverable's content_ref must be rejected."""
    svc = started_agent_service
    a, g_src, g_dep = await _setup_two_goals(svc)

    file_id = await _create_workspace_file(
        started_workspace,
        user_id="usr_1",
        conversation_id=g_src.war_room_conversation_id,
    )
    # No deliverable created.

    path, err = await started_workspace.resolve_deliverable_for_dependent(
        file_id=file_id,
        viewing_agent_id=a.id,
        viewing_goal_id=g_dep.id,
    )
    assert path is None
    assert err is not None
    assert "no deliverable" in err.lower()


async def test_resolve_blocks_draft_deliverable(
    started_agent_service: Any,
    started_workspace: Any,
) -> None:
    svc = started_agent_service
    a, g_src, g_dep = await _setup_two_goals(svc)

    file_id = await _create_workspace_file(
        started_workspace,
        user_id="usr_1",
        conversation_id=g_src.war_room_conversation_id,
    )
    await svc.create_deliverable(
        goal_id=g_src.id, name="spec", kind="spec",
        produced_by_agent_id=a.id,
        content_ref=f"workspace_file:{file_id}",
    )
    await svc.add_goal_dependency(
        dependent_goal_id=g_dep.id,
        source_goal_id=g_src.id,
        required_deliverable_name="spec",
    )
    # Deliberately do NOT finalize.

    path, err = await started_workspace.resolve_deliverable_for_dependent(
        file_id=file_id,
        viewing_agent_id=a.id,
        viewing_goal_id=g_dep.id,
    )
    assert path is None
    assert err is not None
    assert "DRAFT" in err
