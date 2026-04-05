# Doorbell Service

## Summary
Detects doorbell ring events from UniFi Protect cameras and publishes `doorbell.ring` events on the event bus. Uses the scheduler for periodic polling.

## Details

### Service
- `src/gilbert/core/services/doorbell.py` — `DoorbellService`
- Requires: `scheduler`, `event_bus`
- Optional: `configuration`, `presence` (gets UniFi Protect from presence backend)
- Registers a system timer `doorbell-poll` at configurable interval (default 5s)

### Ring Detection
- Polls Protect API for `event_types=["ring"]` events
- Tracks `_last_ring_ts` (epoch ms) to only process new rings
- Lookback window slightly longer than poll interval to avoid gaps
- Maps camera names to friendly door names via `doorbell_names` config

### Events Published
- `doorbell.ring` — data: `{door, camera, timestamp}`

### Configuration
- `DoorbellConfig` in config.py: `enabled`, `poll_interval_seconds`, `doorbell_names`
- `doorbell_names` maps UniFi camera names to friendly names (e.g., "G4 Doorbell" → "Front Door")

## Related
- [Scheduler Service](memory-scheduler-service.md) — runs the polling timer
- [Presence Service](memory-presence-service.md) — provides the UniFi Protect backend
- `tests/unit/test_doorbell_service.py` — 7 tests
