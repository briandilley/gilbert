# WebSocket Protocol

## Summary
Bidirectional WebSocket at `/ws/events` with typed message frames, role-based event filtering, and peer publishing. Primary real-time channel for web UI, inter-Gilbert communication, and external integrations. RPC handlers are distributed across owning services via the `WsHandlerProvider` protocol.

## Details

### Wire Protocol
All frames are JSON with a `type` field using `namespace.resource.verb` naming. Optional `id` (request correlation) and `ref` (response correlation).

### Handler Architecture
RPC handlers are split between core and service-owned:

- **Core handlers** (`gilbert.*`) live in `ws_protocol.py` and are registered via `@rpc_handler` decorator into `_rpc_handlers`.
- **Service handlers** live on service classes that declare `ws_handlers` capability and implement `get_ws_handlers() -> dict[str, handler]`. Each handler is a bound method: `async def _ws_foo(self, conn, frame) -> dict | None`.
- `WsConnectionManager.subscribe_to_bus()` merges core + service handlers into a single registry.
- Shared `require_admin()` helper is in `interfaces/ws.py`.

### Handler Ownership
| Namespace | Service | File |
|-----------|---------|------|
| `gilbert.*` | (core) | `web/ws_protocol.py` |
| `chat.*` | AIService | `core/services/ai.py` |
| `roles.*` | AccessControlService | `core/services/access_control.py` |
| `inbox.*` | InboxService | `core/services/inbox.py` |
| `documents.*` | KnowledgeService | `core/services/knowledge.py` |
| `screens.*` | ScreenService | `core/services/screens.py` |
| `dashboard.*`, `system.*`, `entities.*` | WebApiService | `core/services/web_api.py` |

### Authentication
- Cookie (`gilbert_session`) for web UI
- Query param (`?token=<bearer>`) for peers/integrations
- Both validated via existing `auth_svc.validate_session()`
- `gilbert.welcome` frame sent after auth with user_id, roles, subscriptions

### Event Visibility (Role-Based Filtering)
Defaults in `ws_protocol.py` `_EVENT_VISIBILITY` dict — prefix → min role level:
- **everyone (200)**: presence, doorbell, greeting, timer, alarm, screen
- **user (100)**: chat, inbox, knowledge (also catch-all for unlisted)
- **admin (0)**: service, config, acl

Longest prefix match. System user bypasses. Overrides stored in `acl_event_visibility` collection via AccessControlService.

### Filtering Pipeline (per event, per connection)
1. Pattern match against client's subscriptions (`fnmatch`)
2. Role-level visibility check
3. Chat content filter (conversation membership + `visible_to`)

### Subscription Model
- Auto-subscribe to `*` on connect
- Client narrows via `gilbert.sub.remove` + `gilbert.sub.add`

### Peer Publishing
- Requires `peer` role (level 50) or `admin`
- Server prefixes source with `peer:` to prevent spoofing
- Events tagged `_from_peer: true` — skipped when forwarding to peers (loop prevention)

### Key Files
- `src/gilbert/web/ws_protocol.py` — WsConnection, WsConnectionManager, visibility, core `gilbert.*` handlers, RPC dispatch
- `src/gilbert/interfaces/ws.py` — `WsHandlerProvider` protocol, `require_admin()` helper
- `src/gilbert/web/routes/websocket.py` — thin route handler (auth, connect, send/recv loop)
- `src/gilbert/core/services/web_api.py` — WebApiService for dashboard/system/entities handlers
- `frontend/src/hooks/useWebSocket.tsx` — React provider with `send()`, typed frames, ping heartbeat
- `frontend/src/types/events.ts` — frame type definitions
- `tests/unit/test_ws_protocol.py` — tests for core WS protocol

### Connection Lifecycle
connect → auth (cookie/token) → accept → `gilbert.welcome` → auto-subscribe(`*`) → event stream → client pings every 30s → disconnect → cleanup

## Related
- [Event System](memory-event-system.md) — bus that the WS subscribes to
- [Access Control](memory-access-control.md) — role hierarchy used for visibility filtering
- [AI Service](memory-ai-service.md) — chat RPC handlers call ai_svc.chat()
