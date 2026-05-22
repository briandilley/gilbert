# External Notification Fan-out (Push) — Design

**Status:** Draft for implementation planning (Round-2 revision)
**Date:** 2026-05-09
**Feature:** 03 (External notification fan-out)

> **Round-2 note:** This revision addresses the engineering review's blocker
> (fan-out previously ran inline on `EventBus.publish` and back-pressured
> in-app delivery), names the delivery guarantee explicitly, fixes the
> `contextvars` mistake, removes the unjustified in-memory dedup map,
> tightens secret handling, hard-codes a worker-pool dispatcher, and
> re-orders implementation to ship as two PRs (ntfy-first). See the
> **Revision Log — Round 2** at the bottom for a per-item map.

## Overview

Gilbert today persists user-addressed notifications in `NotificationService` and
delivers them live to that user's WebSocket connections via a per-event content
filter (`WsConnection.can_see_notification_event`). That works perfectly when
the user is sitting at a Gilbert browser tab. It does nothing when they aren't.

This feature adds an **external delivery side-channel**: the same
`notification.received` events also get fanned out, on a per-user basis, to
external messaging providers (ntfy, Pushover, Discord webhook, Telegram bot).
Each user configures their own personal "notification routes" with simple
filters (urgency floor, source filter, quiet hours). Admins configure plugin
backend credentials once; each user picks which routes apply to them.

This is a side-channel, not a replacement. `NotificationService` is **not
modified** — it keeps persisting notifications and publishing the bus event,
exactly as today. Web/WS delivery continues to work exactly as today. The new
service subscribes to the bus event and dispatches in addition.

## Decisions Summary

The shape of the system is set by these decisions made during design:

1. **Event-driven fan-out, not direct method call.** A new
   `PushNotificationService` subscribes to `notification.received` on the
   event bus. `NotificationService` is unchanged — it does not know that
   anyone listens, which preserves its single-purpose contract and lets
   the fan-out service start, stop, and crash without touching the
   persistence path. (Rationale and the rejected alternative below.)
2. **The bus subscriber returns instantly; delivery runs on a worker
   pool.** `InMemoryEventBus.publish` `await`s every subscriber inline
   (`asyncio.gather`), so any work the fan-out subscriber does blocks
   the publisher (and through it, the in-app WS dispatcher). The
   subscriber therefore does **only**: (a) sanity-check the event,
   (b) snapshot the originating context (`copy_context()`), (c) enqueue
   one `_FanOutJob` per event onto a bounded `asyncio.Queue`. A pool
   of N background workers (default `8`) drains the queue and runs the
   filter + send pipeline. See the new "Dispatch architecture" section
   below.
3. **At-most-once delivery in v1, with an explicit promotion path to
   at-least-once in v1.1.** v1 has no durable outbox: a process crash
   between `event_bus.publish` and worker completion drops the in-flight
   push. The in-app notification is unaffected. v1.1 (mandatory before
   "URGENT external delivery" can be marketed) adds the
   `push_notification_deliveries` outbox; the worker writes
   `pending → delivered|failed` rows and on start re-enqueues anything
   left `pending`. See "Delivery guarantees".
4. **One aggregator service, N backend plugins.** Following the existing
   multi-backend aggregator pattern (TTS, web search), one
   `PushNotificationService` holds a registry of `PushNotificationBackend`
   instances keyed by `backend_name`. New providers ship as std-plugins
   that subclass `PushNotificationBackend`.
5. **New backend ABC with its own registry.**
   `PushNotificationBackend` lives in `interfaces/push_notifications.py`
   and follows the universal backend pattern (ABC + `__init_subclass__`
   registry + `backend_config_params()` + `backend_actions()`). It is a
   distinct ABC from any future "TTS-style" backend; the name does not
   collide with `NotificationProvider` (the existing capability protocol)
   because the new ABC is `PushNotificationBackend`, not
   `PushNotificationProvider`. `backend_config_params()` and
   `backend_actions()` are **classmethods** (consumed by the UI before
   any instance exists); `invoke_backend_action()` is an **instance
   method** (consumed after `initialize`, has access to `self._client`).
6. **Per-user routes are owner-scoped storage.** A new entity collection
   `push_notification_routes` holds `PushRoute` records, each owned by a
   user, naming a backend, holding the per-user `destination_data`
   (channel id, ntfy topic, Telegram chat id, …) and the filter rules
   (urgency floor, source allowlist, quiet hours). Users see and edit
   only their own; admins see all via the entities page.
   `acl_collections` is the source of truth for "admin sees all"; WS
   RPCs are a defense-in-depth backstop that delegate to a single
   helper.
7. **Backend-level secrets are admin-only; per-user destinations are
   user-level.** Each plugin's `backend_config_params()` declares the
   shared admin secrets (Pushover app token, Telegram bot token,
   ntfy.sh server URL fallback, etc.) on the Settings page. The
   per-user `destination_data` (Pushover user key, Discord webhook URL,
   Telegram chat id, ntfy topic) lives on the user's `PushRoute` and is
   editable by that user from `/account/notifications`.
8. **Test connection at two levels, with safety rails.** Each backend's
   `backend_actions()` exposes a service-level `test_connection` that
   admins click on the Settings page. Each per-user route also has a
   route-level "Send test message". Per-route tests are
   server-side-debounced (one test per route per 30s) to keep accidental
   double-clicks from flooding shared channels. Discord tests use
   `flags=4096` (suppress notifications) so the channel does not ping.
9. **Retry with jitter, provider-aware backoff, URGENT failures escalated.**
   Bounded retry up to `max_retries=3` (capped server-side at 8) with
   exponential backoff plus uniform jitter. Backends may return
   `PushDeliveryResult.retry_after_s` (parsed from `Retry-After` /
   Telegram `parameters.retry_after` / Discord `X-RateLimit-Reset-After`)
   and the worker prefers it over the configured backoff.
   Retry-exhausted URGENT notifications log at **ERROR** and emit an
   in-app `notification.received` of urgency=URGENT to the operator role
   with `source="push_failure"`. NORMAL/INFO exhaustion logs at
   WARNING. Successful sends log at INFO.
10. **No in-process dedup map.** v1 has no `_delivered` cache. The retry
    layer is a single sequential task per `(notification_id, route_id)`,
    so same-tick duplicates are structurally impossible; cross-tick
    duplicates would require the bus to redeliver the same event
    (`InMemoryEventBus` doesn't), or two processes to share a database
    (out of scope). When the v1.1 outbox lands the row's primary key is
    the dedup token — no separate `push_dedup` collection needed.
11. **Quiet hours are user-tz-aware with explicit fallback order.**
    Route's `quiet_hours_timezone` (IANA) → user profile timezone →
    server tz, with a single WARN logged when fall-through hits the
    server tz. Bounds are persisted as `Optional[str]` (`None` = off);
    no empty-string-as-falsy. The `_in_quiet_hours` helper compares
    wall-clock times in the resolved tz so DST transitions don't skip
    or double-count an hour.
12. **Presence-based gating is v2.** "Only fan out if user is offline"
    would consult `PresenceProvider` and check active WebSocket
    connections; we leave the hook (`should_deliver_route()` extension
    point) but ship v1 as "always deliver, filtered by route rules."
13. **Two-PR rollout.** PR-1 ships interface + service + worker pool +
    ntfy plugin + Routes UI + AI tools + bell-dropdown entry. PR-2 adds
    pushover, discord-webhook, and telegram (with the chat-id wizard).
    See "Implementation order" for the per-PR file list.

## Why event-driven, not a `dispatch` method on `NotificationService`

The user-asked question is: should `NotificationService` call into a new
`PushDeliveryProvider` capability inside its own `notify_user` method, or
should the new service subscribe to the existing bus event?

**Recommended (and adopted): subscribe to `notification.received`.**

- `NotificationService` is one of the simplest, oldest services in the
  tree. Adding an outgoing capability call inside its hot path couples
  it to push delivery, makes "skip persistence on push failure" a real
  question, and means every test of `notify_user` has to mock another
  service.
- The bus event is *already published* and contains the full
  serialized notification. Subscribing is purely additive: zero edits
  to `NotificationService`, zero new capabilities required.
- The fan-out worker can crash, hang, or be slow without affecting
  in-app delivery (which goes through the same event but a different
  subscriber chain — `WsConnectionManager._dispatch_event`).
- Adding a third subscriber later (audit log, analytics, mobile push
  via FCM) is a new service file, no edits to `NotificationService`.

**Rejected alternative: optional `PushDeliveryProvider` capability called from
`NotificationService.notify_user`.** It would force every test of the existing
service to either implement or mock the new capability, would entangle the
persistence happy-path with external HTTP calls, and would make ordering
guarantees harder ("did push fire before persistence?"). The bus already
exists; use it.

## Dispatch architecture (worker pool, NOT inline gather)

`InMemoryEventBus.publish` (`src/gilbert/core/events.py:37-50`) is:

```python
results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
```

— it `await`s every subscriber inline. **A naive subscriber that does I/O
or `asyncio.gather`s a per-route fan-out blocks the publisher**, which
means it also blocks `WsConnectionManager._dispatch_event` (another
subscriber on the same bus) and any caller of
`NotificationService.notify_user` (agents, the scheduler, tools). The
event-driven framing only buys us isolation if the subscriber returns
immediately.

The fan-out service therefore separates the bus subscriber from delivery:

```
EventBus.publish
   └── _on_notification(event)        # runs on the publisher's task
         ├── ctx = copy_context()     # snapshot CALLER's contextvars NOW
         ├── job = _FanOutJob(event.data, ctx)
         ├── self._queue.put_nowait(job)   # never await; bounded queue
         └── return                  # publisher unblocks immediately

[ N background workers, started in `start()` ]
   while True:
       job = await self._queue.get()
       await self._fan_out(job)      # filter + per-route deliveries
```

Concretely:

- `self._queue: asyncio.Queue[_FanOutJob]` with `maxsize=_queue_max`
  (default `1000`). On overflow `_on_notification` logs a WARNING and
  drops the job — back-pressure is preferable to unbounded memory growth
  when a backend is wedged.
- `self._workers: list[asyncio.Task]` — N worker tasks (default 8,
  configurable). Each `await self._queue.get()` in a loop and runs
  `_fan_out` under the job's saved context (using
  `Context.run`-equivalent semantics by spawning the per-route delivery
  tasks with `context=job.context.copy()` so siblings don't clobber).
- `stop()` cancels the workers, drains the queue (jobs already taken
  finish or are cancelled), and closes backend HTTP clients.
- A failing/wedged backend cannot stall the publisher: the worker is
  what blocks, and the queue is bounded.

This is the **only** correct way to honor the spec's "the fan-out
service can crash, hang, or be slow without affecting in-app delivery"
claim. The Round-1 sketch (`asyncio.gather` inside `_on_notification`)
did not achieve this and is removed.

### Why a queue + worker pool, not just `asyncio.create_task`

`create_task` plus an unbounded fire-and-forget pattern fails three
ways:

1. **No back-pressure.** If `discord-webhook` is rate-limited and every
   delivery sleeps 30s on `Retry-After`, a notification storm spawns
   thousands of tasks holding open HTTP clients.
2. **Worker accounting.** A bounded pool gives `metrics.queue_depth` and
   `metrics.in_flight` for free; a sea of tasks does not.
3. **Cancellation.** On `stop()` we want to cancel all in-flight
   deliveries deterministically; that's `task.cancel()` per worker, not
   "find every fire-and-forget task we've ever spawned."

### ContextVars rules (corrected)

`asyncio.create_task` and `asyncio.Task` **do** copy the current context
by default — the risk isn't loss, it's *shared mutation* (siblings
mutate the same Context object). The Round-1 spec had this backwards
in prose; the correction:

- `_on_notification` runs on the bus publisher's task, but the
  publisher's task already inherits the originating
  `notify_user` caller's context (because `await event_bus.publish(...)`
  preserves it). So `ctx = contextvars.copy_context()` captured inside
  `_on_notification` *is* the caller's context. Capture it there, store
  it on the `_FanOutJob`, and **do not** capture again inside per-route
  loops.
- Each per-route delivery task is spawned with
  `context=job.context.copy()` so two routes can't clobber each other
  if a backend mutates a ContextVar.
