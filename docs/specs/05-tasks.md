# Feature 05 — Tasks / Todos service + backends

## 0. Status & ownership

- **Feature:** Tasks / todos — owner-scoped task lists with multiple
  pluggable backends (entity-store local + 3rd-party providers).
- **Implementer scope for this PR:** ship `local_tasks` (built-in) **plus**
  `google_tasks` (added to the existing `google` std-plugin). `todoist`
  and `caldav` backends are sketched here at "v1.1" depth — the
  interfaces + service must accommodate them with no further refactor.
- **Status:** SPEC. Implementation must follow this document; deviations
  must be called out in the PR description so reviewers can compare.
- **Out of scope (do not implement):** sub-tasks beyond depth 1,
  recurrence authoring, pomodoro / time tracking, a full SPA tasks page.

## 1. Pitch

Same shape as the `inbox` service. Each user has 0..N **task lists**
(analogous to mailboxes), each owned by a single user with the option
to share with individual users and/or roles. Each list is bound to one
`TaskBackend` (local, todoist, google_tasks, caldav…). Tasks are
persisted in entity storage tagged with `list_id` so reads, search,
and aggregation work uniformly across backends — external backends
pull on a periodic poll job, an explicit `refresh_list(list_id)` RPC
is available for "show me right now" reads, and outbound writes (add
/ complete / update) push immediately with local-first reconciliation
(see §6.7 for failure handling). The local backend has no upstream
and no poll loop — its writes are direct to entity storage.

The user-facing pitch is:

- A user can say "add a task to call the dentist tomorrow" and Gilbert
  will write it into the right backend (whatever they've configured).
- A user can ask "what's due today?" and Gilbert aggregates across
  every list they can see — local + Todoist + Google Tasks + CalDAV.
- The greeting service can read the day's list and slot it into the
  morning briefing.
- The Inbox-AI chat already has access to `add_task` (it's just an AI
  tool), so it can lift TODOs out of incoming emails without any
  inbox-side glue code.

## 2. Architectural fit

### 2.1 Layer placement

| Module | Layer | Notes |
|---|---|---|
| `interfaces/tasks.py` | `interfaces/` | New file. ABC + dataclasses + `TaskProvider` Protocol + auth helpers. **No imports from `core/`, `integrations/`, `storage/`, `web/`.** |
| `core/services/tasks.py` | `core/services/` | New file. `TasksService` — multi-list aggregator. Imports interfaces only (plus standard side-effect import for the local backend). |
| `integrations/local_tasks.py` | `integrations/` | New file. `LocalTaskBackend` — the built-in vendor-free backend. Imports `interfaces/tasks.py` only. |
| `std-plugins/google/google_tasks.py` | std-plugin | New file inside the existing `google` plugin. `GoogleTasksBackend`. Side-effect imported by the existing `GooglePlugin.setup()`. |
| `std-plugins/todoist/` | std-plugin (v1.1) | New plugin directory. Sketched here. |
| `std-plugins/caldav/` | std-plugin (v1.1) | New plugin directory. Sketched here. |
| `tests/unit/test_tasks_service.py` | tests | Unit tests with fakes. |
| `tests/unit/test_local_task_backend.py` | tests | Local-backend unit tests against an in-memory storage backend. |
| `std-plugins/google/tests/test_google_tasks.py` | std-plugin tests | Google Tasks tests with mocked Google client. |

### 2.2 Patterns reused, not reinvented

- **Universal backend pattern** — `TaskBackend` is an `ABC` with the
  standard `_registry: dict[str, type]` + `__init_subclass__` + `backend_name`
  + `backend_config_params()` shape. See `memory-backend-pattern.md`.
  `TasksService` discovers backends via
  `TaskBackend.registered_backends()` after a side-effect import of
  `gilbert.integrations.local_tasks` (`# noqa: F401`).
- **Multi-backend aggregator** — single service holds N runtime
  `_ListRuntime` records, just like `InboxService._runtimes`. See
  `memory-multi-backend-pattern.md`.
- **Per-user state in ContextVars** — reads use
  `gilbert.core.context.get_current_user()`; mutations take
  `user_ctx: UserContext` explicitly. No per-request state on
  `self`. See `memory-multi-user-isolation.md`.
- **Capability protocol** — `TaskProvider` (`@runtime_checkable Protocol`
  in `interfaces/tasks.py`) is what consumers (greeting service, future
  plugins) `isinstance`-check against. They never import
  `gilbert.core.services.tasks.TasksService`. See
  `memory-capability-protocols.md`.
- **AI prompts are configurable** — `summarize_today` uses
  `complete_one_shot(system_prompt=self._summary_prompt, …)`; the
  prompt is a `ConfigParam(multiline=True, ai_prompt=True)` with the
  bundled string as default. See `memory-ai-prompts-configurable.md`.
- **Authorization helpers in `interfaces/`** — `can_access_list`,
  `can_admin_list`, `determine_access` mirror the inbox helpers. The
  service resolves admin via `AccessControlProvider` and passes
  `is_admin=…` in.
- **Polling via SchedulerProvider** — one `tasks-poll-{list_id}` job
  per `poll_enabled` list whose backend has an upstream. The local
  backend's runtime does **not** schedule a poll job (it has nothing
  to poll). One global tick is not used — each list has its own
  pollable backend. The boot path is a one-shot `tasks-boot` job,
  identical to inbox.

### 2.3 Sync model per backend

| Backend | Reads | Writes | Delta sync | Webhook |
|---|---|---|---|---|
| `local` | direct storage queries (no poll job scheduled) | direct storage write | n/a | n/a |
| `google_tasks` | periodic poll, `updatedMin` for delta | immediate REST push | `updatedMin` (RFC3339) | not supported by Google Tasks API |
| `todoist` (v1.1) | periodic poll, `sync_token` for delta | immediate REST push | `sync_token` cached in `task_events_seen` | supported (Sync API), v1.2+ |
| `caldav` (v1.1) | periodic poll, `getctag` + per-VTODO `etag` for delta | immediate WebDAV PUT (with `If-Match`) | `getctag` short-circuit | none for iCloud/Fastmail; some servers (Nextcloud) support out-of-band push channels — out of scope |

`TaskBackend` does not currently expose a hook for backends to push
upstream changes into the service. v1 is poll-only. If/when webhook
support is added, the ABC will gain an optional `on_external_change`
delivery surface — explicitly a future extension, not a v1 concern.

## 3. Data model

### 3.1 Entity collections

Three collections, all owned by `TasksService`:

| Collection | Key fields |
|---|---|
| `task_lists` | `id`, `name`, `backend_name`, `backend_config`, `owner_user_id`, `shared_with_users`, `shared_with_roles`, `poll_enabled`, `poll_interval_sec`, `is_default`, `created_at`, `degraded_since` (ISO UTC; non-empty when the backend has failed N consecutive polls/pushes), `last_sync_at` (ISO UTC), `last_error` (string; user-visible) |
| `tasks` | `_id` (= our id), `list_id` (required), `source_id` (backend's native id, may equal `_id` for local; **server-side only — never returned to AI tools**), `title`, `notes`, `due_at` (ISO **with TZ**, see §3.5), `due_at_tz` (IANA TZ name, e.g. `America/Los_Angeles`; empty for "any-tz" tasks like all-day), `completed_at` (ISO UTC or empty), `priority` (int 0–4, 4 = highest), `tags` (list[str]), `project` (str — backend-defined grouping; for Google = list title, for Todoist = project name), `status` (`open`/`done`/`cancelled`), `created_at` (ISO UTC), `updated_at` (ISO UTC), `created_by_user_id`, `idempotency_key` (str; see §6.7.4), `due_soon_fired` (bool; internal dedupe flag for `task.due_soon`, not exposed in API responses), `sync_status` (`synced` / `pending_push` / `push_failed` / `pending_delete`), `last_push_attempt_at` (ISO UTC), `last_push_error` (string), `retry_count` (int), `etag` (string; opaque to service — used by backends that need it for `If-Match` conflict detection, e.g. CalDAV), `deleted_at` (ISO UTC; empty for live rows — soft-delete tombstone, hidden from default queries) |
| `task_events_seen` | `_id` (= `list_id:source_id`), `etag`/`updated`, `last_seen_at` — used by external backends to dedupe poll results idempotently. Also stores per-list cursor metadata (`f"{list_id}:_sync_token"` for Todoist, `f"{list_id}:_ctag"` for CalDAV) |

> **Why no `tasks_outbox`.** Inbox keeps an outbox because email send
> is a network-mediated handoff (SMTP) that frequently fails for
> transient reasons and the user expects "queued" semantics. Tasks
> work differently: the user expects "I added it" to mean "it's
> there now." The spec uses **local-first with reconciliation**
> (§6.7) — a task lands in storage immediately with
> `sync_status=pending_push`, the push attempt happens inline, and
> a small recurring `tasks-sync-tick` retries any rows still in
> `pending_push` / `push_failed`. There is no queue ordering and
> no separate outbox collection — `sync_status` on the task row
> *is* the outbox.

### 3.2 Indexes

- `task_lists(owner_user_id)`
- `tasks(list_id, status)` — most common filter ("open tasks in this list")
- `tasks(list_id, due_at)` — for `due_today` / `overdue` per-list
- `tasks(list_id, source_id)` — dedupe key for poll loops
- `tasks(status, due_at)` — **cross-list aggregation** ("what's due today across everything I can see"); used by `due_today` / `overdue` when no `list_id` filter is supplied
- `tasks(idempotency_key)` — pre-create dedupe (§6.7.4)
- `tasks(sync_status)` — `tasks-sync-tick` retry sweep
- `tasks(list_id, deleted_at)` — soft-delete filter

> **Why `(_id, source_id)` are separate.** External backends have native
> ids (Google `tasks/abc123`, Todoist `9876543`, CalDAV `UID:foo@bar`). The
> service stores its own row id (`_id`) so local-only tasks don't need
> a fake `source_id`, but every row also carries `source_id` so the
> poll loop can dedupe when the upstream sends the same record again.
> For `LocalTaskBackend`, the implementer should set `source_id = _id`
> on insert to keep the schema uniform.

### 3.3 Dataclasses (`interfaces/tasks.py`)

```python
@dataclass
class TaskList:
    id: str
    name: str
    backend_name: str
    backend_config: dict[str, object] = field(default_factory=dict)
    owner_user_id: str = ""
    shared_with_users: list[str] = field(default_factory=list)
    shared_with_roles: list[str] = field(default_factory=list)
    poll_enabled: bool = True
    poll_interval_sec: int = 300
    is_default: bool = False
    created_at: str = ""

    def to_dict(self) -> dict[str, object]: ...
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TaskList: ...


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4


@dataclass
class Task:
    """Provider-neutral task. Persisted in the ``tasks`` entity collection.

    See §3.5 for time-zone semantics: ``created_at``, ``updated_at``,
    ``completed_at`` are UTC; ``due_at`` is stored UTC with a separate
    ``due_at_tz`` (IANA name) so day-boundary arithmetic respects the
    task author's local time.
    """

    id: str
    list_id: str
    title: str
    source_id: str = ""           # backend native id; equal to id for local; SERVER-ONLY — not exposed to AI
    notes: str = ""
    due_at: str = ""              # ISO UTC ('Z') — empty = no due date
    due_at_tz: str = ""           # IANA TZ for due_at, e.g. 'America/Los_Angeles'
    completed_at: str = ""        # ISO UTC ('Z') — empty = not completed
    status: TaskStatus = TaskStatus.OPEN
    priority: TaskPriority = TaskPriority.NONE
    tags: list[str] = field(default_factory=list)
    project: str = ""             # backend grouping (gtask list name, todoist project)
    created_at: str = ""          # ISO UTC ('Z')
    updated_at: str = ""          # ISO UTC ('Z')
    created_by_user_id: str = ""
    idempotency_key: str = ""     # see §6.7.4
    sync_status: str = "synced"   # synced | pending_push | push_failed | pending_delete
    last_push_attempt_at: str = ""
    last_push_error: str = ""
    retry_count: int = 0
    etag: str = ""                # opaque, backend-defined (CalDAV uses this)
    deleted_at: str = ""          # ISO UTC; non-empty = soft-deleted

    def to_dict(self) -> dict[str, object]:
        # priority persisted as int(self.priority.value)
        ...

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Task:
        # priority round-trip: TaskPriority(int(data.get("priority", 0) or 0))
        # with a try/except → TaskPriority.NONE on any malformed value.
        # This is the only enum field that needs explicit defensive casting.
        ...


class ListAccess(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"
```

### 3.5 Time-zone semantics

This spec deliberately departs from the scheduler's naive-local
convention. The scheduler is single-host: jobs fire in the host's
local time. Tasks are user-shaped and multi-user — a Pacific user
asking "what's due today?" while Gilbert's host clock is Eastern
must get *their* "today," not the host's. External backends also
speak in tz-aware time (Google Tasks → RFC3339 UTC, Todoist → explicit
TZ, CalDAV → VTIMEZONE). Storing local-naive everywhere would force
a lossy conversion at every boundary.

Rules:

- **Stamps** (`created_at`, `updated_at`, `completed_at`,
  `last_push_attempt_at`, `last_seen_at`, `degraded_since`,
  `last_sync_at`, `deleted_at`) are **ISO UTC with a trailing `Z`**,
  produced via `datetime.now(UTC).isoformat()`.
- **`due_at`** is **ISO UTC with a trailing `Z`**, paired with
  **`due_at_tz`** (IANA TZ name, e.g. `America/Los_Angeles`). The
  `due_at` is the precise instant; `due_at_tz` is the wall-clock zone
  the user authored the date in, used for day-boundary arithmetic
  (`due_today` etc.) and for displaying back to the user. Empty
  `due_at_tz` means "any time zone" — used for all-day reminders
  with no specific moment (CalDAV `VALUE=DATE`, Google Tasks
  date-only).
- **DST and ambiguous wall-clocks** are handled by the IANA TZ via
  `zoneinfo` — the only correct primitive for this. Never rely on
  fixed offsets.
- **`due_today` / `overdue` use the *requesting user's* TZ**, not
  the host's, not the task author's. The user's TZ comes from
  `UserContext.metadata["tz"]` if present, else falls back to the
  Gilbert host's local TZ. Adding a typed field to `UserContext` is
  out of scope for this spec — use the metadata dict.
- **The service must never see tz-naive values.** Backends that hand
  back naive datetimes must localize at the boundary using the
  backend's documented zone (Google Tasks: UTC; CalDAV: VTIMEZONE
  on the calendar; Todoist: response carries `timezone`).

### 3.4 Authorization helpers (`interfaces/tasks.py`)

Single rule, mirrored from `interfaces/inbox.py`:

