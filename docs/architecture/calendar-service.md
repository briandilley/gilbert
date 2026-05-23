# Calendar Service

## Summary
Multi-account calendar service mirroring `InboxService`. Each account
is owned by a user, can be shared with users/roles, and runs one
`CalendarBackend` instance + one scheduler poll job per `poll_enabled`
account. Events are cached in `calendar_events` for fast read tools;
mutations go straight to the backend with optimistic-concurrency etag
support and idempotency keys. Eight AI tools cover read + mutating
flows; the three mutators default to a **preview/confirm `UIBlock`**
flow so an AI never silently fires real invites.

## Details

### Data model

Three entity collections, all owned by `CalendarService`:

| Collection | Key fields |
|---|---|
| `calendar_accounts` | `id`, `name`, `email_address`, `backend_name`, `backend_config`, `calendar_id`, `timezone`, `working_hours_start_hour`, `working_hours_end_hour`, `owner_user_id`, `shared_with_users`, `shared_with_roles`, `poll_enabled`, `poll_interval_sec`, `upcoming_event_lookahead_minutes`, `health` (`ok`/`unhealthy`), `last_error`, `last_error_at`, `created_at` |
| `calendar_events` | `_id = "{account_id}:{event_id}"`, `account_id`, `event_id`, `calendar_id`, `title`, `start`, `end`, `start_utc_iso`, `end_utc_iso`, `all_day`, `etag`, `status`, `transparency`, `attendees_json`, `organizer_email`, `location`, `description`, `html_link`, `recurring_event_id`, `visibility` |
| `calendar_event_announcements` | `_id = "{account_id}:{event_id}"`, `account_id`, `event_id`, `start_iso`, `announced_at` — dedup for `calendar.event.upcoming` so a process restart never re-fires |

Indexes: `calendar_accounts(owner_user_id)`,
`calendar_events(account_id, start_utc_iso)`,
`calendar_events(start_utc_iso)`, `calendar_events(end_utc_iso)`,
`calendar_event_announcements(account_id, start_iso)`. The fetch and
trim windows for `calendar_events` are deliberately **identical**
(`now − effective_cache_back_hours .. now + default_event_lookahead_days`)
so the cache never holds rows the next poll wouldn't return.
`effective_cache_back_hours` is `max(cache_back_hours, 336)` so older
stored configs cannot shrink the cache below the two-week window needed
by the agenda and recent schedule views.

**`start` / `end` vs `start_utc_iso` / `end_utc_iso`** — `start` and
`end` are the original-tz ISO strings (e.g. `"2026-05-09T22:00:00-08:00"`)
kept for human-readable round-trip via `_event_row_to_event`. **All
filter / sort queries use `start_utc_iso` / `end_utc_iso`** because
string-comparing mixed-offset ISOs is not order-preserving (a PST event
later than a UTC event lex-sorts as earlier). Pre-Cal-revise rows
without UTC columns will not match the new queries — they'll get
overwritten on the next poll cycle that touches them.

### Authorization

Single rule, in `interfaces/calendar.py`. `is_admin` is **derived
inside the helpers** from the `UserContext` (admin iff `"admin" in
user_ctx.roles` or `user_ctx is UserContext.SYSTEM`). Callers must
never pass an ad-hoc bool.

- `can_access_account(user_ctx, account)` — admin OR owner OR user in
  `shared_with_users` OR role overlap with `shared_with_roles`. Grants
  read + create_event + free/busy.
- `can_admin_account(user_ctx, account)` — admin OR owner only. Gates
  settings, share edits, and delete.
- `determine_access(user_ctx, account)` — returns the `CalendarAccess`
  tag (`owner`/`admin`/`shared_user`/`shared_role`) for UI grouping.
  Owner > admin > shared_user > shared_role precedence.

### Runtime lifecycle

`self._runtimes: dict[account_id, _AccountRuntime]` keyed by account.
Each runtime owns one backend + one `calendar-poll-{account_id}`
scheduler job. `_AccountRuntime` carries:

- `last_seen_event_ids: set[str]` — diffed against fresh fetches.
  **Lazy-seeded from the persisted `calendar_events` cache on the
  first poll after restart** so a restart doesn't re-publish every
  cached event as `calendar.event.created`.
- `last_seen_event_snapshots: dict[event_id, dict]` — minimum field
  set for diffing (title/start/end/location/description/status/
  attendees), so cosmetic etag/html_link changes don't fire spurious
  `calendar.event.updated`.
- `recent_mutate_publishes: dict[event_id, monotonic]` — the next
  poll diff suppresses republication for ids in this map within
  `mutate_publish_dedup_sec` (default 60). Every successful
  `create_event` / `update_event` / `delete_event` records before
  publishing, so the same logical mutation doesn't fire twice.
- `consecutive_failures: int` — drives the `health` flip after
  `unhealthy_failure_threshold` failures.

`_start_runtime` applies a **mandatory cold-start jitter** of
`random.uniform(0, min(poll_interval_sec, 120))` on the first fire so
N runtimes don't synchronously hit the backend on startup.

### Polling logic

Per `_poll_runtime`:

