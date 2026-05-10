"""Local task backend — vendor-free, persists directly to entity storage.

The local backend's "upstream" *is* core's entity store. Reads come
from the same ``tasks`` collection the service uses for cross-backend
aggregation; writes are no-op confirmations that just stamp
``source_id = task.id`` so the schema stays uniform.

Side-effect imported by ``core/services/tasks.py`` so the backend
registers itself in ``TaskBackend._registry`` without needing a plugin.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.storage import Filter, FilterOp, Query, StorageBackend
from gilbert.interfaces.tasks import (
    StorageAwareTaskBackend,  # noqa: F401  (referenced via isinstance from the service)
    Task,
    TaskBackend,
    TaskStatus,
)

logger = logging.getLogger(__name__)


class LocalTaskBackend(TaskBackend):
    """Task backend backed entirely by entity storage. Vendor-free.

    Acts as both source of truth and read cache — ``list_tasks`` returns
    the storage rows for this list directly. ``add_task`` is a
    confirm-only no-op (the service writes the row). The runtime never
    schedules a poll job for local lists, but ``list_tasks`` is still
    implemented so the explicit ``refresh_list`` RPC can drive a manual
    refresh and so the ABC surface is satisfied.

    Satisfies ``StorageAwareTaskBackend``. External backends do NOT —
    this hook is a local-only concern. Don't copy this pattern into a
    third-party backend.
    """

    backend_name = "local"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        # No credentials — purely local. The service injects ``list_id``
        # via ``initialize`` so the backend can scope its queries.
        return []

    def __init__(self) -> None:
        self._list_id: str = ""
        self._storage: StorageBackend | None = None

    def set_storage(self, storage: object) -> None:
        """Receive entity storage. Satisfies ``StorageAwareTaskBackend``.

        Service-only hook — ``TasksService`` calls this immediately after
        instantiation, BEFORE ``initialize()``. External backends don't
        satisfy ``StorageAwareTaskBackend`` and never see this call.
        """
        # Narrow at the boundary; the protocol keeps the interface
        # decoupled from a specific storage type.
        self._storage = cast("StorageBackend", storage)

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._list_id = str(cfg.get("list_id", ""))
        if self._storage is None:
            logger.warning(
                "LocalTaskBackend initialized without storage (list_id=%s)",
                self._list_id,
            )

    async def close(self) -> None:
        # Nothing to release; the storage handle is owned by the service.
        return None

    async def list_tasks(
        self,
        *,
        include_completed: bool = False,
        updated_since: str = "",
    ) -> list[Task]:
        if self._storage is None or not self._list_id:
            return []
        filters = [
            Filter(field="list_id", op=FilterOp.EQ, value=self._list_id),
            Filter(field="deleted_at", op=FilterOp.EQ, value=""),
        ]
        if not include_completed:
            filters.append(
                Filter(field="status", op=FilterOp.EQ, value=TaskStatus.OPEN.value)
            )
        if updated_since:
            filters.append(
                Filter(field="updated_at", op=FilterOp.GTE, value=updated_since)
            )
        rows = await self._storage.query(Query(collection="tasks", filters=filters))
        return [Task.from_dict(row) for row in rows]

    async def add_task(self, task: Task) -> Task:
        # Service writes the row — backend confirms / normalizes only.
        # Stamp source_id = id for parity with external backends.
        if not task.source_id:
            task.source_id = task.id
        return task

    async def update_task(
        self,
        source_id: str,
        patch: dict[str, Any],
        *,
        etag: str = "",
    ) -> Task:
        # Service handles persistence; backend has nothing to push.
        # Return value carries the patch as a Task with source_id stamped —
        # the service merges this back into the row.
        result = Task(id=source_id, source_id=source_id, list_id=self._list_id)
        # Apply only the fields the patch provided so the service can
        # treat the return as a delta envelope.
        if "title" in patch:
            result.title = str(patch.get("title", ""))
        if "notes" in patch:
            result.notes = str(patch.get("notes", ""))
        if "due_at" in patch:
            result.due_at = str(patch.get("due_at", ""))
        if "due_at_tz" in patch:
            result.due_at_tz = str(patch.get("due_at_tz", ""))
        if "priority" in patch:
            try:
                from gilbert.interfaces.tasks import TaskPriority

                result.priority = TaskPriority(int(patch.get("priority", 0) or 0))
            except (ValueError, TypeError):
                pass
        if "tags" in patch:
            tags_raw = patch.get("tags") or []
            if isinstance(tags_raw, list):
                result.tags = [str(t) for t in tags_raw]
        if "project" in patch:
            result.project = str(patch.get("project", ""))
        return result

    async def complete_task(self, source_id: str) -> None:
        # Service-level path persists; local backend has no upstream.
        return None

    async def delete_task(self, source_id: str) -> None:
        # Service-level path soft-deletes / hard-deletes locally.
        return None

