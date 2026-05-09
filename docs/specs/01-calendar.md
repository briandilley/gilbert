# Feature 01: Calendar Service + Google Calendar Backend

## Summary

Adds a first-class **calendar** capability to Gilbert: a `CalendarBackend` ABC
(events, free/busy, create/update/delete), a `CalendarService` that owns
per-user calendar account configurations and runs one backend instance per
account, and eight AI tools (`list_calendar_accounts`, `get_schedule`,
`next_event`, `get_event`, `find_free_time`, `create_event`, `update_event`,
`delete_event` — the three mutating tools default to a preview/confirm
`UIBlock` flow). The first concrete backend is `GoogleCalendarBackend`,
hosted inside the existing `google` std-plugin so it shares the OAuth
machinery already wired up there for Gmail and Drive.
The architecture mirrors the existing **Inbox** service exactly — accounts
are entities, ownable and shareable, with one `EmailBackend`-style runtime
per account — so the integration is small, predictable, and immediately
usable from the **greeting**, **inbox AI** ("schedule that for next
Tuesday"), and **scheduler** services.

## Motivation

Gilbert currently has no notion of when its users are meeting, traveling,
free, or "head down for the next two hours." That gap blocks several
already-shipping features from getting smarter:

- **Greeting service** announces "good morning, Sarah" but cannot say "you
  have a 10:00 with Brian." Adding calendar context to the morning
  greeting is the first concrete win.
- **Inbox AI** can read mail and reply, but cannot answer "schedule that
  for next Tuesday at 3" or "find an hour next week to do this." Both
  intents need free/busy lookup and event creation.
- **Scheduler service** runs system timers and user alarms in Gilbert's
  internal job loop — it cannot create real calendar events on a user's
  Google Calendar. Routing certain alarms to a real calendar makes them
  visible everywhere the user already lives.
- **Agent service** can plan multi-step tasks but has no time-aware
  context — "what does my afternoon look like?" requires a calendar.

Now is the right time because (a) the Inbox service has already established
the per-user, per-mailbox, per-runtime pattern we want to copy verbatim,
(b) the `google` plugin already owns OAuth credentials, service-account
JSON, and domain-wide delegation, so the marginal cost of a Calendar
backend is low, and (c) several pending features (smart scheduling,
"find me an hour," meeting prep) all gate on the same primitive.

## Scope

### In scope

- New `CalendarBackend` ABC in `interfaces/calendar.py` with the universal
  backend-registry pattern (auto-registration via `__init_subclass__`,
  `backend_config_params()`, lifecycle hooks).
- Shared dataclasses for calendar concepts (`CalendarEvent`,
  `CalendarAttendee`, `FreeBusyBlock`, `EventCreateRequest`, etc.).
- `CalendarAccount` entity model (mirrors `Mailbox`): one account per
  external calendar credential, owned by a user, shareable to users/roles.
- `CalendarService` (core service) that:
  - Manages the `calendar_accounts` collection (CRUD + sharing).
  - Spins up one `CalendarBackend` runtime per `poll_enabled` account.
  - Optionally polls each account on a per-account interval to keep a
    local `calendar_events` cache and emit `calendar.event.upcoming`
    events for greeting / agent consumers (poll cadence is per-account).
  - Exposes the `CalendarProvider` capability protocol so other services
    (greeting, inbox AI, scheduler, agent) can consume it without coupling
    to the concrete class.
  - Exposes 8 AI tools: `list_calendar_accounts`, `get_schedule`,
    `next_event`, `get_event`, `find_free_time`, `create_event`,
    `update_event`, `delete_event` (the three mutating tools default
    to preview/confirm via `UIBlock`).
  - Exposes WS RPCs for the SPA (`calendar.accounts.*`,
    `calendar.events.*`, `calendar.freebusy.get`,
    `calendar.event.create/update/delete`).
- `GoogleCalendarBackend` inside `std-plugins/google/`, reusing the
  existing service-account-JSON + delegated-user pattern that `gmail.py`
  established. Reads from / writes to a single primary calendar per
  account (selectable by ID; defaults to `"primary"`).
- README + memory updates: root README integrations table, std-plugins
  README per-plugin section for `google`, `.claude/memory/MEMORIES.md`
  index entry, and a new `memory-calendar-service.md` capturing the
  design.

### Out of scope (explicit non-goals for this PR)

- **Recurring-event editing semantics.** Read recurring events as
  pre-expanded "instances" via the backend; on
  `update_event`/`delete_event` we pass through the raw event id only —
  splitting "this instance vs. this and all following vs. all
  occurrences" is deferred. The AI tool description warns the model.
- **Outlook / Microsoft 365 / iCal / CalDAV backends.** The ABC is
  designed to admit them; we will not implement them in this feature.
- **Attendee response flow** (RSVP accept/decline). The backend exposes
  `respond_to_event` as an *optional* method (default raises
  `NotImplementedError`), but no AI tool invokes it in this PR. A
  follow-up feature can add it.
- **Two-way sync of Gilbert scheduler timers ↔ Google Calendar.** The
  scheduler service stays internal; the `create_event` tool is the only
  way Gilbert writes to a calendar.
- **Push / webhook subscriptions.** We poll. Google Calendar's watch
  channels can come later.
- **Multi-calendar aggregation per account.** One `CalendarAccount`
  references one calendar id (`primary` or explicit). A user with two
  Google Calendars creates two accounts.
- **Working-hours / OOO awareness in `find_free_time`.** v1 honors a
  global default (9am–6pm local), per-account `working_hours_start_hour`
  / `working_hours_end_hour` config, and treats anything else as busy.
  Per-day exceptions (out-of-office events, vacation) are deferred.
- **AI prompt for greetings that mentions the calendar.** Greeting
  service will gain *injection points* for upcoming-meeting context, but
  the prompt-rewriting happens in a separate follow-up so this PR stays
  focused.

## Architecture

### Layer Decisions

| Module | Layer | Justification |
|---|---|---|
| `src/gilbert/interfaces/calendar.py` | `interfaces/` | Pure ABCs, dataclasses, capability protocol, auth helpers. No imports from `core/`, `integrations/`, `web/`, or `storage/`. |
| `src/gilbert/core/services/calendar.py` | `core/services/` | Singleton `CalendarService`. Imports only from `interfaces/` + `core/context`. |
| `std-plugins/google/google_calendar.py` | plugin | Concrete `GoogleCalendarBackend`. Imports only from `gilbert.interfaces.*` + plugin-internal modules. |
| `std-plugins/google/plugin.py` | plugin | Side-effect import of `google_calendar` to trigger backend registration (mirrors `gmail`, `gdrive_documents`). |
| `frontend/src/components/calendar/*` | core SPA | Chat-side widgets that render upcoming-events / free-busy results from tool calls. Shares the existing settings-page extension slot for backend-specific credential editing. |
| `tests/unit/test_calendar_service.py` | tests | Composition root for tests; allowed to import concrete `CalendarService` and test fakes for the backend. |
| `std-plugins/google/tests/test_google_calendar.py` | tests | Tests `GoogleCalendarBackend` against a mocked Google Calendar API client. |

The calendar service is **always registered** (not toggleable at the
service-registry level), but accounts are opt-in: zero accounts → service
runs but does nothing. This matches `InboxService`.

### New / Modified Interfaces

#### `src/gilbert/interfaces/calendar.py` (new)

##### Dataclasses

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext


class AttendeeResponseStatus(StrEnum):
    NEEDS_ACTION = "needsAction"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    TENTATIVE = "tentative"


@dataclass(frozen=True)
class CalendarAttendee:
    """A single attendee on a calendar event."""

    email: str
    name: str = ""
    response_status: AttendeeResponseStatus = AttendeeResponseStatus.NEEDS_ACTION
    is_organizer: bool = False
    is_self: bool = False  # True if this attendee == the account's email


class EventVisibility(StrEnum):
    DEFAULT = "default"  # inherit calendar default
    PUBLIC = "public"
    PRIVATE = "private"


class EventStatus(StrEnum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


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
    account_id: str  # the CalendarAccount this came from
    title: str
    start: datetime
    end: datetime
    etag: str = ""  # optimistic-concurrency token (Google: event.etag); empty if unsupported
    all_day: bool = False
    description: str = ""
    location: str = ""
    organizer_email: str = ""
    attendees: tuple[CalendarAttendee, ...] = ()
    visibility: EventVisibility = EventVisibility.DEFAULT
    status: EventStatus = EventStatus.CONFIRMED
    transparency: str = "opaque"  # "opaque" (busy) | "transparent" (free) — see find_free_time
    html_link: str = ""  # web URL to view the event in the provider's UI
    recurring_event_id: str | None = None


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
    target backend. Backend-specific knobs (Google extended properties,
    color ids, conferencing data) are intentionally **not** representable
    here; they require either a typed dataclass extension or a v2 ABC
    capability. ``send_invites`` defaults to ``False`` so AI-driven and
    programmatic callers must opt in to firing real invites; the SPA
    create-event drawer flips this to ``True`` for human-confirmed flows.
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
    # Optional idempotency token forwarded to backends that support it
    # (Google Calendar passes it as ``requestId`` on events.insert).
    # When non-empty, identical requests within the backend's
    # deduplication window return the original event instead of creating
    # a duplicate. Service computes a deterministic default when caller
    # omits — see "Idempotency" below.
    idempotency_key: str = ""


@dataclass
class CalendarAccount:
    """A configured calendar account (stored in ``calendar_accounts``)."""

    id: str
    name: str  # human label e.g. "Sarah's work calendar"
    email_address: str  # the account's email (used to compute is_self on attendees)
    backend_name: str  # registered backend name, e.g. "google_calendar"
    backend_config: dict[str, object] = field(default_factory=dict)
    calendar_id: str = "primary"  # which calendar within the account to read/write
    timezone: str = "UTC"  # IANA tz used for display + free-time math; validated on write
    working_hours_start_hour: int = 9
    working_hours_end_hour: int = 18
    owner_user_id: str = ""
    shared_with_users: list[str] = field(default_factory=list)
    shared_with_roles: list[str] = field(default_factory=list)
    poll_enabled: bool = True
    poll_interval_sec: int = 300  # 5 minutes — calendars don't change second-to-second
    upcoming_event_lookahead_minutes: int = 15  # window for calendar.event.upcoming events
    health: str = "ok"  # "ok" | "unhealthy" — flipped by poll-failure heuristic
    last_error: str = ""  # last backend error message (cleared on next successful poll)
    last_error_at: str = ""  # ISO8601 timestamp of last_error
    created_at: str = ""

    def to_dict(self) -> dict[str, object]: ...

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "CalendarAccount": ...


class CalendarAccess(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"
```

##### Authorization helpers (mirror `inbox.py` exactly)

```python
def can_access_account(
    user_ctx: UserContext,
    account: CalendarAccount,
) -> bool: ...


def can_admin_account(
    user_ctx: UserContext,
    account: CalendarAccount,
) -> bool: ...


def determine_access(
    user_ctx: UserContext,
    account: CalendarAccount,
) -> CalendarAccess | None: ...
```

`is_admin` is **derived inside the helpers** from `user_ctx` (true when
`"admin" in user_ctx.roles` or `user_ctx is UserContext.SYSTEM`). Callers
must never pass an ad-hoc `is_admin` boolean — that contract was the
inbox's footgun and we lift it here. Tests that need to simulate admin
build a `UserContext` with `roles={"admin"}`.

Same precedence as inbox: owner > admin > shared_user > shared_role; full
access (owner / admin / any shared) means read + write + create event;
admin-of-account (= owner OR system admin role) means edit settings +
reshare.

##### `CalendarBackend` ABC

```python
class CalendarBackend(ABC):
    """Abstract calendar provider — events, free/busy, mutations.

    Backends register themselves via ``__init_subclass__`` keyed on
    ``backend_name``. Read methods accept timezone-aware datetimes and
    must return timezone-aware datetimes. All times in / out are
    UTC-or-local-but-tz-aware; the caller chooses display.
    """

    _registry: dict[str, type["CalendarBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None: ...

    @classmethod
    def registered_backends(cls) -> dict[str, type["CalendarBackend"]]: ...

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any] | None = None) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def list_calendars(self) -> list[dict[str, str]]:
        """Return ``[{id, name, timezone, primary}, ...]`` for the
        account. Used by the settings UI to populate the calendar_id
        dropdown after the user pastes credentials."""
        ...

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
        ``single_events=True`` asks the backend to expand recurring series
        into individual instances. The CalendarService always passes True
        for v1; aggregation tools never see raw recurrence rules.
        ``max_results`` is per-backend; the service's per-call cap is
        applied **after** aggregation across runtimes.
        """
        ...

    @abstractmethod
    async def get_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> CalendarEvent | None: ...

    @abstractmethod
    async def free_busy(
        self,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> list[FreeBusyBlock]:
        """Return busy intervals for the given calendars in the given
        window. Free time is the complement (computed by the service).
        Backends MUST exclude events with ``transparency="transparent"``
        (e.g. Google "Free" / "Working location") and events whose
        ``status`` is ``cancelled``."""
        ...

    @abstractmethod
    async def create_event(
        self,
        calendar_id: str,
        request: EventCreateRequest,
    ) -> CalendarEvent:
        """Create an event. If ``request.idempotency_key`` is non-empty,
        the backend MUST forward it (Google: as ``requestId``) so retries
        within the backend's dedup window return the original event."""
        ...

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
        backend MUST send it as an ``If-Match`` header (Google supports
        this on patch). On etag mismatch, raise
        ``CalendarBackendConflictError`` so the service can refresh and
        let the caller retry."""
        ...

    @abstractmethod
    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        send_cancellations: bool = False,
    ) -> None: ...

    # Optional — default raises NotImplementedError so backends without
    # RSVP can opt out cleanly. No AI tool calls this in v1.
    async def respond_to_event(
        self,
        calendar_id: str,
        event_id: str,
        response: AttendeeResponseStatus,
    ) -> None:
        raise NotImplementedError(
            f"Backend {self.backend_name!r} does not support responding to events"
        )
```

##### Error taxonomy

```python
class CalendarBackendError(Exception):
    """Base class for all calendar backend errors."""


class CalendarBackendAuthError(CalendarBackendError):
    """OAuth / service-account credentials failed; non-retryable until
    user fixes config. Maps Google 401/403 (when reason = ``authError``,
    ``invalid_grant``, ``forbidden`` for delegation)."""


class CalendarBackendNotFoundError(CalendarBackendError):
    """Calendar or event id not found. Maps Google 404."""


class CalendarBackendConflictError(CalendarBackendError):
    """Optimistic-concurrency conflict (etag mismatch). Service surface
    catches and refreshes the cached event; the caller (UI) is expected
    to retry."""


class CalendarBackendRateLimitError(CalendarBackendError):
    """Backend rate limit hit; ``retry_after_sec`` is the suggested
    wait. Service applies exponential backoff with jitter and surfaces
    repeated failures via account ``health="unhealthy"``."""

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class CalendarBackendTransientError(CalendarBackendError):
    """5xx or network blips — retry with backoff."""
```

The Google backend wraps `googleapiclient.errors.HttpError` into the
appropriate subclass based on `e.resp.status` and the parsed
`reason`. The service layer drives retry/backoff strictly off the
exception type, never off raw HTTP codes.

##### `CalendarProvider` capability protocol

```python
@runtime_checkable
class CalendarProvider(Protocol):
    """Protocol other services consume via ``resolver.get_capability("calendar")``.

    **Identity sourcing:** every method takes ``user_ctx`` explicitly.
    The AI tool dispatcher constructs a ``UserContext`` from injected
    ``_user_id`` / ``_user_roles`` arguments and passes it through; the
    WS RPC layer constructs it from the connection's authenticated
    session. Internal callers (e.g., the poll loop) pass
    ``UserContext.SYSTEM``. The service does **not** read
    ``get_current_user()`` from these methods — making the actor
    explicit at every boundary is the rule from
    ``memory-multi-user-isolation.md`` (Audit procedure: "Tool handlers
    should read caller identity from injected ``_user_id`` arguments,
    not from ``self`` or global context").

    **Aggregation:** when ``account_id=None``, every read fans out
    concurrently across every account the user can access via
    ``asyncio.gather(..., return_exceptions=True)`` with a per-runtime
    timeout of ``aggregation_timeout_sec`` (default 10). Per-runtime
    failures surface in ``warnings`` on the result envelope (see
    ``AggregatedEvents`` below) but never fail the whole call. The
    aggregate result is capped at ``max_results`` *post-merge*.
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
    ) -> "AggregatedEvents": ...

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
```

```python
@dataclass(frozen=True)
class AggregatedEvents:
    """Return envelope for aggregate read methods so partial failures
    are visible to callers (especially the AI tool surface) instead of
    being silently swallowed."""

    events: list[CalendarEvent]
    warnings: list[str] = field(default_factory=list)  # human-readable per-account failure notes
```

##### `CachedCalendarLister` (mirrors `CachedMailboxLister`)

```python
@runtime_checkable
class CachedCalendarLister(Protocol):
    """Snapshot used by ConfigurationService to populate
    ``calendar_accounts`` dropdowns on settings pages."""

    @property
    def cached_accounts(self) -> list[CalendarAccount]: ...
```

##### Why ABC vs. Protocol?

- `CalendarBackend` is an **ABC** (not Protocol) because it owns the
  registry side-effect (`__init_subclass__`) and we want concrete
  classes to fail loudly at definition time if they miss a required
  method. Same shape as `EmailBackend`, `AIBackend`, etc.
- `CalendarProvider`, `CachedCalendarLister` are **Protocols** because
  consumers `isinstance`-check them at runtime to decouple from
  `CalendarService` (rule from `memory-capability-protocols.md`).

### New Service(s)

#### `src/gilbert/core/services/calendar.py` — `CalendarService`

##### Identity

```python
class CalendarService(Service):  # also implements ToolProvider, CalendarProvider, CachedCalendarLister
    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="calendar",
            capabilities=frozenset({"calendar", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset({"event_bus", "configuration", "access_control"}),
            events=frozenset({
                "calendar.event.upcoming",
                "calendar.account.created",
                "calendar.account.updated",
                "calendar.account.deleted",
                "calendar.account.shares.changed",
                "calendar.account.health_changed",
                "calendar.event.created",
                "calendar.event.updated",
                "calendar.event.deleted",
            }),
            ai_calls=frozenset(),  # the service uses no AI of its own
            toggleable=True,
            toggle_description="Calendar polling and event tools",
        )
```

The `events=frozenset({...})` declaration is **advisory** — it
documents what this service publishes, but actual visibility is
governed by entries in `interfaces/acl.py`'s
`DEFAULT_EVENT_VISIBILITY` and the per-event WS fanout filter. Adding
an event name here doesn't grant any user the ability to receive it.

##### Lifecycle

`start(resolver)`:

1. Resolve `entity_storage` (required) → `_storage`. Ensure indexes:
   - `calendar_accounts(owner_user_id)`
   - `calendar_events(account_id, start)` — primary path for per-account queries
   - `calendar_events(start)` — leading-key index for aggregate queries
     spanning multiple accounts (`get_schedule` with `account_id=None`)
   - `calendar_event_announcements(account_id, start_iso)` — for the
     dedup collection's sweep
   - `shared_with_users` / `shared_with_roles` are CONTAINS-filtered
     and deliberately not indexed (low cardinality; mirrors inbox)
2. Resolve `scheduler` (required) via `SchedulerProvider`.
3. Resolve `event_bus`, `configuration`, `access_control` (optional).
4. Read service config (`enabled`, `default_event_lookahead_days`,
   `aggregation_timeout_sec`, `mutate_publish_dedup_sec`).
5. If disabled → return early (matches inbox).
6. Schedule one-shot `calendar-boot` job → `_boot_runtimes()`.
   (Network-bound backend init must not block `start()`; exact same
   pattern InboxService uses.)
7. Schedule recurring `calendar-announcement-sweep` job (every 30 min)
   that deletes `calendar_event_announcements` rows where
   `start_iso < now - 48h` and `calendar_events` rows where
   `start < now - 24h`. This is the **only** cleanup path for those
   collections — entity storage has no TTL primitive, so the spec
   commits to an explicit sweep.

`stop()`: cancel each `calendar-poll-{account_id}` job, the boot job,
and the sweep job; close every runtime backend in parallel via
`asyncio.gather(..., return_exceptions=True)`.

##### Per-account runtime registry

```python
@dataclass
class _AccountRuntime:
    account: CalendarAccount
    backend: CalendarBackend
    poll_job_name: str = ""
    # Seeded from persisted ``calendar_events`` cache on _start_runtime —
    # see "Polling logic" below — so a process restart does NOT
    # republish every existing event as ``calendar.event.created``.
    last_seen_event_ids: set[str] = field(default_factory=set)
    # event_id → mutate-path publish timestamp; the next poll diff
    # suppresses *.created/*.updated/*.deleted publication for ids in
    # this map for ``mutate_publish_dedup_sec`` seconds (default 60).
    recent_mutate_publishes: dict[str, float] = field(default_factory=dict)
    # Consecutive poll failures; drives ``account.health`` flip.
    consecutive_failures: int = 0
```

`self._runtimes: dict[str, _AccountRuntime]` — keyed by `account_id`,
populated by `_start_runtime`, drained by `_stop_runtime`. Exact mirror
of `_MailboxRuntime`.

**Cold-start jitter (mandatory, not optional):** in `_start_runtime`
the first scheduled fire of `calendar-poll-{account_id}` is offset by
`random.uniform(0, min(account.poll_interval_sec, 120))` seconds.
Prevents a Gilbert restart from issuing N simultaneous Google API
requests for N accounts — at scale this trips per-user 600/100s
quota. The jitter is recomputed on every restart (no persistence
needed). Same applies to runtime *creation* via
`create_account` so initial sync doesn't pile on with the boot wave.

##### Storage collections

The fetch and trim windows are **chosen to match exactly** so the
cache never holds rows that the next poll wouldn't return — the
previous draft had a `1h` fetch back-window vs. a `24h` trim
back-window, which left ghost rows in the `[-24h, -1h)` range that
the diff treated as "missing" and deleted.

| Collection | Purpose | Key fields |
|---|---|---|
| `calendar_accounts` | One row per configured account | All `CalendarAccount` fields. `_id == id`. |
| `calendar_events` | Local cache of events in the **fetch window** `[now − cache_back_hours, now + default_event_lookahead_days]`. Trim and fetch use the **same** window. | `_id` = `f"{account_id}:{event_id}"`, `account_id`, `event_id`, `calendar_id`, `title`, `start_iso`, `end_iso`, `all_day`, `etag`, `status`, `transparency`, `attendees_json`, `organizer_email`, `location`, `description`, `html_link`, `recurring_event_id`, `visibility`. |
| `calendar_event_announcements` | Tracks which events have already had `calendar.event.upcoming` published, so a process restart doesn't re-fire them. | `_id` = `f"{account_id}:{event_id}"`, `account_id`, `event_id`, `start_iso`, `announced_at`. |

The cache exists to (a) make `get_schedule` / `next_event` cheap and
not block on a network round-trip, and (b) let the
`_emit_upcoming_for_account` helper fire `calendar.event.upcoming`
events when an event crosses into the N-minute window — without
re-fetching every 30 seconds.

`cache_back_hours` defaults to **2** (service-level ConfigParam) — wide
enough to keep just-finished events visible for "what was my last
meeting?" queries, narrow enough that the cache doesn't grow without
bound. The poll fetches `now − cache_back_hours .. now +
default_event_lookahead_days`, upserts every event, and deletes any
cached row for the same account whose `event_id` no longer appears in
the fresh fetch (so cancels on the remote side propagate). Events
that fall out of the back-window are reaped by the
`calendar-announcement-sweep` job described in Lifecycle.

**Cascading delete on account removal:** `delete_account` MUST also
delete every `calendar_events` row where `account_id == id` and every
`calendar_event_announcements` row where `account_id == id` — done
inside the same service method, before the account row itself is
deleted, so a crash mid-delete leaves the runtime live (and the next
boot drains it cleanly) rather than leaving orphan event rows alive
forever. We do **not** rely on `OnDelete.CASCADE` because the storage
backend doesn't enforce foreign keys across collections.

##### Polling logic

`_make_poll_callback(account_id)` returns an async closure mirroring
`InboxService._make_poll_callback`. The callback:

1. Resolves the runtime by id (no-op if account was removed).
2. **Lazy seed** — if `runtime.last_seen_event_ids` is empty AND the
   runtime was created in this process lifetime less than
   `(now - process_start_time) < 1.0s` ago, seed it from the persisted
   cache:
   `runtime.last_seen_event_ids = {row.event_id for row in storage.query("calendar_events", account_id=account_id)}`.
   This prevents the first poll after restart from re-publishing every
   cached event as `calendar.event.created`. (The 1-second gate is
   what distinguishes "first poll after a fresh `_start_runtime`" from
   "subsequent poll on a long-running runtime that legitimately has
   `last_seen_event_ids = set()` for some other reason.")
3. Calls `backend.list_events(calendar_id, now − cache_back_hours,
   now + default_event_lookahead_days, single_events=True)`. Wraps in
   per-call timeout (`aggregation_timeout_sec`) and routes errors
   through the error taxonomy: auth/notfound errors flip
   `account.health` to `"unhealthy"` and stop the diff/publish path
   (the cache stays as-is so the SPA still renders the last good
   state); rate-limit errors apply exponential backoff with jitter
   (`min(2 ** consecutive_failures, 600)` seconds, capped + jittered);
   transient errors bump `consecutive_failures` and re-schedule.
4. **Filter cancelled events out of the fresh set BEFORE the diff** —
   `fresh = [e for e in events if e.status != EventStatus.CANCELLED]`.
   This is intentional: a cancelled event must show up in the diff as
   "missing" so we publish `calendar.event.deleted` for it once, then
   trim from cache.
5. Diff `fresh` against `last_seen_event_ids`, **suppressing any
   event_id present in `runtime.recent_mutate_publishes` whose
   timestamp is within `mutate_publish_dedup_sec`** (default 60s):
   - **New** ids → `calendar.event.created`.
   - **Missing** ids → `calendar.event.deleted` (covers cancellations
     and remote deletes both).
   - **Same id, different `start`/`end`/`title`/`location`/`description`/
     `attendees`/`status`** → `calendar.event.updated`. Not a literal
     dataclass equality check — the spec lists these fields explicitly
     so cosmetic fields like `etag` don't trigger spurious updates.
6. Upsert every event in `fresh` into `calendar_events`; delete
   missing rows for this account.
7. Set `last_seen_event_ids = {e.event_id for e in fresh}`.
8. Run `_emit_upcoming_for_account(account)` to fire
   `calendar.event.upcoming` for every event whose
   `start - now <= upcoming_event_lookahead_minutes` and which doesn't
   already have a `calendar_event_announcements` row.
9. Reset `consecutive_failures` and clear `last_error` on success.
   If `account.health` was previously `"unhealthy"`, flip it to `"ok"`
   and emit `calendar.account.health_changed`.

**Account health surfacing.** When `consecutive_failures` crosses
`unhealthy_failure_threshold` (default 3) and the latest exception
was an auth error, set `account.health = "unhealthy"`, write the
exception's `str()` to `last_error` (truncated to 500 chars), set
`last_error_at = now.isoformat()`, persist the account row, and emit
`calendar.account.health_changed` with `{account_id, health,
last_error}`. The SPA's account list shows a red badge on unhealthy
accounts, and the `list_calendar_accounts` AI tool surfaces `health` in
its output so the AI can tell the user "your work calendar isn't
syncing — please check the credentials." Without this, "my calendar
stopped working" is a server-log dive.

##### Mutation publish dedup

Every successful mutation (`create_event`, `update_event`,
`delete_event`) records `runtime.recent_mutate_publishes[event_id] =
time.monotonic()` **before** publishing the corresponding
`calendar.event.*` event. The poll's diff (step 5 above) suppresses
republication for any id in that map within
`mutate_publish_dedup_sec`. After that window, entries are pruned;
stale entries left behind during a process crash self-expire on the
first poll after restart (the value is monotonic-relative).

##### Idempotency

`create_event` accepts an optional `idempotency_key` on
`EventCreateRequest`. When the caller (AI tool, WS RPC) omits it, the
service computes:

```python
idempotency_key = sha256(
    f"{account_id}|{request.title}|{request.start.isoformat()}|"
    f"{request.end.isoformat()}|{','.join(sorted(a.email for a in request.attendees))}"
).hexdigest()[:32]
```

Backends that accept `idempotency_key` (Google: `requestId` on
`events.insert`) forward it; duplicate requests within the backend's
dedup window return the original event. This protects against the
common AI / agent retry foot-gun where a retried `create_event` would
otherwise produce two events with two invite emails. WS RPCs from
the SPA pass the user-confirmed UI block's `block_id` as the key so a
double-click submit can't fire twice.

##### Tools exposed

All read tools accept an optional `account_id`. When omitted, the
service aggregates across every account the current user can access.
For mutating tools (`create_event`, `update_event`, `delete_event`),
`account_id` is **required** when the user has more than one
accessible account; missing returns an error telling the AI to call
`list_calendar_accounts` first. (Same pattern inbox tools use.)

Slash command declarations are explicit per-tool: every entry below
has `slash_group="calendar"`, the `slash_command` listed in the table,
and a one-line `slash_help` (the architecture checklist requires
`slash_help` on every command). The `CalendarService` class does
**NOT** declare `slash_namespace` — that field is reserved for plugin
namespacing per `memory-slash-commands.md` ("Plugin namespacing");
core services with grouped tools rely on per-tool `slash_group` only.

| Name | `slash_group` / `slash_command` | Required role | Parallel-safe | Purpose |
|---|---|---|---|---|
| `list_calendar_accounts` | `calendar` / `accounts` | user | yes | List accessible accounts (read-only). |
| `get_schedule` | `calendar` / `schedule` | user | yes | Events on a date or in a `[start, end)` range across one or all accessible accounts. Replaces the previous `today_schedule`. |
| `next_event` | `calendar` / `next` | user | yes | Next event whose `start >= now`. Renamed from `next_meeting` because solo events ("dentist", "focus block") are events, not meetings. |
| `get_event` | (no slash — opaque-id input) | user | yes | Fetch one event by id with full detail — attendees, description, html_link. |
| `find_free_time` | (no slash — too many positional args) | user | yes | Free slots ≥ `duration_minutes` in `[time_min, time_max)`, optionally honoring co-attendees. |
| `create_event` | (no slash — see below) | user | **no** | Mutates: previews + (with `confirm=True`) creates an event. |
| `update_event` | (no slash — opaque ids + multi-field) | user | **no** | Mutates: previews + (with `confirm=True`) updates an event. |
| `delete_event` | (no slash — opaque ids) | user | **no** | Mutates: previews + (with `confirm=True`) deletes an event. |

Slash exclusions follow `memory-slash-commands.md` — the parser maps
positional `shlex` tokens to declared parameters, and tools whose
required parameters are ISO datetimes, opaque ids, or arrays of
emails do not translate to a useful one-line shell form. `find_free_time`
already needs `duration_minutes`, `start`, `end`, optional
`attendee_emails` — too many tokens to be ergonomic. `create_event` /
`update_event` / `delete_event` are all opaque-id-or-multi-field
inputs and additionally hit the "high-blast-radius mutation" criterion;
slash users go through the SPA `/calendar` page instead.

**Preview/confirm pattern for mutating tools (`create_event`,
`update_event`, `delete_event`).** Mirroring `memory-ui-blocks.md`'s
`ToolOutput` pattern: the AI calls the tool with `confirm=False` (the
default). The service does NOT touch the backend; instead it returns
a `ToolOutput` whose `text` is a short summary ("I'm about to create
'Team sync' on Tuesday at 3pm with brian@example.com — confirm?") and
whose `ui_blocks` contains a single `UIBlock` with a Confirm/Cancel
`buttons` element. When the user clicks Confirm, the SPA submits the
form via `POST /chat/form-submit`; the AI re-invokes the same tool
with the same arguments plus `confirm=True`, and the service performs
the actual mutation. **`send_invites` defaults to `False`** at the
tool layer (overriding `EventCreateRequest`'s default) so even on
confirm, the AI must explicitly opt in to firing real invite emails.
This is the only sane default for a chat-driven calendar — fixes a
class of "AI hallucinated the date and now ten people got a bogus
invite" outages.

**The `light` tool profile is incompatible with the preview/confirm
flow.** Greeting and any other `complete_one_shot(tools_override=[])`
caller forces *zero tools*; if greeting needs calendar context, it
must call `CalendarProvider.list_events` directly (via capability
resolution) and inject the formatted text into the prompt. We do
**NOT** make calendar tools available to greeting via tool inclusion —
see "Tool Profile Integration" below for the corrected design.

Tool parameter detail:

- **`list_calendar_accounts`** — no params. Returns
  `[{account_id, name, email_address, calendar_id, access, timezone, health, last_error}, ...]`
  where `access` is one of `owner`, `admin`, `shared_user`, `shared_role`
  (the `shared_*` distinction comes from `determine_access` —
  `shared_user` = explicit per-user grant, `shared_role` = role-based
  grant). The tool description enumerates these four values literally
  so the AI doesn't conflate them.
- **`get_schedule`** — params:
  - `account_id: str | null` (optional, null = aggregate)
  - `date: str | null` (optional ISO local date; "today" / "tomorrow" / "yesterday" accepted as conveniences). Mutually exclusive with `start`/`end`.
  - `start: str | null` (optional ISO datetime, used for ranges)
  - `end: str | null` (optional ISO datetime; required if `start` given)
  - Default: today in the account's tz.
  - Returns: `{events: [{event_id, account_id, title, start, end, location, attendees: [emails], all_day, status}], warnings: [str]}` — `warnings` lists per-account fetch failures from the aggregate path.
  - All datetimes in the response are **ISO 8601 with the account's timezone offset**; description tells the AI to render in the user's local tz.
- **`next_event`** — params:
  - `account_id: str | null` (optional, aggregate by default)
  - `within_hours: int | null` (optional, default 72; null = unlimited). The default of 72 covers "anything tonight or tomorrow" without needing a retry. Note: `0` is **not** a sentinel for unlimited — pass `null` instead, since models routinely supply `0` literally and we don't want "0 hours" to silently mean "the rest of forever."
  - Returns: single event dict (same shape as `get_schedule` items) or `null`.
- **`get_event`** — params:
  - `account_id: str` (required)
  - `event_id: str` (required)
  - Returns: full event dict including `description`, all attendees with response status, `html_link`, `recurring_event_id`. Light-tier so chat can ask "tell me about my next meeting."
- **`find_free_time`** — params:
  - `duration_minutes: int` (required, 5 ≤ value ≤ 480; service rejects out-of-range with a clear error)
  - `account_id: str | null` (optional; for the free-busy lookup. Aggregate across all accessible accounts by default.)
  - `start: str` (optional ISO datetime, default = now)
  - `end: str` (optional ISO datetime, default = start + 7 days; service validates `start < end` and `duration_minutes ≤ (end - start).total_seconds() / 60`)
  - `respect_working_hours: bool` (optional, default true). Working hours come from each `CalendarAccount`'s `working_hours_start_hour` / `working_hours_end_hour`; when aggregating across multiple accounts, the **intersection** of working-hours windows is used (most-restrictive wins).
  - `max_results: int` (optional, default 5)
  - `attendee_emails: list[str] | null` (optional). When present, the backend's `free_busy` is queried for these calendar ids in addition to the user's own. The tool description warns that visibility depends on the other party's calendar sharing settings: if Google returns an `errors` block for a target email, that email's busy intervals are treated as unknown and a warning is appended to the result. Cross-account/cross-user discovery is the canonical use case ("when can Sarah and I meet?") and shipping it in v1 is the difference between a useful tool and a confidence-eroding one.
  - **Algorithm (pinned):** slot granularity is 15 minutes; busy = events whose `transparency != "transparent"` and `status != "cancelled"`; tentative events count as **busy** for find_free_time (people don't want a "free" slot landing on top of a maybe-meeting); declined events count as free; all-day events are intersected with the working-hours window (so a 9-6 working day with an all-day OOO event still treats 9-6 as busy, but a 24h block doesn't bleed into a midnight-to-9 "free" window). Slots returned are clamped to working hours when `respect_working_hours=True`. The implementation walks the merged busy-block list in chronological order; gaps ≥ `duration_minutes` become slots until `max_results` reached.
  - Returns: list of `{start, end, slot_duration_minutes, requested_duration_minutes}`.
- **`create_event`** — params:
  - `account_id: str` (required when user has >1 account; optional when 1)
  - `title: str` (required)
  - `start: str` (required, ISO datetime; tz-aware preferred, naive interpreted in the account's timezone). The tool description includes: *"If the user gave you a relative time ('tomorrow', 'next Tuesday'), call `system_datetime` first to anchor 'now', then compute the ISO string in the account's timezone. Recurring phrases like 'every Tuesday at 3' are NOT supported — ask the user to set those up manually."*
  - `end: str | null` (one of `end` or `duration_minutes` required, not both)
  - `duration_minutes: int | null` (one of `end` or `duration_minutes` required). Most natural-language event creation is "an hour with Sarah" — letting the AI pass a duration avoids ISO arithmetic that models silently get wrong on DST boundaries.
  - `description: str` (optional)
  - `location: str` (optional)
  - `attendees: list[str]` (optional, list of email addresses). The tool description tells the AI: *"If the user references attendees by name, resolve to email first via `directory_search` (from the google_directory plugin) — do NOT hallucinate email addresses."*
  - `all_day: bool` (optional, default false)
  - `send_invites: bool` (optional, default **false** — opt-in)
  - `confirm: bool` (optional, default false). When `false`, the tool returns a preview `ToolOutput` with a confirmation `UIBlock` and does NOT touch the backend.
  - Service rejects naive datetimes if `account.timezone` is invalid (validated at the service entry, not at the backend). At the tool→service boundary, every naive datetime is **localized to `ZoneInfo(account.timezone)`** before constructing `EventCreateRequest`; the resulting `datetime` is always tz-aware before crossing into the dataclass.
  - Returns: created event dict including `html_link`. The tool description tells the AI to include the `html_link` in its reply so the user gets a click-through.
- **`update_event`** — params:
  - `account_id: str` (required)
  - `event_id: str` (required)
  - Same partial-update fields as `create_event` (all optional)
  - `confirm: bool` (default false; preview `UIBlock` shows the *delta* — old value → new value per field).
  - For events with `recurring_event_id != null`, the tool description warns: *"This will modify only this single instance, not the whole recurring series. To change the series, ask the user to do it manually for now."*
  - The service uses optimistic concurrency: it reads the current event's `etag`, passes it as `if_match_etag` on the backend call. On `CalendarBackendConflictError`, the service refreshes the cache and returns a tool error message ("the event changed since you fetched it; please re-read with `get_event` and try again").
  - Returns: updated event dict.
- **`delete_event`** — params:
  - `account_id: str` (required)
  - `event_id: str` (required)
  - `send_cancellations: bool` (optional, default false)
  - `confirm: bool` (default false; preview `UIBlock` shows the event being deleted with attendee count).
  - Same recurring-instance warning as `update_event`.
  - Returns: `{deleted: true, event_id}` on success.

##### Events published

All carry `account_id` in `data`.

- `calendar.event.upcoming` — fired by the poll when an event enters the
  account's `upcoming_event_lookahead_minutes` window. Data:
  `{account_id, event_id, title, start, location, attendee_emails,
  organizer_email, owner_user_id}`.
- `calendar.event.created` / `calendar.event.updated` /
  `calendar.event.deleted` — fired by the poll when the diff against
  `last_seen_event_ids` detects the change, AND fired by the
  service's own `create_event`/`update_event`/`delete_event` calls
  immediately on success. The poll path uses
  `recent_mutate_publishes` (see "Mutation publish dedup" above) to
  suppress duplicate publication when the mutate path already
  announced an event id within `mutate_publish_dedup_sec`.
- `calendar.account.created` / `updated` / `deleted` /
  `shares.changed` / `health_changed` — same shape as inbox mailbox
  events. `health_changed` carries `{account_id, health, last_error}`.

**Cross-user privacy.** The visibility unit is the **account**, not
the per-event attendee list. If user A shares an account with user
B, and an event on that account has user C as an attendee, user B
sees user C's email through the `calendar.event.upcoming` fanout —
that is how shared calendars work. The fanout filter does not
attempt per-attendee filtering; once a user has access to an
account, they see everything in it. Service-account JSON in
`backend_config` is masked at the WS layer via the existing
`sensitive=True` redaction; `backend_config` is **not** echoed in
`calendar.account.*` events.

Event ACL prefix: `calendar.` is registered at `interfaces/acl.py` at
level 100 (user). The WS fanout adds a per-event account-access check
keyed off `account_id` — same mechanism the frontend uses for inbox.

##### WS RPC handlers

Account CRUD + sharing (admin-of-account gated — see "Per-handler
authorization" below):

- `calendar.accounts.list`
- `calendar.accounts.get`
- `calendar.accounts.create`
- `calendar.accounts.update`
- `calendar.accounts.delete`
- `calendar.accounts.share_user` / `unshare_user`
- `calendar.accounts.share_role` / `unshare_role`
- `calendar.accounts.test_connection`
- `calendar.accounts.probe_calendars` — populates the calendar-id
  dropdown in the SPA's account drawer **after the account row has
  been persisted with `poll_enabled=False`**. The handler is a thin
  pass-through: `payload = {"account_id": str}`; it calls
  `CalendarService.probe_calendars(account_id, user_ctx)` which
  resolves the persisted `CalendarAccount`, runs `can_admin_account`,
  instantiates the backend via `CalendarBackend.registered_backends()
  [account.backend_name]()`, calls `await backend.initialize(...)` →
  `await backend.list_calendars()`, and runs `await backend.close()`
  in a `try/finally` even on exception so a probe failure can't leak
  the connection. The previous draft proposed running this against a
  pre-save `backend_config` blob inside the WS handler — that is
  exactly the "concrete-class-instantiation in the wrong place"
  pattern `memory-backend-pattern.md` warns against, plus it bypasses
  the runtime lifecycle and has no cleanup story on error. The
  account-must-be-saved-first path solves both. The SPA flow is:
  `POST create({poll_enabled: false, calendar_id: "primary"})` →
  user picks calendar from probe → `POST update({calendar_id, poll_enabled: true})`.

Reads (caller's accessible accounts; per-handler `can_access_account`
on `account_id`):

- `calendar.events.list` — `{account_id?, time_min, time_max, max_results?}`
- `calendar.events.get`
- `calendar.freebusy.get` — `{account_id?, time_min, time_max}`
- `calendar.find_free_time` — same params as the AI tool

Writes (per-handler `can_access_account` on `account_id`):

- `calendar.events.create` / `update` / `delete`

Backend discovery:

- `calendar.backends.list` — returns registered `CalendarBackend`s and
  `backend_config_params()` schemas. Mirrors `inbox.backends.list`.
  The response includes a `display_name` field per backend (e.g.
  `"Google Calendar"`) so the SPA can render a friendly label rather
  than the bare registry key. `display_name` is a class attribute on
  `CalendarBackend` subclasses (default falls back to a titlecased
  `backend_name`). This avoids the "the user sees `'google'` for
  Auth, Email, AND Calendar in three different drawers" confusion.

##### Per-handler authorization

The ACL prefix-level gate (`"calendar.": 100` in `interfaces/acl.py`)
admits **any authenticated user** to *any* `calendar.*` frame. Every
handler MUST do its own authorization check on top, **before any
storage write**, mirroring how inbox handles its `"inbox."` prefix:

- `accounts.create` / `update` / `delete` / `share_*` /
  `test_connection` / `probe_calendars` → require `can_admin_account`
  on the target account (or, for `create`, just require an
  authenticated user — the creator becomes `owner_user_id`).
- `accounts.list` / `get` → filter to accounts where
  `can_access_account` returns true; never return accounts the caller
  can't see.
- `events.list` / `get` / `freebusy.get` / `find_free_time` /
  `events.create` / `update` / `delete` → require
  `can_access_account` on the resolved `account_id`. For aggregate
  reads (`account_id is None`), the service filters to accessible
  accounts before fanout.

Without the per-handler check, the prefix-level gate would be a
silent privilege-escalation surface — an authenticated `user` could
read or mutate any account in the system. The handler's first action
on every entry is an authorization check that short-circuits with a
`PermissionError` (translated to a WS error frame) before any
side-effect.

##### ConfigParams (service-level)

```python
@property
def config_namespace(self) -> str:
    return "calendar"

@property
def config_category(self) -> str:
    return "Communication"

def config_params(self) -> list[ConfigParam]:
    return [
        ConfigParam(
            key="enabled",
            type=ToolParameterType.BOOLEAN,
            description="Whether calendar polling and event tools are enabled.",
            default=True,
        ),
        ConfigParam(
            key="default_event_lookahead_days",
            type=ToolParameterType.INTEGER,
            description=(
                "How many days into the future the per-account poll caches. "
                "Larger values mean more memory + storage, but more responsive "
                "find_free_time queries that span weeks."
            ),
            default=14,
        ),
        ConfigParam(
            key="cache_back_hours",
            type=ToolParameterType.INTEGER,
            description=(
                "How many hours into the past the cache retains events. "
                "Wide enough to answer 'what was my last meeting' for a few "
                "hours after it ends; narrow enough to keep cache size bounded."
            ),
            default=2,
        ),
        ConfigParam(
            key="upcoming_announce_minutes",
            type=ToolParameterType.INTEGER,
            description=(
                "Default lead time (in minutes) for ``calendar.event.upcoming`` "
                "events. Per-account override available on each account row. "
                "This is the *imminent-event* notification window — for the "
                "morning-brief greeting use case (hours into the future), the "
                "greeting service computes its own lookahead via "
                "CalendarProvider.list_events; do not conflate the two."
            ),
            default=15,
        ),
        ConfigParam(
            key="aggregation_timeout_sec",
            type=ToolParameterType.INTEGER,
            description=(
                "Per-runtime timeout (seconds) when fanning out aggregate "
                "reads (``account_id=None``) across multiple accounts. "
                "A slow or hung backend never blocks the aggregate result; "
                "its failure is surfaced as a warning."
            ),
            default=10,
        ),
        ConfigParam(
            key="mutate_publish_dedup_sec",
            type=ToolParameterType.INTEGER,
            description=(
                "Window (seconds) during which the poll-loop diff suppresses "
                "republication of `calendar.event.*` events for ids the mutate "
                "path already announced. Prevents the same logical mutation "
                "firing twice."
            ),
            default=60,
        ),
        ConfigParam(
            key="unhealthy_failure_threshold",
            type=ToolParameterType.INTEGER,
            description=(
                "Number of consecutive poll failures before the account flips "
                "to ``health=unhealthy`` and ``calendar.account.health_changed`` "
                "fires."
            ),
            default=3,
        ),
        ConfigParam(
            key="show_dashboard_card",
            type=ToolParameterType.BOOLEAN,
            description=(
                "Whether to render the 'Up next' card on the dashboard, "
                "showing the user's next event across accessible accounts."
            ),
            default=True,
        ),
    ]
```

The service does **not** call AI directly, so it has **no
`ai_prompt=True` ConfigParams**. The "calendar-aware greeting" prompt
addition lives in the greeting service, not here (see Migration below).

##### `tool_provider_name`

```python
class CalendarService(Service):
    @property
    def tool_provider_name(self) -> str:
        return "calendar"
```

`CalendarService` is a **core** service, so it sets neither
`slash_namespace` (plugin-only per `memory-slash-commands.md` §
"Plugin namespacing") nor a class-level slash-anything attribute. Per-tool
`slash_group="calendar"` provides the disambiguation. Setting both
would risk double-prefixing depending on slash-registry resolution
order. Confirmed against `InboxService`, which is core and also does
not declare `slash_namespace`.

##### Multi-user state (no `_current_*` attributes)

Per `memory-multi-user-isolation.md`, we store **only** service-lifetime
state on `self`:

- `self._storage`, `self._scheduler`, `self._event_bus` — service-lifetime.
- `self._runtimes: dict[str, _AccountRuntime]` — keyed by account, not user.
- `self._cached_accounts: list[CalendarAccount]` — refreshed on CRUD,
  service-lifetime in shape but its *contents* change.
- `self._enabled`, `self._default_lookahead_days`,
  `self._upcoming_announce_minutes`, etc. — service-level config.

**`_cached_accounts` invariants.** This snapshot is read by
`ConfigurationService._resolve_dynamic_choices("calendar_accounts")`
synchronously (no awaits), so it must be safe to read at any point:
(1) the cache is replaced **atomically** via `self._cached_accounts =
new_list` after each successful CRUD operation — never mutated in
place; (2) it is **not** the source of truth for
`list_accessible_accounts`, which always queries storage to avoid
serving a stale snapshot to a security-sensitive read; (3) for
multi-user filtering, callers must filter the snapshot themselves —
the cache holds every account regardless of caller, mirroring
`InboxService._cached_mailboxes`.

**Identity sourcing.** Every public method on the service takes
`user_ctx: UserContext` as an explicit parameter. AI tool handlers
read `_user_id` / `_user_roles` from injected args (the
`memory-multi-user-isolation.md` § "Required patterns" rule), build a
`UserContext`, and pass it through. WS RPC handlers build it from
the connection's authenticated session. Internal callers (the poll
loop, the announcement sweep) pass `UserContext.SYSTEM`. The service
does **not** read `get_current_user()` from public method bodies —
`get_current_user()` is left as the hidden default for
`UserContext.SYSTEM` in scheduler-driven code paths only.

### New / Modified Backend(s)

#### `std-plugins/google/google_calendar.py` — `GoogleCalendarBackend`

Class shape mirrors `GmailBackend` exactly: service-account JSON +
domain-wide delegation, `googleapiclient.discovery.build("calendar",
"v3", credentials=creds)`, all blocking calls wrapped in
`asyncio.to_thread`.

```python
class GoogleCalendarBackend(CalendarBackend):
    backend_name = "google_calendar"
    display_name = "Google Calendar"  # surfaced by calendar.backends.list

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="email_address",
                type=ToolParameterType.STRING,
                description="Email address of the calendar owner.",
                restart_required=True,
            ),
            ConfigParam(
                key="service_account_json",
                type=ToolParameterType.STRING,
                description=(
                    "Google service account key (paste JSON content). "
                    "Reuse the same service account configured for Gmail "
                    "if domain-wide delegation is set up; otherwise create "
                    "a dedicated one with calendar scopes."
                ),
                sensitive=True,
                restart_required=True,
                multiline=True,
            ),
            ConfigParam(
                key="delegated_user",
                type=ToolParameterType.STRING,
                description=(
                    "Email of the user to impersonate via domain-wide "
                    "delegation. Defaults to email_address."
                ),
                restart_required=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "List the user's calendars to verify the service "
                    "account and delegation."
                ),
            ),
        ]
```

OAuth scopes used during `initialize`:

```python
scopes = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]
```

##### Implementation notes

- `list_calendars()` → `service.calendarList().list().execute()` →
  `[{id, name=summary, timezone=timeZone, primary=primary}]`.
- `list_events()` → `service.events().list(calendarId=..., timeMin=...,
  timeMax=..., singleEvents=single_events, orderBy="startTime",
  maxResults=max_results).execute()`. Map the response into
  `CalendarEvent` dataclasses; preserve `htmlLink` →
  `CalendarEvent.html_link`, `etag` → `CalendarEvent.etag`,
  `transparency` → `CalendarEvent.transparency`. Handle both
  `start.dateTime` (timed) and `start.date` (all-day) — set
  `all_day=True` for the latter and build a midnight-local datetime
  with the calendar's tz (resolved via `ZoneInfo(account.timezone)`).
- `get_event()` → `service.events().get(...).execute()`.
- `free_busy()` → `service.freebusy().query(body={"timeMin": ..., "timeMax":
  ..., "items": [{"id": cid} for cid in calendar_ids]}).execute()`.
  Per-calendar `errors` blocks (visibility denied, calendar not found)
  are mapped to a `CalendarBackendError` *per calendar id* rather than
  failing the whole call — the service layer aggregates these into
  warnings on cross-user free-busy.
- `create_event()` → `service.events().insert(calendarId=...,
  sendUpdates="all" if request.send_invites else "none",
  conferenceDataVersion=0, body={..., "requestId":
  request.idempotency_key} if request.idempotency_key else
  body=...).execute()`. Empty-string `idempotency_key` omits
  `requestId` so Google's default semantics apply.
- `update_event()` → `events().patch(calendarId=..., eventId=...,
  body=..., sendUpdates="all" if request.send_invites else "none")`.
  When `if_match_etag` is non-empty, set the `If-Match` header on the
  request via `request.headers["If-Match"] = if_match_etag` before
  `.execute()`. On `HttpError` with `status == 412`, raise
  `CalendarBackendConflictError`.
- `delete_event()` → `events().delete(calendarId=..., eventId=...,
  sendUpdates="all" if send_cancellations else "none").execute()`.
- All blocking API calls wrap in `asyncio.to_thread`, exact same
  pattern as `gmail.py`.
- **Error mapping** (drives the service's retry/health logic):
  - `HttpError` with `status == 401` OR `403` with reason in
    `{"authError", "invalid_grant", "forbidden"}` → `CalendarBackendAuthError`.
  - `HttpError` with `status == 404` → `CalendarBackendNotFoundError`.
  - `HttpError` with `status == 412` → `CalendarBackendConflictError`.
  - `HttpError` with `status == 429` OR `status == 403` and reason in
    `{"rateLimitExceeded", "userRateLimitExceeded"}` →
    `CalendarBackendRateLimitError(retry_after_sec=...)` (parse
    `Retry-After` header when present, else `None`).
  - `HttpError` with `status >= 500` OR network errors (`socket.timeout`,
    `ConnectionError`, `ssl.SSLError`) → `CalendarBackendTransientError`.
  - Anything else → `CalendarBackendError`.
- **Logging**: every backend call logs at DEBUG with structured fields
  `account_id`, `method`, `calendar_id`, `duration_ms`,
  `event_count`. Failures log at WARNING with the same fields plus
  `error_class` and `status`. Metrics counters
  `calendar.backend.calls{backend, method, status}` and
  `calendar.backend.duration_ms{backend, method}` are incremented if
  a `MetricsProvider` capability is resolved.

##### Reuse of `google` plugin's existing OAuth machinery

What we **reuse**:

- The existing `pyproject.toml` `dependencies` (`google-auth`,
  `google-api-python-client`) — both already present, no version bumps.
- The convention of pasting service-account JSON + `delegated_user`
  inline in backend config (no separate CredentialService).
- The plugin's `setup()` side-effect-import pattern. We add one import:
  `from . import google_calendar`.

What is **new**:

- `google_calendar.py` source file.
- `backend_name = "google_calendar"` (matches sibling convention:
  `gmail.py` → `"gmail"`, `gdrive_documents.py` → `"google_drive"`).
  The `GoogleAuthBackend` uses `backend_name="google"` in the
  `AuthBackend` registry — that's a separate registry so there is no
  technical collision, but using `"google_calendar"` here keeps the
  registry name, the `plugin.yaml` `provides` entry, and the `_id`
  prefix in events all aligned, and keeps the SPA's three "google"
  drawers from looking identical to a confused user.
- Plugin metadata gains `"google_calendar"` in `provides`.

What we **do not reuse**:

- The `GoogleAuthBackend` OAuth-redirect flow (web sign-in via ID
  token). That's for **users logging into Gilbert via Google**; it has
  no overlap with **Gilbert's calendar backend reading a user's
  calendar via service account**. The two flows live in the same
  plugin but use different credentials and different scopes.

##### `registered_backends()` registration

`std-plugins/google/plugin.py` updated:

```python
async def setup(self, context: PluginContext) -> None:
    from . import (  # noqa: F401
        gdrive_documents,
        gmail,
        google_auth,
        google_calendar,   # <- new
        google_directory,
    )
