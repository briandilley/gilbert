# Media Library Service

## Summary
Multi-backend video library + casting aggregator. Plex and Jellyfin
plugins register concrete `MediaLibraryBackend` subclasses; the
`MediaLibraryService` aggregator (`src/gilbert/core/services/media_library.py`)
holds `dict[str, MediaLibraryBackend]` keyed by `backend_name`, fans
queries out via per-backend `asyncio.wait_for`-wrapped tasks,
dispatches playback to whichever server owns the target client, and
exposes 12 AI tools under the `/media …` slash namespace.

## Details

### Architecture
This is the AuthService / KnowledgeService aggregator pattern, NOT
the MusicService single-backend chooser. Per-backend `enabled` flag +
per-backend `settings` subsection. A Plex+Jellyfin household gets
both registered and queries fan out in parallel. Search results merge
by **stable round-robin interleaving** across each backend's own
server-side relevance ordering — no homegrown Levenshtein. Per-op
fan-out timeouts (search 8s, recently_added 8s, continue_watching 5s,
now_playing 5s, list_clients 3s, play 10s) protect against a TCP-
responsive but XML-stalled backend.

### Interface
- `src/gilbert/interfaces/media_library.py` — `MediaLibraryBackend`
  ABC, frozen dataclasses (`MediaItem`, `MediaClient`, `MediaSession`,
  `RecentlyAddedEntry`, `ContinueWatchingEntry`, `MediaPlayCommand`,
  `MediaSearchFilters`, `BackendHealth`), `MediaKind` /
  `MediaPlaybackState` StrEnums, error hierarchy
  (`MediaLibraryError` → `MediaLibraryUnavailableError`,
  `MediaClientNotFoundError`, `MediaClientAmbiguousError`).
- `MediaLibraryProvider` capability protocol — read-only fan-out
  surface (`search`, `recently_added`, `continue_watching`,
  `list_clients`, `now_playing`, `list_backend_health`). Mutations
  (play / pause / seek) require the concrete service.
- Six capability flags on the ABC: `supports_now_playing`,
  `supports_resume`, `supports_continue_watching`,
  `supports_recently_added`, `supports_seek`, `supports_per_user`,
  `supports_next_episode`. Service-level capability gating reads
  "configured-and-supports-X," NOT "currently-healthy-and-supports-X"
  — tools never disappear mid-conversation due to backend health
  flips.

### Service
- Implements `Service`, `Configurable`, `ConfigActionProvider`,
  `ToolProvider`, and `MediaLibraryProvider`. ServiceInfo declares
  capabilities `{"media_library", "ai_tools"}` and the events
  `media.playback.started`, `media.playback.stopped`,
  `media.recently_added`, `media.backend.health_changed`.
- Three configurable AI prompts as
  `ConfigParam(multiline=True, ai_prompt=True)`:
  `recommend_next_prompt`, `item_disambiguation_prompt`,
  `client_disambiguation_prompt`. Falsy-fallback pattern
  (`str(...) or _DEFAULT_*`) — empty-string overrides resolve to
  defaults. `__init__` initializes all three to defaults.
- `config_params()` is computed lazily on every call (NOT cached in
  `__init__`) so a plugin loaded after the first call still surfaces
  in the next Settings refresh.

### AI Tools (12)
Slash namespace `media`. All tools read `_user_id` from the injected
arg dict; missing `_user_id` is a clean JSON error, never a silent
fallback to `get_current_user()`.

- `list_media_clients` (`/media clients`) — everyone, parallel-safe.
- `search_media` (`/media search`) — user, parallel-safe.
  Default-excludes `MUSIC_*` kinds when `kind` is unset (the
  `MusicService` seam). Tool description starts with "Search your
  video library …".
- `play_on` (`/media play`) — user. Resolves SHOW/SEASON to
  next-unwatched / on-deck episode via `next_episode` BEFORE
  dispatch (never silently plays the pilot). Visual UIBlock
  disambiguation when ≥2 high-confidence matches. Description starts
  with "Play **video** content (movies, shows, episodes) on a
  TV/phone client".
- `play_media_id` — button-invoked sibling. Re-resolves the item
  via `get_item(backend_user_id=<clicker>)` so `view_offset_seconds`
  belongs to the clicker, not the searcher.
- `recently_added` (`/media recent`) — gated on
  `supports_recently_added`.
- `continue_watching` (`/media on-deck`) — gated on
  `supports_continue_watching`. Per-user fan-out follows the
  missing-mapping policy: backends with mapping are queried with
  `backend_user_id=<mapped>`; backends without mapping are silently
  skipped (NEVER admin-token fallback) and surface in
  `unmapped_backends: [...]` metadata with a hint.
- `now_playing` (`/media now`) — gated on `supports_now_playing`.
  **Live, NOT cached** — bypasses `self._poll_last_sessions`.
