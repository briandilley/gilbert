# PushNotificationService

## Summary
Side-channel that subscribes to ``notification.received`` and fans out
each notification to the recipient user's external push routes (ntfy,
Pushover, Discord webhook, Telegram). Lives in
``src/gilbert/core/services/push_notifications.py``.
:class:`~gilbert.core.services.notifications.NotificationService` is
**not modified** — this service is purely additive.

## Details

### Capabilities
- ``push_notifications`` — the aggregator capability.
- ``ws_handlers`` — RPCs for managing per-user routes.
- ``ai_tools`` — list / create / delete / send-test tools.

### Why event-driven, not a method call on NotificationService
Subscribing to the existing bus event is purely additive: zero edits to
``NotificationService``, zero new capabilities required. The fan-out
worker can crash, hang, or be slow without affecting in-app delivery
(which goes through the same event but a different subscriber chain —
``WsConnectionManager._dispatch_event``).

### Dispatch architecture (worker pool, NOT inline gather)
``InMemoryEventBus.publish`` ``await``s every subscriber inline
(``asyncio.gather``). A naive subscriber that does I/O or per-route
fan-out blocks the publisher AND the in-app WS dispatcher. The
fan-out service therefore separates the bus subscriber from delivery:

1. ``_on_notification`` runs on the publisher's task. It does ONLY:
   sanity-check the event, snapshot ``copy_context()``, build a
   ``_FanOutJob``, and ``put_nowait`` it on a bounded ``asyncio.Queue``.
   It returns immediately so the publisher unblocks.
2. N worker tasks (default 8) drain the queue. Each worker spawns the
   per-job ``_fan_out`` as ``asyncio.Task(..., context=job.context.copy())``
   so the originating caller's ContextVars (``_user_id``,
   ``_conversation_id``, etc.) propagate. Per-route delivery tasks use
   the same trick to avoid sibling ContextVar mutation collisions.
3. On overflow ``_on_notification`` logs WARNING and drops the job.
   Back-pressure is preferable to unbounded memory growth when a
   backend is wedged.

``Context.run`` is NOT used for the async fan-out body — it only runs
sync code. Per-route tasks are spawned with ``context=...`` at the
call site.

### Delivery guarantees
**v1 is at-most-once.** Crash between publish and worker completion
drops the in-flight push; the persisted notification is unaffected.
The worker stamps ``notifications.<id>.external_delivery_attempted_at``
on the persisted row when fan-out begins so production loss can be
audited.

**v1.1 will be at-least-once via a ``push_notification_deliveries``
outbox.** The schema is documented in the spec; not implemented in v1.

### Backends
``PushNotificationBackend`` ABC in ``interfaces/push_notifications.py``
follows the universal backend pattern (``__init_subclass__`` registry +
``backend_config_params()``). std-plugins ship the concrete backends:
- ``ntfy`` (free, ntfy.sh default, custom server URL supported)
- ``pushover`` (paid one-time)
- ``discord-webhook`` (channel webhooks; SSRF-guarded URL prefix)
- ``telegram`` (bot token + ``getUpdates``-based chat-id discovery)

Method binding rules:
- ``backend_config_params``, ``destination_params``, ``backend_actions``
  are ``@classmethod``. The Settings UI and Routes UI consume them
  before any instance is initialised.
- ``initialize``, ``close``, ``send``, ``invoke_backend_action`` are
  instance methods that may rely on ``self._client`` etc.

### Per-user routes
``push_notification_routes`` collection. Each row is owned by a user
and holds:
- ``backend_name``, ``destination_data`` (per-backend dict), ``label``
- ``urgency_floor`` (info / normal / urgent)
- ``source_allow``, ``source_deny`` (lists of source tags)
- ``quiet_hours_start``, ``quiet_hours_end``, ``quiet_hours_timezone``
- ``last_delivered_at`` (best-effort UI hint)
- ``enabled``, timestamps

Quiet hours resolve effective tz in this order: route's
``quiet_hours_timezone`` → user profile ``tz`` (UserContext.tz field) →
server tz (with a one-time WARN per user). DST-correct via
``zoneinfo.ZoneInfo``; tested with 2026-03-08 spring-forward and
2026-11-01 fall-back fixtures.

