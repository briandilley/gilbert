"""Unit tests for ``CalendarService`` against a fake backend.

These tests construct a service, attach a real SQLite storage
backend, fakes for event bus and scheduler, register one or more
accounts directly, and exercise the public API. We never mock the
service — only its third-party calendar backend and external
collaborators.

Coverage spans the spec's edge-case list: timezone correctness,
all-day events, recurring instances, cancellations, account deletion
cascade, sharing precedence, idempotency, mutation publish dedup, etag
conflicts, restart-no-republish, and aggregate-with-failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.services.calendar import (
    CalendarPermissionError,
    CalendarService,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.calendar import (
    CalendarAccount,
    CalendarAttendee,
    CalendarBackend,
    CalendarBackendAuthError,
    CalendarBackendConflictError,
    CalendarBackendNotFoundError,
    CalendarBackendRateLimitError,
    CalendarEvent,
    EventCreateRequest,
    EventStatus,
    FreeBusyBlock,
)
from gilbert.storage.sqlite import SQLiteStorage
from tests.unit.conftest import _FakeStorageProvider

# ── Fake backend ──────────────────────────────────────────────────────


class FakeCalendarBackend(CalendarBackend):
    """Deterministic in-memory CalendarBackend.

    Supports etag conflicts (set ``conflict_on_event_id``), simulated
    auth/transient errors, and idempotency dedup keyed on
    ``request.idempotency_key``.
    """

    backend_name = "fake_calendar"
    display_name = "Fake Calendar"
    last_initialized_with: dict[str, Any] | None = None

    def __init__(self) -> None:
        self.events: dict[str, CalendarEvent] = {}
        self.calendars: list[dict[str, Any]] = [
            {
                "id": "primary",
                "name": "Primary",
                "timezone": "UTC",
                "primary": True,
            }
        ]
        self.busy_blocks: list[FreeBusyBlock] = []
        self.initialized_with: dict[str, Any] | None = None
        self.closed = False
        self._next_event_id = 1
        self._idempotency: dict[str, str] = {}
        self.conflict_on_event_id: str | None = None
        # Override per-call to throw on the next list_events.
        self.fail_list_events_with: BaseException | None = None
        self.list_events_calls = 0
        self.delete_calls: list[tuple[str, str, bool]] = []

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        self.initialized_with = dict(config or {})
        type(self).last_initialized_with = self.initialized_with

    async def close(self) -> None:
        self.closed = True

    async def list_calendars(self) -> list[dict[str, Any]]:
        return list(self.calendars)

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        max_results: int = 250,
        single_events: bool = True,
    ) -> list[CalendarEvent]:
        self.list_events_calls += 1
        if self.fail_list_events_with is not None:
            exc = self.fail_list_events_with
            self.fail_list_events_with = None
            raise exc
        return [
            e
            for e in self.events.values()
            if e.calendar_id == calendar_id and e.start < time_max and e.end > time_min
        ]

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent | None:
        evt = self.events.get(event_id)
        if evt is None or evt.calendar_id != calendar_id:
            return None
        return evt

    async def free_busy(
        self,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> list[FreeBusyBlock]:
        return [
            b
            for b in self.busy_blocks
            if b.calendar_id in calendar_ids and b.start < time_max and b.end > time_min
        ]

    async def create_event(
        self,
        calendar_id: str,
        request: EventCreateRequest,
    ) -> CalendarEvent:
        if request.idempotency_key:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id is not None and existing_id in self.events:
                return self.events[existing_id]
        evt_id = f"evt_{self._next_event_id}"
        self._next_event_id += 1
        evt = CalendarEvent(
            event_id=evt_id,
            calendar_id=calendar_id,
            account_id="",
            title=request.title,
            start=request.start,
            end=request.end,
            etag=f"etag_{evt_id}_v1",
            all_day=request.all_day,
            description=request.description,
            location=request.location,
            attendees=tuple(request.attendees),
            visibility=request.visibility,
            html_link=f"https://example.com/{evt_id}",
        )
        self.events[evt_id] = evt
        if request.idempotency_key:
            self._idempotency[request.idempotency_key] = evt_id
        return evt

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        request: EventCreateRequest,
        *,
        if_match_etag: str = "",
    ) -> CalendarEvent:
        if event_id not in self.events:
            raise CalendarBackendNotFoundError(event_id)
        cur = self.events[event_id]
        if self.conflict_on_event_id == event_id and if_match_etag and if_match_etag != cur.etag:
            raise CalendarBackendConflictError("etag mismatch")
        new = CalendarEvent(
            event_id=event_id,
            calendar_id=calendar_id,
            account_id=cur.account_id,
            title=request.title or cur.title,
            start=request.start,
            end=request.end,
            etag=f"{cur.etag}_v2",
            all_day=request.all_day,
            description=request.description,
            location=request.location,
            attendees=tuple(request.attendees),
            visibility=request.visibility,
            html_link=cur.html_link,
            recurring_event_id=cur.recurring_event_id,
        )
        self.events[event_id] = new
        return new

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        send_cancellations: bool = False,
    ) -> None:
        self.delete_calls.append((calendar_id, event_id, send_cancellations))
        self.events.pop(event_id, None)

    # Test helpers (not part of the ABC).
    def add_event(self, evt: CalendarEvent) -> None:
        self.events[evt.event_id] = evt


# ── Collaborator fakes ───────────────────────────────────────────────


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self, _t: str, _h: Any) -> Any:
        return lambda: None


class FakeEventBusService:
    def __init__(self) -> None:
        self.bus = FakeEventBus()


class RecordingScheduler:
    """Captures ``add_job`` calls; tests fire them manually."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def add_job(self, **kwargs: Any) -> Any:
        self.jobs[kwargs["name"]] = kwargs

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.jobs.values())

    def get_job(self, name: str) -> Any:
        return self.jobs.get(name)

    async def run_now(self, name: str) -> None:
        cb = self.jobs[name]["callback"]
        await cb()

    async def fire(self, name: str) -> None:
        cb = self.jobs[name]["callback"]
        await cb()

    def schedule_for(self, name: str) -> Any:
        return self.jobs[name]["schedule"]


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        return svc

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


