# Speaker System

## Summary
Speaker control with an abstract interface and three bundled backends: `sonos` (third-party plugin, S2 WebSocket via `aiosonos`), `local` (vendor-free, plays through the host's audio output via a CLI player subprocess), and `browser` (vendor-free, plays in the requesting user's SPA tab via bus events scoped per-user). Supports discovery, grouping (Sonos only), playback, volume, aliases, and short-clip announcements. SoCo (the legacy UPnP/SMAPI library) was removed in the aiosonos migration — S1 speakers are no longer supported.

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

### Local Backend
- `src/gilbert/integrations/local_speaker.py` — `LocalSpeakerBackend` (`backend_name = "local"`). Vendor-free, lives in `integrations/` per the layer rules; the speaker service side-effect-imports it so the `local` choice shows up alongside `sonos` in the backend dropdown.
- Exposes a **single virtual speaker** (`speaker_id = "local"`, name configurable via `display_name`). `list_speakers` always returns one entry. No grouping (`supports_grouping = False`), no repeat (`supports_repeat = False`).
- **Player selection**: `player_command` config param overrides auto-detect. Auto-detect prefers `afplay` on macOS, then `ffplay`, `mpv`, `mpg123`. `initialize()` raises if none are found and the user didn't pre-configure one.
- **Playback**: `play_uri` streams the request's HTTP(S) URI to a temp file with `httpx` (most CLI players can't read URLs directly), then spawns the player via `asyncio.create_subprocess_exec`. The temp file is deleted when the subprocess exits or `stop()` is called.
- **Volume** is per-clip via the player's CLI flag (`afplay -v 0–1`, `ffplay -volume 0–100`, `mpv --volume=0–100`, `mpg123 -f 0–32768`). The host's system mixer is never touched. `set_volume` stores the level and applies it to the *next* clip; the active subprocess is not adjusted mid-clip.
- **Announce**: same code path as normal playback — the upstream `_announce_inner` flow still works because it polls `_estimate_mp3_duration` and then calls `restore()`, which is a no-op here. The backend has no ducking/restore concept; if music was playing, it's stopped and replaced by the announcement, then the speaker is silent until the next `play_uri`.
- **State**: `_state` flips to `PLAYING` when the subprocess spawns and back to `STOPPED` from a fire-and-forget `_watch_proc` task when the player exits. `get_playback_state` also resynchronizes lazily if the proc has exited without the watcher running yet.
- No additional Python deps — uses `httpx` (already in core), `asyncio.subprocess`, and `shutil.which`. Cross-platform: works on any OS that has one of the supported CLI players on PATH.