```

`plugin.yaml` `provides` list gains `google_calendar`. README per-plugin
detail section gains the new backend.

### Multi-Backend Aggregation

For v1, multi-backend selection happens at the **account** level: each
account names its `backend_name`. Within one account there is exactly
one backend. The CalendarService aggregates across accounts at the
**service level** by accepting `account_id=None` on every read tool
and looping over `self._runtimes` filtered by access.

This matches the inbox model exactly. We deliberately avoid a "primary
calendar backend" config knob — that only made sense for backends like
TTS where the consumer has no natural account boundary.

### Multi-User & RBAC

#### Identity sourcing pattern

- **All public methods** (reads and mutations) take `user_ctx:
  UserContext` as an explicit parameter — the rule from
  `memory-multi-user-isolation.md` § "For caller identity in tool
  arguments." Tool handlers read injected `_user_id` / `_user_roles`
  from `tc.arguments`, build a `UserContext`, and pass it through.
  WS RPC handlers build the `UserContext` from the connection's
  authenticated session. Internal callers (poll loop, sweep job)
  pass `UserContext.SYSTEM`.
- The service's public method bodies do **not** call
  `get_current_user()`. Reading `_current_*` global state across
  `await` boundaries is exactly the failure mode the multi-user
  memory documents.

#### Required roles

- All read tools: `required_role="user"`.
- All mutating tools (`create_event`, `update_event`, `delete_event`):
  `required_role="user"` (further gated by `can_access_account`
  inside the service before any backend call).
- WS RPCs ACL prefix in `interfaces/acl.py`: register `calendar.` at
  level 100 (`user`). **Important**: this prefix-level gate admits
  every authenticated user to *any* `calendar.*` frame; per-handler
  checks (`can_access_account` / `can_admin_account`, see "Per-handler
  authorization" above) are what actually enforce account-level
  authorization. The pattern mirrors inbox.

#### Per-collection ACLs

The `calendar_accounts` collection holds **shared-but-not-tenant**
data: one row per account, with explicit owner + share lists. The
default collection ACL (`read=user`, `write=admin`) doesn't fit
because non-admins need to read accounts shared with them, and the
generic `/entities` browser doesn't know about per-account sharing.
Resolution: **lock both calendar collections to `read=admin,
write=admin`** at the collection-ACL layer (matches inbox's
`mailbox_messages` posture), and route every read for non-admin
users through service methods that consult `can_access_account` /
`can_admin_account`. Without this, a curious user opening
`/entities` sees other users' calendar event titles —
`calendar_events` titles can contain medical, legal, or HR-sensitive
text. Service-mediated access only.

#### Sensitive backend_config and at-rest plaintext

Service-account JSON in `backend_config` is `sensitive=True`, which
masks it in WS responses and the SPA, but `sensitive` is **not
encryption** — the JSON sits in plaintext SQLite at
`.gilbert/gilbert.db`. This is a project-wide gap (Gmail does the
same) that this spec inherits and does **not** fix. Mitigations
documented in the std-plugins README:

- File-permission hardening on `.gilbert/gilbert.db` (mode `0600`,
  owned by the running user).
- Recommendation to scope service-account keys to the minimum users
  needed and rotate periodically.
- Open Question #11 below tracks whether storage-layer encryption is
  on the project roadmap; if so, this spec defers; if not, the gap
  is acknowledged and not silently shipped.

#### Ownership model

- Accounts are owned by the user who created them (`owner_user_id`).
- Events created via `create_event` are owned by the account; we don't
  separately track per-event ownership inside the cache.

#### Shared calendars

Sharing an *account* = full access (read all events, free-busy, create
new events on the calendar). This matches inbox-mailbox semantics — full
access means full control. Finer-grained "read-only share" is deferred;
add later by widening `shared_with_users` from `list[str]` to
`list[{user_id, mode}]` if needed.

### Configuration

#### Service-level ConfigParams

See `config_params()` above: `enabled`, `default_event_lookahead_days`,
`upcoming_announce_minutes`. **No `ai_prompt=True` params** — the
service performs no AI calls.

#### Per-account ConfigParams (live on `CalendarAccount` rows, not in YAML)

`calendar_id`, `timezone`, `working_hours_start_hour`,
`working_hours_end_hour`, `poll_enabled`, `poll_interval_sec`,
`upcoming_event_lookahead_minutes`, `shared_with_users`,
`shared_with_roles`. Plus the backend-specific block (`backend_config`)
rendered dynamically from `CalendarBackend.backend_config_params()`.

The `calendar_id` field is **not** wired through the standard
`ConfigParam(choices_from=...)` machinery — that mechanism (see
`memory-configuration-service.md` and the existing `inbox_mailboxes`
choice source in `core/services/configuration.py`) reads from a
sync-cached snapshot. The Calendar id-list comes from a *live*
backend probe and only makes sense after the account is persisted.
Resolution: the calendar-id field is rendered by the
`AccountEditDrawer.tsx` component as a custom dropdown that calls
the `calendar.accounts.probe_calendars` WS RPC directly. The
component shows a "Save credentials and probe" button until the
account has been persisted with `poll_enabled=False`; once saved,
the probe runs and the dropdown populates. This avoids inventing a
new `choices_from` source and stays in line with how
`memory-multi-backend-pattern.md` handles backend-specific UI.

If, in a future spec, we want every backend's "select-from-list"
field to share the same machinery, we'll add a typed `probe_action`
on `ConfigParam` — out of scope for this PR.

#### AI prompts

**The CalendarService itself has zero AI prompts.** No
`ConfigParam(ai_prompt=True)` declarations are needed because no
`complete_one_shot` / `chat` calls happen anywhere in the service.

If a future PR adds AI rewriting (e.g., "summarize my next meeting"),
each prompt MUST be exposed as `ConfigParam(multiline=True,
ai_prompt=True)` per `memory-ai-prompts-configurable.md`.

#### Bootstrap defaults

No bootstrap `gilbert.yaml` section. All calendar config lives in entity
storage. Same decision the inbox redesign made.

### Events

| Event type | Published by | Subscribed by (planned consumers) | Data |
|---|---|---|---|
| `calendar.event.upcoming` | poll's `_emit_upcoming_for_account` | greeting (future PR), agent | `{account_id, event_id, title, start, location, attendee_emails, organizer_email, owner_user_id}` |
| `calendar.event.created` | poll diff + service mutate path | UI react-query invalidation | `{account_id, event_id, title, start, end}` |
| `calendar.event.updated` | poll diff + service mutate path | UI | `{account_id, event_id, title, start, end}` |
| `calendar.event.deleted` | poll diff + service mutate path | UI | `{account_id, event_id}` |
| `calendar.account.created` | account CRUD | UI | `{account_id, name, owner_user_id}` |
| `calendar.account.updated` | account CRUD | UI | `{account_id, name, owner_user_id}` |
| `calendar.account.deleted` | account CRUD | UI | `{account_id}` |
| `calendar.account.shares.changed` | sharing mutators | UI (cache invalidation) | `{account_id, owner_user_id, shared_with_users, shared_with_roles}` |

The service does not subscribe to any events itself — it's a leaf
publisher. Future greeting-integration PR will subscribe to
`calendar.event.upcoming`.

#### New WebSocket frame types

`web/ws_protocol.py` already routes events generically through the
event-bus → WS fanout filter. We add a per-frame `account_id`
visibility check identical to the per-mailbox check. No new frame
*types* — the existing `event` frame envelope carries the new
`calendar.*` event types as data.

The frontend `useCalendarApi()` hook subscribes to:
- `calendar.event.created/updated/deleted` (filtered to caller's
  accessible accounts).
- `calendar.account.*` (filtered to admin-able accounts where relevant).

### AI Tools

(See "Tools exposed" above for the full list with parameters.)

#### Descriptions (the AI reads these literally)

- **`list_calendar_accounts`** —
  > List every calendar account the current user can access. Returns each
  > account's id, display name, email address, the calendar id within it,
  > the user's local timezone for that account, the account's health
  > (`ok` / `unhealthy`), and how access was granted (one of: `owner`,
  > `admin`, `shared_user`, `shared_role`). Call this first when the user's
  > intent doesn't already name a specific account, or when the user reports
  > "calendar isn't working" so you can check `health`.
- **`get_schedule`** —
  > Return the user's events for a specific day or date range across one or
  > all accessible calendar accounts. Defaults to today in the account's
  > timezone. Pass `date` for a single day (today / tomorrow / yesterday or
  > an ISO date), or pass `start` and `end` for a range — useful for "what's
  > on this week." Times in the response are ISO 8601 with the account's
  > timezone offset; render in the user's local timezone when summarizing.
  > Call `list_calendar_accounts` first if you don't know the account_id and
  > the user has more than one account. Returns `events` and a `warnings`
  > array — `warnings` is non-empty when one or more accounts in the
  > aggregate failed to fetch; mention the warning to the user rather than
  > pretending the account doesn't exist.
- **`next_event`** —
  > Return the next single calendar event whose start time is in the future,
  > optionally limited to a window of the next N hours (default 72 — covers
  > tonight and tomorrow without a retry). Use this when the user asks
  > "what's next" or "do I have anything this afternoon." Returns ANY event
  > including all-day events and solo events ("dentist appointment", "focus
  > block"), not only multi-attendee meetings. Pass `within_hours=null` for
  > unlimited; `0` is **not** a sentinel for unlimited and will return null.
  > Times in the response are ISO 8601 with the account's timezone offset.
- **`get_event`** —
  > Fetch one calendar event by id with full detail — title, start, end,
  > description, location, attendees with their response statuses, and
  > html_link to view it in the provider's UI. Use this after
  > `get_schedule` or `next_event` returns an event id when the user asks
  > "tell me about that meeting" or wants attendee details.
- **`find_free_time`** —
  > Find free intervals of at least `duration_minutes` inside a window.
  > By default the window is the next 7 days during working hours
  > (configurable per account). Pass `attendee_emails` to find a time
  > when the user AND those attendees are all free — visibility into other
  > calendars depends on the other party's sharing settings; if Gilbert
  > can't read someone's calendar the response includes a warning, and you
  > should tell the user. "Free" means: events with status `confirmed` or
  > `tentative` count as busy (we don't suggest "free" slots over a
  > tentative meeting); events with status `cancelled` or transparency
  > `transparent` (e.g. Google "Free" / "Working location") count as free;
  > all-day events are treated as fully busy across the working-hours
  > window. Use this when the user asks "when am I free for X minutes" or
  > "when can Sarah and I meet for 30 minutes." Times in the response are
  > ISO 8601 with the account's timezone offset; returns up to
  > `max_results` candidate slots ranked earliest first.
- **`create_event`** —
  > Create a new calendar event on a specific account. **High blast
  > radius**: this can send real invite emails. The tool defaults to
  > preview mode (`confirm=False`): you'll get a confirmation form back —
  > only after the user clicks Confirm should you re-call with the SAME
  > arguments plus `confirm=True`. If the user gave you a relative time
  > ("tomorrow", "next Tuesday", "after lunch"), call `system_datetime`
  > FIRST to anchor "now," then compute the ISO string in the account's
  > timezone — DO NOT guess today's date. Pass either `end` (ISO datetime)
  > OR `duration_minutes` (integer) — exactly one is required. If the user
  > references attendees by name, resolve to email first via
  > `directory_search` (from the google_directory plugin) — DO NOT
  > hallucinate email addresses. `send_invites` defaults to false; only
  > set to true when the user explicitly says "invite them" or similar.
  > Naive datetimes (no timezone) are interpreted in the account's
  > timezone. Recurring phrases like "every Tuesday at 3" are NOT
  > supported — ask the user to set repeats up manually for now. On
  > success the response includes `html_link`; include it in your reply
  > so the user has a click-through.
- **`update_event`** —
  > Modify an existing calendar event by id. **High blast radius**: same
  > preview/confirm pattern as `create_event` — first call with
  > `confirm=False` returns a delta preview (old → new per field); only
  > after the user clicks Confirm should you re-call with `confirm=True`.
  > Only fields you supply are modified. For events that are part of a
  > recurring series (`recurring_event_id` set on the fetched event), this
  > modifies only the SINGLE INSTANCE, not the whole series — tell the
  > user so they're not surprised. If another client edits the event
  > between your read and your write, you'll get an error telling you to
  > re-fetch with `get_event` and try again.
- **`delete_event`** —
  > Delete an existing calendar event by id. **High blast radius**: same
  > preview/confirm pattern as `create_event`. `send_cancellations`
  > defaults to false; set to true when the user says "tell everyone it's
  > cancelled" or similar — otherwise attendees are not emailed. For
  > recurring instances, this deletes only the single instance.

#### Built-in profile inclusion

**No change to the seeded built-in profiles.** All five calendar
tools are available in `light`, `standard`, and `advanced` because
all three seeded profiles use `tool_mode="all"` (per
`_BUILTIN_PROFILES` in `core/services/ai.py`, lines 1117–1133). The
previous draft proposed mutating the `light` seed to
`tool_mode="exclude"` and stuffing `find_free_time` / `create_event`
into its `tools` list — but the seed only fires on first start, and
`_UNDELETABLE_PROFILES = {"light", "standard", "advanced"}` means
existing deployments would retain their persisted `tool_mode="all"`
profile and get the tools anyway. Worse, that change would couple a
single calendar feature to a cross-cutting policy change ("what
`light` means"). If/when product wants to retighten what `light`
allows, that's a separate spec.

How the safety properties are still met without mutating `light`:

1. **Greeting (and any pure-text caller)** uses
   `complete_one_shot(tools_override=[])`, which forces zero tools
   regardless of which profile selected the backend/model. The
   calendar tools are not visible to the morning greeting because no
   tool is. (`memory-ai-context-profiles.md` § "Pure-Text Calls Force
   Zero Tools at the Call Site" documents this.)
2. **Mutating tools (`create_event`, `update_event`, `delete_event`)
   default to preview/confirm mode** (`confirm=False` returns a
   `UIBlock` and does NOT mutate). The model cannot fire a real
   invite from a quick `light`-tier exchange — the user has to click
   Confirm in the SPA.
3. **`send_invites` defaults to false** at the tool layer, so even
   on a confirmed mutation, no third party gets emailed unless the
   model explicitly opted in.

The combination is stronger than profile-level filtering would have
been: a profile-level exclude wouldn't have prevented the `standard`
tier from firing real invites, while preview/confirm + send_invites
opt-in does. If a future PR wants per-tier filtering on top of
this, it can do so by introducing **custom** profiles (as
`memory-ai-context-profiles.md` envisions), without touching the
seeded built-ins.

| Tool | `light` | `standard` | `advanced` | Rationale |
|---|---|---|---|---|
| `list_calendar_accounts` | ✓ | ✓ | ✓ | Read-only, no blast radius. |
| `get_schedule` | ✓ | ✓ | ✓ | Read-only. |
| `next_event` | ✓ | ✓ | ✓ | Read-only. |
| `get_event` | ✓ | ✓ | ✓ | Read-only. |
| `find_free_time` | ✓ | ✓ | ✓ | Read-only. |
| `create_event` | ✓ (preview only via `confirm=False`) | ✓ | ✓ | Mutating; preview/confirm protects against blast. |
| `update_event` | ✓ (preview only via `confirm=False`) | ✓ | ✓ | Same. |
| `delete_event` | ✓ (preview only via `confirm=False`) | ✓ | ✓ | Same. |

### UI / Frontend

#### New SPA pages / panels

- **`/calendar` page** (`frontend/src/components/calendar/CalendarPage.tsx`)
  — sidebar of accessible accounts, an agenda/week view of upcoming
  events, a "create event" drawer. Wired to `useCalendarApi()`.
- **Account edit drawer** — uses the shared `ConfigField` component
  to render `backend_config_params()` for the selected backend (same
  pattern the inbox-mailbox drawer uses), plus the per-account fields
  (`calendar_id` dropdown, `timezone`, working hours, poll cadence,
  share lists).
- **Settings extension panel** — calendar accounts edit UI mounted
  inside the standard `settings.calendar` slot via the existing
  `<PluginPanelSlot>` infrastructure. (Core service, but uses the same
  slot machinery so calendar-related plugin contributions can attach.)

#### Settings UI

Service-level `config_params()` (`enabled`,
`default_event_lookahead_days`, `upcoming_announce_minutes`) are
auto-rendered from the standard Settings page. The complex per-account
UI is custom, exactly like inbox-mailboxes.

#### Plugin UI extension slot

Existing slots used:
- `dashboard.top` — adds an "Up next" widget showing the user's
  next event across all accessible accounts. (Optional, behind a
  config knob `calendar.show_dashboard_card` defaulting to true.)

No new core-side slots are required for v1.

#### WS RPCs called from the UI

`useCalendarApi()` hook in `frontend/src/hooks/useCalendarApi.ts`
exposes typed wrappers around every `calendar.*` RPC listed in "WS RPC
handlers." Per the architecture rules, this hook lives in core (not in
a plugin) because the calendar service is core. Backend-specific UI
remains generic — the `CalendarAccount` editor renders any
`backend_config_params()` schema dynamically.

### Dependencies

- **Python packages added**: none. `google-auth` and
  `google-api-python-client` are already in
  `std-plugins/google/pyproject.toml` for Gmail/Drive; the same client
  serves the Calendar API (`build("calendar", "v3", credentials=...)`).
- **OS-level deps via `runtime_dependencies()`**: none. No new
  binaries; the API call goes over HTTPS.
- **External APIs**: Google Calendar API v3
  (`https://www.googleapis.com/auth/calendar` and
  `https://www.googleapis.com/auth/calendar.events` scopes).
  Domain-wide delegation must be configured for the service account
  with these scopes added in the Google Workspace admin console.
  Document this in the std-plugins README's Google detail section.

