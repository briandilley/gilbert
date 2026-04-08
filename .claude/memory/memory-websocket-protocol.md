# WebSocket Protocol

## Summary
Bidirectional WebSocket at `/ws/events` with typed message frames, role-based event filtering, chat RPC, and peer publishing. Primary real-time channel for web UI, inter-Gilbert communication, and external integrations.

## Details

### Wire Protocol
All frames are JSON with a `type` field using `namespace.resource.verb` naming. Optional `id` (request correlation) and `ref` (response correlation).

### Core Frames (`gilbert.*`)
- **Client → Server**: `gilbert.sub.add`, `gilbert.sub.remove`, `gilbert.sub.list`, `gilbert.ping`, `gilbert.peer.publish`
- **Server → Client**: `gilbert.event` (wrapped bus events), `gilbert.welcome`, `gilbert.pong`, `gilbert.error`, `*.result` acks

### Chat Frames (`chat.*`)
- **Client → Server**: `chat.message.send`, `chat.form.submit`, `chat.history.load`
- **Server → Client**: `chat.message.send.result`, `chat.form.submit.result`, `chat.history.load.result`

### Authentication
- Cookie (`gilbert_session`) for web UI
- Query param (`?token=<bearer>`) for peers/integrations
- Both validated via existing `auth_svc.validate_session()`
- `gilbert.welcome` frame sent after auth with user_id, roles, subscriptions

### Event Visibility (Role-Based Filtering)
Defaults in `ws_protocol.py` `_EVENT_VISIBILITY` dict — prefix → min role level:
- **everyone (200)**: presence, doorbell, greeting, timer, alarm, screen
- **user (100)**: chat, radio_dj, inbox, knowledge (also catch-all for unlisted)
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
- `src/gilbert/web/ws_protocol.py` — WsConnection, WsConnectionManager, visibility, frame handlers, RPC dispatch
- `src/gilbert/web/routes/websocket.py` — thin route handler (auth, connect, send/recv loop)
- `src/gilbert/core/services/access_control.py` — event visibility overrides + AI tools
- `frontend/src/hooks/useWebSocket.tsx` — React provider with `send()`, typed frames, ping heartbeat
- `frontend/src/types/events.ts` — frame type definitions
- `tests/unit/test_ws_protocol.py` — 31 tests

### Connection Lifecycle
connect → auth (cookie/token) → accept → `gilbert.welcome` → auto-subscribe(`*`) → event stream → client pings every 30s → disconnect → cleanup

## Related
- [Event System](memory-event-system.md) — bus that the WS subscribes to
- [Access Control](memory-access-control.md) — role hierarchy used for visibility filtering
- [AI Service](memory-ai-service.md) — chat RPC handlers call ai_svc.chat()
