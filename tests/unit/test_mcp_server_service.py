"""Unit tests for MCPServerService (Part 4.1) — CRUD, token lifecycle,
admin gates, and the authentication lookup path.

Covers both the direct service API and the WS RPC handlers so the
full admin-only surface is pinned down.
"""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.services.mcp_server import (
    TOKEN_PREFIX,
    MCPServerClient,
    MCPServerService,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import ServiceResolver
from tests.unit.test_mcp_service import FakeACL, FakeStorage


class _FakeUsersService:
    """Minimal stand-in for the users service — supports only the
    ``get_user`` method the mcp_server service calls."""

    def __init__(self, users: dict[str, dict[str, Any]]) -> None:
        self._users = users

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._users.get(user_id)


class _FakeResolver(ServiceResolver):
    def __init__(self, caps: dict[str, Any]) -> None:
        self._caps = caps

    def get_capability(self, capability: str) -> Any:
        return self._caps.get(capability)

    def require_capability(self, capability: str) -> Any:
        if capability in self._caps:
            return self._caps[capability]
        raise LookupError(capability)

    def get_all(self, capability: str) -> list[Any]:
        return []


class _FakeConn:
    def __init__(self, user_ctx: UserContext) -> None:
        self.user_ctx = user_ctx
        self.user_level = 0 if "admin" in user_ctx.roles else 100
        import asyncio

        self.shared_conv_ids: set[str] = set()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @property
    def user_id(self) -> str:
        return self.user_ctx.user_id

    def enqueue(self, msg: dict[str, Any]) -> None:
        self.queue.put_nowait(msg)


@pytest.fixture
async def svc() -> MCPServerService:
    service = MCPServerService()
    service._storage = FakeStorage()
    service._acl_svc = FakeACL()
    service._resolver = _FakeResolver(
        {
            "users": _FakeUsersService(
                {
                    "alice": {
                        "user_id": "alice",
                        "email": "alice@example.com",
                        "display_name": "Alice",
                        "roles": ["user"],
                    },
                    "admin": {
                        "user_id": "admin",
                        "email": "admin@example.com",
                        "display_name": "Admin",
                        "roles": ["admin"],
                    },
                },
            ),
            "access_control": FakeACL(),
        },
    )
    return service


@pytest.fixture
def admin_ctx() -> UserContext:
    return UserContext(
        user_id="admin",
        email="admin@x",
        display_name="Admin",
        roles=frozenset({"admin"}),
    )


@pytest.fixture
def alice_ctx() -> UserContext:
    return UserContext(
        user_id="alice",
        email="alice@x",
        display_name="Alice",
        roles=frozenset({"user"}),
    )


class TestTokenLifecycle:
    @pytest.mark.asyncio
    async def test_create_returns_plaintext_token_with_prefix(
        self,
        svc: MCPServerService,
    ) -> None:
        client, token = await svc.create_client(
            name="Claude Desktop",
            owner_user_id="alice",
        )
        assert token.startswith(TOKEN_PREFIX)
        # Stored hash is opaque — never equal to the plaintext.
        assert client.token_hash != token
        assert client.token_hash  # non-empty argon2 output
        # Prefix retained for admin identification in list views.
        assert client.token_prefix == token[: len(TOKEN_PREFIX) + 6]

    @pytest.mark.asyncio
    async def test_create_rejects_blank_name(
        self,
        svc: MCPServerService,
    ) -> None:
        with pytest.raises(ValueError, match="name"):
            await svc.create_client(name="", owner_user_id="alice")

    @pytest.mark.asyncio
    async def test_create_rejects_missing_owner(
        self,
        svc: MCPServerService,
    ) -> None:
        with pytest.raises(ValueError, match="owner_user_id"):
            await svc.create_client(name="X", owner_user_id="")

    @pytest.mark.asyncio
    async def test_rotate_issues_new_token_and_invalidates_old(
        self,
        svc: MCPServerService,
    ) -> None:
        client, old_token = await svc.create_client(
            name="X",
            owner_user_id="alice",
        )
        _, new_token = await svc.rotate_token(client.id)

        assert new_token != old_token
        assert new_token.startswith(TOKEN_PREFIX)

        # Old token should no longer authenticate.
        result = await svc.authenticate(old_token)
        assert result is None

        # New token should authenticate.
        result = await svc.authenticate(new_token)
        assert result is not None

    @pytest.mark.asyncio
    async def test_rotate_unknown_client_raises(
        self,
        svc: MCPServerService,
    ) -> None:
        with pytest.raises(LookupError):
            await svc.rotate_token("nonexistent")


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_successful_auth_resolves_user_context(
        self,
        svc: MCPServerService,
    ) -> None:
        client, token = await svc.create_client(
            name="X",
            owner_user_id="alice",
        )
        result = await svc.authenticate(token, client_ip="10.0.0.1")
        assert result is not None
        resolved_client, user_ctx = result
        assert resolved_client.id == client.id
        assert user_ctx.user_id == "alice"
        assert user_ctx.email == "alice@example.com"
        assert user_ctx.session_id == f"mcp_client:{client.id}"
        assert user_ctx.metadata["mcp_client_id"] == client.id

    @pytest.mark.asyncio
    async def test_authenticate_updates_last_used_at(
        self,
        svc: MCPServerService,
    ) -> None:
        client, token = await svc.create_client(
            name="X",
            owner_user_id="alice",
        )
        assert client.last_used_at is None
        await svc.authenticate(token, client_ip="192.168.1.5")
        refreshed = await svc.get_client(client.id)
        assert refreshed is not None
        assert refreshed.last_used_at is not None
        assert refreshed.last_ip == "192.168.1.5"

    @pytest.mark.asyncio
    async def test_malformed_token_rejected_without_hash_lookup(
        self,
        svc: MCPServerService,
    ) -> None:
        # Any string not starting with the prefix is rejected cheaply.
        assert await svc.authenticate("wrong-shape-token") is None
        assert await svc.authenticate("") is None

    @pytest.mark.asyncio
    async def test_inactive_client_cannot_authenticate(
        self,
        svc: MCPServerService,
    ) -> None:
        client, token = await svc.create_client(
            name="X",
            owner_user_id="alice",
        )
        await svc.update_client(client.id, active=False)
        assert await svc.authenticate(token) is None

    @pytest.mark.asyncio
    async def test_unknown_owner_rejected(
        self,
        svc: MCPServerService,
    ) -> None:
        client, token = await svc.create_client(
            name="X",
            owner_user_id="ghost",
        )
        assert await svc.authenticate(token) is None

    @pytest.mark.asyncio
    async def test_random_string_does_not_match(
        self,
        svc: MCPServerService,
    ) -> None:
        await svc.create_client(name="X", owner_user_id="alice")
        forged = f"{TOKEN_PREFIX}not-the-real-token"
        assert await svc.authenticate(forged) is None


class TestWsHandlers:
    @pytest.mark.asyncio
    async def test_list_refuses_non_admin(
        self,
        svc: MCPServerService,
        alice_ctx: UserContext,
    ) -> None:
        result = await svc._ws_list(_FakeConn(alice_ctx), {"id": 1})
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_list_returns_clients_for_admin(
        self,
        svc: MCPServerService,
        admin_ctx: UserContext,
    ) -> None:
        await svc.create_client(name="A", owner_user_id="alice")
        await svc.create_client(name="B", owner_user_id="alice")
        result = await svc._ws_list(_FakeConn(admin_ctx), {"id": 1})
        assert result is not None
        assert result["type"] == "mcp.clients.list.result"
        assert len(result["clients"]) == 2
        names = {c["name"] for c in result["clients"]}
        assert names == {"A", "B"}
        # Token hash must never surface in serialized output.
        for c in result["clients"]:
            assert "token_hash" not in c

    @pytest.mark.asyncio
    async def test_create_returns_one_shot_token(
        self,
        svc: MCPServerService,
        admin_ctx: UserContext,
    ) -> None:
        result = await svc._ws_create(
            _FakeConn(admin_ctx),
            {
                "id": 1,
                "client": {
                    "name": "Claude Desktop",
                    "owner_user_id": "alice",
                    "ai_profile": "standard",
                },
            },
        )
        assert result is not None
        assert result["type"] == "mcp.clients.create.result"
        assert result["token"].startswith(TOKEN_PREFIX)
        assert result["client"]["name"] == "Claude Desktop"
        # A subsequent ``get`` must NOT return the token.
        get_result = await svc._ws_get(
            _FakeConn(admin_ctx),
            {"id": 2, "client_id": result["client"]["id"]},
        )
        assert get_result is not None
        assert "token" not in get_result
        assert "token_hash" not in get_result["client"]

    @pytest.mark.asyncio
    async def test_create_refuses_non_admin(
        self,
        svc: MCPServerService,
        alice_ctx: UserContext,
    ) -> None:
        result = await svc._ws_create(
            _FakeConn(alice_ctx),
            {
                "id": 1,
                "client": {"name": "X", "owner_user_id": "alice"},
            },
        )
        assert result is not None
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_rotate_refuses_non_admin(
        self,
        svc: MCPServerService,
        alice_ctx: UserContext,
    ) -> None:
        client, _ = await svc.create_client(name="X", owner_user_id="alice")
        result = await svc._ws_rotate(
            _FakeConn(alice_ctx),
            {"id": 1, "client_id": client.id},
        )
        assert result is not None
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_rotate_returns_new_token(
        self,
        svc: MCPServerService,
        admin_ctx: UserContext,
    ) -> None:
        client, _ = await svc.create_client(name="X", owner_user_id="alice")
        result = await svc._ws_rotate(
            _FakeConn(admin_ctx),
            {"id": 1, "client_id": client.id},
        )
        assert result is not None
        assert result["type"] == "mcp.clients.rotate_token.result"
        assert result["token"].startswith(TOKEN_PREFIX)

    @pytest.mark.asyncio
    async def test_update_toggles_active(
        self,
        svc: MCPServerService,
        admin_ctx: UserContext,
    ) -> None:
        client, _ = await svc.create_client(name="X", owner_user_id="alice")
        result = await svc._ws_update(
            _FakeConn(admin_ctx),
            {
                "id": 1,
                "client_id": client.id,
                "client": {"active": False},
            },
        )
        assert result is not None
        assert result["type"] == "mcp.clients.update.result"
        assert result["client"]["active"] is False

    @pytest.mark.asyncio
    async def test_delete_removes_client(
        self,
        svc: MCPServerService,
        admin_ctx: UserContext,
    ) -> None:
        client, _ = await svc.create_client(name="X", owner_user_id="alice")
        result = await svc._ws_delete(
            _FakeConn(admin_ctx),
            {"id": 1, "client_id": client.id},
        )
        assert result is not None
        assert result["type"] == "mcp.clients.delete.result"
        assert await svc.get_client(client.id) is None


class TestSerialization:
    def test_token_hash_never_exposed(self) -> None:
        client = MCPServerClient(
            id="x",
            name="X",
            owner_user_id="alice",
            ai_profile="default",
            token_hash="$argon2id$...",
            token_prefix="mcpc_ab",
        )
        serialized = MCPServerService._serialize_client(client)
        assert "token_hash" not in serialized
        assert serialized["token_prefix"] == "mcpc_ab"
