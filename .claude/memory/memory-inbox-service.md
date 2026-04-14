# Inbox Service

## Summary
Multi-mailbox email service. Every mailbox is owned by a user and can be
shared with individual users and/or roles; the service runs one
`EmailBackend` runtime per `poll_enabled` mailbox, persists messages in
`inbox_messages` (tagged by `mailbox_id`), and flushes queued outbound
drafts from an `inbox_outbox` collection via a shared outbox tick.
Authorization is centralized in `interfaces/inbox.py`.

## Details

### Data model

Three entity collections, all owned by `InboxService`:

| Collection | Key fields |
|---|---|
| `inbox_mailboxes` | `id`, `name`, `email_address`, `backend_name`, `backend_config`, `owner_user_id`, `shared_with_users`, `shared_with_roles`, `poll_enabled`, `poll_interval_sec`, `created_at` |
| `inbox_messages` | `mailbox_id` (required), `message_id`, `thread_id`, `sender_email`, `subject`, `body_text`, `body_html`, `date`, `is_inbound`, `in_reply_to` |
| `inbox_outbox` | `id`, `mailbox_id`, `status`, `send_at`, `draft`, `created_by_user_id`, `sent_at`, `error`, `retry_count` |

Indexes: `inbox_mailboxes(owner_user_id)`, compound
`inbox_messages(mailbox_id, thread_id/date/sender_email)`,
`inbox_outbox(mailbox_id, status, send_at)` and
`inbox_outbox(created_by_user_id, status)`. Thread IDs are only unique
per-mailbox (Gmail thread ids aren't globally unique), so every thread
query must be scoped to a mailbox.

### Authorization

Single rule, in `interfaces/inbox.py`:

- `can_access_mailbox(user_ctx, mailbox, *, is_admin)` — admin OR owner
  OR user in `shared_with_users` OR any role overlap with
  `shared_with_roles`. Grants read + send-as + outbox management.
- `can_admin_mailbox(user_ctx, mailbox, *, is_admin)` — admin OR owner
  only. Gates mailbox settings, share edits, delete.
- `determine_access(user_ctx, mailbox, *, is_admin)` — returns the
  `MailboxAccess` tag (`owner`/`admin`/`shared_user`/`shared_role`) for
  UI grouping. Owner precedence wins over admin.

Callers resolve `is_admin` via `AccessControlProvider.get_effective_level`
and pass it in — the helpers are pure and never touch the capability
resolver.

**Outbox cancellation**: any user with `can_access_mailbox` can cancel
any draft in that mailbox, not just the creator. Rationale: full access
means full control over outbound.

**Sharing semantics** (decided in this PR): shared = full access.
Shared users read, send, and reply through the mailbox — only owner/
admin can edit settings and sharing. If a finer split becomes necessary
later (viewer vs member), `shared_with_users` can become a list of
objects instead of strings.

### Runtime lifecycle

`InboxService` holds a `dict[mailbox_id, _MailboxRuntime]` registry.
Each runtime owns one `EmailBackend` instance and one scheduler job
`inbox-poll-{mailbox_id}`. On `start()`:

1. Schedule a one-shot `inbox-boot` job that calls `_boot_runtimes`
   (non-blocking per CLAUDE.md — backend `initialize()` can hit the network).
2. Register a recurring `inbox-outbox-tick` job (every 10 seconds).

`_boot_runtimes` loads every `poll_enabled` mailbox row and calls
`_start_runtime(mailbox)` for each. Create/update/delete of a mailbox
start/stop/restart the relevant runtime in place — no Gilbert restart
needed.

`update_mailbox` restarts the runtime only when a runtime-affecting
field changes (`backend_name`, `backend_config`, `poll_enabled`,
`poll_interval_sec`, `email_address`). Share edits don't trigger a
restart.

### Outbox

Drafts are persisted as `inbox_outbox` rows with a `status` state machine
`pending → sending → sent/failed/cancelled`. The shared tick runs every
10s, queries for pending rows with `send_at <= now`, transitions each to
`sending`, resolves knowledge-store attachments just in time, calls
`backend.send()`, and transitions to `sent` (persisting a row in
`inbox_messages` too) or `failed`. Events `inbox.outbox.sent` and
`inbox.outbox.failed` carry `mailbox_id` and `outbox_id`.

`send_message()` and `reply_to_message()` are **synchronous bypass paths**
that call `backend.send()` directly — used for "send now" flows like the
AI `inbox_send` tool. The outbox is for *delayed* or *crash-resilient*
queuing, used by plugins (e.g. the sales assistant).

### UserContext threading

- **Reads** (`search_messages`, `get_message`, `get_thread`, `get_stats`,
  `list_outbox`) use `gilbert.core.context.get_current_user()` — no
  explicit parameter. The WS frame dispatch and the AI tool dispatch
  both call `set_current_user(conn.user_ctx)` / `set_current_user(user_ctx)`
  before invoking handlers.
- **Mutations** (`schedule_send`, `send_message`, `reply_to_message`,
  mailbox CRUD, sharing) take `user_ctx: UserContext` as an explicit
  parameter so the actor is unambiguous at the call site and unit tests
  can pass it directly.

### Events published

All events carry `mailbox_id` in their data.

- `inbox.message.received` — new inbound/outbound message persisted during polling
- `inbox.message.replied` — direct reply-to-message sent
- `inbox.message.sent` — direct new-compose sent
- `inbox.outbox.sent` — outbox tick successfully flushed a draft
- `inbox.outbox.failed` — outbox tick send raised; row transitioned to FAILED
- `inbox.mailbox.created`, `inbox.mailbox.updated`, `inbox.mailbox.deleted`
- `inbox.mailbox.shares.changed` — fires on any share_user/unshare_user/share_role/unshare_role

Event visibility: `interfaces/acl.py` sets the `inbox.` prefix to level
100 (user). The WS fanout filter adds a per-event mailbox-access check
on top of this by having the frontend maintain a cache of accessible
mailbox ids and invalidating on `inbox.mailbox.shares.changed` and
`auth.user.roles.changed`. The auth event is dispatched via a dedicated
`can_see_auth_event` filter in `ws_protocol.py` that restricts delivery
to the affected user plus admins.

### InboxProvider protocol

`interfaces/inbox.py::InboxProvider` is a `@runtime_checkable` Protocol
that plugins use via `resolver.get_capability("inbox")` +
`isinstance`. Covers:

- `schedule_send`, `cancel_outbox`, `list_outbox`
- `get_message`, `get_thread`, `search_messages`
- `get_mailbox`, `list_accessible_mailboxes`

Plugins must never import `gilbert.core.services.inbox.InboxService`
directly.

### AI tools

All inbox tools now take a required `mailbox_id` first parameter (no
default mailbox concept). The AI calls `inbox_mailboxes` first to
discover accessible mailbox ids when the user's intent doesn't already
name one.

- `inbox_mailboxes` — list mailboxes the caller can access, with
  access-type tag. Slash: `/inbox mailboxes`
- `inbox_search` — search persisted messages in one mailbox
- `inbox_read` — full content of one message
- `inbox_reply` — threaded reply (body_html required — no slash command)
- `inbox_send` — new compose (body_html required — no slash command)

Forbidden or missing `mailbox_id` calls return a clear error telling
the AI/user to call `/inbox mailboxes` first.

### WS RPCs

Messages / stats / outbox (all optionally filtered by `mailbox_id`;
aggregated over caller's accessible mailboxes when omitted):

- `inbox.stats.get`, `inbox.message.list`, `inbox.message.get`,
  `inbox.thread.get`
- `inbox.outbox.list`, `inbox.outbox.cancel`

Mailbox CRUD + sharing (all gated by `can_admin_mailbox`):

- `inbox.mailboxes.list`, `get`, `create`, `update`, `delete`,
  `test_connection`
- `inbox.mailboxes.share_user`, `unshare_user`, `share_role`, `unshare_role`

Backend discovery for the UI:

- `inbox.backends.list` — returns registered `EmailBackend`s and their
  `backend_config_params()` schemas so the mailbox editor can render
  backend-specific credential fields dynamically.

### Frontend

`/inbox` page has a mailbox sidebar (grouped Mine / Shared with me /
All-for-admins), a mailbox header with inline "Settings" button for
admins, an outbox panel that shows non-terminal drafts for the selected
mailbox with cancel buttons, and the message list / thread dialog from
the old UI. The mailbox edit drawer uses the shared `ConfigField`
component to render backend-specific credential fields dynamically from
the `inbox.backends.list` schema.

The page subscribes to `inbox.mailbox.*`, `inbox.mailbox.shares.changed`,
`auth.user.roles.changed` (filtered to the current user), `inbox.message.received`,
`inbox.outbox.sent`, and `inbox.outbox.failed` to invalidate the relevant
react-query caches.

### Bootstrap YAML

None. The `inbox` section was removed from `gilbert.yaml` entirely —
all inbox configuration lives in entity storage. The service's
`config_params()` exposes only `max_body_length` as a global setting;
everything else lives on individual mailbox records.

### Design decisions

- **No default mailbox anywhere.** Plugins that need to send mail
  configure an explicit `mailbox_id` in their own config. AI tools
  require `mailbox_id` on every call. Avoids ambiguity about "which
  inbox am I operating on right now."
- **Core outbox replaces per-plugin scheduling hacks.** Previously the
  sales assistant plugin reimplemented persistence + delayed send + crash
  recovery in its own `pending_replies` collection with `asyncio.sleep`
  tasks. That's now one outbox row correlated to a `outbox_links` entry
  in the plugin, with post-send bookkeeping triggered by the
  `inbox.outbox.sent` event.
- **`email_address` is on the mailbox row, not resolved from the
  backend.** Each backend could expose a `whoami()` but for v1 users
  just enter the address on mailbox create. A future optimization can
  auto-populate from backend if available.

## Related
- [Event System](memory-event-system.md) — events published by inbox
- [Scheduler Service](memory-scheduler-service.md) — per-mailbox poll jobs + outbox tick
- [Storage Backend](memory-storage-backend.md) — message/outbox/mailbox persistence
- [User & Auth System](memory-user-auth-system.md) — UserContext source
- [Access Control](memory-access-control.md) — admin level resolution
- `src/gilbert/interfaces/inbox.py` — Mailbox, OutboxDraft, auth helpers, InboxProvider
- `src/gilbert/interfaces/email.py` — EmailBackend ABC (unchanged)
- `src/gilbert/core/services/inbox.py` — InboxService with per-mailbox runtime registry
- `std-plugins/gmail/*` — GmailBackend
- `frontend/src/components/inbox/*` — multi-mailbox UI
- `tests/unit/test_inbox_service.py` — unit tests covering auth matrix, outbox lifecycle, polling isolation
- `local-plugins/current-sales-assistant/sales_service.py` — example plugin that queues via `InboxProvider.schedule_send`
