# Tasks Service

## Summary
Multi-list todo aggregator. Each task list is owned by a user and can
be shared with users / roles; the service runs one `TaskBackend`
runtime per list (local + Google Tasks ship in v1, Todoist / CalDAV
sketched at v1.1). Writes use **local-first reconciliation** — every
mutation lands in entity storage immediately with
`sync_status=pending_push`, the upstream push happens inline (bounded
by `push_timeout_sec`), and `tasks-sync-tick` retries pending rows
with exponential backoff. Soft-delete by default, hard-delete admin-
only. Single-source `summarize_today` AI tool — the greeting service
calls the same Provider method.

## Details

### Data model

Three entity collections owned by `TasksService`:

| Collection | Key fields |
|---|---|
| `task_lists` | `id`, `name`, `backend_name`, `backend_config`, `owner_user_id`, `shared_with_users`, `shared_with_roles`, `poll_enabled`, `poll_interval_sec`, `is_default`, `created_at`, `last_sync_at`, `degraded_since`, `last_error` |
| `tasks` | `_id`, `list_id`, `source_id` (server-only — backend native id; equals `_id` for local), `title`, `notes`, `due_at` (UTC `Z`), `due_at_tz` (IANA), `completed_at`, `status` (open/done/cancelled), `priority` (0–4), `tags`, `project`, `created_at`, `updated_at`, `created_by_user_id`, `idempotency_key`, `sync_status`, `last_push_attempt_at`, `last_push_error`, `retry_count`, `etag`, `deleted_at`, `due_soon_fired` |
| `task_events_seen` | `_id = "{list_id}:{source_id}"`, `last_seen_at` — dedup cursor for poll loops |

Indexes: `task_lists(owner_user_id)`,
`tasks(list_id, status)`, `tasks(list_id, due_at)`,
`tasks(list_id, source_id)`, `tasks(status, due_at)` (cross-list
aggregation for `due_today` / `overdue`),
`tasks(idempotency_key)` (pre-create dedup),
`tasks(sync_status)` (sync-tick sweep),
`tasks(list_id, deleted_at)`.

### Time zones

- Stamps (`created_at`, `updated_at`, `completed_at`,
  `last_push_attempt_at`, `last_seen_at`, `degraded_since`,
  `last_sync_at`, `deleted_at`) are **ISO UTC with trailing `Z`**.
- `due_at` is **ISO UTC with trailing `Z`** paired with `due_at_tz`
  (IANA name) so day-boundary math respects the user's wall clock.
- `due_today` / `overdue` use the **requesting user's TZ** from
  `UserContext.tz` (typed field landed in feature 03 — feature 05
  reads it directly, no `metadata["tz"]` workaround).

### Authorization

Single rule, in `interfaces/tasks.py`:

- `can_access_list(user_ctx, task_list, *, is_admin)` — admin OR owner
  OR user in `shared_with_users` OR role overlap with
  `shared_with_roles`. Grants read + add + complete + update.
- `can_admin_list(user_ctx, task_list, *, is_admin)` — admin OR owner
  only. Gates settings, share edits, delete.
- `determine_access(user_ctx, task_list, *, is_admin)` — returns the
  `ListAccess` tag for UI grouping. Owner > admin > shared_user >
  shared_role precedence.

Service resolves `is_admin` via `AccessControlProvider.get_effective_level`
and threads it through. `_require_access` / `_require_admin` raise
`TaskListPermissionError` on violation.

### Backend ABC + built-ins

`TaskBackend` ABC in `interfaces/tasks.py` follows the universal
backend pattern (`__init_subclass__` registry, `backend_name`,
`backend_config_params()`, `initialize` / `close`, `list_tasks`,
`add_task`, `update_task(source_id, patch, *, etag="")`,
`complete_task`, `delete_task`).

`update_task` is **patch-shaped** (not full-Task-shaped): backends
issue PATCH semantics so the user's mobile edits to other fields
survive. CalDAV uses `etag` for `If-Match`; Google Tasks ignores it.
Backends that detect stale-etag mismatch raise
`TaskBackendConflictError` so the service can re-poll-and-rebase
(the service retries once with the fresh etag; persistent mismatch
→ `push_failed`).

