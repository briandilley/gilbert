"""Tasks service — multi-list task aggregator with pluggable backends.

Each task list is owned by a user and can be shared with individual
users and/or roles. The service runs one ``TaskBackend`` instance per
list; external backends also get a ``tasks-poll-{list_id}`` scheduler
job. Local lists do NOT schedule a poll job — the local backend's
"upstream" is core's entity store, so polling would be self-
referential. Tasks are persisted in ``tasks`` (tagged with
``list_id``); writes use **local-first reconciliation** (§6.7) — a row
lands in storage immediately with ``sync_status=pending_push``, the
push attempt happens inline, and a recurring ``tasks-sync-tick``
retries any rows still pending.

Authorization is centralized in ``interfaces/tasks.py`` —
``can_access_list`` gates read/add/complete/update, and
``can_admin_list`` gates settings and share edits.

Single-source rule: the ``summarize_today`` AI tool calls the
:class:`TaskProvider` method. The greeting service's direct call goes
through the same method. One prompt, one assembler, one fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.core.context import get_current_user
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.core.services._ui_blocks import confirm_or_execute
from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tasks import (
    StorageAwareTaskBackend,
    SyncStatus,
    Task,
    TaskBackend,
    TaskBackendAuthError,
    TaskBackendConflictError,
    TaskBackendNotFoundError,
    TaskBackendRateLimitError,
    TaskBackendTransientError,
    TaskList,
    TaskPriority,
    TaskProvider,
    TaskStatus,
    can_access_list,
    can_admin_list,
    determine_access,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput

# Side-effect import: registers ``LocalTaskBackend`` in
# ``TaskBackend._registry`` so the service can discover it without
# importing the concrete class. Per CLAUDE.md layer rules the only
# permitted import from ``integrations/`` is this side-effect form.
try:
    import gilbert.integrations.local_tasks  # noqa: F401
except ImportError:
    pass

logger = logging.getLogger(__name__)

_TASK_LISTS_COLLECTION = "task_lists"
_TASKS_COLLECTION = "tasks"
_TASK_EVENTS_SEEN_COLLECTION = "task_events_seen"

_DEFAULT_SUMMARY_PROMPT = """\
You are Gilbert's daily task summarizer. The user is about to start
their day and wants a brief, encouraging overview of what's on their
plate. Given the JSON list of open tasks below — each with title, due
date, project, and tags — produce a concise English summary in 2–4
sentences. Lead with what's due today, then call out anything overdue,
then wrap with a one-line nudge. Keep it warm and human. Do not list
every task verbatim; cluster related items.
"""

_SYNC_TICK_INTERVAL_SEC = 30
_DUE_SOON_TICK_INTERVAL_SEC = 60
_GC_TICK_INTERVAL_SEC = 24 * 60 * 60


# ── Exception types ─────────────────────────────────────────────────


class TaskListPermissionError(PermissionError):
    """Raised when a caller lacks access to a task list."""


class TaskListNotFoundError(LookupError):
    """Raised when a list_id does not resolve."""


class TaskNotFoundError(LookupError):
    """Raised when a task_id does not resolve (or is soft-deleted)."""


class TaskBackendUnavailableError(RuntimeError):
    """Raised when an upstream push fails after exhausting retries."""


# ── Internal state ──────────────────────────────────────────────────


@dataclass
class _ListRuntime:
    """In-memory per-list state: the list config + live backend."""

    task_list: TaskList
    backend: TaskBackend
    poll_job_name: str = ""
    failure_count: int = 0


def _now_utc_iso() -> str:
    """Return ISO 8601 UTC timestamp with a trailing 'Z'."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _resolve_user_tz(user_ctx: UserContext) -> ZoneInfo:
    """Resolve the requesting user's timezone for day-boundary math.

    Uses ``UserContext.tz`` (typed field added in feature 03). Falls
    back to host local TZ then UTC if invalid / missing.
    """
    name = user_ctx.tz
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
        except Exception:
            pass
    # Host fallback — use host's "local" zone if available, else UTC.
    try:
        return ZoneInfo(time.tzname[0])
    except Exception:
        return ZoneInfo("UTC")


def _coerce_priority(value: Any) -> TaskPriority:
    """Accept int (0–4), numeric string, or named string priority."""
    if isinstance(value, bool):
        # bool is a subclass of int — exclude explicitly.
        return TaskPriority.NONE
    if isinstance(value, int):
        try:
            return TaskPriority(max(0, min(4, value)))
        except ValueError:
            return TaskPriority.NONE
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"none", ""}:
            return TaskPriority.NONE
        if normalized == "low":
            return TaskPriority.LOW
        if normalized == "medium" or normalized == "med":
            return TaskPriority.MEDIUM
        if normalized == "high":
            return TaskPriority.HIGH
        if normalized == "urgent":
            return TaskPriority.URGENT
        # Numeric string fallback.
        try:
            return TaskPriority(max(0, min(4, int(normalized))))
        except (ValueError, TypeError):
            return TaskPriority.NONE
    return TaskPriority.NONE


# ── TasksService ────────────────────────────────────────────────────