## Tool Profile Integration

**No changes to the seeded built-in profiles.** All eight calendar
tools are available in every built-in profile (`light`, `standard`,
`advanced`) because all three are seeded with `tool_mode="all"` and
this spec deliberately does not flip that. Safety properties
(no-blast-radius from `light`-tier callers, no surprise invites)
come from:

1. The preview/confirm `UIBlock` pattern on every mutating tool
   (`confirm=False` returns a form and never touches the backend).
2. `send_invites=False` default at the tool layer (the AI must
   explicitly opt in).
3. Pure-text callers (greeting, roast) using
   `complete_one_shot(tools_override=[])`, which forces zero tools
   regardless of profile.

If a future spec wants per-tier filtering of mutating tools on top of
this, it should do so by introducing **custom** profiles, not by
mutating the seeded ones. The seeded profiles are documented as
"undeletable defaults" — silently changing what they include
violates the contract documented in `memory-ai-context-profiles.md`.

No new profiles are added by this spec. No `ai_call` assignment is
added (the calendar service makes no AI calls).

## Migration / Compatibility

### Existing code touched

1. **`src/gilbert/core/app.py`** — register `CalendarService()` in the
   service-manager registration block. Insert next to `InboxService`
   so the start order is similar. **`app.py` is the only place that
   imports the concrete `CalendarService`** (composition-root rule);
   any other module needing the service consumes the
   `CalendarProvider` capability via `resolver.get_capability("calendar")`.