`complete_task` and `delete_task` are naturally idempotent —
backends MUST swallow upstream 4xx for "already done" / 404 for
"already gone" and return successfully.

#### `LocalTaskBackend` (`integrations/local_tasks.py`)

Vendor-free; the local backend's "upstream" *is* core's entity store.
`list_tasks` queries the same `tasks` collection the service uses for
cross-backend aggregation. `add_task` / `update_task` / `complete_task`
/ `delete_task` are no-op confirmations — the service handles
persistence end-to-end. `set_storage` opt-in via the
`StorageAwareTaskBackend` Protocol (mirror of `UserBackendAware`,
`AICapableTTSBackend`, `TunnelAwareAuthBackend`). Local lists do **NOT**
schedule a poll job — the local backend has no upstream and running
an empty poll loop would be wasted work.

#### `GoogleTasksBackend` (`std-plugins/google/google_tasks.py`)

Service-account JSON + DWD (same pattern as Gmail / Calendar). One
Gilbert list = one Google `tasklist` (bind via `tasklist_id` config
param). `list_tasks` uses `updatedMin` for delta polls. `update_task`
PATCH only translates `title` / `notes` / `due` — Google Tasks has no
native priority or tags, so those patch keys are dropped at the
backend boundary (Gilbert keeps them locally). HTTP errors map to
typed exceptions; 429 honors `Retry-After`. Documented limitations:
DWD requires Google Workspace (no personal `gmail.com`); existing
Gmail service account must additionally be granted the `tasks` scope
in the admin console; no webhook surface — polling only.

### Local-first push model (§6.7)

Every mutation lands in entity storage immediately with
`sync_status=PENDING_PUSH`. The push attempt happens inline (bounded
by `_push_timeout_sec`, default 15s). On success: `sync_status=SYNCED`,
fields normalized from upstream merged in. On retriable failure
(timeout, transient, rate-limit): row stays `PENDING_PUSH`, no
exception bubbles to the AI tool — the user sees "Added; syncing in
background." On non-retriable (auth, not-found): `sync_status=PUSH_FAILED`
+ `task.push_failed` event published.

`tasks-sync-tick` (every 30s) sweeps `PENDING_PUSH` / `PUSH_FAILED` /
`PENDING_DELETE` rows and retries until `_max_push_retries` exhausts
or success. Successful retry fires `task.sync_recovered`.

### Conflict resolution (§6.7.5)

Upstream is authoritative for fields not currently in flight. The
local-first push stamps `sync_status=PENDING_PUSH` BEFORE persisting,
so a row marked pending keeps its dirty fields verbatim until the
push lands. Any field not in flight is overwritten from upstream on
the next poll.

Stale-etag (CalDAV `If-Match` 412): backend raises
`TaskBackendConflictError`; service re-polls upstream, rebases the
patch onto the fresh snapshot, retries once. Persistent mismatch →
`PUSH_FAILED`.

### Idempotency (§6.7.4)

Every `tasks` row carries `idempotency_key`. AI-driven `add_task`
synthesizes the key from injected `(_user_id, _conversation_id,
_tool_call_id)` so retries / multi-turn duplications fold into the
same row. Inbox-AI passes the email's `Message-Id` explicitly so
re-deliveries don't double-add. Pre-create dedup uses the
`tasks(idempotency_key)` index. `complete_task` is naturally
idempotent (mark-done twice = still done). `delete_task` swallows
upstream 404.

### Soft-delete + retention

`delete_task` is **soft-delete by default** — stamps `deleted_at` +
`sync_status=PENDING_DELETE`. Hidden from default queries. The
upstream push is best-effort. Hard-delete is admin-only via the
`tasks.delete force=true` WS RPC; **not exposed to AI tools or slash**.
The `restore_task` admin path (WS RPC) clears `deleted_at` and
re-pushes upstream if needed.

`tasks-gc-tick` (daily) hard-deletes:
- DONE / CANCELLED rows where `completed_at < now - retention_days`
- soft-deleted rows where `deleted_at < now - retention_days`
- orphan `task_events_seen` rows whose `list_id` is gone.

`retention_days=0` disables GC entirely.

### Due-soon eventing (§6.9)

