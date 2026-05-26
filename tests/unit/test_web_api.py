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
        list_loaded_plugins=lambda: [],
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

    restart_items = [i for i in system["items"] if i.get("action") == "restart_host"]
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


async def test_media_group_hidden_when_no_plugin_contributes(
    service: WebApiService,
) -> None:
    """The Media nav group is a placeholder for plugin-contributed
    ``ui_routes(... nav_parent_group="media")`` entries — it has no
    built-in children. When no plugin populates it, the visibility
    filter must drop the group entirely, not render it as a dead
    leaf (``items: []`` would otherwise hit the leaf branch and the
    nav bar would render an unclickable "Media" entry).

    Regression guard for the ``placeholder_group`` flag in
    ``web_api.py``."""
    gilbert = _make_gilbert(acl=_FakeAcl())
    conn = _make_conn(gilbert, user_level=0)  # admin sees everything else

    result = await service._ws_dashboard_get(conn, {"id": "dash-media-empty"})

    assert result is not None
    keys = {g["key"] for g in result["nav"]}
    assert "media" not in keys, (
        "Media group must not appear when no plugin contributes children"
    )


async def test_media_group_appears_when_plugin_contributes(
    service: WebApiService,
) -> None:
    """When a loaded plugin's ``ui_routes()`` adds a child under
    ``nav_parent_group="media"``, the placeholder group flips into a
    real navigable group with that child as its single item."""
    from gilbert.interfaces.plugin import UIRoute

    class _StubPlugin:
        def metadata(self) -> Any:
            return SimpleNamespace(name="andon-fm")

        def ui_routes(self) -> list[UIRoute]:
            return [
                UIRoute(
                    path="/media/andon-fm",
                    panel_id="andon_fm.page",
                    label="Andon FM",
                    icon="radio",
                    required_role="user",
                    add_to_nav=True,
                    nav_parent_group="media",
                )
            ]

        def nav_contributions(self) -> list[Any]:
            return []

    gilbert = _make_gilbert(acl=_FakeAcl())
    gilbert.list_loaded_plugins = lambda: [
        SimpleNamespace(plugin=_StubPlugin())
    ]
    conn = _make_conn(gilbert, user_level=0)

    result = await service._ws_dashboard_get(conn, {"id": "dash-media-full"})

    assert result is not None
    media = next((g for g in result["nav"] if g["key"] == "media"), None)
    assert media is not None, "Media group must appear once a plugin contributes"
    labels = [i["label"] for i in media["items"]]
    assert "Andon FM" in labels
    # default url should fall back to the contributed child's path
    assert media["url"] == "/media/andon-fm"


async def test_route_with_requires_capability_hidden_when_service_disabled(
    service: WebApiService,
) -> None:
    """A plugin route declaring ``requires_capability`` must
    disappear from nav when nothing advertises that capability —
    e.g. the toggleable service the plugin owns is turned off.

    Pairs with the Andon FM service-toggle wiring: the nav-merge
    propagates ``requires_capability`` onto the item dict, and
    ``_visible`` consults the service manager. With no service at
    all (``get_by_capability`` returns None) the item is filtered
    out and — because the Media group has no other children — the
    whole group disappears via the existing placeholder logic."""
    from gilbert.interfaces.plugin import UIRoute

    class _StubPlugin:
        def metadata(self) -> Any:
            return SimpleNamespace(name="andon-fm")

        def ui_routes(self) -> list[UIRoute]:
            return [
                UIRoute(
                    path="/media/andon-fm",
                    panel_id="andon_fm.page",
                    label="Andon FM",
                    required_role="user",
                    requires_capability="andon_fm",
                    add_to_nav=True,
                    nav_parent_group="media",
                )
            ]

        def nav_contributions(self) -> list[Any]:
            return []

    gilbert = _make_gilbert(acl=_FakeAcl())
    gilbert.list_loaded_plugins = lambda: [
        SimpleNamespace(plugin=_StubPlugin())
    ]
    conn = _make_conn(gilbert, user_level=0)

    result = await service._ws_dashboard_get(conn, {"id": "dash-cap-off"})

    assert result is not None
    media = next((g for g in result["nav"] if g["key"] == "media"), None)
    assert media is None, (
        "Media group must stay hidden when the route's required "
        "capability isn't live (toggleable service disabled)"
    )


