# Gilbert-owned music playlists (per-user, named, shuffleable)

**Date:** 2026-07-14
**Status:** Approved (design)

## Summary

Add per-user, named playlists that Gilbert owns and stores itself. A user can
create a playlist, add and remove tracks, rename it, delete it, and play it —
optionally shuffled. Playback resolves each item through the configured music
backend and loads the speaker queue.

Playlists are a **`MusicService`** feature backed by Gilbert's entity store.
They are **not** a `MusicBackend` capability and they never write to the
upstream music service.

## Motivation and the rejected alternative

The obvious first design was "playlist CRUD as an optional `MusicBackend`
capability, implemented against the Spotify Web API" (mirroring
`supports_stations`). It was rejected for two concrete reasons:

1. **OAuth scopes.** The Spotify link requests read-only playlist scopes
   (`sonos_music.py:66-74`). Writing needs `playlist-modify-private` /
   `playlist-modify-public`, which Spotify only grants at authorize time —
   every already-linked installation would have to re-authorize, and existing
   tokens would 403 until they did.
2. **The Spotify link is installation-wide, not per-user.** The operator links
   *one* Spotify account in Settings. Native playlist writes would therefore let
   any household member create, edit, remove tracks from, and unfollow playlists
   in the operator's *personal* Spotify account. Destructive, shared, and not
   undoable from Gilbert.

Gilbert-owned playlists deliver the whole feature with neither problem, and as a
bonus work on **any** music backend and support **mixed-source** playlists (a
Spotify track next to a Plex one), since items are resolved individually at
playback.

Reading the linked service's own playlists already works (`list_playlists`) and
is unchanged — users can still browse and play their Spotify playlists, they
just can't *edit* them from Gilbert.

### Known drift to fix while we're here

`sonos_music.py:69` claims the scopes "intentionally omit `*-modify-*` scopes",
but the scope string on line 72 already includes `user-library-modify`. The
comment is stale; correct it (no behavior change).

## Architecture

| Concern | Owner |
|---|---|
| Playlist CRUD, ACL, shuffle | `MusicService` (`core/services/music.py`) |
| Persistence | Entity store, collection `music_playlists` |
| `Playlist` data type | `interfaces/music.py` (shared data lives in `interfaces/`) |
| Item resolution + playback | Existing `MusicBackend.resolve_playable()` + speaker queue |

No new abstract methods on `MusicBackend`. No new speaker capability.

`MusicService.service_info()` gains `entity_storage` in `requires`, and `start()`
resolves a `StorageProvider` and creates an index on `owner_user_id` — following
`TasksService` (`tasks.py:290`, `tasks.py:300`). No migration is needed; the
generic entity store takes new collections without one.

## Data model

Collection `music_playlists`:

```
id             str   uuid
owner_user_id  str   the creating user; scopes every read and write
name           str   unique per owner, case-insensitive
shuffle        bool  default shuffle for this playlist (default False)
items          list  ordered snapshots of MusicItem
created_at     str
updated_at     str
```

Each entry in `items` is a **snapshot** of a `MusicItem` (id, title, kind, uri,
service, subtitle, album_art_url, duration_seconds). Snapshotting means playback
needs no re-search, and the playlist survives the track disappearing from the
upstream service's search index.

Names are unique per owner so tools can address a playlist by name — which is
what voice and the AI actually use ("add this to my Workout playlist").
Duplicate tracks within a playlist are allowed (as Spotify allows).

## Tools and RBAC

Eight tools under `slash_group="music"`, all `required_role="user"` — they need a
caller identity and they mutate that user's data.

| Tool | Behavior |
|---|---|
| `create_playlist(name, shuffle=False)` | New empty playlist. Fails on duplicate name for this owner. |
| `my_playlists()` | List the caller's playlists (name, track count, shuffle default). |
| `show_playlist(name)` | Ordered items with 1-based positions. |
| `add_to_playlist(name, query?, track_id?)` | See resolution order below. |
| `remove_from_playlist(name, position)` | Remove by the 1-based position shown by `show_playlist`. |
| `update_playlist(name, new_name?, shuffle?)` | Rename and/or change the stored shuffle default. |
| `delete_playlist(name)` | Delete the playlist. |
| `play_playlist(name, shuffle?, speaker_names?, volume?)` | Resolve and play. |

