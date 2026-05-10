"""Unit tests for MCPService — visibility, RBAC, validation, CRUD, execution.

Uses a fake ``MCPBackend`` registered under the ``stdio`` slot so records
carry a valid transport without spawning any subprocesses, and a lightweight
in-memory ``StorageBackend`` that supports enough of the query surface for
slug-uniqueness lookups.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.services.mcp import (
    MCPService,
    _ClientEntry,
)
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.mcp import (
    MCPAuthConfig,
    MCPBackend,
    MCPContentBlock,
    MCPPromptArgument,
    MCPPromptMessage,
    MCPPromptResult,
    MCPPromptSpec,
    MCPResourceContent,
    MCPResourceSpec,
    MCPServerRecord,
    MCPToolResult,
    MCPToolSpec,
)
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)

# ── fakes ───────────────────────────────────────────────────────────


class FakeMCPBackend(MCPBackend):
    """Records state transitions; canned tools/results per-instance."""

    # Intentionally unset — we inject into the registry manually per-test.
    backend_name = ""

    def __init__(self) -> None:
        self.connected: bool = False
        self.record: MCPServerRecord | None = None
        self.tools: list[MCPToolSpec] = []
        self.call_log: list[tuple[str, dict[str, Any]]] = []
        self.next_result: MCPToolResult | None = None
        self.closed_count: int = 0
        self.tools_changed_cb: Any = None
        self.resources: list[MCPResourceSpec] = []
        self.resource_contents: dict[str, list[MCPResourceContent]] = {}
        self.read_log: list[str] = []
        self.prompts: list[MCPPromptSpec] = []
        self.prompt_results: dict[str, MCPPromptResult] = {}
        self.prompt_log: list[tuple[str, dict[str, str]]] = []

    async def connect(self, record: MCPServerRecord) -> None:
        self.record = record
        self.connected = True

    async def close(self) -> None:
        self.connected = False
        self.closed_count += 1

    async def list_tools(self) -> list[MCPToolSpec]:
        return list(self.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> MCPToolResult:
        self.call_log.append((name, arguments))
        if self.next_result is not None:
            return self.next_result
        return MCPToolResult(
            content=(MCPContentBlock(type="text", text=f"called {name}"),),
            is_error=False,
        )

    async def list_resources(self) -> list[MCPResourceSpec]:
        return list(self.resources)

    async def read_resource(self, uri: str) -> list[MCPResourceContent]:
        self.read_log.append(uri)
        if uri in self.resource_contents:
            return list(self.resource_contents[uri])
        raise ValueError(f"unknown resource: {uri}")

    async def list_prompts(self) -> list[MCPPromptSpec]:
        return list(self.prompts)

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> MCPPromptResult:
        self.prompt_log.append((name, dict(arguments)))
        if name in self.prompt_results:
            return self.prompt_results[name]
        raise ValueError(f"unknown prompt: {name}")

    async def set_tools_changed_callback(self, callback: Any) -> None:
        self.tools_changed_cb = callback


class FakeStorage(StorageBackend):
    """In-memory storage that supports the slug-uniqueness query shape."""

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

    async def query(self, query: Query) -> list[dict[str, Any]]:
        rows = list(self._data.get(query.collection, {}).values())
        for f in query.filters:
            rows = [r for r in rows if self._match_filter(r, f)]
        return rows[: query.limit] if query.limit is not None else rows

    @staticmethod
    def _match_filter(row: dict[str, Any], f: Filter) -> bool:
        value = row.get(f.field)
        if f.op == FilterOp.EQ:
            return bool(value == f.value)
        if f.op == FilterOp.NEQ:
            return bool(value != f.value)
        return True

    async def count(self, query: Query) -> int:
        return len(await self.query(query))

    async def delete_query(self, query: Query) -> int:
        matches = await self.query(query)
        coll = self._data.get(query.collection, {})
        removed = 0
        for entity in matches:
            entity_id = entity.get("_id")
            if entity_id is not None and entity_id in coll:
                del coll[entity_id]
                removed += 1
        return removed

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: IndexDefinition) -> None:
        pass

    async def list_indexes(self, collection: str) -> list[IndexDefinition]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


class FakeStorageProvider(StorageProvider):
    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        raise NotImplementedError


class FakeACL(AccessControlProvider):
    """Minimal ACL matching Gilbert's built-in table."""

    _LEVELS = {"admin": 0, "user": 100, "everyone": 200}

    def get_role_level(self, role_name: str) -> int:
        return self._LEVELS.get(role_name, 200)

    def get_effective_level(self, user_ctx: UserContext) -> int:
        return min((self._LEVELS.get(r, 200) for r in user_ctx.roles), default=200)

    def resolve_rpc_level(self, frame_type: str) -> int:
        return 100

    def check_collection_read(self, user_ctx: UserContext, collection: str) -> bool:
        return True

    def check_collection_write(self, user_ctx: UserContext, collection: str) -> bool:
        return True


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def register_fake_backend() -> Iterator[type[FakeMCPBackend]]:
    """Install FakeMCPBackend as the ``stdio`` transport for the test."""
    original = MCPBackend._registry.get("stdio")
    MCPBackend._registry["stdio"] = FakeMCPBackend
    try:
        yield FakeMCPBackend
    finally:
        if original is not None:
            MCPBackend._registry["stdio"] = original
        else:
            MCPBackend._registry.pop("stdio", None)


