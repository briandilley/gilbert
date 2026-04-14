"""Tests for AI Context Profiles — tool filtering, RBAC, and profile management."""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import AIContextProfile, AIService
from gilbert.core.services.access_control import AccessControlService
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
)


# --- Stubs ---


class StubAIBackend(AIBackend):
    def __init__(self) -> None:
        self._responses: list[AIResponse] = []
        self._call_idx = 0
        self.requests: list[AIRequest] = []

    def queue_response(self, response: AIResponse) -> None:
        self._responses.append(response)

    async def initialize(self, config: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass

    async def generate(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        if self._call_idx < len(self._responses):
            resp = self._responses[self._call_idx]
            self._call_idx += 1
            return resp
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="default"),
            model="stub",
        )


class StubStorage(StorageBackend):
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[entity_id] = {"_id": entity_id, **data}

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        self._data.get(collection, {}).pop(entity_id, None)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return entity_id in self._data.get(collection, {})

    async def query(self, query: Any) -> list[dict[str, Any]]:
        col = self._data.get(query.collection, {})
        entities = list(col.values())
        if query.sort:
            for s in reversed(query.sort):
                entities.sort(key=lambda e: e.get(s.field, ""), reverse=s.descending)
        return entities

    async def count(self, query: Any) -> int:
        return len(self._data.get(query.collection, {}))

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: Any) -> None:
        pass

    async def list_indexes(self, collection: str) -> list[Any]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


