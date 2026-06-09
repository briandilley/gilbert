"""Tests for the core SubagentService (engine)."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.services.subagent import _DEFAULT_PREAMBLE, SubagentService
from gilbert.interfaces.ai import AIProvider, ChatTurnResult
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.tools import ToolParameterType


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