class TasksService(Service):
    """Multi-list task service with polling, push reconciliation, AI tools.

    Capabilities: tasks, ai_tools, ws_handlers
    Events: task.{created,completed,updated,deleted,cancelled,due_soon,
                  push_failed,sync_recovered}
            tasks.list.{created,updated,deleted,shares.changed,
                        degraded,recovered}
    """

    slash_namespace = "tasks"

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None
        self._scheduler: SchedulerProvider | None = None
        self._access_control: AccessControlProvider | None = None
        self._ai: AISamplingProvider | None = None
        self._resolver: ServiceResolver | None = None

        self._runtimes: dict[str, _ListRuntime] = {}
        self._cached_lists: list[TaskList] = []

        self._enabled: bool = True
        self._max_summary_tasks: int = 30
        self._default_poll_interval_sec: int = 300
        self._due_soon_lookahead_minutes: int = 10
        self._summary_prompt: str = _DEFAULT_SUMMARY_PROMPT
        self._ai_profile: str = "light"
        self._push_timeout_sec: int = 15
        self._max_push_retries: int = 5
        self._degraded_after_failures: int = 3
        self._retention_days: int = 90

    # ── Capability accessors ─────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "tasks"

    @property
    def cached_lists(self) -> list[TaskList]:
        """Sync snapshot of all task lists — used by config dynamic choices."""
        return list(self._cached_lists)

    # ── Service metadata ─────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tasks",
            capabilities=frozenset({"tasks", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset(
                {"event_bus", "configuration", "access_control", "ai_chat"}
            ),
            events=frozenset(
                {
                    "task.created",
                    "task.completed",
                    "task.updated",
                    "task.deleted",
                    "task.restored",
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
                }
            ),
            ai_calls=frozenset({"tasks_summary"}),
            toggleable=True,
            toggle_description="Tasks / todo lists",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError(
                "entity_storage capability does not provide StorageProvider"
            )
        self._storage = storage_svc.backend

        # Indexes per spec §3.2.
        for index in (
            IndexDefinition(
                collection=_TASK_LISTS_COLLECTION, fields=["owner_user_id"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["list_id", "status"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["list_id", "due_at"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["list_id", "source_id"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["status", "due_at"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["idempotency_key"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["sync_status"]
            ),
            IndexDefinition(
                collection=_TASKS_COLLECTION, fields=["list_id", "deleted_at"]
            ),
        ):
            await self._storage.ensure_index(index)

        # Optional capabilities.
        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        acl_svc = resolver.get_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            self._access_control = acl_svc

        ai_svc = resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AISamplingProvider):
            self._ai = ai_svc

        self._resolver = resolver

        # Load global config.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section_safe(self.config_namespace)

        await self.on_config_changed(section)

        if not self._enabled:
            logger.info("Tasks service disabled via configuration")
            return

        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise TypeError(
                "scheduler capability does not provide SchedulerProvider"
            )
        self._scheduler = scheduler_svc

        # Boot one-shot — defer backend init off the start critical path.
        self._scheduler.add_job(
            name="tasks-boot",
            schedule=Schedule.once_after(0),
            callback=self._boot_runtimes,
            system=True,
        )
        # Recurring sweep: retry pending pushes.
        self._scheduler.add_job(
            name="tasks-sync-tick",
            schedule=Schedule.every(_SYNC_TICK_INTERVAL_SEC),
            callback=self._sync_tick,
            system=True,
        )
        # Recurring sweep: fire task.due_soon events.
        self._scheduler.add_job(
            name="tasks-due-soon-tick",
            schedule=Schedule.every(_DUE_SOON_TICK_INTERVAL_SEC),
            callback=self._due_soon_tick,
            system=True,
        )
        # Daily GC: hard-delete completed / soft-deleted rows past
        # retention.
        self._scheduler.add_job(
            name="tasks-gc-tick",
            schedule=Schedule.every(_GC_TICK_INTERVAL_SEC),
            callback=self._gc_tick,
            system=True,
        )

        logger.info(
            "Tasks service started (boot deferred, sync tick=%ds, "
            "due_soon tick=%ds, gc tick=%ds, retention=%dd)",
            _SYNC_TICK_INTERVAL_SEC,
            _DUE_SOON_TICK_INTERVAL_SEC,
            _GC_TICK_INTERVAL_SEC,
            self._retention_days,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            for runtime in list(self._runtimes.values()):
                if runtime.poll_job_name:
                    with contextlib.suppress(Exception):
                        self._scheduler.remove_job(runtime.poll_job_name)
            for name in (
                "tasks-boot",
                "tasks-sync-tick",
                "tasks-due-soon-tick",
                "tasks-gc-tick",
            ):
                with contextlib.suppress(Exception):
                    self._scheduler.remove_job(name)

        for runtime in list(self._runtimes.values()):
            try:
                await runtime.backend.close()
            except Exception:
                logger.exception(
                    "Error closing backend for task list %s",
                    runtime.task_list.id,
                )
        self._runtimes.clear()
        logger.info("Tasks service stopped")

    async def _boot_runtimes(self) -> None:
        try:
            lists = await self._load_lists()
        except Exception:
            logger.exception("Tasks boot: failed to load lists")
            return

        self._cached_lists = list(lists)

        for task_list in lists:
            if task_list.poll_enabled:
                try:
                    await self._start_runtime(task_list)
                except Exception:
                    logger.exception(
                        "Tasks boot: failed to start runtime for %s",
                        task_list.id,
                    )

        logger.info("Tasks boot: %d runtime(s) started", len(self._runtimes))

    async def _refresh_cache(self) -> None:
        try:
            self._cached_lists = await self._load_lists()
        except Exception:
            logger.exception("Tasks: failed to refresh list cache")

    async def _start_runtime(self, task_list: TaskList) -> None:
        """Instantiate a backend + register a poll job (external lists only)."""
        assert self._scheduler is not None
        assert self._storage is not None

        backends = TaskBackend.registered_backends()
        backend_cls = backends.get(task_list.backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown task backend: {task_list.backend_name}")

        backend = backend_cls()

        # Inject storage on backends that opt in (the local backend) BEFORE
        # initialize() so settings reads can already touch storage if
        # needed. External backends never satisfy this Protocol.
        if isinstance(backend, StorageAwareTaskBackend):
            backend.set_storage(self._storage)

        settings = dict(task_list.backend_config)
        # Pass list_id through so backends that scope their queries by
        # list (the local backend) can read it from the config dict.
        settings.setdefault("list_id", task_list.id)
        await backend.initialize(settings)

        runtime = _ListRuntime(task_list=task_list, backend=backend)

        # Local lists do NOT schedule a poll job — the local backend's
        # "upstream" is core's entity store; running an empty poll loop
        # is wasted work and contradicts the local backend's
        # self-referential ``list_tasks``.
        if task_list.backend_name != "local":
            poll_job_name = f"tasks-poll-{task_list.id}"
            callback = self._make_poll_callback(task_list.id)
            self._scheduler.add_job(
                name=poll_job_name,
                schedule=Schedule.every(task_list.poll_interval_sec),
                callback=callback,
                system=True,
            )
            runtime.poll_job_name = poll_job_name

        self._runtimes[task_list.id] = runtime
        logger.info(
            "Task list runtime started: id=%s backend=%s poll=%s",
            task_list.id,
            task_list.backend_name,
            "no-op (local)"
            if task_list.backend_name == "local"
            else f"every {task_list.poll_interval_sec}s",
        )

    async def _stop_runtime(self, list_id: str) -> None:
        runtime = self._runtimes.pop(list_id, None)
        if runtime is None:
            return
        if self._scheduler is not None and runtime.poll_job_name:
            with contextlib.suppress(Exception):
                self._scheduler.remove_job(runtime.poll_job_name)
        try:
            await runtime.backend.close()
        except Exception:
            logger.exception("Error closing backend for task list %s", list_id)
        logger.info("Task list runtime stopped: id=%s", list_id)

    async def _restart_runtime(self, task_list: TaskList) -> None:
        await self._stop_runtime(task_list.id)
        if task_list.poll_enabled:
            await self._start_runtime(task_list)

    async def _ensure_runtime(self, task_list: TaskList) -> _ListRuntime:
        """Return the runtime for a list, lazily starting it if needed.

        Used by the local-first push path so that even
        ``poll_enabled=False`` lists have a backend instance available
        for outbound writes.
        """
        runtime = self._runtimes.get(task_list.id)
        if runtime is None:
            await self._start_runtime(task_list)
            runtime = self._runtimes.get(task_list.id)
        if runtime is None:
            raise TaskBackendUnavailableError(
                f"Could not start runtime for list {task_list.id}",
            )
        return runtime

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "tasks"

    @property
    def config_category(self) -> str:
        return "Productivity"

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
                description=(
                    "Default poll interval for new task lists (seconds)."
                ),
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
                description=(
                    "Maximum number of tasks the daily summary digests."
                ),
                default=30,
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description=(
                    "AI profile used by ``summarize_today``. Used both by "
                    "the summarize_today AI tool (slash + chat) AND by "
                    "the greeting service's direct TaskProvider call. "
                    "Cheaper tiers (default 'light') are recommended — "
                    "daily summaries don't benefit from advanced models."
                ),
                default="light",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="push_timeout_sec",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-call timeout for outbound writes to external "
                    "backends. Hanging the AI tool call until the upstream "
                    "HTTP client gives up is a UX disaster — bound the wait."
                ),
                default=15,
            ),
            ConfigParam(
                key="max_push_retries",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum number of retry attempts before a pending_push "
                    "task is marked push_failed and task.push_failed is "
                    "published."
                ),
                default=5,
            ),
            ConfigParam(
                key="degraded_after_failures",
                type=ToolParameterType.INTEGER,
                description=(
                    "Number of consecutive failed polls/pushes before a "
                    "list's runtime is marked degraded "
                    "(task_lists.degraded_since set, tasks.list.degraded "
                    "event published)."
                ),
                default=3,
            ),
            ConfigParam(
                key="retention_days",
                type=ToolParameterType.INTEGER,
                description=(
                    "Days to keep DONE / CANCELLED / soft-deleted tasks "
                    "before the daily tasks-gc-tick hard-deletes them. "
                    "Set to 0 to disable GC entirely."
                ),
                default=90,
            ),
            ConfigParam(
                key="summary_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for summarize_today. Drives how the AI "
                    "condenses today's task list into a brief daily "
                    "summary. Leave blank to use the bundled default."
                ),
                default=_DEFAULT_SUMMARY_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", self._enabled))
        self._max_summary_tasks = int(
            config.get("max_summary_tasks", self._max_summary_tasks)
        )
        self._default_poll_interval_sec = int(
            config.get(
                "default_poll_interval_sec", self._default_poll_interval_sec
            )
        )
        self._due_soon_lookahead_minutes = int(
            config.get(
                "due_soon_lookahead_minutes", self._due_soon_lookahead_minutes
            )
        )
        self._ai_profile = str(
            config.get("ai_profile", self._ai_profile) or self._ai_profile
        )
        self._push_timeout_sec = int(
            config.get("push_timeout_sec", self._push_timeout_sec)
        )
        self._max_push_retries = int(
            config.get("max_push_retries", self._max_push_retries)
        )
        self._degraded_after_failures = int(
            config.get(
                "degraded_after_failures", self._degraded_after_failures
            )
        )
        self._retention_days = int(
            config.get("retention_days", self._retention_days)
        )
        # AI prompts are configurable: empty override falls back to default.
        self._summary_prompt = (
            str(config.get("summary_prompt", "") or "") or _DEFAULT_SUMMARY_PROMPT
        )

    # ── ConfigAction provider ────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        # Per-list backend actions are exposed via the list edit UI,
        # not the global service config. Forward whatever any registered
        # backend declares so the dropdown can show available actions.
        return all_backend_actions(
            registry=TaskBackend.registered_backends(),
            current_backend=None,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(None, key, payload)

    # ── Internal: authorization helpers ──────────────────────────────

    def _is_admin(self, user_ctx: UserContext) -> bool:
        if user_ctx.user_id == UserContext.SYSTEM.user_id:
            return True
        if self._access_control is not None:
            return self._access_control.get_effective_level(user_ctx) <= 0
        return "admin" in user_ctx.roles

    def _require_access(
        self,
        task_list: TaskList,
        user_ctx: UserContext,
    ) -> None:
        if not can_access_list(
            user_ctx, task_list, is_admin=self._is_admin(user_ctx)
        ):
            raise TaskListPermissionError(
                f"User {user_ctx.user_id!r} cannot access task list "
                f"{task_list.id!r}",
            )

    def _require_admin(
        self,
        task_list: TaskList,
        user_ctx: UserContext,
    ) -> None:
        if not can_admin_list(
            user_ctx, task_list, is_admin=self._is_admin(user_ctx)
        ):
            raise TaskListPermissionError(
                f"User {user_ctx.user_id!r} cannot administer task list "
                f"{task_list.id!r}",
            )

    # ── List CRUD ────────────────────────────────────────────────────

    async def _load_lists(self) -> list[TaskList]:
        assert self._storage is not None
        rows = await self._storage.query(Query(collection=_TASK_LISTS_COLLECTION))
        return [TaskList.from_dict(row) for row in rows]

    async def list_lists(self) -> list[TaskList]:
        return await self._load_lists()

    async def list_accessible_lists(
        self,
        user_ctx: UserContext,
    ) -> list[TaskList]:
        is_admin = self._is_admin(user_ctx)
        all_lists = await self._load_lists()
        return [
            tl
            for tl in all_lists
            if can_access_list(user_ctx, tl, is_admin=is_admin)
        ]

    async def get_list(self, list_id: str) -> TaskList | None:
        assert self._storage is not None
        row = await self._storage.get(_TASK_LISTS_COLLECTION, list_id)
        if row is None:
            return None
        return TaskList.from_dict(row)

    async def _require_list(self, list_id: str) -> TaskList:
        task_list = await self.get_list(list_id)
        if task_list is None:
            raise TaskListNotFoundError(f"Task list not found: {list_id}")
        return task_list

    async def create_list(
        self,
        task_list: TaskList,
        user_ctx: UserContext,
    ) -> TaskList:
        """Create a new task list. Creator becomes the owner."""
        assert self._storage is not None
        if not task_list.id:
            task_list.id = f"tlst_{uuid.uuid4().hex[:12]}"
        task_list.owner_user_id = user_ctx.user_id
        task_list.created_at = _now_utc_iso()

        existing = await self._storage.get(
            _TASK_LISTS_COLLECTION, task_list.id
        )
        if existing is not None:
            raise ValueError(f"Task list id already exists: {task_list.id}")

        await self._storage.put(
            _TASK_LISTS_COLLECTION,
            task_list.id,
            task_list.to_dict(),
        )
        await self._refresh_cache()

        if self._enabled and task_list.poll_enabled:
            try:
                await self._start_runtime(task_list)
            except Exception:
                logger.exception(
                    "Failed to start runtime for newly created list %s",
                    task_list.id,
                )

        await self._publish_list_event("tasks.list.created", task_list)
        return task_list

    async def update_list(
        self,
        list_id: str,
        updates: dict[str, Any],
        user_ctx: UserContext,
    ) -> TaskList:
        task_list = await self._require_list(list_id)
        self._require_admin(task_list, user_ctx)

        immutable = {"id", "owner_user_id", "created_at"}
        share_fields = {"shared_with_users", "shared_with_roles"}
        restart_keys = {
            "backend_name",
            "backend_config",
            "poll_enabled",
            "poll_interval_sec",
        }
        needs_restart = False

        for key, value in updates.items():
            if key in immutable:
                continue
            if key in share_fields:
                continue  # use share_* methods
            if not hasattr(task_list, key):
                continue
            if key in restart_keys:
                needs_restart = True
            setattr(task_list, key, value)

        assert self._storage is not None
        await self._storage.put(
            _TASK_LISTS_COLLECTION,
            task_list.id,
            task_list.to_dict(),
        )
        await self._refresh_cache()

        if self._enabled and needs_restart:
            try:
                await self._restart_runtime(task_list)
            except Exception:
                logger.exception(
                    "Failed to restart runtime after update for list %s",
                    task_list.id,
                )

        # Refresh runtime's cached list reference so subsequent
        # publishes reflect the updated row.
        runtime = self._runtimes.get(task_list.id)
        if runtime is not None:
            runtime.task_list = task_list

        await self._publish_list_event("tasks.list.updated", task_list)
        return task_list

    async def delete_list(
        self,
        list_id: str,
        user_ctx: UserContext,
        *,
        force: bool = False,
    ) -> None:
        """Delete a list and cascade tasks + seen rows."""
        task_list = await self._require_list(list_id)
        self._require_admin(task_list, user_ctx)
        assert self._storage is not None

        # Refuse if open tasks exist unless force=True.
        if not force:
            open_rows = await self._storage.query(
                Query(
                    collection=_TASKS_COLLECTION,
                    filters=[
                        Filter(field="list_id", op=FilterOp.EQ, value=list_id),
                        Filter(
                            field="status", op=FilterOp.EQ, value=TaskStatus.OPEN.value
                        ),
                        Filter(field="deleted_at", op=FilterOp.EQ, value=""),
                    ],
                    limit=1,
                )
            )
            if open_rows:
                raise ValueError(
                    "Cannot delete task list with open tasks; pass force=True "
                    "to drop incomplete tasks anyway",
                )

        await self._stop_runtime(list_id)

        # Cascade tasks.
        tasks = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="list_id", op=FilterOp.EQ, value=list_id),
                ],
            )
        )
        for t in tasks:
            await self._storage.delete(_TASKS_COLLECTION, t["_id"])

        # Cascade task_events_seen (orphan rows whose key starts with
        # "<list_id>:" — entity storage doesn't have a prefix-match
        # operator so we filter in-Python).
        seen = await self._storage.query(
            Query(collection=_TASK_EVENTS_SEEN_COLLECTION)
        )
        for s in seen:
            sid = str(s.get("_id", ""))
            if sid.startswith(f"{list_id}:"):
                await self._storage.delete(_TASK_EVENTS_SEEN_COLLECTION, sid)

        await self._storage.delete(_TASK_LISTS_COLLECTION, list_id)
        await self._refresh_cache()
        await self._publish_list_event("tasks.list.deleted", task_list)

    async def share_user(
        self,
        list_id: str,
        user_id: str,
        user_ctx: UserContext,
    ) -> TaskList:
        return await self._mutate_share(
            list_id, user_ctx, add_user=user_id
        )

    async def unshare_user(
        self,
        list_id: str,
        user_id: str,
        user_ctx: UserContext,
    ) -> TaskList:
        return await self._mutate_share(
            list_id, user_ctx, remove_user=user_id
        )

    async def share_role(
        self,
        list_id: str,
        role: str,
        user_ctx: UserContext,
    ) -> TaskList:
        return await self._mutate_share(list_id, user_ctx, add_role=role)

    async def unshare_role(
        self,
        list_id: str,
        role: str,
        user_ctx: UserContext,
    ) -> TaskList:
        return await self._mutate_share(list_id, user_ctx, remove_role=role)

    async def _mutate_share(
        self,
        list_id: str,
        user_ctx: UserContext,
        *,
        add_user: str = "",
        remove_user: str = "",
        add_role: str = "",
        remove_role: str = "",
    ) -> TaskList:
        task_list = await self._require_list(list_id)
        self._require_admin(task_list, user_ctx)
        changed = False
        if add_user and add_user not in task_list.shared_with_users:
            task_list.shared_with_users.append(add_user)
            changed = True
        if remove_user and remove_user in task_list.shared_with_users:
            task_list.shared_with_users.remove(remove_user)
            changed = True
        if add_role and add_role not in task_list.shared_with_roles:
            task_list.shared_with_roles.append(add_role)
            changed = True
        if remove_role and remove_role in task_list.shared_with_roles:
            task_list.shared_with_roles.remove(remove_role)
            changed = True
        if changed:
            assert self._storage is not None
            await self._storage.put(
                _TASK_LISTS_COLLECTION,
                task_list.id,
                task_list.to_dict(),
            )
            await self._refresh_cache()
            await self._publish_shares_changed(task_list)
        return task_list

    async def test_list_connection(
        self,
        list_id: str,
        user_ctx: UserContext,
    ) -> dict[str, Any]:
        task_list = await self._require_list(list_id)
        self._require_admin(task_list, user_ctx)
        try:
            backends = TaskBackend.registered_backends()
            backend_cls = backends.get(task_list.backend_name)
            if backend_cls is None:
                return {
                    "ok": False,
                    "error": f"Unknown backend: {task_list.backend_name}",
                }
            probe = backend_cls()
            if isinstance(probe, StorageAwareTaskBackend):
                probe.set_storage(self._storage)
            settings = dict(task_list.backend_config)
            settings.setdefault("list_id", task_list.id)
            await probe.initialize(settings)
            try:
                await probe.list_tasks()
            finally:
                await probe.close()
            return {"ok": True, "error": ""}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Event publishers ─────────────────────────────────────────────

    async def _publish_list_event(
        self,
        event_type: str,
        task_list: TaskList,
    ) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(
                event_type=event_type,
                data={
                    "list_id": task_list.id,
                    "name": task_list.name,
                    "owner_user_id": task_list.owner_user_id,
                },
                source="tasks",
            )
        )

    async def _publish_shares_changed(self, task_list: TaskList) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(
                event_type="tasks.list.shares.changed",
                data={
                    "list_id": task_list.id,
                    "owner_user_id": task_list.owner_user_id,
                    "shared_with_users": list(task_list.shared_with_users),
                    "shared_with_roles": list(task_list.shared_with_roles),
                },
                source="tasks",
            )
        )

    async def _publish_task_event(
        self,
        event_type: str,
        task: Task,
        task_list: TaskList,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        data: dict[str, Any] = {
            "list_id": task.list_id,
            "task_id": task.id,
            "title": task.title,
            "due_at": task.due_at,
            "due_at_tz": task.due_at_tz,
            "created_by_user_id": task.created_by_user_id,
            "backend": task_list.backend_name,
            "sync_status": task.sync_status.value,
        }
        if extra:
            data.update(extra)
        await self._event_bus.publish(
            Event(event_type=event_type, data=data, source="tasks")
        )

    # ── Polling ──────────────────────────────────────────────────────

    def _make_poll_callback(
        self,
        list_id: str,
    ) -> Callable[[], Awaitable[None]]:
        async def _run() -> None:
            runtime = self._runtimes.get(list_id)
            if runtime is None:
                return
            await self._poll_runtime(runtime)

        return _run

    async def _poll_runtime(self, runtime: _ListRuntime) -> None:
        """Fetch new / updated tasks from the upstream backend."""
        backend = runtime.backend
        task_list = runtime.task_list
        assert self._storage is not None

        # Compute updated_since cursor — latest last_seen_at for this
        # list across its seen rows.
        seen_cursor = await self._latest_seen(task_list.id)

        try:
            tasks = await backend.list_tasks(
                include_completed=True,
                updated_since=seen_cursor,
            )
        except Exception as exc:
            await self._on_poll_failure(runtime, exc)
            return

        new_count = 0
        updated_count = 0
        completed_count = 0
        for upstream in tasks:
            if not upstream.source_id:
                continue
            existing = await self._find_by_source(task_list.id, upstream.source_id)
            if existing is None:
                # Insert new row.
                upstream.id = f"tsk_{uuid.uuid4().hex[:12]}"
                upstream.list_id = task_list.id
                upstream.created_at = upstream.created_at or _now_utc_iso()
                upstream.updated_at = upstream.updated_at or upstream.created_at
                upstream.sync_status = SyncStatus.SYNCED
                await self._storage.put(
                    _TASKS_COLLECTION,
                    upstream.id,
                    upstream.to_dict(),
                )
                await self._publish_task_event(
                    "task.created", upstream, task_list
                )
                new_count += 1
            else:
                merged, fields_changed, became_done = self._merge_poll_row(
                    existing, upstream
                )
                if fields_changed:
                    await self._storage.put(
                        _TASKS_COLLECTION,
                        merged.id,
                        merged.to_dict(),
                    )
                    if became_done:
                        await self._publish_task_event(
                            "task.completed",
                            merged,
                            task_list,
                            extra={
                                "completed_at": merged.completed_at,
                                "completed_by_user_id": merged.created_by_user_id,
                            },
                        )
                        completed_count += 1
                    else:
                        await self._publish_task_event(
                            "task.updated",
                            merged,
                            task_list,
                            extra={"changed_fields": list(fields_changed)},
                        )
                        updated_count += 1
            await self._update_seen(task_list.id, upstream.source_id)

        # Reset failure counter, mark recovered if previously degraded.
        if runtime.failure_count >= self._degraded_after_failures:
            await self._mark_recovered(task_list)
        runtime.failure_count = 0

        # Update last_sync_at.
        task_list.last_sync_at = _now_utc_iso()
        task_list.last_error = ""
        await self._storage.put(
            _TASK_LISTS_COLLECTION,
            task_list.id,
            task_list.to_dict(),
        )

        if new_count or updated_count or completed_count:
            logger.info(
                "Tasks poll (%s): %d new, %d updated, %d completed",
                task_list.id,
                new_count,
                updated_count,
                completed_count,
            )

    def _merge_poll_row(
        self,
        existing: Task,
        upstream: Task,
    ) -> tuple[Task, set[str], bool]:
        """Merge upstream snapshot into the local row.

        Conflict resolution policy: upstream is authoritative for fields
        not currently in the row's pending patch. Because patches happen
        field-by-field via ``update_task``, and the local-first push
        path stamps ``sync_status=PENDING_PUSH`` BEFORE the push, a row
        with ``PENDING_PUSH`` keeps its dirty fields verbatim until the
        push lands. Any field not currently in flight is overwritten
        from upstream.

        Returns ``(merged_task, set_of_changed_fields, became_done_now)``.
        """
        is_pending = existing.sync_status in (
            SyncStatus.PENDING_PUSH,
            SyncStatus.PUSH_FAILED,
        )

        merged = Task(
            id=existing.id,
            list_id=existing.list_id,
            source_id=existing.source_id,
            created_at=existing.created_at,
            created_by_user_id=existing.created_by_user_id,
            idempotency_key=existing.idempotency_key,
            sync_status=existing.sync_status,
            last_push_attempt_at=existing.last_push_attempt_at,
            last_push_error=existing.last_push_error,
            retry_count=existing.retry_count,
            etag=upstream.etag or existing.etag,
            deleted_at=existing.deleted_at,
            due_soon_fired=existing.due_soon_fired,
        )

        fields_changed: set[str] = set()

        def _maybe_set(name: str, upstream_val: Any, existing_val: Any) -> Any:
            # When the row has pending edits, we keep the local value
            # for fields that are "live"; v1 doesn't track per-field
            # provenance, so we just keep the local value entirely on
            # pending rows for the listed mutable fields. Upstream wins
            # otherwise.
            if is_pending:
                return existing_val
            if upstream_val != existing_val:
                fields_changed.add(name)
            return upstream_val

        merged.title = _maybe_set("title", upstream.title, existing.title)
        merged.notes = _maybe_set("notes", upstream.notes, existing.notes)
        merged.due_at = _maybe_set("due_at", upstream.due_at, existing.due_at)
        merged.due_at_tz = _maybe_set(
            "due_at_tz", upstream.due_at_tz, existing.due_at_tz
        )
        merged.priority = _maybe_set(
            "priority", upstream.priority, existing.priority
        )
        merged.tags = _maybe_set("tags", list(upstream.tags), list(existing.tags))
        merged.project = _maybe_set(
            "project", upstream.project, existing.project
        )
        new_status = _maybe_set("status", upstream.status, existing.status)
        merged.status = new_status
        merged.completed_at = _maybe_set(
            "completed_at", upstream.completed_at, existing.completed_at
        )
        merged.updated_at = upstream.updated_at or _now_utc_iso()
        # Stamp updated_at without flagging as a "field change" for the
        # event payload — bumping a timestamp every poll is noise.

        became_done = (
            existing.status != TaskStatus.DONE
            and merged.status == TaskStatus.DONE
        )
        return merged, fields_changed, became_done

    async def _on_poll_failure(
        self,
        runtime: _ListRuntime,
        exc: Exception,
    ) -> None:
        runtime.failure_count += 1
        logger.warning(
            "Tasks poll (%s) failed (count=%d): %s",
            runtime.task_list.id,
            runtime.failure_count,
            exc,
        )
        if runtime.failure_count >= self._degraded_after_failures:
            await self._mark_degraded(runtime.task_list, str(exc))

    async def _mark_degraded(
        self,
        task_list: TaskList,
        last_error: str,
    ) -> None:
        if task_list.degraded_since:
            return  # already degraded
        task_list.degraded_since = _now_utc_iso()
        task_list.last_error = (last_error or "")[:500]
        assert self._storage is not None
        await self._storage.put(
            _TASK_LISTS_COLLECTION,
            task_list.id,
            task_list.to_dict(),
        )
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    event_type="tasks.list.degraded",
                    data={
                        "list_id": task_list.id,
                        "name": task_list.name,
                        "last_error": task_list.last_error,
                        "last_sync_at": task_list.last_sync_at,
                    },
                    source="tasks",
                )
            )

    async def _mark_recovered(self, task_list: TaskList) -> None:
        if not task_list.degraded_since:
            return
        task_list.degraded_since = ""
        task_list.last_error = ""
        assert self._storage is not None
        await self._storage.put(
            _TASK_LISTS_COLLECTION,
            task_list.id,
            task_list.to_dict(),
        )
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    event_type="tasks.list.recovered",
                    data={
                        "list_id": task_list.id,
                        "name": task_list.name,
                        "last_sync_at": task_list.last_sync_at,
                    },
                    source="tasks",
                )
            )

    # ── task_events_seen helpers ─────────────────────────────────────

    async def _latest_seen(self, list_id: str) -> str:
        assert self._storage is not None
        rows = await self._storage.query(
            Query(collection=_TASK_EVENTS_SEEN_COLLECTION)
        )
        latest = ""
        for row in rows:
            sid = str(row.get("_id", ""))
            if not sid.startswith(f"{list_id}:"):
                continue
            seen = str(row.get("last_seen_at", ""))
            if seen > latest:
                latest = seen
        return latest

    async def _update_seen(self, list_id: str, source_id: str) -> None:
        assert self._storage is not None
        key = f"{list_id}:{source_id}"
        await self._storage.put(
            _TASK_EVENTS_SEEN_COLLECTION,
            key,
            {
                "_id": key,
                "list_id": list_id,
                "source_id": source_id,
                "last_seen_at": _now_utc_iso(),
            },
        )

    async def _find_by_source(
        self,
        list_id: str,
        source_id: str,
    ) -> Task | None:
        assert self._storage is not None
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="list_id", op=FilterOp.EQ, value=list_id),
                    Filter(field="source_id", op=FilterOp.EQ, value=source_id),
                ],
                limit=1,
            )
        )
        if not rows:
            return None
        return Task.from_dict(rows[0])

    # ── Default-list resolution ──────────────────────────────────────

    async def _resolve_default_list(
        self,
        user_ctx: UserContext,
    ) -> TaskList | None:
        """Pick the default list for ``add_task`` when no ``list_id`` is given.

        Per spec §6.5 / §7.7:
        1. The user's owned list with ``is_default=True``, if any.
        2. Otherwise the user's only owned list (if exactly one exists).
        3. Otherwise None — caller turns this into the "ambiguous"
           branch (UIBlock select / error message).
        """
        all_lists = await self._load_lists()
        owned = [
            tl for tl in all_lists if tl.owner_user_id == user_ctx.user_id
        ]
        for tl in owned:
            if tl.is_default:
                return tl
        if len(owned) == 1:
            return owned[0]
        return None

    # ── Add task (local-first with reconciliation) ───────────────────

    async def add_task(
        self,
        list_id: str,
        task: Task,
        user_ctx: UserContext,
        *,
        idempotency_key: str = "",
    ) -> Task:
        """Add a task. Local-first with reconciliation (§6.7.1).

        Resolution order: persist → push inline → on push failure leave
        ``sync_status=pending_push`` and let the recurring sync tick
        retry.
        """
        assert self._storage is not None
        task_list = await self._require_list(list_id)
        self._require_access(task_list, user_ctx)

        # Idempotency: pre-create dedup.
        effective_key = idempotency_key or task.idempotency_key
        if effective_key:
            existing = await self._find_by_idempotency_key(
                list_id, effective_key
            )
            if existing is not None:
                return existing

        if not task.id:
            task.id = f"tsk_{uuid.uuid4().hex[:12]}"
        task.list_id = list_id
        now = _now_utc_iso()
        if not task.created_at:
            task.created_at = now
        if not task.updated_at:
            task.updated_at = now
        if not task.created_by_user_id:
            task.created_by_user_id = user_ctx.user_id
        task.idempotency_key = effective_key
        task.sync_status = SyncStatus.PENDING_PUSH

        # Persist the row immediately (local-first).
        await self._storage.put(
            _TASKS_COLLECTION,
            task.id,
            task.to_dict(),
        )
        await self._publish_task_event("task.created", task, task_list)

        # Local backend: trivial confirm — stamp source_id = id and
        # mark synced.
        if task_list.backend_name == "local":
            task.source_id = task.id
            task.sync_status = SyncStatus.SYNCED
            await self._storage.put(
                _TASKS_COLLECTION,
                task.id,
                task.to_dict(),
            )
            return task

        # External backend: push inline with timeout.
        try:
            runtime = await self._ensure_runtime(task_list)
        except Exception as exc:
            await self._record_push_failure(task, str(exc))
            return await self._reload_task(task.id) or task

        try:
            created = await asyncio.wait_for(
                runtime.backend.add_task(task),
                timeout=self._push_timeout_sec,
            )
            # Merge upstream-normalized fields into the persisted row.
            task.source_id = created.source_id or task.id
            if created.title:
                task.title = created.title
            if created.due_at:
                task.due_at = created.due_at
            if created.etag:
                task.etag = created.etag
            task.sync_status = SyncStatus.SYNCED
            task.retry_count = 0
            task.last_push_error = ""
            task.last_push_attempt_at = _now_utc_iso()
            await self._storage.put(
                _TASKS_COLLECTION,
                task.id,
                task.to_dict(),
            )
        except (
            TaskBackendAuthError,
            TaskBackendNotFoundError,
        ) as exc:
            # Non-retriable — mark push_failed, publish event.
            await self._record_push_failure(
                task, str(exc), terminal=True
            )
        except (
            TimeoutError,
            TaskBackendTransientError,
            TaskBackendRateLimitError,
        ) as exc:
            # Retriable — leave the row pending, sync tick will retry.
            await self._record_push_failure(task, str(exc))
        except Exception as exc:
            # Unknown errors — treat as retriable (could be transient).
            await self._record_push_failure(task, str(exc))

        return await self._reload_task(task.id) or task

    async def _record_push_failure(
        self,
        task: Task,
        error: str,
        *,
        terminal: bool = False,
    ) -> None:
        assert self._storage is not None
        task.last_push_attempt_at = _now_utc_iso()
        task.last_push_error = (error or "")[:500]
        task.retry_count += 1
        if terminal or task.retry_count >= self._max_push_retries:
            task.sync_status = SyncStatus.PUSH_FAILED
        else:
            task.sync_status = SyncStatus.PENDING_PUSH
        await self._storage.put(
            _TASKS_COLLECTION,
            task.id,
            task.to_dict(),
        )
        if task.sync_status == SyncStatus.PUSH_FAILED:
            tl = await self.get_list(task.list_id)
            if tl is not None and self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        event_type="task.push_failed",
                        data={
                            "list_id": task.list_id,
                            "task_id": task.id,
                            "title": task.title,
                            "last_push_error": task.last_push_error,
                            "retry_count": task.retry_count,
                            "backend": tl.backend_name,
                        },
                        source="tasks",
                    )
                )

    async def _find_by_idempotency_key(
        self,
        list_id: str,
        key: str,
    ) -> Task | None:
        assert self._storage is not None
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="list_id", op=FilterOp.EQ, value=list_id),
                    Filter(field="idempotency_key", op=FilterOp.EQ, value=key),
                ],
                limit=1,
            )
        )
        if not rows:
            return None
        return Task.from_dict(rows[0])

    async def _reload_task(self, task_id: str) -> Task | None:
        assert self._storage is not None
        row = await self._storage.get(_TASKS_COLLECTION, task_id)
        if row is None:
            return None
        return Task.from_dict(row)

    # ── Update / complete / cancel / delete ──────────────────────────

    _UPDATE_FORBIDDEN = frozenset(
        {
            "id",
            "_id",
            "list_id",
            "source_id",
            "status",
            "created_at",
            "created_by_user_id",
            "idempotency_key",
            "sync_status",
            "retry_count",
            "last_push_attempt_at",
            "last_push_error",
            "etag",
            "due_soon_fired",
            "deleted_at",
        }
    )
    _UPDATE_ALLOWED = frozenset(
        {"title", "notes", "due_at", "due_at_tz", "priority", "tags", "project"}
    )

    @classmethod
    def _user_facing_patch(cls, task: Task) -> dict[str, Any]:
        """Project a Task to a dict containing only user-facing mutable
        fields plus ``status`` / ``completed_at`` so a sync-tick retry
        can reconstruct the upstream row without leaking internal
        bookkeeping (``_id``, ``sync_status``, ``last_push_error``,
        ``idempotency_key``, ``retry_count`` etc.).

        Backends contract per spec §6.7.2: ``update_task`` is patch-shaped.
        """
        return {
            "title": task.title,
            "notes": task.notes,
            "due_at": task.due_at,
            "due_at_tz": task.due_at_tz,
            "priority": int(task.priority.value),
            "tags": list(task.tags),
            "project": task.project,
            "status": task.status.value,
            "completed_at": task.completed_at,
        }

    async def update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        user_ctx: UserContext,
    ) -> Task:
        assert self._storage is not None
        task = await self._reload_task(task_id)
        if task is None or task.deleted_at:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        task_list = await self._require_list(task.list_id)
        self._require_access(task_list, user_ctx)

        # Filter to allowed fields, normalizing priority.
        patch: dict[str, Any] = {}
        for key, value in updates.items():
            if key in self._UPDATE_FORBIDDEN:
                continue
            if key not in self._UPDATE_ALLOWED:
                continue
            if key == "priority":
                patch[key] = int(_coerce_priority(value).value)
            elif key == "tags" and isinstance(value, list):
                patch[key] = [str(t) for t in value]
            else:
                patch[key] = value

        if not patch:
            return task

        # Apply locally + flag pending push.
        for key, value in patch.items():
            if key == "priority":
                task.priority = TaskPriority(int(value))
            elif key == "tags":
                task.tags = list(value)
            else:
                setattr(task, key, value)
        if "due_at" in patch:
            # Reschedule clears the due_soon dedup so the next approach
            # fires once.
            task.due_soon_fired = False
        task.updated_at = _now_utc_iso()
        task.sync_status = (
            SyncStatus.SYNCED
            if task_list.backend_name == "local"
            else SyncStatus.PENDING_PUSH
        )
        await self._storage.put(
            _TASKS_COLLECTION,
            task.id,
            task.to_dict(),
        )
        await self._publish_task_event(
            "task.updated",
            task,
            task_list,
            extra={"changed_fields": list(patch.keys())},
        )

        if task_list.backend_name == "local":
            return task

        # External backend: push the patch (not the full Task).
        try:
            runtime = await self._ensure_runtime(task_list)
        except Exception as exc:
            await self._record_push_failure(task, str(exc))
            return await self._reload_task(task.id) or task

        try:
            updated = await asyncio.wait_for(
                runtime.backend.update_task(
                    task.source_id, dict(patch), etag=task.etag
                ),
                timeout=self._push_timeout_sec,
            )
            task.etag = updated.etag or task.etag
            task.sync_status = SyncStatus.SYNCED
            task.retry_count = 0
            task.last_push_error = ""
            task.last_push_attempt_at = _now_utc_iso()
            await self._storage.put(
                _TASKS_COLLECTION, task.id, task.to_dict()
            )
        except TaskBackendConflictError as exc:
            # Stale etag — re-poll then retry once with the fresh etag.
            try:
                fresh = await self._find_or_repoll(task_list, task.source_id)
                if fresh is not None:
                    task.etag = fresh.etag
                    updated = await asyncio.wait_for(
                        runtime.backend.update_task(
                            task.source_id, dict(patch), etag=task.etag
                        ),
                        timeout=self._push_timeout_sec,
                    )
                    task.etag = updated.etag or task.etag
                    task.sync_status = SyncStatus.SYNCED
                    task.retry_count = 0
                    await self._storage.put(
                        _TASKS_COLLECTION, task.id, task.to_dict()
                    )
                else:
                    await self._record_push_failure(
                        task, f"stale etag (no fresh row): {exc}", terminal=True
                    )
            except TaskBackendConflictError as exc2:
                await self._record_push_failure(
                    task,
                    f"stale etag persisted: {exc2}",
                    terminal=True,
                )
            except Exception as exc2:
                await self._record_push_failure(task, str(exc2))
        except Exception as exc:
            await self._record_push_failure(task, str(exc))

        return await self._reload_task(task.id) or task

    async def _find_or_repoll(
        self,
        task_list: TaskList,
        source_id: str,
    ) -> Task | None:
        """Re-poll the upstream and return the fresh row (used for OCC retries)."""
        runtime = self._runtimes.get(task_list.id)
        if runtime is None:
            return None
        try:
            tasks = await runtime.backend.list_tasks(include_completed=True)
        except Exception:
            return None
        for t in tasks:
            if t.source_id == source_id:
                return t
        return None

    async def complete_task(
        self,
        task_id: str,
        user_ctx: UserContext,
    ) -> Task:
        assert self._storage is not None
        task = await self._reload_task(task_id)
        if task is None or task.deleted_at:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        task_list = await self._require_list(task.list_id)
        self._require_access(task_list, user_ctx)

        if task.status == TaskStatus.DONE:
            return task

        task.status = TaskStatus.DONE
        task.completed_at = _now_utc_iso()
        task.updated_at = task.completed_at
        task.sync_status = (
            SyncStatus.SYNCED
            if task_list.backend_name == "local"
            else SyncStatus.PENDING_PUSH
        )
        await self._storage.put(_TASKS_COLLECTION, task.id, task.to_dict())
        await self._publish_task_event(
            "task.completed",
            task,
            task_list,
            extra={
                "completed_at": task.completed_at,
                "completed_by_user_id": user_ctx.user_id,
            },
        )

        if task_list.backend_name == "local":
            return task

        try:
            runtime = await self._ensure_runtime(task_list)
            await asyncio.wait_for(
                runtime.backend.complete_task(task.source_id),
                timeout=self._push_timeout_sec,
            )
            task.sync_status = SyncStatus.SYNCED
            task.retry_count = 0
            task.last_push_error = ""
            task.last_push_attempt_at = _now_utc_iso()
            await self._storage.put(
                _TASKS_COLLECTION, task.id, task.to_dict()
            )
        except Exception as exc:
            await self._record_push_failure(task, str(exc))
        return await self._reload_task(task.id) or task

    async def cancel_task(
        self,
        task_id: str,
        user_ctx: UserContext,
        *,
        reason: str = "",
    ) -> Task:
        assert self._storage is not None
        task = await self._reload_task(task_id)
        if task is None or task.deleted_at:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        task_list = await self._require_list(task.list_id)
        self._require_access(task_list, user_ctx)

        if task.status == TaskStatus.CANCELLED:
            return task

        task.status = TaskStatus.CANCELLED
        task.completed_at = _now_utc_iso()
        task.updated_at = task.completed_at
        if reason:
            suffix = f"\n\n[CANCELLED] {reason}"
            task.notes = (task.notes or "") + suffix
        task.sync_status = (
            SyncStatus.SYNCED
            if task_list.backend_name == "local"
            else SyncStatus.PENDING_PUSH
        )
        await self._storage.put(_TASKS_COLLECTION, task.id, task.to_dict())
        await self._publish_task_event(
            "task.cancelled",
            task,
            task_list,
            extra={
                "completed_at": task.completed_at,
                "cancelled_by_user_id": user_ctx.user_id,
                "reason": reason,
            },
        )

        if task_list.backend_name == "local":
            return task

        # Most external backends don't expose a "cancel" semantic — treat
        # as delete on upstream (Google Tasks: delete; Todoist: close;
        # CalDAV: STATUS:CANCELLED via update_task).
        try:
            runtime = await self._ensure_runtime(task_list)
            await asyncio.wait_for(
                runtime.backend.delete_task(task.source_id),
                timeout=self._push_timeout_sec,
            )
            task.sync_status = SyncStatus.SYNCED
            task.retry_count = 0
            task.last_push_error = ""
            await self._storage.put(
                _TASKS_COLLECTION, task.id, task.to_dict()
            )
        except Exception as exc:
            await self._record_push_failure(task, str(exc))
        return await self._reload_task(task.id) or task

    async def delete_task(
        self,
        task_id: str,
        user_ctx: UserContext,
        *,
        force: bool = False,
    ) -> None:
        assert self._storage is not None
        task = await self._reload_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        task_list = await self._require_list(task.list_id)
        self._require_access(task_list, user_ctx)

        if force and not self._is_admin(user_ctx):
            raise TaskListPermissionError(
                "force=True requires admin privileges",
            )

        if force:
            # Hard-delete: remove storage row + best-effort upstream
            # delete.
            await self._storage.delete(_TASKS_COLLECTION, task_id)
            if task_list.backend_name != "local" and task.source_id:
                try:
                    runtime = await self._ensure_runtime(task_list)
                    await asyncio.wait_for(
                        runtime.backend.delete_task(task.source_id),
                        timeout=self._push_timeout_sec,
                    )
                except Exception as exc:
                    logger.warning(
                        "Hard delete: upstream delete failed for "
                        "list=%s source=%s: %s",
                        task.list_id,
                        task.source_id,
                        exc,
                    )
            await self._publish_task_event(
                "task.deleted", task, task_list, extra={"soft": False}
            )
            return

        # Soft-delete: stamp deleted_at + queue upstream delete.
        task.deleted_at = _now_utc_iso()
        task.updated_at = task.deleted_at
        task.sync_status = (
            SyncStatus.SYNCED
            if task_list.backend_name == "local"
            else SyncStatus.PENDING_DELETE
        )
        await self._storage.put(_TASKS_COLLECTION, task.id, task.to_dict())
        await self._publish_task_event(
            "task.deleted", task, task_list, extra={"soft": True}
        )

        if task_list.backend_name == "local":
            return

        # Best-effort upstream delete.
        try:
            runtime = await self._ensure_runtime(task_list)
            await asyncio.wait_for(
                runtime.backend.delete_task(task.source_id),
                timeout=self._push_timeout_sec,
            )
            task.sync_status = SyncStatus.SYNCED
            task.last_push_error = ""
            task.retry_count = 0
            await self._storage.put(
                _TASKS_COLLECTION, task.id, task.to_dict()
            )
        except Exception as exc:
            # Soft-delete with pending upstream delete — the sync tick
            # will retry.
            await self._record_push_failure(task, str(exc))

    # ── Reads ────────────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> Task | None:
        assert self._storage is not None
        row = await self._storage.get(_TASKS_COLLECTION, task_id)
        if row is None:
            return None
        task = Task.from_dict(row)
        if task.deleted_at:
            return None
        # Visibility check via current user.
        user_ctx = get_current_user()
        task_list = await self.get_list(task.list_id)
        if task_list is None:
            return None
        if not can_access_list(
            user_ctx, task_list, is_admin=self._is_admin(user_ctx)
        ):
            return None
        return task

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
        offset: int = 0,
    ) -> list[Task]:
        assert self._storage is not None
        user_ctx = get_current_user()
        is_admin = self._is_admin(user_ctx)

        # Resolve accessible list ids; restrict by filter if given.
        all_lists = await self._load_lists()
        accessible = {
            tl.id: tl
            for tl in all_lists
            if can_access_list(user_ctx, tl, is_admin=is_admin)
        }
        if list_id is not None:
            if list_id not in accessible:
                return []
            target_ids = [list_id]
        else:
            target_ids = list(accessible.keys())

        if backend is not None:
            target_ids = [
                tid
                for tid in target_ids
                if accessible[tid].backend_name == backend
            ]

        if not target_ids:
            return []

        filters = [
            Filter(field="list_id", op=FilterOp.IN, value=target_ids),
            Filter(field="deleted_at", op=FilterOp.EQ, value=""),
        ]
        if status is not None:
            filters.append(
                Filter(field="status", op=FilterOp.EQ, value=status.value)
            )
        if tag:
            filters.append(Filter(field="tags", op=FilterOp.CONTAINS, value=tag))
        if project:
            filters.append(Filter(field="project", op=FilterOp.EQ, value=project))
        if due_before:
            filters.append(
                Filter(field="due_at", op=FilterOp.LTE, value=due_before)
            )
        if due_after:
            filters.append(
                Filter(field="due_at", op=FilterOp.GTE, value=due_after)
            )
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=filters,
                sort=[SortField(field="due_at", descending=False)],
                limit=limit,
                offset=max(0, offset),
            )
        )
        return [Task.from_dict(row) for row in rows]

    async def due_today(
        self,
        *,
        list_id: str | None = None,
        backend: str | None = None,
    ) -> list[Task]:
        user_ctx = get_current_user()
        tz = _resolve_user_tz(user_ctx)
        now_local = datetime.now(tz)
        start_local = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = now_local.replace(
            hour=23, minute=59, second=59, microsecond=999_999
        )
        start_utc = start_local.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        end_utc = end_local.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return await self.search_tasks(
            list_id=list_id,
            backend=backend,
            status=TaskStatus.OPEN,
            due_after=start_utc,
            due_before=end_utc,
        )

    async def overdue(
        self,
        *,
        list_id: str | None = None,
        backend: str | None = None,
    ) -> list[Task]:
        now_utc = _now_utc_iso()
        return await self.search_tasks(
            list_id=list_id,
            backend=backend,
            status=TaskStatus.OPEN,
            due_before=now_utc,
        )

    async def refresh_list(
        self,
        list_id: str,
        user_ctx: UserContext,
    ) -> dict[str, Any]:
        """Force a fresh upstream pull. Returns counts of changes."""
        task_list = await self._require_list(list_id)
        self._require_access(task_list, user_ctx)
        runtime = self._runtimes.get(list_id)
        if runtime is None:
            try:
                await self._start_runtime(task_list)
                runtime = self._runtimes.get(list_id)
            except Exception as exc:
                return {"error_count": 1, "error": str(exc)[:200]}
        if runtime is None:
            return {"error_count": 1}
        before_ids: set[str] = set()
        assert self._storage is not None
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="list_id", op=FilterOp.EQ, value=list_id),
                ],
            )
        )
        before_ids = {str(r.get("source_id", "")) for r in rows}
        await self._poll_runtime(runtime)
        after_rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="list_id", op=FilterOp.EQ, value=list_id),
                ],
            )
        )
        after_ids = {str(r.get("source_id", "")) for r in after_rows}
        new = len(after_ids - before_ids)
        return {"new": new, "total": len(after_ids)}

    async def restore_task(
        self,
        task_id: str,
        user_ctx: UserContext,
    ) -> Task:
        """Admin-only — clear deleted_at and re-push upstream if needed."""
        if not self._is_admin(user_ctx):
            raise TaskListPermissionError("restore_task requires admin")
        assert self._storage is not None
        row = await self._storage.get(_TASKS_COLLECTION, task_id)
        if row is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        task = Task.from_dict(row)
        if not task.deleted_at:
            return task
        task_list = await self._require_list(task.list_id)
        task.deleted_at = ""
        task.updated_at = _now_utc_iso()
        task.sync_status = (
            SyncStatus.SYNCED
            if task_list.backend_name == "local"
            else SyncStatus.PENDING_PUSH
        )
        await self._storage.put(_TASKS_COLLECTION, task.id, task.to_dict())
        await self._publish_task_event("task.restored", task, task_list)
        return task

    # ── Sync tick ────────────────────────────────────────────────────

    async def _sync_tick(self) -> None:
        """Retry pending pushes via exponential backoff."""
        assert self._storage is not None
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(
                        field="sync_status",
                        op=FilterOp.IN,
                        value=[
                            SyncStatus.PENDING_PUSH.value,
                            SyncStatus.PUSH_FAILED.value,
                            SyncStatus.PENDING_DELETE.value,
                        ],
                    ),
                ],
                limit=200,
            )
        )
        for row in rows:
            task = Task.from_dict(row)
            if task.retry_count >= self._max_push_retries and task.sync_status == SyncStatus.PUSH_FAILED:
                continue
            task_list = await self.get_list(task.list_id)
            if task_list is None or task_list.backend_name == "local":
                # Orphaned or local — force-mark synced.
                task.sync_status = SyncStatus.SYNCED
                await self._storage.put(
                    _TASKS_COLLECTION, task.id, task.to_dict()
                )
                continue
            try:
                runtime = await self._ensure_runtime(task_list)
            except Exception:
                continue
            try:
                if task.sync_status == SyncStatus.PENDING_DELETE:
                    if task.source_id:
                        await asyncio.wait_for(
                            runtime.backend.delete_task(task.source_id),
                            timeout=self._push_timeout_sec,
                        )
                    task.sync_status = SyncStatus.SYNCED
                    task.last_push_error = ""
                    task.retry_count = 0
                    await self._storage.put(
                        _TASKS_COLLECTION, task.id, task.to_dict()
                    )
                else:
                    if not task.source_id:
                        # Hadn't been pushed yet — re-attempt add_task.
                        created = await asyncio.wait_for(
                            runtime.backend.add_task(task),
                            timeout=self._push_timeout_sec,
                        )
                        task.source_id = created.source_id or task.id
                        task.etag = created.etag or task.etag
                    else:
                        # Re-push as a fresh add_task; without per-row
                        # patch tracking the tick can only ensure the
                        # row exists upstream. Build a patch that
                        # contains ONLY user-facing mutable fields —
                        # backends should never see `_id`, `sync_status`,
                        # `last_push_error`, `idempotency_key`, etc.
                        retry_patch = self._user_facing_patch(task)
                        try:
                            await asyncio.wait_for(
                                runtime.backend.update_task(
                                    task.source_id, retry_patch
                                ),
                                timeout=self._push_timeout_sec,
                            )
                        except TaskBackendNotFoundError:
                            created = await asyncio.wait_for(
                                runtime.backend.add_task(task),
                                timeout=self._push_timeout_sec,
                            )
                            task.source_id = created.source_id or task.id
                    task.sync_status = SyncStatus.SYNCED
                    task.last_push_error = ""
                    task.retry_count = 0
                    await self._storage.put(
                        _TASKS_COLLECTION, task.id, task.to_dict()
                    )
                    if self._event_bus is not None:
                        await self._event_bus.publish(
                            Event(
                                event_type="task.sync_recovered",
                                data={
                                    "list_id": task.list_id,
                                    "task_id": task.id,
                                },
                                source="tasks",
                            )
                        )
            except Exception as exc:
                await self._record_push_failure(task, str(exc))

    # ── Due-soon tick ────────────────────────────────────────────────

    async def _due_soon_tick(self) -> None:
        assert self._storage is not None
        now = datetime.now(UTC)
        cutoff = now + timedelta(minutes=self._due_soon_lookahead_minutes)
        now_iso = now.isoformat().replace("+00:00", "Z")
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="status", op=FilterOp.EQ, value=TaskStatus.OPEN.value),
                    Filter(field="deleted_at", op=FilterOp.EQ, value=""),
                    Filter(field="due_at", op=FilterOp.GTE, value=now_iso),
                    Filter(field="due_at", op=FilterOp.LTE, value=cutoff_iso),
                ],
            )
        )
        for row in rows:
            if bool(row.get("due_soon_fired", False)):
                continue
            task = Task.from_dict(row)
            task.due_soon_fired = True
            # Persist FIRST, publish second — order matters so a
            # publish-then-crash doesn't re-fire on the next tick.
            await self._storage.put(
                _TASKS_COLLECTION, task.id, task.to_dict()
            )
            task_list = await self.get_list(task.list_id)
            if task_list is None or self._event_bus is None:
                continue
            await self._event_bus.publish(
                Event(
                    event_type="task.due_soon",
                    data={
                        "list_id": task.list_id,
                        "task_id": task.id,
                        "title": task.title,
                        "due_at": task.due_at,
                        "due_at_tz": task.due_at_tz,
                        "created_by_user_id": task.created_by_user_id,
                        "backend": task_list.backend_name,
                        "lookahead_minutes": self._due_soon_lookahead_minutes,
                    },
                    source="tasks",
                )
            )

    # ── GC tick ──────────────────────────────────────────────────────

    async def _gc_tick(self) -> None:
        if self._retention_days <= 0:
            return
        assert self._storage is not None
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

        # Hard-delete DONE / CANCELLED rows past retention.
        for st in (TaskStatus.DONE.value, TaskStatus.CANCELLED.value):
            rows = await self._storage.query(
                Query(
                    collection=_TASKS_COLLECTION,
                    filters=[
                        Filter(field="status", op=FilterOp.EQ, value=st),
                        Filter(
                            field="completed_at", op=FilterOp.LT, value=cutoff_iso
                        ),
                        Filter(
                            field="completed_at", op=FilterOp.NEQ, value=""
                        ),
                    ],
                )
            )
            for row in rows:
                await self._storage.delete(_TASKS_COLLECTION, row["_id"])

        # Hard-delete soft-deleted tombstones past retention.
        rows = await self._storage.query(
            Query(
                collection=_TASKS_COLLECTION,
                filters=[
                    Filter(field="deleted_at", op=FilterOp.NEQ, value=""),
                    Filter(field="deleted_at", op=FilterOp.LT, value=cutoff_iso),
                ],
            )
        )
        for row in rows:
            await self._storage.delete(_TASKS_COLLECTION, row["_id"])

        # Drop orphan task_events_seen rows whose list_id is gone.
        list_ids = {tl.id for tl in await self._load_lists()}
        seen = await self._storage.query(
            Query(collection=_TASK_EVENTS_SEEN_COLLECTION)
        )
        for row in seen:
            sid = str(row.get("_id", ""))
            list_part = sid.split(":", 1)[0] if ":" in sid else ""
            if list_part and list_part not in list_ids:
                await self._storage.delete(_TASK_EVENTS_SEEN_COLLECTION, sid)

    # ── Summarize today (single-source) ──────────────────────────────

    async def summarize_today(self, user_ctx: UserContext) -> str:
        """Build the daily task summary. Single source for AI tool + greeting."""
        from gilbert.core.context import set_current_user

        # Reads via search_tasks use get_current_user(); set it here so
        # the greeting service's direct call sees the right identity.
        set_current_user(user_ctx)
        due_today = await self.due_today()
        overdue = await self.overdue()
        # Keep total bounded.
        chunks = (due_today + overdue)[: self._max_summary_tasks]
        if not chunks:
            return "No tasks on your plate today. Enjoy a clear morning."

        payload = [
            {
                "id": t.id,
                "title": t.title,
                "due_at": t.due_at,
                "due_at_tz": t.due_at_tz,
                "project": t.project,
                "tags": list(t.tags),
                "status": t.status.value,
                "priority": int(t.priority.value),
                "overdue": t.due_at and t.due_at < _now_utc_iso(),
            }
            for t in chunks
        ]

        # Fall back to a deterministic summary if AI is absent.
        if self._ai is None:
            return self._fallback_summary(payload)

        try:
            response = await self._ai.complete_one_shot(
                messages=[
                    Message(
                        role=MessageRole.USER,
                        content=json.dumps(payload),
                    )
                ],
                system_prompt=self._summary_prompt,
                profile_name=self._ai_profile,
                tools_override=[],
            )
            return response.message.content or self._fallback_summary(payload)
        except Exception:
            logger.exception("summarize_today: AI call failed")
            return self._fallback_summary(payload)

    @staticmethod
    def _fallback_summary(payload: list[dict[str, Any]]) -> str:
        heads = [str(c.get("title", "")) for c in payload[:5]]
        return (
            f"You have {len(payload)} task(s) on your plate. "
            f"Top items: {', '.join(heads)}."
        )

    # ── ToolProvider protocol ────────────────────────────────────────

    def _resolve_user_ctx_from_args(
        self,
        arguments: dict[str, Any],
    ) -> UserContext:
        """Construct a ``UserContext`` from injected ``_user_*`` args."""
        user_id = str(arguments.get("_user_id") or "")
        if not user_id:
            raise PermissionError(
                "missing user context — task tools require an authenticated _user_id"
            )
        roles_raw = arguments.get("_user_roles") or []
        roles: frozenset[str]
        if isinstance(roles_raw, (list, tuple, set, frozenset)):
            roles = frozenset(str(r) for r in roles_raw)
        else:
            roles = frozenset()
        tz_raw = arguments.get("_user_tz")
        return UserContext(
            user_id=user_id,
            email=str(arguments.get("_user_email") or ""),
            display_name=str(arguments.get("_user_name") or user_id),
            roles=roles,
            tz=str(tz_raw) if isinstance(tz_raw, str) and tz_raw else None,
        )

    def get_tools(
        self,
        user_ctx: UserContext | None = None,
    ) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="task_lists",
                slash_group="tasks",
                slash_command="lists",
                slash_help="List your task lists: /tasks lists",
                description=(
                    "List every task list the current user can access "
                    "with id, name, backend, access type, and "
                    "is_default flag. Call this first when the user's "
                    "intent doesn't already name a list, especially for "
                    "add_task when there's no obvious default."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="add_task",
                slash_group="tasks",
                slash_command="add",
                slash_help=(
                    "Add a task: /tasks add <title> [list=...] [due=...] "
                    "[priority=high]"
                ),
                description=(
                    "Create a new task. ``list_id`` is optional — "
                    "resolution: (1) the user's default list if set, "
                    "(2) the user's only owned list, (3) ambiguous → "
                    "return a UIBlock select listing candidates rather "
                    "than guessing.\n"
                    "``due_at`` is ISO with timezone (e.g. "
                    "``2026-05-09T17:00:00-07:00``). Resolve natural "
                    "language with ``system_datetime`` first.\n"
                    "``priority`` accepts an int 0..4 OR a string "
                    "(none/low/medium/high/urgent).\n"
                    "``tags`` are topical labels (e.g. shopping, "
                    "phone-call). Do NOT use them for priority words.\n"
                    "``project`` is backend-defined and read-only for "
                    "Google Tasks / Todoist — the upstream owns it."
                ),
                parameters=[
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="Short task title.",
                    ),
                    ToolParameter(
                        name="list_id",
                        type=ToolParameterType.STRING,
                        description=(
                            "Target list id; see resolution rules in "
                            "description."
                        ),
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="notes",
                        type=ToolParameterType.STRING,
                        description="Long-form notes.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="due_at",
                        type=ToolParameterType.STRING,
                        description=(
                            "ISO with TZ (e.g. 2026-05-09T17:00:00-07:00). "
                            "Empty = no due date."
                        ),
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="due_at_tz",
                        type=ToolParameterType.STRING,
                        description=(
                            "IANA timezone for the user's wall clock "
                            "(e.g. America/Los_Angeles)."
                        ),
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="priority",
                        type=ToolParameterType.STRING,
                        description=(
                            "0..4 or none/low/medium/high/urgent."
                        ),
                        required=False,
                        default="0",
                    ),
                    ToolParameter(
                        name="tags",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "String list of topical labels (NOT priority "
                            "words)."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="project",
                        type=ToolParameterType.STRING,
                        description=(
                            "Project / group name. Read-only for "
                            "Google/Todoist lists."
                        ),
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="idempotency_key",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional dedupe key. AI callers normally "
                            "don't set this — the AI service injects an "
                            "implicit key. Inbox-AI sets it to the email "
                            "Message-Id."
                        ),
                        required=False,
                        default="",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="get_task",
                slash_group="tasks",
                slash_command="get",
                slash_help="Get a task: /tasks get <task_id>",
                description=(
                    "Fetch a single task by id with full notes, tags, "
                    "project, sync status."
                ),
                parameters=[
                    ToolParameter(
                        name="task_id",
                        type=ToolParameterType.STRING,
                        description="Task id (e.g. tsk_abc123).",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="list_tasks",
                slash_group="tasks",
                slash_command="list",
                slash_help=(
                    "List tasks: /tasks list [status=open] [list=...]"
                ),
                description=(
                    "Filtered task listing. Default status=open; pass "
                    "status=all to include DONE/CANCELLED. Includes "
                    "notes by default."
                ),
                parameters=[
                    ToolParameter(
                        name="status",
                        type=ToolParameterType.STRING,
                        description="open | done | cancelled | all",
                        required=False,
                        default="open",
                    ),
                    ToolParameter(
                        name="tag",
                        type=ToolParameterType.STRING,
                        description="Filter by tag.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="project",
                        type=ToolParameterType.STRING,
                        description="Filter by project.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="due_before",
                        type=ToolParameterType.STRING,
                        description="ISO UTC; tasks due before this.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="due_after",
                        type=ToolParameterType.STRING,
                        description="ISO UTC; tasks due after this.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="list_id",
                        type=ToolParameterType.STRING,
                        description="Filter to a single list.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description=(
                            "Filter to lists with this backend "
                            "(e.g. local, google_tasks)."
                        ),
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Max rows (default 50).",
                        required=False,
                        default=50,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="complete_task",
                slash_group="tasks",
                slash_command="done",
                slash_help="Mark complete: /tasks done <task_id>",
                description=(
                    "Mark a task complete by id. Idempotent — completing "
                    "an already-DONE task returns success."
                ),
                parameters=[
                    ToolParameter(
                        name="task_id",
                        type=ToolParameterType.STRING,
                        description="Task id.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="update_task",
                slash_group="tasks",
                slash_command="update",
                slash_help=(
                    "Update fields: /tasks update <task_id> [title=...] "
                    "[due=...] [priority=...]"
                ),
                description=(
                    "Patch fields (title, notes, due_at, due_at_tz, "
                    "priority, tags, project). Forbidden: id, list_id, "
                    "source_id, status, created_*, idempotency_key, "
                    "sync fields. Use complete_task / cancel_task / "
                    "delete_task for status transitions."
                ),
                parameters=[
                    ToolParameter(
                        name="task_id",
                        type=ToolParameterType.STRING,
                        description="Task id.",
                    ),
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="New title.",
                        required=False,
                    ),
                    ToolParameter(
                        name="notes",
                        type=ToolParameterType.STRING,
                        description="New notes.",
                        required=False,
                    ),
                    ToolParameter(
                        name="due_at",
                        type=ToolParameterType.STRING,
                        description="New due_at (ISO with TZ).",
                        required=False,
                    ),
                    ToolParameter(
                        name="due_at_tz",
                        type=ToolParameterType.STRING,
                        description="New IANA tz name.",
                        required=False,
                    ),
                    ToolParameter(
                        name="priority",
                        type=ToolParameterType.STRING,
                        description="0..4 or none/low/medium/high/urgent.",
                        required=False,
                    ),
                    ToolParameter(
                        name="tags",
                        type=ToolParameterType.ARRAY,
                        description="New tags (replaces current list).",
                        required=False,
                    ),
                    ToolParameter(
                        name="project",
                        type=ToolParameterType.STRING,
                        description="New project name.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="cancel_task",
                slash_group="tasks",
                slash_command="cancel",
                slash_help=(
                    "Cancel: /tasks cancel <task_id> [reason=\"...\"]"
                ),
                description=(
                    "Mark CANCELLED with optional reason. Distinct from "
                    "complete_task (which lies about completion) and "
                    "delete_task (which loses history)."
                ),
                parameters=[
                    ToolParameter(
                        name="task_id",
                        type=ToolParameterType.STRING,
                        description="Task id.",
                    ),
                    ToolParameter(
                        name="reason",
                        type=ToolParameterType.STRING,
                        description="Optional reason — appended to notes.",
                        required=False,
                        default="",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="delete_task",
                slash_group="tasks",
                slash_command="delete",
                slash_help=(
                    "Delete (soft) with confirmation: /tasks delete "
                    "<task_id>"
                ),
                description=(
                    "Soft-delete a task by id. Defaults to confirmation "
                    "via UIBlock — pass confirm=false (default) to "
                    "surface a Confirm/Cancel form, the form submission "
                    "re-invokes with confirm=true. Recoverable until "
                    "retention_days elapses; the upstream provider's row "
                    "is removed as a best-effort push. Hard-delete is "
                    "admin-only via the WS RPC."
                ),
                parameters=[
                    ToolParameter(
                        name="task_id",
                        type=ToolParameterType.STRING,
                        description="Task id.",
                    ),
                    ToolParameter(
                        name="confirm",
                        type=ToolParameterType.BOOLEAN,
                        description="Set true after user confirms.",
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="tasks_due",
                slash_group="tasks",
                slash_command="due",
                slash_help=(
                    "Tasks in a window: /tasks due [today|tomorrow|"
                    "this_week|this_month|overdue]"
                ),
                description=(
                    "Tasks due in a named window. Computed in the "
                    "requesting user's timezone."
                ),
                parameters=[
                    ToolParameter(
                        name="window",
                        type=ToolParameterType.STRING,
                        description=(
                            "today | tomorrow | this_week | this_month | "
                            "overdue"
                        ),
                        required=False,
                        default="today",
                    ),
                    ToolParameter(
                        name="list_id",
                        type=ToolParameterType.STRING,
                        description="Optional list filter.",
                        required=False,
                        default="",
                    ),
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description="Optional backend filter.",
                        required=False,
                        default="",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="summarize_today",
                slash_group="tasks",
                slash_command="summary",
                slash_help="AI summary of today: /tasks summary",
                description=(
                    "AI-generated 2–4 sentence summary of today's tasks "
                    "(due today + overdue). Single-source — the greeting "
                    "service's direct call uses the same code path."
                ),
                parameters=[],
                required_role="user",
            ),
        ]

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str | ToolOutput:
        match name:
            case "task_lists":
                return await self._tool_list_lists(arguments)
            case "add_task":
                return await self._tool_add_task(arguments)
            case "get_task":
                return await self._tool_get_task(arguments)
            case "list_tasks":
                return await self._tool_list_tasks(arguments)
            case "complete_task":
                return await self._tool_complete_task(arguments)
            case "update_task":
                return await self._tool_update_task(arguments)
            case "cancel_task":
                return await self._tool_cancel_task(arguments)
            case "delete_task":
                return await self._tool_delete_task(arguments)
            case "tasks_due":
                return await self._tool_tasks_due(arguments)
            case "summarize_today":
                return await self._tool_summarize_today(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    @staticmethod
    def _public_task(task: Task, backend: str = "") -> dict[str, Any]:
        """Project a Task for AI tool returns. Strips ``source_id``."""
        return {
            "id": task.id,
            "list_id": task.list_id,
            "title": task.title,
            "notes": task.notes,
            "due_at": task.due_at,
            "due_at_tz": task.due_at_tz,
            "completed_at": task.completed_at,
            "status": task.status.value,
            "priority": int(task.priority.value),
            "tags": list(task.tags),
            "project": task.project,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "backend": backend,
            "sync_status": task.sync_status.value,
            "last_push_error": task.last_push_error,
        }

    async def _tool_list_lists(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        lists = await self._load_lists()
        is_admin = self._is_admin(user_ctx)
        out: list[dict[str, Any]] = []
        for tl in lists:
            access = determine_access(user_ctx, tl, is_admin=is_admin)
            if access is None:
                continue
            out.append(
                {
                    "id": tl.id,
                    "name": tl.name,
                    "backend": tl.backend_name,
                    "access": access.value,
                    "is_default": tl.is_default,
                    "poll_enabled": tl.poll_enabled,
                    "degraded_since": tl.degraded_since,
                }
            )
        if not out:
            return (
                "You have no accessible task lists. Create one in "
                "Settings → Productivity → Tasks."
            )
        return json.dumps(out, indent=2)

    async def _tool_add_task(
        self, arguments: dict[str, Any]
    ) -> str | ToolOutput:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        title = str(arguments.get("title") or "").strip()
        if not title:
            return "title is required."

        list_id = str(arguments.get("list_id") or "").strip()
        if not list_id:
            default = await self._resolve_default_list(user_ctx)
            if default is None:
                # Multi-list ambiguous — return a UIBlock select to let
                # the user pick.
                accessible = await self.list_accessible_lists(user_ctx)
                owned = [
                    tl
                    for tl in accessible
                    if tl.owner_user_id == user_ctx.user_id
                ]
                if not owned:
                    return (
                        "No accessible task list found for your account. "
                        "Call /tasks lists first or create a list in "
                        "Settings."
                    )
                # Build a select UIBlock — round-trip the original
                # arguments so the user's pick re-invokes add_task with
                # the chosen list_id.
                from gilbert.interfaces.ui import (
                    UIBlock,
                    UIElement,
                    UIOption,
                )

                ui_block = UIBlock(
                    title="Which list?",
                    elements=[
                        UIElement(
                            type="label",
                            name="prompt",
                            label=(
                                f"Which list should '{title}' go on?"
                            ),
                        ),
                        UIElement(
                            type="text",
                            name="pending_arguments",
                            label="",
                            default=json.dumps(
                                {**arguments, "list_id": ""},
                                default=str,
                                sort_keys=True,
                            ),
                        ),
                        UIElement(
                            type="select",
                            name="list_id",
                            options=[
                                UIOption(value=tl.id, label=tl.name)
                                for tl in owned
                            ],
                        ),
                    ],
                    submit_label="Add to selected list",
                    tool_name="add_task",
                )
                return ToolOutput(
                    text=(
                        f"You have multiple lists — pick which one '{title}' "
                        "should go on."
                    ),
                    ui_blocks=[ui_block],
                )
            list_id = default.id

        # Build the task draft.
        priority = _coerce_priority(arguments.get("priority", "0"))
        tags_raw = arguments.get("tags") or []
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        explicit_key = str(arguments.get("idempotency_key") or "")
        if not explicit_key:
            # Synthesize from injected (_user_id, _conversation_id,
            # _tool_call_id) so the same AI tool call doesn't double-add.
            _uid = str(arguments.get("_user_id") or "")
            _cid = str(arguments.get("_conversation_id") or "")
            _tcid = str(arguments.get("_tool_call_id") or "")
            if _uid and _cid and _tcid:
                explicit_key = f"{_uid}:{_cid}:{_tcid}"

        draft = Task(
            title=title,
            notes=str(arguments.get("notes") or ""),
            due_at=str(arguments.get("due_at") or ""),
            due_at_tz=str(arguments.get("due_at_tz") or ""),
            priority=priority,
            tags=tags,
            project=str(arguments.get("project") or ""),
        )
        try:
            created = await self.add_task(
                list_id, draft, user_ctx, idempotency_key=explicit_key
            )
        except TaskListNotFoundError:
            return f"Task list not found: {list_id}"
        except TaskListPermissionError as exc:
            return str(exc)

        suffix = ""
        if created.sync_status == SyncStatus.PENDING_PUSH:
            suffix = " (syncing in background)"
        elif created.sync_status == SyncStatus.PUSH_FAILED:
            suffix = (
                f" (push failed: {created.last_push_error}; saved locally)"
            )
        tl = await self.get_list(list_id)
        backend_name = tl.backend_name if tl else ""
        public = self._public_task(created, backend=backend_name)
        return json.dumps({"created": True, "task": public, "suffix": suffix})

    async def _tool_get_task(self, arguments: dict[str, Any]) -> str:
        # Set context so get_task's visibility check sees this user.
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        from gilbert.core.context import set_current_user

        set_current_user(user_ctx)
        task_id = str(arguments.get("task_id") or "")
        if not task_id:
            return "task_id is required."
        task = await self.get_task(task_id)
        if task is None:
            return "Task not found"
        tl = await self.get_list(task.list_id)
        return json.dumps(
            self._public_task(task, backend=tl.backend_name if tl else ""),
            indent=2,
        )

    async def _tool_list_tasks(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        from gilbert.core.context import set_current_user

        set_current_user(user_ctx)
        status_raw = str(arguments.get("status") or "open").lower()
        status: TaskStatus | None
        if status_raw == "all":
            status = None
        else:
            try:
                status = TaskStatus(status_raw)
            except ValueError:
                status = TaskStatus.OPEN
        results = await self.search_tasks(
            status=status,
            tag=str(arguments.get("tag") or ""),
            project=str(arguments.get("project") or ""),
            due_before=str(arguments.get("due_before") or ""),
            due_after=str(arguments.get("due_after") or ""),
            list_id=(str(arguments.get("list_id") or "") or None),
            backend=(str(arguments.get("backend") or "") or None),
            limit=int(arguments.get("limit", 50) or 50),
        )
        # Backend lookup table for the projection.
        lists = {tl.id: tl for tl in await self._load_lists()}
        out = [
            self._public_task(
                t, backend=lists[t.list_id].backend_name if t.list_id in lists else ""
            )
            for t in results
        ]
        return json.dumps(out, indent=2)

    async def _tool_complete_task(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        task_id = str(arguments.get("task_id") or "")
        if not task_id:
            return "task_id is required."
        try:
            existing = await self._reload_task(task_id)
            if existing is None or existing.deleted_at:
                return "Task not found"
            if existing.status == TaskStatus.DONE:
                return f"Already completed: {existing.title}"
            updated = await self.complete_task(task_id, user_ctx)
        except TaskListPermissionError as exc:
            return str(exc)
        except TaskNotFoundError:
            return "Task not found"
        return json.dumps({"completed": True, "task_id": updated.id})

    async def _tool_update_task(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        task_id = str(arguments.get("task_id") or "")
        if not task_id:
            return "task_id is required."
        # Build patch from the AI's args.
        patch: dict[str, Any] = {}
        ignored: list[str] = []
        for key in (
            "title",
            "notes",
            "due_at",
            "due_at_tz",
            "priority",
            "tags",
            "project",
        ):
            if key in arguments:
                patch[key] = arguments[key]
        # Track what we silently dropped so the AI can surface it.
        for key in arguments:
            if key.startswith("_") or key == "task_id":
                continue
            if key in self._UPDATE_FORBIDDEN:
                ignored.append(key)
        try:
            updated = await self.update_task(task_id, patch, user_ctx)
        except TaskNotFoundError:
            return "Task not found"
        except TaskListPermissionError as exc:
            return str(exc)
        msg = {"updated": True, "task_id": updated.id}
        if ignored:
            msg["ignored"] = ignored
        return json.dumps(msg)

    async def _tool_cancel_task(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        task_id = str(arguments.get("task_id") or "")
        if not task_id:
            return "task_id is required."
        reason = str(arguments.get("reason") or "")
        try:
            updated = await self.cancel_task(
                task_id, user_ctx, reason=reason
            )
        except TaskNotFoundError:
            return "Task not found"
        except TaskListPermissionError as exc:
            return str(exc)
        return json.dumps({"cancelled": True, "task_id": updated.id})

    async def _tool_delete_task(
        self, arguments: dict[str, Any]
    ) -> str | ToolOutput:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        task_id = str(arguments.get("task_id") or "")
        if not task_id:
            return "task_id is required."
        existing = await self._reload_task(task_id)
        if existing is None or existing.deleted_at:
            return "Task not found"
        confirm = bool(arguments.get("confirm", False))
        tl = await self.get_list(existing.list_id)
        list_name = tl.name if tl else existing.list_id
        summary_lines = [
            f"**delete '{existing.title}'**",
            f"due: {existing.due_at or '(no due date)'}",
            f"list: {list_name}",
            (
                f"recoverable for {self._retention_days}d via admin restore"
                if self._retention_days > 0
                else "recoverable via admin restore (retention disabled)"
            ),
        ]

        async def _do_delete() -> str | ToolOutput:
            try:
                await self.delete_task(task_id, user_ctx, force=False)
            except TaskListPermissionError as exc:
                return str(exc)
            except TaskNotFoundError:
                return "Task not found"
            retention_msg = (
                f" (recoverable for {self._retention_days}d)"
                if self._retention_days > 0
                else ""
            )
            return f"Deleted '{existing.title}'{retention_msg}"

        return await confirm_or_execute(
            confirm=confirm,
            tool_name="delete_task",
            title="Delete task?",
            summary=f"Delete '{existing.title}' (id={task_id})?",
            summary_lines=summary_lines,
            arguments=arguments,
            execute=_do_delete,
        )

    async def _tool_tasks_due(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        from gilbert.core.context import set_current_user

        set_current_user(user_ctx)
        window = str(arguments.get("window") or "today").lower().strip()
        list_id = str(arguments.get("list_id") or "") or None
        backend = str(arguments.get("backend") or "") or None
        results = await self._compute_due_window(
            window, list_id=list_id, backend=backend
        )
        lists = {tl.id: tl for tl in await self._load_lists()}
        out = [
            self._public_task(
                t,
                backend=lists[t.list_id].backend_name if t.list_id in lists else "",
            )
            for t in results
        ]
        return json.dumps(out, indent=2)

    async def _compute_due_window(
        self,
        window: str,
        *,
        list_id: str | None = None,
        backend: str | None = None,
    ) -> list[Task]:
        user_ctx = get_current_user()
        tz = _resolve_user_tz(user_ctx)
        now_local = datetime.now(tz)
        if window == "overdue":
            return await self.overdue(list_id=list_id, backend=backend)
        if window == "today":
            return await self.due_today(list_id=list_id, backend=backend)
        if window == "tomorrow":
            tomorrow_local = now_local + timedelta(days=1)
            start = tomorrow_local.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end = tomorrow_local.replace(
                hour=23, minute=59, second=59, microsecond=999_999
            )
        elif window == "this_week":
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = (now_local + timedelta(days=7)).replace(
                hour=23, minute=59, second=59, microsecond=999_999
            )
        elif window == "this_month":
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = (now_local + timedelta(days=30)).replace(
                hour=23, minute=59, second=59, microsecond=999_999
            )
        else:
            return await self.due_today(list_id=list_id, backend=backend)
        start_utc = start.astimezone(UTC).isoformat().replace("+00:00", "Z")
        end_utc = end.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return await self.search_tasks(
            list_id=list_id,
            backend=backend,
            status=TaskStatus.OPEN,
            due_after=start_utc,
            due_before=end_utc,
        )

    async def _tool_summarize_today(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        return await self.summarize_today(user_ctx)

    # ── WS RPCs ──────────────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            # Lists
            "tasks.lists.list": self._ws_lists_list,
            "tasks.lists.get": self._ws_lists_get,
            "tasks.lists.create": self._ws_lists_create,
            "tasks.lists.update": self._ws_lists_update,
            "tasks.lists.delete": self._ws_lists_delete,
            "tasks.lists.test_connection": self._ws_lists_test,
            "tasks.lists.refresh": self._ws_lists_refresh,
            "tasks.lists.share_user": self._ws_lists_share_user,
            "tasks.lists.unshare_user": self._ws_lists_unshare_user,
            "tasks.lists.share_role": self._ws_lists_share_role,
            "tasks.lists.unshare_role": self._ws_lists_unshare_role,
            # Tasks
            "tasks.list": self._ws_tasks_list,
            "tasks.get": self._ws_tasks_get,
            "tasks.add": self._ws_tasks_add,
            "tasks.update": self._ws_tasks_update,
            "tasks.complete": self._ws_tasks_complete,
            "tasks.cancel": self._ws_tasks_cancel,
            "tasks.delete": self._ws_tasks_delete,
            "tasks.restore": self._ws_tasks_restore,
            "tasks.due_today": self._ws_due_today,
            "tasks.due_window": self._ws_due_window,
            "tasks.overdue": self._ws_overdue,
            "tasks.summary": self._ws_summary,
            "tasks.backends.list": self._ws_backends_list,
        }

    @staticmethod
    def _err(frame: dict[str, Any], msg: str, code: int) -> dict[str, Any]:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": msg,
            "code": code,
        }

    def _list_payload(
        self,
        task_list: TaskList,
        user_ctx: UserContext,
        is_admin: bool,
    ) -> dict[str, Any]:
        access = determine_access(user_ctx, task_list, is_admin=is_admin)
        return {
            "id": task_list.id,
            "name": task_list.name,
            "backend_name": task_list.backend_name,
            "backend_config": dict(task_list.backend_config),
            "owner_user_id": task_list.owner_user_id,
            "shared_with_users": list(task_list.shared_with_users),
            "shared_with_roles": list(task_list.shared_with_roles),
            "poll_enabled": task_list.poll_enabled,
            "poll_interval_sec": task_list.poll_interval_sec,
            "is_default": task_list.is_default,
            "created_at": task_list.created_at,
            "last_sync_at": task_list.last_sync_at,
            "degraded_since": task_list.degraded_since,
            "last_error": task_list.last_error,
            "access": access.value if access is not None else None,
            "can_admin": can_admin_list(user_ctx, task_list, is_admin=is_admin),
        }

    async def _ws_lists_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        is_admin = self._is_admin(user_ctx)
        lists = await self._load_lists()
        out = [
            self._list_payload(tl, user_ctx, is_admin)
            for tl in lists
            if determine_access(user_ctx, tl, is_admin=is_admin) is not None
        ]
        return {
            "type": "tasks.lists.list.result",
            "ref": frame.get("id"),
            "lists": out,
        }

    async def _ws_lists_get(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        tl = await self.get_list(list_id)
        if tl is None:
            return self._err(frame, "Task list not found", 404)
        user_ctx = conn.user_ctx
        is_admin = self._is_admin(user_ctx)
        if determine_access(user_ctx, tl, is_admin=is_admin) is None:
            return self._err(frame, "Forbidden", 403)
        return {
            "type": "tasks.lists.get.result",
            "ref": frame.get("id"),
            "list": self._list_payload(tl, user_ctx, is_admin),
        }

    async def _ws_lists_create(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        tl = TaskList(
            id=str(frame.get("id") or ""),
            name=str(frame.get("name") or ""),
            backend_name=str(frame.get("backend_name") or "local"),
            backend_config=dict(frame.get("backend_config") or {}),
            poll_enabled=bool(frame.get("poll_enabled", True)),
            poll_interval_sec=int(
                frame.get("poll_interval_sec", self._default_poll_interval_sec)
                or self._default_poll_interval_sec
            ),
            is_default=bool(frame.get("is_default", False)),
        )
        if not tl.name:
            return self._err(frame, "name is required", 400)
        try:
            created = await self.create_list(tl, user_ctx)
        except ValueError as exc:
            return self._err(frame, str(exc), 400)
        is_admin = self._is_admin(user_ctx)
        return {
            "type": "tasks.lists.create.result",
            "ref": frame.get("id"),
            "list": self._list_payload(created, user_ctx, is_admin),
        }

    async def _ws_lists_update(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        updates = frame.get("updates") or {}
        if not isinstance(updates, dict):
            return self._err(frame, "updates must be an object", 400)
        try:
            tl = await self.update_list(list_id, updates, conn.user_ctx)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except TaskListNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": "tasks.lists.update.result",
            "ref": frame.get("id"),
            "list": self._list_payload(tl, conn.user_ctx, is_admin),
        }

    async def _ws_lists_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        force = bool(frame.get("force", False))
        try:
            await self.delete_list(list_id, conn.user_ctx, force=force)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except TaskListNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except ValueError as exc:
            return self._err(frame, str(exc), 409)
        return {
            "type": "tasks.lists.delete.result",
            "ref": frame.get("id"),
            "status": "ok",
        }

    async def _ws_lists_test(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        try:
            result = await self.test_list_connection(list_id, conn.user_ctx)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except TaskListNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "tasks.lists.test_connection.result",
            "ref": frame.get("id"),
            **result,
        }

    async def _ws_lists_refresh(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        try:
            counts = await self.refresh_list(list_id, conn.user_ctx)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except TaskListNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "tasks.lists.refresh.result",
            "ref": frame.get("id"),
            **counts,
        }

    async def _ws_lists_share_user(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._ws_share(conn, frame, add_user=True)

    async def _ws_lists_unshare_user(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._ws_share(conn, frame, add_user=False)

    async def _ws_lists_share_role(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._ws_share(conn, frame, add_role=True)

    async def _ws_lists_unshare_role(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._ws_share(conn, frame, add_role=False)

    async def _ws_share(
        self,
        conn: Any,
        frame: dict[str, Any],
        *,
        add_user: bool | None = None,
        add_role: bool | None = None,
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        try:
            if add_user is True:
                tl = await self.share_user(
                    list_id, str(frame.get("user_id") or ""), conn.user_ctx
                )
                rtype = "share_user"
            elif add_user is False:
                tl = await self.unshare_user(
                    list_id, str(frame.get("user_id") or ""), conn.user_ctx
                )
                rtype = "unshare_user"
            elif add_role is True:
                tl = await self.share_role(
                    list_id, str(frame.get("role") or ""), conn.user_ctx
                )
                rtype = "share_role"
            else:
                tl = await self.unshare_role(
                    list_id, str(frame.get("role") or ""), conn.user_ctx
                )
                rtype = "unshare_role"
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except TaskListNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": f"tasks.lists.{rtype}.result",
            "ref": frame.get("id"),
            "list": self._list_payload(tl, conn.user_ctx, is_admin),
        }

    async def _ws_tasks_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        from gilbert.core.context import set_current_user

        set_current_user(conn.user_ctx)
        status_raw = str(frame.get("status") or "open").lower()
        status: TaskStatus | None
        if status_raw == "all":
            status = None
        else:
            try:
                status = TaskStatus(status_raw)
            except ValueError:
                status = TaskStatus.OPEN
        limit_raw = int(frame.get("limit", 50) or 50)
        limit = max(1, min(200, limit_raw))
        # Cursor is an opaque string token; v1 encodes the next-page
        # offset into the entity-store's existing offset primitive.
        # Spec §6.2-9 requires the (cursor, limit) pair on tasks.list.
        cursor_raw = str(frame.get("cursor") or "")
        try:
            offset = int(cursor_raw) if cursor_raw else 0
        except ValueError:
            offset = 0
        if offset < 0:
            offset = 0
        results = await self.search_tasks(
            list_id=(str(frame.get("list_id") or "") or None),
            backend=(str(frame.get("backend") or "") or None),
            status=status,
            tag=str(frame.get("tag") or ""),
            project=str(frame.get("project") or ""),
            due_before=str(frame.get("due_before") or ""),
            due_after=str(frame.get("due_after") or ""),
            limit=limit,
            offset=offset,
        )
        lists = {tl.id: tl for tl in await self._load_lists()}
        # If we got back a full page, signal more may exist.
        next_cursor = str(offset + limit) if len(results) >= limit else None
        return {
            "type": "tasks.list.result",
            "ref": frame.get("id"),
            "tasks": [
                self._public_task(
                    t,
                    backend=lists[t.list_id].backend_name
                    if t.list_id in lists
                    else "",
                )
                for t in results
            ],
            "next_cursor": next_cursor,
        }

    async def _ws_tasks_get(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        from gilbert.core.context import set_current_user

        set_current_user(conn.user_ctx)
        task_id = str(frame.get("task_id") or "")
        task = await self.get_task(task_id)
        if task is None:
            return self._err(frame, "Task not found", 404)
        tl = await self.get_list(task.list_id)
        return {
            "type": "tasks.get.result",
            "ref": frame.get("id"),
            "task": self._public_task(
                task, backend=tl.backend_name if tl else ""
            ),
        }

    async def _ws_tasks_add(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        list_id = str(frame.get("list_id") or "")
        if not list_id:
            return self._err(frame, "list_id is required", 400)
        priority = _coerce_priority(frame.get("priority", 0))
        tags_raw = frame.get("tags") or []
        tags = (
            [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        )
        draft = Task(
            title=str(frame.get("title") or ""),
            notes=str(frame.get("notes") or ""),
            due_at=str(frame.get("due_at") or ""),
            due_at_tz=str(frame.get("due_at_tz") or ""),
            priority=priority,
            tags=tags,
            project=str(frame.get("project") or ""),
        )
        if not draft.title:
            return self._err(frame, "title is required", 400)
        try:
            created = await self.add_task(
                list_id,
                draft,
                conn.user_ctx,
                idempotency_key=str(frame.get("idempotency_key") or ""),
            )
        except TaskListNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        tl = await self.get_list(list_id)
        return {
            "type": "tasks.add.result",
            "ref": frame.get("id"),
            "task": self._public_task(
                created, backend=tl.backend_name if tl else ""
            ),
        }

    async def _ws_tasks_update(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = str(frame.get("task_id") or "")
        updates = frame.get("updates") or {}
        if not isinstance(updates, dict):
            return self._err(frame, "updates must be an object", 400)
        try:
            updated = await self.update_task(
                task_id, updates, conn.user_ctx
            )
        except TaskNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        tl = await self.get_list(updated.list_id)
        return {
            "type": "tasks.update.result",
            "ref": frame.get("id"),
            "task": self._public_task(
                updated, backend=tl.backend_name if tl else ""
            ),
        }

    async def _ws_tasks_complete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = str(frame.get("task_id") or "")
        try:
            updated = await self.complete_task(task_id, conn.user_ctx)
        except TaskNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        tl = await self.get_list(updated.list_id)
        return {
            "type": "tasks.complete.result",
            "ref": frame.get("id"),
            "task": self._public_task(
                updated, backend=tl.backend_name if tl else ""
            ),
        }

    async def _ws_tasks_cancel(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = str(frame.get("task_id") or "")
        reason = str(frame.get("reason") or "")
        try:
            updated = await self.cancel_task(
                task_id, conn.user_ctx, reason=reason
            )
        except TaskNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        tl = await self.get_list(updated.list_id)
        return {
            "type": "tasks.cancel.result",
            "ref": frame.get("id"),
            "task": self._public_task(
                updated, backend=tl.backend_name if tl else ""
            ),
        }

    async def _ws_tasks_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = str(frame.get("task_id") or "")
        force = bool(frame.get("force", False))
        try:
            await self.delete_task(
                task_id, conn.user_ctx, force=force
            )
        except TaskNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        return {
            "type": "tasks.delete.result",
            "ref": frame.get("id"),
            "status": "ok",
            "soft": not force,
        }

    async def _ws_tasks_restore(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = str(frame.get("task_id") or "")
        try:
            restored = await self.restore_task(task_id, conn.user_ctx)
        except TaskNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except TaskListPermissionError as exc:
            return self._err(frame, str(exc), 403)
        tl = await self.get_list(restored.list_id)
        return {
            "type": "tasks.restore.result",
            "ref": frame.get("id"),
            "task": self._public_task(
                restored, backend=tl.backend_name if tl else ""
            ),
        }

    async def _ws_due_today(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        from gilbert.core.context import set_current_user

        set_current_user(conn.user_ctx)
        results = await self.due_today(
            list_id=(str(frame.get("list_id") or "") or None),
            backend=(str(frame.get("backend") or "") or None),
        )
        lists = {tl.id: tl for tl in await self._load_lists()}
        return {
            "type": "tasks.due_today.result",
            "ref": frame.get("id"),
            "tasks": [
                self._public_task(
                    t,
                    backend=lists[t.list_id].backend_name
                    if t.list_id in lists
                    else "",
                )
                for t in results
            ],
        }

    async def _ws_due_window(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        from gilbert.core.context import set_current_user

        set_current_user(conn.user_ctx)
        window = str(frame.get("window") or "today")
        results = await self._compute_due_window(
            window,
            list_id=(str(frame.get("list_id") or "") or None),
            backend=(str(frame.get("backend") or "") or None),
        )
        lists = {tl.id: tl for tl in await self._load_lists()}
        return {
            "type": "tasks.due_window.result",
            "ref": frame.get("id"),
            "window": window,
            "tasks": [
                self._public_task(
                    t,
                    backend=lists[t.list_id].backend_name
                    if t.list_id in lists
                    else "",
                )
                for t in results
            ],
        }

    async def _ws_overdue(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        from gilbert.core.context import set_current_user

        set_current_user(conn.user_ctx)
        results = await self.overdue(
            list_id=(str(frame.get("list_id") or "") or None),
            backend=(str(frame.get("backend") or "") or None),
        )
        lists = {tl.id: tl for tl in await self._load_lists()}
        return {
            "type": "tasks.overdue.result",
            "ref": frame.get("id"),
            "tasks": [
                self._public_task(
                    t,
                    backend=lists[t.list_id].backend_name
                    if t.list_id in lists
                    else "",
                )
                for t in results
            ],
        }

    async def _ws_summary(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        text = await self.summarize_today(conn.user_ctx)
        return {
            "type": "tasks.summary.result",
            "ref": frame.get("id"),
            "summary": text,
        }

    async def _ws_backends_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        backends = []
        for name, cls in TaskBackend.registered_backends().items():
            params = [
                {
                    "key": p.key,
                    "type": p.type.value if hasattr(p.type, "value") else str(p.type),
                    "description": p.description,
                    "default": p.default,
                    "restart_required": p.restart_required,
                    "sensitive": p.sensitive,
                    "choices": list(p.choices) if p.choices else None,
                    "multiline": p.multiline,
                    "ai_prompt": p.ai_prompt,
                    "backend_param": True,
                }
                for p in cls.backend_config_params()
            ]
            backends.append({"name": name, "config_params": params})
        return {
            "type": "tasks.backends.list.result",
            "ref": frame.get("id"),
            "backends": backends,
        }


# Make the protocol satisfaction explicit so isinstance(svc, TaskProvider)
# checks at boot have a typed referent. ``InboxProvider`` follows the
# same pattern at the bottom of inbox.py.
_ = TaskProvider