# Ensure the fake backend is registered (the class body runs at import).
assert "fake_calendar" in CalendarBackend.registered_backends()


# ── Helpers ───────────────────────────────────────────────────────────


def _user_ctx(user_id: str = "alice", *, roles: set[str] | None = None) -> UserContext:
    return UserContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id.title(),
        roles=frozenset(roles or set()),
    )


async def _service(
    sqlite_storage: SQLiteStorage,
) -> tuple[CalendarService, RecordingScheduler, FakeEventBus]:
    """Build and start a CalendarService backed by real SQLite.

    Returns the service, scheduler (for firing poll/sweep callbacks
    by name), and the test event bus (whose ``published`` list lets
    tests assert on event types and payloads).
    """
    svc = CalendarService()
    sched = RecordingScheduler()
    ev = FakeEventBusService()
    storage_provider = _FakeStorageProvider(sqlite_storage)
    resolver = FakeResolver()
    resolver.caps["entity_storage"] = storage_provider
    resolver.caps["scheduler"] = sched
    resolver.caps["event_bus"] = ev
    await svc.start(resolver)  # type: ignore[arg-type]
    return svc, sched, ev.bus


def _make_account(
    *,
    id_: str = "cal_a",
    name: str = "Work",
    timezone: str = "UTC",
    poll_enabled: bool = True,
    shared_with_users: list[str] | None = None,
    backend_name: str = "fake_calendar",
) -> CalendarAccount:
    return CalendarAccount(
        id=id_,
        name=name,
        email_address=f"{id_}@example.com",
        backend_name=backend_name,
        timezone=timezone,
        poll_enabled=poll_enabled,
        owner_user_id="alice",
        shared_with_users=list(shared_with_users or []),
    )


async def _seed_account(
    svc: CalendarService,
    account: CalendarAccount | None = None,
) -> tuple[CalendarAccount, FakeCalendarBackend]:
    """Create an account through ``create_account`` and pull out the
    runtime's backend so tests can drive it."""
    a = account or _make_account()
    created = await svc.create_account(a, _user_ctx("alice"))
    runtime = svc._runtimes[created.id]
    backend = runtime.backend
    assert isinstance(backend, FakeCalendarBackend)
    return created, backend


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_registers_boot_and_sweep_jobs(sqlite_storage: SQLiteStorage) -> None:
    svc, sched, _ = await _service(sqlite_storage)
    assert "calendar-boot" in sched.jobs
    assert "calendar-announcement-sweep" in sched.jobs


@pytest.mark.asyncio
async def test_create_account_starts_runtime_and_publishes_event(sqlite_storage: SQLiteStorage) -> None:
    svc, sched, bus = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    assert account.id in svc._runtimes
    assert sched.jobs[svc._runtimes[account.id].poll_job_name]
    types = [e.event_type for e in bus.published]
    assert "calendar.account.created" in types


