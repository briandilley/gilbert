"""Tests for SkillService — discovery, parsing, catalog, tools, and WS handlers."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any
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
    service: SkillService,
    mock_resolver: MagicMock,
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
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
    ) -> None:
        skill_dir = tmp_path / "skills" / "no-desc"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: no-desc\n---\nBody.\n")
        svc = _make_skill_service(
            directories=[str(tmp_path / "skills")],
            user_dir=str(tmp_path / "user-skills"),
        )
        await svc.start(mock_resolver)
        assert "no-desc" not in svc._catalog

    @pytest.mark.asyncio
    async def test_name_defaults_to_directory(
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
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
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
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
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
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
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
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
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
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
        self,
        tmp_path: Path,
        mock_resolver: MagicMock,
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
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.get_active_skills("conv-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_set_active_skills(
        self,
        service: SkillService,
        mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=["test-skill"])
        ai_svc.set_conversation_state = AsyncMock()
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = lambda cap: ai_svc if cap == "ai_chat" else None

        active = await service.get_active_skills("conv-1")
        assert active == ["test-skill"]

        await service.set_active_skills("conv-1", ["test-skill"])
        ai_svc.set_conversation_state.assert_called_once_with(
            "active_skills",
            ["test-skill"],
            "conv-1",
        )

    @pytest.mark.asyncio
    async def test_set_filters_invalid_names(
        self,
        service: SkillService,
        mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.set_conversation_state = AsyncMock()
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = lambda cap: ai_svc if cap == "ai_chat" else None

        await service.set_active_skills("conv-1", ["test-skill", "nonexistent"])
        ai_svc.set_conversation_state.assert_called_once_with(
            "active_skills",
            ["test-skill"],
            "conv-1",
        )


# --- build_skills_context ---


class TestBuildSkillsContext:
    @pytest.mark.asyncio
    async def test_no_active_skills(
        self,
        service: SkillService,
        mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=[])
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = lambda cap: ai_svc if cap == "ai_chat" else None

        ctx = await service.build_skills_context("conv-1")
        assert ctx == ""

    @pytest.mark.asyncio
    async def test_active_skills_context(
        self,
        service: SkillService,
        mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=["test-skill"])
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = lambda cap: ai_svc if cap == "ai_chat" else None

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
        self,
        started_service: SkillService,
        tmp_path: Path,
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
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "browse_skill_workspace",
            {"skill_name": "test-skill", "_user_id": "alice"},
        )
        assert '"files": []' in result

    @pytest.mark.asyncio
    async def test_browse_workspace_with_files(
        self,
        started_service: SkillService,
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
        self,
        started_service: SkillService,
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
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "read_skill_workspace_file",
            {"skill_name": "test-skill", "path": "../../etc/passwd", "_user_id": "alice"},
        )
        assert "traversal" in result.lower()

    @pytest.mark.asyncio
    async def test_attach_workspace_file_returns_reference_attachment(
        self,
        started_service: SkillService,
    ) -> None:
        """attach_workspace_file returns a ToolResult whose attachment
        references the workspace file without inlining bytes."""
        from gilbert.interfaces.tools import ToolResult

        workspace = started_service._get_workspace("alice", "test-skill")
        pdf_path = workspace / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake pdf bytes")

        result = await started_service.execute_tool(
            "attach_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "report.pdf",
                "_user_id": "alice",
            },
        )
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert len(result.attachments) == 1
        att = result.attachments[0]
        # Reference-mode — no inline data, just coordinates.
        assert att.is_reference
        assert att.data == ""
        assert att.text == ""
        assert att.workspace_skill == "test-skill"
        assert att.workspace_path == "report.pdf"
        assert att.name == "report.pdf"
        assert att.kind == "document"
        assert att.media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_attach_workspace_file_path_traversal(
        self,
        started_service: SkillService,
    ) -> None:
        from gilbert.interfaces.tools import ToolResult

        result = await started_service.execute_tool(
            "attach_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "../../etc/passwd",
                "_user_id": "alice",
            },
        )
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert len(result.attachments) == 0
        assert "traversal" in result.content.lower()

    @pytest.mark.asyncio
    async def test_attach_workspace_file_missing_file(
        self,
        started_service: SkillService,
    ) -> None:
        from gilbert.interfaces.tools import ToolResult

        result = await started_service.execute_tool(
            "attach_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "nope.pdf",
                "_user_id": "alice",
            },
        )
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_write_workspace_file_creates_file(
        self,
        started_service: SkillService,
    ) -> None:
        """write_skill_workspace_file writes content to the workspace and
        the file is subsequently readable via read_skill_workspace_file."""
        result = await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "generate_po.py",
                "content": "print('hello from generated script')\n",
                "_user_id": "alice",
            },
        )
        data = json.loads(result)
        assert data["status"] == "written"
        assert data["path"] == "generate_po.py"
        assert data["bytes"] > 0

        # Round-trip: the written file is readable via the existing tool
        read = await started_service.execute_tool(
            "read_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "generate_po.py",
                "_user_id": "alice",
            },
        )
        assert "hello from generated script" in read

    @pytest.mark.asyncio
    async def test_write_workspace_file_creates_parent_dirs(
        self,
        started_service: SkillService,
    ) -> None:
        """Nested paths get their parent directories created on demand."""
        result = await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "configs/po/template.json",
                "content": "{}",
                "_user_id": "alice",
            },
        )
        data = json.loads(result)
        assert data["status"] == "written"
        workspace = started_service._get_workspace("alice", "test-skill")
        assert (workspace / "configs" / "po" / "template.json").is_file()

    @pytest.mark.asyncio
    async def test_write_workspace_file_path_traversal(
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "../../../etc/evil",
                "content": "pwned",
                "_user_id": "alice",
            },
        )
        data = json.loads(result)
        assert "traversal" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_write_workspace_file_size_cap(
        self,
        started_service: SkillService,
    ) -> None:
        huge = "x" * (513 * 1024)
        result = await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "big.txt",
                "content": huge,
                "_user_id": "alice",
            },
        )
        data = json.loads(result)
        assert "too large" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_run_workspace_script_executes_written_script(
        self,
        started_service: SkillService,
    ) -> None:
        """End-to-end: write a script to the workspace, run it, see output."""
        await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "echo_args.py",
                "content": (
                    "import sys\n"
                    "print('args:', ' '.join(sys.argv[1:]))\n"
                ),
                "_user_id": "alice",
            },
        )
        result = await started_service.execute_tool(
            "run_workspace_script",
            {
                "skill_name": "test-skill",
                "path": "echo_args.py",
                "arguments": ["alpha", "beta"],
                "_user_id": "alice",
            },
        )
        assert "args: alpha beta" in result

    @pytest.mark.asyncio
    async def test_run_workspace_script_output_landed_in_workspace(
        self,
        started_service: SkillService,
    ) -> None:
        """A script that writes to its cwd lands the output file back in
        the workspace, where attach_workspace_file can pick it up."""
        from gilbert.interfaces.tools import ToolResult

        await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "write_file.py",
                "content": (
                    "with open('result.txt', 'w') as f:\n"
                    "    f.write('generated content')\n"
                ),
                "_user_id": "alice",
            },
        )
        await started_service.execute_tool(
            "run_workspace_script",
            {
                "skill_name": "test-skill",
                "path": "write_file.py",
                "_user_id": "alice",
            },
        )
        workspace = started_service._get_workspace("alice", "test-skill")
        assert (workspace / "result.txt").is_file()
        assert (workspace / "result.txt").read_text() == "generated content"

        # And attach_workspace_file can hand it back to the user
        attached = await started_service.execute_tool(
            "attach_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "result.txt",
                "_user_id": "alice",
            },
        )
        assert isinstance(attached, ToolResult)
        assert len(attached.attachments) == 1
        assert attached.attachments[0].workspace_path == "result.txt"

    @pytest.mark.asyncio
    async def test_run_workspace_script_missing_file(
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "run_workspace_script",
            {
                "skill_name": "test-skill",
                "path": "nope.py",
                "_user_id": "alice",
            },
        )
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_run_workspace_script_path_traversal(
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "run_workspace_script",
            {
                "skill_name": "test-skill",
                "path": "../../etc/evil.py",
                "_user_id": "alice",
            },
        )
        assert "traversal" in result.lower()

    @pytest.mark.asyncio
    async def test_ai_invocation_gate_blocks_inactive_skill(
        self,
        started_service: SkillService,
    ) -> None:
        """When invoked by the AI on a conversation where the skill
        isn't active, every skill tool refuses with a friendly error."""
        # Wire a fake AI service with a settable active_skills state.
        active: list[str] = []

        async def get_conversation_state(key: str, conv_id: str) -> Any:
            return active if key == "active_skills" else None

        fake_ai = MagicMock()
        fake_ai.get_conversation_state = get_conversation_state
        started_service._resolver.get_capability = MagicMock(  # type: ignore[union-attr]
            side_effect=lambda cap: fake_ai if cap == "ai_chat" else None
        )

        ai_args = {
            "skill_name": "test-skill",
            "path": "data.csv",
            "_user_id": "alice",
            "_conversation_id": "conv-1",
            "_invocation_source": "ai",
        }
        # Skill not in active list — gate refuses.
        result = await started_service.execute_tool(
            "read_skill_workspace_file", ai_args
        )
        assert "not active for this conversation" in result

        # Activate the skill — now the call passes through (and hits
        # the file-not-found path, since we never wrote data.csv).
        active.append("test-skill")
        result = await started_service.execute_tool(
            "read_skill_workspace_file", ai_args
        )
        assert "not active" not in result
        assert "not found" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_slash_invocation_bypasses_gate(
        self,
        started_service: SkillService,
    ) -> None:
        """Slash commands always pass — they're explicit user input,
        so the activation gate doesn't apply."""
        active: list[str] = []  # nothing active

        async def get_conversation_state(key: str, conv_id: str) -> Any:
            return active if key == "active_skills" else None

        fake_ai = MagicMock()
        fake_ai.get_conversation_state = get_conversation_state
        started_service._resolver.get_capability = MagicMock(  # type: ignore[union-attr]
            side_effect=lambda cap: fake_ai if cap == "ai_chat" else None
        )

        # Pre-create a workspace file so the underlying tool succeeds
        # past the gate.
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "via_slash.txt").write_text("ok")

        slash_args = {
            "skill_name": "test-skill",
            "path": "via_slash.txt",
            "_user_id": "alice",
            "_conversation_id": "conv-1",
            "_invocation_source": "slash",
        }
        result = await started_service.execute_tool(
            "read_skill_workspace_file", slash_args
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_per_conversation_workspaces_are_isolated(
        self,
        started_service: SkillService,
    ) -> None:
        """Two conversations with the same user + skill produce two
        independent workspaces — files written in one don't leak into
        the other."""
        # Wire an active-skills mock so the gate lets us through.
        active = ["test-skill"]

        async def get_conversation_state(key: str, conv_id: str) -> Any:
            return active if key == "active_skills" else None

        fake_ai = MagicMock()
        fake_ai.get_conversation_state = get_conversation_state
        started_service._resolver.get_capability = MagicMock(  # type: ignore[union-attr]
            side_effect=lambda cap: fake_ai if cap == "ai_chat" else None
        )

        # Write to conv-A
        await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "marker.txt",
                "content": "from conv A",
                "_user_id": "alice",
                "_conversation_id": "conv-A",
                "_invocation_source": "ai",
            },
        )
        # Write to conv-B
        await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "marker.txt",
                "content": "from conv B",
                "_user_id": "alice",
                "_conversation_id": "conv-B",
                "_invocation_source": "ai",
            },
        )

        # Read each — each should see ONLY its own write.
        result_a = await started_service.execute_tool(
            "read_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "marker.txt",
                "_user_id": "alice",
                "_conversation_id": "conv-A",
                "_invocation_source": "ai",
            },
        )
        result_b = await started_service.execute_tool(
            "read_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "marker.txt",
                "_user_id": "alice",
                "_conversation_id": "conv-B",
                "_invocation_source": "ai",
            },
        )
        assert result_a == "from conv A"
        assert result_b == "from conv B"

        # On disk, both conv dirs exist independently.
        ws_a = started_service._get_workspace("alice", "test-skill", "conv-A")
        ws_b = started_service._get_workspace("alice", "test-skill", "conv-B")
        assert ws_a != ws_b
        assert (ws_a / "marker.txt").is_file()
        assert (ws_b / "marker.txt").is_file()

    @pytest.mark.asyncio
    async def test_attach_workspace_file_carries_conversation_id(
        self,
        started_service: SkillService,
    ) -> None:
        """The attachment that ``attach_workspace_file`` returns carries
        the conversation id so the download handler can find the file
        under the right per-conv workspace later."""
        from gilbert.interfaces.tools import ToolResult

        active = ["test-skill"]

        async def get_conversation_state(key: str, conv_id: str) -> Any:
            return active if key == "active_skills" else None

        fake_ai = MagicMock()
        fake_ai.get_conversation_state = get_conversation_state
        started_service._resolver.get_capability = MagicMock(  # type: ignore[union-attr]
            side_effect=lambda cap: fake_ai if cap == "ai_chat" else None
        )

        # Write a file to conv-XYZ's workspace
        await started_service.execute_tool(
            "write_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "report.txt",
                "content": "the report",
                "_user_id": "alice",
                "_conversation_id": "conv-XYZ",
                "_invocation_source": "ai",
            },
        )
        # Attach it
        result = await started_service.execute_tool(
            "attach_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "report.txt",
                "_user_id": "alice",
                "_conversation_id": "conv-XYZ",
                "_invocation_source": "ai",
            },
        )
        assert isinstance(result, ToolResult)
        assert len(result.attachments) == 1
        att = result.attachments[0]
        assert att.workspace_conv == "conv-XYZ"
        assert att.workspace_skill == "test-skill"
        assert att.workspace_path == "report.txt"

    @pytest.mark.asyncio
    async def test_legacy_workspace_read_fallback(
        self,
        started_service: SkillService,
    ) -> None:
        """A read against a conversation that has no per-conv file
        falls back to the legacy per-(user, skill) workspace, so old
        chats whose attachments reference legacy paths still resolve."""
        active = ["test-skill"]

        async def get_conversation_state(key: str, conv_id: str) -> Any:
            return active if key == "active_skills" else None

        fake_ai = MagicMock()
        fake_ai.get_conversation_state = get_conversation_state
        started_service._resolver.get_capability = MagicMock(  # type: ignore[union-attr]
            side_effect=lambda cap: fake_ai if cap == "ai_chat" else None
        )

        # Manually create a file under the legacy path (where pre-
        # refactor tool runs would have written).
        legacy = started_service._legacy_workspace_dir("alice", "test-skill")
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "ancient.txt").write_text("from before the refactor")

        # Now read via a fresh conv-id — no per-conv file exists, so
        # the fallback should surface the legacy file.
        result = await started_service.execute_tool(
            "read_skill_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "ancient.txt",
                "_user_id": "alice",
                "_conversation_id": "fresh-conv",
                "_invocation_source": "ai",
            },
        )
        assert result == "from before the refactor"

    @pytest.mark.asyncio
    async def test_conversation_destroyed_cleanup(
        self,
        started_service: SkillService,
    ) -> None:
        """Publishing ``chat.conversation.destroyed`` removes the
        per-conversation workspace tree for the named owner."""
        # Create a conv-scoped workspace with a marker file.
        ws = started_service._get_workspace("alice", "test-skill", "doomed")
        (ws / "marker.txt").write_text("rm me")
        assert ws.is_dir()

        # Fire the cleanup directly (the event-bus subscription is just
        # plumbing; the handler is what we care about).
        from gilbert.core.services.event_bus import Event

        await started_service._on_conversation_destroyed(
            Event(
                event_type="chat.conversation.destroyed",
                data={"conversation_id": "doomed", "owner_id": "alice"},
                source="ai",
            )
        )

        # The ``conversations/doomed`` subtree is gone.
        conv_root = started_service._conversation_workspace_root(
            "alice", "doomed"
        )
        assert not conv_root.exists()
        # But other conversations' workspaces are untouched.
        ws_other = started_service._get_workspace(
            "alice", "test-skill", "survivor"
        )
        assert ws_other.is_dir()

    @pytest.mark.asyncio
    async def test_manage_skills_list_filters_inactive_for_ai(
        self,
        started_service: SkillService,
    ) -> None:
        """``manage_skills(action=list)`` only shows active skills when
        invoked by the AI; a slash invocation sees the full catalog."""
        active: list[str] = []

        async def get_conversation_state(key: str, conv_id: str) -> Any:
            return active if key == "active_skills" else None

        fake_ai = MagicMock()
        fake_ai.get_conversation_state = get_conversation_state
        started_service._resolver.get_capability = MagicMock(  # type: ignore[union-attr]
            side_effect=lambda cap: fake_ai if cap == "ai_chat" else None
        )

        ai_args = {
            "action": "list",
            "_user_id": "alice",
            "_conversation_id": "conv-1",
            "_invocation_source": "ai",
        }
        result = await started_service.execute_tool("manage_skills", ai_args)
        data = json.loads(result)
        assert data["count"] == 0  # no skills active → empty list to AI

        # Activate the skill — it shows up.
        active.append("test-skill")
        result = await started_service.execute_tool("manage_skills", ai_args)
        data = json.loads(result)
        assert data["count"] == 1
        assert data["skills"][0]["name"] == "test-skill"

        # A slash invocation always sees the full catalog regardless.
        active.clear()
        slash_args = {**ai_args, "_invocation_source": "slash"}
        result = await started_service.execute_tool("manage_skills", slash_args)
        data = json.loads(result)
        assert data["count"] >= 1

    @pytest.mark.asyncio
    async def test_attach_workspace_file_image_kind(
        self,
        started_service: SkillService,
    ) -> None:
        """Image mime types bucket into kind='image' for inline previews."""
        from gilbert.interfaces.tools import ToolResult

        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "chart.png").write_bytes(b"\x89PNG\r\n")

        result = await started_service.execute_tool(
            "attach_workspace_file",
            {
                "skill_name": "test-skill",
                "path": "chart.png",
                "display_name": "Sales Chart.png",
                "_user_id": "alice",
            },
        )
        assert isinstance(result, ToolResult)
        assert len(result.attachments) == 1
        assert result.attachments[0].kind == "image"
        assert result.attachments[0].media_type == "image/png"
        assert result.attachments[0].name == "Sales Chart.png"


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
            "manage_skills",
            {"action": "list", "_user_id": "alice"},
        )
        assert "test-skill" in result

    @pytest.mark.asyncio
    async def test_install_no_url(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "manage_skills",
            {"action": "install", "_user_id": "alice"},
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
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "manage_skills",
            {"action": "uninstall", "skill_name": "test-skill", "_user_id": "alice"},
        )
        assert "error" in result.lower() or "Cannot" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self, started_service: SkillService) -> None:
        result = await started_service.execute_tool(
            "manage_skills",
            {"action": "bogus", "_user_id": "alice"},
        )
        assert "error" in result


