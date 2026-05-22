# Feature 07 — Media Library Service (Plex / Jellyfin)

**Status:** Spec for implementation
**Owner:** Media domain
**Date:** 2026-05-09
**Related features:** `arr` plugin (Radarr / Sonarr — sources and downloads media), Speaker / Music services (audio playback analog)

---

## 1. Pitch

Gilbert already knows how to *acquire* movies and TV via the `arr` plugin (Radarr finds and downloads movies, Sonarr does the same for shows). What it cannot do today is *watch* anything. The owning user must walk to a TV, open a Plex / Jellyfin app, and pick something — Gilbert is not in the loop.

Feature 07 closes that gap. Introduce a `MediaLibraryBackend` ABC, a singleton aggregator `MediaLibraryService`, and two plugins (`std-plugins/plex/`, `std-plugins/jellyfin/`) that register backends against the same ABC.

The AI gains tools to:

- Search the user's library across all configured servers (`search_media`).
- Surface what arrived recently (`recently_added`).
- Resume what the user is watching (`continue_watching`).
- Inspect what a TV is playing right now (`now_playing`).
- Cast a specific movie / show / track to a named room (`play_on`, `play_media_id`).
- Pause / resume / stop / seek an active session via a single `playback_control` tool.
- Optionally recommend what to watch next via the AI itself (`recommend_next`).

The shape is **multi-backend aggregation** modelled on the closest existing precedents: `AuthService` and `KnowledgeService` (both hold `dict[str, Backend]`, fan out, merge). Search and casting are thematically analogous to `MusicService` + `SpeakerService`, but those services hold a *single chosen backend*, not the per-backend-enabled-flag aggregator this feature needs. Implementers should read `src/gilbert/core/services/auth.py:64` and `knowledge.py:42` for the start-up loop, per-backend `enabled` flag, per-backend settings subsection, and fan-out shape. The shared mental model is: `arr` owns acquisition, `media_library` owns discovery and playback, they don't overlap. Photo support (Plex/Jellyfin photo libraries) is in scope but tier-2 — exposed via `MediaKind.PHOTO` and `search_media`, but no dedicated tools.

---

## 2. Goals & Non-Goals

### Goals

1. One `MediaLibraryBackend` ABC. Two registered backends: `plex`, `jellyfin`. Future video services (Emby, Kodi, Roku-direct) plug in identically.
2. One `MediaLibraryService` aggregator that holds **N backends**, fans library queries out, merges results, and dispatches playback to whichever server owns the target client. Architecturally mirrors `AuthService` / `KnowledgeService` (per-backend enable flag + per-backend settings subsection + fan-out via `asyncio.gather`).
3. Capability-gated AI tools — only register `now_playing` when at least one configured backend reports `supports_now_playing`; only register `playback_control` with the `seek` action enabled when at least one client supports seeking; etc.
4. **Per-user identity mapping.** Each Gilbert user maps to **exactly one (backend, user) pair per backend** in v1 (1:1 per backend; a Gilbert user can be linked to one Plex Home user *and* one Jellyfin user simultaneously, but not to two Plex Home users). Continue-watching, on-deck, and history are read against the *mapped* user, not a service-wide token. Multi-mapping (one Gilbert user → many Plex Home users for a "household" view) is deferred to v2 — see Open Questions. ContextVar-driven: tools read `_user_id` from the injected arg dict, never from `self`.
5. Configurable, hot-reloadable settings for each backend. AI prompts (recommendation, item disambiguation, client disambiguation) follow the "AI prompts are always configurable" rule.
6. Events emitted on `media.playback.started`, `media.playback.stopped`, `media.recently_added`, and `media.backend.health_changed` so other services (notifications, agents, automations) can react.
7. Storage minimal — a `media_library_user_map` (per-user → per-backend account) and a transient `media_library_clients` cache.
8. **Episode-aware playback for shows.** When the AI is asked to "play Severance," `play_on` resolves the show to the user's *next-unwatched / on-deck* episode before dispatching — not the pilot.
9. **Visual disambiguation for items.** When `play_on` resolves to multiple high-confidence items, the user picks via UI poster cards instead of a silent first-match.

### Non-Goals

- Subtitles management (track switching, downloads). Out of scope. (Removed `audio_stream_id` / `subtitle_stream_id` from `MediaPlayCommand` — see section 5.1.)
- Library scan triggers (`POST /library/sections/<id>/refresh`). Plex/Jellyfin scan automatically; admins can use their native UIs.
- Editing media metadata, posters, collections.
- A streaming proxy / transcoded URL surface inside Gilbert. Playback always happens via the user's existing client app on the device — Gilbert just tells it what to play.
- A SPA "library browser" page. Settings UI (with a real per-backend User-Mappings table — see section 13) is in scope (admin); a browse-and-pick page is v2.
- Subsonic-style audio streaming. Audio in this feature is *visual* (music videos, the music subsection of Plex/Jellyfin) cast to TV clients — true audio routing remains the job of `MusicService` / `SpeakerService`. `MUSIC_*` `MediaKind` values are returned by `search_media` *only* when the caller explicitly opts in via the `kind` parameter.
- **Webhook / SSE-driven session events for v1.** Polling is the v1 mechanism; Plex webhooks (Plex Pass) and Jellyfin `/socket` SSE are tracked as v2 work in the Open Questions section.
- **Multiple Plex Home users mapped to one Gilbert user.** v1 enforces 1:1 per backend (see Goal 4).
- **Plex restricted-library aware recently-added events for v1.** Polling runs as `SYSTEM` and emits events that include the `library_section`; downstream subscribers (notifications) re-filter against the recipient's per-user mapping before delivery (see section 6.5).

---

## 3. Plex vs Jellyfin: One Plugin or Two?

**Decision: two plugins (`std-plugins/plex/`, `std-plugins/jellyfin/`), one shared backend ABC.**

### Rationale

The two services have nearly identical *shapes* but enough divergence in detail that one plugin would carry both halves of every fork:

