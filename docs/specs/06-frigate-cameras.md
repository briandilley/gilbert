# Feature 06 — Frigate Cameras + Object-Detection Events

**Status:** Draft (specification only — no code yet)
**Owner:** Gilbert core + new `frigate` std-plugin
**Companion features:** UniFi presence (already shipping), UniFi Protect doorbell (already shipping)

## 0. Elevator pitch

UniFi already covers presence and the doorbell ring. Frigate covers
the rest of the camera/object-detection space — "person at the side
gate", "package on the porch", "car in the driveway", "deer in the back
yard at 3am". This feature lands a generic `CameraEventBackend` interface
and a `CameraEventService` that streams object-detection events from
Frigate (or any future provider) onto the Gilbert event bus, persists a
short rolling history, exposes AI tools (`latest_clips`, `who_was_seen`,
`count_detections`, `get_snapshot`, `list_cameras`),
optionally tags events with a Vision-backed snapshot description, and
wires the existing greeting service so Gilbert can announce "package
delivered to the front porch" without anyone asking.

This is the third event-stream backend after UniFi presence (poll) and
UniFi Protect doorbell (poll). Frigate's native protocol is **MQTT**
push, not poll — so this is also the first long-lived MQTT subscriber in
Gilbert and the spec covers reconnect / liveness in some detail.

## 1. Goals

- A new abstract `CameraEventBackend` ABC sitting in `interfaces/camera.py`,
  designed for **multiple** providers (Frigate now, Reolink AI / Blue Iris
  / NVR-of-the-month later) — even though Frigate is the only initial
  implementation.
- A core singleton `CameraEventService` that:
  - Holds one active `CameraEventBackend` (single-backend service today,
    aggregator-ready for tomorrow — see §6.2),
  - Subscribes to backend events for the entire process lifetime,
  - Publishes `camera.event.detected`, `camera.event.ended`, and
    glob-friendly `camera.<label>.detected.<camera>` events on the bus,
  - Persists each event into a `camera_events` collection (ring-buffer
    style with a configurable retention window, default 7 days),
  - Exposes AI tools for `latest_clips`, `get_snapshot`, `who_was_seen`,
    `count_detections`, `list_cameras`,
  - Implements `Configurable`, `ToolProvider`, `WsHandlerProvider`,
    `ConfigActionProvider`.
- A new `std-plugins/frigate/` plugin:
  - Implements `FrigateCameraBackend(CameraEventBackend)`,
  - Connects to Frigate's MQTT broker for the live event stream and to
    Frigate's HTTP API for snapshots and clips,
  - Ships `aiomqtt` as its only third-party dep,
  - Declares no `runtime_dependencies()` (the broker is the user's
    problem; we connect as a client),
  - Includes a "Test connection" `ConfigAction` that probes both the
    HTTP API and the MQTT broker.
