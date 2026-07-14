"""Tests for AIService's slash-command discovery, RBAC filtering, and
plugin namespacing.

These tests cover the parts of slash-command routing that live inside
``AIService`` itself — the ``_slash_commands_for_user`` helper, the
``slash.commands.list`` RPC handler, and the plugin namespace
resolution (``_resolve_slash_namespace``). The pure parser has its own
tests in ``test_slash_commands.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import AIService
from gilbert.core.services.health import HealthService
from gilbert.core.services.media_library import MediaLibraryService
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

# --- Fixtures & stubs ------------------------------------------------


class _CoreToolService(Service):
    """A tool provider that lives in a core module (empty namespace)."""

    def __init__(self, tools: list[ToolDefinition]) -> None:
        self._tools = tools

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="core_tools",
            capabilities=frozenset({"ai_tools"}),
        )

    @property
    def tool_provider_name(self) -> str:
        return "core_tools"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return list(self._tools)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return f"{name}: ok"


class _PluginToolService(_CoreToolService):
    """Pretend this came from a plugin module.

    The real plugin loader tags services with a ``gilbert_plugin_<name>``
    module prefix; we simulate that by reaching into ``__class__``.
    """

    @property
    def tool_provider_name(self) -> str:
        return "plugin_tools"


class _PluginToolServiceWithShortNamespace(_CoreToolService):
    """Plugin service that declares a short namespace via class attr."""

    slash_namespace = "currev"

    @property
    def tool_provider_name(self) -> str:
        return "short_ns_plugin"


@dataclass
class _StubACL:
    """Minimal ``AccessControlProvider`` — admin=0, user=100, everyone=200."""

    _levels = {"admin": 0, "user": 100, "everyone": 200}

    def get_role_level(self, role_name: str) -> int:
        return self._levels.get(role_name, 100)

    def get_effective_level(self, user_ctx: UserContext) -> int:
        if "admin" in user_ctx.roles:
            return 0
        if "user" in user_ctx.roles:
            return 100
        return 200

    def resolve_rpc_level(self, frame_type: str) -> int:
        return 100

    def get_rpc_override_level(self, frame_type: str) -> int | None:
        return None

    def check_collection_read(self, user_ctx: UserContext, collection: str) -> bool:
        return True

    def check_collection_write(self, user_ctx: UserContext, collection: str) -> bool:
        return True


def _resolver_with(services: list[Service]) -> ServiceResolver:
    """Build a resolver whose ``get_all("ai_tools")`` returns *services*."""
    mock = AsyncMock(spec=ServiceResolver)
    mock.get_capability = AsyncMock(return_value=None)
    mock.require_capability = AsyncMock(side_effect=LookupError("nope"))

    def get_all(cap: str) -> list[Service]:
        if cap == "ai_tools":
            return services
        return []

    mock.get_all = get_all  # type: ignore[method-assign]
    return mock


def _make_service(acl: AccessControlProvider) -> AIService:
    svc = AIService()
    svc._enabled = True
    svc._acl_svc = acl  # type: ignore[attr-defined]
    return svc


# --- Sample tool definitions -----------------------------------------

_ECHO_TOOL = ToolDefinition(
    name="echo",
    slash_command="echo",
    description="Echo input.",
    parameters=[
        ToolParameter(
            name="text",
            type=ToolParameterType.STRING,
            description="Text to echo",
        ),
    ],
    required_role="user",
)

_ADMIN_ONLY_TOOL = ToolDefinition(
    name="admin_reboot",
    slash_command="admin_reboot",
    description="Admin-only tool.",
    parameters=[],
    required_role="admin",
)

_EVERYONE_TOOL = ToolDefinition(
    name="ping",
    slash_command="ping",
    description="Ping.",
    parameters=[],
    required_role="everyone",
)

_NO_SLASH_TOOL = ToolDefinition(
    name="internal",
    description="No slash_command set — AI-only.",
    parameters=[],
    required_role="user",
)


# --- RBAC filtering --------------------------------------------------


def test_slash_commands_for_user_filters_admin_only_from_non_admin() -> None:
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService(
        [
            _ECHO_TOOL,
            _ADMIN_ONLY_TOOL,
            _EVERYONE_TOOL,
        ]
    )
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "echo" in cmds  # user role
    assert "ping" in cmds  # everyone role
    assert "admin_reboot" not in cmds  # admin-only, filtered out


def test_slash_commands_for_user_includes_everything_for_admin() -> None:
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService(
        [
            _ECHO_TOOL,
            _ADMIN_ONLY_TOOL,
            _EVERYONE_TOOL,
        ]
    )
    svc._resolver = _resolver_with([provider])

    admin_ctx = UserContext(
        user_id="a1",
        email="a1@example.com",
        display_name="Admin",
        roles=frozenset({"admin"}),
    )
    cmds = svc._slash_commands_for_user(admin_ctx)

    assert "echo" in cmds
    assert "ping" in cmds
    assert "admin_reboot" in cmds


def test_tools_without_slash_command_are_skipped() -> None:
    """Tools that didn't opt in must NOT show up in the slash list."""
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService([_ECHO_TOOL, _NO_SLASH_TOOL])
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "echo" in cmds
    assert "internal" not in cmds
    assert len(cmds) == 1


