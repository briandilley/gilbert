# Doorbell Service

## Summary
Detects doorbell ring events via a pluggable `DoorbellBackend` and announces over speakers. Publishes `doorbell.ring` events on the event bus.

## Details

### Architecture
- **Interface:** `src/gilbert/interfaces/doorbell.py` — `DoorbellBackend` ABC with `initialize()`, `close()`, `get_ring_events()`, `list_doorbell_names()`
- **Implementation:** `src/gilbert/integrations/unifi/doorbell.py` — `UniFiProtectDoorbellBackend` (creates its own UniFi Protect client, independent of presence service)
- **Service:** `src/gilbert/core/services/doorbell.py` — `DoorbellService(backend)`

### Service
- Requires: `scheduler`, `event_bus`
- Optional: `configuration`, `credentials`, `speaker_control`, `text_to_speech`
- Registers a system timer `doorbell-poll` at configurable interval (default 5s)

### Ring Detection
- Polls backend for ring events with 10-second lookback window
- Tracks `_last_ring_ts` (epoch ms) to only process new rings
- Filters by `doorbell_names` from backend settings (selected doorbells to monitor)

### Announcements
- Announces "Someone is at the {door_name}." via SpeakerService

### Events Published
- `doorbell.ring` — data: `{door, camera, timestamp}`

### UniFi Backend
- Standard backend pattern with settings passed via `initialize(config)`
- Config params: `host`, `username`, `password` (credentials inline, no CredentialService), `doorbell_names`
- `doorbell_names` is an array of camera names to monitor (empty = all). Uses `choices_from="doorbells"` for dynamic choices resolved from the backend's `list_doorbell_names()`.

### Configuration
```yaml
doorbell:
  enabled: false
  backend: unifi
  poll_interval_seconds: 5.0
  speakers: []
  settings:
    host: ""
    username: ""
    password: ""
    doorbell_names: []   # array of camera names to monitor (empty = all)
```

## Related
- [Scheduler Service](memory-scheduler-service.md) — runs the polling timer
- `tests/unit/test_doorbell_service.py` — unit tests
