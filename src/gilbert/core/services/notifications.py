"""NotificationService — persists user-addressed notifications and publishes
``notification.received`` bus events.

Live WebSocket delivery uses the existing event-bus dispatch
(``WsConnectionManager._dispatch_event``) with a per-user content filter
in ``WsConnection.can_see_notification_event``. No separate push API.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.notifications import (
    Notification,
    NotificationUrgency,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)

logger = logging.getLogger(__name__)


_COLLECTION = "notifications"
_NOTIFICATION_RECEIVED_EVENT = "notification.received"


class NotificationService(Service):
    """Persists notifications and publishes ``notification.received``.

    Capabilities declared:

    - ``notifications`` — satisfies ``NotificationProvider``.
    - ``ws_handlers`` — exposes RPCs for list / mark_read / mark_all_read
      / delete.
    """

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None  # EventBus instance from EventBusProvider

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="notifications",
            capabilities=frozenset({"notifications", "ws_handlers"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError("entity_storage capability does not implement StorageProvider")
        self._storage = storage_svc.backend

        event_bus_svc = resolver.require_capability("event_bus")
        if not isinstance(event_bus_svc, EventBusProvider):
            raise RuntimeError("event_bus capability does not implement EventBusProvider")
        self._event_bus = event_bus_svc.bus

        await self._storage.ensure_index(
            IndexDefinition(
                collection=_COLLECTION,
                fields=["user_id", "read", "created_at"],
            )
        )
        logger.info("NotificationService started")

    async def stop(self) -> None:
        return None

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        """Persist a notification and publish ``notification.received``.

        The bus event delivers the notification to live WebSocket
        connections of the target user via the per-event filter in
        ``WsConnection.can_see_notification_event``.
        """
        if self._storage is None or self._event_bus is None:
            raise RuntimeError("NotificationService.start() not called")

        notification = Notification(
            id=str(uuid.uuid4()),
            user_id=user_id,
            source=source,
            message=message,
            urgency=urgency,
            created_at=datetime.now(UTC),
            source_ref=source_ref,
        )
        await self._storage.put(_COLLECTION, notification.id, _serialize(notification))
        await self._event_bus.publish(
            Event(
                event_type=_NOTIFICATION_RECEIVED_EVENT,
                data=_serialize(notification),
                source="notifications",
                timestamp=notification.created_at,
            )
        )
        return notification


def _serialize(n: Notification) -> dict[str, Any]:
    """Convert a Notification to its persisted/wire dict form."""
    return {
        "id": n.id,
        "user_id": n.user_id,
        "source": n.source,
        "message": n.message,
        "urgency": n.urgency.value,
        "created_at": n.created_at.isoformat(),
        "read": n.read,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "source_ref": n.source_ref,
    }


def _deserialize(d: dict[str, Any]) -> Notification:
    """Reverse of ``_serialize``."""
    read_at_raw = d.get("read_at")
    return Notification(
        id=d["id"],
        user_id=d["user_id"],
        source=d.get("source", "system"),
        message=d["message"],
        urgency=NotificationUrgency(d.get("urgency", "normal")),
        created_at=datetime.fromisoformat(d["created_at"]),
        read=bool(d.get("read", False)),
        read_at=datetime.fromisoformat(read_at_raw) if read_at_raw else None,
        source_ref=d.get("source_ref"),
    )
