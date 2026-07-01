"""OAuth 2.1 flow support for MCP client connections.

Gilbert integrates with the MCP SDK's ``OAuthClientProvider`` (which is
itself an ``httpx.Auth``), feeding it:

- a per-server ``TokenStorage`` backed by Gilbert's entity storage
  (``mcp_server_tokens`` collection, never surfaced in the UI), and
- ``redirect_handler`` / ``callback_handler`` coroutines wired through
  the ``OAuthFlowManager`` in this module, which coordinates the
  two-step browser round-trip: Gilbert hands the user an authorization
  URL, the user signs in at the MCP server's auth portal, the auth
  portal redirects to a Gilbert HTTP endpoint, and this module resolves
  the waiting coroutine with the ``(code, state)`` pair.

Tokens never round-trip through the UI. Regular users can start an
OAuth flow for servers they own, but the tokens themselves are stored
alongside core server state with admin-only visibility through the
generic entity browser.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from gilbert.interfaces.mcp import MCPServerRecord
from gilbert.interfaces.storage import StorageBackend

logger = logging.getLogger(__name__)

MCP_TOKENS_COLLECTION = "mcp_server_tokens"


class EntityStorageTokenStorage(TokenStorage):
    """SDK ``TokenStorage`` backed by Gilbert's entity storage.

    Each MCP server gets one row in ``mcp_server_tokens`` keyed on the
    server id. The row holds both the stored OAuth tokens and the
    dynamically-registered client information from the MCP server's
    authorization server, so subsequent connects can reuse the same
    client registration without re-running discovery.
    """

    def __init__(self, storage: StorageBackend, server_id: str) -> None:
        self._storage = storage
        self._server_id = server_id

    async def _load(self) -> dict[str, Any]:
        doc = await self._storage.get(MCP_TOKENS_COLLECTION, self._server_id)
        return dict(doc) if doc else {}

    async def _save(self, doc: dict[str, Any]) -> None:
        await self._storage.put(MCP_TOKENS_COLLECTION, self._server_id, doc)

    async def get_tokens(self) -> OAuthToken | None:
        doc = await self._load()
        raw = doc.get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:  # noqa: BLE001 - corrupted row shouldn't block the flow
            logger.warning(
                "Discarding corrupted OAuth token row for %s",
                self._server_id,
            )
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        doc = await self._load()
        doc["tokens"] = tokens.model_dump(mode="json")
        await self._save(doc)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        doc = await self._load()
        raw = doc.get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Discarding corrupted OAuth client info for %s",
                self._server_id,
            )
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        doc = await self._load()
        doc["client_info"] = client_info.model_dump(mode="json")
        await self._save(doc)


@dataclass
class _PendingFlow:
    """One in-progress OAuth authorization.

    Holds the futures the flow manager resolves as the user and the SDK
    interact asynchronously. ``auth_url_future`` fires when the SDK
    hands us the authorization URL to show to the user;
    ``code_future`` fires when the callback route receives the
    ``(code, state)`` pair from the browser redirect.
    """

    server_id: str
    state: str
    auth_url_future: asyncio.Future[str]
    code_future: asyncio.Future[tuple[str, str | None]]
    connect_task: asyncio.Task[None] | None = None
    created_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


class OAuthFlowManager:
    """Tracks in-flight OAuth flows and resolves browser callbacks.

    Exactly one flow per server can be active at a time — starting a
    new flow while one is pending cancels the prior one. The manager
    keeps flows in two indexes: by server id (for ``oauth_cancel`` and
    ``oauth_start`` idempotency) and by state parameter (for the
    HTTP callback route to look up without knowing the server id).
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._by_server: dict[str, _PendingFlow] = {}
        self._by_state: dict[str, _PendingFlow] = {}
        self._lock = asyncio.Lock()

    def storage_for(self, server_id: str) -> EntityStorageTokenStorage:
        return EntityStorageTokenStorage(self._storage, server_id)

    async def has_tokens(self, server_id: str) -> bool:
        tokens = await self.storage_for(server_id).get_tokens()
        return tokens is not None

    async def clear_tokens(self, server_id: str) -> None:
        """Wipe stored tokens and client info (e.g. on server delete)."""
        try:
            await self._storage.delete(MCP_TOKENS_COLLECTION, server_id)
        except Exception:  # pragma: no cover
            logger.exception("Failed to clear OAuth tokens for %s", server_id)

    async def begin(
        self,
        record: MCPServerRecord,
        redirect_uri: str,
    ) -> tuple[str, OAuthClientProvider]:
        """Start a new flow for ``record`` and return the authorization URL
        and the provider that will be used for subsequent connects.

        The caller is expected to pass the returned provider into the
        transport's ``auth=`` argument when kicking off the actual
        ``connect()`` — which is what triggers the SDK to call our
        redirect/callback handlers in sequence. The authorization URL
        returned here comes from the redirect handler after the SDK has
        gone through discovery and PKCE setup.
        """
        async with self._lock:
            existing = self._by_server.get(record.id)
            if existing is not None:
                self._cancel_locked(existing)

            state = secrets.token_urlsafe(32)
            loop = asyncio.get_running_loop()
            flow = _PendingFlow(
                server_id=record.id,
                state=state,
                auth_url_future=loop.create_future(),
                code_future=loop.create_future(),
            )
            self._by_server[record.id] = flow
            self._by_state[state] = flow

        provider = OAuthClientProvider(
            server_url=record.url or "",
            client_metadata=_client_metadata_for(record, redirect_uri),
            storage=self.storage_for(record.id),
            redirect_handler=self._make_tracking_redirect_handler(flow),
            callback_handler=_make_callback_handler(flow),
        )
        return state, provider

    def _make_tracking_redirect_handler(self, flow: _PendingFlow) -> Any:
        """Redirect handler used by the interactive ``begin()`` flow.

        The MCP SDK invents its *own* ``state`` for the authorization
        request and bakes it into the URL it hands us here — it never
        uses the ``state`` this manager generated in ``begin()``. The
        browser's callback therefore returns carrying the SDK's state,
        so we re-key the pending flow by that state before surfacing the
        URL; otherwise the callback route can never look the flow up and
        every sign-in dead-ends at "No flow in progress"."""

        async def handler(authorization_url: str) -> None:
            await self._rekey_from_auth_url(flow, authorization_url)
            if not flow.auth_url_future.done():
                flow.auth_url_future.set_result(authorization_url)

        return handler

    async def _rekey_from_auth_url(
        self,
        flow: _PendingFlow,
        authorization_url: str,
    ) -> None:
        """Re-index ``flow`` under the ``state`` embedded in the SDK's
        authorization URL, replacing the placeholder state assigned in
        ``begin()``. No-op if the URL carries no state."""
        params = parse_qs(urlparse(authorization_url).query)
        values = params.get("state")
        sdk_state = values[0] if values else None
        if not sdk_state or sdk_state == flow.state:
            return
        async with self._lock:
            # Only move the index entry if it still points at this flow —
            # a concurrent cancel()/begin() may have already reclaimed it.
            if self._by_state.get(flow.state) is flow:
                self._by_state.pop(flow.state, None)
            flow.state = sdk_state
            self._by_state[sdk_state] = flow

    def auth_url_future(self, server_id: str) -> asyncio.Future[str] | None:
        flow = self._by_server.get(server_id)
        return flow.auth_url_future if flow else None

    async def complete(
        self,
        state: str,
        code: str,
        received_state: str | None,
    ) -> bool:
        """Resolve the pending flow identified by ``state``.

        Returns ``True`` if a matching flow was found and the callback
        future was resolved, ``False`` otherwise. The caller (typically
        the HTTP callback route) uses the return value to decide
        whether to render a success or error page."""
        async with self._lock:
            flow = self._by_state.get(state)
            if flow is None:
                return False
            if not flow.code_future.done():
                flow.code_future.set_result((code, received_state or state))
            return True

    async def cancel(self, server_id: str) -> None:
        async with self._lock:
            flow = self._by_server.get(server_id)
            if flow is not None:
                self._cancel_locked(flow)

    def _cancel_locked(self, flow: _PendingFlow) -> None:
        """Caller must hold ``self._lock``."""
        if not flow.auth_url_future.done():
            flow.auth_url_future.cancel()
        if not flow.code_future.done():
            flow.code_future.cancel()
        task = flow.connect_task
        if task is not None and not task.done():
            task.cancel()
        self._by_server.pop(flow.server_id, None)
        self._by_state.pop(flow.state, None)

    def attach_task(self, server_id: str, task: asyncio.Task[None]) -> None:
        flow = self._by_server.get(server_id)
        if flow is not None:
            flow.connect_task = task

    async def settle(self, server_id: str) -> None:
        """Remove the flow for ``server_id`` after its connect task
        completes (successfully or not). Idempotent."""
        async with self._lock:
            flow = self._by_server.pop(server_id, None)
            if flow is not None:
                self._by_state.pop(flow.state, None)