2. **`src/gilbert/interfaces/acl.py`** — register the `calendar.`
   event prefix at level 100 (user) in `DEFAULT_EVENT_VISIBILITY`,
   register `"calendar.": 100` in `DEFAULT_RPC_PERMISSIONS`, and
   register `calendar_accounts` / `calendar_events` /
   `calendar_event_announcements` collections with `read=admin,
   write=admin` (service-mediated reads only — see "Per-collection
   ACLs" above).
3. **`src/gilbert/core/services/web_api.py`** — add a Calendar nav
   group entry to `_ws_dashboard_get`'s `nav_groups` list (per
   `memory-dashboard-nav.md`). Nav entries come from this RPC, **not**
   from a hard-coded TS file. The entry declares
   `requires_capability="calendar"` so a deployment with the
   calendar service disabled doesn't show the nav button.
4. **`std-plugins/google/plugin.py`** — add `google_calendar` to the
   `setup()` side-effect import block.
5. **`std-plugins/google/plugin.yaml`** — add `google_calendar` to
   `provides`.
6. **`std-plugins/google/pyproject.toml`** — no change required (the
   Google client is already a dep). **Note on `tzdata`**: the service
   relies on `zoneinfo.ZoneInfo` for IANA tz resolution. On Linux,
   `zoneinfo` reads from `/usr/share/zoneinfo` (provided by the
   `tzdata` package on most distros). On Alpine/musl, `tzdata` is not
   installed by default and `ZoneInfo("America/New_York")` raises
   `ZoneInfoNotFoundError`. Add a `RuntimeDependency` entry on the
   plugin via `Plugin.runtime_dependencies()` checking for any IANA
   zone resolution at boot, with a friendly error message pointing to
   `apk add tzdata` (or distro-equivalent). Alternatively, depend on
   the PyPI `tzdata` package by adding it to the plugin's
   `pyproject.toml` (cross-platform and self-contained — recommended,
   adds ~700KB).
