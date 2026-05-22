"""Tasks interfaces — task model, backend ABC, capability protocols, auth helpers.

Shared by the core ``TasksService``, the web layer, and plugins that
provide task backends. Imports only from other ``interfaces`` modules —
never from ``core/``, ``integrations/``, ``web/``, or ``storage/``.

Closest analog: ``interfaces/inbox.py``. The structural shape (Task +
TaskList dataclasses, ``can_access_list`` / ``can_admin_list`` helpers,
``TaskProvider`` capability protocol, error taxonomy, backend ABC with
universal registry pattern) is a deliberate copy of the inbox model —
swap "mailbox" for "list" and "message" for "task".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Protocol, cast, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigAction, ConfigActionResult, ConfigParam

# ── Errors ───────────────────────────────────────────────────────────


class TaskError(Exception):
    """Base error type raised by ``TaskBackend`` operations."""


# Convenience alias — concrete backends typically catch the base class
# under a "Backend"-prefixed name to mirror their other typed errors.
TaskBackendError = TaskError


class TaskBackendAuthError(TaskError):
    """Raised when a backend rejects credentials (e.g. HTTP 401/403)."""


class TaskBackendNotFoundError(TaskError):
    """Raised when an upstream task / list isn't found (HTTP 404).

    Backends MUST swallow this for ``complete_task`` and ``delete_task``
    (those are naturally idempotent), but should raise it for ``get`` /
    ``list_tasks`` so the service can surface a clean error.
    """


class TaskBackendConflictError(TaskError):
    """Raised on stale-etag mismatch (CalDAV ``If-Match`` 412)."""


class TaskBackendRateLimitError(TaskError):
    """Raised when a backend reports rate-limit exceeded.

    ``retry_after_sec`` is honored by the service when scheduling the
    next push attempt.
    """

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class TaskBackendTransientError(TaskError):
    """Raised on network / 5xx / timeout — the service may retry."""


# ── Enums ────────────────────────────────────────────────────────────


class TaskStatus(StrEnum):
    """Lifecycle status of a single task."""

    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(IntEnum):
    """Task priority. Higher integer = higher priority.

    Backends with native priorities map to/from this enum at the
    boundary (Todoist's 1-4 flips: their 4 = our URGENT, their 1 = our
    LOW; CalDAV's 1-9 rescales).
    """

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4


class SyncStatus(StrEnum):
    """Local-first reconciliation state for a task row.

    - ``SYNCED`` — local row matches the upstream provider.
    - ``PENDING_PUSH`` — local mutation queued for upstream push (retry
      via ``tasks-sync-tick``).
    - ``PUSH_FAILED`` — push exhausted retries; row stays local until
      the user / admin intervenes.
    - ``PENDING_DELETE`` — soft-delete tombstone whose upstream delete
      hasn't landed yet.
    """

    SYNCED = "synced"
    PENDING_PUSH = "pending_push"
    PUSH_FAILED = "push_failed"
    PENDING_DELETE = "pending_delete"


class ListAccess(StrEnum):
    """How a user came to have access to a task list — used for UI grouping."""

    OWNER = "owner"
    ADMIN = "admin"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class TaskList:
    """A configured task list — one provider binding, owned by a user.

    Lists are stored in the ``task_lists`` collection. The owner is set
    at creation time and never changes automatically; sharing is granted
    separately via ``shared_with_users`` / ``shared_with_roles``.

    ``backend_name`` selects the runtime ``TaskBackend`` (one of
    ``"local"``, ``"google_tasks"``, future plugins).
    ``backend_config`` is the per-list credential / endpoint payload —
    schema is backend-defined via ``backend_config_params()``.
    """

    id: str = ""
    name: str = ""
    backend_name: str = "local"
    backend_config: dict[str, Any] = field(default_factory=dict)
    owner_user_id: str = ""
    shared_with_users: list[str] = field(default_factory=list)
    shared_with_roles: list[str] = field(default_factory=list)
    poll_enabled: bool = True
    poll_interval_sec: int = 300
    is_default: bool = False
    created_at: str = ""
    last_sync_at: str = ""
    degraded_since: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "backend_name": self.backend_name,
            "backend_config": dict(self.backend_config),
            "owner_user_id": self.owner_user_id,
            "shared_with_users": list(self.shared_with_users),
            "shared_with_roles": list(self.shared_with_roles),
            "poll_enabled": self.poll_enabled,
            "poll_interval_sec": self.poll_interval_sec,
            "is_default": self.is_default,
            "created_at": self.created_at,
            "last_sync_at": self.last_sync_at,
            "degraded_since": self.degraded_since,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskList:
        return cls(
            id=str(data.get("id") or data.get("_id") or ""),
            name=str(data.get("name", "")),
            backend_name=str(data.get("backend_name", "local") or "local"),
            backend_config=cast("dict[str, Any]", data.get("backend_config") or {}),
            owner_user_id=str(data.get("owner_user_id", "")),
            shared_with_users=cast("list[str]", data.get("shared_with_users") or []),
            shared_with_roles=cast("list[str]", data.get("shared_with_roles") or []),
            poll_enabled=bool(data.get("poll_enabled", True)),
            poll_interval_sec=int(data.get("poll_interval_sec", 300) or 300),
            is_default=bool(data.get("is_default", False)),
            created_at=str(data.get("created_at", "")),
            last_sync_at=str(data.get("last_sync_at", "")),
            degraded_since=str(data.get("degraded_since", "")),
            last_error=str(data.get("last_error", "")),
        )


@dataclass
class Task:
    """Provider-neutral task. Persisted in the ``tasks`` entity collection.

    Time-zone semantics:

    - ``created_at`` / ``updated_at`` / ``completed_at`` /
      ``last_push_attempt_at`` / ``deleted_at`` are **ISO UTC with a
      trailing 'Z'** — produced via ``datetime.now(UTC).isoformat()``
      with the ``+00:00`` → ``Z`` rewrite at the boundary.
    - ``due_at`` is **ISO UTC with a trailing 'Z'** paired with
      ``due_at_tz`` (IANA name, e.g. ``"America/Los_Angeles"``). The
      ``due_at`` is the precise instant; ``due_at_tz`` is the wall-clock
      zone the user authored the date in, used for day-boundary
      arithmetic and round-trip display.

    Internal-only fields (never returned to AI tool callers):

    - ``source_id`` — backend native id (Google ``tasks/abc``, Todoist
      numeric, CalDAV ``UID``, etc.). Equal to ``id`` for the local
      backend.
    - ``etag`` — opaque, backend-defined. Used by CalDAV for
      ``If-Match``; empty for backends that don't need it.
    - ``due_soon_fired`` — internal dedupe flag for ``task.due_soon``.
    - ``sync_status`` / ``last_push_*`` / ``retry_count`` — local-first
      reconciliation bookkeeping.
    - ``deleted_at`` — soft-delete tombstone; non-empty rows hidden from
      default queries.
    """

    id: str = ""
    list_id: str = ""
    title: str = ""
    source_id: str = ""
    notes: str = ""
    due_at: str = ""
    due_at_tz: str = ""
    completed_at: str = ""
    status: TaskStatus = TaskStatus.OPEN
    priority: TaskPriority = TaskPriority.NONE
    tags: list[str] = field(default_factory=list)
    project: str = ""
    created_at: str = ""
    updated_at: str = ""
    created_by_user_id: str = ""
    idempotency_key: str = ""
    sync_status: SyncStatus = SyncStatus.SYNCED
    last_push_attempt_at: str = ""
    last_push_error: str = ""
    retry_count: int = 0
    etag: str = ""
    deleted_at: str = ""
    due_soon_fired: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "_id": self.id,
            "list_id": self.list_id,
            "title": self.title,
            "source_id": self.source_id,
            "notes": self.notes,
            "due_at": self.due_at,
            "due_at_tz": self.due_at_tz,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "priority": int(self.priority.value),
            "tags": list(self.tags),
            "project": self.project,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by_user_id": self.created_by_user_id,
            "idempotency_key": self.idempotency_key,
            "sync_status": self.sync_status.value,
            "last_push_attempt_at": self.last_push_attempt_at,
            "last_push_error": self.last_push_error,
            "retry_count": int(self.retry_count),
            "etag": self.etag,
            "deleted_at": self.deleted_at,
            "due_soon_fired": bool(self.due_soon_fired),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        # Defensive priority cast — the value can arrive as int, str,
        # or be malformed; round-trip back to TaskPriority.NONE on any
        # invalid value rather than blowing up storage reads.
        try:
            priority = TaskPriority(int(data.get("priority", 0) or 0))
        except (ValueError, TypeError):
            priority = TaskPriority.NONE
        try:
            status = TaskStatus(str(data.get("status", "open") or "open"))
        except ValueError:
            status = TaskStatus.OPEN
        try:
            sync_status = SyncStatus(
                str(data.get("sync_status", "synced") or "synced"),
            )
        except ValueError:
            sync_status = SyncStatus.SYNCED
        return cls(
            id=str(data.get("_id") or data.get("id") or ""),
            list_id=str(data.get("list_id", "")),
            title=str(data.get("title", "")),
            source_id=str(data.get("source_id", "")),
            notes=str(data.get("notes", "")),
            due_at=str(data.get("due_at", "")),
            due_at_tz=str(data.get("due_at_tz", "")),
            completed_at=str(data.get("completed_at", "")),
            status=status,
            priority=priority,
            tags=cast("list[str]", data.get("tags") or []),
            project=str(data.get("project", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            created_by_user_id=str(data.get("created_by_user_id", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
            sync_status=sync_status,
            last_push_attempt_at=str(data.get("last_push_attempt_at", "")),
            last_push_error=str(data.get("last_push_error", "")),
            retry_count=int(data.get("retry_count", 0) or 0),
            etag=str(data.get("etag", "")),
            deleted_at=str(data.get("deleted_at", "")),
            due_soon_fired=bool(data.get("due_soon_fired", False)),
        )


# ── Authorization helpers ────────────────────────────────────────────


def can_access_list(
    user_ctx: UserContext,
    task_list: TaskList,
    *,
    is_admin: bool = False,
) -> bool:
    """Can this user read / add / complete / update tasks in this list?

    Admin, owner, any user in ``shared_with_users``, or any user with a
    role in ``shared_with_roles`` has full access. "Full access" means
    read + add + complete + update — but **not** list settings or share
    edits (those are gated by ``can_admin_list``).
    """
    if is_admin:
        return True
    if user_ctx.user_id == task_list.owner_user_id:
        return True
    if user_ctx.user_id in task_list.shared_with_users:
        return True
    return bool(user_ctx.roles & set(task_list.shared_with_roles))


def can_admin_list(
    user_ctx: UserContext,
    task_list: TaskList,
    *,
    is_admin: bool = False,
) -> bool:
    """Can this user edit list settings, change shares, or delete it?

    Only the owner or a system admin. Shared users — even with full
    access — cannot change configuration or reassign sharing.
    """
    if is_admin:
        return True
    return user_ctx.user_id == task_list.owner_user_id


def determine_access(
    user_ctx: UserContext,
    task_list: TaskList,
    *,
    is_admin: bool = False,
) -> ListAccess | None:
    """Return how the user has access to this list, or ``None`` if none.

    Precedence: owner > admin > shared_user > shared_role. Owner beats
    admin because owner is the more durable relationship — an admin
    who's also the owner should see "owner" in the UI.
    """
    if user_ctx.user_id == task_list.owner_user_id:
        return ListAccess.OWNER
    if is_admin:
        return ListAccess.ADMIN
    if user_ctx.user_id in task_list.shared_with_users:
        return ListAccess.SHARED_USER
    if user_ctx.roles & set(task_list.shared_with_roles):
        return ListAccess.SHARED_ROLE
    return None


# ── TaskBackend ABC ──────────────────────────────────────────────────


class TaskBackend(ABC):
    """Abstract task provider — pull source for read sync, push for writes.

    All persisted reads (search, due_today, overdue) come from entity
    storage; the backend is only consulted to (a) populate that storage
    on poll and (b) accept outbound writes immediately.

    Concrete subclasses set ``backend_name`` and are auto-registered via
    ``__init_subclass__``. See ``memory-backend-pattern.md``.
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
        """Backend-specific config (credentials, list / project ids, etc.)."""
        return []

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        """Backend-specific action buttons (Test connection, etc.)."""
        return []

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        """Invoke a backend-level action by key. Default: unknown action."""
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    @abstractmethod
    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        """Set up HTTP client, auth, etc. Called once per runtime."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP clients, file handles, etc.)."""
        ...

    # ── Pull ─────────────────────────────────────────────────────────

    @abstractmethod
    async def list_tasks(
        self,
        *,
        include_completed: bool = False,
        updated_since: str = "",
    ) -> list[Task]:
        """Return every task currently visible to this backend instance.

        ``updated_since`` is an ISO UTC timestamp ('Z') — implementers
        that support delta polls should honor it; implementers that
        don't can ignore it and return everything (the service will
        upsert and dedupe via ``source_id``).
        """
        ...

    # ── Push ─────────────────────────────────────────────────────────

    @abstractmethod
    async def add_task(self, task: Task) -> Task:
        """Create a task in the upstream provider.

        The returned ``Task`` MUST carry the upstream's ``source_id``
        (and any updated fields the provider normalized). The caller
        will persist the returned object — the input is only a draft.
        """
        ...

    @abstractmethod
    async def update_task(
        self,
        source_id: str,
        patch: dict[str, Any],
        *,
        etag: str = "",
    ) -> Task:
        """Patch only the fields in ``patch`` on the upstream task.

        Patch-shaped (not full-Task-shaped) so backends can issue PATCH
        semantics and Gilbert's local pending edits don't clobber
        unrelated fields the user changed in the upstream UI between
        the last poll and this push. The returned ``Task`` carries the
        upstream's post-patch state, including a fresh ``etag`` if
        applicable.

        ``etag`` is opaque and backend-defined; backends that need
        ``If-Match`` semantics (CalDAV) raise
        :class:`TaskBackendConflictError` on stale-etag mismatch so the
        service can re-poll-and-merge.
        """
        ...

    @abstractmethod
    async def complete_task(self, source_id: str) -> None:
        """Mark a task complete in the upstream provider.

        Separate from ``update_task`` because some providers expose a
        cheap dedicated ``complete`` endpoint. Naturally idempotent:
        calling twice on an already-completed task MUST succeed (no
        error). Backends that get a 4xx from the upstream for
        already-done MUST swallow it and return.
        """
        ...

    @abstractmethod
    async def delete_task(self, source_id: str) -> None:
        """Delete a task in the upstream provider. Naturally idempotent
        — backends MUST swallow upstream 404 ('already gone') and
        return successfully."""
        ...

    # ── Optional capability surfaces ─────────────────────────────────

    def supports_projects(self) -> bool:
        """Whether the backend exposes user-visible groupings (Google
        Tasks ``tasklists``, Todoist ``projects``). Defaults to
        ``False``; subclasses that group tasks override.
        """
        return False

    async def list_projects(self) -> list[str]:
        """Return the human-readable project names. Only meaningful when
        ``supports_projects()`` is True.
        """
        return []


# ── Storage-aware Protocol (opt-in for the local backend) ────────────


@runtime_checkable
class StorageAwareTaskBackend(Protocol):
    """Task backends that need entity storage injected.

    The local backend's "upstream" *is* core's entity store; external
    backends own their own upstream and never need this. ``TasksService``
    calls ``set_storage(storage)`` immediately after instantiation,
    BEFORE ``initialize()``, on any backend that satisfies this
    protocol.

    Mirror of ``UserBackendAware`` (auth), ``TunnelAwareAuthBackend``
    (auth), and ``AICapableTTSBackend`` (tts). Same naming convention:
    ``*Aware*Backend`` Protocol class + ``set_*`` method.

    The ``storage`` parameter is typed as ``object`` — implementations
    narrow at the boundary (the local backend casts to
    ``StorageBackend`` privately). This keeps ``interfaces/tasks.py``
    decoupled from any specific storage type.
    """

    def set_storage(self, storage: object) -> None: ...


# ── Capability protocols ─────────────────────────────────────────────


@runtime_checkable
class TaskProvider(Protocol):
    """What plugins / other services consume from ``TasksService``.

    Resolved via ``resolver.get_capability("tasks")`` and
    ``isinstance``-checked against this protocol — never against the
    concrete ``TasksService`` class.

    Mirror of ``InboxProvider`` — reads use the current user from
    ``gilbert.interfaces.context.get_current_user`` for visibility filtering;
    mutations take ``user_ctx`` explicitly.
    """

    async def add_task(
        self,
        list_id: str,
        task: Task,
        user_ctx: UserContext,
        *,
        idempotency_key: str = "",
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

    async def cancel_task(
        self,
        task_id: str,
        user_ctx: UserContext,
        *,
        reason: str = "",
    ) -> Task: ...

    async def delete_task(
        self,
        task_id: str,
        user_ctx: UserContext,
        *,
        force: bool = False,
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

    async def summarize_today(
        self,
        user_ctx: UserContext,
    ) -> str: ...

    async def get_list(self, list_id: str) -> TaskList | None: ...

    async def list_accessible_lists(
        self,
        user_ctx: UserContext,
    ) -> list[TaskList]: ...


@runtime_checkable
class CachedTaskListLister(Protocol):
    """For ConfigurationService dynamic-choice resolution — same shape
    as ``CachedMailboxLister`` in ``interfaces/inbox.py``.

    Consumed by ``ConfigurationService._resolve_dynamic_choices`` to
    populate ``choices_from="task_lists"`` dropdowns on settings pages.
    """

    @property
    def cached_lists(self) -> list[TaskList]: ...


__all__ = [
    "CachedTaskListLister",
    "ListAccess",
    "StorageAwareTaskBackend",
    "SyncStatus",
    "Task",
    "TaskBackend",
    "TaskBackendAuthError",
    "TaskBackendConflictError",
    "TaskBackendError",
    "TaskBackendNotFoundError",
    "TaskBackendRateLimitError",
    "TaskBackendTransientError",
    "TaskError",
    "TaskList",
    "TaskPriority",
    "TaskProvider",
    "TaskStatus",
    "can_access_list",
    "can_admin_list",
    "determine_access",
]
