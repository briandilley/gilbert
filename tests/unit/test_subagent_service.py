"""Tests for the core SubagentService (engine)."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.services.subagent import _DEFAULT_PREAMBLE, SubagentService
from gilbert.interfaces.ai import AIProvider, ChatTurnResult
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.context import _current_user
from gilbert.interfaces.tools import ToolParameterType, ToolProvider


class _FakeAI:
    """Minimal AIProvider stand-in: records the chat() call, returns canned text."""

    def __init__(self, text: str = "subagent result") -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = text

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        user_ctx: UserContext | None = None,
        system_prompt: str | None = None,
        ai_call: str | None = None,
        attachments: Any = None,
        model: str = "",
        backend_override: str = "",
        ai_profile: str = "",
        max_tool_rounds: int | None = None,
        between_rounds_callback: Any = None,
        mid_round_interrupt: Any = None,
        headless: bool = False,
        source: str = "",
    ) -> ChatTurnResult:
        self.calls.append(
            {
                "user_message": user_message,
                "conversation_id": conversation_id,
                "user_ctx": user_ctx,
                "system_prompt": system_prompt,
                "ai_call": ai_call,
                "ai_profile": ai_profile,
                "max_tool_rounds": max_tool_rounds,
                "headless": headless,
                "source": source,
            }
        )
        return ChatTurnResult(
            response_text=self._text,
            conversation_id="conv-1",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )


def _resolver(**caps: Any) -> Any:
    class _R:
        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_all(self, name: str) -> list[Any]:
            return []

    return _R()


def test_fake_ai_satisfies_aiprovider() -> None:
    # runtime_checkable: presence of chat() is enough; guards the rest of the suite.
    assert isinstance(_FakeAI(), AIProvider)


def test_service_info_declares_subagent_capability() -> None:
    info = SubagentService().service_info()
    assert info.name == "subagent"
    assert "subagent" in info.capabilities
    assert "ai_chat" in info.requires
    assert info.toggleable is True


def test_config_params_include_enabled_and_ai_prompts() -> None:
    params = SubagentService().config_params()
    by_key = {p.key: p for p in params}
    assert by_key["enabled"].type == ToolParameterType.BOOLEAN
    # Shared preamble + the general-purpose type prompt are both AI-authorable.
    assert by_key["preamble"].ai_prompt is True
    assert by_key["preamble"].multiline is True
    assert by_key["general_purpose_system_prompt"].ai_prompt is True


@pytest.mark.asyncio
async def test_on_config_changed_caches_prompt_overrides() -> None:
    svc = SubagentService()
    await svc.on_config_changed(
        {
            "enabled": True,
            "preamble": "CUSTOM PREAMBLE",
            "general_purpose_system_prompt": "CUSTOM GP PROMPT",
        }
    )
    assert svc._preamble == "CUSTOM PREAMBLE"
    assert svc._type_prompts["general-purpose"] == "CUSTOM GP PROMPT"


@pytest.mark.asyncio
async def test_start_binds_ai_chat_capability() -> None:
    svc = SubagentService()
    fake = _FakeAI()
    await svc.start(_resolver(ai_chat=fake))
    assert svc._ai is fake


@pytest.mark.asyncio
async def test_start_without_ai_chat_raises() -> None:
    svc = SubagentService()
    with pytest.raises(LookupError):
        await svc.start(_resolver())


class _FakeConfig:
    """Minimal ConfigurationReader returning a fixed subagent section."""

    def __init__(self, enabled: bool) -> None:
        self._section = {"enabled": enabled}

    def get(self, path: str) -> Any:
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return self._section

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        return self._section

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_start_restores_enabled_after_restart_reset() -> None:
    """ServiceManager.restart_service() sets _enabled=False before calling
    start(); start() must read config and restore it — otherwise a toggled-on
    subagent service silently exposes no tools (no /research) after the restart."""
    svc = SubagentService()
    svc._enabled = False  # exactly what restart_service does before start()
    fake = _FakeAI()
    await svc.start(_resolver(ai_chat=fake, configuration=_FakeConfig(enabled=True)))
    assert svc._enabled is True
    assert any(t.name == "deep_research" for t in svc.get_tools())


@pytest.mark.asyncio
async def test_start_with_disabled_config_leaves_service_off() -> None:
    svc = SubagentService()
    fake = _FakeAI()
    await svc.start(_resolver(ai_chat=fake, configuration=_FakeConfig(enabled=False)))
    assert svc._enabled is False
    assert svc.get_tools() == []


async def _started(text: str = "subagent result") -> tuple[SubagentService, _FakeAI]:
    svc = SubagentService()
    fake = _FakeAI(text)
    await svc.start(_resolver(ai_chat=fake))
    return svc, fake


@pytest.mark.asyncio
async def test_spawn_drives_chat_with_fresh_context_and_type_config() -> None:
    svc, fake = await _started("the report")
    ctx = UserContext(user_id="u1", email="u@x.com", display_name="U")

    out = await svc.spawn("general-purpose", "Research widgets", user_ctx=ctx)

    assert out == "the report"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # Fresh context: no parent conversation is threaded in.
    assert call["conversation_id"] is None
    # Caller identity is inherited (RBAC applies as for the caller).
    assert call["user_ctx"] is ctx
    # System prompt = shared preamble + the type's prompt.
    assert call["system_prompt"].startswith(_DEFAULT_PREAMBLE)
    assert "general-purpose subagent" in call["system_prompt"]
    # Profile + budget + usage tag come from the type.
    assert call["ai_profile"] == "standard"
    assert call["ai_call"] == "subagent.general-purpose"
    assert call["max_tool_rounds"] == 12
    assert call["user_message"] == "Research widgets"
    # Tagged so its ephemeral conversation stays out of the user's chat list.
    assert call["source"] == "subagent"


@pytest.mark.asyncio
async def test_spawn_unknown_type_raises() -> None:
    svc, _ = await _started()
    with pytest.raises(ValueError, match="Unknown agent type"):
        await svc.spawn("nope", "do a thing")


@pytest.mark.asyncio
async def test_spawn_before_start_raises() -> None:
    svc = SubagentService()
    with pytest.raises(RuntimeError, match="not started"):
        await svc.spawn("general-purpose", "do a thing")


@pytest.mark.asyncio
async def test_spawn_uses_configured_prompt_override() -> None:
    svc, fake = await _started()
    await svc.on_config_changed({"preamble": "PRE", "general_purpose_system_prompt": "GP-OVERRIDE"})
    await svc.spawn("general-purpose", "task")
    assert fake.calls[0]["system_prompt"] == "PRE\n\nGP-OVERRIDE"


@pytest.mark.asyncio
async def test_spawn_refuses_when_disabled() -> None:
    """An operator who disables the service stops new spawns immediately —
    the engine is defensive even before the (later-slice) tool layer gates it."""
    svc, fake = await _started()
    await svc.on_config_changed({"enabled": False})
    with pytest.raises(RuntimeError, match="disabled"):
        await svc.spawn("general-purpose", "task")
    assert fake.calls == []  # never reached the AI backend


@pytest.mark.asyncio
async def test_spawn_honors_explicitly_blanked_prompt() -> None:
    """A deliberately empty prompt override is honored, not silently reverted
    to the bundled default."""
    svc, fake = await _started()
    await svc.on_config_changed({"preamble": "", "general_purpose_system_prompt": ""})
    await svc.spawn("general-purpose", "task")
    assert fake.calls[0]["system_prompt"] == "\n\n"


def test_subagent_service_is_a_tool_provider() -> None:
    svc = SubagentService()
    assert isinstance(svc, ToolProvider)
    assert svc.tool_provider_name == "subagent"
    # Advertises ai_tools so AIService._discover_tools picks it up.
    assert "ai_tools" in svc.service_info().capabilities


def test_get_tools_exposes_spawn_agent_with_type_enum() -> None:
    tools = SubagentService().get_tools()
    spawn = next(t for t in tools if t.name == "spawn_agent")
    # Must be excluded from headless subagents (no nesting).
    assert spawn.interactive is True
    assert spawn.ai_visible is True
    # Every tool must declare its required_role (project RBAC-defaults rule).
    assert spawn.required_role == "user"
    agent_type_param = next(p for p in spawn.parameters if p.name == "agent_type")
    assert "general-purpose" in (agent_type_param.enum or [])
    assert any(p.name == "prompt" for p in spawn.parameters)


def test_get_tools_empty_when_disabled() -> None:
    svc = SubagentService()
    svc._enabled = False
    assert svc.get_tools() == []


@pytest.mark.asyncio
async def test_execute_spawn_agent_inherits_current_user() -> None:
    svc, fake = await _started("done")
    caller = UserContext(user_id="u9", email="u9@x.com", display_name="U9")
    token = _current_user.set(caller)
    try:
        out = await svc.execute_tool(
            "spawn_agent",
            {"agent_type": "general-purpose", "prompt": "go", "_user_id": "u9"},
        )
    finally:
        _current_user.reset(token)
    assert out == "done"
    # The spawned chat inherited the caller identity + ran headless.
    assert fake.calls[0]["user_ctx"] is caller
    assert fake.calls[0]["headless"] is True


@pytest.mark.asyncio
async def test_execute_unknown_tool_raises_keyerror() -> None:
    svc, _ = await _started()
    with pytest.raises(KeyError):
        await svc.execute_tool("nope", {})


@pytest.mark.asyncio
async def test_spawn_runs_headless() -> None:
    svc, fake = await _started()
    await svc.spawn("general-purpose", "task")
    assert fake.calls[0]["headless"] is True


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class _FakeEventBusProvider:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> _FakeBus:
        return self._bus


async def _started_with_bus(text: str = "done"):
    bus = _FakeBus()
    svc = SubagentService()
    fake = _FakeAI(text)
    await svc.start(_resolver(ai_chat=fake, event_bus=_FakeEventBusProvider(bus)))
    return svc, fake, bus


@pytest.mark.asyncio
async def test_spawn_emits_started_and_completed_events() -> None:
    from gilbert.interfaces.context import (
        _current_conversation_id,
        _current_user,
    )

    svc, _fake, bus = await _started_with_bus()
    caller = UserContext(user_id="u3", email="u3@x.com", display_name="U3")
    ut = _current_user.set(caller)
    ct = _current_conversation_id.set("conv-parent")
    try:
        await svc.spawn("general-purpose", "research X")
    finally:
        _current_user.reset(ut)
        _current_conversation_id.reset(ct)

    types = [e.event_type for e in bus.events]
    assert "chat.stream.subagent_started" in types
    assert "chat.stream.subagent_completed" in types
    started = next(e for e in bus.events if e.event_type == "chat.stream.subagent_started")
    # Routes to the PARENT conversation, visible only to the caller.
    assert started.data["conversation_id"] == "conv-parent"
    assert started.data["agent_type"] == "general-purpose"
    assert started.data["visible_to"] == ["u3"]
    assert "subagent_id" in started.data
    completed = next(e for e in bus.events if e.event_type == "chat.stream.subagent_completed")
    assert completed.data["subagent_id"] == started.data["subagent_id"]


@pytest.mark.asyncio
async def test_spawn_emits_failed_event_on_error() -> None:
    bus = _FakeBus()
    svc = SubagentService()

    class _BoomAI(_FakeAI):
        async def chat(self, *a: Any, **k: Any):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    await svc.start(_resolver(ai_chat=_BoomAI(), event_bus=_FakeEventBusProvider(bus)))
    with pytest.raises(RuntimeError, match="boom"):
        await svc.spawn("general-purpose", "task")
    types = [e.event_type for e in bus.events]
    assert "chat.stream.subagent_started" in types
    assert "chat.stream.subagent_failed" in types


@pytest.mark.asyncio
async def test_spawn_without_event_bus_still_works() -> None:
    # Events are best-effort: no event_bus capability -> spawn still returns.
    svc, _fake = await _started("ok")  # slice-1 helper, no bus
    assert await svc.spawn("general-purpose", "task") == "ok"


def test_get_tools_includes_deep_research_sugar() -> None:
    tools = SubagentService().get_tools()
    dr = next((t for t in tools if t.name == "deep_research"), None)
    assert dr is not None
    assert dr.slash_command == "research"
    assert dr.required_role == "user"
    # Sugar is also an orchestration tool: keep it out of headless subagents.
    assert dr.interactive is True
    assert any(p.name == "query" for p in dr.parameters)


@pytest.mark.asyncio
async def test_deep_research_tool_spawns_deep_research_type() -> None:
    fake = _FakeAI("the report")
    svc = SubagentService()
    # websearch capability present -> deep research is allowed.
    await svc.start(_resolver(ai_chat=fake, websearch=object()))
    out = await svc.execute_tool("deep_research", {"query": "what is X?"})
    assert out == "the report"
    assert fake.calls[0]["ai_profile"] == "deep-research"
    assert fake.calls[0]["ai_call"] == "subagent.deep-research"


@pytest.mark.asyncio
async def test_deep_research_tool_errors_without_web_search() -> None:
    fake = _FakeAI()
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake))  # no websearch capability
    out = await svc.execute_tool("deep_research", {"query": "x"})
    assert "web" in out.lower() and "search" in out.lower()
    assert fake.calls == []  # never spawned the subagent


@pytest.mark.asyncio
async def test_deep_research_tool_inherits_current_user() -> None:
    """The research subagent runs as the caller (RBAC), same as spawn_agent."""
    from gilbert.interfaces.context import _current_user

    fake = _FakeAI("report")
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake, websearch=object()))
    caller = UserContext(user_id="u7", email="u7@x.com", display_name="U7")
    tok = _current_user.set(caller)
    try:
        await svc.execute_tool("deep_research", {"query": "q"})
    finally:
        _current_user.reset(tok)
    assert fake.calls[0]["user_ctx"] is caller


@pytest.mark.asyncio
async def test_deep_research_empty_query_raises() -> None:
    svc, _ = await _started()
    with pytest.raises(ValueError, match="query"):
        await svc.execute_tool("deep_research", {"query": ""})