`tasks-due-soon-tick` (every 60s) queries open undeleted rows whose
`due_at` falls within `due_soon_lookahead_minutes` of now and whose
`due_soon_fired=False`. **Persists `due_soon_fired=True` FIRST, then
publishes** `task.due_soon` — so a publish-then-crash doesn't re-fire.
The flag is reset in `update_task` when `due_at` is rescheduled past
the lookahead window (so a re-approach to due fires once).
Already-overdue backfilled tasks do NOT fire (forward-looking event).

### Default-list resolution (§6.5)

For `add_task` without a `list_id`:
1. The user's owned list with `is_default=True`, if any.
2. Otherwise the user's **only** owned list (if exactly one exists,
   scoped to `owner_user_id == user_ctx.user_id` — important for
   inbox-AI senders without a Gilbert account).
3. Otherwise the AI tool returns a UIBlock `select` listing
   candidate lists — never an error. The user's pick re-invokes
   `add_task` with the chosen `list_id`.

### `summarize_today` (single-source rule)

Both the AI tool `summarize_today` and the `TaskProvider.summarize_today`
Provider method share one implementation. The greeting service's
direct call goes through the Provider method; chat / slash / inbox-AI
tool calls go through the AI tool, which calls the Provider method.
One prompt (`summary_prompt` ConfigParam, `ai_prompt=True`,
default `_DEFAULT_SUMMARY_PROMPT`), one JSON assembler, one fallback
(deterministic count + top 5 titles) when the AI capability is absent.

### AI tools (10)

Slash group `tasks`:

| Name | Slash | Notes |
|---|---|---|
| `task_lists` | `/tasks lists` | Lists every accessible list with `is_default`, `poll_enabled`, `degraded_since`. `parallel_safe`. |
| `add_task` | `/tasks add` | Title + optional list_id (default-list resolution). Returns UIBlock `select` when ambiguous. Idempotency key auto-injected. |
| `get_task` | `/tasks get` | Full-detail single fetch. `parallel_safe`. |
| `list_tasks` | `/tasks list` | Filters: status / tag / project / due window / list / backend. Includes `notes` by default. `parallel_safe`. |
| `complete_task` | `/tasks done` | Idempotent. |
| `update_task` | `/tasks update` | Patch fields (title, notes, due_at, due_at_tz, priority, tags, project). Forbidden fields silently dropped with `ignored` field in the return. |
| `cancel_task` | `/tasks cancel` | Mark CANCELLED; reason appended to notes. |
| `delete_task` | `/tasks delete` | Soft-delete with UIBlock confirm via shared `confirm_or_execute` helper. Hard-delete NOT exposed. |
| `tasks_due` | `/tasks due` | window ∈ today / tomorrow / this_week / this_month / overdue. Computed in user's TZ. `parallel_safe`. |
| `summarize_today` | `/tasks summary` | Single-source AI summary. |

**`source_id` is NEVER returned to AI tool callers** — server-internal
field, used only for upstream lookups. Tests assert this for
`add_task` / `get_task` / `list_tasks` returns.

### WS RPCs

- `tasks.lists.{list, get, create, update, delete, test_connection,
  refresh, share_user, unshare_user, share_role, unshare_role}`
- `tasks.{list, get, add, update, complete, cancel, delete, restore,
  due_today, due_window, overdue, summary}`
- `tasks.backends.list`

Per-handler authz uses `can_access_list` / `can_admin_list`. Errors
map: 403 permission, 404 unknown, 400 bad arg, 409 delete-with-pending.
`tasks.list` enforces `limit` clamped to [1, 200] and accepts an
opaque `cursor` token (v1 encodes the next-page offset) — the
response carries `next_cursor: str | null` so SPAs can paginate.

### Events published

All carry `list_id` (and `task_id` for per-task events):

- `task.created`, `task.completed`, `task.updated`, `task.cancelled`,
  `task.deleted` (carries `soft: bool`), `task.restored`,
  `task.due_soon`, `task.push_failed`, `task.sync_recovered`
- `tasks.list.created`, `tasks.list.updated`, `tasks.list.deleted`,
  `tasks.list.shares.changed`, `tasks.list.degraded`,
  `tasks.list.recovered`

ACL: `interfaces/acl.py` puts `task.` and `tasks.` at level 100 (user)
for both events and RPCs. WS layer applies per-list-access filtering
on top.

### Inbox-AI integration (§7.7)

