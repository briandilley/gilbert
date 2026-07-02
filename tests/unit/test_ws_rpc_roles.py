"""Service-declared WS RPC roles: declared level applies only to the
declaring service's own frames; admin overrides still win."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from gilbert.interfaces.service import Service, ServiceInfo
from gilbert.interfaces.ws import WsRpcRoleProvider
from gilbert.web.ws_protocol import WsConnectionManager, _resolve_rpc_level


async def _handler(conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
    return {}


class _GameService(Service):
    """Declares 'chat.' hoping to open up another service's frames.

    It legitimately owns one chat.*-prefixed handler (chat.mafia.hack),
    so the registration-time owns_match gate accepts the declaration —
    the per-frame owner scoping is what must keep it off foreign frames.
    """

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="mafia", capabilities=frozenset({"ws_handlers"}))

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "mafia.game.join": _handler,
            "mafia.host.abort": _handler,
            "chat.mafia.hack": _handler,
        }

    def get_ws_rpc_roles(self) -> dict[str, str]:
        return {"mafia.": "everyone", "chat.": "everyone"}


class _ChatService(Service):
    """Owns chat.* frames; declares no roles of its own."""

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="chat", capabilities=frozenset({"ws_handlers"}))

    def get_ws_handlers(self) -> dict[str, Any]:
        # chat.room.join is unlisted in DEFAULT_RPC_PERMISSIONS, so it
        # resolves via the "chat." prefix default (user, 100) — a level
        # a foreign "everyone" (200) declaration would visibly lower.
        return {"chat.message.send": _handler, "chat.room.join": _handler}


class _RivalService(Service):
    """Registers after _GameService and declares a conflicting 'mafia.'."""

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="rival", capabilities=frozenset({"ws_handlers"}))

    def get_ws_handlers(self) -> dict[str, Any]:
        return {"mafia.rival.spy": _handler}

    def get_ws_rpc_roles(self) -> dict[str, str]:
        return {"mafia.": "admin"}


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


def _manager(services: list[Service] | None = None) -> WsConnectionManager:
    mgr = WsConnectionManager()
    mgr.subscribe_to_bus(_Gilbert(services or [_GameService(), _ChatService()]))
    return mgr


def test_declared_role_applies_to_own_frames() -> None:
    mgr = _manager()
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.game.join") == 200
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.host.abort") == 200


def test_declared_role_ignored_for_foreign_frames() -> None:
    """_GameService's 'chat.' declaration must not touch _ChatService's frames."""
    mgr = _manager()
    # The frame's owner is the chat service, the declarer is mafia — the
    # per-owner scoping filters the declaration out entirely.
    assert mgr.resolve_declared_rpc_role("chat.message.send") is None
    assert mgr.resolve_declared_rpc_role("chat.room.join") is None
    # chat.room.join defaults to user (100) via the "chat." prefix; the
    # foreign "everyone" declaration must not lower it to 200.
    assert _resolve_rpc_level(_Conn(mgr, 200), "chat.room.join") == 100


def test_declared_role_applies_to_own_frames_under_foreign_looking_prefix() -> None:
    """The scoped, permitted case: 'chat.' does apply to the declarer's own
    chat.mafia.hack frame — scoping is by handler ownership, not namespace."""
    mgr = _manager()
    assert mgr.resolve_declared_rpc_role("chat.mafia.hack") == "everyone"
    assert _resolve_rpc_level(_Conn(mgr, 200), "chat.mafia.hack") == 200


def test_conflicting_declarations_first_registered_wins() -> None:
    mgr = _manager([_GameService(), _ChatService(), _RivalService()])
    # _GameService registered 'mafia.' → 'everyone' first; the rival's
    # 'mafia.' → 'admin' is skipped, and — being owned by mafia — the
    # surviving declaration doesn't apply to the rival's own frame either.
    assert mgr.resolve_declared_rpc_role("mafia.game.join") == "everyone"
    assert mgr.resolve_declared_rpc_role("mafia.rival.spy") is None


def test_protocol_isinstance() -> None:
    assert isinstance(_GameService(), WsRpcRoleProvider)


class _TypoService(Service):
    """Declares 'evryone' (typo for 'everyone') — not a built-in role."""

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="mafia", capabilities=frozenset({"ws_handlers"}))

    def get_ws_handlers(self) -> dict[str, Any]:
        return {"mafia.game.join": _handler}

    def get_ws_rpc_roles(self) -> dict[str, str]:
        return {"mafia.": "evryone"}


def test_unknown_declared_role_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """A typo'd declared role (e.g. 'evryone') must not fail open. It should
    be dropped at registration time so the frame resolves to the hardcoded
    default (100 = user) rather than a role that resolves to 200 (everyone)
    via the ACL service's unknown-role fallback."""
    with caplog.at_level(logging.WARNING):
        mgr = _manager([_TypoService()])
    assert mgr.resolve_declared_rpc_role("mafia.game.join") is None
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.game.join") == 100
    assert any("not a built-in role" in r.message for r in caplog.records)


def test_subscribe_to_bus_twice_resets_owner_and_declared_roles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for I4: re-running subscribe_to_bus (e.g. re-discovery)
    must reset _handler_owner and _declared_rpc_roles alongside _handlers,
    else stale owners survive and produce bogus conflict warnings — or a
    long-gone service's declared role keeps applying — on the next run."""
    mgr = WsConnectionManager()
    gilbert = _Gilbert([_GameService(), _ChatService()])

    mgr.subscribe_to_bus(gilbert)
    with caplog.at_level(logging.WARNING):
        mgr.subscribe_to_bus(gilbert)

    conflict_warnings = [r for r in caplog.records if "conflict" in r.message]
    assert not conflict_warnings, [r.message for r in conflict_warnings]

    # Declarations still resolve correctly after the second discovery.
    assert mgr.resolve_declared_rpc_role("mafia.game.join") == "everyone"
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.game.join") == 200
    assert mgr.resolve_declared_rpc_role("chat.message.send") is None
