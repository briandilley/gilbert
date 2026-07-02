"""Service-declared WS RPC roles: declared level applies only to the
declaring service's own frames; admin overrides still win."""

from __future__ import annotations

from typing import Any

from gilbert.interfaces.service import Service, ServiceInfo
from gilbert.interfaces.ws import WsRpcRoleProvider
from gilbert.web.ws_protocol import WsConnectionManager, _resolve_rpc_level


class _GameService(Service):
    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="mafia", capabilities=frozenset({"ws_handlers"}))

    def get_ws_handlers(self) -> dict[str, Any]:
        async def handler(conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
            return {}

        return {"mafia.game.join": handler, "mafia.host.abort": handler}

    def get_ws_rpc_roles(self) -> dict[str, str]:
        return {"mafia.": "everyone", "chat.": "everyone"}  # chat. must be ignored


class _SM:
    def __init__(self, services: list[Service]):
        self._services = services

    def get_all_by_capability(self, cap: str) -> list[Service]:
        return list(self._services)

    def get_by_capability(self, cap: str) -> Service | None:
        return None  # no event bus, no access_control


class _Gilbert:
    def __init__(self, services: list[Service]):
        self.service_manager = _SM(services)


class _Conn:
    def __init__(self, manager: WsConnectionManager, level: int):
        self.manager = manager
        self.user_level = level


def _manager() -> WsConnectionManager:
    mgr = WsConnectionManager()
    mgr.subscribe_to_bus(_Gilbert([_GameService()]))
    return mgr


def test_declared_role_applies_to_own_frames() -> None:
    mgr = _manager()
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.game.join") == 200
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.host.abort") == 200


def test_declared_role_ignored_for_foreign_prefix() -> None:
    """The service tried to declare 'chat.' but owns no chat.* handlers."""
    mgr = _manager()
    # chat.message.send is a core handler (or unregistered) — declared
    # 'chat.' from _GameService must not lower it below the default.
    assert mgr.resolve_declared_rpc_role("chat.message.send") is None


def test_protocol_isinstance() -> None:
    assert isinstance(_GameService(), WsRpcRoleProvider)