7. **Frontend `App.tsx`** — register the `/calendar` route.
8. **`README.md` (root)** — add Calendar to the integration list and
   update the "what Gilbert can do" section.
9. **`std-plugins/README.md`** — update the Google plugin's "Provides"
   row and detail section to include `google_calendar`, document the
   additional OAuth scopes required for the service account, and
   document the at-rest plaintext caveat for service-account JSON.
10. **`.claude/memory/MEMORIES.md`** — add an index entry.
11. **`.claude/memory/memory-calendar-service.md`** — new memory file
    summarizing the service, its events, the per-account runtime
    pattern, and the gotchas.

`src/gilbert/core/services/ai.py` is **not** touched by this spec —
the previous draft proposed mutating the seeded `light` profile and
that change is dropped (see "Tool Profile Integration" above).

### Greeting integration (deferred, but enabled)

Greeting service does **not** change in this PR. The
`calendar.event.upcoming` event is published, the
`CalendarProvider` capability is registered — both are ready to be
consumed by a follow-up PR.

**Important constraint for the follow-up PR**: greeting calls
`complete_one_shot(tools_override=[])` (text-only, zero tools). It
therefore CANNOT use the calendar AI tools. Calendar context for the
morning greeting must be:

1. Fetched **outside** the AI call by greeting itself, using
   `CalendarProvider` directly:
   `events = await calendar_provider.list_events(account_id=None,
   time_min=now, time_max=end_of_day, user_ctx=user_ctx_for_owner)`.
