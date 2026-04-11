# Music Service

## Summary
Music search, metadata, and playback service with abstract interface and Spotify Web API implementation. Integrates with the speaker system for playback with seek support.

## Details

### Interface
- `src/gilbert/interfaces/music.py` — `MusicBackend` ABC with rich data classes
- Data types: `TrackInfo`, `AlbumInfo`, `ArtistInfo`, `PlaylistInfo`, `PlaylistDetail`, `SearchResults`
- `TrackInfo` includes: name, artists, album (with art URL), duration_seconds, track_number, uri, external_url, preview_url, explicit flag
- Methods: `search`, `get_track`, `get_album`, `get_album_tracks`, `get_playlist`, `get_playable_uri`

### Spotify Integration
- `src/gilbert/integrations/spotify_music.py` — `SpotifyMusic` using httpx against the Spotify Web API
- Credentials inline in backend settings: `client_id`, `client_secret` (no external CredentialService)
- Uses OAuth2 client credentials flow (no user auth needed for search/metadata)
- Auto-refreshes tokens before expiry
- Returns `spotify:track:xxx` URIs that Sonos can play natively via SoCo

### Service
- `src/gilbert/core/services/music.py` — `MusicService` implementing Service, Configurable, ToolProvider
- Capabilities: `music`, `ai_tools`
- Optional: `configuration`, `speaker_control`
- `play_track` method integrates with SpeakerService — resolves URIs, speaker names, and supports `position_seconds` for seek

### Speaker Position Support
- `PlayRequest.position_seconds` field added to speaker interface
- Sonos implementation seeks after play_uri using HH:MM:SS format
- SpeakerService `play_audio` tool also accepts `position_seconds`

### AI Tools Exposed
- `search_music` — search tracks, albums, playlists
- `get_track_info` — full track metadata with album art, duration, links
- `get_album_info` — album metadata with optional track listing
- `get_playlist` — playlist with tracks
- `play_track` — play on speakers with optional position_seconds and volume

### Configuration
```yaml
music:
  enabled: false
  backend: spotify
  settings:
    client_id: ""      # Spotify app client ID
    client_secret: ""  # Spotify app client secret
```

## Related
- [Speaker System](memory-speaker-system.md) — playback target for music
- `tests/unit/test_music_service.py` — unit tests
