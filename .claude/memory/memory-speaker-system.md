# Speaker System

## Summary
Speaker control with an abstract interface and a Sonos implementation backed by `aiosonos` (S2 local WebSocket API). Supports discovery, grouping, playback, volume, aliases, and native short-clip announcements. SoCo (the legacy UPnP/SMAPI library) was removed in the aiosonos migration — S1 speakers are no longer supported.

## Details

### Interface
- `src/gilbert/interfaces/speaker.py` — `SpeakerBackend` ABC with data classes: `SpeakerInfo`, `SpeakerGroup`, `PlayRequest`, `PlaybackState`, `NowPlaying`.
- `PlayRequest` has an `announce: bool = False` flag. When true the Sonos backend routes the request to `audio_clip` (duck + play + auto-restore); when false it uses `play_stream_url` / `load_content`.
- Grouping is optional — `supports_grouping` property defaults to `False`; the Sonos backend overrides to `True`.
- Methods: `list_speakers`, `get_speaker`, `play_uri`, `stop`, `get_volume`, `set_volume`, `list_groups`, `group_speakers`, `ungroup_speakers`.
- Transport introspection: `get_playback_state(speaker_id)` returns a `PlaybackState`; `get_now_playing(speaker_id)` returns a `NowPlaying` (state + title/artist/album/album_art_url/uri/duration_seconds/position_seconds). Both default to "stopped / no metadata" — Sonos overrides both, following the group coordinator for the authoritative playing track.
- Legacy `snapshot(speaker_ids)` / `restore(speaker_ids)` methods are kept on the interface for backward compatibility but are **no-ops** on the aiosonos-based Sonos backend. `audio_clip` self-restores; callers that used the snapshot dance should set `PlayRequest.announce=True` instead.

### Sonos Backend
- `std-plugins/sonos/sonos_speaker.py` — `SonosSpeaker` using `aiosonos` (S2 local WebSocket API on port 1443).
- **Discovery**: zeroconf watches `_sonos._tcp.local.`. On each service-add event we probe `https://<ip>:1443/api/v1/players/local/info` for identity (playerId, householdId, name, model), then open an `aiosonos.SonosLocalApiClient` per discovered player. The client's `start_listening()` coroutine runs as a per-player task for the lifetime of the backend.
- **Grouping**: declarative — `group.set_group_members(player_ids)` replaces the whole group atomically. No UPnP 800 "state machine rejects" retry logic needed; no per-speaker join/unjoin dance. `_ensure_group` no-ops when membership already matches the target set.
- **Announce path**: `PlayRequest.announce=True` → `player.play_audio_clip(url, volume, name)`. Sonos ducks current music, plays the clip, and restores automatically. No Snapshot/restore ritual, no TTL cleanup timing. Multi-speaker announces fan out with `asyncio.gather`.
- **HTTP URL playback**: `group.play_stream_url(url)`. aiosonos/Sonos negotiates MIME natively — no DIDL wrangling, no UPnP 714 "Illegal MIME-Type" (those were legacy SoCo footguns).
- **Spotify URIs** (`spotify:track:…`, `spotify:playlist:…`, etc.) are detected via `_extract_spotify_ref` and routed through `playback.load_content` with a `MetadataId` of `{serviceId: "9", objectId: <uri>}` — `accountId` is intentionally omitted so Sonos resolves the household's default linked Spotify account.
- **State mapping**: aiosonos's `PLAYBACK_STATE_*` strings map to our `PlaybackState` enum via `_PLAYBACK_STATE_MAP`.
- `scripts/check_sonos_s2.py` is the migration-preflight tool: it uses zeroconf + the info endpoint to verify every LAN speaker speaks S2.

### Service
- `src/gilbert/core/services/speaker.py` — `SpeakerService` implementing Service, Configurable, ToolProvider.
- Capabilities: `speaker_control`, `ai_tools`.
- Requires: `entity_storage` (for aliases).
- Optional: `configuration`, `text_to_speech` (for announce).
- Speaker aliases stored in `speaker_aliases` entity collection with unique index on `alias` field. Alias collision detection against both existing speaker names and other aliases.
- "Last used" speaker tracking — if no speakers specified, reuses previous target set or falls back to all.
- `default_announce_speakers` config — list of speaker names used when no speakers are specified in an announce call (falls back before "last used" or "all").
- **Announce flow**: SpeakerService.announce() generates TTS audio, writes to a workspace file, then calls `play_on_speakers(..., announce=True)`. The speaker backend's announce route (`audio_clip`) handles duck+play+restore. Silence padding is still handled by the TTS service (`silence_padding` config param on TTSConfig), not here.

### Configuration
- Config model: `SpeakerConfig` in `src/gilbert/config.py`.
- YAML section: `speaker:` with `enabled`, `backend`, `default_announce_volume`, `settings`.
- `default_announce_speakers` lives in the speaker service settings (array of speaker names).
- TTS config: `tts:` with `enabled`, `backend`, `silence_padding` (seconds, default 3.0), `settings`.
- Registered in `app.py` with factory for hot-swap support.

### AI Tools Exposed
- `list_speakers`, `play_audio`, `stop_audio`, `set_volume`, `get_volume`
- `set_speaker_alias`, `remove_speaker_alias`
- `announce` (requires TTS service)
- `group_speakers`, `ungroup_speakers`, `list_speaker_groups` (only if backend supports grouping — Sonos does)

## Related
- `src/gilbert/interfaces/tts.py` — TTS interface used by announce feature.
- `src/gilbert/core/services/tts.py` — TTS service dependency for announcements.
- `std-plugins/sonos/tests/test_sonos_speaker.py` — 21 tests covering the aiosonos wiring.
- `tests/unit/test_speaker_service.py` — service-layer unit tests.
- `scripts/check_sonos_s2.py` — S2 preflight check.
