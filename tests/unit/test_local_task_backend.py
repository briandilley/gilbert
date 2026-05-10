"""Tests for ``LocalTaskBackend`` against the real SQLite storage backend.

The local backend's "upstream" *is* core's entity store, so its
``list_tasks`` returns rows the service wrote into the ``tasks``
collection. ``add_task`` / ``update_task`` / ``complete_task`` /
``delete_task`` are no-op confirmations — the service handles
persistence end-to-end. These tests exercise the backend directly to
prove the contract.
"""

from __future__ import annotations

import pytest

from gilbert.integrations.local_tasks import LocalTaskBackend
from gilbert.interfaces.tasks import (
    StorageAwareTaskBackend,
    Task,
    TaskBackend,
    TaskPriority,
    TaskStatus,
)
from gilbert.storage.sqlite import SQLiteStorage


@pytest.fixture
async def backend(sqlite_storage: SQLiteStorage) -> LocalTaskBackend:
    b = LocalTaskBackend()
    b.set_storage(sqlite_storage)
    await b.initialize({"list_id": "tlst_test"})
    return b


class TestRegistration:
    def test_backend_registered(self) -> None:
        assert "local" in TaskBackend.registered_backends()
        assert TaskBackend.registered_backends()["local"] is LocalTaskBackend

    def test_backend_satisfies_storage_aware_protocol(self) -> None:
        b = LocalTaskBackend()
        assert isinstance(b, StorageAwareTaskBackend)


class TestListTasks:
    async def test_returns_only_rows_for_this_list(
        self,
        backend: LocalTaskBackend,
        sqlite_storage: SQLiteStorage,
    ) -> None:
        # Two rows: one in this list, one in another list.
        for tid, lid in (("a", "tlst_test"), ("b", "tlst_other")):
            await sqlite_storage.put(
                "tasks",
                tid,
                Task(
                    id=tid,
                    list_id=lid,
                    source_id=tid,
                    title=f"task-{tid}",
                ).to_dict(),
            )
        rows = await backend.list_tasks()
        assert {r.id for r in rows} == {"a"}

    async def test_excludes_completed_by_default(
        self,
        backend: LocalTaskBackend,
        sqlite_storage: SQLiteStorage,
    ) -> None:
        await sqlite_storage.put(
            "tasks",
            "open1",
            Task(
                id="open1",
                list_id="tlst_test",
                source_id="open1",
                title="open",
                status=TaskStatus.OPEN,
            ).to_dict(),
        )
        await sqlite_storage.put(
            "tasks",
            "done1",
            Task(
                id="done1",
                list_id="tlst_test",
                source_id="done1",
                title="done",
                status=TaskStatus.DONE,
            ).to_dict(),
        )
        rows = await backend.list_tasks()
        assert {r.id for r in rows} == {"open1"}
        rows_all = await backend.list_tasks(include_completed=True)
        assert {r.id for r in rows_all} == {"open1", "done1"}

    async def test_excludes_soft_deleted(
        self,
        backend: LocalTaskBackend,
        sqlite_storage: SQLiteStorage,
    ) -> None:
        await sqlite_storage.put(
            "tasks",
            "del1",
            Task(
                id="del1",
                list_id="tlst_test",
                source_id="del1",
                title="del",
                deleted_at="2026-01-01T00:00:00Z",
            ).to_dict(),
        )
        await sqlite_storage.put(
            "tasks",
            "alive1",
            Task(
                id="alive1",
                list_id="tlst_test",
                source_id="alive1",
                title="alive",
            ).to_dict(),
        )
        rows = await backend.list_tasks()
        assert {r.id for r in rows} == {"alive1"}

    async def test_updated_since_filter(
        self,
        backend: LocalTaskBackend,
        sqlite_storage: SQLiteStorage,
    ) -> None:
        for tid, ts in (("a", "2026-01-01T00:00:00Z"), ("b", "2026-06-01T00:00:00Z")):
            await sqlite_storage.put(
                "tasks",
                tid,
                Task(
                    id=tid,
                    list_id="tlst_test",
                    source_id=tid,
                    title=tid,
                    updated_at=ts,
                ).to_dict(),
            )
        recent = await backend.list_tasks(updated_since="2026-03-01T00:00:00Z")
        assert {r.id for r in recent} == {"b"}


class TestPushNoOps:
    async def test_add_task_stamps_source_id(
        self, backend: LocalTaskBackend
    ) -> None:
        task = Task(id="tsk_x", list_id="tlst_test", title="hello")
        result = await backend.add_task(task)
        assert result.source_id == "tsk_x"

    async def test_add_task_preserves_existing_source_id(
        self, backend: LocalTaskBackend
    ) -> None:
        task = Task(
            id="tsk_x",
            list_id="tlst_test",
            title="hello",
            source_id="prefilled",
        )
        result = await backend.add_task(task)
        assert result.source_id == "prefilled"

    async def test_complete_task_is_noop(
        self, backend: LocalTaskBackend
    ) -> None:
        await backend.complete_task("tsk_x")  # MUST NOT raise

    async def test_delete_task_is_noop(
        self, backend: LocalTaskBackend
    ) -> None:
        await backend.delete_task("tsk_x")  # MUST NOT raise

    async def test_update_task_returns_patch_envelope(
        self, backend: LocalTaskBackend
    ) -> None:
        result = await backend.update_task(
            "tsk_x",
            {"title": "new", "priority": int(TaskPriority.HIGH)},
        )
        assert result.source_id == "tsk_x"
        assert result.title == "new"
        assert result.priority == TaskPriority.HIGH


class TestNoStorage:
    async def test_list_tasks_without_storage_returns_empty(self) -> None:
        b = LocalTaskBackend()
        # No set_storage call.
        await b.initialize({"list_id": "tlst_x"})
        assert await b.list_tasks() == []

