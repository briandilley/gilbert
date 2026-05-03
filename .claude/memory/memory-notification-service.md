# NotificationService

## Summary
Persists user-addressed notifications and publishes ``notification.received`` bus
events. Lives in ``src/gilbert/core/services/notifications.py``.

## Details
**Capabilities declared:** ``notifications`` (satisfies
``NotificationProvider``), ``ws_handlers``.

**Public method:** ``notify_user(*, user_id, message, urgency, source,
source_ref)`` — persists a ``Notification`` entity to the
``notifications`` collection and publishes ``notification.received``
on the bus with the entity's serialized form as ``data``.

**WS RPCs:** ``notification.list`` (filterable, returns items + unread_count),
``notification.mark_read``, ``notification.mark_all_read``,
``notification.delete``. All RBAC-checked against the calling user — you
can only see, mark, or delete your own notifications.

**Live delivery to WebSocket clients:** uses the existing event-bus →
``WsConnectionManager._dispatch_event`` flow. ``WsConnection`` has a
``can_see_notification_event`` content filter that rejects events whose
``data["user_id"]`` does not match the connection's user. There is NO
separate ``push_to_user`` helper or per-user connection registry —
existing dispatch + per-event-type filter is the established pattern.

**Indexes:** ``(user_id, read, created_at)``.

**Audible/visual signal logic** lives entirely in the frontend. The
backend stamps an ``urgency`` field (``info`` / ``normal`` / ``urgent``)
and lets the UI decide.

## Related
- ``src/gilbert/interfaces/notifications.py``
- ``src/gilbert/core/services/notifications.py``
- ``tests/unit/core/test_notification_service.py``
- ``src/gilbert/web/ws_protocol.py:can_see_notification_event``
- ``docs/superpowers/specs/2026-05-03-autonomous-agent-design.md``
- ``docs/superpowers/plans/2026-05-03-autonomous-agent-phase-3-notification-backend.md``
