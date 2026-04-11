# Speaker System

## Summary
Speaker control system with abstract interface and Sonos (SoCo) implementation. Supports discovery, grouping, playback, volume control, aliases, and TTS-powered announcements.

## Details

### Interface
- `src/gilbert/interfaces/speaker.py` — `SpeakerBackend` ABC with data classes: `SpeakerInfo`, `SpeakerGroup`, `PlayRequest`, `PlaybackState`
- Grouping is optional — `supports_grouping` property defaults to `False`; backends override if they support it
- Methods: `list_speakers`, `get_speaker`, `play_uri`, `stop`, `get_volume`, `set_volume`, `list_groups`, `group_speakers`, `ungroup_speakers`

### Sonos Integration
- `src/gilbert/integrations/sonos_speaker.py` — `SonosSpeaker` using the `soco` library
- Smart grouping: checks if speakers are already in desired configuration before regrouping
- Only unjoins target devices — doesn't disrupt unrelated groups
- Group settle time (~2s) with verification and retry logic
- All SoCo calls wrapped with `asyncio.to_thread()` since SoCo is synchronous

### Service
- `src/gilbert/core/services/speaker.py` — `SpeakerService` implementing Service, Configurable, ToolProvider
- Capabilities: `speaker_control`, `ai_tools`
- Requires: `entity_storage` (for aliases)
- Optional: `configuration`, `text_to_speech` (for announce)
- Speaker aliases stored in `speaker_aliases` entity collection with unique index on `alias` field
- Alias collision detection against both existing speaker names and other aliases
- "Last used" speaker tracking — if no speakers specified, reuses previous target set or falls back to all
- `default_announce_speakers` config — a list of speaker names used when no speakers specified in announce call (falls back before "last used" or "all")
- **Announce tool**: generates audio via TTS service (single `voice_id` on TTS backend, no multi-voice), saves to file, groups speakers if needed, plays, then clears the queue after playback so the announcement doesn't linger in history
- Silence padding is handled by the TTS service (`silence_padding` config param on TTSConfig), not the speaker service

### Configuration
- Config model: `SpeakerConfig` in `src/gilbert/config.py`
- YAML section: `speaker:` with `enabled`, `backend`, `default_announce_volume`, `settings`
- `default_announce_speakers` lives in the speaker service settings (array of speaker names)
- TTS config: `tts:` with `enabled`, `backend`, `silence_padding` (seconds, default 3.0), `settings`
- Registered in `app.py` with factory for hot-swap support

### AI Tools Exposed
- `list_speakers`, `play_audio`, `stop_audio`, `set_volume`, `get_volume`
- `set_speaker_alias`, `remove_speaker_alias`
- `announce` (requires TTS service)
- `group_speakers`, `ungroup_speakers`, `list_speaker_groups` (only if backend supports grouping)

## Related
- `src/gilbert/interfaces/tts.py` — TTS interface used by announce feature
- `src/gilbert/core/services/tts.py` — TTS service dependency for announcements
- `tests/unit/test_speaker_service.py` — 29 unit tests