```python
def can_access_list(
    user_ctx: UserContext,
    task_list: TaskList,
    *,
    is_admin: bool = False,
) -> bool:
    """Can this user read / add / complete / update tasks in this list?"""

def can_admin_list(
    user_ctx: UserContext,
    task_list: TaskList,
    *,
    is_admin: bool = False,
) -> bool:
    """Can this user edit list settings / shares / delete?"""

def determine_access(
    user_ctx: UserContext,
    task_list: TaskList,
    *,
    is_admin: bool = False,
) -> ListAccess | None:
    """Owner > admin > shared_user > shared_role > None."""
```

Caller resolves `is_admin` via
`AccessControlProvider.get_effective_level` and passes it in. The
helpers are pure and never touch the capability resolver — same
contract as `interfaces/inbox.py`.

> **Sharing semantics for v1.** Shared = full access (read +
> add + complete + update). Only owner / system admin can edit
> settings, sharing, or delete a list. This matches the inbox rules
> exactly. If we ever need a finer split (viewer vs member),
> `shared_with_users` becomes a list of objects, but for v1 the
> string-list shape stays.

## 4. `TaskBackend` ABC (`interfaces/tasks.py`)

```python
class TaskBackend(ABC):
    """Abstract task provider — pull source for read sync, push for writes.

    All persisted reads (search, due_today, overdue) come from entity
    storage; the backend is only consulted to (a) populate that storage
    on poll and (b) accept outbound writes immediately.

    Concrete subclasses set ``backend_name`` and are auto-registered
    via ``__init_subclass__``.
    """

    _registry: dict[str, type[TaskBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            TaskBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[TaskBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return []

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult: ...

    @abstractmethod
    async def initialize(self, config: dict[str, Any] | None = None) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ---- Pull ----

    @abstractmethod
    async def list_tasks(
        self,
        *,
        include_completed: bool = False,
        updated_since: str = "",
    ) -> list[Task]:
        """Return every task currently visible to this backend instance.

        ``updated_since`` is an ISO UTC timestamp (``Z``) — implementers
        that support delta polls should honor it; implementers that
        don't can ignore it and return everything (the service will
        upsert and dedupe via ``source_id``).
        """

    # ---- Push ----

    @abstractmethod
    async def add_task(self, task: Task) -> Task:
        """Create a task in the upstream provider.

        The returned ``Task`` MUST carry the upstream's ``source_id``
        (and any updated fields the provider normalized). The caller
        will persist the returned object — the input is only a draft.
        """

    @abstractmethod
    async def update_task(
        self,
        source_id: str,
        patch: dict[str, Any],
        *,
        etag: str = "",
    ) -> Task:
        """Patch only the fields in ``patch`` on the upstream task.

        Patch-shaped (not full-Task-shaped) so backends can issue
        PATCH semantics and Gilbert's local pending edits don't
        clobber unrelated fields the user changed in the upstream UI
        between the last poll and this push. The returned ``Task``
        carries the upstream's post-patch state, including a fresh
        ``etag`` if applicable.

        ``etag`` is opaque and backend-defined; backends that need
        ``If-Match`` semantics (CalDAV) raise on stale-etag mismatch
        so the service can re-poll-and-merge.
        """

    @abstractmethod
    async def complete_task(self, source_id: str) -> None:
        """Mark a task complete in the upstream provider.

        Separate from ``update_task`` because some providers expose a
        cheap dedicated ``complete`` endpoint (Todoist, Google Tasks),
        and because completion is the most common write operation —
        worth a discoverable named method. Naturally idempotent:
        calling twice on an already-completed task MUST succeed (no
        error). Backends that get a 4xx from the upstream for
        already-done MUST swallow it and return.
        """

    @abstractmethod
    async def delete_task(self, source_id: str) -> None:
        """Delete a task in the upstream provider. Naturally idempotent
        — backends MUST swallow upstream 404 ('already gone') and
        return successfully."""

    # ---- Optional capability surfaces ----

    def supports_projects(self) -> bool:
        """Whether the backend exposes user-visible groupings (Google
        Tasks ``tasklists``, Todoist ``projects``). Defaults to ``False``;
        subclasses that group tasks override.
        """
        return False

    async def list_projects(self) -> list[str]:
        """Return the human-readable project names. Only meaningful when
        ``supports_projects()`` is True.
        """
        return []
```

> **Why the backend is *just* CRUD, not a full provider.** The service
> owns persistence, search, aggregation, eventing, scheduling, and
> auth. Backends only know how to talk to a single upstream — they
> don't know about multi-list aggregation, authorization, events,
> or local DB shape. This keeps the surface tiny and makes new
> backends a few hundred lines.

### 4.1 `StorageAwareTaskBackend` capability protocol

```python
@runtime_checkable
class StorageAwareTaskBackend(Protocol):
    """Task backends that need entity storage injected.

    The local backend's "upstream" *is* core's entity store; external
    backends own their own upstream and never need this. ``TasksService``
    calls ``set_storage(storage)`` immediately after instantiation,
    BEFORE ``initialize()``, on any backend that satisfies this
    protocol.

    Mirror of ``UserBackendAware`` (auth), ``TunnelAwareAuthBackend``
    (auth), and ``AICapableTTSBackend`` (tts) — see those for prior
    art. Same naming convention: ``*Aware*Backend`` Protocol class +
    ``set_*`` method.

    Declared in ``interfaces/tasks.py`` so both ``core/services/tasks.py``
    and ``integrations/local_tasks.py`` can satisfy / check the
    protocol without ``integrations/`` ever importing ``core/``.

    The ``storage`` parameter is typed as ``object`` — implementations
    narrow at the boundary (the local backend casts to
    ``StorageBackend`` privately). This keeps ``interfaces/tasks.py``
    decoupled from any specific storage type.
    """

    def set_storage(self, storage: object) -> None: ...
```

`TasksService._start_runtime` does:

```python
backend = backend_cls()
if isinstance(backend, StorageAwareTaskBackend):
    backend.set_storage(self._storage)
await backend.initialize(settings)
```

External backends (Google, Todoist, CalDAV) never satisfy this
Protocol — they are storage-agnostic. The `TaskBackend` ABC stays
vendor-shaped (only `initialize(config)` for everyone) and the
service's storage handle is reachable from the local backend
without leaking core types into the ABC.

## 5. `TaskProvider` capability protocol (`interfaces/tasks.py`)

```python
@runtime_checkable
class TaskProvider(Protocol):
    """Plugins / other services consume tasks via this protocol.

    Mirror of ``InboxProvider`` — reads use the current user from
    ``gilbert.core.context.get_current_user`` for visibility filtering;
    mutations take ``user_ctx`` explicitly.
    """

    async def add_task(
        self,
        list_id: str,
        task: Task,
        user_ctx: UserContext,
    ) -> Task: ...

    async def complete_task(
        self,
        task_id: str,
        user_ctx: UserContext,
    ) -> Task: ...

    async def update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        user_ctx: UserContext,
    ) -> Task: ...

    async def delete_task(
        self,
        task_id: str,
        user_ctx: UserContext,
    ) -> None: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def search_tasks(
        self,
        *,
        list_id: str | None = None,
        backend: str | None = None,
        status: TaskStatus | None = TaskStatus.OPEN,
        tag: str = "",
        project: str = "",
        due_before: str = "",
        due_after: str = "",
        limit: int = 50,
    ) -> list[Task]: ...

    async def due_today(
        self,
        *,
        list_id: str | None = None,
        backend: str | None = None,
    ) -> list[Task]: ...

    async def overdue(
        self,
        *,
        list_id: str | None = None,
        backend: str | None = None,
    ) -> list[Task]: ...

    # ---- Lists ----

    async def get_list(self, list_id: str) -> TaskList | None: ...

    async def list_accessible_lists(
        self,
        user_ctx: UserContext,
    ) -> list[TaskList]: ...


@runtime_checkable
class CachedTaskListLister(Protocol):
    """For ConfigurationService dynamic-choice resolution — same shape
    as ``CachedMailboxLister`` in interfaces/inbox.py.

    Consumed by ``ConfigurationService._resolve_dynamic_choices`` to
    populate ``choices_from="task_lists"`` dropdowns on settings
    pages. The ``ai_profile`` ConfigParam in §6.4 already uses
    ``choices_from="ai_profiles"``; the only ConfigParam in the v1
    spec that uses ``task_lists`` is the future per-user "default
    list" picker on the user-profile page (post-v1). The protocol is
    declared now so that addition is plumbing-only.

    If the implementer concludes nothing in v1 actually wires
    ``task_lists`` into a ConfigurationService dynamic-choice
    consumer, drop the protocol and reintroduce when needed — don't
    keep dead surface.
    """

    @property
    def cached_lists(self) -> list[TaskList]: ...
```

## 6. `TasksService` (`core/services/tasks.py`)

### 6.0 Exception types

In `core/services/tasks.py` (mirroring inbox's `InboxPermissionError`):

```python
class TaskListPermissionError(PermissionError):
    """Raised when a caller lacks access to a task list."""

class TaskListNotFoundError(LookupError):
    """Raised when a list_id does not resolve."""

class TaskNotFoundError(LookupError):
    """Raised when a task_id does not resolve (or is soft-deleted)."""

class TaskBackendUnavailableError(RuntimeError):
    """Raised when an upstream push fails after exhausting retries."""
```

Private helpers `_require_access(task_list, user_ctx)` and
`_require_admin(task_list, user_ctx)` consolidate the
`can_access_list` / `can_admin_list` check + raise (parallel to
inbox's `_require_access` / `_require_admin` at
`core/services/inbox.py:442–460`). All public mutation methods call
these instead of inlining the check.

### 6.1 Service info

```python
def service_info(self) -> ServiceInfo:
    return ServiceInfo(
        name="tasks",
        capabilities=frozenset({"tasks", "ai_tools", "ws_handlers"}),
        requires=frozenset({"entity_storage", "scheduler"}),
        optional=frozenset({"event_bus", "configuration", "access_control", "ai_chat"}),
        events=frozenset({
            "task.created",
            "task.completed",
            "task.updated",
            "task.deleted",
            "task.cancelled",
            "task.due_soon",
            "task.push_failed",
            "task.sync_recovered",
            "tasks.list.created",
            "tasks.list.updated",
            "tasks.list.deleted",
            "tasks.list.shares.changed",
            "tasks.list.degraded",
            "tasks.list.recovered",
        }),
        ai_calls=frozenset({"tasks_summary"}),
        toggleable=True,
        toggle_description="Tasks / todo lists",
    )
```

`ai_chat` is **optional** — the only AI call the service makes is
`tasks_summary` (`summarize_today`), and absence of `ai_chat` simply
makes that AI tool degrade to a plain bulleted dump.

### 6.2 Lifecycle

`start(resolver)` performs:

1. Resolve required capabilities (`entity_storage`, `scheduler`).
2. `ensure_index` for every collection listed in §3.2.
3. Resolve optional capabilities (`event_bus`, `configuration`,
   `access_control`, `ai_chat`). Cache `self._resolver` for
   late-binding lookups (same pattern as `InboxService` —
   `WorkspaceProvider`-style late lookup).
4. Read config (`max_summary_tasks`, `default_poll_interval_sec`,
   `summary_prompt`, `ai_profile`, `due_soon_lookahead_minutes`,
   `enabled`). Cache them on `self._max_summary_tasks` etc.
5. Schedule the one-shot `tasks-boot` job (calls `_boot_runtimes`).
6. Schedule the recurring `tasks-due-soon-tick` job (every 60s by
   default; configurable). This drives `task.due_soon` events for
   notifications.
7. Schedule the recurring `tasks-sync-tick` job (every 30s).
   Sweeps `tasks` for rows with `sync_status in (pending_push,
   push_failed, pending_delete)` and retries the upstream push with
   exponential backoff (capped at `_max_push_retries`). On final
   failure, sets `sync_status=push_failed` and publishes
   `task.push_failed`. Local-backend rows never enter this state.
8. Schedule the recurring `tasks-gc-tick` job (daily by default;
   `retention_days` config). Hard-deletes:
   - `tasks` rows where `status in (DONE, CANCELLED)` and
     `completed_at < now - retention_days`.
   - `tasks` rows where `deleted_at != ""` and
     `deleted_at < now - retention_days` (soft-delete tombstones).
   - Orphan `task_events_seen` rows whose `list_id` no longer exists.

`_boot_runtimes` mirrors `InboxService._boot_runtimes`: load every
list, refresh `_cached_lists`, start a `_ListRuntime` for each
`poll_enabled` list.

`stop()` removes every poll job + `tasks-due-soon-tick` +
`tasks-sync-tick` + `tasks-gc-tick` + `tasks-boot`, calls
`backend.close()` on every runtime, clears `_runtimes`.

> **Local lists do NOT schedule a poll job.** `_start_runtime` skips
> `scheduler.add_job(...)` when `runtime.task_list.backend_name == "local"`.
> The local backend has no upstream and persistence happens directly
> via the service write path; running an empty poll loop is wasted
> work and contradicts §10.1's `LocalTaskBackend.list_tasks` (which
> queries the same `tasks` collection — self-referential without
> being useful).

### 6.3 Internal state

```python
@dataclass
class _ListRuntime:
    task_list: TaskList
    backend: TaskBackend
    poll_job_name: str = ""

class TasksService(Service):
    def __init__(self) -> None:
        self._storage: Any = None              # StorageBackend
        self._event_bus: Any = None
        self._scheduler: SchedulerProvider | None = None
        self._access_control: AccessControlProvider | None = None
        self._ai: AISamplingProvider | None = None
        self._resolver: ServiceResolver | None = None

        self._runtimes: dict[str, _ListRuntime] = {}
        self._cached_lists: list[TaskList] = []

        self._max_summary_tasks: int = 30
        self._default_poll_interval_sec: int = 300
        self._due_soon_lookahead_minutes: int = 10
        self._summary_prompt: str = _DEFAULT_SUMMARY_PROMPT  # see §6.10
        self._ai_profile: str = "light"
        self._enabled: bool = True
        self._push_timeout_sec: int = 15
        self._max_push_retries: int = 5
        self._degraded_after_failures: int = 3
        self._retention_days: int = 90
```

> **No per-request state on `self`.** Every request-scoped value
> (current user, current conversation) is read from
> `gilbert.core.context`. Per-list locks (for the rare case where the
> outbound push and the poll for the same list overlap) live in a
> `dict[list_id, asyncio.Lock]` if needed — but for v1 the per-list
> single poll job is naturally serialized by the scheduler, so a lock
> is only required around the dedupe-write path inside `_poll_runtime`,
> which is single-process anyway.

### 6.4 Configuration (`Configurable` protocol)