### Browser Backend
- `src/gilbert/integrations/browser_speaker.py` — `BrowserSpeakerBackend` (`backend_name = "browser"`). Vendor-free; side-effect imported in `SpeakerService.start()` and `config_params()`. Targeted at homelab / headless deployments where the box running Gilbert has no audio output.
- Each authenticated user's connected SPA tab acts as their private speaker. `list_speakers` reads `get_current_user()` from the contextvar and returns a single `SpeakerInfo(speaker_id="browser:<user_id>")` entry — per the multi-user isolation rule, the backend holds no per-user state; identity comes from the request context.
- **Capability injection**: implements the `EventBusAwareSpeakerBackend` protocol (in `interfaces/speaker.py`) — SpeakerService calls `set_event_bus_provider(...)` between construction and `initialize()` (same shape as TTS's `AICapableTTSBackend` pattern). Without a bus, `play_uri` raises so wiring problems surface at the call site, not silently.
- **Playback**: `play_uri` publishes a `speaker.browser.play` event with `data={user_id, conversation_id, url, title, volume, announce, position_seconds}`. `stop` publishes `speaker.browser.stop` with `data={user_id}`. Per-user routing is enforced at two layers:
  1. The backend rejects cross-user targets (`PermissionError` if `caller != target`) — strict policy until admin broadcasting is added.
  2. `WsConnection.can_see_speaker_browser_event` (in `web/ws_protocol.py`) delivers `speaker.browser.*` frames only to the connection whose `user_id == event.data["user_id"]`. Added to the `_dispatch_event` filter chain alongside `can_see_notification_event`.
- **Event ACL**: `"speaker.browser.": 100` (user-level) in `DEFAULT_EVENT_VISIBILITY` (in `interfaces/acl.py`). User-level prefix permission + per-connection user_id narrowing is the same shape as `notification.*`.
- **Volume**: per-clip via `request.volume`. `get_volume` returns the configured default; `set_volume` is a no-op because we can't reach into the browser's HTMLAudioElement after the fact (and don't need to — the next clip carries the new volume).
- **URL rewriting** (`to_browser_url`): module-level helper in `interfaces/speaker.py`. `SpeakerService._audio_url()` mints absolute URLs hardcoded to `http://<LAN-IP>:<web-port>/output/...` — correct for Sonos, broken for a browser that loaded Gilbert via HTTPS through a reverse proxy (mixed-content block, or HTTPS-upgrade → TLS handshake against plaintext port → `SSL_ERROR_RX_RECORD_TOO_LONG`). Before publishing the event, the helper strips scheme + host from URLs whose path starts with `/output/`, so the SPA resolves them against `window.location.origin` instead. External URLs (free-form `play_audio` calls pointing at e.g. a podcast) are left absolute — the `/output/` prefix is the heuristic for "ours." Shared by both `BrowserSpeakerBackend.play_uri` and `SpeakerService._maybe_echo_to_browser` (browser-echo fan-out).

### Per-user browser-echo fan-out
- Lets a user pick "Sonos AND my browser tab" without having to switch the primary speaker backend. Toggle lives in the chat top-bar (headphones icon next to "New chat"), backed by `frontend/src/hooks/useBrowserEchoPref.tsx`.
- Storage: per-user `metadata["speaker.browser_echo"]` boolean. Read/write via the generic `UserPrefReader` protocol (`interfaces/users.py`) — `get_user_pref(user_id, key, default)` / `set_user_pref(user_id, key, value)`. UserService implements both.
- WS RPCs: `users.prefs.get` / `users.prefs.set`. Self-only — the handler reads the connection's authenticated `user_id` and ignores any frame-level identity, so a compromised SPA frame can't flip another user's prefs.
- SpeakerService wiring: `users` and `event_bus` added to `optional` capabilities in `service_info()`. `start()` stashes both as `self._users_svc` / `self._event_bus_provider`; missing either makes the fan-out silently no-op.
- Fan-out hook: `SpeakerService.play_on_speakers` calls `_maybe_echo_to_browser` after the primary backend's `play_uri`. `stop_speakers` calls `_maybe_echo_stop_to_browser` after the primary `stop`. Gate (`_browser_echo_should_fire`): bus + users svc present, primary backend isn't `browser` (would double-play), authenticated non-`system` user, pref evaluates true. Any failure (pref lookup throws, bus publish errors) is logged at debug and swallowed — the primary play already succeeded.
- Event source string is `speaker.echo` (vs. `speaker.browser` when BrowserSpeakerBackend itself emits) so subscribers can tell the two paths apart if they care.
- **SPA wiring**: `frontend/src/hooks/useBrowserSpeaker.tsx` exposes a `BrowserSpeakerProvider` (mounted at `main.tsx` inside `WebSocketProvider`) that subscribes to `speaker.browser.play` / `speaker.browser.stop` via `useEventBus`. It maintains a per-conversation history (bounded at 25 clips) and auto-plays each arrival via a single `Audio` element so a new clip preempts the previous one (autoplay failure is logged but non-fatal — the bubble's own `<audio controls>` lets the user retry). `frontend/src/components/chat/BrowserAudioBubbles.tsx` renders each clip as an inline audio bubble with `<audio controls>` at the tail of `MessageList`, scoped to the active conversation id.
- **Announce flow compat**: `_announce_inner` in `SpeakerService` writes the TTS file, calls `backend.snapshot` (no-op for browser), `play_on_speakers(announce=True)` (publishes the event), sleeps `_estimate_mp3_duration(audio)` seconds, then `backend.restore` (also no-op). The duration sleep is enough for the SPA to finish playing.

### Service
- `src/gilbert/core/services/speaker.py` — `SpeakerService` implementing Service, Configurable, ToolProvider.
- Capabilities: `speaker_control`, `ai_tools`.
- Requires: `entity_storage` (for aliases).
- Optional: `configuration`, `text_to_speech` (for announce).
- Speaker aliases stored in `speaker_aliases` entity collection with unique index on `alias` field. Alias collision detection against both existing speaker names and other aliases.
- "Last used" speaker tracking — if no speakers specified, reuses previous target set or falls back to all.
- `default_announce_speakers` config — list of speaker names used when no speakers are specified in an announce call (falls back before "last used" or "all").
- **Announce flow**: SpeakerService.announce() generates TTS audio, writes to a workspace file, then calls `play_on_speakers(..., announce=True)`. The speaker backend's announce route (`audio_clip`) handles duck+play+restore. Silence padding is still handled by the TTS service (`silence_padding` config param on TTSConfig), not here.
- **Per-speaker announce locks**: Announcements are serialized *per target speaker*, not globally. `_speaker_locks: dict[str, asyncio.Lock]` holds one lock per speaker ID, created lazily under `_speaker_locks_guard`. `announce()` resolves target IDs first, then acquires every target's lock in sorted-ID order (deadlock-free under overlapping sets) via `contextlib.AsyncExitStack` before calling `_announce_inner`. Result: two announces on disjoint speaker sets fan out concurrently; overlapping sets still serialize on the shared speaker. The `announce` ToolDefinition is flagged `parallel_safe=True` so the AI execution loop can `asyncio.gather` N announce calls, and the per-speaker locks handle the correctness guarantee underneath.

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
- `tests/unit/test_local_speaker.py` — LocalSpeakerBackend unit tests (subprocess + httpx mocked).
- `tests/unit/test_browser_speaker.py` — BrowserSpeakerBackend unit tests (stub bus, contextvar-driven user identity).
- `tests/unit/test_ws_protocol.py` — `TestBrowserSpeakerFiltering` covers the per-user `can_see_speaker_browser_event` filter.
- `scripts/check_sonos_s2.py` — S2 preflight check.
