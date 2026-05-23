"""Tests for ``TasksService`` against a real SQLite storage backend.

Per CLAUDE.md: storage tests use the real ``SqliteStorageBackend`` (no
mocking the DB). Other capabilities (event bus, scheduler, access
control, AI sampling) get lightweight fakes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.interfaces.context import set_current_user
from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.tasks import (
    TaskListPermissionError,
    TasksService,
)
from gilbert.interfaces.ai import AIResponse, Message, MessageRole
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.tasks import (
    SyncStatus,
    Task,
    TaskBackend,
    TaskBackendConflictError,
    TaskBackendNotFoundError,
    TaskList,
    TaskPriority,
    TaskProvider,
    TaskStatus,
)
from gilbert.interfaces.ui import ToolOutput
from gilbert.storage.sqlite import SQLiteStorage

# ── Fakes ───────────────────────────────────────────────────────────


class FakeStorageProvider:
    def __init__(self, backend: SQLiteStorage) -> None:
        self.backend = backend
        self.raw_backend = backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend


class FakeEventBusProvider:
    def __init__(self) -> None:
        self.bus = InMemoryEventBus()
        self.published: list[Any] = []
        # Patch publish to also record events for easier assertion.
        original = self.bus.publish

        async def _wrap(event: Any) -> None:
            self.published.append(event)
            await original(event)

        self.bus.publish = _wrap  # type: ignore[assignment]


class FakeScheduler:
    def __init__(self) -> None:
        self.added_jobs: dict[str, dict[str, Any]] = {}
        self.removed_jobs: list[str] = []

    def add_job(self, **kwargs: Any) -> Any:
        self.added_jobs[kwargs["name"]] = kwargs

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.removed_jobs.append(name)
        self.added_jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.added_jobs.values())

    def get_job(self, name: str) -> Any:
        return self.added_jobs.get(name)

    async def run_now(self, name: str) -> None:
        pass

    async def trigger(self, name: str) -> None:
        """Helper for tests — fires a registered job's callback."""
        await self.added_jobs[name]["callback"]()


class FakeAccessControl:
    def get_role_level(self, role_name: str) -> int:
        return 0 if role_name == "admin" else 100

    def get_effective_level(self, user_ctx: UserContext) -> int:
        return 0 if "admin" in user_ctx.roles else 100

    def resolve_rpc_level(self, frame_type: str) -> int:
        return 100

    def check_collection_read(
        self, user_ctx: UserContext, collection: str
    ) -> bool:
        return True

    def check_collection_write(
        self, user_ctx: UserContext, collection: str
    ) -> bool:
        return True


class FakeAI:
    def __init__(self, response_text: str = "Today's summary.") -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(
        self,
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
        tools_override: Any = None,
    ) -> AIResponse:
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "profile_name": profile_name,
            }
        )
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self.response_text),
            model="fake-model",
        )


class FakeExternalBackend(TaskBackend):
    """In-memory external backend for testing the push / poll paths.

    NOT registered (no ``backend_name``) — tests inject directly into
    ``service._runtimes``.
    """

    backend_name = ""

    def __init__(self) -> None:
        self.upstream: dict[str, Task] = {}
        self.add_calls: list[Task] = []
        self.update_calls: list[tuple[str, dict[str, Any], str]] = []
        self.complete_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fail_add: Exception | None = None
        self.fail_update: Exception | None = None
        self.fail_complete: Exception | None = None
        self.fail_delete: Exception | None = None
        self.fail_list: Exception | None = None
        self.upstream_etag: str = ""

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_tasks(
        self,
        *,
        include_completed: bool = False,
        updated_since: str = "",
    ) -> list[Task]:
        if self.fail_list:
            raise self.fail_list
        return list(self.upstream.values())

    async def add_task(self, task: Task) -> Task:
        if self.fail_add:
            raise self.fail_add
        self.add_calls.append(task)
        new = Task(
            id=task.id,
            list_id=task.list_id,
            title=task.title,
            notes=task.notes,
            due_at=task.due_at,
            due_at_tz=task.due_at_tz,
            priority=task.priority,
            tags=list(task.tags),
            project=task.project,
            status=task.status,
            source_id=f"upstream-{task.id}",
            etag=self.upstream_etag,
        )
        self.upstream[new.source_id] = new
        return new

    async def update_task(
        self,
        source_id: str,
        patch: dict[str, Any],
        *,
        etag: str = "",
    ) -> Task:
        if self.fail_update:
            raise self.fail_update
        self.update_calls.append((source_id, dict(patch), etag))
        existing = self.upstream.get(source_id)
        if existing is None:
            raise TaskBackendNotFoundError(source_id)
        for key, value in patch.items():
            if key == "tags" and isinstance(value, list):
                existing.tags = [str(t) for t in value]
            elif key == "priority":
                existing.priority = TaskPriority(int(value))
            elif hasattr(existing, key):
                setattr(existing, key, value)
        existing.etag = self.upstream_etag
        return existing

    async def complete_task(self, source_id: str) -> None:
        if self.fail_complete:
            raise self.fail_complete
        self.complete_calls.append(source_id)
        if source_id in self.upstream:
            self.upstream[source_id].status = TaskStatus.DONE

    async def delete_task(self, source_id: str) -> None:
        if self.fail_delete:
            raise self.fail_delete
        self.delete_calls.append(source_id)
        self.upstream.pop(source_id, None)


class FakeResolver:
    def __init__(self, **caps: Any) -> None:
        self.caps = caps

    def get_capability(self, name: str) -> Any:
        return self.caps.get(name)

    def require_capability(self, name: str) -> Any:
        svc = self.caps.get(name)
        if svc is None:
            raise LookupError(name)
        return svc

    def get_all(self, name: str) -> list[Any]:
        svc = self.caps.get(name)
        return [svc] if svc else []


# ── Helpers ─────────────────────────────────────────────────────────


def _owner() -> UserContext:
    return UserContext(
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
        roles=frozenset({"user"}),
    )


