"""Guests can list plugin routes/panels; per-entry role filter still applies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gilbert.interfaces.acl import resolve_default_rpc_level


def test_ui_listing_rpcs_are_everyone_level() -> None:
    assert resolve_default_rpc_level("ui.routes.list") == 200
    assert resolve_default_rpc_level("ui.panels.list") == 200


class _Route:
    def __init__(self, path: str, panel_id: str, required_role: str) -> None:
        self.path = path
        self.panel_id = panel_id
        self.label = panel_id
        self.description = ""
        self.icon = ""
        self.required_role = required_role
        self.requires_capability = ""


class _Panel:
    def __init__(self, panel_id: str, slot: str, required_role: str) -> None:
        self.panel_id = panel_id
        self.slot = slot
        self.required_role = required_role
        self.label = panel_id
        self.description = ""
        self.icon = ""
        self.requires_capability = ""


class _Plugin:
    def __init__(self, routes: list[_Route], panels: list[_Panel]) -> None:
        self._routes = routes
        self._panels = panels

    def ui_routes(self) -> list[_Route]:
        return self._routes

    def ui_panels(self) -> list[_Panel]:
        return self._panels

    def metadata(self) -> SimpleNamespace:
        return SimpleNamespace(name="test-plugin")


class _PluginEntry:
    def __init__(self, plugin: _Plugin) -> None:
        self.plugin = plugin


class _ServiceManager:
    def get_by_capability(self, cap: str) -> Any:
        return None


class _Gilbert:
    def __init__(self, entry: _PluginEntry) -> None:
        self.service_manager = _ServiceManager()
        self._entry = entry

    def list_loaded_plugins(self) -> list[_PluginEntry]:
        return [self._entry]


class _Conn:
    def __init__(self, user_level: int) -> None:
        self.user_level = user_level
        self.manager = SimpleNamespace(gilbert=None)


@pytest.fixture
def plugin_entry() -> _PluginEntry:
    routes = [
        _Route("/mafia", "mafia-panel", "everyone"),
        _Route("/admin-only", "admin-panel", "admin"),
        _Route("/user-only", "user-panel", "user"),
    ]
    panels = [
        _Panel("mafia-panel", "main", "everyone"),
        _Panel("admin-panel", "main", "admin"),
        _Panel("user-panel", "main", "user"),
    ]
    return _PluginEntry(_Plugin(routes, panels))


@pytest.mark.asyncio
async def test_ws_ui_routes_list_guest_sees_only_everyone_routes(
    plugin_entry: _PluginEntry,
) -> None:
    from gilbert.core.services.web_api import WebApiService

    svc = WebApiService.__new__(WebApiService)
    conn = _Conn(user_level=200)
    conn.manager = SimpleNamespace(gilbert=_Gilbert(plugin_entry))

    result = await svc._ws_ui_routes_list(conn, {"id": "r1"})

    paths = {r["path"] for r in result["routes"]}
    assert "/mafia" in paths
    assert "/admin-only" not in paths
    assert "/user-only" not in paths


@pytest.mark.asyncio
async def test_ws_ui_panels_list_guest_sees_only_everyone_panels(
    plugin_entry: _PluginEntry,
) -> None:
    from gilbert.core.services.plugin_manager import PluginManagerService

    svc = PluginManagerService.__new__(PluginManagerService)
    conn = _Conn(user_level=200)
    conn.manager = SimpleNamespace(gilbert=_Gilbert(plugin_entry))

    result = await svc._ws_ui_panels_list(conn, {"id": "p1"})

    panel_ids = {p["panel_id"] for p in result["panels"]}
    assert "mafia-panel" in panel_ids
    assert "admin-panel" not in panel_ids
    assert "user-panel" not in panel_ids