- The specific ContextVars we need to preserve are: `_user_id`
  (set by AI service per-tool dispatch), `_agent_id` (set when an
  agent run publishes a notification), and `_request_id` (logging
  correlation). We will write a unit test that sets a sentinel
  ContextVar before calling `notify_user`, processes the queue, and
  asserts a fake backend's `send` ran under that same sentinel.
- If the test ever proves no caller-side ContextVar is meaningful at
  delivery time, the `copy_context()` machinery is dead weight and
  must be removed in the same PR — don't ship it as cargo cult.

## Delivery guarantees

**v1 is at-most-once.** A process crash, `OOM`, or `kill -9` between
`event_bus.publish` and worker completion drops the in-flight push.
The persisted `Notification` row is unaffected — the in-app badge and
the `/notifications` page still show it. URGENT pushes are
particularly sensitive to this gap.

To audit losses in production, the worker stamps
`notifications.<id>.external_delivery_attempted_at` (ISO timestamp) on
the persisted notification row when it begins fan-out. (This is the
**only** field we'll add to the existing `notifications` collection;
it is purely informational, set by the new service via a `put` of the
existing row, and is null on rows that pre-date this feature.)

**v1.1 is at-least-once via a `push_notification_deliveries` outbox.**
This is **mandatory before "URGENT external delivery" is shipped as a
product promise.** Schema:

```python
{
    "_id": "<uuid>",
    "notification_id": "<id>",
    "route_id": "<id>",
    "backend_name": "ntfy",
    "user_id": "<id>",
    "status": "pending" | "delivered" | "failed",
    "attempts": 0,
    "started_at": "<iso>",
    "finished_at": "<iso>" | null,
    "error_message": "" | "<scrubbed>",   # see Security notes
}
```

Workflow:
1. Before dispatching to the worker, the bus subscriber writes one row
   per (filtered) route with `status="pending"`.
2. Worker mutates `attempts` and finally writes
   `status="delivered"|"failed"`.
3. On service start, query `status="pending" AND started_at < now-60s`
   and re-enqueue. `attempts` does not reset; if a row exceeds
   `max_attempts_total = 8`, mark it `failed`.

The outbox row's `(notification_id, route_id)` is the durable dedup
token, replacing the in-memory map proposed in Round 1.

**Cross-process dedup** (multi-instance Gilbert) falls out of the
outbox automatically — both processes see the same row and only one
will succeed at flipping `status="pending" → "in_flight"` via
`StorageBackend.put_if(version=...)` semantics. `push_dedup` collection
proposed in Round 1 is **deleted** from the open-questions list.

## Architecture

Three new pieces of core, plus four std-plugins:

```
src/gilbert/interfaces/push_notifications.py        # NEW — ABC + types
src/gilbert/core/services/push_notifications.py     # NEW — aggregator service
frontend/src/pages/account/NotificationRoutesPage   # NEW — per-user routes UI

std-plugins/ntfy/                                   # NEW
std-plugins/pushover/                               # NEW
std-plugins/discord-webhook/                        # NEW
std-plugins/telegram/                               # NEW
```

**Layering** (per CLAUDE.md rules):

- `interfaces/push_notifications.py` imports `interfaces/configuration` for
  `ConfigParam` / `ConfigAction[Result]` and `interfaces/notifications` for
  `NotificationUrgency`. Nothing else.
- `core/services/push_notifications.py` imports `interfaces/` only and uses
  `core/services/_backend_actions.py` for the standard backend-action
  forwarding helper.
- Every plugin imports only `gilbert.interfaces.*` and its own internal
  modules. No imports from `core/services/`, `integrations/`, `web/`, or
  `storage/`.
- `core/services/notifications.py` is **not modified.** Verify by diffing
  against `main` after the implementation lands.

## The `PushNotificationBackend` ABC

```python
# src/gilbert/interfaces/push_notifications.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.notifications import NotificationUrgency


class PushDeliveryStatus(StrEnum):
    """Outcome of a single delivery attempt to one route."""

    DELIVERED = "delivered"
    """The provider accepted the message (HTTP 2xx, send call returned)."""

    REJECTED = "rejected"
    """The provider explicitly rejected the message (4xx, invalid token).
    Retrying will not help; do not retry."""

    TRANSIENT_ERROR = "transient_error"
    """Network blip, 5xx, timeout. Retry until budget exhausted."""

    DISABLED = "disabled"
    """The route or backend is disabled / not configured. Skipped."""


@dataclass(frozen=True)
class PushDeliveryResult:
    """Result of one ``send`` call."""

    status: PushDeliveryStatus
    message: str = ""
    """Human-legible summary; logged on failure, surfaced in test
    connection toasts. **MUST NOT contain credentials, full URLs, or
    response bodies that may echo the destination.** The status line
    only — e.g. ``"HTTP 200"``, ``"network timeout"``,
    ``"server HTTP 502"``. Backends MUST scrub via
    ``_safe_repr(exc)`` before stuffing exception text here."""

    provider_message_id: str = ""
    """Whatever id the provider returned (Pushover receipt, Telegram
    message_id, etc.) — for future audit/dedup; empty if not
    applicable. MUST NOT include any value that itself embeds a
    secret (e.g. webhook tokens)."""

    retry_after_s: float | None = None
    """Provider-supplied retry hint, in seconds. When set on a
    ``TRANSIENT_ERROR`` result, the service worker uses this value
    instead of its configured backoff for the next attempt — Discord
    ``X-RateLimit-Reset-After``, Telegram ``parameters.retry_after``,
    and any standard ``Retry-After`` header are parsed by the backend
    and surfaced here. Capped service-side at 60s to keep one wedged
    provider from monopolizing a worker."""


@dataclass(frozen=True)
class PushDestination:
    """Per-user destination data passed to ``send``.

    ``data`` carries the backend-specific fields (Pushover user_key,
    Discord webhook_url, Telegram chat_id, ntfy topic + optional server).
    The backend defines what keys it expects via ``destination_params``.

    ``user_id`` is the recipient's Gilbert user id, included so backends
    can log it without parsing the route record. ``route_id`` is the
    PushRoute's id, used by the delivery worker for idempotency and
    error correlation.
    """

    user_id: str
    route_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PushMessage:
    """The payload to deliver. Pre-built by the service from a Notification."""

    title: str
    body: str
    urgency: NotificationUrgency
    source: str
    """Origin tag from the original ``Notification.source`` (e.g.
    ``"agent"``, ``"scheduler"``). Backends that support per-source icons
    or topics can use it."""

    source_ref: dict[str, Any] | None = None
    """Optional structured pointer back to whatever produced the original
    notification. Backends that can attach 'click here' URLs (Pushover,
    Discord, ntfy) may build a deep link from it; the service provides
    a ``deep_link_url`` helper alongside this dataclass."""

    notification_id: str = ""
    """Original notification.id — for idempotency keying and logging."""


class PushNotificationBackend(ABC):
    """Abstract interface for external notification delivery providers.

    Each concrete backend (ntfy, Pushover, Discord webhook, Telegram bot)
    is a small std-plugin that subclasses this ABC, sets ``backend_name``,
    and implements ``send``. Backends auto-register via
    ``__init_subclass__`` on import; ``PushNotificationService`` discovers
    them via the registry.

    Backend-level config (admin-only secrets) goes in
    ``backend_config_params``. Per-user destination data (channel id,
    topic, chat id) is described separately via ``destination_params``
    so the per-user Routes UI can render the right fields.

    **Method binding rules** (do not "simplify" these):

    - ``backend_config_params``, ``destination_params``, and
      ``backend_actions`` are ``@classmethod``. The Settings UI and the
      Routes UI consume them *before* any instance is initialised — they
      describe shape, not state.
    - ``initialize``, ``close``, ``send``, and ``invoke_backend_action``
      are **instance methods**. They run after the service has called
      ``initialize(config)`` and may rely on ``self._client``,
      ``self._auth_token``, etc.

    Confusing the two breaks the service in non-obvious ways: marking
    ``invoke_backend_action`` as ``@classmethod`` would mean
    "Test connection" can't see the live HTTP client; marking
    ``backend_config_params`` as an instance method would force the
    Settings UI to instantiate every backend before rendering the form.
    """

    _registry: dict[str, type["PushNotificationBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            PushNotificationBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["PushNotificationBackend"]]:
        return dict(cls._registry)

    # --- Admin-level config (server-wide) ----------------------------------

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Server-wide config (API tokens, default endpoints).

        Rendered on the admin Settings page under the plugin's category
        with ``backend_param=True``. Sensitive values (tokens, app keys)
        MUST set ``sensitive=True``.
        """
        return []

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        """Action buttons on the admin Settings page (e.g. Test connection)."""
        return []

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return ConfigActionResult(status="error", message=f"Unknown action: {key}")

    # --- Per-user destination shape ---------------------------------------

    @classmethod
    @abstractmethod
    def destination_params(cls) -> list[ConfigParam]:
        """Describe the per-user destination fields a route requires.

        These are rendered on the per-user Notification Routes page. The
        UI builds a form from this list, the user fills it in, and the
        resulting dict becomes ``PushDestination.data`` at delivery time.

        Example for Pushover:
            [ConfigParam(key="user_key", type=STRING, sensitive=True,
                         description="Your Pushover user key (30 chars)."),
             ConfigParam(key="device", type=STRING, default="",
                         description="Optional device name; blank = all.")]
        """

    # --- Lifecycle --------------------------------------------------------

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize the backend with admin config. Called by the service
        on start and on backend-config change."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up (HTTP clients, connection pools)."""

    # --- Delivery --------------------------------------------------------

    @abstractmethod
    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        """Deliver one message. Must not raise on transient errors;
        return ``PushDeliveryResult(status=TRANSIENT_ERROR, message=...)``
        instead. Raising is reserved for programmer errors (bad
        destination shape, etc.) — the service treats raised exceptions
        as ``REJECTED`` and logs them at ``ERROR``.
        """

```

