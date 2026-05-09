"""Calendar interfaces — backend ABC, capability protocols, dataclasses, auth helpers.

Shared by the core ``CalendarService``, the web layer, and plugins that
provide calendar backends. Imports only from other ``interfaces``
modules — never from ``core/``, ``integrations/``, ``web/``, or
``storage/``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam

# ── Enums ─────────────────────────────────────────────────────────────


class AttendeeResponseStatus(StrEnum):
    """RSVP state for one attendee on a calendar event."""

    NEEDS_ACTION = "needsAction"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    TENTATIVE = "tentative"


class EventVisibility(StrEnum):
    """Per-event visibility — defaults to inheriting the calendar setting."""

    DEFAULT = "default"
    PUBLIC = "public"
    PRIVATE = "private"


class EventStatus(StrEnum):
    """Lifecycle status of a calendar event."""

    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


class CalendarAccess(StrEnum):
    """How a user has access to a CalendarAccount — used for UI grouping."""

    OWNER = "owner"
    ADMIN = "admin"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class CalendarAttendee:
    """A single attendee on a calendar event."""

    email: str
    name: str = ""
    response_status: AttendeeResponseStatus = AttendeeResponseStatus.NEEDS_ACTION
    is_organizer: bool = False
    is_self: bool = False  # True if this attendee == the account's email

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "response_status": self.response_status.value,
            "is_organizer": self.is_organizer,
            "is_self": self.is_self,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalendarAttendee:
        try:
            status = AttendeeResponseStatus(
                data.get("response_status") or AttendeeResponseStatus.NEEDS_ACTION.value
            )
        except ValueError:
            status = AttendeeResponseStatus.NEEDS_ACTION
        return cls(
            email=str(data.get("email", "")),
            name=str(data.get("name", "")),
            response_status=status,
            is_organizer=bool(data.get("is_organizer", False)),
            is_self=bool(data.get("is_self", False)),
        )


@dataclass(frozen=True)
class CalendarEvent:
    """A single (possibly-recurring-instance) calendar event.

    ``start`` and ``end`` are timezone-aware datetimes. ``all_day`` is
    True for events that span whole days (start/end are 00:00 local on
    consecutive dates and the backend reported only ``date``, not
    ``dateTime``). ``recurring_event_id`` is set on instances of a
    recurring series — None for one-off events.
    """

    event_id: str
    calendar_id: str
    account_id: str
    title: str
    start: datetime
    end: datetime
    etag: str = ""
    all_day: bool = False
    description: str = ""
    location: str = ""
    organizer_email: str = ""
    attendees: tuple[CalendarAttendee, ...] = ()
    visibility: EventVisibility = EventVisibility.DEFAULT
    status: EventStatus = EventStatus.CONFIRMED
    transparency: str = "opaque"
    html_link: str = ""
    recurring_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "calendar_id": self.calendar_id,
            "account_id": self.account_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "etag": self.etag,
            "all_day": self.all_day,
            "description": self.description,
            "location": self.location,
            "organizer_email": self.organizer_email,
            "attendees": [a.to_dict() for a in self.attendees],
            "visibility": self.visibility.value,
            "status": self.status.value,
            "transparency": self.transparency,
            "html_link": self.html_link,
            "recurring_event_id": self.recurring_event_id,
        }


@dataclass(frozen=True)
class FreeBusyBlock:
    """A single busy interval returned by the backend's free/busy query."""

    calendar_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class FreeSlot:
    """A free interval computed by ``find_free_time``.

    ``slot_duration_minutes`` is ``(end - start).total_seconds() // 60``;
    ``requested_duration_minutes`` is the caller's requested minimum
    duration that produced the slot (slot may be longer than requested).
    """

    start: datetime
    end: datetime
    slot_duration_minutes: int
    requested_duration_minutes: int


@dataclass
class EventCreateRequest:
    """Inputs for creating or updating a calendar event.

    The vendor-neutral payload — every field maps to a column on every
    target backend. ``send_invites`` defaults to ``False`` so AI-driven
    and programmatic callers must opt in to firing real invites; the
    SPA create-event drawer flips this to ``True`` for human-confirmed
    flows.
    """

    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    attendees: list[CalendarAttendee] = field(default_factory=list)
    all_day: bool = False
    visibility: EventVisibility = EventVisibility.DEFAULT
    send_invites: bool = False
    idempotency_key: str = ""


