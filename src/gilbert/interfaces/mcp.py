"""MCP (Model Context Protocol) client interfaces.

Defines the backend ABC and shared data types for client-side MCP support.
Gilbert consumes MCP tools from external MCP servers configured per-user or
per-role. Each concrete transport (stdio, HTTP, SSE) is an ``MCPBackend``;
the core ``MCPService`` owns lifecycle, visibility rules, and the adapter
that turns ``MCPToolSpec`` into Gilbert ``ToolDefinition`` records.

Part 1 supports the ``stdio`` transport only. Part 2 adds ``http`` / ``sse``
and Part 3 adds resources, prompts, and sampling — only tool support is
modeled here for now, and the types are designed to grow without breaking
the backend contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam

MCPServerScope = Literal["private", "shared", "public"]
MCPTransport = Literal["stdio", "http", "sse", "browser"]
"""Transport kinds. ``stdio`` spawns a subprocess; ``http`` speaks MCP
over Streamable HTTP; ``sse`` uses the older Server-Sent Events
transport. ``browser`` proxies requests through the owning user's
WebSocket to an MCP server running inside their browser's reach
(localhost or LAN) — these records are session-ephemeral and never
persisted. ``command``/``env``/``cwd`` are only meaningful for
``stdio``; ``url``/``auth`` are only meaningful for ``http``/``sse``;
``browser`` uses neither (the URL lives client-side in the browser
bridge, never on the server)."""

MCPAuthKind = Literal["none", "bearer", "oauth"]


@dataclass(frozen=True)
class MCPAuthConfig:
    """Per-server authentication configuration for remote transports.

    ``kind="none"`` is the default and works for local or unprotected
    MCP servers. ``kind="bearer"`` sends a static bearer token as
    ``Authorization: Bearer <token>``; the token is treated as a
    secret and masked in any non-owner view. ``kind="oauth"`` runs the
    MCP OAuth 2.1 flow handled by the SDK's ``OAuthClientProvider``
    (tokens live in a separate storage, not on this record)."""

    kind: MCPAuthKind = "none"
    bearer_token: str = ""
    # OAuth tunables — all auto-discovered at runtime but can be
    # overridden by the operator if the server's well-known metadata
    # is missing or wrong.
    oauth_scopes: tuple[str, ...] = ()
    oauth_client_name: str = "Gilbert"


@dataclass(frozen=True)
class MCPServerRecord:
    """Runtime snapshot of a configured MCP server.

    Mirrors a row in the ``mcp_servers`` entity collection. This is the
    shape backends and the service use to talk about a server — the
    service materializes it from stored entities before passing it to a
    backend's ``connect()``.
    """

    id: str
    name: str
    slug: str
    transport: MCPTransport
    # Stdio-only fields (ignored for http/sse).
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # Remote-only fields (ignored for stdio).
    url: str | None = None
    auth: MCPAuthConfig = field(default_factory=MCPAuthConfig)

    enabled: bool = True
    auto_start: bool = True

    scope: MCPServerScope = "private"
    owner_id: str = ""
    allowed_roles: tuple[str, ...] = ()
    allowed_users: tuple[str, ...] = ()

    tool_cache_ttl_seconds: int = 300

    # Sampling — remote MCP servers asking Gilbert to run an LLM
    # call on their behalf. Off by default; toggling ``allow_sampling``
    # is admin-only even for private servers because it lets the
    # remote consume AI budget. ``sampling_profile`` names an
    # ``AIContextProfile`` used for every sampling request from this
    # server (normally a tool-less profile) and is validated at
    # service-side dispatch time. The token budget is a sliding
    # window enforced in-memory; crossing it rejects the request
    # with a transient error rather than killing the session.
    allow_sampling: bool = False
    sampling_profile: str = "mcp_sampling"
    sampling_budget_tokens: int = 10_000
    sampling_budget_window_seconds: int = 3600

    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_connected_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class MCPToolSpec:
    """A tool advertised by an MCP server.

    ``input_schema`` is the raw JSON Schema object as returned by the
    server. The service translates it into Gilbert ``ToolParameter`` s
    when building a ``ToolDefinition``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class MCPContentBlock:
    """A single content block returned by an MCP tool call.

    Part 1 surfaces text blocks to the AI; image / audio / resource
    blocks are accepted structurally so the result type does not change
    when later parts wire them through.
    """

    type: Literal["text", "image", "resource", "audio"]
    text: str = ""
    data: str = ""
    mime_type: str = ""
    uri: str = ""


@dataclass(frozen=True)
class MCPToolResult:
    """Result of invoking a tool on an MCP server."""

    content: tuple[MCPContentBlock, ...]
    is_error: bool = False
    structured: dict[str, Any] | None = None


@dataclass(frozen=True)
class MCPResourceSpec:
    """A resource advertised by an MCP server.

    Mirrors ``mcp.types.Resource`` but trimmed to the fields Gilbert
    actually surfaces. Backends translate the SDK type into this
    dataclass so downstream code doesn't have to import SDK types.
    """

    uri: str
    name: str
    description: str = ""
    mime_type: str = ""
    size: int | None = None


@dataclass(frozen=True)
class MCPPromptArgument:
    """One argument a prompt template accepts."""

    name: str
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class MCPPromptSpec:
    """A prompt template advertised by an MCP server.

    Mirrors ``mcp.types.Prompt`` trimmed to what Gilbert surfaces.
    Rendering a prompt is a two-step flow: list → pick → get with
    argument values → received a ``MCPPromptResult`` holding the
    rendered messages."""

    name: str
    title: str = ""
    description: str = ""
    arguments: tuple[MCPPromptArgument, ...] = ()