# --- WS Handlers ---


class TestWsHandlers:
    @pytest.mark.asyncio
    async def test_ws_skills_list(self, started_service: SkillService) -> None:
        conn = MagicMock()
        conn.user_ctx = None
        result = await started_service._ws_skills_list(
            conn,
            {"id": "f1"},
        )
        assert result["type"] == "skills.list.result"
        assert len(result["skills"]) == 1
        assert result["skills"][0]["name"] == "test-skill"
        assert result["skills"][0]["scope"] == "global"

    @pytest.mark.asyncio
    async def test_ws_skills_toggle(
        self,
        service: SkillService,
        mock_resolver: MagicMock,
    ) -> None:
        ai_svc = AsyncMock()
        ai_svc.get_conversation_state = AsyncMock(return_value=[])
        ai_svc.set_conversation_state = AsyncMock()
        await service.start(mock_resolver)
        mock_resolver.get_capability.side_effect = lambda cap: ai_svc if cap == "ai_chat" else None

        conn = MagicMock()
        result = await service._ws_skills_toggle(
            conn,
            {
                "id": "f2",
                "conversation_id": "conv-1",
                "skill": "test-skill",
                "enabled": True,
            },
        )
        assert result["type"] == "skills.conversation.toggle.result"
        assert result["skill"] == "test-skill"
        assert result["enabled"] is True

    @pytest.mark.asyncio
    async def test_ws_toggle_unknown_skill(
        self,
        started_service: SkillService,
    ) -> None:
        conn = MagicMock()
        result = await started_service._ws_skills_toggle(
            conn,
            {
                "id": "f3",
                "conversation_id": "conv-1",
                "skill": "nonexistent",
                "enabled": True,
            },
        )
        assert result["type"] == "gilbert.error"
        assert result["code"] == 404

    @pytest.mark.asyncio
    async def test_ws_toggle_missing_fields(
        self,
        started_service: SkillService,
    ) -> None:
        conn = MagicMock()
        result = await started_service._ws_skills_toggle(conn, {"id": "f4"})
        assert result["type"] == "gilbert.error"
        assert result["code"] == 400

    @pytest.mark.asyncio
    async def test_ws_active_no_conversation(
        self,
        started_service: SkillService,
    ) -> None:
        conn = MagicMock()
        result = await started_service._ws_skills_active(conn, {"id": "f5"})
        assert result["active_skills"] == []

    @pytest.mark.asyncio
    async def test_ws_workspace_browse(
        self,
        started_service: SkillService,
    ) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "file.txt").write_text("content")
        conn = MagicMock()
        conn.user_id = "alice"
        result = await started_service._ws_workspace_browse(
            conn,
            {
                "id": "f6",
                "skill_name": "test-skill",
            },
        )
        assert result["type"] == "skills.workspace.browse.result"
        assert len(result["files"]) == 1
        assert result["files"][0]["path"] == "file.txt"

    @pytest.mark.asyncio
    async def test_ws_workspace_download(
        self,
        started_service: SkillService,
    ) -> None:
        workspace = started_service._get_workspace("alice", "test-skill")
        (workspace / "data.csv").write_text("a,b\n1,2")
        conn = MagicMock()
        conn.user_id = "alice"
        result = await started_service._ws_workspace_download(
            conn,
            {
                "id": "f7",
                "skill_name": "test-skill",
                "path": "data.csv",
            },
        )
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
        self,
        install_service: SkillService,
        mock_resolver: MagicMock,
        tmp_path: Path,
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
        self,
        install_service: SkillService,
        mock_resolver: MagicMock,
        tmp_path: Path,
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
        self,
        install_service: SkillService,
        mock_resolver: MagicMock,
        tmp_path: Path,
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
        self,
        install_service: SkillService,
        mock_resolver: MagicMock,
        tmp_path: Path,
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
        self,
        install_service: SkillService,
        mock_resolver: MagicMock,
        tmp_path: Path,
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
        self,
        install_service: SkillService,
        mock_resolver: MagicMock,
        tmp_path: Path,
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
            "manage_skills",
            "create_skill",
            "read_skill_file",
            "run_skill_script",
            "browse_skill_workspace",
            "read_skill_workspace_file",
            "write_skill_workspace_file",
            "run_workspace_script",
            "attach_workspace_file",
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
        resolver.get_capability.side_effect = lambda cap: (
            storage if cap == "entity_storage" else None
        )
        # Simulate StorageProvider protocol for isinstance check
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        result = await svc.execute_tool(
            "create_skill",
            {
                "skill_md": _VALID_SKILL_MD,
                "scope": "user",
                "_user_id": "alice",
            },
        )
        assert "created" in result
        assert "test-entity-skill" in result
        assert "alice:test-entity-skill" in svc._catalog
        assert svc._catalog["alice:test-entity-skill"].location is None

    @pytest.mark.asyncio
    async def test_create_skill_content_cached(
        self,
        storage_service: tuple[SkillService, AsyncMock],
    ) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = lambda cap: (
            storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        await svc.execute_tool(
            "create_skill",
            {
                "skill_md": _VALID_SKILL_MD,
                "scope": "user",
                "_user_id": "alice",
            },
        )
        content = svc.get_skill_content("alice:test-entity-skill")
        assert content is not None
        assert "# Test Entity Skill" in content.instructions
        assert content.resources == []

    @pytest.mark.asyncio
    async def test_create_skill_missing_frontmatter(
        self,
        storage_service: tuple[SkillService, AsyncMock],
    ) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = lambda cap: (
            storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        result = await svc.execute_tool(
            "create_skill",
            {
                "skill_md": "No frontmatter here",
                "_user_id": "alice",
            },
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_create_skill_missing_name(
        self,
        storage_service: tuple[SkillService, AsyncMock],
    ) -> None:
        svc, storage = storage_service
        resolver = MagicMock()
        resolver.get_capability.side_effect = lambda cap: (
            storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        result = await svc.execute_tool(
            "create_skill",
            {
                "skill_md": "---\ndescription: No name.\n---\nBody.",
                "_user_id": "alice",
            },
        )
        assert "error" in result


class TestDeleteEntitySkill:
    @pytest.mark.asyncio
    async def test_delete_entity_skill(self, tmp_path: Path) -> None:
        storage = AsyncMock()
        storage.ensure_index = AsyncMock()
        storage.put = AsyncMock()
        storage.delete = AsyncMock()
        # Start with the skill in storage
        storage.query = AsyncMock(
            return_value=[
                {
                    "_id": "skill_my-skill",
                    "skill_md": _VALID_SKILL_MD,
                    "scope": "user",
                    "owner_id": "alice",
                },
            ]
        )

        svc = _make_skill_service(
            directories=[],
            user_dir=str(tmp_path / "user-skills"),
        )
        resolver = MagicMock()
        resolver.get_capability.side_effect = lambda cap: (
            storage if cap == "entity_storage" else None
        )
        storage.backend = storage
        storage.raw_backend = storage
        storage.create_namespaced = lambda ns: storage
        await svc.start(resolver)

        # Skill should be in catalog
        assert "alice:test-entity-skill" in svc._catalog

        # Delete it
        result = await svc.execute_tool(
            "manage_skills",
            {
                "action": "delete",
                "skill_name": "test-entity-skill",
                "_user_id": "alice",
            },
        )
        assert "deleted" in result
        assert "alice:test-entity-skill" not in svc._catalog

    @pytest.mark.asyncio
    async def test_cannot_delete_file_based_skill(
        self,
        started_service: SkillService,
    ) -> None:
        result = await started_service.execute_tool(
            "manage_skills",
            {
                "action": "delete",
                "skill_name": "test-skill",
                "_user_id": "alice",
            },
        )
        assert "file-based" in result.lower() or "uninstall" in result.lower()