### Retry policy
Bounded retry up to ``max_retries`` (default 3, capped at
``MAX_RETRIES_CAP=8``) with exponential backoff plus uniform jitter.
Backends may surface ``PushDeliveryResult.retry_after_s`` (parsed from
``Retry-After`` / Telegram ``parameters.retry_after`` / Discord
``X-RateLimit-Reset-After``); the worker prefers it over the configured
backoff (capped at 60s).

URGENT-failure exhaustion logs at ERROR AND publishes an in-app
``notification.received`` with ``source="push_failure"`` so operators
see the loss without re-implementing alerting. NORMAL/INFO exhaustion
logs at WARNING.

### Credential scrubbing
``_safe_repr(exc)`` strips ``Bearer <token>``, ``/bot<token>/`` paths,
full Discord webhook URLs, and ``?token=`` query params from any text
that flows into ``PushDeliveryResult.message`` or
``logger.error``/``logger.exception``. Backends MUST funnel exception
text through it; the per-plugin test asserts that a mocked HTTP error
containing the bot token / webhook URL / Bearer header doesn't appear
in the result message.

### WS RPCs
- ``push.routes.list`` / ``create`` / ``update`` / ``delete``
- ``push.routes.test`` (per-route, server-debounced 30s)
- ``push.routes.test_unsaved`` (form-time validation)
- ``push.backends.list`` (returns destination_params + actions +
  ``runtime_data`` per backend)
- ``push.sources.list`` (distinct ``Notification.source`` values for
  the calling user)

A single ``_authorize_route_access(conn, row, write=...)`` helper is
the trust boundary. Owner-only for writes; admins can read other users'
routes via ``push.routes.list``. Admin testing other users' routes is
denied by default (consent over debugging).

### AI tools
``slash_namespace = "notify"``. Four tools:
- ``list_my_notification_routes`` — read-only, parallel-safe.
- ``create_notification_route`` — creates a route.
- ``delete_notification_route`` — returns a Confirm/Cancel UIBlock on
  the first call (via the shared ``confirm_or_execute``-style helper);
  only ``confirm=True`` actually deletes.
- ``send_test_notification`` — honours the same per-route debounce as
  the WS RPC.

The load-bearing AI tool for the notifications domain is the existing
``notify_user`` (on ``NotificationService``); these tools are
ergonomic polish so users can ask "show me my notification routes"
or "add a Pushover route" in chat.

### Configuration
``push_notifications`` config namespace. Service-level keys:
``enabled_backends``, ``max_retries`` (capped at ``MAX_RETRIES_CAP``),
``retry_initial_delay_s``, ``retry_factor``, ``retry_jitter_pct``,
``worker_count`` / ``queue_max`` (restart_required),
``test_debounce_s``, ``test_message_body``,
``default_deep_link_origin``. Per-backend admin secrets are merged in
under ``backends.<name>.<key>`` with ``backend_param=True``.

``on_config_changed`` hot-reloads backends whose config changed
(promoted from the Round-1 v2 open-question). ``reload_backends``
config action is the manual override.

### Decisions DEFERRED to later versions
- Presence-gated delivery (``deliver_when=when_offline``) — v2.
- ``push_notification_deliveries`` outbox for at-least-once — v1.1
  (mandatory before "URGENT external delivery" can be marketed).
- Encryption-at-rest for backend secrets — separate cross-cutting PR.
- Per-source default route templates ("send all `agent` to phone") —
  v2 ergonomics.

## Related
- ``src/gilbert/interfaces/push_notifications.py``
- ``src/gilbert/core/services/push_notifications.py``
- ``tests/unit/core/test_push_notification_service.py``
- ``tests/unit/test_push_notifications_interface.py``
- [NotificationService](memory-notification-service.md) — unchanged;
  this service subscribes to its bus event.
- [Multi-backend Aggregator Pattern](memory-multi-backend-pattern.md) —
  one service + N backends, not N services.
- [Multi-User Isolation](memory-multi-user-isolation.md) — every
  ``asyncio.Task`` is spawned with ``context=...`` at the call site.
- [Backend Pattern](memory-backend-pattern.md) — universal ABC +
  registry shape this service follows.
- [UI Blocks](memory-ui-blocks.md) — the delete tool's
  Confirm/Cancel block.
- ``docs/specs/03-notification-fanout.md`` — full design spec.