```python
@property
def config_namespace(self) -> str: return "tasks"

@property
def config_category(self) -> str: return "Productivity"

def config_params(self) -> list[ConfigParam]:
    return [
        ConfigParam(
            key="enabled",
            type=ToolParameterType.BOOLEAN,
            description="Enable the tasks service.",
            default=True,
        ),
        ConfigParam(
            key="default_poll_interval_sec",
            type=ToolParameterType.INTEGER,
            description="Default poll interval for new task lists (seconds).",
            default=300,
        ),
        ConfigParam(
            key="due_soon_lookahead_minutes",
            type=ToolParameterType.INTEGER,
            description=(
                "How far ahead to look for the task.due_soon event. "
                "A task fires once when its due_at falls within this "
                "window of now."
            ),
            default=10,
        ),
        ConfigParam(
            key="max_summary_tasks",
            type=ToolParameterType.INTEGER,
            description="Maximum number of tasks the daily summary digests.",
            default=30,
        ),
        ConfigParam(
            key="ai_profile",
            type=ToolParameterType.STRING,
            description=(
                "AI profile used by ``summarize_today``. This profile "
                "is used both by the ``summarize_today`` AI tool "
                "(slash + chat) AND by the greeting service's direct "
                "``TaskProvider.summarize_today`` call. Cheaper tiers "
                "(default ``light``) are recommended — daily summaries "
                "don't benefit from advanced models, and greeting cost "
                "scales with this choice."
            ),
            default="light",
            choices_from="ai_profiles",
        ),
        ConfigParam(
            key="push_timeout_sec",
            type=ToolParameterType.INTEGER,
            description=(
                "Per-call timeout for outbound writes to external "
                "backends (``add_task`` / ``update_task`` / "
                "``complete_task`` / ``delete_task``). Hanging the AI "
                "tool call until the upstream HTTP client gives up "
                "is a UX disaster — bound the wait."
            ),
            default=15,
        ),
        ConfigParam(
            key="max_push_retries",
            type=ToolParameterType.INTEGER,
            description=(
                "Maximum number of retry attempts before a "
                "``pending_push`` task is marked ``push_failed`` and "
                "``task.push_failed`` is published."
            ),
            default=5,
        ),
        ConfigParam(
            key="degraded_after_failures",
            type=ToolParameterType.INTEGER,
            description=(
                "Number of consecutive failed polls/pushes before a "
                "list's runtime is marked degraded "
                "(``task_lists.degraded_since`` set, "
                "``tasks.list.degraded`` event published). The UI "
                "uses this to show 'Connection issues — last sync …'."
            ),
            default=3,
        ),
        ConfigParam(
            key="retention_days",
            type=ToolParameterType.INTEGER,
            description=(
                "Days to keep DONE / CANCELLED / soft-deleted tasks "
                "before the daily ``tasks-gc-tick`` hard-deletes them. "
                "Default 90. Set to 0 to disable GC entirely (rows "
                "never expire automatically)."
            ),
            default=90,
        ),
        ConfigParam(
            key="summary_prompt",
            type=ToolParameterType.STRING,
            description=(
                "System prompt for ``summarize_today``. Drives how the "
                "AI condenses today's task list into a brief daily "
                "summary. Leave blank to use the bundled default."
            ),
            default=_DEFAULT_SUMMARY_PROMPT,
            multiline=True,
            ai_prompt=True,
        ),
    ]

async def on_config_changed(self, config: dict[str, Any]) -> None:
    self._max_summary_tasks = int(config.get("max_summary_tasks", self._max_summary_tasks))
    self._default_poll_interval_sec = int(config.get("default_poll_interval_sec", self._default_poll_interval_sec))
    self._due_soon_lookahead_minutes = int(config.get("due_soon_lookahead_minutes", self._due_soon_lookahead_minutes))
    self._ai_profile = str(config.get("ai_profile", self._ai_profile) or self._ai_profile)
    self._push_timeout_sec = int(config.get("push_timeout_sec", self._push_timeout_sec))
    self._max_push_retries = int(config.get("max_push_retries", self._max_push_retries))
    self._degraded_after_failures = int(config.get("degraded_after_failures", self._degraded_after_failures))
    self._retention_days = int(config.get("retention_days", self._retention_days))
    self._summary_prompt = (
        str(config.get("summary_prompt", "") or "") or _DEFAULT_SUMMARY_PROMPT
    )
```

> **AI prompt rule.** `_DEFAULT_SUMMARY_PROMPT` is a module-level
> string; it's the `default=` of the `summary_prompt` ConfigParam; the
> service caches the active value into `self._summary_prompt` in
> `on_config_changed`. The call site reads `self._summary_prompt`,
> NEVER `_DEFAULT_SUMMARY_PROMPT`. See `memory-ai-prompts-configurable.md`.

### 6.5 List CRUD

`create_list(task_list, user_ctx)`, `update_list(list_id, updates,
user_ctx)`, `delete_list(list_id, user_ctx)`, `share_user`,
`unshare_user`, `share_role`, `unshare_role`,
`test_list_connection(list_id, user_ctx)` — all gated by
`can_admin_list`. Implementations mirror the inbox versions one-to-one
(see `core/services/inbox.py:464–712` for the canonical shape).

Restart-on-change rules for `update_list`: a runtime restart is
required when any of `backend_name`, `backend_config`, `poll_enabled`,
`poll_interval_sec` changes. Share edits do **not** restart the
runtime. `is_default` toggles do **not** restart.

`delete_list` cascades:
1. Stop the runtime.
2. Delete every `tasks` row with `list_id = <list_id>`.
3. Delete every `task_events_seen` row with `_id` starting with
   `<list_id>:`.
4. Delete the `task_lists` row.
5. Refresh cache, publish `tasks.list.deleted`.

`delete_list` should refuse if any task with `status == OPEN` exists
locally and `force` is False. Pass `force=True` from the WS handler
when the user has confirmed they want to drop incomplete tasks.

> **`is_default` flag.** Each user may have one default list per
> backend. `add_task` without a `list_id` argument resolves to:
> 1. The user's owned list with `is_default=True`, if any.
> 2. Otherwise the user's only owned list (if exactly one exists).
> 3. Otherwise an error telling the AI to call `task_lists` first
>    and pass an explicit `list_id`.

### 6.6 Polling

For each `_ListRuntime` whose `task_list.backend_name != "local"`,
`_make_poll_callback(list_id)` returns:

```python
async def _run() -> None:
    runtime = self._runtimes.get(list_id)
    if runtime is None:
        return
    await self._poll_runtime(runtime)
```

`_poll_runtime` flow:

1. Read `last_seen` from `task_events_seen` (latest `last_seen_at`
   across this list's seen rows). If empty, poll without a delta.
2. Call `backend.list_tasks(updated_since=last_seen)`.
3. For each returned `Task`:
   - Compute key `f"{list_id}:{task.source_id}"`.
   - `existing = storage.get("tasks", existing_id_lookup_by_source_id)`.
   - If new → insert with a fresh `_id` (`f"tsk_{uuid.uuid4().hex[:12]}"`),
     publish `task.created`.
   - If exists and meaningful fields changed → update, publish
     `task.updated`.
   - If exists and `status == DONE` newly → publish `task.completed`.
4. Update `task_events_seen[list_id:source_id]` with `updated_at` and
   `last_seen_at = now`.
5. Log per-poll counters.
6. Reset the runtime's failure counter on success. Update
   `task_lists.last_sync_at`. If the list was previously degraded,
   clear `degraded_since` and `last_error`, publish
   `tasks.list.recovered`.

On exception, increment the runtime's in-memory failure counter
(`_failure_counts: dict[list_id, int]` on `TasksService`). Once the
counter reaches `_degraded_after_failures`, persist
`degraded_since=now`, `last_error=<truncated message>` on the
`task_lists` row and publish `tasks.list.degraded`. Polls keep
running on schedule — degradation is a UI hint, not a circuit
breaker. Expose `degraded_since` / `last_error` / `last_sync_at` on
`tasks.lists.list` so the settings UI can show "Connection issues —
last sync 2h ago."

> **Local backend has NO poll job.** §6.2 step 5 explicitly skips
> `scheduler.add_job(...)` for local lists. `LocalTaskBackend.list_tasks`
> still exists for the explicit `refresh_list(list_id)` RPC and for
> implementing the `TaskBackend` ABC, but the runtime never calls it
> on a tick. Tests must verify no `tasks-poll-{local_list_id}` job
> appears in the scheduler after boot.

### 6.7 Direct writes

The write model is **local-first with reconciliation**. Every
mutation lands in entity storage immediately; the upstream push is
attempted inline (bounded by `_push_timeout_sec`) and, if it fails,
the row stays with `sync_status=pending_push` and the
`tasks-sync-tick` retries until success or `max_push_retries`. This
gives the user "I added it = it's there" semantics on the local DB
even when the upstream is unreachable, while preserving eventual
consistency with the backend.

#### 6.7.1 `add_task(list_id, task, user_ctx, *, idempotency_key="")`

1. Resolve & access-check the list (`_require_access`).
2. **Idempotency check (§6.7.4).** Compute the effective key — caller's
   `idempotency_key` if provided, else inferred from
   `(user_id, conversation_id, tool_call_id)` injected into
   `tc.arguments` by `AIService._run_one_tool` when the call comes
   from an AI tool. If a non-empty key is in scope and a row with
   matching `(list_id, idempotency_key)` already exists in storage
   (regardless of `sync_status`), return that row instead of
   creating a duplicate.
3. Generate `task.id = f"tsk_{uuid.uuid4().hex[:12]}"` if empty.
4. Stamp `created_at = updated_at = now(UTC)`,
   `created_by_user_id = user_ctx.user_id`, `list_id`,
   `sync_status="pending_push"`, `idempotency_key=<resolved>`.
5. **Persist immediately.** Write the row to `tasks` and publish
   `task.created`. The row is now visible to all reads.
6. **Push inline** (only for non-local backends):
   - Look up runtime via `_ensure_runtime(list_id)` (lazily spins
     up a transient backend for `poll_enabled=False` lists).
   - `created = await asyncio.wait_for(runtime.backend.add_task(task), timeout=self._push_timeout_sec)`.
   - On success: merge `created.source_id` and any normalized fields
     into the persisted row, set `sync_status="synced"`,
     `retry_count=0`, `last_push_error=""`.
   - On exception (timeout, network error, 5xx, RateLimit): set
     `sync_status="pending_push"`, `last_push_attempt_at=now`,
     `last_push_error=<truncated>`, `retry_count += 1`. Do **not**
     raise to the caller — the task is in the local DB, the AI tool
     reports success ("Added; syncing in background…").
   - On non-retriable upstream rejection (4xx other than 429): set
     `sync_status="push_failed"`, publish `task.push_failed`. The
     AI tool reports the failure plainly so the user can see the
     reason.
7. For the local backend, `runtime.backend.add_task(task)` is a
   no-op confirm (sets `source_id = task.id`); `sync_status` is
   stamped `synced` and no push retry can ever fire.
8. Return the persisted Task.

#### 6.7.2 `update_task(task_id, updates, user_ctx)`

`updates` is a `dict[str, Any]` patch — only the fields the user
actually changed travel through. The service:

1. Loads the row, access-checks the list.
2. **Forbids** `list_id`, `id`, `source_id`, `created_at`,
   `created_by_user_id`, `idempotency_key`, internal sync fields
   (`sync_status`, `retry_count`, `last_push_*`, `etag`,
   `due_soon_fired`, `deleted_at`). The AI cannot mutate these. The
   `status` field is also forbidden — `complete_task` /
   `cancel_task` / `delete_task` are the dedicated transitions.
   Cross-list moves are out of scope for v1 (delete-and-recreate is
   the documented upgrade path; see §21).
3. Applies the patch to the local row, stamps `updated_at=now(UTC)`,
   sets `sync_status="pending_push"`.
4. Pushes the **patch only** (not the full Task) to the backend via
   `runtime.backend.update_task(source_id, patch, etag=row.etag)`.
   On stale-etag mismatch (CalDAV `If-Match` 412), the service
   re-polls the upstream row, rebases the patch onto the fresh
   snapshot, and retries once. Persistent mismatch → `push_failed`.
5. Publishes `task.updated` with `changed_fields = list(updates.keys())`.
6. If `due_at` changed and the new value is past the lookahead window
   the next tick will recompute, clear `due_soon_fired` so the next
   approach to due fires once.

#### 6.7.3 `complete_task(task_id, user_ctx)` and `delete_task` / `cancel_task`

- `complete_task`: sets `status=DONE`, `completed_at=now(UTC)`,
  pushes via `runtime.backend.complete_task(source_id)`. Idempotent
  — calling on an already-DONE task is a no-op.
- `cancel_task(task_id, reason="", user_ctx)`: sets
  `status=CANCELLED`, `completed_at=now(UTC)`, persists `reason` in
  notes if provided, publishes `task.cancelled`. Most external
  backends don't expose a "cancel" semantic — the backend treats it
  as a soft-delete (Google Tasks: `delete`; Todoist: `close`;
  CalDAV: `STATUS:CANCELLED`).
- `delete_task(task_id, user_ctx, *, force=False)`: **soft-delete by
  default** — sets `deleted_at=now(UTC)`, `sync_status="pending_delete"`,
  hides from default queries. The upstream push is best-effort
  (delete-on-upstream); 404 is swallowed (already gone). The hard
  delete (storage row removal) happens in `tasks-gc-tick` after
  `retention_days`. `force=True` performs immediate hard delete and
  is reserved for admin tooling — **not** exposed to AI tools.
  Soft-delete is the AI-facing behavior; the user can recover via
  the future `restore_task` admin path (post-v1).

#### 6.7.4 Idempotency

AI tool calls retry. Multi-turn reasoning duplicates calls.
`add_task` MUST NOT create a second row for the same logical
operation:

- Every `tasks` row carries `idempotency_key`.
- For AI-driven calls, the service resolves the key from
  `tc.arguments`'s injected `_user_id` + `_conversation_id` +
  `_tool_call_id` (the AI service injects all three; if any are
  missing, idempotency is best-effort and an empty key skips the
  pre-check). Callers MAY pass `idempotency_key` explicitly — the
  inbox-AI path passes the email's `Message-Id` (§7.7) so re-deliveries
  of the same email don't double-add.
- `complete_task` is naturally idempotent (mark-done twice = still
  done).
- `delete_task` swallows 404 from upstream.
- The `tests` collection MUST have an index on `idempotency_key`
  (§3.2) for the pre-create lookup to be cheap.

#### 6.7.5 Conflict resolution

Two scenarios, both will happen:

- **Concurrent edits** — Gilbert pushes at T=0, user edits the same
  task on Todoist mobile at T=2, Gilbert polls at T=300. Resolution:
  **upstream is authoritative for fields not currently in
  `pending_push`**. Because `update_task` pushes only the changed
  fields (§6.7.2), the user's mobile edits to other fields survive
  the next poll. The poll merges field-by-field — if a field is
  present in both upstream and a `pending_push` patch, the
  `pending_push` value wins until the push lands; otherwise upstream
  wins.
- **Gilbert was offline** — the queued push has stale fields. Same
  rule: only the patched fields are sent, so concurrent mobile edits
  to other fields survive.

For the `local` backend there is no upstream and conflict resolution
does not apply.

#### 6.7.6 `get_task` and `search_tasks`

- Resolve `user_ctx` from `get_current_user()`.
- Resolve accessible `list_id` set (`_accessible_list_ids(user_ctx)`).
- If filter `list_id` given, verify it's in the accessible set.
- If filter `backend` given, restrict to `list_id`s whose
  `task_list.backend_name == <backend>`.
- Build `Filter`s on top of the storage `Query`. For
  `due_before`/`due_after`/`status`/`tag`/`project`, push to storage.
- Default filter excludes soft-deleted rows (`deleted_at == ""`).
- Default `status=OPEN`. Caller can pass `None` to get all statuses
  (used by admin views). Same default applies to `due_today` and
  `overdue` (which are restricted to OPEN by definition).
- Map raw rows → `Task`.

#### 6.7.7 `refresh_list(list_id, user_ctx)`

Explicit "force a fresh upstream pull" RPC for the rare case where
read latency matters more than poll cadence (e.g. user just clicked
"Sync now" in the settings UI). Access-check, then call
`_poll_runtime` directly. Returns the count of inserted/updated rows.

### 6.8 `due_today` / `overdue`

Day boundaries are **per requesting user**, not per host. The user's
TZ comes from `UserContext.metadata["tz"]` (IANA name) or falls back
to the host's local TZ.

```python
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

def _user_tz(user_ctx: UserContext) -> ZoneInfo:
    name = user_ctx.metadata.get("tz")
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # Fallback: host local TZ
    return ZoneInfo(time.tzname[0]) if hasattr(time, "tzname") else ZoneInfo("UTC")

async def due_today(self, *, list_id=None, backend=None) -> list[Task]:
    user_ctx = get_current_user()
    tz = _user_tz(user_ctx)
    now_local = datetime.now(tz)
    start_of_day_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    end_of_day_utc = now_local.replace(hour=23, minute=59, second=59, microsecond=999_999).astimezone(UTC)
    return await self.search_tasks(
        list_id=list_id,
        backend=backend,
        status=TaskStatus.OPEN,
        due_after=start_of_day_utc.isoformat().replace("+00:00", "Z"),
        due_before=end_of_day_utc.isoformat().replace("+00:00", "Z"),
    )

async def overdue(self, *, list_id=None, backend=None) -> list[Task]:
    now_utc = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return await self.search_tasks(
        list_id=list_id,
        backend=backend,
        status=TaskStatus.OPEN,
        due_before=now_utc,
    )
```

Storage stores `due_at` as ISO UTC `Z`; comparisons are
lexicographic against the same form. `due_at_tz` (the user's
authoring zone) is preserved for round-trip display but is not used
in the query — the query is in absolute time.

### 6.9 `task.due_soon` event

`tasks-due-soon-tick` runs every 60s. On each tick:

1. `now = datetime.now(UTC)`.
2. `cutoff = now + timedelta(minutes=self._due_soon_lookahead_minutes)`.
3. Query `tasks` for `status == OPEN` AND `deleted_at == ""` AND
   `due_at >= now` AND `due_at <= cutoff` AND
   `due_soon_fired != True`.
4. For each match: **persist `due_soon_fired = True` FIRST, then
   publish `task.due_soon`**. Order matters: a publish-then-crash
   would re-fire on next tick. Standard transactional-outbox
   reasoning. The dedupe is single-process-safe by construction —
   if Gilbert ever moves to multi-worker, this needs a re-read with
   row-level locking.
5. Event payload: `{task_id, list_id, title, due_at, due_at_tz,
   created_by_user_id, backend, lookahead_minutes}`.

The flag is reset in `update_task` when `due_at` is rescheduled
past the lookahead window (so the next approach to due fires once).
A reschedule-past-then-back-into-window therefore re-fires — this is
intentional ("fresh approach to due"). Tasks that arrive
already-overdue from an upstream backfill do **not** fire
`task.due_soon` (the window is forward-looking); a future
`task.overdue` event class can address that need separately.

> **Visibility for shared lists.** `task.due_soon` fires for the
> `created_by_user_id`'s timezone-window logic (the tick query uses
> a single absolute window so it applies regardless of viewer TZ).
> The frontend filter (§8) gates events by `list_id` membership in
> the viewer's accessible-list cache — so shared-list members will
> see notifications. That's consistent with inbox's "shared mailbox =
> shared notifications" rule. Document.

