"""Tests for SourceUpdateService — admin branch-switch action."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.services.source_update import SourceUpdateService, _GitError
from gilbert.interfaces.auth import UserContext

# --- Fixtures ---


def _admin() -> UserContext:
    return UserContext(
        user_id="admin-1",
        email="admin@example.com",
        display_name="Admin",
        roles=frozenset({"admin"}),
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Empty repo-root stand-in. The service writes the sentinel here."""
    return tmp_path


@pytest.fixture
def service(repo_root: Path) -> SourceUpdateService:
    svc = SourceUpdateService()
    svc._repo_root = repo_root
    return svc


class _GitDouble:
    """Records git arg vectors and returns canned stdout per first-arg."""

    def __init__(self, *, current: str = "main", origin: str = "git@x:y.git",
                 dirty: str = "", remote_heads: set[str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._current = current
        self._origin = origin
        self._dirty = dirty
        self._remote_heads = remote_heads if remote_heads is not None else {"main"}
        self.fail_fetch = False
        self.fail_origin = False

    async def __call__(self, *args: str) -> str:
        self.calls.append(args)
        first = args[0] if args else ""
        if first == "symbolic-ref":
            return self._current + "\n"
        if first == "remote":
            if self.fail_origin:
                raise _GitError("no origin")
            return self._origin + "\n"
        if first == "status":
            return self._dirty
        if first == "fetch":
            if self.fail_fetch:
                raise _GitError("fetch failed")
            return ""
        if first == "ls-remote":
            # ls-remote --heads origin <branch> — match against our set.
            branch = args[-1] if args else ""
            if branch in self._remote_heads:
                return f"abc123\trefs/heads/{branch}\n"
            return ""
        return ""


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch, service: SourceUpdateService) -> _GitDouble:
    double = _GitDouble()
    monkeypatch.setattr(service, "_git", double)
    return double


# --- Actions: check ---


@pytest.mark.asyncio
async def test_action_check_reports_current_branch(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    git._current = "feature/foo"
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert result.data["current_branch"] == "feature/foo"
    assert result.data["dirty"] is False


@pytest.mark.asyncio
async def test_action_check_reports_dirty(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    git._dirty = " M src/foo.py\n"
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert result.data["dirty"] is True
    assert "dirty" in result.message


@pytest.mark.asyncio
async def test_action_check_handles_missing_origin(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    git.fail_origin = True
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    # _origin_url swallows the GitError and returns "" — check reports
    # "unset" rather than failing.
    assert "unset" in result.message


# --- Actions: apply ---


@pytest.mark.asyncio
async def test_apply_rejects_empty_target(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    service._target_branch = ""
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "empty" in result.message


@pytest.mark.asyncio
async def test_apply_rejects_shell_injection_in_branch_name(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    service._target_branch = "feature/foo; rm -rf /"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "shell-interpreted" in result.message


@pytest.mark.asyncio
async def test_apply_rejects_dirty_tree(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git._dirty = " M src/foo.py\n M src/bar.py\n"
    service._target_branch = "feature/bar"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "uncommitted changes" in result.message
    assert "src/foo.py" in result.message
    # Sentinel must not be written when we refuse.
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_noop_when_already_on_target(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git._current = "feature/foo"
    service._target_branch = "feature/foo"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "ok"
    assert "Already on branch" in result.message
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_rejects_branch_missing_on_origin(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git._remote_heads = {"main"}  # target not present
    service._target_branch = "feature/bar"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "does not exist on ``origin``" in result.message
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_surfaces_fetch_failure(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git.fail_fetch = True
    service._target_branch = "feature/bar"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "fetch failed" in result.message
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_happy_path_writes_sentinel_and_requests_restart(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git._current = "main"
    git._remote_heads = {"main", "feature/bar"}
    service._target_branch = "feature/bar"
    gilbert_stub = MagicMock()
    service.bind_gilbert(gilbert_stub)
    set_current_user(_admin())

    result = await service.invoke_config_action("apply", {})

    assert result.status == "ok"
    assert "queued" in result.message
    assert result.data == {"from_branch": "main", "to_branch": "feature/bar"}
    sentinel = repo_root / ".gilbert" / "pending-branch.txt"
    assert sentinel.read_text(encoding="utf-8").strip() == "feature/bar"
    gilbert_stub.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_apply_without_gilbert_binding_warns_and_does_not_restart(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git._current = "main"
    git._remote_heads = {"main", "feature/bar"}
    service._target_branch = "feature/bar"
    # No bind_gilbert call — _gilbert stays None.
    set_current_user(_admin())

    result = await service.invoke_config_action("apply", {})

    assert result.status == "error"
    assert "not bound" in result.message
    # The sentinel is still on disk so the user can recover with a
    # manual restart — this matches what the action's message tells them.
    sentinel = repo_root / ".gilbert" / "pending-branch.txt"
    assert sentinel.exists()


# --- Actions: unknown ---


@pytest.mark.asyncio
async def test_unknown_action_returns_error(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    result = await service.invoke_config_action("nope", {})
    assert result.status == "error"
    assert "Unknown source-update action" in result.message


# --- Action declarations ---


def test_actions_are_admin_only() -> None:
    svc = SourceUpdateService()
    actions = svc.config_actions()
    assert {a.key for a in actions} == {"check", "apply"}
    for a in actions:
        assert a.required_role == "admin", f"{a.key} should be admin-only"
    # Apply must require explicit confirmation in the UI.
    apply = next(a for a in actions if a.key == "apply")
    assert apply.confirm


def test_service_is_not_toggleable() -> None:
    # Disabling the update mechanism via UI would strand an admin who
    # then needs to switch branches to recover from a broken deploy.
    svc = SourceUpdateService()
    assert svc.service_info().toggleable is False


def test_config_namespace_and_category() -> None:
    svc = SourceUpdateService()
    assert svc.config_namespace == "source_update"
    assert svc.config_category == "System"


def test_target_branch_config_param_has_no_default_branch() -> None:
    # Empty default is intentional — clicking Apply with no value
    # should fail loudly rather than silently switching to ``main``.
    svc = SourceUpdateService()
    params = {p.key: p for p in svc.config_params()}
    assert "target_branch" in params
    assert params["target_branch"].default == ""


def test_invalid_branch_name_pattern() -> None:
    from gilbert.core.services.source_update import _BRANCH_RE
    # Valid
    assert _BRANCH_RE.match("main")
    assert _BRANCH_RE.match("feature/browser_speaker_backend")
    assert _BRANCH_RE.match("release-2026.05")
    assert _BRANCH_RE.match("v1.0.0")
    # Invalid
    assert not _BRANCH_RE.match("")
    assert not _BRANCH_RE.match("feature; rm -rf /")
    assert not _BRANCH_RE.match("$(whoami)")
    assert not _BRANCH_RE.match("feature/foo bar")  # space
    assert not _BRANCH_RE.match("/main")  # leading slash invalid
    assert not _BRANCH_RE.match("--upload-pack=evil")  # ``--`` arg injection


# --- Helper: _discover_repo_root ---


def test_discover_repo_root_walks_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gilbert.core.services.source_update import _discover_repo_root
    # Create a .git marker at tmp_path/root, cwd at tmp_path/root/a/b/c.
    root = tmp_path / "root"
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.chdir(nested)
    assert _discover_repo_root() == root.resolve()


def test_discover_repo_root_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gilbert.core.services.source_update import _discover_repo_root
    monkeypatch.chdir(tmp_path)
    # No .git anywhere — should return cwd rather than throw.
    assert _discover_repo_root() == tmp_path.resolve()


# --- Make sure unused imports don't break the test module ---


def test_module_exports() -> None:
    from gilbert.core.services.source_update import (
        SourceUpdateService as _Svc,
    )
    from gilbert.core.services.source_update import (
        _GitError as _Err,
    )
    assert _Svc is not None
    assert issubclass(_Err, RuntimeError)


_ = Any  # silence "imported but unused" if linters get strict