class StubToolProvider(Service):
    """Tool provider with configurable tools."""

    def __init__(
        self, name: str, tools: list[ToolDefinition], results: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._tools = tools
        self._results = results or {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name=self._name, capabilities=frozenset({"ai_tools"}))

    @property
    def tool_provider_name(self) -> str:
        return self._name

    def get_tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name in self._results:
            return self._results[name]
        return json.dumps({"tool": name, "args": arguments})


# --- Test tools ---

TOOL_PUBLIC = ToolDefinition(name="public_tool", description="Everyone can use", required_role="everyone")
TOOL_USER = ToolDefinition(name="user_tool", description="Users only", required_role="user")
TOOL_ADMIN = ToolDefinition(name="admin_tool", description="Admins only", required_role="admin")
TOOL_SALES = ToolDefinition(name="sales_lead", description="Sales lead tool", required_role="user")
TOOL_SEARCH = ToolDefinition(name="search_music", description="Search music", required_role="everyone")

ALL_TOOLS = [TOOL_PUBLIC, TOOL_USER, TOOL_ADMIN, TOOL_SALES, TOOL_SEARCH]


# --- Fixtures ---


@pytest.fixture
def stub_storage() -> StubStorage:
    return StubStorage()


@pytest.fixture
def stub_backend() -> StubAIBackend:
    return StubAIBackend()


@pytest.fixture
def tool_provider() -> StubToolProvider:
    return StubToolProvider(
        "test_tools", ALL_TOOLS,
        results={t.name: json.dumps({"ok": True}) for t in ALL_TOOLS},
    )


@pytest.fixture
async def resolver(
    stub_storage: StubStorage, stub_backend: StubAIBackend, tool_provider: StubToolProvider,
) -> ServiceResolver:
    storage_svc = StorageService(stub_storage)
    acl_svc = AccessControlService()

    # Persona stub
    from unittest.mock import MagicMock
    persona_svc = MagicMock()
    persona_svc.persona = "Test persona"
    persona_svc.is_customized = True

    mock = AsyncMock(spec=ServiceResolver)

    caps: dict[str, Any] = {
        "entity_storage": storage_svc,
        "persona": persona_svc,
        "access_control": acl_svc,
    }

    def require_cap(cap: str) -> Any:
        if cap in caps:
            return caps[cap]
        raise LookupError(cap)

    def get_cap(cap: str) -> Any:
        return caps.get(cap)

    def get_all(cap: str) -> list[Any]:
        if cap == "ai_tools":
            return [tool_provider]
        svc = caps.get(cap)
        return [svc] if svc else []

    mock.require_capability = require_cap
    mock.get_capability = get_cap
    mock.get_all = get_all

    # Start the ACL service so its role cache is populated
    await acl_svc.start(mock)

    return mock


@pytest.fixture
async def acl_svc(resolver: ServiceResolver) -> AccessControlService:
    """The started ACL service from the resolver."""
    return resolver.get_capability("access_control")


@pytest.fixture
async def ai_svc(
    stub_backend: StubAIBackend, resolver: ServiceResolver, acl_svc: AccessControlService,
) -> AIService:
    """Started AI service with profiles loaded."""
    svc = AIService()
    svc._backend = stub_backend
    svc._enabled = True
    svc._config = {"max_tokens": 1024, "temperature": 0.5}
    svc._system_prompt = "Test assistant"
    svc._max_tool_rounds = 3
    await svc.start(resolver)
    return svc


# --- User contexts ---

def _admin() -> UserContext:
    return UserContext(user_id="admin1", email="a@test.com", display_name="Admin", roles=frozenset({"admin"}))

def _user() -> UserContext:
    return UserContext(user_id="user1", email="u@test.com", display_name="User", roles=frozenset({"user"}))

def _everyone() -> UserContext:
    return UserContext(user_id="guest1", email="g@test.com", display_name="Guest", roles=frozenset({"everyone"}))


# =============================================================================
# Profile Resolution
# =============================================================================


class TestProfileResolution:
    async def test_builtin_profiles_seeded(self, ai_svc: AIService) -> None:
        profiles = ai_svc.list_profiles()
        names = {p.name for p in profiles}
        assert "default" in names
        assert "human_chat" in names
        assert "text_only" in names

    async def test_builtin_assignments_seeded(self, ai_svc: AIService) -> None:
        assignments = ai_svc.list_assignments()
        assert assignments["human_chat"] == "human_chat"
        assert assignments["greeting"] == "text_only"

    async def test_get_profile_returns_none_for_no_call(self, ai_svc: AIService) -> None:
        assert ai_svc.get_profile(None) is None

    async def test_get_profile_returns_default_for_unknown_call(self, ai_svc: AIService) -> None:
        profile = ai_svc.get_profile("unknown_call")
        assert profile is not None
        assert profile.name == "default"

    async def test_get_profile_resolves_via_assignment(self, ai_svc: AIService) -> None:
        profile = ai_svc.get_profile("human_chat")
        assert profile is not None
        assert profile.name == "human_chat"

    async def test_get_profile_resolves_custom_assignment(self, ai_svc: AIService) -> None:
        await ai_svc.set_assignment("custom_call", "text_only")
        profile = ai_svc.get_profile("custom_call")
        assert profile is not None
        assert profile.name == "text_only"


# =============================================================================
# Profile CRUD
# =============================================================================


class TestProfileCRUD:
    async def test_set_profile_creates(self, ai_svc: AIService) -> None:
        p = AIContextProfile(name="custom", description="Custom", tool_mode="include", tools=["user_tool"])
        await ai_svc.set_profile(p)
        assert "custom" in {pr.name for pr in ai_svc.list_profiles()}

    async def test_set_profile_updates(self, ai_svc: AIService) -> None:
        p = AIContextProfile(name="custom", description="v1", tool_mode="all")
        await ai_svc.set_profile(p)
        p2 = AIContextProfile(name="custom", description="v2", tool_mode="exclude", tools=["admin_tool"])
        await ai_svc.set_profile(p2)
        found = [pr for pr in ai_svc.list_profiles() if pr.name == "custom"]
        assert len(found) == 1
        assert found[0].description == "v2"
        assert found[0].tool_mode == "exclude"

    async def test_delete_profile(self, ai_svc: AIService) -> None:
        p = AIContextProfile(name="temp", description="Temp")
        await ai_svc.set_profile(p)
        await ai_svc.delete_profile("temp")
        assert "temp" not in {pr.name for pr in ai_svc.list_profiles()}

    async def test_cannot_delete_default(self, ai_svc: AIService) -> None:
        with pytest.raises(ValueError, match="Cannot delete"):
            await ai_svc.delete_profile("default")


# =============================================================================
# Assignment CRUD
# =============================================================================


class TestAssignmentCRUD:
    async def test_set_assignment(self, ai_svc: AIService) -> None:
        await ai_svc.set_assignment("my_call", "text_only")
        assert ai_svc.list_assignments()["my_call"] == "text_only"

    async def test_set_assignment_unknown_profile_raises(self, ai_svc: AIService) -> None:
        with pytest.raises(ValueError, match="Unknown profile"):
            await ai_svc.set_assignment("my_call", "nonexistent")

    async def test_clear_assignment(self, ai_svc: AIService) -> None:
        await ai_svc.set_assignment("my_call", "text_only")
        await ai_svc.clear_assignment("my_call")
        assert "my_call" not in ai_svc.list_assignments()

    async def test_cleared_call_falls_back_to_default(self, ai_svc: AIService) -> None:
        await ai_svc.set_assignment("my_call", "text_only")
        await ai_svc.clear_assignment("my_call")
        profile = ai_svc.get_profile("my_call")
        assert profile is not None
        assert profile.name == "default"


# =============================================================================
# Tool Mode Filtering
# =============================================================================


class TestToolModeFiltering:
    async def test_all_mode_returns_all_tools(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="all")
        tools = ai_svc._discover_tools(user_ctx=_admin(), profile=profile)
        assert len(tools) == len(ALL_TOOLS)

    async def test_include_mode_only_listed(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="include", tools=["user_tool", "public_tool"])
        tools = ai_svc._discover_tools(user_ctx=_admin(), profile=profile)
        assert set(tools.keys()) == {"user_tool", "public_tool"}

    async def test_include_empty_returns_no_tools(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="include", tools=[])
        tools = ai_svc._discover_tools(user_ctx=_admin(), profile=profile)
        assert len(tools) == 0

    async def test_exclude_mode_removes_listed(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="exclude", tools=["admin_tool", "sales_lead"])
        tools = ai_svc._discover_tools(user_ctx=_admin(), profile=profile)
        assert "admin_tool" not in tools
        assert "sales_lead" not in tools
        assert "user_tool" in tools
        assert "public_tool" in tools

    async def test_no_profile_returns_all_tools(self, ai_svc: AIService) -> None:
        tools = ai_svc._discover_tools(user_ctx=_admin(), profile=None)
        assert len(tools) == len(ALL_TOOLS)


# =============================================================================
# RBAC Filtering
# =============================================================================


class TestRBACFiltering:
    async def test_admin_sees_all_tools(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="all")
        tools = ai_svc._discover_tools(user_ctx=_admin(), profile=profile)
        assert len(tools) == len(ALL_TOOLS)

    async def test_user_cannot_see_admin_tools(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="all")
        tools = ai_svc._discover_tools(user_ctx=_user(), profile=profile)
        assert "admin_tool" not in tools
        assert "user_tool" in tools
        assert "public_tool" in tools

    async def test_everyone_only_sees_everyone_tools(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="all")
        tools = ai_svc._discover_tools(user_ctx=_everyone(), profile=profile)
        assert "admin_tool" not in tools
        assert "user_tool" not in tools
        assert "public_tool" in tools
        assert "search_music" in tools

    async def test_system_user_bypasses_rbac(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="all")
        tools = ai_svc._discover_tools(user_ctx=UserContext.SYSTEM, profile=profile)
        # SYSTEM user_id="system" — RBAC block only runs when user_ctx is not None
        # and user is not system. With SYSTEM, all tools should pass.
        assert len(tools) == len(ALL_TOOLS)

    async def test_no_user_context_skips_rbac(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(name="test", tool_mode="all")
        tools = ai_svc._discover_tools(user_ctx=None, profile=profile)
        assert len(tools) == len(ALL_TOOLS)


# =============================================================================
# Profile tool_roles Overrides
# =============================================================================


class TestToolRolesOverrides:
    async def test_tool_roles_lowers_requirement(self, ai_svc: AIService) -> None:
        """Profile can lower a tool's role requirement for its context."""
        profile = AIContextProfile(
            name="test", tool_mode="all",
            tool_roles={"admin_tool": "everyone"},
        )
        tools = ai_svc._discover_tools(user_ctx=_everyone(), profile=profile)
        assert "admin_tool" in tools

    async def test_tool_roles_raises_requirement(self, ai_svc: AIService) -> None:
        """Profile can raise a tool's role requirement for its context."""
        profile = AIContextProfile(
            name="test", tool_mode="all",
            tool_roles={"public_tool": "admin"},
        )
        tools = ai_svc._discover_tools(user_ctx=_user(), profile=profile)
        assert "public_tool" not in tools

    async def test_tool_roles_only_affects_specified_tools(self, ai_svc: AIService) -> None:
        profile = AIContextProfile(
            name="test", tool_mode="all",
            tool_roles={"admin_tool": "everyone"},
        )
        tools = ai_svc._discover_tools(user_ctx=_everyone(), profile=profile)
        # admin_tool is now accessible
        assert "admin_tool" in tools
        # user_tool still requires "user" — everyone can't access it
        assert "user_tool" not in tools


# =============================================================================
# Defense-in-Depth (Tool Execution Re-check)
# =============================================================================


class TestDefenseInDepth:
    async def test_execution_recheck_blocks_unauthorized(
        self, ai_svc: AIService,
    ) -> None:
        """If RBAC would block the tool, execution returns permission denied."""
        # Build tools dict with an admin tool
        tools_by_name: dict[str, tuple[Any, ToolDefinition]] = {
            "admin_tool": (None, TOOL_ADMIN),
        }
        tool_calls = [ToolCall(tool_call_id="tc1", tool_name="admin_tool", arguments={})]

        results, _ui = await ai_svc._execute_tool_calls(
            tool_calls, tools_by_name, user_ctx=_user(), profile=None,
        )
        assert len(results) == 1
        assert results[0].is_error
        assert "Permission denied" in results[0].content

    async def test_execution_recheck_allows_with_profile_role_override(
        self, ai_svc: AIService, tool_provider: StubToolProvider,
    ) -> None:
        """Profile tool_roles override should apply in defense-in-depth too."""
        tools_by_name: dict[str, tuple[Any, ToolDefinition]] = {
            "admin_tool": (tool_provider, TOOL_ADMIN),
        }
        profile = AIContextProfile(
            name="test", tool_mode="all",
            tool_roles={"admin_tool": "everyone"},
        )
        tool_calls = [ToolCall(tool_call_id="tc1", tool_name="admin_tool", arguments={})]

        results, _ui = await ai_svc._execute_tool_calls(
            tool_calls, tools_by_name, user_ctx=_everyone(), profile=profile,
        )
        assert len(results) == 1
        assert not results[0].is_error

    async def test_system_user_bypasses_execution_recheck(
        self, ai_svc: AIService, tool_provider: StubToolProvider,
    ) -> None:
        tools_by_name: dict[str, tuple[Any, ToolDefinition]] = {
            "admin_tool": (tool_provider, TOOL_ADMIN),
        }
        tool_calls = [ToolCall(tool_call_id="tc1", tool_name="admin_tool", arguments={})]

        results, _ui = await ai_svc._execute_tool_calls(
            tool_calls, tools_by_name, user_ctx=UserContext.SYSTEM, profile=None,
        )
        assert len(results) == 1
        assert not results[0].is_error

    async def test_unknown_tool_returns_error(self, ai_svc: AIService) -> None:
        tool_calls = [ToolCall(tool_call_id="tc1", tool_name="nonexistent", arguments={})]
        results, _ui = await ai_svc._execute_tool_calls(tool_calls, {}, user_ctx=_admin())
        assert len(results) == 1
        assert results[0].is_error
        assert "unknown tool" in results[0].content


# =============================================================================
# End-to-End: chat() with ai_call
# =============================================================================


class TestChatWithProfile:
    async def test_chat_with_include_profile(
        self, ai_svc: AIService, stub_backend: StubAIBackend,
    ) -> None:
        """Tool calls should only see tools in the profile."""
        # Set up a profile that only includes sales_lead
        await ai_svc.set_profile(AIContextProfile(
            name="sales_only", tool_mode="include", tools=["sales_lead"],
        ))
        await ai_svc.set_assignment("test_call", "sales_only")

        stub_backend.queue_response(AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="done"),
            model="stub",
        ))

        await ai_svc.chat("test", ai_call="test_call", user_ctx=UserContext.SYSTEM)

        # The request should only have the sales_lead tool
        assert len(stub_backend.requests) == 1
        tool_names = {t.name for t in stub_backend.requests[0].tools}
        assert tool_names == {"sales_lead"}

    async def test_chat_with_text_only_profile(
        self, ai_svc: AIService, stub_backend: StubAIBackend,
    ) -> None:
        """text_only profile should pass no tools to the AI."""
        stub_backend.queue_response(AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="hello"),
            model="stub",
        ))

        await ai_svc.chat("test", ai_call="greeting", user_ctx=UserContext.SYSTEM)

        assert len(stub_backend.requests) == 1
        assert stub_backend.requests[0].tools == []

    async def test_chat_with_no_ai_call_gets_all_tools(
        self, ai_svc: AIService, stub_backend: StubAIBackend,
    ) -> None:
        """No ai_call means no profile filtering — all tools available."""
        stub_backend.queue_response(AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        ))

        await ai_svc.chat("test", ai_call=None, user_ctx=UserContext.SYSTEM)

        assert len(stub_backend.requests) == 1
        tool_names = {t.name for t in stub_backend.requests[0].tools}
        assert tool_names == {t.name for t in ALL_TOOLS}

    async def test_chat_exclude_profile_hides_tools(
        self, ai_svc: AIService, stub_backend: StubAIBackend,
    ) -> None:
        """Exclude profile hides listed tools."""
        # Create a custom exclude profile and assign it
        await ai_svc.set_profile(AIContextProfile(
            name="exclude_test", description="Excludes user_tool",
            tool_mode="exclude", tools=["user_tool"],
        ))
        await ai_svc.set_assignment("exclude_call", "exclude_test")

        stub_backend.queue_response(AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        ))

        await ai_svc.chat("test", ai_call="exclude_call", user_ctx=_admin())

        tool_names = {t.name for t in stub_backend.requests[0].tools}
        assert "user_tool" not in tool_names
        assert "admin_tool" in tool_names