### 6.10 AI summary prompt

```python
_DEFAULT_SUMMARY_PROMPT = """\
You are Gilbert's daily task summarizer. The user is about to start
their day and wants a brief, encouraging overview of what's on their
plate. Given the JSON list of open tasks below — each with title,
due date, project, and tags — produce a concise English summary in
2–4 sentences. Lead with what's due today, then call out anything
overdue, then wrap with a one-line nudge. Keep it warm and human.
Do not list every task verbatim; cluster related items.
"""
```

The summary tool `summarize_today` builds the JSON payload from
`due_today()` + `overdue()` (limited to `_max_summary_tasks` total),
then calls `self._ai.complete_one_shot(messages=[user_msg],
system_prompt=self._summary_prompt, profile_name=self._ai_profile)`.
If `self._ai is None`, fall back to a deterministic templated
summary (count + top 5 titles).

## 7. AI tools (`ToolProvider` on `TasksService`)

All tools set `slash_group="tasks"` so commands collapse under
`/tasks <verb> …`. `source_id` is **never** included in tool
returns — it's a server-internal field used for upstream lookups.
The AI sees only `id`.

| Tool name | Slash | Required role | Description |
|---|---|---|---|
| `task_lists` | `/tasks lists` | user | List every task list the current user can access. Call first when the user's intent doesn't name a list. Returns `[{id, name, backend, access, is_default, poll_enabled, degraded_since}]`. |
| `add_task` | `/tasks add` | user | Create a task. If the user has multiple lists and none is default, ask via UIBlock `select` rather than picking. |
| `get_task` | `/tasks get` | user | Fetch one task by id with full notes, tags, project, sync status. |
| `list_tasks` | `/tasks list` | user | Filtered listing (status, tag, project, due window, list, backend). Includes `notes` by default. |
| `complete_task` | `/tasks done` | user | Mark a task complete by id. Idempotent. |
| `update_task` | `/tasks update` | user | Patch fields (title, notes, due_at, priority, tags, project). Forbidden fields: id, list_id, source_id, status, created_*, idempotency_key, sync fields. |
| `cancel_task` | `/tasks cancel` | user | Mark CANCELLED with optional reason. Use this when the user said "cancel" or "drop" — distinct from `complete_task` (lying about completion) and `delete_task` (loses history). |
| `delete_task` | `/tasks delete` | user | **Soft-delete** by default with a `UIBlock` confirm step (Confirm/Cancel buttons). The AI calls with `confirm=false` first to surface the form; the user's button click re-invokes with `confirm=true` and the delete proceeds. Recovery path: row stays in DB until `retention_days` elapse. |
| `tasks_due` | `/tasks due` | user | Tasks due in a window. `window` ∈ `today`, `tomorrow`, `this_week`, `this_month`, `overdue`. Uses requesting user's TZ. |
| `summarize_today` | `/tasks summary` | user | AI-generated 2–4 sentence summary. Single-source — same code path used by greeting's direct Provider call. |

> **`due_today` and `overdue` aliases.** Kept as thin tool aliases of
> `tasks_due(window=...)` for backwards-compatibility with the
> default lexicon expected by speakers ("what's due today?",
> "anything overdue?"). The implementation routes to the same
> `_compute_due_window` helper. Slash users get `/tasks today`,
> `/tasks overdue`, `/tasks due tomorrow` — same name space.

### 7.1 `add_task` parameters

```python
ToolDefinition(
    name="add_task",
    slash_group="tasks",
    slash_command="add",
    slash_help="Add a task: /tasks add <title> [list=...] [due=...] [priority=high]",
    description=(
        "Create a new task. ``list_id`` is optional — if omitted, "
        "resolution rules:\n"
        "  1. If the user has exactly one accessible list, use it.\n"
        "  2. If the user has multiple lists and one is marked "
        "     default, use it.\n"
        "  3. If the user has multiple lists and none is default, "
        "     prefer the list whose ``name`` semantically matches "
        "     the task content (e.g. 'work meeting prep' → 'Work', "
        "     'pick up groceries' → 'Personal'). If the choice is "
        "     ambiguous, RETURN A UIBlock with a select element "
        "     listing candidate lists rather than guessing — this is "
        "     a one-time picker per ambiguous turn, not an error.\n"
        "  4. Only error with 'call task_lists first' as a last "
        "     resort.\n"
        "``due_at`` is ISO with timezone (e.g. "
        "``2026-05-09T17:00:00-07:00`` or ``2026-05-09T17:00:00Z``) — "
        "you are responsible for resolving natural-language dates "
        "via the ``system_datetime`` tool and converting to ISO. "
        "Pass the user's TZ in ``due_at_tz`` (IANA name) so day "
        "boundaries respect their wall clock.\n"
        "``priority`` accepts either an integer 0..4 OR a string "
        "(``none``/``low``/``medium``/``high``/``urgent``).\n"
        "``tags`` are user-coined topical labels (e.g. ``shopping``, "
        "``phone-call``). Do NOT use ``tags`` for priority words "
        "like ``urgent`` — those go in ``priority``.\n"
        "``project`` is the backend-defined grouping. For Google "
        "Tasks and Todoist, the upstream determines this — passing "
        "it on add is mostly read-only / ignored. Don't try to set "
        "it for non-local backends."
    ),
    parameters=[
        ToolParameter(name="title", type=STRING, description="Short task title."),
        ToolParameter(name="list_id", type=STRING, description="Target list id; see resolution rules in description.", required=False),
        ToolParameter(name="notes", type=STRING, description="Long-form notes.", required=False),
        ToolParameter(name="due_at", type=STRING, description="ISO with TZ (e.g. 2026-05-09T17:00:00-07:00). Empty = no due date.", required=False),
        ToolParameter(name="due_at_tz", type=STRING, description="IANA timezone name for the user's wall clock (e.g. America/Los_Angeles). Optional but recommended.", required=False),
        ToolParameter(name="priority", type=STRING, description="0..4 or none/low/medium/high/urgent.", required=False, default="0"),
        ToolParameter(name="tags", type=ARRAY, description="String list of topical labels (NOT priority words).", required=False),
        ToolParameter(name="project", type=STRING, description="Project / group name. Read-only for Google/Todoist lists.", required=False),
        ToolParameter(name="idempotency_key", type=STRING, description="Optional dedupe key. AI callers normally don't set this — the AI service injects an implicit key. Inbox-AI sets it to the email Message-Id.", required=False),
    ],
    required_role="user",
)
```

The implementation reads `_user_id` / `_conversation_id` /
`_tool_call_id` from `tc.arguments` (injected by
`AIService._run_one_tool`) for `created_by_user_id` and the implicit
idempotency key — never from `self`. Priority is normalized via
`_coerce_priority(value)` which accepts ints (0–4), strings
(`"none"|"low"|"medium"|"high"|"urgent"`), or numeric strings.

### 7.2 `list_tasks` parameters

`status` (default `open`, pass `all` to include DONE/CANCELLED),
`tag`, `project`, `due_before`, `due_after`, `list_id`, `backend`,
`limit` (default 50). Returns a JSON list of `{id, list_id, title,
notes, due_at, due_at_tz, status, priority, tags, project, backend}`
— **`notes` IS included by default** so the AI doesn't need a
follow-up `get_task` call to disambiguate. `source_id` is never
returned. Soft-deleted rows (`deleted_at != ""`) are excluded
unconditionally.

### 7.3 `get_task`

Single-task fetch with full fields. Returns `{id, list_id, title,
notes, due_at, due_at_tz, completed_at, status, priority, tags,
project, created_at, updated_at, backend, sync_status,
last_push_error}`. Use this for "tell me about that task" or after
a `list_tasks` summary when the user wants details on one item.
404 if soft-deleted.

### 7.4 `complete_task`, `update_task`, `cancel_task`, `tasks_due`

- `complete_task(task_id)` → idempotent. If the task is already
  DONE, returns "Already completed" without raising.
- `update_task(task_id, **fields)` → patches only the fields the AI
  passed. Forbidden fields silently dropped with a warning in the
  return string ("Ignored: status — use complete_task instead").
- `cancel_task(task_id, reason="")` → CANCELLED with optional
  `reason` appended to notes.
- `tasks_due(window="today")` → window ∈ `today` / `tomorrow` /
  `this_week` / `this_month` / `overdue`. Computed in the user's
  TZ. Optional `list_id` / `backend` filters.

### 7.5 `delete_task` (confirm-then-act)

`delete_task` is the one mutation tool that returns a `ToolOutput`
with `ui_blocks` for confirmation, modeled on the calendar
`create_event` confirm pattern (per `memory-ui-blocks.md`):

```python
ToolDefinition(
    name="delete_task",
    description=(
        "Delete a task by id. Defaults to soft-delete with "
        "confirmation: pass ``confirm=false`` (the default) to "
        "surface a Confirm/Cancel UIBlock, and the form submission "
        "will re-invoke this tool with ``confirm=true``. Soft-delete "
        "keeps the row recoverable until ``retention_days`` "
        "elapses; the upstream provider's row is removed as a "
        "best-effort push."
    ),
    parameters=[
        ToolParameter(name="task_id", type=STRING),
        ToolParameter(name="confirm", type=BOOLEAN, required=False, default=False),
    ],
    required_role="user",
)
```

The execute path:

1. Resolve the task. If not found → return `"Task not found"`.
2. If `confirm=false`: return `ToolOutput(text="Confirm delete of <title>?", ui_blocks=[UIBlock(title="Delete task?", elements=[UILabel(<title>, <due_at>, <list_name>), UIButtons("confirm", [UIOption("yes", "Delete"), UIOption("no", "Cancel")])], submit_label="…", tool_name="delete_task")])`. The form submit re-invokes the tool with `confirm=true`.
3. If `confirm=true`: perform the soft-delete (§6.7.3), publish
   `task.deleted`, return `"Deleted '<title>' (recoverable for <retention_days>d)"`.

Slash form `/tasks delete <id>` requires an explicit `--yes` flag
(`/tasks delete tsk_abc --yes`) to bypass the confirm step. Without
it, the slash command returns the same UIBlock so a typo'd id is
recoverable. **Hard delete is not exposed to AI tools or slash
commands**; it's reserved for admin tooling (the `force=True`
service path) accessible only via the `tasks.delete` WS RPC for
admin users.

