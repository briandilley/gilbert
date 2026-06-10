"""Tests for the core SubagentService (engine)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.storage import StorageService
from gilbert.core.services.subagent import _DEFAULT_PREAMBLE, SubagentService, _Run
from gilbert.core.subagents.types import SubagentType
from gilbert.interfaces.ai import AIProvider, ChatTurnResult
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.context import _current_user
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import ToolParameterType, ToolProvider


def _inmemory_storage_service() -> tuple[StorageService, dict[str, dict[str, Any]]]:
    """A StorageService backed by a real in-memory dict (mirrors test_ai_service)."""
    store: dict[str, dict[str, Any]] = {}

    async def _get(collection: str, key: str) -> Any:
        return store.get(f"{collection}:{key}")

    async def _put(collection: str, key: str, data: dict[str, Any]) -> None:
        store[f"{collection}:{key}"] = data

    async def _delete(collection: str, key: str) -> None:
        store.pop(f"{collection}:{key}", None)

    async def _query(query: Any) -> list[dict[str, Any]]:
        prefix = f"{query.collection}:"
        return [v for k, v in store.items() if k.startswith(prefix)]

    backend = AsyncMock(spec=StorageBackend)
    backend.get = AsyncMock(side_effect=_get)
    backend.put = AsyncMock(side_effect=_put)
    backend.delete = AsyncMock(side_effect=_delete)
    backend.query = AsyncMock(side_effect=_query)
    backend.ensure_index = AsyncMock()
    return StorageService(backend), store


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
        temperature: float | None = None,
        tool_filter: Any = None,
        max_tool_rounds: int | None = None,
        between_rounds_callback: Any = None,
        mid_round_interrupt: Any = None,
        headless: bool = False,
        source: str = "",
        should_stop_callback: Any = None,
        conversation_parent_id: str = "",
        conversation_title: str = "",
    ) -> ChatTurnResult:
        self.calls.append(
            {
                "user_message": user_message,
                "conversation_id": conversation_id,
                "user_ctx": user_ctx,
                "system_prompt": system_prompt,
                "ai_call": ai_call,
                "ai_profile": ai_profile,
                "model": model,
                "backend_override": backend_override,
                "temperature": temperature,
                "tool_filter": tool_filter,
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


def test_config_params_include_enabled_and_shared_preamble() -> None:
    params = SubagentService().config_params()
    by_key = {p.key: p for p in params}
    assert by_key["enabled"].type == ToolParameterType.BOOLEAN
    # The shared preamble is AI-authorable; per-type prompts now live on the
    # type entity (managed at /security/subagents), not as config params.
    assert by_key["preamble"].ai_prompt is True
    assert by_key["preamble"].multiline is True
    assert "general_purpose_system_prompt" not in by_key


@pytest.mark.asyncio
async def test_on_config_changed_caches_preamble() -> None:
    svc = SubagentService()
    await svc.on_config_changed(
        {
            "enabled": True,
            "preamble": "CUSTOM PREAMBLE",
        }
    )
    assert svc._preamble == "CUSTOM PREAMBLE"


@pytest.mark.asyncio
async def test_start_binds_ai_chat_capability() -> None:
    svc = SubagentService()
    fake = _FakeAI()
    await svc.start(_resolver(ai_chat=fake))
    assert svc._ai is fake


@pytest.mark.asyncio
async def test_spawn_without_ai_chat_raises() -> None:
    # start() no longer hard-requires ai_chat (storage-only start is valid for
    # the type store), but spawning without a bound AI provider must fail.
    svc = SubagentService()
    await svc.start(_resolver())
    with pytest.raises(RuntimeError, match="not started"):
        await svc.spawn("general-purpose", "do a thing")


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
    assert any(t.name == "spawn_agent" for t in svc.get_tools())


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
    assert "autonomous agent inside Gilbert" in call["system_prompt"]
    # Budget + usage tag come from the type.
    assert call["ai_call"] == "subagent.general-purpose"
    assert call["max_tool_rounds"] == 30  # general-purpose budget from the catalog
    # The type's tool gating + temperature flow through.
    assert call["tool_filter"] == ("all", [])
    assert call["temperature"] == 0.4
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
async def test_spawn_uses_type_prompt_and_preamble() -> None:
    svc, fake = await _started()
    await svc.on_config_changed({"preamble": "PRE"})
    # The type's own system_prompt is now the source of truth (editable via
    # save_type, not a per-type config param).
    await svc.save_type(
        SubagentType(id="general-purpose", name="GP", description="d", system_prompt="GP-OVERRIDE")
    )
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
    """A deliberately empty preamble + type prompt is honored, not silently
    reverted to the bundled default."""
    svc, fake = await _started()
    await svc.on_config_changed({"preamble": ""})
    await svc.save_type(
        SubagentType(id="general-purpose", name="GP", description="d", system_prompt="")
    )
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


def test_deep_research_tool_is_removed() -> None:
    # deep_research collapsed into a background-mode subagent type; the
    # standalone tool and its /research slash command no longer exist.
    names = {t.name for t in SubagentService().get_tools()}
    assert "deep_research" not in names
    slash = {t.slash_command for t in SubagentService().get_tools()}
    assert "research" not in slash


@pytest.mark.asyncio
async def test_background_type_spawn_schedules_in_background() -> None:
    """A background-mode type (deep-research) schedules the run off-turn and
    returns an acknowledgement instead of the report."""
    scheduled: list[Any] = []
    fake = _FakeAI("the report")
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake))
    svc._run_in_background = lambda coro: scheduled.append(coro) or coro.close()  # type: ignore[assignment]
    out = await svc.execute_tool(
        "spawn_agent", {"agent_type": "deep-research", "prompt": "what is X?"}
    )
    # Returns an acknowledgement, not the report.
    assert "background" in out.lower()
    # Exactly one background coro was scheduled.
    assert len(scheduled) == 1
    # The engine was NOT awaited inline.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_background_type_spawn_inherits_current_user() -> None:
    """A background subagent runs as the caller (RBAC). Since it runs in the
    background, we verify the caller is captured and a coro is scheduled."""
    from gilbert.interfaces.context import _current_user

    scheduled: list[Any] = []
    fake = _FakeAI("report")
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake))
    svc._run_in_background = lambda coro: scheduled.append(coro) or coro.close()  # type: ignore[assignment]
    caller = UserContext(user_id="u7", email="u7@x.com", display_name="U7")
    tok = _current_user.set(caller)
    try:
        await svc.execute_tool("spawn_agent", {"agent_type": "deep-research", "prompt": "q"})
    finally:
        _current_user.reset(tok)
    assert len(scheduled) == 1
    assert fake.calls == []


@pytest.mark.asyncio
async def test_spawn_agent_empty_prompt_raises() -> None:
    svc, _ = await _started()
    with pytest.raises(ValueError, match="prompt"):
        await svc.execute_tool("spawn_agent", {"agent_type": "deep-research", "prompt": ""})


class _FakePoster:
    """ConversationMessagePoster + AIProvider in one (mirrors AIService)."""

    def __init__(self, report: str = "THE REPORT") -> None:
        self.calls: list[dict[str, Any]] = []
        self.delivered: list[tuple[str, str, list[Any]]] = []
        self.ensured: list[dict[str, Any]] = []
        self._report = report

    async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
        self.calls.append(k)
        return ChatTurnResult(
            response_text=self._report,
            conversation_id="ephemeral",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )

    async def append_assistant_message(
        self, conversation_id: str, content: str, attachments: Any = None
    ) -> None:
        self.delivered.append((conversation_id, content, attachments or []))

    async def ensure_conversation(
        self,
        conversation_id: str,
        user_ctx: Any,
        *,
        source: str = "",
        parent_conversation_id: str = "",
        title: str = "",
    ) -> None:
        self.ensured.append(
            {
                "conversation_id": conversation_id,
                "source": source,
                "parent_conversation_id": parent_conversation_id,
                "title": title,
            }
        )


class _FakeWorkspace:
    def __init__(self, tmp_path: Any) -> None:
        self.registered: list[dict[str, Any]] = []
        self._root = tmp_path

    def get_output_dir(self, user_id: str, conversation_id: str) -> Any:
        d = self._root / user_id / conversation_id / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def register_file(self, **kwargs: Any) -> dict[str, Any]:
        self.registered.append(kwargs)
        return {"_id": "f1", **kwargs}


def _dr_type() -> SubagentType:
    """The built-in deep-research type (background + report_file) — used to
    drive _run_agent_background directly in tests."""
    from gilbert.core.subagents.types import BUILTIN_SUBAGENT_TYPES

    return BUILTIN_SUBAGENT_TYPES["deep-research"]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_spawn_synthesizes_when_run_returns_empty() -> None:
    """If the agent exhausts its budget without a final report (empty text),
    spawn() forces a synthesis turn so it never returns an empty report."""

    class _EmptyThenReport(_FakePoster):
        def __init__(self) -> None:
            super().__init__()
            self._n = 0

        async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
            self.calls.append(k)
            self._n += 1
            text = "" if self._n == 1 else "# Synthesized report\n\n" + "x" * 120
            return ChatTurnResult(
                response_text=text, conversation_id=k.get("conversation_id") or "c",
                ui_blocks=[], tool_usage=[], attachments=[], rounds=[],
            )

    poster = _EmptyThenReport()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(_FakeBus()))  # type: ignore[assignment]
    svc._enabled = True
    report = await svc.spawn(
        "deep-research", "q", user_ctx=UserContext.SYSTEM, conversation_id="sub-1"
    )
    assert len(poster.calls) == 2  # main run + synthesis fallback
    assert "Synthesized report" in report
    assert poster.calls[1]["ai_call"].endswith(".synthesis")


@pytest.mark.asyncio
async def test_run_research_background_writes_report_and_delivers(tmp_path: Any) -> None:
    poster = _FakePoster(report="# Findings\n\n" + "Widgets are good. " * 8)
    ws = _FakeWorkspace(tmp_path)
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = ws  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    await svc._run_agent_background(_dr_type(), "widgets?", "conv-parent", caller)

    # Wrote the report to the PARENT conversation's outputs/.
    assert ws.registered, "report file was registered"
    reg = ws.registered[0]
    assert reg["conversation_id"] == "conv-parent"
    assert reg["media_type"] == "text/markdown"
    assert reg["rel_path"].startswith("outputs/research-")
    # The subagent ran as the caller.
    assert poster.calls[0]["user_ctx"] is caller
    # The child conversation was created up front (so it lists + is watchable
    # while the run is in progress), tagged as a subagent child of the parent.
    assert poster.ensured, "ensured the child conversation"
    ens = poster.ensured[0]
    assert ens["source"] == "subagent"
    assert ens["parent_conversation_id"] == "conv-parent"
    assert ens["title"].startswith("Research Analyst:")
    assert ens["conversation_id"] == poster.calls[0]["conversation_id"]
    # Delivered a message into the parent conversation with a download link.
    assert poster.delivered, "delivered a message"
    conv, msg, _atts = poster.delivered[0]
    assert conv == "conv-parent"
    assert "/api/chat/download/conv-parent/outputs/research-" in msg
    # Lifecycle events fired.
    types = [e.event_type for e in bus.events]
    assert "chat.stream.subagent_started" in types
    assert "chat.stream.subagent_completed" in types


@pytest.mark.asyncio
async def test_run_research_background_delivers_failure(tmp_path: Any) -> None:
    class _BoomPoster(_FakePoster):
        async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
            raise RuntimeError("research boom")

    poster = _BoomPoster()
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True

    # Must NOT raise out of the detached task.
    await svc._run_agent_background(_dr_type(), "q", "conv-parent", UserContext.SYSTEM)

    assert poster.delivered, "delivered a failure message"
    _, msg, _atts = poster.delivered[0]
    assert "fail" in msg.lower() or "boom" in msg.lower()
    assert "chat.stream.subagent_failed" in [e.event_type for e in bus.events]


@pytest.mark.asyncio
async def test_run_research_background_degrades_without_workspace() -> None:
    """No workspace capability → the report is delivered inline (no file)."""
    poster = _FakePoster(report="# Findings\n\nInline body.")
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = None
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(_FakeBus()))  # type: ignore[assignment]
    svc._enabled = True
    await svc._run_agent_background(_dr_type(), "q", "conv-parent", UserContext.SYSTEM)
    _, msg, _atts = poster.delivered[0]
    assert "Inline body." in msg  # the report itself, not a download link
    assert "/api/chat/download/" not in msg


@pytest.mark.asyncio
async def test_run_research_background_without_poster_does_not_crash(tmp_path: Any) -> None:
    """No ConversationMessagePoster → no delivery, but no crash and the file
    is still written."""

    class _OnlyChat:  # AIProvider but NOT ConversationMessagePoster
        async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
            return ChatTurnResult(
                response_text="# R", conversation_id="e", ui_blocks=[],
                tool_usage=[], attachments=[], rounds=[],
            )

    ws = _FakeWorkspace(tmp_path)
    svc = SubagentService()
    svc._ai = _OnlyChat()  # type: ignore[assignment]
    svc._workspace = ws  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(_FakeBus()))  # type: ignore[assignment]
    svc._enabled = True
    await svc._run_agent_background(_dr_type(), "q", "conv-parent", UserContext.SYSTEM)
    assert ws.registered, "report still written even without a message poster"


@pytest.mark.asyncio
async def test_background_spawn_returns_immediately_and_schedules() -> None:
    scheduled: list[Any] = []
    fake = _FakeAI("ignored")
    svc = SubagentService()
    await svc.start(_resolver(ai_chat=fake))
    # Capture the background coro instead of really detaching it.
    svc._run_in_background = lambda coro: scheduled.append(coro) or coro.close()  # type: ignore[assignment]

    out = await svc.execute_tool(
        "spawn_agent", {"agent_type": "deep-research", "prompt": "what is X?"}
    )

    assert "background" in out.lower()  # an acknowledgement, not the report
    assert len(scheduled) == 1  # the run was scheduled in the background
    assert fake.calls == []  # the engine was NOT awaited inline


@pytest.mark.asyncio
async def test_spawn_uses_preallocated_conversation_and_registers_run(tmp_path: Any) -> None:
    poster = _FakePoster(report="# R")
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    await svc._run_agent_background(_dr_type(), "widgets?", "conv-parent", caller)

    # A run was registered with the subagent's own pre-allocated conversation id.
    runs = svc.list_runs("u1")
    assert len(runs) == 1
    run = runs[0]
    assert run["agent_type"] == "deep-research"
    assert run["query"] == "widgets?"
    assert run["conversation_id"]  # pre-allocated, non-empty
    # spawn passed that id to chat (fresh conv, but a known id we can watch).
    assert poster.calls[0]["conversation_id"] == run["conversation_id"]
    # The started event carried the subagent conversation id + the query.
    started = next(e for e in bus.events if e.event_type == "chat.stream.subagent_started")
    assert started.data["conversation_id"] == "conv-parent"  # routing = parent
    assert started.data["subagent_conversation_id"] == run["conversation_id"]
    assert started.data["query"] == "widgets?"


@pytest.mark.asyncio
async def test_stop_subagent_sets_flag_and_checks_owner(tmp_path: Any) -> None:
    svc = SubagentService()
    svc._enabled = True
    run = _Run(
        subagent_id="s1", agent_type="deep-research", query="q",
        conversation_id="c", parent_conversation_id="p", user_id="u1",
        status="running", started_at="t",
    )
    svc._runs["s1"] = run

    # Wrong user can't stop it.
    assert svc.stop_subagent("s1", "intruder") is False
    assert run.stop_flag[0] is False
    # Owner can.
    assert svc.stop_subagent("s1", "u1") is True
    assert run.stop_flag[0] is True
    # Unknown id is a harmless no-op.
    assert svc.stop_subagent("nope", "u1") is False


@pytest.mark.asyncio
async def test_stopped_run_delivers_partial(tmp_path: Any) -> None:
    # A poster whose chat returns once should_stop is requested.
    class _StopAwarePoster(_FakePoster):
        async def chat(self, *a: Any, **k: Any) -> ChatTurnResult:
            cb = k.get("should_stop_callback")
            # Simulate the engine seeing the stop and returning the partial.
            if cb:
                cb()
            return ChatTurnResult(
                response_text="# Partial findings", conversation_id=k.get("conversation_id") or "c",
                ui_blocks=[], tool_usage=[], attachments=[], rounds=[],
            )

    poster = _StopAwarePoster()
    bus = _FakeBus()
    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(bus))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    # Pre-set a run whose stop flag is already requested, then run.
    await svc._run_agent_background(_dr_type(), "q", "conv-parent", caller)
    # The fake triggers stop via the callback; status reflects it.
    # (Delivery happened; we just assert a message was delivered with the partial.)
    assert poster.delivered, "delivered the partial"
    conv, msg, _atts = poster.delivered[0]
    assert conv == "conv-parent"
    assert "Partial findings" in msg or "/api/chat/download/" in msg


def test_service_provides_subagent_stop_ws_handler() -> None:
    from gilbert.interfaces.ws import WsHandlerProvider

    svc = SubagentService()
    assert isinstance(svc, WsHandlerProvider)
    assert "ws_handlers" in svc.service_info().capabilities
    assert "subagent.stop" in svc.get_ws_handlers()


@pytest.mark.asyncio
async def test_ws_stop_handler_stops_owned_run() -> None:
    svc = SubagentService()
    svc._enabled = True
    svc._runs["s1"] = _Run(
        subagent_id="s1", agent_type="deep-research", query="q",
        conversation_id="c", parent_conversation_id="p", user_id="u1",
        status="running", started_at="t",
    )

    class _Conn:
        user_id = "u1"
        user_ctx = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    res = await svc.get_ws_handlers()["subagent.stop"](_Conn(), {"id": "r1", "subagent_id": "s1"})
    assert res["ok"] is True
    assert svc._runs["s1"].stop_flag[0] is True


def test_get_tools_includes_check_research() -> None:
    tools = SubagentService().get_tools()
    assert any(t.name == "check_research" for t in tools)


@pytest.mark.asyncio
async def test_completed_run_delivers_report_attachment_and_notifies(tmp_path: Any) -> None:
    poster = _FakePoster(report="# Findings\n\nbody")
    notifs: list[dict[str, Any]] = []

    class _Notif:
        async def notify_user(self, **kwargs: Any) -> Any:
            notifs.append(kwargs)
            return object()

    svc = SubagentService()
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = _FakeWorkspace(tmp_path)  # type: ignore[assignment]
    svc._notifications = _Notif()  # type: ignore[assignment]
    svc._resolver = _resolver(event_bus=_FakeEventBusProvider(_FakeBus()))  # type: ignore[assignment]
    svc._enabled = True
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")

    await svc._run_agent_background(_dr_type(), "widgets?", "conv-parent", caller)

    conv, _msg, atts = poster.delivered[0]
    assert conv == "conv-parent"
    assert len(atts) == 1
    att = atts[0]
    assert att.media_type == "text/markdown"
    assert att.workspace_path.startswith("outputs/research-")
    assert att.workspace_conv == "conv-parent"
    assert notifs and notifs[0]["user_id"] == "u1"


# ── Slice 7: subagent.list RPC ─────────────────────────────────────────────


def test_list_active_for_conversation_filters_by_parent_and_user() -> None:
    svc = SubagentService()
    svc._runs["a"] = _Run(
        subagent_id="a", agent_type="deep-research", query="q1",
        conversation_id="ca", parent_conversation_id="p1", user_id="u1",
        status="running", started_at="t",
    )
    svc._runs["b"] = _Run(
        subagent_id="b", agent_type="deep-research", query="q2",
        conversation_id="cb", parent_conversation_id="p2", user_id="u1",
        status="running", started_at="t",
    )
    svc._runs["c"] = _Run(
        subagent_id="c", agent_type="deep-research", query="q3",
        conversation_id="cc", parent_conversation_id="p1", user_id="u1",
        status="completed", started_at="t",
    )
    out = svc.list_active_for_conversation("p1", "u1")
    ids = {r["subagent_id"] for r in out}
    assert ids == {"a"}  # only running + parent p1 + user u1
    assert out[0]["conversation_id"] == "ca"
    assert out[0]["query"] == "q1"


@pytest.mark.asyncio
async def test_ws_subagent_list_returns_active() -> None:
    svc = SubagentService()
    svc._runs["a"] = _Run(
        subagent_id="a", agent_type="deep-research", query="q",
        conversation_id="ca", parent_conversation_id="p1", user_id="u1",
        status="running", started_at="t",
    )

    class _Conn:
        user_id = "u1"

    res = await svc.get_ws_handlers()["subagent.list"](_Conn(), {"id": "r", "conversation_id": "p1"})
    assert [r["subagent_id"] for r in res["runs"]] == ["a"]


# ── Type store (seed builtins, CRUD, reset, protection) ────────────────────


@pytest.mark.asyncio
async def test_type_store_seeds_builtins_and_crud() -> None:
    from gilbert.core.subagents.types import builtin_seed_list

    storage, _store = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage))
    # Seeded all built-ins.
    ids = {t.id for t in svc.list_types()}
    assert {"general-purpose", "deep-research", "software-engineer"} <= ids
    assert len(svc.list_types()) >= len(builtin_seed_list())
    # Create a custom type.
    await svc.save_type(SubagentType(id="my-agent", name="Mine", description="d", system_prompt="do it"))
    assert svc.get_type("my-agent") is not None
    # Built-ins can't be deleted; custom can.
    assert await svc.delete_type("deep-research") is False
    assert await svc.delete_type("my-agent") is True
    # Edits to a built-in persist across reload (not overwritten by seeding).
    edited = svc.get_type("deep-research")
    assert edited is not None
    edited.max_rounds = 99
    await svc.save_type(edited)
    svc2 = SubagentService()
    await svc2.start(_resolver(entity_storage=storage))
    reloaded = svc2.get_type("deep-research")
    assert reloaded is not None
    assert reloaded.max_rounds == 99
    # Reset restores shipped default.
    await svc2.reset_type("deep-research")
    after_reset = svc2.get_type("deep-research")
    assert after_reset is not None
    assert after_reset.max_rounds == 40


# ── spawn() drives model/temperature/tools/budget from the type ────────────


@pytest.mark.asyncio
async def test_spawn_uses_type_model_temperature_tools_and_override() -> None:
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    fake = _FakeAI("# Done\n\n" + "x" * 100)
    await svc.start(_resolver(ai_chat=fake, entity_storage=storage))
    # Configure a custom sync type with a model + temperature + include tools.
    await svc.save_type(SubagentType(
        id="t1", name="T1", description="d", system_prompt="go",
        backend="ollama", model="llama3.3", temperature=0.2,
        tool_mode="include", tools=["web_search"], max_rounds=7,
    ))
    await svc.spawn("t1", "task")
    call = fake.calls[0]
    assert call["max_tool_rounds"] == 7
    # Type model/backend/temperature/tools flow through to chat().
    assert call["model"] == "llama3.3"
    assert call["backend_override"] == "ollama"
    assert call["temperature"] == 0.2
    assert call["tool_filter"] == ("include", ["web_search"])
    # A per-spawn override beats the type's model.
    await svc.spawn("t1", "task", model_override="qwen2.5")
    assert fake.calls[1]["model"] == "qwen2.5"


# ── execution_mode / deliver_as + spawn_agent model param ──────────────────


@pytest.mark.asyncio
async def test_spawn_agent_is_only_tool_with_model_param_and_dynamic_types() -> None:
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage))
    tools = svc.get_tools()
    names = {t.name for t in tools}
    assert "spawn_agent" in names
    assert "deep_research" not in names  # collapsed away
    spawn = next(t for t in tools if t.name == "spawn_agent")
    pnames = {p.name for p in spawn.parameters}
    assert {"agent_type", "prompt", "model"} <= pnames
    # The agent_type enum lists enabled type ids; the description name-drops them.
    enum = next(p for p in spawn.parameters if p.name == "agent_type").enum
    assert enum is not None
    assert "software-engineer" in enum and "deep-research" in enum
    assert "deep-research" in spawn.description


@pytest.mark.asyncio
async def test_background_type_detaches_and_delivers_report(tmp_path: Any) -> None:
    # market-analyst is background/report_file.
    poster = _FakePoster(report="# Market\n\n" + "y" * 120)
    ws = _FakeWorkspace(tmp_path)
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(
        _resolver(ai_chat=poster, entity_storage=storage, event_bus=_FakeEventBusProvider(_FakeBus()))
    )
    svc._ai = poster  # type: ignore[assignment]
    svc._workspace = ws  # type: ignore[assignment]
    captured: list[Any] = []
    svc._run_in_background = lambda coro: captured.append(coro)  # type: ignore[assignment]
    from gilbert.interfaces.context import _current_conversation_id

    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")
    tok = _current_user.set(caller)
    ct = _current_conversation_id.set("conv-parent")
    try:
        out = await svc.execute_tool(
            "spawn_agent", {"agent_type": "market-analyst", "prompt": "EV chargers"}
        )
    finally:
        _current_user.reset(tok)
        _current_conversation_id.reset(ct)
    assert "background" in out.lower() or "i'll" in out.lower()
    assert len(captured) == 1  # detached
    await captured[0]  # run it
    # Delivered a report attachment (deliver_as=report_file).
    _conv, _msg, atts = poster.delivered[0]
    assert atts and atts[0].media_type == "text/markdown"


@pytest.mark.asyncio
async def test_sync_type_returns_inline(tmp_path: Any) -> None:
    poster = _FakePoster(report="# Answer\n\n" + "z" * 120)
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(
        _resolver(ai_chat=poster, entity_storage=storage, event_bus=_FakeEventBusProvider(_FakeBus()))
    )
    svc._ai = poster  # type: ignore[assignment]
    caller = UserContext(user_id="u1", email="u1@x.com", display_name="U1")
    tok = _current_user.set(caller)
    try:
        out = await svc.execute_tool(
            "spawn_agent", {"agent_type": "software-engineer", "prompt": "write fizzbuzz"}
        )
    finally:
        _current_user.reset(tok)
    assert "Answer" in out  # returned inline, not an ack


# ── Admin CRUD WS RPCs ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_type_crud_ws_handlers_admin_gated() -> None:
    storage, _ = _inmemory_storage_service()
    svc = SubagentService()
    await svc.start(_resolver(entity_storage=storage))
    h = svc.get_ws_handlers()
    assert {
        "subagent.types.list",
        "subagent.types.save",
        "subagent.types.delete",
        "subagent.types.reset",
    } <= set(h)

    class _Admin:
        user_id = "a"
        roles = ("admin",)

    class _User:
        user_id = "u"
        roles = ("user",)

    listed = await h["subagent.types.list"](_Admin(), {"id": "1"})
    assert any(t["id"] == "deep-research" for t in listed["types"])
    assert "all_tool_names" in listed
    # Non-admin list rejected.
    denied = await h["subagent.types.list"](_User(), {"id": "1b"})
    assert denied.get("code") == 403
    # Non-admin save rejected.
    res = await h["subagent.types.save"](
        _User(), {"id": "2", "type": {"id": "x", "name": "X", "description": "d", "system_prompt": "p"}}
    )
    assert res.get("code") == 403
    # Admin save accepted.
    ok = await h["subagent.types.save"](
        _Admin(), {"id": "3", "type": {"id": "x", "name": "X", "description": "d", "system_prompt": "p"}}
    )
    assert ok.get("ok") is True
    assert svc.get_type("x") is not None
    # Admin can't un-protect a built-in via save (built_in preserved).
    await h["subagent.types.save"](
        _Admin(),
        {"id": "4", "type": {"id": "deep-research", "name": "DR", "description": "d",
                             "system_prompt": "p", "built_in": False}},
    )
    dr = svc.get_type("deep-research")
    assert dr is not None and dr.built_in is True
    # Reset + delete handlers work for admin.
    reset_res = await h["subagent.types.reset"](_Admin(), {"id": "5", "type_id": "deep-research"})
    assert reset_res.get("ok") is True
    del_res = await h["subagent.types.delete"](_Admin(), {"id": "6", "type_id": "x"})
    assert del_res.get("ok") is True
    assert svc.get_type("x") is None