def test_everyone_user_only_sees_everyone_commands() -> None:
    """The ``guest``-style ``everyone`` role (unauthenticated visitor)
    should only see tools flagged ``required_role="everyone"``."""
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService(
        [
            _ECHO_TOOL,
            _ADMIN_ONLY_TOOL,
            _EVERYONE_TOOL,
        ]
    )
    svc._resolver = _resolver_with([provider])

    guest_ctx = UserContext(
        user_id="guest",
        email="",
        display_name="Guest",
        roles=frozenset({"everyone"}),
    )
    cmds = svc._slash_commands_for_user(guest_ctx)

    assert "ping" in cmds  # everyone
    assert "echo" not in cmds  # user (guest doesn't have it)
    assert "admin_reboot" not in cmds


# --- Plugin namespacing ----------------------------------------------


def test_core_services_get_no_namespace_prefix() -> None:
    """Tools from core modules are exposed as bare ``/name``."""
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService([_ECHO_TOOL])
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)
    assert "echo" in cmds
    assert "." not in next(iter(cmds))


def test_explicit_slash_namespace_overrides_auto_detect() -> None:
    """When a provider class sets ``slash_namespace``, that wins."""
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _PluginToolServiceWithShortNamespace([_ECHO_TOOL])
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "currev.echo" in cmds
    assert "echo" not in cmds