# =============================================================================
# ServiceInfo.ai_calls
# =============================================================================


# =============================================================================
# Date/Time Context Injection
# =============================================================================


class TestDateTimeContext:
    def test_current_datetime_context_format(self) -> None:
        """Should produce a string like 'Current date and time: Monday, April 07, 2026 at ...'"""
        result = AIService._current_datetime_context()
        assert result.startswith("Current date and time:")
        # Should contain a day of week
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        assert any(d in result for d in days)

    async def test_default_prompt_includes_date(
        self, ai_svc: AIService,
    ) -> None:
        """_build_system_prompt should start with the date context."""
        prompt = await ai_svc._build_system_prompt()
        assert prompt.startswith("Current date and time:")

    async def test_custom_prompt_includes_date(
        self, ai_svc: AIService, stub_backend: StubAIBackend,
    ) -> None:
        """When a custom system_prompt is provided, date context is prepended."""
        stub_backend.queue_response(AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            model="stub",
        ))

        await ai_svc.chat(
            "test",
            system_prompt="You are a sales agent.",
            user_ctx=UserContext.SYSTEM,
        )

        assert len(stub_backend.requests) == 1
        system_prompt = stub_backend.requests[0].system_prompt
        assert system_prompt.startswith("Current date and time:")
        assert "You are a sales agent." in system_prompt

    async def test_date_context_contains_timezone(self) -> None:
        """Should contain a timezone abbreviation (PDT, PST, or UTC)."""
        result = AIService._current_datetime_context()
        assert any(tz in result for tz in ["PDT", "PST", "UTC"])

    def test_date_context_includes_yesterday(self) -> None:
        """Should explicitly state what yesterday was to avoid AI day-of-week errors."""
        result = AIService._current_datetime_context()
        assert "Yesterday was" in result


