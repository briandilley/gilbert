# Camera Event Service

## Summary
Streams object-detection events from a camera backend (Frigate today,
multi-backend tomorrow) onto the Gilbert event bus, persists a rolling
history, and exposes AI tools + WebSocket RPCs. First push-driven backend
in Gilbert (presence and doorbell are poll-based) — the service owns the
reconnect supervisor; the plugin exits on transport error and the
service retries.

## Details

### Files
- `src/gilbert/interfaces/camera.py` — `CameraEventBackend` ABC,
  `CameraEvent` / `CameraInfo` / `SnapshotRef` dataclasses,
  `CameraEventPhase` enum, `CameraBackendError` taxonomy,
  `CameraProvider` capability protocol, `AvailableCameraLister`
  protocol for the dynamic-choices hook.
- `src/gilbert/core/services/camera.py` — `CameraEventService`
  singleton.
- `src/gilbert/web/routes/cameras.py` — Gilbert-proxied
  `/api/cameras/events/<id>/snapshot.jpg` and `clip.mp4`
  routes (Range support on the clip route).
- `std-plugins/frigate/` — `FrigateCameraBackend` plus its
  `mqtt_client.py` (aiomqtt v2) and `http_client.py` (httpx).

### Bus events
- `camera.event.detected` — fires on ACTIVE phase. Glob companion
  `camera.<label>.detected.<camera>` is published *only* on ACTIVE so
  subscribers can register exact-match patterns. Both events share the
  same payload.
- `camera.event.ended` — fires on ENDED phase. No glob companion (use
  cases don't motivate "react when X leaves").
- `camera.snapshot.annotated` — fires after the vision pass writes
  `vision_text` to the persisted row. Payload omits `vision_model`
  (`VisionProvider` keeps the surface minimal in v1).
- `camera.backend.connected` / `camera.backend.disconnected` — MQTT
  transport status; admin-only by default
  (`"camera.backend.": 0` in `DEFAULT_EVENT_VISIBILITY`).

### Per-event role override (new ACL primitive in this PR)
`CameraEventService` writes `data["required_role"]` into every
published event based on the per-camera role override
(`role_overrides: {camera_name: "admin"|"user"|"everyone"}`,
defaulting to `default_camera_role`). The WS event filter calls
`resolve_event_visibility(event_type, data)` from `interfaces/acl.py`
which honors the data-level override before falling back to the
prefix table. Tests in `tests/unit/test_ws_protocol.py` pin the
admin-block / fall-back / unknown-value cases.

### Glob-emission asymmetry — note for subscribers
`camera.<label>.detected.<camera>` fires only on ACTIVE; subscribers
who want every person event including end-of-event must listen to
`camera.event.*` or maintain two subscriptions. **Subscribers MUST
pick one of the two for the same logical detection** — listening to
both yields duplicate handler invocations. The greeting service
subscribes only to `camera.event.detected`.

### Persistence
`camera_events` collection, one row per `event_id`. Indexes on
`(camera, started_at)`, `(label, started_at)`, `(started_at,)`. URLs
in the row are Gilbert-proxied (`/api/cameras/events/<id>/...`); raw
Frigate URLs live on `direct_*_url` for advanced LAN-only consumers.
The `raw` payload is NOT persisted (debug-only on the dataclass).
ISO-string timestamp helpers are derived from the int milliseconds on
read — never persisted, never re-derived per row.

### Retention sweep — `delete_query` precursor
`_sweep_old_camera_events` (hourly) calls
`StorageBackend.delete_query(...)` with a `started_at < cutoff`
filter. The new `delete_query` method is implemented in
`SQLiteStorage` as a single parameterized `DELETE FROM ... WHERE`
(falls back to "select ids → delete" if the SQLite build lacks
`UPDATE_DELETE_LIMIT`). Optional `vision_text_retention_days` runs a
parallel scrub that empties `vision_text` on rows older than the
secondary cutoff while keeping the metadata.

### Vision annotation
- Off the hot path — spawned via `asyncio.create_task(...,
  context=copy_context())` so a future `ContextVar.set()` inside the
  task can't leak to the stream consumer or sibling annotations.
- Per-event-id `asyncio.Lock` (held in
  `_annotation_locks: dict[str, asyncio.Lock]`, gated by an
  outer `_annotation_locks_guard`) prevents double-annotation when an
  `update` event fires twice for the same id.
- Bounded by `_vision_semaphore` (size = `vision_concurrency`,
  default 4) so a burst of 50 simultaneous person events doesn't
  saturate the LLM provider.
- Skips when the persisted row already has `vision_text` set.
- Resolves the `vision` capability via
  `resolver.get_capability("vision")` and `isinstance`-checks against
  `VisionProvider` (the new minimal protocol in
  `interfaces/vision.py`).
- Default `vision_enabled_labels = ["package"]` — `"person"` opt-in.
  `vision_per_camera` overrides per-camera with a self-documenting
  truth table (missing key = use defaults; empty list = disabled;
  non-empty = override).

### AI tools (5)
- `list_cameras` (`/cameras list`) — role-filtered.
- `latest_clips` (`/cameras clips`) — page through `camera_events`,
  per-camera role gate applied to results.
- `get_snapshot` — AI-only (no slash command — opaque event_id
  defeats the slash-command UX). Returns a `ToolOutput` with one
  inline `FileAttachment(kind="image", ...)`. Caps inline bytes at
  1 MB raw / ~1.4 MB base64; requests Frigate's `?h=720` server-side
  downscale.
- `who_was_seen` (`/cameras seen`) — **deterministic**, no LLM call.
  Returns face-recognition matches from `sub_label` plus an
  `unknown_count`. The previously-spec'd `who_was_at` (LLM correlation
  over face matches + vision prose + presence) was dropped because it
  produced confidently-wrong identifications.
