# Radio DJ Service

## Summary
Context-aware music DJ that selects genres based on who's present, learns user preferences (likes/vetoes) over time, and rotates through default genres on cold start.

## Details
- **Service file:** `src/gilbert/core/services/radio_dj.py` — `RadioDJService`
- **Capabilities:** `radio_dj`, `ai_tools`
- **Required deps:** `music`, `speaker_control`, `scheduler`
- **Optional deps:** `presence`, `entity_storage`, `event_bus`, `configuration`
- **Config model:** `RadioDJConfig` in `src/gilbert/config.py`, namespace `radio_dj`
- **Config defaults in:** `gilbert.yaml` under `radio_dj:` section

### Genre Selection Algorithm
1. Gather likes from all present users → vote counter
2. Gather vetoes from all present users → exclusion set
3. Highest-voted non-vetoed genre wins
4. If all voted genres vetoed → fall back to default rotation (skipping vetoed)
5. If no preferences → cold-start rotation through `default_genres`
6. Throttle: `min_switch_interval` minutes between auto switches (bypassed on arrivals)

### Storage Collections (namespaced `radio_dj.*`)
- `preferences` — per-user likes/vetoes (`prefs:{user_id}`)
- `state` — DJ state persistence across restarts (`dj_state`)

### Events
- Subscribes to: `presence.arrived`, `presence.departed`
- Emits: `radio_dj.started`, `radio_dj.stopped`, `radio_dj.genre_changed`, `radio_dj.track_liked`, `radio_dj.track_vetoed`

### AI Tools
`radio_start`, `radio_stop`, `radio_request`, `radio_skip`, `radio_like`, `radio_dislike`, `radio_veto`, `radio_status`, `radio_set_preferences` (admin only)

### Scheduler Job
`radio-dj-poll` — system job running every `poll_interval` seconds, checks presence and rotates genres.

## Related
- `src/gilbert/core/services/music.py` — music search and playback
- `src/gilbert/core/services/speaker.py` — speaker control
- `src/gilbert/core/services/presence.py` — presence detection
- `tests/unit/test_radio_dj.py` — 44 unit tests
