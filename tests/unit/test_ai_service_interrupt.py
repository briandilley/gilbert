"""Phase 3 — AIService.chat ``mid_round_interrupt`` callback.

Focused tests that exercise ``AIService._execute_tool_calls`` directly
with a small ``tools_by_name`` dict and a list of three sequential
``ToolCall``s. The interrupt boundary is between tool-call groups, so
running three serial tool calls produces three groups; a callback that
returns ``True`` after the first call resolves should stub-out calls
2 and 3.

Driving ``chat()`` with a multi-tool-call mock is significantly heavier
(needs a stub backend that emits multi-tool responses across rounds);
the ``_execute_tool_calls``-level test verifies the exact contract the
plan calls out.
"""

from __future__ import annotations

from typing import Any

from gilbert.core.services.ai import AIService
from gilbert.interfaces.service import Service, ServiceInfo
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
)


# ── Helpers ──────────────────────────────────────────────────────────


class _CountingToolProvider(Service):
    """Minimal ToolProvider that records each invocation in ``calls``.

    All tools are ``parallel_safe=False`` so each lands in its own group
    — so the interrupt-between-groups check fires between every call.
    """

    def __init__(self, names: list[str]) -> None:
        self._tools = [
            ToolDefinition(name=n, description=f"tool {n}", parallel_safe=False)
            for n in names
        ]
        self.calls: list[str] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="counter", capabilities=frozenset({"ai_tools"}))

    @property
    def tool_provider_name(self) -> str:
        return "counter"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        return list(self._tools)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        return f"result-{name}"


def _tools_by_name(
    provider: _CountingToolProvider,
) -> dict[str, tuple[Any, ToolDefinition]]:
    return {t.name: (provider, t) for t in provider.get_tools()}


def _make_ai_service() -> AIService:
    """A bare AIService — _execute_tool_calls only needs the instance.

    No backends, no storage; the method's only direct external
    dependency is ``get_current_conversation_id`` (returns None
    outside a chat context, which is fine — the room-members lookup
    is skipped).
    """
    svc = AIService()
    svc._backends = {}
    svc._enabled = True
    return svc


def _three_calls() -> list[ToolCall]:
    return [
        ToolCall(tool_call_id="tc_1", tool_name="a", arguments={}),
        ToolCall(tool_call_id="tc_2", tool_name="b", arguments={}),
        ToolCall(tool_call_id="tc_3", tool_name="c", arguments={}),
    ]


# ── Tests ────────────────────────────────────────────────────────────


async def test_mid_round_interrupt_skips_remaining_groups() -> None:
    """First group runs normally; callback returns True before group 2;
    calls 2 and 3 receive stub ToolResults preserving order, the
    underlying tool's executor is never invoked for them."""
    svc = _make_ai_service()
    provider = _CountingToolProvider(["a", "b", "c"])

    interrupt_state = {"trip": False}

    def _interrupt() -> bool:
        # Trip the interrupt the moment the first call has resolved.
        # ``mid_round_interrupt`` is only consulted starting at group 1
        # so this still lets group 0 run.
        return interrupt_state["trip"]

    # Sentinel: the first tool's handler flips the flag so the next
    # boundary check trips. Since tools are not parallel_safe, each
    # call is its own group.
    original_execute = provider.execute_tool

    async def _execute(name: str, arguments: dict[str, Any]) -> str:
        out = await original_execute(name, arguments)
        if name == "a":
            interrupt_state["trip"] = True
        return out

    provider.execute_tool = _execute  # type: ignore[method-assign]

    calls = _three_calls()
    results, _ui = await svc._execute_tool_calls(
        calls,
        _tools_by_name(provider),
        mid_round_interrupt=_interrupt,
    )

    # All three calls produced a result row, in the original order.
    assert len(results) == len(calls)
    assert [r.tool_call_id for r in results] == ["tc_1", "tc_2", "tc_3"]

    # Only the first tool actually ran.
    assert provider.calls == ["a"]

    # Calls 2 and 3 carry the stub interrupt content.
    assert results[0].content == "result-a"
    assert "skipped" in results[1].content
    assert "urgent interrupt" in results[1].content
    assert "skipped" in results[2].content
    # Stub rows are not flagged as errors.
    assert results[1].is_error is False
    assert results[2].is_error is False


async def test_no_interrupt_when_callback_absent() -> None:
    """``mid_round_interrupt=None`` → behavior identical to prior
    phases. Every tool call runs."""
    svc = _make_ai_service()
    provider = _CountingToolProvider(["a", "b", "c"])

    calls = _three_calls()
    results, _ui = await svc._execute_tool_calls(
        calls,
        _tools_by_name(provider),
        mid_round_interrupt=None,
    )

    assert provider.calls == ["a", "b", "c"]
    assert [r.content for r in results] == [
        "result-a",
        "result-b",
        "result-c",
    ]
    assert all(r.is_error is False for r in results)


async def test_no_interrupt_when_callback_returns_false() -> None:
    """A callback that always returns False is functionally identical
    to no callback at all."""
    svc = _make_ai_service()
    provider = _CountingToolProvider(["a", "b", "c"])

    check_count = {"n": 0}

    def _interrupt() -> bool:
        check_count["n"] += 1
        return False

    calls = _three_calls()
    results, _ui = await svc._execute_tool_calls(
        calls,
        _tools_by_name(provider),
        mid_round_interrupt=_interrupt,
    )

    assert provider.calls == ["a", "b", "c"]
    assert [r.content for r in results] == [
        "result-a",
        "result-b",
        "result-c",
    ]
    # Callback consulted between groups only (group 0 skips it),
    # i.e. before groups 1 and 2 — twice for 3 serial groups.
    assert check_count["n"] == 2