### 7.6 `summarize_today`

Synchronous call to `summarize_today()` (§6.10). Returns the AI
text or fallback. **Does not** stream — this is a one-shot.

> **Single-source rule.** The AI tool `summarize_today` and the
> Provider method `TaskProvider.summarize_today` MUST share an
> implementation: the tool calls the method. One prompt, one JSON
> assembler, one fallback. The greeting service's invocation
> (§14) reuses the same code path — no duplication.

> **Two callers, one method.** The greeting service's call goes
> through the Provider method directly (no AI tool framework, no
> tool inheritance — see `memory-ai-context-profiles.md` on why
> `tools_override=[]` is the right pattern for greeting-shaped
> generation). The chat / slash / inbox-AI calls go through the AI
> tool, which is visible to all `tool_mode=all` profiles
> (default). Both ultimately read `self._summary_prompt` and
> `self._ai_profile`. Document.

### 7.7 Inbox-AI integration

The Inbox-AI service (`InboxAIChatService` in
`core/services/inbox_ai_chat.py`) runs every allow-listed inbound
email through the AI under the `standard` profile (`tool_mode=all`).
Because that profile inherits all tools, `add_task` is **already**
visible to the inbox AI with zero glue code — that's the win.

**Important:** the spec previously claimed `inbox_ai_chat.system_prompt`
was an existing ConfigParam — it is **not**. As of today,
`InboxAIChatService.config_params()` exposes only `allowed_emails`,
`allowed_domains`, `required_subject`, and `ai_profile`. The system
prompt is an inlined `context_prefix` string in
`_process_message_locked` (lines 288–296). This feature must
therefore introduce the configurable prompt as a side change:

1. Add `_DEFAULT_INBOX_AI_CHAT_PROMPT` module-level constant in
   `core/services/inbox_ai_chat.py` containing the existing
   `context_prefix` text PLUS the new task guidance:
   > "If the email contains concrete action items the user should
   > track (deadlines, deliverables, follow-ups), you may call
   > ``add_task`` to add them to their list. Use the email's
   > ``Message-Id`` as ``idempotency_key`` so re-deliveries don't
   > create duplicates. Don't create tasks for FYI / informational
   > mail or for items the user has clearly already handled."
2. Add a `system_prompt` ConfigParam on `InboxAIChatService` with
   `multiline=True, ai_prompt=True, default=_DEFAULT_INBOX_AI_CHAT_PROMPT`.
3. Cache the active value in `self._system_prompt` in
   `on_config_changed`, falling back to the default on empty
   override (same pattern as `_DEFAULT_SUMMARY_PROMPT` here).
4. Replace the inlined `context_prefix` literal in
   `_process_message_locked` with `self._system_prompt`. Keep it
   prepended to the user message in the same `chat()` call shape
   (`user_message=self._system_prompt + "\n\n" + body`) — do NOT
   pass it via a separate `system_prompt=` kwarg unless the inbox
   AI's `chat()` call signature is reshaped, which is out of scope.

**Identity for inbox-AI `add_task` calls.**
`_process_message_locked` calls `set_current_user(UserContext.SYSTEM)`
to bypass per-user inbox visibility. Then it passes `user_ctx=user_ctx`
(the resolved sender) into `ai.chat(...)`. For tool execution, the
AI service injects `_user_id` from the *passed* `user_ctx` (the
sender), not from `get_current_user()`. So `add_task` runs as the
sender, not as SYSTEM, and the default-list resolution in §6.5 keys
on the sender's `owner_user_id`. The inbox-AI flow MUST set
`set_current_user(user_ctx)` immediately before the `ai.chat` call
so any tool that *does* read the contextvar (e.g., reads from
`get_current_user()` inside `add_task`) sees the same identity.
This is a small change in `inbox_ai_chat.py` documented as part of
this feature.

