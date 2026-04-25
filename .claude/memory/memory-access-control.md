# Access Control Service (RBAC)

## Summary
Hierarchical role-based access control with numeric levels, per-tool permissions, collection ACLs, overrides, and enforcement at AI tool execution, web routes, and entity storage levels.

## Details

### Role Hierarchy
- `admin` (level 0) — full access, immutable
- `user` (level 100) — standard access, immutable
- `everyone` (level 200) — minimum access for any user including guests, immutable
- Custom roles can be added at any level; lower number = more privileged
- User's effective level = min(levels of assigned roles)
- SYSTEM user (background jobs) = level -1 (bypasses all checks)
- GUEST user (unauthenticated local visitors) = "everyone" role only

### User Contexts
- `UserContext.SYSTEM` — background jobs, no roles, bypasses all RBAC
- `UserContext.GUEST` — unauthenticated local web visitors, has `{"everyone"}` role
- Logged-in users — roles from their user entity

### Service
- `src/gilbert/core/services/access_control.py` — `AccessControlService`
- Capabilities: `access_control`, `ai_tools`
- Stores roles in `acl_roles`, tool overrides in `acl_tool_overrides`, collection ACLs in `acl_collections`
- In-memory cache refreshed on mutations
- Seeds built-in roles on startup (idempotent)

### Permission Flow
1. `ToolDefinition.required_role` (default: "user") declares minimum role
2. AI Context Profiles control which tools are available per AI interaction (see AI service memory)
3. `AIService._discover_tools(user_ctx, profile)` filters tools by profile then by RBAC
4. `AIService._execute_tool_calls()` does defense-in-depth re-check
5. Web routes use `require_role("admin")` dependency (hierarchy-aware)
6. Tool overrides in entity store can change any tool's required role
7. Collection ACLs control read/write per entity collection (default: read=user, write=admin)
8. Timer ownership: non-admin users can only cancel their own timers

### Tool Annotations (baseline)
- **admin**: create_user, sync_users, update/reset_persona, set_configuration, store_entity, set/remove_speaker_alias, all ACL write tools
- **user** (default): play_audio, announce, set_timer, play_track, cancel_timer, etc.
- **everyone**: list_*, get_* (except get_configuration=admin), search_*, synthesize, check_presence, chat

### Local vs Tunnel Web Access
- **Local unauthenticated**: assigned GUEST context (everyone role), can see Chat card, use chat — *only when the `auth.allow_guests` setting is true (the default)*. When admin turns it off, local unauthenticated requests are redirected to /auth/login the same way tunnel requests are, and WebSocket connections without a valid session are closed with code 1008.
- **Tunnel unauthenticated**: redirected to login (except auth flow + static files) — independent of `allow_guests`.
- **Tunnel authenticated**: full access based on roles
- Dashboard cards and nav links filtered by user's effective role level
- AI only describes capabilities matching available tools — won't claim it can do things the user's role doesn't allow

The `allow_guests` toggle is exposed via the `GuestPolicy` runtime_checkable Protocol (`is_guest_allowed() -> bool`) so the web/WS layers query it without importing `AuthService`. `AuthService` reads it from the `auth` config section on start and refreshes via `on_config_changed`.

### Web Route Protection
- `/` (dashboard) → public, cards filtered by role
- `/chat/*` → any user with roles (authenticated or local guest)
- `/roles/*` → admin only
- `/system` → admin only
- `/entities/*` → admin only

### Web UI for Role Management
- `/roles` — role hierarchy table with create/edit/delete
- `/roles/tools` — per-tool permission overrides
- `/roles/users` — assign roles to users
- `/roles/collections` — per-collection read/write ACLs

## Related
- `src/gilbert/interfaces/tools.py` — `required_role` field on ToolDefinition
- `src/gilbert/interfaces/auth.py` — UserContext with SYSTEM and GUEST sentinels
- `src/gilbert/core/services/ai.py` — permission enforcement in tool discovery/execution
- `src/gilbert/web/auth.py` — hierarchy-aware `require_role()`, local vs tunnel auth
- `tests/unit/test_access_control.py` — 42 tests