1. Lazy seed `last_seen_event_ids` from cache if first run.
2. `backend.list_events(now − effective_cache_back_hours, now + lookahead_days)`
   wrapped in `aggregation_timeout_sec`. Auth/notfound errors trigger
   the unhealthy flip after threshold; other errors bump
   `consecutive_failures`.
3. **Filter cancelled events out of `fresh` BEFORE the diff** so a
   cancellation surfaces as a "missing" id and emits
   `calendar.event.deleted` exactly once.
4. Diff `fresh` ids vs `last_seen_event_ids`, suppressing any id in
   `recent_mutate_publishes`. New ids → `calendar.event.created`,
   missing ids → `calendar.event.deleted`, same id with changed
   summary fields → `calendar.event.updated`.
5. Upsert all `fresh` rows into `calendar_events`; delete missing.
6. Run `_emit_upcoming_for_account` — fire `calendar.event.upcoming`
   for events within `upcoming_event_lookahead_minutes` that don't
   have an existing announcement row.
7. Reset failures and (if previously unhealthy) flip `health` back to
   `ok` and emit `calendar.account.health_changed`.

The `calendar-announcement-sweep` recurring job (every 30 min) reaps
stale announcement rows older than 48h and `calendar_events` rows
older than `effective_cache_back_hours` — entity storage has no TTL primitive.

### Mutations

- `create_event` — computes a deterministic
  `idempotency_key = sha256(account_id|title|start|end|sorted_attendees)[:32]`
  when caller omits one. Backends forward (Google: `requestId`)
  so a retry returns the original event instead of duplicating.
- `update_event` — **requires** non-empty `if_match_etag`; passing an
  empty string raises `ValueError`. Without that hard requirement, a
  service-side fresh `get_event` would always match the server etag,
  defeating the whole point of OCC and silently masking writes that
  lost the race. The AI tool's preview/confirm flow stashes the etag
  on the hidden `pending_arguments` field of the preview UIBlock as
  `_etag` (and the merged update overlay as `_merged`); the confirm
  leg pulls them back out and threads them straight into
  `update_event` instead of re-reading. On `CalendarBackendConflictError`
  (Google 412), the service raises through to the caller for
  refresh+retry. The recurring-instance audit log fires on the result
  side (we know `evt.recurring_event_id` from the backend response).
- `delete_event` — sends `sendUpdates="all"` only when
  `send_cancellations=True`.

### Backend backoff signals

`_on_poll_failure` consumes the typed taxonomy:

- `CalendarBackendRateLimitError(retry_after_sec=…)` — sets
  `runtime.next_poll_allowed_at = monotonic + retry_after_sec`. The
  poll callback skips its API call until that point, so a 429 from
  Google (carrying `Retry-After`) actually defers the next attempt
  instead of hammering at the configured cadence.
- `CalendarBackendTransientError` / `TimeoutError` — exponential
  backoff (`2 ** consecutive_failures` seconds) capped at the
  account's `poll_interval_sec`.

A successful poll resets `consecutive_failures` and clears
`next_poll_allowed_at`.

Naive datetimes coming from tool args are localized to
`ZoneInfo(account.timezone)` at the tool→service boundary. Account
timezone is validated on write (rejects unknown IANA zones); same
check on `working_hours_start_hour < working_hours_end_hour`.

### CalendarProvider capability protocol

`@runtime_checkable` `CalendarProvider` in `interfaces/calendar.py`.
Every method takes `user_ctx: UserContext` explicitly — the spec rule
from `memory-multi-user-isolation.md`. Aggregate reads (account_id=
None) fan out concurrently via `asyncio.gather` with a per-runtime
timeout; failures surface as warnings on the `AggregatedEvents`
envelope, not exceptions.

### AI tools (8)

| Name | Slash | Mutating |
|---|---|---|
| `list_calendar_accounts` | `/calendar accounts` | no |
| `get_schedule` | `/calendar schedule` | no |
| `next_event` | `/calendar next` | no |
| `get_event` | (no slash) | no |
| `find_free_time` | (no slash) | no |
| `create_event` | (no slash) | yes |
| `update_event` | (no slash) | yes |
| `delete_event` | (no slash) | yes |

Mutating tools default `confirm=False` and return a `ToolOutput` with a
preview `UIBlock` (Confirm/Cancel buttons). The shared
`confirm_or_execute` helper at
`src/gilbert/core/services/_ui_blocks.py` owns this branching so every
mutating tool produces the same shape. `send_invites` defaults to
`False` at the tool layer, so even on confirm, no third party gets
emailed unless the AI explicitly opts in.

### Events published

All carry `account_id` in `data`.

- `calendar.event.upcoming` — fires from the poll when an event enters
  the per-account `upcoming_event_lookahead_minutes` window.
- `calendar.event.created` / `updated` / `deleted` — fired by the
  poll diff AND by the mutate-path publish, with the dedup window.
- `calendar.account.created` / `updated` / `deleted` /
  `shares.changed` / `health_changed` — same shape as inbox.