- Optional Vision integration: per-camera or per-label opt-in to run the
  snapshot through the existing `VisionService` and tag the event with
  the resulting description (so the AI can answer "what did the camera
  actually see?" without re-fetching the image).
- Existing `GreetingService` extended (small change, see §10.3) to
  subscribe to `camera.event.detected` for select labels (`package`,
  `delivery`) and announce them — gated by config and a per-event
  dedup window so repeated detections of the same package don't double-
  announce.
- Multi-user isolation: cameras and detections are house-wide by default
  (`everyone`), but admins can per-camera gate visibility to
  `admin`/`user`. No per-user state on the singleton.

## 2. Non-goals

- **No RTSP transcoding through Gilbert.** Clips are served by Frigate's
  existing HTTP endpoints; we proxy the URL or stream-pass the bytes
  unchanged. Gilbert does not decode H.264, does not run ffmpeg, does
  not add latency to the video path.
- **No Frigate config management.** This plugin is read-only with respect
  to Frigate — we do not push `config.yml`, do not toggle zones, do not
  add cameras. If the user wants to change Frigate's behavior they edit
  Frigate.
- **No two-way audio / no PTZ control.** Future v2 if there's appetite.
- **No live camera grid UI.** A settings page for backend config is
  required (it's a `Configurable`); a per-camera detail view that lists
  recent events is a stretch goal but optional. A live `<video>` grid
  is explicitly out — it would need WebRTC plumbing and HLS proxying
  that are big projects on their own. Note as v2.
- **No face recognition.** Frigate has its own face module behind a
  flag, and UniFi Protect already does this for presence. We surface
  face matches in events when Frigate emits them (`label: "face"` with
  `sub_label`), but we don't run our own model.
- **No vector-search / "find the time a UPS truck was here last
  Tuesday" semantic indexing.** The Vision-tagged text could feed
  `KnowledgeService` someday, but that's a separate feature with its
  own design. We expose a tool that lets the AI iterate over recent
  events; semantic search is v2.

## 3. User-visible surface

### 3.1 Bus events

Published by `CameraEventService` (all carry `source="camera"` on the
`Event` dataclass):

| Event type | When fired | `data` shape |
|---|---|---|
| `camera.event.detected` | A new object enters frame and Frigate marks the event as "active." | `{event_id, camera, label, sub_label, score, started_at, snapshot_url, clip_url, has_snapshot, has_clip, zones, source_backend, vision_text}` (vision_text empty until annotation lands) |
| `camera.event.ended` | Frigate transitions the event from active → ended (object left frame or timeout). | Same shape as `detected` plus `ended_at`, `top_score`, `duration_seconds`, and the final `clip_url`. |
| `camera.<label>.detected.<camera>` | Same payload as `camera.event.detected` — emitted **in addition** so consumers can subscribe via glob (`camera.person.detected.*`, `camera.*.detected.front_door`). | Same as `camera.event.detected`. |
| `camera.snapshot.annotated` | Vision pass completed and tagged the event with descriptive text. | `{event_id, camera, label, vision_text, required_role}` (no `vision_model` — see §6.6 for rationale) |
| `camera.backend.connected` / `camera.backend.disconnected` | MQTT subscription up/down. Surfaces transient drops to the dashboard / debugging UI. | `{backend_name, transport, error}` (error empty on connected) |

`camera.<label>.detected.<camera>` is emitted as a *second* event with
the same payload, not via wildcards on the subscriber side — Gilbert's
`InMemoryEventBus` glob matching is on the subscription, but emitting
two distinct events is what lets handlers register exact-match subscriptions
for "I only care about person-at-front-door" without paying the cost of
filtering every camera event in user code. This mirrors how `presence.arrived`
+ `presence.<source>.arrived` would be done if we wanted source-tagged
presence events.

**ACL:** add to `interfaces/acl.py` `DEFAULT_EVENT_VISIBILITY` —
`"camera.": 200` (everyone, default), but `CameraEventService` will
**override** the visibility level on a per-event basis when config flags
a camera as `admin`-only. The override mechanism lives entirely in the
service: it inspects the per-camera role and writes the resolved role
string into `event.data["required_role"]` before publish.

> **NEW PRIMITIVE — required by this PR.** The WS event-filter today
> resolves visibility *purely by event type* via
> `get_event_visibility_level(event_type)` in
> `interfaces/acl.py:168` / `web/ws_protocol.py:41`. It does **not**
> inspect `event.data`. This spec adds a new primitive — per-event
> data-level override — and is responsible for landing the change as
> part of the camera PR. Concretely:
>
> 1. Add `resolve_event_visibility(event_type: str, data: Mapping[str, Any]) -> int`
>    to `interfaces/acl.py` next to `resolve_default_event_level`. If
>    `data.get("required_role")` is a known role name (`"admin"` →
>    `0`, `"user"` → `100`, `"everyone"` → `200`), return that level.
>    Otherwise fall back to the prefix-table resolution. Document
>    `data["required_role"]` as a reserved key in the `acl.py` module
>    docstring.
> 2. Update `web/ws_protocol.py:can_see_event` to take the full `Event`
>    (or accept `event_type` + `data` separately) and call the new
>    helper. The publishing code path that fans out to subscribers
>    must pass the event's `data` through.
> 3. Audit the per-event content filters in `WsConnection`
>    (`can_see_doorbell_event`, `can_see_inbox_event`, etc.) — they
>    already accept `Event`, so they're fine; the change is to the
>    *generic* role-level gate.
> 4. Add unit tests in `tests/unit/test_ws_protocol.py`:
>    `test_event_data_required_role_admin_blocks_user`,
>    `test_event_data_required_role_falls_back_to_prefix_when_missing`,
>    `test_event_data_required_role_unknown_value_falls_back`.
>
> Until this primitive lands, admin-gated cameras leak to non-admin
> sockets even though the rest of the spec is correct. The
> implementation checklist in §17 lists this as step 1 (precondition
> for the camera service).

### 3.2 AI tools (declared by `CameraEventService`)

All tools default `required_role="user"`. Where camera-level role gating
is set to `admin`, the tool implementations filter the result list down
to cameras the caller is allowed to see (read identity from the injected
`_user_roles` arg, see [Multi-User Isolation](../../.claude/memory/memory-multi-user-isolation.md)).

| Tool | `slash_command` | What it does |
|---|---|---|
| `list_cameras` | `/cameras list` (`slash_group="cameras"`) | Returns `[{name, label, zones, role_visibility}]` — every camera the caller is allowed to see (admin-gated cameras are redacted entirely from non-admin callers; not surfaced with a `role_visibility` flag the AI can't act on). Reads from the backend's cached `list_cameras()` (populated on `start()` and on a 5-min refresh timer). |
| `latest_clips` | `/cameras clips` | Args: `camera: str | None`, `label: str | None`, `since: str | None` (default `"24h"`; accepts shorthand `{N}m` / `{N}h` / `{N}d` or ISO 8601 — see grammar below), `until: str | None`, `limit: int=20`. Returns most-recent ended events from the `camera_events` collection, descending. Each entry: `{event_id, camera, label, started_at, ended_at, score, clip_url, snapshot_url, vision_text}`. |
| `get_snapshot` | *(AI-only — no slash command)* | Args: `event_id: str`. Returns a `ToolOutput` whose attachments contain the event's snapshot as an inline `image` `FileAttachment` (kind=`"image"`, `media_type="image/jpeg"`). The text portion gives the event metadata so the AI knows what it just received. If the snapshot is no longer available (Frigate retention sweep cleaned it up, typically after ~7 days), returns a `ToolOutput(text="Snapshot no longer available for event <id>; try latest_clips for recent activity.", is_error=True)` rather than an empty attachments tuple. |
| `who_was_seen` | `/cameras seen` | Args: `camera: str`, `since: str` (default `"today"`), `until: str | None`. **Deterministic — no LLM call.** Returns face matches from the events' `sub_label` field only: `[{name, count, first_seen, last_seen}]` plus an `unknown_count` for events with `label="person"` and empty `sub_label`. The AI gets a clean signal it can quote without hallucinating identity. |
| `count_detections` | `/cameras count` | Args: `since: str` (default `"24h"`), `until: str | None`. Returns structured counts: `{total: N, by_camera: {front_door: 7, ...}, by_label: {person: 11, package: 1, ...}, by_camera_label: {front_door.person: 4, ...}}`. The AI composes the prose itself and can drill into any bucket via `latest_clips`. |

These all set `parallel_safe=True` (they're pure reads — no LLM
sub-calls). The previously-spec'd `who_was_at` tool (which would have
invoked the LLM as a correlation engine over face-matches +
vision-prose + presence) has been **dropped**: it conflated three
signals at three confidence levels (deterministic face-recognition vs.
free-text vision prose vs. weak temporal presence correlation) and
would routinely produce confidently-wrong identifications ("Jeff was
at the front door" when Jeff was at his desk and the UPS driver was
at the door). The honest decomposition is `who_was_seen` (face
matches, deterministic) + `latest_clips` (vision prose, the AI reads
it directly) + `presence.who_is_here` (already exists, the AI calls
it itself if it wants to correlate). The previously-spec'd
`recent_detections_summary` tool has likewise been replaced by
`count_detections` because structured counts compose with follow-up
`latest_clips` calls; an opaque prose summary doesn't, and it
discouraged the AI from composing camera data with calendar / inbox /
weather for a true "what happened today?" answer. See §18 Open
Question 1 for re-introducing an LLM-correlated `identify_visitors`
in v2 with explicit unknown-count surfacing.

**`since` / `until` grammar.** All time-window args accept the same
small grammar, parsed by a single helper:
- `{N}m` / `{N}h` / `{N}d` — relative N minutes/hours/days ago.
- `"today"` — start-of-local-day.
- `"yesterday"` — start of yesterday (only valid for `since`; `until="today"` for the upper bound).
- ISO 8601 — `2024-11-05T22:00:00Z` or `2024-11-05`.

Tool descriptions enumerate this exact set so the AI doesn't try other
phrasings (`"2 hours"`, `"1 week"`) and retry on parse error.

**Slash command rationale.** Per [Slash Commands](../../.claude/memory/memory-slash-commands.md),
slash commands are typed by humans and must be usable without a
multi-step lookup. `get_snapshot` requires an opaque `event_id` like
`1730851234.567890-abc` that a user can only obtain by first running
`/cameras clips` to get a list and then copy-pasting — that's a
two-step UX that defeats the slash-command shortcut. The AI knows
event ids transparently from prior `latest_clips` results, so
`get_snapshot` ships as **AI-only** and `slash_command` is
intentionally unset. `who_was_seen` and `count_detections` accept
`camera`/`since` strings from the small grammar above, both of which
a human can type easily, so they keep their slash commands.

**`get_snapshot` rendering.** AI providers want image attachments on
the *next* user/tool message, not as inline base64 in the tool result
text. The `ToolOutput.attachments` mechanism handles this — see
`interfaces/attachments.py` and `interfaces/ui.py:ToolOutput`. The
attachment rides back on the assistant message's `Message` payload and
the next user turn references it implicitly (Anthropic input blocks
combine prior assistant attachments + new user content).

The chat frontend (`frontend/src/components/chat/TurnBubble.tsx` →
`AttachmentChip`) renders inline-mode `FileAttachment` with
`kind="image"` as an `<img>` (data-URL constructed from the inline
base64), not as a downloadable chip. This is verified-and-working for
the existing screen / inbox / OCR attachment flows; no frontend change
is required for `get_snapshot`. If a future attachment-chip refactor
breaks inline image rendering, the camera tests pin this contract via
`test_get_snapshot_tool_returns_attachment` (§13.1).

**Snapshot bytes & storage discipline.** A 1280×720 JPEG from Frigate
is typically 60–200 KB raw → 80–270 KB base64. To keep conversations
from bloating, `get_snapshot`:

1. Requests Frigate's pre-scaled snapshot via the `?h=720` query param
   (Frigate's HTTP API supports server-side downscaling — no Pillow
   dependency added). For `get_snapshot` the bound is fixed at 720px;
   re-annotation calls into `_fetch_snapshot_bytes` may use the same
   bound or a smaller one.
2. Enforces a hard cap of 1 MB raw / ~1.4 MB base64 on the inline
   bytes. If the JPEG comes back larger, the tool errors out with
   `is_error=True` rather than silently bloating context.
3. Documents the `?h=720` downscale in the tool description so the AI
   knows the image is preview-quality.

The conversation row carries the inline base64 (acceptable for v1's
"show me what the camera saw" flow at preview resolution). Migrating
to a workspace-reference attachment that keeps bytes on disk is a
follow-up — see §18 Open Question 5.

### 3.3 WebSocket RPCs (admin + user)

`CameraEventService` implements `WsHandlerProvider` exposing:

| Frame type | Role | Purpose |
|---|---|---|
| `cameras.list` | user | Same as the `list_cameras` AI tool, role-filtered. |
| `cameras.events.list` | user | Page through `camera_events` collection — supports `camera`, `label`, `since`, `until`, `limit`, `offset`. **Role-filtered** — handler reads `caller.roles`, drops rows for cameras the caller can't see (per-camera `role_overrides`). |
| `cameras.events.snapshot` | user | Returns the event snapshot bytes (base64-encoded image) plus `media_type`. The handler proxies the fetch through `backend_auth_headers()` so the browser never sees the raw Frigate token. **Role-filtered** — same per-camera gate. |
| `cameras.events.clip` | user | Returns a Gilbert-proxied clip URL (`/api/cameras/event/<id>/clip.mp4`) suitable for direct `<video>` playback. The handler verifies role gate before returning the URL; the HTTP route enforces the same on every Range request (see §3.4). |
| `cameras.test_connection` | admin | Triggers the backend's `test_connection` `ConfigAction` and returns the result. |

WS RPC naming is **plural-noun-first throughout** (`cameras.events.list`,
`cameras.events.snapshot`, `cameras.events.clip`) — both the
collection-level (`cameras.list`, `cameras.test_connection`) and the
per-event family use a consistent dotted hierarchy.

Add `"camera.": 200` and `"cameras.": 100` (RPC) to `interfaces/acl.py`'s
`DEFAULT_EVENT_VISIBILITY` and `DEFAULT_RPC_PERMISSIONS` respectively.

**Every** RPC handler that returns event rows or media URLs MUST
apply the same per-camera role filter the AI tools apply (read
caller roles from the `WsConnection`, intersect with
`self._role_overrides`). The `cameras.` prefix permission gate (level
100) is *not* sufficient — admin-gated cameras must drop out of
results regardless of frame type. Test:
`test_cameras_events_list_rpc_filters_by_role` (§13.1).

### 3.4 HTTP routes (proxied media)

Bus event payloads carry **Gilbert-proxied** media URLs by default,
not raw Frigate URLs. Frigate's HTTP base URL is typically a
LAN-only IP/hostname (`http://frigate.local:5000/...`); a user
accessing Gilbert via a tunnel can't open it, and `Authorization:
Bearer ...` headers don't get sent cross-origin. To work around
both, this spec adds two routes to `WebApiService`:

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /api/cameras/events/<event_id>/snapshot.jpg` | session-cookie | Streams the event's snapshot bytes from the originating backend (resolved via `event.source_backend`). Adds `backend_auth_headers()` server-side. Honors `If-Modified-Since`. Returns 403 if the caller can't see the camera (role gate); 404 if the event/snapshot is gone. |
| `GET /api/cameras/events/<event_id>/clip.mp4` | session-cookie | Streams the event's clip with **Range request support** (`bytes=N-M`) for `<video>` seek. Same role gate, same 404 semantics. |

Two `CameraEvent` URL fields:

- `clip_url` — Gilbert-proxied URL, **the default in the bus event
  payload** and persisted into `camera_events`. Works everywhere —
  LAN, tunnel, mobile — without exposing Frigate.
- `direct_clip_url` — raw Frigate URL, **opt-in field** for advanced
  LAN-only consumers. Set by the backend; the service forwards it on
  the bus event but the dashboard / AI tools don't use it.

Same shape for `snapshot_url` / `direct_snapshot_url`. The AI tool
results echo the proxied URL; only operators who explicitly read
`direct_*` from the raw event payload see the LAN URL.

Implementation note: `WebApiService` already has the auth-cookie /
session middleware; the new routes are thin handlers (~30 lines each)
that resolve the event row, look up the backend, fetch through
`backend.get_snapshot()` / `backend.get_clip_url()` + the auth-header
helper, and stream the response. Range support for the clip route
uses `httpx.AsyncClient.stream("GET", url, headers={...})` and
forwards the `Range` header — Frigate supports byte-range requests
natively.

### 3.5 SPA / UI

**v1 (this feature):**

- Settings page entry under category `"Monitoring"` for the
  `cameras` namespace (auto-rendered from `config_params()`,
  exactly like the doorbell page today).
- A `<PluginPanelSlot slot="dashboard.bottom">` "Recent camera
  events" card, contributed via `Plugin.ui_panels()` from the
  frigate plugin (since the panel is plugin-frontend code), with
  `required_role="user"`. Shows the five most recent events
  (role-filtered) with thumbnails (fetched from
  `/api/cameras/events/<id>/snapshot.jpg` — see §3.4),
  camera/label, timestamp, click-through to the proxied clip URL.
  Lives in `std-plugins/frigate/frontend/RecentEventsCard.tsx`.

  The card receives event pushes via the existing core event-relay
  channel (`gilbert.sub.events` / `event` frames already streamed to
  WS clients in `web/ws_protocol.py` after the per-event role
  filter). The `useEventStream("camera.event.detected", ...)` hook
  already exists for the doorbell `Recent rings` card; the camera
  card uses the same hook. No additional WS plumbing is required.

**v2 (future, not in this spec):**

- Live HLS / WebRTC grid.
- Per-camera page (`/cameras/<name>`) with timeline, full event list,
  zone overlay.
- Search by Vision-tagged text.

## 4. Architecture

### 4.1 New files (core)

```
src/gilbert/interfaces/camera.py           # CameraEventBackend ABC + dataclasses
src/gilbert/core/services/camera.py        # CameraEventService
tests/unit/test_camera_service.py
```

### 4.2 New files (plugin)

```
std-plugins/frigate/
    plugin.yaml
    plugin.py
    pyproject.toml                         # depends on aiomqtt
    __init__.py
    backend.py                             # FrigateCameraBackend
    mqtt_client.py                         # aiomqtt connection wrapper with reconnect
    http_client.py                         # httpx-based snapshot/clip fetcher
    frontend/
        package.json
        api.ts                             # useFrigateApi()
        RecentEventsCard.tsx               # the dashboard card
        FrigateSettingsPanel.tsx           # extra settings UI (test buttons)
        panels.ts                          # registerPanel("frigate.recent_events", ...)
        types.ts
    tests/
        conftest.py                        # gilbert_plugin_frigate
        test_frigate_backend.py            # unit tests with mock MQTT/HTTP
        test_event_normalization.py        # Frigate JSON → CameraEvent
```

### 4.3 Imports / layer boundaries

The plugin imports **only** from `gilbert.interfaces.*`. Specifically:

- `gilbert.interfaces.camera` — `CameraEventBackend`, `CameraEvent`,
  `CameraInfo`.
- `gilbert.interfaces.configuration` — `ConfigParam`, `ConfigAction`,
  `ConfigActionResult`.
- `gilbert.interfaces.tools` — `ToolParameterType` (for ConfigParam types).
- `gilbert.interfaces.plugin` — `Plugin`, `PluginContext`, `PluginMeta`,
  `UIPanel`.

`CameraEventService` (in `core/services/camera.py`) imports from
`gilbert.interfaces.*` only, plus the cross-service capability
protocols (`ConfigurationReader`, `EventBusProvider`, `SchedulerProvider`,
`StorageProvider`, `SpeakerProvider`, `VisionBackend` — wait, vision is
a backend, not a provider; we get it via the `VisionService` capability
"vision"). Concretely:

```python
from gilbert.interfaces.camera import (
    CameraEvent, CameraEventBackend, CameraInfo,
)
from gilbert.interfaces.configuration import (
    ConfigAction, ConfigActionResult, ConfigParam, ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter, FilterOp, IndexDefinition, Query, SortField, StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition, ToolParameter, ToolParameterType, ToolProvider,
)
from gilbert.interfaces.ui import ToolOutput
from gilbert.interfaces.attachments import FileAttachment
```

`CameraEventService` does **not** import:

- `gilbert.integrations.*` (there are no vendor-free camera backends to
  side-effect-import — Frigate is in the plugin),
- `gilbert.core.services.vision` (use the `vision` capability),
- any `web/` module.

`std-plugins/frigate/backend.py` does **not** import any internal Gilbert
module other than via `gilbert.interfaces.*`. It does the MQTT / HTTP
work in its own helper modules (`mqtt_client.py`, `http_client.py`)
which only import `aiomqtt` and `httpx`.

## 5. The interface — `interfaces/camera.py`

```python
"""Camera event backend interface — object detection + snapshot/clip retrieval.

Mirrors ``DoorbellBackend`` in shape but is built around a long-lived
event stream (push) rather than polling. Backends that can't push (e.g.
Reolink AI events via HTTP polling) implement ``stream_events`` as an
adapter over ``asyncio.Queue`` fed from a polling task.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam


class CameraEventPhase(StrEnum):
    """Lifecycle phase of a detection event.

    Frigate emits ``new`` (object enters frame), ``update`` (score /
    snapshot updated mid-event), and ``end`` (object left frame /
    timeout). We collapse ``new`` + ``update`` into ``ACTIVE`` and only
    re-emit on phase transition (avoid spamming the bus on every
    score update — backends are responsible for the dedup), and ``end``
    becomes ``ENDED``.
    """

    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class CameraInfo:
    """Static metadata about a single camera known to a backend."""

    name: str
    """Camera identifier (Frigate calls this the camera name; matches
    the topic prefix and the URL slug)."""
    labels: tuple[str, ...] = ()
    """Object labels this camera is configured to detect (e.g.
    ``("person", "car", "package")``). Empty if the backend can't
    report this — service callers should treat empty as "unknown,
    accept any label."""
    zones: tuple[str, ...] = ()
    """Named zones configured on this camera (e.g. ``("porch",
    "driveway")``). Used for filtering and human-readable event
    descriptions."""
    has_audio: bool = False
    has_ptz: bool = False
    snapshot_supported: bool = True
    clip_supported: bool = True


@dataclass(frozen=True)
class CameraEvent:
    """A single detection event from a camera.

    Backends produce these; the service publishes them on the bus and
    persists them. All timestamps are epoch milliseconds (UTC) for
    consistency with ``RingEvent`` and direct comparability via ``int``
    arithmetic — service callers that want ISO format use the
    ``started_iso`` / ``ended_iso`` helper properties below.
    """

    event_id: str
    """Backend-assigned unique id (Frigate provides one; if a backend
    doesn't, synthesize ``f"{camera}-{started_at}-{label}"``)."""
    camera: str
    label: str
    """Object class — ``"person"``, ``"car"``, ``"package"``, ``"dog"``,
    ``"face"``, etc. Backend-defined namespace."""
    sub_label: str = ""
    """Optional sub-classification — for ``"face"`` events Frigate
    emits the recognized identity here. For unknown / generic
    detections leave empty."""
    phase: CameraEventPhase = CameraEventPhase.ACTIVE
    score: float = 0.0
    """Confidence 0..1. For ENDED events, this is the *top* score over
    the event's lifetime."""
    started_at: int = 0
    """Epoch ms at first frame in event."""
    ended_at: int = 0
    """Epoch ms at last frame in event (only set when ``phase == ENDED``)."""
    zones: tuple[str, ...] = ()
    """Named zones the object entered during the event."""
    snapshot_url: str = ""
    """HTTP(S) URL pointing at the event's best snapshot, served by the
    backend. Empty if no snapshot is available."""
    clip_url: str = ""
    """HTTP(S) URL pointing at the recorded clip. Empty until the
    backend has finalized the clip (typically only on ENDED). May
    require auth headers — see ``backend_auth_headers``."""
    has_snapshot: bool = False
    has_clip: bool = False
    source_backend: str = ""
    """``backend_name`` of the producing backend, set by the backend so
    the service can stamp the bus event with provenance."""
    direct_snapshot_url: str = ""
    """Raw backend snapshot URL (LAN-only). Operators who know what
    they're doing can use this; bus event consumers should prefer
    ``snapshot_url`` (Gilbert-proxied)."""
    direct_clip_url: str = ""
    """Raw backend clip URL (LAN-only). Same caveat as ``direct_snapshot_url``."""
    raw: Mapping[str, object] = field(default_factory=dict)
    """Original backend payload as a read-only mapping (typing-only;
    backends pass a plain dict and consumers SHOULD treat it as
    immutable). Debug-only and forward-compatible — every legitimate
    consumer reads typed attributes. Not serialized into the bus
    event or persisted form by default."""


class CameraEventBackend(ABC):
    """Abstract camera/object-detection backend.

    **Lifecycle variant — streaming.** This ABC departs from the
    standard ``initialize / close`` lifecycle that polling backends
    (``DoorbellBackend``, ``PresenceBackend``, ``TTSBackend``, …) use.
    Camera backends are *push*-driven and additionally implement
    ``connect / disconnect / stream_events``. The split exists so the
    service can probe config (``test_connection``) via
    ``initialize`` without starting the firehose.

    1. ``initialize(config)`` — connect to the broker / API, do any
       handshake. Must NOT start streaming yet — ``connect()`` does that.
    2. ``connect()`` — begin streaming. Returns once the connection is
       established (or raises). The backend is now obligated to drive
       events into ``stream_events()`` until ``disconnect()`` is called.
    3. ``stream_events()`` — async iterator over ``CameraEvent``.
       Implemented as ``async def stream_events(self) -> AsyncIterator[CameraEvent]: yield ...`` —
       i.e., an *async generator function* (typed return is
       ``AsyncIterator``; the runtime object is an
       ``AsyncGenerator``). Yields on every state change. Iterator
       stops cleanly when ``disconnect()`` is called.
    4. ``disconnect()`` — stop streaming, close connections, but the
       instance remains reusable (call ``connect()`` again to resume).
    5. ``close()`` — full teardown, release HTTP clients, wipe state.
       Backend instance should not be used after this.

    Future polling-style camera backends (e.g. Reolink AI HTTP poll)
    SHOULD still subclass ``CameraEventBackend`` and implement
    ``stream_events`` as an adapter over an internal ``asyncio.Queue``
    fed by a polling task — that way the service interface stays
    uniform. The ``connect / disconnect`` methods become wrappers
    around starting/stopping the polling task.
    """

    _registry: dict[str, type["CameraEventBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Defensive: every concrete subclass must declare ``backend_name``
        # explicitly, otherwise an unnamed subclass silently registers as
        # the empty string and overwrites any prior unnamed registration.
        # Test fakes and intermediate abstract subclasses can leave it
        # empty (they don't end up in the registry).
        if cls.backend_name:
            existing = CameraEventBackend._registry.get(cls.backend_name)
            if existing is not None and existing is not cls:
                # Last-write-wins is the documented behavior, but warn
                # so duplicate-registration bugs don't go silent.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "CameraEventBackend %r already registered as %s; "
                    "overwriting with %s",
                    cls.backend_name, existing.__name__, cls.__name__,
                )
            CameraEventBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["CameraEventBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def connect(self) -> None:
        """Open the live event stream. Idempotent: if the backend is
        already connected, returns without reopening. After a transport
        error (where the service caught a ``CameraBackendError``), the
        backend is in a disconnected state and ``connect()`` MUST
        reopen the underlying broker/API connection."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the live event stream. Idempotent: a second call on
        an already-disconnected backend MUST NOT raise.

        Must terminate any in-flight ``stream_events()`` iterators
        cleanly (e.g. by closing the underlying queue / cancelling the
        consumer task).
        """
        ...

    @abstractmethod
    async def stream_events(self) -> AsyncIterator[CameraEvent]:
        """Yield CameraEvents as the backend produces them.

        Implementations write this as an async generator:
        ``async def stream_events(self) -> AsyncIterator[CameraEvent]:
        while ...: yield event``. The typed return is
        ``AsyncIterator[CameraEvent]``; the runtime object is the
        ``AsyncGenerator`` produced by the generator function. (PEP 492
        async generators ARE both ``AsyncIterator`` and
        ``AsyncGenerator`` — the wider type is fine for the ABC.)

        MUST be safe to call exactly once per ``connect()`` cycle. The
        service will not subscribe twice. If the backend supports fanout,
        it can wrap a broadcast queue internally.

        Stops cleanly when ``disconnect()`` is called or when the
        underlying transport is permanently lost (and reconnect retries
        are exhausted) — in the latter case the backend SHOULD raise
        a ``CameraBackendError`` rather than silently returning, so
        the service can surface a ``camera.backend.disconnected`` event.
        """
        ...

    @abstractmethod
    async def list_cameras(self) -> list[CameraInfo]:
        """Static metadata about every camera the backend can see.

        Called on ``start()`` and on a 5-minute refresh timer. Backends
        that need to authenticate to enumerate cameras should cache the
        result internally and refresh on a slower cadence.
        """
        ...

    @abstractmethod
    async def get_snapshot(
        self,
        camera: str,
        event_id: str | None = None,
    ) -> SnapshotRef:
        """Fetch a snapshot.

        - ``event_id is None`` → live snapshot of the camera.
        - ``event_id`` → the historic snapshot for that event (must
          have been emitted by ``stream_events()`` previously, or
          retrievable from the backend's history).

        Returns a ``SnapshotRef`` so callers can decide whether to
        embed the bytes or proxy via URL. Backends are encouraged to
        return a URL for live snapshots (cheap) and bytes for historic
        ones (cached on disk by the backend, no HTTP round-trip).
        """
        ...

    @abstractmethod
    async def get_clip_url(self, event_id: str) -> str | None:
        """Return a URL to the event's clip, or None if unavailable.

        The URL may require auth headers exposed via
        ``backend_auth_headers()``. Service callers proxy via a
        Gilbert HTTP route that adds those headers transparently —
        the URL never gets handed to the browser raw if it needs auth.
        """
        ...

    def backend_auth_headers(self) -> dict[str, str]:
        """Optional auth headers for direct HTTP fetches of
        snapshot/clip URLs. Default: no auth. Override when the
        backend serves media behind a token."""
        return {}


@dataclass(frozen=True)
class SnapshotRef:
    """Discriminated container for a snapshot — URL or bytes.

    Exactly one of ``url`` and ``data`` should be non-empty.
    ``media_type`` is required when ``data`` is set.
    """

    url: str = ""
    data: bytes = b""
    media_type: str = ""

    @property
    def is_inline(self) -> bool:
        return bool(self.data)


class CameraBackendError(Exception):
    """Raised by a backend when its event stream has terminally
    failed (reconnect retries exhausted, auth permanently rejected,
    etc). The service catches this, publishes
    ``camera.backend.disconnected``, and may schedule a re-connect
    based on its own retry policy.
    """


@runtime_checkable
class CameraProvider(Protocol):
    """Capability protocol for the camera service. Other services
    (greeting, agent tools, future plugins) ``isinstance``-check
    against this rather than the concrete ``CameraEventService``."""

    async def list_cameras(self) -> list[CameraInfo]: ...

    async def latest_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 20,
    ) -> list[CameraEvent]: ...

    async def get_event(self, event_id: str) -> CameraEvent | None: ...

    async def get_snapshot_bytes(
        self,
        event_id: str,
        *,
        max_height: int | None = 720,
    ) -> tuple[bytes, str] | None:
        """Return ``(bytes, media_type)`` for an event's snapshot, or
        None if not available. The service handles backend resolution
        and auth-header proxying. ``max_height`` requests a server-side
        downscale where the backend supports one (Frigate honors
        ``?h=<n>``); pass ``None`` to request the full-resolution
        snapshot."""
        ...
```

Notes:

- `CameraEvent` is `frozen=True` like `RingEvent`, `UserPresence`, and
  `Event`. Persisted form goes through `dataclasses.asdict(...)` minus
  the `raw` field (which we drop for storage to avoid bloating rows).
- `SnapshotRef` is the explicit "URL or bytes" choice. Callers who
  want raw bytes call `service.get_snapshot_bytes(...)` and the
  service handles URL fetch + auth header injection transparently.
- `CameraProvider` is the **capability protocol** other services use.
  Greeting subscribes to events directly off the bus (no protocol
  needed). The AI itself can compose `latest_clips` results with a
  separate `presence.who_is_here()` call (existing `PresenceProvider`
  protocol) when it wants to correlate; the camera service does
  not invoke presence in any tool implementation.

## 6. The service — `core/services/camera.py`

### 6.1 Class skeleton

```python
class CameraEventService(
    Service,                # ServiceInfo, start, stop
    # implements via duck typing (Configurable, ToolProvider,
    # ConfigActionProvider, WsHandlerProvider, CameraProvider)
):
    """Subscribes to a CameraEventBackend, republishes onto the bus,
    persists, and exposes AI tools.

    Singleton. Per-request state (caller user_id, conversation id) is
    read from injected tool args / ContextVars, never stored on self.
    """

    config_namespace = "cameras"
    config_category = "Monitoring"
    slash_namespace = "cameras"   # for ToolProvider tools

    def __init__(self) -> None:
        self._backend: CameraEventBackend | None = None
        self._backend_name: str = "frigate"
        self._enabled: bool = False
        self._event_bus: EventBus | None = None
        self._storage: StorageBackend | None = None
        self._resolver: ServiceResolver | None = None

        # Live tasks
        self._stream_task: asyncio.Task[None] | None = None
        self._reconnect_attempt: int = 0

        # Cached static metadata
        self._cameras: list[CameraInfo] = []
        self._cameras_by_name: dict[str, CameraInfo] = {}

        # Config (cached, repopulated in on_config_changed)
        self._retention_days: int = 7
        self._vision_text_retention_days: int = 0
        self._selected_cameras: tuple[str, ...] = ()
        self._default_camera_role: str = "everyone"
        self._role_overrides: dict[str, str] = {}     # camera_name -> "admin" | "user" | "everyone"
        self._vision_enabled_labels: frozenset[str] = frozenset()
        self._vision_per_camera: dict[str, list[str]] = {}     # camera -> list[label] (empty = disabled)
        self._vision_prompt: str = _DEFAULT_VISION_PROMPT
        self._reconnect_max_seconds: float = 60.0

        # Per-event-id locks for parallel vision annotation; never global.
        self._annotation_locks: dict[str, asyncio.Lock] = {}
        self._annotation_locks_guard = asyncio.Lock()
        # Bounded parallelism for vision calls (sized in on_config_changed).
        self._vision_semaphore: asyncio.Semaphore = asyncio.Semaphore(4)
```

`service_info()`:

```python
return ServiceInfo(
    name="cameras",
    capabilities=frozenset({"cameras", "ai_tools", "ws_handlers"}),
    requires=frozenset({"event_bus", "entity_storage"}),
    optional=frozenset({"configuration", "scheduler", "vision"}),
    events=frozenset({
        "camera.event.detected",
        "camera.event.ended",
        "camera.snapshot.annotated",
        "camera.backend.connected",
        "camera.backend.disconnected",
        # NOTE: camera.<label>.detected.<camera> is dynamic and
        # cannot be enumerated. ServiceInfo.events is frozenset[str]
        # (exact names) today; we list only the static names here
        # and document the dynamic family in memory-camera-events.md.
        # If a future ServiceInfo.event_patterns field is added, the
        # dynamic glob can be declared there.
    }),
    toggleable=True,
    toggle_description="Camera object-detection events",
)
```

### 6.2 Single backend now, aggregator-ready

The service holds **one** backend at a time, but the backend resolution
code path lives behind a small private method (`_resolve_backend`) that
returns a single backend instance from config. When/if we add a second
provider (Reolink), we change `_backend: CameraEventBackend | None` to
`_backends: dict[str, CameraEventBackend]` and fan stream consumption
across them via `asyncio.gather`. Public API on `CameraProvider`
(query / list / snapshot) is already shape-compatible with N backends —
list_cameras and latest_events naturally union, get_snapshot resolves
the right backend by event id (events store `source_backend` already).

**Camera-name collision rule (forward-compat note):** when a future
multi-backend deployment has two backends both reporting a camera named
`front_door`, the merge in `list_cameras()` MUST resolve the collision
deterministically — preserve the `source_backend` namespace by
returning the camera as `f"{source_backend}:{camera_name}"` for any
camera whose name appears in more than one backend's report (single-
backend case retains the bare name for ergonomic slash commands and
config parity). The aggregator's persistence-layer query already
filters by `source_backend` because events are stamped with it. v1
implementations don't need to do this work — but the merge function
is the only place this rule lives, so document it in
`memory-camera-events.md` so a future implementer doesn't rediscover
it.

Config schema is forward-compatible too: today there's a single
`backend: "frigate"` string and a single `settings.*` block; tomorrow
we'd accept a list `backends: [{name: ..., settings: ...}, ...]` and
the loader migrates the old single-backend form into a one-element
list. No public-API break.

### 6.3 start() flow

```python
async def start(self, resolver: ServiceResolver) -> None:
    self._resolver = resolver

    # Required
    bus_svc = resolver.require_capability("event_bus")
    if isinstance(bus_svc, EventBusProvider):
        self._event_bus = bus_svc.bus

    storage_svc = resolver.require_capability("entity_storage")
    if isinstance(storage_svc, StorageProvider):
        self._storage = storage_svc.backend

    # Indexes for query performance
    if self._storage is not None:
        await self._storage.ensure_index(IndexDefinition(
            collection="camera_events",
            fields=["camera", "started_at"],
        ))
        await self._storage.ensure_index(IndexDefinition(
            collection="camera_events",
            fields=["label", "started_at"],
        ))
        await self._storage.ensure_index(IndexDefinition(
            collection="camera_events",
            fields=["started_at"],
        ))

    # Optional: configuration
    full_section: dict = {}
    config_svc = resolver.get_capability("configuration")
    if isinstance(config_svc, ConfigurationReader):
        full_section = config_svc.get_section(self.config_namespace)
        self._apply_config(full_section)

    if not full_section.get("enabled", False):
        logger.info("Camera service disabled (enabled=false)")
        return

    self._enabled = True

    # Resolve backend
    self._backend_name = full_section.get("backend", "frigate")
    backends = CameraEventBackend.registered_backends()
    backend_cls = backends.get(self._backend_name)
    if backend_cls is None:
        logger.warning(
            "Camera service NOT starting — unknown backend %r "
            "(registered: %s)",
            self._backend_name, sorted(backends),
        )
        self._enabled = False
        return
    self._backend = backend_cls()

    settings = dict(full_section.get("settings", {}))
    await self._backend.initialize(settings)

    try:
        self._cameras = await self._backend.list_cameras()
        self._cameras_by_name = {c.name: c for c in self._cameras}
    except Exception:
        logger.warning("Could not enumerate cameras at startup", exc_info=True)

    # Schedule periodic camera-list refresh + retention sweep
    scheduler = resolver.get_capability("scheduler")
    if isinstance(scheduler, SchedulerProvider):
        scheduler.add_job(
            name="cameras-refresh",
            schedule=Schedule.every(300.0),    # 5 min
            callback=self._refresh_camera_list,
            system=True,
        )
        scheduler.add_job(
            name="cameras-retention-sweep",
            schedule=Schedule.every(3600.0),   # 1 hour
            callback=self._sweep_old_events,
            system=True,
        )

    # Spawn the stream consumer task. Use an explicit context
    # snapshot so any ContextVar.set() inside the loop doesn't
    # leak across tasks (and inherits SYSTEM user from boot).
    # ``asyncio.create_task`` accepts ``context=`` as of Python 3.11;
    # passing an explicit copy here ensures the consumer's own context
    # is not shared with whatever task happens to be calling
    # ``start()``. See §11 for the multi-user-isolation rationale.
    import contextvars
    self._stream_task = asyncio.create_task(
        self._run_stream_consumer(),
        name="cameras-stream-consumer",
        context=contextvars.copy_context(),
    )
```

### 6.4 The stream consumer loop

This is the heart of the service — and the part that's easy to get
wrong.

```python
async def _run_stream_consumer(self) -> None:
    """Long-lived consumer of the backend's event stream.

    Reconnects on transport failures with exponential backoff capped
    at ``self._reconnect_max_seconds``. Publishes
    ``camera.backend.connected`` / ``camera.backend.disconnected`` so
    the dashboard can show the live status.
    """
    backoff = 1.0
    while self._enabled and self._backend is not None:
        try:
            await self._backend.connect()
            self._reconnect_attempt = 0
            backoff = 1.0
            await self._publish_status("connected")

            async for ev in self._backend.stream_events():
                await self._handle_event(ev)

            # Stream ended cleanly — most often because disconnect()
            # was called by stop(); the outer while loop will exit.
            await self._publish_status("disconnected", error="stream ended")

        except CameraBackendError as exc:
            logger.warning("Camera backend stream error: %s — reconnecting in %.1fs", exc, backoff)
            await self._publish_status("disconnected", error=str(exc))
            try:
                await self._backend.disconnect()
            except Exception:
                logger.debug("disconnect() during reconnect raised", exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, self._reconnect_max_seconds)
            self._reconnect_attempt += 1
            continue

        except asyncio.CancelledError:
            # Service shutdown
            raise

        except Exception:
            logger.exception("Unexpected error in camera stream consumer")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, self._reconnect_max_seconds)
            continue

    logger.info("Camera stream consumer exited")
```

### 6.5 Per-event handling

```python
async def _handle_event(self, ev: CameraEvent) -> None:
    """Process a single backend event: persist, publish, optionally annotate."""
    # 1. Filter — selected_cameras config may narrow which cameras we care about.
    if self._selected_cameras and ev.camera not in self._selected_cameras:
        return

    # 2. Persist FIRST so the annotation task (spawned below) can read
    #    the row back without racing the persist write. Failures here
    #    are logged but don't block the bus event — the bus is the
    #    authoritative real-time signal, the camera_events collection
    #    is best-effort. See §6.6 contract note.
    try:
        await self._persist_event(ev)
    except Exception:
        logger.warning("Failed to persist camera event %s", ev.event_id, exc_info=True)

    # 3. Publish onto the bus, twice — once with the static name, once
    # with the dynamic glob-friendly name.
    required_role = self._effective_role(ev.camera)
    payload = self._event_to_payload(ev, required_role)

    detected_type = (
        "camera.event.detected" if ev.phase is CameraEventPhase.ACTIVE
        else "camera.event.ended"
    )
    if self._event_bus is not None:
        await self._event_bus.publish(Event(
            event_type=detected_type,
            data=payload,
            source="camera",
        ))
        # Glob-friendly companion event — only on ACTIVE (we don't
        # double-emit on END, the use cases are "react when X
        # appears" not "react when X leaves").
        if ev.phase is CameraEventPhase.ACTIVE:
            # Sanitize: skip the glob if label/camera contains "."
            # or whitespace — in practice Frigate constrains both
            # to identifiers, but malformed payloads shouldn't crash
            # the consumer or produce ambiguous patterns.
            if _glob_safe(ev.label) and _glob_safe(ev.camera):
                await self._event_bus.publish(Event(
                    event_type=f"camera.{ev.label}.detected.{ev.camera}",
                    data=payload,
                    source="camera",
                ))

    # 4. Vision annotation — async, do not block this handler. Spawn
    #    with an explicit context snapshot so future ContextVar.set()
    #    calls inside the annotation task can't leak back to the
    #    stream consumer. (See §11.)
    if self._should_annotate(ev):
        import contextvars
        asyncio.create_task(
            self._annotate_event(ev),
            name=f"camera-annotate-{ev.event_id}",
            context=contextvars.copy_context(),
        )
```

`_effective_role(camera)` returns the per-camera role override or the
default (`self._default_camera_role`, see §6.7). `_event_to_payload(ev,
required_role)` produces the dict for the `Event.data` field, including
`required_role` so the WS event filter can gate delivery (see §3.1 for
the data-level override primitive added by this PR).

**Glob emission asymmetry — note for subscribers.** The
`camera.<label>.detected.<camera>` companion event fires only on
`ACTIVE` (not `ENDED`). Subscribers who want "every person event,
including end-of-event"  must subscribe to `camera.event.*` or
maintain two subscriptions. Subscribers MUST also pick **one** of
the two — listening to both `camera.event.detected` and
`camera.<label>.detected.<camera>` for the same logical detection
yields duplicate handler invocations. This asymmetry is documented
in `memory-camera-events.md`.

### 6.6 Vision annotation

Off the hot path. Lock per `event_id` so a flapping `update` event
(should already be deduped at the backend, but defensive) can't fire
two annotations for the same event.

A semaphore (`self._vision_semaphore: asyncio.Semaphore`, default
size 4 via `vision_concurrency` config — §6.7) gates how many vision
calls can run in parallel; on a deployment with 50 simultaneous
person events we don't want to spawn 50 concurrent `describe_image`
calls and saturate the LLM provider's rate limit / RAM.

```python
async def _annotate_event(self, ev: CameraEvent) -> None:
    if not ev.has_snapshot:
        return

    async with self._annotation_lock(ev.event_id):
        # Avoid re-annotating: check storage for vision_text presence.
        # Persist completed before spawn (§6.5 step 2), so the row
        # exists; if it doesn't (storage write failed) we proceed
        # rather than skip — annotation runs harmlessly and falls
        # back to a published-only annotation event.
        existing = await self._load_event(ev.event_id)
        if existing is not None and existing.get("vision_text"):
            return

        if self._resolver is None:
            return
        vision_svc = self._resolver.get_capability("vision")
        if not isinstance(vision_svc, VisionProvider):
            return  # Vision not available, silently skip

        # Fetch the snapshot bytes (prefer the backend's cached copy;
        # fall back to URL fetch with auth-header injection). No
        # in-memory cache for v1 — re-annotation is rare; the
        # service's deduplication via existing-row-check above
        # prevents repeat fetches for the same event_id. See §18 Q5.
        snap = await self._fetch_snapshot_bytes(ev, max_height=720)
        if snap is None:
            return
        bytes_, media_type = snap

        async with self._vision_semaphore:
            try:
                text = await vision_svc.describe_image(bytes_, media_type)
            except Exception:
                logger.warning("Vision describe_image failed for %s", ev.event_id, exc_info=True)
                return

        if not text:
            return

        # Partial update: re-load the latest persisted row, set just
        # vision_text/vision_model, write back. This avoids
        # clobbering fields a concurrent ``update`` event may have
        # already written (e.g. a later score/zones change).
        await self._update_event_vision_text(ev.event_id, text)

        if self._event_bus is not None:
            await self._event_bus.publish(Event(
                event_type="camera.snapshot.annotated",
                data={
                    "event_id": ev.event_id,
                    "camera": ev.camera,
                    "label": ev.label,
                    "vision_text": text,
                    "required_role": self._effective_role(ev.camera),
                },
                source="camera",
            ))
```

**Re-annotation contract.** On Gilbert restart, in-flight annotations
for events that hadn't completed are simply abandoned — the row
exists in storage with empty `vision_text`. The next `update` event
for that event id (if Frigate sends one after Gilbert reboots) will
run annotation again because the existing-row check sees
`vision_text == ""`. Annotations that completed before restart are
skipped via the existing-row check. Two `CameraEventService`
instances running concurrently is not supported by Gilbert's
service-singleton model; per-event-id locks intentionally do not
protect across processes.

**`VisionProvider` is a NEW capability protocol** added by this PR to
`interfaces/vision.py` (next to `VisionBackend`). Shape:

```python
@runtime_checkable
class VisionProvider(Protocol):
    """Capability protocol for the vision service (cross-service
    image-description access)."""

    async def describe_image(
        self,
        image_bytes: bytes,
        media_type: str,
    ) -> str: ...
```

This is the **minimal** surface the camera service needs. The spec
intentionally does **not** add `model_name` or `available`
properties — those would require touching every existing
`VisionBackend` implementation (`local_vision`, `anthropic_vision`,
…) and the `VisionService` surface, which is wider than this PR
warrants. `camera.snapshot.annotated` event payloads therefore
*omit* the `vision_model` field (callers who care which model wrote
the description can introspect via the configuration / settings
API). The `VisionService` already exposes `describe_image` — the
new protocol is purely declarative. Resolution: see §18, Open
Question 2.

### 6.7 Configurable

One AI prompt goes behind `ConfigParam(ai_prompt=True)`:

| Prompt | Purpose |
|---|---|
| `vision_prompt` | System prompt fed to Vision when annotating snapshots. See default below. |

The previously-spec'd `who_was_at_prompt` has been **dropped** along
with the `who_was_at` tool (see §3.2 — that tool's three-signal-merge
correlation pass was unsafe; the deterministic `who_was_seen` and
structured `count_detections` replacements don't call the LLM). The
greeting-service `camera_announce_prompt` (§10.3) is still
configurable and per-label.

`_DEFAULT_VISION_PROMPT` (full text — replaces the previous draft;
the previous "one paragraph" default produced speculation-heavy
prose that poisoned downstream tools):

```
Describe in one terse, observational sentence what is visible in
this security camera frame. Note: people (count and notable attire
or carried objects), vehicles (count and color), packages or
containers, animals. State only what you can see. Do not speculate
about identity, intent, or activity ("a delivery driver dropping
off a package" is wrong; "a person in brown clothing setting a box
near the door" is right). No preamble, no emoji, no hedging
qualifiers.
```

Voice rationale: the output of this prompt feeds (a) the
persisted `vision_text` field on the event row, (b) the
`latest_clips` AI tool result list. Both flow into downstream
LLM reasoning, so the prompt explicitly forbids identity and
intent speculation — those would otherwise become hallucinated
"facts" the AI cites confidently in answers.

The prompt follows the pattern in
[AI Prompts Are Always Configurable](../../.claude/memory/memory-ai-prompts-configurable.md):
module-level `_DEFAULT_VISION_PROMPT` constant,
`default=_DEFAULT_VISION_PROMPT` on the `ConfigParam`, cached on
`self._vision_prompt`, fallback to the constant on empty override.

Service-level params:

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | BOOLEAN | `false` | Standard toggle. |
| `backend` | STRING | `"frigate"` | `restart_required=True`. `choices` derived from `CameraEventBackend.registered_backends()`. |
| `retention_days` | NUMBER | `7` | How long `camera_events` rows live before sweep. |
| `vision_text_retention_days` | NUMBER | `0` | If `> 0`, run a parallel sweep that strips `vision_text`/`vision_model` from rows older than this (keeping the bare event metadata). `0` = no separate scrub (vision text expires with the row). Useful for users who want camera *metadata* retained for longer than camera *descriptions* — see §18 Q4 for PII rationale. |
| `selected_cameras` | ARRAY | `[]` | Subset of cameras to monitor; empty = all. `choices_from="cameras"`. |
| `default_camera_role` | STRING | `"everyone"` | Role required to see camera events for cameras NOT explicitly listed in `role_overrides`. `"everyone"` / `"user"` / `"admin"`. Households with kids/guests can flip this to `"user"` without authoring per-camera entries. |
| `role_overrides` | OBJECT | `{}` | Per-camera role override: `{camera_name: "admin" \| "user" \| "everyone"}`. Bumps both bus event visibility (via `data["required_role"]`) and AI tool / RPC result filtering. **UI:** the Settings page renders this as a per-camera grid (camera name × role select) populated from the cached `_cameras` list (rather than asking admins to author a JSON blob). The grid is the per-camera-role primitive across the configuration UI; this spec treats it as additive functionality on the `OBJECT` `ConfigParam` UI control. If the grid renderer doesn't exist yet, v1 ships the JSON-textarea fallback and a UI follow-up adds the grid. |
| `vision_per_camera` | OBJECT | `{}` | Per-camera override for vision-annotation labels: `{camera_name: ["person", "package"]}`. Truth table: missing key → use defaults; present with empty list `[]` → vision disabled for that camera; present with non-empty list → use these labels for that camera (overrides `vision_enabled_labels`). Replaces the ambiguous `vision_enabled_cameras` from the prior draft. |
| `vision_enabled_labels` | ARRAY | `["package"]` | Default labels to auto-annotate snapshots for. Empty = never. **Default narrowed** from `["person", "package"]` to `["package"]` — `person` events fire constantly on busy outdoor cameras (mail truck, neighbor walking dog, leaves in wind) and annotating every one burns LLM tokens. Add `"person"` opt-in. |
| `vision_concurrency` | NUMBER | `4` | Max parallel `describe_image` calls across all cameras (`asyncio.Semaphore`). Protects the LLM provider's rate limit. |
| `vision_prompt` | STRING | `_DEFAULT_VISION_PROMPT` | `multiline=True, ai_prompt=True`. |
| `reconnect_max_seconds` | NUMBER | `60.0` | Cap on reconnect backoff. |

(Removed from prior draft: `announce_labels`, `dedup_window_seconds`,
`vision_enabled_cameras`, `who_was_at_prompt`. Greeting-service
announcement config lives entirely under the greeting service —
see §10.3. The `vision_enabled_cameras` field had ambiguous
"empty = use defaults" semantics; `vision_per_camera` replaces
it with a self-documenting truth table.)

Plus all backend params merged in under `settings.*` with `backend_param=True`.
Forward `ai_prompt=bp.ai_prompt` per
[memory-ai-prompts-configurable.md](../../.claude/memory/memory-ai-prompts-configurable.md)
"Backend-declared prompts" — `_apply_backend_params` mirrors the
doorbell service's loop.

`choices_from="cameras"` is a new dynamic choices source — register it
with `ConfigurationService._resolve_dynamic_choices` similarly to how
`"doorbells"` and `"speakers"` work. The hook reads the cached
`self._cameras` from the running `CameraEventService` (using a new
`AvailableCameraLister` `@runtime_checkable Protocol` similar to
`AvailableDoorbellLister`).

### 6.8 ConfigActionProvider

Forward backend actions plus a service-level "Replay last 5 minutes"
action that's useful for debugging — pulls the backend's recent events
(if it exposes any historic-fetch surface, optional) and republishes them
onto the bus. Optional, marked admin-only. Delete from the spec if the
implementation budget is tight.

### 6.9 ToolProvider

The five tools listed in §3.2 (`list_cameras`, `latest_clips`,
`get_snapshot`, `who_was_seen`, `count_detections`). Each tool reads
the caller's roles from the injected `_user_roles: list[str]` argument
(per [memory-multi-user-isolation.md](../../.claude/memory/memory-multi-user-isolation.md))
and filters cameras accordingly. Snapshots / clips for `admin`-gated
cameras are 403-equivalent for non-admin callers (return error string,
`is_error=True`).

`get_snapshot` returns a `ToolOutput` (with the size cap and downscale
described in §3.2):

```python
MAX_INLINE_BYTES = 1_000_000  # raw JPEG bytes; ~1.4 MB base64

snap = await self.get_snapshot_bytes(event_id, max_height=720)
if snap is None:
    return ToolOutput(
        text=f"Snapshot no longer available for event {event_id}; "
             f"try latest_clips for recent activity.",
        is_error=True,
    )
snap_bytes, media_type = snap
if len(snap_bytes) > MAX_INLINE_BYTES:
    return ToolOutput(
        text=f"Snapshot for event {event_id} is too large for inline "
             f"display ({len(snap_bytes)} bytes). The clip URL "
             f"{ev.clip_url} should still work.",
        is_error=True,
    )

return ToolOutput(
    text=(
        f"Snapshot for {ev.camera} at {ev.started_iso} "
        f"(label={ev.label}, score={ev.score:.2f}). "
        f"Image is preview-quality (720px tall)."
    ),
    attachments=(
        FileAttachment(
            kind="image",
            name=f"{ev.camera}_{ev.event_id}.jpg",
            media_type=media_type,
            data=base64.b64encode(snap_bytes).decode(),
        ),
    ),
)
```

### 6.10 WsHandlerProvider

The four RPCs from §3.3. ACL prefixes go in `interfaces/acl.py`.

### 6.11 stop()

```python
async def stop(self) -> None:
    self._enabled = False
    if self._stream_task is not None:
        self._stream_task.cancel()
        try:
            await self._stream_task
        except (asyncio.CancelledError, Exception):
            pass
        self._stream_task = None
    if self._backend is not None:
        try:
            await self._backend.disconnect()
        except Exception:
            logger.debug("disconnect() during stop raised", exc_info=True)
        try:
            await self._backend.close()
        except Exception:
            logger.debug("close() during stop raised", exc_info=True)
        self._backend = None
```

## 7. Persistence — the `camera_events` collection

Document shape (one row per event, identified by `event_id`):

```json
{
  "_id": "<event_id>",
  "event_id": "<event_id>",
  "camera": "front_door",
  "label": "person",
  "sub_label": "",
  "score": 0.91,
  "phase": "ended",
  "started_at": 1730851234567,
  "ended_at": 1730851254567,
  "duration_seconds": 20.0,
  "zones": ["porch"],
  "snapshot_url": "/api/cameras/events/<id>/snapshot.jpg",
  "clip_url": "/api/cameras/events/<id>/clip.mp4",
  "has_snapshot": true,
  "has_clip": true,
  "source_backend": "frigate",
  "vision_text": "",
  "required_role": "everyone"
}
```

ISO-8601 string variants of `started_at` / `ended_at` are NOT
persisted; the dataclass exposes `started_iso` / `ended_iso`
properties that derive from the epoch-ms ints on read. Storing
both fields invites drift if a row is ever written by hand. The
epoch-ms field is authoritative for indexes and arithmetic; ISO
strings are presentation-layer concerns. (Also dropped from the
persisted shape: `vision_model`, per §6.6 — `VisionProvider` doesn't
expose `model_name` in v1.)

URLs in the persisted row are **Gilbert-proxied paths** (per §3.4),
not raw Frigate URLs. The `direct_*_url` fields from `CameraEvent`
are not persisted — they're operator escape hatches and can be
recomputed from the backend's HTTP base URL on demand.

Indexes (declared via `ensure_index` in `start()`):

- `(camera, started_at)` — for `latest_events(camera=...)`.
- `(label, started_at)` — for `latest_events(label=...)`.
- `(started_at,)` — for the time-window sweep and global recent listing.

`raw` is intentionally NOT persisted — it's debug-only and can balloon
the row size for events with embedded base64. If a future feature wants
to keep raw payloads, route them through a separate `camera_events_raw`
collection with its own retention.

### 7.1 Retention sweep — batched delete

`_sweep_old_events` runs hourly, removes rows where
`started_at < now_ms - retention_days * 86_400_000`. On a busy install
(10+ cameras, hundreds of events/day, 7-day retention = 5–10K rows),
a naïve `query → for row: delete(row._id)` loop blocks the event loop
for noticeable wall-time and contends with concurrent storage writes.

**This spec adds a new method to `StorageBackend`:**

```python
@abstractmethod
async def delete_query(self, query: Query) -> int:
    """Delete every row matching ``query``. Returns the count of rows
    removed. Implementations SHOULD perform this as a single atomic
    operation where the underlying store supports it (one
    ``DELETE WHERE`` for SQLite). Cascading FK deletes still apply."""
```

The SQLite implementation maps to one parameterized
`DELETE FROM <coll> WHERE started_at < ?` and returns the row count.
The retention sweep then becomes one round-trip plus FK-cascade
fallout. This is a precursor change — implementing agent must land
`StorageBackend.delete_query` and its SQLite implementation **before**
the camera retention sweep references it. See §17 step 1c.

If the precursor `delete_query` change is too large to land in this
PR, the v1 fallback is a chunked loop:
- `query(..., limit=100)` → batch of `_id`s.
- `await asyncio.gather(*(delete(coll, _id) for _id in batch))`.
- `await asyncio.sleep(0)` between batches.
- Cap total deletes per tick at 1000; remainder rolls into next tick.

Pick one path; mark the other in §17.

### 7.2 Schema-tolerance / forward-compat

Every backend-payload read goes through `dict.get(...) or default`,
never `payload[key]`. A Frigate version that drops or renames a field
(`current_zones` → `entered_zones` semantics shift, etc.) MUST NOT
crash the dispatch loop — the row gets persisted with whatever
fields *were* present, the rest stay at their dataclass defaults.
The `_on_message` exception-catching in §8.5 is the safety net;
defensive per-field reads in §8.6 are the first line of defense.

Minimum supported Frigate version: **0.13.0**. The
`test_connection` `ConfigAction` (§8.4) probes `/api/version` and
emits a WARNING (`ConfigActionResult.success=true` but with the
warning surfaced in the message) if the broker reports < 0.13.0 —
the plugin will still try to parse, but operators are warned.

## 8. The plugin — `std-plugins/frigate/`

### 8.1 `plugin.yaml`

```yaml
name: frigate
version: "1.0.0"
description: "Frigate NVR object-detection events via MQTT, plus snapshot/clip retrieval over HTTP"

provides:
  - frigate_camera

requires: []
depends_on: []
```

### 8.2 `plugin.py`

```python
from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, UIPanel


class FrigatePlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="frigate",
            version="1.0.0",
            description="Frigate camera-event backend (MQTT push + HTTP snapshots/clips)",
            provides=["frigate_camera"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # Side-effect import triggers CameraEventBackend.__init_subclass__.
        from . import backend  # noqa: F401

    async def teardown(self) -> None:
        pass

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="frigate.recent_events",
                slot="dashboard.bottom",
                label="Recent camera events",
                required_role="user",
            ),
            UIPanel(
                panel_id="frigate.settings",
                slot="settings.monitoring",
                label="Frigate diagnostics",
                required_role="admin",
            ),
        ]


def create_plugin() -> Plugin:
    return FrigatePlugin()
```

### 8.3 `pyproject.toml`

```toml
[project]
name = "gilbert-plugin-frigate"
version = "1.0.0"
description = "Frigate camera/object-detection backend for Gilbert"
requires-python = ">=3.12"
dependencies = [
    "aiomqtt>=2.3.0,<3.0.0",   # asyncio-native MQTT; pin major to avoid v3 breakage
]

[tool.uv]
package = false
```

**Why `aiomqtt`, not `paho-mqtt`?** Frigate publishes events natively
over MQTT; the entire backend exists to subscribe to those topics. Our
hot path is `async for msg in client.messages: ...` which `aiomqtt`
exposes idiomatically. `paho-mqtt` is sync; using it from asyncio
requires a thread for the receive loop and an asyncio.Queue bridge —
all the complexity of `aiomqtt` plus a thread we don't need. `aiomqtt`
is a thin asyncio wrapper around paho-mqtt under the hood, so we get the
mature, well-tested broker compatibility.

Add `aiomqtt` to the root `pyproject.toml`'s `[tool.uv.sources]` block
the same way every plugin's deps are declared.

**Maintenance status (late 2025):** aiomqtt is actively maintained —
last release within the past 6 months, paho-mqtt v2 supported. The
breaking-change history (v1 → v2 in 2024) means a v2-only constraint
is required (the `Client(...)` kwargs and `client.messages`
async-iterator are v2 shapes). If aiomqtt becomes abandoned, the
migration path is paho-mqtt v2 in a thread executor — `FrigateMQTT`
is small enough that this swap stays local to the plugin.

### 8.4 `backend.py` — `FrigateCameraBackend`

`backend_name = "frigate"`. Implements every abstract method.

`backend_config_params()` returns:

| Key | Type | Default | Notes |
|---|---|---|---|
| `mqtt_host` | STRING | `""` | `restart_required=True` |
| `mqtt_port` | INTEGER | `1883` | Default `8883` if `mqtt_tls` is on. |
| `mqtt_topic_prefix` | STRING | `"frigate"` | Frigate's `mqtt.topic_prefix`. |
| `mqtt_username` | STRING | `""` | |
| `mqtt_password` | STRING | `""` | `sensitive=True` |
| `mqtt_client_id` | STRING | `"gilbert-cameras"` | |
| `mqtt_tls` | BOOLEAN | `false` | Enables TLS; the params below configure it. |
| `mqtt_tls_ca_cert` | STRING | `""` | Path to a CA bundle (PEM) or inline PEM blob; required for self-signed broker certs on a home LAN (the common Mosquitto deployment). `sensitive=True` is unnecessary (CA certs are public), but the inline-PEM form should be `multiline=True`. |
| `mqtt_tls_client_cert` | STRING | `""` | Client certificate (PEM) for mutual TLS. `sensitive=True`. |
| `mqtt_tls_client_key` | STRING | `""` | Client private key (PEM) for mutual TLS. `sensitive=True`. |
| `mqtt_tls_insecure` | BOOLEAN | `false` | Skip hostname/cert verification. For self-signed brokers where you don't want to ship the CA. **Disables MITM protection** — surface a warning in the UI. |
| `mqtt_tls_server_hostname` | STRING | `""` | SNI / cert-CN override (use when the broker cert's CN doesn't match the IP / mDNS name). |
| `http_base_url` | STRING | `""` | `restart_required=True`. Frigate web UI base, e.g. `http://frigate.local:5000`. |
| `http_auth_mode` | STRING | `"none"` | One of `"none"` / `"bearer"`. v1 supports unauthenticated (typical LAN-only deploy) and bearer-token (proxy-style or Frigate API keys). Frigate 0.14+ session-cookie auth is **out of scope** for v1 — operators using it should disable it or front Frigate with a proxy that adds the bearer header. |
| `http_token` | STRING | `""` | `sensitive=True`. Bearer token; ignored when `http_auth_mode="none"`. |
| `verify_ssl` | BOOLEAN | `true` | Wired through to the HTTP client (`httpx.AsyncClient(verify=self._verify_ssl)`). Default `True`; toggle off for self-signed Frigate installs on the LAN — common deployment. |
| `cameras_filter` | ARRAY | `[]` | Empty = all cameras the broker reports. |

All TLS params map to `aiomqtt.TLSParameters(ca_certs=..., certfile=...,
keyfile=..., tls_insecure=..., server_hostname=...)`. The plugin
constructs the `TLSParameters` instance only when `mqtt_tls=true`; it
is `None` otherwise.

`backend_actions()` returns one action: `test_connection` which probes
HTTP `/api/version`, tries a 5-second MQTT connect+subscribe to
`<prefix>/+/events`, and additionally checks the retained
`<prefix>/available` topic for `online` / `offline`. Returns a
`ConfigActionResult` summarizing all three probes ("Connected to
Frigate 0.13.2; MQTT subscription successful; saw N cameras; broker
reports Frigate online."). If Frigate version < 0.13.0, surface a
warning in the result message but do not fail.

### 8.5 `mqtt_client.py` — connection wrapper

Frigate's MQTT topics:

- `<prefix>/events` — JSON payload with `{type: "new"|"update"|"end", before, after}`.
- `<prefix>/<camera>/events` — same per-camera; we subscribe to the
  global one and demux ourselves.
- `<prefix>/available` — Frigate's own LWT, `"online"` / `"offline"` retained.
- `<prefix>/<camera>/person`, `<prefix>/<camera>/car`, etc. — boolean
  retained on/off per label per camera; we ignore (the `/events` topic
  has the structured data).
- `<prefix>/<camera>/<label>/snapshot` — retained binary JPEG. We
  *might* subscribe to this for cheap thumbnail access without an HTTP
  round-trip; v1 spec uses HTTP for snapshots and we leave the MQTT
  binary topic as a future optimization.

**Reconnect strategy — single layer, in the service.** The plugin's
`FrigateMQTT._run` opens **one** `aiomqtt.Client` per call. On any
`MqttError` (transport drop, auth rejection, broker shutdown), it
exits the `async with` block, drains the sentinel into the queue,
and returns. The plugin does NOT loop internally; the
`stream_events` async iterator terminates and the *service*'s
`_run_stream_consumer` (§6.4) handles backoff and re-entry by
calling `backend.connect()` again. This avoids two-layer retry
where the layers might disagree on backoff or stop-conditions.

The terminal error is wrapped as `CameraBackendError` with the
underlying `MqttError` chained. Auth-permanent rejection
(`MqttCodeError` with auth-related code) raises immediately
without reconnect (the service's outer loop will still backoff,
but caps at `reconnect_max_seconds` — operator action required).

**LWT — Frigate's broker availability:** subscribe to
`<prefix>/available` (retained). When the message says `"offline"`
(Frigate down), publish `camera.backend.disconnected` with
`error="frigate offline"`. When it transitions to `"online"`,
publish `camera.backend.connected`. This is independent of the
MQTT transport status — Gilbert can be connected to a healthy
broker while Frigate-the-detector is down.

**LWT — Gilbert's own:** v1 does NOT publish a Gilbert LWT
(`gilbert/cameras/available` or similar). Gilbert is read-only on
this broker; cross-host status is a v2 concern. Documented
explicitly so an implementer doesn't add one speculatively.

**Frigate event re-fire after stationary gap:** Frigate sometimes
re-emits `new` after a 30-second "stationary" gap on the same
object. From Frigate's perspective these are distinct events with
distinct event_ids; from the service's perspective they're real
detections that flow through normally. Greeting-side dedup (§10.3)
handles the announcement collapsing.

Pseudocode (production code goes through `aiomqtt.Client`):

```python
class FrigateMQTT:
    def __init__(
        self,
        host, port, prefix, username, password,
        tls, tls_params,           # aiomqtt.TLSParameters | None
        client_id,
        client_factory=aiomqtt.Client,   # injectable for tests
    ) -> None:
        ...
        self._queue: asyncio.Queue[CameraEvent | object] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._frigate_online: bool | None = None  # last seen LWT state

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def events(self) -> AsyncIterator[CameraEvent]:
        while True:
            ev = await self._queue.get()
            if ev is _SENTINEL:
                return
            yield ev

    async def _run(self) -> None:
        # ONE aiomqtt session per call. Any error exits and surfaces
        # to the service for outer-loop reconnect handling.
        try:
            async with self._client_factory(
                hostname=self._host,
                port=self._port,
                username=self._username or None,
                password=self._password or None,
                tls_params=self._tls_params,           # constructed from §8.4
                client_id=self._client_id,
            ) as client:
                await client.subscribe(f"{self._prefix}/events")
                await client.subscribe(f"{self._prefix}/available")
                async for msg in client.messages:
                    if self._stop.is_set():
                        break
                    try:
                        self._on_message(msg)
                    except Exception:
                        logger.exception("Failed to dispatch frigate MQTT message")
        except aiomqtt.MqttError as exc:
            # Wrap and re-raise as CameraBackendError. The service's
            # outer loop catches it, sleeps with backoff, and calls
            # connect() again.
            raise CameraBackendError(f"MQTT transport error: {exc}") from exc
        finally:
            await self._queue.put(_SENTINEL)
```

`_on_message` parses the JSON payload and dispatches by topic:

- `<prefix>/events` — extract `before`/`after`, map to `CameraEvent`,
  enqueue. **Defensive parsing:** every field read uses
  `dict.get(...)` with a default; never `payload["after"]["id"]`.
  Garbage / partial / non-JSON payloads are logged at WARNING and
  dropped (no exception propagation). Test:
  `test_invalid_json_payload_dropped`.
- `<prefix>/available` — payload `b"online"` or `b"offline"`; update
  `self._frigate_online`, enqueue a `_FrigateAvailability` sentinel
  the consumer translates into a `camera.backend.connected` /
  `camera.backend.disconnected` bus event with
  `error="frigate offline"`.
- Anything else — ignore.

`_on_message` enqueues via `put_nowait`; the
`asyncio.QueueFull` exception is caught and logged as a warning.
We'd rather lose a single event than block the MQTT loop and have
the broker disconnect us for being slow. The 1000-event queue size
is a generous bound for the target install (< 1 event/sec).

### 8.6 Event normalization

Frigate's `events` payload (abridged):

```json
{
  "type": "new",
  "before": null,
  "after": {
    "id": "1730851234.567890-abc",
    "camera": "front_door",
    "label": "person",
    "sub_label": ["jeff", 0.93],
    "score": 0.81,
    "top_score": 0.91,
    "start_time": 1730851234.567,
    "end_time": null,
    "false_positive": false,
    "current_zones": ["porch"],
    "entered_zones": ["porch"],
    "snapshot": {"frame_time": 1730851234.567},
    "has_snapshot": true,
    "has_clip": false,
    "stationary": false
  }
}
```

Mapping (defensive — every read uses `.get()` with a default; missing
required fields drop the event with a debug-level log):

| Frigate field | `CameraEvent` field |
|---|---|
| `after.id` | `event_id` (required; drop if missing) |
| `after.camera` | `camera` (required; drop if missing) |
| `after.label` | `label` (required; drop if missing) |
| `after.sub_label` (see below) | `sub_label` |
| `type == "end"` | `phase = ENDED` else `ACTIVE` |
| `top_score` if ENDED else `score`, default `0.0` | `score` |
| `start_time * 1000` (rounded int), default `0` | `started_at` |
| `end_time * 1000` if set | `ended_at` |
| `current_zones` (list) | `zones` |
| computed via http_base_url + Gilbert proxy | `snapshot_url`, `clip_url` (proxied) and `direct_snapshot_url`, `direct_clip_url` (raw Frigate) |
| `has_snapshot` / `has_clip`, default `False` | direct |
| `false_positive == true` | drop the event entirely (no publish, no persist). |

**`sub_label` parsing** — Frigate's shape varies by version: sometimes
`["jeff", 0.93]`, sometimes `"jeff"`, sometimes `null`, sometimes
absent.

```python
sub_label_raw = after.get("sub_label")
if isinstance(sub_label_raw, list) and sub_label_raw:
    sub_label = sub_label_raw[0] if isinstance(sub_label_raw[0], str) else ""
elif isinstance(sub_label_raw, str):
    sub_label = sub_label_raw
else:
    sub_label = ""
```

The naïve `sub_label[0]` on a string field would yield the first
character of the name (`"j"`) — bug-prone. Test:
`test_sub_label_string_form` and `test_sub_label_list_form`.

**URLs.** The persisted/published `snapshot_url` and `clip_url` are
the **Gilbert-proxied** paths:

- `snapshot_url = f"/api/cameras/events/{event_id}/snapshot.jpg"`
- `clip_url = f"/api/cameras/events/{event_id}/clip.mp4"`

The plugin also stamps `direct_snapshot_url` and `direct_clip_url`
with the raw Frigate URLs:

- `direct_snapshot_url = f"{http_base_url}/api/events/{event_id}/snapshot.jpg"`
- `direct_clip_url = f"{http_base_url}/api/events/{event_id}/clip.mp4"`

Both raw URLs are gated by `http_token` if set; the proxy routes add
the auth header server-side via `backend_auth_headers()`.

**Phase mapping.**

- `type == "new"` → emit `ACTIVE`.
- `type == "update"` → backend dedup: only re-emit if score has
  changed by more than `0.05` OR new zones appeared OR snapshot
  frame_time advanced — otherwise drop. This prevents the bus from
  getting hammered on every Frigate detector tick.
- `type == "end"` → emit `ENDED`.

**Audio detection events** flow through transparently. Frigate 0.13+
emits `label` values like `bark`, `speech`, `baby_cry`, `glass_break`,
`dog`, `cat`, etc., on cameras that have `audio: { enabled: true }`.
For these:
- `has_snapshot` and `has_clip` are typically `false` (no frame to
  capture), so vision annotation is a no-op (the `has_snapshot`
  guard in `_annotate_event` short-circuits).
- The `camera.<label>.detected.<camera>` glob fires identically;
  subscribers can listen for `camera.glass_break.detected.*` if they
  want a security trigger.
- Greeting subscriptions respect `announce_camera_labels`. Default
  excludes audio (`["package"]`); operators opt-in by adding
  `glass_break` (etc.) to the list. A bark from the dog is excluded
  by default; an actual security siren is one config edit away.
- `vision_per_camera` and `vision_enabled_labels` apply uniformly —
  the audio-event short-circuit happens after label filtering, so
  configuring `vision_enabled_labels=["bark"]` is harmless (just
  produces no annotations because `has_snapshot=false`).

No special-casing of audio events in the dispatch path — they're just
events where `has_snapshot` happens to be false.

### 8.7 `http_client.py`

Thin httpx wrapper. The `httpx.AsyncClient` is constructed with
`verify=self._verify_ssl` so self-signed Frigate installs work
(`verify_ssl=False` is the LAN-only deployment) — default `True`.

Bearer auth depends on `http_auth_mode`:
- `"none"` — no `Authorization` header sent.
- `"bearer"` — adds `Authorization: Bearer <http_token>`. (Frigate
  0.14+ session-cookie auth is out of scope for v1; operators
  using it should disable it or front Frigate with a proxy that
  adds the bearer header.)

Methods: `get_version()`, `get_snapshot(event_id, height=720) -> bytes`
(passes `?h=<height>` to Frigate for server-side downscale; bounds
inline-attachment size — see §3.2), `get_clip_redirect(event_id) ->
str` (builds the URL plus auth headers; no fetch — the byte stream is
proxied through `WebApiService` per §3.4 with Range support).

### 8.8 `frontend/RecentEventsCard.tsx`

Subscribes to `camera.event.detected` via the existing
`useEventStream` hook (the same hook that drives the doorbell
`Recent rings` card and any other event-driven dashboard widget —
core's `web/ws_protocol.py` relays bus events to subscribed
clients as `event` frames after the role filter). Maintains a
sliding window of the last 5 events in component state. Renders
one row per event: thumbnail (fetched via
`/api/cameras/events/<id>/snapshot.jpg`, the proxy route from
§3.4), camera name, label, timestamp, click-through to the
proxied clip URL `/api/cameras/events/<id>/clip.mp4`.

If `useEventStream("camera.event.detected", ...)` is not wired up
in the existing core SPA hook, the implementing agent must add it
following the same shape as the doorbell hook — the relay path
already works at the WS protocol level.

Plugin-local API hook (`api.ts`):

```ts
import { useWebSocket } from "@/hooks/useWebSocket";
export function useFrigateApi() {
  const { rpc } = useWebSocket();
  return {
    listCameras: () => rpc("cameras.list", {}),
    listEvents: (params) => rpc("cameras.events.list", params),
    eventSnapshotBytes: (event_id) => rpc("cameras.events.snapshot", { event_id }),
    eventClipUrl: (event_id) => rpc("cameras.events.clip", { event_id }),
    testConnection: () => rpc("cameras.test_connection", {}),
  };
}
```

Same shape as `useBrowserApi` etc.

## 9. Configuration shape

```yaml
cameras:
  enabled: false
  backend: frigate
  retention_days: 7
  vision_text_retention_days: 0    # 0 = expire with the row
  selected_cameras: []
  default_camera_role: everyone    # everyone | user | admin
  role_overrides: {}               # {camera_name: role}
  vision_per_camera: {}            # {camera_name: ["person", "package"]}
  vision_enabled_labels:
    - package                      # narrowed default; add "person" opt-in
  vision_concurrency: 4
  vision_prompt: |
    Describe in one terse, observational sentence what is visible in
    this security camera frame. Note: people (count and notable attire
    or carried objects), vehicles (count and color), packages or
    containers, animals. State only what you can see. Do not speculate
    about identity, intent, or activity ("a delivery driver dropping
    off a package" is wrong; "a person in brown clothing setting a box
    near the door" is right). No preamble, no emoji, no hedging
    qualifiers.
  reconnect_max_seconds: 60.0
  settings:
    mqtt_host: ""
    mqtt_port: 1883
    mqtt_topic_prefix: frigate
    mqtt_username: ""
    mqtt_password: ""
    mqtt_client_id: gilbert-cameras
    mqtt_tls: false
    mqtt_tls_ca_cert: ""
    mqtt_tls_client_cert: ""
    mqtt_tls_client_key: ""
    mqtt_tls_insecure: false
    mqtt_tls_server_hostname: ""
    http_base_url: ""
    http_auth_mode: none           # none | bearer
    http_token: ""
    verify_ssl: true
    cameras_filter: []
```

(Greeting-side announcement config — `announce_camera_labels`,
`camera_announce_dedup_seconds`, `camera_announce_prompt`,
`camera_zone_groups`, `camera_announce_per_label_overrides` — lives
under the greeting service's namespace, not here. See §10.3.)

## 10. Wiring into existing services

### 10.1 `app.py` — register the service

Add (next to `DoorbellService`, since it's a sibling event-stream
service):

```python
from gilbert.core.services.camera import CameraEventService
self.service_manager.register(CameraEventService())
```

Plus a config factory for hot-swap (`config_svc.register_factory("cameras", self._factory_cameras)`).

### 10.2 ACL defaults — `interfaces/acl.py`

Add to `DEFAULT_EVENT_VISIBILITY`:

```python
"camera.": 200,                  # everyone (default)
"camera.backend.": 0,            # admin (debug status)
```

(The per-event override via `data["required_role"]` overrides this for
specific cameras — see the §3.1 callout for the new
`resolve_event_visibility(event_type, data)` helper this PR adds.)

Add to `DEFAULT_RPC_PERMISSIONS`:

```python
"cameras.": 100,                 # user-level (handlers filter by role per camera)
```

The per-frame handlers do their own role-gating against the per-camera
overrides — RPC prefix permission is necessary but not sufficient.

**Concrete `interfaces/acl.py` changes shipped by this PR:**

1. Add `"camera.": 200` and `"camera.backend.": 0` to
   `DEFAULT_EVENT_VISIBILITY`.
2. Add `"cameras.": 100` to `DEFAULT_RPC_PERMISSIONS`.
3. Add new function `resolve_event_visibility(event_type, data)` that
   honors `data["required_role"]` and falls back to
   `resolve_default_event_level(event_type)`. Document
   `data["required_role"]` as a reserved key in the module docstring.
4. Update `web/ws_protocol.py` to call `resolve_event_visibility`
   in the per-event filter path (currently `can_see_event` resolves
   purely by event type — it must take the `Event` and pass `data`
   through).
5. Tests in `tests/unit/test_ws_protocol.py` covering the data-level
   override case (admin gates, falls back on missing/unknown values).

### 10.3 GreetingService extension

`GreetingService.start()` already subscribes to `presence.arrived`. Add
a parallel subscription to `camera.event.detected`, filtered by config:

```python
if self._announce_camera_labels:
    self._unsubscribe_camera = self._event_bus.subscribe(
        "camera.event.detected",
        self._on_camera_event,
    )
```

`_on_camera_event` checks the event's `label` against
`self._announce_camera_labels`, computes a **dedup key** (see below),
checks the in-memory per-key last-announced timestamp window, and
announces via the same `SpeakerProvider.announce()` already used for
arrivals.

**Dedup key — composite, label-driven.** Plain per-(camera, label)
dedup is broken in real-world deployments: one person walking up the
path triggers `camera=driveway/label=person` then
`camera=front_door/label=person` within ten seconds (overlapping
fields-of-view across adjacent cameras), and Frigate's stationary-gap
re-fire produces yet a third `new` event. The user's expectation is
"one announcement per incoming visitor / one announcement per
delivery." Two knobs make that work:

1. **`camera_zone_groups: dict[str, list[str]]`** (new config) —
   maps a logical zone name to the cameras that watch the same
   physical area. Example:
   ```yaml
   camera_zone_groups:
     front_entry: [driveway, front_porch, front_door]
     side: [side_gate, side_yard]
   ```
   For dedup purposes, every camera in a group is treated as the
   same camera. Cameras not listed in any group are their own
   one-element group.

2. **`camera_announce_dedup_keys: dict[str, list[str]]`** (new
   config) — per-label dedup-key shape. Default:
   ```yaml
   camera_announce_dedup_keys:
     package: ["label"]                    # one announce per delivery, regardless of camera
     person:  ["label", "zone_group"]      # one announce per zone group
   ```
   `["label"]` collapses across all cameras (right for `package`:
   one delivery = one announcement, even if the box is visible on
   three cameras). `["label", "zone_group"]` collapses within a
   zone (right for `person`: front entry and side yard each get
   one announcement, but they're independent of each other). Apps
   that want "one announce per camera-event" can use
   `["label", "camera"]`.

The dedup-state map (`self._camera_announce_dedup_state: dict[str,
float]`) keys on the rendered tuple per the per-label key shape and
records the last-announced timestamp.

**Greeting config (cameras-related):**

| Key | Default | Notes |
|---|---|---|
| `announce_camera_labels` | `["package"]` | Default list. `package` (deliveries) is the obvious always-on; `person` is opt-in (busy outdoor cameras fire constantly). |
| `camera_announce_dedup_seconds` | `300.0` | Window for the dedup key. |
| `camera_announce_dedup_keys` | `{package: ["label"], person: ["label", "zone_group"]}` | Per-label dedup-key shape. |
| `camera_zone_groups` | `{}` | Map a logical zone name to the list of cameras watching it. Empty by default; users with adjacent cameras populate it. |
| `camera_announce_prompt` | `_DEFAULT_CAMERA_ANNOUNCE_PROMPT` | `multiline=True, ai_prompt=True`. Used when no per-label override is set. |
| `camera_announce_per_label_prompts` | `{}` | Optional per-label prompt overrides: `{package: "...", person: "...", glass_break: "..."}`. The `OBJECT` ConfigParam renders as a key-value editor. Tone matters by label — package = polite/informative, glass_break = urgent — and one base prompt can't cover both well. |

`_DEFAULT_CAMERA_ANNOUNCE_PROMPT` (full text):

```
Generate one short alert sentence (under 12 words) that announces a
{label} event at the {camera} camera. Use the time of day if it is
relevant ({time_of_day}: late_night, morning, midday, evening). Vary
the phrasing across calls — don't always start with "There's…". No
emoji. No hedging. Pick a tone consistent with the label: package =
warm and informative; person = neutral observation; glass_break /
smoke = brief and urgent.
```

The greeting service substitutes `{label}`, `{camera}`, and
`{time_of_day}` before sending the prompt to the LLM, then speaks the
result via the speaker provider. (The prompt is configurable; users
who want full custom control can author an entirely different
template.)

**Default behavior alignment with the elevator pitch.** The §0
elevator pitch lists "person at the side gate" as a use case. The
spec defaults `announce_camera_labels: ["package"]` (no `person`)
because announcing every person event on a busy outdoor camera is
intolerable. Operators who want "person at the side gate" combine
`announce_camera_labels: ["person", "package"]` with
`selected_cameras: ["side_gate"]` (or use `camera_zone_groups` to
narrow). The pitch is achievable in two config edits.

### 10.3.1 Mute camera alerts

A new `mute_camera_alerts` AI tool (declared on `GreetingService`,
not `CameraEventService`, since it controls greeting-side dedup):

| Tool | `slash_command` | What it does |
|---|---|---|
| `mute_camera_alerts` | `/cameras mute` | Args: `camera: str | None`, `label: str | None`, `until: str | None` (relative or ISO). Writes a temporary mute entry to a `camera_mutes` collection (`{camera, label, until_ms}`). `_on_camera_event` consults this before announcing — dropped events still flow on the bus, only the announcement is suppressed. Returns a `UIBlock` confirmation ("Mute side_gate person alerts until 8am tomorrow? [Confirm]") so accidental mutes don't fire silently. |

Without this affordance, "Hey Gilbert, stop announcing the side gate
camera until tomorrow morning" forces the user to navigate Settings,
edit `announce_camera_labels` or zone groups, save, and undo it the
next morning — a UX dead-end. The mute tool, the slash command, and
the confirm UIBlock close the loop.

`label=None` mutes every label for the camera (e.g. mute everything
on side_gate). `camera=None` mutes the label across all cameras (e.g.
mute every package alert tonight). `until=None` defaults to "until
08:00 tomorrow local."

### 10.4 VisionService — read-only consumer

`CameraEventService` resolves the vision capability via
`resolver.get_capability("vision")` on demand inside `_annotate_event`,
checking it against the new `VisionProvider` protocol with
`isinstance`. No changes to `VisionService` itself — the protocol is
purely declarative and the existing `describe_image` method is the
entire surface required. The camera service does not invoke
`PresenceProvider` from any tool path (the prior draft's `who_was_at`
correlation has been dropped — see §3.2).

### 10.5 ConfigurationService — dynamic choices

Add `cameras` to `_resolve_dynamic_choices` so any param with
`choices_from="cameras"` enumerates camera names from a service
implementing the new `AvailableCameraLister` protocol:

```python
@runtime_checkable
class AvailableCameraLister(Protocol):
    @property
    def available_cameras(self) -> list[str]: ...
```

Lives in `interfaces/camera.py` next to `CameraProvider`.

### 10.6 Slash command groups

`CameraEventService.slash_namespace = "cameras"`. Tools share
`slash_group="cameras"` and individual `slash_command` values:
`list`, `clips`, `seen`, `count`. (`get_snapshot` is AI-only — no
slash command, see §3.2 rationale.) Greeting's `mute_camera_alerts`
contributes `mute` under the same group. All have `slash_help`
strings (one-liners).

## 11. Multi-user isolation audit

Per the [Multi-User Isolation memory](../../.claude/memory/memory-multi-user-isolation.md):

- `CameraEventService` is a singleton. **Every** instance attribute is
  service-lifetime: backend handle, cached camera list, config
  values, the long-lived stream task, and the per-event-id dict of
  annotation locks (gated by `_annotation_locks_guard`). No
  `_current_*` / `_active_*` / `_pending_*` attrs.
- Tool handlers read caller identity from injected `_user_id`,
  `_user_roles` arguments, never from `self`. The `who_was_seen`,
  `count_detections`, `latest_clips`, and `get_snapshot` tools filter
  result lists by per-camera role overrides using the injected roles.
- The stream consumer task is spawned with
  `asyncio.create_task(..., context=contextvars.copy_context())` so
  any future `set_current_*` calls inside it (none today) can't leak.
  This matches the `create_task(coro, context=...)` form in §6.3.
- Vision annotation tasks spawned via `asyncio.create_task` likewise
  use `context=contextvars.copy_context()` — explicit at the call site
  in §6.5 step 4. (`create_task` defaults to inheriting the current
  context, which is the stream-consumer task's context — passing an
  explicit copy makes the snapshot-on-spawn intent obvious and
  forecloses the "what about future ContextVar.set() calls inside
  the consumer" concern.)
- Per-event-id locks (`_annotation_locks: dict[str, asyncio.Lock]`)
  rather than a global lock. Different events annotate concurrently;
  same event won't double-annotate.
- The `_vision_semaphore: asyncio.Semaphore` (size = `vision_concurrency`)
  bounds parallel vision calls, but doesn't carry per-user state.
- No global lock around `_handle_event` itself — it's called serially
  by the single stream consumer task, so concurrent invocations are
  impossible by construction.

Tests should include:

- Two simulated concurrent user calls to `latest_clips` with different
  `_user_roles` — admin sees admin-gated cameras, user does not.
- Concurrent `_handle_event` calls (test directly, not via the loop)
  for the same event id — second call must observe the first call's
  persistence and skip re-annotation.

## 12. Dependencies between services / startup ordering

`CameraEventService.requires = {"event_bus", "entity_storage"}`
(scheduler is optional — service degrades to no retention sweep). With
the existing topological sort, both required deps are core services
that come up first. The backend is loaded by the plugin during
`plugin.setup()`; `start_all()` runs after `_load_plugins()` so by the
time `CameraEventService.start()` calls
`CameraEventBackend.registered_backends()`, `FrigateCameraBackend` is
present.

## 13. Test plan

### 13.1 Core service unit tests (`tests/unit/test_camera_service.py`)

Test fixtures use a **per-test `_FakeCameraBackend` registry reset**
to prevent test fakes leaking into other tests' registries — an
`autouse` fixture snapshots `CameraEventBackend._registry` in
`setup` and restores it in `teardown`.

- **`test_starts_disabled_when_config_off`** — `enabled: false` →
  service starts but doesn't open backend, doesn't spawn task.
- **`test_starts_with_unknown_backend_logs_and_no_op`** — config picks
  a non-registered backend → service starts but stays disabled, no
  exception bubbled.
- **`test_publishes_detected_and_glob_event`** — drive a fake backend
  yielding one ACTIVE event → bus receives `camera.event.detected` and
  `camera.<label>.detected.<camera>` with matching payloads.
- **`test_does_not_publish_glob_on_ended`** — ENDED event → only
  `camera.event.ended` is published, no glob companion.
- **`test_glob_emission_skipped_for_unsafe_label_or_camera`** —
  label or camera contains `"."` or whitespace → only the static
  `camera.event.detected` event is published; the glob form is
  skipped.
- **`test_persists_event_to_camera_events_collection`** — fake backend
  + real SQLite test DB → row appears with expected fields, no
  `started_iso`/`ended_iso`/`vision_model` keys.
- **`test_persists_proxied_urls_not_raw_frigate_urls`** — fake
  backend returns raw + direct URLs → persisted row has the
  Gilbert-proxied paths in `snapshot_url`/`clip_url`.
- **`test_retention_sweep_deletes_old_rows`** — pre-seed rows with
  varied `started_at`, run sweep, assert only fresh rows remain.
  Uses the new `StorageBackend.delete_query` (or chunked fallback if
  the precursor change isn't landed yet).
- **`test_annotation_off_path_when_label_not_in_vision_enabled_labels`**
  — `vision_enabled_labels: ["package"]`, event with `label: "person"`
  → no annotation task spawned (verify via mock vision).
- **`test_annotation_runs_with_vision_provider`** — fake VisionProvider
  returns a description → event row updated, `camera.snapshot.annotated`
  fires (without a `vision_model` field in the data).
- **`test_annotation_lock_prevents_duplicate`** — fire two annotations
  for the same `event_id` concurrently → `describe_image` called once.
- **`test_annotation_persist_first_then_spawn`** — assert
  `_persist_event` completes before `_annotate_event` is spawned (no
  race where annotation reads a not-yet-persisted row).
- **`test_vision_semaphore_caps_concurrency`** — fake VisionProvider
  with a slow `describe_image` + 10 simultaneous events → at most
  `vision_concurrency` calls in flight.
- **`test_camera_role_override_filters_user_tool_call`** — tool called
  with `_user_roles=["user"]`, camera in `role_overrides` set to
  `"admin"` → that camera's events absent from result.
- **`test_default_camera_role_user_blocks_unknown_role_caller`** —
  `default_camera_role: "user"`, caller with no roles → empty results.
- **`test_required_role_lands_in_event_data`** — admin-gated camera
  → published event's `data["required_role"] == "admin"`.
- **`test_cameras_events_list_rpc_filters_by_role`** — call
  `cameras.events.list` with a non-admin caller, two seeded events
  (one admin-gated camera, one everyone) → only the everyone event
  comes back.
- **`test_reconnect_backoff_caps`** — fake backend raises
  `CameraBackendError` repeatedly → backoff doubles up to
  `reconnect_max_seconds`, never beyond.
- **`test_reconnect_calls_backend_connect_each_cycle`** — service
  retries by calling `backend.connect()` again after each
  `CameraBackendError` (single-layer reconnect, plugin doesn't
  retry internally).
- **`test_frigate_lwt_offline_publishes_disconnected`** —
  `<prefix>/available = "offline"` → `camera.backend.disconnected`
  fires with `error="frigate offline"`. Transition back to `"online"`
  → `camera.backend.connected`.
- **`test_stop_cancels_stream_task_promptly`** — start, stop within
  100ms → `_stream_task` is gone, backend.disconnect was called once,
  backend.close was called once.
- **`test_get_snapshot_tool_returns_attachment`** — set up a fake
  backend that returns `SnapshotRef(data=b"jpeg", media_type="image/jpeg")`
  → `get_snapshot` tool returns ToolOutput with one image attachment.
- **`test_get_snapshot_returns_error_when_backend_404`** — fake
  backend returns `None` (snapshot expired in Frigate) →
  `ToolOutput(is_error=True, text=...no longer available...)`.
- **`test_get_snapshot_caps_max_inline_bytes`** — fake backend returns
  > 1 MB raw → tool errors out rather than producing oversized
  attachment.
- **`test_who_was_seen_returns_face_matches_and_unknown_count`** —
  pre-seed 5 events, 1 with `sub_label="jeff"`, 4 with empty
  `sub_label` and `label="person"` → result has `[{name: "jeff",
  count: 1, ...}]` and `unknown_count: 4`.
- **`test_count_detections_returns_structured_buckets`** — pre-seed
  mixed events → result has `total`, `by_camera`, `by_label`,
  `by_camera_label` matching the seeded shape.
- **`test_concurrent_user_calls_isolate_roles`** — two coroutines call
  `latest_clips` with different `_user_roles` simultaneously, await
  both → each sees the role-correct slice.

Additionally, in `tests/unit/test_ws_protocol.py`:

- **`test_event_data_required_role_admin_blocks_user`** — a
  `camera.event.detected` event with `data["required_role"]="admin"`
  is filtered out for a user-level connection.
- **`test_event_data_required_role_falls_back_to_prefix_when_missing`**
  — same event without the data key falls through to prefix-based
  resolution.
- **`test_event_data_required_role_unknown_value_falls_back`** —
  `data["required_role"]="dragon"` (unknown role) falls back to the
  prefix table rather than crashing or letting the event through.

### 13.2 Plugin tests (`std-plugins/frigate/tests/`)

- **`test_event_normalization.py`** — feed Frigate-shaped JSON dicts
  through the parser, assert each yields the right CameraEvent:
  - `test_new_event_yields_active`,
  - `test_update_event_yields_active_when_score_changes`,
  - `test_update_event_dropped_when_no_change`,
  - `test_end_event_yields_ended`,
  - `test_sub_label_string_form` — `"jeff"` → `sub_label="jeff"`.
  - `test_sub_label_list_form` — `["jeff", 0.93]` → `sub_label="jeff"`.
  - `test_sub_label_null_form` — `null` → `sub_label=""`.
  - `test_missing_end_time_handled` — no `end_time` field → `ended_at=0`.
  - `test_false_positive_dropped` — `false_positive=true` → no event.
  - `test_missing_required_field_dropped` — no `after.id` → drop with debug log.
  - `test_invalid_json_payload_dropped` — `_on_message` receives non-
    JSON bytes → logged at WARNING, queue empty, no exception.
  - `test_audio_event_no_snapshot` — `label="bark"` with
    `has_snapshot=false` → event flows, annotation short-circuits.
- **`test_frigate_backend.py`** — mock the MQTT client (don't talk
  to a real broker), feed messages through the dispatch path, assert
  `events()` async iterator yields the expected sequence. Use a
  `MockMQTTClient` fake injected via the constructor; do **not** mock
  `aiomqtt` itself globally — refactor `FrigateMQTT` to take a
  `client_factory` so tests can pass in a fake.
- **`test_frigate_lwt_handling.py`** — fake MQTT delivers
  `<prefix>/available = "offline"` then `"online"` → consumer queue
  receives a `_FrigateAvailability` sentinel for each transition; the
  service translates these into `camera.backend.{disconnected,connected}`.
- **`test_backend_actions.py`** — `test_connection` action with a fake
  HTTP client returning a 200 + version JSON, plus a fake MQTT client
  that connects → returns "ok" with both probes summarized.
- **`test_backend_actions_old_frigate_warning.py`** — version probe
  returns `0.12.0` → result success but message contains the
  warning text.
- **`test_backend_actions_no_mqtt.py`** — same but MQTT connect raises
  → returns "error" with the MQTT failure detail and HTTP success.
- **`test_tls_params_constructed_from_config.py`** — backend
  initialized with `mqtt_tls=true` and `mqtt_tls_ca_cert="..."` →
  `aiomqtt.TLSParameters(ca_certs=...)` is what gets passed to the
  client factory.
- **`test_http_client_verify_ssl_wired.py`** — `verify_ssl=false` →
  `httpx.AsyncClient(verify=False)`.
- **`test_http_client_auth_modes.py`** — `http_auth_mode="none"` → no
  `Authorization` header. `http_auth_mode="bearer"` with `http_token`
  → header set.

### 13.3 Greeting integration

Existing `tests/unit/test_greeting_service.py` gets new cases:

- **`test_announces_on_camera_package_event`** — config sets
  `announce_camera_labels: ["package"]` → publishing a fake
  `camera.event.detected` with label `package` triggers
  `speaker.announce`.
- **`test_dedups_repeat_camera_event`** — two events same camera+label
  within `camera_announce_dedup_seconds` → one announce.
- **`test_does_not_announce_label_not_in_list`** — label `person` → no
  announce when not in `announce_camera_labels`.
- **`test_announce_dedups_across_camera_zone_group`** — config sets
  `camera_zone_groups: {front_entry: [driveway, front_porch,
  front_door]}` and dedup key `["label", "zone_group"]` for `person`
  → three person events on the three cameras within the dedup
  window produce **one** announcement.
- **`test_announce_dedups_package_label_only`** — default config →
  package events on driveway and front_porch within the window
  produce one announcement (label-only dedup key).
- **`test_per_label_prompt_override_used_when_set`** —
  `camera_announce_per_label_prompts: {glass_break: "..."}` →
  glass_break events use the override prompt; person events use
  the base prompt.
- **`test_mute_camera_alerts_suppresses_announce`** — call
  `mute_camera_alerts(camera="side_gate", until="08:00 tomorrow")`
  → person event on side_gate within the window is **not**
  announced; the bus event still fires. After the until-window,
  announcements resume.

## 14. Failure modes / edge cases

- **MQTT broker unreachable at boot.** `aiomqtt.Client.__aenter__`
  raises `MqttError`. The plugin wraps it as `CameraBackendError`
  and exits the streaming session; the service's stream consumer
  catches the exception, publishes `camera.backend.disconnected`,
  sleeps with backoff, calls `backend.connect()` again. Service
  itself doesn't fail-start — the rest of Gilbert continues to run.
- **Frigate broker reports `<prefix>/available = "offline"`.** Even
  while the MQTT transport is healthy, Frigate-the-detector might be
  down. The plugin treats this as a backend disconnect: publishes
  `camera.backend.disconnected` with `error="frigate offline"`.
  When the LWT transitions back to `"online"`, publishes
  `camera.backend.connected`. Subscribers can show the right status
  on the dashboard.
- **TLS handshake failure (cert verify, missing CA, expired cert).**
  Surfaces as `MqttError` from `aiomqtt`; same path as broker-down.
  Operators see the error in the `camera.backend.disconnected`
  event payload. `mqtt_tls_insecure=true` skips verification.
- **Snapshot bytes exceed the 1 MB cap.** `get_snapshot` returns
  `is_error=True` rather than producing oversized inline
  attachments. The `?h=720` server-side downscale should keep
  almost all JPEGs under the cap; the cap exists for the
  pathological case (4K cameras with bad JPEG quality settings).
- **HTTP base URL wrong but MQTT works.** Snapshots / clips fail; events
  still flow. `get_snapshot` returns the URL but byte-fetch fails;
  Vision annotation logs a warning and skips. The event's `vision_text`
  stays empty; the event row still exists, the AI tools still return
  it.
- **Frigate sends `false_positive: true` mid-event.** Drop the event
  entirely. If we already published a `new` for that event id, optionally
  publish a `camera.event.dismissed` event so subscribers can clean up
  — *deferred to v2; v1 just stops emitting for that id*.
- **MQTT message backlog (1000+ queued events).** Drop new events with
  a logged warning. The Frigate spec says reasonable home installations
  see < 1 event/sec; if that's wrong we'll widen the queue or add a
  spillover-to-disk path.
- **Two backends register the same `backend_name`.** Backend registry
  is last-write-wins on import order, identical to other backends.
- **Storage write fails mid-event.** Persisted event is missing; bus
  event still fires. Audit-style consumers must accept that the
  collection is best-effort.
- **Vision is disabled for everything.** No task spawned, no extra
  cost.
- **Camera renamed in Frigate.** `list_cameras` returns the new name.
  Old events keyed by the old camera name remain in `camera_events`
  until retention sweep; the role override for the old name silently
  no-ops. Operators who care can purge manually.
- **Per-camera role override for a camera that no longer exists.**
  Ignored. No-op. Logged at debug level.
- **Plugin deps not installed (fresh install).** `aiomqtt` import fails
  inside `backend.py`; `__init_subclass__` never runs; service finds
  no `frigate` backend in registry; logs the warning from §6.3 step
  "Unknown camera backend." Runs disabled. Standard plugin runtime-install
  flow already handles the deps-then-restart dance via
  `needs_restart=True`.

## 15. Things to consciously NOT do

- **Don't subscribe to `<prefix>/<camera>/<label>/snapshot` MQTT
  binary topics.** Tempting (free thumbnails) but it doubles our
  in-memory state and Frigate already serves snapshots cheaply on
  HTTP. Re-evaluate in v2.
- **Don't add a `camera.<label>.ended.<camera>` glob event.** The
  use cases don't motivate it; we'd just be paying double the publish
  cost.
- **Don't store the `raw` payload.** §7 explicit.
- **Don't put MQTT credentials anywhere outside the backend's
  `settings.*` block.** `sensitive=True` flagging means the Settings
  UI redacts them; entity storage is the only persistence target.
- **Don't import from `gilbert.integrations.*` in the plugin.** Plugins
  are forbidden from doing this — see Layer Dependency Rules in
  `CLAUDE.md`. The plugin uses only `gilbert.interfaces.*` plus its
  own modules.
- **Don't emit `camera.<label>.detected.<camera>` if `<label>` or
  `<camera>` contains a dot or whitespace.** Sanitize or skip the
  glob emission. Frigate label / camera names are constrained to
  identifiers in practice but assert this defensively.
- **Don't call `presence_svc.who_is_here()` synchronously in
  `_handle_event`.** The stream consumer is the throughput-critical
  path. The camera service no longer correlates with presence at all
  (see §3.2 — `who_was_at` was dropped); if a future LLM-correlated
  `identify_visitors` v2 tool is added, it must be on the AI-tool
  path, not the stream path.
- **Don't add a Gilbert-side MQTT LWT (`gilbert/cameras/available`).**
  v1 is read-only on the broker; cross-host status is a v2 concern.
  See §8.5.
- **Don't subscribe twice to both `camera.event.detected` and
  `camera.<label>.detected.<camera>` for the same logical
  detection.** Subscribers MUST pick one or the other; subscribing
  to both yields duplicate handler invocations.

## 16. Documentation updates

- `README.md` (root): bump the integration table to mention "Frigate
  cameras" alongside the existing UniFi entries; reference
  `std-plugins/frigate/`.
- `std-plugins/README.md`: add a row to the plugin table and a full
  detail section for `frigate` matching the format used for `unifi`
  (provides, deps, config keys, slash commands, Settings location).
- `std-plugins/CLAUDE.md`: no change unless plugin-development
  conventions need updating (this plugin should fit the existing
  conventions).
- `.claude/memory/MEMORIES.md`: add an entry
  `[Camera Event Service](memory-camera-events.md) — generic
  CameraEventBackend interface, Frigate MQTT plugin, vision-annotated
  events, glob-friendly bus topics`.
- `.claude/memory/memory-camera-events.md`: new file — summary,
  details (interface shape, service responsibilities, event
  vocabulary, MQTT specifics, vision integration), related links.
- `.claude/memory/memory-doorbell-service.md`: add a "See also" line
  pointing at `memory-camera-events.md` so future readers find the
  sibling event-stream service.
- `.claude/memory/memory-greeting-service.md` (if it exists; if not,
  no new memory needed for the greeting tweak — it's a small
  additional event subscription documented in `memory-camera-events.md`).

## 17. Implementation checklist (for the implementing agent)

In order. Steps 1a–1c are **precursor changes** that must land first
because the camera service depends on them — they're cross-cutting
features, not "already in place" as the prior draft assumed.

### 17.0 Precursors (cross-cutting changes)

1a. **Per-event ACL data-level override.**
    - Add `resolve_event_visibility(event_type, data)` to
      `interfaces/acl.py`. Document `data["required_role"]` as a
      reserved key.
    - Update `web/ws_protocol.py:can_see_event` to accept the full
      `Event` (or `event_type` + `data`), call the new helper.
    - Update the publish path to pass `event.data` through.
    - Tests: `test_event_data_required_role_admin_blocks_user`,
      `test_event_data_required_role_falls_back_to_prefix_when_missing`,
      `test_event_data_required_role_unknown_value_falls_back`.

1b. **`VisionProvider` capability protocol.**
    - Add `@runtime_checkable Protocol` `VisionProvider` to
      `interfaces/vision.py` with just `describe_image(...)`.
    - Confirm `VisionService` already satisfies it (no surface
      change required).
    - **Do NOT** add `model_name` / `available` properties — the
      camera service doesn't depend on them in v1 (see §6.6).

1c. **`StorageBackend.delete_query` (preferred — see §7.1).**
    - Add `async def delete_query(self, query: Query) -> int` to the
      `StorageBackend` ABC.
    - Implement in the SQLite backend as one parameterized
      `DELETE WHERE`.
    - Tests in `tests/integration/test_storage.py`.
    - **Fallback:** if landing this is too large, the camera
      retention sweep uses the chunked-delete loop documented in
      §7.1 and `delete_query` becomes a follow-up.

### 17.1 Camera service + interface

2. Land `interfaces/camera.py` with the full ABC + dataclasses
   (`CameraEvent`, `CameraInfo`, `SnapshotRef`, `CameraEventPhase`,
   `CameraBackendError`) + protocols (`CameraProvider`,
   `AvailableCameraLister`).
3. Land `core/services/camera.py` (config, lifecycle, persistence, no
   tools yet) with the in-memory `_FakeCameraBackend` for tests
   (with the per-test registry-reset fixture from §13.1).
4. Land `tests/unit/test_camera_service.py` for the lifecycle /
   persistence / role-override / reconnect / LWT cases. **All green
   before moving on.**
5. Land tool definitions on `CameraEventService` (`list_cameras`,
   `latest_clips`, `get_snapshot`, `who_was_seen`,
   `count_detections`) + tool tests.
6. Update `interfaces/acl.py` with the new event/RPC prefix entries
   (`"camera.": 200`, `"camera.backend.": 0`, `"cameras.": 100`).
7. Wire `CameraEventService` into `app.py` (register + factory).
8. Add `cameras` dynamic choices source to `ConfigurationService`
   and the `AvailableCameraLister` protocol.

### 17.2 HTTP proxy routes

9. Add `GET /api/cameras/events/<event_id>/snapshot.jpg` and
   `GET /api/cameras/events/<event_id>/clip.mp4` to `WebApiService`
   (or wherever the existing media-passthrough patterns live —
   confirm at impl time). Both routes:
   - Resolve the event via the `CameraProvider`.
   - Apply the per-camera role gate.
   - Stream through the backend's `backend_auth_headers()` so the
     browser never sees the raw Frigate token.
   - Snapshot route: 304 on `If-Modified-Since`.
   - Clip route: support `Range` requests for `<video>` seek.
   Tests in `tests/unit/test_web_camera_routes.py`.

### 17.3 Plugin

10. Land the `frigate` plugin: yaml, py, pyproject (with
    `aiomqtt>=2.3.0,<3.0.0`), backend.py (with
    `__init_subclass__(backend_name="frigate")` registration),
    mqtt_client.py (with TLS params + LWT handling + injectable
    `client_factory`), http_client.py (with `verify_ssl` +
    `http_auth_mode` wiring).
11. Run `uv sync` to pull `aiomqtt` into the workspace.
12. Land plugin tests with mock MQTT/HTTP clients per §13.2.

### 17.4 Greeting + UI

13. Extend `GreetingService` with the camera subscription, the new
    config (`camera_zone_groups`,
    `camera_announce_dedup_keys`, `camera_announce_per_label_prompts`),
    the `mute_camera_alerts` AI tool + slash command, and the
    `camera_mutes` collection. Tests per §13.3.
14. Land the plugin's `frontend/` directory: `package.json`,
    `api.ts`, `RecentEventsCard.tsx`, `panels.ts`, `types.ts`.
    Verify `useEventStream("camera.event.detected", ...)` works in
    the existing core SPA hook; if not, add it.

### 17.5 Docs + final

15. Update `README.md` (root integration table), `std-plugins/README.md`
    (new row + detail section), `.claude/memory/MEMORIES.md` (new
    entry), `.claude/memory/memory-camera-events.md` (new file with
    interface shape, MQTT specifics, glob-emission asymmetry rule,
    multi-backend collision rule from §6.2),
    `.claude/memory/memory-doorbell-service.md` (cross-link).
    If `memory-greeting-service.md` exists, update with the new
    config keys; else just document the additions in the camera memory.
16. Run the full test suite and `mypy src/` before opening the PR.
17. Manual smoke: configure with a real Frigate instance, watch the
    bus, verify glob subscription works
    (`bus.subscribe_pattern("camera.person.detected.*", ...)`),
    verify TLS broker connection, verify LWT triggers
    `camera.backend.{disconnected,connected}` on Frigate restart,
    verify proxy routes work over a tunnel.

**No integration test in CI.** The camera plugin's only integration
points are MQTT broker + Frigate HTTP API, both heavyweight to
spin up in CI. Manual smoke (step 17) covers integration; a future
docker-compose fixture is a follow-up.

## 18. Open questions

The following are genuinely undecided and worth resolving with the
human before implementation locks them in.

1. **`identify_visitors` (LLM-correlated) for v2.** v1 ships the
   deterministic `who_was_seen` (face matches only) plus
   `latest_clips` (vision prose, AI reads it directly). A v2
   `identify_visitors` tool that uses the LLM to correlate
   `vision_text` + `sub_label` + presence is honest *only if* its
   prompt explicitly requires listing unknowns ("4 unidentified
   person events" alongside "Jeff (1 event)") and the AI's reply
   guidance instructs it to mention unknowns explicitly. Decide:
   ship `identify_visitors` in v2 with strict unknown-surfacing, or
   never re-add it (the deterministic `who_was_seen` + AI's own
   judgment over `latest_clips` may be sufficient).

2. **`VisionProvider.model_name` — wider PR or skip?** The spec
   chose the cheap path (no `model_name` property; payload omits
   `vision_model`). The wider path adds `model_name: str` to
   `VisionBackend` + every existing implementation
   (`local_vision`, `anthropic_vision`, etc.) + `VisionService`.
   Confirm the cheap path is acceptable for v1, or schedule the
   wider PR ahead of camera v1.

3. **MQTT broker setup friction.** Most home users will run
   Frigate's bundled mosquitto or a shared broker. The plugin
   docs (root README + std-plugins README + the `frigate`
   detail section) need a "if you don't already have a broker,
   point this at Frigate's `mqtt:` block in `config.yml` — the
   bundled broker is what Frigate publishes to" sentence
   prominently. Confirm with the human that this is the
   intended onboarding hint vs. recommending a separate broker.

4. **`vision_text` retention vs. event-row retention.** The
   `vision_text_retention_days` knob (§6.7) defaults to `0` (no
   separate scrub — vision_text expires with the row). Some users
   will want shorter retention specifically for the
   AI-generated prose ("a man in a blue jacket carrying a
   brown box" describing actual humans is more sensitive than
   the bare detection metadata). Confirm the default — `0` (off)
   vs. `7` (matches the global default but as a separate field).

5. **Snapshot bytes — inline vs. workspace-reference.** v1 uses
   inline base64 in the conversation row, capped at 1 MB raw and
   pre-scaled to 720p server-side. The conversation row carries
   the bytes forever, but bounded size + workspace-skill-free
   simplicity favor v1. v2 may switch `get_snapshot` to a
   workspace-reference attachment (bytes on disk, conversation
   row stays small). Decide whether to schedule the v2 follow-up
   now or defer.

6. **`mute_camera_alerts` confirmation.** The spec ships a
   `UIBlock` confirmation by default (matching calendar-mutation
   tone). For volume-style mutes ("everyone says no, fire and
   forget"), the confirm is friction. Decide: confirm-by-default
   (current spec) vs. fire-and-forget with an `undo` slash
   command.

### Closed (decided)

- ~~`bus.subscribe_pattern("camera.*.detected.*", ...)` — does
  fnmatch handle the dotted globs correctly?~~ Yes;
  `fnmatch` `*` matches literal `.` characters, verified by the
  `core/services/proposals.py:475` consumers using similar
  shapes. Test:
  `test_pattern_subscription_matches_camera_detected_glob`.
- ~~Snapshot proxy route — extend existing or new?~~ New routes
  in `WebApiService` per §3.4; existing media-passthrough is
  document/screen-specific and not generic.
- ~~`SnapshotRef`-on-disk caching for re-annotation.~~ Skipped
  for v1; the existing-row check in `_annotate_event` already
  prevents re-fetch for the same event id. A small LRU is a
  one-line follow-up if profiling shows it matters.
- ~~Frigate `face` event `sub_label[1]` confidence as a separate
  `CameraEvent` field?~~ Skipped for v1; the event-level `score`
  already covers detection confidence, and the face-match
  confidence flowing through `sub_label_confidence` would be a
  separate dataclass field nobody currently reads. Re-evaluate
  if a face-recognition feature requests it.

## 19. Acceptance criteria

### Precursors

- [ ] `interfaces/acl.py` has `resolve_event_visibility(event_type,
  data)` honoring `data["required_role"]`; `web/ws_protocol.py`
  uses it; tests pass for admin-block / fall-back / unknown-value
  cases.
- [ ] `interfaces/vision.py` declares `VisionProvider` protocol with
  `describe_image` only; `VisionService` satisfies it (no surface
  change required).
- [ ] `StorageBackend.delete_query` lands (preferred) OR the camera
  retention sweep ships with the chunked-delete fallback (§7.1).

### Camera service + interface

- [ ] `interfaces/camera.py` lands with `CameraEventBackend`,
  `CameraEvent`, `CameraInfo`, `SnapshotRef`, `CameraEventPhase`,
  `CameraBackendError`, `CameraProvider`, `AvailableCameraLister`.
  `stream_events` is documented as an async-generator-function
  (`async def ... yield ...`). `__init_subclass__` warns on
  duplicate `backend_name` registration.
- [ ] `core/services/camera.py` lands with full lifecycle,
  persistence (no `started_iso`/`ended_iso`/`vision_model` in the
  row, proxied URLs), role-gated event publishing (with
  `data["required_role"]`), vision annotation (semaphore-bounded,
  per-event-id locks, persist-then-spawn ordering), and the AI tool
  surface (`list_cameras`, `latest_clips`, `get_snapshot`,
  `who_was_seen`, `count_detections`).
- [ ] `acl.py` has the new prefix entries
  (`"camera."`, `"camera.backend."`, `"cameras."`).

### HTTP proxy routes

- [ ] `GET /api/cameras/events/<id>/snapshot.jpg` and
  `/clip.mp4` routes ship in `WebApiService` with role gating,
  Range support (clip), and `backend_auth_headers()` injection.

### Plugin

- [ ] `std-plugins/frigate/` ships a working `FrigateCameraBackend`
  speaking MQTT (with TLS / mTLS / SNI / insecure-mode toggles
  wired through) and HTTP (with `verify_ssl` and `http_auth_mode`
  wired through) to a real Frigate instance (manual smoke).
- [ ] Frigate LWT (`<prefix>/available`) translates into
  `camera.backend.{disconnected,connected}` bus events.
- [ ] `aiomqtt` pinned `>=2.3.0,<3.0.0`.
- [ ] Single-layer reconnect: plugin exits on `MqttError`, service
  retries via `connect()`.
- [ ] Defensive Frigate event parsing: missing fields drop the
  event, sub_label list/string/null forms all handled, audio events
  flow through transparently.

### Greeting + UI

- [ ] `GreetingService` announces events with composite dedup keys
  (`camera_zone_groups`, per-label dedup-key shape) so a single
  visitor doesn't fire 2–3 announcements across adjacent cameras.
- [ ] `mute_camera_alerts` AI tool + slash command + `UIBlock`
  confirmation lands.
- [ ] Per-label announce prompt overrides supported.
- [ ] `RecentEventsCard` dashboard panel uses the existing
  `useEventStream` hook + the new proxy routes for thumbnails.

### Cross-cutting

- [ ] Bus event surface matches §3.1; glob subscription works
  (test `test_pattern_subscription_matches_camera_detected_glob`).
- [ ] All AI tools and WS RPCs apply per-camera role filtering;
  privilege-escalation surface tests pass.
- [ ] Documentation updated (root README, std-plugins README, memory
  index + new `memory-camera-events.md`).
- [ ] All new and existing tests pass; `mypy src/` clean.
- [ ] No layer-rule violations: plugin imports only
  `gilbert.interfaces.*`; core service imports only `interfaces` +
  capability protocols; no concrete-class `isinstance` checks.

## Revision Log — Round 2

Three independent reviews (architect / product / engineering)
produced a converging set of blockers and important issues. This
revision addresses every blocker, every important item, and the
nits worth fixing for clarity. Notable changes:

### Architecture

- **§3.1, §10.2** — explicit specification of the per-event ACL
  data-level override. The prior draft assumed the WS layer
  "already understands" `event.data["required_role"]`; it does
  not. Added `resolve_event_visibility(event_type, data)` to
  `interfaces/acl.py`, the `web/ws_protocol.py` filter update,
  and three unit tests as a precursor change.
- **§5** — `stream_events` typed as `async def stream_events(...)
  -> AsyncIterator[CameraEvent]: yield ...` (async generator
  function). Lifecycle docstring calls out that
  `connect/disconnect/stream_events` is a **streaming-backend
  variant** on top of the standard `initialize/close`, so future
  polling-style backends know to wrap a queue.
- **§5** — `__init_subclass__` warns on duplicate
  `backend_name` registration; tests use a per-test registry-reset
  fixture so fakes don't leak.
- **§5, §7** — `started_iso`/`ended_iso`/`vision_model` dropped
  from the persisted shape (derived on read or removed entirely).
- **§5** — `raw` typed as `Mapping[str, object]` to make the
  read-only intent explicit on a frozen dataclass.
- **§6.2** — multi-backend camera-name collision rule
  documented (use `f"{source_backend}:{camera_name}"`).
- **§6.3, §6.5, §11** — consistent
  `asyncio.create_task(coro, name=..., context=copy_context())`
  for both stream consumer and annotation tasks. Resolves the
  contradiction between §6.5 (`create_task` no context) and §11
  (claimed copy_context).
- **§6.6** — `VisionProvider` protocol kept minimal
  (`describe_image` only); `model_name`/`available` properties
  not added to avoid touching every existing `VisionBackend`
  implementation in this PR. Camera event payload omits
  `vision_model` accordingly.

### Product

- **§3.2** — `who_was_at` (LLM-correlated) **dropped**. Its
  three-signal merge (face matches + vision prose + presence)
  produced confidently wrong identifications. Replaced by
  deterministic `who_was_seen` (face matches only, with
  `unknown_count` so the AI can't silently drop strangers) and
  structured `count_detections` (composes with `latest_clips`
  for follow-ups, and with calendar/inbox/weather for cross-
  feature daily briefs).
- **§3.2** — `recent_detections_summary` (opaque prose)
  replaced by `count_detections` (structured buckets the AI
  composes prose from).
- **§3.2** — slash commands cleaned up: `get_snapshot` is
  AI-only (opaque event_id makes the slash command unusable);
  `who_was_seen` and `count_detections` keep slash commands
  because their args are typeable.
- **§3.2** — `since`/`until` grammar enumerated explicitly so
  the AI doesn't try unsupported phrasings.
- **§3.2** — `get_snapshot` cap at 1 MB raw with `?h=720`
  server-side downscale; explicit failure on snapshot-expired
  (404) and oversized cases. Confirmed inline image attachments
  render as `<img>` (not download chip).
- **§6.7** — `vision_prompt` default rewritten: terse,
  observational, explicit prohibition on identity/intent
  speculation. The prior "one paragraph" default produced
  speculation that poisoned downstream tools.
- **§6.7** — `vision_enabled_labels` default narrowed from
  `["person", "package"]` to `["package"]` (person events fire
  too often on outdoor cameras to auto-annotate by default).
- **§6.7** — ambiguous `vision_enabled_cameras` replaced with
  `vision_per_camera: dict[str, list[str]]` with a
  self-documenting truth table.
- **§6.7** — `default_camera_role` knob added; `role_overrides`
  documented as a per-camera grid in the Settings UI rather
  than a JSON blob.
- **§10.3** — multi-camera announcement dedup fixed:
  `camera_zone_groups` + per-label `camera_announce_dedup_keys`
  ensure one visitor walking up the path = one announcement,
  not three.
- **§10.3** — per-label announce prompt overrides
  (`camera_announce_per_label_prompts`) added so package /
  person / glass_break can have appropriately different tones.
- **§10.3.1** — `mute_camera_alerts` AI tool + slash command +
  `UIBlock` confirmation added.

### Engineering

- **§3.4** — Gilbert-proxied media routes specified explicitly:
  `/api/cameras/events/<id>/snapshot.jpg` and `clip.mp4` with
  Range support, role gating, `backend_auth_headers()` injection.
  `clip_url` / `snapshot_url` in event payloads carry the
  proxied path; raw Frigate URLs available on
  `direct_clip_url` / `direct_snapshot_url` for LAN-only
  consumers.
- **§7.1** — `StorageBackend.delete_query` precursor change
  specified, with a chunked-delete fallback if the precursor PR
  doesn't land in time.
- **§7.2** — schema-tolerance contract (defensive `dict.get()`
  reads, drop-on-missing-required-fields) and minimum
  Frigate-version probe documented.
- **§8.3** — `aiomqtt` pinned `>=2.3.0,<3.0.0` to avoid v3
  breakage; maintenance-status / fallback-path note added.
- **§8.4** — TLS configurability filled in:
  `mqtt_tls_ca_cert`, `mqtt_tls_client_cert`,
  `mqtt_tls_client_key`, `mqtt_tls_insecure`,
  `mqtt_tls_server_hostname`. `http_auth_mode` enum and
  `verify_ssl` wired through.
- **§8.5** — single-layer reconnect: plugin exits on `MqttError`,
  service retries via `connect()`. Resolves the §6.4-vs-§8.5
  contradiction. Frigate LWT (`<prefix>/available`)
  handling specified.
- **§8.5** — `client_factory` injectable for tests so
  `aiomqtt.Client` itself isn't globally mocked.
- **§8.6** — defensive `sub_label` parsing covering
  list/string/null forms; audio-event passthrough documented.
- **§8.7** — HTTP client documented to wire `verify_ssl`,
  `http_auth_mode`, and `?h=720` for snapshot fetches.
- **§8.8** — frontend uses existing `useEventStream` hook (not
  ad-hoc `useWebSocket` subscription).
- **§13** — test plan expanded: schema-tolerance tests,
  TLS-params tests, LWT-handling tests, batched-delete tests,
  privilege-escalation tests, audio-event tests,
  invalid-JSON-payload tests.
- **§17** — implementation checklist re-ordered with explicit
  precursor section (ACL data-level override,
  `VisionProvider` protocol, `StorageBackend.delete_query`)
  before the camera service itself.

### Open questions resolved or refocused

- **fnmatch pattern subscription** — closed; `fnmatch` `*`
  matches literal `.` characters. Test ships in v1.
- **Snapshot proxy route** — closed; new routes in
  `WebApiService` per §3.4.
- **`SnapshotRef` on-disk caching** — deferred; existing-row
  check prevents re-fetch for the same event id.
- **`VisionProvider.model_name`** — moved into the spec
  proper as the *cheap* path (no model_name in v1); §18 Q2
  asks the human to confirm vs. landing the wider PR ahead.
- New §18 questions added for `identify_visitors` v2,
  `vision_text` retention, snapshot inline-vs-workspace,
  mute confirm-vs-fire-and-forget.

