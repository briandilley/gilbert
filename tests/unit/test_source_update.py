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
    """Records git arg vectors and serves canned output for each subcommand.

    Configurable via attributes:
    - ``current_branch`` (str): what ``symbolic-ref --short HEAD`` returns.
    - ``tracking_remotes`` (dict[branch, remote]): looked up by
       ``config branch.<name>.remote``.
    - ``remotes`` (dict[name, url]): what ``remote`` and ``remote get-url``
       see.
    - ``branches_by_remote`` (dict[remote, set[branch]]): drives both
       ``ls-remote --heads <remote>`` (full list) and the
       ``ls-remote --heads <remote> <branch>`` existence probe.
    - ``dirty`` (str): porcelain output for ``status``.
    - ``fail_fetch``, ``fail_origin``: error toggles.
    """

    def __init__(
        self,
        *,
        current: str = "main",
        tracking_remotes: dict[str, str] | None = None,
        remotes: dict[str, str] | None = None,
        branches_by_remote: dict[str, set[str]] | None = None,
        dirty: str = "",
        # Back-compat for older tests that used ``remote_heads`` as a
        # set: those tests assume an ``origin`` remote with that head set.
        remote_heads: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.current_branch = current
        self.tracking_remotes = (
            tracking_remotes if tracking_remotes is not None else {}
        )
        self.remotes = (
            remotes if remotes is not None else {"origin": "git@x:y.git"}
        )
        if branches_by_remote is not None:
            self.branches_by_remote = branches_by_remote
        elif remote_heads is not None:
            self.branches_by_remote = {"origin": set(remote_heads)}
        else:
            self.branches_by_remote = {"origin": {"main"}}
        self.dirty = dirty
        self.fail_fetch = False
        self.fail_origin = False

    async def __call__(self, *args: str) -> str:
        self.calls.append(args)
        first = args[0] if args else ""
        if first == "symbolic-ref":
            return self.current_branch + "\n"
        if first == "config":
            # ``config branch.<name>.remote`` — used to find a branch's
            # tracking remote. Empty string is a normal "no tracking"
            # state; raise to simulate a missing config key.
            key = args[1] if len(args) > 1 else ""
            if key.startswith("branch.") and key.endswith(".remote"):
                branch = key[len("branch.") : -len(".remote")]
                if branch in self.tracking_remotes:
                    return self.tracking_remotes[branch] + "\n"
                raise _GitError("not set")
            raise _GitError(f"unknown config key {key!r}")
        if first == "remote":
            if self.fail_origin:
                raise _GitError("remote listing failed")
            # ``remote`` alone → newline-separated list.
            # ``remote get-url <name>`` → URL or raises if unknown.
            if len(args) == 1:
                return "\n".join(sorted(self.remotes)) + "\n"
            if len(args) >= 3 and args[1] == "get-url":
                name = args[2]
                if name not in self.remotes:
                    raise _GitError(f"No such remote {name!r}")
                return self.remotes[name] + "\n"
            return ""
        if first == "status":
            return self.dirty
        if first == "fetch":
            if self.fail_fetch:
                raise _GitError("fetch failed")
            return ""
        if first == "ls-remote":
            # ``ls-remote --heads <remote>`` (full list, args length 3)
            # or ``ls-remote --heads <remote> <branch>`` (existence
            # probe, args length 4). Both filtered against
            # ``branches_by_remote``.
            remote = args[2] if len(args) >= 3 else "origin"
            heads = self.branches_by_remote.get(remote, set())
            if len(args) == 3:
                return (
                    "\n".join(f"sha-{b}\trefs/heads/{b}" for b in sorted(heads))
                    + ("\n" if heads else "")
                )
            branch = args[3] if len(args) >= 4 else ""
            if branch in heads:
                return f"abc123\trefs/heads/{branch}\n"
            return ""
        return ""

    # Aliases preserved so the existing assertions on legacy attrs keep
    # working — the old tests poked ``_current``, ``_dirty``, etc.
    @property
    def _current(self) -> str:
        return self.current_branch

    @_current.setter
    def _current(self, v: str) -> None:
        self.current_branch = v

    @property
    def _dirty(self) -> str:
        return self.dirty

    @_dirty.setter
    def _dirty(self, v: str) -> None:
        self.dirty = v

    @property
    def _remote_heads(self) -> set[str]:
        # Returns origin's heads for legacy single-remote tests.
        return self.branches_by_remote.get("origin", set())

    @_remote_heads.setter
    def _remote_heads(self, v: set[str]) -> None:
        self.branches_by_remote["origin"] = set(v)


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
    git.current_branch = "feature/foo"
    git.tracking_remotes = {"feature/foo": "origin"}
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert result.data["current_branch"] == "feature/foo"
    assert result.data["current_remote"] == "origin"
    assert result.data["dirty"] is False
    assert result.data["last_rollback"] is None


@pytest.mark.asyncio
async def test_action_check_reports_dirty(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    git.dirty = " M src/foo.py\n"
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert result.data["dirty"] is True
    assert "dirty" in result.message


@pytest.mark.asyncio
async def test_action_check_defaults_tracking_remote_to_origin(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    # No tracking_remotes entry — ``git config`` raises, service
    # falls back to "origin" rather than failing the action.
    git.current_branch = "detached-ish"
    git.tracking_remotes = {}
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert result.data["current_remote"] == "origin"


@pytest.mark.asyncio
async def test_action_check_surfaces_last_rollback(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    rollback_path = repo_root / ".gilbert" / "last-rollback.json"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path.write_text(
        '{"from_remote":"upstream","from_branch":"feature/oops",'
        '"to_remote":"origin","to_branch":"main","exit_code":1,'
        '"elapsed_seconds":12,"timestamp":"2026-05-17T14:23:11Z"}',
        encoding="utf-8",
    )
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert "Last auto-rollback" in result.message
    assert "upstream/feature/oops" in result.message
    assert "origin/main" in result.message
    rb = result.data["last_rollback"]
    assert rb is not None
    assert rb["from_branch"] == "feature/oops"
    assert rb["elapsed_seconds"] == 12


@pytest.mark.asyncio
async def test_action_check_silently_ignores_malformed_rollback(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    rollback_path = repo_root / ".gilbert" / "last-rollback.json"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path.write_text("not-json", encoding="utf-8")
    result = await service.invoke_config_action("check", {})
    assert result.status == "ok"
    assert result.data["last_rollback"] is None


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
    git.current_branch = "feature/foo"
    git.tracking_remotes = {"feature/foo": "origin"}
    service._target_remote = "origin"
    service._target_branch = "feature/foo"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "ok"
    assert "Already on origin/feature/foo" in result.message
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_rejects_branch_missing_on_origin(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git.branches_by_remote = {"origin": {"main"}}  # target not present
    service._target_branch = "feature/bar"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "does not exist on remote 'origin'" in result.message
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_rejects_unknown_remote(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    service._target_remote = "upstream"  # not configured locally
    service._target_branch = "main"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "not configured locally" in result.message
    assert not (repo_root / ".gilbert" / "pending-branch.txt").exists()


@pytest.mark.asyncio
async def test_apply_rejects_shell_injection_in_remote_name(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    service._target_remote = "origin; echo pwned"
    service._target_branch = "main"
    result = await service.invoke_config_action("apply", {})
    assert result.status == "error"
    assert "shell-interpreted" in result.message


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
async def test_apply_happy_path_writes_two_line_sentinel_and_requests_restart(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git.current_branch = "main"
    git.tracking_remotes = {"main": "origin"}
    git.branches_by_remote = {"origin": {"main", "feature/bar"}}
    service._target_branch = "feature/bar"
    gilbert_stub = MagicMock()
    service.bind_gilbert(gilbert_stub)
    set_current_user(_admin())

    result = await service.invoke_config_action("apply", {})

    assert result.status == "ok"
    assert "queued" in result.message
    assert result.data == {
        "from_remote": "origin",
        "from_branch": "main",
        "to_remote": "origin",
        "to_branch": "feature/bar",
    }
    sentinel = repo_root / ".gilbert" / "pending-branch.txt"
    # Two-line format: ``<remote>\n<branch>\n``.
    assert sentinel.read_text(encoding="utf-8") == "origin\nfeature/bar\n"
    gilbert_stub.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_apply_targets_non_origin_remote(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    # Multi-remote setup: switch from origin/main to upstream/main.
    git.current_branch = "main"
    git.tracking_remotes = {"main": "origin"}
    git.remotes = {
        "origin": "git@github.com:jereanon/gilbert.git",
        "upstream": "git@github.com:briandilley/gilbert.git",
    }
    git.branches_by_remote = {
        "origin": {"main"},
        "upstream": {"main", "feature/x"},
    }
    service._target_remote = "upstream"
    service._target_branch = "feature/x"
    gilbert_stub = MagicMock()
    service.bind_gilbert(gilbert_stub)
    set_current_user(_admin())

    result = await service.invoke_config_action("apply", {})

    assert result.status == "ok"
    sentinel = repo_root / ".gilbert" / "pending-branch.txt"
    assert sentinel.read_text(encoding="utf-8") == "upstream\nfeature/x\n"
    gilbert_stub.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_apply_same_branch_different_remote_is_a_real_switch(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    # Same branch name, different remote — should NOT be a no-op
    # because tracking gets repointed.
    git.current_branch = "main"
    git.tracking_remotes = {"main": "origin"}
    git.remotes = {
        "origin": "x",
        "upstream": "y",
    }
    git.branches_by_remote = {"origin": {"main"}, "upstream": {"main"}}
    service._target_remote = "upstream"
    service._target_branch = "main"
    gilbert_stub = MagicMock()
    service.bind_gilbert(gilbert_stub)
    set_current_user(_admin())

    result = await service.invoke_config_action("apply", {})

    assert result.status == "ok"
    assert "queued" in result.message
    sentinel = repo_root / ".gilbert" / "pending-branch.txt"
    assert sentinel.read_text(encoding="utf-8") == "upstream\nmain\n"


@pytest.mark.asyncio
async def test_apply_without_gilbert_binding_warns_and_does_not_restart(
    service: SourceUpdateService, git: _GitDouble, repo_root: Path
) -> None:
    git.current_branch = "main"
    git.branches_by_remote = {"origin": {"main", "feature/bar"}}
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


# --- Actions: refresh_branches + cache ---


@pytest.mark.asyncio
async def test_refresh_branches_populates_both_caches(
    service: SourceUpdateService, git: _GitDouble
) -> None:
    git.remotes = {"origin": "x", "upstream": "y"}
    git.branches_by_remote = {
        "origin": {"main", "feature/foo"},
        "upstream": {"main", "feature/bar", "develop"},
    }
    result = await service.invoke_config_action("refresh_branches", {})
    assert result.status == "ok"
    assert service.cached_remotes == ["origin", "upstream"]
    # ``target_remote`` defaults to ``origin`` so its branch dropdown
    # reflects origin's heads, sorted alphabetically.
    assert service.cached_target_remote_branches == [
        "feature/foo",
        "main",
    ]
    # Switching target_remote in-memory immediately exposes the other
    # remote's branches — no second refresh needed for that toggle.
    service._target_remote = "upstream"
    assert service.cached_target_remote_branches == [
        "develop",
        "feature/bar",
        "main",
    ]
    assert "2 remote(s)" in result.message
    assert "5 branch(es)" in result.message


@pytest.mark.asyncio
async def test_refresh_branches_partial_failure_keeps_prior_cache(
    service: SourceUpdateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Custom double: ``origin`` works, ``upstream`` fails on fetch.
    fail_remotes = {"upstream"}

    async def fake_git(*args: str) -> str:
        if args[0] == "remote" and len(args) == 1:
            return "origin\nupstream\n"
        if args[0] == "fetch":
            remote = args[2] if len(args) >= 3 else ""
            if remote in fail_remotes:
                raise _GitError(f"fetch failed for {remote}")
            return ""
        if args[0] == "ls-remote":
            remote = args[2]
            if remote == "origin":
                return "sha1\trefs/heads/main\nsha2\trefs/heads/feature/x\n"
            if remote == "upstream":
                return "sha3\trefs/heads/main\n"
            return ""
        return ""

    monkeypatch.setattr(service, "_git", fake_git)
    # Pre-seed upstream's cache so we can confirm the partial-failure
    # path preserves it.
    service._cached_branches_by_remote = {"upstream": ["main", "release/old"]}

    result = await service.invoke_config_action("refresh_branches", {})
    assert result.status == "pending"
    assert "upstream" in result.message
    # Origin refreshed cleanly.
    service._target_remote = "origin"
    assert service.cached_target_remote_branches == ["feature/x", "main"]
    # Upstream's cache survived since fetch failed before ls-remote.
    service._target_remote = "upstream"
    assert service.cached_target_remote_branches == ["main", "release/old"]


@pytest.mark.asyncio
async def test_refresh_branches_dedupes_and_ignores_garbage_lines(
    service: SourceUpdateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_git(*args: str) -> str:
        if args[0] == "remote" and len(args) == 1:
            return "origin\n"
        if args[0] == "fetch":
            return ""
        if args[0] == "ls-remote":
            return (
                "sha1\trefs/heads/main\n"
                "sha2\trefs/heads/main\n"  # duplicate
                "sha3\trefs/tags/v1.0\n"   # tag — ignore
                "garbage-line-no-tab\n"      # malformed — ignore
                "sha4\trefs/heads/feature/x\n"
            )
        return ""

    monkeypatch.setattr(service, "_git", fake_git)
    result = await service.invoke_config_action("refresh_branches", {})
    assert result.status == "ok"
    assert service.cached_target_remote_branches == ["feature/x", "main"]


def test_cached_target_remote_branches_returns_copy() -> None:
    svc = SourceUpdateService()
    svc._cached_branches_by_remote = {"origin": ["main", "feature/x"]}
    snapshot = svc.cached_target_remote_branches
    snapshot.append("hostile-mutation")
    # Internal state shouldn't be mutated by an outsider.
    assert svc._cached_branches_by_remote["origin"] == ["main", "feature/x"]


def test_cached_remotes_returns_copy() -> None:
    svc = SourceUpdateService()
    svc._cached_remotes = ["origin", "upstream"]
    snapshot = svc.cached_remotes
    snapshot.append("hostile")
    assert svc._cached_remotes == ["origin", "upstream"]


def test_service_implements_both_listers() -> None:
    from gilbert.interfaces.source_update import (
        GitRemoteLister,
        RemoteBranchLister,
    )
    svc = SourceUpdateService()
    assert isinstance(svc, RemoteBranchLister)
    assert isinstance(svc, GitRemoteLister)


def test_service_advertises_source_update_capability() -> None:
    svc = SourceUpdateService()
    assert "source_update" in svc.service_info().capabilities


def test_target_branch_param_uses_target_remote_branches_dropdown() -> None:
    svc = SourceUpdateService()
    params = {p.key: p for p in svc.config_params()}
    assert params["target_branch"].choices_from == "target_remote_branches"


def test_target_remote_param_uses_git_remotes_dropdown() -> None:
    svc = SourceUpdateService()
    params = {p.key: p for p in svc.config_params()}
    assert params["target_remote"].choices_from == "git_remotes"
    assert params["target_remote"].default == "origin"


def test_refresh_branches_action_is_admin_only() -> None:
    svc = SourceUpdateService()
    actions = {a.key: a for a in svc.config_actions()}
    assert "refresh_branches" in actions
    assert actions["refresh_branches"].required_role == "admin"
    # No confirm prompt — refreshing is read-only.
    assert actions["refresh_branches"].confirm == ""


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
    assert {a.key for a in actions} == {"check", "refresh_branches", "apply"}
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


def test_remote_name_pattern() -> None:
    from gilbert.core.services.source_update import _REMOTE_RE
    # Valid
    assert _REMOTE_RE.match("origin")
    assert _REMOTE_RE.match("upstream")
    assert _REMOTE_RE.match("my-fork")
    assert _REMOTE_RE.match("backup_2026")
    # Invalid — no slashes allowed in remote names, no shell metas
    assert not _REMOTE_RE.match("origin/foo")  # slashes reserved
    assert not _REMOTE_RE.match("origin; echo")
    assert not _REMOTE_RE.match("$(whoami)")
    assert not _REMOTE_RE.match("--upload-pack=evil")
    assert not _REMOTE_RE.match("")
    assert not _REMOTE_RE.match("a" * 65)  # too long


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
