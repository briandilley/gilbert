"""Tests for UserContext and context propagation."""

import pytest

from gilbert.core.context import get_current_user, set_current_user
from gilbert.interfaces.auth import AuthInfo, UserContext, is_valid_tz

# --- UserContext ---


def test_system_sentinel() -> None:
    assert UserContext.SYSTEM.user_id == "system"
    assert UserContext.SYSTEM.email == "system@localhost"
    assert UserContext.SYSTEM.provider == "system"


def test_user_context_is_frozen() -> None:
    ctx = UserContext(user_id="u1", email="a@b.com", display_name="A")
    with pytest.raises(AttributeError):
        ctx.user_id = "u2"  # type: ignore[misc]


def test_user_context_roles_frozenset() -> None:
    ctx = UserContext(
        user_id="u1",
        email="a@b.com",
        display_name="A",
        roles=frozenset({"admin", "user"}),
    )
    assert "admin" in ctx.roles
    assert isinstance(ctx.roles, frozenset)


def test_user_context_defaults() -> None:
    ctx = UserContext(user_id="u1", email="a@b.com", display_name="A")
    assert ctx.roles == frozenset()
    assert ctx.provider == "local"
    assert ctx.session_id is None
    assert ctx.metadata == {}
    assert ctx.tz is None


def test_user_context_carries_tz() -> None:
    ctx = UserContext(
        user_id="u1",
        email="a@b.com",
        display_name="A",
        tz="America/Los_Angeles",
    )
    assert ctx.tz == "America/Los_Angeles"


def test_is_valid_tz_accepts_iana() -> None:
    assert is_valid_tz("UTC")
    assert is_valid_tz("America/Los_Angeles")
    assert is_valid_tz("Europe/Paris")


def test_is_valid_tz_rejects_garbage() -> None:
    assert not is_valid_tz("")
    assert not is_valid_tz("Not/A/Real/Zone")
    assert not is_valid_tz("UTC+5")  # not IANA-formatted


# --- AuthInfo ---


def test_auth_info_creation() -> None:
    info = AuthInfo(
        provider_type="google",
        provider_user_id="123",
        email="a@b.com",
        display_name="A",
        roles=frozenset({"admin"}),
        raw={"org": "test"},
    )
    assert info.provider_type == "google"
    assert info.provider_user_id == "123"
    assert "admin" in info.roles
    assert info.raw["org"] == "test"


# --- Context propagation ---


def test_get_current_user_default_is_system() -> None:
    # In a fresh context, should return SYSTEM.
    user = get_current_user()
    assert user.user_id == "system"


def test_set_and_get_current_user() -> None:
    ctx = UserContext(user_id="u1", email="a@b.com", display_name="A")
    set_current_user(ctx)
    assert get_current_user().user_id == "u1"
    # Reset to avoid leaking into other tests.
    set_current_user(UserContext.SYSTEM)
