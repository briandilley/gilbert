# Media Library uses the aggregator pattern (one service, N backends)

The media library service fans queries across *all* configured backends (Plex + Jellyfin) in
parallel and dispatches playback to whichever backend owns the target client — rather than the
single-active-backend "chooser" pattern that MusicService uses. A mixed household wants both servers
live at once, not one selected.

## Consequences

- Cross-backend search results are merged by **stable round-robin interleaving** over each backend's
  own relevance order; there is no global cross-backend relevance ranking.
- Capability gating reads **"configured-and-supports-X"**, not "currently-healthy-and-supports-X",
  so a tool doesn't vanish mid-conversation on a transient health flip.
- `now_playing` bypasses the poll cache (it must be fresh); `recently_added`/sessions are served from
  the cache.