@pytest.mark.asyncio
async def test_create_account_validates_timezone(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    bad = _make_account(timezone="Not/A/Real/Zone")
    with pytest.raises(ValueError):
        await svc.create_account(bad, _user_ctx("alice"))


@pytest.mark.asyncio
async def test_create_account_validates_working_hours(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    a = _make_account()
    a.working_hours_start_hour = 18
    a.working_hours_end_hour = 9
    with pytest.raises(ValueError):
        await svc.create_account(a, _user_ctx("alice"))


@pytest.mark.asyncio
async def test_admin_can_administer_other_users_account(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    bob_admin = _user_ctx("bob", roles={"admin"})
    # Sharing should succeed for the admin even though they're not the owner.
    updated = await svc.share_user(account.id, "carol", bob_admin)
    assert "carol" in updated.shared_with_users


@pytest.mark.asyncio
async def test_non_owner_non_admin_cannot_admin(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    with pytest.raises(CalendarPermissionError):
        await svc.update_account(account.id, {"name": "X"}, _user_ctx("bob"))


@pytest.mark.asyncio
async def test_update_account_with_invalid_timezone_rejected(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    with pytest.raises(ValueError):
        await svc.update_account(
            account.id,
            {"timezone": "Not/A/Real/Zone"},
            _user_ctx("alice"),
        )


@pytest.mark.asyncio
async def test_delete_account_cascades_events_and_announcements(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    # Insert events + announcements directly via storage.
    await svc._storage.put(
        "calendar_events",
        f"{account.id}:e1",
        {"account_id": account.id, "event_id": "e1"},
    )
    await svc._storage.put(
        "calendar_event_announcements",
        f"{account.id}:e1",
        {"account_id": account.id, "event_id": "e1"},
    )
    await svc.delete_account(account.id, _user_ctx("alice"))
    assert await svc._storage.get("calendar_accounts", account.id) is None
    assert await svc._storage.get("calendar_events", f"{account.id}:e1") is None
    assert await svc._storage.get("calendar_event_announcements", f"{account.id}:e1") is None
    assert account.id not in svc._runtimes


@pytest.mark.asyncio
async def test_first_poll_after_restart_does_not_republish_existing(sqlite_storage: SQLiteStorage) -> None:
    """Edge case 14 — restart with populated cache must NOT fire
    ``calendar.event.created`` for pre-existing events."""
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    # Pre-seed cache as if the previous process had already polled.
    for i in range(1, 6):
        s_iso = (datetime.now(UTC) + timedelta(hours=i)).isoformat()
        e_iso = (datetime.now(UTC) + timedelta(hours=i, minutes=30)).isoformat()
        await svc._storage.put(
            "calendar_events",
            f"{account.id}:evt_{i}",
            {
                "account_id": account.id,
                "event_id": f"evt_{i}",
                "calendar_id": "primary",
                "title": f"Existing {i}",
                "start": s_iso,
                "end": e_iso,
                "start_utc_iso": s_iso,
                "end_utc_iso": e_iso,
                "all_day": False,
                "etag": "x",
                "status": "confirmed",
                "transparency": "opaque",
                "attendees_json": "[]",
                "organizer_email": "",
                "location": "",
                "description": "",
                "html_link": "",
                "recurring_event_id": None,
                "visibility": "default",
            },
        )
        backend.add_event(
            CalendarEvent(
                event_id=f"evt_{i}",
                calendar_id="primary",
                account_id=account.id,
                title=f"Existing {i}",
                start=datetime.now(UTC) + timedelta(hours=i),
                end=datetime.now(UTC) + timedelta(hours=i, minutes=30),
            )
        )
    bus.published.clear()
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    types = [e.event_type for e in bus.published]
    assert "calendar.event.created" not in types


@pytest.mark.asyncio
async def test_poll_publishes_created_for_new_events(sqlite_storage: SQLiteStorage) -> None:
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    bus.published.clear()
    backend.add_event(
        CalendarEvent(
            event_id="evt_new",
            calendar_id="primary",
            account_id=account.id,
            title="New",
            start=datetime.now(UTC) + timedelta(hours=2),
            end=datetime.now(UTC) + timedelta(hours=3),
        )
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    types = [e.event_type for e in bus.published]
    assert "calendar.event.created" in types


@pytest.mark.asyncio
async def test_cancellation_emits_deleted_exactly_once(sqlite_storage: SQLiteStorage) -> None:
    """Edge case 6 — a cancelled event is filtered from the fresh set
    and surfaces once as ``calendar.event.deleted``."""
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    backend.add_event(
        CalendarEvent(
            event_id="evt_x",
            calendar_id="primary",
            account_id=account.id,
            title="Standup",
            start=datetime.now(UTC) + timedelta(hours=1),
            end=datetime.now(UTC) + timedelta(hours=1, minutes=30),
        )
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    bus.published.clear()
    # Now cancel it.
    backend.events["evt_x"] = CalendarEvent(
        event_id="evt_x",
        calendar_id="primary",
        account_id=account.id,
        title="Standup",
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=1, minutes=30),
        status=EventStatus.CANCELLED,
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    deletes = [e for e in bus.published if e.event_type == "calendar.event.deleted"]
    assert len(deletes) == 1
    # Idempotent — cancelling again should not re-fire.
    bus.published.clear()
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    assert all(e.event_type != "calendar.event.deleted" for e in bus.published)


@pytest.mark.asyncio
async def test_mutation_publish_dedup_suppresses_poll_republication(sqlite_storage: SQLiteStorage) -> None:
    """Edge case 15 — create_event publishes once; the next poll's
    diff DOES NOT re-publish."""
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    bus.published.clear()
    req = EventCreateRequest(
        title="Created via mutate",
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=1, minutes=30),
    )
    evt = await svc.create_event(account.id, req, user)
    creates_after_mutate = [e for e in bus.published if e.event_type == "calendar.event.created"]
    assert len(creates_after_mutate) == 1
    assert creates_after_mutate[0].data["event_id"] == evt.event_id
    # Now run the poll — it sees the same event and must not fire again.
    bus.published.clear()
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    assert all(e.event_type != "calendar.event.created" for e in bus.published), [
        e.event_type for e in bus.published
    ]


@pytest.mark.asyncio
async def test_idempotency_dedup_for_repeated_create(sqlite_storage: SQLiteStorage) -> None:
    """Edge case 17 — same args ⇒ same idempotency key ⇒ backend
    deduplicates and only one event is created."""
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    req1 = EventCreateRequest(
        title="Coffee",
        start=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        attendees=[CalendarAttendee(email="bob@example.com")],
    )
    req2 = EventCreateRequest(
        title="Coffee",
        start=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        attendees=[CalendarAttendee(email="bob@example.com")],
    )
    evt1 = await svc.create_event(account.id, req1, user)
    evt2 = await svc.create_event(account.id, req2, user)
    assert evt1.event_id == evt2.event_id
    assert len(backend.events) == 1


@pytest.mark.asyncio
async def test_etag_conflict_on_update_propagates(sqlite_storage: SQLiteStorage) -> None:
    """Edge case 16 — a stale if_match_etag yields ``CalendarBackendConflictError``."""
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    req = EventCreateRequest(
        title="X",
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
    )
    evt = await svc.create_event(account.id, req, user)
    backend.conflict_on_event_id = evt.event_id
    with pytest.raises(CalendarBackendConflictError):
        await svc.update_event(
            account.id,
            evt.event_id,
            req,
            user,
            if_match_etag="stale",
        )


@pytest.mark.asyncio
async def test_aggregate_events_with_account_filter_returns_warnings(
    sqlite_storage: SQLiteStorage,
) -> None:
    """Edge case 13 — one account fails, others still produce events,
    failure surfaces as a warning. Drives the real ``_event_row_to_event``
    decode by writing a malformed ``start`` so a future regression in the
    decoder is caught too (instead of monkeypatching the SUT helper)."""
    svc, sched, _ = await _service(sqlite_storage)
    a1, b1 = await _seed_account(svc, _make_account(id_="cal_1", name="One"))
    a2, _b2 = await _seed_account(svc, _make_account(id_="cal_2", name="Two"))
    user = _user_ctx("alice")
    b1.add_event(
        CalendarEvent(
            event_id="e1",
            calendar_id="primary",
            account_id=a1.id,
            title="A",
            start=datetime.now(UTC) + timedelta(hours=1),
            end=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    await sched.fire(svc._runtimes[a1.id].poll_job_name)
    # Inject a row with a malformed ISO timestamp so the real
    # ``_event_row_to_event`` decoder throws when it tries to parse
    # ``start``. The aggregate must catch and surface as a warning.
    now_iso = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await svc._storage.put(
        "calendar_events",
        f"{a2.id}:e_fail",
        {
            "account_id": a2.id,
            "event_id": "e_fail",
            "calendar_id": "primary",
            "title": "Two",
            "start": "this-is-not-a-datetime",
            "end": "neither-is-this",
            "start_utc_iso": now_iso,
            "end_utc_iso": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "all_day": False,
            "etag": "",
            "status": "confirmed",
            "transparency": "opaque",
            "attendees_json": "[]",
            "organizer_email": "",
            "location": "",
            "description": "",
            "html_link": "",
            "recurring_event_id": None,
            "visibility": "default",
        },
    )
    agg = await svc.list_events(
        None,
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) + timedelta(days=1),
        user,
    )
    titles = [e.title for e in agg.events]
    assert "A" in titles
    assert "Two" not in titles
    assert any("cal_2" in w or "Two" in w for w in agg.warnings), agg.warnings


@pytest.mark.asyncio
async def test_unhealthy_after_repeated_auth_failures(sqlite_storage: SQLiteStorage) -> None:
    """Health flips to ``unhealthy`` ONLY after the threshold-th
    consecutive failure (default 3) — pin the threshold so a regression
    that flips early/late is caught."""
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)

    # Fire 1: still ok.
    backend.fail_list_events_with = CalendarBackendAuthError("nope")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    assert (await svc._require_account(account.id)).health == "ok"

    # Fire 2: still ok.
    backend.fail_list_events_with = CalendarBackendAuthError("nope")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    assert (await svc._require_account(account.id)).health == "ok"

    # Fire 3: now unhealthy + exactly one health_changed event so far.
    backend.fail_list_events_with = CalendarBackendAuthError("nope")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    fresh = await svc._require_account(account.id)
    assert fresh.health == "unhealthy"
    assert "nope" in fresh.last_error
    health_events = [e for e in bus.published if e.event_type == "calendar.account.health_changed"]
    assert len(health_events) == 1


@pytest.mark.asyncio
async def test_health_recovers_to_ok_on_successful_poll(sqlite_storage: SQLiteStorage) -> None:
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    # Force three failures to flip to unhealthy.
    for _ in range(3):
        backend.fail_list_events_with = CalendarBackendAuthError("nope")
        await sched.fire(svc._runtimes[account.id].poll_job_name)
    bus.published.clear()
    # Successful poll — should flip back.
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    fresh = await svc._require_account(account.id)
    assert fresh.health == "ok"
    health_events = [e for e in bus.published if e.event_type == "calendar.account.health_changed"]
    assert health_events  # at least one recovery event


@pytest.mark.asyncio
async def test_find_free_time_validates_arguments(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    user = _user_ctx("alice")
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        await svc.find_free_time(account.id, now, now + timedelta(hours=1), 4, user)
    with pytest.raises(ValueError):
        await svc.find_free_time(account.id, now, now + timedelta(hours=1), 481, user)
    with pytest.raises(ValueError):
        await svc.find_free_time(account.id, now + timedelta(hours=1), now, 30, user)
    with pytest.raises(ValueError):
        await svc.find_free_time(
            account.id,
            now,
            now + timedelta(minutes=15),
            30,
            user,
        )


@pytest.mark.asyncio
async def test_find_free_time_returns_full_window_when_calendar_empty(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    user = _user_ctx("alice")
    # Pick a Wed during working hours so respect_working_hours=True
    # gives us slots inside 9-18 UTC.
    start = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)  # Wed 10:00 UTC
    end = start + timedelta(hours=4)
    result = await svc.find_free_time(account.id, start, end, 30, user)
    assert len(result.slots) > 0
    for s in result.slots:
        assert s.slot_duration_minutes >= 30
        assert s.start.hour >= 9
        assert s.end.hour <= 18
    assert result.warnings == []


@pytest.mark.asyncio
async def test_get_account_returns_none_for_unauthorized_user(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    out = await svc.get_account(account.id, _user_ctx("carol"))
    assert out is None


@pytest.mark.asyncio
async def test_probe_calendars_passes_calendar_id_to_backend(
    sqlite_storage: SQLiteStorage,
) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account = _make_account(
        poll_enabled=False,
    )
    account.calendar_id = "shared-calendar@example.com"
    account.backend_config = {"service_account_json": "{}"}
    created = await svc.create_account(account, _user_ctx("alice"))
    FakeCalendarBackend.last_initialized_with = None

    await svc.probe_calendars(created.id, _user_ctx("alice"))

    assert FakeCalendarBackend.last_initialized_with is not None
    assert (
        FakeCalendarBackend.last_initialized_with["calendar_id"]
        == "shared-calendar@example.com"
    )


@pytest.mark.asyncio
async def test_connection_passes_calendar_id_to_backend(
    sqlite_storage: SQLiteStorage,
) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account = _make_account(
        poll_enabled=False,
    )
    account.calendar_id = "shared-calendar@example.com"
    account.backend_config = {"service_account_json": "{}"}
    created = await svc.create_account(account, _user_ctx("alice"))
    FakeCalendarBackend.last_initialized_with = None

    out = await svc.test_account_connection(created.id, _user_ctx("alice"))

    assert out["ok"] is True
    assert FakeCalendarBackend.last_initialized_with is not None
    assert (
        FakeCalendarBackend.last_initialized_with["calendar_id"]
        == "shared-calendar@example.com"
    )


@pytest.mark.asyncio
async def test_naive_start_in_create_event_localized_to_account_tz(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    a = _make_account(timezone="America/New_York")
    account, backend = await _seed_account(svc, a)
    user = _user_ctx("alice")
    naive_start = datetime(2026, 6, 1, 14, 0)  # naive
    naive_end = datetime(2026, 6, 1, 15, 0)
    req = EventCreateRequest(title="X", start=naive_start, end=naive_end)
    evt = await svc.create_event(account.id, req, user)
    assert evt.start.tzinfo is not None
    # The localized start should be 14:00 in America/New_York.
    from zoneinfo import ZoneInfo

    expected = naive_start.replace(tzinfo=ZoneInfo("America/New_York"))
    assert evt.start == expected


@pytest.mark.asyncio
async def test_get_tools_includes_eight_named_tools_when_enabled(sqlite_storage: SQLiteStorage) -> None:
    """All eight tools are present, every one is gated on the ``user``
    role (not ``admin``), and the parameter counts match the spec."""
    svc, _, _ = await _service(sqlite_storage)
    tools = svc.get_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "list_calendar_accounts",
        "get_schedule",
        "next_event",
        "get_event",
        "find_free_time",
        "create_event",
        "update_event",
        "delete_event",
    }
    # Every calendar tool requires an authenticated user — never admin.
    for tool in tools:
        assert tool.required_role == "user", tool.name
    # Pin the parameter counts so a regression that drops/adds a
    # parameter shows up here.
    expected_param_counts = {
        "list_calendar_accounts": 0,
        "get_schedule": 4,
        "next_event": 2,
        "get_event": 2,
        "find_free_time": 7,
        "create_event": 11,
        "update_event": 11,
        "delete_event": 4,
    }
    for name, expected_count in expected_param_counts.items():
        assert len(by_name[name].parameters) == expected_count, name
    # Mutating tools opt out of parallel execution so two confirms can't race.
    for name in ("create_event", "update_event", "delete_event"):
        assert by_name[name].parallel_safe is False, name


@pytest.mark.asyncio
async def test_get_tools_returns_empty_when_disabled(sqlite_storage: SQLiteStorage) -> None:
    svc = CalendarService()
    svc._enabled = False
    assert svc.get_tools() == []


@pytest.mark.asyncio
async def test_create_event_tool_returns_preview_when_unconfirmed(sqlite_storage: SQLiteStorage) -> None:
    """The mutating preview-confirm helper must not touch the backend
    when ``confirm=False``, AND the preview's content must show the
    proposed title and start so the user can verify before confirming."""
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    initial = len(backend.events)
    out = await svc.execute_tool(
        "create_event",
        {
            "_user_id": "alice",
            "account_id": account.id,
            "title": "Team Sync",
            "start": "2026-06-01T10:00:00+00:00",
            "duration_minutes": 30,
            "confirm": False,
        },
    )
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    assert out.ui_blocks
    # The preview must reference the proposed event title + start so the
    # user has something to confirm against.
    block = out.ui_blocks[0]
    summary_text = next(
        (e.label for e in block.elements if e.type == "label"),
        "",
    )
    assert "Team Sync" in summary_text
    assert "2026-06-01T10:00:00" in summary_text
    assert len(backend.events) == initial


@pytest.mark.asyncio
async def test_create_event_tool_writes_when_confirmed(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    initial = len(backend.events)
    await svc.execute_tool(
        "create_event",
        {
            "_user_id": "alice",
            "account_id": account.id,
            "title": "Hello",
            "start": "2026-06-01T10:00:00+00:00",
            "duration_minutes": 30,
            "confirm": True,
        },
    )
    assert len(backend.events) == initial + 1


@pytest.mark.asyncio
async def test_update_event_tool_returns_preview_when_unconfirmed(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    evt = await svc.create_event(
        account.id,
        EventCreateRequest(
            title="Initial",
            start=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
        ),
        user,
    )
    out = await svc.execute_tool(
        "update_event",
        {
            "_user_id": "alice",
            "account_id": account.id,
            "event_id": evt.event_id,
            "title": "Renamed",
            "confirm": False,
        },
    )
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    # Title hasn't actually changed yet.
    fresh = backend.events[evt.event_id]
    assert fresh.title == "Initial"


@pytest.mark.asyncio
async def test_delete_event_tool_returns_preview_when_unconfirmed(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    evt = await svc.create_event(
        account.id,
        EventCreateRequest(
            title="X",
            start=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
        ),
        user,
    )
    out = await svc.execute_tool(
        "delete_event",
        {
            "_user_id": "alice",
            "account_id": account.id,
            "event_id": evt.event_id,
            "confirm": False,
        },
    )
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    assert evt.event_id in backend.events  # still there


@pytest.mark.asyncio
async def test_announcement_published_once_for_imminent_event(sqlite_storage: SQLiteStorage) -> None:
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    bus.published.clear()
    soon = datetime.now(UTC) + timedelta(minutes=5)
    backend.add_event(
        CalendarEvent(
            event_id="soon",
            calendar_id="primary",
            account_id=account.id,
            title="Imminent",
            start=soon,
            end=soon + timedelta(minutes=15),
        )
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    upcoming = [e for e in bus.published if e.event_type == "calendar.event.upcoming"]
    assert len(upcoming) == 1
    bus.published.clear()
    # Second poll within window must not re-announce.
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    upcoming = [e for e in bus.published if e.event_type == "calendar.event.upcoming"]
    assert upcoming == []


@pytest.mark.asyncio
async def test_list_accessible_accounts_filters_by_access(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    a1, _ = await _seed_account(svc, _make_account(id_="cal_a"))
    a2, _ = await _seed_account(
        svc,
        _make_account(id_="cal_b", shared_with_users=["bob"]),
    )
    a3, _ = await _seed_account(svc, _make_account(id_="cal_c"))
    bob = _user_ctx("bob")
    accessible = await svc.list_accessible_accounts(bob)
    ids = {a.id for a in accessible}
    assert ids == {"cal_b"}


@pytest.mark.asyncio
async def test_share_user_publishes_shares_changed(sqlite_storage: SQLiteStorage) -> None:
    svc, _, bus = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    bus.published.clear()
    await svc.share_user(account.id, "bob", _user_ctx("alice"))
    types = [e.event_type for e in bus.published]
    assert "calendar.account.shares.changed" in types


# ── New: blocker / important regression coverage ─────────────────────


@pytest.mark.asyncio
async def test_find_free_time_with_24_hour_working_window_does_not_crash(
    sqlite_storage: SQLiteStorage,
) -> None:
    """``working_hours_end_hour=24`` must not raise — the validator
    accepts it as "end of day" and ``_collect_slots`` must compute the
    boundary as next-midnight rather than ``datetime.replace(hour=24)``
    which would ValueError."""
    svc, _, _ = await _service(sqlite_storage)
    a = _make_account()
    a.working_hours_start_hour = 0
    a.working_hours_end_hour = 24
    account, _ = await _seed_account(svc, a)
    user = _user_ctx("alice")
    start = datetime(2026, 6, 3, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=4)  # crosses midnight
    result = await svc.find_free_time(account.id, start, end, 30, user)
    # Returned slots must overlap the search window without crashing.
    assert all(s.end <= end for s in result.slots)
    assert all(s.start >= start for s in result.slots)


@pytest.mark.asyncio
async def test_list_events_handles_non_utc_account_timezone(
    sqlite_storage: SQLiteStorage,
) -> None:
    """ISO-string queries must order correctly when stored events carry
    non-UTC offsets — the UTC-iso columns make this a non-issue, but
    the test pins the behaviour."""
    svc, sched, _ = await _service(sqlite_storage)
    a = _make_account(timezone="America/Los_Angeles")
    account, backend = await _seed_account(svc, a)
    user = _user_ctx("alice")
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    # An event "tomorrow morning" Pacific time so the cache_back / look-
    # ahead window covers it regardless of when the test is run.
    today_pacific = datetime.now(pacific).date()
    tomorrow = today_pacific + timedelta(days=1)
    local_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, tzinfo=pacific)
    local_end = local_start + timedelta(hours=1)
    backend.add_event(
        CalendarEvent(
            event_id="evt_tz",
            calendar_id="primary",
            account_id=account.id,
            title="Local",
            start=local_start,
            end=local_end,
        )
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)

    # Query with a UTC window that includes the event.
    time_min = local_start.astimezone(UTC) - timedelta(hours=1)
    time_max = local_end.astimezone(UTC) + timedelta(hours=1)
    agg = await svc.list_events(account.id, time_min, time_max, user)
    titles = [e.title for e in agg.events]
    assert "Local" in titles, agg


@pytest.mark.asyncio
async def test_sweep_does_not_delete_current_events_on_west_of_utc_calendar(
    sqlite_storage: SQLiteStorage,
) -> None:
    """The sweep filter must compare on UTC, not on the original-tz
    ISO that's just stored for human display."""
    svc, sched, _ = await _service(sqlite_storage)
    a = _make_account(timezone="America/Los_Angeles")
    account, backend = await _seed_account(svc, a)
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    # Event is in the future relative to "now", but its local-tz ISO
    # string sorts BEFORE a UTC cutoff (a regression that compared on
    # the ``start`` column would prematurely delete this row).
    future_local = datetime.now(pacific) + timedelta(hours=2)
    backend.add_event(
        CalendarEvent(
            event_id="evt_future",
            calendar_id="primary",
            account_id=account.id,
            title="Future",
            start=future_local,
            end=future_local + timedelta(minutes=30),
        )
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    await svc._sweep_old_records()
    row = await svc._storage.get(
        "calendar_events", svc._event_row_id(account.id, "evt_future")
    )
    assert row is not None, "sweep deleted a still-current event"


@pytest.mark.asyncio
async def test_sweep_deletes_records_older_than_cache_window(
    sqlite_storage: SQLiteStorage,
) -> None:
    """Pre-seed a stale event row and a stale announcement row, fire
    the sweep, and assert both are gone."""
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    # Stale event — older than cache_back_hours (default 2h).
    stale_event_iso = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    await svc._storage.put(
        "calendar_events",
        f"{account.id}:stale",
        {
            "account_id": account.id,
            "event_id": "stale",
            "calendar_id": "primary",
            "title": "Old",
            "start": stale_event_iso,
            "end": stale_event_iso,
            "start_utc_iso": stale_event_iso,
            "end_utc_iso": stale_event_iso,
            "all_day": False,
            "etag": "x",
            "status": "confirmed",
            "transparency": "opaque",
            "attendees_json": "[]",
            "organizer_email": "",
            "location": "",
            "description": "",
            "html_link": "",
            "recurring_event_id": None,
            "visibility": "default",
        },
    )
    # Stale announcement — older than 48h.
    stale_announcement_iso = (datetime.now(UTC) - timedelta(hours=49)).isoformat()
    await svc._storage.put(
        "calendar_event_announcements",
        f"{account.id}:stale",
        {
            "account_id": account.id,
            "event_id": "stale",
            "start_iso": stale_announcement_iso,
            "announced_at": stale_announcement_iso,
        },
    )
    await svc._sweep_old_records()
    assert await svc._storage.get("calendar_events", f"{account.id}:stale") is None
    assert (
        await svc._storage.get("calendar_event_announcements", f"{account.id}:stale")
        is None
    )


@pytest.mark.asyncio
async def test_429_defers_next_poll_via_retry_after(
    sqlite_storage: SQLiteStorage,
) -> None:
    """A backend rate-limit response with ``retry_after_sec`` must
    push the next poll attempt out by at least that many seconds."""
    svc, sched, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    backend.fail_list_events_with = CalendarBackendRateLimitError(
        "slow down", retry_after_sec=30.0
    )
    import time as _time

    before = _time.monotonic()
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    runtime = svc._runtimes[account.id]
    assert runtime.next_poll_allowed_at >= before + 29.0
    # Subsequent fire skips without touching the backend.
    backend.list_events_calls = 0
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    assert backend.list_events_calls == 0


@pytest.mark.asyncio
async def test_transient_failure_increments_counter_without_flipping_health(
    sqlite_storage: SQLiteStorage,
) -> None:
    """A single ``TimeoutError`` (transient) bumps the counter but
    health stays ``ok`` — the threshold is what triggers the flip."""
    svc, sched, bus = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    backend.fail_list_events_with = TimeoutError("slow")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    fresh = await svc._require_account(account.id)
    assert fresh.health == "ok"
    runtime = svc._runtimes[account.id]
    assert runtime.consecutive_failures == 1
    health_events = [
        e for e in bus.published if e.event_type == "calendar.account.health_changed"
    ]
    assert health_events == []


@pytest.mark.asyncio
async def test_runtime_start_schedules_jittered_first_poll(
    sqlite_storage: SQLiteStorage,
) -> None:
    """The poll job's first fire (``start_at``) is jittered to within
    ``[now, now + min(poll_interval_sec, 120)]`` to prevent thundering-
    herd behaviour on Gilbert restart."""
    svc, sched, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    schedule = sched.schedule_for(svc._runtimes[account.id].poll_job_name)
    assert schedule.start_at is not None
    now = datetime.now()
    delta = (schedule.start_at - now).total_seconds()
    upper = float(min(account.poll_interval_sec, 120))
    # Allow a tiny clock-skew window for the assertion (start_at was
    # computed before ``now`` here).
    assert -1.0 <= delta <= upper + 1.0, delta


@pytest.mark.asyncio
async def test_poll_skips_storage_writes_for_unchanged_events(
    sqlite_storage: SQLiteStorage,
) -> None:
    """After the first poll has populated the cache, a second poll
    with no event mutations must NOT call storage.put for those
    events again — the diff-summary check should short-circuit."""
    svc, sched, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    backend.add_event(
        CalendarEvent(
            event_id="evt_quiet",
            calendar_id="primary",
            account_id=account.id,
            title="Quiet",
            start=datetime.now(UTC) + timedelta(hours=2),
            end=datetime.now(UTC) + timedelta(hours=3),
        )
    )
    await sched.fire(svc._runtimes[account.id].poll_job_name)

    # Wrap the storage backend's put to count only ``calendar_events``
    # writes.
    real_put = svc._storage.put
    put_calls: list[str] = []

    async def counting_put(collection: str, key: str, data: dict[str, Any]) -> None:
        if collection == "calendar_events":
            put_calls.append(key)
        await real_put(collection, key, data)

    svc._storage.put = counting_put  # type: ignore[method-assign]
    try:
        await sched.fire(svc._runtimes[account.id].poll_job_name)
    finally:
        svc._storage.put = real_put  # type: ignore[method-assign]

    assert put_calls == [], put_calls


@pytest.mark.asyncio
async def test_update_event_requires_etag(sqlite_storage: SQLiteStorage) -> None:
    """``update_event`` must reject an empty ``if_match_etag`` so the
    caller can't accidentally do last-write-wins by passing nothing."""
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    user = _user_ctx("alice")
    evt = await svc.create_event(
        account.id,
        EventCreateRequest(
            title="X",
            start=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
        ),
        user,
    )
    with pytest.raises(ValueError, match="if_match_etag"):
        await svc.update_event(
            account.id,
            evt.event_id,
            EventCreateRequest(
                title="Y",
                start=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
                end=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
            ),
            user,
            if_match_etag="",
        )


@pytest.mark.asyncio
async def test_update_event_logs_audit_for_recurring_instance(
    sqlite_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Updating a recurring instance emits an INFO log line carrying
    the series id, so ops can audit instance-vs-series mutations."""
    svc, _, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    # Seed a recurring instance.
    backend.events["evt_rec"] = CalendarEvent(
        event_id="evt_rec",
        calendar_id="primary",
        account_id=account.id,
        title="Weekly",
        start=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
        etag="etag_rec_v1",
        recurring_event_id="series_001",
    )
    with caplog.at_level(logging.INFO, logger="gilbert.core.services.calendar"):
        await svc.update_event(
            account.id,
            "evt_rec",
            EventCreateRequest(
                title="Weekly Renamed",
                start=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
                end=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
            ),
            user,
            if_match_etag="etag_rec_v1",
        )
    assert any(
        "recurring instance" in rec.getMessage() and "series_001" in rec.getMessage()
        for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_resolve_user_ctx_raises_when_user_id_missing(
    sqlite_storage: SQLiteStorage,
) -> None:
    """Tools must NEVER silently elevate to ``UserContext.SYSTEM`` —
    a missing ``_user_id`` is a programming error, raise loudly."""
    svc, _, _ = await _service(sqlite_storage)
    with pytest.raises(PermissionError, match="missing user context"):
        await svc.execute_tool("list_calendar_accounts", {})


@pytest.mark.asyncio
async def test_account_payload_masks_sensitive_backend_config(
    sqlite_storage: SQLiteStorage,
) -> None:
    """Sensitive ConfigParam values must be returned as ``********``
    by ``_account_payload`` so shared-users (and even admins) don't
    receive credentials by default — admins can re-fetch via
    ``calendar.accounts.reveal_backend_config``."""
    from gilbert.interfaces.calendar import CalendarBackend
    from gilbert.interfaces.configuration import ConfigParam
    from gilbert.interfaces.tools import ToolParameterType

    # Register a one-off backend with a sensitive param so this test
    # doesn't depend on the std-plugin being importable.
    class _SensitiveCalendarBackend(FakeCalendarBackend):
        backend_name = "sensitive_calendar"

        @classmethod
        def backend_config_params(cls) -> list[ConfigParam]:
            return [
                ConfigParam(
                    key="email_address",
                    type=ToolParameterType.STRING,
                    description="public",
                ),
                ConfigParam(
                    key="service_account_json",
                    type=ToolParameterType.STRING,
                    description="secret",
                    sensitive=True,
                ),
            ]

    try:
        svc, _, _ = await _service(sqlite_storage)
        a = _make_account(backend_name="sensitive_calendar")
        a.backend_config = {
            "email_address": "alice@example.com",
            "service_account_json": "{...secret...}",
        }
        account, _ = await _seed_account(svc, a)
        user = _user_ctx("alice")
        payload = svc._account_payload(account, user)
        assert payload["backend_config"]["service_account_json"] == "********"
        assert payload["backend_config"]["email_address"] == "alice@example.com"
        # When ``reveal_backend_config=True``, the unmasked value is returned.
        revealed = svc._account_payload(account, user, reveal_backend_config=True)
        assert revealed["backend_config"]["service_account_json"] == "{...secret...}"
    finally:
        CalendarBackend._registry.pop("sensitive_calendar", None)


def _tomorrow_at(hour: int, minute: int = 0) -> datetime:
    """Tomorrow at HH:MM UTC — keeps test-local dates inside the
    default 14-day cache lookahead regardless of when the test runs."""
    base = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return base


@pytest.mark.asyncio
async def test_find_free_time_excludes_busy_intervals(
    sqlite_storage: SQLiteStorage,
) -> None:
    """A single busy event inside the search window must NOT appear
    inside any returned free slot."""
    svc, sched, _ = await _service(sqlite_storage)
    account, backend = await _seed_account(svc)
    user = _user_ctx("alice")
    busy_start = _tomorrow_at(10)
    busy_end = _tomorrow_at(11)
    backend.add_event(
        CalendarEvent(
            event_id="evt_busy",
            calendar_id="primary",
            account_id=account.id,
            title="Busy",
            start=busy_start,
            end=busy_end,
        )
    )
    # Pump the cache.
    await sched.fire(svc._runtimes[account.id].poll_job_name)

    window_start = _tomorrow_at(9)
    window_end = _tomorrow_at(13)
    result = await svc.find_free_time(account.id, window_start, window_end, 30, user)
    # No returned slot may overlap the busy interval.
    assert result.slots, "expected at least one free slot"
    for s in result.slots:
        assert s.end <= busy_start or s.start >= busy_end, (s, busy_start, busy_end)


@pytest.mark.asyncio
async def test_find_free_time_unions_busy_intervals_across_accounts(
    sqlite_storage: SQLiteStorage,
) -> None:
    """When the user has multiple accessible accounts and ``account_id``
    is None, busy blocks from each are unioned — a slot must avoid the
    union, not just one calendar's blocks."""
    svc, sched, _ = await _service(sqlite_storage)
    a1, b1 = await _seed_account(svc, _make_account(id_="cal_x"))
    a2, b2 = await _seed_account(svc, _make_account(id_="cal_y"))
    user = _user_ctx("alice")
    busy1_start = _tomorrow_at(10)
    busy1_end = _tomorrow_at(10, 30)
    busy2_start = _tomorrow_at(11)
    busy2_end = _tomorrow_at(11, 30)
    # cal_x has a busy block 10:00-10:30.
    b1.add_event(
        CalendarEvent(
            event_id="b1",
            calendar_id="primary",
            account_id=a1.id,
            title="b1",
            start=busy1_start,
            end=busy1_end,
        )
    )
    # cal_y has a busy block 11:00-11:30.
    b2.add_event(
        CalendarEvent(
            event_id="b2",
            calendar_id="primary",
            account_id=a2.id,
            title="b2",
            start=busy2_start,
            end=busy2_end,
        )
    )
    await sched.fire(svc._runtimes[a1.id].poll_job_name)
    await sched.fire(svc._runtimes[a2.id].poll_job_name)

    window_start = _tomorrow_at(9)
    window_end = _tomorrow_at(13)
    result = await svc.find_free_time(None, window_start, window_end, 30, user)
    busy_intervals = [
        (busy1_start, busy1_end),
        (busy2_start, busy2_end),
    ]
    assert result.slots, "expected at least one free slot"
    for s in result.slots:
        for bs, be in busy_intervals:
            assert s.end <= bs or s.start >= be, (s, bs, be)


# ── WS RPC happy-path + auth-deny coverage ───────────────────────────


class _FakeConn:
    """Minimal stand-in for the WS connection passed to RPC handlers."""

    def __init__(self, user_ctx: UserContext) -> None:
        self.user_ctx = user_ctx


@pytest.mark.asyncio
async def test_ws_accounts_create_admin_path_creates_account(
    sqlite_storage: SQLiteStorage,
) -> None:
    svc, _, _ = await _service(sqlite_storage)
    admin = _user_ctx("admin1", roles={"admin"})
    conn = _FakeConn(admin)
    frame = {
        "id": 42,
        "name": "Admin's calendar",
        "email_address": "admin@example.com",
        "backend_name": "fake_calendar",
        "backend_config": {},
        "timezone": "UTC",
    }
    out = await svc._ws_accounts_create(conn, frame)
    assert out["type"] == "calendar.accounts.create.result"
    assert out["account"]["name"] == "Admin's calendar"


@pytest.mark.asyncio
async def test_ws_events_create_denies_non_shared_user(
    sqlite_storage: SQLiteStorage,
) -> None:
    """A user with no access to the account must get a 403 from
    ``calendar.events.create``."""
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    bob = _user_ctx("bob")  # not owner / admin / shared
    conn = _FakeConn(bob)
    frame = {
        "id": 1,
        "account_id": account.id,
        "event": {
            "title": "X",
            "start": "2026-06-01T10:00:00+00:00",
            "duration_minutes": 30,
        },
    }
    out = await svc._ws_events_create(conn, frame)
    assert out["type"] == "gilbert.error"
    assert out["code"] == 403


@pytest.mark.asyncio
async def test_ws_events_create_happy_path(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    conn = _FakeConn(_user_ctx("alice"))
    frame = {
        "id": 1,
        "account_id": account.id,
        "event": {
            "title": "Hello",
            "start": "2026-06-01T10:00:00+00:00",
            "duration_minutes": 30,
        },
    }
    out = await svc._ws_events_create(conn, frame)
    assert out["type"] == "calendar.events.create.result"
    assert out["event"]["title"] == "Hello"


@pytest.mark.asyncio
async def test_ws_find_free_time_happy_path(sqlite_storage: SQLiteStorage) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    conn = _FakeConn(_user_ctx("alice"))
    frame = {
        "id": 1,
        "account_id": account.id,
        "time_min": "2026-06-03T09:00:00+00:00",
        "time_max": "2026-06-03T13:00:00+00:00",
        "duration_minutes": 30,
    }
    out = await svc._ws_find_free_time(conn, frame)
    assert out["type"] == "calendar.find_free_time.result"
    assert isinstance(out["slots"], list)
    assert "warnings" in out


@pytest.mark.asyncio
async def test_ws_find_free_time_denies_non_shared_user(
    sqlite_storage: SQLiteStorage,
) -> None:
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    bob = _user_ctx("bob")
    conn = _FakeConn(bob)
    frame = {
        "id": 1,
        "account_id": account.id,
        "time_min": "2026-06-03T09:00:00+00:00",
        "time_max": "2026-06-03T13:00:00+00:00",
        "duration_minutes": 30,
    }
    out = await svc._ws_find_free_time(conn, frame)
    assert out["type"] == "gilbert.error"
    assert out["code"] == 403


@pytest.mark.asyncio
async def test_ws_reveal_backend_config_admin_only(
    sqlite_storage: SQLiteStorage,
) -> None:
    """Only admins (or the owner) of an account can reveal the
    plaintext backend_config."""
    svc, _, _ = await _service(sqlite_storage)
    account, _ = await _seed_account(svc)
    # Owner can reveal.
    out = await svc._ws_accounts_reveal_backend_config(
        _FakeConn(_user_ctx("alice")),
        {"id": 1, "account_id": account.id},
    )
    assert out["type"] == "calendar.accounts.reveal_backend_config.result"
    # Non-owner non-admin denied.
    out_denied = await svc._ws_accounts_reveal_backend_config(
        _FakeConn(_user_ctx("bob")),
        {"id": 1, "account_id": account.id},
    )
    assert out_denied["type"] == "gilbert.error"
    assert out_denied["code"] == 403
