# Doorbell Service

## Summary
Detects doorbell ring events via a pluggable `DoorbellBackend` and announces over speakers. Publishes `doorbell.ring` events on the event bus.

## Details

### Architecture
- **Interface:** `src/gilbert/interfaces/doorbell.py` — `DoorbellBackend` ABC with `initialize()`, `close()`, `get_ring_events()`
- **Implementation:** `src/gilbert/integrations/unifi/doorbell.py` — `UniFiProtectDoorbellBackend` (creates its own UniFi Protect client, independent of presence service)
- **Service:** `src/gilbert/core/services/doorbell.py` — `DoorbellService(backend)`

### Service
- Requires: `scheduler`, `event_bus`
- Optional: `configuration`, `credentials`, `speaker_control`, `text_to_speech`
- Registers a system timer `doorbell-poll` at configurable interval (default 5s)
- Resolves credentials for the backend during `start()` (same pattern as presence service)

### Ring Detection
- Polls backend for ring events with 10-second lookback window
- Tracks `_last_ring_ts` (epoch ms) to only process new rings
- Maps camera names to friendly door names via `doorbell_names` config

### Announcements
- Announces "Someone is at the {door_name}." via SpeakerService
- Configurable `speakers` list and `voice_name`

### Events Published
- `doorbell.ring` — data: `{door, camera, timestamp}`

### Configuration
- `DoorbellConfig` in config.py: `enabled`, `backend`, `poll_interval_seconds`, `doorbell_names`, `speakers`, `voice_name`
- Backend config nested under `unifi_protect: {host, credential}`
- `doorbell_names` maps camera names to friendly names (e.g., "G4 Doorbell Pro" -> "Front Door")

## Related
- [Scheduler Service](memory-scheduler-service.md) — runs the polling timer
- `tests/unit/test_doorbell_service.py` — 9 tests