`add_to_playlist` resolves what to add in this order:

1. `track_id` given → add that item (a hit from a prior `search`).
2. `query` given → search, add the top hit.
3. **neither given → add the currently-playing track.**

(3) is what makes "Gilbert, add this song to my Workout playlist" work.

### Disambiguation from the existing tool

The existing `list_playlists` tool (the linked service's saved playlists) stays.
Tool descriptions must distinguish them explicitly so the AI picks correctly:

- `list_playlists` — "saved playlists from the linked music service (read-only)"
- `my_playlists` — "playlists you own in Gilbert (editable)"

## Playback and shuffle

`play_playlist` models itself on `start_station` (`music.py:506-559`):

1. Resolve the effective shuffle: the `shuffle` argument when supplied,
   otherwise the playlist's stored `shuffle` default.
2. If shuffling, shuffle the item order **for this playback only** — the stored
   order is never mutated, so each play reshuffles.
3. `play_item(first)` — clears the queue and starts playback.
4. `add_to_queue(rest)` in order, when the backend `supports_queue`.

Like `start_station`, this **degrades gracefully**: a backend with no queue plays
the first track rather than erroring, so `play_playlist` needs no capability gate
and stays available on every backend.

**Limitation (accepted):** shuffling reorders items at queue-load time, so a
shuffled play is a *fixed* shuffled order once queued. True speaker-level shuffle
mode (reshuffling as it goes, surviving queue edits) would need a new
`SpeakerBackend.set_shuffle` + `supports_shuffle` capability mirroring
`set_repeat`. **Out of scope** for this cut; it is a clean separate addition.

## Multi-user isolation

Every read and write is scoped by `owner_user_id`, taken from
`get_current_user()` (`interfaces/context.py:22`) — the same contextvar
`TasksService` uses inside tool handlers (`tasks.py:2002`). An ACL check
mirroring `tasks.py:716-744` ensures user B cannot list, show, play, mutate, or
delete user A's playlists. Nothing in this feature ever writes to the operator's
upstream music account.

## Error handling

| Case | Behavior |
|---|---|
| Item fails to resolve at playback | Skip it; continue. Report "Queued 11 of 12 — 1 track unavailable." |
| Playlist is empty | Friendly error, no speaker calls. |
| Duplicate playlist name on create | Error naming the existing playlist. |
| Unknown playlist name | Error listing the caller's playlist names. |
| `remove_from_playlist` position out of range | Error stating the valid range. |
| `add_to_playlist` with no query/track_id and nothing playing | Error explaining the three ways to add. |
| Another user's playlist | Denied by ACL, indistinguishable from "not found". |

## Events

`music.playlist_created`, `music.playlist_updated`, `music.playlist_deleted` are
published on the bus so other systems can react. Playback continues to emit the
existing `music.playback_started`.

## Testing

Unit tests (fake storage, fake backend) plus SQLite-backed integration tests:

- Create; duplicate-name rejection (case-insensitive).
- Add by `track_id`, by `query` (top hit), and by now-playing; the no-source error.
- Remove by position; out-of-range rejection.
- Rename and shuffle-default change via `update_playlist`.
- Delete.
- **Per-user isolation:** user B is denied on every operation against user A's playlist.
- `play_playlist` resolves and queues items in stored order.
- `play_playlist(shuffle=True)` queues a permutation of the items and leaves stored order unchanged.
- Stored `shuffle=True` default applies when the argument is omitted; the argument overrides the default in both directions.
- Partial-resolve degradation: one unresolvable item is skipped, the rest play.
- No-queue backend: plays the first track instead of erroring.

## Out of scope

- Track reordering within a playlist.
- Sharing playlists between users.
- An SPA playlist UI (tools and slash commands only).
- Speaker-level shuffle mode (`SpeakerBackend.set_shuffle`).
- Any write to the upstream music service (Spotify) — explicitly rejected above.