The inbox-AI service (`inbox_ai_chat`) gains the AI-callable `add_task`
tool for free because that profile (`standard`, `tool_mode=all`)
inherits all tools. This feature introduced two changes to make the
extraction reliable:

1. **`inbox_ai_chat.system_prompt` ConfigParam** — multiline,
   ai_prompt=True. Default `_DEFAULT_INBOX_AI_CHAT_PROMPT` covers
   the existing email-context guidance PLUS a hint to call `add_task`
   for action items, use the `Message-Id` as `idempotency_key`, and
   mention the addition in the email reply. Cached on
   `self._system_prompt`; replaces the inlined `context_prefix` literal.

2. **`set_current_user(user_ctx)` before `ai.chat()`** — defense in
   depth so any tool that reads `get_current_user()` inside its
   execute path (e.g., `add_task`) sees the resolved sender, not the
   `SYSTEM` sentinel set above for inbox visibility bypass. The
   AIService's `_run_one_tool` also sets the contextvar from injected
   `_user_id`, so this is partially redundant — explicit is better.

Default-list resolution scopes to `owner_user_id == user_ctx.user_id`,
so an allow-listed sender without a Gilbert account fails loudly
("no list found") rather than dumping into another user's list.

### Frontend (`/tasks`)

Mounted at `/tasks` in `App.tsx`. Components in
`frontend/src/components/tasks/`:
- `TasksPage` — main page, sidebar + filters + task list.
- `TaskListSidebar` — accessible lists with `is_default` star and
  degraded badge.
- `TaskRow` — inline complete toggle, edit drawer trigger; renders
  pending_push clock / push_failed badge.
- `TaskEditDrawer` — task create / edit / soft-delete (with
  confirm-button gate inside the drawer).
- `TaskListEditDrawer` — list CRUD with backend picker that renders
  `tasks.backends.list` config_params dynamically.
- `DueTodayCard` — dashboard widget; mounted in `DashboardPage`
  next to `UpcomingEventCard` and `BriefingCard`. Hidden when nothing
  is due / overdue.

WS RPC wrappers live in `frontend/src/hooks/useWsApi.ts` (core, not
plugin — `TasksService` is core).

### Design decisions

- **No bootstrap YAML.** All list configuration lives in the
  `task_lists` entity collection, mirroring inbox / calendar / feeds.
  `gilbert.yaml` does not gain a `tasks:` section.
- **`set_storage` Protocol opt-in over a generic `bind_storage` ABC method**
  per round-2 architect review. The local backend is the only concrete
  case; external backends never satisfy `StorageAwareTaskBackend`.
  This matches `UserBackendAware`, `TunnelAwareAuthBackend`,
  `AICapableTTSBackend`.
- **Local lists do NOT poll.** The local backend's `list_tasks` is
  retained for the explicit `refresh_list` RPC and to satisfy the
  ABC, but the runtime never schedules `tasks-poll-{local_list_id}`.
  Tests assert this explicitly.
- **Soft-delete is the AI / slash default.** Hard-delete is admin-only
  via `tasks.delete force=true` WS RPC. AI tool's `delete_task` always
  returns the UIBlock confirm form when `confirm=False` (the default).

## Related
- [Inbox Service](inbox-service.md) — closest structural analog
- [Feeds Service](feeds-service.md) — recent analog with WS RPCs + SPA
- [Calendar Service](calendar-service.md) — UIBlock confirm precedent
- AI prompts are always configurable
- Backend pattern
- Multi-user isolation — ContextVar discipline
- [Capability Protocols](capability-protocols.md) — `TaskProvider`,
  `StorageAwareTaskBackend`
- `src/gilbert/interfaces/tasks.py` — backend ABC, dataclasses, helpers,
  TaskProvider, StorageAwareTaskBackend, error taxonomy
- `src/gilbert/integrations/local_tasks.py` — built-in `LocalTaskBackend`
- `src/gilbert/core/services/tasks.py` — service implementation
- `std-plugins/google/google_tasks.py` — `GoogleTasksBackend`
- `tests/unit/test_tasks_service.py`, `tests/unit/test_local_task_backend.py`
- `std-plugins/google/tests/test_google_tasks.py`
- `frontend/src/components/tasks/*` — `/tasks` SPA page
