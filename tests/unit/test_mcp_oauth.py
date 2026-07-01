"""Unit tests for OAuth 2.1 flow support.

Covers:

- ``EntityStorageTokenStorage`` round-tripping ``OAuthToken`` and
  ``OAuthClientInformationFull`` against a fake storage backend.
- ``OAuthFlowManager`` state machine: begin → resolve auth URL →
  callback → settle, plus cancel and unknown-state handling.
- ``MCPService`` treating an OAuth record without stored tokens as
  ``needs_oauth`` rather than attempting a connection.
"""

from __future__ import annotations

import pytest
from mcp.shared.auth import OAuthToken

from gilbert.core.services.mcp import MCPService
from gilbert.core.services.mcp_oauth import (
    MCP_TOKENS_COLLECTION,
    EntityStorageTokenStorage,
    OAuthFlowManager,
)
from gilbert.interfaces.mcp import MCPAuthConfig, MCPServerRecord
from tests.unit.test_mcp_service import (
    FakeACL,
    FakeMCPBackend,
    FakeStorage,
    _install_client,
    admin,  # noqa: F401
    alice,  # noqa: F401
    make_record,
    register_fake_backend,  # noqa: F401 — fixture re-export
)


def _remote_oauth_record(server_id: str = "a") -> MCPServerRecord:
    return MCPServerRecord(
        id=server_id,
        name="Remote",
        slug="remote",
        transport="http",
        url="https://example.com/mcp",
        command=(),
        owner_id="alice",
        scope="private",
        auth=MCPAuthConfig(kind="oauth", oauth_scopes=("read",)),
    )


class TestEntityStorageTokenStorage:
    @pytest.mark.asyncio
    async def test_get_tokens_returns_none_when_empty(self) -> None:
        storage = FakeStorage()
        store = EntityStorageTokenStorage(storage, "srv1")
        assert await store.get_tokens() is None

    @pytest.mark.asyncio
    async def test_set_and_get_tokens_roundtrip(self) -> None:
        storage = FakeStorage()
        store = EntityStorageTokenStorage(storage, "srv1")
        await store.set_tokens(
            OAuthToken(
                access_token="at-123",
                token_type="Bearer",
                expires_in=3600,
                refresh_token="rt-456",
                scope="read",
            ),
        )
        restored = await store.get_tokens()
        assert restored is not None
        assert restored.access_token == "at-123"
        assert restored.refresh_token == "rt-456"
        assert restored.scope == "read"

    @pytest.mark.asyncio
    async def test_corrupted_row_returns_none(self) -> None:
        storage = FakeStorage()
        # Write a deliberately-malformed row directly
        await storage.put(
            MCP_TOKENS_COLLECTION,
            "srv1",
            {"tokens": {"access_token": 123}},
        )
        store = EntityStorageTokenStorage(storage, "srv1")
        assert await store.get_tokens() is None


