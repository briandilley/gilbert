"""Notifications interface — capability protocol and shared data types.

A notification is a small record addressed to a specific user, optionally
referencing a source object (e.g. an autonomous-agent goal/run). Services
publish notifications by calling ``NotificationProvider.notify_user``.
Delivery to live WebSocket clients is handled by the existing event-bus
dispatch with a per-user content filter — there is no separate push API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class NotificationUrgency(StrEnum):
    """How loudly a notification should signal its arrival.

    The frontend reads this to decide whether to play a sound, flash the
    title bar, or silently bump the badge. The backend just stamps it on
    the entity and the bus event.
    """

    INFO = "info"
    """Quiet — badge bump only."""

    NORMAL = "normal"
    """Default — badge bump with subtle visual update."""

    URGENT = "urgent"
    """Loud — sound, title-bar flash, animated badge."""


@dataclass
class Notification:
    """A single notification record."""

    id: str
    user_id: str
    """Recipient. Notifications are 1:1; fan-out is the caller's job."""

    source: str
    """Origin tag (e.g. ``"agent"``, ``"scheduler"``, ``"ingest"``).
    Free-form; consumers may switch on it for icon selection."""

    message: str
    urgency: NotificationUrgency
    created_at: datetime
    read: bool = False
    read_at: datetime | None = None
    source_ref: dict[str, Any] | None = None
    """Optional structured pointer back to whatever produced this
    notification — e.g. ``{"goal_id": "...", "run_id": "..."}``. The
    frontend uses it to deep-link from a notification to its source."""


@runtime_checkable
class NotificationProvider(Protocol):
    """Protocol exposed by the notifications service.

    Resolved by other services via
    ``resolver.get_capability("notifications")``. The runtime-checkable
    ``isinstance`` narrowing lets callers avoid importing the concrete
    ``NotificationService`` class.
    """

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        """Persist a notification and publish ``notification.received``."""
        ...