@pytest.fixture
def svc() -> MCPService:
    service = MCPService()
    service._enabled = True
    service._storage = FakeStorage()
    service._acl_svc = FakeACL()
    return service


@pytest.fixture
def alice() -> UserContext:
    return UserContext(
        user_id="alice",
        email="a@x",
        display_name="Alice",
        roles=frozenset({"user"}),
    )


@pytest.fixture
def bob() -> UserContext:
    return UserContext(
        user_id="bob",
        email="b@x",
        display_name="Bob",
        roles=frozenset({"user"}),
    )


@pytest.fixture
def admin() -> UserContext:
    return UserContext(
        user_id="root",
        email="r@x",
        display_name="Root",
        roles=frozenset({"admin"}),
    )


def make_record(
    *,
    id: str = "srv",
    name: str = "Test",
    slug: str = "test",
    owner_id: str = "alice",
    scope: str = "private",
    allowed_roles: tuple[str, ...] = (),
    allowed_users: tuple[str, ...] = (),
) -> MCPServerRecord:
    return MCPServerRecord(
        id=id,
        name=name,
        slug=slug,
        transport="stdio",
        command=("true",),
        owner_id=owner_id,
        scope=scope,  # type: ignore[arg-type]
        allowed_roles=allowed_roles,
        allowed_users=allowed_users,
    )


def _install_client(
    svc: MCPService,
    record: MCPServerRecord,
    tools: list[MCPToolSpec] | None = None,
) -> FakeMCPBackend:
    """Directly attach a running fake client to the service, bypassing
    ``_start_client`` so we don't need a full async lifecycle in tests."""
    backend = FakeMCPBackend()
    backend.connected = True
    backend.record = record
    backend.tools = tools or []
    entry = _ClientEntry(record, backend)
    entry.connected = True
    entry.tools = tools or []
    entry.tools_fetched_at = 999999.0  # far in future so cache never expires
    svc._clients[record.id] = entry
    return backend


# ── tool name encoding ──────────────────────────────────────────────


class TestToolNameEncoding:
    def test_roundtrip(self) -> None:
        encoded = MCPService._encode_tool_name("weather", "forecast")
        assert encoded == "mcp__weather__forecast"
        assert MCPService._decode_tool_name(encoded) == ("weather", "forecast")

    def test_decode_rejects_foreign_names(self) -> None:
        with pytest.raises(KeyError):
            MCPService._decode_tool_name("not_mcp_name")

    def test_decode_rejects_malformed(self) -> None:
        with pytest.raises(KeyError):
            MCPService._decode_tool_name("mcp__onlyslug")


# ── visibility ──────────────────────────────────────────────────────


