"""Pure-data tests for the calendar interfaces — auth helpers + dataclasses.

Covers the access matrix (admin / owner / shared user / shared role /
no access) for the three authorization helpers, plus dataclass round
trips through ``to_dict`` / ``from_dict``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.calendar import (
    AttendeeResponseStatus,
    CalendarAccess,
    CalendarAccount,
    CalendarAttendee,
    CalendarEvent,
    EventStatus,
    EventVisibility,
    can_access_account,
    can_admin_account,
    determine_access,
)


def _ctx(user_id: str, *, roles: set[str] | None = None) -> UserContext:
    return UserContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id.title(),
        roles=frozenset(roles or set()),
    )


def _account(
    *,
    owner_user_id: str = "alice",
    shared_with_users: list[str] | None = None,
    shared_with_roles: list[str] | None = None,
) -> CalendarAccount:
    return CalendarAccount(
        id="cal_x",
        name="Test calendar",
        email_address="alice@example.com",
        backend_name="fake",
        owner_user_id=owner_user_id,
        shared_with_users=list(shared_with_users or []),
        shared_with_roles=list(shared_with_roles or []),
    )


# ── Access matrix ─────────────────────────────────────────────────────


class TestAccessMatrix:
    def test_owner_has_access(self) -> None:
        a = _account(owner_user_id="alice")
        assert can_access_account(_ctx("alice"), a) is True
        assert can_admin_account(_ctx("alice"), a) is True

    def test_admin_has_access(self) -> None:
        a = _account(owner_user_id="alice")
        admin = _ctx("bob", roles={"admin"})
        assert can_access_account(admin, a) is True
        assert can_admin_account(admin, a) is True

    def test_system_has_access(self) -> None:
        a = _account(owner_user_id="alice")
        assert can_access_account(UserContext.SYSTEM, a) is True
        assert can_admin_account(UserContext.SYSTEM, a) is True

    def test_shared_user_has_access_but_not_admin(self) -> None:
        a = _account(owner_user_id="alice", shared_with_users=["bob"])
        assert can_access_account(_ctx("bob"), a) is True
        assert can_admin_account(_ctx("bob"), a) is False

    def test_shared_role_has_access_but_not_admin(self) -> None:
        a = _account(owner_user_id="alice", shared_with_roles=["sales"])
        bob = _ctx("bob", roles={"sales"})
        assert can_access_account(bob, a) is True
        assert can_admin_account(bob, a) is False

    def test_no_relationship_has_no_access(self) -> None:
        a = _account(owner_user_id="alice")
        carol = _ctx("carol")
        assert can_access_account(carol, a) is False
        assert can_admin_account(carol, a) is False


class TestDetermineAccess:
    def test_owner_takes_precedence_over_admin(self) -> None:
        """An admin-roled user who is also the owner should see OWNER."""
        a = _account(owner_user_id="alice")
        alice_also_admin = _ctx("alice", roles={"admin"})
        assert determine_access(alice_also_admin, a) == CalendarAccess.OWNER

    def test_admin_only(self) -> None:
        a = _account(owner_user_id="alice")
        bob = _ctx("bob", roles={"admin"})
        assert determine_access(bob, a) == CalendarAccess.ADMIN

    def test_admin_who_is_also_shared_returns_admin(self) -> None:
        """Admin > shared_user precedence."""
        a = _account(owner_user_id="alice", shared_with_users=["bob"])
        bob_admin = _ctx("bob", roles={"admin"})
        assert determine_access(bob_admin, a) == CalendarAccess.ADMIN

    def test_shared_user(self) -> None:
        a = _account(owner_user_id="alice", shared_with_users=["bob"])
        assert determine_access(_ctx("bob"), a) == CalendarAccess.SHARED_USER

    def test_shared_role(self) -> None:
        a = _account(owner_user_id="alice", shared_with_roles=["sales"])
        bob = _ctx("bob", roles={"sales"})
        assert determine_access(bob, a) == CalendarAccess.SHARED_ROLE

    def test_no_relationship_returns_none(self) -> None:
        a = _account(owner_user_id="alice")
        assert determine_access(_ctx("carol"), a) is None

    def test_shared_user_beats_shared_role(self) -> None:
        a = _account(
            owner_user_id="alice",
            shared_with_users=["bob"],
            shared_with_roles=["sales"],
        )
        bob = _ctx("bob", roles={"sales"})
        assert determine_access(bob, a) == CalendarAccess.SHARED_USER


# ── Dataclass round-trips ────────────────────────────────────────────


class TestCalendarAccountRoundTrip:
    def test_to_from_dict(self) -> None:
        a = CalendarAccount(
            id="cal_a",
            name="Work",
            email_address="alice@example.com",
            backend_name="google_calendar",
            backend_config={"k": "v"},
            calendar_id="primary",
            timezone="America/New_York",
            working_hours_start_hour=8,
            working_hours_end_hour=17,
            owner_user_id="alice",
            shared_with_users=["bob"],
            shared_with_roles=["sales"],
            poll_enabled=True,
            poll_interval_sec=600,
            upcoming_event_lookahead_minutes=20,
            health="unhealthy",
            last_error="boom",
            last_error_at="2026-05-09T12:00:00+00:00",
            created_at="2026-05-09T11:59:00+00:00",
        )
        dumped = a.to_dict()
        loaded = CalendarAccount.from_dict(dumped)
        assert loaded == a

    def test_from_dict_uses_defaults_for_missing_fields(self) -> None:
        loaded = CalendarAccount.from_dict({"id": "cal_x", "name": "X"})
        assert loaded.id == "cal_x"
        assert loaded.timezone == "UTC"
        assert loaded.working_hours_start_hour == 9
        assert loaded.working_hours_end_hour == 18
        assert loaded.poll_enabled is True
        assert loaded.poll_interval_sec == 300

    def test_from_dict_accepts_id_alias(self) -> None:
        # The accounts collection stores rows keyed by ``_id`` — the
        # dataclass should pick that up if ``id`` is missing.
        loaded = CalendarAccount.from_dict({"_id": "cal_y", "name": "Y"})
        assert loaded.id == "cal_y"


class TestCalendarEventSerialization:
    def test_to_dict_preserves_attendee_status(self) -> None:
        evt = CalendarEvent(
            event_id="e1",
            calendar_id="primary",
            account_id="cal_a",
            title="Standup",
            start=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
            end=datetime(2026, 5, 9, 9, 30, tzinfo=UTC),
            attendees=(
                CalendarAttendee(
                    email="alice@example.com",
                    response_status=AttendeeResponseStatus.ACCEPTED,
                    is_organizer=True,
                ),
            ),
            visibility=EventVisibility.DEFAULT,
            status=EventStatus.CONFIRMED,
        )
        dumped = evt.to_dict()
        assert dumped["attendees"][0]["response_status"] == "accepted"
        assert dumped["status"] == "confirmed"
        assert dumped["visibility"] == "default"


class TestAttendeeFromDict:
    def test_unknown_response_status_falls_back_to_needs_action(self) -> None:
        loaded = CalendarAttendee.from_dict({"email": "x@y", "response_status": "garbage"})
        assert loaded.response_status == AttendeeResponseStatus.NEEDS_ACTION


# ── Sanity: the helpers really do derive admin from user_ctx and not
# from a caller-passed bool. The spec was explicit that callers must
# never pass an ad-hoc is_admin — let's verify the signature.


def test_helpers_take_no_is_admin_kwarg() -> None:
    import inspect

    sig = inspect.signature(can_access_account)
    assert "is_admin" not in sig.parameters
    sig = inspect.signature(can_admin_account)
    assert "is_admin" not in sig.parameters
    sig = inspect.signature(determine_access)
    assert "is_admin" not in sig.parameters


# ── Backend registry sanity test ─────────────────────────────────────


def test_calendar_backend_registry_subclassing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subclass with ``backend_name`` registers automatically."""
    from gilbert.interfaces.calendar import CalendarBackend

    class _DummyBackend(CalendarBackend):
        backend_name = "_test_dummy"

        async def initialize(self, config: dict | None = None) -> None: ...

        async def close(self) -> None: ...

        async def list_calendars(self) -> list[dict]:
            return []

        async def list_events(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return []

        async def get_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return None

        async def free_busy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return []

        async def create_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def update_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def delete_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    try:
        registry = CalendarBackend.registered_backends()
        assert "_test_dummy" in registry
    finally:
        CalendarBackend._registry.pop("_test_dummy", None)
