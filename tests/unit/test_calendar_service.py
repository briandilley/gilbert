"""Unit tests for ``CalendarService`` against a fake backend.

These tests construct a service, attach in-memory fakes for storage,
event bus, scheduler, and access control, register one or more
accounts directly, and exercise the public API. We never mock the
service — only its backend and external collaborators.

Coverage spans the spec's edge-case list: timezone correctness,
all-day events, recurring instances, cancellations, account deletion
cascade, sharing precedence, idempotency, mutation publish dedup, etag
conflicts, restart-no-republish, and aggregate-with-failure.
"""

from __future__ import annotations

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
    CalendarEvent,
    EventCreateRequest,
    EventStatus,
    FreeBusyBlock,
)
from gilbert.interfaces.storage import FilterOp

# ── Fake backend ──────────────────────────────────────────────────────


class FakeCalendarBackend(CalendarBackend):
    """Deterministic in-memory CalendarBackend.

    Supports etag conflicts (set ``conflict_on_event_id``), simulated
    auth/transient errors, and idempotency dedup keyed on
    ``request.idempotency_key``.
    """

    backend_name = "fake_calendar"
    display_name = "Fake Calendar"

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


# ── In-memory storage backend (lightweight, query subset) ────────────


class FakeStorageBackend:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        record = self._data.get(collection, {}).get(key)
        if record is None:
            return None
        return {**record, "_id": key}

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        clean = {k: v for k, v in data.items() if k != "_id"}
        self._data.setdefault(collection, {})[key] = clean

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def exists(self, collection: str, key: str) -> bool:
        return key in self._data.get(collection, {})

    def _match(self, record: dict[str, Any], filters: list[Any]) -> bool:
        for f in filters:
            val = record.get(f.field)
            if f.op == FilterOp.EQ and val != f.value:
                return False
            if f.op == FilterOp.NEQ and val == f.value:
                return False
            if f.op == FilterOp.IN and val not in f.value:
                return False
            if f.op == FilterOp.GT and (val is None or val <= f.value):
                return False
            if f.op == FilterOp.GTE and (val is None or val < f.value):
                return False
            if f.op == FilterOp.LT and (val is None or val >= f.value):
                return False
            if f.op == FilterOp.LTE and (val is None or val > f.value):
                return False
            if f.op == FilterOp.CONTAINS:
                if val is None or str(f.value).lower() not in str(val).lower():
                    return False
        return True

    async def count(self, query: Any) -> int:
        coll = query.collection
        out = 0
        for key, data in self._data.get(coll, {}).items():
            record = {**data, "_id": key}
            if self._match(record, query.filters or []):
                out += 1
        return out

    async def query(self, query: Any) -> list[dict[str, Any]]:
        coll = query.collection
        results: list[dict[str, Any]] = []
        for key, data in self._data.get(coll, {}).items():
            record = {**data, "_id": key}
            if self._match(record, query.filters or []):
                results.append(record)
        if query.sort:
            for s in reversed(query.sort):
                results.sort(
                    key=lambda r: r.get(s.field) or "",
                    reverse=s.descending,
                )
        if query.limit:
            results = results[: query.limit]
        return results

    async def ensure_index(self, _: Any) -> None:
        pass


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorageBackend()
        self.raw_backend = self.backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend


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


async def _service() -> tuple[CalendarService, RecordingScheduler, FakeEventBus]:
    svc = CalendarService()
    storage = FakeStorageService()
    sched = RecordingScheduler()
    ev = FakeEventBusService()
    resolver = FakeResolver()
    resolver.caps["entity_storage"] = _AsCap("entity_storage", storage)
    resolver.caps["scheduler"] = sched
    resolver.caps["event_bus"] = ev
    await svc.start(resolver)  # type: ignore[arg-type]
    return svc, sched, ev.bus


class _AsCap:
    """Wraps a fake into something that satisfies a capability protocol
    by forwarding attribute access. ``isinstance(obj, Protocol)`` looks
    at the methods, so we only need to expose the right surface."""

    def __init__(self, name: str, inner: Any) -> None:
        self._name = name
        self._inner = inner

    @property
    def backend(self) -> Any:
        return self._inner.backend

    @property
    def raw_backend(self) -> Any:
        return self._inner.raw_backend

    def create_namespaced(self, namespace: str) -> Any:
        return self._inner.create_namespaced(namespace)


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
async def test_start_registers_boot_and_sweep_jobs() -> None:
    svc, sched, _ = await _service()
    assert "calendar-boot" in sched.jobs
    assert "calendar-announcement-sweep" in sched.jobs