2. Formatted into a plain string ("Today: 9am Standup, 10am 1:1 with
   Brian, ...").
3. Injected into the greeting `system_prompt` as text via prompt
   composition (the greeting prompt is already a
   `ConfigParam(ai_prompt=True)`; the calendar block is a separate
   substitution).

The original draft suggested adding calendar tools to the `light`
profile so greeting could "be calendar-aware via tools" — that
contradicts how greeting calls AI today (`tools_override=[]`).
Calling out the conflict here so the follow-up PR doesn't
re-discover it. (Verified against
`memory-ai-context-profiles.md` § "Pure-Text Calls Force Zero Tools
at the Call Site".)

If a future spec wants AI-driven summarization of calendar data
(e.g., "you've got a packed morning, then a clear afternoon"), it
should add a `summarize_day` AI tool with a `ConfigParam(ai_prompt=
True)` system prompt — per `memory-ai-prompts-configurable.md`'s rule
that every non-trivial system prompt must be admin-tunable. That
helper does not exist in v1.

### Inbox AI integration (deferred, but enabled)

`InboxAIChatService` (`core/services/inbox_ai_chat.py`) gets the
calendar tools automatically because it discovers all `ai_tools`
providers via the standard mechanism — no code change is required.
A follow-up PR can add a more specific "schedule from email" UI affordance.

### Scheduler integration (deferred)

A future PR can teach the `set_alarm` tool to optionally also create
a Google Calendar event by calling `calendar.create_event` so the
alarm shows up everywhere. For now, the two systems are independent.

### Backwards compat

No existing user-visible behavior changes. The new service starts
with zero accounts and is invisible until an admin creates one.

### DB migrations

None — entity storage is schemaless. Collection naming is final
(`calendar_accounts`, `calendar_events`,
`calendar_event_announcements`); rename costs a future migration if
ever needed.

## Testing Strategy

### Test files

- `tests/unit/test_calendar_service.py` — `CalendarService` unit tests
  with a `FakeCalendarBackend` (in-memory, deterministic, supports
  simulated etag conflicts and idempotency dedup so the
  optimistic-concurrency and retry tests can exercise real behavior).
  Real SQLite for storage. Mocks: scheduler (use a
  `RecordingScheduler` test helper that records `add_job` calls,
  exposes the recorded *initial-fire offset* so the jitter
  requirement can be asserted, and exposes a manual `fire(name)` to
  trigger callbacks), event bus (use the in-process
  `InMemoryEventBus` directly to count published events).
- `tests/unit/test_calendar_interfaces.py` — pure-data tests for
  authorization helpers (`can_access_account`, `can_admin_account`,
  `determine_access`). Cover the access matrix: admin / owner /
  shared user / shared role / no-access for a few `UserContext`
  shapes; verify `is_admin` is correctly derived from `user_ctx.roles`
  by helper internals (no caller-supplied bool path exists anymore).
- `std-plugins/google/tests/test_google_calendar.py` — unit tests for
  `GoogleCalendarBackend`'s payload mapping (Google event JSON →
  `CalendarEvent`, `EventCreateRequest` → Google API body, all-day
  edge cases, recurring-instance flag, transparency mapping, etag
  forwarding, error mapping for each `HttpError(status, reason)`
  combination → exception subclass). Mocks the `googleapiclient`
  `service` object with a `MagicMock` that records
  `.events().list().execute()` etc. invocations and returns canned
  fixtures.

### Real vs. mocked

Per the user's CLAUDE.md ("don't mock the thing you're supposed to be
testing"):

| Test file | Mocked | Real |
|---|---|---|
| `test_calendar_service.py` | The `CalendarBackend` (use `FakeCalendarBackend` test double — same approach inbox tests use with a fake `EmailBackend`); the AI service (not invoked anyway). | SQLite storage backend, in-memory event bus, `RecordingScheduler` test helper, `CalendarService` itself (the thing under test). |
| `test_calendar_interfaces.py` | Nothing — pure dataclass tests. | Everything. |
| `test_google_calendar.py` | The Google API client (`googleapiclient.discovery.build` returns a `MagicMock`). The OAuth credential constructor (returns a `MagicMock`). | `GoogleCalendarBackend` (the thing under test), all payload-mapping logic. |

### Edge cases to cover

1. **Timezone correctness**:
   - Event with `start.dateTime="2026-05-09T10:00:00-05:00"` (CDT)
     comes back as a tz-aware datetime in `America/Chicago` and is
     rendered to a user in `America/Los_Angeles` correctly.
   - Account `timezone="America/New_York"` with a naive `start`
     passed to `create_event` — service localizes to
     `ZoneInfo("America/New_York")` at the tool→service boundary
     before constructing `EventCreateRequest`. The dataclass field
     is always tz-aware.
   - Account with invalid `timezone` (typo, deleted Olson zone) —
     `update_account` validates via `ZoneInfo(value)` and raises
     `ValueError` if it fails; first-use does the same defensive
     check and raises a clear `RuntimeError` rather than silently
     falling back to UTC.
   - "Today" in `get_schedule` resolves against the *account's* tz,
     not the server's tz, not the user's browser tz.
2. **All-day events**:
   - Google Calendar returns `{"start": {"date": "2026-05-09"}, ...}`;
     backend must treat as 00:00 → 24:00 local in the account's tz and
     set `all_day=True`. `find_free_time` post-processes
     `FreeBusyBlock`s by intersecting with the account's working-hours
     window so a midnight-to-9 "free" window doesn't bleed in front
     of the working day.
3. **Recurring events**:
   - `single_events=True` is always passed; we get instances back.
   - `update_event` and `delete_event` only operate on the single
     instance id passed in — no series-wide semantics. Tool
     description warns the AI; WS RPC documents the limitation.
   - When `update_event` or `delete_event` is invoked on an event
     with `recurring_event_id != None`, the service logs at INFO with
     `event_id`, `recurring_event_id`, and the operation, so an
     operator can audit "did the AI just touch a recurring instance?".
4. **Missing OAuth / bad credentials**:
   - `initialize()` swallows errors and logs (matches `gmail.py`); the
     backend stays in an uninitialized state. Subsequent `list_events`
     calls raise `CalendarBackendAuthError` which the poll loop
     converts into `account.health="unhealthy"` after
     `unhealthy_failure_threshold` consecutive failures, and emits
     `calendar.account.health_changed`. The SPA shows a red badge.
   - `test_connection` action surfaces the error to the UI clearly.
5. **Event with no end time** (rare; some providers send only `start`):
   - Backend defaults `end = start + 1h` and logs at WARNING with
     event_id.
6. **Cancelled events** — `status="cancelled"`:
   - Filtered out of the diff's `fresh` set BEFORE the diff runs (see
     "Polling logic" step 4), so a cancelled event is treated as
     "missing" and emits `calendar.event.deleted` exactly once.
   - Excluded from `get_schedule` / `next_event` results.
7. **Account deletion with active poll**:
   - `delete_account` cancels the poll job before deleting the row;
     also deletes all `calendar_events` rows where
     `account_id == id` and all `calendar_event_announcements` rows.
     Mirror `delete_mailbox` for the runtime-cancel half; the cascade
     delete is calendar-specific because entity storage doesn't enforce
     FKs across collections.
   - In-flight `create_event` against a deleting account: the runtime
     is removed first, so any concurrent `create_event` raises
     "account not found" cleanly. No half-mutate state.
8. **Sharing precedence**:
   - User is owner AND admin → `determine_access` returns `OWNER`.
   - User is admin AND in shared list → returns `ADMIN` (admin wins
     over shared).
   - User is admin AND owner of a different account → handled
     correctly per call.
9. **Concurrent `create_event` from two users sharing one account**:
   - Service must not race on cache invalidation. The poll handles
     eventual consistency; the explicit mutate path emits the
     `calendar.event.created` event immediately after the backend
     returns success.
   - Idempotency keys produced by two simultaneous logically-different
     creates have different inputs (different `start`/`title` etc.)
     so they don't collide. Same logical create from the same caller
     (retry) produces the same key and Google deduplicates.
10. **`find_free_time` with empty calendar**:
    - Returns slots covering the entire requested window, clamped to
      working hours, capped at `max_results`.
11. **`find_free_time` across multiple accounts** — busy intervals are
    unioned; free = window minus union; working-hours is intersected
    across the participating accounts (most-restrictive wins).
12. **`find_free_time` invalid arguments**:
    - `duration_minutes < 5` or `> 480` → `ValueError` with the
      acceptable range in the message.
    - `start >= end` → `ValueError`.
    - `duration_minutes > (end - start).total_seconds() / 60` →
      `ValueError("requested duration exceeds search window").
    - `working_hours_start_hour >= working_hours_end_hour` on the
      account → `ValueError` raised at account-create/update time, so
      a misconfigured account is rejected at write rather than
      silently producing zero slots.
13. **Aggregate read with one unhealthy account**:
    - Three accounts, one returns `CalendarBackendAuthError` on
      `list_events` → other two still produce events; the failed
      account contributes a string like `"calendar 'work' failed:
      authentication expired"` to the result envelope's `warnings`.
      Test asserts (a) the call returns within
      `aggregation_timeout_sec`, (b) the warnings list is non-empty,
      (c) the events from the other two are present.
14. **First poll after process restart**:
    - Persisted cache for account A contains 5 future events. After
      restart, `_start_runtime` creates the runtime with empty
      `last_seen_event_ids`; the first poll lazy-seeds it from the
      cache (step 2 of "Polling logic"). No `calendar.event.created`
      events fire for the 5 pre-existing events.
    - Test seeds the cache, instantiates a fresh service, fires the
      poll callback once via `RecordingScheduler.fire(...)`, asserts
      that no `calendar.event.created` events were published.
15. **Mutation publish dedup**:
    - `create_event` publishes `calendar.event.created`; the next
      `_poll_runtime` immediately after returns the new event in
      `fresh` but the diff suppresses re-publication because
      `recent_mutate_publishes` carries the id. Test counts published
      events and asserts exactly one.
16. **etag conflict on update**:
    - `update_event` is invoked with a stale `if_match_etag`; the
      backend's `events.patch` raises `HttpError(412)` →
      `CalendarBackendConflictError` → tool returns a clear "the
      event changed since you fetched it" error string. Test asserts
      no partial mutation visible (the original event is unchanged).
17. **Idempotency on retry**:
    - `create_event` is called twice with identical args; the
      service computes the same `idempotency_key`, both calls reach
      the backend with the same `requestId`, only one event is
      created. Test uses a `FakeCalendarBackend` that simulates
      Google's dedup behavior.

## Open Questions / Risks