| Concern | Plex | Jellyfin |
|---|---|---|
| Discovery | `https://plex.tv/api/v2/resources` (Plex Cloud, signed-in account → list of owned servers) | Direct LAN URL or admin-supplied URL; no cloud index |
| Auth (server) | `X-Plex-Token` header, obtained via Plex Cloud OAuth-ish PIN flow on `plex.tv/api/v2/pins` | Per-user `Authorization: MediaBrowser Token="…"` header obtained from `POST /Users/AuthenticateByName` |
| Auth (per-user) | One Plex account owns the server; "Plex Home" sub-users + "Managed users" share that token via `X-Plex-Token` of the Home user (each Home user is a separate token) | Each Jellyfin user has their own credentials and token |
| Library API | `/library/sections`, `/library/sections/<id>/all`, XML by default (JSON via `Accept: application/json`) | `/Users/{userId}/Items`, `/Users/{userId}/Views`, JSON natively |
| Playback dispatch | `POST /player/playback/playMedia?key=<itemKey>&machineIdentifier=<server>&address=…&port=…` to the *client*, OR `POST /clients/<id>/playMedia` (legacy companion) | `POST /Sessions/{sessionId}/Playing?ItemIds=…&PlayCommand=PlayNow` |
| Sessions | `/status/sessions` (XML) | `/Sessions` (JSON) |
| Python client | [`plexapi`](https://github.com/pkkid/python-plexapi) — synchronous, mature, lots of helpers; we'd wrap in `asyncio.to_thread` | [`jellyfin-apiclient-python`](https://github.com/jellyfin/jellyfin-apiclient-python) — partially synchronous, less complete; we'll likely call REST directly via `httpx` |
| Continue-watching | `/library/onDeck` and `/hubs/home/onDeck` | `/Users/{userId}/Items/Resume` |
| Recently added | `/library/recentlyAdded` per section, or `/hubs/home/recentlyAdded` | `/Users/{userId}/Items/Latest?ParentId=…` per library |

A single plugin would (a) carry both clients as dependencies even when only one server is in use, (b) double the test surface, (c) couple unrelated upstream releases (a `plexapi` bump shouldn't force the Jellyfin user to re-`uv sync`). Splitting into two plugins keeps each `pyproject.toml` tight and lets users install only what they actually run.

The **`MediaLibraryBackend` ABC lives in core (`src/gilbert/interfaces/media_library.py`)**, not in either plugin. Both plugins register concrete subclasses (`backend_name = "plex"` and `backend_name = "jellyfin"`) and the aggregator service iterates `MediaLibraryBackend.registered_backends()` exactly the same way `MusicService` consults `MusicBackend.registered_backends()`. A user with both Plex and Jellyfin (yes, this exists in the wild) gets both registered and the service queries both in parallel.

### Out-of-scope alternative considered

A "single video plugin" combining the two and discovering at runtime which library was asked for. Rejected — it would make the plugin's `pyproject.toml` install both client libraries unconditionally and conflate two upstream release cadences. `arr` is the precedent for "two related services, one plugin" (Radarr + Sonarr) — but those *share* the underlying `arr_client.py` and a single API key style; Plex and Jellyfin do not share enough to justify it.

---

## 4. Architecture Overview

```
src/gilbert/interfaces/media_library.py        # NEW — MediaLibraryBackend ABC + dataclasses
src/gilbert/core/services/media_library.py     # NEW — MediaLibraryService aggregator + tools
std-plugins/plex/                              # NEW — Plex backend
    plugin.yaml
    plugin.py                                  # side-effect import
    pyproject.toml                             # depends on plexapi, httpx
    plex_backend.py
    tests/
std-plugins/jellyfin/                          # NEW — Jellyfin backend
    plugin.yaml
    plugin.py                                  # side-effect import
    pyproject.toml                             # depends on httpx (REST direct)
    jellyfin_backend.py
    tests/
```

### Layer rules

1. `interfaces/media_library.py` — depends on `interfaces/configuration.py` (for `ConfigParam`) and stdlib only. No imports from `core/`, `integrations/`, `web/`, `storage/`, or any plugin.
2. `core/services/media_library.py` — depends on `interfaces/*`, `core/services/_backend_actions` (existing helper), and `core/output` (only if the service ever writes a file — we do not in this spec). Never imports from plugins or `web/`.
3. `std-plugins/plex/plex_backend.py` — depends on `gilbert.interfaces.media_library` and `gilbert.interfaces.configuration` only. Same for `jellyfin_backend.py`. No reach across into `core/services/`.
4. `app.py` (composition root) registers the singleton `MediaLibraryService`. Plugins side-effect-import their backends in `setup()`; the registry pattern picks them up.

Plugins implement the **backend ABC pattern** (see `.claude/memory/memory-backend-pattern.md`):

```python
class PlexBackend(MediaLibraryBackend):
    backend_name = "plex"
    supports_now_playing = True
    supports_resume = True
    supports_continue_watching = True
    supports_recently_added = True
    supports_seek = True
    supports_per_user = True
    supports_next_episode = True
```

Subclassing triggers `__init_subclass__` which registers the class in `MediaLibraryBackend._registry["plex"]`.

---

## 5. The `MediaLibraryBackend` Interface

File: `src/gilbert/interfaces/media_library.py`. All dataclasses are `@dataclass(frozen=True)`. All async methods. Uses `from __future__ import annotations` for forward refs.

### 5.1 Enums and dataclasses

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gilbert.interfaces.configuration import ConfigParam


class MediaKind(StrEnum):
    """What kind of thing a library item represents."""

    MOVIE = "movie"
    SHOW = "show"           # a series (collection of seasons)
    SEASON = "season"
    EPISODE = "episode"
    MUSIC_ARTIST = "music_artist"
    MUSIC_ALBUM = "music_album"
    MUSIC_TRACK = "music_track"
    MUSIC_VIDEO = "music_video"
    PHOTO = "photo"
    UNKNOWN = "unknown"


class MediaPlaybackState(StrEnum):
    """Current playback state of a media client / session."""

    PLAYING = "playing"
    PAUSED = "paused"
    BUFFERING = "buffering"
    STOPPED = "stopped"
```

#### `MediaItem`

A unified library item. Mirrors `MusicItem` deliberately; the only mandatory field differences are the `kind` enum and the addition of season/episode coordinates for episode items.

```python
@dataclass(frozen=True)
class MediaItem:
    """Unified descriptor for a movie, show, episode, album, track, etc.

    ``id`` is opaque and backend-specific (Plex: `ratingKey`; Jellyfin:
    item Id GUID). ``backend_name`` identifies which backend owns the id —
    callers must pass it back unchanged when resolving / playing. Two
    backends can share the same numeric id with no collision because
    ``(backend_name, id)`` is the actual key.
    """

    id: str
    backend_name: str            # "plex" or "jellyfin" — identifies the owning backend
    server_id: str               # Plex machineIdentifier or Jellyfin server id; "" for single-server setups
    title: str
    kind: MediaKind
    sort_title: str = ""
    year: int | None = None
    duration_seconds: float = 0.0
    summary: str = ""
    rating: float | None = None    # 0–10 normalized
    content_rating: str = ""       # "PG-13", "TV-MA", etc.
    studio: str = ""
    genres: tuple[str, ...] = field(default_factory=tuple)
    actors: tuple[str, ...] = field(default_factory=tuple)
    directors: tuple[str, ...] = field(default_factory=tuple)
    poster_url: str = ""
    backdrop_url: str = ""
    parent_id: str = ""            # immediate parent: for EPISODE → SEASON id, for SEASON → SHOW id, for ALBUM → ARTIST id
    parent_title: str = ""
    grandparent_id: str = ""       # one level up: for EPISODE → SHOW id (Plex's grandparentRatingKey)
    grandparent_title: str = ""
    season_number: int | None = None       # episodes only
    episode_number: int | None = None      # episodes only
    library_section: str = ""      # "Movies", "TV Shows", "Music" — backend's section name
    added_at: float = 0.0          # unix timestamp; the backend's "addedAt" field
    last_viewed_at: float = 0.0    # unix timestamp; 0 if never watched
    view_count: int = 0
    view_offset_seconds: float = 0.0  # resume position; 0 if not in progress
    is_watched: bool = False
```

Notes:

- `id` and `backend_name` together form the canonical reference. The aggregator passes `MediaItem` objects through opaquely — the AI never deals with raw ids unless it explicitly opts into `play_media_id`.
- `added_at` and `last_viewed_at` are **UTC unix timestamps**. Backends MUST normalize to UTC at the mapping helper boundary (Plex returns server-local epoch; Jellyfin returns ISO 8601 — both convert to UTC seconds before constructing `MediaItem`). Tests for `_plex_to_media_item` and `_jellyfin_to_media_item` MUST cover a non-UTC server timezone fixture to lock this in.
- `view_offset_seconds` is **per the *querying* user**. A `MediaItem` returned to user A carries A's resume offset. When this `MediaItem` is serialized into a button payload and clicked later (possibly by a different user from chat history), the service MUST re-resolve the offset at click time using the clicker's `_user_id` rather than trusting the embedded value. The clean rule (encoded in `play_media_id`'s tool handler): the embedded `view_offset_seconds` is **ignored for button-driven plays**; the handler calls `backend.get_item(item_id, backend_user_id=<clicker's mapped id>)` to fetch the clicker's current resume position.
- `poster_url` is best-effort and may expire. On Plex, the URL embeds the server's `X-Plex-Token` query parameter; on token rotation (admin re-runs `link_account`) cached URLs return 401 until re-fetched. Re-rendering a stale poster is a broken thumbnail, never a playback failure (playback uses `id`, not `poster_url`).

#### `MediaClient`

```python
@dataclass(frozen=True)
class MediaClient:
    """A target the library backend can dispatch playback to.

    Plex calls these "Players" (Plex for Apple TV, Plex Web, Plex for
    Roku, etc.); Jellyfin calls them "Sessions" (the device with an
    active client connection). In both cases each one has a stable
    identifier the playback API uses to address it.
    """

    client_id: str               # Plex: clientIdentifier; Jellyfin: SessionId
    backend_name: str            # which backend owns the client
    server_id: str
    name: str                    # human-friendly: "Living Room TV", "Bedroom Apple TV"
    device: str = ""             # device model: "Apple TV", "Roku Ultra", "Web (Chrome)"
    platform: str = ""           # "tvOS", "Android", "Web"
    address: str = ""            # IP — empty if backend doesn't expose
    user_id: str = ""             # last-known controlling user (backend-side id)
    is_online: bool = True
    supports_remote_control: bool = True
    supports_seek: bool = True
    supports_audio_stream_select: bool = False
    supports_subtitle_stream_select: bool = False
    last_seen_at: float = 0.0
```

`name` is what the AI matches against when the user says "play it on Living Room TV." Aliases are out of scope for v1 (use the actual client name); add them later if needed via the same pattern as `speaker_aliases`.

#### `MediaSession`

The **already-running** playback view, surfaced by `now_playing()`. Distinct from `MediaClient` because a client may exist (Apple TV is online) without a session (nothing playing). On Plex, sessions come from `/status/sessions`; on Jellyfin, from `/Sessions` filtered to those with `NowPlayingItem` non-null.

```python
@dataclass(frozen=True)
class MediaSession:
    """An in-progress playback session on a media client."""

    session_id: str              # backend's session identifier
    backend_name: str
    client: MediaClient
    item: MediaItem              # what's playing (for episodes, includes season/episode/parent)
    state: MediaPlaybackState
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    backend_user_name: str = ""  # backend-side user that started the session (Plex Home name / Jellyfin username)
    started_at: float = 0.0
    is_transcoding: bool = False
    quality_label: str = ""      # "Original (1080p)", "1.5 Mbps 720p" — empty if unknown
```

#### `RecentlyAddedEntry`

```python
@dataclass(frozen=True)
class RecentlyAddedEntry:
    """One slot in a recently-added feed.

    A wrapper rather than a bare ``MediaItem`` so future fields (e.g.
    "added by", "library section", "marker") can land without changing
    the underlying item shape.
    """

    item: MediaItem
    added_at: float              # unix timestamp


@dataclass(frozen=True)
class ContinueWatchingEntry:
    """One slot in a per-user continue-watching feed.

    Always has a non-zero ``view_offset_seconds`` on ``item``. For TV,
    the entry may instead reference a *next-up* episode (offset 0) — the
    ``next_up`` flag distinguishes that case so the AI can phrase
    'pick up where you left off' versus 'start the next episode'.
    """

    item: MediaItem
    next_up: bool = False
```

#### `MediaPlayCommand`

```python
@dataclass(frozen=True)
class MediaPlayCommand:
    """Composed playback request."""

    item: MediaItem
    client: MediaClient
    offset_seconds: float = 0.0
    idempotency_key: str = ""
```

`offset_seconds` lets `play(...)` start at a resume point; the service uses `item.view_offset_seconds` as the default when it's non-zero, but callers can override.

`idempotency_key` is an opaque string the service computes once per logical "play this thing now" decision (typically `f"{client.client_id}:{item.id}:{int(time.monotonic())//1}"`). The aggregator's per-client lock checks the most-recent key on the same client and short-circuits within a 5-second window to dedupe AI-loop / network-retry repeats. See section 6.10.

Audio-track / subtitle selection is **out of scope for v1** (see section 2 Non-Goals); the dataclass intentionally does **not** carry stream-selector fields. Adding them later when subtitle management lands is a non-breaking dataclass-field addition.

#### `MediaSearchFilters`

```python
@dataclass(frozen=True)
class MediaSearchFilters:
    """Optional filters to narrow a library search."""

    kinds: tuple[MediaKind, ...] = field(default_factory=tuple)
    library_section: str = ""        # Plex section title or Jellyfin library name
    year_from: int | None = None
    year_to: int | None = None
    genre: str = ""
    unwatched_only: bool = False
    limit: int = 30
```

### 5.2 Capability flags

Class attributes, default `False`, overridden by concrete backends:

```python
class MediaLibraryBackend(ABC):
    _registry: dict[str, type[MediaLibraryBackend]] = {}
    backend_name: str = ""

    # --- Capability flags ---
    supports_now_playing: bool = False
    """Backend can return active sessions with progress / state."""

    supports_resume: bool = False
    """Backend reports per-item view_offset and can start playback at it."""

    supports_continue_watching: bool = False
    """Backend can return a per-user 'on deck' / 'continue watching' list."""

    supports_recently_added: bool = False
    """Backend can return a recently-added feed."""

    supports_seek: bool = False
    """Backend's clients accept absolute-position seek commands."""

    supports_per_user: bool = False
    """Backend has a notion of multiple users (Jellyfin always; Plex
    only when Plex Home is configured) and the per-user APIs (resume,
    history) require a user mapping."""

    supports_next_episode: bool = False
    """Backend can resolve a SHOW (or SEASON) item to the user's
    next-unwatched / on-deck episode. Plex: `/library/metadata/<show>/onDeck`
    or per-show episodes filtered by `viewCount=0,view_offset>0`.
    Jellyfin: `/Shows/{showId}/NextUp?UserId=<uid>` or
    `/Shows/{showId}/Episodes?UserId=<uid>&IsPlayed=false&Limit=1` ordered
    by season+episode. Required by `play_on`'s show-resolution logic; if
    no backend supports it, `play_on` for a `SHOW` item returns
    `{error: "Episode resolution unavailable"}` rather than playing the
    pilot."""
```

`supports_transcoding` and `supports_play_queue` were considered and **removed from v1**: the former gated nothing (it was informational only — `is_transcoding` lives on `MediaSession` directly), and the latter is unused by any tool in section 7.4. Carrying them as dead config bloats the audit surface with no benefit; if v2 needs queue tooling, the flag returns alongside the new tool.

The concrete `PlexBackend` sets all six to `True` (Plex supports them all, including next-episode via `onDeck`). The `JellyfinBackend` sets all six to `True` (Jellyfin's `/Shows/{id}/NextUp` covers next-episode).

### 5.3 Methods

**Optional-method convention.** Methods like `recently_added`, `continue_watching`, `now_playing`, `next_episode`, and `seek` are non-abstract (they have a `raise NotImplementedError` default). Each is paired with a `supports_<X>` capability flag. Backends that don't support the operation **inherit the default** and leave the flag `False`; the service guards on the flag before calling, so the `NotImplementedError` is defense-in-depth, never user-visible. Backends that *do* support the operation override the method *and* set the flag to `True`. This matches `MusicBackend.start_station`'s pattern. Methods that every conceivable backend must implement (`initialize`, `close`, `search`, `get_item`, `list_libraries`, `list_backend_users`, `list_clients`, `play`, `pause`, `resume`, `stop`) are `@abstractmethod`.

**Class-attribute registry.** `_registry: dict[str, type[MediaLibraryBackend]] = {}` is declared on the ABC class itself. `__init_subclass__` writes to `MediaLibraryBackend._registry` *explicitly* (not `cls._registry[...]`, which would create per-subclass shadows). Matches the `MusicBackend` precedent.

```python
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            MediaLibraryBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[MediaLibraryBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    # --- Lifecycle ---

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # --- Library queries ---

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        filters: MediaSearchFilters | None = None,
        backend_user_id: str = "",
    ) -> list[MediaItem]:
        """Full-text search across all libraries the user can see.

        ``backend_user_id`` is the Plex/Jellyfin user id Gilbert mapped
        the calling Gilbert user to. Empty string means 'use the
        backend's primary / admin user' — acceptable for shared-account
        deployments.
        """

    @abstractmethod
    async def get_item(self, item_id: str, backend_user_id: str = "") -> MediaItem | None:
        """Resolve an opaque id back into a fresh ``MediaItem``.

        Implementations should re-query the backend with the supplied
        ``backend_user_id`` so ``view_offset_seconds`` reflects the
        clicker's progress (see section 5.1, "view_offset_seconds is
        per the querying user" note).
        """

    @abstractmethod
    async def list_libraries(self, backend_user_id: str = "") -> list[str]:
        """Return library section names (e.g. 'Movies', 'TV Shows').

        Both v1 backends (Plex, Jellyfin) implement this — promoted from
        an optional default to ``@abstractmethod`` since the service
        relies on it for the Settings UI's library-section dropdown.
        """

    @abstractmethod
    async def list_backend_users(self) -> list[dict[str, str]]:
        """Return ``[{id, username, display_name}]`` for every user on
        this backend's server.

        Used by the Settings UI's User Mappings panel (section 13) to
        populate the per-row dropdown of Plex Home / Jellyfin users.
        Plex: ``self._account.users()`` filtered to Home users + the
        owner. Jellyfin: ``GET /Users``.
        """

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
        library_section: str = "",
        backend_user_id: str = "",
    ) -> list[RecentlyAddedEntry]:
        """Return the most-recently-added items.

        Backends that don't support recently-added (``supports_recently_added``
        is ``False``) raise ``MediaLibraryUnavailableError``. The service
        guards on the flag before calling.
        """
        raise NotImplementedError(
            "This media library backend does not support recently-added"
        )

    async def continue_watching(
        self,
        *,
        backend_user_id: str = "",
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]:
        """Return the per-user continue-watching feed."""
        raise NotImplementedError(
            "This media library backend does not support continue-watching"
        )

    async def next_episode(
        self,
        show_id: str,
        *,
        backend_user_id: str = "",
    ) -> MediaItem | None:
        """Return the user's next-unwatched (or next-resumable) episode
        for a show.

        Resolution policy (in order):
        1. If any episode of the show has ``view_offset_seconds > 0``
           (in progress), return that one.
        2. Otherwise return the lowest (season_number, episode_number)
           with ``view_count == 0``.
        3. If the user has watched everything, return ``None`` (caller
           surfaces a "you're caught up" UIBlock — see section 7.1).

        Backends that don't support this (``supports_next_episode``
        False) raise ``NotImplementedError``. The service guards on the
        flag.
        """
        raise NotImplementedError(
            "This media library backend does not support next-episode resolution"
        )

    # --- Clients & sessions ---

    @abstractmethod
    async def list_clients(self) -> list[MediaClient]:
        """Return online, remote-controllable clients on this backend.

        Filtered to clients reachable by Gilbert. Offline clients are
        returned with ``is_online=False`` so the AI can phrase
        'the Apple TV is asleep'.
        """

    async def now_playing(self) -> list[MediaSession]:
        """Return active sessions across all clients on this backend."""
        raise NotImplementedError(
            "This media library backend does not support now-playing"
        )

    # --- Playback control ---

    @abstractmethod
    async def play(
        self,
        command: MediaPlayCommand,
        *,
        backend_user_id: str = "",
    ) -> None:
        """Start playing ``command.item`` on ``command.client``.

        Replaces any current playback on the target client. If the item
        has a non-zero view_offset and ``command.offset_seconds`` is 0,
        backends should resume from the offset (the service sets this
        explicitly so behaviour is callable-controlled).
        """

    @abstractmethod
    async def pause(self, client_id: str) -> None: ...

    @abstractmethod
    async def resume(self, client_id: str) -> None: ...

    @abstractmethod
    async def stop(self, client_id: str) -> None: ...

    async def seek(self, client_id: str, position_seconds: float) -> None:
        """Jump to ``position_seconds`` on ``client_id``.

        Backends that don't support seek (``supports_seek`` False) raise
        ``NotImplementedError``. Service guards on the flag.
        """
        raise NotImplementedError("This backend does not support seek")
```

### 5.4 Errors

A small domain hierarchy — all errors share `MediaLibraryError` so the tool layer can do a single `except MediaLibraryError` catch and translate uniformly.

```python
class MediaLibraryError(RuntimeError):
    """Base class for media-library domain errors. All errors raised
    by the ABC and its concrete subclasses derive from this so callers
    can catch the family with one ``except``.
    """


class MediaLibraryUnavailableError(MediaLibraryError):
    """Raised when the backend can't fulfill a request — typically
    because configured credentials are missing/invalid or the upstream
    server is unreachable. Services catch this and surface the message
    in the tool result rather than crashing the AI turn.
    """


class MediaClientNotFoundError(MediaLibraryError):
    """Raised when the AI asks to play on a client that doesn't exist
    on any configured backend. The service catches and turns into a
    'no client named X' tool error with a list of options.
    """


class MediaClientAmbiguousError(MediaLibraryError):
    """Raised by ``find_clients()`` when the caller-supplied name
    matches multiple clients and no disambiguation context is
    available. Carries a ``candidates: list[MediaClient]`` attribute
    so the caller can surface choices to the user.
    """

    def __init__(self, message: str, candidates: list["MediaClient"]) -> None:
        super().__init__(message)
        self.candidates = candidates
```

Per-backend HTTP / library errors (`plexapi.exceptions.PlexApiException`, `httpx.HTTPStatusError`, `httpx.ConnectError`) are caught at the *backend boundary* and translated to one of the above before crossing into core. Section 17 enumerates the per-backend mapping.

### 5.5 Capability-introspection protocol

Other services (notifications, agents) need to *ask* whether the media library is available without importing the concrete service. Add a `@runtime_checkable Protocol` whose method signatures **match the concrete service exactly** — keyword names included, since `@runtime_checkable` only structurally verifies attribute presence and a kwarg-name mismatch will pass `isinstance` but break callers at invocation time.

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaLibraryProvider(Protocol):
    """Capability protocol for the media library aggregator.

    Exposes only the read-only, fan-out-safe operations. Mutations
    (play / pause / etc.) and admin operations (user mapping, ConfigAction
    invocation) require the concrete service — consumers that need them
    must depend on it explicitly via the composition root.
    """

    async def search(
        self,
        query: str,
        *,
        kind: MediaKind | None = None,
        gilbert_user_id: str | None = None,
    ) -> list[MediaItem]: ...

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
    ) -> list[RecentlyAddedEntry]: ...

    async def continue_watching(
        self,
        *,
        gilbert_user_id: str,
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]: ...

    async def list_clients(self) -> list[MediaClient]: ...

    async def now_playing(self, client_name: str | None = None) -> list[MediaSession]: ...

    async def list_backend_health(self) -> list[dict[str, object]]: ...
```

`MediaLibraryService` satisfies this protocol; the service registers the capability `"media_library"` and consumers do `isinstance(svc, MediaLibraryProvider)` per the standard pattern (see `.claude/memory/memory-capability-protocols.md`). The capability check at the service-registration boundary is `isinstance(resolver.get_capability("media_library"), MediaLibraryProvider)` — never `isinstance(svc, MediaLibraryService)` (concrete-class anti-pattern).

---

## 6. The `MediaLibraryService` Aggregator

File: `src/gilbert/core/services/media_library.py`. Implements `Service`, `Configurable`, `ConfigActionProvider`, `ToolProvider`, and `MediaLibraryProvider`.

### 6.1 ServiceInfo

```python
def service_info(self) -> ServiceInfo:
    return ServiceInfo(
        name="media_library",
        capabilities=frozenset({"media_library", "ai_tools"}),
        requires=frozenset({"entity_storage"}),
        optional=frozenset({"configuration", "event_bus", "ai_chat", "scheduler"}),
        events=frozenset({
            "media.playback.started",
            "media.playback.stopped",
            "media.recently_added",
            "media.backend.health_changed",
        }),
        toggleable=True,
        toggle_description="Plex / Jellyfin video library and playback",
    )
```

`ai_chat` is optional because the only place we use it is `recommend_next` and the item-/client-disambiguation flows — when the capability isn't around (AI service disabled), `recommend_next` de-registers itself and disambiguation falls back to the deterministic ordering described in section 6.6 (last-used > online > alphabetical).

`configuration` is **truly** optional: when the configuration service isn't available, polling falls back to the hard-coded defaults (30s for now-playing, 300s for recently-added, both enabled). Recompute prompts default to the `_DEFAULT_*` constants. Backend instantiation requires *config*, however, so without `configuration` the service runs with zero backends — a degraded but non-crashing state.

`scheduler` is optional: without it, the polling loops simply do not run and event-emission is limited to direct tool-driven plays. The service still functions for AI tool dispatch.

### 6.2 Per-instance backend list

The aggregator instantiates **each registered backend at most once** and stores them as `self._backends: dict[str, MediaLibraryBackend]` keyed by `backend_name`. The closest existing precedents in the codebase are `AuthService` (`src/gilbert/core/services/auth.py:64`) and `KnowledgeService` (`knowledge.py:42`) — both hold `dict[str, Backend]`, both use a per-backend `enabled` flag plus a per-backend `settings` subsection, and both fan out via `asyncio.gather`. Implementers should read `auth.py:79–141` for the start-up loop, *not* `music.py` (which uses a single chosen backend, a different pattern).

Behaviour:

1. On `start`, read the `media_library` config section.
2. For each enabled subsection (`backends.plex.enabled`, `backends.jellyfin.enabled`), look up the backend class in `MediaLibraryBackend.registered_backends()`, instantiate, and `await backend.initialize(subsection["settings"])`. Track health (see section 6.11) — a failed `initialize` flips that backend to `unhealthy` but does NOT abort start-up; the rest of the aggregator continues.
3. Store as `self._backends: dict[str, MediaLibraryBackend]` keyed by `backend_name`.
4. On `stop`, iterate and `await backend.close()`.

Disabled backends are skipped — no instance is created, so a Jellyfin-only deployment never imports `plexapi` (the plugin isn't loaded), and a Plex-only deployment likewise. The aggregator's `list_clients()` returns the *union* across all instantiated backends.

**Capability-gating timing.** `get_tools()` is consulted by the AI service on every conversation turn (per the standard `ToolProvider` contract); it computes `self.supports_*` from `self._backends.values()` *configured* state, NOT runtime health. A backend that is configured but currently unreachable still counts toward capability gating — its tools stay registered and surface a `MediaLibraryUnavailableError` translation in the tool result. Tools never disappear mid-conversation because of transient health flips. See section 6.11 and the regression test `test_tool_remains_registered_when_only_backend_unhealthy`.

**Plugin load timing.** `config_params()` (section 10.1) iterates `MediaLibraryBackend.registered_backends()` to add per-backend rows. This iteration is **lazy** — the Settings WS handler invokes `config_params()` on demand, not at service-registration time, so by the time a user opens the Settings UI all plugins have completed `setup()`. Implementers MUST NOT cache the result of `config_params()` in `__init__`; it must be computed on every call so a plugin loaded after the first call (theoretically possible during runtime install) shows up on the next Settings refresh.

### 6.3 Per-user identity mapping

Each Gilbert user (by `user_id`) maps to **exactly one** `(backend_name, backend_user_id)` pair per backend in v1 — i.e., one Plex Home user *and* one Jellyfin user simultaneously, but not two of either. The unique index encodes this constraint directly. Multi-mapping (a Gilbert user → many Plex Home users for a "household merged view") is deferred to v2 — see section 23.

Storage:

- Entity collection: `media_library_user_map`.
- Index: `(gilbert_user_id, backend_name)` unique. Defined as `IndexDefinition(collection="media_library_user_map", fields=["gilbert_user_id", "backend_name"], unique=True)`.
- Document shape:

  ```json
  {
    "_id": "<uuid>",
    "gilbert_user_id": "u_abc",
    "backend_name": "plex",
    "backend_user_id": "12345",
    "backend_username": "alice_plex_home",
    "created_at": 1715212800,
    "updated_at": 1715212800
  }
  ```

The aggregator exposes two helpers:

```python
async def resolve_backend_user(self, gilbert_user_id: str, backend_name: str) -> str:
    """Return the backend's user id for this Gilbert user, or '' if unmapped.

    A return of '' means 'fall back to the backend's primary user' —
    acceptable behaviour for single-user / shared-account households. The
    service does NOT raise when the mapping is missing; tools that need
    a strict mapping (per-user history, continue-watching) check for ''
    and surface a helpful error.
    """

async def set_user_mapping(
    self,
    gilbert_user_id: str,
    backend_name: str,
    backend_user_id: str,
    backend_username: str = "",
) -> None: ...
```

#### Cache layering — three distinct caches with three distinct lifetimes

Three caches touch user identity. They are deliberately **layered** and the spec separates them so an implementer doesn't conflate scopes:

1. **Gilbert→backend-user mapping** (the entity-storage lookup): **request-scoped — read on every tool call, no in-memory mutation across users.** This is what section 11 ("Multi-User Isolation") is about. Storage hits are cheap and the mapping is small.
2. **Backend's username→backend-user-id resolution** (e.g., Jellyfin's `_resolve_jellyfin_user_id("alice")` → `"abc-123"`): **service-lifetime cache, keyed by the *backend's own* username** (NOT by Gilbert user id). This cannot leak across Gilbert users because the key is the backend's name; two Gilbert users mapped to the same Jellyfin username share the same id by definition.
3. **Plex Home user→token cache** (`dict[backend_user_id, str_token]`): **service-lifetime, keyed by Plex Home user uuid.** Same reasoning as (2) — keyed by backend identity, not Gilbert identity. See section 8.5 for the lock granularity.

#### Per-backend missing-mapping policy (fan-out)

A Gilbert user may have a Plex mapping but no Jellyfin mapping (or vice versa). The aggregator's per-user fan-out (`continue_watching`, the per-user paths inside `search`/`recently_added`) follows this policy:

- **Backends WITH a mapping for this user**: queried with `backend_user_id=<mapped>`.
- **Backends WITHOUT a mapping**: **silently skipped** (not queried, no admin-token fallback). Their absence is surfaced in the response metadata as `unmapped_backends: ["jellyfin"]` so the AI / UI can hint "Jellyfin not linked — ask an admin to run /media link-user".
- **All configured backends unmapped**: returns `{error: "No <backend(s)> account linked to your Gilbert user; ask an admin to run /media link-user", available_backends: [...]}`.

This is the precedent established by `search` for "one of two backends down" — partial results > silent admin-token fallback > nothing. Example payload:

```json
{
  "entries": [{"item": {...}, "next_up": false}],
  "unmapped_backends": ["jellyfin"],
  "hint": "Continue-watching from Plex only — Jellyfin is not linked to your Gilbert user."
}
```

The mapping is **read on every tool call** (no in-memory cache mutation across users — see "Multi-User Isolation"). Storage hits are cheap and the mapping is small.

The Settings UI exposes a per-user editing panel — see section 13 for the User Mappings table and the `list_backend_users()` ABC method that powers it. The slash commands (`/media link-user`, `/media unlink-user`) are admin-only convenience shims that share the same backing service method; the canonical path is the Settings UI.

### 6.4 ContextVar use

Tools read the calling Gilbert user from the injected `_user_id` argument (set by `AIService._run_one_tool` — see `core/services/ai.py` and `memory-multi-user-isolation.md`). The injection is **guaranteed**: the AI dispatcher and the slash dispatcher both populate `arguments["_user_id"]` from `get_current_user().user_id` *before* invoking any tool handler. No tool handler in this service falls back to `get_current_user()` itself — the silent-fallback footgun (a ContextVar set on a parent task that didn't `copy_context()` returning the wrong user under concurrent load) is eliminated by requiring the argument and raising a clean error if it's missing.

```python
async def _tool_continue_watching(self, arguments: dict[str, Any]) -> str:
    user_id = arguments.get("_user_id")
    if not user_id:
        return json.dumps({"error": "Internal: tool invoked without _user_id"})
    ...
```

This matches the inbox / agent precedent (`_resolve_caller_user_id` pattern in `core/services/ai.py`). The slash-dispatch path injects `_user_id` from `get_current_user()` *before* invoking the tool — documented as a precondition both there and here.

`MediaLibraryService` instance state is restricted to:

- Backend handles (`self._backends`).
- Storage / event-bus references (lifecycle-scoped, never per-request).
- Cached `MediaLibraryBackend.registered_backends()` lookup (read-only).
- The active prompt strings (`self._recommend_next_prompt`, `self._client_disambiguation_prompt`, `self._item_disambiguation_prompt`) — overwritten only via `on_config_changed`. **`__init__` sets each to its `_DEFAULT_*` constant** so the very first tool call (before any config-change event has fired) does not `AttributeError`.
- **Polling-loop diff caches** (service-lifetime, keyed by backend / section, NOT by Gilbert user):
  - `self._poll_last_sessions: dict[tuple[str, str], MediaSession]` — keyed by `(backend_name, session_id)`.
  - `self._poll_last_added_at: dict[tuple[str, str], float]` — keyed by `(backend_name, library_section)`.
  - `self._poll_first_run_done: set[str]` — set of poll job ids that have completed at least one cycle (the "baseline run" sentinel; see section 6.5).
- **Per-client playback locks** (`dict[tuple[str, str], asyncio.Lock]` keyed by `(backend_name, client_id)` — see section 6.10).
- **Per-backend health state** (`dict[str, BackendHealth]` keyed by `backend_name` — see section 6.11).

No `_current_*`, `_active_*`, `_pending_*` attributes. Concurrent calls from two users do not share any mutable per-call state.

### 6.5 Event publication

When a backend's `play()` succeeds, the aggregator emits:

```python
Event(
    event_type="media.playback.started",
    data={
        "backend": backend_name,
        "client_id": client.client_id,
        "client_name": client.name,
        "item_id": item.id,
        "item_title": item.title,
        "item_kind": item.kind.value,
        "item_year": item.year,
        "user_id": gilbert_user_id,    # "" for poll-detected events; see below
        "backend_user_name": session.backend_user_name,  # who started it on the Plex/Jellyfin side
        "library_section": item.library_section,  # for restricted-library re-filtering
        "initiator": initiator,        # "user" | "ai" | "agent" | "external"
    },
    source="media_library",
)
```

`media.playback.stopped` carries the same shape with `position_seconds` and `progress_pct` instead of `item_year`.

#### `user_id` semantics for system-driven events

Events are emitted from two sources:

- **Tool-driven** (the user / AI invoked `play_on` etc.): `user_id = <gilbert user>`, `initiator = "user" | "ai" | "agent"`.
- **Poll-detected** (someone hit play on the Plex remote, not via Gilbert): `user_id = ""`, `initiator = "external"`. The Gilbert user is not knowable from the Plex/Jellyfin side; downstream subscribers (notifications) must handle the empty case — typically by addressing the notification to the household, or by mapping `backend_user_name` back to a Gilbert user via `media_library_user_map`.

#### Polling jobs

Two scheduled jobs via the `SchedulerProvider` capability. Job ids: `media_library.poll_now_playing` and `media_library.poll_recently_added`. Each callback **explicitly sets `_current_user = UserContext.SYSTEM` at entry** (matching the knowledge-service reindex job and the calendar poll job — never relying on the implicit default).

Both polls run with **per-backend startup jitter**: on the first scheduling of each job, the initial fire is delayed by `random.uniform(0, interval_seconds)` so two backends don't lockstep against the same flaky NAS. Subsequent fires follow the configured interval.

Both polls fan out across backends with `asyncio.gather(..., return_exceptions=True)` wrapped per-backend in `asyncio.wait_for(timeout=...)` (5s for now-playing, 8s for recently-added) — see section 6.8. A backend that times out is logged at WARN, dropped from this cycle, and retried on the next.

##### `poll_now_playing` — adaptive cadence

Default interval: 30s. **Adaptive backoff**: when `now_playing()` returns empty for `idle_threshold` consecutive polls (default 10), the effective interval doubles up to a `idle_max_interval_seconds` cap (default 300s). When *any* session is observed, the interval resets to the base value. The poll loop also subscribes to its own bus: any `media.playback.started` event (e.g., from a tool-driven `play_on`) immediately resets the cadence and forces the next poll to fire on the next scheduler tick.

State diffing: each cycle compares the union of returned sessions against `self._poll_last_sessions`. Transitions emit:

- New `(backend_name, session_id)` → `media.playback.started`.
- Disappeared `(backend_name, session_id)` → `media.playback.stopped`.
- **State changes (PLAYING ↔ PAUSED) are NOT emitted in v1.** Polling can't reliably distinguish a 31-second pause from a stopped+restarted session, and emitting `state_changed` on every observed flip would spam subscribers. v2 webhook/SSE work will add a separate `media.playback.state_changed` event.

Sessions shorter than the poll interval (e.g., a 25-second clip) start and end between polls and emit no events; this is an accepted limitation of the polling model and is one of the motivations for the v2 webhook upgrade.

The `now_playing` *tool* (section 7.2) **bypasses the cache** and queries each backend live — users asking "what's playing right now?" expect sub-second freshness, not a 30s-stale snapshot.

##### `poll_recently_added` — baseline-run sentinel

Default interval: 300s. The cache (`self._poll_last_added_at`) is in-memory; **restart re-baselines silently** because the very first poll cycle for each `(backend_name, library_section)` is a *baseline run* — it populates the cache and emits NO events. The `self._poll_first_run_done: set[str]` sentinel tracks which job ids have completed their first cycle:

```python
async def _poll_recently_added(self) -> None:
    set_current_user(UserContext.SYSTEM)
    is_baseline = "media_library.poll_recently_added" not in self._poll_first_run_done
    # … fetch entries per backend …
    if is_baseline:
        # populate cache only; emit nothing
        for entry in entries:
            self._poll_last_added_at[(backend, section)] = max(...)
        self._poll_first_run_done.add("media_library.poll_recently_added")
        return
    # subsequent runs: diff and emit
    for entry in entries:
        if entry.added_at > self._poll_last_added_at.get(key, 0.0):
            await self._event_bus.publish(...)
            self._poll_last_added_at[key] = entry.added_at
```

This is the same failure-mode-mitigation the calendar service spec adopted; without it, the first poll after restart would emit one event per item in the entire `recently_added` feed.

##### Restricted-library / per-user visibility for `recently_added` events

Plex (and Jellyfin) honor per-user library restrictions: a kid's Home user may not see the R-rated movies library at all. The polling loop runs as `SYSTEM` with `backend_user_id=""` and therefore sees the *admin/owner's* full feed. Two-stage handling:

1. **Event payload includes `library_section` and `backend` keys.** Subscribers (notifications service in particular) MUST re-filter against the recipient user's mapping before delivering: a notification destined for Alice's UI is dropped if Alice's `backend_user_id` lacks visibility into `library_section`.
2. **Per-user re-filter helper on the service**: `media_library.user_can_see(gilbert_user_id, backend_name, library_section) -> bool` — performs a backend-side check (Plex: query `account.user(id).servers()` for visible sections; Jellyfin: `GET /Users/{userId}/Views`) with a short-lived per-user cache (60s TTL).

Restricted-library households that cannot tolerate this two-stage approach (e.g., kids should never see titles in the *event payload*, even if filtered before display) should disable `poll_recently_added.enabled` in v1 and revisit when the v2 webhook path lands. This is documented in the open questions.

### 6.6 AI prompts

Three configurable prompts are introduced. All follow the standard pattern (see `memory-ai-prompts-configurable.md`).

#### `recommend_next_prompt`

System prompt for `recommend_next` — asks the AI to pick three items from a candidate list and return them as `ToolOutput` `UIBlock`s. The default is warmer and aware of the user's stated intent (passed via the optional `intent` tool parameter):

```python
_DEFAULT_RECOMMEND_NEXT_PROMPT = """\
You are helping someone pick what to watch right now. They have a
library of candidate items below — pick three based on what they
asked, weighing recency, unwatched status, and genre fit.

You will receive:
  - <user_intent>: the user's stated mood / genre / runtime, if any.
  - <candidates>: JSON list of items (title, year, genres, short
    summary, watch state, and runtime).
  - <recent_history>: a short list of the user's last few watched
    items, when known.

For each pick: one short, warm sentence on why this one — not "this
is a great choice." Be specific. Avoid recommending two items from
the same franchise.

If <recent_history> is empty (new user), recommend based on
<candidates> alone — lean on recency and genre fit.

Reply with a JSON array of three objects, each with `id` (echo back
unchanged), `reason` (one sentence), and `confidence` (0.0–1.0). No
extra commentary, no markdown — pure JSON.
"""
```

#### `item_disambiguation_prompt`

(Tier-2 — only invoked when the AI is configured *and* the visual UI-block path can't be used.) The default `play_on` policy returns multiple `UIBlock` poster cards when ≥2 high-confidence matches are returned (see section 7.1) — that's the primary disambiguation surface and it doesn't pay an AI round-trip. This prompt covers the residual case where the AI reasoning path is preferred (e.g., a non-interactive automation context):

```python
_DEFAULT_ITEM_DISAMBIGUATION_PROMPT = """\
The user asked to play something, but several library items matched
the title. Pick the one most likely to be the intended item based on
(a) recency (year), (b) the user's recent viewing history, (c) the
user's stated intent.

Reply with the JSON object `{"item_id": "<chosen id>", "backend":
"<plex|jellyfin>"}` — nothing else.
"""
```

#### `client_disambiguation_prompt`

System prompt for the rare case where `play_on(client_name="bedroom")` matches multiple clients (`Bedroom Apple TV`, `Bedroom iPad`). The service hands the AI the candidates and a short `<context>` block describing what the user just said and asks it to pick. **Deterministic fallback** when the AI capability is unavailable or returns an invalid id (one not in the candidate list):

1. Last-used (per-user) — read `media_library_clients_cache.last_used_at` for this user/client.
2. Online before offline (`is_online=True` first).
3. Alphabetical by `name`.

A new ConfigParam `client_disambiguation_threshold` (INTEGER, default 3) controls when the AI is invoked at all — if `len(candidates) < threshold`, the deterministic ordering above is used directly without paying an AI round-trip.

```python
_DEFAULT_CLIENT_DISAMBIGUATION_PROMPT = """\
You are helping pick a single playback target. The user named a
device, but several clients matched the name. Choose the one most
likely to be the right target based on (a) device type, (b) which one
the user used most recently, and (c) the user's stated intent.

Reply with the JSON object `{"client_id": "<chosen id>"}` — nothing
else.
"""
```

#### Caching pattern

All three prompts are exposed as `ConfigParam(multiline=True, ai_prompt=True)` on `MediaLibraryService.config_params()`, cached in `on_config_changed` as `self._recommend_next_prompt`, `self._item_disambiguation_prompt`, and `self._client_disambiguation_prompt`. Defaults are the constants above. The literal one-liner from `memory-ai-prompts-configurable.md` for the falsy-fallback (so an empty-string override does NOT yield an empty prompt at the call site):

```python
self._recommend_next_prompt = (
    str(config.get("recommend_next_prompt", "") or "")
    or _DEFAULT_RECOMMEND_NEXT_PROMPT
)
```

`__init__` initializes all three to their `_DEFAULT_*` constants so the very first call (before any config-change event has fired) does not `AttributeError`. Tests cover the call site reading `self._foo_prompt` (not the constant) for both the recommend and disambiguation flows.

#### Capability-protocol check

The `recommend_next` and disambiguation flows access the AI capability via `AISamplingProvider.complete_one_shot`. The capability check at registration time is `isinstance(resolver.get_capability("ai_chat"), AISamplingProvider)` — never `isinstance(svc, AIService)` (concrete-class anti-pattern). The `supports_recommend_next` property short-circuits to `False` when the protocol check fails.

### 6.7 Exposed methods (Python API)

```python
class MediaLibraryService(Service):

    # Library queries — fan out across backends and merge
    async def search(self, query: str, *, kind: MediaKind | None = None,
                     gilbert_user_id: str | None = None) -> list[MediaItem]: ...
    async def recently_added(self, *, kind: MediaKind | None = None,
                             limit: int = 10) -> list[RecentlyAddedEntry]: ...
    async def continue_watching(self, *, gilbert_user_id: str,
                                limit: int = 10) -> list[ContinueWatchingEntry]: ...
    async def now_playing(self, client_name: str | None = None) -> list[MediaSession]: ...
    async def next_episode(self, item: MediaItem, *,
                           gilbert_user_id: str) -> MediaItem | None: ...

    # Clients (find_clients returns ALL matches; find_client picks one or raises)
    async def list_clients(self) -> list[MediaClient]: ...
    async def find_clients(self, name_or_id: str) -> list[MediaClient]: ...
    async def find_client(self, name_or_id: str, *,
                          gilbert_user_id: str = "") -> MediaClient: ...
        # raises MediaClientNotFoundError if 0 matches,
        # MediaClientAmbiguousError(candidates=[...]) if >1 and no
        # disambiguation context resolves it.

    # Playback
    async def play_item(
        self,
        item: MediaItem,
        client: MediaClient,
        *,
        offset_seconds: float = 0.0,
        gilbert_user_id: str = "",
        initiator: str = "user",
        idempotency_key: str = "",
    ) -> None: ...
    async def pause_client(self, client: MediaClient) -> None: ...
    async def resume_client(self, client: MediaClient) -> None: ...
    async def stop_client(self, client: MediaClient) -> None: ...
    async def seek_client(self, client: MediaClient, position_seconds: float) -> None: ...

    # User mapping
    async def resolve_backend_user(self, gilbert_user_id: str, backend_name: str) -> str: ...
    async def set_user_mapping(self, gilbert_user_id: str, backend_name: str,
                               backend_user_id: str, backend_username: str = "") -> None: ...
    async def list_user_mappings(self, gilbert_user_id: str) -> list[dict[str, str]]: ...
    async def list_backend_users(self, backend_name: str) -> list[dict[str, str]]: ...

    # Backend health (read-only — see section 6.11)
    async def list_backend_health(self) -> list[dict[str, object]]: ...

    # Capability flags exposed to the tool registration loop
    @property
    def supports_now_playing(self) -> bool: ...   # ANY configured backend
    @property
    def supports_continue_watching(self) -> bool: ...
    @property
    def supports_recently_added(self) -> bool: ...
    @property
    def supports_seek(self) -> bool: ...
    @property
    def supports_next_episode(self) -> bool: ...
    @property
    def supports_recommend_next(self) -> bool: ...   # AI service available + at least one backend
```

`find_client` vs. `find_clients` — the split exists so the disambiguation flow doesn't conflate "not found" with "ambiguous." `find_clients(name_or_id)` returns the raw match list (callers handle the ambiguity themselves); `find_client(...)` is the high-level entry that raises a typed error so a `None` return is unambiguously "not found."

### 6.8 Fan-out behaviour

All aggregating reads (`search`, `recently_added`, `now_playing`, `continue_watching`, `list_clients`) route through a single helper:

```python
async def _fanout(
    self,
    op: Callable[[MediaLibraryBackend], Awaitable[T]],
    *,
    timeout_seconds: float,
    op_name: str,
) -> list[tuple[str, T | BaseException]]:
    """Run `op` against every healthy backend with a per-backend timeout.

    Returns a list of (backend_name, result_or_exception). Exceptions
    (including TimeoutError from asyncio.wait_for) are returned, not
    raised, so callers can surface partial results. Logs WARN per
    timeout / exception, tagged with op_name.
    """
```

Per-backend timeouts (configurable via `media_library.backend_timeout_seconds.<op>` keys, with sensible defaults):

| Operation | Default timeout |
|---|---|
| `search` | 8s |
| `recently_added` | 8s |
| `continue_watching` | 5s |
| `now_playing` | 5s |
| `list_clients` | 3s |
| `play` | 10s (per-client, no fan-out) |

Without per-backend timeouts a TCP-responsive but XML-stalled Plex blocks the entire AI turn. `asyncio.gather(..., return_exceptions=True)` alone is correct for *exceptions* but useless for *hangs*; `asyncio.wait_for` per backend closes the gap. Result handling:

- Per-backend exceptions and timeouts are logged at WARN level and silently dropped from the merged result. The service does **not** raise when one backend is down — the user experience for a Plex+Jellyfin household with Plex offline should be "I see Jellyfin results."
- Health is updated as a side effect: a timeout or `MediaLibraryUnavailableError` flips that backend's health to `degraded`/`unhealthy` (see section 6.11).

#### Result merging & ordering

- **`recently_added`**: sort by `added_at` desc. Tiebreaker: `(backend_name asc, item.id asc)` for determinism.
- **`search`**: trust each backend's *server-side* relevance ordering (Plex's library search and Jellyfin's `searchTerm` both rank internally) and merge by **stable round-robin interleaving** — first hit from Plex, first from Jellyfin, second from Plex, second from Jellyfin, … This avoids the homegrown-Levenshtein anti-pattern: Levenshtein on a 4-word title against a short query would rank the backend with the shorter title higher (smaller edit distance), strictly worse than each backend's own scoring. Tiebreaker for the order of backends in the round-robin: `backend_name asc`.
- **`continue_watching`**: round-robin across mapped backends (a user with Plex + Jellyfin alternates).
- **`now_playing`**: union; merge sessions across backends by `(client.name.lower(), client.address)` — if both backends report a session for the same physical TV, prefer the one with state `PLAYING` over `STOPPED`. The dedup is best-effort (same physical Apple TV registered with both servers); if the merge can't find a strong match the AI sees both sessions and the tool description warns it that "two backends sometimes report the same physical device."

The service caps the merged list at `limit` after merge, not per-backend. If the user asks for 10 recently-added across two backends, we ask each for 10, then merge and trim.

#### Search limit cap

`MediaSearchFilters.limit` is **service-side capped at 50** regardless of caller request. The AI passing `limit=10000` does not cause runaway memory; it gets 50 results. Pagination beyond the cap is out of scope for v1 (no `cursor` field on `MediaSearchFilters`). Per-backend `limit` is forwarded to the backend's own search API to let them truncate server-side.

`list_clients` likewise returns the union, but does NOT dedupe — Plex and Jellyfin clients are separate physical destinations. `find_clients(name)` does a case-insensitive substring match across the union; ambiguity is surfaced through the disambiguation flow described in section 6.6.

### 6.9 Storage

Two collections used by the service:

| Collection | Purpose | Retention |
|---|---|---|
| `media_library_user_map` | Gilbert user → (backend, backend user id) mapping | Persistent. Admin-edited. |
| `media_library_clients_cache` | Last seen `MediaClient` per `(backend, client_id)` so we can phrase "Apple TV is asleep" with the previously-known name when a client falls offline | **Merge-not-replace** — see below. Items older than 30 days are reaped on service start. |

Indexes:

```python
IndexDefinition(collection="media_library_user_map",
                fields=["gilbert_user_id", "backend_name"], unique=True)
IndexDefinition(collection="media_library_clients_cache",
                fields=["backend_name", "client_id"], unique=True)
```

#### Clients-cache merge semantics

Each `list_clients()` call applies a **merge-not-replace** update to the cache:

1. Backends return the *currently-online* clients. Service marks each with `is_online=True` and persists `(backend, client_id, name, device, last_seen_at=<now>)` to the cache (upsert).
2. **Recently-seen clients NOT in the current response** are returned alongside the live ones with `is_online=False`. The service queries the cache for `(backend_name, last_seen_at >= now - 30d)` rows whose `client_id` is missing from the live response and yields them as `MediaClient(is_online=False, ...)`.
3. Items older than 30 days are reaped on service start.

This serves the original use case (Plex's "list clients" sometimes drops a sleeping Apple TV) — the cached `Apple TV` continues to surface as "asleep" rather than disappearing entirely. Without the merge step, the offline TV simply vanishes from `list_clients` output, defeating the cache.

Cache also tracks `last_used_at: float` per `(backend, client_id, gilbert_user_id)` triple — written when a tool-driven `play_item` succeeds — to power the deterministic `find_client` fallback ordering described in section 6.6 (last-used > online > alphabetical).

### 6.10 Per-client locking

Per-client `asyncio.Lock` keyed by `(backend_name, client_id)` (the conservative tuple — two backends with the same `client_id` for different physical devices is rare but possible). The lock dict is `dict[tuple[str, str], asyncio.Lock]` on the service, lazily populated on first use. A *global* lock guards only the dict-lookup itself, never the network call. This is the textbook per-target-resource pattern (Appendix C anti-pattern: "One global `_session_lock` wrapping every `play_item`").

#### Idempotency

`play_item(...)` accepts an optional `idempotency_key`. The per-client lock holds a small `(idempotency_key, completed_at)` history (last 5 entries, 5-second TTL); a re-entry with the same key within the window short-circuits and returns the previous outcome instead of re-dispatching. This dedupes:

- AI-loop retries (the AI calling `play_on(...)` twice on a network blip).
- The button-click race (a user double-tapping the Play button).

The composite tools (`play_on`, `play_media_id`) compute the key as `f"{client.client_id}:{item.id}"` per logical play decision; callers passing `idempotency_key=""` get no dedup (single-shot, expected for explicit "play it again" intent).

### 6.11 Backend health

A small `BackendHealth` dataclass tracks per-backend status:

```python
@dataclass(frozen=True)
class BackendHealth:
    backend_name: str
    status: str               # "healthy" | "degraded" | "unhealthy"
    last_error: str = ""
    last_error_at: float = 0.0
    last_success_at: float = 0.0
```

The service holds `self._health: dict[str, BackendHealth]` keyed by `backend_name`. Transitions:

- Successful operation → `healthy` (resets `last_error`).
- Per-call timeout or transient error → `degraded`.
- Auth failure (`MediaLibraryUnavailableError` from a 401 / revoked token) → `unhealthy` and the service emits `media.backend.health_changed` with the new status.
- Subsequent calls to an `unhealthy` backend still attempt the operation (no client-side hard cut-off) — but the result feeds back into health and the Settings UI surfaces the banner. **Tools never disappear because of health flips** — they remain registered (consistent with the "configured-and-supports-X" gating in section 6.2) and surface the error in the tool result.

`list_backend_health()` returns `[{backend_name, status, last_error, last_error_at, last_success_at}]`. The Settings UI's "Media Library" panel renders one row per backend with a colored dot. The notifications service can subscribe to `media.backend.health_changed` to flip a banner ("Plex token revoked — re-link in Settings").

`on_config_changed` clears health for any backend whose config-section changed (a re-link wipes the per-Home-user token cache *and* resets health to `healthy` until the next call confirms).

---

## 7. AI Tools Exposed by `MediaLibraryService`

All tools have `slash_group="media"` (consistent with `arr`'s `radarr`/`sonarr` groups). All set `slash_help` per the convention. Read-only tools (`search_media`, `recently_added`, `continue_watching`, `now_playing`, `list_media_clients`) opt in to `parallel_safe=True`. Required role defaults to `"user"` for playback, `"everyone"` for queries (so guest users can see what's playing on the family TV); admin-only for user mapping.

**Tool count: 11 user-facing + 3 admin = 14 total** (was 14 user-facing previously). The four playback verbs (`pause_playback`, `resume_playback`, `stop_playback`, `seek_playback`) collapse into a single `playback_control(action, ...)` tool — one entry in the tool list, four slash commands mapping to pre-filled `action` arguments. The merge follows the same shape `set_volume(speaker, level)` uses on the speaker service.

### 7.1 Always-on tools

#### `list_media_clients`
- Slash: `/media clients`
- Help: "List media clients (TVs, phones, etc.) Gilbert can cast to."
- Parameters: none
- Required role: `everyone`
- Parallel-safe: yes
- Returns: JSON array of `MediaClient` dicts plus a flag for which is the user's last-used target.

#### `search_media`
- Slash: `/media search <query> [kind=movie|show|episode|music]`
- Help: "Search the library: /media search <query>"
- Parameters:
  - `query: STRING` — required
  - `kind: STRING` — optional, enum `[movie, show, episode, music_album, music_track]`
- Required role: `user`
- Parallel-safe: yes
- **Default-excludes `MUSIC_*` kinds.** When `kind` is unset, the tool restricts results to `(MOVIE, SHOW, EPISODE, SEASON)`. The user has to opt in to `kind=music_album` / `kind=music_track` to surface Plex's music library — the canonical music path is `MusicService.play_music`. This prevents the AI from picking `play_on(title="Adele", client="living room")` over `play_music("Adele")`.
- **Tool description (load-bearing)**: starts with *"Search your video library (movies, shows, episodes, optionally photos / music videos / music tracks if explicitly requested via `kind`). For audio playback to a speaker use `play_music` instead."*
- Returns: `ToolOutput` with text JSON + per-result `UIBlock` (poster + title/year/summary + Play button whose value is the `MediaItem` JSON, mirroring `MusicService._build_search_result_block`). UIBlock button labels are state-aware (see "Button label matrix" below).

#### `play_on`
- Slash: `/media play <title> on <client_name>`
- Help: "Play a movie/episode on a TV: /media play <title> on <client>"
- Parameters:
  - `title: STRING` — required, what to look up
  - `client: STRING` — required, target client name (substring match; disambiguates via prompt or deterministic ordering — see section 6.6)
  - `kind: STRING` — optional, enum same as `search_media`
- Required role: `user`
- **Tool description (load-bearing)**: *"Play **video** content (movies, shows, episodes) on a TV/phone client. The `client` parameter is **a single client name**, not a list — video plays on one screen at a time. For audio playback to a speaker use `play_music` instead."*

##### Composite resolution policy

1. `search_media(title, kind=…)` (with the music-kinds exclusion above) against all configured backends.
2. **Disambiguate items visually** when there's ambiguity (the dominant case — *"play that movie about dreams"* matches three things). Behaviour:
   - **Exactly 1 high-confidence match**: proceed.
   - **2–5 high-confidence matches**: return `ToolOutput` carrying N `UIBlock`s — one per candidate with poster + title + year + Play button (`tool_name="play_media_id"`, `value=<full MediaItem JSON>`). No actual playback. The user picks. (Threshold "high-confidence" = backend-supplied relevance ≥ 0.7, or top-3 results when all backends rank identically.)
   - **6+ matches**: trim to 5 by `(year desc, last_viewed_at desc)` and present as above.
   - **0 matches**: return `{"error": "Nothing in your library matches '<title>'", "suggestion": "/radarr.find <title> — to add it"}`.
   - The AI item-disambiguation prompt path (`item_disambiguation_prompt`) is reserved for non-interactive contexts (automations, agent runs). The default UX is the visual UIBlock picker — no AI round-trip, no risk of the model choosing wrong, immediate visual feedback.
3. **Resolve a SHOW (or SEASON) to the user's next episode** if the chosen item is `kind == SHOW` or `kind == SEASON`. This is the answer to *"play the next unwatched Severance on the living room TV"*:
   - Call `service.next_episode(item, gilbert_user_id=...)` which forwards to `backend.next_episode(item.id, backend_user_id=...)` for the owning backend.
   - If the backend returns an episode, proceed with that item.
   - If the backend returns `None` (user has watched everything), return a "caught up" `UIBlock`:

     ```text
     You're caught up on Severance — 19 episodes watched.
     [ Restart from S1E1 ]   [ Show what's coming next ]   [ Cancel ]
     ```

     The "Restart from S1E1" button fires `play_media_id` with the resolved S1E1 item and `offset_seconds=0`. The "Show what's coming next" button fires `sonarr_upcoming` with a `series` filter — letting the AI's reasoning chain on the user's choice. The UIBlock provides the affordances; cross-service calls happen only on user-driven button clicks.
   - If the backend doesn't support `next_episode` (no backend has `supports_next_episode=True`), return `{"error": "Episode resolution unavailable on this backend", "suggestion": "Use /media search to pick a specific episode"}` — does **not** silently play the pilot.
4. Resume eligibility — if the item has `view_offset_seconds > 0` (carried by `MediaItem` for the *querying* user), use it as the start offset. For button-driven plays via `play_media_id` the service re-resolves via `get_item(..., backend_user_id=<clicker>)` (see section 5.1).
5. Resolve the client via `find_client(client, gilbert_user_id=user_id)` — raises `MediaClientAmbiguousError` on ambiguity, which the tool catches and returns `{error: "Multiple matches for client '<name>'", candidates: [...]}` with a `UIBlock` per candidate.
6. Compute `idempotency_key = f"{client.client_id}:{item.id}"`, acquire the per-client lock, call `play_item`.

Returns: JSON `{status: "playing", title, client, offset_seconds, backend, resumed: bool, resolved_episode: {…}?}` or `{error, candidates: [...]}`.

#### `play_media_id`
- No slash command — exception per `memory-architecture-checklist.md` slash-command rule: opaque-`(backend, id)`-pair-only inputs are unsuitable for slash typing. Cited explicitly so a future audit pass doesn't flag it.
- Description: "Play a specific item by `(backend_name, item_id)` — use after `search_media` returns a target you can address directly."
- Parameters:
  - `backend: STRING` — required, `plex` | `jellyfin`
  - `item_id: STRING` — required
  - `client: STRING` — required
  - `offset_seconds: NUMBER` — optional. **Ignored** if the embedded value comes from a button payload's stale `MediaItem`; the handler re-resolves via `get_item(..., backend_user_id=<clicker>)` when the calling Gilbert user has a per-user mapping (see section 5.1).
- Required role: `user`
- Used by UIBlock buttons in `search_media` / `recently_added` / `continue_watching` / `play_on`-disambiguation results.

### 7.2 Capability-gated tools

Only registered in `get_tools()` when `self.supports_*` is true (`supports_*` reads "any configured backend with the capability," NOT "any healthy backend" — see section 6.2).

#### `recently_added` (gated on `supports_recently_added`)
- Slash: `/media recent [kind=movie|show] [limit=10]`
- Help: "Recently added: /media recent [kind=movie|show]"
- Required role: `everyone`
- Parallel-safe: yes
- Parameter order: `kind` first, `limit` second (kind is the more commonly-supplied first arg per `memory-architecture-checklist.md`'s "Parameter order hostile to shell use" rule).
- Returns: JSON `{entries: [...]}` plus per-item `UIBlock`s with poster + state-aware Play button (see button matrix below).

#### `continue_watching` (gated on `supports_continue_watching`)
- Slash: `/media on-deck` (renamed from `/media resume` to free that slash up for the playback-control action — see section 7.4 for the rationale)
- Help: "Show what to resume: /media on-deck"
- Required role: `user`
- Parallel-safe: yes
- Reads `arguments["_user_id"]`, calls `self.continue_watching(gilbert_user_id=user_id)`. Returns top 10 items with progress percentages and `UIBlock` Resume buttons. When the user has unmapped backends, the response includes `unmapped_backends: [...]` per section 6.3's missing-mapping policy.

#### `now_playing` (gated on `supports_now_playing`)
- Slash: `/media now [client]`
- Help: "What's playing now: /media now [client]"
- Required role: `everyone`
- Parallel-safe: yes
- Parameters: `client: STRING` optional.
- **Live, not cached**: queries each backend's `now_playing()` directly and bypasses `self._poll_last_sessions`. Users asking "what's playing right now?" expect sub-second freshness.
- When `client` is given, filtered to that client by **substring match against `client.name`** (matches the `find_client` substring convention). Not by `client_id` or address.
- Returns: JSON list of `MediaSession` dicts.

#### `playback_control`
- Slashes (one tool, four slashes — each slash pre-fills the `action` argument; same pattern as `/music play` vs `/music play-queue` mapping to different tool internals):
  - `/media pause [client]` — `action=pause`
  - `/media resume [client]` — `action=resume` (NEW canonical resume slash, not `resume-playback`)
  - `/media stop [client]` — `action=stop`
  - `/media seek <position> [client]` — `action=seek` (gated on `supports_seek` — when no backend supports seek, the slash silently un-registers; the `playback_control` tool stays for the other three actions)
- Required role: `user`
- Parallel-safe: no (per-client locking ensures correct serialization on the *same* client; per-client `asyncio.Lock` keyed by `(backend_name, client_id)` lets unrelated clients run in parallel — see section 6.10. Global locks are explicitly forbidden per Appendix C.)
- Parameters:
  - `action: STRING` — required, enum `[pause, resume, stop, seek]`.
  - `client: STRING` — optional. When omitted, auto-picks the active session if exactly one is playing across all backends; ambiguous → returns the candidate list as a UIBlock picker.
  - `position: STRING` — optional, only consumed when `action=seek`. Lenient parser accepts: `"5m"`/`"5min"`/`"5mins"` (5 minutes), `"1h22m"`/`"1hr22min"` (82 minutes), `"1:22:00"` (`H:MM:SS`), `"1:22"` (interpreted as `M:SS` — minutes:seconds, the more common shorthand for media), raw seconds (`"3700"`), and tolerates leading/trailing whitespace + units like `s`/`sec`/`secs`. Negative offsets ("rewind 30s") are out of scope — seek is absolute, not relative. Position parsing lives in `media_library` service, not the backends.
- Tool description points the AI at the slash names: *"Pause, resume, stop, or seek the active session on a media client. Use action=seek with the position parameter to jump to a specific point."*
- Returns: JSON `{status: "<action>ed", client, backend, position_seconds?}` or `{error, available_clients: [...]}`.

#### `recommend_next` (gated on `supports_recommend_next` — needs AI service)
- Slash: `/media recommend [kind=movie|show] [intent=…]`
- Help: "Get a recommendation: /media recommend"
- Required role: `user`
- Parameters:
  - `kind: STRING` — optional.
  - `intent: STRING` — optional, free-text mood/genre/runtime hint passed verbatim into the prompt's `<user_intent>` block (e.g., *"something funny under 90 minutes"*).
- **Candidate composition with caps** (parallelized via `asyncio.gather` with a single `asyncio.wait_for(timeout=15s)` budget):
  - Up to 5 from `continue_watching`.
  - Up to 10 from `recently_added`.
  - Up to 15 from a `unwatched_only=True` `search` filtered by the user's `preferred_genres` (a household-level config in v1; per-user preferred-genre config is deferred to v2 — see Open Questions).
  - **Total cap: 30** items (configurable via `recommend_next_max_candidates`, default 30). Each item's `summary` is truncated to the first 200 chars before serialization to control token budget.
  - When `continue_watching` returns nothing (new household, freshly-imported library), the AI prompt's `<recent_history>` is empty and the prompt branches accordingly ("recommend based on `<candidates>` alone — lean on recency and genre fit"). The default prompt explicitly handles this case.
- Passes the prompt + candidates + `<user_intent>` (from the optional parameter) + the user's recent viewing history (last 5 watched, if available) to the AI capability via `AISamplingProvider.complete_one_shot`. Parses the AI's three-item JSON response. Returns three `UIBlock`s with state-aware Play buttons.
- **Graceful degradation**: if `recently_added` times out but `continue_watching` succeeds, recommend with what came back rather than failing the whole tool. Falls back to the first three of `continue_watching` if the AI errors or returns malformed JSON.

#### Button label matrix (UIBlocks across all tools)

UIBlock button labels for items are **state-aware** so the user knows whether clicking Play will start over or pick up where they left off:

| Item state | Button label |
|---|---|
| `view_offset_seconds > 0` (in progress) | `Resume (1:23:45)` (offset rendered as `H:MM:SS` or `M:SS` based on duration) |
| `is_watched=False` and `view_offset_seconds == 0` | `Play` |
| `is_watched=True` and `view_offset_seconds == 0` | `Watch again` |

All three labels invoke `play_media_id` with the same `MediaItem` JSON; the offset is carried in the button's value (and re-resolved at click time per section 5.1).

### 7.3 Admin tools (user mapping)

The admin tools are **convenience shims for the slash-bar workflow**. The canonical path is the Settings UI's User Mappings panel (section 13). Both surfaces share the same backing service methods (`set_user_mapping`, `list_user_mappings`); ConfigActions on the Settings panel and AI tools must not drift. The previously-spec'd duplicate `ConfigAction`s (`relink_user`, `unlink_user`, `list_user_mappings` from section 10.4 round 1) are **removed** — Settings UI invokes the service methods directly via the standard `WsHandlerProvider` capability, no separate ConfigAction layer.

#### `media_library_link_user`
- Slash: `/media link-user <gilbert_user> <backend> <backend_username>`
- Help: "Map a Gilbert user to a Plex/Jellyfin account: /media link-user alice plex alice_plex"
- Required role: `admin`
- Parameters:
  - `gilbert_user: STRING` — required. **Accepts either a Gilbert username or a user_id**, prefers username, falls back to id. On no match, returns `{error: "No Gilbert user named '<name>' or with id '<id>'", available: [...]}`.
  - `backend: STRING` — required, enum `[plex, jellyfin]`.
  - `backend_username: STRING` — required.
- The service looks up the backend user id via the backend's `list_backend_users()` (or the backend's user-by-name endpoint), persists the mapping, returns `{status: "linked", backend_user_id, backend_username}`. Re-linking the same `(gilbert_user, backend)` pair overwrites the existing mapping (the unique index is enforced as upsert at the service layer; no error returned to the admin).

#### `media_library_unlink_user`
- Slash: `/media unlink-user <gilbert_user> <backend>`
- Required role: `admin`
- Parallel-safe: no.

#### `media_library_list_user_mappings`
- Slash: `/media user-mappings`
- Required role: `admin`
- Parallel-safe: yes
- Returns the current Gilbert→backend mappings.

### 7.4 Tool surface summary

| Tool | Slash | Role | Parallel-safe | Gated by |
|---|---|---|---|---|
| `list_media_clients` | `/media clients` | everyone | yes | always |
| `search_media` | `/media search` | user | yes | always |
| `play_on` | `/media play` | user | no | always |
| `play_media_id` | (none — button-invoked) | user | no | always |
| `recently_added` | `/media recent` | everyone | yes | `supports_recently_added` |
| `continue_watching` | `/media on-deck` | user | yes | `supports_continue_watching` |
| `now_playing` | `/media now` | everyone | yes | `supports_now_playing` |
| `playback_control` | `/media pause` / `/media resume` / `/media stop` / `/media seek` | user | no (per-client lock) | seek action gated on `supports_seek` |
| `recommend_next` | `/media recommend` | user | no | `supports_recommend_next` |
| `media_library_link_user` | `/media link-user` | admin | no | always |
| `media_library_unlink_user` | `/media unlink-user` | admin | no | always |
| `media_library_list_user_mappings` | `/media user-mappings` | admin | yes | always |

The `slash_namespace` for `MediaLibraryService` is `"media"` (set as a class attribute on the service per `memory-plugin-system.md`'s slash conventions — except `MediaLibraryService` lives in core, where the service-name-derived default is `media_library`; we override to `"media"` for terseness).

Slash-name attribute convention: `slash_command="link-user"` (kebab-case) — hyphens are legal per the `[a-zA-Z][a-zA-Z0-9_\-]*` regex; the in-code attribute on the `Tool` instance is the same string the user types. Internal Python identifiers stay snake_case (`_tool_link_user`).

---

## 8. Plex Backend (`std-plugins/plex/`)

### 8.1 Layout

```
std-plugins/plex/
    plugin.yaml
    plugin.py
    pyproject.toml
    plex_backend.py
    plex_client.py            # async wrapper around plexapi (run in to_thread)
    __init__.py
    tests/
        conftest.py
        test_plex_backend.py
```

### 8.2 `plugin.yaml`

```yaml
name: plex
version: "1.0.0"
description: "Plex Media Server library + playback backend"

provides:
  - plex_media_library

requires: []
depends_on: []
```

### 8.3 `pyproject.toml`

```toml
[project]
name = "gilbert-plugin-plex"
version = "1.0.0"
description = "Plex Media Server library + playback backend for Gilbert"
requires-python = ">=3.12"
dependencies = [
    # plexapi is mature, well-maintained, and abstracts both the local
    # XML API and the plex.tv cloud account. Synchronous — we wrap calls
    # in asyncio.to_thread to keep the event loop unblocked.
    "plexapi>=4.15.0",
    # httpx for direct calls when plexapi doesn't expose what we need
    # (e.g. /clients/<id>/playMedia for legacy companion clients).
    "httpx>=0.27.0",
]

[tool.uv]
package = false
```

### 8.4 `plugin.py`

```python
"""Plex Media Server plugin — registers the PlexBackend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class PlexPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="plex",
            version="1.0.0",
            description="Plex Media Server library + playback backend",
            provides=["plex_media_library"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import plex_backend  # noqa: F401 — triggers registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return PlexPlugin()
```

### 8.5 `plex_backend.py`

```python
class PlexBackend(MediaLibraryBackend):
    backend_name = "plex"
    supports_now_playing = True
    supports_resume = True
    supports_continue_watching = True
    supports_recently_added = True
    supports_seek = True
    supports_per_user = True
    supports_next_episode = True
```

#### `runtime_dependencies()`

```python
@classmethod
def runtime_dependencies(cls) -> list[RuntimeDependency]:
    return []
```

No system binaries required. Plex talks over HTTP (local LAN or plex.tv-routed); Gilbert never transcodes. No ffmpeg / Chromium / tesseract dependencies.

#### Configuration (`backend_config_params`)

| Key | Type | Sensitive | Restart? | Description |
|---|---|---|---|---|
| `account_token` | STRING | yes | yes | The Plex.tv account token (X-Plex-Token). Obtained via the linking flow (PIN). |
| `server_machine_id` | STRING | no | yes | Machine identifier of the chosen server. Filled by the linking flow's "Choose server" step. |
| `server_url` | STRING | no | no | Override the auto-discovered URL. Empty = let plexapi pick. |
| `verify_tls` | BOOLEAN | no | no | Whether to verify TLS for `https://` server URLs. Default `True`. Some self-signed setups need `False`. |
| `request_timeout_seconds` | NUMBER | no | no | Default 15. |
| `default_user_token` | STRING | yes | no | Optional: fallback X-Plex-Token used for "no Gilbert user mapping" calls. Defaults to `account_token`. |

#### Connection / linking

Two `ConfigAction`s:

- `link_account` — generates a Plex PIN (`POST https://plex.tv/api/v2/pins`), returns the `code` and an `open_url` to `https://plex.tv/link`. Status check polls `GET /api/v2/pins/<id>` until the user authorizes; upon authorization, the response carries the `auth_token` which the action persists into `account_token`.
- `choose_server` — once `account_token` is set, calls `GET https://plex.tv/api/v2/resources` (filtered to `provides=server`), returns the list as `dynamic_choices` for `server_machine_id` so the user picks from a dropdown.
- `test_connection` — verifies `server_url + account_token` resolve to a live server (calls `/identity`).

#### Methods

| Method | Implementation |
|---|---|
| `initialize` | Build a `plexapi.MyPlexAccount(token=account_token)` and pin the chosen `PlexServer`. Cache an `httpx.AsyncClient` for direct calls. |
| `close` | `httpx.AsyncClient.aclose()`; plexapi has no explicit close. |
| `search` | `await asyncio.to_thread(self._server.search, query, mediatype=…)` — translate `MediaKind` into plexapi's `mediatype` strings. Use `backend_user_id` to switch the X-Plex-Token to the home-user's token (cached after first lookup). Map results via `_plex_to_media_item`. |
| `get_item` | `await asyncio.to_thread(self._server.fetchItem, int(item_id))`. |
| `list_libraries` | `[s.title for s in self._server.library.sections()]`. |
| `recently_added` | `self._server.library.recentlyAdded()` (or per-section), wrap in `to_thread`, paginate, sort, slice. |
| `continue_watching` | `self._server.library.onDeck()` for the home user identified by `backend_user_id`. |
| `list_clients` | `self._account.devices()` filtered to `provides`-includes-`player`, plus `self._server.clients()` for legacy companion clients. Convert to `MediaClient`. |
| `now_playing` | `self._server.sessions()` mapped to `MediaSession`. |
| `play` | `client.playMedia(item)` from plexapi when the device is reachable, or `httpx.post("/clients/<id>/playMedia", params=…)` for legacy companions. Pass `offset` parameter for resume. |
| `pause / resume / stop / seek` | `client.pause() / play() / stop() / seekTo(ms)`. |

#### Per-user X-Plex-Token

Plex Home users are independent token holders. The backend caches **two** dicts, both service-lifetime, both keyed by `backend_user_id` (Plex Home user uuid — *not* by Gilbert user id, so no cross-Gilbert-user leakage):

- `self._user_tokens: dict[str, str]` — Plex Home user id → X-Plex-Token.
- `self._user_servers: dict[str, plexapi.PlexServer]` — Plex Home user id → memoized `PlexServer` instance for that user. Constructed once and reused; `plexapi.PlexServer.__init__` performs a synchronous HTTP call to `/identity` to verify the token, so per-call construction is more expensive than the comment "plexapi instances are cheap" implied. Wrap construction in `asyncio.to_thread` and memoize.

Lock granularity: `self._user_locks: dict[str, asyncio.Lock]` — one lock per Plex Home user id, NOT a single global lock. A *global* `asyncio.Lock` over the whole token cache serializes every per-user token fetch across all Gilbert users; the per-Home-user lock dict is what the audit rules require. A second, very short global lock guards the `dict.setdefault` of the per-user lock itself:

```python
self._user_locks_dict_lock = asyncio.Lock()
self._user_locks: dict[str, asyncio.Lock] = {}

async def _get_user_lock(self, backend_user_id: str) -> asyncio.Lock:
    async with self._user_locks_dict_lock:
        return self._user_locks.setdefault(backend_user_id, asyncio.Lock())

async def _get_user_server(self, backend_user_id: str) -> plexapi.PlexServer:
    if backend_user_id == "":
        return self._server   # admin / default-token PlexServer
    lock = await self._get_user_lock(backend_user_id)
    async with lock:
        if backend_user_id in self._user_servers:
            return self._user_servers[backend_user_id]
        token = await asyncio.to_thread(
            self._account.user(backend_user_id).get_token,
            self._server.machineIdentifier,
        )
        self._user_tokens[backend_user_id] = token
        server = await asyncio.to_thread(
            plexapi.PlexServer, self._server.url, token=token,
        )
        self._user_servers[backend_user_id] = server
        return server
```

Two concurrent calls for the *same* Home user serialize through that user's lock; two concurrent calls for *different* Home users do not serialize. On 401 (`plexapi.exceptions.Unauthorized`), the backend evicts both `self._user_tokens[backend_user_id]` and `self._user_servers[backend_user_id]` and re-fetches on the next call (lazy invalidation).

#### `account_token` lifecycle

Plex tokens don't auto-refresh; they're revoked when the Plex.tv user signs out on any device or rotates their password. The backend handles three failure modes:

1. **Plain expiration / revocation** (`plexapi.exceptions.Unauthorized` on any call using `account_token`): the backend transitions to `unhealthy`, emits `media.backend.health_changed`, stops re-trying with the same token until an admin re-runs `link_account`. Subsequent calls return `MediaLibraryUnavailableError("Plex token revoked — re-link in Settings")`.
2. **Admin re-runs `link_account`** (`account_token` changes via `on_config_changed`): the backend **atomically clears all per-Home-user state** — `self._user_tokens`, `self._user_servers`, `self._user_locks` — and re-pins the chosen `PlexServer` with the new token. This avoids the failure mode where a stale Home-user token cache references uuids that no longer resolve under the new admin token.
3. **Encryption at rest**: `account_token`, `default_user_token`, and the per-Home-user tokens cached in memory are `sensitive=True` for log redaction but **not** encrypted in SQLite. This is inherited tech debt across the codebase (Sonos OAuth tokens, Spotify tokens, Plex tokens all share this property). v1 explicitly mandates file-permission hardening on `.gilbert/gilbert.db` (mode `0600`); a generic at-rest encryption story is tracked outside this feature.

### 8.6 Mapping helpers

`_plex_to_media_item(obj: plexapi.video.Video | plexapi.audio.Audio) -> MediaItem` — pure function in `plex_backend.py`. Tests cover the kind-by-kind mapping: Movie, Show, Season, Episode, Artist, Album, Track. URL fields (`thumbUrl`, `artUrl`) are absolute URLs the Plex server returns; the backend passes them through unchanged so the SPA can render them directly (Plex serves art behind X-Plex-Token, so the URLs include the token query param — sensitive enough that we treat them carefully when logging).

### 8.7 Errors

Translated to the domain hierarchy at the backend boundary:

| Upstream | Domain |
|---|---|
| `account_token` unset | `MediaLibraryUnavailableError("Plex not configured")` |
| `plexapi.exceptions.Unauthorized` | `MediaLibraryUnavailableError("Plex token revoked")` (also flips health) |
| `plexapi.exceptions.NotFound` on item / client | `MediaClientNotFoundError` (clients) or returns `None` (items) |
| HTTP error / connection refused | `MediaLibraryUnavailableError("Plex server unreachable")` |
| Any other `plexapi.exceptions.PlexApiException` | `MediaLibraryUnavailableError(str(exc))` |

### 8.8 Tests

`std-plugins/plex/tests/test_plex_backend.py` covers:

- Mapping: each `MediaKind` round-trips through `_plex_to_media_item`. **Each test uses a fixture file `tests/fixtures/plex/<kind>.xml`** captured from a real Plex server (sanitized of tokens / server identity). Without recorded fixtures the mapping helpers are tested against the implementer's *imagination* of the response shape — Plex XML carries dozens of attributes a hand-shaped mock will miss.
- Timezone normalization: a fixture `tests/fixtures/plex/movie_non_utc.xml` (Plex server reporting `addedAt` in a non-UTC timezone) — assert `MediaItem.added_at` is normalized to UTC unix seconds at the mapping boundary.
- Capability flags propagate.
- Auth caching: two concurrent calls for the same Home user share one token fetch (per-user lock); two concurrent calls for *different* Home users don't serialize.
- `account_token` rotation clears all per-Home-user caches.
- Search filters: kind, library_section, year_from/year_to translate to plexapi's filter dict correctly.
- `list_clients` merges account devices + server clients with no duplicate ids; offline clients re-surface from the cache with `is_online=False`.
- `play` invokes the right plexapi call for both companion (`Client.playMedia`) and remote (`/clients/<id>/playMedia`) flows; idempotency-key dedup short-circuits the second call within the 5s window.
- `next_episode` resolves a show to the user's on-deck episode and returns `None` when the user is caught up.
- `MediaLibraryUnavailableError` raised on `Unauthorized`; backend health flips to `unhealthy` and `media.backend.health_changed` event fires.

Tests use mocked `plexapi.PlexServer` (the *external API*, not our own classes — see `CLAUDE.md` test rule "Don't mock the thing you're suppose to be testing"). Where the response shape matters (mapping helpers especially), tests assert against the recorded XML fixtures rather than hand-built mock objects.

#### Fixture regeneration

A `make plex-fixtures` target (or `uv run python scripts/capture_plex_fixtures.py`) hits a real Plex server (URL + token from env vars), captures one response per `MediaKind`, redacts tokens / server identifiers via a regex pass, and writes to `tests/fixtures/plex/`. The script lives in the plugin and is documented in `std-plugins/plex/README.md`. Re-run when plexapi or the Plex API contract changes.

---

## 9. Jellyfin Backend (`std-plugins/jellyfin/`)

### 9.1 Layout

```
std-plugins/jellyfin/
    plugin.yaml
    plugin.py
    pyproject.toml
    jellyfin_backend.py
    jellyfin_client.py        # thin async REST wrapper around httpx
    __init__.py
    tests/
        conftest.py
        test_jellyfin_backend.py
```

### 9.2 `plugin.yaml`

```yaml
name: jellyfin
version: "1.0.0"
description: "Jellyfin Media Server library + playback backend"

provides:
  - jellyfin_media_library

requires: []
depends_on: []
```

### 9.3 `pyproject.toml`

```toml
[project]
name = "gilbert-plugin-jellyfin"
version = "1.0.0"
description = "Jellyfin Media Server library + playback backend for Gilbert"
requires-python = ">=3.12"
dependencies = [
    # We talk to Jellyfin's REST API directly via httpx — the official
    # `jellyfin-apiclient-python` is partially synchronous and missing
    # some endpoints we need (Sessions remote control). REST is well
    # documented and stable.
    "httpx>=0.27.0",
]

[tool.uv]
package = false
```

### 9.4 `plugin.py`

Mirror of Plex's, with `name="jellyfin"` and `from . import jellyfin_backend` in `setup()`.

### 9.5 `jellyfin_backend.py`

```python
class JellyfinBackend(MediaLibraryBackend):
    backend_name = "jellyfin"
    supports_now_playing = True
    supports_resume = True
    supports_continue_watching = True
    supports_recently_added = True
    supports_seek = True
    supports_per_user = True
    supports_next_episode = True
```

#### `runtime_dependencies()`

```python
@classmethod
def runtime_dependencies(cls) -> list[RuntimeDependency]:
    return []
```

No system binaries required (REST over HTTP via httpx).

#### Configuration

| Key | Type | Sensitive | Restart? | Description |
|---|---|---|---|---|
| `server_url` | STRING | no | yes | Base URL e.g. `http://jellyfin.local:8096`. |
| `admin_username` | STRING | no | yes | Admin user (used to bootstrap the device-token; required only at link time). |
| `admin_password` | STRING | yes | yes | Admin password — only used to obtain the access token; NOT persisted after `link_account` unless `keep_password` is true. |
| `device_id` | STRING | no | yes | Stable device identifier Gilbert uses when authenticating (`X-Emby-Authorization`'s `DeviceId`). Defaults to a generated UUID written on first run. |
| `access_token` | STRING | yes | yes | Auto-populated by the link flow. The admin's token. Used for all server-wide queries. |
| `verify_tls` | BOOLEAN | no | no | Default `True`. |
| `request_timeout_seconds` | NUMBER | no | no | Default 15. |

#### Connection / linking

- `link_account` — `POST /Users/AuthenticateByName` with the admin credentials; persists the resulting `AccessToken` into `access_token`. Clears `admin_password` from the config after success unless `keep_password=true` (transient field cleared on save like Sonos's `spotify_auth_code`).
- `test_connection` — `GET /System/Info?api_key=<access_token>` returns the server name + version.

#### Per-user authentication

**v1 design (admin-token + user-id query param):** Jellyfin allows an admin to read other users' libraries by including the `userId` in the URL path or query (`/Users/{userId}/Items/Resume`, etc.). The backend uses the **admin's `access_token`** for all calls and supplies `backend_user_id` as the path parameter when the operation is per-user.

**Trade-off (acknowledged):** every per-user query is logged on the Jellyfin server's audit trail as the admin user. This is documented as a v1 limitation. Per-user-token minting (each Gilbert user maps to a Jellyfin user with their *own* token) is genuinely tricky — Jellyfin's `POST /Users/AuthenticateByName` requires the user's password, which the admin doesn't have. v2 may switch to per-user API-key tokens (Jellyfin 10.9+ supports user-scoped api-keys), but that's a separate scoping decision.

**Reconciliation with the no-mapping-fallback rule:** when a Gilbert user has *no* Jellyfin mapping, per-user tools STILL refuse to fall back to the admin's own user-id (which would leak the admin's continue-watching to whoever is asking). Section 18 stands: unmapped Gilbert users on Jellyfin get `{error: "...not linked..."}`, never silent admin-id fallback. The "admin token" here is the *credential*; the `userId` path parameter is what scopes the data — without a mapping there is no `userId` to send and the call is refused upstream of the HTTP layer.

Mapping a Gilbert user to a Jellyfin user:
- `_resolve_jellyfin_user_id(username)` — `GET /Users` (admin token) → find the user with matching `Name`. **Service-lifetime cache keyed by the Jellyfin username** (NOT by Gilbert user id) — see section 6.3 for the cache-layering explanation. Two Gilbert users mapped to the same Jellyfin username share the resolved id, which is the correct behaviour.

#### Methods

| Method | Implementation |
|---|---|
| `initialize` | Build an `httpx.AsyncClient(base_url=server_url, headers={"X-Emby-Token": access_token})`. |
| `close` | `await client.aclose()`. |
| `search` | `GET /Users/{userId}/Items?searchTerm=<query>&IncludeItemTypes=…&Recursive=true`. |
| `get_item` | `GET /Users/{userId}/Items/{id}`. |
| `list_libraries` | `GET /Users/{userId}/Views`. |
| `recently_added` | `GET /Users/{userId}/Items/Latest?Limit=…`. |
| `continue_watching` | `GET /Users/{userId}/Items/Resume`. |
| `list_clients` | `GET /Sessions` filtered to `SupportsRemoteControl=true`, mapped to `MediaClient`. |
| `now_playing` | `GET /Sessions` filtered to entries with non-null `NowPlayingItem`. |
| `play` | `POST /Sessions/{SessionId}/Playing?ItemIds=<id>&PlayCommand=PlayNow&StartPositionTicks=<offset_ticks>`. `ItemIds` is plural (Jellyfin accepts a comma-separated list) but v1 sends one item per call. |
| `pause / resume / stop` | `POST /Sessions/{SessionId}/Playing/{Pause|Unpause|Stop}`. |
| `seek` | `POST /Sessions/{SessionId}/Playing/Seek?SeekPositionTicks=<position_ticks>`. |
| `next_episode` | `GET /Shows/{showId}/NextUp?UserId=<uid>` first; falls back to `/Shows/{showId}/Episodes?UserId=<uid>&IsPlayed=false&SortBy=ParentIndexNumber,IndexNumber&Limit=1` if NextUp returns empty. |

`StartPositionTicks` and `SeekPositionTicks` are 100-ns ticks. Convert from seconds: `int(seconds * 10_000_000)`.

#### Mapping helper

`_jellyfin_to_media_item(json: dict) -> MediaItem`. Image URLs are constructed from the item's `ImageTags` map: `{server_url}/Items/{Id}/Images/Primary?tag=<tag>&maxHeight=480` for posters, `Backdrop/0` for backdrops. Tests cover all kinds.

### 9.6 Errors

Translated to the domain hierarchy at the backend boundary:

| Upstream | Domain |
|---|---|
| `access_token` unset | `MediaLibraryUnavailableError("Jellyfin not configured")` |
| `httpx.HTTPStatusError(401|403)` | `MediaLibraryUnavailableError("Jellyfin token revoked")` (also flips health) |
| `httpx.HTTPStatusError(404)` on item / session | returns `None` (items) / `MediaClientNotFoundError` (sessions) |
| `httpx.ConnectError` / `httpx.ReadTimeout` | `MediaLibraryUnavailableError("Jellyfin server unreachable")` |
| Other 5xx | `MediaLibraryUnavailableError(f"Jellyfin returned {status}")` |

### 9.7 Tests

`std-plugins/jellyfin/tests/test_jellyfin_backend.py` uses `httpx.MockTransport` for REST stubbing. **Recorded JSON fixtures live in `std-plugins/jellyfin/tests/fixtures/jellyfin/`** — one per `MediaKind` plus session / sessions-list / search responses. Captured from a real Jellyfin server (sanitized via the `make jellyfin-fixtures` target) so the mapping helpers exercise realistic payload shapes (Jellyfin's JSON varies by `IncludeFields` query — fixtures lock in the `IncludeFields` we send). Coverage:

- Auth: `link_account` calls `/Users/AuthenticateByName`, persists token, clears `admin_password`.
- Mapping: each `MediaKind` round-trips through `_jellyfin_to_media_item` against a recorded fixture; image URLs are constructed correctly.
- Timezone normalization: a fixture with `DateCreated` in a non-UTC offset → `MediaItem.added_at` is normalized to UTC seconds.
- `recently_added` correctly translates Latest endpoint pagination.
- `continue_watching` includes `next_up=True` for episodes with offset 0 returned from the Resume endpoint.
- `play` constructs the right `Sessions/<id>/Playing` URL with ItemIds + StartPositionTicks; idempotency dedup short-circuits.
- `seek` translates seconds to ticks (`5.0 → 50_000_000`).
- `next_episode` returns the NextUp episode and falls back to `Episodes?IsPlayed=false&Limit=1` when NextUp is empty; returns `None` when the user has watched everything.
- Auth failure (401) → `MediaLibraryUnavailableError`; backend health flips and `media.backend.health_changed` event fires.

#### Fixture regeneration

A `make jellyfin-fixtures` target (`uv run python scripts/capture_jellyfin_fixtures.py`) hits a real Jellyfin server (URL + admin token from env), captures one response per shape, redacts tokens via regex pass, writes to `tests/fixtures/jellyfin/`. Documented in `std-plugins/jellyfin/README.md`.

#### `python-vcr` consideration

Recording fixtures via `python-vcr` cassettes (replay-on-subsequent-runs) was considered. Decision for v1: **stay with hand-curated fixtures in `tests/fixtures/<backend>/`**. VCR introduces per-test recording state and a regenerate-when-API-changes workflow that's heavier than the explicit `make` target. Revisit if fixture maintenance becomes painful.

---

## 10. Configuration

### 10.1 `MediaLibraryService.config_params()`

```python
config_namespace = "media_library"
config_category = "Media"

def config_params(self) -> list[ConfigParam]:
    params: list[ConfigParam] = [
        ConfigParam("enabled", BOOLEAN, "Enable the media library service.",
                    default=False, restart_required=True),
        ConfigParam("default_client", STRING,
                    "Default client name when the user doesn't specify one. "
                    "Falls back to last-used.",
                    default=""),
        ConfigParam("default_kind", STRING,
                    "Default media kind for ambiguous searches.",
                    default="movie",
                    choices=("movie", "show", "episode", "music_video")),
        ConfigParam("preferred_genres", STRING,
                    "Comma-separated genres used by `recommend_next` to pick "
                    "candidates. Household-level default in v1; per-user "
                    "preferences are deferred to v2. Empty = no preference.",
                    default=""),
        ConfigParam("ai_profile", STRING,
                    "AI profile used for recommend_next and disambiguation "
                    "calls. Avoid 'light' — small models hallucinate client "
                    "names.",
                    default="standard",
                    choices_from="ai_profiles"),
        ConfigParam("client_disambiguation_threshold", INTEGER,
                    "Minimum candidate count before invoking the AI to pick "
                    "a client. Below this, deterministic ordering "
                    "(last-used > online > alphabetical) is used.",
                    default=3),
        ConfigParam("recommend_next_max_candidates", INTEGER,
                    "Cap on candidates passed into the recommend_next AI "
                    "call. Trims to this many before sending; each item's "
                    "summary is truncated to 200 chars.",
                    default=30),
        ConfigParam("backend_timeout_seconds.search", NUMBER,
                    "Per-backend timeout (seconds) for search.", default=8.0),
        ConfigParam("backend_timeout_seconds.recently_added", NUMBER,
                    "Per-backend timeout for recently_added.", default=8.0),
        ConfigParam("backend_timeout_seconds.continue_watching", NUMBER,
                    "Per-backend timeout for continue_watching.", default=5.0),
        ConfigParam("backend_timeout_seconds.now_playing", NUMBER,
                    "Per-backend timeout for now_playing.", default=5.0),
        ConfigParam("backend_timeout_seconds.list_clients", NUMBER,
                    "Per-backend timeout for list_clients.", default=3.0),
        ConfigParam("backend_timeout_seconds.play", NUMBER,
                    "Per-client timeout for play.", default=10.0),
        ConfigParam("poll_now_playing.enabled", BOOLEAN,
                    "Poll for in-progress sessions and emit "
                    "media.playback.started/stopped events.",
                    default=True),
        ConfigParam("poll_now_playing.interval_seconds", INTEGER,
                    "Base poll interval. Adaptive backoff doubles up to "
                    "idle_max_interval_seconds when no sessions seen.",
                    default=30),
        ConfigParam("poll_now_playing.idle_threshold", INTEGER,
                    "Empty polls before backoff kicks in.", default=10),
        ConfigParam("poll_now_playing.idle_max_interval_seconds", INTEGER,
                    "Cap on the backed-off interval.", default=300),
        ConfigParam("poll_recently_added.enabled", BOOLEAN,
                    "Poll for newly-added items and emit "
                    "media.recently_added events. The first poll cycle "
                    "after restart is a baseline run that emits no events.",
                    default=True),
        ConfigParam("poll_recently_added.interval_seconds", INTEGER,
                    "How often to poll recently-added.", default=300),
        ConfigParam("recommend_next_prompt", STRING,
                    "System prompt for the recommend_next AI call. "
                    "Leave blank to use the bundled default.",
                    default=_DEFAULT_RECOMMEND_NEXT_PROMPT,
                    multiline=True, ai_prompt=True),
        ConfigParam("item_disambiguation_prompt", STRING,
                    "System prompt used when AI item disambiguation is "
                    "needed (rare; visual UIBlock picker is the default). "
                    "Leave blank for default.",
                    default=_DEFAULT_ITEM_DISAMBIGUATION_PROMPT,
                    multiline=True, ai_prompt=True),
        ConfigParam("client_disambiguation_prompt", STRING,
                    "System prompt used when the user's named client matches "
                    "multiple devices. Leave blank for default.",
                    default=_DEFAULT_CLIENT_DISAMBIGUATION_PROMPT,
                    multiline=True, ai_prompt=True),
    ]
    # Per-backend enable + settings. Computed lazily on every call —
    # not cached in __init__ — so plugins loaded after the first call
    # still surface in the next Settings refresh (see section 6.2
    # "Plugin load timing").
    for backend_name, backend_cls in MediaLibraryBackend.registered_backends().items():
        params.append(
            ConfigParam(f"backends.{backend_name}.enabled", BOOLEAN,
                        f"Enable the {backend_name} backend.",
                        default=False, restart_required=True),
        )
        for bp in backend_cls.backend_config_params():
            params.append(
                ConfigParam(
                    key=f"backends.{backend_name}.settings.{bp.key}",
                    type=bp.type, description=bp.description,
                    default=bp.default, restart_required=bp.restart_required,
                    sensitive=bp.sensitive, choices=bp.choices,
                    choices_from=bp.choices_from, multiline=bp.multiline,
                    ai_prompt=bp.ai_prompt, backend_param=True,
                )
            )
    return params
```

The forwarding loop **must** propagate `ai_prompt=bp.ai_prompt` (per the architecture checklist's "Backend wrappers must forward `ai_prompt`" rule). Even though our two backends ship no AI-prompt fields today, missing this forwarding would silently break a future backend that does.

### 10.2 `on_config_changed`

Recompute `self._recommend_next_prompt`, `self._item_disambiguation_prompt`, and `self._client_disambiguation_prompt` from the section using the `str(...) or _DEFAULT_*` falsy-fallback pattern. Recompute backend-timeout knobs (`self._backend_timeouts: dict[str, float]`). Re-initialize each enabled backend with its updated `backends.<name>.settings` subsection. If a backend's `account_token` (Plex) or `access_token` (Jellyfin) changed, **clear all per-Home-user / per-user caches** for that backend before re-initializing (see section 8.5 — the `account_token` lifecycle path).

### 10.3 Default `gilbert.yaml` section

The service ships disabled — admins opt in. No default `gilbert.yaml` block is added; the entity-storage config UI is the entry point.

### 10.4 `ConfigAction`s

The aggregator forwards backend-declared actions via `_backend_actions.all_backend_actions(...)`/`invoke_backend_action(...)` (already used by `MusicService`). User-mapping mutations are **not** duplicated as ConfigActions — they live as AI tools (section 7.3) and are invoked by the Settings UI's User Mappings panel (section 13) directly via the WS handler that forwards to the service's `set_user_mapping` / `list_user_mappings` methods. Two surfaces, one backing implementation, no drift.

ConfigActions exposed by the service itself are:

- `test_backend` — admin payload `{backend}`. Pings the named backend (`/identity` for Plex, `/System/Info` for Jellyfin) and returns `{ok: bool, server_name, version, error?}`. Used by the Settings UI's "Test connection" button per backend.

Per-backend ConfigActions (Plex's `link_account` / `choose_server`, Jellyfin's `link_account`) are forwarded by the standard `_backend_actions` machinery — admin invokes `media_library.<backend>.link_account` and the aggregator dispatches to the backend.

---

## 11. Multi-User Isolation

This service touches several user-scoped surfaces. Follow `memory-multi-user-isolation.md` exactly.

### Forbidden in this service

- ❌ `self._current_user_id`
- ❌ `self._active_session`
- ❌ `self._last_user_mapping`
- ❌ Any cache keyed *only* by backend, when the value depends on the user (e.g. a global "continue watching" cache — must be `dict[gilbert_user_id, list[ContinueWatchingEntry]]` or absent).

### Required

- All tool handlers read `arguments["_user_id"]` (injected by `AIService._run_one_tool`). The schemas do **not** declare `_user_id` as a `ToolParameter` — the AI strips it from the tool's JSON schema. **No fallback to `get_current_user()`** — missing `_user_id` is treated as a programmer error and surfaced as a JSON tool error, never silently resolved (see section 6.4).
- The polling loops (`poll_now_playing`, `poll_recently_added`) **explicitly call `set_current_user(UserContext.SYSTEM)` at job entry** (matching the knowledge-service reindex job and the calendar poll job — never relying on the implicit default). They never read per-user state. Inside the job, backend calls pass `backend_user_id=""` (admin / primary user).
- `MediaLibraryService.search`, `recently_added`, etc. accept `gilbert_user_id` as an explicit kwarg — never read context vars implicitly inside library logic. Tools translate `arguments["_user_id"]` into the kwarg at the call boundary.
- Per-Plex-Home-user token caches inside `PlexBackend` are keyed by `backend_user_id` (Plex Home user uuid) — service-lifetime, backend-side user tokens, NOT Gilbert per-request state. Reads/writes are protected by a **per-Home-user `asyncio.Lock`** (`dict[backend_user_id, asyncio.Lock]`), with a short global lock guarding only the `dict.setdefault` of the per-user lock itself. This is correct per the audit rules; a *single* global lock around the whole token cache would serialize every per-user token fetch across all Gilbert users and is explicitly forbidden (Appendix C).
- The polling-diff caches on the service (`self._poll_last_sessions`, `self._poll_last_added_at`, `self._poll_first_run_done`) are service-lifetime and keyed by `(backend_name, session_id)` / `(backend_name, library_section)` / job_id — NOT by Gilbert user. They are correct on `self`.
- The per-client locks (`self._client_locks: dict[tuple[str, str], asyncio.Lock]`) are service-lifetime and keyed by `(backend_name, client_id)`. Per the same rationale: a TV is a backend-side object, not a Gilbert user.
- The per-backend health dict (`self._health: dict[str, BackendHealth]`) is service-lifetime, keyed by `backend_name`. Correct on `self`.

### Audit checklist for this feature

After implementation, walk each instance attribute on `MediaLibraryService`, `PlexBackend`, `JellyfinBackend`. For each, ask:
- Service-lifetime (config, backend handles, registry pointer)? → fine on `self`.
- Per-request (user, conversation, session id)? → must NOT be on `self`.

Run `grep -n 'self\._current_\|self\._active_\|self\._pending_'` across the new files; result must be empty.

---

## 12. Coexistence with `arr`

`arr` and `media_library` are **complementary, never overlapping**:

| Concern | Owner |
|---|---|
| "Find a movie that's not in my library yet, and download it" | `arr` (`radarr_search`, `radarr_add`) |
| "Show me what was downloaded last night" | `arr` (`radarr_recent`, `sonarr_recent`) |
| "What movies do I have in my library?" | `media_library` (`search_media`) |
| "Play it on the living room TV" | `media_library` (`play_on`) |
| "What's playing right now?" | `media_library` (`now_playing`) |
| "Resume my show" | `media_library` (`continue_watching`) |
| "When does this season's next episode air?" | `arr` (`sonarr_upcoming`) — air-date, not playback |

The seam is **acquisition vs consumption**. `arr` writes files to disk; Plex/Jellyfin index those files; `media_library` plays them. The AI's mental model (encoded in tool descriptions) should reinforce this — `radarr_add`'s description starts "Add a movie to Radarr and start searching for a download" while `play_on`'s starts "Play a movie/show/episode on a media client (TV, phone, …)".

#### Cross-service flow example

User says: *"Get me Dune Part Two and play it tomorrow night."*

The AI executes:

1. `radarr_find("Dune Part Two")` → user clicks Add, Radarr starts download.
2. (Later, after the file lands.) `media_library_recently_added(kind="movie", limit=5)` to confirm it's now in the library.
3. The user (or an automation) eventually invokes `play_on(title="Dune Part Two", client="Living Room TV")`.

Both services emit events the agent service can subscribe to:
- `arr` emits its own download-complete events (already exists).
- `media_library` emits `media.recently_added` when Plex/Jellyfin index the new file.
- An autonomous agent can chain on `media.recently_added` — no service-to-service coupling needed.

#### What we explicitly do not do

- We do not auto-discover the same movie in both Radarr and Plex and link them. The AI naturally bridges via title.
- We do not surface `arr`-pending downloads inside `media_library_recently_added`. The boundary is "indexed and playable" vs "queued in the downloader."
- `media_library` does **not** trigger Radarr / Sonarr searches. If a user asks "play X" and X isn't in the library, the response is `{error: "X not in library", suggestion: "/radarr.find X"}`.

---

## 13. Web Layer Touch Points

`MediaLibraryService` is a `WsHandlerProvider` only via the standard `ai_tools` capability — there are no custom WebSocket frames. WS calls into the service for the Settings UI go through the existing `service.method.invoke` envelope (the same one Settings uses for any service exposing read-only methods), specifically `media_library.list_backend_users(backend)`, `media_library.list_user_mappings()`, `media_library.set_user_mapping(...)`, and `media_library.list_backend_health()`.

### 13.1 User Mappings Settings panel

The most important UX surface in this feature. **`<PluginPanelSlot slot="settings.media_library">` is declared by the core SPA** (this is a core service, so the slot lives in the core SPA, not a plugin's `frontend/`).

The panel mounts a `MediaLibraryUserMappings` component that renders one table per configured backend:

```
Plex
+---------------------+---------------------+--------+----------+
| Gilbert User        | Plex Home User      | Test   | Unlink   |
+---------------------+---------------------+--------+----------+
| Alice               | [alice_plex     ▼]  | [Test] | [Unlink] |
| Bob                 | [bob_kids       ▼]  | [Test] | [Unlink] |
| (Add new mapping…)  | [Choose Plex... ▼]  |        |          |
+---------------------+---------------------+--------+----------+

Jellyfin
+---------------------+---------------------+--------+----------+
| ...                                                            |
```

Behaviour:

- Rows: Gilbert users from `auth.list_users()`. The Plex/Jellyfin user dropdown is populated from `service.list_backend_users(backend)` (which calls the backend's `list_backend_users()` ABC method).
- The dropdown is **dynamic_choices** — refetched on panel mount and on a "Refresh" button click. Choices include `id`, `username`, and `display_name` for clarity ("alice_plex_home — Alice in the Family").
- "Test" sends a no-op auth check using that user's id (Plex: `account.user(id).get_token(machine_id)` succeeds; Jellyfin: `GET /Users/{userId}` returns 200). Shows a green check on success, red X with message on failure.
- "Unlink" calls `service.set_user_mapping(...)` with the unlink semantic (or `unlink_user`).
- "Add new mapping" row only appears for Gilbert users without a mapping for that backend.
- Save button at the bottom commits all pending edits in one batch.

The slash commands (`/media link-user`, `/media unlink-user`) remain as **convenience shims** for admins who prefer the keyboard. Both surfaces invoke the same backing service methods. The Settings UI is the **documented path**; admins onboarding a Plex Home household with five users do five rows of dropdowns, not five slash invocations.

### 13.2 Backend Health banner

The Settings panel also renders a per-backend health banner driven by `service.list_backend_health()`:

```
[●] Plex     — healthy (last success 12s ago)
[●] Jellyfin — unhealthy (token revoked — re-link below)
```

A red dot means `unhealthy`; yellow `degraded`; green `healthy`. A `media.backend.health_changed` event subscription pushes updates to the panel without requiring a refresh.

### 13.3 Plugin-contributed frontends (optional, additive)

The two plugins (`plex/`, `jellyfin/`) may optionally ship a `frontend/` directory with a backend-specific widget mounted at `<PluginPanelSlot slot="settings.media_library.<backend>">` — for example, a Plex "Choose Server" wizard that builds on `link_account`. v1 ships **backend-only**; the frontends can land in a follow-up. Per `memory-plugin-ui-extensions.md`, this is purely additive.

### 13.4 Browse / library SPA page

Out of scope for v1. The Settings panel ships with this feature; a discoverable browse-and-pick page is v2 work.

---

## 14. Boot Wiring (`app.py`)

Add the registration line in alphabetical order alongside other `core/services/*` registrations:

```python
from gilbert.core.services import (
    ...
    MediaLibraryService,
    ...
)

self.service_manager.register(MediaLibraryService())
```

Update `src/gilbert/core/services/__init__.py` to export `MediaLibraryService`. Plugins (Plex, Jellyfin) load themselves; the side-effect import in each plugin's `setup()` populates `MediaLibraryBackend._registry`.

Boot order is determined by `service_info().requires` / `optional`, not by the call order in `app.py`. `MediaLibraryService` declares `requires=frozenset({"entity_storage"})` and `optional={"configuration", "event_bus", "ai_chat", "scheduler"}`; the service manager resolves the topological order. `agent` and `notifications` declare `optional={"media_library"}` if they wish to subscribe to media events at start-up; lacking that, they obtain the capability lazily via `resolver.get_capability("media_library")` after start.

---

## 15. Storage Migrations

None required. Both new collections are created lazily via `ensure_index` on first start. No data shape changes to existing collections.

---

## 16. Logging

Two new module loggers:

- `gilbert.core.services.media_library` — service lifecycle, fan-out fan-in events, config changes.
- `gilbert_plugin_plex.plex_backend` and `gilbert_plugin_jellyfin.jellyfin_backend` — per-backend HTTP errors, mapping fallbacks, token expiry.

Sensitive fields (`account_token`, `access_token`, image URLs containing tokens) MUST be filtered in logs. Use the existing `_redact_sensitive` helper in `core/services/_logging.py` if it exists; otherwise add one in the service. The redactor must handle **query-string secrets** specifically — Plex media URLs include the `X-Plex-Token` as a query parameter, and Jellyfin debugging URLs may include `?api_key=…`. Patterns to redact:

- `X-Plex-Token=[^&\s]+` → `X-Plex-Token=<REDACTED>`
- `\?api_key=[^&\s]+` → `?api_key=<REDACTED>`
- Full-string match for any value of `account_token`, `access_token`, `default_user_token`, `admin_password` from the active config.

A new entry in the AI API call log fires for `recommend_next` invocations of `complete_one_shot`. No special handling needed — the AI service already logs `complete_one_shot`.

---

## 17. Error Handling Strategy

Tools never raise out of `execute_tool`. Patterns:

- Per-backend transient errors (one of two backends down) → log WARN, drop from merged result, continue.
- Per-backend auth errors → log WARN, return JSON `{error: "<backend> authentication failed; check Settings"}`.
- Client not found → JSON `{error: "No client named '<name>' on any configured backend", available: [...]}` with the available client list so the AI can offer alternatives.
- All backends failed → JSON `{error: "Media library is unavailable on all configured backends", details: [...]}`.
- Capability-gated tool called when no backend supports it → the tool isn't registered, so the AI can't call it. Slash users hitting it directly get the standard "Unknown tool" error from the slash-dispatch layer.

`MediaLibraryUnavailableError` and `MediaClientNotFoundError` are caught in the tool layer; `KeyError`, `ValueError`, `httpx.HTTPError`, `plexapi.exceptions.PlexApiException` are caught at the backend boundary and translated to one of those two domain errors.

---

## 18. Privacy & Security

- `account_token` (Plex), `access_token` and `admin_password` (Jellyfin), and `default_user_token` (Plex) are all `sensitive=True`. They never appear in logs or events. Encryption-at-rest is inherited tech debt across the codebase (Sonos / Spotify tokens have the same property); v1 mandates `0600` file mode on `.gilbert/gilbert.db`. A generic at-rest encryption story is tracked outside this feature.
- Plex media URLs include the X-Plex-Token as a query parameter. Treat URLs the same way as tokens for log redaction (see section 16 for the regex).
- **Browser-side rendering**: poster URLs are handed to the same browser that authenticated to Gilbert, so live rendering is fine. **But:** chat-history exports (e.g., conversation-export JSON dumps) MUST redact `X-Plex-Token` query params before serialization. The export pipeline filters message contents through the same `_redact_sensitive` regex pass; tested as a non-functional requirement.
- A "proxy poster URLs through Gilbert" approach (`GET /media/proxy/<backend>/<id>/poster` — Gilbert reads upstream with the admin token, streams to the SPA) was considered and **deferred to v2**. The benefit (no token leak in browser dev tools) doesn't justify the bandwidth cost on every poster fetch in v1; export-time redaction closes the most important leak.
- Per-user mappings are admin-write-only. A regular user cannot link their own Gilbert account to an arbitrary Jellyfin user — that's the admin's job, both because it's an authorization decision and because the lookup requires admin token access.
- When a Gilbert user has no mapping for a given backend, per-user tools (`continue_watching`) follow the per-backend missing-mapping policy in section 6.3 — silently skip un-mapped backends, surface results from mapped ones, hint about the missing link in response metadata. **Never silently fall back to the admin token** (which would leak another user's history).
- Restricted-library leakage: poll-detected `media.recently_added` events run as `SYSTEM` and may include items a per-user mapping cannot see (Plex Home library restrictions). Subscribers MUST re-filter via `service.user_can_see(...)` before delivering to a per-user UI. See section 6.5.

---

## 19. Tests

### 19.1 Service-level (in `tests/unit/test_media_library_service.py`)

- **`test_aggregator_merges_search_across_backends`**: two fake `MediaLibraryBackend`s, each returning two items; `service.search("dune")` returns four, ordered by stable round-robin (backend A first, then backend B, alternating).
- **`test_aggregator_drops_failing_backend`**: one backend raises `MediaLibraryUnavailableError`; result still contains the other backend's items, error logged at WARN, that backend's health flips to `unhealthy`.
- **`test_aggregator_per_backend_timeout`**: one backend hangs for >timeout; `asyncio.wait_for` cancels its call, the merged result returns immediately with the other backend's data; that backend's health flips to `degraded`.
- **`test_search_limit_capped_at_50`**: AI passes `limit=10000`; the service-side cap trims to 50.
- **`test_recently_added_caps_after_merge`**: backends each return 10; service called with `limit=5` returns exactly 5.
- **`test_continue_watching_uses_per_user_mapping`**: stubbed mapping `(alice → "u_plex_42")`; the fake backend's `continue_watching` is called with `backend_user_id="u_plex_42"`.
- **`test_continue_watching_partial_mapping_returns_unmapped_hint`**: alice has Plex but no Jellyfin; the response includes Plex entries plus `unmapped_backends: ["jellyfin"]` and a hint string. Critical: Jellyfin is **not** queried with admin-fallback.
- **`test_continue_watching_no_mapping_returns_error`**: user with no mappings on any backend → tool returns `{error: ..., suggestion: "/media link-user"}`.
- **`test_search_concurrent_users_no_state_leak`** (the most important multi-user test in the suite): kick off two `search` calls under different `set_current_user(...)` contexts using `asyncio.gather` with `context=copy_context()` for each branch — *not* a sequential-then-assert version that catches nothing. Assert each call's tool path resolved its OWN `_user_id` from the injected arg (verified by capturing the `backend_user_id` passed to the fake backend per call).
- **`test_play_on_show_resolves_next_episode`**: fake backend's `next_episode` returns S2E3 for a SHOW item; `play_on(title="severance", client="tv")` plays S2E3, NOT the pilot. When the user is caught up (`next_episode` returns `None`), the tool returns the "caught up" UIBlock with Restart / Show upcoming buttons.
- **`test_play_on_visual_disambiguation`**: search returns 3 high-confidence matches for "Inception"; `play_on` returns 3 UIBlocks (no playback) with Play buttons; clicking one fires `play_media_id`.
- **`test_play_on_idempotency_dedup`**: two calls to `play_on(...)` with the same `(client, item)` within 5 seconds — the second is short-circuited (no second backend call) and returns the first's outcome.
- **`test_per_client_lock_does_not_serialize_across_clients`**: two parallel `play_item` calls to *different* clients; both complete without serialization (per-client lock, NOT global).
- **`test_play_emits_event`**: stub event bus, call `play_item(...)`, assert one `media.playback.started` Event with the right payload.
- **`test_play_button_reresolves_view_offset`**: User A's `MediaItem` (with `view_offset_seconds=1842`) is serialized into a Play button; User B clicks. `play_media_id` calls `get_item(item_id, backend_user_id=<B's mapped id>)` and uses B's offset (or 0), NOT 1842.
- **`test_now_playing_tool_bypasses_cache`**: tool path queries each backend live; assert the underlying `now_playing()` is called once per tool invocation regardless of how recently the poll fired.
- **`test_now_playing_poll_emits_started_stopped`**: simulate two consecutive `now_playing()` returns differing by one session; assert one `media.playback.started` then one `media.playback.stopped` event.
- **`test_now_playing_poll_no_state_change_event_in_v1`**: a session transitions PLAYING → PAUSED → PLAYING across three polls; assert no `state_changed` event fires (v1 documented limitation).
- **`test_now_playing_poll_adaptive_backoff`**: 10 consecutive empty polls; assert the next interval doubles. A `media.playback.started` event resets the cadence.
- **`test_recently_added_poll_baseline_is_silent_on_first_run`**: first poll cycle populates `_poll_last_added_at` and `_poll_first_run_done` but emits NO events. The second cycle, with one new item, emits exactly one event.
- **`test_recently_added_poll_includes_library_section_for_filtering`**: emitted events carry `library_section` so subscribers can re-filter for restricted-library users.
- **`test_capability_gating_now_playing_off`**: both fake backends have `supports_now_playing=False`; `service.get_tools()` does NOT include `now_playing`. `playback_control` (pause/resume/stop) stays registered (those don't gate on this flag).
- **`test_tool_remains_registered_when_only_backend_unhealthy`**: configured Plex backend has `supports_seek=True` but is currently `unhealthy`. `service.get_tools()` STILL includes `playback_control` with the `seek` action; calling it surfaces `{error: "Plex unavailable"}` in the result. Tools never disappear because of health flips.
- **`test_recommend_next_prompt_falls_back_to_default_when_blank`**: set the config to `""`; `service._recommend_next_prompt` matches `_DEFAULT_RECOMMEND_NEXT_PROMPT`. The test reads `self._recommend_next_prompt` at the call site, NOT the constant directly.
- **`test_recommend_next_includes_user_intent`**: tool called with `intent="something funny under 90 minutes"`; assert the prompt sent to `complete_one_shot` contains that string in a `<user_intent>` block.
- **`test_recommend_next_caps_candidates_at_30`**: 60 candidates available (continue_watching=10, recently_added=20, genre_search=30); the prompt sees exactly 30 with truncated summaries.
- **`test_recommend_next_handles_empty_history`**: new household, `continue_watching=[]`; the prompt doesn't claim a watch history exists; the tool still returns three picks from `recently_added`/genre-search.
- **`test_recommend_next_returns_three_blocks`**: stub AISamplingProvider returns canned JSON; tool returns three `UIBlock`s.
- **`test_recommend_next_falls_back_on_invalid_ai_response`**: AI returns garbage; tool returns first three of `continue_watching` instead.
- **`test_user_mapping_unique_index`**: persisting two mappings for `(user_id, backend_name)` upserts — the second overwrites; the unique index is enforced at the storage layer, the service handles the overwrite path.
- **`test_position_parser_accepts_lenient_units`**: `"1h22m"` → 4920, `"1:22:00"` → 4920, `"3700"` → 3700, `"5m"` → 300, `"5min"` → 300, `"5 minutes"` → 300, `"1:22"` → 82 (M:SS), `" 5 mins "` → 300. Negative inputs raise / return error (seek is absolute).
- **`test_button_label_state_matrix`**: items with `view_offset>0` render `Resume (1:23:45)`; unwatched render `Play`; watched render `Watch again`.

### 19.2 Plex backend tests (in `std-plugins/plex/tests/`)

Already enumerated in section 8.8. Use `MagicMock` for `plexapi.PlexServer` (an external dependency, not our class).

### 19.3 Jellyfin backend tests (in `std-plugins/jellyfin/tests/`)

Use `httpx.MockTransport` for REST stubbing — the standard pattern for httpx-based plugins. Tests cover:
- Auth: `link_account` calls `/Users/AuthenticateByName`, persists token.
- Mapping: each `MediaKind` round-trips; image URLs are constructed correctly.
- `recently_added` correctly translates Latest endpoint pagination.
- `continue_watching` includes `next_up=True` for episodes with offset 0 returned from the Resume endpoint.
- `play` constructs the right `Sessions/<id>/Playing` URL with ItemIds + StartPositionTicks.
- `seek` translates seconds to ticks (`5.0 → 50_000_000`).
- Auth failure (401) → `MediaLibraryUnavailableError`.

### 19.4 Test fakes

A reusable `FakeMediaLibraryBackend` in `tests/unit/_fakes/media_library.py` (matching the existing `tests/unit/_fakes/` convention used by other services' fakes — e.g. `_fakes/auth.py`, `_fakes/storage.py`; locate per the codebase's actual layout when implementing) — concrete `MediaLibraryBackend` subclass with all capability flags `True`, in-memory item / client / session lists, configurable error injection (e.g. `fake.fail_next("search", MediaLibraryUnavailableError(...))` and `fake.hang_next("search", duration=10)` for timeout tests). Used by all service-level tests.

Test fakes implement the `MediaLibraryBackend` ABC fully — they ARE the thing being tested-against, not a mock of internal classes. Per `CLAUDE.md`: "Don't mock the thing you're suppose to be testing." Mocks are reserved for *external* dependencies (`plexapi.PlexServer`, `httpx.AsyncClient`).

---

## 20. Phasing & Milestones

The spec is delivered in three slices:

### M1 — Interface + Service skeleton
- `interfaces/media_library.py` complete.
- `core/services/media_library.py` registers without backends, exposes config, no tools.
- Boot wiring in `app.py`.
- User-mapping CRUD + tests.
- The aggregator runs cleanly with **zero** registered backends (returns empty everywhere); proves the no-backend path doesn't crash.

### M2 — Plex backend
- `std-plugins/plex/` scaffolded with `runtime_dependencies() -> []`.
- `PlexBackend` ships with all six capabilities (`now_playing`, `resume`, `continue_watching`, `recently_added`, `seek`, `per_user`, `next_episode`).
- Settings UI link flow + User Mappings panel (section 13) work end-to-end against a real Plex Cloud account.
- Tools `search_media`, `recently_added`, `continue_watching`, `now_playing`, `list_media_clients`, `play_on` (with episode resolution + visual disambiguation), `playback_control` all functional.
- Recorded fixtures in `tests/fixtures/plex/` and `make plex-fixtures` regeneration target.
- Tests for backend + service (via `FakeMediaLibraryBackend` fed Plex-shaped inputs).
- README updates (`README.md` integration table, `std-plugins/README.md` plugin row + detail section).

### M3 — Jellyfin backend + recommend_next
- `std-plugins/jellyfin/` scaffolded with `runtime_dependencies() -> []`.
- `JellyfinBackend` parity with Plex.
- `recommend_next` tool wires through `AISamplingProvider` with `intent` parameter, candidate cap, and warmer prompt default.
- Polling loops (`poll_now_playing` with adaptive backoff, `poll_recently_added` with baseline-run sentinel) ship.
- Backend-health surface (`media.backend.health_changed`, `list_backend_health()`, Settings panel banner).
- Cross-service test: with both backends configured, ensure search merges and `unmapped_backends` hint surfaces correctly.
- Recorded fixtures in `tests/fixtures/jellyfin/` and `make jellyfin-fixtures` regeneration target.
- README updates.

Each milestone is independently shippable. M1 alone gives admins a Settings page with no backends to enable yet; M2 makes it useful for Plex households; M3 brings parity.

---

## 21. Memory Updates

After implementation, two new memories are added to `.claude/memory/MEMORIES.md`:

```markdown
- [Media Library Service](memory-media-library-service.md) — multi-backend Plex/Jellyfin library + casting, per-user mapping, capability-gated tools
- [Plex / Jellyfin Backends](memory-media-library-backends.md) — backend-specific gotchas (Plex Home user tokens, Jellyfin admin-token fan-out, ticks vs seconds)
```

The first mirrors `memory-music-service.md` in structure; the second mirrors `memory-speaker-system.md`'s "Sonos Backend" section. Both are **created in the same commit that lands the implementation** (per `CLAUDE.md` "Keeping Memories Current").

The plugin-specific repo (`std-plugins/`) gets its own pair of memories under its `.claude/memory/`:

```markdown
- [Plex backend](memory-plex-backend.md) — plexapi wrapping, Home-user token caching, /clients fallbacks
- [Jellyfin backend](memory-jellyfin-backend.md) — REST direct via httpx, admin-token fan-out, ticks
```

Add corresponding entries to `std-plugins/CLAUDE.md` if it lists notable plugins.

---

## 22. Documentation Updates

Per the "Documentation Freshness" architecture-checklist section:

- **`README.md` (Gilbert root)** — add `Plex` and `Jellyfin` rows to the integrations table; mention `media_library` service in the Media section. Update if the bundled-integrations list mentions specific backends.
- **`std-plugins/README.md`** — add a row to the plugin table and a per-plugin detail section for both `plex` and `jellyfin`. Each detail section lists: what it provides (`media_library` backend), deps with version floors, primary config keys, slash commands (none — handled by the core `MediaLibraryService`), and OS-level prerequisites (none).
- **`std-plugins/CLAUDE.md`** — Insert `MediaLibraryBackend` into the existing "Key Interfaces" backend list, in alphabetical order between `EmailBackend` and `MusicBackend`. The placement is mechanical (alphabetical), not editorial.
- **`CLAUDE.md` (root)** — no change. The new service is one of many and follows existing conventions.

Documentation is delivered in the **same commit** as the code that introduces it. README drift is treated as a regression.

---

## 23. Open Questions Deferred to Implementation / Future Versions

These do not block the spec — they're either choices the implementer can make in flight, or scope deferrals to v2.

1. **Plex per-user-token cache invalidation.** v1: lazy on next 401 (clear and re-fetch via the eviction path in section 8.5). No proactive expiry.
2. **Jellyfin clients with `SupportsRemoteControl=false`.** v1 includes them, flagged `supports_remote_control=False` on the `MediaClient` so the AI doesn't try `play_on` against them.
3. **`recommend_next` candidate-set construction.** The spec's heuristic (continue_watching + recently_added + unwatched preferred-genres, capped at 30) is a starting point — tune empirically.
4. **Casting *to a music speaker* via `play_on` for `music_track`.** Out of scope for v1 — overlaps `MusicService`. Tool descriptions explicitly draw the seam (search_media default-excludes `MUSIC_*` kinds; `play_on` description says "video content"). The user explicitly opts in to music kinds.
5. **Webhook / SSE-based now-playing for v2.** Plex Pass supports webhooks (`/library/sections/onWebhook`); Jellyfin supports SSE on `/socket` for session events. v1 ships polling; v2 will add a webhook/SSE event source that bypasses the 30s cadence and emits `media.playback.state_changed` events for pause/resume that v1 polling cannot reliably detect.
6. **Per-user `preferred_genres` for `recommend_next`.** v1 is household-level; v2 may move to a per-user setting via a new `media_library_user_preferences` collection. Defer until usage data justifies it.
7. **Multi-mapping per Gilbert user (one Gilbert user → many Plex Home users).** v1 enforces 1:1 per backend via the unique index. v2 may relax to N:M for "household merged view" use cases — but the spec needs a primary-flag or fan-out-and-merge semantic before the index can be relaxed. Defer.
8. **Per-user-token minting for Jellyfin** (replacing admin-token + UserId-query-param fan-out). v1 audit trail logs all per-user queries as the admin user. v2 may use Jellyfin's user-scoped api-keys (10.9+) once they're stable enough.
9. **Poster-URL proxy through Gilbert.** v1 hands raw upstream URLs (Plex token in query string) to the SPA, with export-time redaction. v2 may proxy via `GET /media/proxy/<backend>/<id>/poster` to remove the leak entirely.
10. **`MediaLibraryProvider` Protocol surface.** v1 exposes `search`, `recently_added`, `continue_watching`, `list_clients`, `now_playing`, `list_backend_health`. If notifications service or agents need write access, they take a hard dependency on the concrete service. Do not extend the Protocol with mutations.
11. **Restricted-library aware `recently_added` event payloads.** v1 emits events with `library_section` and expects subscribers to re-filter. v2 may switch to per-user polling (N polls instead of 1) for households with strict isolation needs. Defer until a real household reports the leak.
12. **Adaptive backoff applied to `poll_recently_added`.** v1 ships adaptive backoff for `poll_now_playing` only. Recently-added churn is more uniform, so the value is unclear; revisit if polling cost becomes painful.

---

## 24. Acceptance Criteria

The feature is **done** when all of the following hold:

1. `MediaLibraryBackend` ABC exists in `interfaces/media_library.py` with the methods, dataclasses, capability flags, and errors specified in section 5. The `MediaLibraryProvider` Protocol's method signatures match `MediaLibraryService`'s methods exactly (kwarg names included).
2. `MediaLibraryService` is registered in `app.py`, declares `media_library` capability, exposes the AI tools listed in section 7 (11 user-facing + 3 admin = 14 total), and follows the Configurable / ConfigActionProvider / ToolProvider patterns.
3. The service exposes `recommend_next_prompt`, `item_disambiguation_prompt`, and `client_disambiguation_prompt` as `ConfigParam(multiline=True, ai_prompt=True)` and reads the cached values (`self._recommend_next_prompt`, etc.) at the call site, never the `_DEFAULT_*` constants. The falsy-fallback pattern (`str(...) or _DEFAULT_*`) is used so empty-string overrides resolve to defaults.
4. `std-plugins/plex/` and `std-plugins/jellyfin/` exist with `plugin.yaml`, `plugin.py`, `pyproject.toml`, backend module, and tests. `uv sync` installs both. `uv run pytest` passes. Both backends declare `runtime_dependencies() -> []`.
5. The aggregator runs end-to-end against a real Plex server **and** a real Jellyfin server (manual smoke; not part of unit tests). Recorded fixtures (`tests/fixtures/plex/`, `tests/fixtures/jellyfin/`) are committed and the `make plex-fixtures` / `make jellyfin-fixtures` regeneration targets are documented.
6. Tools invoked by two concurrent users return user-correct results (no leak; verified by `test_search_concurrent_users_no_state_leak` using `asyncio.gather` with `context=copy_context()` per branch).
7. `media.playback.started`, `media.playback.stopped`, `media.recently_added`, and `media.backend.health_changed` events fire when expected. Poll-detected events carry `user_id=""` and `initiator="external"`; the `recently_added` first cycle after restart is silent (baseline-run sentinel).
8. Per-user mapping is admin-write-only; a regular user invoking `media_library_link_user` is rejected with the standard role-error response. The Settings UI's User Mappings panel and the slash commands share the same backing service methods (no ConfigAction duplicate).
9. Sensitive fields (`account_token`, `access_token`, `admin_password`) and `X-Plex-Token=…` query strings in URLs never appear in logs (verify with `grep` against the captured log output during a full run-through). Chat-export pipeline redacts the same.
10. Per-backend timeouts are enforced (`asyncio.wait_for` per backend on every fan-out); a hung backend cannot block the AI turn beyond the configured timeout.
11. Per-client `asyncio.Lock` keyed by `(backend_name, client_id)` serializes `play_item` for the same client; unrelated clients run in parallel. Idempotency-key dedup short-circuits same-key plays within 5s.
12. `play_on` resolves a SHOW or SEASON to the user's next-unwatched / on-deck episode via `next_episode`, NOT the pilot. When the user is caught up, the "caught up" UIBlock surfaces instead of silent restart.
13. `play_on` returns visual UIBlock disambiguation (poster cards) when ≥2 high-confidence matches exist; the AI item-disambiguation prompt is reserved for non-interactive paths.
14. Capability-gating reflects "configured-and-supports-X," NOT "currently-healthy-and-supports-X" — tools never disappear mid-conversation due to backend health flips.
15. `search_media` default-excludes `MUSIC_*` kinds when `kind` is unset; tool descriptions explicitly draw the `play_on` (video) ↔ `play_music` (audio) seam.
16. README, `std-plugins/README.md`, `std-plugins/CLAUDE.md`, and the two new memories land in the same commit as the implementation.
17. Architecture audit (`memory-architecture-checklist.md`) passes against the new files: zero layer violations, zero hardcoded prompts, zero per-request state on `self` (`grep -n 'self\._current_\|self\._active_\|self\._pending_'` returns empty across the new files), zero concrete-class `isinstance` checks, slash commands properly grouped under `/media`.

---

## Appendix A — Sample tool result payloads

For implementer reference; not normative. Field names match the `MediaItem` / `MediaSession` / `MediaClient` shapes in section 5.

### `search_media("dune")`

```json
{"results": [
  {"id": "1234", "backend_name": "plex", "title": "Dune: Part Two",
   "kind": "movie", "year": 2024, "duration_seconds": 9960, "rating": 8.5,
   "poster_url": "https://plex.local/…", "is_watched": false},
  {"id": "5678", "backend_name": "jellyfin", "title": "Dune (2021)",
   "kind": "movie", "year": 2021, "duration_seconds": 9300, "is_watched": true}
]}
```

The accompanying `UIBlock`s carry per-result Play buttons whose values are the full `MediaItem` JSON (mirroring `MusicService`).

### `now_playing()`

```json
{"sessions": [
  {"session_id": "sess-7", "backend_name": "plex",
   "client": {"client_id": "tv-living-room", "name": "Living Room TV",
              "device": "Apple TV 4K"},
   "item": {"id": "1234", "title": "Dune: Part Two", "kind": "movie"},
   "state": "playing", "position_seconds": 1842.5,
   "is_transcoding": false, "quality_label": "Original (4K HDR)"}
]}
```

### `play_on(...)` results

- Success: `{"status": "playing", "title": "...", "client": "...", "backend": "plex", "offset_seconds": 0.0}`
- Resume: same with `"resumed": true` and a non-zero offset.
- Client not found: `{"error": "No client named '...' on any configured backend", "available": [...]}`
- Item not found: `{"error": "Nothing in your library matches '...'", "suggestion": "/radarr.find ... — to add it"}`

---

## Appendix B — Slash command tree (final)

```
/media clients                                       — list TVs / phones / etc.
/media search <query> [kind=…]                       — search the library
/media play <title> on <client>                      — search + cast to client
/media recent [kind=…] [limit=10]                    — recently added
/media on-deck                                       — continue-watching list
/media now [client]                                  — what's playing right now
/media pause [client]                                — pause active session
/media resume [client]                               — resume the paused session
/media stop [client]                                 — stop active session
/media seek <position> [client]                      — jump (e.g. /media seek 1h22m bedroom-tv)
/media recommend [kind=…] [intent=…]                 — AI picks 3 things to watch
/media link-user <gilbert_user> <backend> <backend_user>     [admin]
/media unlink-user <gilbert_user> <backend>                  [admin]
/media user-mappings                                         [admin]
```

Slash autocomplete examples (the `slash_help` strings the user actually sees):

- `/media seek` → "Jump to a position: /media seek 1h22m bedroom-tv"
- `/media on-deck` → "Show what to resume: /media on-deck"
- `/media play` → "Play a movie/episode on a TV: /media play <title> on <client>"

All collapsed under `/media` — 11 user-facing + 3 admin slashes mapping to **12 tool entries** (`playback_control` is one tool with four pre-filled-action slashes; admin `media_library_*` are separate). Distinct from `/radarr` and `/sonarr` which own acquisition.

#### Slash naming notes

- `/media on-deck` (continue-watching) replaces the original `/media resume` to free the natural-feeling slash for the playback resume action. `/media resume` is what humans instinctively type when the TV is paused; the previous `/media resume-playback` was a footgun.
- `/media seek 1:22` is interpreted as M:SS (82 seconds) — minutes:seconds is the intuitive default for media; explicit H:MM:SS is `/media seek 1:22:00`.

---

## Appendix C — Anti-patterns to avoid

For implementers who reach for shortcuts.

- ❌ **Importing `plexapi.PlexServer` from `core/services/media_library.py`.** Layer violation. `plexapi` only exists inside `std-plugins/plex/`.
- ❌ **`isinstance(svc, MediaLibraryService)`** in any consumer. Use `isinstance(svc, MediaLibraryProvider)`.
- ❌ **`from gilbert.core.services.media_library import MediaLibraryService`** in a plugin (`std-plugins/plex/...`). Plugins import only from `gilbert.interfaces.*`.
- ❌ **Caching the active user on the service** (`self._current_user_id = arguments["_user_id"]`). Per-request state must stay in arguments / ContextVars.
- ❌ **Hardcoding the recommend prompt** (`system_prompt="You are a media-recommendation assistant. …"`). Read `self._recommend_next_prompt`.
- ❌ **One global `_session_lock`** wrapping every `play_item`. Per-client lock if any (a TV can only play one thing at a time, but two different TVs are independent).
- ❌ **Fanning out `search` to backends in series** with a `for backend in self._backends:` await chain. Use `asyncio.gather`.
- ❌ **Using `getattr(svc, "search", ...)`** to access library methods. Use the `MediaLibraryProvider` protocol.
- ❌ **Polling now-playing in an `asyncio.create_task` loop on service start** instead of a `SchedulerProvider` job. The scheduler-job version is observable via the existing `/scheduler` UI; a hidden background task isn't.
- ❌ **A single `_user_token` field shared by every Plex Home user.** Each home user's token is keyed by their user id in a dict.
- ❌ **A single global `asyncio.Lock` over the per-Home-user token cache.** Per-Home-user locks (`dict[backend_user_id, asyncio.Lock]`) — a global lock serializes all token fetches across all Gilbert users.
- ❌ **`asyncio.gather(*backends, return_exceptions=True)` without `asyncio.wait_for` per backend.** Exceptions are caught; hangs are not. A flaky backend that returns TCP but stalls mid-XML blocks the AI turn indefinitely. Wrap each branch in `asyncio.wait_for`.
- ❌ **A homegrown `levenshtein_ratio` over backend titles.** Trust each backend's server-side relevance ranking; merge by stable round-robin interleaving.
- ❌ **`if "_user_id" not in arguments: user_id = get_current_user().user_id`.** Silent fallback. Under concurrent load, `get_current_user()` can return another user's id if the ContextVar wasn't `copy_context`-ed at the task boundary. Require `_user_id`; raise on absence.
- ❌ **Trusting `view_offset_seconds` from a `MediaItem` embedded in a button payload.** That offset belongs to the user who *searched*, not the user who *clicked*. Re-resolve via `get_item(item_id, backend_user_id=<clicker>)` at click time.
- ❌ **Emitting recently-added events for items the user can't see.** Restricted-library leakage. Subscribers must re-filter by `library_section` before delivering to a per-user UI.
- ❌ **Letting `now_playing` the *tool* read the polled cache.** The tool path bypasses the cache and queries each backend live; the polled cache exists only to drive the diff for `started`/`stopped` events.
- ❌ **Caching `config_params()` result in `__init__`.** A plugin loaded after the first call won't show up. Compute on every call.
- ❌ **Calling `find_client(name) -> None`** to express both "not found" and "ambiguous." Use `find_clients() -> list` (raw matches) plus `find_client() -> MediaClient` that raises `MediaClientNotFoundError` / `MediaClientAmbiguousError`.

---

## Revision Log — Round 2

This revision integrates fixes from three round-1 reviews (architect, product, engineering). Notable changes:

### Blockers fixed

- **`MediaLibraryProvider` Protocol signature drift** — Protocol's `search`/`now_playing` kwargs now match the concrete service exactly (`gilbert_user_id`, `client_name=None`); added `recently_added`, `continue_watching`, `list_backend_health` to the Protocol surface. (architect)
- **ContextVar fallback footgun** — removed the `arguments.get("_user_id") or get_current_user().user_id` pattern; tools now require `_user_id` and surface a JSON error if missing. Slash dispatch is documented as injecting `_user_id` before invoking the tool. (architect)
- **Per-Home-user token cache lock granularity** — explicit `dict[backend_user_id, asyncio.Lock]` per Plex Home user, with a short global lock guarding only the dict-setdefault. Single global lock across all users is now an Appendix C anti-pattern. Added per-user `PlexServer` memoization to avoid the `/identity` round-trip on every call. (architect, engineering)
- **Per-user mapping fan-out policy** — explicit per-backend missing-mapping policy in section 6.3: silently skip un-mapped backends, return mapped-backend results plus `unmapped_backends: [...]` metadata. Test added (`test_continue_watching_partial_mapping_returns_unmapped_hint`). (architect, engineering)
- **Capability-gating reflects "configured-and-supports-X," not "currently-healthy-and-supports-X"** — tools never disappear mid-conversation due to health flips. Test added. (engineering)
- **Recently-added poll baseline-run sentinel** — explicit `_poll_first_run_done: set[str]` flag; first cycle populates the cache and emits no events. Test added. (architect)
- **`config_params()` lazy-eval** — explicitly NOT cached in `__init__`; computed on every call so plugins loading after the first call still surface in the next Settings refresh. (architect)
- **Slash-name collision `/media resume` (continue-watching) vs `/media resume-playback` (action)** — renamed continue-watching slash to `/media on-deck`; freed `/media resume` for the playback control verb. (architect, product, engineering)
- **Plex `account_token` lifecycle** — explicit handling of revocation (401 → `unhealthy` + `media.backend.health_changed`) and re-link (atomic clear of all per-Home-user caches). At-rest encryption acknowledged as inherited tech debt with `0600` file mode mandate. (engineering)
- **Jellyfin per-user auth contradiction** — clarified: v1 uses admin-token + `userId` query param; unmapped users still get an error rather than silent admin fallback (the `userId` is what scopes the data, not the credential). v2 may switch to user-scoped api-keys. (engineering)
- **Multi-mapping ambiguity** — Goals updated: 1:1 per backend in v1; multi-mapping deferred to v2 with the unique index left in place. Documented in Open Questions. (engineering)
- **Per-backend timeouts on fan-out** — `asyncio.wait_for(timeout=...)` per branch in `_fanout` helper; per-op defaults configurable. Test added. (engineering)
- **Idempotency / per-client locking** — `idempotency_key` on `MediaPlayCommand`, per-client `asyncio.Lock` keyed by `(backend_name, client_id)`, 5s dedup window. Test added. (architect, engineering)
- **Restricted-library `recently_added` events** — events carry `library_section`; subscribers must re-filter via `service.user_can_see(...)`. Documented as a v1 limitation; v2 may switch to per-user polling. (engineering)
- **Pagination / search-limit cap** — `MediaSearchFilters.limit` service-side capped at 50. Test added. (engineering)
- **`view_offset_seconds` per-clicker re-resolution** — button-driven plays re-resolve via `get_item(..., backend_user_id=<clicker>)` rather than trusting embedded offsets. Test added. (engineering)
- **Episode resolution for shows** — added `next_episode` ABC method + `supports_next_episode` flag; `play_on` resolves SHOW → next-unwatched/on-deck episode before dispatch. "Caught up" UIBlock with Restart / Show upcoming buttons. (product)
- **Item disambiguation via UIBlock poster cards** — primary disambiguation surface is visual, not AI-prompt. AI item-disambiguation prompt reserved for non-interactive contexts. Test added. (product)
- **`MusicService` seam** — `search_media` default-excludes `MUSIC_*` kinds; tool descriptions explicitly draw the seam (`play_on` = video, `play_music` = audio). (product)
- **`playback_control` consolidation** — `pause_playback` / `resume_playback` / `stop_playback` / `seek_playback` collapsed into a single `playback_control(action, ...)` tool. Tool count: 14 → 12. (product)
- **Real Settings UI for user mappings** — added section 13 with explicit User Mappings table (per-backend, per-row dropdown of backend users via new `list_backend_users()` ABC method). Slash commands kept as convenience shims; canonical path is the Settings UI. (product)

### Important changes

- Replaced the homegrown `levenshtein_ratio` ranking with stable round-robin interleaving across backends' own server-side ranking. (architect, engineering)
- Removed dead capability flags `supports_transcoding` (gated nothing) and `supports_play_queue` (no tools used it in v1). (architect, engineering)
- Promoted `list_libraries` and added `list_backend_users` to `@abstractmethod` — both v1 backends implement them. (architect, product)
- Added `recommend_next` improvements: `intent` parameter, candidate cap (default 30) with summary truncation, warmer prompt default, empty-history branch, parallelized candidate-source fetches with a single 15s overall budget. (product, engineering)
- Added `ai_profile` ConfigParam (default `standard`) to keep the `light` profile out of media reasoning. (product)
- Per-backend health surface (`BackendHealth` dataclass, `list_backend_health()` method, `media.backend.health_changed` event, Settings panel banner). (engineering)
- Per-poll startup jitter for `poll_now_playing` and `poll_recently_added` to avoid lockstep across backends. (engineering)
- Adaptive backoff on `now_playing` poll (idle threshold + max interval). (product, engineering)
- `now_playing` *tool* explicitly bypasses the polled cache. (engineering)
- AI prompts use the `str(...) or _DEFAULT_*` falsy-fallback pattern (literal one-liner from `memory-ai-prompts-configurable.md`). (architect)
- `MediaPlayCommand` lost `audio_stream_id` / `subtitle_stream_id` (out-of-scope per Non-Goals); subtitle/audio selection landing in v2 is a non-breaking field addition. (engineering)
- `MediaItem` gained `grandparent_id` / `grandparent_title` (Plex-style) so EPISODE → SHOW lookups don't conflate with EPISODE → SEASON. (engineering)
- `MediaSession.user_name` renamed to `backend_user_name` for symmetry with `backend_user_id`. (engineering)
- Lenient seek-position parser: accepts `"5min"`, `"5 minutes"`, `"1:22"` (M:SS), whitespace tolerance. Negative offsets explicitly out of scope. (product, engineering)
- State-aware UIBlock button labels (`Resume (1:23:45)` / `Play` / `Watch again`). (product)
- Domain error hierarchy: `MediaLibraryError` base → `MediaLibraryUnavailableError`, `MediaClientNotFoundError`, `MediaClientAmbiguousError`. Single `except` catches the family. (architect)
- Test fidelity: recorded fixtures in `tests/fixtures/<backend>/` (one per `MediaKind`, including non-UTC timezone fixture); `make <backend>-fixtures` regeneration targets. (engineering)
- Slash `/media resume` is now playback-resume; `/media on-deck` is continue-watching. (architect, product, engineering)
- Documented poll-detected events carry `user_id=""` and `initiator="external"` so subscribers can distinguish from tool-driven events. (engineering)
- `find_client` split into `find_clients` (returns list) + `find_client` (raises typed errors) so `None` doesn't conflate "not found" with "ambiguous." (architect)
- Added explicit log-redaction patterns for `X-Plex-Token=…` query strings; chat-export pipeline mandated to redact the same. (architect, engineering)
- Removed duplicate `ConfigAction`s for user mapping (kept the AI tools / Settings UI invocation; dropped the `relink_user` / `unlink_user` ConfigActions to avoid two surfaces with two audit trails). (architect)
- Added `runtime_dependencies() -> []` declaration on both Plex and Jellyfin backends explicitly. (architect)
- Reframed precedent: this aggregator follows `AuthService` / `KnowledgeService`'s `dict[str, Backend]` pattern, NOT `MusicService`'s single-backend chooser. Implementer pointer updated. (architect)

### Unresolved → Open Questions

- Multi-mapping per Gilbert user (1:1 in v1; N:M deferred).
- Webhook / SSE-based session events (polling in v1; webhooks/SSE in v2).
- Per-user `preferred_genres` (household-level in v1).
- Per-user-token minting for Jellyfin (admin-token + UserId-query in v1).
- Poster-URL proxy (raw URLs in v1, with export-time redaction).
- Restricted-library aware per-user `recently_added` polling (re-filter at subscriber in v1).
- Adaptive backoff for `recently_added` poll (only `now_playing` adapts in v1).

### Nits applied

- Pitch updated to acknowledge `MUSIC_*` and `PHOTO` kinds (photo support is in scope but tier-2).
- Section 11 explicit allow-list now includes the polling-diff caches, per-client locks, and per-backend health dict.
- Section 14 boot wiring states the dependency-edge declaration rather than asserting call-order in `app.py`.
- `std-plugins/CLAUDE.md` placement of `MediaLibraryBackend` made mechanical (alphabetical between `EmailBackend` and `MusicBackend`).
- Test-fakes location pinned to `tests/unit/_fakes/media_library.py` (matching codebase convention).
- Appendix B slash-help examples added; `/media seek 1:22` semantics clarified as M:SS.
- Acceptance criteria expanded from 11 to 17 items reflecting the new normative requirements.

### Nits NOT applied

- "25+ tests" count language was kept (informational; replaced in places by explicit per-test enumeration which is the spec's actual contract).
- `MediaSearchFilters.kinds` (plural) on the dataclass kept; `search_media`'s `kind: STRING` (singular) is the AI surface — the multi-kind capability stays on the dataclass for future tools.
- `find_client` retained as a public service method (used by `_tool_play_on` and exposed via the Python API); didn't privatize since the test plan exercises it directly.

---

*End of spec.*
