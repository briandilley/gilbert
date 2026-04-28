"""Tests for SourceInspectorService — read-only AI source inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gilbert.core.services.source_inspector import SourceInspectorService

# ── Helpers ──────────────────────────────────────────────────────────


class _FakeResolver:
    def get_capability(self, cap: str) -> Any:
        return None

    def require_capability(self, cap: str) -> Any:
        raise LookupError(cap)

    def get_all(self, cap: str) -> list[Any]:
        return []


def _build_repo(root: Path) -> None:
    """Build a fake repo tree the inspector is allowed to read."""
    (root / "src" / "gilbert" / "core" / "services").mkdir(parents=True)
    (root / "src" / "gilbert" / "core" / "services" / "alpha.py").write_text(
        "def hello():\n    return 'world'\n",
    )
    (root / "src" / "gilbert" / "core" / "services" / "beta.py").write_text(
        "import json\n\n\nclass Beta:\n    pass\n",
    )
    (root / "std-plugins").mkdir()
    (root / "std-plugins" / "demo.py").write_text("DEMO = 1\n")
    # Cache dir that should be skipped.
    (root / "src" / "gilbert" / "core" / "__pycache__").mkdir()
    (root / "src" / "gilbert" / "core" / "__pycache__" / "junk.pyc").write_bytes(
        b"\x00\x01\x02",
    )
    # README at the root — single-file allowlist entry.
    (root / "README.md").write_text("# fake repo\n")
    # Off-limits dir — must NOT be reachable.
    (root / "secrets").mkdir()
    (root / "secrets" / "api_key").write_text("sk-leak-me\n")


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _build_repo(tmp_path)
    return tmp_path


@pytest.fixture
async def started_service(repo: Path) -> SourceInspectorService:
    svc = SourceInspectorService(repo_root=repo)
    await svc.start(_FakeResolver())
    return svc


# ── Tests ────────────────────────────────────────────────────────────


class TestServiceInfo:
    def test_capabilities(self) -> None:
        info = SourceInspectorService().service_info()
        assert info.name == "source_inspector"
        assert "source_inspector" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert info.toggleable is True


class TestPathAllowlist:
    @pytest.mark.asyncio
    async def test_blocks_traversal(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "../etc/passwd"},
            ),
        )
        assert "error" in result
        assert "escapes" in result["error"] or "allowlist" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_off_allowlist_dir(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "secrets/api_key"},
            ),
        )
        assert "error" in result
        assert "allowlist" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_symlink_escape(
        self, started_service: SourceInspectorService, repo: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path.parent / "outside_target.txt"
        outside.write_text("private\n")
        link = repo / "src" / "gilbert" / "evil_link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unsupported in test environment")
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "src/gilbert/evil_link.txt"},
            ),
        )
        # Resolved target is outside the repo root → either escapes-root
        # or off-allowlist depending on resolution order. Both block.
        assert "error" in result


class TestListFiles:
    @pytest.mark.asyncio
    async def test_lists_directory(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_list_files",
                {"path": "src/gilbert/core/services"},
            ),
        )
        names = {Path(e["path"]).name for e in result["entries"]}
        assert "alpha.py" in names
        assert "beta.py" in names

    @pytest.mark.asyncio
    async def test_skips_pycache(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_list_files",
                {"path": "src/gilbert/core"},
            ),
        )
        for entry in result["entries"]:
            assert "__pycache__" not in entry["path"]


class TestReadFile:
    @pytest.mark.asyncio
    async def test_reads_text(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "src/gilbert/core/services/alpha.py"},
            ),
        )
        assert "world" in result["content"]
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncates_large_file(
        self, started_service: SourceInspectorService, repo: Path
    ) -> None:
        big = repo / "src" / "gilbert" / "huge.py"
        big.write_text("x = '" + ("a" * 1_000) + "'\n")
        started_service._max_file_bytes = 64
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "src/gilbert/huge.py"},
            ),
        )
        assert result["truncated"] is True
        assert len(result["content"]) <= 64

    @pytest.mark.asyncio
    async def test_refuses_binary(
        self, started_service: SourceInspectorService, repo: Path
    ) -> None:
        png = repo / "src" / "gilbert" / "logo.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "src/gilbert/logo.png"},
            ),
        )
        assert "error" in result
        assert "binary" in result["error"]

    @pytest.mark.asyncio
    async def test_single_file_allowlist_entry(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_read_file",
                {"path": "README.md"},
            ),
        )
        assert "fake repo" in result["content"]


class TestGrep:
    @pytest.mark.asyncio
    async def test_finds_match(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_grep",
                {"pattern": "class Beta", "path": "src/gilbert"},
            ),
        )
        assert any(m["path"].endswith("beta.py") for m in result["matches"])

    @pytest.mark.asyncio
    async def test_default_searches_all_allowed_roots(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_grep",
                {"pattern": "DEMO"},
            ),
        )
        assert any(m["path"].endswith("demo.py") for m in result["matches"])

    @pytest.mark.asyncio
    async def test_invalid_regex_returns_error(
        self, started_service: SourceInspectorService
    ) -> None:
        result = json.loads(
            await started_service.execute_tool(
                "gilbert_grep",
                {"pattern": "(unclosed"},
            ),
        )
        assert "error" in result
        assert "regex" in result["error"].lower()


class TestToolDiscoveryPaths:
    def test_get_tools_respects_enabled(self) -> None:
        svc = SourceInspectorService()
        svc._enabled = False
        assert svc.get_tools() == []

    def test_get_tool_definitions_ignores_enabled(self) -> None:
        # Always-on path used by ProposalsService — disabled flag must
        # NOT hide the tools from the reflection AI.
        svc = SourceInspectorService()
        svc._enabled = False
        defs = svc.get_tool_definitions()
        names = {td.name for td in defs}
        assert names == {
            "gilbert_list_files",
            "gilbert_read_file",
            "gilbert_grep",
        }


class TestExecuteUnknown:
    @pytest.mark.asyncio
    async def test_unknown_tool_raises(
        self, started_service: SourceInspectorService
    ) -> None:
        with pytest.raises(KeyError):
            await started_service.execute_tool("nope", {})