def test_plugin_module_prefix_is_auto_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Services whose ``__module__`` starts with ``gilbert_plugin_`` get
    the module's sanitized name as their namespace, even without an
    explicit ``slash_namespace`` attribute."""
    acl = _StubACL()
    svc = _make_service(acl)

    # Fake the module name as if this class were loaded from a plugin
    class _FakePluginService(_CoreToolService):
        pass

    monkeypatch.setattr(
        _FakePluginService,
        "__module__",
        "gilbert_plugin_my_cool_plugin",
    )

    provider = _FakePluginService([_ECHO_TOOL])
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "my_cool_plugin.echo" in cmds
    assert "echo" not in cmds


def test_namespace_isolation_prevents_slash_collisions() -> None:
    """Two plugins can both claim ``/echo`` as long as their tool names
    are distinct — namespacing keeps the slash commands unique even
    though they share the same local slash_command value.
    """
    acl = _StubACL()
    svc = _make_service(acl)

    # Tool names must be distinct (the tool registry deduplicates by
    # name regardless of namespace), but both claim slash_command="echo".
    tool_a = ToolDefinition(
        name="plugin1_echo",
        slash_command="echo",
        description="Plugin 1's echo",
        required_role="user",
    )
    tool_b = ToolDefinition(
        name="plugin2_echo",
        slash_command="echo",
        description="Plugin 2's echo",
        required_role="user",
    )

    class _Plugin1(_CoreToolService):
        slash_namespace = "plugin1"

    class _Plugin2(_CoreToolService):
        slash_namespace = "plugin2"

    svc._resolver = _resolver_with(
        [
            _Plugin1([tool_a]),
            _Plugin2([tool_b]),
        ]
    )

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "plugin1.echo" in cmds
    assert "plugin2.echo" in cmds
    assert "echo" not in cmds  # neither claims the bare form


# --- slash.commands.list RPC handler ---------------------------------


class _FakeWsConn:
    def __init__(self, user_ctx: UserContext) -> None:
        self.user_ctx = user_ctx


# --- Grouped commands -----------------------------------------------


_RADIO_START_TOOL = ToolDefinition(
    name="radio_start",
    slash_group="radio",
    slash_command="start",
    description="Start the radio DJ.",
    parameters=[
        ToolParameter(
            name="genre",
            type=ToolParameterType.STRING,
            description="Genre to start with",
            required=False,
        ),
    ],
    required_role="user",
)

_RADIO_STOP_TOOL = ToolDefinition(
    name="radio_stop",
    slash_group="radio",
    slash_command="stop",
    description="Stop the radio DJ.",
    required_role="user",
)


def test_grouped_commands_are_keyed_by_full_form() -> None:
    """Registry keys should be ``"radio start"`` / ``"radio stop"`` —
    the user-visible grouped form, not the internal tool name."""
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService([_RADIO_START_TOOL, _RADIO_STOP_TOOL])
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "radio start" in cmds
    assert "radio stop" in cmds
    # Bare forms should NOT be registered when a group is set.
    assert "start" not in cmds
    assert "stop" not in cmds
    assert "radio" not in cmds


def test_match_slash_command_prefers_grouped_form() -> None:
    """``/radio start chill`` should match the two-word key ``"radio start"``,
    not the bare ``"radio"`` even if both exist."""
    registry = {
        "radio": (object(), object()),  # type: ignore[dict-item]
        "radio start": (object(), object()),  # type: ignore[dict-item]
    }
    # Dummy lookups — we only care about the key that gets returned
    assert (
        AIService._match_slash_command(
            "/radio start chill",
            registry,
        )
        == "radio start"
    )


def test_match_slash_command_falls_back_to_bare_form() -> None:
    """When the second word isn't a known subcommand, the first-word
    match still wins."""
    registry = {"radio": (object(), object())}  # type: ignore[dict-item]
    assert (
        AIService._match_slash_command(
            "/radio chill beats",
            registry,
        )
        == "radio"
    )


def test_match_slash_command_returns_none_for_unknown() -> None:
    registry = {"announce": (object(), object())}  # type: ignore[dict-item]
    assert AIService._match_slash_command("/radio start", registry) is None


def test_match_slash_command_handles_namespaced_groups() -> None:
    """Plugin-prefixed grouped commands (``/currev.sync status``) should
    also match via the two-word path — the dot stays attached to the
    group because the first space is the separator."""
    registry = {
        "currev.sync status": (object(), object()),  # type: ignore[dict-item]
    }
    assert (
        AIService._match_slash_command(
            "/currev.sync status",
            registry,
        )
        == "currev.sync status"
    )


def test_grouped_dispatch_with_namespace() -> None:
    """Plugin + group together: /ns.group cmd."""
    acl = _StubACL()
    svc = _make_service(acl)

    class _PluginService(_CoreToolService):
        slash_namespace = "currev"

    provider = _PluginService([_RADIO_START_TOOL])
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "currev.radio start" in cmds


def test_media_library_slash_commands_use_single_media_prefix() -> None:
    """Media library is a core service, so grouped slash commands should
    register as /media clients, not /media.media clients."""
    acl = _StubACL()
    svc = _make_service(acl)
    media = MediaLibraryService()
    media._enabled = True
    svc._resolver = _resolver_with([media])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "media clients" in cmds
    assert "media.media clients" not in cmds


def test_health_slash_commands_use_single_health_prefix() -> None:
    """Health is a core service with grouped slash commands, so commands
    should register as /health links, not /health.health links."""
    acl = _StubACL()
    svc = _make_service(acl)
    health = HealthService()
    health._enabled = True
    svc._resolver = _resolver_with([health])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "health links" in cmds
    assert "health.health links" not in cmds


def test_media_now_slash_survives_music_now_playing_tool_collision() -> None:
    """Music and media both have a "now playing" concept, but both slash
    commands must remain callable."""
    acl = _StubACL()
    svc = _make_service(acl)
    media = MediaLibraryService()
    media._enabled = True

    class _NowPlayingBackend:
        supports_now_playing = True
        supports_continue_watching = False
        supports_recently_added = False
        supports_seek = False
        supports_next_episode = False

    media._backends = {"plex": _NowPlayingBackend()}  # type: ignore[assignment]
    music_now = ToolDefinition(
        name="now_playing",
        slash_group="music",
        slash_command="now",
        description="Music now playing.",
        required_role="everyone",
    )
    svc._resolver = _resolver_with([_CoreToolService([music_now]), media])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    cmds = svc._slash_commands_for_user(user_ctx)

    assert "music now" in cmds
    assert "media now" in cmds


# --- slash.commands.list RPC handler ---------------------------------


async def test_ws_slash_commands_list_returns_filtered_commands() -> None:
    """The WS handler should return exactly what the user can invoke,
    with full namespaced command names and parameter metadata."""
    acl = _StubACL()
    svc = _make_service(acl)
    provider = _CoreToolService(
        [
            _ECHO_TOOL,
            _ADMIN_ONLY_TOOL,
            _EVERYONE_TOOL,
        ]
    )
    svc._resolver = _resolver_with([provider])

    user_ctx = UserContext(
        user_id="u1",
        email="u1@example.com",
        display_name="User",
        roles=frozenset({"user"}),
    )
    conn = _FakeWsConn(user_ctx)
    result = await svc._ws_slash_commands_list(conn, {"id": "req-1"})

    assert result is not None
    assert result["type"] == "slash.commands.list.result"
    assert result["ref"] == "req-1"

    commands = {c["command"] for c in result["commands"]}
    assert "echo" in commands
    assert "ping" in commands
    assert "admin_reboot" not in commands  # filtered

    echo_entry = next(c for c in result["commands"] if c["command"] == "echo")
    assert echo_entry["tool_name"] == "echo"
    assert echo_entry["required_role"] == "user"
    assert echo_entry["usage"].startswith("/echo")
    param_names = {p["name"] for p in echo_entry["parameters"]}
    assert "text" in param_names