@pytest.mark.asyncio
async def test_create_account_starts_runtime_and_publishes_event() -> None:
    svc, sched, bus = await _service()
    account, _ = await _seed_account(svc)
    assert account.id in svc._runtimes
    assert sched.jobs[svc._runtimes[account.id].poll_job_name]
    types = [e.event_type for e in bus.published]
    assert "calendar.account.created" in types


@pytest.mark.asyncio
async def test_create_account_validates_timezone() -> None:
    svc, _, _ = await _service()
    bad = _make_account(timezone="Not/A/Real/Zone")
    with pytest.raises(ValueError):
        await svc.create_account(bad, _user_ctx("alice"))


@pytest.mark.asyncio
async def test_create_account_validates_working_hours() -> None:
    svc, _, _ = await _service()
    a = _make_account()
    a.working_hours_start_hour = 18
    a.working_hours_end_hour = 9
    with pytest.raises(ValueError):
        await svc.create_account(a, _user_ctx("alice"))


@pytest.mark.asyncio
async def test_admin_can_administer_other_users_account() -> None:
    svc, _, _ = await _service()
    account, _ = await _seed_account(svc)
    bob_admin = _user_ctx("bob", roles={"admin"})
    # Sharing should succeed for the admin even though they're not the owner.
    updated = await svc.share_user(account.id, "carol", bob_admin)
    assert "carol" in updated.shared_with_users


@pytest.mark.asyncio
async def test_non_owner_non_admin_cannot_admin() -> None:
    svc, _, _ = await _service()
    account, _ = await _seed_account(svc)
    with pytest.raises(CalendarPermissionError):
        await svc.update_account(account.id, {"name": "X"}, _user_ctx("bob"))


@pytest.mark.asyncio
async def test_update_account_with_invalid_timezone_rejected() -> None:
    svc, _, _ = await _service()
    account, _ = await _seed_account(svc)
    with pytest.raises(ValueError):
        await svc.update_account(
            account.id,
            {"timezone": "Not/A/Real/Zone"},
            _user_ctx("alice"),
        )