class TestVisibility:
    def test_private_owner_only(
        self, svc: MCPService, alice: UserContext, bob: UserContext
    ) -> None:
        record = make_record(scope="private", owner_id="alice")
        _install_client(svc, record)

        assert svc._can_see_server(record, alice) is True
        assert svc._can_see_server(record, bob) is False

    def test_admin_sees_all_private_servers(
        self,
        svc: MCPService,
        admin: UserContext,
    ) -> None:
        record = make_record(scope="private", owner_id="alice")
        assert svc._can_see_server(record, admin) is True

    def test_public_everyone(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(scope="public", owner_id="root")
        assert svc._can_see_server(record, alice) is True
        assert svc._can_see_server(record, bob) is True

    def test_shared_by_role(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        everyone_ctx = UserContext(
            user_id="guest",
            email="",
            display_name="Guest",
            roles=frozenset({"everyone"}),
        )
        record = make_record(
            scope="shared",
            owner_id="root",
            allowed_roles=("user",),
        )
        assert svc._can_see_server(record, alice) is True
        assert svc._can_see_server(record, everyone_ctx) is False

    def test_shared_by_user_id(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(
            scope="shared",
            owner_id="root",
            allowed_users=("alice",),
        )
        assert svc._can_see_server(record, alice) is True
        assert svc._can_see_server(record, bob) is False

    def test_shared_allow_lists_are_union(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(
            scope="shared",
            owner_id="root",
            allowed_roles=("nonexistent",),
            allowed_users=("alice",),
        )
        assert svc._can_see_server(record, alice) is True
        assert svc._can_see_server(record, bob) is False


# ── get_tools ───────────────────────────────────────────────────────


class TestGetTools:
    def test_returns_empty_when_disabled(self, svc: MCPService, alice: UserContext) -> None:
        svc._enabled = False
        assert svc.get_tools(alice) == []

    def test_returns_empty_when_user_ctx_none(self, svc: MCPService) -> None:
        record = make_record()
        _install_client(svc, record, tools=[MCPToolSpec(name="x", description="", input_schema={})])
        assert svc.get_tools(None) == []

    def test_hides_other_users_private_tools(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        alice_record = make_record(
            id="srv-alice",
            slug="alice-srv",
            owner_id="alice",
            scope="private",
        )
        bob_record = make_record(
            id="srv-bob",
            slug="bob-srv",
            owner_id="bob",
            scope="private",
        )
        _install_client(
            svc, alice_record, tools=[MCPToolSpec(name="a_tool", description="", input_schema={})]
        )
        _install_client(
            svc, bob_record, tools=[MCPToolSpec(name="b_tool", description="", input_schema={})]
        )

        alice_tools = [t.name for t in svc.get_tools(alice)]
        bob_tools = [t.name for t in svc.get_tools(bob)]

        assert alice_tools == ["mcp__alice-srv__a_tool"]
        assert bob_tools == ["mcp__bob-srv__b_tool"]

    def test_public_tool_visible_to_everyone(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(
            id="srv1",
            slug="weather",
            owner_id="root",
            scope="public",
        )
        _install_client(
            svc, record, tools=[MCPToolSpec(name="forecast", description="", input_schema={})]
        )

        assert "mcp__weather__forecast" in {t.name for t in svc.get_tools(alice)}
        assert "mcp__weather__forecast" in {t.name for t in svc.get_tools(bob)}

    def test_disconnected_client_hidden(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record, tools=[MCPToolSpec(name="t", description="", input_schema={})])
        # Directly mark the entry as disconnected
        svc._clients[record.id].connected = False
        assert svc.get_tools(alice) == []

    def test_tool_definition_shape(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(
            id="srv1",
            slug="cal",
            owner_id="alice",
        )
        _install_client(
            svc,
            record,
            tools=[
                MCPToolSpec(
                    name="next_event",
                    description="Get the next calendar event",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "description": "Max events"},
                            "q": {"type": "string", "description": "Search"},
                        },
                        "required": ["limit"],
                    },
                )
            ],
        )
        tools = svc.get_tools(alice)
        assert len(tools) == 1
        t = tools[0]
        assert t.name == "mcp__cal__next_event"
        # Visibility is the sole gate — the emitted tool uses ``everyone``
        # so downstream RBAC in AIService is a no-op for MCP tools.
        assert t.required_role == "everyone"
        assert t.slash_group == "cal"
        assert t.slash_command == "next_event"
        param_names = {p.name for p in t.parameters}
        assert param_names == {"limit", "q"}
        limit_param = next(p for p in t.parameters if p.name == "limit")
        assert limit_param.required is True


# ── execute_tool ────────────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_dispatches_to_correct_backend(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(id="srv1", slug="weather", owner_id="alice")
        backend = _install_client(
            svc,
            record,
            tools=[MCPToolSpec(name="forecast", description="", input_schema={})],
        )
        set_current_user(alice)
        result = await svc.execute_tool(
            "mcp__weather__forecast",
            {"city": "SF"},
        )
        assert "called forecast" in result
        assert backend.call_log == [("forecast", {"city": "SF"})]

    @pytest.mark.asyncio
    async def test_raises_permission_error_for_hidden_server(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(id="srv1", slug="weather", owner_id="alice", scope="private")
        _install_client(
            svc,
            record,
            tools=[MCPToolSpec(name="forecast", description="", input_schema={})],
        )
        set_current_user(bob)
        with pytest.raises(PermissionError):
            await svc.execute_tool("mcp__weather__forecast", {})

    @pytest.mark.asyncio
    async def test_raises_when_slug_unknown(self, svc: MCPService, alice: UserContext) -> None:
        set_current_user(alice)
        with pytest.raises(PermissionError):
            await svc.execute_tool("mcp__nonexistent__foo", {})

    @pytest.mark.asyncio
    async def test_error_result_is_prefixed(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(id="srv1", slug="weather", owner_id="alice")
        backend = _install_client(svc, record)
        backend.tools = [MCPToolSpec(name="forecast", description="", input_schema={})]
        backend.next_result = MCPToolResult(
            content=(MCPContentBlock(type="text", text="boom"),),
            is_error=True,
        )
        set_current_user(alice)
        result = await svc.execute_tool("mcp__weather__forecast", {})
        assert result.startswith("[error]")
        assert "boom" in result


# ── validation ──────────────────────────────────────────────────────


class TestRemoteTransports:
    def test_backends_registered(self) -> None:
        """stdio/http/sse all live in the registry after import."""
        # Trigger import side-effects even if the service module wasn't
        # loaded earlier in the test session.
        import gilbert.integrations.mcp_http  # noqa: F401
        import gilbert.integrations.mcp_stdio  # noqa: F401

        registry = MCPBackend.registered_backends()
        assert "stdio" in registry
        assert "http" in registry
        assert "sse" in registry

    def test_http_record_requires_url(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="http",
            command=(),
            owner_id="alice",
        )
        with pytest.raises(ValueError, match="URL"):
            MCPService._validate_record(record)

    def test_http_url_must_be_absolute(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="http",
            url="example.com/mcp",
            command=(),
            owner_id="alice",
        )
        with pytest.raises(ValueError, match="http://"):
            MCPService._validate_record(record)

    def test_http_accepts_https_url(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="http",
            url="https://example.com/mcp",
            command=(),
            owner_id="alice",
        )
        MCPService._validate_record(record)  # should not raise

    def test_sse_record_requires_url(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="sse",
            command=(),
            owner_id="alice",
        )
        with pytest.raises(ValueError, match="URL"):
            MCPService._validate_record(record)

    def test_stdio_rejects_auth(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Local",
            slug="local",
            transport="stdio",
            command=("true",),
            owner_id="alice",
            auth=MCPAuthConfig(kind="bearer", bearer_token="secret"),
        )
        with pytest.raises(ValueError, match="Stdio"):
            MCPService._validate_record(record)

    def test_bearer_requires_token(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="http",
            url="https://example.com/mcp",
            command=(),
            owner_id="alice",
            auth=MCPAuthConfig(kind="bearer", bearer_token=""),
        )
        with pytest.raises(ValueError, match="Bearer"):
            MCPService._validate_record(record)


class TestAuthMerging:
    def test_masked_bearer_preserved(self) -> None:
        from gilbert.core.services.mcp import _merge_auth

        existing = MCPAuthConfig(kind="bearer", bearer_token="real-secret")
        merged = _merge_auth(existing, {"kind": "bearer", "bearer_token": "****"})
        assert merged.bearer_token == "real-secret"
        assert merged.kind == "bearer"

    def test_real_bearer_replaces(self) -> None:
        from gilbert.core.services.mcp import _merge_auth

        existing = MCPAuthConfig(kind="bearer", bearer_token="old")
        merged = _merge_auth(existing, {"kind": "bearer", "bearer_token": "new"})
        assert merged.bearer_token == "new"

    def test_switching_to_none_clears_kind(self) -> None:
        from gilbert.core.services.mcp import _merge_auth

        existing = MCPAuthConfig(kind="bearer", bearer_token="real")
        merged = _merge_auth(existing, {"kind": "none", "bearer_token": ""})
        assert merged.kind == "none"

    def test_scope_list_replaced(self) -> None:
        from gilbert.core.services.mcp import _merge_auth

        existing = MCPAuthConfig(kind="oauth", oauth_scopes=("read",))
        merged = _merge_auth(
            existing,
            {"kind": "oauth", "oauth_scopes": ["read", "write"]},
        )
        assert merged.oauth_scopes == ("read", "write")


class TestBearerSerialization:
    def test_bearer_masked_for_non_owner(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        # alice owns a bearer-auth record; bob shouldn't see the real token.
        record = MCPServerRecord(
            id="a",
            name="Remote",
            slug="remote",
            transport="stdio",  # use stdio to avoid real HTTP, validation
            command=("true",),
            owner_id="alice",
            scope="public",
        )
        # Manually set the auth since stdio rejects bearer at validation
        # time; the serializer doesn't know or care about that rule.
        from dataclasses import replace

        record = replace(
            record,
            auth=MCPAuthConfig(kind="bearer", bearer_token="real-secret"),
        )
        _install_client(svc, record)
        alice_view = svc._serialize_record(record, alice)
        bob_view = svc._serialize_record(record, bob)
        assert alice_view["auth"]["bearer_token"] == "real-secret"
        assert bob_view["auth"]["bearer_token"] == "****"


class TestValidation:
    def test_rejects_uppercase_slug(self) -> None:
        with pytest.raises(ValueError, match="Invalid slug"):
            MCPService._validate_record(make_record(slug="Weather"))

    def test_rejects_slug_with_double_underscore(self) -> None:
        with pytest.raises(ValueError, match=r"Invalid slug"):
            MCPService._validate_record(make_record(slug="we__ather"))

    def test_rejects_empty_command(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="X",
            slug="x",
            transport="stdio",
            command=(),
            owner_id="alice",
        )
        with pytest.raises(ValueError, match="command"):
            MCPService._validate_record(record)

    def test_rejects_shared_without_any_grants(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            MCPService._validate_record(make_record(scope="shared"))

    def test_rejects_private_with_grants(self) -> None:
        with pytest.raises(ValueError, match="cannot have"):
            MCPService._validate_record(
                make_record(scope="private", allowed_users=("bob",)),
            )

    def test_rejects_missing_owner(self) -> None:
        with pytest.raises(ValueError, match="owner_id"):
            MCPService._validate_record(make_record(owner_id=""))

    def test_rejects_sampling_on_stdio(self) -> None:
        from dataclasses import replace

        record = replace(make_record(owner_id="alice"), allow_sampling=True)
        with pytest.raises(ValueError, match="remote MCP transports"):
            MCPService._validate_record(record)

    def test_rejects_sampling_with_zero_budget(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="http",
            url="https://example.com/mcp",
            command=(),
            owner_id="alice",
            allow_sampling=True,
            sampling_budget_tokens=0,
        )
        with pytest.raises(ValueError, match="sampling_budget_tokens"):
            MCPService._validate_record(record)

    def test_rejects_sampling_with_empty_profile(self) -> None:
        record = MCPServerRecord(
            id="x",
            name="Remote",
            slug="remote",
            transport="http",
            url="https://example.com/mcp",
            command=(),
            owner_id="alice",
            allow_sampling=True,
            sampling_profile="",
        )
        with pytest.raises(ValueError, match="sampling_profile"):
            MCPService._validate_record(record)


# ── slug uniqueness (global) ────────────────────────────────────────


class TestSlugUniqueness:
    @pytest.mark.asyncio
    async def test_reject_duplicate_slug_on_create(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
    ) -> None:
        rec_a = make_record(id="a", slug="weather", owner_id="alice")
        rec_b = make_record(id="b", slug="weather", owner_id="bob")
        await svc.create_server(rec_a)
        with pytest.raises(ValueError, match="already in use"):
            await svc.create_server(rec_b)

    @pytest.mark.asyncio
    async def test_update_allows_keeping_own_slug(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
    ) -> None:
        rec = make_record(id="a", slug="weather", owner_id="alice", name="Old")
        await svc.create_server(rec)
        updated = make_record(id="a", slug="weather", owner_id="alice", name="New")
        result = await svc.update_server(updated)
        assert result.name == "New"

    @pytest.mark.asyncio
    async def test_update_rejects_taking_someone_elses_slug(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
    ) -> None:
        await svc.create_server(
            make_record(id="a", slug="weather", owner_id="alice"),
        )
        await svc.create_server(
            make_record(id="b", slug="cal", owner_id="bob"),
        )
        collider = make_record(id="b", slug="weather", owner_id="bob")
        with pytest.raises(ValueError, match="already in use"):
            await svc.update_server(collider)


# ── record marshalling ─────────────────────────────────────────────


class _FakeConn:
    """Minimal stand-in for ``WsConnectionBase`` — handlers only touch
    ``user_ctx``, so we don't need the queue/enqueue machinery."""

    def __init__(self, user_ctx: UserContext) -> None:
        self.user_ctx = user_ctx
        self.user_level = 0 if "admin" in user_ctx.roles else 100
        self.shared_conv_ids: set[str] = set()
        # pytest's asyncio mode doesn't actually drive this, but the
        # Protocol asks for it so satisfy the shape.
        import asyncio

        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @property
    def user_id(self) -> str:
        return self.user_ctx.user_id

    def enqueue(self, msg: dict[str, Any]) -> None:
        self.queue.put_nowait(msg)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Weather",
        "slug": "weather",
        "transport": "stdio",
        "command": ["true"],
        "env": {},
        "enabled": True,
        "auto_start": True,
        "scope": "private",
    }
    base.update(overrides)
    return base


# ── WS RPC handlers ────────────────────────────────────────────────


class TestWsHandlers:
    @pytest.mark.asyncio
    async def test_list_only_returns_visible_servers(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        await svc.create_server(
            make_record(
                id="a",
                slug="alice-srv",
                owner_id="alice",
                scope="private",
            )
        )
        await svc.create_server(
            make_record(
                id="b",
                slug="bob-srv",
                owner_id="bob",
                scope="private",
            )
        )
        frame = {"id": 1}
        result = await svc._ws_list(_FakeConn(alice), frame)
        assert result is not None
        slugs = {s["slug"] for s in result["servers"]}
        assert slugs == {"alice-srv"}

    @pytest.mark.asyncio
    async def test_list_masks_env_for_non_owner(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        admin: UserContext,
        alice: UserContext,
    ) -> None:
        # Admin creates a public server with an API key in env.
        await svc.create_server(
            MCPServerRecord(
                id="srv",
                name="Weather",
                slug="weather",
                transport="stdio",
                command=("true",),
                env={"API_KEY": "super-secret"},
                scope="public",
                owner_id="root",
            )
        )
        # Alice can see the public server but env should be masked.
        result = await svc._ws_list(_FakeConn(alice), {"id": 1})
        assert result is not None
        server = result["servers"][0]
        assert server["env"] == {"API_KEY": "****"}

        # Admin sees the real value.
        admin_result = await svc._ws_list(_FakeConn(admin), {"id": 2})
        assert admin_result is not None
        assert admin_result["servers"][0]["env"] == {"API_KEY": "super-secret"}

    @pytest.mark.asyncio
    async def test_non_admin_cannot_create_shared(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        frame = {
            "id": 1,
            "server": _payload(scope="shared", allowed_users=["bob"]),
        }
        result = await svc._ws_create(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_non_admin_can_create_private(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        frame = {"id": 1, "server": _payload()}
        result = await svc._ws_create(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "mcp.servers.create.result"
        assert result["server"]["owner_id"] == "alice"
        assert result["server"]["slug"] == "weather"

    @pytest.mark.asyncio
    async def test_admin_can_create_public(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        admin: UserContext,
    ) -> None:
        frame = {"id": 1, "server": _payload(scope="public")}
        result = await svc._ws_create(_FakeConn(admin), frame)
        assert result is not None
        assert result["type"] == "mcp.servers.create.result"
        assert result["server"]["scope"] == "public"

    @pytest.mark.asyncio
    async def test_non_owner_non_admin_cannot_update(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        await svc.create_server(make_record(id="a", slug="cal", owner_id="alice"))
        frame = {
            "id": 1,
            "server": {**_payload(slug="cal"), "id": "a", "name": "Hijacked"},
        }
        result = await svc._ws_update(_FakeConn(bob), frame)
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_owner_cannot_change_scope(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        await svc.create_server(make_record(id="a", slug="cal", owner_id="alice"))
        frame = {
            "id": 1,
            "server": {
                **_payload(slug="cal", scope="public"),
                "id": "a",
            },
        }
        result = await svc._ws_update(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403
        assert "scope" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_owner_can_change_non_visibility_fields(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        await svc.create_server(make_record(id="a", slug="cal", owner_id="alice"))
        frame = {
            "id": 1,
            "server": {
                **_payload(slug="cal"),
                "id": "a",
                "name": "Calendar (updated)",
                "command": ["new-binary"],
            },
        }
        result = await svc._ws_update(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "mcp.servers.update.result"
        assert result["server"]["name"] == "Calendar (updated)"
        assert result["server"]["command"] == ["new-binary"]

    @pytest.mark.asyncio
    async def test_update_preserves_masked_env_values(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        record = MCPServerRecord(
            id="a",
            name="Cal",
            slug="cal",
            transport="stdio",
            command=("true",),
            env={"API_KEY": "real-secret", "DEBUG": "1"},
            owner_id="alice",
            scope="private",
        )
        await svc.create_server(record)

        # Simulate the UI sending the masked value back unchanged for
        # API_KEY and a real new value for DEBUG; omitting a key means
        # "drop it".
        frame = {
            "id": 1,
            "server": {
                **_payload(slug="cal"),
                "id": "a",
                "env": {"API_KEY": "****", "DEBUG": "0"},
            },
        }
        result = await svc._ws_update(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "mcp.servers.update.result"
        stored = svc._clients["a"].record
        assert stored.env == {"API_KEY": "real-secret", "DEBUG": "0"}

    @pytest.mark.asyncio
    async def test_delete_owner_only(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        await svc.create_server(make_record(id="a", slug="cal", owner_id="alice"))
        denied = await svc._ws_delete(_FakeConn(bob), {"id": 1, "server_id": "a"})
        assert denied is not None
        assert denied["code"] == 403
        ok = await svc._ws_delete(_FakeConn(alice), {"id": 2, "server_id": "a"})
        assert ok is not None
        assert ok["type"] == "mcp.servers.delete.result"
        assert "a" not in svc._clients

    @pytest.mark.asyncio
    async def test_non_admin_cannot_test_shared_draft(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        frame = {
            "id": 1,
            "server": _payload(scope="shared", allowed_roles=["user"]),
        }
        result = await svc._ws_test(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_non_admin_cannot_enable_sampling_on_create(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        frame = {
            "id": 1,
            "server": {
                **_payload(),
                "transport": "http",
                "url": "https://example.com/mcp",
                "allow_sampling": True,
            },
        }
        result = await svc._ws_create(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403
        assert "sampling" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_non_admin_cannot_enable_sampling_on_update(
        self,
        svc: MCPService,
        register_fake_backend: type[FakeMCPBackend],
        alice: UserContext,
    ) -> None:
        # Alice owns a remote private server with sampling off.
        base = MCPServerRecord(
            id="a",
            name="Remote",
            slug="remote",
            transport="http",
            url="https://example.com/mcp",
            command=(),
            owner_id="alice",
            scope="private",
        )
        await svc.create_server(base)
        # Try to flip allow_sampling without being admin.
        frame = {
            "id": 1,
            "server": {
                "id": "a",
                "name": "Remote",
                "slug": "remote",
                "transport": "http",
                "url": "https://example.com/mcp",
                "scope": "private",
                "allow_sampling": True,
            },
        }
        result = await svc._ws_update(_FakeConn(alice), frame)
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403
        assert "sampling" in result["error"].lower()


class TestResourceHandlers:
    """Exercise the ``mcp.servers.resources.*`` RPCs — visibility
    gating, happy-path serialization, and NotImplementedError
    translation for backends that don't advertise resource support."""

    @pytest.mark.asyncio
    async def test_list_returns_server_resources(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        backend = _install_client(svc, record)
        backend.resources = [
            MCPResourceSpec(
                uri="file:///a.txt",
                name="a",
                description="file A",
                mime_type="text/plain",
                size=42,
            ),
        ]
        result = await svc._ws_resources_list(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id},
        )
        assert result is not None
        assert result["type"] == "mcp.servers.resources.list.result"
        assert len(result["resources"]) == 1
        entry = result["resources"][0]
        assert entry["uri"] == "file:///a.txt"
        assert entry["name"] == "a"
        assert entry["size"] == 42

    @pytest.mark.asyncio
    async def test_list_hides_server_from_non_owner(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(owner_id="alice", scope="private")
        _install_client(svc, record)
        result = await svc._ws_resources_list(
            _FakeConn(bob),
            {"id": 1, "server_id": record.id},
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 404

    @pytest.mark.asyncio
    async def test_list_rejects_disconnected_server(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record)
        svc._clients[record.id].connected = False
        svc._clients[record.id].last_error = "boom"
        result = await svc._ws_resources_list(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id},
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 503
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_read_returns_content(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        backend = _install_client(svc, record)
        backend.resource_contents["file:///a.txt"] = [
            MCPResourceContent(
                uri="file:///a.txt",
                kind="text",
                mime_type="text/plain",
                text="hello",
            ),
        ]
        result = await svc._ws_resources_read(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id, "uri": "file:///a.txt"},
        )
        assert result is not None
        assert result["type"] == "mcp.servers.resources.read.result"
        assert backend.read_log == ["file:///a.txt"]
        contents = result["contents"]
        assert len(contents) == 1
        assert contents[0]["kind"] == "text"
        assert contents[0]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_read_missing_uri_returns_400(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record)
        result = await svc._ws_resources_read(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id},
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert "uri" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_read_surfaces_backend_error(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record)
        result = await svc._ws_resources_read(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id, "uri": "file:///missing"},
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert "unknown resource" in result["error"]


class TestPromptHandlers:
    """Exercise ``mcp.servers.prompts.*`` — visibility, serialization,
    arg handling, and error paths."""

    @pytest.mark.asyncio
    async def test_list_returns_prompts(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        backend = _install_client(svc, record)
        backend.prompts = [
            MCPPromptSpec(
                name="friendly_intro",
                description="Intro someone.",
                arguments=(
                    MCPPromptArgument(name="user_name", required=True),
                    MCPPromptArgument(name="tone", required=False),
                ),
            ),
        ]
        result = await svc._ws_prompts_list(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id},
        )
        assert result is not None
        assert result["type"] == "mcp.servers.prompts.list.result"
        assert len(result["prompts"]) == 1
        prompt = result["prompts"][0]
        assert prompt["name"] == "friendly_intro"
        assert len(prompt["arguments"]) == 2
        required = [a for a in prompt["arguments"] if a["required"]]
        assert len(required) == 1
        assert required[0]["name"] == "user_name"

    @pytest.mark.asyncio
    async def test_list_hidden_from_non_owner(
        self,
        svc: MCPService,
        alice: UserContext,
        bob: UserContext,
    ) -> None:
        record = make_record(owner_id="alice", scope="private")
        _install_client(svc, record)
        result = await svc._ws_prompts_list(
            _FakeConn(bob),
            {"id": 1, "server_id": record.id},
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 404

    @pytest.mark.asyncio
    async def test_get_renders_prompt_with_args(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        backend = _install_client(svc, record)
        backend.prompt_results["friendly_intro"] = MCPPromptResult(
            description="Canned intro.",
            messages=(
                MCPPromptMessage(
                    role="user",
                    content=MCPContentBlock(
                        type="text",
                        text="Say hello to Alice",
                    ),
                ),
            ),
        )
        result = await svc._ws_prompts_get(
            _FakeConn(alice),
            {
                "id": 1,
                "server_id": record.id,
                "name": "friendly_intro",
                "arguments": {"user_name": "Alice"},
            },
        )
        assert result is not None
        assert result["type"] == "mcp.servers.prompts.get.result"
        assert result["description"] == "Canned intro."
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"]["text"] == "Say hello to Alice"
        # Backend received the argument dict
        assert backend.prompt_log == [("friendly_intro", {"user_name": "Alice"})]

    @pytest.mark.asyncio
    async def test_get_missing_name_returns_400(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record)
        result = await svc._ws_prompts_get(
            _FakeConn(alice),
            {"id": 1, "server_id": record.id, "arguments": {}},
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert "name" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_invalid_arguments_type_returns_400(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record)
        result = await svc._ws_prompts_get(
            _FakeConn(alice),
            {
                "id": 1,
                "server_id": record.id,
                "name": "friendly_intro",
                "arguments": "not a dict",
            },
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert "arguments" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_surfaces_backend_error(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        record = make_record(owner_id="alice")
        _install_client(svc, record)
        result = await svc._ws_prompts_get(
            _FakeConn(alice),
            {
                "id": 1,
                "server_id": record.id,
                "name": "nonexistent",
                "arguments": {},
            },
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert "unknown prompt" in result["error"]


class TestMarshalling:
    def test_record_from_doc_roundtrip(self) -> None:
        original = make_record(
            id="x",
            slug="cal",
            owner_id="alice",
            scope="shared",
            allowed_roles=("user",),
            allowed_users=("bob",),
        )
        doc = MCPService._doc_from_record(original)
        assert doc["_id"] == "x"
        assert doc["slug"] == "cal"
        restored = MCPService._record_from_doc(doc)
        assert restored.id == original.id
        assert restored.slug == original.slug
        assert restored.allowed_users == original.allowed_users
        assert restored.allowed_roles == original.allowed_roles
        assert restored.scope == original.scope
        assert restored.command == original.command


# ── browser bridge (session-ephemeral) ─────────────────────────────


class FakeBridgeConn:
    """A fake ``WsConnectionBase`` that pipes ``call_client`` to a stub
    browser, records close callbacks, and exposes a ``user_ctx``."""

    def __init__(self, user_ctx: UserContext) -> None:
        self.user_ctx = user_ctx
        self.user_level = 100
        self.shared_conv_ids: set[str] = set()
        self.queue: Any = None
        self.manager: Any = None
        self._close_callbacks: list[Any] = []
        # Pluggable responder — the test sets this to shape replies.
        self.responder: Any = None
        self.call_log: list[dict[str, Any]] = []

    @property
    def user_id(self) -> str:
        return self.user_ctx.user_id

    def enqueue(self, msg: dict[str, Any]) -> None:
        raise AssertionError("unused in bridge tests")

    async def call_client(
        self,
        frame: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.call_log.append(frame)
        if self.responder is None:
            raise RuntimeError("no responder configured")
        return await self.responder(frame)

    def cancel_pending_outbound(self) -> None:
        pass

    def add_close_callback(self, callback: Any) -> None:
        self._close_callbacks.append(callback)

    def run_close_callbacks(self) -> None:
        for cb in self._close_callbacks:
            cb()
        self._close_callbacks.clear()


def _bridge_reply(
    tools: list[dict[str, Any]] | None = None, tool_results: dict[str, dict[str, Any]] | None = None
) -> Any:
    """Build a responder that answers tools/list and tools/call."""
    tool_results = tool_results or {}

    async def responder(frame: dict[str, Any]) -> dict[str, Any]:
        method = frame.get("method")
        if method == "tools/list":
            return {"ok": True, "result": {"tools": tools or []}}
        if method == "tools/call":
            name = frame.get("params", {}).get("name", "")
            if name in tool_results:
                return {"ok": True, "result": tool_results[name]}
            return {
                "ok": True,
                "result": {
                    "content": [{"type": "text", "text": f"called {name}"}],
                    "isError": False,
                },
            }
        return {"ok": False, "error": f"unknown method {method}"}

    return responder


class TestBrowserBridge:
    async def test_announce_registers_session_tools(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        conn = FakeBridgeConn(alice)
        conn.responder = _bridge_reply(
            tools=[
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            ],
        )

        result = await svc._ws_bridge_announce(
            conn,
            {"id": "a1", "servers": [{"slug": "fs", "name": "Filesystem"}]},
        )
        assert result is not None
        assert result["type"] == "mcp.bridge.announce.result"
        assert result["results"] == [
            {"slug": "fs", "ok": True, "tool_count": 1},
        ]

        # Session registry populated, conn registered as owner, close
        # callback wired in.
        assert "fs" in svc._session_clients["alice"]
        assert svc._session_conn["alice"] is conn
        assert len(conn._close_callbacks) == 1

        # get_tools sees the session tool, nobody else does.
        tools = svc.get_tools(alice)
        assert [t.name for t in tools] == ["mcp__fs__read_file"]
        other = UserContext(
            user_id="carol",
            email="",
            display_name="Carol",
            roles=frozenset({"user"}),
        )
        assert svc.get_tools(other) == []

    async def test_execute_session_tool_round_trips(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        conn = FakeBridgeConn(alice)
        conn.responder = _bridge_reply(
            tools=[{"name": "echo", "description": "", "inputSchema": {}}],
            tool_results={
                "echo": {
                    "content": [{"type": "text", "text": "hi!"}],
                    "isError": False,
                },
            },
        )
        await svc._ws_bridge_announce(
            conn,
            {"id": "a1", "servers": [{"slug": "tool", "name": "Tool"}]},
        )
        set_current_user(alice)
        result = await svc.execute_tool(
            "mcp__tool__echo",
            {"message": "hello"},
        )
        assert "hi!" in result
        # Two bridge calls: tools/list (announce probe) + tools/call.
        methods = [c["method"] for c in conn.call_log]
        assert methods == ["tools/list", "tools/call"]
        assert conn.call_log[1]["params"] == {
            "name": "echo",
            "arguments": {"message": "hello"},
        }

    async def test_disconnect_tears_down_session(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        conn = FakeBridgeConn(alice)
        conn.responder = _bridge_reply(
            tools=[{"name": "t", "description": "", "inputSchema": {}}],
        )
        await svc._ws_bridge_announce(
            conn,
            {"id": "a", "servers": [{"slug": "fs", "name": "FS"}]},
        )
        assert svc.get_tools(alice)

        # Simulate disconnect: the close callback should drop the session.
        conn.run_close_callbacks()
        # Give any scheduled teardown tasks a chance to run.
        await asyncio.sleep(0)
        assert "alice" not in svc._session_clients
        assert "alice" not in svc._session_conn
        assert svc.get_tools(alice) == []

    async def test_second_tab_replaces_first(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        tab_one = FakeBridgeConn(alice)
        tab_one.responder = _bridge_reply(
            tools=[{"name": "a", "description": "", "inputSchema": {}}],
        )
        await svc._ws_bridge_announce(
            tab_one,
            {"id": "1", "servers": [{"slug": "fs", "name": "FS"}]},
        )

        tab_two = FakeBridgeConn(alice)
        tab_two.responder = _bridge_reply(
            tools=[{"name": "b", "description": "", "inputSchema": {}}],
        )
        await svc._ws_bridge_announce(
            tab_two,
            {"id": "2", "servers": [{"slug": "fs", "name": "FS"}]},
        )

        # Session now belongs to tab_two with its own tool set.
        assert svc._session_conn["alice"] is tab_two
        tools = [t.name for t in svc.get_tools(alice)]
        assert tools == ["mcp__fs__b"]

        # Tab one disconnecting late must NOT clobber the live session.
        tab_one.run_close_callbacks()
        await asyncio.sleep(0)
        assert svc._session_conn.get("alice") is tab_two
        assert [t.name for t in svc.get_tools(alice)] == ["mcp__fs__b"]

        # Tab two disconnecting does clean up.
        tab_two.run_close_callbacks()
        await asyncio.sleep(0)
        assert "alice" not in svc._session_conn

    async def test_slug_collision_with_persisted_rejected(
        self,
        svc: MCPService,
        alice: UserContext,
        register_fake_backend: type[FakeMCPBackend],
    ) -> None:
        # Install a persisted shared record visible to alice.
        shared = make_record(
            id="shared",
            slug="fs",
            owner_id="root",
            scope="shared",
            allowed_roles=("user",),
        )
        _install_client(svc, shared)

        conn = FakeBridgeConn(alice)
        conn.responder = _bridge_reply(
            tools=[{"name": "read", "description": "", "inputSchema": {}}],
        )
        result = await svc._ws_bridge_announce(
            conn,
            {"id": "a", "servers": [{"slug": "fs", "name": "Mine"}]},
        )
        assert result is not None
        assert result["results"][0]["ok"] is False
        assert "conflicts" in result["results"][0]["error"]
        # Session was opened (owner set) but fs was rejected.
        assert svc._session_conn.get("alice") is conn
        assert "fs" not in svc._session_clients.get("alice", {})

    async def test_invalid_slug_rejected(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        conn = FakeBridgeConn(alice)
        result = await svc._ws_bridge_announce(
            conn,
            {"id": "a", "servers": [{"slug": "Not Valid!", "name": "Bad"}]},
        )
        assert result is not None
        assert result["results"][0]["ok"] is False
        assert "invalid slug" in result["results"][0]["error"]
        # No responder was needed because validation failed pre-probe.
        assert conn.call_log == []

    async def test_probe_failure_surfaced(
        self,
        svc: MCPService,
        alice: UserContext,
    ) -> None:
        conn = FakeBridgeConn(alice)

        async def responder(frame: dict[str, Any]) -> dict[str, Any]:
            return {"ok": False, "error": "localhost:9999 unreachable"}

        conn.responder = responder
        result = await svc._ws_bridge_announce(
            conn,
            {"id": "a", "servers": [{"slug": "fs", "name": "FS"}]},
        )
        assert result is not None
        assert result["results"][0]["ok"] is False
        assert "unreachable" in result["results"][0]["error"]
        assert "fs" not in svc._session_clients.get("alice", {})
