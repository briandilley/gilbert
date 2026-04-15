"""Tests for WebApiService — dashboard nav filtering.

Focused on the action-style menu plumbing added in the "Restart" item:
nav entries can now be either navigable (``url``) or action-triggering
(``action``, no ``url``). The frontend dispatches on which field is
present, so if ``web_api.py`` silently drops ``action`` from the
serialized item — or if the ``default_url`` fallback picks an
action-only item — the menu breaks in ways unit tests are the only
reliable way to catch.

The restart RPC itself is ``plugins.restart_host``, which already had
its own call path and RBAC gate before the menu item was added; this
file only exercises the dashboard wiring that makes the menu item
appear and route correctly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gilbert.core.services.web_api import WebApiService
from gilbert.interfaces.acl import BUILTIN_ROLE_LEVELS

# ── Test doubles ─────────────────────────────────────────────────────


class _FakeServiceManager:
    """Minimal stand-in for ``ServiceManager`` covering the bits
    ``_ws_dashboard_get`` touches.

    ``get_by_capability`` returns the fake acl when asked for
    ``access_control`` and ``None`` for everything else (so the
    ``requires_capability`` filter is a no-op for the tests that don't
    care about it).
    """

    def __init__(self, acl: Any | None) -> None:
        self._acl = acl

    def get_by_capability(self, cap: str) -> Any | None:
        if cap == "access_control":
            return self._acl
        return None


class _FakeAcl:
    """``get_role_level`` is the only method ``_visible`` calls."""

    def get_role_level(self, role: str) -> int:
        return BUILTIN_ROLE_LEVELS.get(role, 100)


def _make_gilbert(acl: Any | None = None) -> Any:
    """Build a ``gilbert`` object with just enough shape to drive
    ``_ws_dashboard_get``. ``request_restart`` is a ``MagicMock`` so
    tests can assert it was (or wasn't) called from other entry
    points."""
    return SimpleNamespace(
        service_manager=_FakeServiceManager(acl),
        request_restart=MagicMock(),
    )


def _make_conn(gilbert: Any, *, user_level: int = 0) -> Any:
    """Shape the RPC handlers read: ``conn.manager.gilbert`` +
    ``conn.user_level``."""
    return SimpleNamespace(
        manager=SimpleNamespace(gilbert=gilbert),
        user_level=user_level,
    )


@pytest.fixture
def service() -> WebApiService:
    return WebApiService()


# ── Restart RPC permission defaults ──────────────────────────────────


def test_restart_host_is_admin_only_via_acl_defaults() -> None:
    """The ``plugins.restart_host`` frame type — which the Restart
    menu item fires — must resolve to the admin level (0) under the
    default RPC permission rules, so the WS framework's RBAC gate
    rejects non-admin callers before they ever reach the handler.

    Regression guard for anyone tempted to loosen
    ``DEFAULT_RPC_PERMISSIONS["plugins.": 0]`` — if that changes, the
    menu item's ``required_role`` in ``web_api.py`` needs to change
    in lockstep or the two gates will disagree (UI hides the button
    but the RPC accepts the frame, or vice versa)."""
    from gilbert.interfaces.acl import resolve_default_rpc_level

    assert resolve_default_rpc_level("plugins.restart_host") == 0


# ── dashboard.get nav wiring ─────────────────────────────────────────


async def test_dashboard_admin_sees_restart_menu_item(
    service: WebApiService,
) -> None:
    """For an admin caller, the Restart item must be present under
    the System group with the ``restart_host`` action payload and
    NO ``url`` field — the frontend distinguishes action items from
    navigation items on exactly that."""
    gilbert = _make_gilbert(acl=_FakeAcl())
    conn = _make_conn(gilbert, user_level=0)  # admin

    result = await service._ws_dashboard_get(conn, {"id": "dash-1"})

    assert result is not None
    nav = result["nav"]
    system = next(g for g in nav if g["key"] == "system")

    restart_items = [
        i for i in system["items"] if i.get("action") == "restart_host"
    ]
    assert len(restart_items) == 1, "expected exactly one Restart item"

    restart = restart_items[0]
    assert restart["label"] == "Restart"
    assert restart["required_role"] == "admin"
    # Action-style items MUST NOT carry a url — the frontend uses the
    # presence of ``action`` to decide whether to open a confirm
    # dialog vs navigate to a route.
    assert "url" not in restart


async def test_dashboard_non_admin_does_not_see_restart(
    service: WebApiService,
) -> None:
    """A ``user``-level caller must have the Restart item filtered
    out before the frame leaves the server, even though its parent
    System group may still appear because Scheduler is user-level."""
    gilbert = _make_gilbert(acl=_FakeAcl())
    conn = _make_conn(gilbert, user_level=100)  # user, not admin

    result = await service._ws_dashboard_get(conn, {"id": "dash-2"})

    assert result is not None
    nav = result["nav"]
    system_groups = [g for g in nav if g["key"] == "system"]

    for group in system_groups:
        actions = [i.get("action") for i in group["items"]]
        assert "restart_host" not in actions


async def test_dashboard_group_default_url_skips_action_only_items(
    service: WebApiService,
) -> None:
    """Regression: the ``default_url`` fallback must skip items that
    have no ``url`` (action-style entries like Restart). Before the
    action-items change, ``visible_items[0]["url"]`` would have
    KeyError-crashed for an admin whose only visible System child was
    the Restart item. The current code uses a generator that walks
    until it finds a navigable child, or keeps the hard-coded
    default.

    For an admin the default ``/settings`` is visible and should
    win; this test's primary job is to confirm the group URL is
    *non-empty* even in the presence of action-only items."""
    gilbert = _make_gilbert(acl=_FakeAcl())
    conn = _make_conn(gilbert, user_level=0)

    result = await service._ws_dashboard_get(conn, {"id": "dash-3"})

    assert result is not None
    system = next(g for g in result["nav"] if g["key"] == "system")
    assert system["url"], "system group must have a non-empty default url"
    # The group URL must point at a real navigable item, never at
    # the action-only Restart entry (which has no url field to begin
    # with, but belt-and-suspenders: assert the group URL isn't the
    # empty-string fallback either).
    url_bearing = [
        i.get("url") for i in system["items"] if i.get("url")
    ]
    assert system["url"] in url_bearing