(There is no `supports_attachment` capability hook. The Round-1 spec
included one that was never read; we removed it. If a future backend
truly cannot attach a deep link, the service can detect that the URL
didn't appear in the rendered message and surface the warning at that
time. Don't ship dead surface area.)

Notes:

- `destination_params` is a classmethod with `@abstractmethod` because the per-user
  Routes UI needs to ask "what fields do I need to render for backend X?" before
  any instance exists (mirrors how `backend_config_params` is consumed by the
  Settings UI).
- `PushDeliveryResult.status` distinguishes `REJECTED` (do not retry) from
  `TRANSIENT_ERROR` (retry). Backends are expected to be specific; the
  delivery worker reads this to decide retry behavior.
- `PushMessage.title` and `PushMessage.body` are derived by the service
  from the original `Notification.message` — backends should not have to
  know that the source `Notification` had a single `message` field. See
  the service section for the title/body split rule.

## The `PushRoute` entity and storage

Each user owns zero or more routes. Stored in entity collection
`push_notification_routes`.

```python
# Persisted shape (JSON dict written via StorageBackend)
{
    "_id": "<uuid>",
    "user_id": "<owner-user-id>",
    "label": "Phone via ntfy",                     # user-chosen
    "backend_name": "ntfy",                         # matches PushNotificationBackend.backend_name
    "destination_data": {                           # per-backend, shape from destination_params
        "topic": "gilbert-jeff-phone-x82js",
        "server": "https://ntfy.sh"
    },
    "enabled": true,
    "urgency_floor": "normal",                      # info | normal | urgent
    "source_allow": [],                             # empty = all sources
    "source_deny": [],
    "quiet_hours_start": null,                      # "22:00" or null (off)
    "quiet_hours_end": null,                        # "07:00" or null (off)
    "quiet_hours_timezone": null,                   # IANA tz, or null (use user-profile tz)
    "last_delivered_at": null,                      # iso, in-process best-effort (S3)
    "created_at": "<iso8601>",
    "updated_at": "<iso8601>"
}
```

`_id` (not `id`) is the storage layer's convention — `notifications`
uses a denormalised `id` field on the dict body for legacy reasons; new
collections should use `_id` only. Documented in
`memory-storage-backend.md`. `last_delivered_at` is a best-effort
in-memory write-through: the worker patches the row when a delivery
flips to DELIVERED, and the field powers the per-route "Last delivered"
chip on the Routes UI (S3). It is not relied upon for correctness —
restart loss is acceptable.

Indexes:

- `(user_id, enabled)` — primary lookup at fan-out time.
- `(backend_name,)` — when a backend is uninstalled, the service can
  surface "you have N routes pointing at a missing backend."

Owner-scoping is enforced at the WS RPC layer: every route RPC reads
`conn.user_ctx.user_id`, filters/writes scoped to that id, and rejects
attempts to address another user's routes. Admins reading the collection
through the entities page see all rows; entity-collection ACL is set in
`acl_collections` (read=user, write=user, with admin reading all per the
existing override). The user can only mutate their own.

### Route filter semantics

A notification flows to a route if **all** of these are true:

1. Route `enabled` is `true`.
2. Notification `user_id` matches route `user_id`.
3. `notification.urgency` is at or above `route.urgency_floor` (ordering:
   `info < normal < urgent`).
4. If `route.source_allow` is non-empty, `notification.source` is in it.
5. `notification.source` is not in `route.source_deny`.
6. The current wall-clock time, **resolved in the route's effective
   timezone**, is **not** within `[quiet_hours_start, quiet_hours_end)`.
   If either bound is `None`, quiet hours are off.

**Effective-timezone resolution** (in order, first non-null wins):

1. `route.quiet_hours_timezone` (IANA, e.g. `"America/Los_Angeles"`).
2. `users.<user_id>.timezone` from the user profile (added as a
   prerequisite of this feature — see "Dependencies").
3. The system timezone, with a one-time `WARNING` log per user_id of
   the form `push: route %s for user %s falling back to server tz` so
   operators see when the user-profile TZ is missing.

The service helper `_in_quiet_hours(now_aware_or_naive, start_str,
end_str, tz)`:

1. Coerces `now` to an aware datetime in `tz` (DST-correct via
   `zoneinfo.ZoneInfo` — never via UTC offset arithmetic, which would
   skip an hour in the spring-forward window).
2. Compares wall-clock `(hour, minute)` tuples.
3. Handles wrap-around (start=22:00, end=07:00) by checking either
   `now >= start` or `now < end`.
4. The function is pure (no service state) and unit-tested with DST
   transition fixtures (US 2026-03-08 and 2026-11-01) plus midnight
   wrap.

## The `PushNotificationService`

```python
# src/gilbert/core/services/push_notifications.py
class PushNotificationService(Service):
    """Listens to ``notification.received`` and fans out to per-user routes.

    Capabilities declared:
    - ``push_notifications`` — the aggregator capability.
    - ``ws_handlers`` — RPCs for managing routes.
    - ``ai_tools`` — exposes ``list_my_notification_routes`` (read-only,
      everyone-role) and the route-create/test tools (user-role) so users
      can ask the AI to set things up.
    """

    config_namespace = "push_notifications"
    config_category = "Notifications"

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: EventBus | None = None
        self._access_control: AccessControlProvider | None = None
        self._users: ConfigurationReader | None = None
        self._notifications: NotificationProvider | None = None  # for URGENT-failure escalation
        self._unsubscribe: Callable[[], None] | None = None
        self._backends: dict[str, PushNotificationBackend] = {}
        self._enabled_backends: set[str] = set()
        # Retry — read from config in on_config_changed, NEVER from the
        # _DEFAULT_* constants directly. Capped server-side at MAX_RETRIES_CAP.
        self._max_retries: int = 3
        self._retry_initial_delay_s: float = 1.0
        self._retry_factor: float = 4.0
        self._retry_jitter_pct: float = 0.10  # ±10% uniform jitter
        # Worker pool / queue
        self._queue: asyncio.Queue[_FanOutJob] | None = None
        self._queue_max: int = 1000
        self._workers: list[asyncio.Task[None]] = []
        self._worker_count: int = 8
        # Per-route test-debounce: route_id -> last test ts (monotonic)
        self._test_last: dict[str, float] = {}
        self._test_debounce_s: float = 30.0
        # Best-effort "last delivered" memory for the Routes UI (S3)
        self._last_delivered: dict[str, str] = {}  # route_id -> iso ts

MAX_RETRIES_CAP = 8  # admin can set max_retries up to 8; 9+ silently capped


@dataclass(frozen=True)
class _FanOutJob:
    """One notification → many routes. Captured under the publisher's context."""
    data: dict[str, Any]
    context: contextvars.Context
```

### Lifecycle

`start(resolver)`:

1. Resolve `entity_storage`, `event_bus`, `configuration`,
   `access_control`, and `notifications` (the latter is used to escalate
   URGENT delivery failures back into the in-app stream — see "Retry
   policy").
2. Ensure indexes on `push_notification_routes`.
3. Seed an `acl_collections` row for `push_notification_routes` if not
   present (`read=user`, `write=user`, admin-override on per existing
   policy) so the entities admin page can browse all rows. The WS RPC
   layer remains the trust boundary; the ACL row exists so admins can
   read other users' routes via the existing entities surface without
   bespoke code paths.
4. Read service config (see ConfigParams below). Populates retry budget,
   queue size, worker count, debounce window, and the
   `enabled_backends: list[str]` admin allowlist (default: all
   registered).
5. For every `PushNotificationBackend.registered_backends()` entry
   whose name is in `enabled_backends`, instantiate and
   `await backend.initialize(admin_config_for_that_backend)`. Failures
   log at ERROR (with exception type and `backend_name` only — not the
   config dict) and skip — one broken backend must not stop others.
6. Create `self._queue = asyncio.Queue(maxsize=self._queue_max)`.
7. Spawn `self._worker_count` worker tasks via
   `asyncio.create_task(self._worker_loop(), name=f"push-fanout-worker-{i}")`.
8. Subscribe
   `self._unsubscribe = self._event_bus.subscribe("notification.received", self._on_notification)`.

`on_config_changed()`: invoked by the configuration service when a
ConfigParam in this namespace changes. It re-reads:

- Retry params (`max_retries` clamped at `MAX_RETRIES_CAP`,
  `retry_initial_delay_s`, `retry_factor`, `retry_jitter_pct`,
  `test_debounce_s`) — applied to the **next** delivery; in-flight
  retries already-scheduled use the prior values, which is fine for
  this knob.
- `enabled_backends` and per-backend admin config — for any backend
  whose admin config changed (or whose enabled state flipped on),
  call `await backend.close()` if currently initialised, then
  `await backend.initialize(new_config)`. This is the implementation
  of the Round-1 open question "Backend hot-reload after credential
  change" — it's promoted into v1 because the cost is ~5 lines and the
  UX foot-gun is real (admin updates token, nothing happens until they
  remember the `reload_backends` action).
- Queue / worker counts (`worker_count`, `queue_max`) — applied on
  next start; we don't dynamically resize the pool in v1. A WARNING
  is logged if the admin changes them while the service is running.

`stop()`:

1. Unsubscribe.
2. Cancel workers (`for w in self._workers: w.cancel()`); `await
   asyncio.gather(*workers, return_exceptions=True)`.
3. Drain the queue without further dispatch (jobs already taken are
   cancelled with their workers; queued jobs are dropped — at-most-once
   in v1; v1.1 leaves them as `pending` outbox rows for the next start
   to re-enqueue).
4. `await backend.close()` for every initialized backend.

### Config namespace

`gilbert.config.push_notifications`:

```python
def config_params(self) -> list[ConfigParam]:
    base = [
        ConfigParam(key="enabled_backends", type=ToolParameterType.STRING,
                    description="Comma-separated list of push backend names to enable (e.g. 'ntfy,pushover'). Empty = all.",
                    default=""),
        ConfigParam(key="max_retries", type=ToolParameterType.INTEGER,
                    description=f"Per-route retry budget on transient errors. Capped server-side at {MAX_RETRIES_CAP}.",
                    default=3),
        ConfigParam(key="retry_initial_delay_s", type=ToolParameterType.NUMBER,
                    description="Initial backoff seconds between retries.",
                    default=1.0),
        ConfigParam(key="retry_factor", type=ToolParameterType.NUMBER,
                    description="Multiplicative backoff factor per retry.",
                    default=4.0),
        ConfigParam(key="retry_jitter_pct", type=ToolParameterType.NUMBER,
                    description="Random jitter applied to each backoff (0.10 = ±10%). Prevents thundering-herd retries when a provider has a brief outage.",
                    default=0.10),
        ConfigParam(key="worker_count", type=ToolParameterType.INTEGER,
                    description="Number of concurrent delivery workers draining the fan-out queue. Applied on next service start.",
                    default=8),
        ConfigParam(key="queue_max", type=ToolParameterType.INTEGER,
                    description="Maximum pending jobs in the fan-out queue. Overflow drops with a WARNING — back-pressure, not unbounded growth.",
                    default=1000),
        ConfigParam(key="test_debounce_s", type=ToolParameterType.NUMBER,
                    description="Server-side cooldown for per-route 'Send test' actions (seconds). Prevents accidental flooding of shared channels.",
                    default=30.0),
        ConfigParam(key="test_message_body", type=ToolParameterType.STRING,
                    description="The body sent by the per-route 'Send test' button. Operator-overridable for branding / localization.",
                    default="This is a test from your Gilbert notification routes page.",
                    multiline=True),
        ConfigParam(key="default_deep_link_origin", type=ToolParameterType.STRING,
                    description="Public origin for click-through URLs in delivered messages (e.g. 'https://gilbert.example.com'). Empty = omit links.",
                    default=""),
    ]
    # Each enabled backend's params are merged in via the standard helper:
    for name, cls in PushNotificationBackend.registered_backends().items():
        base.extend(_prefixed_backend_params(name, cls.backend_config_params()))
    return base
```

`test_message_body` is the only user-facing string the service emits;
it's a `ConfigParam` (not an `ai_prompt=True` one — there's no LLM call
here) so operators can localize / rebrand. CLAUDE.md's "AI prompts are
always configurable" rule does not apply (no LLM); but the parallel
hygiene rule "configurable strings live in config" does, hence this
param.

Backend-specific params are merged under the prefix `backends.<name>.<key>`
to keep them isolated. The settings UI displays them grouped under "Backend
Settings" per backend (existing pattern in `ConfigSection.tsx`).

### `config_actions` / `invoke_config_action`

Service-level actions:

- `reload_backends` — idempotent. Tears down (`await backend.close()`)
  every currently-initialised backend, then re-runs the init sweep
  using the latest admin config. Safe to invoke any time; no-op when
  configs match. Useful right after `/plugin install` or as a manual
  override if `on_config_changed`-based hot-reload (see Lifecycle)
  doesn't pick something up.

Each backend's `backend_actions()` is exposed under
`backend_action=True` with `backend=<name>`. **Helper choice
(architecture review):** the existing `merge_backend_actions(backend,
fallback_cls)` (in `core/services/_backend_actions.py:86-110`) is
shaped for "one selected backend"; `all_backend_actions(registry,
current_backend)` (lines 42-83) is shaped for "one selected backend
plus probes of registered alternatives." Neither matches "N
concurrently live backends" — which is what this service has. We
therefore inline a 5-line merge in `PushNotificationService.config_actions()`:

```python
def config_actions(self) -> list[ConfigAction]:
    actions = [_RELOAD_BACKENDS_ACTION]
    for name, backend in self._backends.items():
        for a in type(backend).backend_actions():
            actions.append(replace(a, backend_action=True, backend=name))
    return actions
```

`invoke_config_action(key, payload)` dispatches by inspecting
`payload["backend"]` (set by the UI from the action's tag); the
existing `config.action.invoke` RPC plumbing already carries this
field. **Telegram's `discover_chat_id` flows through this same RPC**
— there is no bespoke `push.backends.telegram.discover_chat_id` frame
(architect review #1). The frontend special-cases the action *key*
(`discover_chat_id`) when it sees `data={"chats": [...]}` in the
result, not the RPC frame name.

### The fan-out path: bus subscriber → queue → workers

The bus subscriber is a tight non-blocking handoff:

```python
async def _on_notification(self, event: Event) -> None:
    """Subscriber for ``notification.received``.

    Runs on the event-bus publisher's task. MUST return immediately
    after enqueueing — see "Dispatch architecture" for why.
    """
    data = event.data or {}
    notification_id = data.get("id", "")
    user_id = data.get("user_id", "")
    if not notification_id or not user_id:
        logger.debug("push: dropping malformed event (no id/user)")
        return
    if self._queue is None:
        return  # service stopped mid-publish; fine

    # Capture the originating caller's contextvars NOW, on the publisher's
    # task. asyncio.create_task copies *implicitly* but workers run their
    # own loop, so we pass the snapshot through the queue and apply it on
    # the worker side before spawning per-route tasks.
    job = _FanOutJob(data=data, context=contextvars.copy_context())

    try:
        self._queue.put_nowait(job)
    except asyncio.QueueFull:
        logger.warning(
            "push: fan-out queue full (%d); dropping notification %s for user %s. "
            "Increase 'queue_max' or investigate stalled backends.",
            self._queue_max, notification_id, user_id,
        )
```

The worker loop:

```python
async def _worker_loop(self) -> None:
    assert self._queue is not None
    while True:
        try:
            job = await self._queue.get()
        except asyncio.CancelledError:
            return
        try:
            # Re-enter the originating caller's context so per-route
            # tasks inherit the right _user_id / _agent_id / _request_id.
            await job.context.run(self._wrap_fan_out, job)
        except Exception:
            logger.exception("push: worker crashed handling notification %s",
                             job.data.get("id", "?"))
        finally:
            self._queue.task_done()


async def _wrap_fan_out(self, job: _FanOutJob) -> None:
    # contextvars.Context.run runs sync callables synchronously; for an
    # async target we still need create_task. Spawn one Task per route so
    # any ContextVar.set() inside one delivery can't leak across siblings.
    await self._fan_out(job)


async def _fan_out(self, job: _FanOutJob) -> None:
    data = job.data
    user_id = data["user_id"]

    routes = await self._storage.query(Query(
        collection="push_notification_routes",
        filters=[
            Filter(field="user_id", op=FilterOp.EQ, value=user_id),
            Filter(field="enabled", op=FilterOp.EQ, value=True),
        ],
    ))
    routes = [r for r in routes if self._route_passes_filters(r, data)]
    if not routes:
        return

    # Stamp external_delivery_attempted_at on the original notification
    # so we can audit losses (v1 best-effort, no outbox yet).
    await self._stamp_delivery_attempted(data)

    message = self._build_push_message(data)

    # Per-route deliveries run concurrently, each in its own Context copy.
    tasks = [
        asyncio.Task(
            self._deliver_with_retry(route, message),
            context=job.context.copy(),
        )
        for route in routes
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
```

**Multi-user isolation** (per CLAUDE.md): the service is a singleton
and this code is called concurrently for many users. The only per-job
state lives on local variables (`data`, `route`, `message`) plus the
`_FanOutJob` itself — no instance-attribute writes touch per-user
data. There is no `_current_*` / `_active_*` / `_pending_*` field on
the service; the queue holds in-flight jobs but each job is keyed by
its own data shape, not by reference to a "currently-being-processed"
slot.

### `_deliver_with_retry(route_dict, message)`

```python
async def _deliver_with_retry(
    self,
    route_dict: dict[str, Any],
    message: PushMessage,
) -> None:
    route_id = route_dict["_id"]
    backend_name = route_dict["backend_name"]
    user_id = route_dict["user_id"]
    is_urgent = message.urgency is NotificationUrgency.URGENT

    backend = self._backends.get(backend_name)
    if backend is None:
        logger.warning("push: route=%s points at unknown backend=%r",
                       route_id, backend_name)
        return

    destination = PushDestination(
        user_id=user_id,
        route_id=route_id,
        data=route_dict.get("destination_data", {}),
    )

    max_retries = min(self._max_retries, MAX_RETRIES_CAP)
    delay = self._retry_initial_delay_s
    last_message = ""
    for attempt in range(1, max_retries + 2):  # initial + N retries
        try:
            result = await backend.send(destination, message)
        except Exception as exc:
            # Programmer error / unhandled — treat as REJECTED, scrub creds.
            logger.error("push: route=%s backend=%s raised: %s",
                         route_id, backend_name, _safe_repr(exc))
            return
        last_message = result.message

        if result.status is PushDeliveryStatus.DELIVERED:
            logger.info("push: route=%s backend=%s notification=%s status=delivered",
                        route_id, backend_name, message.notification_id)
            self._mark_last_delivered(route_id)
            return

        if result.status in (PushDeliveryStatus.REJECTED,
                             PushDeliveryStatus.DISABLED):
            logger.warning("push: route=%s backend=%s status=%s: %s",
                           route_id, backend_name, result.status.value,
                           result.message)
            return

        # TRANSIENT_ERROR — log retry attempts at DEBUG (high volume).
        if attempt > max_retries:
            break
        if result.retry_after_s is not None:
            sleep_for = min(float(result.retry_after_s), 60.0)
        else:
            sleep_for = delay * (1.0 + random.uniform(-self._retry_jitter_pct,
                                                        self._retry_jitter_pct))
        logger.debug("push: route=%s transient error attempt=%d sleep=%.2fs: %s",
                     route_id, attempt, sleep_for, result.message)
        await asyncio.sleep(sleep_for)
        delay *= self._retry_factor

    # Retries exhausted — escalate URGENT to ERROR + operator notification.
    level = logging.ERROR if is_urgent else logging.WARNING
    logger.log(level, "push: route=%s backend=%s exhausted retries: %s",
               route_id, backend_name, last_message)
    if is_urgent and self._notifications is not None:
        # Surface the failure into the in-app stream so operators see it.
        try:
            await self._notifications.notify_user(
                user_id=user_id,
                message=f"External push failed for route '{route_dict.get('label', route_id)}'.",
                urgency=NotificationUrgency.URGENT,
                source="push_failure",
                source_ref={"route_id": route_id, "notification_id": message.notification_id},
            )
        except Exception:
            logger.exception("push: failed to record push_failure notification")
```

**No in-process dedup map.** The Round-1 design kept a `self._delivered`
set keyed by `(notification_id, route_id)`. Engineering review showed
this map served no purpose: same-tick duplicates can't happen
(`_deliver_with_retry` is sequential per task), cross-tick duplicates
require either bus replay (none) or a second instance (out of scope).
v1 has no dedup map; v1.1's outbox row's primary key is the durable
dedup token.

`_mark_last_delivered(route_id)` updates the in-memory
`self._last_delivered[route_id] = now_iso` and writes the same field
through to the row (best-effort `put`; failures logged at DEBUG).
Restart loss is acceptable — this powers a UI hint, not correctness.

### `_safe_repr(exc)` — credential scrubbing

```python
_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+)",
    re.IGNORECASE,
)

def _safe_repr(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)
```

Backends MUST funnel any exception text intended for
`PushDeliveryResult.message` or for `logger.exception` / `logger.error`
through this helper. The unit test for each backend asserts that a
mocked `httpx` error containing the bot token / webhook URL / Bearer
header does not appear in the result message.

### Title / body construction

`Notification.message` today is one short string. Most external providers
have title + body. Rule:

- `title`: `"Gilbert"` if `notification.source` is `"system"` or empty,
  otherwise `f"Gilbert · {source}"` (capitalized).
- `body`: `notification.message`.
- `urgency`: passed through.
- `source_ref`: passed through; if the service has a
  `default_deep_link_origin` config and `source_ref` carries a known
  shape (e.g. `{"goal_id": ...}`, `{"conversation_id": ...}`), it builds
  a `deep_link_url` and stuffs it onto `PushMessage` (a derived field
  the backend can read from `source_ref["deep_link_url"]` if it wants).

Each backend implements its own provider-specific formatting: ntfy uses
`Title:` and `Tags:` headers; Discord builds an embed; Pushover uses
`title` + `message` + optional `url`/`url_title`; Telegram sends Markdown
text + optional inline keyboard with the URL.

### WS RPCs (per-user routes management)

All RBAC-checked: only the calling user can read/write their own routes.
**A single helper** `_authorize_route_access(conn, route_row,
write: bool) -> None` is used by every RPC; it raises a `PermissionError`
unless `conn.user_ctx.user_id == row["user_id"]` or (for `write=False`
only) `conn.user_ctx.is_admin`. Don't reimplement this in five
handlers — defense in depth, single source of truth.

| Frame | Purpose | Required role |
|---|---|---|
| `push.routes.list` | List the calling user's routes (or any user's, admin only) | user |
| `push.routes.create` | Create a new route for the calling user | user |
| `push.routes.update` | Update one of the calling user's routes | user |
| `push.routes.delete` | Delete one | user |
| `push.routes.test` | Send a real test message via this route | user |
| `push.routes.test_unsaved` | Send a test before the form is saved | user |
| `push.backends.list` | List installed backends (name, label, destination_params, runtime_data) | user |
| `push.sources.list` | Distinct `notification.source` values seen for this user in last 30 days | user |

#### `push.routes.test` payload: `{route_id: str}`

Server checks `self._test_last[route_id]` against the configured
debounce window (`test_debounce_s`, default 30). If too soon, returns
`{ok: false, status: "debounced", message: "Please wait Ns before retesting."}`.
Otherwise: constructs a `PushMessage(title="Gilbert · Test",
body=self._test_message_body, urgency=NORMAL, source="test",
notification_id="test-<uuid>")`, calls
`backend.send(destination, message)` once (no retry), updates
`self._test_last[route_id]`, returns `{ok: bool, status: str,
message: str}` so the UI shows a toast.

**Admin testing other users' routes:** by default, `push.routes.test`
on another user's route is **rejected** (consent over debugging). An
admin who needs to test another user's route can flip the route's
`enabled=true`, send a notification with `source_ref.test=true`, and
observe in their logs. Documented behavior, gated.

#### `push.routes.test_unsaved` payload: `{backend_name: str, destination_data: dict}`

Same delivery path as `push.routes.test` but without a saved row —
used by the route-creation form to validate destination credentials
**before** persisting bad data. Same debounce, keyed by a
hash of `(user_id, backend_name, destination_data)` so a user
hammering the button on a typo'd key still hits the cooldown.

#### `push.backends.list` returns:

```json
{
  "type": "push.backends.list.result",
  "backends": [
    {
      "name": "ntfy",
      "label": "ntfy",
      "description": "ntfy.sh / self-hosted ntfy server",
      "destination_params": [
        {"key": "topic", "type": "string", "sensitive": false, "default": "", ...},
        {"key": "server", "type": "string", "sensitive": false, "default": "https://ntfy.sh", ...}
      ],
      "actions": [
        {"key": "test_connection", "label": "Test connection", "description": "..."}
      ],
      "runtime_data": {}
    },
    {
      "name": "telegram",
      "label": "Telegram",
      "description": "Telegram bot push messages",
      "destination_params": [...],
      "actions": [
        {"key": "test_connection", "label": "Verify bot", "description": "..."},
        {"key": "discover_chat_id", "label": "Discover chat id", "description": "..."}
      ],
      "runtime_data": {"bot_username": "MyGilbertBot"}
    }
  ]
}
```

`runtime_data` is a per-backend dict the server populates lazily after
`initialize` (e.g. Telegram caches `getMe.username` so the wizard's
deep link `https://t.me/<bot_username>` renders without a second
roundtrip — product review S1). `runtime_data` is **never** populated
with secret material; it's strictly for UI hints (bot username, max
upload size, etc.).

The frontend uses this to render the per-route form fields dynamically.
Adding a new backend plugin shows up in the dropdown automatically with
no core SPA changes.

#### `push.sources.list` returns:

```json
{
  "type": "push.sources.list.result",
  "sources": ["agent", "scheduler", "inbox", "doorbell", "system"]
}
```

Computed from `notifications.source` distinct values in the calling
user's last-30-days notifications. Replaces the Round-1 hard-coded
list in the SPA (product review P3); the Source allow/deny multiselect
binds to this dynamic list, falling back to free-form text input for
sources the user wants to anticipate.

#### Telegram `discover_chat_id` routing

There is **no bespoke `push.backends.telegram.discover_chat_id` frame.**
The action is dispatched through the existing
`config.action.invoke` RPC that the configuration service already
exposes — the SPA passes `{namespace: "push_notifications", key:
"discover_chat_id", payload: {...}, backend: "telegram"}` and the
service's `invoke_config_action` routes to
`self._backends["telegram"].invoke_backend_action("discover_chat_id",
payload)`. The frontend special-cases the action **key** when the
result carries `data={"chats": [...]}` (rendering chips); it does not
special-case the RPC frame. (Architect review #1.)

### AI tools

**Framing note (product review P2):** the load-bearing AI tool for the
notifications domain is `notify_user` (already exposed by the existing
`NotificationService` — see `core/services/notifications.py:326-357`).
The push fan-out service adds *route-management* tools, which are
ergonomic polish, not the headline. We ship them because users will
ask "show me my notification routes" and "add a Pushover route" in
chat; we do not pretend they unlock new agentic capability. Future
agentic value comes from the existing `notify_user` tool, which now
transparently fans out via the bus subscriber.

Tools registered by `PushNotificationService`:

- `list_my_notification_routes` (everyone, slash: `/notify routes`,
  `slash_help`: "List your push notification routes (ntfy, Pushover,
  Discord, Telegram).") — lists the calling user's routes.
- `create_notification_route` (user, slash: `/notify route_create`,
  `slash_help`: "Add a new push notification route. The model will
  ask for the backend, label, and destination details.") — creates a
  route. Args: `backend_name`, `label`, `destination` (JSON dict),
  `urgency_floor` (default `"normal"`).
- `delete_notification_route` (user, slash: `/notify route_delete`,
  `slash_help`: "Delete a push route. Asks for confirmation before
  destroying.") — **does not delete directly**; returns a UI block
  (per `memory-ui-blocks.md`) with a "Confirm delete" button that
  hits the WS RPC. This avoids the footgun of a model mis-resolving a
  label-to-id lookup and silently destroying the wrong route.
  (Product review S7.)
- `send_test_notification` (user, slash: `/notify test`,
  `slash_help`: "Send a test message through one of your push
  notification routes.") — sends the test message through one route,
  honoring the same `test_debounce_s` cooldown as the WS RPC.

All four use the standard `_user_id` injection pattern from
`AIService._run_one_tool` — they read `arguments["_user_id"]` and never
touch `self`-stored caller state. `slash_namespace = "notify"` on the
service class. Each tool sets an explicit `slash_help` string so the
autocomplete popover renders cleanly.

## Per-user Notification Routes UI

A new page mounted at `/account/notifications`. **SPA-declared route,
backend-driven schema:** the route itself is hard-coded in the SPA
(this is a core service, not a plugin) but every form field below the
backend dropdown is rendered from `push.backends.list`, so adding a
push plugin requires zero SPA edits.

### Empty-state hero flow (product review P1 — NEW)

When the page loads with zero routes, render a hero CTA, not the
generic empty state:

```
You don't have any notification routes yet.
Gilbert can deliver important notifications to your phone or chat
even when you're not at this tab.

   [ ► Quick setup: ntfy on my phone ]   ← primary CTA, ntfy is "Recommended"

   Other options:
   [ Add Pushover ]   [ Add Discord channel ]   [ Add Telegram bot ]*
                                                           *if admin set bot token
```

**Quick-setup-ntfy** is a one-click flow:

1. Generate a random topic `gilbert-<user_id_short>-<8 random chars>`.
2. Create the route server-side via `push.routes.create` with
   `backend_name="ntfy"`, `label="Phone via ntfy"`,
   `destination_data={"topic": "<topic>", "server": ""}` (server blank
   = use admin default, which is `https://ntfy.sh` out-of-the-box),
   `urgency_floor="normal"`.
3. Render a QR code that opens `ntfy://subscribe/<topic>` (the ntfy
   mobile app's deep link). Caption: "Scan with the ntfy app on your
   phone, then tap 'Send test' below."
4. Auto-trigger `push.routes.test` after 5 seconds so the user sees
   the message land on their device.

This is the entire ballgame for self-hosters. ntfy.sh + a random topic
+ a QR + a test = "I have notifications on my phone" in under 60s.

### Page layout (one-or-more-routes state)

```
[ My Notification Routes ]                    [ + Add Route ]

▸ Phone via ntfy           [enabled toggle] [edit] [test] [×]
   topic: gilbert-jeff-phone-x82js
   Send when urgency is at least: Normal
   Sources: all       Quiet hours: 22:00–07:00 (America/Los_Angeles)
   Last delivered: 2 hours ago                          ← S3

▸ Pushover phone           [enabled toggle] [edit] [test] [×]
   user_key: ********                Send when urgency is at least: Urgent
   Last delivered: never                                ← prompts user to test
```

**Last delivered** is computed from `route.last_delivered_at` (the
in-memory write-through field) — no audit collection, no extra round
trip. "never" is a strong signal the user should hit the test button.

### "Add Route" / "Edit Route" form

1. **Backend dropdown** — populated from `push.backends.list`. ntfy
   gets a "Recommended" badge for new users (no existing routes).
2. **Label** text input (e.g. "Phone").
3. **Destination fields** — rendered from the chosen backend's
   `destination_params`. Sensitive fields masked (`type=password`),
   types respected.
4. **Send when urgency is at least** — `Info | Normal | Urgent`
   dropdown bound to the `urgency_floor` field. Copy is **not**
   "Urgency floor" (Round-1 wording was developer-speak — product
   review U3).
5. **Only deliver from these sources** / **Never deliver from these
   sources** — multiselects. Populated from `push.sources.list`
   (per-user dynamic list of recent sources, not a hard-coded SPA
   list — product review P3) plus a free-form text input for
   anticipated sources. Copy follows product review S8.
6. **Quiet hours** — start picker, end picker. **Timezone field is
   hidden under an "Advanced" disclosure**, defaulting to the user's
   browser timezone via `Intl.DateTimeFormat().resolvedOptions().timeZone`
   (product review U2). 95% of users will never open the disclosure.
7. **Save** + **Send test**. The test button is available **before
   save** via `push.routes.test_unsaved` (product review S2): user
   enters a Pushover user_key, clicks "Send test", we validate without
   saving bad data first.
8. **ntfy `server` field shows the resolved value when blank**:
   `Server: [           ] (using admin default: https://ntfy.sh)` —
   product review S5, so the dependency on admin config is visible.

### Telegram chat-id wizard (product review U1 — promoted to top-level)

Telegram is the only backend with non-trivial setup friction (no
OAuth, the bot needs to see a message before `getUpdates` returns a
chat id). The wizard is its own component, driven by the
`runtime_data.bot_username` from `push.backends.list` and the
`discover_chat_id` action result shape — **not** a Telegram-specific
component import. Any backend that returns `data={"chats": [...]}` from
a `discover_chat_id` action gets the same chip-pick UI for free.

Wizard screens:

| Step | UI |
|---|---|
| 1 | "Open Telegram and DM `@MyGilbertBot`." Button: **[ Open Telegram ]** linking to `https://t.me/<bot_username>` (uses `runtime_data.bot_username`). One click instead of "copy this username." |
| 2 | "Send any message to the bot." (no UI element — instructional only). |
| 3 | "Click below once you've sent a message." Button: **[ I sent it — find my chat ]**. On click, invoke `config.action.invoke` with `key="discover_chat_id"`. Server polls `getUpdates` and returns `data={"chats": [{"chat_id": "...", "name": "Jeff", "last_text": "hi"}]}`. The page renders each chat as a clickable chip. |
| 4 | User clicks a chip → `chat_id` field on the form is populated. |
| 5 | "Save & test" — runs `push.routes.test_unsaved` first to confirm the bot can DM that chat, then on success persists. |

The wizard component lives at
`frontend/src/components/notifications/ChatIdWizard.tsx` (note: not
`TelegramSetupWizard`) and renders whenever the chosen backend's
`actions[].key == "discover_chat_id"`. **It does not import Telegram-
specific code** — the architectural rule is "core SPA pages don't
import plugin-specific code" (`memory-architecture-checklist.md` UI
extension section). Future backends with chat-id-style setup
(e.g. WhatsApp Business) reuse this component automatically.

### Header bell badge — entry point added (product review U5)

The existing notification bell does not change visually — in-app
delivery is unchanged, no "external delivery enabled" indicator. **But
the bell dropdown's footer gets one new link:**

```
[ ... last 5 unread notifications ... ]
[ View all → ]    [ Manage delivery routes → ]   ← new
```

Plus the existing `/notifications` page (the in-app list at
`NotificationsPage`) gets a header button **[ Delivery routes ]** that
links to `/account/notifications`. Two entry points, zero design cost.

## The four backend plugins

Each plugin has the same skeleton. I'll give the full spec for `ntfy`
and the deltas for the other three.

### Plugin shape (template)

```
std-plugins/<name>/
├── __init__.py             # empty
├── plugin.yaml             # name/version/provides
├── plugin.py               # create_plugin() returning side-effect plugin
├── pyproject.toml          # third-party deps (httpx already present)
├── <name>_push.py          # the PushNotificationBackend subclass
└── tests/
    ├── conftest.py
    └── test_<name>_push.py
```

`provides` for all four: a unique capability tag. The service does not
require these capabilities — discovery is via the backend registry —
but advertising them lets `/plugin list` show what each plugin
contributes.

### `std-plugins/ntfy/`

**`plugin.yaml`:**

```yaml
name: ntfy
version: "1.0.0"
description: "ntfy push-notification backend (ntfy.sh or self-hosted)."

provides:
  - ntfy_push
requires: []
depends_on: []
```

**`pyproject.toml`:**

```toml
[project]
name = "gilbert-plugin-ntfy"
version = "1.0.0"
description = "ntfy push notification backend for Gilbert"
requires-python = ">=3.12"
# Uses httpx, already in core.
dependencies = []

[tool.uv]
package = false
```

**`plugin.py`:**

```python
from __future__ import annotations
from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class NtfyPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="ntfy",
            version="1.0.0",
            description="ntfy push-notification backend",
            provides=["ntfy_push"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import ntfy_push  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return NtfyPlugin()
```

**`ntfy_push.py`:**

```python
from __future__ import annotations
import logging
from typing import Any

import httpx

from gilbert.interfaces.configuration import (
    ConfigAction, ConfigActionResult, ConfigParam,
)
from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryResult, PushDeliveryStatus, PushDestination,
    PushMessage, PushNotificationBackend,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_DEFAULT_SERVER = "https://ntfy.sh"
_DEFAULT_TIMEOUT = 10


class NtfyPush(PushNotificationBackend):
    backend_name = "ntfy"

    def __init__(self) -> None:
        self._default_server: str = _DEFAULT_SERVER
        self._auth_token: str = ""           # admin-set; optional
        self._timeout: int = _DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="default_server", type=ToolParameterType.STRING,
                        description="Default ntfy server (used when a route doesn't specify its own).",
                        default=_DEFAULT_SERVER),
            ConfigParam(key="auth_token", type=ToolParameterType.STRING,
                        description="Optional Bearer token for protected ntfy servers. Leave empty for the public ntfy.sh.",
                        sensitive=True, default=""),
            ConfigParam(key="timeout", type=ToolParameterType.INTEGER,
                        description="HTTP timeout in seconds.",
                        default=_DEFAULT_TIMEOUT),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="topic", type=ToolParameterType.STRING,
                        description="ntfy topic (path component). Pick something obscure.",
                        default=""),
            ConfigParam(key="server", type=ToolParameterType.STRING,
                        description="ntfy server URL. Leave empty to use the admin default.",
                        default=""),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description="Send 'Gilbert ntfy connectivity test' to a topic of your choice.",
            ),
        ]

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_connection":
            topic = str(payload.get("topic", "")).strip()
            if not topic:
                # NEVER default to "gilbert-test" — that topic is global
                # on ntfy.sh and every Gilbert install in the world would
                # broadcast its connectivity tests to whoever's listening.
                # Force the admin to pass an explicit topic from the UI.
                return ConfigActionResult(
                    status="error",
                    message="Provide a topic in the action payload (e.g. a random string).",
                )
            return await self._action_test_connection(topic)
        return ConfigActionResult(status="error", message=f"Unknown action: {key}")

    async def _action_test_connection(self, topic: str) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(status="error",
                                      message="ntfy backend not initialized — save settings first.")
        try:
            resp = await self._client.post(
                f"{self._default_server.rstrip('/')}/{topic}",
                content="Gilbert ntfy connectivity test".encode("utf-8"),
                headers=self._headers({"Title": "Gilbert · Test"}),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ConfigActionResult(status="error",
                                      message=f"ntfy returned HTTP {exc.response.status_code}.")
        except Exception as exc:
            return ConfigActionResult(status="error",
                                      message=f"Connection failed: {exc}")
        return ConfigActionResult(status="ok",
                                  message=f"ntfy accepted message on topic {topic!r}.")

    async def initialize(self, config: dict[str, Any]) -> None:
        self._default_server = str(config.get("default_server", _DEFAULT_SERVER) or _DEFAULT_SERVER)
        self._auth_token = str(config.get("auth_token", "") or "")
        self._timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(
        self, destination: PushDestination, message: PushMessage,
    ) -> PushDeliveryResult:
        if self._client is None:
            return PushDeliveryResult(status=PushDeliveryStatus.DISABLED,
                                      message="ntfy backend not initialized")

        topic = str(destination.data.get("topic", "")).strip()
        if not topic:
            return PushDeliveryResult(status=PushDeliveryStatus.REJECTED,
                                      message="route is missing 'topic'")
        server = str(destination.data.get("server", "")).strip() or self._default_server

        priority = _ntfy_priority(message.urgency)
        headers = self._headers({
            "Title": message.title,
            "Priority": priority,
            "Tags": _ntfy_tag_for_source(message.source),
        })
        deep_link = (message.source_ref or {}).get("deep_link_url") if message.source_ref else None
        if deep_link:
            headers["Click"] = str(deep_link)

        try:
            resp = await self._client.post(
                f"{server.rstrip('/')}/{topic}",
                content=message.body.encode("utf-8"),
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return PushDeliveryResult(status=PushDeliveryStatus.TRANSIENT_ERROR,
                                      message=f"network error ({type(exc).__name__})")
        except Exception as exc:
            # Scrub: never include raw exception args (URL/token leak).
            return PushDeliveryResult(status=PushDeliveryStatus.REJECTED,
                                      message=_safe_repr(exc))

        if 200 <= resp.status_code < 300:
            return PushDeliveryResult(status=PushDeliveryStatus.DELIVERED,
                                      message=f"HTTP {resp.status_code}")
        if 500 <= resp.status_code < 600:
            return PushDeliveryResult(status=PushDeliveryStatus.TRANSIENT_ERROR,
                                      message=f"server HTTP {resp.status_code}")
        # Status line only — DO NOT include resp.text (may echo URL/topic).
        return PushDeliveryResult(status=PushDeliveryStatus.REJECTED,
                                  message=f"HTTP {resp.status_code}")

    def _headers(self, extras: dict[str, str]) -> dict[str, str]:
        headers = dict(extras)
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers


def _ntfy_priority(urgency: NotificationUrgency) -> str:
    return {
        NotificationUrgency.INFO: "2",
        NotificationUrgency.NORMAL: "3",
        NotificationUrgency.URGENT: "5",
    }[urgency]


def _ntfy_tag_for_source(source: str) -> str:
    # ntfy renders these as emojis. Stay close to existing source tags.
    return {
        "agent": "robot",
        "scheduler": "alarm_clock",
        "inbox": "email",
        "doorbell": "bell",
        "presence": "house",
        "ai": "brain",
        "test": "white_check_mark",
    }.get(source, "bell")
```

Tests (`tests/test_ntfy_push.py`):

- Backend registers under `"ntfy"` after import.
- `destination_params()` declares `topic` and `server`.
- `send` with a successful 200 response → `DELIVERED`.
- `send` with 5xx → `TRANSIENT_ERROR`.
- `send` with 401 → `REJECTED`.
- `send` with `route.data["topic"]` empty → `REJECTED("missing topic")`,
  no HTTP call (mock the client, assert `post` not called).
- Quiet-hour and urgency-floor filtering live in the service tests, not
  here — the backend's job is one HTTP call.

### `std-plugins/pushover/`

`plugin.yaml`: `name: pushover`, `provides: [pushover_push]`.

`pyproject.toml`: deps `[]` (httpx).

**Backend `pushover_push.py`** (deltas only):

```python
class PushoverPush(PushNotificationBackend):
    backend_name = "pushover"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="api_token", type=ToolParameterType.STRING,
                        description="Pushover application API token (admin creates a Pushover app once and shares the token).",
                        sensitive=True, default=""),
            ConfigParam(key="timeout", type=ToolParameterType.INTEGER,
                        description="HTTP timeout in seconds.",
                        default=10),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="user_key", type=ToolParameterType.STRING,
                        description="Your Pushover user key (30-character string from pushover.net).",
                        sensitive=True, default=""),
            ConfigParam(key="device", type=ToolParameterType.STRING,
                        description="Optional device name to target a specific device. Leave empty for all.",
                        default=""),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(key="test_connection", label="Validate API token",
                         description="Calls Pushover's /users/validate.json with the configured token."),
        ]
```

**`send` mapping:**

| Pushover field | Source |
|---|---|
| `token` | admin `api_token` |
| `user` | route `destination_data.user_key` |
| `device` | route `destination_data.device` if non-empty |
| `title` | `message.title` |
| `message` | `message.body` |
| `priority` | `-1` for INFO, `0` for NORMAL, `1` for URGENT |
| `url` | `message.source_ref["deep_link_url"]` if present |
| `url_title` | `"Open in Gilbert"` if `url` set |

POST to `https://api.pushover.net/1/messages.json`. Pushover returns
`{status: 1}` on success and HTTP 200 with `{status: 0, errors: [...]}`
on auth failure → map to `REJECTED`. 5xx → `TRANSIENT_ERROR`.

**Test connection action:** POST `https://api.pushover.net/1/users/validate.json`
with the admin token and the user_key from the action payload (UI prompts
for a user_key when invoking — using the standard `payload` channel).
Response includes `devices: [...]`; surface count.

Tests: same shape as ntfy (success / 5xx / 401 / missing user_key).

### `std-plugins/discord-webhook/`

`plugin.yaml`: `name: discord-webhook`, `provides: [discord_webhook_push]`.

`pyproject.toml`: deps `[]`.

**Backend `discord_webhook_push.py`** (deltas):

```python
class DiscordWebhookPush(PushNotificationBackend):
    backend_name = "discord-webhook"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        # Discord webhooks are per-channel; no shared admin secret is
        # required. We expose just an HTTP timeout.
        return [
            ConfigParam(key="timeout", type=ToolParameterType.INTEGER,
                        description="HTTP timeout in seconds.",
                        default=10),
            ConfigParam(key="username_override", type=ToolParameterType.STRING,
                        description="Override the webhook display name (default: webhook's configured name).",
                        default="Gilbert"),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="webhook_url", type=ToolParameterType.STRING,
                        description="Full Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>).",
                        sensitive=True, default=""),
            ConfigParam(key="mention", type=ToolParameterType.STRING,
                        description="Optional mention prefix on URGENT messages, e.g. '@here' or '<@USER_ID>'.",
                        default=""),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        # Service-level test_connection is awkward here because there's no
        # shared cred — Discord webhooks are per-route. A useful service-
        # level "test" verifies an arbitrary webhook URL passed in payload.
        return [
            ConfigAction(key="test_connection", label="Test a webhook URL",
                         description="Pings the Discord webhook URL provided in the action payload."),
        ]
```

**`send` mapping:**

POST to `webhook_url` with JSON. **Validate the URL prefix** before
calling — accept only
`https://discord.com/api/webhooks/<id>/<token>` or
`https://discordapp.com/api/webhooks/<id>/<token>`. Reject anything
else as `REJECTED("invalid Discord webhook URL")`. Without this guard,
a typo or a malicious paste could turn the backend into an SSRF
vector against internal endpoints (engineering review §12).

```json
{
  "username": "Gilbert",
  "content": "<mention if URGENT> **<title>**\n<body>\n<deep_link_url>",
  "flags": 0,
  "embeds": [{
    "title": "<title>",
    "description": "<body>",
    "url": "<deep_link_url>",
    "color": 16744448,
    "footer": {"text": "Gilbert · <source>"}
  }]
}
```

Color map: INFO=`0x6E7681` (gray), NORMAL=`0xFF8C00` (amber),
URGENT=`0xCC2222` (red).

**Test-message variant** (engineering review §7): when invoked from
`push.routes.test` or `push.routes.test_unsaved` (signalled by
`message.source == "test"`), the JSON adds `"flags": 4096`
(SUPPRESS_NOTIFICATIONS) and the channel does **not** ping members,
so users testing in shared channels don't spam co-workers. The body
remains visible — it's a quiet-but-readable message.

**Rate-limit handling.** Discord's 429 includes
`X-RateLimit-Reset-After` (seconds, float). Parse it into
`PushDeliveryResult.retry_after_s` so the service-level retry layer
sleeps for that duration instead of the configured backoff
(engineering review §5). Cap at 60s service-side.

Discord's webhook responds 204 on success. 401/404 (revoked or deleted
webhook) → `REJECTED`. 429 → `TRANSIENT_ERROR(retry_after_s=parsed)`.
5xx → `TRANSIENT_ERROR`.

**`PushDeliveryResult.message` scrubbing**: Discord's response body on
401/404 echoes the URL path including the webhook token. Backends MUST
funnel exception text through `_safe_repr` and MUST NOT include
`resp.text` in `message`; the test suite asserts the webhook token
never appears in `message` for any failure path (engineering review §8).

**Service-level `test_connection` action**: takes
`payload.webhook_url` (does NOT default to a saved one — the action
prompt explicitly asks for a URL each time). Same prefix validation
and same `flags: 4096` apply.

Tests: success 204; rate-limit honors `X-RateLimit-Reset-After`; 404
→ REJECTED; missing `webhook_url` → REJECTED; invalid URL prefix →
REJECTED with no HTTP call; test variant sends `flags: 4096`; failure
text never contains the webhook token.

### `std-plugins/telegram/`

`plugin.yaml`: `name: telegram`, `provides: [telegram_push]`.

`pyproject.toml`: deps `[]` (we hit the Bot API directly via httpx — no
SDK needed for sending).

**Backend `telegram_push.py`** (deltas):

```python
class TelegramPush(PushNotificationBackend):
    backend_name = "telegram"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="bot_token", type=ToolParameterType.STRING,
                        description="Telegram bot token from @BotFather.",
                        sensitive=True, default=""),
            ConfigParam(key="timeout", type=ToolParameterType.INTEGER,
                        description="HTTP timeout in seconds.",
                        default=15),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(key="chat_id", type=ToolParameterType.STRING,
                        description="Telegram chat id (numeric for users, '-100…' for channels). Use the 'Discover chat id' action below.",
                        default=""),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(key="test_connection", label="Verify bot",
                         description="Calls getMe to verify the bot token."),
            ConfigAction(key="discover_chat_id", label="Discover chat id",
                         description="Polls getUpdates and shows recent chat ids the bot has seen."),
        ]
```

**`send` mapping:**

POST `https://api.telegram.org/bot<token>/sendMessage` with JSON:

```json
{
  "chat_id": "<chat_id>",
  "text": "*<title>*\n<body>\n<deep_link>",
  "parse_mode": "Markdown",
  "disable_notification": <urgency==INFO>,
  "reply_markup": {            // only when deep_link is set
    "inline_keyboard": [[
      {"text": "Open in Gilbert", "url": "<deep_link_url>"}
    ]]
  }
}
```

Telegram responds 200 with `{"ok": true, ...}` on success or `{"ok":
false, "error_code": ..., "description": "..."}`. Map by `error_code`:

- 401 / 403 (bot blocked, chat not found, token bad) → `REJECTED`.
- 429 (Too Many Requests) → `TRANSIENT_ERROR(retry_after_s=parameters.retry_after)`.
- 500-range → `TRANSIENT_ERROR`.

**Webhook-mode bots are not supported in v1** (engineering review §12).
On `initialize`, the backend calls `getWebhookInfo`; if `result.url` is
non-empty, it logs ERROR `"telegram bot is in webhook mode; v1 requires
polling-mode bots"` and stays in DISABLED state. `send` returns
`DISABLED("webhook-mode bot")` rather than spuriously failing. The
admin-facing `test_connection` action surfaces the same message.

**Bot username caching for the wizard**: on successful `initialize`,
the backend calls `getMe` and stores
`self._bot_username = result.username`. The service exposes it via
`runtime_data["bot_username"]` in `push.backends.list` so the wizard's
`https://t.me/<bot_username>` deep link renders without a second
roundtrip (product review S1). Token is **not** included in
`runtime_data`.

**Token scrubbing**: the bot token appears in every API URL
(`/bot<token>/sendMessage`). `_safe_repr` strips it. The test suite
asserts the token never appears in `PushDeliveryResult.message` or in
`logger.exception` text on any error path (engineering review §8).

#### Telegram chat-id setup dance (the user flow)

This is the awkward part. A user who has never DM'd the bot does not have
a chat id. The action `discover_chat_id` walks them through it; the per-user
Routes UI also has a guided wizard:

1. Admin configures the bot token in Settings (one-time).
2. User goes to `/account/notifications`, clicks "Add Route", selects
   "Telegram".
3. The form shows: **Step 1.** "Open Telegram and send any message to
   `@<bot_username>`." (the bot username is fetched server-side via
   `getMe` and surfaced in `push.backends.list` for telegram).
4. **Step 2.** "Click 'Discover chat id' below." This invokes the
   service's wrapper around `discover_chat_id`. The server calls the
   bot's `getUpdates` and returns a list of `(chat_id, name, last_text)`
   tuples seen in the last 24h. UI renders them as clickable
   chips.
5. User picks the right chip → `chat_id` field is populated.
6. User saves the route.
7. User clicks "Send test" — bot DMs them "This is a test from your
   Gilbert notification routes page."

The `discover_chat_id` action returns `data={"chats": [{"chat_id": "...",
"name": "Jeff", "last_text": "hi"}]}` and a `pending` status with a
human-legible toast. The frontend has special handling for this action
key on the Routes page (showing the chip list); on the global Settings
page it shows the toast text only.

Server-side helper: the service exposes
`push.backends.telegram.discover_chat_id` as a special-case WS RPC the
Routes page invokes (the `push.backends.list` response signals to the
SPA that telegram has a `discover_chat_id` action so the UI can swap in
the wizard). Backends without this action don't get the wizard.

Tests:

- `getMe` response with `username` → action returns `pending` + bot
  username in `data`.
- `sendMessage` 200 ok → `DELIVERED`.
- `sendMessage` 403 "bot was blocked" → `REJECTED`.
- `sendMessage` 429 with `retry_after: 5` → `TRANSIENT_ERROR`, sleeps
  5s before returning.

## Concrete file inventory (post-implementation)

```
src/gilbert/interfaces/push_notifications.py            NEW (~250 LoC)
src/gilbert/core/services/push_notifications.py         NEW (~600 LoC)
tests/unit/core/test_push_notification_service.py       NEW
tests/unit/test_push_notifications_interface.py         NEW

frontend/src/pages/account/NotificationRoutesPage.tsx   NEW
frontend/src/components/notifications/RouteForm.tsx     NEW
frontend/src/components/notifications/RouteList.tsx     NEW
frontend/src/components/notifications/ChatIdWizard.tsx  NEW (backend-agnostic)
frontend/src/components/notifications/NtfyQuickSetup.tsx NEW (PR-1 hero flow)
frontend/src/hooks/usePushNotificationsApi.ts           NEW

std-plugins/ntfy/__init__.py                            NEW (empty)
std-plugins/ntfy/plugin.yaml                            NEW
std-plugins/ntfy/plugin.py                              NEW
std-plugins/ntfy/pyproject.toml                         NEW
std-plugins/ntfy/ntfy_push.py                           NEW
std-plugins/ntfy/tests/conftest.py                      NEW
std-plugins/ntfy/tests/test_ntfy_push.py                NEW

std-plugins/pushover/...                                NEW (mirror of ntfy)
std-plugins/discord-webhook/...                         NEW (mirror)
std-plugins/telegram/...                                NEW (mirror)
```

`src/gilbert/core/app.py` registers `PushNotificationService` exactly
once during boot, after `NotificationService`. Order does not matter for
correctness (events buffer if no subscriber yet — actually they don't,
they're in-memory pub/sub, but no notifications fire during boot before
both services are up).

`src/gilbert/core/services/notifications.py` is **not modified**.

## Open questions

These are intentionally left for the implementation phase or v2:

1. **Presence-gated delivery.** Should the service consult
   `PresenceProvider` and skip delivery when the user is "here"? The
   request hinted at this. v1 says no — easier semantics, and "always
   deliver, filter by route rules" is what users expect. v2 hook:
   add `route.deliver_when` field with values `always | when_offline |
   when_no_active_session`. The "no active session" variant requires
   counting that user's live WebSocket connections, which lives on the
   `WsConnectionManager` and would need a new capability protocol
   (`WsSessionInformation`) to query without crossing layers.
2. **Per-source default routes.** Ergonomically, "send all `agent`
   notifications to my phone, all `inbox` to email" is the goal. v1's
   `source_allow` / `source_deny` covers this, but the UI could expose
   a "Quick setup: default routes by source" template. Punt.
3. **Routing on group membership / role.** "Send all `urgent` to every
   admin" cannot be expressed as one user's route. That's a separate
   feature ("broadcast notifications") and outside the scope of
   per-user fan-out. The existing `notify_user` already targets one
   user; adding "notify_role" would be the cleaner v2 addition.
4. **User profile timezone field.** This spec depends on a
   `users.<user_id>.timezone` field for quiet-hour tz fall-through.
   If the auth/user-profile schema does not already expose one, this
   spec inherits the responsibility to add it (small change in
   `interfaces/auth.py` + the user profile UI). Confirm during
   implementation kickoff; if it's missing, do this in PR-1 — see
   "Dependencies" below.

### Resolved in this revision (no longer open)

- ~~Persistent delivery audit log~~ — promoted from v2 to **v1.1
  mandatory** (the outbox that turns at-most-once into at-least-once).
  Still not in v1, but no longer a "punt"; it's a known follow-up.
- ~~Cross-process dedup via separate `push_dedup` collection~~ —
  removed. The v1.1 outbox row's primary key is the cross-process
  dedup token; no separate collection needed.
- ~~Backend hot-reload after credential change~~ — promoted into v1
  via the `on_config_changed` hook (Lifecycle section). Cost was tiny
  and the UX foot-gun was real.

## Test plan

Service-level (unit, against fakes — `FakePushBackend`,
`FakeStorageBackend`, `FakeEventBus`):

- **Bus subscriber returns immediately.** Publishing
  `notification.received` resolves the publisher's `await` within a
  bounded time even when `FakePushBackend.send` blocks indefinitely
  (the worker is what blocks, not the subscriber). Critical
  regression test for the engineering review §1 fix — failure of this
  test means the worker pool isn't actually decoupled.
- **Worker pool semantics.** `worker_count=N` means at most N
  in-flight deliveries; the (N+1)th is queued. Queue overflow at
  `queue_max` drops with a WARNING and does not raise.
- **ContextVar preservation.** Set a sentinel `ContextVar` in the
  publisher's task, fire `notify_user`, assert the fake backend's
  `send` ran with that sentinel visible. Set a different sentinel on
  another concurrent publisher and assert no cross-talk between
  workers.
- **Subscribes to `notification.received` on start; unsubscribes on
  stop.**
- **`on_config_changed` hot-reloads backends** when admin config
  changes; backends not affected stay running. `max_retries=12` is
  silently capped at `MAX_RETRIES_CAP` (8).
- **`_route_passes_filters`** cases: urgency below floor, source in
  deny, source not in allow, quiet-hour wrap-around, mismatched
  user_id, DST spring-forward (2026-03-08 02:00 PT) and fall-back
  (2026-11-01 02:00 PT) inside a `22:00–07:00` window, missing user
  TZ falling back to server TZ with the expected one-time WARN.
- **`_deliver_with_retry`** with a fake backend: DELIVERED → no retry;
  REJECTED → no retry; TRANSIENT_ERROR three times → retried with
  jittered backoff (test asserts each sleep is within the expected
  `[delay*(1-jitter), delay*(1+jitter)]` window); raise → logged, no
  retry; URGENT exhaustion → ERROR log + `notify_user` call with
  `source="push_failure"`; provider-supplied `retry_after_s` is used
  in place of configured backoff and capped at 60s.
- **No dedup map regression.** Confirm `self._delivered` does not
  exist; only the v1.1 outbox is the dedup token.
- **Multi-user isolation.** Two notifications for two users
  dispatched in the same event-bus tick — neither sees the other's
  routes; verified by scenario test that runs `_on_notification` for
  both concurrently with `asyncio.gather` and inspects per-user
  delivery records.
- **WS RPCs.** list / create / update / delete / test / test_unsaved
  all owner-scoped via the single helper, rejecting mismatched
  `user_id`. `push.routes.test` debounces within
  `test_debounce_s` window. Admin-on-other-user test rejected by
  default. `push.sources.list` returns distinct sources from the last
  30 days, scoped to the calling user.

Plugin-level (per backend):

- Registry: `PushNotificationBackend.registered_backends()["<name>"]`
  returns the class after side-effect import.
- `destination_params()` returns the documented fields.
- `send` happy-path returns DELIVERED.
- 5xx → TRANSIENT_ERROR.
- 4xx (auth) → REJECTED.
- Missing required destination field → REJECTED with no HTTP call.
- `test_connection` action returns ok on a mocked happy-path response.
- **Credential-scrubbing test** (engineering review §8): mock the
  HTTP client to raise an exception whose `str()` contains the bot
  token / webhook URL / Bearer header. Assert the resulting
  `PushDeliveryResult.message` and any `logger.error`/`logger.exception`
  text contain `<redacted>` and not the original credential.
- **Discord SSRF guard**: `webhook_url` outside the allowed prefix
  list → REJECTED with no HTTP call.
- **Discord test message uses `flags=4096`** when `message.source ==
  "test"`.
- **Discord 429 honors `X-RateLimit-Reset-After`** in
  `retry_after_s`.
- **ntfy `test_connection` requires explicit topic** (no default).
- **Telegram webhook-mode rejection**: `getWebhookInfo` with
  non-empty `url` → backend stays DISABLED; `send` returns DISABLED.
- **Telegram bot username surfaced via `runtime_data`** but token is
  not.
- **Telegram `discover_chat_id`** action returns the parsed chat list
  in `data["chats"]`.

Integration (end-to-end): with a real `NotificationService`, a real
`InMemoryEventBus`, and a fake backend, calling
`notification_svc.notify_user(...)` results in `fake_backend.send(...)`
being called once with the expected `PushDestination` and `PushMessage`.
**Two integration tests** demonstrate the publisher unblocking:
(a) `notify_user` returns within 100ms even when the backend's `send`
sleeps 5s; (b) the WS dispatcher sees the in-app notification before
the worker's HTTP call completes.

## Security notes

- Bot tokens, app tokens, and Pushover user keys go through `ConfigParam`
  with `sensitive=True` so the Settings UI masks them.
- Per-user `destination_data` is read at delivery time and never logged
  in full. The delivery worker logs `route_id` and `backend_name`, never
  the destination payload.
- **Credential scrubbing in `PushDeliveryResult.message`.** Backends
  MUST funnel exception text through `_safe_repr(exc)` before placing
  it in `result.message` or any `logger.error`/`logger.exception` call.
  `_safe_repr`'s regex strips Bearer tokens, `/bot<token>/` URL paths,
  Discord webhook tokens, and `?token=` query params. The unit test
  for each backend asserts the secret never appears in the result
  surface.
- **`PushDeliveryResult.message` is a status line only.** Backends
  MUST NOT include `resp.text` in `message` (Discord and Telegram echo
  URL+token in their 4xx bodies). `f"HTTP {status}"` is enough; the
  detailed body, scrubbed, may go to `logger.debug`.
- **Backend init logs.** On `initialize` failure, log
  `backend_name` and the exception **type**, not the exception's
  stringified args — httpx errors include the URL by default.
- **No `destination_data` snapshots in the v1.1 outbox row.** The
  outbox stores `notification_id`, `route_id`, `backend_name`, status,
  attempts, and a scrubbed `error_message` only.
- Route RPCs validate `conn.user_ctx.user_id` against the row's
  `user_id` via the single `_authorize_route_access` helper.
  `acl_collections` is the source of truth for "admin sees all" via
  the entities page; the WS layer is the trust-boundary backstop.
- **Admin testing other users' routes** is **denied** by default
  (engineering review §13). `push.routes.test` rejects when
  `conn.user_ctx.user_id != row.user_id`, even for admins.
- Discord webhook URLs encode their secret in the path. The Routes UI
  masks them with `sensitive=True` on `webhook_url`. **The webhook URL
  is also prefix-validated** in `send` to prevent SSRF: only
  `https://discord.com/api/webhooks/` and
  `https://discordapp.com/api/webhooks/` are accepted.
- The `default_deep_link_origin` config is admin-only; users cannot
  inject arbitrary URLs into outgoing messages — links are derived
  server-side from `source_ref` shape, not user-supplied.

## Architecture-checklist compliance

Verified against `.claude/memory/memory-architecture-checklist.md`:

- **Layer rules.** `interfaces/` imports nothing outside `interfaces/`
  and stdlib. `core/services/push_notifications.py` imports `interfaces/`
  only and the existing `core/services/_backend_actions.py` helper.
  Plugins import only `gilbert.interfaces.*`.
- **No concrete imports.** Backends discovered via
  `PushNotificationBackend.registered_backends()`. Capability access
  uses `ConfigurationReader`, `EventBusProvider`, `StorageProvider`,
  `AccessControlProvider`, and `NotificationProvider` — never concrete
  service classes.
- **No business logic in web routes.** Routes UI talks to the service
  via WS RPCs; the service computes filters, builds `PushMessage`, and
  selects backends.
- **AI prompts.** This service has no AI prompts. The single user-
  facing string (`test_message_body`) is exposed as a `ConfigParam`
  (not `ai_prompt=True`) for operator override / localization.
- **Multi-user isolation.** No `_current_*` / `_active_*` / `_pending_*`
  on the service. The fan-out queue holds in-flight jobs but each
  `_FanOutJob` carries its own data; no per-user slot. There is no
  in-process dedup map. Concurrent `asyncio.Task`s for per-route
  delivery use `context=job.context.copy()` so siblings don't clobber
  ContextVars; the parent context is captured once on the bus
  publisher's task.
- **Slash commands.** All four AI tools declare `slash_command` **and
  `slash_help`**; the service sets `slash_namespace = "notify"` so
  they collapse under `/notify *`.
- **Documentation freshness.** Implementation must update
  `README.md` (root) configuration table and `std-plugins/README.md`
  (the four new plugin sections + capability table) in the same commit
  that adds the code.
- **Memories.** Implementation creates
  `.claude/memory/memory-push-notification-service.md` (and updates
  `MEMORIES.md`) **in the same commit** that introduces
  `core/services/push_notifications.py` (engineering review §15).
  The existing `memory-notification-service.md` is unchanged because
  `NotificationService` is unchanged; a one-line cross-reference at
  the bottom of that memory file pointing at the new one is the only
  edit.

## Dependencies

Before this spec can land, confirm the user-profile schema exposes a
`timezone` field (used by quiet-hour fall-through). If not present,
the PR-1 author must add it to the user-profile interface and
migration in the same PR. This is small (one optional string field on
the user record) but it is the only cross-feature dependency.

## Implementation order (two PRs)

### PR-1 — "ntfy in 90 seconds"

1. (If missing) `users.<id>.timezone` field on the user-profile
   schema.
2. `interfaces/push_notifications.py` + interface unit tests
   (registry, classmethod-vs-instance enforced by mypy +
   `inspect.isfunction`).
3. `core/services/push_notifications.py` skeleton: lifecycle, queue +
   worker pool, bus subscriber, filter helpers, `_deliver_with_retry`,
   `_safe_repr`, `on_config_changed`, owner-scoping helper.
   `FakePushBackend` lives in `tests/unit/core/`. Service tests pass
   green **before** wiring into `app.py`.
4. `std-plugins/ntfy/` end-to-end (simplest, no auth on the public
   server).
5. Wire registration in `app.py` after the service tests pass.
   Integration test against the real `NotificationService` and real
   `InMemoryEventBus`.
6. WS RPCs (list/create/update/delete/test/test_unsaved/backends.list/
   sources.list) + the four AI tools (delete via UI block).
7. Frontend `NotificationRoutesPage`, `RouteForm`, `RouteList`,
   `ChatIdWizard` (yes — even though only Telegram uses it, the
   component is generic and lives in PR-1 so PR-2 doesn't touch the
   SPA).
8. Bell-dropdown footer link + `/notifications` page header button to
   `/account/notifications`.
9. README + memory updates **in the same commit** as the service
   landing.

### PR-2 — "the other three providers"

10. `std-plugins/pushover/` (3-line plugin.py + backend + tests).
11. `std-plugins/discord-webhook/` with prefix validation,
    `flags=4096` test path, `Retry-After` honored.
12. `std-plugins/telegram/` (with `getMe` username caching + webhook-
    mode rejection). The wizard component is already shipped in PR-1
    and lights up automatically once the Telegram plugin's
    `discover_chat_id` action is registered.
13. README updates for the three new plugins. Final
    architecture-checklist sweep across both PRs.

## Revision Log — Round 2

Each entry: `[review item] → change`.

**Engineering blockers:**

- `[eng §1 BLOCKER]` Replaced inline `await asyncio.gather(...)` in
  `_on_notification` with a bounded `asyncio.Queue` and N background
  workers. Bus subscriber now returns immediately. New "Dispatch
  architecture" section. Two integration tests added: publisher
  unblocks within 100ms, WS dispatcher sees in-app notification before
  worker HTTP completes.
- `[eng §2 BLOCKER]` Named the delivery guarantee: v1 = at-most-once,
  v1.1 = at-least-once via the `push_notification_deliveries` outbox.
  Outbox schema documented; `notifications.<id>.external_delivery_attempted_at`
  field added in v1 for production audit.
- `[eng §3 REQUIRED]` Corrected the `contextvars.create_task` claim
  (it does copy by default; the issue is sibling mutation, not loss).
  Capture the context once on the publisher's task into the
  `_FanOutJob`; spawn per-route tasks with
  `context=job.context.copy()`. Added a sentinel-ContextVar test.
- `[eng §4 REQUIRED]` Removed the `self._delivered` in-process dedup
  map. v1 has no dedup map; v1.1 outbox row is the dedup token.
  Test added asserting the field doesn't exist.
- `[eng §5 REQUIRED]` Added retry jitter (`retry_jitter_pct`,
  default 0.10), provider-aware `Retry-After` via
  `PushDeliveryResult.retry_after_s`, capped at 60s service-side.
  URGENT exhaustion → ERROR + in-app `notify_user` to operator with
  `source="push_failure"`.
- `[eng §6 REQUIRED]` Quiet-hour timezone fall-through: route
  `quiet_hours_timezone` → user-profile `timezone` → server tz with
  WARN. `_in_quiet_hours` uses `zoneinfo.ZoneInfo` for DST-correct
  wall-clock comparison; tests for 2026-03-08 / 2026-11-01 added.
  Bounds persisted as `Optional[str]`, not empty strings.
- `[eng §7 REQUIRED]` Discord test-connection: server-side debounce
  (per-route 30s default), `flags=4096` to suppress channel
  notifications on tests. Service-level `test_connection` requires
  explicit `webhook_url` in payload.
- `[eng §8 REQUIRED]` Added `_safe_repr` helper. Backends MUST scrub
  exception text. `PushDeliveryResult.message` documented as status-
  line-only. Backend init logs use exception type, not stringified
  args. v1.1 outbox forbids `destination_data` snapshots and
  secret-bearing provider message ids.
- `[eng §9 REQUIRED]` Log levels specified: success INFO, REJECTED
  WARNING, transient retries DEBUG, retries-exhausted WARNING for
  NORMAL/INFO, ERROR for URGENT. Backend init failures ERROR.
- `[eng §10 REQUIRED]` Added `on_config_changed` hook spec. Promoted
  Round-1 open-question "hot-reload after credential change" into v1.
  `max_retries` capped server-side at `MAX_RETRIES_CAP=8`. Test added.
- `[eng §11 NICE]` Promoted `test_message_body` to a `ConfigParam`
  (operator override / localization).
- `[eng §12 NICE]` Discord webhook URL prefix validation (SSRF
  guard). ntfy `test_connection` rejects empty topic instead of
  defaulting to `gilbert-test`. Telegram webhook-mode rejection on
  `initialize`. `provides:` capability tags marked advisory.
- `[eng §13 NICE→REQUIRED]` Single `_authorize_route_access` helper
  used by every RPC. Admin testing other users' routes denied by
  default.
- `[eng §14 NICE]` Storage `_id` (not `id`) convention documented.
- `[eng §15 NICE]` Memory updates moved to the same commit as the
  service landing, not deferred to a final step.

**Architect blockers/important:**

- `[arch #1]` Telegram `discover_chat_id` routed through existing
  `config.action.invoke` RPC, not a bespoke
  `push.backends.telegram.discover_chat_id` frame. Frontend special-
  cases the action *key* on `data={"chats": [...]}`, not the RPC
  frame name.
- `[arch #2]` ABC docstring now explicitly distinguishes classmethod
  vs instance method binding and explains why each is which.
- `[arch #3]` Helper choice spelled out: neither
  `merge_backend_actions` nor `all_backend_actions` matches the N-
  concurrent-backend case. Inlined a 5-line merge in
  `config_actions()`.
- `[arch #4]` `acl_collections` seed step added explicitly to the
  Lifecycle.
- `[arch #5]` Every AI tool gets a `slash_help` string.
- `[arch #6]` "Backend-declared route" section heading reworded to
  "SPA-declared route, backend-driven schema."
- `[arch #7]` `ChatIdWizard.tsx` (renamed from `TelegramSetupWizard`)
  is backend-agnostic, driven by action result shape; lives in PR-1.

**Product important:**

- `[prod P1]` Empty-state hero flow with "Quick setup: ntfy on my
  phone" + random topic + QR code + auto-test. New top-level
  subsection.
- `[prod P2]` Reframed AI tools section: the load-bearing AI tool is
  the existing `notify_user` (which now transparently fans out via
  the bus subscriber); route-management tools are polish.
- `[prod P3]` `push.sources.list` RPC; SPA source filter is dynamic
  per-user, not hard-coded.
- `[prod U1]` Telegram chat-id wizard promoted to its own subsection
  with 5-step wizard table, `Open Telegram` deep link, and component
  rules.
- `[prod U2]` `quiet_hours_timezone` defaults to browser tz, hidden
  under "Advanced" disclosure.
- `[prod U3]` UI copy: "Send when urgency is at least:" instead of
  "Urgency floor."
- `[prod U4]` ntfy gets a "Recommended" badge in the dropdown for
  new users.
- `[prod U5]` Bell-dropdown footer link + `/notifications` page
  header button to `/account/notifications`.

**Product nits/medium:**

- `[prod S1]` `runtime_data` field on `push.backends.list` entries;
  Telegram populates `bot_username`.
- `[prod S2]` `push.routes.test_unsaved` RPC; "Send test" works on
  unsaved form values.
- `[prod S3]` Per-route `last_delivered_at` field + UI chip ("Last
  delivered: 2 hours ago" / "never"); in-memory write-through.
- `[prod S4]` Two-PR rollout (ntfy-first); explicit per-PR file lists
  in "Implementation order."
- `[prod S5]` ntfy `server` field UI shows the resolved admin default
  inline when blank.
- `[prod S6]` Removed dead `supports_attachment()` capability hook.
- `[prod S7]` AI `delete_notification_route` returns a UI block with
  "Confirm delete" instead of executing immediately.
- `[prod S8]` UI copy: "Only deliver from these sources:" / "Never
  deliver from these sources:" instead of `source_allow` /
  `source_deny`.

**Resolved Round-1 open questions** (no longer open):
audit log → v1.1 mandatory; cross-process dedup → falls out of
outbox; backend hot-reload → v1 via `on_config_changed`.

**Conflicts / remaining open questions** (none load-bearing):
- Architect implied `acl_collections` admin override is sufficient;
  engineering wanted both ACL + WS RPC checks. Resolved by keeping
  both, with ACL as source of truth and a single `_authorize_route_access`
  helper as defense-in-depth backstop. Not an open question; documented.
- Engineering and product both flagged the v1 audit story; resolved
  by promoting the outbox to v1.1 with a documented timeline rather
  than v2 punt.