@dataclass
class CalendarAccount:
    """A configured calendar account (stored in ``calendar_accounts``)."""

    id: str
    name: str
    email_address: str
    backend_name: str
    backend_config: dict[str, Any] = field(default_factory=dict)
    calendar_id: str = "primary"
    timezone: str = "UTC"
    working_hours_start_hour: int = 9
    working_hours_end_hour: int = 18
    owner_user_id: str = ""
    shared_with_users: list[str] = field(default_factory=list)
    shared_with_roles: list[str] = field(default_factory=list)
    poll_enabled: bool = True
    poll_interval_sec: int = 300
    upcoming_event_lookahead_minutes: int = 15
    health: str = "ok"
    last_error: str = ""
    last_error_at: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email_address": self.email_address,
            "backend_name": self.backend_name,
            "backend_config": dict(self.backend_config),
            "calendar_id": self.calendar_id,
            "timezone": self.timezone,
            "working_hours_start_hour": self.working_hours_start_hour,
            "working_hours_end_hour": self.working_hours_end_hour,
            "owner_user_id": self.owner_user_id,
            "shared_with_users": list(self.shared_with_users),
            "shared_with_roles": list(self.shared_with_roles),
            "poll_enabled": self.poll_enabled,
            "poll_interval_sec": self.poll_interval_sec,
            "upcoming_event_lookahead_minutes": self.upcoming_event_lookahead_minutes,
            "health": self.health,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalendarAccount:
        raw_backend_config = data.get("backend_config") or {}
        raw_shared_users = data.get("shared_with_users") or []
        raw_shared_roles = data.get("shared_with_roles") or []
        raw_poll = data.get("poll_interval_sec", 300) or 300
        raw_lookahead = data.get("upcoming_event_lookahead_minutes", 15) or 15
        raw_wh_start = data.get("working_hours_start_hour", 9)
        raw_wh_end = data.get("working_hours_end_hour", 18)
        return cls(
            id=str(data.get("id") or data.get("_id") or ""),
            name=str(data.get("name", "")),
            email_address=str(data.get("email_address", "")),
            backend_name=str(data.get("backend_name", "")),
            backend_config=cast("dict[str, Any]", raw_backend_config),
            calendar_id=str(data.get("calendar_id", "primary")),
            timezone=str(data.get("timezone", "UTC")),
            working_hours_start_hour=int(cast("int", raw_wh_start)),
            working_hours_end_hour=int(cast("int", raw_wh_end)),
            owner_user_id=str(data.get("owner_user_id", "")),
            shared_with_users=cast("list[str]", raw_shared_users),
            shared_with_roles=cast("list[str]", raw_shared_roles),
            poll_enabled=bool(data.get("poll_enabled", True)),
            poll_interval_sec=int(cast("int", raw_poll)),
            upcoming_event_lookahead_minutes=int(cast("int", raw_lookahead)),
            health=str(data.get("health", "ok")),
            last_error=str(data.get("last_error", "")),
            last_error_at=str(data.get("last_error_at", "")),
            created_at=str(data.get("created_at", "")),
        )


@dataclass(frozen=True)
class AggregatedEvents:
    """Return envelope for aggregate read methods so partial failures
    are visible to callers (especially the AI tool surface) instead of
    being silently swallowed."""

    events: list[CalendarEvent]
    warnings: list[str] = field(default_factory=list)


# ── Authorization helpers ─────────────────────────────────────────────
#
# ``is_admin`` is derived from ``user_ctx`` inside the helpers so callers
# can never pass a stale boolean. SYSTEM and any user with ``"admin"``
# in roles is treated as admin.


def _is_admin_ctx(user_ctx: UserContext) -> bool:
    if user_ctx.user_id == UserContext.SYSTEM.user_id:
        return True
    return "admin" in user_ctx.roles


def can_access_account(
    user_ctx: UserContext,
    account: CalendarAccount,
) -> bool:
    """Can this user read events / create events on this account?

    Admin, owner, any user in ``shared_with_users``, or any user with a
    role in ``shared_with_roles`` has full access — read, write,
    create_event, free/busy. Settings / share edits are gated by
    ``can_admin_account``.
    """
    if _is_admin_ctx(user_ctx):
        return True
    if user_ctx.user_id == account.owner_user_id:
        return True
    if user_ctx.user_id in account.shared_with_users:
        return True
    return bool(user_ctx.roles & set(account.shared_with_roles))