- `count_detections` (`/cameras count`) — structured counts
  (`total`, `by_camera`, `by_label`, `by_camera_label`) returned as
  parseable JSON. Replaces the previously-spec'd opaque
  `recent_detections_summary`.

### Time-window grammar in caller TZ
`_parse_time_window` accepts `30m` / `4h` / `7d` / `today` /
`yesterday` / ISO-8601. The AI dispatcher injects `_user_tz` (IANA
name from `UserContext.tz`); the camera tools pass it through so
`today` / `yesterday` anchor to **the caller's local midnight**, not
UTC midnight. Unknown TZ strings fall back to UTC. The relative
shorthands (`24h`) are TZ-independent (elapsed-time deltas).

### WebSocket RPCs
`cameras.list`, `cameras.get`, `cameras.events.list`,
`cameras.events.get`, `cameras.events.since`, `cameras.snapshots.get`,
`cameras.zones.list`, `cameras.mutes.list`, `cameras.mutes.set`,
`cameras.mutes.clear`, `cameras.test_connection` (admin-only).
Note: `cameras.zones.update` is intentionally **not** registered —
Frigate is read-only from Gilbert's perspective, so a stub that always
501s would just lie to clients. The SPA can branch on whether the
frame type exists. Every
handler that returns event rows / media URLs applies the per-camera
role gate from the cached `_role_overrides` map.

