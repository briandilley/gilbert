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
- AI tools: `check_presence`, `who_is_here`, `list_all_presence`

### UniFi Backend
- `std-plugins/unifi/` — plugin package (a `std-plugins` submodule member), 6 modules
- **client.py**: `UniFiClient` — async httpx client with cookie auth, auto re-login on 401, SSL off by default
- **network.py**: `UniFiNetwork` — WiFi client tracking via `/proxy/network/api/s/default/stat/sta`, device-to-person mapping (MAC map → device name → hostname parsing)
- **protect.py**: `UniFiProtect` — camera AI via `/proxy/protect/api/events`, face recognition from `metadata.detectedThumbnails[].group.matchedName`, zone aliases
- **access.py**: `UniFiAccess` — badge events via `/proxy/access/api/v2/device/logs`, direction classification (entry/exit), currently-badged-in computation
- **presence.py**: `UniFiPresenceBackend` — composite aggregator

### Signal Aggregation Priority
1. Badge IN → PRESENT (source: unifi:access)
2. Badge OUT → AWAY (source: unifi:access)
3. Face seen → PRESENT (source: unifi:protect)
4. WiFi phone connected → PRESENT (source: unifi:network) — only `dev_family`
   values in `_PHONE_DEV_FAMILIES` (iPhone/Samsung/Android) count, so a laptop
   or IoT device on the network never marks someone present
5. No signals → AWAY

### Configuration
Lives in the `gilbert.config` entity collection under the `presence` id (managed
at Settings → Monitoring → Presence), **not** in `gilbert.yaml` — the YAML block
below is the shape, not the storage location.
- UniFi credentials inline in config: `host`, `username`, `password`, `verify_ssl` — no CredentialService
- `unifi_network.api_key` authenticates with `X-API-KEY` instead of a cookie login and
  takes precedence over username/password. Mint it in the Network app under
  Settings → Control Plane → Integrations. Protect/Access remain username/password.
- Separate controller configs for network (UDM/gateway console) vs protect/access (UNVR)
- Client deduplication: shared host → shared UniFiClient instance
- Error isolation: each subsystem failure is caught independently

```yaml
presence:
  enabled: false
  backend: unifi
  poll_interval_seconds: 30
  unifi_network:
    host: ""
    api_key: ""        # preferred; wins over username/password
    username: ""
    password: ""
  unifi_protect:
    host: ""
    username: ""
    password: ""
  device_person_map: {}
  zone_aliases: {}
  face_lookback_minutes: 30
  badge_lookback_hours: 24
```

## Related
- `src/gilbert/interfaces/events.py` — EventBus for presence events
- `std-plugins/unifi/tests/` — `test_unifi_presence.py`, `test_unifi_doorbell.py`,
  `test_unifi_client_auth.py` (client auth modes)