def can_admin_account(
    user_ctx: UserContext,
    account: CalendarAccount,
) -> bool:
    """Can this user edit account settings, change shares, or delete it?

    Only the owner or a system admin. Shared users — even with full
    access — cannot change configuration or reassign sharing.
    """
    if _is_admin_ctx(user_ctx):
        return True
    return user_ctx.user_id == account.owner_user_id


def determine_access(
    user_ctx: UserContext,
    account: CalendarAccount,
) -> CalendarAccess | None:
    """Return how the user has access to this account, or None.

    Precedence: owner > admin > shared_user > shared_role. Owner beats
    admin because owner is the more durable relationship — an admin who
    is also the owner should see "owner" in the UI.
    """
    if user_ctx.user_id == account.owner_user_id:
        return CalendarAccess.OWNER
    if _is_admin_ctx(user_ctx):
        return CalendarAccess.ADMIN
    if user_ctx.user_id in account.shared_with_users:
        return CalendarAccess.SHARED_USER
    if user_ctx.roles & set(account.shared_with_roles):
        return CalendarAccess.SHARED_ROLE
    return None


# ── Error taxonomy ────────────────────────────────────────────────────


class CalendarBackendError(Exception):
    """Base class for all calendar backend errors."""


class CalendarBackendAuthError(CalendarBackendError):
    """OAuth / service-account credentials failed; non-retryable until
    user fixes config. Maps Google 401/403 (when reason is
    ``authError``, ``invalid_grant``, or ``forbidden`` for delegation)."""


class CalendarBackendNotFoundError(CalendarBackendError):
    """Calendar or event id not found. Maps Google 404."""


class CalendarBackendConflictError(CalendarBackendError):
    """Optimistic-concurrency conflict (etag mismatch). Service catches
    and refreshes the cached event; the caller (UI) is expected to
    retry."""


class CalendarBackendRateLimitError(CalendarBackendError):
    """Backend rate limit hit; ``retry_after_sec`` is the suggested
    wait. Service applies exponential backoff with jitter and surfaces
    repeated failures via ``health="unhealthy"``."""

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class CalendarBackendTransientError(CalendarBackendError):
    """5xx or network blips — retry with backoff."""


# ── Backend ABC ───────────────────────────────────────────────────────


