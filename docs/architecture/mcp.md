# MCP (Model Context Protocol)

## Summary
Gilbert both **consumes** external MCP servers (making their tools available to its own AI pipeline, with per-server RBAC) and **exposes** its own tools as an MCP server that external agents (Claude Desktop, Cursor, etc.) can connect to. Both sides are multi-user-aware.

## Details

### Client side — `MCPService` (`src/gilbert/core/services/mcp.py`)

Connects *out* to external MCP servers and surfaces their tools to Gilbert's own AI pipeline. Each entry in the `mcp_servers` entity collection is an `MCPServerRecord` with:

- `transport` — stdio / http / sse
- `url` or `command` + `args` / `env`
- `auth` (`MCPAuthConfig`): `kind = none / bearer / oauth`
- `scope` — private / shared / public (who can see this server)
- `allowed_users` — when private, the exact users allowed
- `required_role_to_invoke` — RBAC gate applied at tool-call time
- `tool_cache_ttl_seconds` — defaults to 300
- `allow_sampling`, `sampling_profile`, `sampling_budget_tokens` — whether the server can ask Gilbert for completions, under which AI profile, and with what token budget

Backends (auto-registered via `__init_subclass__`):
- `StdioMCPBackend` — `src/gilbert/integrations/mcp_stdio.py`, uses `AsyncExitStack`
- `HttpMCPBackend` / `SseMCPBackend` — `src/gilbert/integrations/mcp_http.py`, runs the SDK session in a **dedicated task** with `asyncio.Event` coordination. The obvious `AsyncExitStack` pattern trips anyio's "cancel scope in a different task" check under load; the dedicated-task approach sidesteps it.

Supervisor loop in `MCPService._supervise` reconnects with exponential backoff + jitter. OAuth flow runs via `OAuthFlowManager` (`core/services/mcp_oauth.py`) which wraps the SDK's `OAuthClientProvider` with an `EntityStorageTokenStorage` so tokens persist per-server.

Visibility model: **"if you can see it, you can use it"** — users see the set of servers their scope + `allowed_users` list grants, and any tool discovered from a server they can see is callable subject to the server's `required_role_to_invoke`. No separate tool-allowlist per user.

Each external server's tools are namespaced `mcp__<slug>__<tool>` when merged into the global tool registry.

### Server side — `MCPServerService` (`src/gilbert/core/services/mcp_server.py`)

Exposes Gilbert's own tools as an MCP endpoint for external agents. Owns the `mcp_server_clients` entity collection. Implements `Configurable` with a single `enabled` param in the `mcp_server` namespace (restart_required=True).

Each `MCPServerClient` binds:
- `name`, `description`, `owner_user_id`, `ai_profile`, `active`
- Argon2 hash of the bearer token (token is shown exactly once at create/rotate time and never again)
- `token_prefix` for UI identification

Admin-only WS RPCs under `mcp.clients.*`:
- `list`, `create`, `update`, `delete`, `rotate_token`
- `preview_tools` — given an `owner_user_id` + `profile_name`, returns the list of tools the client would be able to see (powers the Create dialog's safety preview)

### HTTP transport — `mcp_server_http.py` + `web/routes/mcp.py`

- `MCPServerHttpApp` builds an SDK `Server` and wires `list_tools` / `call_tool` handlers that dispatch through `AIService.discover_tools(user_ctx, profile_name)`.
- `list_tools` uses the owner's `UserContext` + the client's `ai_profile` — identical filtering to what the AI sees in chat. So if you want an MCP client to have broad access, point it at an `all`-mode profile; if you want it locked down, use an `include`-mode profile with an explicit allowlist.
- `call_tool` runs under the owner's `UserContext` via `_current_user.set(...)`, so every RBAC / ownership check behaves as though that user is calling the tool from chat. Per-call audit lines go to the `gilbert.mcp_server.audit` logger (tool_ok / tool_denied / tool_rejected / tool_error with client_id, user_id, duration_ms).
- The endpoint is mounted as a **raw Starlette ASGI Route** (not a FastAPI route) via `_McpAsgiEndpoint` class because the SDK's `StreamableHTTPSessionManager` owns the ASGI protocol directly; returning a Response from a FastAPI handler after the session manager writes to `send` causes double-response errors. The class-vs-function matters: starlette's `Route` introspects with `inspect.isfunction` and would wrap a function handler in its request/response helper.
- Mounted at **`/api/mcp`** (not `/mcp`) so it doesn't collide with the SPA's `/mcp/*` admin pages — browser refreshes on frontend MCP pages fall through to the SPA fallback cleanly. External clients configure `https://<host>/api/mcp` as their server URL with `Authorization: Bearer <token>`.

### Default profile safety

The seeded `mcp_server_client` profile defaults to `tool_mode="include"` with an empty tools list — a fresh MCP client sees **zero tools** until an admin explicitly adds them to the profile (or switches to a different profile). This is deliberate: external agents are untrusted by default, and an empty allowlist fails safe.

The admin Create dialog in `frontend/src/components/mcp/McpClientsPage.tsx` now has a real profile dropdown (populated from `listProfiles()`) with inline badges indicating `tool_mode` (destructive "all tools", secondary "allowlist"), plus a live tool-preview panel that calls `mcp.clients.preview_tools` to show exactly which tools the new client will be able to see. Picking a profile with `tool_mode="all"` raises an amber warning.

### Frontend routes

- `/mcp/servers` — `McpPage.tsx`, manage external servers Gilbert connects to (user-level, scope-filtered)
- `/mcp/clients` — `McpClientsPage.tsx`, admin-only, manage bearer tokens for external MCP clients connecting to Gilbert

Both live under `/mcp/*` so a bare `/mcp` redirect in `App.tsx` lands on `/mcp/servers`. The backend MCP endpoint is at `/api/mcp`, completely separate from the frontend routes.

## Related
- `src/gilbert/interfaces/mcp.py` — `MCPBackend` ABC, records, specs
- `src/gilbert/core/services/mcp.py` — client side service
- `src/gilbert/core/services/mcp_oauth.py` — OAuth flow manager + token storage
- `src/gilbert/core/services/mcp_server.py` — server side service (Configurable, Argon2 tokens, preview_tools)
- `src/gilbert/core/services/mcp_server_http.py` — SDK `Server` wiring + `list_tools` / `call_tool` dispatch
- `src/gilbert/integrations/mcp_stdio.py` — stdio backend
- `src/gilbert/integrations/mcp_http.py` — http/sse backends with dedicated-task pattern
- `src/gilbert/web/routes/mcp.py` — raw ASGI handler class at `/api/mcp`
- `frontend/src/components/mcp/McpPage.tsx` — servers admin page
- `frontend/src/components/mcp/McpClientsPage.tsx` — clients admin page with profile preview
- [AI Context Profiles](ai-context-profiles.md) — the profile system MCP filtering builds on
- `src/gilbert/interfaces/acl.py` — RBAC layer applied on top of profile filtering