Items moved out of "Open Questions" into the spec body (no longer
optional): cold-start polling jitter (now mandatory in
`_start_runtime`); timezone string validation on account write (now
explicit in account-CRUD edge case 1); calendar-id dropdown (now
explicit in the per-account ConfigParam section).

1. **Recurring-event editing**: ABC currently treats series as
   read-only. Real users will eventually want "delete this and all
   following." Defer to v2 — needs a `recurrence_scope: "instance" |
   "future" | "all"` parameter on `update_event` / `delete_event` and
   backend-side support for Google's recurring-instance semantics.
2. **Working-hours per day-of-week** — current model is a single
   start/end hour pair. Friday-half-day, weekend-on-call etc. are
   common. Defer to v2.
3. **Confirmation pattern standardization** — should the
   preview/confirm `UIBlock` pattern proposed here be hoisted to a
   shared helper used by inbox `inbox_send`, future agent actions,
   etc.? Inbox today fires invites/notifications without
   confirmation, which has the same blast-radius problem. **Reviewer
   recommendation: standardize across mutating tools.** Leaving open
   for human resolution since it's cross-feature.
4. **Polling vs. webhooks** — webhooks would be lower-latency but
   require a public HTTPS endpoint plus a watch-channel renewal job.
   Polling 5min is "good enough" for v1 but worth measuring.
5. **OAuth refresh** — Google service-account creds don't expire the
   way user OAuth tokens do, so the refresh story matches Gmail's
   (none required). If we ever add user-OAuth-based credentials
   instead of service accounts, we'll need a refresh-token store.
6. **Concurrent edits on the same `CalendarAccount`** — two admins
   editing the same account row: last-write-wins is the default for
   entity storage. Acceptable for v1 (shared-account admin churn is
   low). If product wants optimistic concurrency on the account row,
   add a `version: int` field and check on update.
7. **Trust boundary for "WS-only"** — if the agent service eventually
   drives WS RPCs, the WS-vs-tool fence isn't a real isolation
   boundary. We've collapsed the distinction by exposing
   `update_event` / `delete_event` as AI tools (with preview/confirm)
   in v1, so this is mooted for the calendar feature itself. Flag for
   inbox spec authors who still rely on WS-only as a safety story.
8. **Sensitive backend_config plaintext at rest** — service-account
   JSON sits in plaintext SQLite. Acknowledged in the spec as a
   project-wide gap (Gmail does the same). **Open question for the
   human**: is encryption-at-rest for `backend_config` being
   addressed at the storage layer in a separate effort? If yes, this
   spec defers; if no, the std-plugins README documents the gap and
   recommends file-permission hardening on `.gilbert/gilbert.db`.
9. **Cross-user free-busy quality** — `find_free_time` ships with
   `attendee_emails` in v1. The reviewer flagged that visibility
   varies dramatically based on the other party's sharing settings
   (Google may return `errors:[{reason: "insufficientPermissions"}]`
   for a target email). The tool description tells the AI to surface
   warnings to the user; product decision pending on whether we want
   richer per-attendee visibility-status indicators in the SPA.
10. **Tentative-event semantics** — for `find_free_time`, tentative
    events count as **busy** (we don't suggest "free" slots over a
    maybe-meeting); for `get_schedule` / `next_event`, tentative
    events appear with `status: "tentative"` so the AI can summarize
    honestly ("you have a tentative 3pm with Sarah"). Differential
    semantics are intentional and pinned in the algorithm
    description, but worth confirming with product.
11. **Persona / tone for AI-generated calendar summaries** — when a
    follow-up PR adds a "summarize my day" helper, does soul/identity
    carry enough flavor or do we want a calendar-specific tone
    prompt? Probably the former; flag for the follow-up PR's design.

### Disputed reviewer items

- **`light` profile inclusion table contradiction** (product nit):
  the original draft did contradict itself by saying `light` uses
  `tool_mode=ALL` while excluding two tools. The revised spec drops
  the entire profile-mutation idea (see Tool Profile Integration);
  the contradiction goes away by deletion.

## Implementation Plan (Step-by-Step)

In dependency order — each step is independently testable.

1. **Interfaces**: create `src/gilbert/interfaces/calendar.py` with
   the dataclasses, `CalendarBackend` ABC, `CalendarProvider` and
   `CachedCalendarLister` Protocols, and the three authorization
   helpers. Add `__all__`.
2. **ACL prefixes**: add `calendar.` event prefix at level 100 and
   `calendar_accounts` / `calendar_events` collection ACLs in
   `src/gilbert/interfaces/acl.py`.
3. **Interface tests**: add `tests/unit/test_calendar_interfaces.py`
   covering the access matrix and dataclass round-trips.
4. **Service skeleton**: create `src/gilbert/core/services/calendar.py`
   with `CalendarService` — `service_info`, `start`, `stop`,
   `_boot_runtimes`, `_start_runtime`, `_stop_runtime`,
   `_restart_runtime`, `cached_accounts`. No tools, no events yet.
5. **Service: account CRUD + sharing**: `create_account`,
   `update_account`, `delete_account`, `share_user`, `unshare_user`,
   `share_role`, `unshare_role`, `list_accessible_accounts`,
   `get_account`, `test_account_connection`. Publish `calendar.account.*`
   events. Match `InboxService` API surface.
6. **Service: read methods**: `list_events`, `next_event`,
   `free_busy`, `find_free_time`. Implement against a
   `FakeCalendarBackend` test double.
7. **Service: mutation methods**: `create_event`, `update_event`,
   `delete_event`. Publish `calendar.event.*` events.
8. **Service: polling + caching**: `_make_poll_callback`,
   `_poll_runtime`, `_persist_event`, diff-based event publish, the
   `calendar_event_announcements` dedup, the
   `_emit_upcoming_for_account` helper.
9. **Service: AI tools**: `get_tools()` returning the 8
   `ToolDefinition`s (`list_calendar_accounts`, `get_schedule`,
   `next_event`, `get_event`, `find_free_time`, `create_event`,
   `update_event`, `delete_event`). `execute_tool()` dispatching to
   the read / mutate methods. Convert tool args ↔
   `EventCreateRequest`. Implement the preview/confirm `UIBlock`
   pattern for the three mutating tools — when `confirm=False`,
   return a `ToolOutput` with a confirmation form; when
   `confirm=True`, call the underlying service mutate method.
10. **Service: ConfigParams**: `config_namespace`, `config_category`,
    `config_params` (eight params per the table above), `on_config_changed`.
    No `ai_prompt=True` params.
11. **Service: WS RPCs**: `get_ws_handlers` returning every
    `calendar.*` handler, plus `calendar.backends.list` and
    `calendar.accounts.probe_calendars`. Each handler MUST do its own
    `can_admin_account` / `can_access_account` check before any
    side-effect — see "Per-handler authorization" above.
12. **Service unit tests** (`tests/unit/test_calendar_service.py`):
    real SQLite, fake backend, recording scheduler, in-memory event
    bus. Cover all 17 edge cases above. Specifically include:
    no-republish-on-restart (edge 14), mutation publish dedup (edge
    15), etag conflict (edge 16), idempotency (edge 17), and the
    aggregate-with-failure (edge 13).
13. **Wire into `app.py`**: register `CalendarService()` next to
    `InboxService()`. **`app.py` is the only module that imports
    `CalendarService` directly** — composition-root rule.
14. **Google backend**: create
    `std-plugins/google/google_calendar.py` with
    `GoogleCalendarBackend`. Implement every ABC method. Wire the
    `test_connection` action.
15. **Google plugin metadata**: update
    `std-plugins/google/plugin.py` (add `google_calendar` import),
    `plugin.yaml` (add to `provides`).
16. **Google backend tests**:
    `std-plugins/google/tests/test_google_calendar.py` mocking the
    `googleapiclient` client.
17. **Frontend types**: `frontend/src/types/calendar.ts` with
    `CalendarAccount`, `CalendarEvent`, `EventCreateRequest`,
    `FreeSlot`, `FreeBusyBlock`, `CalendarBackendDescriptor`.
18. **Frontend hook**: `frontend/src/hooks/useCalendarApi.ts` exposing
    typed wrappers for every `calendar.*` WS RPC.
19. **Frontend components**:
    - `CalendarPage.tsx` — main `/calendar` page.
    - `CalendarSidebar.tsx` — accessible-accounts list.
    - `EventList.tsx` / `WeekAgenda.tsx` — agenda views.
    - `AccountEditDrawer.tsx` — admin-of-account edit drawer (uses
      shared `ConfigField`).
    - `CreateEventDrawer.tsx` — explicit-create drawer.
    - `UpcomingEventCard.tsx` — dashboard widget.
20. **Frontend route + nav**: register `/calendar` in `App.tsx`,
    add nav entry in dashboard.
21. **README updates**: root `README.md`, `std-plugins/README.md` (add
    `google_calendar` to the Google entry).
22. **Memory updates**: add the index line to
    `.claude/memory/MEMORIES.md`, create
    `.claude/memory/memory-calendar-service.md`.
23. **Manual smoke**: run `uv run pytest`, `uv run mypy src/`, `uv run
    ruff check src/ tests/ std-plugins/`, then end-to-end against a
    real Google account.

## File Manifest

### New files

| File | Purpose |
|---|---|
| `src/gilbert/interfaces/calendar.py` | `CalendarBackend` ABC, `CalendarProvider`/`CachedCalendarLister` protocols, dataclasses, auth helpers. |
| `src/gilbert/core/services/calendar.py` | `CalendarService` — accounts, runtimes, polling, tools, WS RPCs. |
| `std-plugins/google/google_calendar.py` | `GoogleCalendarBackend`. |
| `tests/unit/test_calendar_interfaces.py` | Access-matrix tests for the auth helpers + dataclass round-trips. |
| `tests/unit/test_calendar_service.py` | `CalendarService` unit tests against a `FakeCalendarBackend`. |
| `std-plugins/google/tests/test_google_calendar.py` | `GoogleCalendarBackend` payload-mapping tests against a mocked Google client. |
| `frontend/src/types/calendar.ts` | TypeScript types for calendar data. |
| `frontend/src/hooks/useCalendarApi.ts` | WS RPC wrapper hook. |
| `frontend/src/components/calendar/CalendarPage.tsx` | `/calendar` page shell. |
| `frontend/src/components/calendar/CalendarSidebar.tsx` | Accessible-accounts list. |
| `frontend/src/components/calendar/WeekAgenda.tsx` | Agenda view. |
| `frontend/src/components/calendar/EventList.tsx` | List view. |
| `frontend/src/components/calendar/AccountEditDrawer.tsx` | Account CRUD UI for owner/admin. |
| `frontend/src/components/calendar/CreateEventDrawer.tsx` | Manual "create event" UI. |
| `frontend/src/components/calendar/UpcomingEventCard.tsx` | Dashboard widget. |
| `.claude/memory/memory-calendar-service.md` | Memory capturing the design + per-account runtime pattern + gotchas. |

### Modified files

| File | Change |
|---|---|
| `src/gilbert/interfaces/acl.py` | Register `calendar.` event prefix at level 100, `"calendar.": 100` RPC permission, and `calendar_accounts` / `calendar_events` / `calendar_event_announcements` collections at `read=admin, write=admin`. |
| `src/gilbert/core/app.py` | `service_manager.register(CalendarService())` next to inbox. |
| `src/gilbert/core/services/web_api.py` | Add a Calendar nav group entry to `_ws_dashboard_get`'s `nav_groups` list (per `memory-dashboard-nav.md`), `requires_capability="calendar"`. |
| `std-plugins/google/plugin.py` | Add `google_calendar` to the side-effect import block. |
| `std-plugins/google/plugin.yaml` | Add `google_calendar` to `provides`. |
| `std-plugins/google/pyproject.toml` | Add `tzdata` to dependencies (cross-platform IANA zone data). |
| `frontend/src/App.tsx` | Register `/calendar` route. |
| `frontend/src/components/dashboard/DashboardPage.tsx` | Mount `<UpcomingEventCard />` (gated on `calendar.show_dashboard_card`). |
| `README.md` (root) | Add Calendar to integrations + features. |
| `std-plugins/README.md` | Update Google entry's `Provides` and detail (add `google_calendar`, document new OAuth scopes for the service account, document at-rest plaintext caveat). |
| `.claude/memory/MEMORIES.md` | Add index entry for `memory-calendar-service.md`. |

`src/gilbert/core/services/ai.py` is **not** modified (the seeded
profile-mutation idea was dropped). `frontend/src/lib/dashboard-nav.ts`
is **not** modified — nav entries come from the
`web_api._ws_dashboard_get` RPC, not a frontend constant
(`memory-dashboard-nav.md`).

## Revision Log — Round 2

### Architecture review

- **[arch.blocker.1]** Removed silent rewrite of the seeded `light`
  built-in profile. All three built-in profiles stay
  `tool_mode="all"`. Tool Profile Integration section rewritten to
  explain how safety properties are met instead via preview/confirm
  `UIBlock` + `send_invites=False` default + greeting using
  `tools_override=[]`. Migration section drops the
  `core/services/ai.py` modification.
- **[arch.blocker.2]** Removed the temp-backend-in-WS-handler pattern
  for `list_calendars`. Renamed to
  `calendar.accounts.probe_calendars`; SPA now creates the account
  with `poll_enabled=False` first, then the WS handler delegates to
  `CalendarService.probe_calendars(account_id, user_ctx)` which owns
  registry lookup, `initialize`/`close` in `try/finally`, and the
  `can_admin_account` check.
- **[arch.blocker.3]** Renamed `backend_name = "google"` to
  `backend_name = "google_calendar"`; matches `gmail.py` /
  `gdrive_documents.py` sibling convention and aligns with the
  `plugin.yaml provides` entry already named `google_calendar`.
  Added `display_name = "Google Calendar"` for SPA display.
- **[arch.blocker.4]** Tools-exposed table now has explicit
  `slash_group` / `slash_command` columns; mutating tools dropped
  their slash commands per the parser's positional-arg constraint.
  `slash_help` is required on every command (architecture checklist).
  Removed `slash_namespace` from the service class — that's a
  plugin-only concept; core services use per-tool `slash_group` only.
- **[arch.blocker.5]** Per-handler-authorization sub-section added
  under "WS RPC handlers"; explicitly states the prefix-level
  `"calendar.": 100` ACL admits any authenticated user and per-handler
  `can_admin_account` / `can_access_account` checks MUST short-circuit
  before any storage write. Mirrors the inbox precedent.
- **[arch.important.1]** Dropped `CalendarEvent.raw: dict[str, Any]`
  entirely — vendor-neutral interface should not leak the backend
  payload. Added typed `etag` / `transparency` / `EventStatus` fields
  for the things that were actually consumed.
- **[arch.important.2]** Cold-start jitter promoted from "Open
  Questions" into the lifecycle section as a hard requirement on
  `_start_runtime`. `_AccountRuntime` got a `last_seen_event_ids`
  lazy-seed step (step 2 of polling) that loads from persisted cache
  before the first diff so a process restart doesn't republish every
  event as `created`.
- **[arch.important.3]** Mutation publish dedup added: every mutate
  records `recent_mutate_publishes[event_id] = monotonic()` before
  publishing; the next poll's diff suppresses republication within
  `mutate_publish_dedup_sec` (default 60). New ConfigParam
  `mutate_publish_dedup_sec`.
- **[arch.important.4]** `choices_from="calendars"` removed —
  replaced with explicit description of the per-component
  `calendar.accounts.probe_calendars` flow in `AccountEditDrawer`.
  No new `ConfigParam` machinery is being introduced by this PR.
- **[arch.important.5]** `calendar_events` /
  `calendar_event_announcements` collections now lock to
  `read=admin, write=admin` (service-mediated only); explicit
  rationale that event titles can be sensitive and the generic
  `/entities` browser doesn't know about per-account ACLs.
- **[arch.important.6]** Timezone validation moved out of "Open
  Questions" into edge case 1 and into account-create/update edge
  case 12 — `update_account` validates `timezone` via
  `ZoneInfo(value)` at write time.
- **[arch.important.7]** Dropped `slash_namespace` from
  `CalendarService` (core service). Confirmed against `InboxService`.
- **[arch.important.8]** Multi-user state section now documents
  `_cached_accounts` invariants: atomic replacement, sync-readable,
  not source-of-truth for security-sensitive reads.
- **[arch.important.9]** Identity sourcing rewritten — every public
  method takes `user_ctx: UserContext` explicitly per
  `memory-multi-user-isolation.md` § "Required patterns." No
  `get_current_user()` in public method bodies. Tool handlers build
  `UserContext` from injected `_user_id`.
- **[arch.important.10]** `tzdata` added to plugin's
  `pyproject.toml` (cross-platform IANA zone data, addresses Alpine /
  musl gap; `RuntimeDependency` alternative also documented).
- **[arch.important.11]** Dropped `frontend/src/lib/dashboard-nav.ts`
  modification; nav entries come from
  `web_api._ws_dashboard_get` per `memory-dashboard-nav.md`.
- **[arch.nit.1]** Dropped the leftover `list_outbox`-equivalent
  comment from Multi-user state.
- **[arch.nit.2]** `events=frozenset({...})` documented as advisory
  (visibility actually flows from `interfaces/acl.py`).
- **[arch.nit.3]** `CalendarAttendee.response_status` made a
  `StrEnum` (`AttendeeResponseStatus`); `CalendarEvent.status` made a
  `StrEnum` (`EventStatus`).
- **[arch.nit.4]** Dropped `EventCreateRequest.extras: dict[str, Any]`.
- **[arch.nit.5]** Storage namespace alignment kept as-is (already
  consistent); removed the "naming is final" overly-assertive line.
- **[arch.nit.6]** Implementation Plan step 13 reaffirms the
  composition-root rule for `app.py`.

### Product / UX review

- **[product.blocker.1]** `create_event` description now explicitly
  tells the AI to call `system_datetime` before producing ISO
  datetimes. Added `duration_minutes: int | null` parameter; exactly
  one of `end` or `duration_minutes` is required.
- **[product.blocker.2]** Added preview/confirm `UIBlock` pattern to
  `create_event` (and `update_event` / `delete_event`); `confirm`
  parameter defaults to `false` and the tool returns a `ToolOutput`
  with a confirmation form when not confirmed. `send_invites` default
  flipped to `False` at the tool layer (the dataclass default was
  already changed to match).
- **[product.blocker.3]** Added `attendee_emails: list[str] | null`
  to `find_free_time`. Cross-account/cross-user free-busy ships in
  v1; description tells the AI that visibility depends on the other
  party's sharing settings, and the response surfaces `warnings` for
  insufficient-permission cases.
- **[product.blocker.4]** Renamed `today_schedule` → `get_schedule`;
  added `start` + `end` range parameters (mutually exclusive with
  `date`); description handles "what's on this week."
- **[product.important.1]** `today_schedule` → `get_schedule` and
  `next_meeting` → `next_event` (verb-first; "event" not "meeting"
  so solo events are obviously included).
- **[product.important.2]** `next_event` description clarifies the
  `[now, now + within_hours]` boundary; default raised to 72h.
- **[product.important.3]** `within_hours: int | null` (null = unlimited;
  `0` no longer a sentinel). Same treatment for any int-with-overload.
- **[product.important.4]** Added `update_event` and `delete_event`
  AI tools with mandatory preview/confirm. The previous "WS-only for
  safety" rationale didn't actually solve the safety problem;
  preview/confirm does.
- **[product.important.5]** Added `get_event` AI tool for full-detail
  fetch by id.
- **[product.important.6]** `list_calendar_accounts` description
  enumerates all four `CalendarAccess` values literally.
- **[product.important.7]** `light` profile inclusion table revised:
  no exclusions; safety from confirm-required + send_invites=False
  defaults instead. "Calendar-aware greeting" follow-up PR plan
  rewritten — greeting fetches calendar context outside the AI call
  via `CalendarProvider.list_events`, since greeting uses
  `tools_override=[]`.
- **[product.important.8]** Every read tool description now states
  "Times in the response are ISO 8601 with the account's timezone
  offset; render in the user's local timezone."
- **[product.important.9]** `create_event` description tells the AI
  to call `directory_search` from the google_directory plugin to
  resolve attendee names → emails; do NOT hallucinate.
- **[product.important.10]** Mutating tools dropped slash commands
  (`memory-slash-commands.md` constraints — too many positional ISO
  / opaque-id args).
- **[product.important.11]** Added a note to the Greeting follow-up
  PR section: a `summarize_day` tool with
  `ConfigParam(ai_prompt=True)` is the proper home for AI-driven
  calendar summarization, per `memory-ai-prompts-configurable.md`.
  Not in this PR.
- **[product.important.12]** `find_free_time` description spells out
  what "free" / "busy" mean (tentative = busy, declined = free,
  cancelled = free, transparent = free, all-day = busy across
  working-hours window).
- **[product.nit.1]** `calendar_accounts` renamed to
  `list_calendar_accounts` (verb-first).
- **[product.nit.2]** Profile-table contradiction is gone by
  deletion (see disputed item below).
- **[product.nit.3]** `upcoming_event_lookahead_minutes` kept
  per-account; `upcoming_announce_minutes` is the service-level
  default. Service-level config description distinguishes them
  ("imminent notifications" vs. "morning brief, hours ahead").
- **[product.nit.4]** `EventCreateRequest.send_invites` default
  flipped to `False`; tool layer also defaults to `False`.
- **[product.nit.5]** Added `show_dashboard_card` ConfigParam
  (default true) — was referenced but not declared.
- **[product.nit.6]** `create_event` description tells the AI to
  include `html_link` in its reply.
- **[product.nit.7]** `next_event` default `within_hours` raised to
  72h.

### Engineering review

- **[eng.blocker.1]** Auth helpers no longer take `is_admin: bool` —
  derived from `user_ctx.roles` inside the helpers. Spec calls out
  that callers must never pass an ad-hoc bool.
- **[eng.blocker.2]** Aggregate read for `account_id=None` now
  documented with concurrent fan-out via `asyncio.gather(...,
  return_exceptions=True)`, per-runtime timeout
  (`aggregation_timeout_sec` ConfigParam, default 10), partial-result
  return via `AggregatedEvents` envelope with `warnings: list[str]`,
  and `max_results` applied post-merge.
- **[eng.blocker.3]** Idempotency: `EventCreateRequest` gains
  `idempotency_key`; service computes a deterministic SHA-256-derived
  key when caller omits; backends forward (Google: `requestId`).
  `update_event` gains `if_match_etag` for optimistic concurrency
  (Google: If-Match header on patch); `CalendarBackendConflictError`
  raised on 412.
- **[eng.blocker.4]** Cache trim/fetch contradiction resolved: both
  use the same `cache_back_hours` window (default 2). Sweep job
  reaps anything older.
- **[eng.blocker.5]** Diff-after-restart: `last_seen_event_ids` now
  lazy-seeds from persisted cache on the first poll (step 2 of
  polling logic). Test asserts no `calendar.event.created` events
  fire on restart with a populated cache.
- **[eng.blocker.6]** `calendar_event_announcements` cleanup is
  explicit: dedicated `calendar-announcement-sweep` scheduler job
  every 30 min, also reaps `calendar_events` rows older than
  `cache_back_hours`.
- **[eng.blocker.7]** Naive datetime handling pinned: localized to
  `ZoneInfo(account.timezone)` at the tool→service boundary;
  `EventCreateRequest.start/end` is always tz-aware. Invalid
  `account.timezone` rejected at write time and at first-use.
- **[eng.blocker.8]** Account health surfacing: new `health`,
  `last_error`, `last_error_at` fields on `CalendarAccount`; new
  `calendar.account.health_changed` event; new
  `unhealthy_failure_threshold` ConfigParam. Health flips after
  threshold consecutive auth failures; SPA shows red badge;
  `list_calendar_accounts` AI tool surfaces it.
- **[eng.blocker.9]** Plaintext-at-rest for `backend_config`
  acknowledged in a dedicated subsection ("Sensitive backend_config
  and at-rest plaintext"); std-plugins README documents file-perm
  hardening and the gap; Open Question #8 tracks the storage-layer
  fix.
- **[eng.important.1]** Polling jitter is now mandatory in
  `_start_runtime` — `random.uniform(0, min(poll_interval_sec, 120))`
  on first fire — and in runtime creation via `create_account`.
- **[eng.important.2]** `find_free_time` algorithm pinned: 15-min
  granularity; tentative=busy, declined=free, cancelled=free,
  transparent=free, all-day=busy clamped to working hours;
  cross-account working-hours = intersection (most-restrictive);
  argument validation rejects out-of-range duration / inverted
  windows / cross-midnight working hours.
- **[eng.important.3]** `max_results` reconciled: backend default
  250, provider default 250, aggregate cap applied post-merge after
  per-runtime fan-out. Documented in the `CalendarBackend.list_events`
  docstring and the aggregation paragraph.
- **[eng.important.4]** Backend `display_name` field added to
  `GoogleCalendarBackend` (= "Google Calendar"); surfaced by
  `calendar.backends.list` so SPA renders friendly name, not
  registry key.
- **[eng.important.5]** `test_account_connection` contract pinned:
  instantiate temp backend with proposed config, call
  `list_calendars()`, return `{ok, calendars?, error?}`. Service
  owns lifecycle.
- **[eng.important.6]** Concurrent-write on the same account
  acknowledged as last-write-wins for v1; flagged in Open Q #6.
- **[eng.important.7]** `_restart_runtime` documented to fire only
  when a `restart_required=True` field actually changed value (not on
  every cosmetic edit).
- **[eng.important.8]** `create_event.start/end` tool args
  documented as **strict ISO 8601**; relative-time handling
  delegated to AI via the `system_datetime` instruction in the
  description, not by accepting "tomorrow" in the tool itself. Date
  conveniences ("today" / "tomorrow" / "yesterday") are accepted only
  on `get_schedule.date`, where they were already specified.
- **[eng.important.9]** Working-hours intersection across multiple
  accounts is pinned (most-restrictive wins).
- **[eng.important.10]** Cancellation propagation pinned: cancelled
  events are filtered out of `fresh` BEFORE the diff (step 4 of
  polling), so they emit `calendar.event.deleted` exactly once.
- **[eng.important.11]** Storage indexes updated to include
  `calendar_events(start)` for aggregate queries plus
  `calendar_event_announcements(account_id, start_iso)` for the
  sweep. CONTAINS-filtered fields documented as deliberately
  unindexed.
- **[eng.important.12]** Profile mode contradiction goes away by
  dropping the profile-mutation idea entirely (see arch.blocker.1).
- **[eng.important.13]** Observability section added to the Google
  backend: structured DEBUG / WARNING logs with
  `account_id`/`method`/`calendar_id`/`duration_ms`/`event_count`,
  metrics counters when a `MetricsProvider` capability is resolved,
  exponential backoff with jitter on rate-limit/transient errors.
  Error taxonomy explicitly defined in interfaces/calendar.py
  (`CalendarBackendAuthError`, `RateLimitError` (with
  `retry_after_sec`), `ConflictError`, `NotFoundError`,
  `TransientError`).
- **[eng.important.14]** Recurring-instance write ops log at INFO
  with `event_id`, `recurring_event_id`, and operation. Tool
  descriptions for `update_event` / `delete_event` warn about
  single-instance-only semantics.
- **[eng.important.15]** Cross-user privacy paragraph added to
  Events Published — the visibility unit is the account, not the
  per-attendee email; documented as intentional.
- **[eng.important.16]** `delete_account` cascade documented
  explicitly — also deletes all `calendar_events` and
  `calendar_event_announcements` rows for the account.
- **[eng.nit.1]** `CalendarEvent.raw` dropped (architecture nit
  alignment).
- **[eng.nit.2]** Tool handler is documented as mapping
  `attendees: list[str]` → `[CalendarAttendee(email=e) for e in ...]`
  before constructing `EventCreateRequest`.
- **[eng.nit.3]** `CalendarAccount.timezone` default kept at "UTC"
  but described as validated on write; deferring "auto-detect from
  primary calendar's timezone" to v2 (would require a probe before
  save, which we already do for calendar_id — but the user-visible
  surface is fine to leave as a default). Deferred (see below).
- **[eng.nit.4]** Tool output documented to collapse
  `SHARED_USER`/`SHARED_ROLE` distinction in the AI-facing
  `list_calendar_accounts` output: the description enumerates all
  four values literally so the AI sees both `shared_user` and
  `shared_role` and can use whichever matters.
- **[eng.nit.5]** `is_recurring_instance` redundant field removed —
  consumers check `recurring_event_id is not None`.
- **[eng.nit.6]** `EventVisibility.DEFAULT` kept as `"default"` (the
  Google-Calendar string the value maps to); inline doc clarifies
  "inherit calendar default."
- **[eng.nit.7]** Tool naming made consistent — `get_schedule` and
  `next_event` both verb-first.
- **[eng.nit.8]** `FreeSlot` renamed `duration_minutes` →
  `slot_duration_minutes` and added `requested_duration_minutes` so
  callers can tell the difference (the engineering-nit's option B).

### Deferred (intentionally not addressed in this round)

- **[eng.nit.3 — partial]** Auto-detection of `CalendarAccount.timezone`
  from the calendar's primary timezone via the same probe used for
  `calendar_id`. Would require sequencing the probe ahead of `create`,
  which conflicts with the simplified probe-after-save flow that
  resolves arch.blocker.2. Deferred to v2; the default of "UTC" plus
  validation on write is the v1 floor.

### Disputed

(None of the reviewer items in this round were judged incorrect.
The architect's blocker about `slash_namespace` mentioning that
"`InboxService` does not declare `slash_namespace` (it's core)" was
verified against the inbox source and is correct; we removed the
field from `CalendarService` accordingly.)