### Frigate plugin
- `aiomqtt>=2.3.0,<3.0.0` (asyncio-native; v2-only because v1 → v2
  was a breaking change, and v3 hasn't shipped). HTTP via httpx
  (already a core dep).
- TLS configurability: CA cert, mTLS client cert/key, insecure-mode
  flag (warns operator), SNI / cert-CN override. Note that
  `tls_insecure` and `server_hostname` are aiomqtt **client kwargs**
  in v2, not `TLSParameters` fields — `_build_client_tls_kwargs`
  splits them out.
- HTTP auth modes: `none` (LAN deploy) and `bearer` (Frigate API
  keys / proxy). 0.14+ session-cookie auth is out of scope for v1.
- Single-layer reconnect: `FrigateMQTT._run` opens **one**
  `aiomqtt.Client` per call. Any `MqttError` exits the `async with`
  block, drains the LWT-offline sentinel into the queue, and
  re-raises as `CameraBackendError`. The service's
  `_run_stream_consumer` catches it, sleeps with exponential backoff
  (capped at `reconnect_max_seconds`), and calls `connect()` again.
- LWT translation: `<prefix>/available = "online" / "offline"` flips
  publish `camera.backend.{connected,disconnected}` events.
- Defensive payload parsing: every field read uses `.get()` with a
  default; `sub_label` accepts string / `[name, score]` list / null /
  missing forms; missing required fields drop the event with a
  debug-level log; `false_positive=true` drops the event entirely;
  invalid JSON payloads are logged at WARNING and dropped.
- Update dedup: `update`-type events drop unless score changed by
  ≥ 0.05 OR new zones appeared OR snapshot frame_time advanced.
- Audio events (Frigate 0.13+ — `bark`, `glass_break`, etc.) flow
  through transparently; `has_snapshot=False` short-circuits vision
  annotation.

### Greeting integration
- Subscribes to `camera.event.detected` on `start()`; the handler
  filters by `announce_camera_labels` (default `["package"]`).
- Composite dedup keys (`camera_announce_dedup_keys`) prevent a
  single visitor across adjacent cameras from triggering 3
  announcements: default `package = ["label"]` (one announce per
  delivery house-wide), `person = ["label", "zone_group"]` (one per
  zone). `camera_zone_groups` maps logical zones to camera lists;
  cameras not in any group are their own one-element group.
- `mute_camera_alerts` AI tool (`/cameras mute`) writes a
  `camera_mutes` row keyed by `(camera, label)` (with `*` wildcards).
  Returns a Confirm/Cancel `UIBlock` via the shared
  `confirm_or_execute` helper from `_ui_blocks.py`. The handler
  consults the same collection before announcing — bus events still
  flow; only the announcement is suppressed.
- Per-label prompt overrides via
  `camera_announce_per_label_prompts` (tone differs by label —
  package warm/informative, glass_break brief/urgent).

### SPA
- All camera UI lives **inside the frigate plugin**:
  `std-plugins/frigate/frontend/`. Core SPA never imports from the
  plugin — see `memory-plugin-ui-extensions.md` for the slot mechanism.
- `CamerasPage.tsx` (per-camera grid + recent-events feed + mute
  drawer) ships as `panel_id="frigate.cameras_page"` declared via
  `Plugin.ui_routes()` for `path="/cameras"`. Core's `<PluginRoutes>`
  injects the `<Route>` automatically.
- `RecentEventsCard.tsx` mounts into the `dashboard.bottom` slot via
  `panel_id="frigate.recent_events"` declared in `Plugin.ui_panels()`.
  Core's `DashboardPage.tsx` renders only `<PluginPanelSlot
  slot="dashboard.bottom" />` — no hardcoded import.
- Plugin-local API hook (`std-plugins/frigate/frontend/api.ts`) wraps
  every `cameras.*` WS RPC. It uses the core `useWebSocket` helper via
  the `@/` alias — that's the canonical plugin-frontend pattern.
- `panels.ts` is the side-effect file core's
  `frontend/src/plugins/index.ts` `import.meta.glob` picks up; it
  registers both panels in `lib/plugin-panels`.

### Multi-user isolation
- Singleton; **every** `__init__` attribute is service-lifetime.
  Per-event annotation locks live in a `dict[event_id, asyncio.Lock]`
  gated by an outer guard lock — different events annotate
  concurrently; same event won't double-annotate.
- Tool handlers read caller roles from the AI-injected
  `_user_roles: list[str]` argument, never from `self`. The
  `latest_clips`, `count_detections`, and `who_was_seen` tools filter
  result lists by the per-camera role overrides using the injected
  roles.
- Stream consumer + annotation tasks both spawned with
  `asyncio.create_task(coro, context=copy_context())` so a future
  `ContextVar.set()` inside either can't leak.

### Open / decision-locked
- v1 ships deterministic `who_was_seen` + `latest_clips` +
  `count_detections` + `get_snapshot` + `mute_camera_alerts` ONLY.
  An LLM-correlated `identify_visitors` (combining face matches,
  vision prose, presence) is v2 — STOP and FLAG if you reach for it.
- `VisionProvider.model_name` deferred — the cheap path keeps
  `VisionProvider` minimal (just `describe_image`) so we don't have
  to touch every existing `VisionBackend` implementation.
  `camera.snapshot.annotated` payload omits `vision_model`
  accordingly.
- Backend secrets stay plaintext for v1 (matches existing services).

## Related
- [Doorbell Service](memory-doorbell-service.md) — sibling
  event-stream service.
- [Storage Backend](memory-storage-backend.md) — `delete_query`
  precursor.
- [WebSocket Protocol](memory-websocket-protocol.md) —
  `resolve_event_visibility` data-level override.
- [Multi-User Isolation](memory-multi-user-isolation.md) — singleton
  + ContextVar invariants.
- [UI Blocks](memory-ui-blocks.md) / `_ui_blocks.py` —
  `confirm_or_execute` helper used by `mute_camera_alerts`.
- [Greeting Service](memory-greeting-service.md) — camera-event
  subscription + dedup + mute integration.