class TestServiceInfoAiCalls:
    def test_default_ai_calls_is_empty(self) -> None:
        info = ServiceInfo(name="test")
        assert info.ai_calls == frozenset()

    def test_ai_calls_declared(self) -> None:
        info = ServiceInfo(
            name="test",
            ai_calls=frozenset({"call_a", "call_b"}),
        )
        assert info.ai_calls == frozenset({"call_a", "call_b"})

    def test_greeting_service_declares_ai_calls(self) -> None:
        from gilbert.core.services.greeting import GreetingService
        svc = GreetingService()
        assert "greeting" in svc.service_info().ai_calls

    def test_roast_service_declares_ai_calls(self) -> None:
        from gilbert.core.services.roast import RoastService
        svc = RoastService()
        assert "roast" in svc.service_info().ai_calls

    def test_inbox_ai_chat_declares_ai_calls(self) -> None:
        from gilbert.core.services.inbox_ai_chat import InboxAIChatService
        svc = InboxAIChatService()
        assert "inbox_ai_chat" in svc.service_info().ai_calls


# =============================================================================
# Profile AI Tools (management via execute_tool)
# =============================================================================


class TestProfileTools:
    async def test_list_profiles_tool(self, ai_svc: AIService) -> None:
        result = await ai_svc.execute_tool("list_ai_profiles", {})
        data = json.loads(result)
        assert "profiles" in data
        assert "assignments" in data
        names = {p["name"] for p in data["profiles"]}
        assert "default" in names

    async def test_set_profile_tool(self, ai_svc: AIService) -> None:
        result = await ai_svc.execute_tool("set_ai_profile", {
            "name": "new_profile",
            "description": "Test",
            "tool_mode": "include",
            "tools": ["user_tool"],
        })
        data = json.loads(result)
        assert data["status"] == "saved"
        assert "new_profile" in {p.name for p in ai_svc.list_profiles()}

    async def test_delete_profile_tool(self, ai_svc: AIService) -> None:
        await ai_svc.set_profile(AIContextProfile(name="to_delete"))
        result = await ai_svc.execute_tool("delete_ai_profile", {"name": "to_delete"})
        data = json.loads(result)
        assert data["status"] == "deleted"

    async def test_delete_default_profile_tool_fails(self, ai_svc: AIService) -> None:
        result = await ai_svc.execute_tool("delete_ai_profile", {"name": "default"})
        data = json.loads(result)
        assert "error" in data

    async def test_assign_profile_tool(self, ai_svc: AIService) -> None:
        result = await ai_svc.execute_tool("assign_ai_profile", {
            "call_name": "test_call",
            "profile": "text_only",
        })
        data = json.loads(result)
        assert data["status"] == "assigned"
        assert ai_svc.list_assignments()["test_call"] == "text_only"

    async def test_clear_assignment_tool(self, ai_svc: AIService) -> None:
        await ai_svc.set_assignment("test_call", "text_only")
        result = await ai_svc.execute_tool("clear_ai_assignment", {"call_name": "test_call"})
        data = json.loads(result)
        assert data["status"] == "cleared"
        assert "test_call" not in ai_svc.list_assignments()
