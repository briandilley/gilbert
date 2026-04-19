"""MCP (Model Context Protocol) client service.

Federates tools from external MCP servers into Gilbert's AI tool pipeline.
Each server is a ``mcp_servers`` entity with scope ``private`` / ``shared``
/ ``public`` plus ``allowed_roles`` / ``allowed_users`` lists. Visibility is
enforced per-user at ``get_tools()`` time so that private servers never
leak into another user's tool list, and ``execute_tool()`` re-checks
visibility before dispatching to prevent direct tool-name access.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import gilbert.integrations.mcp_browser  # noqa: F401
import gilbert.integrations.mcp_http  # noqa: F401

# Side-effect imports: trigger backend registration on MCPBackend._registry
# so the service can look up the backend class by name without importing
# concretes. Each module adds itself to the registry under its
# ``backend_name`` class attribute.
import gilbert.integrations.mcp_stdio  # noqa: F401
from gilbert.core.context import get_current_user
from gilbert.core.services.mcp_oauth import (
    OAuthFlowManager,
    auth_for_stored_tokens,
)
from gilbert.integrations.mcp_browser import BrowserMCPBackend
from gilbert.integrations.mcp_http import _RemoteMCPBackend
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.mcp import (
    MCPAuthConfig,
    MCPAuthKind,
    MCPBackend,
    MCPContentBlock,
    MCPPromptMessage,
    MCPPromptSpec,
    MCPResourceContent,
    MCPResourceSpec,
    MCPServerRecord,
    MCPServerScope,
    MCPToolResult,
    MCPToolSpec,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ws import RpcHandler, WsConnectionBase

logger = logging.getLogger(__name__)

MCP_SERVERS_COLLECTION = "mcp_servers"
TOOL_NAME_PREFIX = "mcp__"
TOOL_NAME_SEP = "__"
# Slug rules: lowercase alnum + hyphen, must start with a letter, no double
# underscores (tool name encoding uses __ as a separator, so `weather__x`
# would ambiguate). Enforced at create/update time.
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class _SamplingBudget:
    """Sliding-window token budget for a single MCP server.

    Tracks ``(monotonic_timestamp, tokens_consumed)`` pairs and
    rejects a request when admitting it would push the in-window
    total past ``max_tokens``. In-memory only — the budget resets on
    every process restart, which is fine for a rate limit but means
    it can't be used as an accounting system. See Part 3.3 notes in
    the design for the rationale.
    """

    def __init__(self, *, max_tokens: int, window_seconds: float) -> None:
        self.max_tokens = max_tokens
        self.window_seconds = window_seconds
        self._events: deque[tuple[float, int]] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def can_admit(self, tokens: int) -> bool:
        now = time.monotonic()
        self._prune(now)
        used = sum(t for _, t in self._events)
        return used + tokens <= self.max_tokens

    def consume(self, tokens: int) -> None:
        now = time.monotonic()
        self._events.append((now, tokens))

    def used(self) -> int:
        self._prune(time.monotonic())
        return sum(t for _, t in self._events)


class _ClientEntry:
    """Per-server runtime state held by MCPService.

    Owns one ``MCPBackend`` instance, caches its tool list with a TTL, and
    serializes concurrent refresh attempts with a lock so a storm of
    ``get_tools()`` calls can't trigger overlapping ``list_tools`` fetches.

    ``supervisor`` is the background task that drives the connect /
    reconnect / health-check loop for this entry. See ``MCPService._supervise``
    for the state machine. ``retry_count`` and ``next_retry_at`` are
    surfaced to the UI so users can see when the next reconnection
    attempt is scheduled.
    """

    def __init__(self, record: MCPServerRecord, backend: MCPBackend) -> None:
        self.record = record
        self.backend = backend
        self.tools: list[MCPToolSpec] = []
        self.tools_fetched_at: float = 0.0
        self.refresh_lock = asyncio.Lock()
        self.connected: bool = False
        self.last_error: str | None = None
        self.supervisor: asyncio.Task[None] | None = None
        self.retry_count: int = 0
        self.next_retry_at: datetime | None = None

    def cache_expired(self) -> bool:
        if self.tools_fetched_at == 0.0:
            return True
        age = time.monotonic() - self.tools_fetched_at
        return age >= float(self.record.tool_cache_ttl_seconds)


class MCPService(Service):
    """MCP client — owns per-server lifecycle and federates tools to the AI."""

    slash_namespace = "mcp"
    tool_provider_name = "mcp"

    def __init__(self) -> None:
        self._enabled = False
        self._connect_timeout: float = 15.0
        self._call_timeout: float = 60.0
        self._reconnect_initial_delay: float = 1.0
        self._reconnect_max_delay: float = 60.0
        self._reconnect_multiplier: float = 2.0
        self._reconnect_jitter: float = 0.25
        self._clients: dict[str, _ClientEntry] = {}
        self._storage: StorageBackend | None = None
        self._acl_svc: Any = None
        self._tunnel_svc: Any = None
        self._resolver: ServiceResolver | None = None
        self._oauth: OAuthFlowManager | None = None
        self._needs_oauth: set[str] = set()
        """Server ids that require an OAuth sign-in before they can
        connect. Populated by ``_start_client`` when it finds an oauth
        record without stored tokens; cleared when the sign-in flow
        completes and the client connects."""
        self._sampling_budgets: dict[str, _SamplingBudget] = {}
        """Per-server sliding-window token budgets for remote
        ``sampling/createMessage`` requests. Keyed by server id;
        rebuilt on each record change so budget parameters stay in
        sync with the stored config. In-memory only — budgets reset
        on process restart."""
        self._session_clients: dict[str, dict[str, _ClientEntry]] = {}
        """Ephemeral per-user MCP clients fed by a browser bridge.

        Keyed by ``user_id → slug → entry``. Populated when a user's
        WebSocket sends ``mcp.bridge.announce`` and torn down when
        the owning connection closes. Never touches entity storage.
        Visibility is strictly private to the owning user — admins
        do not see another user's browser-hosted MCP servers.
        """
        self._session_conn: dict[str, Any] = {}
        """The WS connection that currently owns each user's session.

        If a second tab announces for the same user, the old session
        is torn down immediately (last-tab-wins). Disconnect cleanup
        only fires if the disconnecting connection is still the
        registered owner, so a stale teardown from a previous tab
        can't wipe the active session.
        """

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="mcp",
            capabilities=frozenset({"ai_tools", "mcp", "ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"configuration", "access_control", "tunnel"}),
            toggleable=True,
            toggle_description=(
                "MCP client — federate tools from external Model Context "
                "Protocol servers into Gilbert's AI tool pipeline."
            ),
        )

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        self._acl_svc = resolver.get_capability("access_control")
        self._tunnel_svc = resolver.get_capability("tunnel")

        storage_svc = resolver.get_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("MCPService requires an entity_storage capability")
        self._storage = storage_svc.backend
        self._oauth = OAuthFlowManager(self._storage)
        await self._storage.ensure_index(
            IndexDefinition(collection=MCP_SERVERS_COLLECTION, fields=["owner_id"])
        )
        await self._storage.ensure_index(
            IndexDefinition(collection=MCP_SERVERS_COLLECTION, fields=["slug"], unique=True)
        )

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)

        if not section.get("enabled", True):
            logger.info("MCP service disabled")
            return

        self._enabled = True
        self._connect_timeout = float(section.get("connect_timeout_seconds", 15))
        self._call_timeout = float(section.get("call_timeout_seconds", 60))
        self._reconnect_initial_delay = float(section.get("reconnect_initial_delay_seconds", 1.0))
        self._reconnect_max_delay = float(section.get("reconnect_max_delay_seconds", 60.0))

        await self._load_and_autostart()
        logger.info(
            "MCP service started — %d server(s) loaded, %d connected",
            len(self._clients),
            sum(1 for c in self._clients.values() if c.connected),
        )

    async def stop(self) -> None:
        # Cancel supervisors first; each one is responsible for
        # closing its own backend during cleanup. Then fall back to
        # an explicit close for any entry without a supervisor (stub
        # entries registered for disabled servers).
        errors: list[tuple[str, Exception]] = []
        for entry in list(self._clients.values()):
            if entry.supervisor is not None:
                entry.supervisor.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await entry.supervisor
            else:
                try:
                    await entry.backend.close()
                except Exception as exc:  # pragma: no cover - best-effort
                    errors.append((entry.record.slug, exc))
        for slug, err in errors:
            logger.warning("MCP backend %s close failed: %s", slug, err)
        self._clients.clear()
        # Tear down any remaining browser-hosted sessions. On a clean
        # shutdown these are normally already gone (their owning WS
        # connections closed first), but a hard stop may leave entries
        # behind — close them explicitly so the registry is empty.
        for user_id in list(self._session_clients.keys()):
            self._teardown_session(user_id)

    async def _load_and_autostart(self) -> None:
        assert self._storage is not None
        docs = await self._storage.query(Query(collection=MCP_SERVERS_COLLECTION))
        for doc in docs:
            try:
                record = self._record_from_doc(doc)
            except Exception:
                logger.exception("Skipping malformed mcp_servers doc: %s", doc.get("_id"))
                continue
            if not record.enabled or not record.auto_start:
                # Still track the record so list/update RPCs can see it;
                # just don't spin up a backend.
                self._register_record(record)
                continue
            await self._start_client(record)

    def _register_record(self, record: MCPServerRecord) -> None:
        """Track a record without instantiating a live backend — used for
        disabled / non-auto-start servers so visibility filtering and
        listing RPCs can still see them."""
        existing = self._clients.get(record.id)
        if existing is None:
            # Create a placeholder entry with a stub backend. We never call
            # into it; the entry exists so the record is reachable via
            # ``_clients`` for the listing / visibility helpers.
            backend_cls = MCPBackend.registered_backends().get(record.transport)
            if backend_cls is None:
                logger.warning(
                    "Unknown MCP transport %r for server %s; skipping",
                    record.transport,
                    record.slug,
                )
                return
            self._clients[record.id] = _ClientEntry(record, backend_cls())
        else:
            existing.record = record

    async def _start_client(self, record: MCPServerRecord) -> _ClientEntry | None:
        """Register a server and launch its supervisor task.

        This is idempotent — if a supervisor is already running for
        the record's id, it's cancelled first so a fresh attempt can
        take over with the new record. The supervisor is responsible
        for the full connect / reconnect / health-check state machine;
        this method just wires it up and returns quickly.
        """
        backend_cls = MCPBackend.registered_backends().get(record.transport)
        if backend_cls is None:
            logger.warning(
                "Unknown MCP transport %r for server %s",
                record.transport,
                record.slug,
            )
            return None

        # If a supervisor is already running for this id (e.g. update
        # bounces the client), cancel it before taking over.
        existing = self._clients.get(record.id)
        if existing is not None and existing.supervisor is not None:
            existing.supervisor.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await existing.supervisor

        backend = backend_cls()
        entry = _ClientEntry(record, backend)
        self._clients[record.id] = entry

        async def _invalidate_cache() -> None:
            entry.tools_fetched_at = 0.0

        await backend.set_tools_changed_callback(_invalidate_cache)

        # Install the sampling callback only when the record has
        # opted in. Leaving it unset for disabled records means the
        # SDK rejects any sampling request with ``METHOD_NOT_FOUND``,
        # which is exactly what we want — a remote can't probe for
        # sampling support on servers that haven't granted it.
        if record.allow_sampling:

            async def _sampling_handler(ctx: Any, params: Any) -> Any:
                # Capture ``entry`` so reconnects see the current
                # record (including budget parameter updates).
                return await self._on_sampling_request(entry.record, params)

            await backend.set_sampling_callback(_sampling_handler)

        entry.supervisor = asyncio.create_task(self._supervise(entry))
        return entry

    async def _supervise(self, entry: _ClientEntry) -> None:
        """Connect / reconnect / health-check loop for a single client.

        State transitions:

        1. **Gate on OAuth** — if the record uses OAuth and has no
           stored tokens, mark ``needs_oauth`` and exit. The supervisor
           relaunches once tokens arrive via ``_ws_oauth_start``.
        2. **Connect** — run a single connect attempt with
           ``_connect_timeout``. On success, reset retry state and
           move to the monitor loop.
        3. **Monitor** — sleep for ``tool_cache_ttl_seconds`` and then
           call ``list_tools`` as a combined health check +
           cache refresh. On failure, log, close the backend, and
           drop back to step 2.
        4. **Backoff** — on connect failure, sleep for the current
           backoff (exponential, jittered, capped at
           ``reconnect_max_delay``), increment ``retry_count``, and
           loop back to step 2.

        Exits cleanly on ``CancelledError`` (caller called
        ``_stop_client``) or on OAuth gate trips (flow will relaunch
        the supervisor). The backend is closed on every exit path so
        transport resources don't leak."""
        record = entry.record
        backoff = self._reconnect_initial_delay
        try:
            while True:
                # --- OAuth gate ---
                if record.auth.kind == "oauth":
                    assert self._oauth is not None
                    if not await self._oauth.has_tokens(record.id):
                        self._needs_oauth.add(record.id)
                        entry.connected = False
                        entry.last_error = "OAuth sign-in required"
                        entry.retry_count = 0
                        entry.next_retry_at = None
                        return
                    self._needs_oauth.discard(record.id)

                # --- Connect attempt ---
                try:
                    await self._attempt_connect(entry)
                except asyncio.CancelledError:
                    raise
                except (Exception, BaseExceptionGroup) as exc:
                    entry.connected = False
                    entry.last_error = str(exc)
                    entry.retry_count += 1
                    # Jittered backoff so parallel supervisors don't stampede
                    # a recovering remote all at the same moment.
                    jitter = random.uniform(
                        -self._reconnect_jitter,
                        self._reconnect_jitter,
                    )
                    delay = max(0.1, backoff * (1.0 + jitter))
                    entry.next_retry_at = datetime.now(UTC) + timedelta(
                        seconds=delay,
                    )
                    logger.info(
                        "MCP %s connect attempt %d failed: %s (next retry in %.1fs)",
                        record.slug,
                        entry.retry_count,
                        exc,
                        delay,
                    )
                    await self._persist_connection_state(
                        record.id,
                        last_error=str(exc),
                        connected=False,
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
                    backoff = min(
                        backoff * self._reconnect_multiplier,
                        self._reconnect_max_delay,
                    )
                    continue

                # --- Monitor loop (connected) ---
                backoff = self._reconnect_initial_delay
                entry.retry_count = 0
                entry.next_retry_at = None
                entry.connected = True
                entry.last_error = None
                await self._persist_connection_state(
                    record.id,
                    last_error=None,
                    connected=True,
                )

                try:
                    await self._monitor(entry)
                except asyncio.CancelledError:
                    raise
                except (Exception, BaseExceptionGroup) as exc:
                    # Monitor caught a transport failure. Close the
                    # backend and fall through to reconnect.
                    logger.info(
                        "MCP %s health check failed, reconnecting: %s",
                        record.slug,
                        exc,
                    )
                    entry.connected = False
                    entry.last_error = f"connection lost: {exc}"
                    with contextlib.suppress(Exception):
                        await entry.backend.close()
                    # Fresh backend instance for the next attempt.
                    backend_cls = MCPBackend.registered_backends().get(
                        record.transport,
                    )
                    if backend_cls is None:  # pragma: no cover
                        return
                    entry.backend = backend_cls()
                    await entry.backend.set_tools_changed_callback(
                        self._make_invalidate_cache(entry),
                    )
        except asyncio.CancelledError:
            entry.connected = False
            entry.next_retry_at = None
            with contextlib.suppress(Exception):
                await entry.backend.close()
            raise

    def _make_invalidate_cache(
        self,
        entry: _ClientEntry,
    ) -> Callable[[], Awaitable[None]]:
        async def _cb() -> None:
            entry.tools_fetched_at = 0.0

        return _cb

    async def _attempt_connect(self, entry: _ClientEntry) -> None:
        """One connect attempt, under ``_connect_timeout``. Raises on
        failure for the supervisor to classify."""
        record = entry.record
        backend = entry.backend
        httpx_auth = (
            self._build_httpx_auth(record) if isinstance(backend, _RemoteMCPBackend) else None
        )
        async with asyncio.timeout(self._connect_timeout):
            if isinstance(backend, _RemoteMCPBackend) and httpx_auth is not None:
                await backend.connect_with_auth(record, auth=httpx_auth)
            else:
                await backend.connect(record)
        # Prime the tool cache on a successful connect so the first AI
        # turn doesn't pay the round-trip.
        await self._refresh_tools(entry)

    async def _monitor(self, entry: _ClientEntry) -> None:
        """Sleep then health-check in a loop until the connection
        breaks. Raises on health-check failure so the supervisor can
        reconnect."""
        while True:
            # Use the per-server TTL as the health-check interval so
            # there's a single knob to turn for both "how fresh is my
            # tool list" and "how quickly do I notice a dropped
            # connection".
            interval = max(1.0, float(entry.record.tool_cache_ttl_seconds))
            await asyncio.sleep(interval)
            # Refreshing tools doubles as a health check: any transport
            # error bubbles out of list_tools and triggers a reconnect.
            await entry.backend.list_tools()
            entry.tools_fetched_at = time.monotonic()

    def _build_httpx_auth(self, record: MCPServerRecord) -> Any:
        """Build the per-request ``httpx.Auth`` for a remote record.

        Returns ``None`` for anything other than ``oauth`` — bearer
        auth goes through the initial header dict in
        ``_auth_headers``, so the transport doesn't need a per-request
        hook, and ``none`` never needs one."""
        if record.auth.kind != "oauth":
            return None
        assert self._storage is not None
        return auth_for_stored_tokens(
            self._storage,
            record,
            self._oauth_redirect_uri(),
        )

    def _oauth_redirect_uri(self) -> str:
        """Build the callback URL OAuth servers redirect back to.

        Uses the tunnel's public URL when configured (so the redirect
        is reachable from the user's browser even when Gilbert is
        behind NAT). Falls back to ``http://localhost:<port>`` when
        there's no tunnel — fine for local development, but that host
        must actually be reachable from the authorizing browser."""
        tunnel = self._tunnel_svc
        if tunnel is not None:
            public_url_for = getattr(tunnel, "public_url_for", None)
            if callable(public_url_for):
                url = public_url_for("/api/mcp/oauth/callback")
                if url:
                    return str(url)
        return "http://localhost:8000/api/mcp/oauth/callback"

    async def _stop_client(self, server_id: str) -> None:
        entry = self._clients.pop(server_id, None)
        if entry is None:
            return
        # Cancel the supervisor first so its cleanup handler closes
        # the backend under its own control. Calling close() first can
        # race with an in-flight connect attempt and leak transport
        # resources on top of whatever the supervisor is already
        # managing.
        if entry.supervisor is not None:
            entry.supervisor.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await entry.supervisor
        else:
            try:
                await entry.backend.close()
            except Exception:  # pragma: no cover
                logger.exception("Error closing MCP backend %s", entry.record.slug)

    async def _refresh_tools(self, entry: _ClientEntry) -> None:
        async with entry.refresh_lock:
            try:
                specs = await entry.backend.list_tools()
            except Exception as exc:
                entry.last_error = str(exc)
                logger.warning("MCP list_tools failed for %s: %s", entry.record.slug, exc)
                return
            entry.tools = specs
            entry.tools_fetched_at = time.monotonic()

    async def _persist_connection_state(
        self,
        server_id: str,
        *,
        last_error: str | None,
        connected: bool,
    ) -> None:
        assert self._storage is not None
        doc = await self._storage.get(MCP_SERVERS_COLLECTION, server_id)
        if doc is None:
            return
        doc["last_error"] = last_error
        if connected:
            doc["last_connected_at"] = datetime.now(UTC).isoformat()
        await self._storage.put(MCP_SERVERS_COLLECTION, server_id, doc)

    # ── Configurable ──────────────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "mcp"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the MCP client subsystem.",
                default=True,
                restart_required=True,
            ),
            ConfigParam(
                key="connect_timeout_seconds",
                type=ToolParameterType.INTEGER,
                description="Seconds to wait for an MCP server handshake before giving up.",
                default=15,
            ),
            ConfigParam(
                key="call_timeout_seconds",
                type=ToolParameterType.INTEGER,
                description="Seconds to wait for a single tool call to complete.",
                default=60,
            ),
            ConfigParam(
                key="reconnect_initial_delay_seconds",
                type=ToolParameterType.NUMBER,
                description=(
                    "Seconds to wait after a failed connect before retrying. "
                    "Doubles each subsequent failure up to the max delay."
                ),
                default=1.0,
            ),
            ConfigParam(
                key="reconnect_max_delay_seconds",
                type=ToolParameterType.NUMBER,
                description="Cap on the exponential reconnect delay.",
                default=60.0,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._connect_timeout = float(config.get("connect_timeout_seconds", 15))
        self._call_timeout = float(config.get("call_timeout_seconds", 60))
        self._reconnect_initial_delay = float(config.get("reconnect_initial_delay_seconds", 1.0))
        self._reconnect_max_delay = float(config.get("reconnect_max_delay_seconds", 60.0))

    # ── visibility / RBAC ─────────────────────────────────────────────

    def _is_admin(self, user_ctx: UserContext) -> bool:
        acl = self._acl_svc
        if acl is None or not isinstance(acl, AccessControlProvider):
            return "admin" in user_ctx.roles
        return bool(acl.get_effective_level(user_ctx) <= acl.get_role_level("admin"))

    def _can_see_server(self, record: MCPServerRecord, user_ctx: UserContext) -> bool:
        if user_ctx.user_id == record.owner_id:
            return True
        if self._is_admin(user_ctx):
            return True
        if record.scope == "public":
            return True
        if record.scope == "shared":
            if user_ctx.user_id in record.allowed_users:
                return True
            if set(user_ctx.roles) & set(record.allowed_roles):
                return True
        return False

    def _visible_clients(self, user_ctx: UserContext) -> list[_ClientEntry]:
        out = [
            entry
            for entry in self._clients.values()
            if entry.connected and self._can_see_server(entry.record, user_ctx)
        ]
        # Browser-hosted session entries are strictly private to the
        # owning user — never shown to anyone else, not even admins.
        session = self._session_clients.get(user_ctx.user_id) or {}
        out.extend(entry for entry in session.values() if entry.connected)
        return out

    # ── ToolProvider ──────────────────────────────────────────────────

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        if user_ctx is None:
            # MCP tools are always per-user; a call with no context sees none.
            # Callers that legitimately want "all tools" (e.g. slash-command
            # uniqueness tests) iterate ``_clients`` directly.
            return []

        out: list[ToolDefinition] = []
        stale: list[_ClientEntry] = []
        for entry in self._visible_clients(user_ctx):
            if entry.cache_expired():
                stale.append(entry)
            for spec in entry.tools:
                out.append(self._to_tool_definition(entry.record, spec))

        # Kick off a background refresh for any stale cache entries without
        # blocking the current call. The next get_tools() sees the refreshed
        # set; tools/list_changed notifications provide the fast path. If
        # there's no running loop (caller is synchronous), just leave the
        # cache stale — some other call from an async context will pick it
        # up, or the notification path will invalidate it first.
        if stale:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                for entry in stale:
                    loop.create_task(self._refresh_tools(entry))

        return out

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        slug, tool_name = self._decode_tool_name(name)
        entry = self._find_client_for(slug, user_ctx)
        if entry is None:
            raise PermissionError(f"MCP tool {name!r} is not available to this user")
        if not entry.connected:
            raise RuntimeError(
                f"MCP server {slug!r} is not connected: {entry.last_error or 'unknown error'}"
            )

        async with asyncio.timeout(self._call_timeout):
            result = await entry.backend.call_tool(tool_name, arguments)
        return self._format_result(result)

    # ── encoding helpers ──────────────────────────────────────────────

    @staticmethod
    def _encode_tool_name(slug: str, tool_name: str) -> str:
        return f"{TOOL_NAME_PREFIX}{slug}{TOOL_NAME_SEP}{tool_name}"

    @staticmethod
    def _decode_tool_name(name: str) -> tuple[str, str]:
        if not name.startswith(TOOL_NAME_PREFIX):
            raise KeyError(f"Not an MCP tool name: {name!r}")
        remainder = name[len(TOOL_NAME_PREFIX) :]
        slug, sep, tool = remainder.partition(TOOL_NAME_SEP)
        if not sep or not tool:
            raise KeyError(f"Malformed MCP tool name: {name!r}")
        return slug, tool

    def _find_client_for(self, slug: str, user_ctx: UserContext) -> _ClientEntry | None:
        # Session entries are checked first: they're user-private, so a
        # hit here belongs to the caller by construction. The announce
        # handler also rejects slugs that would collide with a visible
        # persisted server, so there's no ambiguity to resolve.
        session = self._session_clients.get(user_ctx.user_id) or {}
        entry = session.get(slug)
        if entry is not None:
            return entry
        for entry in self._clients.values():
            if entry.record.slug != slug:
                continue
            if not self._can_see_server(entry.record, user_ctx):
                return None
            return entry
        return None

    def _to_tool_definition(
        self,
        record: MCPServerRecord,
        spec: MCPToolSpec,
    ) -> ToolDefinition:
        params = self._translate_schema(spec.input_schema)
        # ``required_role="everyone"`` because visibility (``_can_see_server``)
        # is the sole access gate for MCP tools — if the caller can see the
        # server, they can use its tools. The downstream RBAC filter in
        # AIService still runs, but the ``everyone`` floor makes it a no-op
        # for MCP tools (admin/user/everyone all satisfy level ≤ 200).
        return ToolDefinition(
            name=self._encode_tool_name(record.slug, spec.name),
            description=spec.description or f"{record.name}: {spec.name}",
            parameters=params,
            required_role="everyone",
            slash_group=record.slug,
            slash_command=spec.name,
            slash_help=spec.description or f"{record.name}: {spec.name}",
        )

    @staticmethod
    def _translate_schema(schema: dict[str, Any]) -> list[ToolParameter]:
        """Translate a tool's JSON Schema into flat ToolParameter list.

        Handles the common ``{"type": "object", "properties": {...},
        "required": [...]}`` shape. Nested objects and arrays are kept as
        their outer type with the raw sub-schema passed through — the AI
        provider still sees the full schema via ``to_json_schema()`` when
        Gilbert adapts back to JSON, but only the top level is enumerated
        as ``ToolParameter``s."""
        if not isinstance(schema, dict):
            return []
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        out: list[ToolParameter] = []
        for pname, pschema in properties.items():
            if not isinstance(pschema, dict):
                continue
            ptype = _json_schema_type(pschema.get("type"))
            out.append(
                ToolParameter(
                    name=pname,
                    type=ptype,
                    description=str(pschema.get("description") or ""),
                    required=pname in required,
                    enum=list(pschema["enum"]) if isinstance(pschema.get("enum"), list) else None,
                    default=pschema.get("default"),
                )
            )
        return out

    @staticmethod
    def _format_result(result: MCPToolResult) -> str:
        """Flatten MCP content blocks into a single string for the AI.

        Part 1 surfaces text blocks directly; non-text blocks are
        announced in brackets so the AI knows they exist. Later parts
        will deliver them as richer multimodal content."""
        parts: list[str] = []
        for block in result.content:
            parts.append(_render_block(block))
        rendered = "\n".join(p for p in parts if p)
        if result.is_error:
            return f"[error] {rendered}" if rendered else "[error]"
        return rendered

    # ── CRUD (called by RPC handlers in Part 1.6) ─────────────────────

    async def list_servers_for(self, user_ctx: UserContext) -> list[MCPServerRecord]:
        return [
            entry.record
            for entry in self._clients.values()
            if self._can_see_server(entry.record, user_ctx)
        ]

    def get_server(self, server_id: str) -> MCPServerRecord | None:
        entry = self._clients.get(server_id)
        return entry.record if entry else None

    async def assert_slug_unique(
        self,
        slug: str,
        *,
        excluding_id: str | None = None,
    ) -> None:
        """Enforce global slug uniqueness at save time."""
        assert self._storage is not None
        docs = await self._storage.query(
            Query(
                collection=MCP_SERVERS_COLLECTION,
                filters=[Filter(field="slug", op=FilterOp.EQ, value=slug)],
                limit=2,
            )
        )
        for doc in docs:
            if excluding_id is not None and doc.get("_id") == excluding_id:
                continue
            owner = doc.get("owner_id", "<unknown>")
            scope = doc.get("scope", "<unknown>")
            raise ValueError(
                f"MCP server slug {slug!r} is already in use "
                f"(owner={owner}, scope={scope}). Choose a different name."
            )

    async def create_server(self, record: MCPServerRecord) -> MCPServerRecord:
        assert self._storage is not None
        self._validate_record(record)
        if not record.id:
            record = replace(record, id=str(uuid.uuid4()))
        now = datetime.now(UTC)
        record = replace(record, created_at=now, updated_at=now)
        await self.assert_slug_unique(record.slug)
        await self._storage.put(MCP_SERVERS_COLLECTION, record.id, self._doc_from_record(record))
        if record.enabled and record.auto_start:
            await self._start_client(record)
        else:
            self._register_record(record)
        return record

    async def update_server(self, record: MCPServerRecord) -> MCPServerRecord:
        assert self._storage is not None
        self._validate_record(record)
        await self.assert_slug_unique(record.slug, excluding_id=record.id)
        now = datetime.now(UTC)
        record = replace(record, updated_at=now)
        await self._storage.put(MCP_SERVERS_COLLECTION, record.id, self._doc_from_record(record))
        # Drop the cached sampling budget so new budget parameters
        # (or a full disable) take effect on the next request
        # instead of keeping the old one around with stale limits.
        self._sampling_budgets.pop(record.id, None)
        # Bounce the client so the new command/env/etc take effect.
        await self._stop_client(record.id)
        if record.enabled and record.auto_start:
            await self._start_client(record)
        else:
            self._register_record(record)
        return record

    async def delete_server(self, server_id: str) -> None:
        assert self._storage is not None
        await self._stop_client(server_id)
        if self._oauth is not None:
            await self._oauth.cancel(server_id)
            await self._oauth.clear_tokens(server_id)
        self._needs_oauth.discard(server_id)
        self._sampling_budgets.pop(server_id, None)
        await self._storage.delete(MCP_SERVERS_COLLECTION, server_id)

    async def test_server(self, record: MCPServerRecord) -> list[MCPToolSpec]:
        """One-shot connect → list_tools → disconnect. Never touches
        persistent state — used by the UI to validate a draft config."""
        backend_cls = MCPBackend.registered_backends().get(record.transport)
        if backend_cls is None:
            raise ValueError(f"Unknown MCP transport: {record.transport}")
        backend = backend_cls()
        try:
            async with asyncio.timeout(self._connect_timeout):
                await backend.connect(record)
            return await backend.list_tools()
        finally:
            try:
                await backend.close()
            except Exception:  # pragma: no cover
                pass

    # ── WS RPC handlers ───────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, RpcHandler]:
        """Expose server CRUD + lifecycle operations over WebSocket.

        Frame type namespace: ``mcp.servers.*``. All frames come in at the
        default ``user`` permission level (declared in ``interfaces/acl.py``);
        handlers upgrade to admin-only when the payload asks for a shared
        or public scope, or when an edit changes the visibility fields of
        an existing record. Ownership is always enforced per-handler on
        top of the frame-type level.
        """
        return {
            "mcp.servers.list": self._ws_list,
            "mcp.servers.get": self._ws_get,
            "mcp.servers.create": self._ws_create,
            "mcp.servers.update": self._ws_update,
            "mcp.servers.delete": self._ws_delete,
            "mcp.servers.start": self._ws_start,
            "mcp.servers.stop": self._ws_stop,
            "mcp.servers.test": self._ws_test,
            "mcp.servers.tools": self._ws_tools,
            "mcp.servers.oauth_start": self._ws_oauth_start,
            "mcp.servers.oauth_cancel": self._ws_oauth_cancel,
            "mcp.servers.resources.list": self._ws_resources_list,
            "mcp.servers.resources.read": self._ws_resources_read,
            "mcp.servers.prompts.list": self._ws_prompts_list,
            "mcp.servers.prompts.get": self._ws_prompts_get,
            "mcp.bridge.announce": self._ws_bridge_announce,
        }

    # --- handlers ----------------------------------------------------

    async def _ws_list(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        user_ctx = conn.user_ctx
        servers = [
            entry.record
            for entry in self._clients.values()
            if self._can_see_server(entry.record, user_ctx)
        ]
        return {
            "type": "mcp.servers.list.result",
            "ref": frame.get("id"),
            "servers": [self._serialize_record(r, user_ctx) for r in servers],
        }

    async def _ws_get(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        server_id = str(frame.get("id_") or frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None or not self._can_see_server(entry.record, conn.user_ctx):
            return _ws_error(frame, "Server not found", code=404)
        return {
            "type": "mcp.servers.get.result",
            "ref": frame.get("id"),
            "server": self._serialize_record(entry.record, conn.user_ctx),
        }

    async def _ws_create(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        payload = frame.get("server") or {}
        if not isinstance(payload, dict):
            return _ws_error(frame, "Missing or invalid 'server' payload")

        scope = str(payload.get("scope") or "private")
        if scope != "private" and not self._is_admin(conn.user_ctx):
            return _ws_error(
                frame,
                "Only admins can create shared or public MCP servers",
                code=403,
            )
        if bool(payload.get("allow_sampling", False)) and not self._is_admin(
            conn.user_ctx,
        ):
            return _ws_error(
                frame,
                "Only admins can enable sampling on MCP servers",
                code=403,
            )

        # Owner is always the creator. Even admins creating a shared server
        # for someone else become the owner — transfer isn't a Part 1 feature.
        record = self._record_from_payload(payload, owner_id=conn.user_ctx.user_id)
        try:
            created = await self.create_server(record)
        except ValueError as exc:
            return _ws_error(frame, str(exc))
        return {
            "type": "mcp.servers.create.result",
            "ref": frame.get("id"),
            "server": self._serialize_record(created, conn.user_ctx),
        }

    async def _ws_update(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        payload = frame.get("server") or {}
        if not isinstance(payload, dict):
            return _ws_error(frame, "Missing or invalid 'server' payload")
        server_id = str(payload.get("id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server.id'")

        existing = self._clients.get(server_id)
        if existing is None:
            return _ws_error(frame, "Server not found", code=404)

        old = existing.record
        if not self._can_edit_record(old, conn.user_ctx):
            return _ws_error(frame, "You cannot edit this MCP server", code=403)

        # Scope + visibility changes are admin-only, even for the owner.
        visibility_changing = (
            payload.get("scope", old.scope) != old.scope
            or _as_tuple(payload.get("allowed_roles", old.allowed_roles)) != old.allowed_roles
            or _as_tuple(payload.get("allowed_users", old.allowed_users)) != old.allowed_users
        )
        if visibility_changing and not self._is_admin(conn.user_ctx):
            return _ws_error(
                frame,
                "Only admins can change scope, allowed_roles, or allowed_users",
                code=403,
            )

        # Sampling changes are admin-only too: toggling ``allow_sampling``,
        # switching the profile, or changing the budget all control a
        # server's ability to spend AI budget on the owner's behalf.
        # Regular users can't raise these fields on their own servers.
        sampling_changing = (
            bool(payload.get("allow_sampling", old.allow_sampling)) != old.allow_sampling
            or str(payload.get("sampling_profile", old.sampling_profile)) != old.sampling_profile
            or int(payload.get("sampling_budget_tokens", old.sampling_budget_tokens))
            != old.sampling_budget_tokens
            or int(
                payload.get(
                    "sampling_budget_window_seconds",
                    old.sampling_budget_window_seconds,
                )
            )
            != old.sampling_budget_window_seconds
        )
        if sampling_changing and not self._is_admin(conn.user_ctx):
            return _ws_error(
                frame,
                "Only admins can change sampling settings",
                code=403,
            )

        # Preserve existing env values when the incoming payload sends masked placeholders.
        new_env = _merge_env(old.env, payload.get("env") or {})
        # Same rule for the bearer token so a non-owner view of the form
        # can be submitted round-trip without leaking the real token.
        new_auth = _merge_auth(old.auth, payload.get("auth") or {})

        record = self._record_from_payload(
            payload,
            owner_id=old.owner_id,  # owner is immutable in Part 1
            fallback_id=server_id,
            env_override=new_env,
            auth_override=new_auth,
            created_at_override=old.created_at,
        )
        try:
            updated = await self.update_server(record)
        except ValueError as exc:
            return _ws_error(frame, str(exc))
        return {
            "type": "mcp.servers.update.result",
            "ref": frame.get("id"),
            "server": self._serialize_record(updated, conn.user_ctx),
        }

    async def _ws_delete(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None:
            return _ws_error(frame, "Server not found", code=404)
        if not self._can_edit_record(entry.record, conn.user_ctx):
            return _ws_error(frame, "You cannot delete this MCP server", code=403)
        await self.delete_server(server_id)
        return {
            "type": "mcp.servers.delete.result",
            "ref": frame.get("id"),
            "server_id": server_id,
        }

    async def _ws_start(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None:
            return _ws_error(frame, "Server not found", code=404)
        if not self._can_edit_record(entry.record, conn.user_ctx):
            return _ws_error(frame, "You cannot control this MCP server", code=403)
        # Re-launching stops any existing supervisor and starts a
        # fresh one — same pattern used by ``_ws_update`` after a
        # record change.
        await self._start_client(entry.record)

        # Give the supervisor up to 3 seconds to finish its first
        # connect attempt so the response reflects a useful state.
        # The UI's refetch interval picks up later transitions.
        new_entry = self._clients.get(server_id)
        if new_entry is not None:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if new_entry.connected or new_entry.retry_count > 0:
                    break
                await asyncio.sleep(0.05)

        return {
            "type": "mcp.servers.start.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "connected": new_entry.connected if new_entry else False,
            "last_error": new_entry.last_error if new_entry else None,
        }

    async def _ws_stop(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None:
            return _ws_error(frame, "Server not found", code=404)
        if not self._can_edit_record(entry.record, conn.user_ctx):
            return _ws_error(frame, "You cannot control this MCP server", code=403)
        # Mark the record disabled so the stopped state survives restart.
        record = replace(entry.record, enabled=False)
        assert self._storage is not None
        await self._storage.put(
            MCP_SERVERS_COLLECTION,
            record.id,
            self._doc_from_record(record),
        )
        await self._stop_client(server_id)
        self._register_record(record)
        return {
            "type": "mcp.servers.stop.result",
            "ref": frame.get("id"),
            "server_id": server_id,
        }

    async def _ws_test(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Dry-run connect → list_tools → disconnect.

        Non-admins can only test drafts with ``scope=private``. Admins can
        test any draft. This mirrors the create-scope gate so users can't
        use ``test`` as a back door to invoke arbitrary commands as admin."""
        payload = frame.get("server") or {}
        if not isinstance(payload, dict):
            return _ws_error(frame, "Missing or invalid 'server' payload")
        scope = str(payload.get("scope") or "private")
        if scope != "private" and not self._is_admin(conn.user_ctx):
            return _ws_error(
                frame,
                "Only admins can test shared or public MCP servers",
                code=403,
            )
        try:
            record = self._record_from_payload(payload, owner_id=conn.user_ctx.user_id)
            self._validate_record(record)
            specs = await self.test_server(record)
        except ValueError as exc:
            return _ws_error(frame, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface connection errors to the UI
            return _ws_error(frame, f"Test failed: {exc}")
        return {
            "type": "mcp.servers.test.result",
            "ref": frame.get("id"),
            "tools": [
                {
                    "name": s.name,
                    "description": s.description,
                    "input_schema": s.input_schema,
                }
                for s in specs
            ],
        }

    async def _ws_tools(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None or not self._can_see_server(entry.record, conn.user_ctx):
            return _ws_error(frame, "Server not found", code=404)
        return {
            "type": "mcp.servers.tools.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "connected": entry.connected,
            "last_error": entry.last_error,
            "tools": [
                {
                    "name": s.name,
                    "description": s.description,
                    "input_schema": s.input_schema,
                }
                for s in entry.tools
            ],
        }

    async def _ws_oauth_start(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Kick off an OAuth 2.1 flow for a remote MCP server.

        Returns the authorization URL the UI should open in a new tab.
        The flow runs to completion asynchronously in a background
        task: the SDK blocks on the ``callback_handler`` future until
        the user's browser hits the callback route and the flow
        manager resolves it with ``(code, state)``."""
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None:
            return _ws_error(frame, "Server not found", code=404)
        if not self._can_edit_record(entry.record, conn.user_ctx):
            return _ws_error(
                frame,
                "You cannot authenticate this MCP server",
                code=403,
            )
        record = entry.record
        if record.auth.kind != "oauth":
            return _ws_error(
                frame,
                "This MCP server is not configured for OAuth",
            )
        if record.transport not in ("http", "sse"):
            return _ws_error(frame, "OAuth is only supported for remote transports")
        assert self._oauth is not None

        state, provider = await self._oauth.begin(
            record,
            redirect_uri=self._oauth_redirect_uri(),
        )

        async def _run() -> None:
            # Drive a throwaway connect solely to push the SDK through
            # its OAuth flow — ``OAuthClientProvider`` calls our
            # ``redirect_handler`` (resolving ``auth_url_future``) and
            # then our ``callback_handler`` (awaiting ``code_future``)
            # as the user interacts with their browser. When connect
            # returns, tokens are guaranteed to be in storage. We then
            # close this backend and hand control back to the
            # supervisor by re-running ``_start_client`` — the new
            # supervisor's OAuth gate will find tokens and connect
            # normally, which is also the path future reconnects will
            # take.
            backend_cls = MCPBackend.registered_backends().get(record.transport)
            if backend_cls is None:
                return
            backend = backend_cls()
            try:
                await backend.connect_with_auth(record, auth=provider)  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await backend.close()
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MCP OAuth connect failed for %s: %s",
                    record.slug,
                    exc,
                )
                existing = self._clients.get(record.id)
                if existing is not None:
                    existing.connected = False
                    existing.last_error = str(exc)
                await self._persist_connection_state(
                    record.id,
                    last_error=str(exc),
                    connected=False,
                )
                with contextlib.suppress(Exception):
                    await backend.close()
                return
            finally:
                assert self._oauth is not None
                await self._oauth.settle(record.id)

            # OAuth connect succeeded — tokens are stored. Tear down
            # the throwaway backend and relaunch the supervisor so the
            # normal supervised loop owns the live connection from
            # here on out.
            with contextlib.suppress(Exception):
                await backend.close()
            self._needs_oauth.discard(record.id)
            await self._start_client(record)

        task = asyncio.create_task(_run())
        self._oauth.attach_task(record.id, task)

        # Wait (briefly) for the SDK to hand us the authorization URL.
        # The SDK does discovery + client registration before calling
        # the redirect handler, so this can take a second or two for a
        # cold server.
        auth_url_future = self._oauth.auth_url_future(record.id)
        if auth_url_future is None:
            return _ws_error(frame, "Flow disappeared before starting")
        try:
            async with asyncio.timeout(30):
                auth_url = await auth_url_future
        except (TimeoutError, asyncio.CancelledError) as exc:
            await self._oauth.cancel(record.id)
            return _ws_error(frame, f"Failed to begin OAuth flow: {exc}")
        return {
            "type": "mcp.servers.oauth_start.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "authorization_url": auth_url,
            "state": state,
        }

    async def _ws_oauth_cancel(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None:
            return _ws_error(frame, "Server not found", code=404)
        if not self._can_edit_record(entry.record, conn.user_ctx):
            return _ws_error(frame, "You cannot cancel this flow", code=403)
        assert self._oauth is not None
        await self._oauth.cancel(server_id)
        return {
            "type": "mcp.servers.oauth_cancel.result",
            "ref": frame.get("id"),
            "server_id": server_id,
        }

    async def _ws_resources_list(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """List resources advertised by a connected MCP server.

        Access is gated by visibility — any user who can see the
        server can list its resources. Unlike tool execution,
        resources aren't auto-injected into AI context (yet), so the
        gate here is simply "does the caller know this server
        exists"."""
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None or not self._can_see_server(entry.record, conn.user_ctx):
            return _ws_error(frame, "Server not found", code=404)
        if not entry.connected:
            return _ws_error(
                frame,
                f"Server not connected: {entry.last_error or 'unknown error'}",
                code=503,
            )
        try:
            async with asyncio.timeout(self._call_timeout):
                specs = await entry.backend.list_resources()
        except NotImplementedError:
            return _ws_error(
                frame,
                "This MCP server does not support resources",
                code=501,
            )
        except Exception as exc:  # noqa: BLE001
            return _ws_error(frame, f"list_resources failed: {exc}")
        return {
            "type": "mcp.servers.resources.list.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "resources": [self._serialize_resource(r) for r in specs],
        }

    async def _ws_resources_read(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read a single resource by URI.

        Visibility-gated like ``list``. The caller passes the URI
        returned by the prior ``list`` call; no sanitization here —
        the MCP server is responsible for validating its own URIs
        (it advertises what it's willing to serve)."""
        server_id = str(frame.get("server_id") or "").strip()
        uri = str(frame.get("uri") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        if not uri:
            return _ws_error(frame, "Missing 'uri'")
        entry = self._clients.get(server_id)
        if entry is None or not self._can_see_server(entry.record, conn.user_ctx):
            return _ws_error(frame, "Server not found", code=404)
        if not entry.connected:
            return _ws_error(
                frame,
                f"Server not connected: {entry.last_error or 'unknown error'}",
                code=503,
            )
        try:
            async with asyncio.timeout(self._call_timeout):
                contents = await entry.backend.read_resource(uri)
        except NotImplementedError:
            return _ws_error(
                frame,
                "This MCP server does not support resources",
                code=501,
            )
        except Exception as exc:  # noqa: BLE001
            return _ws_error(frame, f"read_resource failed: {exc}")
        return {
            "type": "mcp.servers.resources.read.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "uri": uri,
            "contents": [self._serialize_resource_content(c) for c in contents],
        }

    async def _ws_prompts_list(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """List prompt templates advertised by a connected MCP server.

        Same visibility gate as resources: if the caller can see the
        server, they can browse its prompt catalog. Rendering a
        specific prompt uses the separate ``prompts.get`` handler so
        argument values flow through the payload."""
        server_id = str(frame.get("server_id") or "").strip()
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        entry = self._clients.get(server_id)
        if entry is None or not self._can_see_server(entry.record, conn.user_ctx):
            return _ws_error(frame, "Server not found", code=404)
        if not entry.connected:
            return _ws_error(
                frame,
                f"Server not connected: {entry.last_error or 'unknown error'}",
                code=503,
            )
        try:
            async with asyncio.timeout(self._call_timeout):
                specs = await entry.backend.list_prompts()
        except NotImplementedError:
            return _ws_error(
                frame,
                "This MCP server does not support prompts",
                code=501,
            )
        except Exception as exc:  # noqa: BLE001
            return _ws_error(frame, f"list_prompts failed: {exc}")
        return {
            "type": "mcp.servers.prompts.list.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "prompts": [self._serialize_prompt(p) for p in specs],
        }

    async def _ws_prompts_get(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Render a prompt with concrete argument values.

        Required arguments are validated against the payload before
        dispatching to the backend so a missing-arg error comes back
        with a useful label rather than a raw protocol error."""
        server_id = str(frame.get("server_id") or "").strip()
        name = str(frame.get("name") or "").strip()
        raw_args = frame.get("arguments") or {}
        if not server_id:
            return _ws_error(frame, "Missing 'server_id'")
        if not name:
            return _ws_error(frame, "Missing 'name'")
        if not isinstance(raw_args, dict):
            return _ws_error(frame, "'arguments' must be an object")
        arguments = {str(k): str(v) for k, v in raw_args.items()}

        entry = self._clients.get(server_id)
        if entry is None or not self._can_see_server(entry.record, conn.user_ctx):
            return _ws_error(frame, "Server not found", code=404)
        if not entry.connected:
            return _ws_error(
                frame,
                f"Server not connected: {entry.last_error or 'unknown error'}",
                code=503,
            )
        try:
            async with asyncio.timeout(self._call_timeout):
                result = await entry.backend.get_prompt(name, arguments)
        except NotImplementedError:
            return _ws_error(
                frame,
                "This MCP server does not support prompts",
                code=501,
            )
        except Exception as exc:  # noqa: BLE001
            return _ws_error(frame, f"get_prompt failed: {exc}")
        return {
            "type": "mcp.servers.prompts.get.result",
            "ref": frame.get("id"),
            "server_id": server_id,
            "name": name,
            "description": result.description,
            "messages": [self._serialize_prompt_message(m) for m in result.messages],
        }

    @staticmethod
    def _serialize_prompt(spec: MCPPromptSpec) -> dict[str, Any]:
        return {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "arguments": [
                {
                    "name": a.name,
                    "description": a.description,
                    "required": a.required,
                }
                for a in spec.arguments
            ],
        }

    @staticmethod
    def _serialize_prompt_message(message: MCPPromptMessage) -> dict[str, Any]:
        block = message.content
        return {
            "role": message.role,
            "content": {
                "type": block.type,
                "text": block.text,
                "mime_type": block.mime_type,
                "uri": block.uri,
                "data": block.data,
            },
        }

    @staticmethod
    def _serialize_resource(spec: MCPResourceSpec) -> dict[str, Any]:
        return {
            "uri": spec.uri,
            "name": spec.name,
            "description": spec.description,
            "mime_type": spec.mime_type,
            "size": spec.size,
        }

    @staticmethod
    def _serialize_resource_content(content: MCPResourceContent) -> dict[str, Any]:
        return {
            "uri": content.uri,
            "kind": content.kind,
            "mime_type": content.mime_type,
            "text": content.text,
            "data": content.data,
        }

    async def _on_sampling_request(
        self,
        record: MCPServerRecord,
        params: Any,
    ) -> Any:
        """Handle a ``sampling/createMessage`` request from a remote
        MCP server.

        Runs under the SDK's client session in whatever task is
        processing the server-initiated request. Returns either a
        ``CreateMessageResult`` (success) or an ``ErrorData`` (any
        refusal — disabled, budget exceeded, profile missing, backend
        unavailable). Never raises to the SDK so a bad sampling
        request can't poison the session.

        The gate order matters: feature flag first (cheapest check),
        then transport validation, then profile existence, then
        budget, then capability resolution, then the actual call.
        """
        from mcp import types as mcp_types

        from gilbert.interfaces.ai import Message, MessageRole

        def _error(code: int, message: str) -> mcp_types.ErrorData:
            logger.info(
                "MCP sampling refused for %s: %s",
                record.slug,
                message,
            )
            return mcp_types.ErrorData(code=code, message=message)

        if not record.allow_sampling:
            return _error(
                -32601,
                f"Sampling is not enabled for MCP server {record.slug!r}",
            )
        if record.transport not in ("http", "sse"):
            return _error(
                -32601,
                "Sampling is only supported for remote MCP transports",
            )

        max_tokens = int(getattr(params, "maxTokens", 0) or 0)
        if max_tokens <= 0:
            max_tokens = 1024  # conservative default if caller omits
        budget = self._sampling_budgets.get(record.id)
        if budget is None:
            budget = _SamplingBudget(
                max_tokens=max(1, record.sampling_budget_tokens),
                window_seconds=max(1, record.sampling_budget_window_seconds),
            )
            self._sampling_budgets[record.id] = budget
        if not budget.can_admit(max_tokens):
            return _error(
                -32000,
                (
                    f"Sampling budget exhausted for {record.slug!r} "
                    f"({budget.used()}/{budget.max_tokens} tokens in "
                    f"the last {int(budget.window_seconds)}s)"
                ),
            )

        resolver = self._resolver
        if resolver is None:
            return _error(-32000, "MCP service not started")
        ai_svc = resolver.get_capability("ai")
        if ai_svc is None or not hasattr(ai_svc, "complete_one_shot"):
            return _error(-32000, "AI service unavailable")

        # Verify the requested profile exists. We don't enforce it's
        # tool-less — that's a config decision, not a runtime one —
        # but we do reject calls that name a missing profile so an
        # admin mistyping the config name doesn't silently degrade
        # to the default profile.
        profiles = getattr(ai_svc, "_profiles", None)
        if isinstance(profiles, dict) and record.sampling_profile not in profiles:
            return _error(
                -32000,
                (
                    f"AI profile {record.sampling_profile!r} does not exist "
                    "— set sampling_profile on the MCP server to an "
                    "existing profile name"
                ),
            )

        messages: list[Message] = []
        raw_messages = getattr(params, "messages", None) or []
        for m in raw_messages:
            role_raw = getattr(m, "role", "user")
            # SDK sampling uses only user/assistant — map anything
            # else defensively to USER so a server-side typo doesn't
            # blow up the request.
            if role_raw == "assistant":
                role = MessageRole.ASSISTANT
            else:
                role = MessageRole.USER
            content = getattr(m, "content", None)
            text = ""
            if content is not None:
                if getattr(content, "type", "") == "text":
                    text = str(getattr(content, "text", "") or "")
                else:
                    # Non-text sampling content (image/audio) isn't
                    # supported in Part 3.3 — the MCP spec allows it
                    # but routing through AIBackend needs the
                    # backend to accept multimodal. Serialize a hint
                    # so the remote sees something structural.
                    text = f"[non-text content: {getattr(content, 'type', 'unknown')}]"
            messages.append(Message(role=role, content=text))

        if not messages:
            return _error(-32602, "Sampling request has no messages")

        system_prompt = str(getattr(params, "systemPrompt", "") or "")

        try:
            response = await ai_svc.complete_one_shot(
                messages=messages,
                system_prompt=system_prompt,
                profile_name=record.sampling_profile,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MCP sampling call failed for %s: %s",
                record.slug,
                exc,
            )
            return _error(-32000, f"Sampling call failed: {exc}")

        # Consume actual tokens used. If the backend doesn't report
        # usage, consume the max_tokens ceiling as a conservative
        # fallback so the budget can't be bypassed by a backend that
        # forgets to fill in the field.
        used = max_tokens
        usage = getattr(response, "usage", None)
        if usage is not None:
            used = int(usage.input_tokens + usage.output_tokens) or max_tokens
        budget.consume(used)

        reply_text = getattr(response.message, "content", "") or ""
        stop_reason_raw = str(getattr(response, "stop_reason", "end_turn"))
        stop_reason = "maxTokens" if "max" in stop_reason_raw else "endTurn"

        return mcp_types.CreateMessageResult(
            role="assistant",
            content=mcp_types.TextContent(type="text", text=reply_text),
            model=str(getattr(response, "model", "") or "gilbert"),
            stopReason=stop_reason,
        )

    # ── Browser bridge (session-ephemeral MCP servers) ────────────────

    async def _ws_bridge_announce(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Register a set of browser-hosted MCP servers for this session.

        The frame shape is ``{servers: [{slug, name}, ...]}``. For each
        entry we build an in-memory ``MCPServerRecord``, wrap it in a
        ``BrowserMCPBackend`` bound to the calling connection, probe
        the browser with a ``tools/list`` round-trip to validate the
        local server is reachable, and register the entry in
        ``_session_clients[user_id]``.

        Returns a per-slug status list so the UI can surface which
        servers came up, which were rejected for collisions, and which
        failed the initial probe.
        """
        if not self._enabled:
            return _ws_error(frame, "MCP service is disabled")
        servers_raw = frame.get("servers") or []
        if not isinstance(servers_raw, list):
            return _ws_error(frame, "'servers' must be a list")

        user_ctx = conn.user_ctx
        user_id = user_ctx.user_id

        # Replace any existing session for this user. If the old
        # session is owned by a different conn, tear it down so the
        # newest tab wins. If it's owned by the same conn (re-announce
        # from a single tab after the user edits their local config),
        # same behaviour — start fresh.
        self._teardown_session(user_id)
        self._session_conn[user_id] = conn
        conn.add_close_callback(self._make_session_close_callback(user_id, conn))

        results: list[dict[str, Any]] = []
        for item in servers_raw:
            if not isinstance(item, dict):
                results.append({"ok": False, "error": "server entry must be an object"})
                continue
            slug = str(item.get("slug") or "").strip().lower()
            name = str(item.get("name") or slug).strip()
            if not slug:
                results.append({"ok": False, "error": "slug is required"})
                continue
            if not SLUG_RE.match(slug):
                results.append({"slug": slug, "ok": False, "error": f"invalid slug {slug!r}"})
                continue
            if TOOL_NAME_SEP in slug:
                results.append({"slug": slug, "ok": False, "error": "slug must not contain '__'"})
                continue
            # Collision with a persisted server the user can already see.
            if self._user_sees_persisted_slug(user_ctx, slug):
                results.append(
                    {
                        "slug": slug,
                        "ok": False,
                        "error": (
                            f"slug {slug!r} conflicts with an existing MCP server "
                            "visible to your account — pick a different name for "
                            "your local server"
                        ),
                    }
                )
                continue

            record = MCPServerRecord(
                id=f"browser:{user_id}:{slug}",
                name=name,
                slug=slug,
                transport="browser",
                scope="private",
                owner_id=user_id,
                # Effectively disable background cache refresh —
                # browser entries are session-ephemeral and refreshed
                # via explicit re-announce from the settings page. A
                # TTL of zero means "always expired", which would fire
                # a refresh on every get_tools() call (once per chat
                # turn), flooding the bridge with tools/list round-
                # trips. One day is effectively never for a tab that
                # lives on the order of hours.
                tool_cache_ttl_seconds=86_400,
            )
            backend = BrowserMCPBackend()
            backend.bind(conn, slug, call_timeout=self._call_timeout)
            entry = _ClientEntry(record, backend)
            try:
                await backend.connect(record)
                entry.tools = await backend.list_tools()
                entry.tools_fetched_at = time.monotonic()
                entry.connected = True
            except Exception as exc:  # noqa: BLE001
                # Include the exception class because some exception
                # types (notably ``asyncio.TimeoutError``) have an
                # empty ``str()``, which renders the log useless for
                # diagnosis. Log the full repr at info level too.
                exc_label = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                logger.info(
                    "Browser MCP %s announce failed for %s: %s",
                    slug,
                    user_id,
                    exc_label,
                )
                results.append({"slug": slug, "ok": False, "error": exc_label})
                continue

            self._session_clients.setdefault(user_id, {})[slug] = entry
            results.append(
                {
                    "slug": slug,
                    "ok": True,
                    "tool_count": len(entry.tools),
                }
            )

        return {
            "type": "mcp.bridge.announce.result",
            "ref": frame.get("id"),
            "results": results,
        }

    def _user_sees_persisted_slug(
        self,
        user_ctx: UserContext,
        slug: str,
    ) -> bool:
        """Would the user see a persisted server with this slug?"""
        for entry in self._clients.values():
            if entry.record.slug != slug:
                continue
            if self._can_see_server(entry.record, user_ctx):
                return True
        return False

    def _make_session_close_callback(
        self,
        user_id: str,
        conn: Any,
    ) -> Callable[[], None]:
        """Build a close callback bound to a specific user+conn pair.

        The callback is synchronous (WS unregister is sync), so it
        schedules an async teardown if a loop is running. If the
        session has been replaced by a newer tab before this fires,
        the owner check rejects the teardown so the active session
        survives.
        """

        def _cb() -> None:
            if self._session_conn.get(user_id) is not conn:
                return
            self._teardown_session(user_id)

        return _cb

    def _teardown_session(self, user_id: str) -> None:
        """Drop every session entry for a user and forget the owner."""
        session = self._session_clients.pop(user_id, None)
        self._session_conn.pop(user_id, None)
        if not session:
            return
        for entry in session.values():
            # BrowserMCPBackend.close is sync-safe (just drops the conn
            # ref); schedule the await so we don't block if a loop is
            # running, otherwise run it synchronously via new loop.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            coro = entry.backend.close()
            if loop is not None:
                loop.create_task(coro)
            else:  # pragma: no cover — only in sync test paths
                asyncio.run(coro)

    async def complete_oauth_callback(
        self,
        state: str,
        code: str,
        received_state: str | None,
    ) -> bool:
        """Called by the FastAPI callback route when a browser redirect
        hits ``/api/mcp/oauth/callback``. Resolves the pending flow
        identified by ``state`` so the SDK's callback_handler returns
        to the blocked connect task."""
        if self._oauth is None:
            return False
        return await self._oauth.complete(state, code, received_state)

    # --- helpers -----------------------------------------------------

    def _can_edit_record(self, record: MCPServerRecord, user_ctx: UserContext) -> bool:
        return user_ctx.user_id == record.owner_id or self._is_admin(user_ctx)

    def _serialize_record(
        self,
        record: MCPServerRecord,
        viewer: UserContext,
    ) -> dict[str, Any]:
        """JSON-safe view of a record. Env values are masked unless the
        viewer is the owner or an admin — every other viewer gets the keys
        only, with ``"****"`` for values, so they can see which variables
        exist without leaking secrets."""
        owner_or_admin = viewer.user_id == record.owner_id or self._is_admin(viewer)
        env_out: dict[str, str] = {}
        for k, v in record.env.items():
            env_out[k] = v if owner_or_admin else "****"
        auth_out: dict[str, Any] = {
            "kind": record.auth.kind,
            "oauth_scopes": list(record.auth.oauth_scopes),
            "oauth_client_name": record.auth.oauth_client_name,
            "bearer_token": (
                record.auth.bearer_token
                if owner_or_admin
                else ("****" if record.auth.bearer_token else "")
            ),
        }
        entry = self._clients.get(record.id)
        return {
            "id": record.id,
            "name": record.name,
            "slug": record.slug,
            "transport": record.transport,
            "command": list(record.command),
            "env": env_out,
            "cwd": record.cwd,
            "url": record.url,
            "auth": auth_out,
            "enabled": record.enabled,
            "auto_start": record.auto_start,
            "scope": record.scope,
            "owner_id": record.owner_id,
            "allowed_roles": list(record.allowed_roles),
            "allowed_users": list(record.allowed_users),
            "tool_cache_ttl_seconds": record.tool_cache_ttl_seconds,
            "allow_sampling": record.allow_sampling,
            # Sampling budget details leak how much the server is
            # allowed to spend, so mask them for non-owners/non-admins
            # to keep private config private. The ``allow_sampling``
            # flag itself is visible to everyone who can see the
            # server — they deserve to know the server can consume
            # AI budget on their behalf.
            "sampling_profile": (record.sampling_profile if owner_or_admin else ""),
            "sampling_budget_tokens": (record.sampling_budget_tokens if owner_or_admin else 0),
            "sampling_budget_window_seconds": (
                record.sampling_budget_window_seconds if owner_or_admin else 0
            ),
            "sampling_budget_used": (
                self._sampling_budgets[record.id].used()
                if owner_or_admin and record.id in self._sampling_budgets
                else 0
            ),
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            "last_connected_at": (
                record.last_connected_at.isoformat() if record.last_connected_at else None
            ),
            "last_error": record.last_error,
            "connected": bool(entry and entry.connected),
            "tool_count": len(entry.tools) if entry else 0,
            "needs_oauth": record.id in self._needs_oauth,
            "retry_count": entry.retry_count if entry else 0,
            "next_retry_at": (
                entry.next_retry_at.isoformat() if entry and entry.next_retry_at else None
            ),
        }

    def _record_from_payload(
        self,
        payload: dict[str, Any],
        *,
        owner_id: str,
        fallback_id: str = "",
        env_override: dict[str, str] | None = None,
        auth_override: MCPAuthConfig | None = None,
        created_at_override: datetime | None = None,
    ) -> MCPServerRecord:
        """Build a ``MCPServerRecord`` from a raw RPC payload.

        The caller is responsible for enforcing authorization; this is
        purely a deserializer. Unknown/missing fields get sensible
        defaults, and the slug is derived from ``name`` when not
        supplied, so the UI can omit it."""
        name = str(payload.get("name") or "").strip()
        slug = str(payload.get("slug") or _slugify(name)).strip().lower()
        env = env_override if env_override is not None else dict(payload.get("env") or {})
        auth = (
            auth_override
            if auth_override is not None
            else _auth_from_payload(
                payload.get("auth") or {},
            )
        )
        return MCPServerRecord(
            id=str(payload.get("id") or fallback_id or ""),
            name=name,
            slug=slug,
            transport=str(payload.get("transport") or "stdio"),  # type: ignore[arg-type]
            command=tuple(payload.get("command") or ()),
            env=env,
            cwd=payload.get("cwd") or None,
            url=(str(payload["url"]).strip() or None) if payload.get("url") else None,
            auth=auth,
            enabled=bool(payload.get("enabled", True)),
            auto_start=bool(payload.get("auto_start", True)),
            scope=str(payload.get("scope") or "private"),  # type: ignore[arg-type]
            owner_id=owner_id,
            allowed_roles=_as_tuple(payload.get("allowed_roles")),
            allowed_users=_as_tuple(payload.get("allowed_users")),
            tool_cache_ttl_seconds=int(payload.get("tool_cache_ttl_seconds", 300)),
            allow_sampling=bool(payload.get("allow_sampling", False)),
            sampling_profile=str(
                payload.get("sampling_profile") or "standard",
            ),
            sampling_budget_tokens=int(
                payload.get("sampling_budget_tokens", 10_000),
            ),
            sampling_budget_window_seconds=int(
                payload.get("sampling_budget_window_seconds", 3600),
            ),
            created_at=created_at_override,
        )

    # ── validation & marshalling ──────────────────────────────────────

    @staticmethod
    def _validate_record(record: MCPServerRecord) -> None:
        if not record.name.strip():
            raise ValueError("MCP server name is required")
        if not SLUG_RE.match(record.slug):
            raise ValueError(
                f"Invalid slug {record.slug!r}: must start with a lowercase "
                "letter and contain only lowercase letters, digits, and hyphens"
            )
        if TOOL_NAME_SEP in record.slug:
            raise ValueError(f"Slug {record.slug!r} must not contain '__'")
        if record.transport == "stdio":
            if not record.command:
                raise ValueError("Stdio MCP servers require a command")
        elif record.transport in ("http", "sse"):
            if not record.url:
                raise ValueError(f"{record.transport.upper()} MCP servers require a URL")
            if not (record.url.startswith("http://") or record.url.startswith("https://")):
                raise ValueError("MCP server URL must start with http:// or https://")
        elif record.transport == "browser":
            # Browser-hosted MCP servers are session-ephemeral and never
            # reach the persisted-create path. Rejecting them here keeps
            # the UI and entity storage free of transport="browser" rows.
            raise ValueError(
                "Browser-hosted MCP servers are session-only and cannot "
                "be created via the standard CRUD flow",
            )
        else:
            raise ValueError(f"Invalid transport: {record.transport}")
        if record.auth.kind not in ("none", "bearer", "oauth"):
            raise ValueError(f"Invalid auth kind: {record.auth.kind}")
        if record.auth.kind == "bearer" and not record.auth.bearer_token:
            raise ValueError("Bearer auth requires a token")
        if record.transport == "stdio" and record.auth.kind != "none":
            raise ValueError("Stdio MCP servers do not support auth")
        if not record.owner_id:
            raise ValueError("MCP server must have an owner_id")
        if record.scope not in ("private", "shared", "public"):
            raise ValueError(f"Invalid scope: {record.scope}")
        if record.scope == "shared" and not record.allowed_roles and not record.allowed_users:
            raise ValueError("Shared MCP servers must grant access to at least one role or user")
        if record.scope == "private" and (record.allowed_roles or record.allowed_users):
            raise ValueError("Private MCP servers cannot have allowed_roles or allowed_users")
        if record.allow_sampling:
            if record.transport == "stdio":
                raise ValueError(
                    "Sampling is only supported for remote MCP transports",
                )
            if record.sampling_budget_tokens <= 0:
                raise ValueError(
                    "sampling_budget_tokens must be positive when sampling is enabled",
                )
            if record.sampling_budget_window_seconds <= 0:
                raise ValueError(
                    "sampling_budget_window_seconds must be positive when sampling is enabled",
                )
            if not record.sampling_profile.strip():
                raise ValueError(
                    "sampling_profile must name an AI context profile",
                )

    @staticmethod
    def _doc_from_record(record: MCPServerRecord) -> dict[str, Any]:
        return {
            "_id": record.id,
            "name": record.name,
            "slug": record.slug,
            "transport": record.transport,
            "command": list(record.command),
            "env": dict(record.env),
            "cwd": record.cwd,
            "url": record.url,
            "auth": {
                "kind": record.auth.kind,
                "bearer_token": record.auth.bearer_token,
                "oauth_scopes": list(record.auth.oauth_scopes),
                "oauth_client_name": record.auth.oauth_client_name,
            },
            "enabled": record.enabled,
            "auto_start": record.auto_start,
            "scope": record.scope,
            "owner_id": record.owner_id,
            "allowed_roles": list(record.allowed_roles),
            "allowed_users": list(record.allowed_users),
            "tool_cache_ttl_seconds": record.tool_cache_ttl_seconds,
            "allow_sampling": record.allow_sampling,
            "sampling_profile": record.sampling_profile,
            "sampling_budget_tokens": record.sampling_budget_tokens,
            "sampling_budget_window_seconds": record.sampling_budget_window_seconds,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            "last_connected_at": (
                record.last_connected_at.isoformat() if record.last_connected_at else None
            ),
            "last_error": record.last_error,
        }

    @staticmethod
    def _record_from_doc(doc: dict[str, Any]) -> MCPServerRecord:
        scope: MCPServerScope = doc.get("scope", "private")
        transport = doc.get("transport", "stdio")
        auth_raw = doc.get("auth") or {}
        auth_kind: MCPAuthKind = auth_raw.get("kind", "none")
        auth = MCPAuthConfig(
            kind=auth_kind,
            bearer_token=str(auth_raw.get("bearer_token") or ""),
            oauth_scopes=tuple(auth_raw.get("oauth_scopes") or ()),
            oauth_client_name=str(auth_raw.get("oauth_client_name") or "Gilbert"),
        )
        return MCPServerRecord(
            id=str(doc.get("_id") or ""),
            name=str(doc.get("name") or ""),
            slug=str(doc.get("slug") or ""),
            transport=transport,
            command=tuple(doc.get("command") or ()),
            env=dict(doc.get("env") or {}),
            cwd=doc.get("cwd"),
            url=doc.get("url"),
            auth=auth,
            enabled=bool(doc.get("enabled", True)),
            auto_start=bool(doc.get("auto_start", True)),
            scope=scope,
            owner_id=str(doc.get("owner_id") or ""),
            allowed_roles=tuple(doc.get("allowed_roles") or ()),
            allowed_users=tuple(doc.get("allowed_users") or ()),
            tool_cache_ttl_seconds=int(doc.get("tool_cache_ttl_seconds", 300)),
            allow_sampling=bool(doc.get("allow_sampling", False)),
            sampling_profile=str(doc.get("sampling_profile") or "standard"),
            sampling_budget_tokens=int(doc.get("sampling_budget_tokens", 10_000)),
            sampling_budget_window_seconds=int(
                doc.get("sampling_budget_window_seconds", 3600),
            ),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
            last_connected_at=_parse_dt(doc.get("last_connected_at")),
            last_error=doc.get("last_error"),
        )


# ── module-level helpers (not methods; no `self` needed) ─────────────


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json_schema_type(raw: Any) -> ToolParameterType:
    """Map a JSON Schema ``type`` token to the closest ToolParameterType.

    MCP servers may advertise arrays like ``["string","null"]``; we pick
    the first non-null token. Unknown or missing types degrade to string,
    which is a safe-ish passthrough since most AI providers accept string
    inputs even for numeric-looking tools."""
    if isinstance(raw, list):
        for item in raw:
            if item and item != "null":
                raw = item
                break
    token = str(raw or "").lower()
    mapping = {
        "string": ToolParameterType.STRING,
        "integer": ToolParameterType.INTEGER,
        "number": ToolParameterType.NUMBER,
        "boolean": ToolParameterType.BOOLEAN,
        "array": ToolParameterType.ARRAY,
        "object": ToolParameterType.OBJECT,
    }
    return mapping.get(token, ToolParameterType.STRING)


def _render_block(block: MCPContentBlock) -> str:
    if block.type == "text":
        return block.text
    if block.type == "image":
        return f"[image {block.mime_type or 'application/octet-stream'}]"
    if block.type == "audio":
        return f"[audio {block.mime_type or 'application/octet-stream'}]"
    if block.type == "resource":
        label = block.uri or "resource"
        return f"[resource {label}]{(': ' + block.text) if block.text else ''}"
    return ""


def _ws_error(
    frame: dict[str, Any],
    error: str,
    *,
    code: int = 400,
) -> dict[str, Any]:
    return {
        "type": "gilbert.error",
        "ref": frame.get("id"),
        "error": error,
        "code": code,
    }


def _slugify(name: str) -> str:
    """Derive a safe slug from a display name.

    Lowercase, strip non-alphanumerics to hyphens, collapse runs, drop
    leading/trailing hyphens, and ensure it starts with a letter by
    prepending ``s-`` if needed. The result still goes through
    ``SLUG_RE`` validation downstream, so this is a convenience helper —
    the UI can always override with an explicit slug."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        return ""
    if not s[0].isalpha():
        s = f"s-{s}"
    return s


def _auth_from_payload(raw: dict[str, Any]) -> MCPAuthConfig:
    """Build a fresh ``MCPAuthConfig`` from an RPC payload (no masked-
    value preservation — that's ``_merge_auth``'s job on update)."""
    kind: MCPAuthKind = raw.get("kind", "none")
    return MCPAuthConfig(
        kind=kind,
        bearer_token=str(raw.get("bearer_token") or ""),
        oauth_scopes=_as_tuple(raw.get("oauth_scopes")),
        oauth_client_name=str(raw.get("oauth_client_name") or "Gilbert"),
    )


def _merge_auth(
    existing: MCPAuthConfig,
    incoming: dict[str, Any],
) -> MCPAuthConfig:
    """Merge an incoming auth payload over the existing auth.

    Mirrors the env-merge rule: a bearer token value of ``"****"`` in
    the incoming payload means "keep the stored token". All other
    fields are replaced by the incoming values so switching kinds or
    scopes still behaves like a straight update."""
    kind: MCPAuthKind = incoming.get("kind", existing.kind)
    raw_bearer = str(incoming.get("bearer_token") or "")
    if raw_bearer == "****":
        bearer = existing.bearer_token
    else:
        bearer = raw_bearer
    return MCPAuthConfig(
        kind=kind,
        bearer_token=bearer,
        oauth_scopes=_as_tuple(incoming.get("oauth_scopes", existing.oauth_scopes)),
        oauth_client_name=str(
            incoming.get("oauth_client_name") or existing.oauth_client_name,
        ),
    )


def _merge_env(
    existing: dict[str, str],
    incoming: dict[str, Any],
) -> dict[str, str]:
    """Merge an incoming env payload over the existing env.

    The serializer masks sensitive values with ``"****"``; on update, a
    masked value in the incoming payload means "keep the existing value
    for this key". Any key missing from the incoming payload is dropped,
    so the UI's edit form is the source of truth for which keys exist.
    """
    merged: dict[str, str] = {}
    for key, raw in incoming.items():
        value = str(raw) if raw is not None else ""
        if value == "****" and key in existing:
            merged[key] = existing[key]
        else:
            merged[key] = value
    return merged


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(v) for v in value)
    return (str(value),)