class CalendarBackend(ABC):
    """Abstract calendar provider — events, free/busy, mutations.

    Backends register themselves via ``__init_subclass__`` keyed on
    ``backend_name``. Read methods accept timezone-aware datetimes and
    must return timezone-aware datetimes.
    """

    _registry: dict[str, type[CalendarBackend]] = {}
    backend_name: str = ""
    display_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            CalendarBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[CalendarBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        """Authenticate and prepare the backend for use."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""

    @abstractmethod
    async def list_calendars(self) -> list[dict[str, Any]]:
        """Return ``[{id, name, timezone, primary}, ...]`` for the
        account. Used by the settings UI to populate the calendar_id
        dropdown after the user pastes credentials."""

    @abstractmethod
    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        max_results: int = 250,
        single_events: bool = True,
    ) -> list[CalendarEvent]:
        """Return events whose [start, end) overlaps [time_min, time_max).

        ``time_min`` and ``time_max`` MUST be timezone-aware. Returned
        events MUST also have timezone-aware ``start``/``end``.
        ``single_events=True`` asks the backend to expand recurring
        series into individual instances. ``max_results`` is per-backend;
        the service's per-call cap is applied **after** aggregation
        across runtimes.
        """

    @abstractmethod
    async def get_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> CalendarEvent | None:
        """Fetch one event. Returns None if not found."""

    @abstractmethod
    async def free_busy(
        self,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> list[FreeBusyBlock]:
        """Return busy intervals for the given calendars in the given
        window. Backends MUST exclude events with
        ``transparency="transparent"`` and events whose ``status`` is
        ``cancelled``."""

    @abstractmethod
    async def create_event(
        self,
        calendar_id: str,
        request: EventCreateRequest,
    ) -> CalendarEvent:
        """Create an event. If ``request.idempotency_key`` is non-empty,
        the backend MUST forward it (Google: as ``requestId``)."""

    @abstractmethod
    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        request: EventCreateRequest,
        *,
        if_match_etag: str = "",
    ) -> CalendarEvent:
        """Update an event. When ``if_match_etag`` is non-empty, the
        backend MUST send it as an ``If-Match`` header. On etag
        mismatch, raise ``CalendarBackendConflictError`` so the
        service can refresh and let the caller retry."""

    @abstractmethod
    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        send_cancellations: bool = False,
    ) -> None:
        """Delete an event."""

    async def respond_to_event(
        self,
        calendar_id: str,
        event_id: str,
        response: AttendeeResponseStatus,
    ) -> None:
        """Optional — default raises NotImplementedError so backends
        without RSVP can opt out cleanly. No AI tool calls this in v1."""
        raise NotImplementedError(
            f"Backend {self.backend_name!r} does not support responding to events"
        )


# ── Provider protocols ────────────────────────────────────────────────


@runtime_checkable
class CalendarProvider(Protocol):
    """Protocol other services consume via ``resolver.get_capability("calendar")``.

    Every method takes ``user_ctx`` explicitly. AI tool dispatch
    constructs a ``UserContext`` from injected ``_user_id`` /
    ``_user_roles`` arguments and passes it through; the WS RPC layer
    builds it from the connection's authenticated session. Internal
    callers (e.g. the poll loop) pass ``UserContext.SYSTEM``.

    When ``account_id=None``, every read fans out concurrently across
    every account the user can access. Per-runtime failures surface in
    the ``warnings`` list on the result envelope but never fail the
    whole call.
    """

    async def list_accessible_accounts(
        self,
        user_ctx: UserContext,
    ) -> list[CalendarAccount]: ...

    async def get_account(
        self,
        account_id: str,
        user_ctx: UserContext,
    ) -> CalendarAccount | None: ...

    async def list_events(
        self,
        account_id: str | None,
        time_min: datetime,
        time_max: datetime,
        user_ctx: UserContext,
        *,
        max_results: int = 250,
    ) -> AggregatedEvents: ...

    async def next_event(
        self,
        account_id: str | None,
        user_ctx: UserContext,
        *,
        after: datetime | None = None,
        within: timedelta | None = None,
    ) -> CalendarEvent | None: ...

    async def free_busy(
        self,
        account_id: str | None,
        time_min: datetime,
        time_max: datetime,
        user_ctx: UserContext,
    ) -> list[FreeBusyBlock]: ...

    async def find_free_time(
        self,
        account_id: str | None,
        time_min: datetime,
        time_max: datetime,
        duration_minutes: int,
        user_ctx: UserContext,
        *,
        respect_working_hours: bool = True,
        max_results: int = 5,
        attendee_emails: list[str] | None = None,
    ) -> list[FreeSlot]: ...

    async def create_event(
        self,
        account_id: str,
        request: EventCreateRequest,
        user_ctx: UserContext,
    ) -> CalendarEvent: ...

    async def update_event(
        self,
        account_id: str,
        event_id: str,
        request: EventCreateRequest,
        user_ctx: UserContext,
        *,
        if_match_etag: str = "",
    ) -> CalendarEvent: ...

    async def delete_event(
        self,
        account_id: str,
        event_id: str,
        user_ctx: UserContext,
        *,
        send_cancellations: bool = False,
    ) -> None: ...


@runtime_checkable
class CachedCalendarLister(Protocol):
    """Snapshot used by ConfigurationService to populate
    ``calendar_accounts`` dropdowns on settings pages."""

    @property
    def cached_accounts(self) -> list[CalendarAccount]: ...


__all__ = [
    "AttendeeResponseStatus",
    "EventVisibility",
    "EventStatus",
    "CalendarAccess",
    "CalendarAttendee",
    "CalendarEvent",
    "FreeBusyBlock",
    "FreeSlot",
    "EventCreateRequest",
    "CalendarAccount",
    "AggregatedEvents",
    "CalendarBackend",
    "CalendarBackendError",
    "CalendarBackendAuthError",
    "CalendarBackendNotFoundError",
    "CalendarBackendConflictError",
    "CalendarBackendRateLimitError",
    "CalendarBackendTransientError",
    "CalendarProvider",
    "CachedCalendarLister",
    "can_access_account",
    "can_admin_account",
    "determine_access",
]