Event visibility prefix: `calendar.` at level 100 (user) in
`interfaces/acl.py`. The WS layer's per-event account-access filter
adds the per-account narrowing on top — same mechanism inbox uses.

### WS RPCs

- `calendar.accounts.{list,get,create,update,delete,test_connection,probe_calendars,reveal_backend_config,share_user,unshare_user,share_role,unshare_role}`
- `calendar.events.{list,get,create,update,delete}`
- `calendar.freebusy.get`
- `calendar.find_free_time`
- `calendar.backends.list`

`calendar.events.update` requires a non-empty `if_match_etag` frame
field (returns 400 otherwise). The SPA reads it from the cached event
before submitting the form.

`calendar.find_free_time` returns both `slots` and `warnings`. The
service's `find_free_time` returns `FreeTimeResult(slots, warnings)`
so cross-attendee free/busy probe failures (the most common partial-
failure mode — colleague's calendar isn't shared with the requester)
surface to the caller without aborting. The AI tool stringifies
warnings into its return JSON; the WS handler passes them through.

`probe_calendars` is the spec's two-phase create flow: SPA creates the
account with `poll_enabled=False`, then calls
`calendar.accounts.probe_calendars` which delegates to
`CalendarService.probe_calendars(account_id, user_ctx)` — the service
owns the lifecycle (`backend.initialize` / `list_calendars` /
`backend.close` in a `try/finally`). The previous "instantiate a
backend in the WS handler with an unsaved config blob" pattern is
explicitly avoided; that's the exact anti-pattern
`memory-backend-pattern.md` warns against.

Operator setup instructions for the Google Calendar service-account flow live in
`docs/how-to/google-calendar-setup.md`. Use that guide for personal Gmail
shared-calendar setup, Workspace domain-wide delegation setup, and common
Google API troubleshooting.

### Shared confirm/preview helper

`src/gilbert/core/services/_ui_blocks.py` was extracted as part of
this PR. It exposes `confirm_or_execute(...)` plus
`build_preview_output(...)` and `build_confirm_block(...)`. Future
features (#06 `mute_camera_alerts`, #08 health-record deletion,
future mutating tools) reuse it via:

```python
from gilbert.core.services._ui_blocks import confirm_or_execute
return await confirm_or_execute(
    confirm=bool(args.get("confirm")),
    tool_name="<tool>",
    title="<short>",
    summary="<sentence>",
    summary_lines=[...],
    arguments=args,
    execute=lambda: self._do_actual_mutation(args),
)
```

Do NOT retrofit existing inbox / music tools to use this helper — that
is a separate UX pass per `OPEN_QUESTIONS.md`'s decision lock.

### Plaintext-at-rest gap + masking

Service-account JSON in `backend_config` is `sensitive=True`. The
default `_account_payload` walks `backend_cls.backend_config_params()`
and **masks** any sensitive ConfigParam value as `"********"` before
returning it over WS — shared users and even admins get the masked
payload by default. Admins (or the owner) can re-fetch the unmasked
value via `calendar.accounts.reveal_backend_config(account_id)`,
which checks `can_admin_account` and emits an INFO audit log line
on every reveal. The SPA's `AccountEditDrawer` calls this RPC on
edit-open when the user has admin access so the form repopulates
with the live secrets instead of overwriting them with the mask.

Masking is **not** encryption — the JSON still sits in plaintext
SQLite. This is a project-wide gap inherited by every backend that
stores secrets (Gmail, Drive, Slack, Withings when shipped). Tracked
in `OPEN_QUESTIONS.md` as a deferred v2 item; the std-plugins README
documents the gap and recommends file-permission hardening.

### Multi-user state — what's on `self`

Service-lifetime only:

- `self._storage`, `self._scheduler`, `self._event_bus` — handles.
- `self._runtimes: dict[account_id, _AccountRuntime]` — keyed by
  account, not by user.
- `self._cached_accounts: list[CalendarAccount]` — replaced atomically
  on each CRUD; not the source of truth for security-sensitive reads
  (those re-query storage).
- service-level config knobs (`_default_lookahead_days`, etc.).

Per-user state lives nowhere. Public methods take `user_ctx` as an
explicit parameter; tool dispatch builds it from injected `_user_id`
/ `_user_roles` arguments.

## Related
- `src/gilbert/interfaces/calendar.py` — ABC, dataclasses, helpers, errors
- `src/gilbert/core/services/calendar.py` — `CalendarService`
- `src/gilbert/core/services/_ui_blocks.py` — shared confirm/preview helper
- `std-plugins/google/google_calendar.py` — `GoogleCalendarBackend`
- `tests/unit/test_calendar_interfaces.py` — auth matrix + dataclass round trips
- `tests/unit/test_calendar_service.py` — service tests against fake backend
- `std-plugins/google/tests/test_google_calendar.py` — backend payload + error mapping
- [Inbox Service](memory-inbox-service.md) — closest analog
- [UI Blocks](memory-ui-blocks.md) — `ToolOutput` / `UIBlock` mechanics
- [Multi-User Isolation](memory-multi-user-isolation.md) — ContextVar discipline
- [Backend Pattern](memory-backend-pattern.md) — registry + side-effect imports
