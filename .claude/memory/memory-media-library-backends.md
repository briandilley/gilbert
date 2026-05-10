# Plex / Jellyfin Media-Library Backends

## Summary
Per-backend gotchas for the two `MediaLibraryBackend` implementations
shipping with Gilbert: `std-plugins/plex/` (wraps `plexapi`) and
`std-plugins/jellyfin/` (REST direct via `httpx`). Both register
themselves via the universal Backend ABC pattern; the
`MediaLibraryService` aggregator picks them up from the registry.

## Details

### Plex (`std-plugins/plex/`)

Synchronous `plexapi` wrapped in `asyncio.to_thread` to keep the
event loop unblocked. `plexapi.PlexServer.__init__` performs a
`/identity` HTTP round-trip on construction, so every per-Home-user
`PlexServer` is **memoized** alongside the token cache.

**Per-Plex-Home-user state lives on the backend instance**, keyed by
the Plex Home user uuid (NOT by Gilbert user id — the cache is
keyed by *backend* identity so it can't leak across Gilbert users):

- `self._user_tokens: dict[backend_user_id, str]` — Plex Home user
  uuid → X-Plex-Token.
- `self._user_servers: dict[backend_user_id, PlexServer]` — memoized
  per-Home-user `PlexServer` instance.
- `self._user_locks: dict[backend_user_id, asyncio.Lock]` —
  per-Home-user lock so two concurrent calls for the *same* Home
  user serialize, two for *different* Home users do not. A second,
  very short global lock (`self._user_locks_dict_lock`) guards only
  the `dict.setdefault` of the per-user lock itself. A single global
  lock around the whole token cache is the explicit Appendix C
  anti-pattern — it serializes every per-user fetch across every
  Gilbert user.

**Token rotation** (admin re-runs `link_account`): `initialize()`
detects a different `account_token` and atomically clears all three
per-Home-user dicts before re-pinning the chosen `PlexServer`. This
avoids the failure mode where a stale Home-user token cache
references uuids that no longer resolve under the new admin token.

**Authorization failure** (`plexapi.exceptions.Unauthorized`): the
backend translates to `MediaLibraryUnavailableError("Plex token
revoked")`, evicts the offending per-Home-user cache entry (lazy
invalidation), and lets the aggregator flip its health to
`unhealthy` + emit `media.backend.health_changed`.

**Linking** uses the Plex.tv PIN flow:
1. `link_account` POSTs `https://plex.tv/api/v2/pins?strong=true`,
   returns `{code, pin_id}` — Settings UI opens `plex.tv/link` and
   shows the code.
2. `link_account_complete` (the hidden two-phase followup) polls
   `GET /api/v2/pins/<id>` for `authToken`; persists into
   `account_token` once present.
3. `choose_server` lists `https://plex.tv/api/v2/resources` filtered
   to `provides=server` so the admin can pick the
   `server_machine_id` from a dropdown.

**`runtime_dependencies()` returns `[]`** — Plex talks over HTTP,
Gilbert never transcodes.

### Jellyfin (`std-plugins/jellyfin/`)

Pure `httpx.AsyncClient` against `/Users/{userId}/Items` etc. — no
SDK, just a thin REST wrapper. The official
`jellyfin-apiclient-python` is partially synchronous and missing
the Sessions remote-control endpoints we need.

**v1 design: admin token + `userId` query/path parameter** for
per-user data. Each per-user query is logged on the Jellyfin server's
audit trail as the admin user — accepted v1 limitation. v2 may
switch to user-scoped api-keys (Jellyfin 10.9+).

**Username → user-id resolution** (`_resolve_jellyfin_user_id`): a
service-lifetime cache keyed by the *Jellyfin* username (NOT by
Gilbert user id). Two Gilbert users mapped to the same Jellyfin
username share the resolved id by definition. Cache cleared on
`initialize()` if the `access_token` changed.

**Reconciliation with the no-mapping fallback rule**: when a Gilbert
user has no Jellyfin mapping, per-user methods (`recently_added`,
`continue_watching`, `next_episode`) refuse to fall back to the
admin's own user-id — that would leak the admin's history. The
`userId` is what *scopes* the data; the admin token is just the
*credential*. Without a `userId` the call is refused upstream of
the HTTP layer.

**Ticks vs seconds**: Jellyfin uses 100-ns ticks for every position /
duration field. `1 second == 10_000_000 ticks`. Helpers
`_seconds_to_ticks` / `_ticks_to_seconds` handle the conversion;
ALL `StartPositionTicks` / `SeekPositionTicks` / `PositionTicks`
boundary crossings go through them. Mapping helpers convert
`RunTimeTicks` and `PlaybackPositionTicks` to seconds before
constructing `MediaItem`.

**Linking** (`POST /Users/AuthenticateByName`) returns `AccessToken`
which the action persists into `access_token`. `admin_password` is
cleared from the config after success unless `keep_password=true`
(transient field cleared on save like Sonos's `spotify_auth_code`).

**`runtime_dependencies()` returns `[]`** — REST over HTTP.

### Shared

Both backends:
- Set all six capability flags to `True`
  (`now_playing`, `resume`, `continue_watching`, `recently_added`,
  `seek`, `per_user`, `next_episode`).
- Translate transport / auth errors to the domain hierarchy
  (`MediaLibraryUnavailableError`, `MediaClientNotFoundError`)
  at the backend boundary — never leak `plexapi.PlexApiException`
  or `httpx.HTTPError` into `core/services/`.
- Normalize `added_at` / `last_viewed_at` to UTC unix seconds at
  the mapping helper boundary regardless of server timezone.
  Tests cover a non-UTC fixture per backend
  (`movie_non_utc.xml` / `movie_non_utc.json`).
- Ship a `scripts/capture_<backend>_fixtures.py` that hits a real
  server, redacts tokens / private identifiers, and writes the
  result to `tests/fixtures/<backend>/`.

## Related
- `std-plugins/plex/plex_backend.py` — `PlexBackend`.
- `std-plugins/jellyfin/jellyfin_backend.py` — `JellyfinBackend`.
- `std-plugins/plex/tests/test_plex_backend.py` — 20 tests covering
  mapping, capability flags, per-Home-user lock concurrency, token
  rotation cache eviction, search filter translation,
  `list_clients` merge, companion play, `next_episode`, and 401
  → `MediaLibraryUnavailableError` + cache eviction.
- `std-plugins/jellyfin/tests/test_jellyfin_backend.py` — 25 tests
  covering mapping, link flow + password clearing, recently_added /
  continue_watching paths, `play` / `pause` / `resume` / `stop` /
  `seek` URL construction with ticks, `next_episode` NextUp →
  Episodes fallback, 401 translation, and Jellyfin-username cache.
- [Media Library Service](memory-media-library-service.md) — the
  aggregator they plug into.
