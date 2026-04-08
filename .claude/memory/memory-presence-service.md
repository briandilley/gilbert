# Presence Service & UniFi Backend

## Summary
User presence detection with polling, event publishing, and AI tools. UniFi implementation aggregates WiFi clients (Network), camera face recognition (Protect), and badge readers (Access) into composite per-user presence states.

## Details

### Interface
- `src/gilbert/interfaces/presence.py` — `PresenceBackend` ABC, `PresenceState` (present/nearby/away/unknown), `UserPresence`

### Service
- `src/gilbert/core/services/presence.py` — `PresenceService`
- Polls via scheduler system timer `presence-poll` (default 30s)
- State tracked in entity store (`user_presence` collection): record exists = here, no record = gone
- Each poll: loads stored IDs, polls backend, diffs → `presence.arrived` for new, `presence.departed` for missing (record deleted)
- Publishes events: `presence.arrived`, `presence.departed`
- Resolves credentials for subsystem controllers and passes full config to backend
- AI tools: `check_presence`, `who_is_here`, `list_all_presence`

### UniFi Backend
- `src/gilbert/integrations/unifi/` — package with 5 modules
- **client.py**: `UniFiClient` — async httpx client with cookie auth, auto re-login on 401, SSL off by default
- **network.py**: `UniFiNetwork` — WiFi client tracking via `/proxy/network/api/s/default/stat/sta`, device-to-person mapping (MAC map → device name → hostname parsing)
- **protect.py**: `UniFiProtect` — camera AI via `/proxy/protect/api/events`, face recognition from `metadata.detectedThumbnails[].group.matchedName`, zone aliases
- **access.py**: `UniFiAccess` — badge events via `/proxy/access/api/v2/device/logs`, direction classification (entry/exit), currently-badged-in computation
- **presence.py**: `UniFiPresenceBackend` — composite aggregator

### Signal Aggregation Priority
1. Badge IN → PRESENT (source: unifi:access)
2. Badge OUT → AWAY (source: unifi:access)
3. Face seen → PRESENT (source: unifi:protect)
4. WiFi connected → NEARBY (source: unifi:network)
5. No signals → AWAY

### Configuration
- `PresenceConfig` + `UniFiControllerConfig` in config.py
- Separate host+credential for network controller (UDM) vs protect/access (UNVR)
- Client deduplication: shared host → shared UniFiClient instance
- Error isolation: each subsystem failure is caught independently

## Related
- `src/gilbert/interfaces/events.py` — EventBus for presence events
- `tests/unit/test_unifi_presence.py` — 36 unit tests