class TestOAuthFlowManager:
    @pytest.mark.asyncio
    async def test_begin_returns_state_and_provider(self) -> None:
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        record = _remote_oauth_record()
        state, provider = await mgr.begin(record, "https://g/callback")
        assert state
        assert provider is not None
        # A subsequent begin for the same server cancels the first.
        state2, _ = await mgr.begin(record, "https://g/callback")
        assert state2 != state
        # Only the newer state is tracked.
        assert await mgr.complete(state, "code", state) is False
        # Tidy up the second flow so we don't leak futures.
        await mgr.cancel(record.id)

    @pytest.mark.asyncio
    async def test_complete_unknown_state_returns_false(self) -> None:
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        assert await mgr.complete("bogus", "code", "bogus") is False

    @pytest.mark.asyncio
    async def test_complete_resolves_code_future(self) -> None:
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        record = _remote_oauth_record()
        state, _provider = await mgr.begin(record, "https://g/callback")

        # Grab the flow's code_future via the private index so we can
        # await it directly — in production, the SDK's callback_handler
        # does the same thing.
        flow = mgr._by_state[state]  # noqa: SLF001 - test introspection
        code_future = flow.code_future

        ok = await mgr.complete(state, "auth-code-xyz", state)
        assert ok is True
        assert code_future.done()
        code, returned_state = await code_future
        assert code == "auth-code-xyz"
        assert returned_state == state
        await mgr.settle(record.id)

    @pytest.mark.asyncio
    async def test_callback_resolves_by_sdk_state_not_gilbert_state(self) -> None:
        """The MCP SDK generates its OWN ``state`` for the authorization
        URL (see ``mcp.client.auth.oauth2`` — ``state = secrets.token_urlsafe(32)``),
        independent of anything Gilbert invents. The browser callback comes
        back carrying THAT state, so the flow manager must be able to resolve
        the pending flow by the SDK's state. Otherwise every interactive
        sign-in dead-ends at "No flow in progress"."""
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        record = _remote_oauth_record()
        _gilbert_state, provider = await mgr.begin(record, "https://g/callback")

        # Simulate the SDK handing us the authorization URL — it bakes its
        # own state into the query string, never Gilbert's.
        sdk_state = "sdk-generated-state-abc123"
        auth_url = (
            "https://authserver.example/authorize?response_type=code"
            f"&client_id=x&state={sdk_state}&code_challenge=y"
        )
        await provider.context.redirect_handler(auth_url)

        # The auth URL is still surfaced to the UI.
        flow_auth_url = await mgr.auth_url_future(record.id)
        assert flow_auth_url == auth_url

        # The browser callback returns carrying the SDK's state.
        ok = await mgr.complete(sdk_state, "auth-code", sdk_state)
        assert ok is True
        await mgr.settle(record.id)

    @pytest.mark.asyncio
    async def test_cancel_clears_indexes(self) -> None:
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        record = _remote_oauth_record()
        state, _ = await mgr.begin(record, "https://g/callback")
        await mgr.cancel(record.id)
        assert record.id not in mgr._by_server  # noqa: SLF001
        assert state not in mgr._by_state  # noqa: SLF001
        # A post-cancel complete() for the same state is a no-op.
        assert await mgr.complete(state, "code", state) is False

    @pytest.mark.asyncio
    async def test_has_tokens_reflects_storage(self) -> None:
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        assert await mgr.has_tokens("srv1") is False
        await mgr.storage_for("srv1").set_tokens(
            OAuthToken(access_token="at", token_type="Bearer"),
        )
        assert await mgr.has_tokens("srv1") is True

    @pytest.mark.asyncio
    async def test_clear_tokens_wipes_row(self) -> None:
        storage = FakeStorage()
        mgr = OAuthFlowManager(storage)
        await mgr.storage_for("srv1").set_tokens(
            OAuthToken(access_token="at", token_type="Bearer"),
        )
        await mgr.clear_tokens("srv1")
        assert await mgr.has_tokens("srv1") is False


class TestMCPServiceOAuthBranch:
    @pytest.mark.asyncio
    async def test_start_client_marks_needs_oauth_without_tokens(self) -> None:
        """An OAuth record with no stored tokens should be registered
        in ``needs_oauth`` and left disconnected — no connection
        attempt, so the HTTP backend is never touched."""
        service = MCPService()
        service._enabled = True
        service._storage = FakeStorage()
        service._oauth = OAuthFlowManager(service._storage)
        service._acl_svc = FakeACL()

        record = _remote_oauth_record()
        entry = await service._start_client(record)
        assert entry is not None
        # The supervisor runs the OAuth gate on its first iteration
        # and exits immediately. Await it to observe the state.
        assert entry.supervisor is not None
        await entry.supervisor
        assert entry.connected is False
        assert entry.last_error == "OAuth sign-in required"
        assert record.id in service._needs_oauth

    @pytest.mark.asyncio
    async def test_needs_oauth_surfaces_in_serialization(
        self,
        register_fake_backend: type[FakeMCPBackend],  # noqa: F811
        alice,  # noqa: F811
    ) -> None:
        service = MCPService()
        service._enabled = True
        service._storage = FakeStorage()
        service._oauth = OAuthFlowManager(service._storage)
        service._acl_svc = FakeACL()

        # Build a record and install it through the fake backend, then
        # manually mark needs_oauth so the serializer sees it.
        record = make_record(id="a", slug="remote", owner_id="alice")
        _install_client(service, record)
        service._needs_oauth.add(record.id)
        view = service._serialize_record(record, alice)
        assert view["needs_oauth"] is True

    @pytest.mark.asyncio
    async def test_delete_server_clears_tokens_and_pending(
        self,
        register_fake_backend: type[FakeMCPBackend],  # noqa: F811
    ) -> None:
        service = MCPService()
        service._enabled = True
        service._storage = FakeStorage()
        service._oauth = OAuthFlowManager(service._storage)
        service._acl_svc = FakeACL()

        # Pre-seed tokens and a pending flow.
        await service._oauth.storage_for("a").set_tokens(
            OAuthToken(access_token="at", token_type="Bearer"),
        )
        service._needs_oauth.add("a")
        await service.create_server(make_record(id="a", slug="remote", owner_id="alice"))

        await service.delete_server("a")

        assert "a" not in service._clients
        assert "a" not in service._needs_oauth
        assert await service._oauth.has_tokens("a") is False
