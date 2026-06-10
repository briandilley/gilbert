"""The shared single-agent run engine. See package docstring for rationale."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gilbert.interfaces.ai import AIProvider, ChatTurnResult
from gilbert.interfaces.auth import UserContext

logger = logging.getLogger(__name__)

# Async callable the engine invokes to publish a lifecycle event. The caller
# supplies a closure that merges its own routing (parent conversation +
# audience) into the engine-supplied event payload. ``None`` disables events.
OnEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class RunSpec:
    """A fully-resolved description of one single-agent run.

    Dumb data only: every value is already resolved (profile/model/tools/
    budgets/prompt). The engine maps these straight onto ``AIProvider.chat``.
    """

    system_prompt: str
    user_message: str = ""
    ai_profile: str = ""
    model: str = ""
    backend_override: str = ""
    temperature: float | None = None
    tool_filter: tuple[str, list[str]] | None = None
    max_rounds: int | None = None
    max_wall_clock_s: float | None = None
    headless: bool = False
    ai_call: str = ""
    source: str = ""
    # Identifies the run in lifecycle events (subagent type id, or agent id).
    agent_type: str = ""
    between_rounds_callback: Any = None
    mid_round_interrupt: Callable[[], bool] | None = None
    should_stop_callback: Callable[[], bool] | None = None
    conversation_parent_id: str = ""
    conversation_title: str = ""
    # When True, an empty/near-empty result with a persisted conversation
    # triggers one synthesis turn (subagent path). Durable runs leave it off.
    synthesize_on_empty: bool = False


@dataclass
class RunResult:
    """Outcome of an engine run."""

    text: str
    chat_result: ChatTurnResult | None
    was_stopped: bool


class AgentRunEngine:
    """Runs one ``RunSpec`` against an ``AIProvider``."""

    async def run(
        self,
        spec: RunSpec,
        *,
        ai: AIProvider,
        user_ctx: UserContext | None = None,
        conversation_id: str | None = None,
        subagent_id: str = "",
        on_event: OnEvent | None = None,
    ) -> RunResult:
        """Drive one chat turn from ``spec`` and return the final text + usage.

        ``conversation_id`` is the pre-allocated (watchable) conversation id, or
        ``None`` for a fresh detached turn. ``subagent_id`` correlates lifecycle
        events; one is generated if absent. ``on_event`` publishes lifecycle
        events (caller merges routing); ``None`` disables them.
        """
        subagent_id = subagent_id or uuid.uuid4().hex

        # Fold the wall-clock budget into the stop check: chat() checks
        # should_stop between rounds, so an expired deadline ends the run
        # gracefully (keeping the partial + synthesis), exactly like a user
        # Stop. Combines with any caller-provided stop.
        caller_stop = spec.should_stop_callback
        should_stop = caller_stop
        if spec.max_wall_clock_s is not None:
            deadline = time.monotonic() + spec.max_wall_clock_s

            def should_stop() -> bool:  # noqa: F811 — wraps the caller's stop
                if caller_stop is not None and caller_stop():
                    return True
                return time.monotonic() >= deadline

        await self._emit(
            on_event,
            "chat.stream.subagent_started",
            {
                "subagent_id": subagent_id,
                "agent_type": spec.agent_type,
                "subagent_conversation_id": conversation_id,
                "query": spec.user_message,
            },
        )

        try:
            result = await ai.chat(
                user_message=spec.user_message,
                conversation_id=conversation_id,
                user_ctx=user_ctx,
                system_prompt=spec.system_prompt,
                ai_call=spec.ai_call or None,
                model=spec.model,
                backend_override=spec.backend_override,
                ai_profile=spec.ai_profile,
                temperature=spec.temperature,
                tool_filter=spec.tool_filter,
                max_tool_rounds=spec.max_rounds,
                between_rounds_callback=spec.between_rounds_callback,
                mid_round_interrupt=spec.mid_round_interrupt,
                headless=spec.headless,
                should_stop_callback=should_stop,
                source=spec.source,
                conversation_parent_id=spec.conversation_parent_id,
                conversation_title=spec.conversation_title,
            )
        except Exception as exc:
            await self._emit(
                on_event,
                "chat.stream.subagent_failed",
                {"subagent_id": subagent_id, "agent_type": spec.agent_type, "reason": str(exc)},
            )
            raise

        was_stopped = should_stop is not None and bool(should_stop())
        report = result.response_text

        # Budget-exhaustion guard: if the agent used up its rounds mid-tool-use
        # and never wrote a final answer (empty/near-empty text), force one
        # synthesis turn so we never return an empty "report". Only possible
        # when the run has a persisted conversation to reload its findings from.
        if spec.synthesize_on_empty and not was_stopped and len(report.strip()) < 80 and conversation_id:
            try:
                synth = await ai.chat(
                    user_message=(
                        "You've reached your step limit. Do NOT call any "
                        "more tools. Using everything you have already gathered in "
                        "this conversation, write your COMPLETE final answer now — "
                        "a thorough, well-structured Markdown report that directly "
                        "answers the original task, with citations where relevant."
                    ),
                    conversation_id=conversation_id,
                    user_ctx=user_ctx,
                    system_prompt=spec.system_prompt,
                    ai_call=(f"{spec.ai_call}.synthesis" if spec.ai_call else None),
                    model=spec.model,
                    backend_override=spec.backend_override,
                    ai_profile=spec.ai_profile,
                    temperature=spec.temperature,
                    tool_filter=spec.tool_filter,
                    max_tool_rounds=2,
                    headless=spec.headless,
                    source=spec.source,
                    conversation_parent_id=spec.conversation_parent_id,
                    conversation_title=spec.conversation_title,
                )
                if synth.response_text.strip():
                    report = synth.response_text
            except Exception:
                logger.exception("subagent synthesis fallback failed")

        # A graceful stop returns normally with the partial — emit the distinct
        # "stopped" terminal event so the UI can label it (both are terminal).
        await self._emit(
            on_event,
            "chat.stream.subagent_stopped" if was_stopped else "chat.stream.subagent_completed",
            {"subagent_id": subagent_id, "agent_type": spec.agent_type},
        )
        return RunResult(text=report, chat_result=result, was_stopped=was_stopped)

    @staticmethod
    async def _emit(on_event: OnEvent | None, event_type: str, payload: dict[str, Any]) -> None:
        if on_event is None:
            return
        await on_event(event_type, payload)