- `playback_control` (`/media pause` / `resume` / `stop` / `seek`)
  — one tool with `action` enum, four pre-filled-action slashes.
  `seek` action gated on `supports_seek`. Lenient position parser
  (`parse_position`) accepts "5m", "1h22m", "1:22:00" (H:MM:SS),
  "1:22" (M:SS), raw seconds.
- `recommend_next` (`/media recommend`) — gated on
  `supports_recommend_next` (= AI capability + at least one backend).
  Candidates: 5 from continue_watching + 10 from recently_added + 15
  from unwatched preferred-genre search, capped at 30 total. Each
  summary truncated to 200 chars before serialization. Optional
  `intent` parameter passed verbatim into a `<user_intent>` block.
- `media_library_link_user` / `unlink_user` /
  `list_user_mappings` — admin-only, slash kebab-case
  (`/media link-user`, `/media unlink-user`,
  `/media user-mappings`).

### Multi-user
Singleton service. Per-request state (active user, conversation id)
NEVER on `self`. Tools read `arguments["_user_id"]`. Service-lifetime
state on `self`:
- Backend handles `self._backends`.
- Active prompt strings.
- Polling-diff caches keyed by `(backend_name, session_id)` /
  `(backend_name, library_section)` / job_id — NOT by Gilbert user.
- Per-client locks (`dict[(backend_name, client_id), asyncio.Lock]`)
  with a single short global lock guarding `dict.setdefault`.
- Per-backend health (`dict[backend_name, BackendHealth]`).

The most important multi-user test in the suite is
`test_search_concurrent_users_no_state_leak` — kicks off two `search`
calls under different `set_current_user(...)` contexts via
`asyncio.gather` with `context=copy_context()` per branch and asserts
each call's `backend_user_id` was its OWN.

### Storage
Two collections, both lazily created via `ensure_index` on first
start (no migrations):
- `media_library_user_map` — Gilbert user → (backend, backend user
  id, backend username). Unique index on `(gilbert_user_id,
  backend_name)`. Upsert semantics — re-link of the same Gilbert+
  backend pair overwrites.
- `media_library_clients_cache` — last-seen `MediaClient` per
  `(backend, client_id)`. Unique index on
  `(backend_name, client_id)`. **Merge-not-replace** —
  `list_clients()` upserts live clients with `last_seen_at=<now>`,
  then re-surfaces cached clients (within 30 days) for backends that
  *did* return a response, marked `is_online=False`. Reaped on
  service start.

### Polling
Two scheduled jobs via `SchedulerProvider`:
- `media_library.poll_now_playing` — default 30s with adaptive
  backoff (idle threshold 10 polls → interval doubles up to 300s
  cap; resets on any session observed OR on a
  `media.playback.started` bus event).
- `media_library.poll_recently_added` — default 300s, baseline-run
  sentinel (`self._poll_first_run_done: set[str]`) — first cycle
  populates the cache and emits NO events. Without it every restart
  would emit one event per item in the entire feed.

Both jobs explicitly set `set_current_user(UserContext.SYSTEM)` at
job entry and run `asyncio.wait_for`-wrapped per-backend fan-out so
a hung backend can't block the cycle. Per-backend startup jitter
randomizes the first fire so two backends don't lockstep.

State diffing: new sessions → `media.playback.started`; disappeared
sessions → `media.playback.stopped`. **State changes (PLAYING ↔
PAUSED) are NOT emitted in v1** — polling can't reliably distinguish
a 31-second pause from a stop+restart; v2 webhook/SSE work will add
`media.playback.state_changed`.

### Health
`BackendHealth(status, last_error, …)` per backend. Successful op →
healthy; timeout → degraded; auth failure → unhealthy. Transitions
emit `media.backend.health_changed`. The Settings Media-library
panel renders one row per backend with a colored dot. Tools never
disappear because of health flips.

### Open Questions Deferred
N:M Gilbert↔backend mapping (1:1 in v1, deferred to v2). Webhook /
SSE session events (polling in v1). Per-user `preferred_genres`
(household-level in v1). Per-user-token minting for Jellyfin (admin-
token + `userId` in v1). Poster-URL proxy (raw URLs in v1, with
export-time redaction). Restricted-library aware per-user
`recently_added` polling. Adaptive backoff on
`poll_recently_added`. See `docs/specs/OPEN_QUESTIONS.md`.

## Related
- `src/gilbert/interfaces/media_library.py` — ABC + dataclasses +
  protocol + errors.
- `src/gilbert/core/services/media_library.py` — aggregator service.
- `tests/unit/test_media_library_service.py` — comprehensive
  coverage of fan-out, mapping, polling, locking, multi-user race.
- `tests/unit/_fakes/media_library.py` — `FakeMediaLibraryBackend`.
- [Plex / Jellyfin Backends](memory-media-library-backends.md) —
  per-backend gotchas.
- [Multi-User Isolation](memory-multi-user-isolation.md).
- [Backend Pattern](memory-backend-pattern.md).
- [Capability Protocols](memory-capability-protocols.md).
