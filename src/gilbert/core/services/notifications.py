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