# ── private helpers ──────────────────────────────────────────────────


def _make_redirect_handler(
    flow: _PendingFlow,
) -> Any:
    async def handler(authorization_url: str) -> None:
        if not flow.auth_url_future.done():
            flow.auth_url_future.set_result(authorization_url)

    return handler


def _make_callback_handler(
    flow: _PendingFlow,
) -> Any:
    async def handler() -> tuple[str, str | None]:
        return await flow.code_future

    return handler


def _client_metadata_for(
    record: MCPServerRecord,
    redirect_uri: str,
) -> OAuthClientMetadata:
    """Build the OAuth client metadata Gilbert registers with the
    authorization server.

    ``redirect_uri`` points at Gilbert's callback route, built off the
    tunnel public URL so the user's browser can actually reach it."""
    scopes = " ".join(record.auth.oauth_scopes) if record.auth.oauth_scopes else None
    return OAuthClientMetadata(
        redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scopes,
        client_name=record.auth.oauth_client_name or "Gilbert",
        client_uri=None,
        logo_uri=None,
        contacts=None,
        tos_uri=None,
        policy_uri=None,
        jwks_uri=None,
        jwks=None,
        software_id=None,
        software_version=None,
    )


def auth_for_stored_tokens(
    storage: StorageBackend,
    record: MCPServerRecord,
    redirect_uri: str,
) -> httpx.Auth:
    """Build an ``httpx.Auth`` for a record that already has tokens
    stored. Used by the normal ``_start_client`` path when the tokens
    were already acquired in a prior flow."""
    flow = _PendingFlow(
        server_id=record.id,
        state="",
        auth_url_future=asyncio.Future(),
        code_future=asyncio.Future(),
    )
    return OAuthClientProvider(
        server_url=record.url or "",
        client_metadata=_client_metadata_for(record, redirect_uri),
        storage=EntityStorageTokenStorage(storage, record.id),
        redirect_handler=_make_redirect_handler(flow),
        callback_handler=_make_callback_handler(flow),
    )