async def test_route_with_requires_capability_visible_when_service_enabled(
    service: WebApiService,
) -> None:
    """Mirror: when a service advertising the required capability
    IS enabled, the route's nav entry appears."""
    from gilbert.interfaces.plugin import UIRoute

    class _StubPlugin:
        def metadata(self) -> Any:
            return SimpleNamespace(name="andon-fm")

        def ui_routes(self) -> list[UIRoute]:
            return [
                UIRoute(
                    path="/media/andon-fm",
                    panel_id="andon_fm.page",
                    label="Andon FM",
                    required_role="user",
                    requires_capability="andon_fm",
                    add_to_nav=True,
                    nav_parent_group="media",
                )
            ]

        def nav_contributions(self) -> list[Any]:
            return []

    class _ServiceManagerWithCap:
        def __init__(self, acl: Any | None) -> None:
            self._acl = acl

        def get_by_capability(self, cap: str) -> Any | None:
            if cap == "access_control":
                return self._acl
            if cap == "andon_fm":
                return SimpleNamespace(enabled=True)
            return None

    gilbert = _make_gilbert(acl=_FakeAcl())
    gilbert.service_manager = _ServiceManagerWithCap(_FakeAcl())
    gilbert.list_loaded_plugins = lambda: [
        SimpleNamespace(plugin=_StubPlugin())
    ]
    conn = _make_conn(gilbert, user_level=0)

    result = await service._ws_dashboard_get(conn, {"id": "dash-cap-on"})

    assert result is not None
    media = next((g for g in result["nav"] if g["key"] == "media"), None)
    assert media is not None
    assert any(i["label"] == "Andon FM" for i in media["items"])


async def test_toplevel_route_hidden_when_service_disabled(
    service: WebApiService,
) -> None:
    """Sibling of ``test_route_with_requires_capability_hidden_when_service_disabled``
    but for the case where the route has NO ``nav_parent_group`` —
    it synthesizes a NEW top-level entry instead of slotting into
    an existing group like Media.

    Regression: when synthesizing the new group from the item, the
    nav-merge logic copied label/url/icon/required_role but NOT
    ``requires_capability``. The resulting leaf had no gating
    field, so ``_visible(group)`` returned True even when the
    underlying service was disabled — leaving a dead top-level
    nav entry that landed on a blank page. Voice-agent's ``/voice``
    route hit this whenever the service was toggled off in
    Settings → Services.
    """
    from gilbert.interfaces.plugin import UIRoute

    class _StubPlugin:
        def metadata(self) -> Any:
            return SimpleNamespace(name="voice-agent")

        def ui_routes(self) -> list[UIRoute]:
            return [
                UIRoute(
                    path="/voice",
                    panel_id="voice_agent.page",
                    label="Voice",
                    icon="mic",
                    required_role="user",
                    requires_capability="voice_agent",
                    add_to_nav=True,
                    # ``nav_parent_group=""`` (default) → synthesize
                    # a new top-level group from the route itself.
                    # This is the path the bug lived on.
                )
            ]

        def nav_contributions(self) -> list[Any]:
            return []

    gilbert = _make_gilbert(acl=_FakeAcl())
    gilbert.list_loaded_plugins = lambda: [
        SimpleNamespace(plugin=_StubPlugin())
    ]
    conn = _make_conn(gilbert, user_level=0)

    result = await service._ws_dashboard_get(conn, {"id": "dash-voice-off"})

    assert result is not None
    voice = next((g for g in result["nav"] if g["key"] == "voice"), None)
    assert voice is None, (
        "Synthesized top-level route entry must stay hidden when the "
        "route's required capability isn't live (service disabled)"
    )


async def test_toplevel_route_visible_when_service_enabled(
    service: WebApiService,
) -> None:
    """Mirror of the disabled-case above — when a service advertising
    the required capability IS enabled, the synthesized top-level
    nav entry appears."""
    from gilbert.interfaces.plugin import UIRoute

    class _StubPlugin:
        def metadata(self) -> Any:
            return SimpleNamespace(name="voice-agent")

        def ui_routes(self) -> list[UIRoute]:
            return [
                UIRoute(
                    path="/voice",
                    panel_id="voice_agent.page",
                    label="Voice",
                    icon="mic",
                    required_role="user",
                    requires_capability="voice_agent",
                    add_to_nav=True,
                )
            ]

        def nav_contributions(self) -> list[Any]:
            return []

    class _ServiceManagerWithCap:
        def __init__(self, acl: Any | None) -> None:
            self._acl = acl

        def get_by_capability(self, cap: str) -> Any | None:
            if cap == "access_control":
                return self._acl
            if cap == "voice_agent":
                return SimpleNamespace(enabled=True)
            return None

    gilbert = _make_gilbert(acl=_FakeAcl())
    gilbert.service_manager = _ServiceManagerWithCap(_FakeAcl())
    gilbert.list_loaded_plugins = lambda: [
        SimpleNamespace(plugin=_StubPlugin())
    ]
    conn = _make_conn(gilbert, user_level=0)

    result = await service._ws_dashboard_get(conn, {"id": "dash-voice-on"})

    assert result is not None
    voice = next((g for g in result["nav"] if g["key"] == "voice"), None)
    assert voice is not None
    assert voice["url"] == "/voice"


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
    url_bearing = [i.get("url") for i in system["items"] if i.get("url")]
    assert system["url"] in url_bearing