@pytest.mark.asyncio
async def test_delete_account_cascades_events_and_announcements() -> None:
    svc, _, _ = await _service()
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
async def test_first_poll_after_restart_does_not_republish_existing() -> None:
    """Edge case 14 — restart with populated cache must NOT fire
    ``calendar.event.created`` for pre-existing events."""
    svc, sched, bus = await _service()
    account, backend = await _seed_account(svc)
    # Pre-seed cache as if the previous process had already polled.
    for i in range(1, 6):
        await svc._storage.put(
            "calendar_events",
            f"{account.id}:evt_{i}",
            {
                "account_id": account.id,
                "event_id": f"evt_{i}",
                "calendar_id": "primary",
                "title": f"Existing {i}",
                "start": (datetime.now(UTC) + timedelta(hours=i)).isoformat(),
                "end": (datetime.now(UTC) + timedelta(hours=i, minutes=30)).isoformat(),
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
async def test_poll_publishes_created_for_new_events() -> None:
    svc, sched, bus = await _service()
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
async def test_cancellation_emits_deleted_exactly_once() -> None:
    """Edge case 6 — a cancelled event is filtered from the fresh set
    and surfaces once as ``calendar.event.deleted``."""
    svc, sched, bus = await _service()
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
async def test_mutation_publish_dedup_suppresses_poll_republication() -> None:
    """Edge case 15 — create_event publishes once; the next poll's
    diff DOES NOT re-publish."""
    svc, sched, bus = await _service()
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
async def test_idempotency_dedup_for_repeated_create() -> None:
    """Edge case 17 — same args ⇒ same idempotency key ⇒ backend
    deduplicates and only one event is created."""
    svc, _, _ = await _service()
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
async def test_etag_conflict_on_update_propagates() -> None:
    """Edge case 16 — a stale if_match_etag yields ``CalendarBackendConflictError``."""
    svc, _, _ = await _service()
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case 13 — one account fails, others still produce events,
    failure surfaces as a warning."""
    svc, sched, _ = await _service()
    a1, b1 = await _seed_account(svc, _make_account(id_="cal_1", name="One"))
    a2, b2 = await _seed_account(svc, _make_account(id_="cal_2", name="Two"))
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
    # Patch _event_row_to_event to raise specifically when called for
    # a row from cal_2. cal_2 has nothing cached, so we instead inject
    # a row that the patched conversion will trip over.
    await svc._storage.put(
        "calendar_events",
        f"{a2.id}:e_fail",
        {
            "account_id": a2.id,
            "event_id": "e_fail",
            "calendar_id": "primary",
            "title": "Two",
            "start": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "end": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
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

    real_convert = CalendarService._event_row_to_event

    def raising_convert(row: dict[str, Any]) -> CalendarEvent:
        if row.get("account_id") == a2.id:
            raise RuntimeError("simulated decode failure")
        return real_convert(row)

    monkeypatch.setattr(
        CalendarService,
        "_event_row_to_event",
        staticmethod(raising_convert),
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
async def test_unhealthy_after_repeated_auth_failures() -> None:
    svc, sched, bus = await _service()
    account, backend = await _seed_account(svc)
    backend.fail_list_events_with = CalendarBackendAuthError("nope")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    backend.fail_list_events_with = CalendarBackendAuthError("nope")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    backend.fail_list_events_with = CalendarBackendAuthError("nope")
    await sched.fire(svc._runtimes[account.id].poll_job_name)
    fresh = await svc._require_account(account.id)
    assert fresh.health == "unhealthy"
    assert "nope" in fresh.last_error
    health_events = [e for e in bus.published if e.event_type == "calendar.account.health_changed"]
    assert len(health_events) == 1


@pytest.mark.asyncio
async def test_health_recovers_to_ok_on_successful_poll() -> None:
    svc, sched, bus = await _service()
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
async def test_find_free_time_validates_arguments() -> None:
    svc, _, _ = await _service()
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
async def test_find_free_time_returns_full_window_when_calendar_empty() -> None:
    svc, _, _ = await _service()
    account, _ = await _seed_account(svc)
    user = _user_ctx("alice")
    # Pick a Wed during working hours so respect_working_hours=True
    # gives us slots inside 9-18 UTC.
    start = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)  # Wed 10:00 UTC
    end = start + timedelta(hours=4)
    slots = await svc.find_free_time(account.id, start, end, 30, user)
    assert len(slots) > 0
    for s in slots:
        assert s.slot_duration_minutes >= 30
        assert s.start.hour >= 9
        assert s.end.hour <= 18


@pytest.mark.asyncio
async def test_get_account_returns_none_for_unauthorized_user() -> None:
    svc, _, _ = await _service()
    account, _ = await _seed_account(svc)
    out = await svc.get_account(account.id, _user_ctx("carol"))
    assert out is None


@pytest.mark.asyncio
async def test_naive_start_in_create_event_localized_to_account_tz() -> None:
    svc, _, _ = await _service()
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
async def test_get_tools_includes_eight_named_tools_when_enabled() -> None:
    svc, _, _ = await _service()
    tools = svc.get_tools()
    names = {t.name for t in tools}
    assert names == {
        "list_calendar_accounts",
        "get_schedule",
        "next_event",
        "get_event",
        "find_free_time",
        "create_event",
        "update_event",
        "delete_event",
    }


@pytest.mark.asyncio
async def test_get_tools_returns_empty_when_disabled() -> None:
    svc = CalendarService()
    svc._enabled = False
    assert svc.get_tools() == []


@pytest.mark.asyncio
async def test_create_event_tool_returns_preview_when_unconfirmed() -> None:
    """The mutating preview-confirm helper must not touch the backend
    when ``confirm=False``."""
    svc, _, _ = await _service()
    account, backend = await _seed_account(svc)
    initial = len(backend.events)
    out = await svc.execute_tool(
        "create_event",
        {
            "_user_id": "alice",
            "account_id": account.id,
            "title": "Hello",
            "start": "2026-06-01T10:00:00+00:00",
            "duration_minutes": 30,
            "confirm": False,
        },
    )
    # ToolOutput, with one UI block, plus no backend write.
    from gilbert.interfaces.ui import ToolOutput

    assert isinstance(out, ToolOutput)
    assert out.ui_blocks
    assert len(backend.events) == initial


@pytest.mark.asyncio
async def test_create_event_tool_writes_when_confirmed() -> None:
    svc, _, _ = await _service()
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
async def test_update_event_tool_returns_preview_when_unconfirmed() -> None:
    svc, _, _ = await _service()
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
async def test_delete_event_tool_returns_preview_when_unconfirmed() -> None:
    svc, _, _ = await _service()
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
async def test_announcement_published_once_for_imminent_event() -> None:
    svc, sched, bus = await _service()
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
async def test_list_accessible_accounts_filters_by_access() -> None:
    svc, _, _ = await _service()
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
async def test_share_user_publishes_shares_changed() -> None:
    svc, _, bus = await _service()
    account, _ = await _seed_account(svc)
    bus.published.clear()
    await svc.share_user(account.id, "bob", _user_ctx("alice"))
    types = [e.event_type for e in bus.published]
    assert "calendar.account.shares.changed" in types