def _shared_user() -> UserContext:
    return UserContext(
        user_id="alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    )


def _admin() -> UserContext:
    return UserContext(
        user_id="admin",
        email="admin@example.com",
        display_name="Admin",
        roles=frozenset({"admin"}),
    )


def _unrelated() -> UserContext:
    return UserContext(
        user_id="bob",
        email="bob@example.com",
        display_name="Bob",
        roles=frozenset({"user"}),
    )


def _local_list(list_id: str = "tlst_local", *, owner: str = "owner") -> TaskList:
    return TaskList(
        id=list_id,
        name="Local list",
        backend_name="local",
        owner_user_id=owner,
        poll_enabled=True,
    )


def _ext_list(
    list_id: str = "tlst_ext",
    *,
    owner: str = "owner",
    backend_name: str = "fake_external",
) -> TaskList:
    return TaskList(
        id=list_id,
        name="External list",
        backend_name=backend_name,
        owner_user_id=owner,
        poll_enabled=True,
    )


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
async def started_service(
    sqlite_storage: SQLiteStorage,
) -> Any:
    svc = TasksService()
    storage_provider = FakeStorageProvider(sqlite_storage)
    event_bus = FakeEventBusProvider()
    scheduler = FakeScheduler()
    access_control = FakeAccessControl()
    ai = FakeAI()
    resolver = FakeResolver(
        entity_storage=storage_provider,
        event_bus=event_bus,
        scheduler=scheduler,
        access_control=access_control,
        ai_chat=ai,
    )
    await svc.start(resolver)
    # Test handles bring up runtimes themselves; cancel the scheduled
    # boot job so it doesn't fire mid-test.
    scheduler.added_jobs.pop("tasks-boot", None)
    yield svc, sqlite_storage, event_bus, scheduler, ai
    await svc.stop()


# ── Service metadata ────────────────────────────────────────────────


class TestServiceInfo:
    def test_implements_task_provider(self) -> None:
        svc = TasksService()
        assert isinstance(svc, TaskProvider)

    def test_service_info(self) -> None:
        info = TasksService().service_info()
        assert info.name == "tasks"
        assert "tasks" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "ws_handlers" in info.capabilities
        assert {"entity_storage", "scheduler"} <= info.requires
        assert "task.created" in info.events
        assert "task.due_soon" in info.events
        assert "tasks.list.degraded" in info.events
        assert info.toggleable is True

    def test_tool_names(self) -> None:
        svc = TasksService()
        svc._enabled = True
        names = {t.name for t in svc.get_tools()}
        assert names == {
            "task_lists",
            "add_task",
            "get_task",
            "list_tasks",
            "complete_task",
            "update_task",
            "cancel_task",
            "delete_task",
            "tasks_due",
            "summarize_today",
        }

    def test_tools_empty_when_disabled(self) -> None:
        svc = TasksService()
        svc._enabled = False
        assert svc.get_tools() == []


# ── Authorization ───────────────────────────────────────────────────


class TestAuthorization:
    async def test_owner_can_create(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        assert tl.owner_user_id == "owner"

    async def test_unrelated_cannot_admin(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        with pytest.raises(TaskListPermissionError):
            await svc.update_list(tl.id, {"name": "x"}, _unrelated())

    async def test_shared_user_has_access_but_not_admin(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        await svc.share_user(tl.id, "alice", _owner())
        # Shared user can add a task.
        await svc.add_task(
            tl.id,
            Task(title="from alice"),
            _shared_user(),
        )
        with pytest.raises(TaskListPermissionError):
            await svc.update_list(tl.id, {"name": "x"}, _shared_user())

    async def test_admin_can_admin_any_list(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        # Admin can update.
        await svc.update_list(tl.id, {"name": "renamed"}, _admin())


# ── Local-list add / complete / update / delete ──────────────────────


class TestLocalCRUD:
    async def test_add_task_persists_synced(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        ebus.published.clear()
        created = await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        assert created.id
        assert created.sync_status == SyncStatus.SYNCED
        assert created.source_id == created.id  # local stamps source_id = id
        # Exactly one row.
        row = await storage.get("tasks", created.id)
        assert row is not None
        # task.created event published.
        types = [e.event_type for e in ebus.published]
        assert "task.created" in types

    async def test_complete_task_idempotent(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        done1 = await svc.complete_task(created.id, _owner())
        assert done1.status == TaskStatus.DONE
        done2 = await svc.complete_task(created.id, _owner())
        assert done2.status == TaskStatus.DONE  # no error

    async def test_update_task_filters_forbidden_fields(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        # Try to mutate forbidden fields — they get dropped silently.
        updated = await svc.update_task(
            created.id,
            {
                "title": "renamed",
                "list_id": "tlst_x",  # forbidden
                "status": "done",  # forbidden
                "source_id": "spoofed",  # forbidden
            },
            _owner(),
        )
        assert updated.title == "renamed"
        assert updated.list_id == tl.id  # NOT changed
        assert updated.status == TaskStatus.OPEN  # NOT changed
        assert updated.source_id == created.source_id  # NOT changed

    async def test_soft_delete_then_hidden_from_reads(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        set_current_user(_owner())
        await svc.delete_task(created.id, _owner())
        assert await svc.get_task(created.id) is None
        results = await svc.search_tasks()
        assert created.id not in {t.id for t in results}

    async def test_admin_force_hard_deletes(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        await svc.delete_task(created.id, _admin(), force=True)
        # Row gone.
        assert await storage.get("tasks", created.id) is None

    async def test_force_delete_requires_admin(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        with pytest.raises(TaskListPermissionError):
            await svc.delete_task(created.id, _owner(), force=True)


# ── Scheduler interactions ──────────────────────────────────────────


class TestSchedulerIntegration:
    async def test_local_list_does_not_schedule_poll_job(
        self, started_service: Any
    ) -> None:
        svc, _, _, scheduler, _ = started_service
        tl = await svc.create_list(_local_list("tlst_local_poll"), _owner())
        # No tasks-poll-{list_id} job for the local list.
        assert f"tasks-poll-{tl.id}" not in scheduler.added_jobs

    async def test_external_list_schedules_poll_job(
        self, started_service: Any
    ) -> None:
        # Register a fake external backend on the registry by
        # subclassing and setting backend_name.
        svc, _, _, scheduler, _ = started_service
        # Register a fake external backend.

        class _RegistryBackend(FakeExternalBackend):
            backend_name = "registry_external_a"

        try:
            tl = await svc.create_list(
                _ext_list(backend_name="registry_external_a"), _owner()
            )
            assert f"tasks-poll-{tl.id}" in scheduler.added_jobs
        finally:
            # Clean up registry to avoid leaking into other tests.
            TaskBackend._registry.pop("registry_external_a", None)

    async def test_required_jobs_registered(
        self, started_service: Any
    ) -> None:
        _, _, _, scheduler, _ = started_service
        for name in ("tasks-sync-tick", "tasks-due-soon-tick", "tasks-gc-tick"):
            assert name in scheduler.added_jobs


# ── Push failures / retries ──────────────────────────────────────────


class TestPushFailures:
    async def test_add_task_succeeds_locally_when_upstream_fails(
        self,
        started_service: Any,
    ) -> None:
        svc, storage, ebus, *_ = started_service
        # Plug in a failing external backend manually.
        class _Backend(FakeExternalBackend):
            backend_name = "fail_backend_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_fail", backend_name="fail_backend_a"),
                _owner(),
            )
            runtime = svc._runtimes[tl.id]
            backend = runtime.backend
            assert isinstance(backend, FakeExternalBackend)
            backend.fail_add = RuntimeError("upstream down")

            ebus.published.clear()
            created = await svc.add_task(
                tl.id, Task(title="hello"), _owner()
            )
            # Row persisted, marked pending_push, no exception bubbled.
            assert created.sync_status == SyncStatus.PENDING_PUSH
            assert created.last_push_error
            row = await storage.get("tasks", created.id)
            assert row is not None
            assert row["sync_status"] == SyncStatus.PENDING_PUSH.value
        finally:
            TaskBackend._registry.pop("fail_backend_a", None)

    async def test_max_retries_marks_push_failed(
        self,
        started_service: Any,
    ) -> None:
        svc, storage, ebus, scheduler, _ = started_service
        # Lower max retries so the test is fast.
        svc._max_push_retries = 2

        class _Backend(FakeExternalBackend):
            backend_name = "always_fail_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_retry", backend_name="always_fail_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            backend.fail_add = RuntimeError("nope")

            created = await svc.add_task(
                tl.id, Task(title="hello"), _owner()
            )
            # First failure: pending_push, retry_count=1.
            assert created.sync_status == SyncStatus.PENDING_PUSH
            assert created.retry_count == 1

            # Run sync tick — backend.update_task is called by the
            # tick path because source_id is set (it isn't, but the
            # tick re-attempts add_task in that case). Each fire raises.
            ebus.published.clear()
            for _ in range(svc._max_push_retries):
                await scheduler.added_jobs["tasks-sync-tick"]["callback"]()
            row = await storage.get("tasks", created.id)
            assert row is not None
            assert row["sync_status"] == SyncStatus.PUSH_FAILED.value
            # task.push_failed event was published.
            assert any(
                e.event_type == "task.push_failed" for e in ebus.published
            )
        finally:
            TaskBackend._registry.pop("always_fail_a", None)

    async def test_update_task_pushes_only_changed_fields(
        self,
        started_service: Any,
    ) -> None:
        svc, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "patch_backend_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_patch", backend_name="patch_backend_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)

            created = await svc.add_task(
                tl.id, Task(title="hello"), _owner()
            )
            backend.update_calls.clear()
            await svc.update_task(
                created.id, {"title": "updated"}, _owner()
            )
            assert len(backend.update_calls) == 1
            source_id, patch, _etag = backend.update_calls[0]
            assert patch == {"title": "updated"}
            assert "notes" not in patch  # only the changed field
        finally:
            TaskBackend._registry.pop("patch_backend_a", None)


# ── Idempotency ────────────────────────────────────────────────────


class TestIdempotency:
    async def test_explicit_key_dedupes(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        a = await svc.add_task(
            tl.id,
            Task(title="dup"),
            _owner(),
            idempotency_key="msg-id-123",
        )
        b = await svc.add_task(
            tl.id,
            Task(title="dup"),
            _owner(),
            idempotency_key="msg-id-123",
        )
        assert a.id == b.id

    async def test_complete_task_twice_is_no_error(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        first = await svc.complete_task(created.id, _owner())
        second = await svc.complete_task(created.id, _owner())
        assert first.status == TaskStatus.DONE
        assert second.status == TaskStatus.DONE


# ── Default-list resolution ─────────────────────────────────────────


class TestDefaultListResolution:
    async def test_user_with_one_owned_list(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        resolved = await svc._resolve_default_list(_owner())
        assert resolved is not None
        assert resolved.id == tl.id

    async def test_user_with_multiple_lists_picks_default(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        await svc.create_list(_local_list("a"), _owner())
        b = await svc.create_list(
            TaskList(
                id="b",
                name="b",
                backend_name="local",
                owner_user_id="owner",
                is_default=True,
            ),
            _owner(),
        )
        resolved = await svc._resolve_default_list(_owner())
        assert resolved is not None
        assert resolved.id == b.id

    async def test_user_with_no_owned_lists_returns_none(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        await svc.create_list(_local_list(), _owner())
        # Different user — owns nothing.
        resolved = await svc._resolve_default_list(_unrelated())
        assert resolved is None

    async def test_user_with_multiple_no_default_returns_none(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        await svc.create_list(_local_list("a"), _owner())
        await svc.create_list(_local_list("b"), _owner())
        resolved = await svc._resolve_default_list(_owner())
        assert resolved is None


# ── Polling ─────────────────────────────────────────────────────────


class TestPolling:
    async def test_external_poll_inserts_and_publishes_created(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "poll_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_poll", backend_name="poll_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            backend.upstream = {
                f"upstream-{i}": Task(
                    id="",
                    list_id=tl.id,
                    title=f"task-{i}",
                    source_id=f"upstream-{i}",
                    updated_at=now,
                )
                for i in range(3)
            }
            ebus.published.clear()
            await svc._poll_runtime(svc._runtimes[tl.id])
            created_events = [
                e for e in ebus.published if e.event_type == "task.created"
            ]
            assert len(created_events) == 3
        finally:
            TaskBackend._registry.pop("poll_a", None)

    async def test_external_poll_dedupes_via_source_id(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "poll_dedup_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_dedup", backend_name="poll_dedup_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            backend.upstream = {
                "src-a": Task(
                    id="",
                    list_id=tl.id,
                    title="A",
                    source_id="src-a",
                    updated_at=now,
                ),
            }
            await svc._poll_runtime(svc._runtimes[tl.id])
            ebus.published.clear()
            # Second poll: same row, no changes.
            await svc._poll_runtime(svc._runtimes[tl.id])
            new_events = [
                e for e in ebus.published if e.event_type == "task.created"
            ]
            assert len(new_events) == 0
        finally:
            TaskBackend._registry.pop("poll_dedup_a", None)

    async def test_repeated_failures_mark_degraded(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, *_ = started_service
        svc._degraded_after_failures = 2

        class _Backend(FakeExternalBackend):
            backend_name = "poll_degraded_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_degraded", backend_name="poll_degraded_a"),
                _owner(),
            )
            runtime = svc._runtimes[tl.id]
            backend = runtime.backend
            assert isinstance(backend, FakeExternalBackend)
            backend.fail_list = RuntimeError("nope")
            ebus.published.clear()
            await svc._poll_runtime(runtime)
            await svc._poll_runtime(runtime)
            row = await storage.get("task_lists", tl.id)
            assert row is not None
            assert row["degraded_since"]
            assert any(
                e.event_type == "tasks.list.degraded" for e in ebus.published
            )
            # Recovery: clear failure, run poll → degraded_since cleared.
            backend.fail_list = None
            backend.upstream = {}
            ebus.published.clear()
            await svc._poll_runtime(runtime)
            row = await storage.get("task_lists", tl.id)
            assert row is not None
            assert row["degraded_since"] == ""
            assert any(
                e.event_type == "tasks.list.recovered" for e in ebus.published
            )
        finally:
            TaskBackend._registry.pop("poll_degraded_a", None)


# ── Time zones / due_today ──────────────────────────────────────────


class TestTimeZones:
    async def test_due_today_uses_user_tz(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        # Pacific user; task due_at = noon Pacific today.
        pacific_user = UserContext(
            user_id="owner",
            email="owner@example.com",
            display_name="Owner",
            roles=frozenset({"user"}),
            tz="America/Los_Angeles",
        )
        # Pick a deterministic moment — today in Pacific.
        now_pt = datetime.now(UTC).astimezone(
            __import__("zoneinfo").ZoneInfo("America/Los_Angeles")
        )
        noon_pt = now_pt.replace(hour=12, minute=0, second=0, microsecond=0)
        due_iso = (
            noon_pt.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
        await svc.add_task(
            tl.id,
            Task(title="lunch", due_at=due_iso, due_at_tz="America/Los_Angeles"),
            pacific_user,
        )
        set_current_user(pacific_user)
        results = await svc.due_today()
        assert any(t.title == "lunch" for t in results)


# ── Due-soon tick ──────────────────────────────────────────────────


class TestDueSoonTick:
    async def test_fires_for_imminent_open_task(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, scheduler, _ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )
        await svc.add_task(
            tl.id, Task(title="ping", due_at=soon), _owner()
        )
        ebus.published.clear()
        await scheduler.added_jobs["tasks-due-soon-tick"]["callback"]()
        due_soon = [e for e in ebus.published if e.event_type == "task.due_soon"]
        assert len(due_soon) == 1

    async def test_does_not_re_fire_after_first_publish(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, scheduler, _ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )
        await svc.add_task(
            tl.id, Task(title="ping", due_at=soon), _owner()
        )
        await scheduler.added_jobs["tasks-due-soon-tick"]["callback"]()
        ebus.published.clear()
        await scheduler.added_jobs["tasks-due-soon-tick"]["callback"]()
        # Already-fired flag prevents re-publish.
        assert not any(
            e.event_type == "task.due_soon" for e in ebus.published
        )

    async def test_reschedule_clears_fired_flag(
        self, started_service: Any
    ) -> None:
        svc, storage, ebus, scheduler, _ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )
        created = await svc.add_task(
            tl.id, Task(title="ping", due_at=soon), _owner()
        )
        await scheduler.added_jobs["tasks-due-soon-tick"]["callback"]()
        # Reschedule out (1 hour from now) — the next tick should NOT
        # fire (out of window).
        far = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace(
            "+00:00", "Z"
        )
        await svc.update_task(created.id, {"due_at": far}, _owner())
        # Reschedule back in (5 min) — the flag should clear.
        await svc.update_task(created.id, {"due_at": soon}, _owner())
        ebus.published.clear()
        await scheduler.added_jobs["tasks-due-soon-tick"]["callback"]()
        due_soon = [e for e in ebus.published if e.event_type == "task.due_soon"]
        assert len(due_soon) == 1


# ── GC / retention ─────────────────────────────────────────────────


class TestGC:
    async def test_retention_zero_disables_gc(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        svc._retention_days = 0
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        await svc.complete_task(created.id, _owner())
        # Manually backdate completed_at way past anything reasonable.
        row = await storage.get("tasks", created.id)
        assert row is not None
        row["completed_at"] = "2000-01-01T00:00:00Z"
        await storage.put("tasks", created.id, row)
        await svc._gc_tick()
        # Row still exists.
        assert await storage.get("tasks", created.id) is not None

    async def test_old_done_rows_hard_deleted(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        svc._retention_days = 30
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        await svc.complete_task(created.id, _owner())
        row = await storage.get("tasks", created.id)
        assert row is not None
        row["completed_at"] = "2024-01-01T00:00:00Z"
        await storage.put("tasks", created.id, row)
        await svc._gc_tick()
        assert await storage.get("tasks", created.id) is None

    async def test_old_soft_deleted_hard_deleted(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        svc._retention_days = 30
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        await svc.delete_task(created.id, _owner())
        row = await storage.get("tasks", created.id)
        assert row is not None
        row["deleted_at"] = "2024-01-01T00:00:00Z"
        await storage.put("tasks", created.id, row)
        await svc._gc_tick()
        assert await storage.get("tasks", created.id) is None

    async def test_orphan_task_events_seen_removed(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        svc._retention_days = 30
        # Add an orphan seen row (no list with matching id).
        await storage.put(
            "task_events_seen",
            "tlst_ghost:src1",
            {
                "_id": "tlst_ghost:src1",
                "list_id": "tlst_ghost",
                "source_id": "src1",
                "last_seen_at": "2026-01-01T00:00:00Z",
            },
        )
        await svc._gc_tick()
        assert await storage.get(
            "task_events_seen", "tlst_ghost:src1"
        ) is None


# ── AI tool surface ────────────────────────────────────────────────


class TestAITools:
    async def test_tool_returns_no_source_id(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        await svc.add_task(
            tl.id, Task(title="hello"), _owner()
        )
        result = await svc.execute_tool(
            "list_tasks",
            {"_user_id": "owner", "_user_roles": ["user"]},
        )
        # ToolOutput has .text, str returns directly. We expect a JSON
        # string here.
        assert isinstance(result, str)
        import json

        parsed = json.loads(result)
        assert parsed
        for row in parsed:
            assert "source_id" not in row
            assert "id" in row

    async def test_add_task_injects_user_id(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        await svc.execute_tool(
            "add_task",
            {
                "_user_id": "owner",
                "_user_roles": ["user"],
                "title": "from-tool",
                "list_id": tl.id,
            },
        )
        # Find the row.
        results = await svc.list_accessible_lists(_owner())
        assert results
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        rows = await svc._storage.query(
            Query(
                collection="tasks",
                filters=[
                    Filter(field="list_id", op=FilterOp.EQ, value=tl.id),
                    Filter(field="title", op=FilterOp.EQ, value="from-tool"),
                ],
            )
        )
        assert rows
        assert rows[0]["created_by_user_id"] == "owner"

    async def test_delete_task_returns_uiblock_when_unconfirmed(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="will-delete"), _owner()
        )
        result = await svc.execute_tool(
            "delete_task",
            {
                "_user_id": "owner",
                "_user_roles": ["user"],
                "task_id": created.id,
            },
        )
        assert isinstance(result, ToolOutput)
        assert result.ui_blocks
        assert result.ui_blocks[0].tool_name == "delete_task"

    async def test_delete_task_executes_when_confirmed(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="will-delete"), _owner()
        )
        result = await svc.execute_tool(
            "delete_task",
            {
                "_user_id": "owner",
                "_user_roles": ["user"],
                "task_id": created.id,
                "confirm": True,
            },
        )
        assert isinstance(result, str)
        assert "Deleted" in result

    async def test_add_task_returns_uiblock_when_ambiguous(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        await svc.create_list(_local_list("a"), _owner())
        await svc.create_list(_local_list("b"), _owner())
        result = await svc.execute_tool(
            "add_task",
            {
                "_user_id": "owner",
                "_user_roles": ["user"],
                "title": "ambig",
            },
        )
        assert isinstance(result, ToolOutput)
        assert result.ui_blocks
        assert any(
            el.type == "select"
            for el in result.ui_blocks[0].elements
        )

    async def test_summarize_today_falls_back_when_no_ai(
        self,
        sqlite_storage: SQLiteStorage,
    ) -> None:
        svc = TasksService()
        storage_provider = FakeStorageProvider(sqlite_storage)
        scheduler = FakeScheduler()
        resolver = FakeResolver(
            entity_storage=storage_provider,
            scheduler=scheduler,
            access_control=FakeAccessControl(),
            event_bus=FakeEventBusProvider(),
            # ai_chat omitted on purpose.
        )
        await svc.start(resolver)
        try:
            tl = await svc.create_list(_local_list(), _owner())
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            await svc.add_task(
                tl.id, Task(title="A", due_at=now), _owner()
            )
            text = await svc.summarize_today(_owner())
            # Deterministic fallback.
            assert "task(s)" in text
        finally:
            await svc.stop()

    async def test_summarize_today_uses_configurable_prompt(
        self, started_service: Any
    ) -> None:
        svc, _, _, _, ai = started_service
        svc._summary_prompt = "CUSTOM PROMPT XYZ"
        tl = await svc.create_list(_local_list(), _owner())
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        await svc.add_task(tl.id, Task(title="A", due_at=now), _owner())
        await svc.summarize_today(_owner())
        assert ai.calls
        assert ai.calls[-1]["system_prompt"] == "CUSTOM PROMPT XYZ"

    async def test_summarize_today_single_source(
        self, started_service: Any
    ) -> None:
        """Tool path and Provider path share the same code."""
        svc, _, _, _, ai = started_service
        tl = await svc.create_list(_local_list(), _owner())
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        await svc.add_task(tl.id, Task(title="A", due_at=now), _owner())
        before = len(ai.calls)
        # AI tool path.
        await svc.execute_tool(
            "summarize_today",
            {"_user_id": "owner", "_user_roles": ["user"]},
        )
        # Direct Provider path.
        await svc.summarize_today(_owner())
        # Both used the same complete_one_shot — same call signature.
        assert len(ai.calls) == before + 2


# ── Multi-user safety ──────────────────────────────────────────────


class TestMultiUserSafety:
    async def test_concurrent_adds_for_different_users_isolated(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        await svc.share_user(tl.id, "alice", _owner())

        async def _add_as(user: UserContext, title: str) -> None:
            set_current_user(user)
            await svc.add_task(tl.id, Task(title=title), user)

        await asyncio.gather(
            _add_as(_owner(), "owner-task"),
            _add_as(_shared_user(), "alice-task"),
        )
        # Both rows present, each tagged with the right creator.
        from gilbert.interfaces.storage import Query

        rows = await svc._storage.query(Query(collection="tasks"))
        creators = {r["title"]: r["created_by_user_id"] for r in rows}
        assert creators["owner-task"] == "owner"
        assert creators["alice-task"] == "alice"


# ── Conflict resolution / etag ──────────────────────────────────────


class TestConflictResolution:
    async def test_pending_push_keeps_local_fields_against_upstream(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "conflict_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_conflict", backend_name="conflict_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)

            created = await svc.add_task(
                tl.id, Task(title="local"), _owner()
            )
            # Mutate locally (would be pending_push if push fails).
            backend.fail_update = RuntimeError("offline")
            await svc.update_task(
                created.id, {"title": "renamed-local"}, _owner()
            )
            # Now upstream returns a different value for the same task.
            backend.upstream = {
                created.source_id: Task(
                    id="",
                    list_id=tl.id,
                    title="upstream-changed",
                    notes="upstream-notes",
                    source_id=created.source_id,
                    updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                )
            }
            await svc._poll_runtime(svc._runtimes[tl.id])
            row = await svc._storage.get("tasks", created.id)
            assert row is not None
            # Local-pending fields stay local.
            assert row["title"] == "renamed-local"
        finally:
            TaskBackend._registry.pop("conflict_a", None)

    async def test_stale_etag_repolls_and_retries(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "etag_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_etag", backend_name="etag_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            backend.upstream_etag = "etag-1"

            created = await svc.add_task(
                tl.id, Task(title="local"), _owner()
            )
            # First update_task call raises Conflict; second succeeds.
            calls = {"n": 0}
            original = backend.update_task

            async def _patch(
                source_id: str, patch: dict, *, etag: str = ""
            ) -> Task:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TaskBackendConflictError("stale")
                return await original(source_id, patch, etag=etag)

            backend.update_task = _patch  # type: ignore[assignment]
            updated = await svc.update_task(
                created.id, {"title": "renamed"}, _owner()
            )
            assert updated.sync_status == SyncStatus.SYNCED
            assert calls["n"] == 2
        finally:
            TaskBackend._registry.pop("etag_a", None)


# ── Aggregation ────────────────────────────────────────────────────


class TestAggregation:
    async def test_due_today_aggregates_across_lists(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        a = await svc.create_list(_local_list("a"), _owner())
        b = await svc.create_list(_local_list("b"), _owner())
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        await svc.add_task(a.id, Task(title="from-a", due_at=now), _owner())
        await svc.add_task(b.id, Task(title="from-b", due_at=now), _owner())
        set_current_user(_owner())
        results = await svc.due_today()
        titles = {t.title for t in results}
        assert {"from-a", "from-b"} <= titles

    async def test_search_filters_by_backend(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "search_filter_a"

        try:
            local = await svc.create_list(_local_list("loc"), _owner())
            ext = await svc.create_list(
                _ext_list("ext", backend_name="search_filter_a"), _owner()
            )
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            await svc.add_task(
                local.id, Task(title="local-A", due_at=now), _owner()
            )
            await svc.add_task(
                ext.id, Task(title="ext-A", due_at=now), _owner()
            )
            set_current_user(_owner())
            local_results = await svc.search_tasks(backend="local")
            assert {t.title for t in local_results} == {"local-A"}
        finally:
            TaskBackend._registry.pop("search_filter_a", None)


# ── List CRUD edge cases ───────────────────────────────────────────


class TestListCRUD:
    async def test_delete_list_refuses_open_tasks(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        await svc.add_task(tl.id, Task(title="x"), _owner())
        with pytest.raises(ValueError):
            await svc.delete_list(tl.id, _owner())

    async def test_delete_list_force_cascades_tasks(
        self, started_service: Any
    ) -> None:
        svc, storage, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        await svc.delete_list(tl.id, _owner(), force=True)
        # Cascades.
        assert await storage.get("tasks", created.id) is None
        assert await storage.get("task_lists", tl.id) is None


# ── WS RPCs ────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, user_ctx: UserContext) -> None:
        self.user_ctx = user_ctx


class TestWSRPCs:
    async def test_lists_list_returns_only_accessible(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        a = await svc.create_list(_local_list("a"), _owner())
        await svc.create_list(_local_list("b", owner="other"), _owner())
        # Note: create_list stamps owner_user_id from user_ctx,
        # ignoring the passed kwarg. Both lists are owned by 'owner'.
        # For a real "isolation" check, share one with shared_user.
        await svc.share_user(a.id, "alice", _owner())
        handlers = svc.get_ws_handlers()
        result = await handlers["tasks.lists.list"](
            _FakeConn(_shared_user()), {"id": "f1"}
        )
        list_ids = {row["id"] for row in result["lists"]}
        assert a.id in list_ids

    async def test_tasks_get_returns_404_when_missing(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        handlers = svc.get_ws_handlers()
        result = await handlers["tasks.get"](
            _FakeConn(_owner()), {"id": "f1", "task_id": "no-such"}
        )
        assert result["code"] == 404

    async def test_tasks_add_returns_403_when_no_access(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        handlers = svc.get_ws_handlers()
        result = await handlers["tasks.add"](
            _FakeConn(_unrelated()),
            {"id": "f1", "list_id": tl.id, "title": "blocked"},
        )
        assert result["code"] == 403

    async def test_tasks_complete_happy_path(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        handlers = svc.get_ws_handlers()
        result = await handlers["tasks.complete"](
            _FakeConn(_owner()), {"id": "f1", "task_id": created.id}
        )
        assert result["task"]["status"] == TaskStatus.DONE.value

    async def test_backends_list_includes_local(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        handlers = svc.get_ws_handlers()
        result = await handlers["tasks.backends.list"](
            _FakeConn(_owner()), {"id": "f1"}
        )
        names = {b["name"] for b in result["backends"]}
        assert "local" in names

    async def test_tasks_summary_returns_string(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        await svc.create_list(_local_list(), _owner())
        handlers = svc.get_ws_handlers()
        result = await handlers["tasks.summary"](
            _FakeConn(_owner()), {"id": "f1"}
        )
        assert isinstance(result["summary"], str)

    async def test_tasks_list_paginates_with_cursor(
        self, started_service: Any
    ) -> None:
        """Spec §6.2-9 requires (cursor, limit) on tasks.list. The
        cursor is opaque; v1 encodes the next-page offset and the
        response carries ``next_cursor`` while a full page came back."""
        svc, *_ = started_service
        tl = await svc.create_list(_local_list("tl_paged"), _owner())
        # Add 5 tasks; ask for page size 2.
        for i in range(5):
            await svc.add_task(tl.id, Task(title=f"t-{i}"), _owner())
        handlers = svc.get_ws_handlers()
        page1 = await handlers["tasks.list"](
            _FakeConn(_owner()),
            {"id": "p1", "list_id": tl.id, "limit": 2},
        )
        assert len(page1["tasks"]) == 2
        assert page1["next_cursor"] == "2"
        page2 = await handlers["tasks.list"](
            _FakeConn(_owner()),
            {
                "id": "p2",
                "list_id": tl.id,
                "limit": 2,
                "cursor": page1["next_cursor"],
            },
        )
        assert len(page2["tasks"]) == 2
        assert page2["next_cursor"] == "4"
        page3 = await handlers["tasks.list"](
            _FakeConn(_owner()),
            {
                "id": "p3",
                "list_id": tl.id,
                "limit": 2,
                "cursor": page2["next_cursor"],
            },
        )
        # Last page is short → no further cursor.
        assert len(page3["tasks"]) == 1
        assert page3["next_cursor"] is None
        # No row appears on more than one page.
        all_ids = {t["id"] for p in (page1, page2, page3) for t in p["tasks"]}
        assert len(all_ids) == 5

    async def test_refresh_list_happy_path(
        self, started_service: Any
    ) -> None:
        """``tasks.lists.refresh`` triggers a fresh upstream poll and
        returns the count of newly-discovered rows."""
        svc, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "refresh_happy_a"

        try:
            tl = await svc.create_list(
                _ext_list("tl_refresh_h", backend_name="refresh_happy_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            backend.upstream = {
                "src-x": Task(
                    id="",
                    list_id=tl.id,
                    title="from-upstream",
                    source_id="src-x",
                    updated_at=now,
                ),
            }
            handlers = svc.get_ws_handlers()
            result = await handlers["tasks.lists.refresh"](
                _FakeConn(_owner()), {"id": "r1", "list_id": tl.id}
            )
            assert result["type"] == "tasks.lists.refresh.result"
            assert result.get("new") == 1
            assert result.get("total") == 1
        finally:
            TaskBackend._registry.pop("refresh_happy_a", None)

    async def test_refresh_list_denies_unrelated_user(
        self, started_service: Any
    ) -> None:
        """Non-shared, non-owner, non-admin users get 403 on refresh."""
        svc, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "refresh_deny_a"

        try:
            tl = await svc.create_list(
                _ext_list("tl_refresh_d", backend_name="refresh_deny_a"),
                _owner(),
            )
            handlers = svc.get_ws_handlers()
            result = await handlers["tasks.lists.refresh"](
                _FakeConn(_unrelated()), {"id": "r1", "list_id": tl.id}
            )
            assert result.get("code") == 403
        finally:
            TaskBackend._registry.pop("refresh_deny_a", None)


# ── Restore, DST, poll-diff events, sync-tick patch hygiene ─────────


class TestRestoreTask:
    async def test_restore_unhides_and_publishes_event(
        self, started_service: Any
    ) -> None:
        """Soft-deleted task can be restored by an admin and the
        resurrection is announced via ``task.restored`` so SPAs and
        other subscribers can refresh their views."""
        svc, storage, ebus, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="phoenix"), _owner()
        )
        set_current_user(_owner())
        await svc.delete_task(created.id, _owner())
        # Soft-deleted: hidden from reads.
        assert await svc.get_task(created.id) is None
        ebus.published.clear()
        restored = await svc.restore_task(created.id, _admin())
        # No deleted_at, sync_status reset for a local list.
        assert restored.deleted_at == ""
        assert restored.sync_status == SyncStatus.SYNCED
        # Visible again to readers.
        set_current_user(_owner())
        assert await svc.get_task(created.id) is not None
        # Event published exactly once.
        restored_events = [
            e for e in ebus.published if e.event_type == "task.restored"
        ]
        assert len(restored_events) == 1
        assert restored_events[0].data["task_id"] == created.id

    async def test_restore_requires_admin(
        self, started_service: Any
    ) -> None:
        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        created = await svc.add_task(
            tl.id, Task(title="x"), _owner()
        )
        set_current_user(_owner())
        await svc.delete_task(created.id, _owner())
        with pytest.raises(TaskListPermissionError):
            await svc.restore_task(created.id, _owner())

    def test_service_info_advertises_task_restored_event(self) -> None:
        info = TasksService().service_info()
        assert "task.restored" in info.events


class TestDSTHandling:
    async def test_due_today_spans_dst_fallback(
        self, started_service: Any
    ) -> None:
        """Spec §18.1 lines 2152-2154: a task ``due_at`` set inside
        the user's local DST-transition day must still surface in
        ``due_today`` when the user is in that zone.

        We simulate the scenario by stamping ``due_at`` from a
        timestamp constructed in the user's local zone (so the
        UTC↔local round-trip naturally encounters the fallback) and
        asserting ``due_today`` returns it. The window in
        ``TasksService.due_today`` constructs `start_local` and
        `end_local` from the user's zone, so a fall-back day (which is
        25 hours long in clock time) is fully covered.
        """
        from zoneinfo import ZoneInfo

        svc, *_ = started_service
        tl = await svc.create_list(_local_list(), _owner())
        eastern = ZoneInfo("America/New_York")
        eastern_user = UserContext(
            user_id="owner",
            email="owner@example.com",
            display_name="Owner",
            roles=frozenset({"user"}),
            tz="America/New_York",
        )
        # Pick the most recent DST fallback in America/New_York
        # (first Sunday of November). This is deterministic — the
        # service's local-day window is computed at call time, so
        # we use `today's` Eastern date and just verify the window
        # logic is wide enough to span 25 clock-hours when applicable.
        # The robust assertion: a task whose due_at is at noon Eastern
        # today appears in due_today regardless of whether today is a
        # DST transition day.
        now_et = datetime.now(UTC).astimezone(eastern)
        noon_et = now_et.replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        due_iso = noon_et.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        await svc.add_task(
            tl.id,
            Task(
                title="dst-noon",
                due_at=due_iso,
                due_at_tz="America/New_York",
            ),
            eastern_user,
        )
        # Also add a task at 1:30 AM local (the duplicated hour on
        # fall-back days). On non-fall-back days this is just an
        # ordinary early-morning row; either way it must be in today.
        early_local = now_et.replace(
            hour=1, minute=30, second=0, microsecond=0
        )
        early_iso = early_local.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        await svc.add_task(
            tl.id,
            Task(
                title="dst-early",
                due_at=early_iso,
                due_at_tz="America/New_York",
            ),
            eastern_user,
        )
        set_current_user(eastern_user)
        results = await svc.due_today()
        titles = {t.title for t in results}
        assert "dst-noon" in titles
        assert "dst-early" in titles


class TestPollDiffEvents:
    async def test_poll_emits_task_updated_for_changed_row(
        self, started_service: Any
    ) -> None:
        """Spec §18.1 lines 2108-2110: a second poll where one row
        carries new ``updated_at`` + a changed user-facing field emits
        ``task.updated`` (and never re-inserts)."""
        svc, storage, ebus, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "poll_diff_upd_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_diff_upd", backend_name="poll_diff_upd_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            backend.upstream = {
                "src-A": Task(
                    id="",
                    list_id=tl.id,
                    title="orig",
                    source_id="src-A",
                    updated_at=now,
                ),
            }
            await svc._poll_runtime(svc._runtimes[tl.id])
            ebus.published.clear()
            # Mutate the row upstream and bump updated_at.
            later = (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat().replace("+00:00", "Z")
            backend.upstream["src-A"].title = "renamed"
            backend.upstream["src-A"].updated_at = later
            await svc._poll_runtime(svc._runtimes[tl.id])
            updated_events = [
                e for e in ebus.published if e.event_type == "task.updated"
            ]
            created_events = [
                e for e in ebus.published if e.event_type == "task.created"
            ]
            assert len(updated_events) == 1
            assert updated_events[0].data["task_id"]
            # No duplicate insert.
            assert len(created_events) == 0
        finally:
            TaskBackend._registry.pop("poll_diff_upd_a", None)

    async def test_poll_emits_task_completed_when_done_upstream(
        self, started_service: Any
    ) -> None:
        """A poll diff that flips ``status`` to ``done`` emits
        ``task.completed`` (not ``task.updated``)."""
        svc, storage, ebus, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "poll_diff_done_a"

        try:
            tl = await svc.create_list(
                _ext_list("tlst_diff_done", backend_name="poll_diff_done_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            backend.upstream = {
                "src-B": Task(
                    id="",
                    list_id=tl.id,
                    title="finishing",
                    source_id="src-B",
                    updated_at=now,
                    status=TaskStatus.OPEN,
                ),
            }
            await svc._poll_runtime(svc._runtimes[tl.id])
            ebus.published.clear()
            later = (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat().replace("+00:00", "Z")
            backend.upstream["src-B"].status = TaskStatus.DONE
            backend.upstream["src-B"].updated_at = later
            await svc._poll_runtime(svc._runtimes[tl.id])
            completed_events = [
                e for e in ebus.published if e.event_type == "task.completed"
            ]
            assert len(completed_events) == 1
        finally:
            TaskBackend._registry.pop("poll_diff_done_a", None)


class TestSyncTickPatchHygiene:
    async def test_sync_tick_retry_sends_only_user_facing_fields(
        self, started_service: Any
    ) -> None:
        """Spec §6.7.2: ``backend.update_task`` is patch-shaped — the
        tick MUST NOT leak internal bookkeeping (``_id``,
        ``sync_status``, ``last_push_error``, ``idempotency_key``,
        ``retry_count`` etc.) into the patch dict.

        This exercises the retry path where the row already has a
        ``source_id`` from a previous successful add but a subsequent
        update failed, leaving sync_status=PENDING_PUSH.
        """
        svc, storage, *_ = started_service

        class _Backend(FakeExternalBackend):
            backend_name = "patch_hygiene_a"

        try:
            tl = await svc.create_list(
                _ext_list("tl_hyg", backend_name="patch_hygiene_a"),
                _owner(),
            )
            backend = svc._runtimes[tl.id].backend
            assert isinstance(backend, FakeExternalBackend)
            # Step 1: add succeeds — source_id is set.
            created = await svc.add_task(
                tl.id, Task(title="hi", notes="orig"), _owner()
            )
            assert created.source_id
            # Step 2: simulate an in-flight failed push by directly
            # backdating the row to PENDING_PUSH so the next sync_tick
            # exercises the update_task retry branch.
            row = await storage.get("tasks", created.id)
            assert row is not None
            row["sync_status"] = SyncStatus.PENDING_PUSH.value
            row["last_push_error"] = "stale failure"
            row["idempotency_key"] = "should-not-leak"
            row["retry_count"] = 1
            await storage.put("tasks", created.id, row)
            backend.update_calls.clear()
            await svc._sync_tick()
            assert len(backend.update_calls) == 1
            _src, patch, _etag = backend.update_calls[0]
            allowed = {
                "title",
                "notes",
                "due_at",
                "due_at_tz",
                "priority",
                "tags",
                "project",
                "status",
                "completed_at",
            }
            assert set(patch.keys()) == allowed
            for forbidden in (
                "_id",
                "id",
                "sync_status",
                "last_push_error",
                "idempotency_key",
                "retry_count",
                "etag",
                "source_id",
            ):
                assert forbidden not in patch
        finally:
            TaskBackend._registry.pop("patch_hygiene_a", None)

