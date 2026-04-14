"""Tests for SkillService — discovery, parsing, catalog, tools, and WS handlers."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.config import SkillsConfig
from gilbert.core.services.skills import SkillService


def _make_skill_service(
    directories: list[str] | None = None,
    user_dir: str = "",
) -> SkillService:
    """Create a SkillService with the given config attributes."""
    svc = SkillService()
    svc._enabled = True
    if directories is not None:
        svc._directories = directories
    if user_dir:
        svc._user_dir = user_dir
        user_path = Path(user_dir)
        svc._user_skills_dir = user_path if user_path.is_absolute() else Path.cwd() / user_path
    return svc


# --- Fixtures ---


@pytest.fixture
def tmp_skills_dir(tmp_path: Path) -> Path:
    """Create a temporary skills directory with a valid skill."""
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-skill\n"
        "description: A test skill for unit tests.\n"
        "allowed-tools: tool_a tool_b\n"
        "metadata:\n"
        "  category: testing\n"
        "  icon: flask\n"
        "  required-role: user\n"
        "---\n\n"
        "# Test Skill\n\n"
        "Do test things.\n"
    )
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\nSome reference content.")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("print('hello from script')")
    return tmp_path / "skills"


@pytest.fixture
def config(tmp_skills_dir: Path, tmp_path: Path) -> SkillsConfig:
    return SkillsConfig(
        directories=[str(tmp_skills_dir)],
        user_dir=str(tmp_path / "user-skills"),
    )


@pytest.fixture
def service(config: SkillsConfig) -> SkillService:
    svc = SkillService()
    svc._directories = config.directories
    svc._user_dir = config.user_dir
    user_dir = Path(config.user_dir)
    svc._user_skills_dir = user_dir if user_dir.is_absolute() else Path.cwd() / user_dir
    svc._enabled = True
    return svc


@pytest.fixture
def mock_resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.get_capability.return_value = None
    return resolver


@pytest.fixture
async def started_service(
    service: SkillService, mock_resolver: MagicMock,
) -> SkillService:
    await service.start(mock_resolver)
    return service


# --- SKILL.md Parsing ---


class TestParseSkillMd:
    def test_valid_skill(self, tmp_skills_dir: Path) -> None:
        skill_md = tmp_skills_dir / "test-skill" / "SKILL.md"
        fm, body = SkillService._parse_skill_md(skill_md)
        assert fm is not None
        assert fm["name"] == "test-skill"
        assert fm["description"] == "A test skill for unit tests."
        assert fm["allowed-tools"] == "tool_a tool_b"
        assert body is not None
        assert "# Test Skill" in body

    def test_missing_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "no-front.md"
        f.write_text("# Just markdown\nNo frontmatter here.")
        fm, body = SkillService._parse_skill_md(f)
        assert fm is None
        assert body is None

    def test_empty_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.md"
        f.write_text("---\n---\nBody content.")
        fm, body = SkillService._parse_skill_md(f)
        assert fm is None

    def test_malformed_yaml_with_colon(self, tmp_path: Path) -> None:
        f = tmp_path / "colon.md"
        f.write_text(
            "---\n"
            "name: my-skill\n"
            "description: Use this skill when: the user asks about PDFs\n"
            "---\n"
            "Body.\n"
        )
        fm, body = SkillService._parse_skill_md(f)
        assert fm is not None
        assert fm["name"] == "my-skill"

    def test_unparseable_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.md"
        f.write_text("---\n[[[invalid yaml\n---\nBody.\n")
        fm, body = SkillService._parse_skill_md(f)
        assert fm is None

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        fm, body = SkillService._parse_skill_md(tmp_path / "nope.md")
        assert fm is None

    def test_allowed_tools_as_list(self, tmp_path: Path) -> None:
        f = tmp_path / "list-tools.md"
        f.write_text(
            "---\n"
            "name: list-tools\n"
            "description: Skill with list-style tools.\n"
            "allowed-tools:\n"
            "  - tool_x\n"
            "  - tool_y\n"
            "---\n"
            "Body.\n"
        )
        fm, _ = SkillService._parse_skill_md(f)
        assert fm is not None
        assert fm["allowed-tools"] == ["tool_x", "tool_y"]


# --- Skill Discovery ---


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_skill(self, started_service: SkillService) -> None:
        assert "test-skill" in started_service._catalog

    @pytest.mark.asyncio
    async def test_catalog_entry_fields(self, started_service: SkillService) -> None:
        entry = started_service._catalog["test-skill"]
        assert entry.name == "test-skill"
        assert entry.description == "A test skill for unit tests."
        assert entry.category == "testing"
        assert entry.icon == "flask"
        assert entry.required_role == "user"
        assert entry.allowed_tools == ["tool_a", "tool_b"]
        assert entry.scope == "global"
        assert entry.owner_id == ""

    @pytest.mark.asyncio
    async def test_missing_description_skipped(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        skill_dir = tmp_path / "skills" / "no-desc"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: no-desc\n---\nBody.\n"
        )
        svc = _make_skill_service(
            directories=[str(tmp_path / "skills")],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        assert "no-desc" not in svc._catalog

    @pytest.mark.asyncio
    async def test_name_defaults_to_directory(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        skill_dir = tmp_path / "skills" / "dir-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: A skill without a name field.\n---\nBody.\n"
        )
        svc = _make_skill_service(
            directories=[str(tmp_path / "skills")],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        assert "dir-name" in svc._catalog

    @pytest.mark.asyncio
    async def test_duplicate_name_warns(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        for d in ("a", "b"):
            skill_dir = tmp_path / "skills" / d
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: dupe\ndescription: Duplicate.\n---\nBody.\n"
            )
        svc = _make_skill_service(
            directories=[str(tmp_path / "skills")],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        assert "dupe" in svc._catalog

    @pytest.mark.asyncio
    async def test_nonexistent_directory_ignored(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        svc = _make_skill_service(
            directories=["/nonexistent/path"],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        assert len(svc._catalog) == 0


# --- Per-User Skill Discovery ---


class TestPerUserDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_user_skills(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        user_dir = tmp_path / "user-skills" / "users" / "alice"
        skill_dir = user_dir / "alice-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: alice-skill\ndescription: Alice's skill.\n---\nBody.\n"
        )
        svc = _make_skill_service(
            directories=[],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        key = "alice:alice-skill"
        assert key in svc._catalog
        assert svc._catalog[key].scope == "user"
        assert svc._catalog[key].owner_id == "alice"

    @pytest.mark.asyncio
    async def test_catalog_hides_other_users_skills(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        # Create skills for alice and bob
        for user in ("alice", "bob"):
            skill_dir = tmp_path / "user-skills" / "users" / user / f"{user}-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {user}-skill\ndescription: {user}'s skill.\n---\nBody.\n"
            )
        svc = _make_skill_service(
            directories=[],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)

        # Alice should only see her skill
        alice_ctx = MagicMock()
        alice_ctx.user_id = "alice"
        catalog = svc.get_catalog(alice_ctx)
        names = [e.name for _, e in catalog]
        assert "alice-skill" in names
        assert "bob-skill" not in names

    @pytest.mark.asyncio
    async def test_user_and_global_no_collision(
        self, tmp_path: Path, mock_resolver: MagicMock,
    ) -> None:
        # Global skill
        global_dir = tmp_path / "user-skills" / "shared"
        global_dir.mkdir(parents=True)
        (global_dir / "SKILL.md").write_text(
            "---\nname: shared\ndescription: Global.\n---\nBody.\n"
        )
        # User skill with same name
        user_dir = tmp_path / "user-skills" / "users" / "alice" / "shared"
        user_dir.mkdir(parents=True)
        (user_dir / "SKILL.md").write_text(
            "---\nname: shared\ndescription: Alice's version.\n---\nBody.\n"
        )
        svc = _make_skill_service(
            directories=[],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        assert "shared" in svc._catalog
        assert "alice:shared" in svc._catalog


# --- Skill Content Loading ---


class TestSkillContent:
    @pytest.mark.asyncio
    async def test_get_content(self, started_service: SkillService) -> None:
        content = started_service.get_skill_content("test-skill")
        assert content is not None
        assert "# Test Skill" in content.instructions
        assert content.catalog.name == "test-skill"

    @pytest.mark.asyncio
    async def test_content_resources(self, started_service: SkillService) -> None:
        content = started_service.get_skill_content("test-skill")
        assert content is not None
        assert "references/guide.md" in content.resources
        assert "scripts/helper.py" in content.resources

    @pytest.mark.asyncio
    async def test_content_cached(self, started_service: SkillService) -> None:
        c1 = started_service.get_skill_content("test-skill")
        c2 = started_service.get_skill_content("test-skill")
        assert c1 is c2

    @pytest.mark.asyncio
    async def test_unknown_skill(self, started_service: SkillService) -> None:
        assert started_service.get_skill_content("nonexistent") is None


# --- Active Skills Per Conversation ---


class TestActiveSkills:
    @pytest.mark.asyncio
    async def test_get_active_no_ai_service(
        self, started_service: SkillService,
    ) -> None:
        result = await started_service.get_active_skills("conv-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_set_active_skills(
        self, service: SkillService, mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=["test-skill"])
        ai_svc.set_conversation_state = AsyncMock()
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = (
            lambda cap: ai_svc if cap == "ai_chat" else None
        )

        active = await service.get_active_skills("conv-1")
        assert active == ["test-skill"]

        await service.set_active_skills("conv-1", ["test-skill"])
        ai_svc.set_conversation_state.assert_called_once_with(
            "active_skills", ["test-skill"], "conv-1",
        )

    @pytest.mark.asyncio
    async def test_set_filters_invalid_names(
        self, service: SkillService, mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.set_conversation_state = AsyncMock()
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = (
            lambda cap: ai_svc if cap == "ai_chat" else None
        )

        await service.set_active_skills("conv-1", ["test-skill", "nonexistent"])
        ai_svc.set_conversation_state.assert_called_once_with(
            "active_skills", ["test-skill"], "conv-1",
        )


# --- build_skills_context ---


class TestBuildSkillsContext:
    @pytest.mark.asyncio
    async def test_no_active_skills(
        self, service: SkillService, mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=[])
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = (
            lambda cap: ai_svc if cap == "ai_chat" else None
        )

        ctx = await service.build_skills_context("conv-1")
        assert ctx == ""

    @pytest.mark.asyncio
    async def test_active_skills_context(
        self, service: SkillService, mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=["test-skill"])
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = (
            lambda cap: ai_svc if cap == "ai_chat" else None
        )

        ctx = await service.build_skills_context("conv-1")
        assert "## Active Skills" in ctx
        assert "### test-skill" in ctx
        assert "# Test Skill" in ctx
        assert '<skill_content name="test-skill">' in ctx
        assert "</skill_content>" in ctx
        assert "<skill_resources>" in ctx


# --- Allowed Tools ---


class TestAllowedTools:
    @pytest.mark.asyncio
    async def test_get_allowed_tools(self, started_service: SkillService) -> None:
        tools = started_service.get_active_allowed_tools(["test-skill"])
        assert tools == {"tool_a", "tool_b"}

    @pytest.mark.asyncio
    async def test_no_skills_no_tools(self, started_service: SkillService) -> None:
        tools = started_service.get_active_allowed_tools([])
        assert tools == set()

    @pytest.mark.asyncio
    async def test_unknown_skill_ignored(self, started_service: SkillService) -> None:
        tools = started_service.get_active_allowed_tools(["nonexistent"])
        assert tools == set()


# --- Workspace ---


class TestWorkspace:
    @pytest.mark.asyncio
    async def test_workspace_created(self, started_service: SkillService) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        assert workspace.is_dir()
        assert "skill-workspaces/alice/test-skill" in str(workspace)

    @pytest.mark.asyncio
    async def test_run_script_uses_workspace(
        self, started_service: SkillService, tmp_path: Path,
    ) -> None:
        """Script runs in workspace, not skill dir."""
        result = await started_service.execute_tool(
            "run_skill_script",
            {
                "skill_name": "test-skill",
                "script": "scripts/helper.py",
                "_user_id": "alice",
            },
        )
        assert "hello from script" in result
        workspace = started_service._get_workspace("alice", "test-skill")
        assert workspace.is_dir()

    @pytest.mark.asyncio
    async def test_browse_empty_workspace(
        self, started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "browse_skill_workspace",
            {"skill_name": "test-skill", "_user_id": "alice"},
        )
        assert '"files": []' in result

    @pytest.mark.asyncio
    async def test_browse_workspace_with_files(
        self, started_service: SkillService,
    ) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "output.txt").write_text("result data")
        result = await started_service.execute_tool(
            "browse_skill_workspace",
            {"skill_name": "test-skill", "_user_id": "alice"},
        )
        assert "output.txt" in result

    @pytest.mark.asyncio
    async def test_read_workspace_file(
        self, started_service: SkillService,
    ) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "data.csv").write_text("a,b,c\n1,2,3")
        result = await started_service.execute_tool(
            "read_skill_workspace_file",
            {"skill_name": "test-skill", "path": "data.csv", "_user_id": "alice"},
        )
        assert "a,b,c" in result

    @pytest.mark.asyncio
    async def test_read_workspace_path_traversal(
        self, started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "read_skill_workspace_file",
            {"skill_name": "test-skill", "path": "../../etc/passwd", "_user_id": "alice"},
        )
        assert "traversal" in result.lower()


# --- Tool: read_skill_file ---


class TestReadSkillFile:
    @pytest.mark.asyncio
    async def test_read_valid_file(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "read_skill_file",
            {"skill_name": "test-skill", "path": "references/guide.md", "_user_id": "alice"},
        )
        assert "# Guide" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "read_skill_file",
            {"skill_name": "test-skill", "path": "nope.txt", "_user_id": "alice"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "read_skill_file",
            {"skill_name": "test-skill", "path": "../../etc/passwd", "_user_id": "alice"},
        )
        assert "traversal" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_skill(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "read_skill_file",
            {"skill_name": "nonexistent", "path": "file.md", "_user_id": "alice"},
        )
        assert "error" in result


# --- Tool: run_skill_script ---


class TestRunSkillScript:
    @pytest.mark.asyncio
    async def test_run_python_script(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "run_skill_script",
            {"skill_name": "test-skill", "script": "scripts/helper.py", "_user_id": "alice"},
        )
        assert "hello from script" in result

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "run_skill_script",
            {"skill_name": "test-skill", "script": "../../etc/passwd", "_user_id": "alice"},
        )
        assert "traversal" in result.lower()

    @pytest.mark.asyncio
    async def test_nonexistent_script(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "run_skill_script",
            {"skill_name": "test-skill", "script": "scripts/nope.py", "_user_id": "alice"},
        )
        assert "error" in result


# --- Tool: manage_skills ---


class TestManageSkills:
    @pytest.mark.asyncio
    async def test_list_skills(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "manage_skills", {"action": "list", "_user_id": "alice"},
        )
        assert "test-skill" in result

    @pytest.mark.asyncio
    async def test_install_no_url(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "manage_skills", {"action": "install", "_user_id": "alice"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_install_requires_scope(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "manage_skills",
            {"action": "install", "url": "https://github.com/test/repo", "_user_id": "alice"},
        )
        assert "needs_scope" in result

    @pytest.mark.asyncio
    async def test_uninstall_builtin_rejected(
        self, started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "manage_skills",
            {"action": "uninstall", "skill_name": "test-skill", "_user_id": "alice"},
        )
        assert "error" in result.lower() or "Cannot" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "manage_skills", {"action": "bogus", "_user_id": "alice"},
        )
        assert "error" in result


# --- WS Handlers ---


class TestWsHandlers:
    @pytest.mark.asyncio
    async def test_ws_skills_list(self, started_service: SkillService) -> None:
        conn = MagicMock()
        conn.user_ctx = None
        result = await started_service._ws_skills_list(
            conn, {"id": "f1"},
        )
        assert result["type"] == "skills.list.result"
        assert len(result["skills"]) == 1
        assert result["skills"][0]["name"] == "test-skill"
        assert result["skills"][0]["scope"] == "global"

    @pytest.mark.asyncio
    async def test_ws_skills_toggle(
        self, service: SkillService, mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=[])
        ai_svc.set_conversation_state = AsyncMock()
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = (
            lambda cap: ai_svc if cap == "ai_chat" else None
        )

        conn = MagicMock()
        result = await service._ws_skills_toggle(conn, {
            "id": "f2",
            "conversation_id": "conv-1",
            "skill": "test-skill",
            "enabled": True,
        })
        assert result["type"] == "skills.conversation.toggle.result"
        assert result["skill"] == "test-skill"
        assert result["enabled"] is True

    @pytest.mark.asyncio
    async def test_ws_toggle_unknown_skill(
        self, started_service: SkillService,
    ) -> None:
        conn = MagicMock()
        result = await started_service._ws_skills_toggle(conn, {
            "id": "f3",
            "conversation_id": "conv-1",
            "skill": "nonexistent",
            "enabled": True,
        })
        assert result["type"] == "gilbert.error"
        assert result["code"] == 404

    @pytest.mark.asyncio
    async def test_ws_toggle_missing_fields(
        self, started_service: SkillService,
    ) -> None:
        conn = MagicMock()
        result = await started_service._ws_skills_toggle(conn, {"id": "f4"})
        assert result["type"] == "gilbert.error"
        assert result["code"] == 400

    @pytest.mark.asyncio
    async def test_ws_active_no_conversation(
        self, started_service: SkillService,
    ) -> None:
        conn = MagicMock()
        result = await started_service._ws_skills_active(conn, {"id": "f5"})
        assert result["active_skills"] == []

    @pytest.mark.asyncio
    async def test_ws_workspace_browse(
        self, started_service: SkillService,
    ) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "file.txt").write_text("content")
        conn = MagicMock()
        conn.user_id = "alice"
        result = await started_service._ws_workspace_browse(conn, {
            "id": "f6",
            "skill_name": "test-skill",
        })
        assert result["type"] == "skills.workspace.browse.result"
        assert len(result["files"]) == 1
        assert result["files"][0]["path"] == "file.txt"

    @pytest.mark.asyncio
    async def test_ws_workspace_download(
        self, started_service: SkillService,
    ) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "data.csv").write_text("a,b\n1,2")
        conn = MagicMock()
        conn.user_id = "alice"
        result = await started_service._ws_workspace_download(conn, {
            "id": "f7",
            "skill_name": "test-skill",
            "path": "data.csv",
        })
        assert result["type"] == "skills.workspace.download.result"
        assert result["filename"] == "data.csv"
        assert "content_base64" in result


# --- GitHub Installation ---


class TestGitHubInstall:
    @pytest.fixture
    def install_service(self, tmp_path: Path) -> SkillService:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        return _make_skill_service(
            directories=[str(skills_dir)],
            user_dir=str(tmp_path / "user-skills"),
        )

    @pytest.mark.asyncio
    async def test_install_user_scope(
        self, install_service: SkillService, mock_resolver: MagicMock, tmp_path: Path,
    ) -> None:
        await install_service.start(mock_resolver)

        repo_dir = tmp_path / "fake-repo"
        skill_dir = repo_dir / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: Installed skill.\n---\nBody.\n"
        )

        with patch.object(install_service, "_fetch_from_github", return_value=repo_dir):
            result = await install_service.execute_tool(
                "manage_skills",
                {
                    "action": "install",
                    "url": "https://github.com/test/repo",
                    "scope": "user",
                    "_user_id": "alice",
                },
            )
        assert "my-skill" in result
        assert "alice:my-skill" in install_service._catalog
        assert install_service._catalog["alice:my-skill"].scope == "user"

    @pytest.mark.asyncio
    async def test_install_global_scope(
        self, install_service: SkillService, mock_resolver: MagicMock, tmp_path: Path,
    ) -> None:
        await install_service.start(mock_resolver)

        repo_dir = tmp_path / "skill-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "SKILL.md").write_text(
            "---\nname: global-skill\ndescription: Global skill.\n---\nBody.\n"
        )

        with patch.object(install_service, "_fetch_from_github", return_value=repo_dir):
            result = await install_service.execute_tool(
                "manage_skills",
                {
                    "action": "install",
                    "url": "https://github.com/test/repo",
                    "scope": "global",
                    "_user_id": "admin",
                },
            )
        assert "global-skill" in result
        assert "global-skill" in install_service._catalog
        assert install_service._catalog["global-skill"].scope == "global"

    @pytest.mark.asyncio
    async def test_install_no_skills_in_repo(
        self, install_service: SkillService, mock_resolver: MagicMock, tmp_path: Path,
    ) -> None:
        await install_service.start(mock_resolver)

        repo_dir = tmp_path / "empty-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("# Not a skill")

        with patch.object(install_service, "_fetch_from_github", return_value=repo_dir):
            result = await install_service.execute_tool(
                "manage_skills",
                {
                    "action": "install",
                    "url": "https://github.com/test/empty",
                    "scope": "user",
                    "_user_id": "alice",
                },
            )
        assert "error" in result


# --- Archive Installation ---


class TestArchiveInstall:
    @pytest.fixture
    def install_service(self, tmp_path: Path) -> SkillService:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        return _make_skill_service(
            directories=[str(skills_dir)],
            user_dir=str(tmp_path / "user-skills"),
        )

    def _make_zip(self, tmp_path: Path) -> bytes:
        """Create a zip archive containing a skill."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "my-skill/SKILL.md",
                "---\nname: my-skill\ndescription: Zip skill.\n---\nBody.\n",
            )
        return buf.getvalue()

    def _make_targz(self, tmp_path: Path) -> bytes:
        """Create a tar.gz archive containing a skill."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            content = b"---\nname: tar-skill\ndescription: Tar skill.\n---\nBody.\n"
            info = tarfile.TarInfo(name="tar-skill/SKILL.md")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_install_from_zip(
        self, install_service: SkillService, mock_resolver: MagicMock, tmp_path: Path,
    ) -> None:
        await install_service.start(mock_resolver)
        zip_data = self._make_zip(tmp_path)

        with patch("gilbert.core.services.skills.httpx.get") as mock_get:
            resp = MagicMock()
            resp.content = zip_data
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp

            result = await install_service.execute_tool(
                "manage_skills",
                {
                    "action": "install",
                    "url": "https://example.com/skill.zip",
                    "scope": "user",
                    "_user_id": "alice",
                },
            )
        assert "my-skill" in result

    @pytest.mark.asyncio
    async def test_install_from_targz(
        self, install_service: SkillService, mock_resolver: MagicMock, tmp_path: Path,
    ) -> None:
        await install_service.start(mock_resolver)
        tgz_data = self._make_targz(tmp_path)

        with patch("gilbert.core.services.skills.httpx.get") as mock_get:
            resp = MagicMock()
            resp.content = tgz_data
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp

            result = await install_service.execute_tool(
                "manage_skills",
                {
                    "action": "install",
                    "url": "https://example.com/skill.tar.gz",
                    "scope": "user",
                    "_user_id": "alice",
                },
            )
        assert "tar-skill" in result

    @pytest.mark.asyncio
    async def test_git_url_still_works(
        self, install_service: SkillService, mock_resolver: MagicMock, tmp_path: Path,
    ) -> None:
        """Non-archive URLs still go through git clone."""
        await install_service.start(mock_resolver)

        repo_dir = tmp_path / "repo"
        skill_dir = repo_dir / "git-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: git-skill\ndescription: Git skill.\n---\nBody.\n"
        )

        with patch.object(install_service, "_fetch_from_github", return_value=repo_dir):
            result = await install_service.execute_tool(
                "manage_skills",
                {
                    "action": "install",
                    "url": "https://github.com/test/repo",
                    "scope": "user",
                    "_user_id": "alice",
                },
            )
        assert "git-skill" in result


# --- Service Info ---


class TestServiceInfo:
    def test_service_info(self, service: SkillService) -> None:
        info = service.service_info()
        assert info.name == "skills"
        assert "skills" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "ws_handlers" in info.capabilities

    def test_tool_provider_name(self, service: SkillService) -> None:
        assert service.tool_provider_name == "skills"

    def test_get_tools(self, service: SkillService) -> None:
        tools = service.get_tools()
        names = {t.name for t in tools}
        assert names == {
            "manage_skills", "create_skill", "read_skill_file",
            "run_skill_script", "browse_skill_workspace",
            "read_skill_workspace_file",
        }


# --- Entity-Stored Skills ---

_VALID_SKILL_MD = (
    "---\n"
    "name: test-entity-skill\n"
    "description: A test skill stored in entity storage.\n"
    "metadata:\n"
    "  category: testing\n"
    "---\n\n"
    "# Test Entity Skill\n\n"
    "Follow these instructions.\n"
)


class TestCreateSkill:
    @pytest.fixture
    def storage_service(self, tmp_path: Path) -> tuple[SkillService, AsyncMock]:
        storage = AsyncMock()
        storage.query = AsyncMock(return_value=[])
        storage.put = AsyncMock()
        storage.delete = AsyncMock()
        storage.ensure_index = AsyncMock()

        svc = _make_skill_service(
            directories=[],
            user_dir=str(tmp_path / "user-skills"),
        )
        return svc, storage

    @pytest.mark.asyncio
    async def test_create_skill(self, storage_service: tuple[SkillService, AsyncMock]) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = (
            lambda cap: storage if cap == "entity_storage" else None
        )
        # Simulate StorageProvider protocol for isinstance check
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        result = await svc.execute_tool("create_skill", {
            "skill_md": _VALID_SKILL_MD,
            "scope": "user",
            "_user_id": "alice",
        })
        assert "created" in result
        assert "test-entity-skill" in result
        assert "alice:test-entity-skill" in svc._catalog
        assert svc._catalog["alice:test-entity-skill"].location is None

    @pytest.mark.asyncio
    async def test_create_skill_content_cached(
        self, storage_service: tuple[SkillService, AsyncMock],
    ) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = (
            lambda cap: storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        await svc.execute_tool("create_skill", {
            "skill_md": _VALID_SKILL_MD,
            "scope": "user",
            "_user_id": "alice",
        })
        content = svc.get_skill_content("alice:test-entity-skill")
        assert content is not None
        assert "# Test Entity Skill" in content.instructions
        assert content.resources == []

    @pytest.mark.asyncio
    async def test_create_skill_missing_frontmatter(
        self, storage_service: tuple[SkillService, AsyncMock],
    ) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = (
            lambda cap: storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        result = await svc.execute_tool("create_skill", {
            "skill_md": "No frontmatter here",
            "_user_id": "alice",
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_create_skill_missing_name(
        self, storage_service: tuple[SkillService, AsyncMock],
    ) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = (
            lambda cap: storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        result = await svc.execute_tool("create_skill", {
            "skill_md": "---\ndescription: No name.\n---\nBody.",
            "_user_id": "alice",
        })
        assert "error" in result


class TestDeleteEntitySkill:
    @pytest.mark.asyncio
    async def test_delete_entity_skill(self, tmp_path: Path) -> None:
        storage = AsyncMock()
        storage.ensure_index = AsyncMock()
        storage.put = AsyncMock()
        storage.delete = AsyncMock()
        # Start with the skill in storage
        storage.query = AsyncMock(return_value=[
            {"_id": "skill_my-skill", "skill_md": _VALID_SKILL_MD,
             "scope": "user", "owner_id": "alice"},
        ])

        svc = _make_skill_service(
            directories=[], user_dir=str(tmp_path / "user-skills"),
        )
        resolver = MagicMock()
        resolver.get_capability.side_effect = (
            lambda cap: storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        # Skill should be in catalog
        assert "alice:test-entity-skill" in svc._catalog

        # Delete it
        result = await svc.execute_tool("manage_skills", {
            "action": "delete",
            "skill_name": "test-entity-skill",
            "_user_id": "alice",
        })
        assert "deleted" in result
        assert "alice:test-entity-skill" not in svc._catalog

    @pytest.mark.asyncio
    async def test_cannot_delete_file_based_skill(
        self, started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool("manage_skills", {
            "action": "delete",
            "skill_name": "test-skill",
            "_user_id": "alice",
        })
        assert "file-based" in result.lower() or "uninstall" in result.lower()