**User feedback when inbox-AI adds a task.**
The AI's reply email is sent verbatim. The implementer must update
the inbox-AI default prompt to include: "If you call ``add_task``,
mention the addition in plain English in your reply (e.g., 'I added
this to your todo list.')." This gives the user an in-band
confirmation. Additionally, the `task.created` event already fires;
the SPA's notification panel surfaces it for connected sessions —
no new event is needed.

**Allowlist-vs-default-list safety.**
A sender allow-listed via `allowed_domains` may not have a Gilbert
user record. In that case, `_resolve_user` returns a basic
`UserContext` with `user_id=email`. There may be no `task_lists`
row with `owner_user_id == email`. Default-list resolution must
fail loudly in this case rather than picking some other user's
list. Concretely, §6.5's rule 2 (`user's only owned list (if
exactly one exists)`) MUST scope to lists where
`owner_user_id == user_ctx.user_id`. If zero matches, the AI gets
the same "no list found, call task_lists" error and surfaces it to
the user via the email reply.

**Idempotency for re-delivered emails.**
The inbox already has `inbox_ai_chat_replied` to prevent reply-spam.
A second AI run for the same `message_id` is suppressed at
`_already_replied` time. Even so, on rare reflows, `add_task` MUST
de-duplicate by passing the `Message-Id` as `idempotency_key`
(prompt instruction above + the `tasks` collection's
`idempotency_key` index). Belt and suspenders.

### 7.8 Slash command argument ordering

For shell ergonomics:

- `/tasks add <title>` — title first, everything else is `kw=value`.
  Slash users can type `due="tomorrow 5pm"` — the slash parser
  resolves human strings via a small server-side `parse_when()`
  utility (deterministic, host-side date math; not the AI). The AI
  tool path remains strict ISO. Priority accepts strings:
  `priority=high|medium|low|urgent|none|0|1|2|3|4`.
  e.g. `/tasks add "Call dentist" due="tomorrow 3pm" priority=high`.
- `/tasks done <task_id>`. Idempotent.
- `/tasks cancel <task_id> reason="..."`.
- `/tasks delete <task_id> --yes`. Without `--yes`, returns the
  same `UIBlock` confirm as the AI tool path so a typo is
  recoverable.
- `/tasks update <task_id> due="..." priority=high tags=foo,bar`.
- `/tasks list status=open project="Home"`.
- `/tasks today`, `/tasks overdue`, `/tasks due tomorrow`,
  `/tasks summary` — zero args (or one for `due`).
- `/tasks lists` — zero args. Lists every list with `is_default`
  highlighted.

## 8. Events

All events emitted by `TasksService`. ACL prefix `tasks.` set in
`interfaces/acl.py` to level 100 (user) — same as `inbox.`.

| Event | Payload |
|---|---|
| `task.created` | `{list_id, task_id, title, due_at, due_at_tz, created_by_user_id, backend, sync_status}` |
| `task.completed` | `{list_id, task_id, title, completed_at, completed_by_user_id, backend}` |
| `task.updated` | `{list_id, task_id, changed_fields: list[str]}` |
| `task.cancelled` | `{list_id, task_id, title, completed_at, cancelled_by_user_id, reason, backend}` |
| `task.deleted` | `{list_id, task_id, soft: bool}` (`soft=true` for soft-delete; `false` after gc / admin force) |
| `task.due_soon` | `{list_id, task_id, title, due_at, due_at_tz, created_by_user_id, backend, lookahead_minutes}` |
| `task.push_failed` | `{list_id, task_id, title, last_push_error, retry_count, backend}` |
| `task.sync_recovered` | `{list_id, task_id}` (fired when a `pending_push` row finally syncs) |
| `tasks.list.created` | `{list_id, name, owner_user_id}` |
| `tasks.list.updated` | `{list_id, name, owner_user_id}` |
| `tasks.list.deleted` | `{list_id, name, owner_user_id}` |
| `tasks.list.shares.changed` | `{list_id, owner_user_id, shared_with_users, shared_with_roles}` |
| `tasks.list.degraded` | `{list_id, name, last_error, last_sync_at}` |
| `tasks.list.recovered` | `{list_id, name, last_sync_at}` |

`source` on every event is `"tasks"`.

> **Visibility filter.** The frontend maintains a cache of accessible
> `list_id`s and discards events whose `list_id` is not in the
> cache. The cache is invalidated on `tasks.list.shares.changed` and
> `auth.user.roles.changed` (mirror of how inbox does it). The WS
> fanout filter in `web/ws_protocol.py` should be extended with a
> `can_see_tasks_event` filter to validate at the server side too —
> but for v1, the role-prefix-level gate (level 100) is enough.

## 9. WS RPCs

| Frame | Description |
|---|---|
| `tasks.lists.list` | List accessible lists + access tag |
| `tasks.lists.get` | Single list |
| `tasks.lists.create` | Create — admin (caller becomes owner) |
| `tasks.lists.update` | Update — owner/admin |
| `tasks.lists.delete` | Delete cascade — owner/admin |
| `tasks.lists.test_connection` | Probe upstream — owner/admin |
| `tasks.lists.refresh` | Force-poll a list now (read-side latency override) |
| `tasks.lists.share_user` / `unshare_user` / `share_role` / `unshare_role` | Share edits |
| `tasks.list` | Search tasks (filters: list_id, backend, status, tag, project, due_before, due_after; `cursor` + `limit` for pagination — default `limit=50`, max `limit=200`) |
| `tasks.get` | One task |
| `tasks.add` | Create |
| `tasks.update` | Patch (only changed fields travel) |
| `tasks.complete` | Mark done (idempotent) |
| `tasks.cancel` | Mark cancelled with reason |
| `tasks.delete` | Soft-delete (admin can pass `force=true` for hard) |
| `tasks.restore` | Admin-only — clears `deleted_at`, sets `sync_status=pending_push` to re-create upstream if needed |
| `tasks.due_today` | Aggregated; uses requesting user's TZ |
| `tasks.due_window` | Aggregated by named window (today/tomorrow/this_week/this_month/overdue) |
| `tasks.overdue` | Aggregated |
| `tasks.summary` | AI summary (returns string) |
| `tasks.backends.list` | Registered backends + their `backend_config_params()` schemas — feeds the list edit drawer. `required_role="user"` (level 100) — read-only schema discovery, not admin-only. |

Error mapping mirrors inbox: `403` for permission errors, `404` for
unknown list/task, `400` for missing required args, `409` for
delete-with-pending.

## 10. Built-in backend: `LocalTaskBackend` (`integrations/local_tasks.py`)

The reference, vendor-free implementation. Tasks live in the same
`tasks` collection used for the cache layer of every other backend —
**but** the local backend's own state IS that collection, so the
"persist on poll" loop becomes "no-op poll" + "writes go directly to
storage."

### 10.1 Implementation notes

```python
from gilbert.interfaces.tasks import (
    StorageAwareTaskBackend,  # the @runtime_checkable Protocol from §4.1
    Task, TaskBackend, TaskStatus,
)

class LocalTaskBackend(TaskBackend):
    """Task backend backed entirely by entity storage. Vendor-free.

    Side-effect imported from ``core/services/tasks.py`` so the
    registry knows about it without a plugin.

    Acts as both source of truth and read cache — ``list_tasks``
    returns the storage rows for this list directly. ``add_task``
    is a confirm-only no-op (the service writes the row). The
    runtime never schedules a poll job for local lists (§6.6), but
    ``list_tasks`` is still implemented so the explicit
    ``refresh_list`` RPC can drive a manual refresh and so the ABC
    surface is satisfied.

    Satisfies ``StorageAwareTaskBackend`` (§4.1). External backends
    do NOT — this hook is a local-only concern. Don't copy this
    pattern into a third-party backend.
    """

    backend_name = "local"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []  # no credentials — purely local

    def __init__(self) -> None:
        self._list_id: str = ""
        self._storage: Any = None  # narrowed StorageBackend; see set_storage

    def set_storage(self, storage: object) -> None:
        """Receive entity storage. Satisfies StorageAwareTaskBackend.

        Service-only hook — TasksService calls this immediately after
        instantiation, BEFORE initialize(). External backends don't
        satisfy StorageAwareTaskBackend and never see this call.
        """
        self._storage = storage  # narrowed by usage; ABC stays vendor-shaped

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._list_id = str(cfg.get("list_id", ""))

    async def close(self) -> None:
        pass

    async def list_tasks(self, *, include_completed: bool = False, updated_since: str = "") -> list[Task]:
        filters = [
            Filter(field="list_id", op=FilterOp.EQ, value=self._list_id),
            Filter(field="deleted_at", op=FilterOp.EQ, value=""),
        ]
        if not include_completed:
            filters.append(Filter(field="status", op=FilterOp.EQ, value=TaskStatus.OPEN.value))
        if updated_since:
            filters.append(Filter(field="updated_at", op=FilterOp.GTE, value=updated_since))
        rows = await self._storage.query(Query(collection="tasks", filters=filters))
        return [Task.from_dict(row) for row in rows]

    async def add_task(self, task: Task) -> Task:
        # Stamp source_id = id for parity with external backends.
        if not task.source_id:
            task.source_id = task.id
        # Service writes the row — backend confirms / normalizes only.
        return task

    async def update_task(self, source_id: str, patch: dict[str, Any], *, etag: str = "") -> Task:
        # Service handles persistence; backend has nothing to push.
        # Return value is the patch as a Task with source_id stamped —
        # the service merges this back into the row.
        return Task(id=source_id, list_id=self._list_id, title="", source_id=source_id, **patch)

    async def complete_task(self, source_id: str) -> None:
        return  # service-level path persists; local backend has no upstream

    async def delete_task(self, source_id: str) -> None:
        return
```

> **Why the local backend is mostly no-op.** The service has to
> persist every task to entity storage anyway (so cross-backend
> aggregation works). Asking the local backend to *also* persist
> would either duplicate the row or require the service to skip the
> persist step for `local`. The cleaner pattern is: external backends
> push to upstream + the service mirrors locally; the local backend
> lets the service do all the work and only confirms `source_id`.
> Tests must verify the row count is 1 per task on the local
> backend (not 2), and that no `tasks-poll-{local_list_id}` job is
> scheduled.

> **`set_storage` is the storage-aware-protocol opt-in.** It is **not**
> a special method on the ABC. The protocol
> (`StorageAwareTaskBackend`) lives in `interfaces/tasks.py` (§4.1).
> The service narrows with `isinstance(backend, StorageAwareTaskBackend)`
> and calls `set_storage` only on backends that opt in. External
> backends never satisfy the protocol, so they never see the call.
> This is the same pattern as `UserBackendAware` (auth) and
> `AICapableTTSBackend` (tts) — see those for prior art and
> precedent. The naming (`*Aware*Backend` Protocol class +
> `set_*` method) is the established Gilbert convention.

## 11. External backend: `GoogleTasksBackend`
(`std-plugins/google/google_tasks.py`)

Added to the existing `google` std-plugin alongside `gmail.py`,
`gdrive_documents.py`, etc. Same auth pattern as `GmailBackend`:
service-account JSON + delegated user + Google API client.

### 11.1 Backend config params

```python
@classmethod
def backend_config_params(cls) -> list[ConfigParam]:
    return [
        ConfigParam(
            key="service_account_json",
            type=STRING,
            description="Google service account key (paste JSON content).",
            sensitive=True, restart_required=True, multiline=True,
        ),
        ConfigParam(
            key="delegated_user",
            type=STRING,
            description="Email of the user to impersonate via DWD.",
            restart_required=True,
        ),
        ConfigParam(
            key="tasklist_id",
            type=STRING,
            description=(
                "Google tasklist id to bind this Gilbert list to. "
                "Use 'Test connection' first to fetch the list of "
                "available tasklists for your account."
            ),
            restart_required=True,
        ),
    ]

@classmethod
def backend_actions(cls) -> list[ConfigAction]:
    return [
        ConfigAction(key="test_connection", label="Test connection",
                     description="List your Google Tasks lists."),
        ConfigAction(key="list_tasklists", label="Show available tasklists",
                     description="Returns id + title of every tasklist."),
    ]
```

### 11.2 Initialize / API surface

OAuth scopes: `https://www.googleapis.com/auth/tasks` (full read/write).

The backend builds the Tasks API resource exactly the way `GmailBackend`
builds Gmail's:

```python
from googleapiclient.discovery import build
from google.oauth2 import service_account

creds = service_account.Credentials.from_service_account_info(
    sa_info, scopes=["https://www.googleapis.com/auth/tasks"],
).with_subject(delegated_user)

self._service = await asyncio.to_thread(build, "tasks", "v1", credentials=creds)
```

### 11.3 Mapping Google → `Task`

| Google field | `Task` field | Notes |
|---|---|---|
| `id` | `source_id` | |
| `title` | `title` | |
| `notes` | `notes` | |
| `due` (RFC 3339) | `due_at` | Normalize to ISO UTC (`Z`); set `due_at_tz="UTC"` (Google's `due` field is always day-precision UTC midnight in practice — there is no time-of-day data). |
| `completed` (RFC 3339) | `completed_at` | Same |
| `status` (`needsAction` / `completed`) | `status` | Map to `OPEN` / `DONE` |
| _(no native priority)_ | `priority` | `NONE` |
| _(no native tags)_ | `tags` | `[]` |
| tasklist title | `project` | One Google tasklist = one project |

### 11.4 CRUD calls

- **List:** `service.tasks().list(tasklist=tasklist_id, showCompleted=include_completed,
  updatedMin=updated_since_iso_z).execute()`
- **Add:** `service.tasks().insert(tasklist=tasklist_id, body=body).execute()`,
  body fields: `title`, `notes`, `due` (RFC 3339Z), `status`.
- **Update:** `service.tasks().patch(tasklist=tasklist_id, task=source_id, body=patch).execute()`.
- **Complete:** `patch(body={"status": "completed"})`.
- **Delete:** `service.tasks().delete(tasklist=tasklist_id, task=source_id).execute()`.

All calls run through `asyncio.to_thread` because the Google client
is sync.

### 11.5 Updates to the google plugin

There are **two** manifests that must be updated, not one:

- `std-plugins/google/plugin.yaml` — add `google_tasks` to the
  `provides:` list.
- `std-plugins/google/plugin.py` — both the `PluginMeta.provides`
  attribute (runtime manifest) AND the `setup()` side-effect import:

```python
class GooglePlugin(Plugin):
    META = PluginMeta(
        name="google",
        provides=["gmail", "gdrive_documents", "google_auth",
                  "google_directory", "google_tasks"],  # ← +google_tasks
    )

    async def setup(self, context: PluginContext) -> None:
        from . import (  # noqa: F401
            gdrive_documents, gmail, google_auth, google_directory,
            google_tasks,  # ← new
        )
```

`std-plugins/google/pyproject.toml` already pulls
`google-api-python-client` for Gmail/Drive — same dep covers Tasks,
no new wheels.

### 11.6 Limitations and operator notes

These are real, non-trivial constraints that affect setup; the
spec calls them out so the implementer's settings UI / docs cover
them honestly.

- **DWD requires Google Workspace.** Service-account + delegated-user
  auth (the same pattern as `gmail.py`) only works for Workspace
  domains where an admin has authorized the service account. **Personal
  `gmail.com` accounts cannot be used with DWD.** A future feature
  (tracked separately) adds the per-user OAuth flow; until then,
  document this prerequisite in the settings UI's `service_account_json`
  field help text.
- **One Gilbert list = one Google tasklist.** Google Tasks accounts
  contain N tasklists ("My Tasks", "Shopping", "Work"). To bind to
  all of them, the user creates one Gilbert task list per Google
  tasklist with a different `tasklist_id`. The settings UI's
  `list_tasklists` `ConfigAction` enumerates available tasklists.
- **Existing Gmail service account may lack the `tasks` scope.**
  Workspace service-account scopes are configured in the admin
  console, not in the JSON key. If the user reuses their existing
  Gmail JSON for Tasks, the admin must also grant
  `https://www.googleapis.com/auth/tasks` (full read/write) to the
  service account's client ID. The settings UI's `test_connection`
  action surfaces "insufficient scope" errors clearly.
- **No webhooks.** Google Tasks API has no Pub/Sub or push
  notification surface (unlike Gmail / Calendar). Polling is the
  only sync mechanism — `_poll_runtime` runs at
  `poll_interval_sec` and uses `updatedMin` for delta semantics.

## 12. External backend: `TodoistBackend` (sketch — v1.1)

`std-plugins/todoist/` — new plugin directory.

### 12.1 Plugin layout

```
std-plugins/todoist/
    __init__.py
    plugin.yaml          # name=todoist, provides=[todoist_tasks]
    plugin.py            # imports todoist_backend module
    pyproject.toml       # depends on httpx>=0.27 (already in core)
    todoist_backend.py
    tests/
        conftest.py
        test_todoist_backend.py
```

### 12.2 Auth

- `api_token` ConfigParam (sensitive=True). User generates from
  Todoist Settings → Integrations → Developer.
- OAuth flow is supported by Todoist, but for v1.1 the API-token path
  is enough (matches the rest of Gilbert's "paste credential" UX).
- Test-connection action: `GET https://api.todoist.com/rest/v2/projects`
  with `Authorization: Bearer <token>`.

### 12.3 Mapping

| Todoist | `Task` |
|---|---|
| `id` (string) | `source_id` |
| `content` | `title` |
| `description` | `notes` |
| `due.datetime` (or `due.date`) | `due_at` |
| `is_completed` | `status` |
| `priority` (1=low … 4=urgent in their API; flip for ours: their 1 → ours `LOW`, etc.) | `priority` |
| `labels` | `tags` |
| `project_id` → resolved project name | `project` |

### 12.4 CRUD

REST endpoints (no SDK needed — `httpx`):

- List: `GET /rest/v2/tasks` (or `?project_id=…&filter=…`)
- Add: `POST /rest/v2/tasks` body `{content, description, due_string,
  priority, project_id, labels}`
- Update: `POST /rest/v2/tasks/{id}` body fields
- Close: `POST /rest/v2/tasks/{id}/close`
- Delete: `DELETE /rest/v2/tasks/{id}`

Rate limit: 1000 req/15 min/user — generous, no special handling
needed for v1.1.

### 12.5 Sync model

Todoist supports the **sync API** (`POST /sync/v9/sync`) with a
`sync_token` for incremental updates — drop-in for `updated_since`.
v1.1 implementer should use that, not full polls. Cache the
`sync_token` in `task_events_seen` keyed `f"{list_id}:_sync_token"`.

## 13. External backend: `CalDAVBackend` (sketch — v1.1)

`std-plugins/caldav/` — supports Apple Reminders (via iCloud CalDAV),
Fastmail Reminders, Nextcloud Tasks, anything else speaking CalDAV
VTODO.

### 13.1 Plugin layout

```
std-plugins/caldav/
    plugin.yaml    # name=caldav, provides=[caldav_tasks]
    plugin.py      # side-effect imports the backend module
    pyproject.toml # depends on caldav>=1.3 + icalendar>=5
    caldav_backend.py
    tests/
```

### 13.2 Backend config params

- `server_url` (e.g. `https://caldav.icloud.com`)
- `username` (Apple ID / Fastmail user)
- `app_password` (sensitive=True — Apple/Fastmail require app-specific
  passwords for CalDAV)
- `calendar_url` (URL of the specific reminders/tasks calendar; the
  test_connection action enumerates available calendars)

### 13.3 Mapping VTODO → `Task`

| VTODO property | `Task` field |
|---|---|
| `UID` | `source_id` |
| `SUMMARY` | `title` |
| `DESCRIPTION` | `notes` |
| `DUE` | `due_at` |
| `COMPLETED` | `completed_at` |
| `STATUS` (`NEEDS-ACTION` / `COMPLETED` / `CANCELLED`) | `status` |
| `PRIORITY` (1–9 in iCal) | `priority` (rescaled) |
| `CATEGORIES` | `tags` |
| Calendar `displayname` | `project` |

### 13.4 Sync model

CalDAV supports `getctag` + `etag`-based incremental sync. v1.1
implementer caches `etag` per VTODO in `task_events_seen` and uses
the calendar's `getctag` to short-circuit polls when nothing changed.

### 13.5 Apple-specific gotcha

iCloud CalDAV requires a hostname-rewrite dance after the first
auth — the server returns a per-user shard like
`https://p123-caldav.icloud.com`. The `caldav` Python library handles
this via `Principal.calendars()`. Implementer must use that, not raw
URL hardcoding.

### 13.6 v1.1 implementer open issues (NOT v1 spec gaps — flagged here)

These are *not* solved by the sketch above; the v1.1 implementer
of CalDAV must address each before declaring done:

- **Recurring VTODOs (RRULE).** Apple Reminders and other CalDAV
  servers heavily use `RRULE:FREQ=DAILY;…` for repeating items.
  The current `Task` model has no recurrence shape and "recurrence
  authoring" is out of scope (§0). For *reading* recurring tasks,
  pick one of: (a) ignore `RRULE` entirely — the task only appears
  on its first instance (broken UX); (b) expand to N future
  instances at sync time and store as separate rows (storage
  bloat); (c) generate "next occurrence" rows on the fly during
  `due_today` aggregation (correct but complex). v1.1 picks one
  before merging.
- **`RELATED-TO` (sub-tasks).** Out of scope per §0; the v1.1
  CalDAV backend ignores these properties for its initial cut and
  documents the limitation in the plugin README.
- **Etag conflict detection (`If-Match`).** The `Task` dataclass
  carries `etag` (§3.3) so CalDAV pushes can use `If-Match: <etag>`.
  On 412 (precondition failed), the backend raises a typed
  `StaleEtagError` and the service falls back to re-poll-and-merge
  (§6.7.5).
- **`getctag` short-circuit.** The CalDAV poll uses the calendar's
  `getctag` to decide whether anything changed at all. If
  `getctag` is unchanged from the last poll, skip the per-VTODO
  scan entirely. Cache the ctag in `task_events_seen[list_id:_ctag]`.

## 14. Greeting integration

`std-plugins`-or-core greeting service will call
`tasks_provider.summarize_today(user_ctx)` (or, if the greeting
service prefers to do its own AI call, `due_today()` + `overdue()`)
once per morning briefing.

The implementer of THIS feature does **not** need to wire greeting —
that's a separate change tracked in feature 04 / wherever greeting
lives. This spec only requires that the `TaskProvider` capability be
discoverable so a future greeting integration can resolve it via:

```python
tasks_svc = resolver.get_capability("tasks")
if isinstance(tasks_svc, TaskProvider):
    summary = await tasks_svc.summarize_today(user_ctx=...)
```

## 15. Wiring (`src/gilbert/core/app.py`)

In the existing service-registration block (around the `InboxService`
registration on line ~264):

```python
from gilbert.core.services.tasks import TasksService

self.service_manager.register(TasksService())
```

`TasksService` registers AFTER `InboxService` and BEFORE `AIService`
so `AIService._discover_tools` picks up its tool provider.

A side-effect import inside `core/services/tasks.py` triggers
backend registration:

```python
try:
    import gilbert.integrations.local_tasks  # noqa: F401
except ImportError:
    pass
```

External backends register themselves through their plugin's
`setup()` (e.g. `std-plugins/google/plugin.py`).

> **Layer compliance.** `core/services/tasks.py` MUST NOT import
> from `integrations/` directly except via the side-effect import
> noted above. It MUST NOT import any plugin module. Backend
> resolution at runtime is purely via `TaskBackend.registered_backends()`.

## 16. ACL prefix

In `interfaces/acl.py`:

```python
EVENT_VISIBILITY_PREFIXES = {
    ...,
    "tasks.": 100,    # user level — same as inbox.
    "task.": 100,
}
```

(both prefixes covered because per-task events are `task.*` and
list-level events are `tasks.*`.)

## 17. Bootstrap YAML

**None.** Like the inbox service, all task list configuration lives
in the `task_lists` entity collection. `gilbert.yaml` does not gain
a `tasks:` section. The service's `config_params()` exposes only
the global tunables listed in §6.4.

## 18. Tests

### 18.1 `tests/unit/test_tasks_service.py` — required coverage

Construct a `TasksService` with a **real test SQLite storage
backend** (per CLAUDE.md: "database tests use a real test SQLite
database — no mocking the DB"). Attach lightweight fakes for the
non-storage capabilities the service needs (event bus, scheduler,
access control, AI sampling). Exercise the public API. **Do not
start the real boot path** — instead drive each test path
explicitly so the assertions are sharp.

Mock pattern: `tests/unit/fakes.py` (shared) holds:
- `FakeEventBus` (collects publishes for assertion)
- `FakeScheduler` (records add_job/remove_job; tests call jobs
  manually instead of waiting wall time)
- `FakeAccessControl` (returns role levels per test fixture)
- `FakeAISampling` (records `complete_one_shot` calls; returns
  preset strings)

**Storage** is the real `SqliteStorageBackend` against a temp file —
this is the only way `Filter`/`FilterOp`/`SortField` queries
actually execute and the indexes (§3.2) prove they work.

Required test classes:

- **List CRUD**
  - create, update (no-restart fields), update (restart-on-change),
    delete, delete refuses with open tasks unless `force=True`.
- **Authorization matrix** (covers every `can_access_list` /
  `can_admin_list` / `determine_access` branch)
  - owner, admin, shared user, shared role, unrelated user.
  - admin who's also the owner → tag is `owner`, not `admin`.
- **Add / complete / update / delete task** happy path
  - persists the row, publishes the right event, returns the
    Task with `source_id` populated.
- **Aggregation**
  - `due_today` across two lists (one local, one fake-external) —
    only the user's accessible lists contribute.
  - `overdue` — open tasks with `due_at < now`.
  - `search_tasks(backend="google_tasks")` filters by backend.
- **Default-list resolution**
  - user has no default → error mentions `/tasks lists`.
  - user has one owned list → that's the default.
  - user has multiple owned, one is `is_default=True` → that one
    wins.
- **Polling**
  - external backend returns 3 tasks → 3 rows persisted, 3
    `task.created` events.
  - second poll returns 2 unchanged + 1 with new `updated_at` → 1
    `task.updated` event, no duplicate inserts.
  - completion in upstream → `task.completed` event.
  - **No poll job scheduled for local lists** — assert
    `FakeScheduler.add_job` was NOT called with a name matching
    `tasks-poll-{local_list_id}`.
  - Three consecutive failed polls mark the list `degraded_since`
    and publish `tasks.list.degraded`. A subsequent successful
    poll publishes `tasks.list.recovered` and clears
    `degraded_since`.
- **Push failures**
  - `add_task` succeeds locally + upstream → row persisted with
    `sync_status="synced"`.
  - `add_task` succeeds locally, upstream raises → row persisted
    with `sync_status="pending_push"`, AI tool returns success
    with "syncing in background" suffix, no exception bubbles to
    caller.
  - `tasks-sync-tick` retries `pending_push` rows; after
    `max_push_retries` failures, sets `sync_status="push_failed"`,
    publishes `task.push_failed`.
  - `update_task` patch-only push: assert backend
    `update_task(source_id, patch={"title": "x"})` is called with
    only the changed field — not the whole Task.
- **Idempotency**
  - Two `add_task` calls with the same effective key
    (`(_user_id, _conversation_id, _tool_call_id)` injected) →
    one row, second call returns the same id.
  - Explicit `idempotency_key` parameter (inbox-AI Message-Id
    case) round-trips identically.
  - `complete_task` called twice → no error, single row in DONE.
  - `delete_task` against a 404 upstream → success, soft-delete
    row stamped.
- **Conflict resolution**
  - Push pending; upstream poll returns the same task with
    DIFFERENT field values for fields not in the pending patch →
    upstream values merged, pending fields preserved.
  - Stale-etag mismatch path: backend raises `StaleEtagError`,
    service re-polls and rebases the patch, retries once. Second
    success: row updated. Second mismatch: `push_failed`.
- **Time zones**
  - `due_today` for a user with `tz="America/Los_Angeles"` at host
    time = `2026-05-09T22:00:00-04:00` (ET) returns tasks with
    `due_at` between `2026-05-09T07:00:00Z` and
    `2026-05-10T06:59:59Z` — i.e. PT day, not ET day.
  - Cross-DST date: a task `due_at=2026-11-01T08:30:00-08:00`
    (PST) appears in `due_today` on the user's PT calendar day
    correctly across DST fallback.
- **Due-soon tick**
  - task with `due_at` 5 min from now and `due_soon_lookahead_minutes=10`
    → fires `task.due_soon` exactly once.
  - rescheduling `due_at` past the cutoff and ticking again does NOT
    re-fire.
- **AI tool dispatch**
  - `add_task` tool injects `created_by_user_id` from `_user_id`.
  - `summarize_today` falls back to deterministic summary when AI
    capability is absent.
  - `summary_prompt` ConfigParam respects override + falls back to
    default on empty string.
- **`TaskProvider` Protocol satisfaction**
  - `assert isinstance(svc, TaskProvider)` — runtime check enforces
    the protocol surface.
- **Multi-user safety**
  - Two concurrent `add_task` calls (different users) must both
    land, each tagged with the right `created_by_user_id`. The
    test sets `set_current_user(...)` before each call and asserts
    the rows.
  - **Same-list collision:** Two concurrent `add_task` calls to
    the SAME list under different users → both rows persist with
    distinct `_id`s, no `is_default` race or storage `put` clobber.
- **GC and retention**
  - `tasks-gc-tick` deletes DONE rows older than `retention_days`
    and leaves newer DONE rows + all OPEN rows untouched.
  - Soft-deleted rows (`deleted_at != ""`) older than
    `retention_days` are hard-deleted; younger ones are kept.
  - Orphan `task_events_seen` rows whose `list_id` doesn't exist
    are removed.
- **`source_id` not exposed**
  - The `add_task` / `list_tasks` / `get_task` tool returns must
    NOT contain a `source_id` key. Verify by JSON-parsing the tool
    return and asserting absence.

### 18.2 `tests/unit/test_local_task_backend.py`

- `set_storage` + `initialize(list_id)` then `add_task` (driven by
  service) results in exactly 1 row in storage (no double-insert).
- `assert isinstance(local_backend, StorageAwareTaskBackend)` —
  runtime protocol satisfaction.
- `list_tasks(updated_since=...)` honors the filter when given.
- `list_tasks` excludes soft-deleted rows (`deleted_at != ""`).
- `complete_task` is a no-op at the backend level — service
  handles the storage update.
- `update_task` returns a Task with `source_id` populated — service
  merges the patch.

### 18.3 `std-plugins/google/tests/test_google_tasks.py`

- Mock `googleapiclient.discovery.build` to return a fake service
  resource. Cover:
  - `list_tasks` empty / non-empty.
  - `add_task` returns Google's `id` as `source_id` and the
    upstream's normalized fields.
  - `update_task(source_id, patch)` issues PATCH only with the
    fields in `patch` — assert the body sent to Google contains
    *only* those keys.
  - `complete_task` sends `{"status": "completed"}`. Calling on an
    already-completed task does NOT raise.
  - `delete_task` calls `tasks().delete(...)`. A 404 upstream is
    swallowed.
  - **Date conversion:** RFC3339 UTC → ISO UTC `Z`. A Google
    `due="2026-05-10T00:00:00.000Z"` round-trips to `Task.due_at =
    "2026-05-10T00:00:00Z"`.
  - **DWD impersonation paths:** `delegated_user` set vs unset.
    With `delegated_user=""`, the backend raises a clear
    configuration error — Google Tasks needs DWD. Assert the test
    mocks `Credentials.with_subject(...)` is called when
    `delegated_user` is non-empty, and not called otherwise.
- The fake service must be set on the backend via the same path
  `initialize()` uses; monkeypatching `_service` directly is fine
  for tests (matches how `gmail.py`'s tests work).

### 18.4 No-mocking-the-thing-you-test rule

Reminder from CLAUDE.md: do **not** mock `TasksService` itself —
build it for real with the real `SqliteStorageBackend` and fakes
for the rest. Same for `LocalTaskBackend` — exercise it against
the real backend. Shared fakes go in `tests/unit/fakes.py`.

### 18.5 Optional contract tests

If the env var `GOOGLE_TASKS_TEST_CREDS` is set (path to a
service-account JSON with the `tasks` scope), run a parameterized
contract test that hits the real Google Tasks API: create → list →
update → complete → delete. Skipped in CI by default; run locally
before PRs that touch the backend. This is the only way to catch
upstream protocol drift (Google API behavior changes). Same
pattern is worth applying to Todoist and CalDAV when their
backends ship in v1.1.

## 19. Frontend (settings only — full SPA page is out of scope)

The implementer of this feature ships a `settings.tasks` panel
allowing admins to:

- Create / edit / delete task lists.
- Pick a backend from `tasks.backends.list`.
- Edit backend-specific credential fields rendered dynamically
  from `backend_config_params()` (reuse the shared `ConfigField`
  component the inbox page uses).
- Toggle `is_default` per list.
- Trigger `test_connection` and see the result inline.
- Manage shares (user / role).

The settings panel lives at `frontend/src/components/settings/TasksPanel.tsx`
and is mounted via the existing settings-category slot mechanism
(`<PluginPanelSlot slot="settings.tasks">` already wired by
`Configurable.config_category = "Productivity"` if there's a slot
for it; if not, add one). No new top-level `/tasks` route.

A future feature adds a real `/tasks` SPA page (with kanban / list
views, drag-to-complete, etc.). That is explicitly v2.

> **Plugin-shipped frontend rule.** If we later want a richer task UI
> contributed by, say, the Todoist plugin (e.g. project-tree picker),
> that lives under `std-plugins/todoist/frontend/` per the plugin UI
> rules — never under `frontend/src/`. See
> `memory-plugin-ui-extensions.md`.

## 20. Migration / rollout

- **No migrations needed** — entity storage doesn't have a schema,
  the new collections (`task_lists`, `tasks`, `task_events_seen`)
  appear empty on first start.
- **Gradual adoption** — users start with zero lists. The settings
  panel lets them create a `local` list (zero credentials needed) on
  first visit. The greeting service stays silent about tasks until
  at least one list exists — explicit `if list_accessible_lists():`
  gate.
- **External backends are opt-in** — adding a Google Tasks list
  requires the user to paste the same service-account JSON used for
  Gmail (or a separate one with `tasks` scope). The settings UI
  surfaces an action to re-use existing Google credentials from
  Gmail (post-v1).
- **No breaking changes** to anything outside the new files. The
  inbox service, scheduler, and AI service are untouched except for
  `inbox_ai_chat` changes (new `system_prompt` ConfigParam +
  `set_current_user(user_ctx)` before the AI call) described in
  §7.7.

## 21. Open questions / decisions to confirm with the user

| Question | Resolution |
|---|---|
| `add_task` natural-language `due_at`? | **AI tool path: strict ISO.** The AI resolves natural language via `system_datetime` first. **Slash command path: human strings accepted** via a small server-side `parse_when()` utility (deterministic; not the AI). |
| `priority` on slash form? | **Yes** — `priority=high\|medium\|low\|urgent\|none\|0\|1\|2\|3\|4`. The AI tool also accepts dual-input (string or int) per §7.1. |
| Completing in upstream removes the local row? | **No** — row stays `status=DONE` with `completed_at`. **GC IS shipped in v1** as `tasks-gc-tick` + `retention_days` (default 90). |
| Local backend recurrence? | **No** — explicitly out of scope. Use scheduler-driven repeats. |
| `archived` lists? | **No** for v1. `delete_list` cascade is the deletion model. |
| Hard-delete vs. soft-delete for `delete_task`? | **Resolved: soft-delete** is the AI / slash default; hard-delete is admin-only via `tasks.delete force=true` WS RPC. Soft rows expire via `tasks-gc-tick`. |
| Cross-backend list moves on `update_task`? | **No for v1.** `list_id` is forbidden in `update_task` patches. Delete-and-recreate is the documented upgrade path. |
| Idempotency on `add_task`? | **Yes — shipped in v1.** Implicit key from `(_user_id, _conversation_id, _tool_call_id)` for AI calls; explicit `idempotency_key` parameter for callers like inbox-AI. |
| `cancel_task` AI tool? | **Yes — shipped in v1.** Status enum already had `CANCELLED`; the missing tool is added per §7.4. |
| Multi-list disambiguation when no default? | **Resolved: UIBlock select.** §7.1 specifies the AI returns a select-list UIBlock when ambiguous rather than erroring. The "first time cliff" is closed. |
| `list_tasks` includes `notes` by default? | **Yes** — saves a round-trip on "tell me about that task." The bytes are small. |
| Multi-tier model routing for `summarize_today`? | Always routes through `tasks.ai_profile` (default `light`) — independent of the calling chat's tier. Documented in §6.4 description. |
| `set_default_list` AI tool? | **No for v1.** Default is set in settings UI. The `task_lists` tool description tells the AI to suggest "edit in settings" rather than hallucinate the tool. |

### Genuinely open (deferred to user / next round)

- **Inbox-AI default-list safety for non-Gilbert senders.** §7.7 says
  default-list resolution scopes to `owner_user_id == user_ctx.user_id`
  and fails loudly otherwise. **Confirm:** is "fail loudly" the right
  default, or should we silently skip the `add_task` call and just
  reply normally? Recommendation: fail loudly (the user can see the
  failure in the email reply and act).
- **Webhook surface for Todoist (v1.2+).** Sketched as a future ABC
  extension. **Confirm direction:** add `on_external_change(list_id)`
  on `TasksService` that backends call back into via the resolver,
  or a different shape?
- **`task.overdue` event.** Not defined in v1 (`due_soon` is forward-
  looking only). **Confirm:** v1.1 add a periodic "task became
  overdue" event for already-past tasks the user hasn't seen yet?
- **`UserContext.tz` typed field.** This spec uses
  `UserContext.metadata["tz"]`. Promoting it to a typed field on
  `UserContext` is a cross-cutting change that affects every
  service — outside this feature. Track separately.

## 22. Architecture-checklist self-audit

Before opening the PR, run through these and confirm:

- [ ] No `import gilbert.core.services.*` from `integrations/`.
- [ ] No `import gilbert.core.services.*` from
      `std-plugins/google/google_tasks.py`.
- [ ] No `import gilbert.integrations.local_tasks` from
      `interfaces/`. The integrations side imports the *interface*,
      never the other way.
- [ ] `TasksService` does not directly import `LocalTaskBackend`
      *for use* — only the side-effect registration import.
- [ ] All `isinstance` checks against capabilities use
      `@runtime_checkable Protocol`s from `interfaces/`, never
      concrete service classes. The `set_storage` injection
      uses `StorageAwareTaskBackend` from `interfaces/tasks.py`.
- [ ] No per-request state on `self`. Grep
      `self\._current_|self\._active_|self\._pending_` in
      `core/services/tasks.py` — empty result expected.
      (Note: `_pending_attachments` lives on `InboxAIChatService`,
      not `TasksService` — different service, different rules.)
- [ ] Every AI prompt is a `ConfigParam(ai_prompt=True, multiline=True)`,
      cached on `self._summary_prompt` in `on_config_changed`. No
      `_DEFAULT_*PROMPT` constant referenced at the call site. Same
      rule applied to the new `inbox_ai_chat.system_prompt`
      ConfigParam introduced as part of §7.7.
- [ ] `slash_namespace` set on `TasksService` if it's a plugin
      (it isn't — it's core). `slash_group="tasks"` on every tool.
- [ ] Every `ToolDefinition` has `slash_command` + `slash_help`.
- [ ] `tasks.list.test_connection` action defined on every backend
      that has any external dependency (so the settings UI's "Test
      connection" button works). `LocalTaskBackend.backend_actions()`
      legitimately returns `[]` — settings UI hides the button.
- [ ] Every write tool has documented idempotency semantics
      (`add_task` via `idempotency_key`; `complete_task` /
      `delete_task` natively idempotent).
- [ ] Every datetime stored in entity storage has a documented
      timezone (UTC for `created_at` / `updated_at` / `completed_at`
      / `deleted_at`; UTC-with-companion-IANA for `due_at` +
      `due_at_tz`). Per §3.5.
- [ ] `source_id` is never returned in AI tool responses or WS RPC
      payloads visible to non-admin users — server-internal only.
- [ ] `README.md` (root) mentions tasks in the integration section
      and update the std-plugins README to mention the new
      `google_tasks` backend in the `google` plugin row.
- [ ] `std-plugins/CLAUDE.md` doesn't need updates — the layout
      rules already cover the (future) `todoist` and `caldav`
      plugins.
- [ ] Root `CLAUDE.md` — grep for any "AI capability table" or
      service list that would go stale with the new tasks service.
      Update if found.

## 23. Memory updates required

After implementation lands:

- **Create** `.claude/memory/memory-tasks-service.md` with:
  - The data model (3 collections + indexes).
  - Auth helpers + sharing semantics.
  - Polling + due-soon flow.
  - AI tool list and `summarize_today` prompt config.
  - Why `LocalTaskBackend` satisfies `StorageAwareTaskBackend`
    (the one place the local backend departs from the pure ABC),
    with cross-references to the prior art (`UserBackendAware`,
    `TunnelAwareAuthBackend`, `AICapableTTSBackend`).
  - List of registered backends (local + google_tasks; todoist /
    caldav added when shipped).
- **Update** `.claude/memory/MEMORIES.md` with a one-liner index
  entry pointing to the new memory file.
- **Update** `memory-inbox-service.md` if the system_prompt default
  string for `inbox_ai_chat` changes substantially — only a one-line
  addition is expected, but mention it in the memory's "Design
  decisions" section.

## 24. File-by-file summary (implementer checklist)

New files:

```
src/gilbert/interfaces/tasks.py                   ~250 lines
src/gilbert/core/services/tasks.py                ~900 lines
src/gilbert/integrations/local_tasks.py           ~120 lines
std-plugins/google/google_tasks.py                ~250 lines
tests/unit/test_tasks_service.py                  ~700 lines
tests/unit/test_local_task_backend.py             ~120 lines
std-plugins/google/tests/test_google_tasks.py     ~200 lines
frontend/src/components/settings/TasksPanel.tsx   ~400 lines
frontend/src/hooks/useTasksApi.ts                 ~150 lines (core, not plugin — TasksService is core)
.claude/memory/memory-tasks-service.md            new
```

Modified files:

```
src/gilbert/core/app.py            +3 lines (register TasksService)
src/gilbert/interfaces/acl.py      +2 lines (tasks. / task. prefixes)
std-plugins/google/plugin.py       +3 lines (PluginMeta.provides AND
                                              side-effect import)
std-plugins/google/plugin.yaml     +1 line  (provides google_tasks)
src/gilbert/core/services/inbox_ai_chat.py
                                   ~30 lines:
                                   - new _DEFAULT_INBOX_AI_CHAT_PROMPT
                                     constant (merged context + task
                                     guidance)
                                   - new system_prompt ConfigParam
                                     (multiline=True, ai_prompt=True)
                                   - cache active prompt in
                                     self._system_prompt
                                   - replace inlined context_prefix
                                     literal with self._system_prompt
                                   - add set_current_user(user_ctx)
                                     before the ai.chat call so
                                     add_task and other tools see the
                                     resolved sender
README.md                          row for tasks service in the integration table
std-plugins/README.md              add "Google Tasks" line to the google plugin entry
.claude/memory/MEMORIES.md         +1 line (index entry)
.claude/memory/memory-inbox-service.md
                                   +1 paragraph in "Design decisions"
                                   noting the new system_prompt
                                   ConfigParam introduced for the
                                   inbox-AI flow
```

Total estimate: ~3000 lines of new code + ~3000 lines of frontend/tests
+ docs. Single PR is reasonable; if the reviewer prefers, the
frontend settings panel can split into a follow-up since the
backend tests prove the WS RPCs work.

## 25. Definition of done

A reviewer should be able to verify:

1. `uv run pytest tests/unit/test_tasks_service.py` — all green.
2. `uv run pytest tests/unit/test_local_task_backend.py` — all green.
3. `uv run pytest std-plugins/google/tests/test_google_tasks.py` —
   all green (with mocked Google client).
4. `uv run mypy src/gilbert/interfaces/tasks.py
   src/gilbert/core/services/tasks.py
   src/gilbert/integrations/local_tasks.py` — clean.
5. `uv run ruff check src/ tests/ std-plugins/` — clean.
6. Manual: create a `local` task list via Settings, add a task via
   `/tasks add "Foo"`, see it appear in `/tasks list`, complete it
   with `/tasks done <id>`, see `task.completed` in the event log.
7. Manual (with credentials): create a `google_tasks` list, run
   `tasks.lists.test_connection`, add a task via slash, verify it
   appears in the user's Google Tasks UI within ~poll-interval
   seconds.
8. The "check the rules" architecture audit (§22) passes with no
   findings.
9. The new memory file exists and is linked in the index.

— END SPEC —

## Revision Log — Round 2

Reviews resolved: `.spec-reviews/round1/05-architect.md`,
`.spec-reviews/round1/05-product.md`,
`.spec-reviews/round1/05-engineering.md`.

### Architecture (architect review)

- **`bind_storage` → `set_storage`** with the `*Aware*Backend` Protocol
  convention. Renamed `LocalStorageAware` → `StorageAwareTaskBackend`
  and **moved its declaration from `core/services/tasks.py` into
  `interfaces/tasks.py` §4.1** to mirror `UserBackendAware`,
  `TunnelAwareAuthBackend`, and `AICapableTTSBackend`. Storage
  parameter typed as `object`, narrowed at the implementation. Updated
  §10.1, §22 audit, and the local-backend code sample.
- **Local backend polling contradiction resolved.** §6.6 now states
  explicitly that local lists do NOT schedule a poll job (Option A
  from the architect's review). `LocalTaskBackend.list_tasks` is
  retained for the explicit `refresh_list(list_id)` RPC and to
  satisfy the ABC. New audit-checklist item + new test asserts the
  scheduler has no `tasks-poll-{local_list_id}` job after boot.
- **Added `_require_access` / `_require_admin` helpers** + new
  exception classes (`TaskListPermissionError`, `TaskListNotFoundError`,
  `TaskNotFoundError`, `TaskBackendUnavailableError`) in §6.0,
  parallel to inbox.
- **`due_soon_fired` field** now listed in §3.1 and §6.9 specifies
  flag-write-before-publish ordering with single-process-safety note.
- **Outbox absence justified** in §3.1 ("Why no `tasks_outbox`").
- **`CachedTaskListLister.cached_task_lists` → `cached_lists`**
  (architect's recommended cosmetic). Doc note explains the consumer
  expectation and that the protocol should be dropped if no v1
  consumer exists.
- **Two-place Google plugin manifest update** (`plugin.yaml::provides`
  AND `plugin.py::PluginMeta.provides` + side-effect import) made
  explicit in §11.5 and §24.
- **DWD impersonation tests** added to §18.3.
- `tasks.backends.list` WS RPC now explicitly `required_role="user"`.

### Product (product review)

- **Phantom `inbox_ai_chat.system_prompt` fixed.** §7.7 fully
  rewritten: the spec now requires the implementer to **introduce**
  this ConfigParam (with `_DEFAULT_INBOX_AI_CHAT_PROMPT` constant,
  cached in `self._system_prompt`, replacing the inlined
  `context_prefix`). §24's modified-files list reflects the ~30-line
  edit to `inbox_ai_chat.py`.
- **Inbox-AI identity flow specified.** §7.7 documents that
  `set_current_user(user_ctx)` must be set before `ai.chat()` so
  tools see the sender's identity. Default-list resolution in §6.5 /
  §7.7 now explicitly scopes to `owner_user_id == user_ctx.user_id`
  to prevent dumping tasks into the wrong user's list when an
  allow-listed sender has no Gilbert account.
- **Inbox-AI user feedback** documented: AI must mention the task
  addition in the email reply; `task.created` event also surfaces
  in the SPA notification panel for connected sessions.
- **`delete_task` confirmation surface.** Switched to
  **soft-delete by default** with a `UIBlock` Confirm/Cancel form
  (§7.5). Slash form requires `--yes`. Hard-delete is admin-only
  via `tasks.delete force=true` WS RPC and not exposed to AI / slash.
  Recovery is automatic until `retention_days` (default 90) elapses.
- **Multi-list disambiguation.** §7.1 now describes the four-step
  resolution including a UIBlock `select` element when ambiguous —
  the "first time cliff" of erroring on multi-list-no-default is
  closed.
- **Single-source `summarize_today`.** §7.6 explicitly: AI tool
  calls the Provider method. Greeting calls the same Provider
  method. One prompt, one assembler, one fallback.
- **`cancel_task` AI tool added** (§7.4) — closes the gap between
  delete (loses history) and complete (lies about completion).
- **`get_task` AI tool added** (§7.3) for full-detail single-task
  fetch.
- **`tasks_due(window=...)` parameterized aggregation tool added**
  (§7.4); `due_today` / `overdue` retained as aliases.
- **`source_id` hidden** from AI tool returns. Audit checklist
  item + test added.
- **AI tool descriptions enriched** for `add_task`: list resolution
  rules, `tags` vs `priority` guidance, project read-only-ness for
  external backends, dual-input priority (string or int).
- **`list_tasks` includes `notes` by default** to save round-trips.
- **`task_lists` return shape specified** (`is_default`,
  `poll_enabled`, `degraded_since` exposed).
- **`update_task` forbidden fields enumerated** (§6.7.2): id,
  list_id, source_id, status, created_*, idempotency_key, sync
  fields, due_soon_fired, deleted_at. Cross-list moves NOT
  supported in v1 (delete-and-recreate is the upgrade path).
- **Slash `due_at` natural-language** accepted via server-side
  `parse_when()` for humans; AI tool path remains strict ISO.
- **`due_soon_fired` reschedule semantics** documented: clearing on
  reschedule-past-cutoff is intentional re-fire; already-overdue
  backfilled tasks do not fire (forward-looking event).

### Engineering (SWE review)

- **Per-backend sync model table** added at §2.3.
- **Webhook surface** explicitly deferred to v1.2+ (§2.3 + §12.5);
  v1 is poll-only.
- **Push-failure handling specified** as **local-first with
  reconciliation** (§6.7.1). New fields on `Task`: `sync_status`,
  `last_push_attempt_at`, `last_push_error`, `retry_count`. New
  recurring `tasks-sync-tick` job. New events `task.push_failed` and
  `task.sync_recovered`. New ConfigParams: `push_timeout_sec`
  (default 15), `max_push_retries` (default 5),
  `degraded_after_failures` (default 3).
- **`update_task` ABC changed** from `update_task(task: Task)` to
  `update_task(source_id: str, patch: dict[str, Any], *, etag: str = "")`
  to enable PATCH semantics and field-level conflict resolution.
- **Conflict resolution policy** specified in §6.7.5: upstream
  authoritative for non-pending fields; pending patch fields win
  until they sync; CalDAV `If-Match` / `StaleEtagError` →
  re-poll-and-rebase, retry once.
- **Idempotency** specified in §6.7.4. Implicit key from injected
  `(_user_id, _conversation_id, _tool_call_id)`; explicit
  `idempotency_key` parameter for inbox-AI Message-Id case.
  `complete_task` natively idempotent. `delete_task` swallows
  upstream 404. New `tasks(idempotency_key)` index.
- **Retention** specified in §6.2 step 8 + new ConfigParam
  `retention_days` (default 90). Daily `tasks-gc-tick` deletes DONE
  / CANCELLED rows past retention, soft-delete tombstones past
  retention, and orphan `task_events_seen`. New `deleted_at` field
  on `Task` for soft-delete.
- **Time zones** redone in §3.5: `created_at` / `updated_at` /
  `completed_at` / stamps are **ISO UTC `Z`**; `due_at` is **ISO UTC
  `Z`** paired with `due_at_tz` (IANA name). `due_today` / `overdue`
  use the **requesting user's TZ** from `UserContext.metadata["tz"]`,
  not the host's. New tests cover cross-TZ and DST-fallback cases.
- **Google Tasks limitations** documented in §11.6 (DWD requires
  Workspace, one Gilbert list = one Google tasklist, scope-on-
  existing-service-account caveat, no webhooks).
- **CalDAV v1.1 implementer items flagged** in §13.6 (RRULE,
  RELATED-TO, etag conflict, ctag short-circuit).
- **Tests use real SQLite**, not a fake (§18.1). Fakes only for
  non-storage capabilities (event bus, scheduler, access control,
  AI sampling). Optional contract tests against real Google Tasks
  via `GOOGLE_TASKS_TEST_CREDS` env var (§18.5).
- **Pagination** added to `tasks.list` WS RPC (§9): `cursor` +
  `limit` (default 50, max 200).
- **Cross-list `(status, due_at)` index** explicitly called out in
  §3.2. New `(idempotency_key)`, `(sync_status)`, `(list_id, deleted_at)`
  indexes added.
- **Push timeout** via `asyncio.wait_for(..., timeout=self._push_timeout_sec)`
  (§6.7.1).

### Conflicts between reviewers

- **`bind_storage` vs `initialize(config={"storage_provider": ...})`.**
  Architect required the `set_storage` Protocol pattern. SWE
  recommended the config-time-injection alternative. **Resolved in
  favor of the architect's `set_storage` Protocol** because that's
  the established Gilbert convention (`UserBackendAware`,
  `TunnelAwareAuthBackend`, `AICapableTTSBackend`) and consistency
  trumps the marginal "purer ABC" win. The SWE concern (a special
  hook adds a code path) is mitigated by the explicit Protocol
  living in `interfaces/tasks.py` and being type-checked, not a
  duck-typed `getattr`.
- **`due_at` time format.** Original spec said local-naive everywhere.
  Architect was silent. SWE pushed for UTC. Product did not weigh in.
  **Resolved in favor of SWE's UTC + IANA companion** because the
  multi-user TZ correctness argument is decisive and external
  backends already speak tz-aware time.
- **Hard delete vs. soft delete.** Product asked for soft. SWE
  recommended preserving the field for soft. Architect did not
  weigh in. **Resolved: soft by default**, hard reserved for admin.

### Items intentionally not changed

- **`cached_task_lists` → `cached_lists` rename** kept. The protocol
  itself is a "drop if unused" candidate per its docstring — the
  implementer decides.
- **`task_lists` tool name vs. `task_list`** kept as `task_lists`
  (the inbox precedent is `inbox_mailboxes`-style noun-first
  naming; the inconsistency with verb-first `add_task` is
  documented as intentional in product nits).
- **Slash command name prefixes** (`add_task` vs hypothetical
  `task_add`) kept noun-positioned. `slash_group="tasks"` does the
  disambiguation work in the slash UI.
- **`summary_prompt` "do not start with 'Here's…'" guidance** —
  punted as a nit; the bundled default is conservative and
  admins can edit.