@dataclass(frozen=True)
class MCPPromptMessage:
    """One message in a rendered prompt — role plus content block."""

    role: Literal["user", "assistant", "system"]
    content: MCPContentBlock


@dataclass(frozen=True)
class MCPPromptResult:
    """Result of rendering a prompt with concrete argument values."""

    description: str
    messages: tuple[MCPPromptMessage, ...]


@dataclass(frozen=True)
class MCPResourceContent:
    """One chunk of a read-resource response.

    An MCP server can return multiple content entries per resource
    read — typically one, but some servers split large resources
    into multiple pieces. ``kind`` is ``"text"`` for
    ``TextResourceContents`` and ``"blob"`` for ``BlobResourceContents``
    (with ``data`` base64-encoded). Part 3.1 renders only text;
    binary blobs are surfaced structurally but not rendered.
    """

    uri: str
    kind: Literal["text", "blob"]
    mime_type: str = ""
    text: str = ""
    data: str = ""  # base64-encoded when kind="blob"


class MCPBackend(ABC):
    """Abstract transport for an MCP client connection.

    One backend instance owns one connection to one MCP server; the
    ``MCPService`` spawns one per configured ``MCPServerRecord`` and
    routes tool calls through it. Concrete implementations register
    themselves by setting ``backend_name`` — the service looks them up
    from the registry keyed on the server's ``transport`` field.
    """

    _registry: dict[str, type[MCPBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            MCPBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[MCPBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-wide parameters. Stdio has none — per-server fields
        (command, env, cwd) live on the entity record, not in global
        config — but later transports may declare proxy settings, CA
        bundles, etc. here."""
        return []

    @abstractmethod
    async def connect(self, record: MCPServerRecord) -> None:
        """Establish the transport and complete MCP initialization."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the connection and release any OS resources."""

    @abstractmethod
    async def list_tools(self) -> list[MCPToolSpec]:
        """Return the tools currently advertised by the server."""

    @abstractmethod
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> MCPToolResult:
        """Invoke ``name`` on the connected server."""

    async def list_resources(self) -> list[MCPResourceSpec]:
        """Return the resources currently advertised by the server.

        Default implementation returns an empty list so backends that
        don't yet support resources compile and behave as "no
        resources available". The stdio and HTTP/SSE backends
        override this.
        """
        return []

    async def read_resource(self, uri: str) -> list[MCPResourceContent]:
        """Read a single resource by URI.

        Default raises ``NotImplementedError`` so any caller that hits
        an unsupported backend gets a clear signal rather than silent
        empty results."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support reading resources",
        )

    async def list_prompts(self) -> list[MCPPromptSpec]:
        """Return the prompt templates advertised by the server.

        Default empty list so backends without prompt support behave
        as "no prompts available". Stdio and HTTP/SSE override."""
        return []

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> MCPPromptResult:
        """Render a prompt template with the given argument values."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support rendering prompts",
        )

    async def set_tools_changed_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Register a coroutine invoked when the server pushes a
        ``notifications/tools/list_changed``. Default: no-op. Backends
        that support push invalidation override this so the service can
        drop its tool cache without waiting for the TTL."""
        return None

    async def set_sampling_callback(
        self,
        callback: Any,
    ) -> None:
        """Register a callback to handle server-initiated
        ``sampling/createMessage`` requests.

        The callback runs when a remote MCP server asks Gilbert to
        execute an LLM call on its behalf. Default is no-op, meaning
        sampling requests are rejected by the SDK as unsupported.
        Backends that plumb sampling through to ``ClientSession``
        override this — the service sets it **before** calling
        ``connect`` on each fresh backend instance, just like
        ``set_tools_changed_callback``. The callback signature is
        the SDK's ``SamplingFnT``: async ``(context, params) ->
        CreateMessageResult | ErrorData``.
        """
        return None


@runtime_checkable
class AuthCapableMCPBackend(Protocol):
    """Protocol for MCP backends that accept a per-connect auth override.

    Remote (HTTP/SSE) backends implement this so the service can pass
    an ``httpx.Auth``-compatible provider — typically the SDK's
    ``OAuthClientProvider`` — without the call site having to know which
    concrete backend it's holding.
    """

    async def connect_with_auth(
        self,
        record: MCPServerRecord,
        *,
        auth: Any,
    ) -> None:
        ...


@runtime_checkable
class WsBoundMCPBackend(Protocol):
    """Protocol for MCP backends that ride a live WebSocket connection.

    The browser transport is bound to a user's WS session before
    ``connect`` so calls can be routed back to the bridge running in
    that user's browser. The service uses this protocol to bind the
    backend without importing the concrete browser class.
    """

    def bind(
        self,
        conn: Any,
        slug: str,
        *,
        call_timeout: float = 30.0,
    ) -> None:
        ...


@runtime_checkable
class MCPOAuthCallbackHandler(Protocol):
    """Protocol for the MCP client service's OAuth callback completion.

    The web route at ``/api/mcp/oauth/callback`` uses this to resolve
    the pending flow without importing the concrete client service.
    """

    async def complete_oauth_callback(
        self,
        state: str,
        code: str,
        received_state: str | None,
    ) -> bool:
        ...


@runtime_checkable
class MCPServerEndpoint(Protocol):
    """Protocol for the MCP server-side endpoint that the web layer
    delegates to.

    The HTTP route at ``/api/mcp`` resolves the ``"mcp_server"``
    capability and uses this protocol to gate the request and run
    bearer authentication, without importing the concrete service
    class from ``core/services/``.
    """

    @property
    def enabled(self) -> bool:
        ...

    async def authenticate(
        self,
        token: str,
        *,
        client_ip: str = "",
    ) -> Any:
        ...
