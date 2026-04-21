# Music Service

## Summary
Music search, browse, and playback. The default `MusicBackend "sonos"` was rewritten as part of the aiosonos migration — it now talks to Spotify's Web API directly for search/browse and hands resolved Spotify URIs to the speaker backend, which renders through the speaker's own linked Spotify account. SMAPI (legacy Sonos-proxied search) and soco are gone.

## Details

### Architecture
Search and browse are **Spotify concerns**, not Sonos concerns. The modern Sonos mobile app itself talks directly to Spotify's cloud API for library views, then tells speakers what to play — we follow the same split:

1. **Gilbert↔Spotify (browse/search)** — one Spotify OAuth token registered against a Spotify developer app. Search, user playlists, liked songs.
2. **Sonos speaker↔Spotify (playback)** — the speaker's own linked Spotify account (configured in the Sonos mobile app). Gilbert hands it a URI; it streams via its binding.

Both links coexist independently. Users typically link one Spotify account to Gilbert (usually the household's "music curator") and can still play on speakers linked to a different Spotify family-plan member's account — Spotify URIs are universal.

### Interface
- `src/gilbert/interfaces/music.py` — `MusicBackend` ABC, `MusicItem`, `Playable`, `MusicItemKind` (TRACK / ALBUM / ARTIST / PLAYLIST / STATION / FAVORITE), `MusicSearchUnavailableError`.
- Methods: `list_favorites`, `list_playlists`, `search(query, kind, limit)`, `resolve_playable(item)`.
- `supports_queue: bool` class attribute (default `False`) — backends declare whether they can route resolved items through a speaker queue. `MusicService.supports_queue` mirrors it and gates the queue tools.
- `LinkedMusicServiceLister` protocol — `list_linked_services()` used by `ConfigurationService` to drive the `preferred_service` dropdown.

### Backend (Spotify Web API)
- `std-plugins/sonos/sonos_music.py` — `SonosMusic`, still named "sonos" for config-schema compatibility even though browse/search hits Spotify directly.
- Uses Spotify's Web API at `api.spotify.com/v1`:
  - `GET /search?q=…&type=track|album|artist|playlist` — search.
  - `GET /me/tracks` — user's Liked Songs (exposed as `list_favorites`).
  - `GET /me/playlists` — user's playlists (exposed as `list_playlists`).
  - `GET /me` — used by `test_connection` to verify the token.
- OAuth: standard Authorization Code flow. Access tokens refresh automatically via the stored refresh token, margin ~5 min before expiry. `_SpotifyClient` handles token lifecycle.
- Item mappers (`_spotify_*_to_music_item`) normalize Spotify JSON into `MusicItem`. The returned `MusicItem.uri` is always a canonical `spotify:<kind>:<id>` string.
- `resolve_playable(item)` passes the Spotify URI straight through as a `Playable(uri=…)`. The speaker backend's `play_uri` detects the spotify: scheme and routes to `playback.load_content` with a `MetadataId{serviceId: "9", objectId: uri}` — Sonos uses the household's default linked Spotify account.
- Station queries (no such thing in Spotify proper) map to `type=playlist` so `/music search stations` surfaces editorial playlists, the closest analogue.
- Apple Music / Amazon Music / etc. are **not supported** — they required SMAPI and went away with it.

### Link flow (manual-paste OAuth)
Two `ConfigAction`s expose the flow to the Settings UI:
- **`link_spotify`** — generates an authorize URL containing `client_id`, `redirect_uri`, and a CSRF `state` nonce. Returns it as `open_url`.
- **`link_spotify_complete`** — reads the auth code out of the `spotify_auth_code` config field (the user pasted it after approving in Spotify), exchanges it for access + refresh tokens, persists the refresh token into settings via the `persist` side-channel, and auto-clears the paste field.

`_extract_auth_code` parses whatever the user pasted — a full redirect URL (`https://localhost:8000/callback?code=…`), a query fragment (`?code=…`), or a bare code.

### Config
- `client_id` — Spotify app client ID (from the Spotify Developer Dashboard).
- `client_secret` *(sensitive)* — matching Spotify app client secret.
- `redirect_uri` — must match one registered on the Spotify app exactly; default `https://localhost:8000/callback`. Spotify requires `https://` for named hosts (plain `http://localhost:…` is rejected as "Insecure"). Users can alternatively register a numeric-loopback form like `http://127.0.0.1:8000/callback` if they prefer plain HTTP. The endpoint doesn't need to actually respond — Spotify only validates the URL format at authorize time and we parse the code out of the URL the user pastes.
- `refresh_token` *(sensitive)* — auto-populated by the link flow.
- `spotify_auth_code` — transient, cleared by `link_spotify_complete`.
- Legacy fields retained for backward compat but ignored: `preferred_service`, `auth_token`, `auth_key` (the old SMAPI token was speaker-bound and isn't transferable to the Web API; users must re-run the link flow after upgrade).

### Service
- `src/gilbert/core/services/music.py` — `MusicService` implementing Service, Configurable, ToolProvider.
- Wraps the backend; no direct Spotify knowledge lives here.
- `play_item(item, speaker_names, volume)` calls `backend.resolve_playable(item)` then `speaker_svc.play_on_speakers(uri=playable.uri, ...)`. The speaker backend handles the Spotify-specific `load_content` dispatch.

### AI Tools Exposed
- `list_favorites`, `list_playlists` — browse user's Spotify library.
- `search_music` (+ `/music search <query>`) — Spotify search across kinds.
- `play_music` — resolve + play a search result or library item.
- `play_item` — button-invoked sibling of `play_music` that takes a JSON-encoded MusicItem payload.
- `add_to_queue` (+ `/music queue <title>`) — resolve + append to the speaker queue without stopping playback. **Only exposed when the active backend sets `supports_queue = True`.** Routes through `SpeakerService.enqueue_on_speakers`, which delegates to `SpeakerBackend.enqueue_uri` (default raises `NotImplementedError`; `SonosSpeaker` overrides to call `SonosSmapiClient.enqueue_spotify` — pure `AddURIToQueue` + idempotent `SetAVTransportURI`, no clear/seek/play).
- `queue_item` — button-invoked sibling of `add_to_queue` (same JSON payload shape as `play_item`).
- `now_playing` — queries the speaker backend for current track.

## Related
- `src/gilbert/interfaces/music.py` — `MusicBackend` ABC.
- `std-plugins/sonos/sonos_music.py` — Spotify Web API backend.
- `std-plugins/sonos/tests/test_sonos_music.py` — 21 tests covering Spotify JSON mapping, the link flow, and `resolve_playable`.
- [Speaker System](memory-speaker-system.md) — the aiosonos speaker backend that actually plays the Spotify URIs this backend resolves.
